#!/usr/bin/env python3
"""vprunners.py -- the three harness runners for the Voice Pod v12 control layer.

Plan v12 file 03: K-01 (runner abstraction), K-02 (FINDINGS from a text fence),
K-03 (provider-error classification from the OpenCode server log), K-04
(export to a file, never a pipe), K-05 (Node 22 first on PATH; per-server
XDG_DATA_HOME), K-08 (turn timeout, usage), K-12 (--pure), K-18 (kill by
process group, never by pattern).

Every runner turns a `TurnSpec` into a `TurnOutcome`.  Every subprocess:
  * stdin is /dev/null (a held pipe blocks `opencode run` and `codex exec`
    forever),
  * is started in its own session (`start_new_session=True`) so a timeout
    or a STOP kills the whole process group, never a pattern,
  * is bounded by `spec.timeout_s`,
  * has its exit code recorded but NEVER used as a success signal.

Success is only ever: the expected record exists on disk (or arrives in the
result JSON) AND validates.  An empty stream is UNKNOWN, never OK.

stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = [
    "TurnSpec", "TurnOutcome", "Exec", "OpenCodeRunner", "OpenCodeHttpRunner",
    "CodexRunner", "ClaudeRunner", "AgyRunner", "classify_opencode_log",
    "classify_provider_error", "extract_json_fence", "STATUS_DONE", "runner_for",
]

# --------------------------------------------------------------------------
# statuses (closed set; 07-FAILURE-CATALOG names them)
# --------------------------------------------------------------------------

STATUS_DONE = "DONE"
STATUS_INCOMPLETE = "INCOMPLETE"       # record exists but does not validate
STATUS_EMPTY = "RUNNER_EMPTY"          # no record, no error, no text
STATUS_PROGRESS_STOP = "PROGRESS_STOP" # clean stop, no record (resumable)
STATUS_DENIED = "RUNNER_DENIED"        # a tool the role needed was denied
STATUS_TIMEOUT = "RUNNER_TIMEOUT"
STATUS_CRASH = "RUNNER_CRASH"
STATUS_REFUSED = "RUNNER_REFUSED"      # model refused the task (safety)
STATUS_ABORTED = "ABORTED"
STATUS_QUOTA_WEEKLY = "QUOTA_WEEKLY"
STATUS_QUOTA_ROLLING = "QUOTA_ROLLING"
STATUS_RATE = "RATE"
STATUS_DEGRADED = "DEGRADED"
STATUS_AUTH = "AUTH"
STATUS_MODEL_MISMATCH = "MODEL_MISMATCH"
STATUS_SESSION_MISMATCH = "SESSION_MISMATCH"

PARK_STATUSES = (STATUS_QUOTA_WEEKLY, STATUS_QUOTA_ROLLING, STATUS_RATE,
                 STATUS_DEGRADED, STATUS_AUTH)

NODE22_BIN = str(Path.home() / ".local" / "node-v22.11.0-darwin-arm64" / "bin")
# D13: every child (codex, opencode CLI, autofix tools, box proofs) must find
# `uv`, `ruff` and node no matter how the driver itself was launched (7 rows
# failed their ruff/collect-only checks with "uv: command not found" when the
# spawning process had a launchd/minimal PATH).  Prepended in this order,
# missing dirs skipped, never duplicated.
TOOL_BINS = (NODE22_BIN, str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin")
RAW_HEAD_LINES = 20


def tool_path(path=None, node22_first=True):
    """PATH with TOOL_BINS in front (node22 only when asked), deduplicated."""
    cur = [p for p in (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep) if p]
    front = [b for b in TOOL_BINS if os.path.isdir(b) and (node22_first or b != NODE22_BIN)]
    out = []
    for p in front + cur:
        if p not in out:
            out.append(p)
    return os.pathsep.join(out)


def utc_ms():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def _iso_to_epoch(text):
    try:
        return datetime.strptime(text[:23], "%Y-%m-%dT%H:%M:%S.%f").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        try:
            return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None


# --------------------------------------------------------------------------
# spec / outcome
# --------------------------------------------------------------------------

class TurnSpec(object):
    """Everything a runner needs for one turn.  Plain attributes."""

    def __init__(self, role, item, cwd, prompt, model, **kw):
        self.role = role
        self.item = item
        self.cwd = str(cwd)
        self.prompt = prompt
        self.model = model
        self.variant = kw.get("variant")          # opencode
        self.effort = kw.get("effort")            # codex / claude
        self.server_url = kw.get("server_url")    # opencode --attach
        self.xdg_data_home = kw.get("xdg_data_home")
        self.agent = kw.get("agent")
        self.title = kw.get("title") or "%s %s" % (item, role)
        self.session_id = kw.get("session_id")    # resume when set
        self.out_path = kw.get("out_path")        # record the role must write
        self.schema_path = kw.get("schema_path")  # codex --output-schema
        self.schema_text = kw.get("schema_text")  # claude --json-schema
        self.settings_path = kw.get("settings_path")
        self.allowed_tools = kw.get("allowed_tools") or []
        self.tools = kw.get("tools")              # claude --tools (restrict)
        self.add_dirs = kw.get("add_dirs") or []
        self.max_turns = kw.get("max_turns")
        self.max_budget_usd = kw.get("max_budget_usd")
        self.sandbox = kw.get("sandbox") or "read-only"
        self.timeout_s = float(kw.get("timeout_s") or 2700)
        self.log_dir = kw.get("log_dir")          # where stdout/exports land
        self.tag = kw.get("tag") or "1"           # file prefix inside log_dir
        self.expect_fence = bool(kw.get("expect_fence", False))
        self.validator = kw.get("validator")      # callable(path)->(ok, errs)
        self.env = kw.get("env") or {}


class TurnOutcome(object):

    def __init__(self, status, detail="", **kw):
        self.status = status
        self.detail = detail
        self.session_id = kw.get("session_id")
        self.usage = kw.get("usage") or {}
        self.text = kw.get("text") or ""
        self.structured = kw.get("structured")
        self.exit = kw.get("exit")
        self.duration_s = kw.get("duration_s", 0.0)
        self.start_ts = kw.get("start_ts")
        self.end_ts = kw.get("end_ts")
        self.raw_head = kw.get("raw_head") or []
        self.lines = kw.get("lines", 0)
        self.denied_tools = kw.get("denied_tools") or []
        self.log_path = kw.get("log_path")
        self.export_path = kw.get("export_path")
        self.argv = kw.get("argv") or []
        self.record_path = kw.get("record_path")   # the validated record
        self.runner = kw.get("runner")
        self.model_seen = kw.get("model_seen")

    @property
    def ok(self):
        return self.status == STATUS_DONE

    def to_dict(self):
        return {
            "status": self.status, "detail": self.detail,
            "session_id": self.session_id, "usage": self.usage,
            "exit_code": self.exit, "duration_s": self.duration_s,
            "start_ts": self.start_ts, "end_ts": self.end_ts,
            "raw_head": self.raw_head, "event_lines": self.lines,
            "denied_tools": self.denied_tools, "log_path": self.log_path,
            "export_path": self.export_path, "argv": self.argv,
            "record_path": self.record_path, "runner": self.runner,
            "model_seen": self.model_seen,
            "text_head": (self.text or "")[:2000],
        }


# --------------------------------------------------------------------------
# Exec -- the only place that touches processes (fakeable)
# --------------------------------------------------------------------------

class Exec(object):
    """Spawns a process in its own session and streams stdout lines.

    `stream(argv, env, cwd, timeout_s, on_line, abort_flag)` returns
    (returncode, timed_out, aborted).  `on_line(str)` is called per line.
    """

    def env_for(self, overrides=None, node22_first=True):
        env = dict(os.environ)
        env["PATH"] = tool_path(env.get("PATH", ""), node22_first=node22_first)
        if overrides:
            env.update({k: str(v) for k, v in overrides.items()})
        return env

    def stream(self, argv, env=None, cwd=None, timeout_s=None, on_line=None,
               abort_flag=None, log_fh=None):
        if env is None:
            env = self.env_for()
        try:
            popen = subprocess.Popen(
                list(argv), env=env, cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True, bufsize=1,
                start_new_session=True)
        except OSError as exc:
            if on_line:
                on_line("spawn failed: %s" % exc)
            return 127, False, False
        timed_out = [False]
        aborted = [False]
        done = threading.Event()

        def watchdog():
            deadline = None if not timeout_s else time.monotonic() + timeout_s
            while not done.is_set():
                if abort_flag is not None and abort_flag.is_set():
                    aborted[0] = True
                    self.kill_group(popen)
                    return
                if deadline is not None and time.monotonic() > deadline:
                    timed_out[0] = True
                    self.kill_group(popen)
                    return
                done.wait(0.5)

        th = threading.Thread(target=watchdog, daemon=True)
        th.start()
        try:
            for line in popen.stdout:
                line = line.rstrip("\n")
                if log_fh is not None:
                    try:
                        log_fh.write(line + "\n")
                    except OSError:
                        pass
                if on_line:
                    on_line(line)
        finally:
            done.set()
            try:
                popen.stdout.close()
            except Exception:
                pass
        rc = popen.wait()
        th.join(timeout=2.0)
        return rc, timed_out[0], aborted[0]

    @staticmethod
    def kill_group(popen):
        """Kill the process group we created (K-18): never a pattern."""
        try:
            pgid = os.getpgid(popen.pid)
        except OSError:
            pgid = None
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    popen.send_signal(sig)
            except OSError:
                pass
            for _ in range(20):
                if popen.poll() is not None:
                    return
                time.sleep(0.1)

    def run(self, argv, env=None, cwd=None, timeout_s=120):
        """Blocking helper for short commands (git, export) and the box proof.

        D89: on timeout the WHOLE process tree dies, not just the child.
        `subprocess.run(timeout=)` SIGKILLs the direct child only; vpproof's
        pytest sits in its own session (start_new_session), so it and its
        Postgres clusters ran on orphaned (L06-HOSTED-R5, 03:32Z, pid 11825).
        Descendants are walked via `ps` before anything is signalled, then
        SIGINT -> SIGTERM -> SIGKILL, deepest first, with short graces."""
        if env is None:
            env = self.env_for()
        try:
            proc = subprocess.Popen(list(argv), env=env, cwd=cwd,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, universal_newlines=True,
                                    start_new_session=True)
        except OSError as exc:
            return 127, "", "%s" % exc
        try:
            out, err = proc.communicate(timeout=timeout_s)
            return proc.returncode, out or "", err or ""
        except subprocess.TimeoutExpired:
            killed = kill_tree(proc.pid)
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return 124, out or "", "timeout after %ss; killed tree %s" % (timeout_s, killed)


def descendants(pid):
    """every live descendant pid of `pid`, deepest last (stdlib: one `ps`)"""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], stdout=subprocess.PIPE,
                             universal_newlines=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    kids = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, frontier = [], [int(pid)]
    while frontier:
        nxt = []
        for p in frontier:
            for c in kids.get(p, []):
                if c not in found:
                    found.append(c)
                    nxt.append(c)
        frontier = nxt
    return found


def kill_tree(pid, grace_int_s=5.0, grace_term_s=3.0, sleep=time.sleep):
    """SIGINT -> SIGTERM -> SIGKILL to `pid` and every descendant (deepest
    first); returns the pids signalled.  A descendant in its own session
    (pytest under vpproof) is reached by pid, which killpg never was."""
    targets = list(reversed(descendants(pid))) + [int(pid)]
    signalled = list(targets)
    for sig, grace in ((signal.SIGINT, grace_int_s), (signal.SIGTERM, grace_term_s), (signal.SIGKILL, None)):
        alive = []
        for p in targets:
            try:
                os.kill(p, sig)
                alive.append(p)
            except OSError:
                pass
        if not alive:
            break
        if grace is None:
            break
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not any(_alive(p) for p in alive):
                break
            sleep(0.1)
        targets = [p for p in alive if _alive(p)]
        if not targets:
            break
    return sorted(set(signalled))


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True

    def run_to_file(self, argv, path, env=None, cwd=None, timeout_s=300):
        """K-04: stdout straight to a file (a pipe truncates at 65,536 bytes)."""
        if env is None:
            env = self.env_for()
        try:
            with open(path, "w", encoding="utf-8") as fh:
                cp = subprocess.run(list(argv), env=env, cwd=cwd,
                                    timeout=timeout_s, stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=subprocess.PIPE,
                                    universal_newlines=True,
                                    start_new_session=True)
            return cp.returncode, cp.stderr or ""
        except subprocess.TimeoutExpired:
            return 124, "timeout after %ss" % timeout_s
        except OSError as exc:
            return 127, "%s" % exc


# --------------------------------------------------------------------------
# helpers shared by runners
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S)


def extract_json_fence(text):
    """K-02: the LAST ```json fence that parses as an object, else None."""
    if not text:
        return None
    for chunk in reversed(_FENCE_RE.findall(text)):
        chunk = chunk.strip()
        if not chunk.startswith("{"):
            continue
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    # a bare JSON object as the whole message
    stripped = (text or "").strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    return None


# K-03: the real OpenCode log wordings (A5 §quota).  Order matters: weekly
# before rolling because both contain "usage limit".
_OC_LOG_PATTERNS = (
    (STATUS_QUOTA_WEEKLY, re.compile(r"weekly usage limit reached", re.I)),
    (STATUS_QUOTA_ROLLING, re.compile(r"5-hour usage limit reached", re.I)),
    (STATUS_RATE, re.compile(r"rate_limit_exceeded|too many requests|429", re.I)),
    (STATUS_DEGRADED, re.compile(r"service_overloaded|upstream response was not "
                                 r"valid json|overloaded|502|503|504", re.I)),
    (STATUS_AUTH, re.compile(r"invalid api key|invalid_api_key|unauthorized|401",
                             re.I)),
)
_RESETS_RE = re.compile(r"resets? in (\d+)\s*(min|minute|h|hour|s|sec)", re.I)


def classify_opencode_log(log_path, start_epoch, end_epoch, tail_bytes=2_000_000):
    """Scan the server log for provider errors stamped inside the window.

    -> (status or None, detail, reset_minutes or None).  The driver's stream
    never carries these (measured); the log is the only instrument.
    """
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()
            lines = fh.readlines()
    except OSError:
        return None, "log unreadable: %s" % log_path, None
    hit = None
    for line in lines:
        if not line.startswith("timestamp="):
            continue
        ts = _iso_to_epoch(line[10:34])
        if ts is None or ts < start_epoch - 2 or ts > end_epoch + 2:
            continue
        for status, rx in _OC_LOG_PATTERNS:
            if rx.search(line):
                mins = None
                m = _RESETS_RE.search(line)
                if m:
                    n = int(m.group(1))
                    unit = m.group(2).lower()
                    mins = n * 60 if unit.startswith("h") else (
                        max(1, n // 60) if unit.startswith("s") else n)
                hit = (status, line.strip()[:300], mins)
                if status == STATUS_QUOTA_WEEKLY:
                    return hit
                break
    if hit:
        return hit
    return None, "", None


def classify_provider_error(text):
    """Provider error text (message `info.error`, server log line, CLI stderr)
    -> (status, reset_minutes or None).  Monthly and weekly caps are the long
    park (QUOTA_WEEKLY); the 5-hour window is QUOTA_ROLLING."""
    t = (text or "")
    if re.search(r"(weekly|monthly) usage limit", t, re.I):
        st = STATUS_QUOTA_WEEKLY
    elif re.search(r"5-hour usage limit|usage limit reached|insufficient_quota|quota", t, re.I):
        st = STATUS_QUOTA_ROLLING
    elif re.search(r"rate.?limit|too many requests|\b429\b", t, re.I):
        st = STATUS_RATE
    elif re.search(r"invalid api key|invalid_api_key|unauthori[sz]ed|\b401\b|ProviderAuthError", t, re.I):
        st = STATUS_AUTH
    elif re.search(r"overloaded|not valid json|\b50[234]\b|ECONNRESET|ECONNREFUSED|stream|timeout|"
                   r"aborted|isRetryable|APIError", t, re.I):
        st = STATUS_DEGRADED
    else:
        st = STATUS_DEGRADED
    mins = None
    m = _RESETS_RE.search(t)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        mins = n * 60 if unit.startswith("h") else (max(1, n // 60) if unit.startswith("s") else n)
    m = re.search(r"resets? in (\d+)\s*days?", t, re.I)
    if m:
        mins = int(m.group(1)) * 24 * 60
    return st, mins


_REFUSAL_RE = re.compile(
    r"\b(i can.?t help|i cannot help|cannot assist|can.?t assist|unable to "
    r"(?:help|assist|review)|i won.?t be able to)\b", re.I)


def _looks_refused(text):
    return bool(text) and len(text) < 1500 and bool(_REFUSAL_RE.search(text))


def _validate(spec, path):
    if spec.validator is None:
        return True, []
    try:
        ok, errs = spec.validator(path)
    except Exception as exc:          # validator bugs must not kill a turn
        return False, ["validator raised: %s" % exc]
    return bool(ok), list(errs or [])


# --------------------------------------------------------------------------
# OpenCode
# --------------------------------------------------------------------------

class OpenCodeRunner(object):
    name = "opencode"
    DENIAL_MARKER = "rule which prevents"

    def __init__(self, exec_=None, binary="opencode"):
        self.exec = exec_ or Exec()
        self.binary = binary

    def argv(self, spec):
        argv = [self.binary, "run", "--attach", spec.server_url,
                "--dir", spec.cwd, "--agent", spec.agent, "--model", spec.model]
        if spec.variant:
            argv += ["--variant", spec.variant]
        argv += ["--format", "json", "--pure", "--title", spec.title]
        if spec.session_id:
            argv += ["--session", spec.session_id]
        argv.append(spec.prompt)
        return argv

    ERROR_POLL_S = 5.0

    def run(self, spec, abort_flag=None):
        argv = self.argv(spec)
        env = self.exec.env_for({"XDG_DATA_HOME": spec.xdg_data_home}
                                if spec.xdg_data_home else None)
        env.update(spec.env)
        acc = _OpenCodeAcc()
        raw_head, n = [], [0]
        log_path = self._log_path(spec, "opencode.jsonl")
        start_ts, start_mono, start_epoch = utc_ms(), time.monotonic(), time.time()
        # `opencode run` never exits when the provider stream errors (GO3 monthly
        # cap, 2026-09-17): a watchdog polls the server for the session's
        # assistant message and trips the abort flag on `info.error`.
        watch_abort = threading.Event()
        provider_error = [None]
        stop_watch = threading.Event()

        def combined_abort_set():
            return (abort_flag is not None and abort_flag.is_set()) or watch_abort.is_set()

        class _Flag(object):
            def is_set(self_inner):
                return combined_abort_set()

        def watchdog():
            http = _Http(spec.server_url, spec.cwd) if spec.server_url else None
            while http is not None and not stop_watch.wait(self.ERROR_POLL_S):
                sid = acc.session_id
                if not sid:
                    continue
                err = http.session_error(sid)
                if err:
                    provider_error[0] = err
                    watch_abort.set()
                    return

        th = threading.Thread(target=watchdog, daemon=True)
        th.start()

        def on_line(line):
            n[0] += 1
            if len(raw_head) < RAW_HEAD_LINES:
                raw_head.append(line)
            s = line.strip()
            if s.startswith("{"):
                try:
                    acc.feed(json.loads(s))
                except ValueError:
                    pass

        with _maybe_open(log_path) as fh:
            rc, timed_out, aborted = self.exec.stream(
                argv, env=env, cwd=spec.cwd, timeout_s=spec.timeout_s,
                on_line=on_line, abort_flag=_Flag(), log_fh=fh)
        stop_watch.set()
        th.join(timeout=1.0)
        end_ts, end_epoch = utc_ms(), time.time()
        base = dict(session_id=acc.session_id, usage=acc.usage(), text=acc.text(),
                    exit=rc, duration_s=round(time.monotonic() - start_mono, 3),
                    start_ts=start_ts, end_ts=end_ts, raw_head=raw_head,
                    lines=n[0], denied_tools=acc.denied, log_path=str(log_path),
                    argv=argv, runner=self.name)

        if provider_error[0]:
            st, mins = classify_provider_error(provider_error[0])
            base["usage"]["reset_minutes"] = mins
            return TurnOutcome(st, "provider error (child aborted): %s" % provider_error[0][:300],
                               **base)
        if aborted:
            return TurnOutcome(STATUS_ABORTED, "STOP", **base)
        if timed_out:
            return TurnOutcome(STATUS_TIMEOUT, "killed after %ss" % spec.timeout_s,
                               **base)
        if spec.session_id and acc.session_id and acc.session_id != spec.session_id:
            return TurnOutcome(STATUS_SESSION_MISMATCH,
                               "stream session %s != requested %s"
                               % (acc.session_id, spec.session_id), **base)

        # K-03: the server log is the only place provider errors appear.
        if spec.xdg_data_home:
            logp = os.path.join(spec.xdg_data_home, "opencode", "log", "opencode.log")
            st, det, mins = classify_opencode_log(logp, start_epoch, end_epoch)
            if st and (not acc.steps or acc.tokens_out < 20 or st == STATUS_QUOTA_WEEKLY):
                base["usage"]["reset_minutes"] = mins
                return TurnOutcome(st, det, **base)

        # record on disk?
        out = Path(spec.out_path) if spec.out_path else None
        if out is not None and out.exists() and out.stat().st_size > 0:
            ok, errs = _validate(spec, out)
            if ok:
                return TurnOutcome(STATUS_DONE, "", record_path=str(out), **base)
            return TurnOutcome(STATUS_INCOMPLETE, "; ".join(errs[:8]),
                               record_path=str(out), **base)

        # K-02: the junior cannot write; take the fence.
        if spec.expect_fence and out is not None:
            obj = extract_json_fence(acc.text())
            if obj is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(obj, indent=2, sort_keys=True),
                               encoding="utf-8")
                ok, errs = _validate(spec, out)
                if ok:
                    return TurnOutcome(STATUS_DONE, "record taken from text fence",
                                       record_path=str(out), **base)
                return TurnOutcome(STATUS_INCOMPLETE,
                                   "fence record invalid: " + "; ".join(errs[:8]),
                                   record_path=str(out), **base)

        if acc.denied and not acc.text().strip():
            return TurnOutcome(STATUS_DENIED, "denied: %s" % json.dumps(acc.denied[:3]),
                               **base)
        if rc == 127 or (n[0] == 0 and rc != 0):
            return TurnOutcome(STATUS_CRASH, "no events, exit %s" % rc, **base)
        if _looks_refused(acc.text()):
            return TurnOutcome(STATUS_REFUSED, acc.text()[:300], **base)
        if acc.last_reason == "stop" and acc.steps:
            return TurnOutcome(STATUS_PROGRESS_STOP,
                               "step_finish reason=stop with no record", **base)
        if not acc.steps or acc.tokens_out < 20:
            return TurnOutcome(STATUS_EMPTY, "no record and <20 output tokens (%d steps)"
                               % acc.steps, **base)
        return TurnOutcome(STATUS_PROGRESS_STOP, "no record and no error", **base)

    def export(self, spec, session_id, dest_path):
        """K-04: `opencode export <sid>` to a FILE; asserts it is not the
        65,536-byte pipe truncation size."""
        env = self.exec.env_for({"XDG_DATA_HOME": spec.xdg_data_home}
                                if spec.xdg_data_home else None)
        rc, err = self.exec.run_to_file([self.binary, "export", session_id],
                                        dest_path, env=env, cwd=spec.cwd)
        try:
            size = os.path.getsize(dest_path)
        except OSError:
            size = -1
        return {"exit": rc, "bytes": size, "stderr": err[:500],
                "truncated_suspect": size == 65536, "path": str(dest_path)}

    @staticmethod
    def _log_path(spec, name):
        if not spec.log_dir:
            return None
        Path(spec.log_dir).mkdir(parents=True, exist_ok=True)
        return Path(spec.log_dir) / ("%s-%s" % (spec.tag, name))


class _OpenCodeAcc(object):
    """Folds `opencode run --format json` events (shape measured 2026-09-12)."""

    def __init__(self):
        self.session_id = None
        self.texts = []
        self.tokens_in = self.tokens_out = self.tokens_reason = 0
        self.cache_read = self.cache_write = 0
        self.cost = 0.0
        self.steps = 0
        self.last_reason = None
        self.denied = []
        self.saw_error = False

    def usage(self):
        if not self.steps:
            return {"tokens_in": None, "tokens_out": None, "tokens_reason": None,
                    "cache_read": None, "cache_write": None, "cost": None}
        return {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "tokens_reason": self.tokens_reason, "cache_read": self.cache_read,
                "cache_write": self.cache_write, "cost": round(self.cost, 8)}

    def text(self):
        return "\n".join(self.texts)

    def feed(self, obj):
        if not isinstance(obj, dict):
            return
        if self.session_id is None:
            sid = obj.get("sessionID")
            if isinstance(sid, str) and sid:
                self.session_id = sid
        typ = str(obj.get("type", ""))
        part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
        if typ == "text":
            val = part.get("text", obj.get("text"))
            if isinstance(val, str) and val.strip():
                self.texts.append(val)
        elif typ == "step_finish":
            self.steps += 1
            reason = part.get("reason", obj.get("reason"))
            if isinstance(reason, str) and reason:
                self.last_reason = reason
            tok = part.get("tokens", obj.get("tokens"))
            if isinstance(tok, dict):
                for key, attr in (("input", "tokens_in"), ("output", "tokens_out"),
                                  ("reasoning", "tokens_reason")):
                    v = tok.get(key)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        setattr(self, attr, getattr(self, attr) + v)
                cache = tok.get("cache")
                if isinstance(cache, dict):
                    for key, attr in (("read", "cache_read"), ("write", "cache_write")):
                        v = cache.get(key)
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            setattr(self, attr, getattr(self, attr) + v)
            cost = part.get("cost", obj.get("cost"))
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                self.cost += cost
        elif typ == "tool_use":
            state = part.get("state")
            if isinstance(state, dict) and str(state.get("status", "")).lower() == "error":
                err = state.get("error") or part.get("error") or ""
                if not isinstance(err, str):
                    err = json.dumps(err)
                tool = part.get("tool") or part.get("name") or "?"
                if OpenCodeRunner.DENIAL_MARKER in err.lower():
                    self.denied.append({"tool": tool, "error": err[:300]})
                else:
                    self.saw_error = True
        if "error" in typ.lower():
            self.saw_error = True


# --------------------------------------------------------------------------
# OpenCode over the server HTTP API (D4: the CLI's per-process sqlite locks
# at 4-/8-wide and hangs on provider stream errors; the server is fine)
# --------------------------------------------------------------------------

class _Http(object):
    """Tiny urllib client for one `opencode serve`.  Every call carries the
    worktree as `?directory=` so the session is bound to that checkout."""

    def __init__(self, base_url, directory, timeout_s=30.0):
        self.base = str(base_url).rstrip("/")
        self.directory = str(directory) if directory else None
        self.timeout_s = float(timeout_s)

    def url(self, path, **query):
        q = dict(query)
        if self.directory:
            q.setdefault("directory", self.directory)
        qs = urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
        return self.base + path + (("?" + qs) if qs else "")

    def request(self, method, path, body=None, timeout_s=None, **query):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url(path, **query), data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read() if hasattr(exc, "read") else b""
            status = exc.code
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise _HttpDown("%s %s: %s" % (method, path, exc))
        text = raw.decode("utf-8", "replace") if raw else ""
        try:
            return status, (json.loads(text) if text.strip() else None)
        except ValueError:
            return status, {"raw": text[:2000]}

    def session_error(self, sid):
        """The assistant message error text for a session, or None."""
        try:
            st, msgs = self.request("GET", "/session/%s/message" % sid, timeout_s=10)
        except _HttpDown:
            return None
        if st != 200 or not isinstance(msgs, list):
            return None
        for m in reversed(msgs):
            info = (m or {}).get("info") or {}
            if info.get("role") == "assistant" and info.get("error"):
                return _error_text(info["error"])
        return None


class _HttpDown(Exception):
    pass


def _error_text(err):
    if not err:
        return ""
    if isinstance(err, str):
        return err
    name = str(err.get("name") or "")
    data = err.get("data") if isinstance(err.get("data"), dict) else {}
    parts = [name, str(data.get("message") or "")]
    if data.get("statusCode"):
        parts.append("status %s" % data["statusCode"])
    if data.get("responseBody"):
        parts.append(str(data["responseBody"])[:300])
    return " ".join(p for p in parts if p)


class OpenCodeHttpRunner(object):
    """One turn = POST /session (or resume) -> POST /session/<id>/prompt_async
    -> poll GET /session/<id>/message until the assistant message carries
    `time.completed` or `info.error`.  The per-turn timeout is enforced here
    (POST /session/<id>/abort), the STOP flag aborts the same way.  Success
    is only ever the validated record on disk (or the K-02 fence)."""
    name = "opencode"
    DENIAL_MARKER = "rule which prevents"
    POLL_S = 2.0

    def __init__(self, exec_=None, binary=None, poll_s=None, http_factory=None):
        self.exec = exec_ or Exec()
        self.binary = binary
        self.poll_s = float(poll_s or self.POLL_S)
        self.http_factory = http_factory or _Http

    @staticmethod
    def split_model(model):
        model = str(model or "")
        if "/" in model:
            provider, mid = model.split("/", 1)
            return provider, mid
        return "opencode-go", model

    def run(self, spec, abort_flag=None):
        http = self.http_factory(spec.server_url, spec.cwd)
        provider, model_id = self.split_model(spec.model)
        log_path = self._log_path(spec, "opencode-http.jsonl")
        start_ts, start_mono = utc_ms(), time.monotonic()
        argv = ["http", spec.server_url, "prompt_async", "%s/%s" % (provider, model_id),
                spec.variant or "-", spec.agent or "-"]
        base = dict(session_id=spec.session_id, usage=_empty_usage(), text="", exit=None,
                    duration_s=0.0, start_ts=start_ts, end_ts=None, raw_head=[], lines=0,
                    denied_tools=[], log_path=str(log_path) if log_path else None,
                    argv=argv, runner=self.name)
        log_fh = _maybe_open(log_path).__enter__()

        def log(kind, payload):
            if log_fh is not None:
                try:
                    log_fh.write(json.dumps({"ts": utc_ms(), "type": kind, "data": payload},
                                            default=str)[:20000] + "\n")
                    log_fh.flush()
                except OSError:
                    pass

        def finish(status, detail, **kw):
            base["duration_s"] = round(time.monotonic() - start_mono, 3)
            base["end_ts"] = utc_ms()
            if log_fh is not None:
                try:
                    log_fh.close()
                except OSError:
                    pass
            base.update(kw)
            return TurnOutcome(status, detail, **base)

        # 1. session
        sid = spec.session_id
        try:
            if not sid:
                body = {"title": spec.title}
                if spec.agent:
                    body["agent"] = spec.agent
                st, data = http.request("POST", "/session", body)
                log("session.create", {"status": st, "data": data})
                if st != 200 or not isinstance(data, dict) or not data.get("id"):
                    return finish(STATUS_CRASH, "POST /session -> %s %s" % (st, json.dumps(data)[:200]))
                sid = data["id"]
                base["session_id"] = sid
            before = 0
            st, msgs = http.request("GET", "/session/%s/message" % sid)
            if st == 200 and isinstance(msgs, list):
                before = len(msgs)
            # 2. prompt
            body = {"model": {"providerID": provider, "modelID": model_id},
                    "parts": [{"type": "text", "text": spec.prompt}]}
            if spec.variant:
                body["variant"] = spec.variant
            if spec.agent:
                body["agent"] = spec.agent
            st, data = http.request("POST", "/session/%s/prompt_async" % sid, body)
            log("prompt_async", {"status": st, "data": data, "model": body["model"],
                                 "variant": spec.variant})
            if st not in (200, 204):
                txt = json.dumps(data)[:400] if data is not None else ""
                stt, mins = classify_provider_error(txt) if st in (401, 402, 403, 429, 500, 502, 503) \
                    else (STATUS_CRASH, None)
                base["usage"]["reset_minutes"] = mins
                return finish(stt, "prompt_async -> %s %s" % (st, txt))
        except _HttpDown as exc:
            return finish(STATUS_DEGRADED, "server unreachable: %s" % str(exc)[:200])

        # 3. poll
        deadline = start_mono + float(spec.timeout_s or 2700)
        acc = _OpenCodeHttpAcc(before)
        last_err = None
        while True:
            if abort_flag is not None and abort_flag.is_set():
                self._abort(http, sid, log)
                return finish(STATUS_ABORTED, "STOP", **acc.base_fields())
            if time.monotonic() > deadline:
                self._abort(http, sid, log)
                return finish(STATUS_TIMEOUT, "aborted after %ss" % spec.timeout_s,
                              **acc.base_fields())
            try:
                st, msgs = http.request("GET", "/session/%s/message" % sid, timeout_s=20)
            except _HttpDown as exc:
                last_err = str(exc)
                time.sleep(self.poll_s)
                continue
            if st == 200 and isinstance(msgs, list):
                acc.fold(msgs)
                if acc.error:
                    log("messages", msgs[-3:])
                    stt, mins = classify_provider_error(acc.error)
                    fields = acc.base_fields()
                    fields["usage"]["reset_minutes"] = mins
                    return finish(stt, "provider error: %s" % acc.error[:300], **fields)
                if acc.completed:
                    log("messages", msgs[before:])
                    break
            time.sleep(self.poll_s)

        fields = acc.base_fields()
        if spec.model and acc.model_seen and acc.model_seen != "%s/%s" % (provider, model_id):
            return finish(STATUS_MODEL_MISMATCH, "message model %s != %s/%s"
                          % (acc.model_seen, provider, model_id), **fields)
        out = Path(spec.out_path) if spec.out_path else None
        if out is not None and out.exists() and out.stat().st_size > 0:
            ok, errs = _validate(spec, out)
            if ok:
                return finish(STATUS_DONE, "", record_path=str(out), **fields)
            return finish(STATUS_INCOMPLETE, "; ".join(errs[:8]), record_path=str(out), **fields)
        if spec.expect_fence and out is not None:
            obj = extract_json_fence(acc.text())
            if obj is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
                ok, errs = _validate(spec, out)
                if ok:
                    return finish(STATUS_DONE, "record taken from text fence",
                                  record_path=str(out), **fields)
                return finish(STATUS_INCOMPLETE, "fence record invalid: " + "; ".join(errs[:8]),
                              record_path=str(out), **fields)
        if acc.denied and not acc.text().strip():
            return finish(STATUS_DENIED, "denied: %s" % json.dumps(acc.denied[:3]), **fields)
        if _looks_refused(acc.text()):
            return finish(STATUS_REFUSED, acc.text()[:300], **fields)
        if acc.messages and acc.tokens_out < 20 and not acc.text().strip():
            return finish(STATUS_EMPTY, "assistant message with <20 output tokens and no text"
                          " (%s)" % (last_err or ""), **fields)
        return finish(STATUS_PROGRESS_STOP, "assistant finished (%s) with no record"
                      % (acc.finish or "?"), **fields)

    def _abort(self, http, sid, log):
        try:
            st, data = http.request("POST", "/session/%s/abort" % sid, timeout_s=10)
            log("abort", {"status": st, "data": data})
        except _HttpDown as exc:
            log("abort", {"error": str(exc)})

    def export(self, spec, session_id, dest_path):
        """The session transcript straight from the server (no CLI)."""
        http = self.http_factory(spec.server_url, spec.cwd)
        try:
            st, msgs = http.request("GET", "/session/%s/message" % session_id, timeout_s=60)
        except _HttpDown as exc:
            return {"exit": 1, "bytes": -1, "stderr": str(exc)[:500], "path": str(dest_path)}
        try:
            Path(dest_path).write_text(json.dumps(msgs, indent=1, default=str), encoding="utf-8")
            size = os.path.getsize(dest_path)
        except OSError as exc:
            return {"exit": 1, "bytes": -1, "stderr": str(exc)[:500], "path": str(dest_path)}
        return {"exit": 0 if st == 200 else 1, "bytes": size, "stderr": "",
                "truncated_suspect": False, "path": str(dest_path)}

    def probe(self, server_url, directory, model, variant=None, timeout_s=90.0, text="OK"):
        """One tiny live turn: reply-with-OK.  (server_url, model) -> (ok, detail, seconds)."""
        spec = TurnSpec("probe", "-", directory, "Reply with exactly %s and nothing else." % text,
                        model, variant=variant, server_url=server_url, timeout_s=timeout_s,
                        title="lanedriver probe %s" % utc_ms())
        out = self.run(spec)
        ok = out.status in (STATUS_PROGRESS_STOP, STATUS_DONE, STATUS_EMPTY) and \
            text.lower() in (out.text or "").lower()
        return ok, "%s %s %r" % (out.status, out.detail[:120], (out.text or "")[:60]), out.duration_s

    @staticmethod
    def _log_path(spec, name):
        if not spec.log_dir:
            return None
        Path(spec.log_dir).mkdir(parents=True, exist_ok=True)
        return Path(spec.log_dir) / ("%s-%s" % (spec.tag, name))


def _empty_usage():
    return {"tokens_in": None, "tokens_out": None, "tokens_reason": None,
            "cache_read": None, "cache_write": None, "cost": None}


class _OpenCodeHttpAcc(object):
    """Folds GET /session/<id>/message rows created by this turn."""

    def __init__(self, before):
        self.before = int(before)
        self.messages = 0
        self.texts = []
        self.denied = []
        self.tool_errors = []
        self.tokens_in = self.tokens_out = self.tokens_reason = 0
        self.cache_read = self.cache_write = 0
        self.cost = 0.0
        self.completed = False
        self.error = None
        self.finish = None
        self.model_seen = None
        self.raw_head = []

    def fold(self, msgs):
        self.texts, self.denied, self.tool_errors = [], [], []
        self.tokens_in = self.tokens_out = self.tokens_reason = 0
        self.cache_read = self.cache_write = 0
        self.cost = 0.0
        self.messages = 0
        self.completed = False
        self.error = None
        for m in msgs[self.before:]:
            info = (m or {}).get("info") or {}
            if info.get("role") != "assistant":
                continue
            self.messages += 1
            if len(self.raw_head) < RAW_HEAD_LINES:
                self.raw_head.append(json.dumps(info, default=str)[:400])
            tok = info.get("tokens") or {}
            for key, attr in (("input", "tokens_in"), ("output", "tokens_out"),
                              ("reasoning", "tokens_reason")):
                v = tok.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    setattr(self, attr, getattr(self, attr) + v)
            cache = tok.get("cache") or {}
            for key, attr in (("read", "cache_read"), ("write", "cache_write")):
                v = cache.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    setattr(self, attr, getattr(self, attr) + v)
            c = info.get("cost")
            if isinstance(c, (int, float)) and not isinstance(c, bool):
                self.cost += c
            if info.get("modelID"):
                self.model_seen = "%s/%s" % (info.get("providerID") or "", info["modelID"])
            if info.get("finish"):
                self.finish = info["finish"]
            if info.get("error"):
                self.error = _error_text(info["error"])
            for p in m.get("parts") or []:
                if p.get("type") == "text" and isinstance(p.get("text"), str) and \
                        not p.get("synthetic") and p["text"].strip():
                    self.texts.append(p["text"])
                elif p.get("type") == "tool":
                    stt = p.get("state") or {}
                    if str(stt.get("status", "")).lower() == "error":
                        err = str(stt.get("error") or "")
                        if OpenCodeHttpRunner.DENIAL_MARKER in err.lower():
                            self.denied.append({"tool": p.get("tool"), "error": err[:300]})
                        else:
                            self.tool_errors.append(err[:200])
            if (info.get("time") or {}).get("completed"):
                self.completed = True
            else:
                self.completed = False

    def text(self):
        return "\n".join(self.texts)

    def base_fields(self):
        usage = _empty_usage() if not self.messages else {
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "tokens_reason": self.tokens_reason, "cache_read": self.cache_read,
            "cache_write": self.cache_write, "cost": round(self.cost, 8)}
        return dict(usage=usage, text=self.text(), raw_head=list(self.raw_head),
                    lines=self.messages, denied_tools=list(self.denied),
                    model_seen=self.model_seen)


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------

_CODEX_QUOTA_RE = re.compile(r"usage limit|rate.?limit|too many requests|try again in|"
                             r"quota|insufficient_quota", re.I)
_CODEX_WEEKLY_RE = re.compile(r"week", re.I)
_CODEX_AUTH_RE = re.compile(r"\b401\b|unauthori[sz]ed|not logged in|login", re.I)
_CODEX_CONN_RE = re.compile(r"econnrefused|connection refused|reconnecting|"
                            r"\b502\b|\b503\b|\b504\b|failed to connect", re.I)


class CodexRunner(object):
    name = "codex"
    COMMON = ["--json", "--skip-git-repo-check",
              "-c", "notify=[]",
              "-c", "memories.use_memories=false",
              "-c", "memories.generate_memories=false"]

    def __init__(self, exec_=None, binary="codex"):
        self.exec = exec_ or Exec()
        self.binary = binary

    def argv(self, spec, last_path):
        # `-C`/`-s` belong to `codex exec` itself; the `resume` subcommand's
        # parser rejects them, so they must precede it.  Everything else
        # (--json, -c, -m, --output-schema, -o) is accepted by both.
        base = [self.binary, "exec", "-C", spec.cwd, "-s", spec.sandbox]
        if spec.session_id:
            base.append("resume")
        base += self.COMMON + ["-m", spec.model]
        if spec.effort:
            base += ["-c", 'model_reasoning_effort="%s"' % spec.effort]
        if spec.schema_path:
            base += ["--output-schema", str(spec.schema_path)]
        base += ["-o", str(last_path)]
        if spec.session_id:
            base.append(spec.session_id)   # positional SESSION_ID for resume
        base.append(spec.prompt)
        return base

    def run(self, spec, abort_flag=None):
        log_dir = Path(spec.log_dir) if spec.log_dir else Path(spec.cwd) / ".vp"
        log_dir.mkdir(parents=True, exist_ok=True)
        last_path = log_dir / ("%s-codex.last.txt" % spec.tag)
        log_path = log_dir / ("%s-codex.jsonl" % spec.tag)
        try:
            if last_path.exists():
                last_path.unlink()
        except OSError:
            pass
        argv = self.argv(spec, last_path)
        env = self.exec.env_for(None)
        env.update(spec.env)
        acc = _CodexAcc()
        raw_head, n = [], [0]
        start_ts, start_mono = utc_ms(), time.monotonic()

        def on_line(line):
            n[0] += 1
            if len(raw_head) < RAW_HEAD_LINES:
                raw_head.append(line)
            s = line.strip()
            if s.startswith("{"):
                try:
                    acc.feed(json.loads(s))
                except ValueError:
                    pass

        with open(log_path, "w", encoding="utf-8") as fh:
            rc, timed_out, aborted = self.exec.stream(
                argv, env=env, cwd=spec.cwd, timeout_s=spec.timeout_s,
                on_line=on_line, abort_flag=abort_flag, log_fh=fh)
        base = dict(session_id=acc.thread_id or spec.session_id, usage=acc.usage(),
                    text=acc.last_text(), exit=rc,
                    duration_s=round(time.monotonic() - start_mono, 3),
                    start_ts=start_ts, end_ts=utc_ms(), raw_head=raw_head,
                    lines=n[0], log_path=str(log_path), argv=argv,
                    runner=self.name, model_seen=acc.model_seen)
        if aborted:
            return TurnOutcome(STATUS_ABORTED, "STOP", **base)
        if timed_out:
            return TurnOutcome(STATUS_TIMEOUT, "killed after %ss" % spec.timeout_s,
                               **base)
        if not acc.model_seen and acc.thread_id:
            acc.model_seen = self._rollout_model(acc.thread_id)
            base["model_seen"] = acc.model_seen
        if acc.model_seen and spec.model and acc.model_seen != spec.model:
            return TurnOutcome(STATUS_MODEL_MISMATCH, "stream model %s != %s"
                               % (acc.model_seen, spec.model), **base)
        # provider-level errors
        errs = " | ".join(acc.errors)
        if errs:
            if _CODEX_QUOTA_RE.search(errs):
                st = STATUS_QUOTA_WEEKLY if _CODEX_WEEKLY_RE.search(errs) \
                    else STATUS_QUOTA_ROLLING
                return TurnOutcome(st, errs[:300], **base)
            if _CODEX_AUTH_RE.search(errs) and not acc.turn_completed:
                return TurnOutcome(STATUS_AUTH, errs[:300], **base)
            if _CODEX_CONN_RE.search(errs) and not acc.turn_completed:
                return TurnOutcome(STATUS_DEGRADED, errs[:300], **base)
        # the record: -o file (schema-shaped when --output-schema was given)
        text = ""
        try:
            text = last_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if not text.strip():
            text = acc.last_text()
        obj = extract_json_fence(text)
        if obj is None and text.strip().startswith("{"):
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
        if obj is not None and spec.out_path:
            out = Path(spec.out_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
            ok, verrs = _validate(spec, out)
            if ok:
                return TurnOutcome(STATUS_DONE, "", record_path=str(out),
                                   structured=obj, **base)
            return TurnOutcome(STATUS_INCOMPLETE, "; ".join(verrs[:8]),
                               record_path=str(out), structured=obj, **base)
        if _looks_refused(text):
            return TurnOutcome(STATUS_REFUSED, text[:300], **base)
        if rc == 127 or n[0] == 0:
            return TurnOutcome(STATUS_CRASH, "no events, exit %s" % rc, **base)
        if not acc.turn_completed:
            return TurnOutcome(STATUS_CRASH, "no turn.completed; errors=%s" % errs[:200],
                               **base)
        if not text.strip():
            return TurnOutcome(STATUS_EMPTY, "turn completed with no message", **base)
        return TurnOutcome(STATUS_INCOMPLETE, "message is not a JSON record: %s"
                           % text[:200], **base)


    PREOPEN_PROMPT = "Reply with exactly READY and nothing else."

    def preopen(self, spec, timeout_s=180.0):
        """Open a codex thread before the review turn so the scheduler's
        `start --child-id` can carry the REAL thread id (review_gate requires
        review task child_id == reviewer session id).  Returns (thread_id or
        None, detail).  The thread is then resumed for the review prompt."""
        pre = TurnSpec(spec.role, spec.item, spec.cwd, self.PREOPEN_PROMPT, spec.model,
                       effort=spec.effort, sandbox=spec.sandbox, timeout_s=timeout_s,
                       log_dir=spec.log_dir, tag="%s-preopen" % spec.tag)
        out = self.run(pre)
        if out.status in PARK_STATUSES:
            return None, "%s: %s" % (out.status, out.detail[:200])
        if not out.session_id:
            return None, "%s: no thread id (%s)" % (out.status, out.detail[:200])
        return out.session_id, out.status

    @staticmethod
    def _rollout_model(thread_id, root=None):
        """The stream has no model field; the rollout's turn_context does
        (A7).  Returns None when the file is not found quickly."""
        root = Path(root or (Path.home() / ".codex" / "sessions"))
        try:
            today = datetime.now()          # rollout dirs use LOCAL date
            cands = []
            for d in (today, today - timedelta(days=1), today + timedelta(days=1)):
                cands += list((root / d.strftime("%Y") / d.strftime("%m") / d.strftime("%d")
                               ).glob("rollout-*-%s.jsonl" % thread_id))
            for path in cands[:1]:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh):
                        if i > 40:
                            break
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        if obj.get("type") == "turn_context":
                            m = (obj.get("payload") or {}).get("model") or obj.get("model")
                            if isinstance(m, str):
                                return m
        except OSError:
            pass
        return None


class _CodexAcc(object):
    def __init__(self):
        self.thread_id = None
        self.texts = []
        self.errors = []
        self.usage_raw = None
        self.turn_completed = False
        self.model_seen = None

    def last_text(self):
        return self.texts[-1] if self.texts else ""

    def usage(self):
        u = self.usage_raw or {}
        if not u:
            return {"tokens_in": None, "tokens_out": None, "tokens_reason": None,
                    "cache_read": None, "cache_write": None, "cost": None}
        return {"tokens_in": u.get("input_tokens"),
                "tokens_out": u.get("output_tokens"),
                "tokens_reason": u.get("reasoning_output_tokens"),
                "cache_read": u.get("cached_input_tokens"),
                "cache_write": u.get("cache_write_input_tokens"),
                "cost": None}

    def feed(self, obj):
        typ = str(obj.get("type", ""))
        if typ == "thread.started":
            self.thread_id = obj.get("thread_id") or self.thread_id
        elif typ == "turn.started":
            m = obj.get("model")
            if isinstance(m, str):
                self.model_seen = m
        elif typ == "item.completed":
            item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
            it = item.get("type")
            if it == "agent_message":
                t = item.get("text")
                if isinstance(t, str):
                    self.texts.append(t)
            elif it == "error":
                self.errors.append(str(item.get("message", ""))[:400])
        elif typ == "turn.completed":
            self.turn_completed = True
            u = obj.get("usage")
            if isinstance(u, dict):
                self.usage_raw = u
        elif typ == "error":
            self.errors.append(str(obj.get("message", ""))[:400])
        elif typ == "turn.failed":
            e = obj.get("error")
            self.errors.append(json.dumps(e)[:400] if not isinstance(e, str) else e[:400])


# --------------------------------------------------------------------------
# Claude (headless `claude -p`)
# --------------------------------------------------------------------------

_CLAUDE_RATE_RE = re.compile(r"rate limit|usage limit|limit reached|429|overloaded",
                             re.I)


class ClaudeRunner(object):
    name = "claude"

    def __init__(self, exec_=None, binary="claude"):
        self.exec = exec_ or Exec()
        self.binary = binary

    def argv(self, spec):
        argv = [self.binary, "-p", spec.prompt, "--output-format", "json",
                "--permission-mode", "dontAsk"]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.effort:
            argv += ["--effort", spec.effort]
        if spec.schema_text:
            argv += ["--json-schema", spec.schema_text]
        if spec.settings_path:
            argv += ["--settings", str(spec.settings_path)]
        if spec.tools is not None:
            argv += ["--tools", ",".join(spec.tools) if spec.tools else ""]
        if spec.allowed_tools:
            argv += ["--allowedTools"] + list(spec.allowed_tools)
        for d in spec.add_dirs:
            argv += ["--add-dir", str(d)]
        if spec.max_turns:
            argv += ["--max-turns", str(int(spec.max_turns))]
        if spec.max_budget_usd:
            argv += ["--max-budget-usd", str(spec.max_budget_usd)]
        if spec.session_id:
            argv += ["--resume", spec.session_id]
        return argv

    def run(self, spec, abort_flag=None):
        argv = self.argv(spec)
        env = self.exec.env_for(None, node22_first=False)
        env.update(spec.env)
        log_dir = Path(spec.log_dir) if spec.log_dir else Path(spec.cwd) / ".vp"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / ("%s-%s.json" % (spec.tag, self.name))
        lines, raw_head = [], []
        start_ts, start_mono = utc_ms(), time.monotonic()

        def on_line(line):
            lines.append(line)
            if len(raw_head) < RAW_HEAD_LINES:
                raw_head.append(line[:400])

        with open(log_path, "w", encoding="utf-8") as fh:
            rc, timed_out, aborted = self.exec.stream(
                argv, env=env, cwd=spec.cwd, timeout_s=spec.timeout_s,
                on_line=on_line, abort_flag=abort_flag, log_fh=fh)
        blob = "\n".join(lines)
        res = None
        s = blob.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                res = json.loads(s)
            except ValueError:
                res = None
        if isinstance(res, list):        # stream-json style: last result object
            res = next((r for r in reversed(res)
                        if isinstance(r, dict) and r.get("type") == "result"), None)
        res = res if isinstance(res, dict) else {}
        usage = res.get("usage") if isinstance(res.get("usage"), dict) else {}
        base = dict(session_id=res.get("session_id") or spec.session_id,
                    usage={"tokens_in": usage.get("input_tokens"),
                           "tokens_out": usage.get("output_tokens"),
                           "tokens_reason": None,
                           "cache_read": usage.get("cache_read_input_tokens"),
                           "cache_write": usage.get("cache_creation_input_tokens"),
                           "cost": res.get("total_cost_usd")},
                    text=str(res.get("result") or "")[:20000], exit=rc,
                    duration_s=round(time.monotonic() - start_mono, 3),
                    start_ts=start_ts, end_ts=utc_ms(), raw_head=raw_head,
                    lines=len(lines), log_path=str(log_path), argv=argv,
                    runner=self.name,
                    denied_tools=[d for d in (res.get("permission_denials") or [])
                                  if isinstance(d, dict)][:10])
        if aborted:
            return TurnOutcome(STATUS_ABORTED, "STOP", **base)
        if timed_out:
            return TurnOutcome(STATUS_TIMEOUT, "killed after %ss" % spec.timeout_s,
                               **base)
        if not res:
            low = blob.lower()
            if _CLAUDE_RATE_RE.search(low):
                return TurnOutcome(STATUS_RATE, blob[:300], **base)
            if "not logged in" in low or "unauthorized" in low or "401" in low:
                return TurnOutcome(STATUS_AUTH, blob[:300], **base)
            return TurnOutcome(STATUS_CRASH, "no result JSON (exit %s): %s"
                               % (rc, blob[:200]), **base)
        if res.get("is_error"):
            txt = str(res.get("result") or res.get("error") or "")
            if _CLAUDE_RATE_RE.search(txt):
                return TurnOutcome(STATUS_RATE, txt[:300], **base)
            sub = str(res.get("subtype") or "")
            if "max_turns" in sub or "budget" in sub:
                return TurnOutcome(STATUS_INCOMPLETE, "%s: %s" % (sub, txt[:200]),
                                   **base)
            return TurnOutcome(STATUS_CRASH, "%s: %s" % (sub, txt[:300]), **base)
        # the record
        obj = res.get("structured_output")
        if not isinstance(obj, dict):
            obj = extract_json_fence(str(res.get("result") or ""))
        if isinstance(obj, dict) and spec.out_path:
            out = Path(spec.out_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
            ok, verrs = _validate(spec, out)
            if ok:
                return TurnOutcome(STATUS_DONE, "", record_path=str(out),
                                   structured=obj, **base)
            return TurnOutcome(STATUS_INCOMPLETE, "; ".join(verrs[:8]),
                               record_path=str(out), structured=obj, **base)
        if spec.out_path and Path(spec.out_path).exists():
            ok, verrs = _validate(spec, Path(spec.out_path))
            if ok:
                return TurnOutcome(STATUS_DONE, "record written by the role",
                                   record_path=str(spec.out_path), **base)
            return TurnOutcome(STATUS_INCOMPLETE, "; ".join(verrs[:8]),
                               record_path=str(spec.out_path), **base)
        if base["denied_tools"]:
            return TurnOutcome(STATUS_DENIED, "denied: %s"
                               % json.dumps(base["denied_tools"][:3])[:300], **base)
        if _looks_refused(base["text"]):
            return TurnOutcome(STATUS_REFUSED, base["text"][:300], **base)
        if not base["text"].strip():
            return TurnOutcome(STATUS_EMPTY, "result JSON with empty result", **base)
        if not spec.out_path:
            return TurnOutcome(STATUS_DONE, "no record expected", **base)
        return TurnOutcome(STATUS_INCOMPLETE, "no structured record in result", **base)


# --------------------------------------------------------------------------
# Agy (Gemini CLI, advisory only): `agy -p … --model … --output-format json
# --json-schema …`; the record is `structured_output` of the result JSON.
# --------------------------------------------------------------------------

