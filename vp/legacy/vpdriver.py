#!/usr/bin/env python3
"""vpdriver.py -- the cheap-model turn daemon for the Voice Pod v9 control layer.

SPEC.md Sec.Driver.  Stdlib only.

    python3 vpdriver.py --roster RUN_ROOT/roster.json --once
    python3 vpdriver.py --roster RUN_ROOT/roster.json --loop

Every tick (default 5 s) the driver:
  * writes RUN_ROOT/driver.heartbeat
  * honours the STOP file (abort running turns, stop spawning, keep beating)
  * asks the store (through `python3 vpctl.py ... --json`) which items need a turn
  * creates a worktree from the SPEC template for ASSIGNED items that lack one
  * spawns at most `max_concurrent` OpenCode turns per server
  * classifies each finished turn, writes turns/<item>/<attempt>/<n>-<role>.json,
    exports the session, closes the attempt and hands the payload back to the
    store (submit-result / findings), escalating anything that is not DONE.

Every external process (git, opencode, opencode export, osascript) and the abort
HTTP POST go through the `Runner` class so tests can substitute a fake.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vpschema  # noqa: E402

__all__ = ["Driver", "Runner", "Store", "classify", "BUILDER_PROMPT",
           "JUNIOR_PROMPT"]

# --------------------------------------------------------------------------
# Fixed prompts.  These strings are byte-identical across every round of every
# item so the provider can cache the prefix; the per-item payload lives in the
# worktree files, never in the prompt.  DO NOT interpolate anything here.
# --------------------------------------------------------------------------
BUILDER_PROMPT = (
    "Read .vp/PACKET.md and .vp/BENCHMARK.md. If .vp/FINDINGS.json exists "
    "repair only its FAIL lines. Follow the agent rules. Write "
    ".vp/RESULT.json per .vp/RESULT_SCHEMA.json and stop."
)
JUNIOR_PROMPT = (
    "Grade the current HEAD commit of this worktree against .vp/BENCHMARK.md "
    "line by line. Write .vp/FINDINGS.json per .vp/FINDINGS_SCHEMA.json with "
    "file:line evidence and stop."
)
RESUME_PROMPT = "continue"

MAX_RESUMES = 3          # progress-stop resumes before STUCK
RAW_HEAD_LINES = 20      # raw event lines kept in the turn record
DEFAULT_INTERVAL = 5.0
QUOTA_COOLDOWN_S = 15 * 60
# Per-item backoff after a DRIVER-SIDE failure (a refused/failed vpctl
# call, a worktree that cannot be built).  Without it a permanently
# broken item is retried every tick and floods escalations.jsonl.
FAIL_BACKOFF_S = (60, 120, 300)
FAIL_CAP = 3            # consecutive driver-side failures -> STUCK

ROLE_BUILDER = "builder"
ROLE_JUNIOR = "junior"
ROLE_INFRA = "infra"
KIND_OF_ROLE = {ROLE_BUILDER: "build", ROLE_JUNIOR: "grade", ROLE_INFRA: "infra"}
OUTPUT_OF_ROLE = {ROLE_BUILDER: "RESULT.json", ROLE_JUNIOR: "FINDINGS.json",
                  ROLE_INFRA: "RESULT.json"}
PROMPT_OF_ROLE = {ROLE_BUILDER: BUILDER_PROMPT, ROLE_JUNIOR: JUNIOR_PROMPT,
                  ROLE_INFRA: BUILDER_PROMPT}

# item.status -> role whose turn is owed
ROLE_FOR_STATUS = {"BUILDING": ROLE_BUILDER, "GRADING": ROLE_JUNIOR}

NOTIFY_KINDS = ("STUCK", "QUOTA", "AUTH")
ESCALATION_KINDS = ("STUCK", "STALLED", "QUOTA", "AUTH", "INCOMPLETE")

NODE22_BIN = str(Path.home() / ".local" / "node-v22" / "bin")


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def utc_ms():
    """UTC ISO-8601 with milliseconds, per SPEC preamble."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") \
        + "%03dZ" % (datetime.now(timezone.utc).microsecond // 1000)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Runner -- the only place that touches the outside world
# --------------------------------------------------------------------------

class Proc(object):
    """Thin wrapper over Popen exposing just what the driver needs."""

    def __init__(self, popen):
        self._p = popen

    @property
    def pid(self):
        return self._p.pid

    def readline(self):
        line = self._p.stdout.readline()
        return line if line else ""

    def wait(self):
        try:
            self._p.stdout.close()
        except Exception:
            pass
        return self._p.wait()

    def poll(self):
        return self._p.poll()

    def kill(self):
        try:
            self._p.kill()
        except Exception:
            pass


class Runner(object):
    """All subprocess / HTTP / notification effects.  Fakeable in tests."""

    def env_for(self, overrides=None, node22_first=True):
        env = dict(os.environ)
        if node22_first and os.path.isdir(NODE22_BIN):
            env["PATH"] = NODE22_BIN + os.pathsep + env.get("PATH", "")
        if overrides:
            env.update({k: str(v) for k, v in overrides.items()})
        return env

    def run(self, argv, env=None, cwd=None, timeout=None):
        """-> (returncode, stdout, stderr).  Never raises on non-zero exit.

        stdin is ALWAYS /dev/null: `opencode run` blocks forever when stdin is
        an open pipe (root cause of a hang on the first real run), and an
        inherited terminal would let any child steal the daemon's input.
        """
        try:
            cp = subprocess.run(
                list(argv), env=env, cwd=cwd, timeout=timeout,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True)
            return cp.returncode, cp.stdout or "", cp.stderr or ""
        except subprocess.TimeoutExpired as exc:
            return 124, (exc.stdout or ""), "timeout after %ss" % timeout
        except OSError as exc:
            return 127, "", "%s" % exc

    def spawn(self, argv, env=None, cwd=None):
        """Start a streaming process; stderr folded into stdout.

        stdin is /dev/null for the same reason as `run` -- with a pipe on fd 0
        `opencode run` never exits.
        """
        popen = subprocess.Popen(
            list(argv), env=env, cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, bufsize=1)
        return Proc(popen)

    def post(self, url, timeout=10.0):
        """-> (status_int_or_0, body).  Used for /session/<sid>/abort."""
        req = urllib.request.Request(url, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:           # URLError, socket timeout, ...
            return 0, "%s" % exc

    def notify(self, title, message, osascript_bin="osascript"):
        safe_m = message.replace('"', "'").replace("\\", "/")[:220]
        safe_t = title.replace('"', "'")[:80]
        script = 'display notification "%s" with title "%s"' % (safe_m, safe_t)
        return self.run([osascript_bin, "-e", script])


# --------------------------------------------------------------------------
# Store -- vpctl.py over subprocess, always --json
# --------------------------------------------------------------------------

class StoreError(Exception):
    pass


class Store(object):
    """Decoupled access to vpstore via the vpctl CLI.

    Exit codes per SPEC: 0 ok, 2 usage, 3 refused, 4 conflict.
    """

    def __init__(self, runner, vpctl_cmd, run_root, cwd=None):
        self.runner = runner
        self.vpctl_cmd = list(vpctl_cmd)
        self.run_root = Path(run_root)
        self.cwd = cwd
        self.calls = []          # (argv, rc) -- useful for audits and tests
        self._lock = threading.Lock()

    def call(self, args, allow=(0,)):
        argv = list(self.vpctl_cmd) + [str(a) for a in args] + ["--json"]
        rc, out, err = self.runner.run(argv, cwd=self.cwd)
        with self._lock:
            self.calls.append((argv, rc))
        data = None
        if out.strip():
            try:
                data = json.loads(out)
            except ValueError:
                data = {"raw": out.strip()}
        if data is None:
            data = {"raw": "", "stderr": err.strip()}
        if rc not in allow:
            raise StoreError("vpctl %s exited %d: %s"
                             % (" ".join(args[:2]), rc, (err or out).strip()[:400]))
        return rc, data

    # --- reads -----------------------------------------------------------
    def report_items(self):
        _rc, data = self.call(["report", "items"])
        if isinstance(data, dict):
            items = data.get("items", [])
        elif isinstance(data, list):
            items = data
        else:
            items = []
        return [i for i in items if isinstance(i, dict)]

    def running_attempt_items(self):
        """Items that already carry a RUNNING attempt row, per `report
        liveness`.  Guards against a second driver (or a restarted one)
        opening a concurrent attempt on the same item."""
        try:
            _rc, data = self.call(["report", "liveness"])
        except StoreError:
            return set()
        rows = (data or {}).get("attempts_running") or []
        return {r.get("item") for r in rows
                if isinstance(r, dict) and r.get("item")}

    # --- writes ----------------------------------------------------------
    def packet_show(self, item):
        """-> the item's packet row (packet_path / benchmark_path / base_sha),
        or {} when the store cannot produce one."""
        try:
            _rc, data = self.call(["packet", "show", item], allow=(0, 2, 3))
        except StoreError:
            return {}
        if isinstance(data, dict):
            row = data.get("packet") if isinstance(data.get("packet"), dict) \
                else data
            if isinstance(row, dict) and (row.get("packet_path")
                                          or row.get("benchmark_path")):
                return row
        return {}

    def claim(self, item, role_id, worktree, expected_rev):
        """ASSIGNED -> BUILDING, one writer per item (expected_rev check)."""
        return self.call(["claim", item, "--role", role_id,
                          "--worktree", str(worktree),
                          "--expected-rev", str(expected_rev)])

    def turn_start(self, item, kind, session, server, agent, model, variant):
        _rc, data = self.call([
            "turn", "start", item, "--kind", kind, "--session", session or "-",
            "--server", server, "--agent", agent, "--model", model,
            "--variant", variant])
        aid = None
        if isinstance(data, dict):
            aid = data.get("attempt_id") or data.get("attempt")
        return aid

    def turn_end(self, attempt_id, status, result_path, usage=None):
        args = ["turn", "end", str(attempt_id), "--status", status,
                "--result", str(result_path)]
        usage = usage or {}
        for flag, key in (("--tokens-in", "tokens_in"),
                          ("--tokens-out", "tokens_out"),
                          ("--tokens-reason", "tokens_reason"),
                          ("--cache-read", "cache_read"),
                          ("--cost", "cost")):
            if usage.get(key) is not None:
                args += [flag, str(usage[key])]
        return self.call(args)

    def submit_result(self, item, commit, result_path):
        return self.call(["submit-result", item, "--commit", commit,
                          "--result", str(result_path)])

    def findings(self, item, path):
        return self.call(["findings", item, "--path", str(path)])

    def escalate(self, kind, item, attempt, detail, ref=None):
        """vpctl escalate ...; falls back to appending escalations.jsonl when
        vpctl does not implement the verb (exit 2 = usage)."""
        # vpctl escalate takes --kind --item --attempt --detail only; the
        # record path travels inside --detail.
        if ref:
            detail = "%s [record=%s]" % (detail, ref)
        args = ["escalate", "--kind", kind, "--item", item or "-",
                "--detail", detail]
        if attempt:
            args += ["--attempt", str(attempt)]
        try:
            rc, _data = self.call(args, allow=(0, 2, 3))
        except StoreError:
            rc = 2
        if rc == 0:
            return True
        self._append_escalation(kind, item, attempt, detail, ref)
        return False

    def _append_escalation(self, kind, item, attempt, detail, ref):
        row = {"ts": utc_ms(), "mono": time.monotonic(), "kind": kind,
               "item": item, "attempt": attempt, "role": "daemon",
               "ref": ref, "detail": detail, "source": "vpdriver"}
        path = self.run_root / "escalations.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError:
            pass


# --------------------------------------------------------------------------
# event-stream parsing  (--format json => one JSON object per line)
# --------------------------------------------------------------------------

_SESSION_KEYS = ("sessionID", "session_id", "sessionId", "session")


def find_session_id(obj, _depth=0):
    """Tolerant search for the session id anywhere in an event object."""
    if _depth > 8:
        return None
    if isinstance(obj, dict):
        for key in _SESSION_KEYS:
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
            if isinstance(val, dict):
                inner = val.get("id")
                if isinstance(inner, str) and inner:
                    return inner
        info = obj.get("info")
        if isinstance(info, dict):
            inner = info.get("id")
            if isinstance(inner, str) and inner:
                return inner
        for val in obj.values():
            got = find_session_id(val, _depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for val in obj:
            got = find_session_id(val, _depth + 1)
            if got:
                return got
    return None


def _find_key(obj, key, _depth=0):
    if _depth > 8:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for val in obj.values():
            got = _find_key(val, key, _depth + 1)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for val in obj:
            got = _find_key(val, key, _depth + 1)
            if got is not None:
                return got
    return None


def _num(val):
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return val
    return None


class EventAccumulator(object):
    """Folds the `opencode run --format json` event stream.

    Verified shape (one JSON object per line):
        {"type":"step_start|tool_use|text|step_finish",
         "timestamp":<ms>,"sessionID":"ses_…","part":{…}}
      * `sessionID` is top-level on EVERY event; the first one wins.
      * `text.part.text` is the assistant text.
      * `step_finish.part` carries `reason` ("stop"), `cost` and
        `tokens:{total,input,output,reasoning,cache:{write,read}}`.
        Usage is SUMMED over every step_finish -- one run is many steps.
      * a denied tool is `tool_use` with `part.state.status == "error"` and an
        `error` string containing "rule which prevents".

    Every field is read defensively: the accumulator must survive a format
    change without losing the session id or the raw lines.
    """

    DENIAL_MARKER = "rule which prevents"

    def __init__(self):
        self.session_id = None
        self.texts = []
        self.tokens_in = 0
        self.tokens_out = 0
        self.tokens_reason = 0
        self.cache_read = 0
        self.cache_write = 0
        self.tokens_total = 0
        self.cost = 0.0
        self.steps = 0            # step_finish events seen
        self.last_reason = None   # reason of the LAST step_finish
        self.denied_tools = []
        self.saw_error = False

    # -- usage is only meaningful when at least one step_finish landed ----
    def usage(self):
        if not self.steps:
            return {"tokens_in": None, "tokens_out": None,
                    "tokens_reason": None, "cache_read": None, "cost": None}
        return {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "tokens_reason": self.tokens_reason,
                "cache_read": self.cache_read,
                "cost": round(self.cost, 8)}

    def text(self):
        return "\n".join(self.texts)

    def feed(self, obj):
        if not isinstance(obj, dict):
            return
        if self.session_id is None:
            sid = obj.get("sessionID")
            if isinstance(sid, str) and sid:
                self.session_id = sid
            else:
                self.session_id = find_session_id(obj)

        typ = str(obj.get("type", ""))
        part = obj.get("part")
        if not isinstance(part, dict):
            part = {}

        if typ == "text":
            val = part.get("text", obj.get("text"))
            if isinstance(val, str) and val.strip():
                self.texts.append(val)
        elif typ == "step_finish":
            self.steps += 1
            reason = part.get("reason", obj.get("reason"))
            if isinstance(reason, str) and reason:
                self.last_reason = reason
            self._add_tokens(part.get("tokens", obj.get("tokens")))
            cost = _num(part.get("cost", obj.get("cost")))
            if cost is not None:
                self.cost += cost
        elif typ == "tool_use":
            state = part.get("state")
            if isinstance(state, dict):
                if str(state.get("status", "")).lower() == "error":
                    err = state.get("error") or part.get("error") or ""
                    if not isinstance(err, str):
                        err = json.dumps(err)
                    tool = part.get("tool") or part.get("name") or "?"
                    if self.DENIAL_MARKER in err.lower():
                        self.denied_tools.append({"tool": tool,
                                                  "error": err[:300]})
                    else:
                        self.saw_error = True
        if "error" in typ.lower():
            self.saw_error = True

    def _add_tokens(self, tokens):
        if not isinstance(tokens, dict):
            return
        for key, attr in (("input", "tokens_in"), ("output", "tokens_out"),
                          ("reasoning", "tokens_reason"),
                          ("total", "tokens_total")):
            val = _num(tokens.get(key))
            if val is not None:
                setattr(self, attr, getattr(self, attr) + val)
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            for key, attr in (("read", "cache_read"), ("write", "cache_write")):
                val = _num(cache.get(key))
                if val is not None:
                    setattr(self, attr, getattr(self, attr) + val)


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

AUTH_MARKERS = ("401", "unauthorized", "invalid api key", "invalid_api_key")
QUOTA_MARKERS = ("429", "rate limit", "rate_limit", "quota",
                 "too many requests")

PROGRESS_STOP = "PROGRESS_STOP"   # internal, never written as a status


def classify(blob, assistant_text, output_path, role, saw_error=False,
             last_reason=None):
    """Return (status, detail).

    `blob` is the whole captured stdout of the turn, lowercased by the caller
    or not -- this function lowercases defensively.  Exit code is deliberately
    NOT an input: exit 0 is never success by itself (SPEC / task brief).
    `last_reason` is the `reason` of the LAST step_finish event: "stop" with no
    output file on disk is exactly the Muse progress-line stop.
    """
    low = (blob or "").lower()
    for marker in AUTH_MARKERS:
        if marker in low:
            return "AUTH", "auth marker %r in stream" % marker
    for marker in QUOTA_MARKERS:
        if marker in low:
            return "QUOTA", "quota marker %r in stream" % marker

    provider_error = ("provider" in low and "error" in low)
    stream_error = saw_error or ("error" in low)
    has_text = bool((assistant_text or "").strip())

    exists = Path(output_path).exists()
    if not exists:
        if last_reason == "stop":
            # the model ended its turn cleanly and simply did not write the
            # file -- a progress-line stop, resumable with "continue".
            return PROGRESS_STOP, ("step_finish reason=stop with no %s"
                                   % Path(output_path).name)
        if provider_error or (stream_error and not has_text):
            return "STALLED", "stream error and no %s" % Path(output_path).name
        return PROGRESS_STOP, "no %s and no error" % Path(output_path).name

    if role == ROLE_JUNIOR:
        ok, errors = vpschema.validate_findings(output_path)
    else:
        ok, errors = vpschema.validate_result(output_path)
    if ok:
        return "DONE", ""
    return "INCOMPLETE", "; ".join(errors[:8])


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

class Driver(object):

    def __init__(self, roster_path, runner=None, vpctl_cmd=None,
                 stop_file=None, interval=DEFAULT_INTERVAL,
                 git_bin="git", opencode_bin="opencode",
                 osascript_bin="osascript", store=None):
        self.roster_path = Path(roster_path).resolve()
        self.run_root = self.roster_path.parent
        with open(self.roster_path, "r", encoding="utf-8") as fh:
            self.roster = json.load(fh)
        self.runner = runner or Runner()
        self.interval = float(interval)
        self.git_bin = git_bin
        self.opencode_bin = opencode_bin
        self.osascript_bin = osascript_bin

        here = Path(__file__).resolve().parent
        self.vpctl_cmd = list(vpctl_cmd) if vpctl_cmd else \
            [sys.executable, str(here / "vpctl.py")]
        self.store = store or Store(self.runner, self.vpctl_cmd, self.run_root,
                                    cwd=str(here))

        self.trunk = Path(os.path.expanduser(self.roster["trunk"]))
        self.worktrees_root = Path(os.path.expanduser(self.roster["worktrees"]))
        # $CN is the parent of the trunk checkout.
        cn = self.trunk.parent
        self.stop_file = Path(stop_file) if stop_file else \
            cn / "test-logs" / "driver" / "STOP"
        self.heartbeat_path = self.run_root / "driver.heartbeat"
        self.turns_root = self.run_root / "turns"

        self.zen_fallback = bool(self.roster.get("zen_fallback", False))
        self.servers = {}
        for name, cfg in self.roster.get("servers", {}).items():
            self.servers[name] = {
                "name": name,
                "port": int(cfg["port"]),
                "data": os.path.expanduser(cfg.get("data", "")),
                "max_concurrent": int(cfg.get("max_concurrent", 1)),
                "skipped_until": 0.0,
                "skip_reason": None,
            }
        self.groups = {str(k): v for k, v in self.roster.get("groups", {}).items()}
        self.models = self.roster.get("models", {})

        self._lock = threading.Lock()
        self._active = {}        # server -> in-flight turn count
        self._live_items = set()  # items with a turn thread running
        self._running = {}       # item -> {"proc","sid","server","attempt",...}
        self._threads = []
        # item -> {"count", "next_try" (mono), "rev", "stuck"}
        self._fail = {}
        self._stopping = False
        self._aborted_once = False
        self.tick_count = 0

    # -- roster / server routing -----------------------------------------

    def server_for_group(self, group_no):
        name = self.groups.get(str(group_no))
        if name is None:
            name = self.groups.get("infra")
        if name is None and self.servers:
            name = sorted(self.servers)[0]
        return name

    def _is_skipped(self, name):
        srv = self.servers.get(name)
        return bool(srv) and srv["skipped_until"] > time.time()

    def pick_server(self, group_no):
        """Primary server, or zen when the primary is quota-skipped and
        roster.zen_fallback is true.  -> (name, tier) or (None, None)."""
        name = self.server_for_group(group_no)
        if name and not self._is_skipped(name):
            return name, "primary"
        if self.zen_fallback and "zen" in self.servers and \
                not self._is_skipped("zen"):
            return "zen", "zen_fallback"
        return None, None

    def _try_acquire(self, name):
        srv = self.servers[name]
        with self._lock:
            used = self._active.get(name, 0)
            if used >= srv["max_concurrent"]:
                return False
            self._active[name] = used + 1
            return True

    def _release(self, name):
        with self._lock:
            self._active[name] = max(0, self._active.get(name, 1) - 1)

    # -- per-item driver-side failure backoff ----------------------------

    def _fail_entry(self, item, rev):
        """Current backoff entry for the item, reset when the store row's rev
        moved (a new rev means somebody changed the thing that was broken)."""
        with self._lock:
            ent = self._fail.get(item)
            if ent is not None and ent.get("rev") != rev:
                ent = None
                self._fail.pop(item, None)
            return ent

    def item_is_backed_off(self, item, rev):
        """True when this item must be skipped this tick."""
        ent = self._fail_entry(item, rev)
        if not ent:
            return False
        if ent.get("stuck"):
            return True          # no further retries until rev changes
        return time.monotonic() < ent.get("next_try", 0.0)

    def note_item_failure(self, item, rev, detail, attempt=None):
        """Record one driver-side failure and escalate EXACTLY ONCE for it.

        Escalates STALLED with the running count, or STUCK on the third
        consecutive failure, after which the item is not retried until its rev
        changes.  Returns the new count.
        """
        with self._lock:
            ent = self._fail.get(item)
            if ent is None or ent.get("rev") != rev:
                ent = {"count": 0, "next_try": 0.0, "rev": rev, "stuck": False}
                self._fail[item] = ent
            ent["count"] += 1
            count = ent["count"]
            idx = min(count - 1, len(FAIL_BACKOFF_S) - 1)
            ent["next_try"] = time.monotonic() + FAIL_BACKOFF_S[idx]
            if count >= FAIL_CAP:
                ent["stuck"] = True
        if count >= FAIL_CAP:
            self._escalate("STUCK", item, attempt,
                           "%d consecutive driver-side failures, no further "
                           "retries until rev %s changes: %s"
                           % (count, rev, detail), None)
        else:
            self.store.escalate(
                "STALLED", item, attempt,
                "driver-side failure %d/%d (retry in %ds): %s"
                % (count, FAIL_CAP, FAIL_BACKOFF_S[idx], detail))
        return count

    def clear_item_failures(self, item):
        with self._lock:
            self._fail.pop(item, None)

    def mark_server_skipped(self, name, reason, cooldown=QUOTA_COOLDOWN_S):
        srv = self.servers.get(name)
        if not srv:
            return
        srv["skipped_until"] = time.time() + cooldown
        srv["skip_reason"] = reason

    # -- worktree template -----------------------------------------------

    def worktree_path(self, item):
        return self.worktrees_root / item

    def ensure_worktree(self, rec):
        """Create the worktree from the SPEC template if it is missing."""
        item = rec["item"]
        wt = Path(rec.get("worktree") or self.worktree_path(item))
        if not wt.exists():
            base = rec.get("base_sha") or rec.get("base") or "HEAD"
            wt.parent.mkdir(parents=True, exist_ok=True)
            rc, out, err = self.runner.run(
                [self.git_bin, "-C", str(self.trunk), "worktree", "add",
                 str(wt), "-b", "vp/%s" % item, str(base)])
            if rc != 0 and not wt.exists():
                raise StoreError("worktree add failed for %s: %s"
                                 % (item, (err or out).strip()[:300]))
        # the three read-only symlinks
        for rel in ("agent/.venv", "portal/node_modules", "platform/.venv"):
            link = wt / rel
            target = self.trunk / rel
            try:
                link.parent.mkdir(parents=True, exist_ok=True)
                if not link.exists() and not link.is_symlink():
                    os.symlink(str(target), str(link))
            except OSError:
                pass
        self._write_vp_files(wt, rec)
        return wt

    def _resolve_packet_paths(self, rec):
        """`report items` rows now carry packet_path/benchmark_path; fall back
        to `vpctl packet show <item>` when a row predates that."""
        if rec.get("packet_path") and rec.get("benchmark_path"):
            return rec
        row = self.store.packet_show(rec.get("item"))
        for key in ("packet_path", "benchmark_path"):
            if not rec.get(key) and row.get(key):
                rec[key] = row[key]
        return rec

    def _write_vp_files(self, wt, rec):
        vp = wt / ".vp"
        vp.mkdir(parents=True, exist_ok=True)
        self._resolve_packet_paths(rec)
        for key, name in (("packet_path", "PACKET.md"),
                          ("benchmark_path", "BENCHMARK.md")):
            src = rec.get(key)
            dst = vp / name
            if src and Path(src).exists():
                dst.write_text(Path(src).read_text(encoding="utf-8"),
                               encoding="utf-8")
            elif dst.exists() and dst.stat().st_size > 0:
                pass          # already placed by an earlier round
            else:
                # Never hand a model an empty payload: an empty BENCHMARK.md
                # grades as vacuously satisfied and an empty PACKET.md wastes
                # a whole turn.  Refuse loudly instead.
                raise StoreError(
                    "item %s: no usable %s (rec[%r]=%r) and %s is absent/empty"
                    % (rec.get("item"), name, key, src, dst))
        here = Path(__file__).resolve().parent
        for name, doc in (("RESULT_SCHEMA.json", vpschema.RESULT_SCHEMA_DOC),
                          ("FINDINGS_SCHEMA.json", vpschema.FINDINGS_SCHEMA_DOC)):
            onfile = here / "schemas" / name
            text = onfile.read_text(encoding="utf-8") if onfile.exists() \
                else json.dumps(doc, indent=2)
            (vp / name).write_text(text, encoding="utf-8")

    def builder_role_id(self, rec):
        """Role id the driver claims an item as.  SPEC's role table is keyed by
        role_id; the daemon's builders are named per group."""
        return rec.get("builder_role") or "builder%s" % rec.get("group_no")

    def claim_item(self, rec, worktree):
        """`vpctl claim` right after the worktree exists.  Without this an
        ASSIGNED item never reaches BUILDING and sits forever."""
        item = rec["item"]
        rev = rec.get("rev")
        if rev is None:
            raise StoreError("item %s: no rev in the store row, cannot claim"
                             % item)
        self.store.claim(item, self.builder_role_id(rec), worktree, rev)
        return True

    # -- argv -------------------------------------------------------------

    def build_argv(self, server, worktree, agent, model, variant, title,
                   prompt, session=None):
        srv = self.servers[server]
        argv = [self.opencode_bin, "run",
                "--attach", "http://127.0.0.1:%d" % srv["port"],
                "--dir", str(worktree),
                "--agent", agent,
                "--model", model,
                "--variant", variant,
                "--format", "json",
                "--title", title]
        if session:
            argv += ["--session", session]
        argv.append(prompt)
        return argv

    def _env_for_server(self, server):
        srv = self.servers[server]
        over = {}
        if srv["data"]:
            over["XDG_DATA_HOME"] = srv["data"]
        return self.runner.env_for(over)

    # -- the tick ---------------------------------------------------------

    def stop_requested(self):
        return self.stop_file.exists()

    def write_heartbeat(self, stopping):
        with self._lock:
            active = dict(self._active)
            live = sorted(self._live_items)
        payload = {
            "ts": utc_ms(), "mono": time.monotonic(), "pid": os.getpid(),
            "tick": self.tick_count, "stopping": bool(stopping),
            "roster": str(self.roster_path), "run_root": str(self.run_root),
            "active_by_server": active, "live_items": live,
            "servers": {n: {"port": s["port"],
                            "skipped_until": s["skipped_until"],
                            "skip_reason": s["skip_reason"]}
                        for n, s in self.servers.items()},
        }
        try:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.heartbeat_path.with_suffix(".heartbeat.tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True),
                           encoding="utf-8")
            os.replace(str(tmp), str(self.heartbeat_path))
        except OSError:
            pass

    def abort_running(self):
        """POST /session/<sid>/abort for every in-flight turn (idempotent)."""
        with self._lock:
            live = list(self._running.items())
        for item, info in live:
            sid = info.get("sid")
            port = info.get("port")
            if not sid or not port:
                continue
            if info.get("aborted"):
                continue
            info["aborted"] = True
            url = "http://127.0.0.1:%d/session/%s/abort" % (port, sid)
            self.runner.post(url)

    def tick(self):
        self.tick_count += 1
        stopping = self.stop_requested()
        if stopping:
            self._stopping = True
        self.write_heartbeat(stopping)
        if stopping:
            self.abort_running()
            return 0

        try:
            items = self.store.report_items()
        except StoreError as exc:
            self.store.escalate("STALLED", None, None,
                                "vpctl report items failed: %s" % exc)
            return 0

        busy_items = self.store.running_attempt_items()

        spawned = 0
        for rec in items:
            if self._stopping or self.stop_requested():
                break
            item = rec.get("item")
            status = (rec.get("status") or "").upper()
            if not item:
                continue
            with self._lock:
                if item in self._live_items:
                    continue
            if self.item_is_backed_off(item, rec.get("rev")):
                continue

            if status == "ASSIGNED":
                try:
                    wt = self.ensure_worktree(rec)
                    self.claim_item(rec, wt)
                except StoreError as exc:
                    self.note_item_failure(item, rec.get("rev"), str(exc))
                continue

            role = ROLE_FOR_STATUS.get(status)
            if role is None:
                continue
            if rec.get("running_attempt") or item in busy_items:
                continue

            server, tier = self.pick_server(rec.get("group_no"))
            if server is None:
                continue
            if not self._try_acquire(server):
                continue
            with self._lock:
                self._live_items.add(item)

            th = threading.Thread(
                target=self._turn_thread,
                args=(dict(rec), role, server, tier),
                name="vp-turn-%s" % item, daemon=True)
            self._threads.append(th)
            th.start()
            spawned += 1
        return spawned

    def _turn_thread(self, rec, role, server, tier):
        item = rec.get("item")
        try:
            self.run_turn(rec, role, server, tier)
            self.clear_item_failures(item)
        except Exception as exc:                       # never kill the daemon
            try:
                self.note_item_failure(item, rec.get("rev"),
                                       "%s: %s" % (type(exc).__name__, exc))
            except Exception:
                pass
        finally:
            self._release(server)
            with self._lock:
                self._live_items.discard(item)
                self._running.pop(item, None)

    # -- one full turn ----------------------------------------------------

    def run_turn(self, rec, role, server, tier):
        item = rec["item"]
        wt = self.ensure_worktree(rec)
        mcfg = self.models.get(role) or self.models.get(ROLE_BUILDER) or {}
        agent = mcfg.get("agent", "vp-%s" % role)
        model = mcfg.get("model", "opencode-go/unknown")
        variant = mcfg.get("variant", "max")
        kind = KIND_OF_ROLE[role]
        rnd = rec.get("round", 0) or 0
        out_name = OUTPUT_OF_ROLE[role]
        out_path = wt / ".vp" / out_name

        # A stale payload from an earlier round would read as this turn's
        # output; remove it before the first process of the attempt.
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass

        attempt_id = self.store.turn_start(
            item, kind, rec.get("session_id") or "", server, agent, model,
            variant) or "a0"
        title = "%s %s r%s" % (item, kind, rnd)

        sid = rec.get("session_id") or None
        resumes = 0
        n = 0
        status = None
        detail = ""
        usage = {}
        record_path = None
        while True:
            n += 1
            prompt = PROMPT_OF_ROLE[role] if n == 1 else RESUME_PROMPT
            argv = self.build_argv(server, wt, agent, model, variant, title,
                                   prompt, session=sid)
            res = self._exec_turn(item, argv, server, attempt_id)
            sid = res["session_id"] or sid
            status, detail = classify(res["blob"], res["text"], out_path, role,
                                      saw_error=res["saw_error"],
                                      last_reason=res.get("last_reason"))
            if self._stopping or res["aborted"]:
                status, detail = "ABORTED", "STOP file"
            usage = res["usage"]
            record_path = self._write_turn_record(
                item, attempt_id, n, role, kind, rnd, server, tier, sid, agent,
                model, variant, prompt, argv, res, status, detail, wt,
                out_path)
            if status == PROGRESS_STOP and not self._stopping:
                if resumes < MAX_RESUMES:
                    resumes += 1
                    continue
                status, detail = "STUCK", \
                    "no %s after %d resumes" % (out_name, MAX_RESUMES)
                record_path = self._rewrite_status(record_path, status, detail)
            break

        export_path = None
        if sid and status != "ABORTED":
            export_path = self._export_session(item, attempt_id, n, role, sid,
                                               server)

        self.store.turn_end(attempt_id, status, record_path, usage)

        if status == "DONE":
            self._deliver(item, role, wt, out_path, record_path)
        elif status == "ABORTED":
            pass
        else:
            if status == "QUOTA":
                self.mark_server_skipped(server, detail)
            self._escalate(status, item, attempt_id, detail, record_path)
        return status

    def _exec_turn(self, item, argv, server, attempt_id):
        srv = self.servers[server]
        env = self._env_for_server(server)
        start_ts, start_mono = utc_ms(), time.monotonic()
        lines = []
        raw_head = []
        acc = EventAccumulator()
        try:
            proc = self.runner.spawn(argv, env=env, cwd=str(self.trunk))
        except OSError as exc:
            return {"blob": "spawn failed: %s" % exc, "text": "",
                    "session_id": None, "usage": acc.usage(), "exit": 127,
                    "start_ts": start_ts, "end_ts": utc_ms(),
                    "duration": 0.0, "raw_head": [], "saw_error": True,
                    "aborted": False, "lines": 0, "last_reason": None,
                    "steps": 0, "denied_tools": []}
        with self._lock:
            self._running[item] = {"proc": proc, "sid": None,
                                   "server": server, "port": srv["port"],
                                   "attempt": attempt_id, "aborted": False}
        while True:
            line = proc.readline()
            if line == "":
                break
            line = line.rstrip("\n")
            lines.append(line)
            if len(raw_head) < RAW_HEAD_LINES:
                raw_head.append(line)
            stripped = line.strip()
            if not (stripped.startswith("{") or stripped.startswith("[")):
                continue
            try:
                obj = json.loads(stripped)
            except ValueError:
                continue
            had_sid = acc.session_id
            acc.feed(obj)
            if not had_sid and acc.session_id:
                with self._lock:
                    info = self._running.get(item)
                    if info is not None:
                        info["sid"] = acc.session_id
        rc = proc.wait()
        with self._lock:
            info = self._running.pop(item, None)
        aborted = bool(info and info.get("aborted"))
        return {"blob": "\n".join(lines), "text": acc.text(),
                "session_id": acc.session_id, "usage": acc.usage(), "exit": rc,
                "start_ts": start_ts, "end_ts": utc_ms(),
                "duration": round(time.monotonic() - start_mono, 3),
                "raw_head": raw_head, "saw_error": acc.saw_error,
                "aborted": aborted, "lines": len(lines),
                "last_reason": acc.last_reason, "steps": acc.steps,
                "denied_tools": acc.denied_tools}

    # -- records ----------------------------------------------------------

    def _turn_dir(self, item, attempt_id):
        d = self.turns_root / str(item) / str(attempt_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_turn_record(self, item, attempt_id, n, role, kind, rnd, server,
                           tier, sid, agent, model, variant, prompt, argv, res,
                           status, detail, wt, out_path):
        srv = self.servers[server]
        rec = {
            "item": item, "attempt": attempt_id, "n": n, "role": role,
            "kind": kind, "round": rnd,
            "server": server, "port": srv["port"], "tier": tier,
            "session_id": sid, "agent": agent, "model": model,
            "variant": variant,
            "prompt_sha256": sha256_text(prompt),
            "prompt_bytes": len(prompt.encode("utf-8")),
            "argv": list(argv),
            "start_ts": res["start_ts"], "end_ts": res["end_ts"],
            "duration_s": res["duration"],
            "exit_code": res["exit"],
            "classification": status, "classification_detail": detail,
            "tokens_in": res["usage"].get("tokens_in"),
            "tokens_out": res["usage"].get("tokens_out"),
            "tokens_reason": res["usage"].get("tokens_reason"),
            "cache_read": res["usage"].get("cache_read"),
            "cost": res["usage"].get("cost"),
            "event_lines": res["lines"],
            "steps": res.get("steps"),
            "last_step_reason": res.get("last_reason"),
            "denied_tools": res.get("denied_tools") or [],
            "raw_head": res["raw_head"],
            "paths": {
                "worktree": str(wt),
                "output": str(out_path),
                "output_exists": out_path.exists(),
                "packet": str(wt / ".vp" / "PACKET.md"),
                "benchmark": str(wt / ".vp" / "BENCHMARK.md"),
                "export": None,
            },
            "xdg_data_home": srv["data"],
        }
        path = self._turn_dir(item, attempt_id) / ("%d-%s.json" % (n, role))
        rec["paths"]["record"] = str(path)
        path.write_text(json.dumps(rec, indent=2, sort_keys=True),
                        encoding="utf-8")
        return path

    def _rewrite_status(self, record_path, status, detail):
        try:
            rec = json.loads(Path(record_path).read_text(encoding="utf-8"))
            rec["classification"] = status
            rec["classification_detail"] = detail
            Path(record_path).write_text(
                json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
        except (OSError, ValueError):
            pass
        return record_path

    def _export_session(self, item, attempt_id, n, role, sid, server):
        """`opencode export <sid>` with the SAME XDG_DATA_HOME as the server."""
        env = self._env_for_server(server)
        rc, out, err = self.runner.run([self.opencode_bin, "export", sid],
                                       env=env, cwd=str(self.trunk))
        path = self._turn_dir(item, attempt_id) / ("%d-%s.export.json" % (n, role))
        if rc == 0 and out.strip():
            path.write_text(out, encoding="utf-8")
        else:
            path.write_text(json.dumps(
                {"error": "export failed", "exit": rc,
                 "stderr": (err or out)[:4000], "session_id": sid},
                indent=2), encoding="utf-8")
        rp = self._turn_dir(item, attempt_id) / ("%d-%s.json" % (n, role))
        try:
            rec = json.loads(rp.read_text(encoding="utf-8"))
            rec["paths"]["export"] = str(path)
            rp.write_text(json.dumps(rec, indent=2, sort_keys=True),
                          encoding="utf-8")
        except (OSError, ValueError):
            pass
        return path

    # -- hand-back --------------------------------------------------------

    def _head_sha(self, wt):
        rc, out, _err = self.runner.run(
            [self.git_bin, "-C", str(wt), "rev-parse", "HEAD"])
        return out.strip() if rc == 0 and out.strip() else None

    def _deliver(self, item, role, wt, out_path, record_path):
        if role == ROLE_JUNIOR:
            self.store.findings(item, out_path)
            return
        commit = None
        try:
            commit = json.loads(out_path.read_text(encoding="utf-8")).get("commit")
        except (OSError, ValueError):
            commit = None
        commit = commit or self._head_sha(wt) or "HEAD"
        self.store.submit_result(item, commit, out_path)

    def _escalate(self, kind, item, attempt_id, detail, record_path):
        if kind not in ESCALATION_KINDS:
            return
        self.store.escalate(kind, item, attempt_id, detail,
                            ref=str(record_path) if record_path else None)
        if kind in NOTIFY_KINDS:
            self.runner.notify("vp %s" % kind, "%s: %s" % (item, detail),
                               osascript_bin=self.osascript_bin)

    # -- loops ------------------------------------------------------------

    def join(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        for th in list(self._threads):
            left = None if deadline is None else max(
                0.0, deadline - time.monotonic())
            th.join(left)
        self._threads = [t for t in self._threads if t.is_alive()]

    def run_once(self):
        self.tick()
        self.join()
        self.write_heartbeat(self.stop_requested())
        return 0

    def loop(self, max_ticks=None, idle_exit=False):
        ticks = 0
        while True:
            self.tick()
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            if self._stopping:
                # keep heartbeating while the aborts drain, then leave
                self.join(timeout=self.interval * 4)
                self.write_heartbeat(True)
                if not self._threads:
                    break
            if idle_exit and not self._threads and not self._live_items:
                break
            time.sleep(self.interval)
        self.join(timeout=self.interval * 6)
        self.write_heartbeat(self.stop_requested())
        return 0


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="vpdriver.py",
                                 description="Voice Pod v9 turn daemon")
    ap.add_argument("--roster", required=True, help="RUN_ROOT/roster.json")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="one tick, then drain")
    mode.add_argument("--loop", action="store_true", help="tick until STOP")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--stop-file", default=None,
                    help="default $CN/test-logs/driver/STOP")
    ap.add_argument("--vpctl", default=None,
                    help="vpctl command (default: <python> vpctl.py)")
    ap.add_argument("--max-ticks", type=int, default=None)
    args = ap.parse_args(argv)

    vpctl_cmd = args.vpctl.split() if args.vpctl else None
    drv = Driver(args.roster, vpctl_cmd=vpctl_cmd, stop_file=args.stop_file,
                 interval=args.interval)
    if args.once:
        return drv.run_once()
    return drv.loop(max_ticks=args.max_ticks)


if __name__ == "__main__":
    raise SystemExit(main())
