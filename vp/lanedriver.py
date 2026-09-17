#!/usr/bin/env python3
"""lanedriver.py -- the v13 tick loop over the Chief-era scheduler.

    python3 lanedriver.py --roster RUN_ROOT/roster.json --loop
    python3 lanedriver.py --roster RUN_ROOT/roster.json --once

Task authority is `orchestration_control.py` + `run-state.json`; this daemon
never edits that file.  Every write goes through a subprocess call to the
scheduler (claim / start / complete / instantiate / promote / drain / reconcile)
and every call is logged to `control.jsonl` (argv, rc, stdout, sequence
before/after).  Reads use `ready` / `frontier`; a read-only view of the state
file is taken only for rows those verbs do not expose (RUNNING attempts to
adopt after a restart, active claims for a local conflict pre-check, the
registered candidate).

  scheduler state       driver action                          -> scheduler verb
  READY                 worktree, claim owned paths, start     claim, start
  CLAIMED (orphan)      start, adopt the attempt               start
  RUNNING (orphan)      adopt the attempt (resume session)     --
  RUNNING (live)        role turn(s) in a thread; harvest      complete
  REPAIR_REQUIRED       instantiate one REPAIR per defect      instantiate
  VERIFIED repair       promote the parent it repairs          promote
  READY empty           frontier; alert IDLE every 30 min      frontier

A VERIFIED completion with an output sha always passes --unlock-dependents.
Quota / rate / auth outcomes park the role or server and leave the attempt
RUNNING for a later resume -- they never fail the task.  STOP is a file; the
driver aborts children, keeps the attempts RUNNING and adopts them on the
next start.  stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import laneproof  # noqa: E402
import vplint     # noqa: E402
import vprunners  # noqa: E402
import vpschema   # noqa: E402
from vpdriver import (BUILDER_PROMPT, JUNIOR_PROMPT, RESUME_PROMPT,  # noqa: E402
                      estimate_cost, findings_verdicts, sha256_text,
                      validate_findings_recomputed)
from vprunners import (PARK_STATUSES, STATUS_ABORTED, STATUS_DONE,  # noqa: E402
                       STATUS_INCOMPLETE, STATUS_PROGRESS_STOP, TurnSpec)

__all__ = ["LaneDriver", "Control", "ControlError", "PROBE_PROMPT", "REVIEW_PROMPT"]

PROBE_PROMPT = (
    "Read .vp/PACKET.md and .vp/BENCHMARK.md. Do the work the packet describes "
    "inside this worktree only; commit anything you write. Write .vp/RESULT.json "
    "per .vp/RESULT_SCHEMA.json with every check you ran and stop."
)
REVIEW_PROMPT = (
    "You are the reviewer named in .vp/REVIEW_REQUEST.json. Read it, then "
    ".vp/PACKET.md and .vp/BENCHMARK.md, then inspect `git diff <base>..<candidate>` "
    "and every file it touches. Verify each benchmark id yourself with file:line "
    "evidence. Output ONLY the JSON object per .vp/REVIEW_SCHEMA.json, copying item, "
    "subject, base, candidate and reviewer from REVIEW_REQUEST.json verbatim. "
    "Verdict APPROVE only when every benchmark id is PASS with evidence and no "
    "finding of severity medium or higher carries a reproduce command."
)

BUILD_KINDS = ("builder", "control", "security_build", "integration")
REVIEW_KINDS = ("junior", "security", "final_review", "adjudicator")
FINISHED_STATES = ("VERIFIED", "INTEGRATED", "REPAIR_REQUIRED", "BLOCKED",
                   "INVALID_EVIDENCE", "CANCELLED")
ADOPTABLE_STATES = ("CLAIMED", "RUNNING", "DISPATCHED", "RESULT_RECEIVED")

DEFAULT_ROUTE_BINDINGS = {
    "BIND_APPROVED_GO_MUSE_ROUTE": ["go2/muse-spark-1.3-contributor", "xhigh",
                                    "router_go2_muse_spark_1_3_contributor"],
    "BIND_APPROVED_GO_DEEPSEEK_ROUTE": ["go2/deepseek-v4.1-flash", "high",
                                        "router_go2_deepseek_v4_1_flash"],
}

MAX_RESUMES = 3
FAIL_CAP = 3
FAIL_BACKOFF_S = (60, 120, 300)
DEFAULT_INTERVAL = 5.0
IST = timezone(timedelta(hours=5, minutes=30))


def utc_ms():
    return vprunners.utc_ms()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _append_jsonl(path, rec, lock=None):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, sort_keys=True, default=str) + "\n"
        if lock is not None:
            with lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
        else:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Control -- orchestration_control.py over subprocess, every call logged
# --------------------------------------------------------------------------

class ControlError(Exception):
    pass


class Control(object):
    """The only path to run-state.json.  `call` runs one scheduler verb and
    appends a control.jsonl line: argv, rc, stdout, stderr, sequence before
    and after (read from the state file; -1 when unreadable)."""

    def __init__(self, exec_, python, script, state_path, catalog_path, log_path,
                 cwd=None, timeout_s=120):
        self.exec = exec_
        self.python = str(python)
        self.script = str(script)
        self.state_path = Path(state_path)
        self.catalog_path = Path(catalog_path)
        self.log_path = Path(log_path)
        self.cwd = str(cwd or Path(self.script).parent)
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._catalog = None
        self._catalog_sha = None
        self.calls = []

    def sequence(self):
        try:
            return int(json.loads(self.state_path.read_text(encoding="utf-8")).get("sequence", -1))
        except (OSError, ValueError, TypeError):
            return -1

    def call(self, verb, args=(), allow=(0,)):
        argv = [self.python, self.script, "--state", str(self.state_path),
                "--catalog", str(self.catalog_path), verb] + [str(a) for a in args]
        seq_before = self.sequence()
        t0 = time.monotonic()
        with self._lock:
            rc, out, err = self.exec.run(argv, cwd=self.cwd, timeout_s=self.timeout_s)
            seq_after = self.sequence()
        rec = {"ts": utc_ms(), "verb": verb, "argv": argv, "rc": rc,
               "stdout": (out or "")[:20000], "stderr": (err or "")[-4000:],
               "seq_before": seq_before, "seq_after": seq_after,
               "ms": int((time.monotonic() - t0) * 1000)}
        _append_jsonl(self.log_path, rec, self._lock)
        self.calls.append((verb, rc))
        data = None
        if (out or "").strip():
            try:
                data = json.loads(out)
            except ValueError:
                data = {"raw": out.strip()}
        if rc not in allow:
            msg = (err or out or "").strip().splitlines()
            raise ControlError("%s exited %d: %s" % (verb, rc, (msg[-1] if msg else "")[:400]))
        return rc, data

    # -- read-only views -------------------------------------------------------

    def state_view(self):
        """Read-only mirror of run-state.json.  Never written by the driver."""
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def catalog(self):
        try:
            sha = sha256_file(self.catalog_path)
        except OSError:
            return self._catalog or {}
        if sha != self._catalog_sha:
            self._catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            self._catalog_sha = sha
        return self._catalog

    def contract(self, task_id, row=None):
        fixed = {c["id"]: c for c in self.catalog().get("contracts", [])}
        if task_id in fixed:
            return fixed[task_id]
        row = row or {}
        params = row.get("parameters") or {}
        return {"id": task_id, "kind": row.get("kind"), "role": row.get("role") or {},
                "chief": row.get("chief"), "depends_on": row.get("depends_on", []),
                "owned_paths": params.get("owned_paths") or [],
                "title": row.get("template_id") or task_id,
                "build_steps": params.get("steps") or [],
                "verification": row.get("acceptance") or [],
                "acceptance": row.get("acceptance") or [],
                "parameters": params}

    # -- verbs --------------------------------------------------------------------

    def ready(self):
        _rc, data = self.call("ready")
        return data if isinstance(data, dict) else {"ready": [], "not_ready": []}

    def frontier(self):
        _rc, data = self.call("frontier")
        return data if isinstance(data, dict) else {}

    def reconcile(self):
        _rc, data = self.call("reconcile")
        return data if isinstance(data, dict) else {}

    def claim(self, task, chief, attempt_id, base_sha, paths):
        args = ["--task", task, "--chief", chief, "--attempt-id", attempt_id,
                "--base-sha", base_sha]
        for p in paths:
            args += ["--path", p]
        return self.call("claim", args)

    def start(self, task, attempt_id, child_id, model, effort, agent_type=None):
        args = ["--task", task, "--attempt-id", attempt_id, "--child-id", child_id,
                "--resolved-model", model, "--resolved-effort", effort]
        if agent_type:
            args += ["--agent-type", agent_type]
        return self.call("start", args)

    def complete(self, task, attempt_id, outcome, evidence=(), output_sha=None,
                 tree_sha=None, reason=None, verdict=None, unlock=False):
        args = ["--task", task, "--attempt-id", attempt_id, "--outcome", outcome]
        if unlock:
            args.append("--unlock-dependents")
        if output_sha:
            args += ["--output-sha", output_sha]
        if tree_sha:
            args += ["--tree-sha", tree_sha]
        for e in evidence:
            args += ["--evidence", str(e)]
        if reason:
            args += ["--reason", reason[:1000]]
        if verdict:
            args += ["--verdict", str(verdict)]
        return self.call("complete", args)

    def instantiate(self, template, task, parent, chief, params_path, depends_on=()):
        args = ["--template", template, "--task", task, "--parent-contract", parent,
                "--chief", chief, "--parameters-json", str(params_path)]
        for d in depends_on:
            args += ["--depends-on", d]
        return self.call("instantiate", args)

    def promote(self, task, output_sha, tree_sha, evidence, supporting=(), verdict=None):
        args = ["--task", task, "--output-sha", output_sha, "--tree-sha", tree_sha]
        for s in supporting:
            args += ["--supporting-task", s]
        for e in evidence:
            args += ["--evidence", str(e)]
        if verdict:
            args += ["--verdict", str(verdict)]
        return self.call("promote", args)

    def drain(self, reason):
        return self.call("drain", ["--reason", reason[:300]])

    def activate(self):
        return self.call("activate")


# --------------------------------------------------------------------------
# the driver
# --------------------------------------------------------------------------

class LaneDriver(object):

    def __init__(self, roster_path, exec_=None, runners=None, control=None,
                 interval=DEFAULT_INTERVAL, bins=None, clock=None, proof=None):
        self.roster_path = Path(roster_path).resolve()
        self.run_root = self.roster_path.parent
        self.roster = json.loads(self.roster_path.read_text(encoding="utf-8"))
        self._roster_mtime = self.roster_path.stat().st_mtime
        self.exec = exec_ or vprunners.Exec()
        self.interval = float(interval)
        self.bins = dict(bins or {})
        self.clock = clock or time.time
        self.here = Path(__file__).resolve().parent

        run = self.roster.get("run", {})
        self.trunk = Path(os.path.expanduser(run["trunk"]))
        self.cn = Path(os.path.expanduser(run.get("cn") or str(self.trunk.parent)))
        self.worktrees_root = Path(os.path.expanduser(run.get("worktrees")
                                                      or str(self.run_root / "vp-worktrees")))
        self.stop_file = self.run_root / (run.get("stop_file") or "STOP")
        self.drain_file = self.run_root / (run.get("drain_file") or "DRAIN")
        self.stop_grace_s = int(run.get("stop_grace_s", 120))
        self.pack_dir = Path(os.path.expanduser(run["pack_dir"])) if run.get("pack_dir") else None

        ctl = self.roster.get("control", {})
        self.control = control or Control(
            self.exec, ctl.get("python") or sys.executable, ctl["script"],
            ctl["state"], ctl["catalog"], self.run_root / "control.jsonl",
            cwd=ctl.get("cwd"))
        self.route_bindings = dict(DEFAULT_ROUTE_BINDINGS)
        self.route_bindings.update(ctl.get("route_bindings") or {})

        self.heartbeat_path = self.run_root / "driver.heartbeat"
        self.turns_root = self.run_root / "turns"
        self.costs_path = self.run_root / "costs.jsonl"
        self.log_path = self.run_root / "driver.log"
        self.alerts_md = self.run_root / "OWNER-ALERTS.md"
        self.alerts_jsonl = self.run_root / "alerts.jsonl"
        self.git_jsonl = self.run_root / "git.jsonl"
        self._sealed_day = None

        self._apply_roster(self.roster)
        self.git_bin = self.bins.get("git", "git")
        transport = run.get("opencode_transport", "http")
        self.runners = runners if runners is not None else {
            name: vprunners.runner_for(name, self.exec, self.bins, opencode_transport=transport)
            for name in ("opencode", "codex", "claude", "agy")
        }

        self.proof = proof or laneproof.Proof(
            self.run_root, self.here, self.cn, self.git, self.exec, self.log, self.alert,
            self.roster.get("proof", {}), python=ctl.get("python") or sys.executable)

        self._lock = threading.Lock()
        self._live = {}                 # task -> attempt_id
        self._threads = []
        self._fail = {}                 # task -> {count, next_try, key, stuck}
        self._alerted = set()
        self._stopping = False
        self._stop_started = None
        self._abort = threading.Event()
        self._reconciled = False
        self._budget_stop = False
        self._disk_paused = False
        self._last_frontier_mono = 0.0
        self._last_idle_alert_mono = None
        self._idle_since = None
        self._phase_logged = None
        self.tick_count = 0
        self.spawned_total = 0
        self.completed = []             # (task, attempt, outcome) for tests / handoff

    def _apply_roster(self, data):
        self.roster = data
        self.night = data.get("night", {})
        self.conc = data.get("concurrency", {})
        self.budget = data.get("budget", {})
        self.pricing = data.get("pricing", {})
        self.backoff = data.get("backoff", {})
        self.roles = data.get("roles", {})
        self.alerts_cfg = data.get("alerts", {})
        self.servers = getattr(self, "servers", {})
        for name, cfg in data.get("servers", {}).items():
            url = cfg.get("url") or ("http://127.0.0.1:%d" % int(cfg.get("port", 0)))
            srv = self.servers.get(name) or {"name": name, "active": 0, "parked_until": 0.0,
                                             "park_reason": None, "park_status": None}
            srv.update({"url": url, "max_concurrent": int(cfg.get("max_concurrent", 2)),
                        "xdg": os.path.expanduser(cfg.get("xdg_data_home") or cfg.get("data") or ""),
                        "parked": bool(cfg.get("parked", False))})
            self.servers[name] = srv
        rs = getattr(self, "runner_state", None) or {}
        for name, key, default in (("opencode", None, 99), ("codex", "codex_max", 2),
                                   ("claude", "claude_max", 2), ("agy", "agy_max", 2)):
            st = rs.get(name) or {"active": 0, "parked_until": 0.0, "park_reason": None,
                                  "park_status": None}
            st["max"] = int(self.conc.get(key, default)) if key else default
            rs[name] = st
        self.runner_state = rs
        self.max_tasks = int(self.conc.get("max_tasks_in_flight", 12))
        self.proof_cfg = data.get("proof", {})
        if getattr(self, "proof", None) is not None:
            self.proof.cfg = dict(self.proof_cfg)

    # -- logging / alerts -------------------------------------------------------------

    def log(self, msg):
        line = "%s %s" % (utc_ms(), msg)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def alert(self, kind, text, task=None):
        ts = utc_ms()
        self.log("ALERT %s %s" % (kind, text[:300]))
        line = "- %s **%s**%s — %s\n" % (ts, kind, (" `%s`" % task) if task else "", text[:800])
        try:
            with self._lock:
                with open(self.alerts_md, "a", encoding="utf-8") as fh:
                    fh.write(line)
        except OSError:
            pass
        _append_jsonl(self.alerts_jsonl, {"ts": ts, "kind": kind, "task": task, "text": text},
                      self._lock)
        self.comms("lanedriver", "owner", text, kind="alert:%s" % kind, task=task)
        if kind in ("STUCK", "QUOTA_WEEKLY", "QUOTA_ROLLING", "AUTH", "CONTROL_DOWN"):
            self._notify(kind, text)

    def alert_once(self, key, kind, text, task=None):
        with self._lock:
            if key in self._alerted:
                return
            self._alerted.add(key)
        self.alert(kind, text, task)

    def _notify(self, kind, text):
        osa = self.bins.get("osascript")
        if not osa:
            return
        try:
            self.exec.run([osa, "-e", 'display notification "%s" with title "lanedriver %s"'
                           % (text[:120].replace('"', "'"), kind)], timeout_s=10)
        except Exception:
            pass

    # -- clock / guards ---------------------------------------------------------------

    def ist_now(self):
        return datetime.fromtimestamp(self.clock(), tz=IST)

    def disk_gb(self):
        try:
            return shutil.disk_usage(str(self.cn)).free / (1024 ** 3)
        except OSError:
            return None

    def spent(self):
        usd, tokens = 0.0, {}
        try:
            with open(self.costs_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    usd += float(rec.get("cost") or 0.0)
                    r = rec.get("runner") or "?"
                    tokens[r] = tokens.get(r, 0) + int(rec.get("tokens_in") or 0) + \
                        int(rec.get("tokens_out") or 0)
        except OSError:
            pass
        return round(usd, 4), tokens

    def _guards(self):
        gb = self.disk_gb()
        floor = float(self.night.get("pause_on_disk_gb", 25))
        if gb is not None and gb < floor:
            if not self._disk_paused:
                self._disk_paused = True
                self.alert("DISK", "free disk %.1f GB < %.0f GB: no new worktrees or claims"
                           % (gb, floor))
        elif self._disk_paused:
            self._disk_paused = False
            self.log("disk recovered: %.1f GB" % gb)
        usd, tokens = self.spent()
        cap = self.budget.get("max_cost_usd_per_run")
        if cap is not None and usd >= float(cap) and not self._budget_stop:
            self._budget_stop = True
            self.alert("BUDGET", "run budget reached (%.2f USD, tokens %s): no new turns"
                       % (usd, json.dumps(tokens)))
        # F3: the weekly/monthly caps are token caps per runner -- the runner is
        # parked (its roles wait), every other runner keeps going
        for runner, tcap in (self.budget.get("max_tokens_per_runner") or {}).items():
            st = self.runner_state.get(runner)
            if st is None:
                continue
            used = tokens.get(runner, 0)
            if used >= int(tcap) and st.get("park_status") != "TOKEN_CAP":
                st["parked_until"] = float("inf")
                st["park_reason"] = "%d tokens >= cap %d" % (used, int(tcap))
                st["park_status"] = "TOKEN_CAP"
                self.alert("TOKEN_CAP", "%s used %d tokens >= cap %d: parked until the cap is "
                           "raised in the roster (other runners continue)" % (runner, used, int(tcap)))
            elif used < int(tcap) and st.get("park_status") == "TOKEN_CAP":
                st["parked_until"], st["park_reason"], st["park_status"] = 0.0, None, None
                self.log("UNPARK runner %s: token cap raised" % runner)
        return not self._budget_stop and not self._disk_paused

    def _reload_roster_if_changed(self):
        try:
            m = self.roster_path.stat().st_mtime
        except OSError:
            return
        if m == self._roster_mtime:
            return
        try:
            data = json.loads(self.roster_path.read_text(encoding="utf-8"))
        except ValueError:
            return
        self._roster_mtime = m
        with self._lock:
            self._apply_roster(data)
        self.log("roster reloaded (mtime changed)")

    # -- parking / concurrency -----------------------------------------------------------

    def _parked(self, st):
        return st.get("parked") or st["parked_until"] > time.monotonic()

    def _park(self, st, minutes, reason, status, what):
        st["parked_until"] = time.monotonic() + minutes * 60
        st["park_reason"] = reason
        st["park_status"] = status
        self.log("PARK %s %s for %dm: %s" % (what, status, minutes, reason[:200]))
        self.alert(status, "%s parked %d min: %s" % (what, minutes, reason[:200]))

    def park_after(self, outcome, server, runner):
        st = outcome.status
        rm = (outcome.usage or {}).get("reset_minutes")
        mins = {"QUOTA_WEEKLY": int(self.backoff.get("weekly_park_min", 6 * 60)),
                "QUOTA_ROLLING": (rm or 60) + int(self.backoff.get("quota_rolling_extra_min", 2)),
                "RATE": int(self.backoff.get("rate_min", 20)),
                "DEGRADED": int(self.backoff.get("degraded_min", 10)),
                "AUTH": int(self.backoff.get("auth_park_min", 120))}.get(st)
        if mins is None:
            return
        if runner == "opencode" and server:
            self._park(self.servers[server], mins, outcome.detail, st, server)
        else:
            self._park(self.runner_state[runner], mins, outcome.detail, st, runner)

    def _maybe_unpark(self):
        for name, srv in self.servers.items():
            if srv["park_reason"] and not self._parked(srv):
                if self._http_ok(srv["url"] + "/session"):
                    self.log("UNPARK %s" % name)
                    srv["park_reason"] = srv["park_status"] = None
                else:
                    srv["parked_until"] = time.monotonic() + 300
        for name, st in self.runner_state.items():
            if st["park_reason"] and not self._parked(st):
                self.log("UNPARK runner %s" % name)
                st["park_reason"] = st["park_status"] = None

    def _http_ok(self, url, timeout=5.0):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.status < 500
        except Exception:
            return False

    def _try_acquire(self, server, runner):
        with self._lock:
            if len(self._live) >= self.max_tasks:
                return False
            rs = self.runner_state[runner]
            if rs["active"] >= rs["max"] or self._parked(rs):
                return False
            if server:
                srv = self.servers[server]
                if srv["active"] >= srv["max_concurrent"] or self._parked(srv):
                    return False
                srv["active"] += 1
            rs["active"] += 1
            return True

    def _release(self, server, runner):
        with self._lock:
            self.runner_state[runner]["active"] = max(0, self.runner_state[runner]["active"] - 1)
            if server:
                self.servers[server]["active"] = max(0, self.servers[server]["active"] - 1)

    def _pick_server(self, rcfg):
        want = rcfg.get("server")
        names = [want] if want else sorted(self.servers)
        for name in names + [n for n in sorted(self.servers) if n not in names]:
            srv = self.servers.get(name)
            if srv and not self._parked(srv) and srv["active"] < srv["max_concurrent"]:
                return name
        return None

    # -- backoff --------------------------------------------------------------------

    def _backed_off(self, task, key):
        with self._lock:
            ent = self._fail.get(task)
            if ent is None:
                return False
            if ent["key"] != key:
                self._fail.pop(task, None)
                return False
            if ent["stuck"]:
                return True
            return time.monotonic() < ent["next_try"]

    def note_failure(self, task, key, detail, kind="STALLED"):
        with self._lock:
            ent = self._fail.get(task)
            if ent is None or ent["key"] != key:
                ent = {"count": 0, "next_try": 0.0, "key": key, "stuck": False}
                self._fail[task] = ent
            ent["count"] += 1
            count = ent["count"]
            ent["next_try"] = time.monotonic() + FAIL_BACKOFF_S[min(count - 1, len(FAIL_BACKOFF_S) - 1)]
            if count >= FAIL_CAP:
                ent["stuck"] = True
        self.log("FAIL %s %d/%d %s" % (task, count, FAIL_CAP, detail[:200]))
        self.alert("STUCK" if count >= FAIL_CAP else kind,
                   "%s failure %d/%d: %s" % (task, count, FAIL_CAP, detail[:300]), task)
        return count

    def clear_failures(self, task):
        with self._lock:
            self._fail.pop(task, None)

    # -- git / worktree ----------------------------------------------------------------

    def git(self, args, cwd=None, timeout_s=300, log=False):
        rc, out, err = self.exec.run([self.git_bin] + list(args), cwd=cwd, timeout_s=timeout_s)
        if log:
            _append_jsonl(self.git_jsonl, {"ts": utc_ms(), "argv": [self.git_bin] + list(args),
                                           "cwd": cwd, "rc": rc, "stdout": (out or "")[:2000],
                                           "stderr": (err or "")[:2000]}, self._lock)
        return rc, out, err

    def head_sha(self, path):
        rc, out, _ = self.git(["-C", str(path), "rev-parse", "HEAD"])
        return out.strip() if rc == 0 and out.strip() else None

    def tree_sha(self, path):
        rc, out, _ = self.git(["-C", str(path), "rev-parse", "HEAD^{tree}"])
        return out.strip() if rc == 0 and out.strip() else None

    def trunk_sha(self):
        return self.head_sha(self.trunk)

    def worktree_path(self, task):
        return self.worktrees_root / task

    def ensure_worktree(self, task, base):
        wt = self.worktree_path(task)
        if wt.exists() and not (wt / ".git").exists():
            raise ControlError("worktree path %s exists but is not a worktree" % wt)
        if not wt.exists():
            rc, _o, _e = self.git(["-C", str(self.trunk), "cat-file", "-e", base + "^{commit}"])
            if rc != 0:
                raise ControlError("%s base %s is not a commit in trunk" % (task, base[:12]))
            wt.parent.mkdir(parents=True, exist_ok=True)
            rc, out, err = self.git(["-C", str(self.trunk), "worktree", "add", str(wt),
                                     "-b", "vp/%s" % task, base], log=True)
            if rc != 0 and not wt.exists():
                rc, out, err = self.git(["-C", str(self.trunk), "worktree", "add", str(wt),
                                         "vp/%s" % task], log=True)
                if rc != 0:
                    raise ControlError("worktree add failed for %s: %s"
                                       % (task, (err or out).strip()[:300]))
        for rel in ("agent/.venv", "portal/node_modules", "platform/.venv"):
            link, target = wt / rel, self.trunk / rel
            try:
                link.parent.mkdir(parents=True, exist_ok=True)
                if not link.exists() and not link.is_symlink() and target.exists():
                    os.symlink(str(target), str(link))
            except OSError:
                pass
        return wt

    def _pack_paths(self, task):
        if not self.pack_dir:
            return None, None
        d = self.pack_dir / task
        p, b = d / "PACKET.md", d / "BENCHMARK.md"
        return (p if p.exists() and p.stat().st_size else None,
                b if b.exists() and b.stat().st_size else None)

    @staticmethod
    def render_packet(contract, base, row=None):
        owned = contract.get("owned_paths") or []
        lines = ["---", "item: %s" % contract["id"], "title: %s" % contract.get("title", contract["id"]),
                 "kind: %s" % contract.get("kind"), "base_sha: %s" % base,
                 "depends_on: [%s]" % ", ".join(contract.get("depends_on") or []),
                 "owned_files:"] + ["  - %s" % p for p in owned] + \
                ["max_rounds: %d" % int((row or {}).get("max_rounds") or 3), "---",
                 "## Goal", contract.get("title", ""), "", "## Steps"]
        for i, s in enumerate(contract.get("build_steps") or contract.get("steps") or [], 1):
            lines.append("%d. %s" % (i, s))
        params = (contract.get("parameters") or (row or {}).get("parameters") or {})
        if params:
            lines += ["", "## Parameters", "```json", json.dumps(params, indent=2, sort_keys=True), "```"]
        lines += ["", "## Prohibitions", "Change only owned_files. No migrations unless the packet "
                  "names the number. No pushes."]
        return "\n".join(lines) + "\n"

    @staticmethod
    def render_benchmark(contract):
        rows = []
        crit = list(dict.fromkeys((contract.get("verification") or []) +
                                  (contract.get("acceptance") or [])))
        for i, c in enumerate(crit, 1):
            rows.append("- B%d [evidence] %s — check: RESULT.json checks[] and the files it names"
                        % (i, c.replace("\n", " ")))
        if not rows:
            rows.append("- B1 [evidence] RESULT.json records every check run with its command "
                        "and exit code — check: RESULT.json")
        return "\n".join(rows) + "\n"

    def write_vp_files(self, wt, task, contract, base, row=None):
        vp = wt / ".vp"
        vp.mkdir(parents=True, exist_ok=True)
        pp, bp = self._pack_paths(task)
        (vp / "PACKET.md").write_text(pp.read_text(encoding="utf-8") if pp
                                      else self.render_packet(contract, base, row), encoding="utf-8")
        (vp / "BENCHMARK.md").write_text(bp.read_text(encoding="utf-8") if bp
                                         else self.render_benchmark(contract), encoding="utf-8")
        for name, doc in (("RESULT_SCHEMA.json", vpschema.RESULT_SCHEMA_DOC),
                          ("FINDINGS_SCHEMA.json", vpschema.FINDINGS_SCHEMA_DOC),
                          ("REVIEW_SCHEMA.json", vpschema.REVIEW_SCHEMA_DOC)):
            onfile = self.here / "schemas" / name
            (vp / name).write_text(onfile.read_text(encoding="utf-8") if onfile.exists()
                                   else json.dumps(doc, indent=2), encoding="utf-8")
        (vp / "BASE").write_text(base + "\n", encoding="utf-8")
        (vp / "CONTRACT.json").write_text(json.dumps(contract, indent=2, sort_keys=True),
                                          encoding="utf-8")
        try:
            rc, out, _ = self.git(["-C", str(wt), "rev-parse", "--git-common-dir"])
            common = Path(out.strip()) if rc == 0 and out.strip() else None
            if common is not None and not common.is_absolute():
                common = wt / common
            if common is not None:
                exclude = common / "info" / "exclude"
                exclude.parent.mkdir(parents=True, exist_ok=True)
                cur = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
                if ".vp/" not in cur.splitlines():
                    exclude.write_text(cur.rstrip("\n") + "\n.vp/\n", encoding="utf-8")
        except OSError:
            pass

    # -- heartbeat / STOP ----------------------------------------------------------------

    def stop_requested(self):
        return self.stop_file.exists()

    def drain_state(self):
        """RUN_ROOT/DRAIN is a state row with an expiry (F1): {since, deadline_min,
        reason, expires}.  An empty/legacy file drains without a deadline."""
        try:
            text = self.drain_file.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def drain_requested(self):
        return self.drain_state() is not None

    def write_drain(self, reason, deadline_min=None):
        now = self.clock()
        row = {"since": utc_ms(), "since_epoch": now, "reason": reason,
               "deadline_min": deadline_min,
               "expires_epoch": (now + float(deadline_min) * 60) if deadline_min else None,
               "pid": os.getpid()}
        self.drain_file.write_text(json.dumps(row, indent=2, sort_keys=True), encoding="utf-8")
        self.log("DRAIN written: %s deadline=%s" % (reason, deadline_min))
        return row

    def clear_drain(self, why):
        try:
            self.drain_file.unlink()
        except OSError:
            return False
        self.log("DRAIN cleared: %s" % why)
        return True

    def _drain_deadline_step(self):
        """F1: a drain whose paired restart never came is released by the
        driver itself when its deadline passes -- dispatch resumes, one alert."""
        row = self.drain_state()
        if row is None:
            return False
        exp = row.get("expires_epoch")
        if not exp or self.clock() < float(exp) or self._stopping:
            return False
        self.clear_drain("deadline %s min passed without a restart" % row.get("deadline_min"))
        self.alert("DRAIN_EXPIRED", "drain since %s (%s) hit its %s-minute deadline with no "
                   "restart; dispatch restored by the driver"
                   % (row.get("since"), row.get("reason"), row.get("deadline_min")))
        return True

    def write_heartbeat(self, extra=None):
        with self._lock:
            live = dict(self._live)
            servers = {n: {"active": s["active"], "parked": self._parked(s),
                           "park_status": s["park_status"]} for n, s in self.servers.items()}
            runners = {n: {"active": s["active"], "max": s["max"], "parked": self._parked(s),
                           "park_status": s.get("park_status")} for n, s in self.runner_state.items()}
        usd, tokens = self.spent()
        payload = {"ts": utc_ms(), "mono": time.monotonic(), "pid": os.getpid(),
                   "tick": self.tick_count, "stopping": bool(self._stopping),
                   "draining": self.drain_requested(), "run_root": str(self.run_root),
                   "live": live, "active": len(live), "servers": servers, "runners": runners,
                   "budget": {"spent_usd": usd, "tokens": tokens,
                              "max_usd": self.budget.get("max_cost_usd_per_run"),
                              "stopped": self._budget_stop},
                   "disk_gb": round(self.disk_gb() or 0, 1), "disk_paused": self._disk_paused,
                   "sequence": self.control.sequence(), "ist": self.ist_now().strftime("%H:%M"),
                   "idle_since": self._idle_since, "spawned_total": self.spawned_total}
        if extra:
            payload.update(extra)
        try:
            tmp = self.heartbeat_path.with_suffix(".heartbeat.tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(str(tmp), str(self.heartbeat_path))
        except OSError:
            pass
        return payload

    # -- comms + seal (plan §2.1 point 5) ---------------------------------------------------

    def comms(self, sender, to, text, kind="message", task=None, ref=None):
        """Append one line to comms.jsonl.  This is the hook the Claude sessions
        (Chief / Architect / Fixer) call so every cross-session message about the
        run lives in the audit tree next to control.jsonl; the driver itself logs
        its own outbound alerts here too."""
        row = {"ts": utc_ms(), "from": sender, "to": to, "kind": kind, "text": text}
        if task:
            row["task"] = task
        if ref:
            row["ref"] = ref
        _append_jsonl(self.run_root / "comms.jsonl", row)
        return row

    SEAL_SKIP = ("driver.heartbeat", "driver.heartbeat.tmp", "STOP", "DRAIN")

    def seal(self, day=None, force=False):
        """Daily seal: MANIFEST-<day>.json listing sha256 + size of every file
        under RUN_ROOT (except the live heartbeat and control flags, and earlier
        manifests) so the tree can be verified after the fact.  Idempotent per
        IST day unless `force`."""
        day = day or self.ist_now().strftime("%Y-%m-%d")
        out = self.run_root / ("MANIFEST-%s.json" % day)
        if out.exists() and not force:
            return None
        files, total = {}, 0
        for p in sorted(self.run_root.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(self.run_root).as_posix()
            if rel in self.SEAL_SKIP or (rel.startswith("MANIFEST-") and rel.endswith(".json")):
                continue
            if rel.startswith("worktrees/") or rel.startswith("trunk/"):
                continue
            try:
                h = hashlib.sha256()
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                size = p.stat().st_size
            except OSError:
                continue
            files[rel] = {"sha256": h.hexdigest(), "bytes": size}
            total += size
        body = json.dumps({"day": day, "ts": utc_ms(), "run_root": str(self.run_root),
                           "sequence": self.control.sequence(), "files": len(files),
                           "bytes": total, "entries": files}, indent=2, sort_keys=True)
        manifest_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        try:
            tmp = out.with_suffix(".json.tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(str(tmp), str(out))
        except OSError:
            return None
        _append_jsonl(self.run_root / "seals.jsonl",
                      {"ts": utc_ms(), "day": day, "manifest": out.name, "sha256": manifest_sha,
                       "files": len(files), "bytes": total})
        self.log("SEAL %s files=%d sha256=%s" % (out.name, len(files), manifest_sha[:12]))
        return out

    def _seal_step(self):
        """seal the previous IST day once its clock has rolled over, and the current
        day at most once per driver start (so a restart leaves a fresh manifest)"""
        today = self.ist_now().strftime("%Y-%m-%d")
        if self._sealed_day == today:
            return
        try:
            if self._sealed_day is not None:
                # day rolled over under a live driver: the closing seal for the
                # day just ended replaces the provisional one written at start
                self.seal(self._sealed_day, force=True)
            self.seal(today)
        except Exception as exc:  # noqa: BLE001 -- seal must never take the tick down
            self.log("seal failed: %s" % exc)
        self._sealed_day = today

    def _stop_step(self):
        elapsed = time.monotonic() - (self._stop_started or time.monotonic())
        with self._lock:
            live = len(self._live)
        if live and elapsed < self.stop_grace_s:
            return
        if live and not self._abort.is_set():
            self.log("STOP grace %ds over with %d live: aborting children" % (self.stop_grace_s, live))
            self._abort.set()

    def _finish(self, reason):
        self.log("FINISH %s" % reason)
        with self._lock:
            live = dict(self._live)
        handoff = {"ts": utc_ms(), "reason": reason, "tick": self.tick_count,
                   "live_at_stop": live, "sequence": self.control.sequence(),
                   "completed": self.completed[-50:]}
        try:
            (self.run_root / "STOP-HANDOFF.json").write_text(
                json.dumps(handoff, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            pass
        self.write_heartbeat({"finished": reason})
        self.comms("lanedriver", "audit", "finish %s tick=%d" % (reason, self.tick_count), kind="lifecycle")

    # -- the tick ---------------------------------------------------------------------------

    def tick(self):
        self.tick_count += 1
        self._reload_roster_if_changed()
        if self.stop_requested() and not self._stopping:
            self._stopping = True
            self._stop_started = time.monotonic()
            self.log("STOP file seen; grace %ds" % self.stop_grace_s)
        if not self._reconciled:
            try:
                res = self.control.reconcile()
            except ControlError as exc:
                self.log("reconcile failed: %s" % exc)
                self.alert_once("reconcile", "RECONCILE_FAILED",
                                "scheduler reconcile failed (%s); retrying every tick" % str(exc)[:200])
            else:
                self._reconciled = True
                self.log("RECONCILE %s" % json.dumps(res)[:300])
        self.write_heartbeat()
        self._seal_step()
        if self._stopping:
            self._stop_step()
            return 0
        may_start = self._guards()
        self._maybe_unpark()
        self._drain_deadline_step()
        state = self.control.state_view()
        phase = state.get("phase")
        if phase != self._phase_logged:
            self.log("PHASE %s" % phase)
            self._phase_logged = phase
        draining = phase != "ACTIVE" or self.drain_requested()
        spawned = 0
        spawned += self._adopt_orphans(state, draining)
        if not draining and may_start:
            spawned += self._dispatch_ready(state)
        with self._lock:
            live = len(self._live)
        if not live and not draining and may_start:
            self._frontier_tick(state)
        elif live:
            self._idle_since = None
            self._last_idle_alert_mono = None
        self.spawned_total += spawned
        if spawned:
            self.write_heartbeat()
        return spawned

    # -- dispatch -------------------------------------------------------------------------

    def _route_for(self, contract):
        role = contract.get("role") or {}
        model = role.get("model")
        if model in self.route_bindings:
            m, e, a = self.route_bindings[model]
            return m, e, a
        return model, role.get("effort"), None

    def _claim_paths(self, contract, row):
        paths = list(contract.get("owned_paths") or [])
        params = row.get("parameters") or {}
        paths += [p for p in (params.get("owned_paths") or []) if p not in paths]
        paths = [p.strip().rstrip("/") for p in paths if p and not p.startswith("/")
                 and not any(ch in p for ch in "*?[]")]
        return paths or ["control/evidence/%s" % row["task_id"]]

    @staticmethod
    def _conflicts(paths, state):
        active = [c for c in (state.get("claims") or {}).values() if c.get("state") == "ACTIVE"]
        for c in active:
            for left in paths:
                for right in c.get("paths") or []:
                    if left == right or left.startswith(right + "/") or right.startswith(left + "/"):
                        return c.get("task_id")
        return None

    def _base_for(self, row, state):
        if row.get("review_key") or (row.get("kind") in REVIEW_KINDS):
            return (state.get("candidate") or {}).get("sha")
        return self.trunk_sha()

    def _dispatch_ready(self, state):
        try:
            rows = self.control.ready().get("ready") or []
        except ControlError as exc:
            self.alert_once("control:ready", "CONTROL_DOWN",
                            "ready failed (%s); the driver idles until the scheduler answers"
                            % str(exc)[:200])
            return 0
        with self._lock:
            self._alerted.discard("control:ready")
        n = 0
        for row in sorted(rows, key=lambda r: (0 if r.get("dynamic") else 1, r.get("updated_at") or "")):
            if self._stopping:
                break
            task = row["task_id"]
            with self._lock:
                if task in self._live:
                    continue
            if self._backed_off(task, "ready:%s" % row.get("updated_at")):
                continue
            contract = self.control.contract(task, row)
            kind = contract.get("kind") or row.get("kind")
            rcfg = self.roles.get(kind)
            if not rcfg:
                self.alert_once("role:%s" % kind, "ROSTER",
                                "no roster role for kind %r (task %s); not dispatched" % (kind, task))
                continue
            runner = rcfg.get("runner", "opencode")
            server = self._pick_server(rcfg) if runner == "opencode" else None
            if runner == "opencode" and server is None:
                continue
            paths = self._claim_paths(contract, row)
            other = self._conflicts(paths, state)
            if other:
                self.log("WAIT %s: paths conflict with active claim of %s" % (task, other))
                continue
            base = self._base_for(row, state)
            if not base:
                self.alert_once("base:%s" % task, "NO_BASE",
                                "%s needs a registered candidate before it can be claimed" % task)
                continue
            if not self._try_acquire(server, runner):
                continue
            attempt = "%s-a%s" % (task, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3])
            try:
                self.control.claim(task, row["chief"], attempt, base, paths)
                self.log("CLAIM %s %s base %s paths %s" % (task, attempt, base[:12], paths))
                if runner != "codex":
                    self._start(task, attempt, contract, "lanedriver:%d:%s" % (os.getpid(), attempt))
            except ControlError as exc:
                self._release(server, runner)
                self.note_failure(task, "ready:%s" % row.get("updated_at"), str(exc))
                continue
            # codex kinds: the thread pre-opens the codex session and calls
            # `start` with the real thread id (review_gate linkage)
            self._spawn(task, attempt, row, contract, server, runner, base,
                        needs_start=(runner == "codex"))
            n += 1
        return n

    def _start(self, task, attempt, contract, child):
        model, effort, agent_type = self._route_for(contract)
        self.control.start(task, attempt, child, model, effort, agent_type)
        self.log("START %s %s child=%s %s/%s" % (task, attempt, child, model, effort))

    def _adopt_orphans(self, state, draining):
        n = 0
        for task, row in (state.get("tasks") or {}).items():
            if row.get("state") not in ADOPTABLE_STATES:
                continue
            with self._lock:
                if task in self._live:
                    continue
            key = row.get("attempt_id")
            if self._backed_off(task, key):
                continue
            contract = self.control.contract(task, row)
            kind = contract.get("kind") or row.get("kind")
            rcfg = self.roles.get(kind)
            if not rcfg:
                continue
            runner = rcfg.get("runner", "opencode")
            server = self._pick_server(rcfg) if runner == "opencode" else None
            if runner == "opencode" and server is None:
                continue
            attempt = row.get("attempt_id")
            if not attempt:
                continue
            needs_start = False
            if row["state"] == "CLAIMED":
                if draining:
                    continue
                if runner == "codex":
                    needs_start = True
                else:
                    try:
                        self._start(task, attempt, contract, "lanedriver:%d:%s" % (os.getpid(), attempt))
                    except ControlError as exc:
                        self.note_failure(task, key, str(exc))
                        continue
            if not self._try_acquire(server, runner):
                continue
            self.log("ADOPT %s %s (%s)" % (task, attempt, row["state"]))
            self._spawn(task, attempt, row, contract, server, runner,
                        row.get("base_sha") or self.trunk_sha() or "", needs_start=needs_start)
            n += 1
        return n

    def _spawn(self, task, attempt, row, contract, server, runner, base, needs_start=False):
        with self._lock:
            self._live[task] = attempt
        th = threading.Thread(target=self._task_thread,
                              args=(task, attempt, dict(row), contract, server, runner, base,
                                    needs_start),
                              name="vp-%s" % task, daemon=True)
        self._threads.append(th)
        th.start()

    # -- one task attempt (thread) ------------------------------------------------------------

    def _task_thread(self, task, attempt, row, contract, server, runner, base, needs_start=False):
        try:
            self._run_attempt(task, attempt, row, contract, server, runner, base, needs_start)
        except Exception as exc:          # never kill the daemon
            self.log("EXC %s: %s: %s" % (task, type(exc).__name__, exc))
            try:
                self.note_failure(task, attempt, "%s: %s" % (type(exc).__name__, exc))
            except Exception:
                pass
        finally:
            self._release(server, runner)
            with self._lock:
                self._live.pop(task, None)

    def _turn_dir(self, task, attempt):
        d = self.turns_root / task / attempt
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _run_attempt(self, task, attempt, row, contract, server, runner, base, needs_start=False):
        kind = contract.get("kind") or row.get("kind")
        rcfg = self.roles.get(kind) or {}
        wt = self.ensure_worktree(task, base)
        self.write_vp_files(wt, task, contract, base, row)
        tdir = self._turn_dir(task, attempt)
        fkey = attempt
        sid = self._saved_session(tdir)
        if needs_start:
            if not sid:
                pre = self._spec("reviewer", task, wt, "", rcfg, runner, server, None,
                                 wt / ".vp" / "REVIEW.json", None, 180.0, tdir, "0-preopen", False, 0)
                sid, detail = self.runners[runner].preopen(pre)
                if not sid:
                    self.note_failure(task, fkey, "codex preopen: %s" % detail, kind="STALLED")
                    return
                self._save_session(tdir, sid, runner)
                self.log("PREOPEN %s %s codex thread %s" % (task, attempt, sid))
            self._start(task, attempt, contract, sid)
            row = dict(row, child_id=sid)
        if kind in BUILD_KINDS:
            outcome, result = self._build_pipeline(task, attempt, row, contract, server, runner,
                                                   rcfg, wt, tdir, sid)
        elif kind in REVIEW_KINDS:
            outcome, result = self._review_pipeline(task, attempt, row, contract, server, runner,
                                                    rcfg, wt, tdir, sid, base)
        else:
            outcome, result = self._single_pipeline(task, attempt, row, contract, server, runner,
                                                    rcfg, wt, tdir, sid, "probe")
        if outcome is None:
            return
        if outcome.status == STATUS_ABORTED:
            self.log("ABORTED %s %s (attempt stays RUNNING for adoption)" % (task, attempt))
            return
        if outcome.status in PARK_STATUSES:
            # the role is parked, the attempt stays RUNNING and is adopted
            # after the park lifts; a quota never fails the task
            self.park_after(outcome, server, runner)
            self.log("PARKED %s %s: %s (attempt stays RUNNING)" % (task, attempt, outcome.status))
            return
        if result is None:
            count = self.note_failure(task, fkey, "%s: %s" % (outcome.status, outcome.detail),
                                      kind=outcome.status)
            if count >= FAIL_CAP:
                self._complete(task, attempt, "INVALID_EVIDENCE", tdir,
                               evidence=[tdir / "record.json"],
                               reason="%d consecutive runner failures: %s: %s"
                               % (count, outcome.status, outcome.detail[:300]))
            return
        self.clear_failures(task)
        self._complete(task, attempt, result["outcome"], tdir, evidence=result.get("evidence") or [],
                       output_sha=result.get("output_sha"), tree_sha=result.get("tree_sha"),
                       reason=result.get("reason"), verdict=result.get("verdict"),
                       fails=result.get("fails"))

    def _saved_session(self, tdir):
        try:
            return json.loads((tdir / "session.json").read_text(encoding="utf-8")).get("session_id")
        except (OSError, ValueError):
            return None

    def _save_session(self, tdir, sid, runner):
        if sid:
            try:
                (tdir / "session.json").write_text(json.dumps({"session_id": sid, "runner": runner,
                                                              "ts": utc_ms()}), encoding="utf-8")
            except OSError:
                pass

    def _complete(self, task, attempt, outcome, tdir, evidence=(), output_sha=None, tree_sha=None,
                  reason=None, verdict=None, fails=None):
        ev = [str(p) for p in evidence if p and Path(p).exists()]
        unlock = outcome == "VERIFIED" and bool(output_sha) and bool(ev)
        harvest = {"ts": utc_ms(), "task": task, "attempt": attempt, "outcome": outcome,
                   "output_sha": output_sha, "tree_sha": tree_sha, "evidence": ev,
                   "reason": reason, "fails": fails or [], "unlock_dependents": unlock}
        try:
            (tdir / "harvest.json").write_text(json.dumps(harvest, indent=2, sort_keys=True),
                                               encoding="utf-8")
        except OSError:
            pass
        try:
            _rc, data = self.control.complete(task, attempt, outcome, ev, output_sha, tree_sha,
                                              reason, verdict, unlock)
        except ControlError as exc:
            if verdict:
                self.alert("VERDICT_REFUSED", "%s: scheduler refused the review verdict (%s); "
                           "completing INVALID_EVIDENCE" % (task, str(exc)[:200]), task)
                try:
                    _rc, data = self.control.complete(task, attempt, "INVALID_EVIDENCE", ev,
                                                      reason="verdict refused: %s" % str(exc)[:300])
                    outcome = "INVALID_EVIDENCE"
                except ControlError as exc2:
                    self.alert("COMPLETE_REFUSED", "%s %s: %s" % (task, attempt, str(exc2)[:300]), task)
                    return False
            else:
                self.alert("COMPLETE_REFUSED", "%s %s: %s" % (task, attempt, str(exc)[:300]), task)
                return False
        newly = (data or {}).get("newly_ready") or []
        self.log("COMPLETE %s %s -> %s unlock=%s newly_ready=%s" % (task, attempt, outcome, unlock, newly))
        self.completed.append((task, attempt, outcome))
        return True

    # -- pipelines ----------------------------------------------------------------------

    def _single_pipeline(self, task, attempt, row, contract, server, runner, rcfg, wt, tdir, sid, role):
        out_path = wt / ".vp" / "RESULT.json"
        outcome = self._turn(task, attempt, row, server, runner, rcfg, wt, tdir, role, PROBE_PROMPT,
                             out_path, vpschema.validate_result, sid, 1)
        if outcome.status != STATUS_DONE:
            return outcome, None
        head = self.head_sha(wt)
        return outcome, {"outcome": "VERIFIED", "output_sha": head, "tree_sha": self.tree_sha(wt),
                         "evidence": [out_path, tdir / "record.json"]}

    def _build_pipeline(self, task, attempt, row, contract, server, runner, rcfg, wt, tdir, sid):
        gcfg = self.roles.get("grader") or {}
        grunner = gcfg.get("runner", "opencode")
        max_rounds = int(self.conc.get("max_rounds", 3))
        out_path = wt / ".vp" / "RESULT.json"
        fpath = wt / ".vp" / "FINDINGS.json"
        outcome, fails = None, []
        kind = contract.get("kind") or row.get("kind")
        needs_proof = kind in (self.proof_cfg.get("require_for_kinds") or ["integration"])
        pending = self._proof_pending(tdir)
        if needs_proof and pending and self.head_sha(wt) == pending.get("sha"):
            # an earlier attempt built and graded this exact head; only the proof is owed
            self.log("PROOF %s resumes on %s (build+grade skipped)" % (task, pending["sha"][:12]))
            return self._proof_step(task, attempt, row, contract, wt, tdir, pending["sha"],
                                    pending.get("base") or self._base_of(wt))
        for rnd in range(1, max_rounds + 1):
            outcome = self._turn(task, attempt, row, server, runner, rcfg, wt, tdir, "builder",
                                 BUILDER_PROMPT, out_path, vpschema.validate_result, sid, rnd)
            if outcome.status != STATUS_DONE:
                return outcome, None
            self._autofix(wt, task, tdir)
            gserver = self._pick_server(gcfg) if grunner == "opencode" else None
            if grunner == "opencode" and gserver is None:
                gserver = server
            try:
                fpath.unlink()
            except OSError:
                pass
            gout = self._turn(task, attempt, row, gserver, grunner, gcfg, wt, tdir, "grader",
                              JUNIOR_PROMPT, fpath, validate_findings_recomputed, None, rnd,
                              expect_fence=True)
            if gout.status != STATUS_DONE:
                return gout, None
            _doc, fails, unknown = findings_verdicts(fpath)
            if not fails and not unknown:
                head = self.head_sha(wt)
                if needs_proof:
                    return self._proof_step(task, attempt, row, contract, wt, tdir, head,
                                            self._base_of(wt), build_outcome=outcome)
                return outcome, {"outcome": "VERIFIED", "output_sha": head, "tree_sha": self.tree_sha(wt),
                                 "evidence": [out_path, fpath, tdir / "record.json"]}
            self.log("ROUND %s %d/%d fails=%s unknown=%s" % (task, rnd, max_rounds, fails, unknown))
        head = self.head_sha(wt)
        return outcome, {"outcome": "REPAIR_REQUIRED", "output_sha": head, "tree_sha": self.tree_sha(wt),
                         "evidence": [out_path, fpath, tdir / "record.json"],
                         "reason": "%d rounds; FAIL %s" % (max_rounds, ",".join(fails)[:300]),
                         "fails": self._fail_lines(fpath)}

    # -- proof step (F6 lives in laneproof) --------------------------------------------------

    @staticmethod
    def _base_of(wt):
        try:
            return (Path(wt) / ".vp" / "BASE").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @staticmethod
    def _proof_pending(tdir):
        try:
            return json.loads((Path(tdir) / "proof-pending.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _proof_step(self, task, attempt, row, contract, wt, tdir, cand, base, build_outcome=None):
        hdr = {}
        try:
            hdr, _ = vplint.parse_front_matter((wt / ".vp" / "PACKET.md").read_text(encoding="utf-8"))
            hdr = hdr or {}
        except (OSError, ValueError):
            pass
        pkind = str(hdr.get("proof_kind") or self.proof_cfg.get("default_kind") or "platform")
        paths = list(hdr.get("test_paths") or [])
        pending = {"sha": cand, "base": base, "kind": pkind, "paths": paths, "ts": utc_ms()}
        (tdir / "proof-pending.json").write_text(json.dumps(pending, indent=2), encoding="utf-8")
        pid = "proof-%s-%s" % (task, attempt[-15:])
        rec = self.proof.run(task, pid, wt, base, cand, pkind, paths, abort=lambda: self._abort.is_set())
        ppath = self.run_root / "proofs" / ("%s.json" % pid)
        status = rec.get("status")
        outcome = build_outcome or vprunners.TurnOutcome(STATUS_DONE, "proof only", runner="proof")
        evidence = [wt / ".vp" / "RESULT.json", wt / ".vp" / "FINDINGS.json", tdir / "record.json", ppath]
        if status == "PASS":
            try:
                (tdir / "proof-pending.json").unlink()
            except OSError:
                pass
            return outcome, {"outcome": "VERIFIED", "output_sha": cand, "tree_sha": self.tree_sha(wt),
                             "evidence": evidence}
        if status == "FAIL_PRODUCT":
            try:
                (tdir / "proof-pending.json").unlink()
            except OSError:
                pass
            nodes = rec.get("failed_nodes") or []
            errs = rec.get("errors") or {}
            return outcome, {"outcome": "REPAIR_REQUIRED", "output_sha": cand, "tree_sha": self.tree_sha(wt),
                             "evidence": evidence,
                             "reason": "proof %s FAIL_PRODUCT: %s" % (pid, ", ".join(nodes)[:300]),
                             "fails": [{"id": n, "note": (errs.get(n) or "red on %s" % rec.get("route"))[:300],
                                        "evidence": n} for n in nodes]}
        # FAIL_INFRA / UNKNOWN / CANCELLED: not the candidate's fault -- retry the
        # proof alone after the backoff (proof-pending.json keeps the head)
        return vprunners.TurnOutcome("PROOF_" + str(status), "proof %s %s: %s"
                                     % (pid, status, str(rec.get("reason") or "")[:200]),
                                     runner="proof"), None

    @staticmethod
    def _fail_lines(fpath):
        try:
            doc = json.loads(Path(fpath).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [{"id": str(l.get("id")), "note": str(l.get("note") or "")[:300],
                 "evidence": str(l.get("evidence") or "")[:300]}
                for l in doc.get("lines") or [] if isinstance(l, dict) and l.get("verdict") != "PASS"]

    PY_EXT = (".py",)
    PORTAL_EXT = (".ts", ".tsx", ".js", ".jsx", ".json", ".css", ".scss", ".md")

    def _tool(self, name, wt):
        """Prefer the worktree's own venv/node_modules binary, then the roster bins, then PATH."""
        for cand in (wt / "platform" / ".venv" / "bin" / name, wt / "agent" / ".venv" / "bin" / name,
                     wt / "portal" / "node_modules" / ".bin" / name, self.trunk / "platform" / ".venv" / "bin" / name,
                     self.trunk / "portal" / "node_modules" / ".bin" / name):
            if cand.exists():
                return str(cand)
        return self.bins.get(name) or shutil.which(name)

    def _autofix(self, wt, task, tdir=None, base=None):
        """Deterministic fixers after a builder turn (plan §2.1 point 6): ruff
        --fix + ruff format on touched .py, prettier on touched portal files,
        tsc --noEmit on the portal when touched (report only).  Whatever they
        changed is committed as `autofix:` so the grader and every reviewer
        see a tree no human formatting nit can fail."""
        base = base or self._base_of(wt)
        rc, out, _ = self.git(["-C", str(wt), "diff", "--name-only", "%s..HEAD" % base]) if base else (1, "", "")
        changed = [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []
        py = [f for f in changed if f.endswith(self.PY_EXT) and (wt / f).exists()]
        portal = [f for f in changed if f.startswith("portal/") and f.endswith(self.PORTAL_EXT)
                  and (wt / f).exists() and "node_modules" not in f]
        steps = []

        def run(name, argv, cwd):
            rc, o, e = self.exec.run(argv, cwd=str(cwd), timeout_s=600)
            steps.append({"tool": name, "argv": argv, "rc": rc, "stdout": (o or "")[-1500:],
                          "stderr": (e or "")[-1500:]})
            return rc

        ruff = self._tool("ruff", wt) if py else None
        if py and ruff:
            run("ruff-fix", [ruff, "check", "--fix", "--exit-zero"] + py, wt)
            run("ruff-format", [ruff, "format"] + py, wt)
        elif py:
            steps.append({"tool": "ruff", "skipped": "ruff not found"})
        prettier = self._tool("prettier", wt) if portal else None
        if portal and prettier:
            run("prettier", [prettier, "--write"] + portal, wt)
        elif portal:
            steps.append({"tool": "prettier", "skipped": "prettier not found"})
        tsc_rc = None
        if portal and (wt / "portal" / "tsconfig.json").exists():
            tsc = self._tool("tsc", wt)
            npx = self.bins.get("npx") or shutil.which("npx")
            if tsc:
                tsc_rc = run("tsc", [tsc, "--noEmit", "-p", "portal"], wt)
            elif npx:
                tsc_rc = run("tsc", [npx, "tsc", "--noEmit", "-p", "portal"], wt)
            else:
                steps.append({"tool": "tsc", "skipped": "tsc/npx not found"})
        rc, status, _ = self.git(["-C", str(wt), "status", "--porcelain", "--untracked-files=no"])
        committed = None
        if rc == 0 and status.strip():
            files = sorted(set(l[3:].strip() for l in status.splitlines() if l.strip()))
            self.git(["-C", str(wt), "add", "--"] + files, log=True)
            rc2, o2, e2 = self.git(["-C", str(wt), "-c", "user.email=lanedriver@vp", "-c", "user.name=lanedriver",
                                    "commit", "-q", "-m", "autofix: ruff/prettier (%s)" % task], log=True)
            committed = self.head_sha(wt) if rc2 == 0 else None
            if rc2 != 0:
                steps.append({"tool": "git-commit", "rc": rc2, "stderr": (e2 or o2)[-500:]})
            else:
                self.log("AUTOFIX %s committed %s (%d files)" % (task, committed[:12], len(files)))
        rec = {"ts": utc_ms(), "task": task, "base": base, "changed": changed, "py": py, "portal": portal,
               "steps": steps, "tsc_rc": tsc_rc, "committed": committed}
        if tdir is not None:
            try:
                (Path(tdir) / "autofix.json").write_text(json.dumps(rec, indent=2, sort_keys=True),
                                                        encoding="utf-8")
            except OSError:
                pass
        if tsc_rc:
            self.log("AUTOFIX %s tsc --noEmit rc=%s (reported to the grader via autofix.json)" % (task, tsc_rc))
        return rec

    def _review_pipeline(self, task, attempt, row, contract, server, runner, rcfg, wt, tdir, sid, base):
        state = self.control.state_view()
        cand = (state.get("candidate") or {}).get("sha") or base
        req = {"item": task, "subject": "item", "base": base, "candidate": cand,
               "reviewer": rcfg.get("model") or "reviewer",
               "benchmark_ids": self._benchmark_ids(wt), "unverified_ids": []}
        (wt / ".vp" / "REVIEW_REQUEST.json").write_text(json.dumps(req, indent=2, sort_keys=True),
                                                        encoding="utf-8")
        out_path = wt / ".vp" / "REVIEW.json"
        outcome = self._turn(task, attempt, row, server, runner, rcfg, wt, tdir, "reviewer",
                             REVIEW_PROMPT, out_path, vpschema.validate_review, sid, 1)
        if outcome.status != STATUS_DONE:
            return outcome, None
        try:
            doc = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        verdict = doc.get("verdict")
        if verdict == "APPROVE":
            packet = self._verdict_packet(task, row, contract, state, outcome, out_path, tdir, doc)
            return outcome, {"outcome": "VERIFIED", "output_sha": cand, "tree_sha": self.tree_sha(wt),
                             "evidence": [out_path, tdir / "record.json"], "verdict": packet}
        fails = [{"id": str(f.get("id")), "note": str(f.get("title") or "")[:300],
                  "evidence": "%s:%s" % (f.get("file"), f.get("line"))} for f in doc.get("findings") or []]
        return outcome, {"outcome": "REPAIR_REQUIRED", "evidence": [out_path, tdir / "record.json"],
                         "reason": "review %s: %s" % (verdict, str(doc.get("summary") or "")[:300]),
                         "fails": fails}

    def _verdict_packet(self, task, row, contract, state, outcome, out_path, tdir, doc):
        """review_gate packet from the review record; the scheduler validates it
        (runtime log under ~/.codex/sessions, reviewer == the task's child_id)."""
        role = contract.get("kind")
        role = {"final_review": "final_review"}.get(role, role)
        cand = state.get("candidate") or {}
        target = (row.get("parameters") or {}).get("parent_contract_id") or task
        tcon = self.control.contract(target)
        trow = (state.get("tasks") or {}).get(target) or {}
        crit = list(dict.fromkeys((tcon.get("verification") or []) + (tcon.get("acceptance") or [])))
        runtime = self._codex_rollout(outcome.session_id)
        authors = [a for a in (trow.get("child_id"), "lanedriver:%d" % os.getpid())
                   if a and a != outcome.session_id]
        packet = {"role": role, "verdict": "PASS", "candidate_sha": cand.get("sha"),
                  "tree_sha": cand.get("tree"), "catalog_sha256": (state.get("catalog") or {}).get("sha256"),
                  "review_task_id": task, "reviewer_session_id": outcome.session_id,
                  "author_session_ids": authors,
                  "unresolved_blocking_findings": [],
                  "runtime_log": {"path": str(runtime) if runtime else "",
                                  "sha256": sha256_file(runtime) if runtime else ""},
                  "artifacts": [{"path": str(out_path), "sha256": sha256_file(out_path)}],
                  "coverage": {target: {c: "PASS" for c in crit}},
                  "model_seen": outcome.model_seen, "summary": doc.get("summary")}
        p = tdir / "verdict-packet.json"
        p.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
        return p

    @staticmethod
    def _codex_rollout(thread_id, root=None):
        if not thread_id:
            return None
        root = Path(root or (Path.home() / ".codex" / "sessions"))
        try:
            hits = sorted(root.rglob("rollout-*-%s.jsonl" % thread_id))
        except OSError:
            hits = []
        return hits[0] if hits else None

    def _benchmark_ids(self, wt):
        try:
            return vplint.benchmark_ids(wt / ".vp" / "BENCHMARK.md")
        except Exception:
            return []

    # -- one runner turn (with resumes), records prompt/raw/record ---------------------------

    def _turn(self, task, attempt, row, server, runner, rcfg, wt, tdir, role, prompt, out_path,
              validator, sid, rnd, expect_fence=False):
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass
        mm = self.conc.get("max_minutes_per_turn", {})
        timeout_s = float(mm.get(role, mm.get("default", 45))) * 60
        n, resumes, outcome = 0, 0, None
        while True:
            n += 1
            tag = "%d-r%d-%s" % (n, rnd, role)
            text = prompt if n == 1 else RESUME_PROMPT
            spec = self._spec(role, task, wt, text, rcfg, runner, server, sid, out_path, validator,
                              timeout_s, tdir, tag, expect_fence, rnd)
            try:
                (tdir / ("%s-prompt.md" % tag)).write_text(text, encoding="utf-8")
                if not (tdir / "prompt.md").exists():
                    (tdir / "prompt.md").write_text(text, encoding="utf-8")
            except OSError:
                pass
            outcome = self.runners[runner].run(spec, abort_flag=self._abort)
            sid = outcome.session_id or sid
            self._save_session(tdir, sid, runner)
            if self._stopping and outcome.status != STATUS_DONE:
                outcome.status, outcome.detail = STATUS_ABORTED, "STOP"
            cost, est, basis = estimate_cost(self.pricing, runner, spec.model, outcome.usage)
            if basis == "unpriced" and runner == "codex":
                # F3: Codex is a subscription -- tokens are the spend, dollars are 0
                cost, basis = 0.0, "subscription"
            if basis == "unpriced" and (outcome.usage or {}).get("tokens_in") is not None:
                self.alert_once("pricing:%s:%s" % (runner, spec.model), "UNPRICED",
                                "%s model %s reports tokens but no USD and pricing.%s.models[%s] "
                                "is missing; its spend counts 0 toward the USD cap"
                                % (runner, spec.model, runner, spec.model))
            if basis != "reported" and outcome.usage is not None:
                outcome.usage["cost"] = cost
            self._write_record(task, attempt, tdir, tag, role, runner, server, spec, outcome, rnd, n)
            _append_jsonl(self.costs_path, {
                "ts": utc_ms(), "task": task, "attempt": attempt, "n": n, "round": rnd,
                "role": role, "runner": runner, "model": spec.model, "server": server,
                "cost": cost or 0.0, "est_cost_usd": est, "cost_basis": basis,
                "tokens_in": (outcome.usage or {}).get("tokens_in"),
                "tokens_out": (outcome.usage or {}).get("tokens_out"),
                "cache_read": (outcome.usage or {}).get("cache_read"),
                "status": outcome.status, "duration_s": outcome.duration_s}, self._lock)
            self.log("TURN %s %s %s n=%d -> %s %s" % (task, attempt, role, n, outcome.status,
                                                    (outcome.detail or "")[:120]))
            if outcome.status == STATUS_PROGRESS_STOP and not self._stopping \
                    and runner == "opencode" and resumes < MAX_RESUMES:
                resumes += 1
                continue
            break
        if outcome.status == STATUS_PROGRESS_STOP:
            outcome.status = "STUCK"
        if runner == "opencode" and sid and outcome.status != STATUS_ABORTED \
                and hasattr(self.runners[runner], "export"):
            try:
                self.runners[runner].export(spec, sid, str(tdir / ("%s-export.json" % tag)))
            except Exception as exc:
                self.log("export failed %s: %s" % (task, exc))
        return outcome

    def _spec(self, role, task, wt, prompt, rcfg, runner, server, sid, out_path, validator,
              timeout_s, tdir, tag, expect_fence, rnd):
        base = dict(variant=rcfg.get("variant"), agent=rcfg.get("agent"), session_id=sid,
                    out_path=str(out_path), timeout_s=timeout_s, log_dir=str(tdir), tag=tag,
                    validator=validator, title="%s %s r%d" % (task, role, rnd),
                    effort=rcfg.get("effort"))
        if runner == "opencode":
            srv = self.servers[server]
            return TurnSpec(role, task, wt, prompt, rcfg.get("model"), server_url=srv["url"],
                            xdg_data_home=srv["xdg"], expect_fence=expect_fence, **base)
        if runner == "codex":
            return TurnSpec(role, task, wt, prompt, rcfg.get("model"),
                            sandbox=rcfg.get("sandbox", "read-only"),
                            schema_path=str(self.here / "schemas" / "REVIEW_SCHEMA.json"), **base)
        if runner == "agy":
            return TurnSpec(role, task, wt, prompt, rcfg.get("model"),
                            schema_text=json.dumps(vpschema.REVIEW_SCHEMA_DOC), **base)
        settings = self.run_root / "claude-settings" / ("%s.json" % role)
        return TurnSpec(role, task, wt, prompt, rcfg.get("model", "opus"),
                        schema_text=json.dumps(vpschema.REVIEW_SCHEMA_DOC),
                        settings_path=str(settings) if settings.exists() else None,
                        max_turns=rcfg.get("max_turns", 30),
                        max_budget_usd=self.budget.get("claude_max_budget_usd_per_call", 3),
                        add_dirs=[str(self.run_root)], **base)

    def _write_record(self, task, attempt, tdir, tag, role, runner, server, spec, outcome, rnd, n):
        payload = outcome.to_dict()
        payload.update({"task": task, "attempt": attempt, "n": n, "round": rnd, "role": role,
                        "runner": runner, "server": server, "model": spec.model,
                        "variant": spec.variant, "effort": spec.effort,
                        "prompt_sha256": sha256_text(spec.prompt), "prompt_bytes": len(spec.prompt),
                        "prompt_path": str(tdir / ("%s-prompt.md" % tag)), "cwd": spec.cwd,
                        "written_ts": utc_ms()})
        try:
            (tdir / ("%s-record.json" % tag)).write_text(json.dumps(payload, indent=2, sort_keys=True,
                                                                     default=str), encoding="utf-8")
            (tdir / "record.json").write_text(json.dumps(payload, indent=2, sort_keys=True,
                                                         default=str), encoding="utf-8")
            # raw.jsonl: the runner's full raw stream, verbatim, one JSON line
            # per event, framed by a header line naming the turn it came from
            raw = tdir / "raw.jsonl"
            with open(raw, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"vp": "turn", "tag": tag, "runner": runner, "role": role,
                                     "n": n, "round": rnd, "log": str(outcome.log_path or ""),
                                     "ts": utc_ms()}) + "\n")
                if outcome.log_path and Path(outcome.log_path).exists():
                    text = Path(outcome.log_path).read_text(encoding="utf-8", errors="replace")
                    if text.lstrip().startswith("{") and "\n" in text.strip() and runner == "claude":
                        fh.write(json.dumps({"vp": "claude-result", "raw": text}) + "\n")
                    else:
                        for line in text.splitlines():
                            fh.write(line.rstrip("\n") + "\n")
        except OSError:
            pass

    # -- frontier (idle rule) -------------------------------------------------------------

    def _frontier_tick(self, state):
        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = utc_ms()
        every = float(self.alerts_cfg.get("frontier_every_s", 60))
        if now - self._last_frontier_mono < every:
            return
        self._last_frontier_mono = now
        try:
            fr = self.control.frontier()
        except ControlError as exc:
            self.alert_once("control:frontier", "CONTROL_DOWN", "frontier failed: %s" % str(exc)[:200])
            return
        counts = fr.get("counts") or {}
        self.log("FRONTIER seq=%s counts=%s warnings=%s" % (fr.get("sequence"), json.dumps(counts),
                                                             fr.get("warnings")))
        acted = self._frontier_actions(state, fr)
        if acted:
            self._last_idle_alert_mono = None
            return
        idle_every = float(self.alerts_cfg.get("idle_every_min", 30)) * 60
        if self._last_idle_alert_mono is None or now - self._last_idle_alert_mono >= idle_every:
            self._last_idle_alert_mono = now
            self.alert("IDLE", "nothing dispatchable since %s; counts=%s; actions=%s; warnings=%s"
                       % (self._idle_since, json.dumps(counts),
                          json.dumps(fr.get("actions") or [])[:600], fr.get("warnings")))

    def _frontier_actions(self, state, fr):
        acted = 0
        tasks = state.get("tasks") or {}
        promoted = set()
        for task, row in tasks.items():
            if row.get("template_id") == "REPAIR" and row.get("state") == "VERIFIED" \
                    and row.get("unlocks_dependents") and row.get("output_sha"):
                parent = (row.get("parameters") or {}).get("parent_contract_id")
                prow = tasks.get(parent) or {}
                if prow.get("state") == "REPAIR_REQUIRED":
                    if self._promote_parent(parent, task, row):
                        acted += 1
                        promoted.add(parent)
        for a in fr.get("actions") or []:
            if a.get("state") == "REPAIR_REQUIRED" and a["task"] not in promoted:
                if self._instantiate_repair(a["task"], tasks):
                    acted += 1
        return acted

    def _instantiate_repair(self, parent, tasks):
        prow = tasks.get(parent) or {}
        for t, r in tasks.items():
            if r.get("template_id") != "REPAIR" or \
                    (r.get("parameters") or {}).get("parent_contract_id") != parent:
                continue
            if r.get("state") not in FINISHED_STATES:
                return False          # a repair is live
            if r.get("state") == "VERIFIED" and r.get("unlocks_dependents"):
                return False          # consumable repair awaiting promotion
        harvest = self._last_harvest(parent)
        fails = harvest.get("fails") or []
        defect = (fails[0]["id"] if fails else "REPAIR").replace("/", "-")
        n = 1 + sum(1 for t, r in tasks.items()
                    if r.get("template_id") == "REPAIR" and (r.get("parameters") or {}).get("parent_contract_id") == parent)
        task = "R-%s-%s-%d" % (parent, defect, n)
        if task in tasks:
            return False
        contract = self.control.contract(parent, prow)
        params = {"parent_contract_id": parent, "defect_id": defect,
                  "failing_criterion": (fails[0]["note"] if fails else str(prow.get("blocker") or "")[:300]),
                  "reproduction": (fails[0]["evidence"] if fails else "see harvest.json"),
                  "reviewed_sha": prow.get("output_sha") or prow.get("base_sha") or self.trunk_sha() or "",
                  "owned_paths": self._claim_paths(contract, prow),
                  "fails": fails, "harvest": harvest.get("path")}
        pdir = self.run_root / "repairs"
        pdir.mkdir(parents=True, exist_ok=True)
        ppath = pdir / ("%s.json" % task)
        ppath.write_text(json.dumps(params, indent=2, sort_keys=True), encoding="utf-8")
        try:
            self.control.instantiate("REPAIR", task, parent, prow.get("chief") or "A", ppath)
        except ControlError as exc:
            self.alert_once("repair:%s" % parent, "REPAIR_REFUSED",
                            "could not instantiate a repair for %s: %s" % (parent, str(exc)[:200]), parent)
            return False
        self.log("INSTANTIATE REPAIR %s for %s (%s)" % (task, parent, defect))
        self.alert("REPAIR", "%s instantiated for %s defect %s" % (task, parent, defect), parent)
        return True

    def _last_harvest(self, task):
        d = self.turns_root / task
        try:
            cands = sorted(d.glob("*/harvest.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            cands = []
        if not cands:
            return {}
        try:
            data = json.loads(cands[-1].read_text(encoding="utf-8"))
            data["path"] = str(cands[-1])
            return data
        except (OSError, ValueError):
            return {}

    def _promote_parent(self, parent, repair, rrow):
        ev = [e["path"] for e in rrow.get("evidence") or [] if Path(e.get("path", "")).exists()]
        if not ev:
            return False
        try:
            self.control.promote(parent, rrow["output_sha"], rrow.get("tree_sha") or rrow["output_sha"],
                                 ev, supporting=[repair])
        except ControlError as exc:
            self.alert_once("promote:%s:%s" % (parent, repair), "PROMOTE_REFUSED",
                            "%s via %s: %s" % (parent, repair, str(exc)[:200]), parent)
            return False
        self.log("PROMOTE %s via %s -> INTEGRATED" % (parent, repair))
        self.alert("PROMOTED", "%s promoted with repair %s" % (parent, repair), parent)
        return True

    # -- loop -------------------------------------------------------------------------

    def join(self, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        for th in list(self._threads):
            th.join(None if end is None else max(0.0, end - time.monotonic()))

    def run_once(self):
        n = self.tick()
        self.join()
        self._finish("once")
        return n

    def loop(self, max_ticks=None):
        ticks = 0
        while True:
            self.tick()
            ticks += 1
            with self._lock:
                live = len(self._live)
            if self._stopping and live == 0:
                self._finish("STOP")
                return ticks
            if max_ticks is not None and ticks >= max_ticks:
                self.join()
                self._finish("max_ticks")
                return ticks
            time.sleep(self.interval)


class DryRunRunner(object):
    """Ladder / dry-run runner: no model, no network.  Writes a valid record for
    the role (RESULT / FINDINGS / REVIEW), commits a marker file for build-like
    roles, and reports zero-cost usage.  `verdict` scripts the grader/reviewer
    answer per task id (default PASS/APPROVE)."""
    name = "dryrun"

    def __init__(self, exec_=None, verdicts=None, delay_s=0.0):
        self.exec = exec_ or vprunners.Exec()
        self.verdicts = dict(verdicts or {})
        self.delay_s = float(delay_s)
        self.calls = []

    def _git(self, wt, *args):
        return self.exec.run(["git", "-C", str(wt)] + list(args), timeout_s=120)

    def run(self, spec, abort_flag=None):
        self.calls.append((spec.role, spec.item))
        wt = Path(spec.cwd)
        if self.delay_s:
            end = time.monotonic() + self.delay_s
            while time.monotonic() < end:
                if abort_flag is not None and abort_flag.is_set():
                    return vprunners.TurnOutcome(STATUS_ABORTED, "STOP", runner=self.name)
                time.sleep(0.05)
        usage = {"tokens_in": 0, "tokens_out": 0, "tokens_reason": 0, "cache_read": 0,
                 "cache_write": 0, "cost": 0.0}
        sid = "dry-%s-%s" % (spec.role, spec.item)
        base = (wt / ".vp" / "BASE").read_text(encoding="utf-8").strip() \
            if (wt / ".vp" / "BASE").exists() else ""
        if spec.role in ("builder", "probe"):
            marker = wt / "control" / "dryrun" / ("%s.txt" % spec.item)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("%s %s\n" % (spec.role, utc_ms()), encoding="utf-8")
            self._git(wt, "add", "-A")
            self._git(wt, "-c", "user.email=dryrun@vp", "-c", "user.name=dryrun",
                      "commit", "-q", "--allow-empty", "-m", "dryrun %s" % spec.item)
            _rc, head, _ = self._git(wt, "rev-parse", "HEAD")
            rec = {"item": spec.item, "attempt": 1, "commit": head.strip(), "base": base,
                   "diff_stat": {"files": 1, "insertions": 1, "deletions": 0},
                   "checks": [{"name": "dryrun", "command": "true", "exit": 0, "log": "dry run"}],
                   "disputes": [], "blocked": None, "notes": "dry run, no model"}
        elif spec.role == "grader":
            _rc, head, _ = self._git(wt, "rev-parse", "HEAD")
            v = self.verdicts.get(spec.item, "PASS")
            try:
                ids = vplint.benchmark_ids(wt / ".vp" / "BENCHMARK.md")
            except Exception:
                ids = ["B1"]
            rec = {"item": spec.item, "attempt": 1, "commit": head.strip(),
                   "lines": [{"id": i, "kind": "evidence", "verdict": v,
                              "evidence": "control/dryrun/%s.txt:1" % spec.item,
                              "note": "dry run"} for i in ids],
                   "all_pass": v == "PASS"}
        else:
            req = {}
            try:
                req = json.loads((wt / ".vp" / "REVIEW_REQUEST.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            v = self.verdicts.get(spec.item, "APPROVE")
            rec = {"item": spec.item, "subject": "item", "base": req.get("base") or base,
                   "candidate": req.get("candidate") or base, "reviewer": req.get("reviewer") or "dryrun",
                   "verdict": v,
                   "verdicts": [{"id": i, "verdict": "PASS" if v == "APPROVE" else "FAIL",
                                 "evidence": "control/dryrun/%s.txt:1" % spec.item}
                                for i in req.get("benchmark_ids") or ["B1"]],
                   "findings": [] if v == "APPROVE" else [
                       {"id": "F1", "severity": "medium", "title": "dry run finding",
                        "file": "control/dryrun/%s.txt" % spec.item, "line": 1,
                        "detail": "scripted", "reproduce": None, "benchmark_line": "B1"}],
                   "evidence": ["control/dryrun/%s.txt:1" % spec.item], "summary": "dry run"}
        out = Path(spec.out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
        ok, errs = True, []
        if spec.validator:
            ok, errs = spec.validator(out)
        if spec.log_dir:
            Path(spec.log_dir).mkdir(parents=True, exist_ok=True)
            (Path(spec.log_dir) / ("%s-dryrun.jsonl" % spec.tag)).write_text(
                json.dumps({"type": "dryrun", "role": spec.role, "item": spec.item}) + "\n",
                encoding="utf-8")
        st = STATUS_DONE if ok else STATUS_INCOMPLETE
        return vprunners.TurnOutcome(st, "; ".join(errs[:4]), session_id=sid, usage=usage,
                                     record_path=str(out), runner=self.name,
                                     log_path=str(Path(spec.log_dir) / ("%s-dryrun.jsonl" % spec.tag))
                                     if spec.log_dir else None)

    def export(self, spec, session_id, dest_path):
        Path(dest_path).write_text("{}", encoding="utf-8")
        return {"path": str(dest_path), "bytes": 2}


# --------------------------------------------------------------------------
# control surface: drain / undrain / restart / item (F1, F8, F9)
# --------------------------------------------------------------------------

HEARTBEAT_FRESH_S = 60.0


def read_heartbeat(run_root):
    try:
        hb = json.loads((Path(run_root) / "driver.heartbeat").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ts = hb.get("ts")
    try:
        age = time.time() - datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        age = None
    hb["age_s"] = age
    hb["fresh"] = age is not None and age < HEARTBEAT_FRESH_S and not hb.get("finished")
    return hb


def live_tasks(run_root):
    hb = read_heartbeat(run_root)
    if not hb or not hb["fresh"]:
        return {}
    return dict(hb.get("live") or {})


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def cmd_drain(drv, args):
    row = drv.write_drain(args.reason, args.deadline)
    drv.alert("DRAIN", "drain requested: %s (deadline %s min)" % (args.reason, args.deadline))
    print(json.dumps({"status": "DRAINING", "drain": row}, indent=2))
    return 0


def cmd_undrain(drv, args):
    ok = drv.clear_drain(args.reason or "undrain")
    print(json.dumps({"status": "ACTIVE" if ok else "NOT_DRAINING"}, indent=2))
    return 0


def wait_until(pred, timeout_s, poll_s=1.0):
    end = time.monotonic() + float(timeout_s)
    while True:
        if pred():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(poll_s)


def cmd_restart(drv, args, python=None, here=None):
    """F9: drain -> wait heartbeat active=0 -> STOP -> wait finished -> start.
    Never kills a process: a driver that ignores STOP is reported, not shot."""
    run_root = drv.run_root
    hb = read_heartbeat(run_root)
    report = {"steps": []}
    drv.write_drain("restart", args.timeout)
    report["steps"].append("DRAIN written (deadline %s min)" % args.timeout)
    if hb and hb["fresh"] and _pid_alive(hb.get("pid")):
        quiet = wait_until(lambda: (read_heartbeat(run_root) or {}).get("active", 1) == 0,
                           args.timeout * 60, args.poll)
        report["steps"].append("active=0 %s" % ("reached" if quiet else "NOT reached in time"))
        if not quiet and not args.force:
            drv.clear_drain("restart aborted: turns still live")
            drv.alert("RESTART_ABORTED", "active turns did not finish within %s min; drain "
                      "released, driver left running" % args.timeout)
            report["status"] = "ABORTED"
            print(json.dumps(report, indent=2))
            return 2
        drv.stop_file.write_text("restart %s\n" % utc_ms(), encoding="utf-8")
        report["steps"].append("STOP written")
        pid = hb.get("pid")
        gone = wait_until(lambda: not _pid_alive(pid) or bool((read_heartbeat(run_root) or {}).get("finished")),
                          max(60, drv.stop_grace_s + 60), args.poll)
        report["steps"].append("old driver pid %s %s" % (pid, "exited" if gone else "STILL RUNNING"))
        if not gone:
            drv.alert("RESTART_STUCK", "driver pid %s ignored STOP for %ds; not killed -- owner action"
                      % (pid, drv.stop_grace_s + 60))
            report["status"] = "STUCK"
            print(json.dumps(report, indent=2))
            return 3
    else:
        report["steps"].append("no live driver (heartbeat stale or pid gone)")
    try:
        drv.stop_file.unlink()
    except OSError:
        pass
    drv.clear_drain("restart complete")
    report["steps"].append("STOP + DRAIN cleared")
    if args.no_start:
        report["status"] = "STOPPED"
        print(json.dumps(report, indent=2))
        return 0
    python = python or sys.executable
    here = here or Path(__file__).resolve()
    argv = [python, str(here), "--roster", str(drv.roster_path), "run", "--loop",
            "--interval", str(args.interval)]
    if args.fake_runners:
        argv.append("--fake-runners")
    out = open(run_root / "driver.out", "a", encoding="utf-8")
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                            start_new_session=True, cwd=str(here.parent))
    report["steps"].append("started pid %d: %s" % (proc.pid, " ".join(argv)))
    report["status"] = "RESTARTED"
    report["pid"] = proc.pid
    drv.alert("RESTART", "driver restarted: pid %d" % proc.pid)
    print(json.dumps(report, indent=2))
    return 0


def cmd_item(drv, args):
    """F8: hand status edits refuse while the driver has a live turn on the task."""
    live = live_tasks(drv.run_root)
    if args.task in live:
        print(json.dumps({"status": "REFUSED", "task": args.task,
                          "reason": "attempt %s is RUNNING under the live driver; stop or drain first"
                          % live[args.task]}, indent=2))
        return 4
    try:
        if args.item_cmd == "block":
            _rc, data = drv.control.call("block", ["--task", args.task, "--blocker-class", args.blocker_class,
                                                   "--reason", args.reason, "--unblock-action", args.unblock_action,
                                                   "--evidence", args.evidence])
        elif args.item_cmd == "unblock":
            _rc, data = drv.control.call("unblock", ["--task", args.task, "--evidence", args.evidence])
        else:
            _rc, data = drv.control.complete(args.task, args.attempt_id, args.outcome,
                                             args.evidence or [], args.output_sha, args.tree_sha,
                                             args.reason, None, args.unlock_dependents)
    except ControlError as exc:
        print(json.dumps({"status": "REFUSED_BY_SCHEDULER", "task": args.task, "error": str(exc)}, indent=2))
        return 5
    drv.log("ITEM %s %s by hand" % (args.item_cmd, args.task))
    print(json.dumps({"status": "OK", "task": args.task, "result": data}, indent=2, default=str))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--roster", required=True)
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="tick loop (default when no command is given)")
    g = run.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", action="store_true")
    run.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    run.add_argument("--max-ticks", type=int, default=None)
    run.add_argument("--fake-runners", action="store_true",
                     help="dry run: every runner is DryRunRunner (no model, no network)")
    d = sub.add_parser("drain", help="F1: no new claims; released by the driver after --deadline")
    d.add_argument("--reason", required=True)
    d.add_argument("--deadline", type=float, default=30.0, help="minutes; 0 = no deadline")
    u = sub.add_parser("undrain")
    u.add_argument("--reason")
    r = sub.add_parser("restart", help="F9: drain -> active=0 -> STOP -> start")
    r.add_argument("--timeout", type=float, default=30.0, help="minutes to wait for active=0")
    r.add_argument("--poll", type=float, default=2.0)
    r.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    r.add_argument("--force", action="store_true", help="STOP even if turns are still live")
    r.add_argument("--no-start", action="store_true")
    r.add_argument("--fake-runners", action="store_true")
    it = sub.add_parser("item", help="F8: hand edits, refused while the task has a live turn")
    isub = it.add_subparsers(dest="item_cmd", required=True)
    b = isub.add_parser("block")
    b.add_argument("task")
    b.add_argument("--blocker-class", required=True)
    b.add_argument("--reason", required=True)
    b.add_argument("--unblock-action", required=True)
    b.add_argument("--evidence", required=True)
    ub = isub.add_parser("unblock")
    ub.add_argument("task")
    ub.add_argument("--evidence", required=True)
    c = isub.add_parser("complete")
    c.add_argument("task")
    c.add_argument("--attempt-id", required=True)
    c.add_argument("--outcome", required=True)
    c.add_argument("--unlock-dependents", action="store_true")
    c.add_argument("--output-sha")
    c.add_argument("--tree-sha")
    c.add_argument("--evidence", action="append", default=[])
    c.add_argument("--reason")
    cm = sub.add_parser("comms", help="append one cross-session message to RUN_ROOT/comms.jsonl")
    cm.add_argument("--from", dest="sender", required=True, help="e.g. chief, architect, fixer")
    cm.add_argument("--to", required=True)
    cm.add_argument("--text", required=True)
    cm.add_argument("--kind", default="message")
    cm.add_argument("--task")
    cm.add_argument("--ref", help="session id / message id the line answers")
    se = sub.add_parser("seal", help="write MANIFEST-<day>.json (sha256 of every audit file)")
    se.add_argument("--day", help="IST day YYYY-MM-DD; default today")
    se.add_argument("--force", action="store_true", help="rewrite an existing manifest")
    return ap


def cmd_comms(drv, args):
    row = drv.comms(args.sender, args.to, args.text, kind=args.kind, task=args.task, ref=args.ref)
    print(json.dumps(row))
    return 0


def cmd_seal(drv, args):
    out = drv.seal(args.day, force=args.force)
    if out is None:
        print(json.dumps({"status": "EXISTS", "day": args.day or drv.ist_now().strftime("%Y-%m-%d")}))
        return 0
    print(json.dumps({"status": "OK", "manifest": str(out)}))
    return 0


SUBCOMMANDS = ("run", "drain", "undrain", "restart", "item", "comms", "seal")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and not any(a in argv for a in SUBCOMMANDS):
        argv = argv[:2] + ["run"] + argv[2:] if argv[0] == "--roster" else argv
    args = build_parser().parse_args(argv)
    if args.cmd == "run":
        runners = None
        if args.fake_runners:
            dry = DryRunRunner()
            runners = {"opencode": dry, "codex": dry, "claude": dry, "agy": dry}
        drv = LaneDriver(args.roster, interval=args.interval, runners=runners,
                         bins={"osascript": shutil.which("osascript")} if shutil.which("osascript") else None)
        if args.fake_runners:
            drv.log("FAKE RUNNERS in force: no model call will be made")
        drv.log("lanedriver start pid=%d roster=%s" % (os.getpid(), drv.roster_path))
        drv.comms("lanedriver", "audit", "start pid=%d fake_runners=%s" % (os.getpid(), args.fake_runners),
                  kind="lifecycle")
        if args.once:
            drv.run_once()
        else:
            drv.loop(max_ticks=args.max_ticks)
        return 0
    drv = LaneDriver(args.roster, runners={})
    if args.cmd == "drain":
        return cmd_drain(drv, args)
    if args.cmd == "undrain":
        return cmd_undrain(drv, args)
    if args.cmd == "restart":
        return cmd_restart(drv, args)
    if args.cmd == "item":
        return cmd_item(drv, args)
    if args.cmd == "comms":
        return cmd_comms(drv, args)
    if args.cmd == "seal":
        return cmd_seal(drv, args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