class AgyRunner(ClaudeRunner):
    name = "agy"

    def __init__(self, exec_=None, binary="agy"):
        ClaudeRunner.__init__(self, exec_, binary)

    def argv(self, spec):
        argv = [self.binary, "-p", spec.prompt, "--output-format", "json"]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.schema_text:
            argv += ["--json-schema", spec.schema_text]
        return argv


# --------------------------------------------------------------------------

class _maybe_open(object):
    def __init__(self, path):
        self.path = path
        self.fh = None

    def __enter__(self):
        if self.path:
            try:
                self.fh = open(self.path, "w", encoding="utf-8")
            except OSError:
                self.fh = None
        return self.fh

    def __exit__(self, *exc):
        if self.fh:
            try:
                self.fh.close()
            except OSError:
                pass
        return False


def runner_for(name, exec_=None, binaries=None, opencode_transport="http"):
    binaries = binaries or {}
    if name == "opencode":
        if opencode_transport == "cli":
            return OpenCodeRunner(exec_, binaries.get("opencode", "opencode"))
        return OpenCodeHttpRunner(exec_)
    if name == "codex":
        return CodexRunner(exec_, binaries.get("codex", "codex"))
    if name == "claude":
        return ClaudeRunner(exec_, binaries.get("claude", "claude"))
    if name == "agy":
        return AgyRunner(exec_, binaries.get("agy", "agy"))
    raise ValueError("unknown runner %r" % name)
