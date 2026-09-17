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
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import laneproof  # noqa: E402
import vplint     # noqa: E402
import vppack     # noqa: E402
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

# catalog kind -> roster role (plan §2.2 names).  A roster may override any
# entry under "kind_map"; a role named directly under "roles" always wins.
DEFAULT_PROOF_KINDS = ["builder", "integrator", "infra"]
# D12 hot reload: RUN_ROOT/RELOAD (owner-created) -> quiesce new claims, wait for
# active == 0, reload these helper modules in dependency order, load a fresh copy
# of lanedriver.py and rebind every live object's class to it, resume.
RELOAD_FILE = "RELOAD"
RELOAD_ORDER = ("vpstore", "vpschema", "vplint", "vpcircle", "vpdriver", "vpproof", "vpmerge",
                "vprunners", "vppack", "laneproof", "lanedryrun")
HOSTED_TAG_RE = re.compile(r"^-\s*(B\d+)\b.*\[hosted\]", re.M)

KIND_MAP = {"builder": "builder", "design": "builder", "integration": "integrator",
            "probe": "probe", "control": "probe", "verification": "grader", "grader": "grader",
            "operations": "infra", "provider": "infra", "security_build": "security",
            "junior": "junior", "final_review": "final", "adjudicator": "adjudicator",
            "ruling": "ruling", "advisor": "advisor"}


def normalize_roster(data):
    """Accept the Architect's roster-v13 vocabulary next to the driver's own:
    `run.scheduler/run_state/catalog/packets_dir` -> `control{}` + `run.pack_dir`;
    `servers[].role == "fallback"` -> parked; `night.render_every_min` ->
    `alerts.render_every_s`; kinds without a role resolve through `kind_map`.
    Pure: returns a new dict, never writes the file."""
    r = json.loads(json.dumps(data))
    run = r.setdefault("run", {})
    ctl = r.setdefault("control", {})
    if run.get("scheduler") and not ctl.get("script"):
        ctl["script"] = run["scheduler"]
        ctl.setdefault("cwd", str(Path(os.path.expanduser(run["scheduler"])).parent))
    if run.get("run_state") and not ctl.get("state"):
        ctl["state"] = run["run_state"]
    if run.get("catalog") and not ctl.get("catalog"):
        ctl["catalog"] = run["catalog"]
    if run.get("packets_dir") and not run.get("pack_dir"):
        run["pack_dir"] = run["packets_dir"]
    for srv in (r.get("servers") or {}).values():
        if "parked" not in srv and srv.get("role") == "fallback":
            srv["parked"] = True
    night = r.get("night") or {}
    alerts = r.setdefault("alerts", {})
    if night.get("render_every_min") and "render_every_s" not in alerts:
        alerts["render_every_s"] = float(night["render_every_min"]) * 60
    if night.get("status_line_every_min") and "idle_every_min" not in alerts:
        alerts["idle_every_min"] = night["status_line_every_min"]
    kind_map = dict(KIND_MAP)
    kind_map.update(r.get("kind_map") or {})
    roles = r.setdefault("roles", {})
    for kind, role in kind_map.items():
        if kind not in roles and role in roles:
            roles[kind] = roles[role]
    r["kind_map"] = kind_map
    # v13: every packet names a proof_kind and 06-ROUTING §5 routes it (box targeted /
    # CircleCI full), so the building roles owe a proof unless the roster says otherwise
    # (the v12 default "integration" matches no v13 kind and silently ran none).
    proof = dict(r.get("proof") or {})
    if "require_for_kinds" not in proof and run.get("pack_dir"):
        proof["require_for_kinds"] = DEFAULT_PROOF_KINDS
        r["proof"] = proof
    return r


def snapshot_roster(run_root, content):
    """07 §1: roster.json is the one rewritable file; every edit is also kept
    as roster.<n>.json (append-only history).  Returns the snapshot path, or
    None when the latest snapshot already holds this content."""
    run_root = Path(run_root)
    snaps = sorted(run_root.glob("roster.*.json"),
                   key=lambda p: int(p.name.split(".")[1]) if p.name.split(".")[1].isdigit() else -1)
    snaps = [p for p in snaps if p.name.split(".")[1].isdigit()]
    if snaps and snaps[-1].read_bytes() == content:
        return None
    n = (int(snaps[-1].name.split(".")[1]) + 1) if snaps else 1
    out = run_root / ("roster.%d.json" % n)
    out.write_bytes(content)
    return out


def cmd_init_run(args):
    """§4(a) / 06 §0: copy v13-pack/roster-v13.json to RUN_ROOT/roster.json at
    start (RUN_ROOT from the roster's run.run_root unless --run-root), lint it,
    snapshot it as roster.<n>.json, and refuse a roster with lint errors."""
    import vplint
    src = Path(args.source).resolve()
    data = json.loads(src.read_text(encoding="utf-8"))
    run_root = Path(os.path.expanduser(args.run_root or data.get("run", {}).get("run_root") or "")).resolve() \
        if (args.run_root or data.get("run", {}).get("run_root")) else None
    if run_root is None:
        print(json.dumps({"status": "REFUSED", "reason": "no run.run_root in %s and no --run-root" % src}))
        return 2
    msgs = vplint.lint_roster(str(src))
    errors = [m for m in msgs if m.startswith("ERROR")]
    if errors and not args.force:
        print(json.dumps({"status": "REFUSED", "reason": "vplint roster errors", "lint": msgs}, indent=2))
        return 2
    run_root.mkdir(parents=True, exist_ok=True)
    dest = run_root / "roster.json"
    content = src.read_bytes()
    changed = not dest.exists() or dest.read_bytes() != content
    if changed:
        dest.write_bytes(content)
    snap = snapshot_roster(run_root, content)
    for sub in ("turns", "probes", "proofs", "claude-settings"):
        (run_root / sub).mkdir(exist_ok=True)
    rec = {"ts": utc_ms(), "op": "init-run", "source": str(src), "source_sha256": sha256_file(src),
           "roster": str(dest), "changed": changed, "snapshot": str(snap) if snap else None, "lint": msgs}
    _append_jsonl(run_root / "git.jsonl", rec)
    print(json.dumps(dict(rec, status="OK"), indent=2))
    return 0


class LaneDriver(object):

    def __init__(self, roster_path, exec_=None, runners=None, control=None,
                 interval=DEFAULT_INTERVAL, bins=None, clock=None, proof=None):
        self.roster_path = Path(roster_path).resolve()
        self.run_root = self.roster_path.parent
        self.roster = normalize_roster(json.loads(self.roster_path.read_text(encoding="utf-8")))
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
        self.fake_runners = False
        self._gate_last = {}
        # §4(c): the packet layer -- loaded from run.pack_dir, bound to tasks by
        # _pack_reconcile (once after the scheduler reconcile, then every
        # alerts.pack_every_s to bind packets whose dependencies just appeared)
        self._authority_stop = False
        self._reload_pending = None      # {reason, requested_at} while quiescing for a reload
        self._reload_failed = None       # error text after a failed reload (no new claims)
        self._reload_count = 0
        self._code_hashes = self.code_hashes()
        self.pack, self.pack_lint = {}, []
        self.pack_by_task = {}
        self._pack_last_mono = None
        self._pack_logged = set()
        self._load_pack()

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
        self.regrade_once = bool((data.get("review") or {}).get("regrade_same_commit_on_unknown_once", True))
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
        self._authority_check()
        return (not self._budget_stop and not self._disk_paused and not self._authority_stop
                and not self._reload_failed)

    # -- §4(d) authority: the scheduler + review_gate the driver runs are the pinned ones ---

    AUTHORITY_EVERY_S = 60.0

    def _authority_check(self):
        """CATALOG-AUTHORITY.json pins the scheduler, the catalog and the review
        validator (the s3-adjacent review_gate.py with `_fork_cutoff`).  The
        scheduler imports review_gate from its own directory, so that is the copy
        L42 runs with.  A hash mismatch or a copy without `_fork_cutoff` /
        `MODEL_ALIASES` stops every new dispatch (AUTHORITY_MISMATCH) -- the
        activation rule says refuse startup, and a mid-run edit is the same
        failure -- until the file or the pin is corrected."""
        last = getattr(self, "_authority_last", None)
        if last is not None and time.monotonic() - last < self.AUTHORITY_EVERY_S:
            return
        self._authority_last = time.monotonic()
        problems = self.authority_problems()
        if problems and not self._authority_stop:
            self._authority_stop = True
            self.alert("AUTHORITY_MISMATCH", "; ".join(problems)[:600])
        elif not problems and self._authority_stop:
            self._authority_stop = False
            self.log("authority restored: scheduler/catalog/review_gate match CATALOG-AUTHORITY.json")

    def authority_problems(self):
        ctl_dir = Path(self.control.cwd) if getattr(self.control, "cwd", None) else Path(self.control.script).parent
        gate = ctl_dir / "review_gate.py"
        problems = []
        try:
            text = gate.read_text(encoding="utf-8")
        except OSError:
            return ["review_gate.py missing next to the scheduler (%s)" % gate]
        for marker in ("def _fork_cutoff", "MODEL_ALIASES"):
            if marker not in text:
                problems.append("review_gate.py at %s lacks %s (older copy; L42 must not run with it)" % (gate, marker))
        auth = Path(self.roster.get("control", {}).get("authority") or (ctl_dir / "CATALOG-AUTHORITY.json"))
        if not auth.exists():
            return problems + ["CATALOG-AUTHORITY.json not found at %s" % auth]
        try:
            pins = json.loads(auth.read_text(encoding="utf-8"))
        except ValueError as exc:
            return problems + ["CATALOG-AUTHORITY.json unreadable: %s" % exc]
        checks = [("review_validator_sha256", gate), ("scheduler_sha256", Path(self.control.script))]
        cat = Path(self.control.catalog_path)
        if cat.resolve().parent == ctl_dir.resolve():
            checks.append(("catalog_sha256", cat))      # a throwaway catalog elsewhere is not the pinned one
        for key, path in checks:
            pinned = pins.get(key)
            if not pinned:
                continue
            actual = sha256_file(path) if path.exists() else None
            if actual != pinned:
                problems.append("%s: %s is %s, authority pins %s" % (key, path.name, (actual or "missing")[:12],
                                                                    pinned[:12]))
        return problems

    def _reload_roster_if_changed(self):
        try:
            m = self.roster_path.stat().st_mtime
        except OSError:
            return
        if m == self._roster_mtime:
            return
        try:
            data = normalize_roster(json.loads(self.roster_path.read_text(encoding="utf-8")))
        except ValueError:
            return
        self._roster_mtime = m
        with self._lock:
            self._apply_roster(data)
        snap = snapshot_roster(self.run_root, self.roster_path.read_bytes())
        self.log("roster reloaded (mtime changed) -> %s" % (snap.name if snap else "no snapshot"))

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
                ok = self.probe(name, srv["url"] + "/session", "unpark")
                if ok:
                    self.log("UNPARK %s" % name)
                    srv["park_reason"] = srv["park_status"] = None
                else:
                    srv["parked_until"] = time.monotonic() + 300
        for name, st in self.runner_state.items():
            if st["park_reason"] and not self._parked(st):
                self.log("UNPARK runner %s" % name)
                st["park_reason"] = st["park_status"] = None

    def probe(self, server, url, why="probe"):
        """F14: every liveness probe leaves a record under probes/ — a JSON line
        per probe in probes/probes.jsonl plus the latest verdict per server in
        probes/<server>.json — so an empty probes/ can no longer hide a dead box."""
        t0 = time.monotonic()
        self._probe_detail = ""
        if self.fake_runners:
            ok, self._probe_detail = True, "fake runners: network probe skipped"
        else:
            ok = self._http_ok(url)
        rec = {"ts": utc_ms(), "server": server, "url": url, "why": why, "ok": ok,
               "detail": self._probe_detail, "ms": int((time.monotonic() - t0) * 1000),
               "tick": self.tick_count}
        pdir = self.run_root / "probes"
        try:
            pdir.mkdir(parents=True, exist_ok=True)
            _append_jsonl(pdir / "probes.jsonl", rec)
            (pdir / ("%s.json" % server)).write_text(json.dumps(rec, indent=2, sort_keys=True),
                                                     encoding="utf-8")
        except OSError:
            pass
        return ok

    def _http_ok(self, url, timeout=5.0):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                self._probe_detail = "HTTP %s" % resp.status
                return resp.status < 500
        except Exception as exc:  # noqa: BLE001 -- any failure is "not ok" with its reason
            self._probe_detail = "%s: %s" % (type(exc).__name__, str(exc)[:200])
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
        if wt.exists():
            why = self._worktree_stale(wt, base)
            if why:
                self._retire_stale_worktree(wt, task, why)
        if not wt.exists():
            rc, _o, _e = self.git(["-C", str(self.trunk), "cat-file", "-e", base + "^{commit}"])
            if rc != 0:
                raise ControlError("%s base %s is not a commit in trunk" % (task, base[:12]))
            wt.parent.mkdir(parents=True, exist_ok=True)
            self.git(["-C", str(self.trunk), "worktree", "prune"])
            branch = "vp/%s" % task
            self._retire_stale_branch(branch, base, task)
            rc, out, err = self.git(["-C", str(self.trunk), "worktree", "add", str(wt),
                                     "-b", branch, base], log=True)
            if rc != 0 and not wt.exists():
                # the branch exists and descends from base: an earlier candidate
                # of this task is resumed on it
                rc, out, err = self.git(["-C", str(self.trunk), "worktree", "add", str(wt), branch], log=True)
                if rc != 0:
                    raise ControlError("worktree add failed for %s: %s"
                                       % (task, (err or out).strip()[:300]))
        self.link_deps(wt, task)
        return wt

    def _worktree_stale(self, wt, base):
        """D15: a worktree is the task's only when it belongs to THIS trunk and
        its base is an ancestor of HEAD.  TRUNK-04 was built on a leftover
        worktree of the pre-v13 repo (branch tip 1f9e1236, 195-file diff
        against the v13 base): ensure_worktree reused any existing path."""
        rc, common, _ = self.git(["-C", str(wt), "rev-parse", "--git-common-dir"])
        if rc != 0:
            return "not a git worktree"
        try:
            mine = (Path(common.strip()) if Path(common.strip()).is_absolute()
                    else (wt / common.strip())).resolve()
            trunk_git = (self.trunk / ".git").resolve()
            rc2, tcommon, _ = self.git(["-C", str(self.trunk), "rev-parse", "--git-common-dir"])
            if rc2 == 0:
                tc = Path(tcommon.strip())
                trunk_git = (tc if tc.is_absolute() else (self.trunk / tc)).resolve()
        except OSError:
            return "git dir unresolvable"
        if mine != trunk_git:
            return "belongs to %s, not the trunk" % mine
        if base:
            rc, _o, _e = self.git(["-C", str(wt), "merge-base", "--is-ancestor", base, "HEAD"])
            if rc != 0:
                return "base %s is not an ancestor of its HEAD" % base[:12]
        return None

    def _retire_stale_worktree(self, wt, task, why):
        stamp = utc_ms().replace(":", "").replace("-", "")[:15]
        dst = wt.with_name("%s.stale-%s" % (wt.name, stamp))
        try:
            os.rename(str(wt), str(dst))
        except OSError as exc:
            raise ControlError("stale worktree %s could not be moved aside (%s): %s" % (wt, why, exc))
        self.git(["-C", str(self.trunk), "worktree", "prune"])
        _append_jsonl(self.git_jsonl, {"ts": utc_ms(), "op": "stale_worktree", "task": task, "from": str(wt),
                                       "to": str(dst), "why": why}, self._lock)
        self.alert("WORKTREE_STALE", "%s: worktree %s (%s) moved to %s; a fresh one is created on the "
                   "task's base" % (task, wt, why, dst.name), task)
        self.log("WORKTREE %s stale (%s) -> %s" % (task, why, dst.name))

    def _retire_stale_branch(self, branch, base, task):
        """a same-named vp/<task> branch whose tip does not descend from base
        (a pre-v13 candidate) is renamed vp-stale/<task>-<ts>, never deleted."""
        rc, _o, _e = self.git(["-C", str(self.trunk), "rev-parse", "--verify", "-q", "refs/heads/%s" % branch])
        if rc != 0:
            return False
        rc, _o, _e = self.git(["-C", str(self.trunk), "merge-base", "--is-ancestor", base, branch])
        if rc == 0:
            return False
        stamp = utc_ms().replace(":", "").replace("-", "")[:15]
        old = "vp-stale/%s-%s" % (task, stamp)
        rc, out, err = self.git(["-C", str(self.trunk), "branch", "-m", branch, old], log=True)
        if rc != 0:
            raise ControlError("stale branch %s could not be renamed: %s" % (branch, (err or out)[:200]))
        self.log("WORKTREE %s stale branch %s -> %s (tip does not descend from %s)" % (task, branch, old, base[:12]))
        return True

    WORKTREE_LINKS = ("platform/.venv", "agent/.venv", "portal/node_modules")

    def link_deps(self, wt, task=None):
        """§4(a): every worktree gets the trunk's platform/.venv, agent/.venv and
        portal/node_modules as symlinks (L04-FLOOR, docs/TEST_GATES.md).  A
        missing target is alerted once per run, never silently skipped."""
        made, missing = [], []
        for rel in self.WORKTREE_LINKS:
            link, target = wt / rel, self.trunk / rel
            if not target.exists():
                missing.append(rel)
                continue
            try:
                link.parent.mkdir(parents=True, exist_ok=True)
                if not link.exists() and not link.is_symlink():
                    os.symlink(str(target), str(link))
                    made.append(rel)
            except OSError as exc:
                missing.append("%s (%s)" % (rel, exc))
        _append_jsonl(self.git_jsonl, {"ts": utc_ms(), "op": "link_deps", "task": task, "worktree": str(wt),
                                       "linked": made, "missing": missing}, self._lock)
        if missing:
            self.alert_once("deps-missing", "WORKTREE_DEPS_MISSING",
                            "trunk lacks %s; worktrees run without them (box proofs will fail for that "
                            "surface)" % ", ".join(missing), task)
        return made, missing

    # -- §4(c) the packet layer -----------------------------------------------------------

    def _load_pack(self):
        if not self.pack_dir or not self.pack_dir.is_dir():
            return
        try:
            self.pack, self.pack_lint = vppack.load_pack(self.pack_dir)
        except Exception as exc:  # noqa: BLE001 -- a broken pack is reported, not fatal
            self.pack, self.pack_lint = {}, ["ERROR pack: %s: %s" % (type(exc).__name__, exc)]
        for line in self.pack_lint:
            self.log("PACK %s" % line)

    def packet_for(self, task):
        pid = self.pack_by_task.get(task)
        return self.pack.get(pid) if pid else None

    def _owner_gate_open(self, task):
        p = self.packet_for(task)
        if not p or vppack.owner_gate_open(p, self.roster):
            return True
        self.alert_once("owner-gate:%s" % p["owner_gate"], "OWNER_GATE",
                        "%s waits for %s (roster owner_gates.%s is not true); nothing dispatched behind it"
                        % (p["id"], p["owner_gate"], p["owner_gate"]), task)
        return False

    # -- D16: a verified retry/fix retires the rows it supersedes ----------------------------
    SUPERSEDE_RE = re.compile(r"^(?P<root>.+?)-(?:R(?P<r>\d+)|FIX-(?P<f>\d+))$")
    RETIRABLE = ("PLANNED", "WAITING_DEPENDENCY", "READY", "REPAIR_REQUIRED", "BLOCKED", "INVALID_EVIDENCE")

    @classmethod
    def superseded_by(cls, task, tasks):
        """rows that a VERIFIED `task` = <ID>-R<n> / <ID>-FIX-<n> supersedes:
        <ID> itself, every <ID>-R<m> with m < n (for -FIX-<n>: every -R<m> and
        every -FIX-<m> with m < n) that exists and sits in a retirable state.
        RUNNING/CLAIMED rows are left alone (F8), accepted ones are not rows to
        retire."""
        m = cls.SUPERSEDE_RE.match(task)
        if not m:
            return []
        root = m.group("root")
        n = int(m.group("r") or m.group("f"))
        is_fix = m.group("f") is not None
        out = []
        for t, row in tasks.items():
            if t == task or not t.startswith(root):
                continue
            if t == root:
                pass
            else:
                mm = cls.SUPERSEDE_RE.match(t)
                if not mm or mm.group("root") != root:
                    continue
                k = int(mm.group("r") or mm.group("f"))
                if mm.group("f") is not None:
                    if not is_fix or k >= n:
                        continue
                elif not is_fix and k >= n:
                    continue
            if (row or {}).get("state") in cls.RETIRABLE:
                out.append(t)
        return sorted(out)

    def _supersede(self, task, tasks=None):
        """retire what `task` supersedes via the scheduler's `retire` (closer =
        task, reason SUPERSEDED_BY:<task>).  Returns the retired ids."""
        tasks = tasks if tasks is not None else ((self.control.state_view() or {}).get("tasks") or {})
        done = []
        for target in self.superseded_by(task, tasks):
            try:
                self.control.call("retire", ["--task", target, "--closer", task, "--reason",
                                             "SUPERSEDED_BY:%s" % task])
                done.append(target)
            except ControlError as exc:
                self.alert_once("supersede:%s:%s" % (task, target), "SUPERSEDE_REFUSED",
                                "%s: retire of superseded %s refused: %s" % (task, target, str(exc)[:200]), task)
        if done:
            self.log("SUPERSEDED by %s: %s retired" % (task, ",".join(done)))
            _append_jsonl(self.run_root / "packets" / "closures.jsonl",
                          {"ts": utc_ms(), "op": "supersede", "task": task, "retired": done}, self._lock)
        return done

    def _supersede_sweep(self, state):
        """idempotent: every accepted <ID>-R<n>/<ID>-FIX-<n> row retires the
        rows it supersedes (covers verdicts reached before D16 existed, e.g.
        WA-04-DP12R-DP17R-DP18R whose -R2 was VERIFIED)."""
        tasks = (state or {}).get("tasks") or {}
        retired = []
        for t, row in sorted(tasks.items()):
            if (row or {}).get("state") in vppack.ACCEPTED and self.SUPERSEDE_RE.match(t) \
                    and self.superseded_by(t, tasks):
                retired += self._supersede(t, tasks)
        return retired

    def _pack_step(self):
        every = float(self.alerts_cfg.get("pack_every_s", 300))
        last = self._pack_last_mono
        if last is not None and time.monotonic() - last < every:
            return
        self._pack_last_mono = time.monotonic()
        if self.pack:
            try:
                self._pack_reconcile()
            except Exception as exc:  # noqa: BLE001 -- never take the tick down
                self.log("PACK reconcile failed: %s: %s" % (type(exc).__name__, exc))
        try:
            self._supersede_sweep(self.control.state_view())
        except Exception as exc:  # noqa: BLE001
            self.log("SUPERSEDE sweep failed: %s: %s" % (type(exc).__name__, exc))

    def _pack_record(self, packet, rec):
        d = self.run_root / "packets"
        d.mkdir(parents=True, exist_ok=True)
        rec = dict(rec, ts=utc_ms())
        (d / ("%s.json" % packet["id"])).write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
        _append_jsonl(d / "bindings.jsonl", rec, self._lock)
        return rec

    def _pack_reconcile(self):
        """bind every packet to its scheduler task (vppack D9): direct packets
        map onto existing rows; dynamic ones are instantiated once their
        dependency packets are bound; reviews wait for a registered candidate;
        owner-gated packets wait for the roster flag.  L42 (roster
        packet.rewire, default ["L42"]) gets its depends_on rewired to the
        pack's set.  Every binding is a packets/<id>.json record."""
        state = self.control.state_view()
        tasks = state.get("tasks") or {}
        candidate = state.get("candidate") or {}
        bound_now = 0
        self._pack_restore(tasks)
        bindings = {pid: t for t, pid in self.pack_by_task.items()}
        for pid in vppack.topo_order(self.pack):
            p = self.pack[pid]
            retry = self._pack_retry_requested(pid)
            if retry is not None:
                if self._pack_retry(p, tasks, candidate, bindings, retry):
                    bound_now += 1
                continue
            if pid in bindings and bindings[pid] in tasks:
                continue                          # bound once, bound for the run
            mode = vppack.bind(p, tasks)
            if mode[0] == "hold":
                if ("hold", pid) not in self._pack_logged:
                    self._pack_logged.add(("hold", pid))
                    self.log("PACK %s held: %s" % (pid, mode[1]))
                continue
            task = mode[1]
            if task in tasks:
                if self.pack_by_task.get(task) != pid:
                    self.pack_by_task[task] = pid
                    self._pack_record(p, vppack.dispatch_record(p, task, mode[0], mode[2] if len(mode) > 2 else None,
                                                                tasks[task].get("parent_contract_id"), p["base_sha"],
                                                                {"state": tasks[task].get("state")}))
                continue
            # dynamic and not yet instantiated
            if self._pack_instantiate(p, task, mode[2], tasks, candidate, bindings):
                bound_now += 1
        for tid in (self.roster.get("packet") or {}).get("rewire", ["L42"]):
            self._pack_rewire(tid, tasks, bindings)
        if bound_now:
            self.log("PACK bound %d new task(s)" % bound_now)
        return bound_now

    def _pack_instantiate(self, p, task, template, tasks, candidate, bindings, extra=None):
        """instantiate packet `p` as dynamic scheduler task `task`:
        True bound, False still waiting (gate/deps/candidate), None refused"""
        pid = p["id"]
        if not vppack.owner_gate_open(p, self.roster):
            self._owner_gate_open(task) if task in self.pack_by_task else self.alert_once(
                "owner-gate:%s" % p["owner_gate"], "OWNER_GATE",
                "%s waits for %s (roster owner_gates.%s is not true)" % (pid, p["owner_gate"], p["owner_gate"]))
            return False
        deps = vppack.dependency_tasks(p, self.pack, tasks, bindings)
        if deps is None:
            return False                          # a dependency packet is not bound yet
        parent, how = vppack.parent_for(p, tasks, self.pack)
        if not parent:
            self.alert_once("pack-parent:%s" % pid, "PACKET_NO_PARENT",
                            "%s: no parent_contract derivable; add `parent_contract:` to its header" % pid)
            return False
        covered = self._covered_rows(tasks) if template in vppack.REVIEW_TEMPLATES else None
        params = vppack.parameters_for(p, template, parent, candidate, covered)
        if params is None:
            if ("cand", pid) not in self._pack_logged:
                self._pack_logged.add(("cand", pid))
                self.log("PACK %s waits for a registered candidate" % pid)
            return False
        pdir = self.run_root / "packets"
        pdir.mkdir(parents=True, exist_ok=True)
        ppath = pdir / ("%s.params.json" % (pid if task in (pid, vppack.dynamic_id(p)) else task))
        ppath.write_text(json.dumps(params, indent=2, sort_keys=True), encoding="utf-8")
        try:
            self.control.instantiate(template, task, parent, "B", ppath, deps)
        except ControlError as exc:
            self.alert_once("pack-inst:%s" % task, "PACKET_INSTANTIATE_REFUSED",
                            "%s as %s (%s, parent %s): %s" % (pid, task, template, parent, str(exc)[:240]))
            return None
        tasks[task] = {"state": "PLANNED", "parent_contract_id": parent, "dynamic": True}
        self.pack_by_task[task] = pid
        bindings[pid] = task
        rec = {"parent_how": how, "depends_on_tasks": deps, "parameters": str(ppath), "covered_rows": covered}
        rec.update(extra or {})
        self._pack_record(p, vppack.dispatch_record(p, task, "dynamic", template, parent, p["base_sha"], rec))
        if how.startswith(vppack.GUESSED_PARENT):
            self.alert_once("pack-guess:%s" % pid, "PACKET_PARENT_GUESSED",
                            "%s parent %s derived by %s; pin `parent_contract:` in its header if wrong"
                            % (pid, parent, how))
        self.log("PACK %s -> %s (%s, parent %s via %s, deps %s)" % (pid, task, template, parent, how, deps))
        return True

    # -- retry of a wrongly-closed dynamic packet task ---------------------------------
    RETRY_MARKER = "%s.retry.json"

    def _pack_restore(self, tasks):
        """seed pack_by_task from packets/<id>.json so a binding (incl. a retry
        task) survives a driver restart; the current file wins over bind()"""
        pdir = self.run_root / "packets"
        if not pdir.is_dir():
            return
        for pid in self.pack:
            if pid in self.pack_by_task.values():
                continue
            f = pdir / ("%s.json" % pid)
            if not f.exists():
                continue
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            t = rec.get("task")
            if t in tasks and t not in self.pack_by_task:
                self.pack_by_task[t] = pid

    def _pack_retry_requested(self, pid):
        f = self.run_root / "packets" / (self.RETRY_MARKER % pid)
        if not f.exists():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def request_packet_retry(self, pid, reason):
        """CLI `retry-packet`: ask the loop to re-instantiate a packet whose
        bound dynamic task ended without a real verdict (e.g. RUNNER_CRASH ->
        INVALID_EVIDENCE).  Refused for unknown packets and for tasks that are
        still running or accepted; the loop consumes the marker."""
        p = self.pack.get(pid) if self.pack else None
        if not p:
            raise ValueError("unknown packet %s" % pid)
        tasks = (self.control.state_view().get("tasks") or {})
        self._pack_restore(tasks)
        cur = {q: t for t, q in self.pack_by_task.items()}.get(pid) or vppack.bound_task(p, tasks)
        row = tasks.get(cur) if cur else None
        if not row:
            raise ValueError("%s has no bound task to retry" % pid)
        if row.get("state") in vppack.UNFINISHED or row.get("state") in vppack.ACCEPTED:
            raise ValueError("%s is bound to %s which is %s; only a finished, unaccepted task can be retried"
                             % (pid, cur, row.get("state")))
        pdir = self.run_root / "packets"
        pdir.mkdir(parents=True, exist_ok=True)
        rec = {"packet": pid, "previous_task": cur, "previous_state": row.get("state"),
               "reason": reason, "requested_at": utc_ms()}
        (pdir / (self.RETRY_MARKER % pid)).write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
        _append_jsonl(pdir / "bindings.jsonl", dict(rec, op="retry-requested"), self._lock)
        self.comms("lanedriver", "audit", "retry requested for %s (was %s %s): %s"
                   % (pid, cur, row.get("state"), reason), kind="lifecycle", task=cur)
        return rec

    def _pack_retry(self, p, tasks, candidate, bindings, req):
        """consume packets/<id>.retry.json: instantiate <id>-R<n> from the same
        template/parent/params and rebind the packet to it.  The scheduler's
        own duplicate-review rule decides (it admits a retry only when the
        previous review is INVALID_EVIDENCE/CANCELLED)."""
        pid = p["id"]
        marker = self.run_root / "packets" / (self.RETRY_MARKER % pid)
        prev = bindings.get(pid) or req.get("previous_task")
        row = tasks.get(prev) or {}
        if prev and (row.get("state") in vppack.UNFINISHED or row.get("state") in vppack.ACCEPTED):
            self.alert("PACKET_RETRY_REFUSED",
                       "%s: bound task %s is %s; retry marker ignored" % (pid, prev, row.get("state")), prev)
            marker.unlink(missing_ok=True)
            return False
        mode = vppack.bind(p, tasks)
        template = mode[2] if mode[0] == "dynamic" else vppack.template_for(p)
        if not template:
            self.alert("PACKET_RETRY_REFUSED", "%s: no template to retry from" % pid, prev)
            marker.unlink(missing_ok=True)
            return False
        base = vppack.dynamic_id(p)
        n = 1
        while ("%s-R%d" % (base, n)) in tasks:
            n += 1
        task = "%s-R%d" % (base, n)
        if prev in self.pack_by_task:
            del self.pack_by_task[prev]
        bindings.pop(pid, None)
        ok = self._pack_instantiate(p, task, template, tasks, candidate, bindings,
                                    {"retry_of": prev, "retry_reason": req.get("reason")})
        if not ok:
            if prev:
                self.pack_by_task[prev] = pid     # keep the old binding until it works
                bindings[pid] = prev
            if ok is None:                        # scheduler refused: do not loop on it
                marker.unlink(missing_ok=True)
                _append_jsonl(self.run_root / "packets" / "bindings.jsonl",
                              {"ts": utc_ms(), "packet": pid, "op": "retry-refused", "task": task}, self._lock)
            return False                          # else marker stays: retried next reconcile
        marker.unlink(missing_ok=True)
        self.alert("PACKET_RETRIED", "%s re-instantiated as %s (was %s %s): %s"
                   % (pid, task, prev, row.get("state"), req.get("reason")), task)
        return True

    def _pack_rewire(self, tid, tasks, bindings=None):
        p = self.pack.get(tid)
        row = tasks.get(tid)
        if not p or not row or row.get("state") not in ("PLANNED", "WAITING_DEPENDENCY", "READY"):
            return
        deps = vppack.dependency_tasks(p, self.pack, tasks, bindings)
        if deps is None or list(row.get("depends_on") or []) == deps:
            return
        try:
            self.control.call("rewire", ["--task", tid, "--depends-on"] + deps +
                              ["--reason", "v13 pack %s depends_on (PACK-COMPLETE §4.3)" % tid])
        except ControlError as exc:
            self.alert_once("rewire:%s" % tid, "REWIRE_REFUSED", "%s: %s" % (tid, str(exc)[:240]), tid)
            return
        row["depends_on"] = deps
        self.log("PACK rewired %s.depends_on %s -> %s" % (tid, row.get("depends_on"), deps))
        _append_jsonl(self.run_root / "packets" / "bindings.jsonl",
                      {"ts": utc_ms(), "packet": tid, "op": "rewire", "depends_on": deps}, self._lock)

    def _covered_rows(self, tasks):
        """the implementation rows a union review covers: every INTEGRATED
        non-review task without a current junior review (L42's REVIEW:* waits)"""
        l42 = tasks.get("L42") or {}
        waits = [w.split(":")[1] for w in (l42.get("waiting_for") or []) if str(w).startswith("REVIEW:")]
        if waits:
            return sorted(waits)
        return sorted(t for t, r in tasks.items() if r.get("state") == "INTEGRATED"
                      and (r.get("kind") or "") not in REVIEW_KINDS and t not in ("L35", "L42", "L44"))

    def _pack_closure(self, task, harvest, wt):
        """§6b after VERIFIED: retire the closes targets whose last closer this
        is; promote the packet's own scheduler_task; leave the rest as
        closes_pending in RESULT.json + harvest."""
        p = self.packet_for(task)
        if not p or not p["closes"]:
            return None
        tasks = (self.control.state_view() or {}).get("tasks") or {}
        plan = vppack.closure_plan(p, self.pack, tasks, {pid: t for t, pid in self.pack_by_task.items()})
        done = {"retired": [], "promoted": [], "pending": plan["pending"], "missing": plan["missing"], "refused": {}}
        ev = [e for e in (harvest.get("evidence") or []) if Path(e).exists()][:1]
        for target in plan["retire"]:
            try:
                self.control.call("retire", ["--task", target, "--closer", task, "--reason",
                                             "closed by packet %s" % p["id"]] + sum((["--evidence", e] for e in ev), []))
                done["retired"].append(target)
            except ControlError as exc:
                done["refused"][target] = str(exc)[:240]
        for target in plan["promote"]:
            try:
                self.control.promote(target, harvest.get("output_sha"), harvest.get("tree_sha"), ev, supporting=[task])
                done["promoted"].append(target)
            except ControlError as exc:
                done["refused"][target] = str(exc)[:240]
        if plan["pending"]:
            self._annotate_result(wt, {"closes_pending": plan["pending"]})
        if done["refused"]:
            self.alert("CLOSURE_REFUSED", "%s: %s" % (p["id"], json.dumps(done["refused"])[:300]), task)
        self.log("PACK closure %s: %s" % (p["id"], json.dumps({k: v for k, v in done.items() if v})))
        _append_jsonl(self.run_root / "packets" / "closures.jsonl",
                      dict(done, ts=utc_ms(), packet=p["id"], task=task), self._lock)
        return done

    def _annotate_result(self, wt, extra):
        rp = Path(wt) / ".vp" / "RESULT.json"
        try:
            doc = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {}
            doc.update(extra)
            rp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def _pack_record_review(self, task, verdict_path, role):
        """union review: `record-review --task X --role <role>` for every covered
        row named in the review task's parameters (REVIEW-* packets)"""
        state = self.control.state_view() or {}
        row = (state.get("tasks") or {}).get(task) or {}
        covered = (row.get("parameters") or {}).get("covered_rows") or []
        out = {"recorded": [], "refused": {}}
        for x in covered:
            try:
                self.control.call("record-review", ["--task", x, "--role", role, "--verdict", str(verdict_path)])
                out["recorded"].append(x)
            except ControlError as exc:
                out["refused"][x] = str(exc)[:200]
        if covered:
            self.log("PACK record-review %s role=%s: %d recorded, %d refused"
                     % (task, role, len(out["recorded"]), len(out["refused"])))
            _append_jsonl(self.run_root / "packets" / "reviews.jsonl",
                          dict(out, ts=utc_ms(), task=task, role=role, verdict=str(verdict_path)), self._lock)
        return out

    def write_dispatch_record(self, wt, task, attempt, contract, base, row):
        """<worktree>/.vp/DISPATCH.json -- the driver's dispatch record for the
        turn (PACKET-FORMAT: REVIEW-* packets read their union id from it and
        the `<union>` placeholder in owned_files is substituted here)."""
        p = self.packet_for(task)
        union = "union-%s" % attempt
        rec = {"task": task, "attempt": attempt, "union": union, "base_sha": base, "kind": contract.get("kind"),
               "candidate_sha": ((row.get("parameters") or {}).get("candidate_sha")),
               "tree_sha": ((row.get("parameters") or {}).get("tree_sha")),
               "covered_rows": ((row.get("parameters") or {}).get("covered_rows")),
               "parent_contract_id": row.get("parent_contract_id"), "template": row.get("template_id"),
               "review_task_id": task if (contract.get("kind") in REVIEW_KINDS) else None, "ts": utc_ms()}
        if p:
            rec.update({"packet": p["id"], "v13_kind": p["v13_kind"], "runner_role": p["runner_role"],
                        "closes": list(p["closes"]), "proof_kind": p["proof_kind"], "owner_gate": p["owner_gate"],
                        "hosted_owed": p["hosted_owed"], "owned_files": vppack.substitute_union(p, union),
                        "packet_dir": p["dir"]})
        try:
            (Path(wt) / ".vp" / vppack.DISPATCH_RECORD).write_text(json.dumps(rec, indent=2, sort_keys=True),
                                                                   encoding="utf-8")
        except OSError:
            pass
        return rec

    def _pack_paths(self, task):
        if not self.pack_dir:
            return None, None
        pid = self.pack_by_task.get(task, task)
        d = self.pack_dir / pid
        p, b = d / "PACKET.md", d / "BENCHMARK.md"
        return (p if p.exists() and p.stat().st_size else None,
                b if b.exists() and b.stat().st_size else None)

    def _proof_hints(self, task, row):
        """(test_paths, proof_kind) for a row without a packet of its own (a
        driver-instantiated REPAIR): the parent packet's, else the test files
        the defect names, else nothing.  Without this a repair proofs with an
        empty path list, which run_box turns into the FULL suite."""
        row = row or {}
        pid = self.pack_by_task.get(task) or self.pack_by_task.get(row.get("parent_contract_id") or "")
        p = self.pack.get(pid) if pid else None
        if p is None:
            parent = row.get("parent_contract_id") or ""
            for cand in (parent, self._pack_root(parent)):
                if cand in self.pack:
                    p = self.pack[cand]
                    break
        if p is not None and (p.get("test_paths") or p.get("proof_kind")):
            return list(p.get("test_paths") or []), p.get("proof_kind")
        files = []
        for f in (row.get("parameters") or {}).get("fails") or []:
            for text in (str(f.get("id") or ""), str(f.get("evidence") or "")):
                node = text.split("::")[0].strip()
                if node.endswith(".py") and "/tests/" in node and node not in files:
                    files.append(node)
        return files, None

    @staticmethod
    def _pack_root(task):
        """L17-REPLY-WIRING-R2 / R-X-V13-R1 / L02-V13 -> the packet id they descend from"""
        t = str(task or "")
        while True:
            m = re.match(r"^(.*)-R\d+$", t)
            if m:
                t = m.group(1)
                continue
            if t.endswith("-V13"):
                t = t[:-4]
                continue
            return t

    @staticmethod
    def render_packet(contract, base, row=None, test_paths=None, proof_kind=None):
        owned = contract.get("owned_paths") or []
        lines = ["---", "item: %s" % contract["id"], "title: %s" % contract.get("title", contract["id"]),
                 "kind: %s" % contract.get("kind"), "base_sha: %s" % base,
                 "depends_on: [%s]" % ", ".join(contract.get("depends_on") or []),
                 "owned_files:"] + ["  - %s" % p for p in owned] + \
                (["test_paths:"] + ["  - %s" % p for p in test_paths] if test_paths else []) + \
                (["proof_kind: %s" % proof_kind] if proof_kind else []) + \
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
        paths, pkind = (None, None) if pp else self._proof_hints(task, row)
        (vp / "PACKET.md").write_text(pp.read_text(encoding="utf-8") if pp
                                      else self.render_packet(contract, base, row, test_paths=paths,
                                                              proof_kind=pkind), encoding="utf-8")
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
                   "idle_since": self._idle_since, "spawned_total": self.spawned_total,
                   "code_version": self.code_version(), "reloads": self._reload_count,
                   "reload_pending": self._reload_pending is not None,
                   "reload_failed": bool(self._reload_failed)}
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

    SEAL_SKIP = ("driver.heartbeat", "driver.heartbeat.tmp", "STOP", "DRAIN", "RELOAD")

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

    # -- F14 render on a timer ----------------------------------------------------------

    def render(self):
        """Write LEDGER.md from the scheduler's state view + the driver's own
        books.  Called every alerts.render_every_s (default 300) and at finish,
        so the ledger can never silently freeze while the run moves."""
        try:
            state = self.control.state_view()
        except Exception as exc:  # noqa: BLE001 -- render never takes the tick down
            self.log("render: state unreadable: %s" % exc)
            return None
        usd, tokens = self.spent()
        with self._lock:
            live = dict(self._live)
            parked = {n: s["park_status"] for n, s in list(self.servers.items()) +
                      list(self.runner_state.items()) if s.get("park_reason")}
        tasks = state.get("tasks", {})
        counts = {}
        for row in tasks.values():
            counts[row.get("state")] = counts.get(row.get("state"), 0) + 1
        lines = ["# LEDGER", "",
                 "run: %s  phase: %s  sequence: %s" % (state.get("run_id", "-"), state.get("phase", "-"),
                                                        state.get("sequence", "-")),
                 "generated: %s (tick %d, pid %d)" % (utc_ms(), self.tick_count, os.getpid()),
                 "spend: $%.2f  tokens: %s" % (usd, json.dumps(tokens, sort_keys=True)),
                 "live: %d  parked: %s" % (len(live), json.dumps(parked, sort_keys=True) if parked else "none"),
                 "states: " + ", ".join("%s=%d" % kv for kv in sorted(counts.items())), "",
                 "| task | state | kind | attempt | unlocks | updated | note |",
                 "| --- | --- | --- | --- | --- | --- | --- |"]
        for tid in sorted(tasks):
            row = tasks[tid]
            note = "LIVE" if tid in live else (row.get("blocker") or {}).get("reason", "") if isinstance(
                row.get("blocker"), dict) else ""
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                tid, row.get("state", "-"), row.get("kind", "-"), row.get("attempt_id") or "-",
                "yes" if row.get("unlocks_dependents") else "-", row.get("updated_at", "-"),
                str(note)[:80]))
        lines += ["", "## Alerts (last 20)", ""]
        alerts = []
        try:
            if self.alerts_jsonl.exists():
                alerts = [json.loads(l) for l in self.alerts_jsonl.read_text(encoding="utf-8").splitlines()
                          if l.strip()][-20:]
        except (OSError, ValueError):
            pass
        lines += ["- %s **%s** %s" % (a.get("ts"), a.get("kind"), str(a.get("text", ""))[:160])
                  for a in alerts] or ["none"]
        out = self.run_root / "LEDGER.md"
        try:
            tmp = out.with_suffix(".md.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(str(tmp), str(out))
        except OSError:
            return None
        self._last_render_mono = time.monotonic()
        return out

    def _render_step(self):
        every = float(self.alerts_cfg.get("render_every_s", 300))
        last = getattr(self, "_last_render_mono", None)
        if last is None or time.monotonic() - last >= every:
            self.render()

    # -- F16 activation record ----------------------------------------------------------

    PROFILES = ("boss", "final", "integrator", "junior", "senior")

    def write_activation_record(self):
        """activation-record.json: sha256 of all five Claude profiles under
        RUN_ROOT/claude-settings (seeded from dispatcher/vp/claude-settings when
        missing), the roster, the catalog and the scheduler.  Rewritten on every
        start; a profile whose hash moved since the previous record raises a
        PROFILE_CHANGED alert so an edit after activation is never silent."""
        sdir = self.run_root / "claude-settings"
        src = self.here / "claude-settings"
        try:
            sdir.mkdir(parents=True, exist_ok=True)
            for name in self.PROFILES:
                if not (sdir / ("%s.json" % name)).exists() and (src / ("%s.json" % name)).exists():
                    shutil.copy(str(src / ("%s.json" % name)), str(sdir / ("%s.json" % name)))
        except OSError:
            pass
        profiles = {}
        for name in self.PROFILES:
            p = sdir / ("%s.json" % name)
            profiles[name] = sha256_file(p) if p.exists() else None
        ctl = self.roster.get("control", {})
        gate = (Path(ctl.get("cwd") or Path(ctl.get("script", "")).parent) / "review_gate.py") if ctl.get("script") else None
        inputs = {"roster": sha256_file(self.roster_path),
                  "catalog": sha256_file(Path(ctl["catalog"])) if ctl.get("catalog") else None,
                  "scheduler": sha256_file(Path(ctl["script"])) if ctl.get("script") else None,
                  "review_gate": sha256_file(gate) if gate and gate.exists() else None}
        out = self.run_root / "activation-record.json"
        previous = None
        try:
            if out.exists():
                previous = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
        changed = []
        if previous:
            for name, digest in profiles.items():
                if (previous.get("profiles") or {}).get(name) != digest:
                    changed.append("profile:%s" % name)
            for name, digest in inputs.items():
                if (previous.get("inputs") or {}).get(name) != digest:
                    changed.append(name)
        rec = {"ts": utc_ms(), "pid": os.getpid(), "run_root": str(self.run_root),
               "profiles": profiles, "inputs": inputs, "changed_since_previous": changed,
               "previous_ts": previous.get("ts") if previous else None,
               "missing_profiles": [n for n, d in profiles.items() if d is None]}
        try:
            tmp = out.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(str(tmp), str(out))
            _append_jsonl(self.run_root / "activation-records.jsonl", rec)
        except OSError:
            pass
        if changed:
            self.alert("PROFILE_CHANGED", "since %s: %s" % (rec["previous_ts"], ", ".join(changed)))
        if rec["missing_profiles"]:
            self.alert_once("profiles-missing", "PROFILE_MISSING",
                            "claude-settings lacks %s" % ", ".join(rec["missing_profiles"]))
        return rec

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
        self.render()
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
        if self._reload_step():
            return 0                      # reloaded this tick: the new code takes the next one
        if self._reconciled and not self._stopping:
            self._pack_step()
        if self.tick_count == 1:
            self.write_activation_record()
            for name, srv in self.servers.items():
                if not srv.get("parked"):
                    self.probe(name, srv["url"] + "/session", "startup")
        self._seal_step()
        self._render_step()
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
        draining = phase != "ACTIVE" or self.drain_requested() or self._reload_pending is not None
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

    # -- D12 hot reload -------------------------------------------------------------------

    def reload_targets(self):
        """[(module_name, path)] of the code the running driver executes: the
        loaded helper modules (RELOAD_ORDER) and lanedriver.py itself, last."""
        out = []
        for name in getattr(self, "reload_modules", RELOAD_ORDER):
            mod = sys.modules.get(name)
            f = getattr(mod, "__file__", None)
            if f and Path(f).resolve().parent == self.here:
                out.append((name, Path(f).resolve()))
        for name in getattr(self, "reload_extra", []):
            mod = sys.modules.get(name)
            if mod is not None and getattr(mod, "__file__", None):
                out.append((name, Path(mod.__file__).resolve()))
        own = sys.modules.get(type(self).__module__)
        own_file = Path(getattr(own, "__file__", None) or __file__).resolve()
        out.append((type(self).__module__, own_file))
        return out

    def code_hashes(self):
        return {name: sha256_file(path) for name, path in self.reload_targets()}

    def code_version(self):
        h = hashlib.sha256()
        for name, digest in sorted(self._code_hashes.items()):
            h.update(("%s=%s\n" % (name, digest)).encode())
        return h.hexdigest()[:12]

    def reload_requested(self):
        return (self.run_root / RELOAD_FILE).exists()

    def _reload_reason(self):
        try:
            raw = (self.run_root / RELOAD_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if not raw:
            return ""
        try:
            doc = json.loads(raw)
            return str(doc.get("reason") or raw)[:300] if isinstance(doc, dict) else raw[:300]
        except ValueError:
            return raw[:300]

    def _reload_step(self):
        """RUN_ROOT/RELOAD -> quiesce (no new claims or adoptions) until active == 0,
        then hot_reload().  True when a reload happened this tick."""
        if self._stopping:
            return False
        if self._reload_pending is None:
            if not self.reload_requested():
                return False
            self._reload_pending = {"reason": self._reload_reason(), "requested_at": utc_ms()}
            self.log("RELOAD requested (%s): quiescing, no new claims until active == 0"
                     % (self._reload_pending["reason"] or "no reason given"))
            self.comms("lanedriver", "audit", "reload requested: %s" % self._reload_pending["reason"],
                       kind="lifecycle")
        self.join(timeout=0.0)
        self._threads = [t for t in self._threads if t.is_alive()]
        with self._lock:
            live = len(self._live)
        if live or self._threads:
            return False
        req = self._reload_pending
        self._reload_pending = None
        try:
            (self.run_root / RELOAD_FILE).unlink()
        except OSError:
            pass
        self.hot_reload(req.get("reason") or "")
        return True

    def hot_reload(self, reason=""):
        """Reload the driver's code in place.  Every file is compiled first (a
        syntax error changes nothing); helper modules are importlib.reload()ed
        in dependency order; lanedriver.py is loaded as a fresh module and every
        live object's class is rebound to it; roster-derived attributes are
        re-applied so new instance fields exist.  A failure leaves the old code
        running but stops new claims (RELOAD_FAILED) until the owner restarts
        or a later RELOAD succeeds."""
        before = dict(self._code_hashes)
        targets = self.reload_targets()
        after = {name: sha256_file(path) for name, path in targets}
        changed = sorted(n for n in after if before.get(n) != after[n])
        rec = {"ts": utc_ms(), "reason": reason, "pid": os.getpid(), "n": self._reload_count + 1,
               "before": before, "after": after, "changed": changed, "ok": False}
        old_mod = type(self).__module__
        try:
            for name, path in targets:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
                try:                              # never trust a stale .pyc (same mtime second + size)
                    os.unlink(importlib.util.cache_from_source(str(path)))
                except (OSError, ValueError):
                    pass
            importlib.invalidate_caches()
            for name, path in targets[:-1]:
                importlib.reload(sys.modules[name])
            own_path = targets[-1][1]
            new_name = "lanedriver_r%d" % (self._reload_count + 1)
            spec = importlib.util.spec_from_file_location(new_name, str(own_path))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[new_name] = mod
            spec.loader.exec_module(mod)
            reloaded = {name for name, _ in targets[:-1]}
            self._rebind(self, mod, old_mod, reloaded, seen=set())
            self._reload_count += 1
            self._code_hashes = after
            self._reload_failed = None
            self._apply_roster(self.roster)
            rec.update({"ok": True, "module": new_name})
            self.log("RELOAD ok #%d %s -> %s changed=%s (%s)"
                     % (self._reload_count, self._short(before), self._short(after), changed, reason))
            self.alert("RELOAD", "code reloaded in place (#%d): %s; version %s -> %s"
                       % (self._reload_count, ", ".join(changed) or "no file changed",
                          self._short(before), self._short(after)))
        except Exception as exc:  # noqa: BLE001 -- keep the old code, stop claiming
            rec.update({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                        "traceback": traceback.format_exc()[-2000:]})
            self._reload_failed = rec["error"]
            self.log("RELOAD FAILED: %s" % rec["error"])
            self.alert("RELOAD_FAILED", "code reload failed, old code keeps running but NO NEW CLAIMS "
                       "until a RELOAD succeeds or the owner restarts: %s" % rec["error"][:300])
        _append_jsonl(self.run_root / "reloads.jsonl", rec, self._lock)
        self.comms("lanedriver", "audit", "reload %s: %s" % ("ok" if rec["ok"] else "FAILED",
                                                            rec.get("error") or ", ".join(changed) or "no change"),
                   kind="lifecycle")
        try:
            self.write_activation_record()
        except Exception as exc:  # noqa: BLE001
            self.log("RELOAD activation record: %s" % exc)
        return rec

    @staticmethod
    def _short(hashes):
        h = hashlib.sha256()
        for name, digest in sorted(hashes.items()):
            h.update(("%s=%s\n" % (name, digest)).encode())
        return h.hexdigest()[:12]

    @classmethod
    def _rebind(cls, obj, mod, old_mod, reloaded, seen, depth=0):
        """point obj (and the objects it holds, three levels deep) at the
        reloaded classes: lanedriver classes come from `mod`, helper classes
        from their reloaded module"""
        if id(obj) in seen or depth > 3:
            return
        seen.add(id(obj))
        t = type(obj)
        if t.__module__ == old_mod or t.__module__.startswith("lanedriver"):
            new_cls = getattr(mod, t.__name__, None)
            if isinstance(new_cls, type):
                try:
                    obj.__class__ = new_cls
                except TypeError:
                    pass
        elif t.__module__ in reloaded:
            new_cls = getattr(sys.modules[t.__module__], t.__name__, None)
            if isinstance(new_cls, type):
                try:
                    obj.__class__ = new_cls
                except TypeError:
                    pass
        d = getattr(obj, "__dict__", None)
        if not isinstance(d, dict):
            return
        for v in list(d.values()):
            cls._rebind_value(v, mod, old_mod, reloaded, seen, depth + 1)

    @classmethod
    def _rebind_value(cls, v, mod, old_mod, reloaded, seen, depth):
        if isinstance(v, dict):
            for x in list(v.values()):
                cls._rebind_value(x, mod, old_mod, reloaded, seen, depth)
        elif isinstance(v, (list, tuple, set)):
            for x in list(v):
                cls._rebind_value(x, mod, old_mod, reloaded, seen, depth)
        elif hasattr(v, "__dict__") and not isinstance(v, type) and not callable(v):
            cls._rebind(v, mod, old_mod, reloaded, seen, depth)

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
            if not self._owner_gate_open(task):
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
            if key and (self.turns_root / task / key / "gate-hold.json").exists():
                self._gate_hold_retry(task, key)
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

    GATE_RETRY_S = 60.0

    def _gate_hold_retry(self, task, attempt):
        """a finished attempt the review gate holds: re-offer `complete` at most
        once a minute; success retires the task, refusal keeps the hold."""
        path = self.turns_root / task / attempt / "gate-hold.json"
        try:
            hold = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        last = self._gate_last.get(attempt)
        if last is not None and time.monotonic() - last < self.GATE_RETRY_S:
            return
        self._gate_last[attempt] = time.monotonic()
        try:
            _rc, data = self.control.complete(task, attempt, hold["outcome"], hold.get("evidence") or [],
                                              hold.get("output_sha"), hold.get("tree_sha"),
                                              hold.get("reason"), None, hold.get("unlock_dependents"))
        except ControlError as exc:
            hold["retries"] = int(hold.get("retries", 0)) + 1
            hold["last_refusal"] = {"ts": utc_ms(), "error": str(exc)[:300]}
            try:
                path.write_text(json.dumps(hold, indent=2, sort_keys=True), encoding="utf-8")
            except OSError:
                pass
            return
        newly = (data or {}).get("newly_ready") or []
        self.log("COMPLETE %s %s -> %s (gate released after %d retries) newly_ready=%s"
                 % (task, attempt, hold["outcome"], int(hold.get("retries", 0)), newly))
        self.completed.append((task, attempt, hold["outcome"]))
        try:
            path.rename(path.with_name("gate-hold.released.json"))
        except OSError:
            pass

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
        self.write_dispatch_record(wt, task, attempt, contract, base, row)
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
                       fails=result.get("fails"), wt=wt, kind=kind, hosted_owed=result.get("hosted_owed"))

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
                  reason=None, verdict=None, fails=None, wt=None, kind=None, hosted_owed=None):
        ev = [str(p) for p in evidence if p and Path(p).exists()]
        unlock = outcome == "VERIFIED" and bool(output_sha) and bool(ev)
        harvest = {"ts": utc_ms(), "task": task, "attempt": attempt, "outcome": outcome,
                   "output_sha": output_sha, "tree_sha": tree_sha, "evidence": ev,
                   "reason": reason, "fails": fails or [], "unlock_dependents": unlock,
                   "hosted_owed": list(hosted_owed or [])}
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
            elif "requires --verdict" in str(exc):
                # the scheduler's review gate (junior / final) holds the completion:
                # the work is done, the verdict is not.  Park the finished attempt
                # under gate-hold.json; adoption retries only `complete` (never the
                # turns) until the review packet records its verdict.  Found by the
                # ladder: L35 looped builder->grader->proof 46 times in 3 minutes.
                hold = dict(harvest, refused=str(exc)[:400], held_at=utc_ms(), retries=0)
                try:
                    (tdir / "gate-hold.json").write_text(json.dumps(hold, indent=2, sort_keys=True),
                                                         encoding="utf-8")
                except OSError:
                    pass
                self.alert_once("gate-hold:%s" % attempt, "REVIEW_GATE_HOLD",
                                "%s %s finished but the scheduler holds it for its review: %s"
                                % (task, attempt, str(exc)[:200]), task)
                return False
            else:
                self.alert("COMPLETE_REFUSED", "%s %s: %s" % (task, attempt, str(exc)[:300]), task)
                return False
        newly = (data or {}).get("newly_ready") or []
        self.log("COMPLETE %s %s -> %s unlock=%s newly_ready=%s" % (task, attempt, outcome, unlock, newly))
        self.completed.append((task, attempt, outcome))
        if outcome == "VERIFIED":
            try:
                if verdict and kind in REVIEW_KINDS:
                    self._pack_record_review(task, verdict, kind)
                if self.packet_for(task):
                    self._pack_closure(task, harvest, wt or (self.worktrees_root / task))
                self._supersede(task)
            except Exception as exc:  # noqa: BLE001 -- closure never undoes a completion
                self.alert("CLOSURE_FAILED", "%s: %s: %s" % (task, type(exc).__name__, str(exc)[:200]), task)
        return True

    # -- pipelines ----------------------------------------------------------------------

    def _note_unrun_checks(self, task, out_path):
        """checks[].exit == null is an honest NOT EXECUTED (the builder profile
        denies pytest/psql/...); the turn stands, the proof is the gate, but
        the owner should know the packet mandates a command the sandbox denies."""
        try:
            raw = json.loads(Path(out_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        canon, errs, warnings = vpschema.normalize_result(raw)
        if warnings:
            self.log("RESULT %s read leniently (D10): %s" % (task, "; ".join(warnings)[:600]))
        if not canon:
            return []
        unrun = [c for c in canon["checks"] if not c["executed"]]
        if unrun:
            self.alert_once("check-not-run:%s" % task, "CHECK_NOT_RUN",
                            "%s: %d check(s) not executed in the sandbox: %s -- proof is the gate; "
                            "if the packet mandates them, the vp-builder profile denies them"
                            % (task, len(unrun), "; ".join("%s (%s)" % (c["name"], c["command"][:80])
                                                            for c in unrun)[:400]), task)
        return unrun

    def _single_pipeline(self, task, attempt, row, contract, server, runner, rcfg, wt, tdir, sid, role):
        out_path = wt / ".vp" / "RESULT.json"
        outcome = self._turn(task, attempt, row, server, runner, rcfg, wt, tdir, role, PROBE_PROMPT,
                             out_path, vpschema.validate_result, sid, 1)
        if outcome.status != STATUS_DONE:
            return outcome, None
        self._note_unrun_checks(task, out_path)
        head = self.head_sha(wt)
        return outcome, {"outcome": "VERIFIED", "output_sha": head, "tree_sha": self.tree_sha(wt),
                         "evidence": [out_path, tdir / "record.json"]}

    def _build_pipeline(self, task, attempt, row, contract, server, runner, rcfg, wt, tdir, sid):
        """D14 order per round: build -> autofix -> PROOF -> grade.  The proof
        runs on the exact head the grader sees and its record + log are copied
        into <wt>/.vp/proofs/ (the grader's sandbox cannot read RUN_ROOT);
        red proof nodes are appended to FINDINGS.json as FAIL lines so the next
        builder round repairs them like any other FAIL.  Before D14 the grade
        came first: [test] rows were UNKNOWN (no proof yet), the proof ran only
        on an otherwise-clean grade, and a red proof went straight to
        REPAIR_REQUIRED without a repair round (5 rows graded with no proof at
        all, L17-REPLY-WIRING-R2 needed a hand-written FIX packet)."""
        gcfg = self.roles.get("grader") or {}
        grunner = gcfg.get("runner", "opencode")
        max_rounds = int(self.conc.get("max_rounds", 3))
        out_path = wt / ".vp" / "RESULT.json"
        fpath = wt / ".vp" / "FINDINGS.json"
        outcome, fails, blocking, owed, prec = None, [], [], [], None
        kind = contract.get("kind") or row.get("kind")
        needs_proof = kind in (self.proof_cfg.get("require_for_kinds") or ["integration"])
        pending = self._proof_pending(tdir)
        resume_proof = bool(needs_proof and pending and self.head_sha(wt) == pending.get("sha"))
        for rnd in range(1, max_rounds + 1):
            if resume_proof:
                # an earlier attempt built this exact head and its proof died on
                # infra: the build is not repeated, the proof (then grade) is
                resume_proof = False
                self.log("PROOF %s resumes on %s (build skipped)" % (task, pending["sha"][:12]))
                outcome = vprunners.TurnOutcome(STATUS_DONE, "proof resumed", runner="proof")
            else:
                outcome = self._turn(task, attempt, row, server, runner, rcfg, wt, tdir, "builder",
                                     BUILDER_PROMPT, out_path, vpschema.validate_result, sid, rnd)
                if outcome.status != STATUS_DONE:
                    return outcome, None
                self._note_unrun_checks(task, out_path)
                self._autofix(wt, task, tdir)
            head = self.head_sha(wt)
            prec = None
            if needs_proof:
                pout, prec = self._proof_step(task, attempt, row, contract, wt, tdir, head,
                                              self._base_of(wt), build_outcome=outcome)
                if prec is None:
                    return pout, None            # FAIL_INFRA/UNKNOWN: retry the proof alone later
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
            self._merge_proof_findings(fpath, prec)
            _doc, fails, unknown = findings_verdicts(fpath)
            hosted = self.hosted_rows(wt)
            owed = [u for u in unknown if u in hosted]
            blocking = [u for u in unknown if u not in hosted]
            if owed:
                self.alert_once("hosted-owed:%s" % task, "HOSTED_OWED",
                                "%s: %s graded UNKNOWN on the box as 04-REVIEW-POLICY §2 allows; the "
                                "<ID>-HOSTED twin owes the CircleCI/VPS evidence" % (task, ",".join(owed)), task)
            if not fails and blocking and self.regrade_once:
                # the proof is on disk and only [box] UNKNOWNs remain: one regrade
                # of this exact commit (review.regrade_same_commit_on_unknown_once)
                self.log("ROUND %s %d/%d unknown=%s -> one regrade" % (task, rnd, max_rounds, blocking))
                gout = self._turn(task, attempt, row, gserver, grunner, gcfg, wt, tdir, "grader",
                                  JUNIOR_PROMPT, fpath, validate_findings_recomputed, None, rnd + 100,
                                  expect_fence=True)
                if gout.status != STATUS_DONE:
                    return gout, None
                self._merge_proof_findings(fpath, prec)
                _doc, fails, unknown = findings_verdicts(fpath)
                owed = [u for u in unknown if u in hosted]
                blocking = [u for u in unknown if u not in hosted]
            if not fails and not blocking:
                return self._accept_build(task, attempt, row, contract, wt, tdir, outcome, out_path, fpath,
                                          prec, owed)
            self.log("ROUND %s %d/%d fails=%s unknown=%s%s" % (task, rnd, max_rounds, fails, blocking,
                                                             " hosted_owed=%s" % owed if owed else ""))
        head = self.head_sha(wt)
        reason = "%d rounds; FAIL %s" % (max_rounds, ",".join(fails)[:300])
        if not fails and blocking:
            reason = "%d rounds; UNGRADEABLE %s" % (max_rounds, ",".join(blocking)[:300])
        if prec is not None and prec.get("status") == "FAIL_PRODUCT":
            reason = "%d rounds; proof %s FAIL_PRODUCT: %s" % (max_rounds, prec.get("proof_id"),
                                                                ", ".join(prec.get("failed_nodes") or [])[:300])
        return outcome, {"outcome": "REPAIR_REQUIRED", "output_sha": head, "tree_sha": self.tree_sha(wt),
                         "evidence": [out_path, fpath, tdir / "record.json"] + self._proof_evidence(prec),
                         "reason": reason, "fails": self._fail_lines(fpath), "hosted_owed": owed}

    def _proof_evidence(self, prec):
        if not prec:
            return []
        return [self.run_root / "proofs" / ("%s.json" % prec.get("proof_id"))]

    @staticmethod
    def _merge_proof_findings(fpath, prec):
        """append the proof's red nodes to FINDINGS.json as FAIL lines (id = the
        node id, evidence = the log copied under .vp/proofs/) and recompute
        all_pass; the builder's next round sees them as FAIL lines to repair."""
        if not prec or prec.get("status") != "FAIL_PRODUCT":
            return False
        try:
            doc = json.loads(Path(fpath).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        lines = doc.get("lines") if isinstance(doc, dict) else None
        if not isinstance(lines, list):
            return False
        have = {str(l.get("id")) for l in lines if isinstance(l, dict)}
        errs = prec.get("errors") or {}
        log = prec.get("wt_log") or prec.get("log") or ""
        added = 0
        for node in prec.get("failed_nodes") or []:
            if node in have:
                continue
            lines.append({"id": node, "kind": "test", "verdict": "FAIL", "evidence": str(log)[:300],
                          "note": ("proof %s red: %s" % (prec.get("proof_id"), errs.get(node) or
                                                          "red on %s" % prec.get("route")))[:300]})
            added += 1
        if added:
            doc["all_pass"] = False
            Path(fpath).write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        return bool(added)

    def _accept_build(self, task, attempt, row, contract, wt, tdir, outcome, out_path, fpath, prec, owed):
        head = self.head_sha(wt)
        ev = [out_path, fpath, tdir / "record.json"] + self._proof_evidence(prec)
        return outcome, {"outcome": "VERIFIED", "output_sha": head, "tree_sha": self.tree_sha(wt),
                         "evidence": ev, "hosted_owed": owed}

    @staticmethod
    def hosted_rows(wt):
        """benchmark ids tagged [hosted] in <wt>/.vp/BENCHMARK.md (04-REVIEW-POLICY §2:
        graded UNKNOWN on the parent item, never blocking; the twin task owns them)"""
        try:
            text = (Path(wt) / ".vp" / "BENCHMARK.md").read_text(encoding="utf-8")
        except OSError:
            return set()
        return set(HOSTED_TAG_RE.findall(text))

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
        """run the proof on `cand`; returns (outcome, record) for PASS /
        FAIL_PRODUCT (the caller grades next, the record decides) and
        (PROOF_<status> outcome, None) for FAIL_INFRA/UNKNOWN/CANCELLED, which
        keeps proof-pending.json so the retry resumes at the proof.  The record
        and the pytest log are copied to <wt>/.vp/proofs/<pid>.{json,log} and
        the record to .vp/PROOF.json: that is what the grader may read."""
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
        status = rec.get("status")
        self._copy_proof_into_worktree(wt, pid, rec)
        outcome = build_outcome or vprunners.TurnOutcome(STATUS_DONE, "proof only", runner="proof")
        if status in ("PASS", "FAIL_PRODUCT"):
            try:
                (tdir / "proof-pending.json").unlink()
            except OSError:
                pass
            return outcome, rec
        # FAIL_INFRA / UNKNOWN / CANCELLED: not the candidate's fault -- retry the
        # proof alone after the backoff (proof-pending.json keeps the head)
        return vprunners.TurnOutcome("PROOF_" + str(status), "proof %s %s: %s"
                                     % (pid, status, str(rec.get("reason") or "")[:200]),
                                     runner="proof"), None

    PROOF_LOG_CAP = 2 * 1024 * 1024

    def _copy_proof_into_worktree(self, wt, pid, rec):
        """<wt>/.vp/proofs/<pid>.json + .log (tail-capped) and .vp/PROOF.json.
        The grader runs in a sandbox rooted at the worktree: RUN_ROOT/proofs is
        "permission denied" to it, so the evidence must live under .vp/."""
        pdir = Path(wt) / ".vp" / "proofs"
        try:
            pdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        src = rec.get("log") or (rec.get("counts") or {}).get("log")
        if src and Path(str(src)).exists():
            try:
                data = Path(str(src)).read_bytes()
                if len(data) > self.PROOF_LOG_CAP:
                    data = b"[... head truncated by lanedriver ...]\n" + data[-self.PROOF_LOG_CAP:]
                (pdir / ("%s.log" % pid)).write_bytes(data)
                rec["wt_log"] = ".vp/proofs/%s.log" % pid
            except OSError:
                pass
        rec["wt_record"] = ".vp/proofs/%s.json" % pid
        text = json.dumps(rec, indent=2, sort_keys=True, default=str)
        for dst in (pdir / ("%s.json" % pid), Path(wt) / ".vp" / "PROOF.json"):
            try:
                dst.write_text(text, encoding="utf-8")
            except OSError:
                pass

    @staticmethod
    def _fail_lines(fpath):
        try:
            doc = json.loads(Path(fpath).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [{"id": str(l.get("id")), "note": str(l.get("note") or "")[:300],
                 "evidence": str(l.get("evidence") or "")[:300]}
                for l in doc.get("lines") or [] if isinstance(l, dict) and l.get("verdict") != "PASS"]

    def _rebind_result(self, wt, head, task):
        """The autofix commit sits on top of the builder's commit, so the
        RESULT.json the builder wrote names the pre-autofix sha while 54
        packets' [evidence] rows require `commit` == `git rev-parse HEAD`.
        The record is the graded head's: rebind commit (keeping the builder's
        under pre_autofix_commit) and refresh diff_stat from git."""
        rp = Path(wt) / ".vp" / "RESULT.json"
        try:
            doc = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(doc, dict) or doc.get("commit") == head:
            return False
        doc["pre_autofix_commit"] = doc.get("commit")
        doc["commit"] = head
        base = doc.get("base") or self._base_of(wt)
        if base:
            rc, out, _ = self.git(["-C", str(wt), "diff", "--numstat", "%s..%s" % (base, head)])
            if rc == 0:
                ins = dele = files = 0
                for line in out.splitlines():
                    parts = line.split("\t")
                    if len(parts) == 3:
                        files += 1
                        ins += int(parts[0]) if parts[0].isdigit() else 0
                        dele += int(parts[1]) if parts[1].isdigit() else 0
                doc["diff_stat"] = {"files": files, "insertions": ins, "deletions": dele}
        try:
            rp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            return False
        self.log("AUTOFIX %s RESULT.json commit rebound %s -> %s" % (task, str(doc["pre_autofix_commit"])[:12], head[:12]))
        return True

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
                self._rebind_result(wt, committed, task)
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
        p = self.packet_for(task)
        rp = self._review_packet_plan(task, row, contract, wt, base, cand, state) if p and p.get("v13_kind") == "review" else None
        if rp:
            base = rp["base"]
            # D17: the reviewer judges the SUBJECT (the target contracts' own
            # criteria over review_base..candidate), never the packet's benchmark
            # about the verdict file the driver has yet to write
            (wt / ".vp" / "BENCHMARK.md").write_text(rp["review_benchmark"], encoding="utf-8")
        req = {"item": task, "subject": "item", "base": base, "candidate": cand,
               "reviewer": rcfg.get("model") or "reviewer",
               "benchmark_ids": self._benchmark_ids(wt), "unverified_ids": []}
        if rp:
            req.update({"subject": rp["subject"], "coverage_targets": rp["targets"],
                        "criteria": rp["criteria"], "packet": p["id"]})
        (wt / ".vp" / "REVIEW_REQUEST.json").write_text(json.dumps(req, indent=2, sort_keys=True),
                                                        encoding="utf-8")
        out_path = wt / ".vp" / "REVIEW.json"
        outcome = self._turn(task, attempt, row, server, runner, rcfg, wt, tdir, "reviewer",
                             REVIEW_PROMPT, out_path, vpschema.validate_review, sid, 1)
        if rp:
            (wt / ".vp" / "BENCHMARK.md").write_text(rp["packet_benchmark"], encoding="utf-8")
        if outcome.status != STATUS_DONE:
            return outcome, None
        try:
            doc = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        verdict = doc.get("verdict")
        if verdict == "APPROVE":
            packet = self._verdict_packet(task, row, contract, state, outcome, out_path, tdir, doc,
                                          targets=(rp or {}).get("targets"), criteria=(rp or {}).get("criteria"))
            if rp:
                return self._review_packet_finish(task, attempt, row, contract, server, wt, tdir, outcome,
                                                  packet, rp, cand)
            return outcome, {"outcome": "VERIFIED", "output_sha": cand, "tree_sha": self.tree_sha(wt),
                             "evidence": [out_path, tdir / "record.json"], "verdict": packet}
        fails = [{"id": str(f.get("id")), "note": str(f.get("title") or "")[:300],
                  "evidence": "%s:%s" % (f.get("file"), f.get("line"))} for f in doc.get("findings") or []]
        return outcome, {"outcome": "REPAIR_REQUIRED", "evidence": [out_path, tdir / "record.json"],
                         "reason": "review %s: %s" % (verdict, str(doc.get("summary") or "")[:300]),
                         "fails": fails}

    # -- D17: v13 review packets ------------------------------------------------------------

    def _review_packet_plan(self, task, row, contract, wt, base, cand, state):
        """what a `v13_kind: review` packet asks the driver to review.  Header
        keys (optional): `review_base` (the subject's base sha; default the
        row base -- for SEC-REVIEW-S3 the s3 delta d18363e5..5deac821, not an
        empty base==candidate diff) and `coverage_targets` (contracts whose
        criteria the verdict packet must cover; default the parent contract).
        The reviewer's benchmark is those criteria, one row each."""
        hdr = {}
        try:
            hdr, _ = vplint.parse_front_matter((wt / ".vp" / "PACKET.md").read_text(encoding="utf-8"))
            hdr = hdr or {}
        except (OSError, ValueError):
            pass
        rbase = str(hdr.get("review_base") or "").strip() or base
        if rbase != base:
            rc, _o, _e = self.git(["-C", str(wt), "cat-file", "-e", rbase + "^{commit}"])
            if rc != 0:
                self.alert_once("review-base:%s" % task, "REVIEW_BASE_UNKNOWN",
                                "%s: review_base %s is not a commit; reviewing from the row base %s"
                                % (task, rbase[:12], base[:12]), task)
                rbase = base
        params = row.get("parameters") or {}
        target = params.get("parent_contract_id") or task
        targets = []
        for t in (hdr.get("coverage_targets") or []):
            if str(t).strip() == "<union>":
                # the union's members: the review row's covered_rows (the
                # INTEGRATED contracts the dispatch record lists)
                targets += [str(c) for c in (params.get("covered_rows") or [])]
            else:
                targets.append(str(t).strip())
        targets = list(dict.fromkeys(targets)) or [target]
        if target not in targets:
            targets.insert(0, target)
        criteria = {}
        for t in targets:
            tcon = self.control.contract(t, ((state.get("tasks") or {}).get(t) or {}))
            criteria[t] = list(dict.fromkeys((tcon.get("verification") or []) + (tcon.get("acceptance") or [])))
        lines, n = [], 0
        for t in targets:
            for c in criteria[t]:
                n += 1
                lines.append("- B%d [invariant] [box] %s: %s — check: `git diff %s..%s` and the files it touches\n"
                             % (n, t, c.replace("\n", " "), rbase[:12], cand[:12]))
        if not lines:
            lines.append("- B1 [invariant] [box] %s: the candidate is sound over %s..%s — check: the diff\n"
                         % (target, rbase[:12], cand[:12]))
        try:
            packet_benchmark = (wt / ".vp" / "BENCHMARK.md").read_text(encoding="utf-8")
        except OSError:
            packet_benchmark = ""
        head = "# Review subject: %s..%s (%s)\n" % (rbase[:12], cand[:12], ", ".join(targets))
        return {"base": rbase, "targets": targets, "criteria": criteria, "subject": "union" if "union" in
                str((row.get("parameters") or {}).get("diff_or_scope") or "") else "item",
                "review_benchmark": head + "".join(lines), "packet_benchmark": packet_benchmark}

    def _verdict_owned_path(self, task, p, row):
        """the packet's verdict-packet.json under owned_files, with <union>
        resolved the way the dispatch record resolves it."""
        owned = []
        try:
            disp = json.loads((self.worktrees_root / task / ".vp" / "DISPATCH.json").read_text(encoding="utf-8"))
            owned = list(disp.get("owned_files") or [])
        except (OSError, ValueError):
            owned = list((row.get("parameters") or {}).get("owned_paths") or p.get("owned_files") or [])
        for f in owned:
            if f.endswith("verdict-packet.json") and "<" not in f:
                return f
        return "control/evidence/%s/v13/verdict-packet.json" % p["id"]

    def _review_packet_finish(self, task, attempt, row, contract, server, wt, tdir, outcome, packet, rp, cand):
        """after APPROVE: validate the verdict packet with review_gate (the
        packet's Step: a refusal is recorded, never relabelled), copy it to the
        packet's owned path and commit, then grade the packet's OWN benchmark
        with the grader as for any build; VERIFIED carries the verdict."""
        p = self.packet_for(task)
        state = self.control.state_view()
        gate_ok, gate_msg = self._review_gate_check(packet, state, row, task, contract)
        rel = self._verdict_owned_path(task, p, row)
        dst = wt / rel
        if not gate_ok:
            # the packet's own Step: a refusal is recorded in RESULT.json
            # `blocked`, nothing is committed, the verdict is never relabelled.
            # INVALID_EVIDENCE (not REPAIR_REQUIRED): the gate refused the
            # session linkage/rollout, not the review's substance, and only an
            # INVALID_EVIDENCE/CANCELLED review admits a retry under the same
            # review key
            head = self.head_sha(wt)
            self._write_review_result(wt, task, attempt, head, self._base_of(wt), packet, False, gate_msg, rp)
            self.log("REVIEW %s APPROVE but review_gate refused: %s" % (task, gate_msg[:200]))
            return outcome, {"outcome": "INVALID_EVIDENCE", "output_sha": head, "tree_sha": self.tree_sha(wt),
                             "evidence": [wt / ".vp" / "REVIEW.json", wt / ".vp" / "RESULT.json",
                                          tdir / "record.json", packet],
                             "reason": "review_gate refused the verdict packet: %s" % gate_msg[:300],
                             "fails": [{"id": "review_gate", "note": gate_msg[:300], "evidence": rel}]}
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(packet), str(dst))
            gate = {"validated": gate_ok, "message": gate_msg, "packet": str(packet), "ts": utc_ms()}
            (dst.parent / "review-gate.json").write_text(json.dumps(gate, indent=2, sort_keys=True), encoding="utf-8")
            self.git(["-C", str(wt), "add", "--", rel, str(dst.parent / "review-gate.json")], log=True)
            self.git(["-C", str(wt), "-c", "user.email=lanedriver@vp", "-c", "user.name=lanedriver",
                      "commit", "-q", "-m", "%s: v13 %s verdict packet" % (p["id"], contract.get("kind"))], log=True)
        except OSError as exc:
            self.log("REVIEW %s verdict copy failed: %s" % (task, exc))
        head = self.head_sha(wt)
        self._write_review_result(wt, task, attempt, head, self._base_of(wt), packet, gate_ok, gate_msg, rp)
        gcfg = self.roles.get("grader") or {}
        grunner = gcfg.get("runner", "opencode")
        gserver = self._pick_server(gcfg) if grunner == "opencode" else None
        if grunner == "opencode" and gserver is None:
            gserver = server
        fpath = wt / ".vp" / "FINDINGS.json"
        gout = self._turn(task, attempt, row, gserver, grunner, gcfg, wt, tdir, "grader",
                          JUNIOR_PROMPT, fpath, validate_findings_recomputed, None, 1, expect_fence=True)
        if gout.status != STATUS_DONE:
            return gout, None
        _doc, fails, unknown = findings_verdicts(fpath)
        hosted = self.hosted_rows(wt)
        blocking = [u for u in unknown if u not in hosted]
        ev = [wt / ".vp" / "REVIEW.json", wt / ".vp" / "RESULT.json", fpath, tdir / "record.json", packet]
        if fails or blocking:
            return outcome, {"outcome": "REPAIR_REQUIRED", "output_sha": head, "tree_sha": self.tree_sha(wt),
                             "evidence": ev, "reason": "verdict written; packet benchmark FAIL %s UNKNOWN %s"
                             % (",".join(fails)[:200], ",".join(blocking)[:100]), "fails": self._fail_lines(fpath)}
        return outcome, {"outcome": "VERIFIED", "output_sha": head, "tree_sha": self.tree_sha(wt),
                         "evidence": ev, "verdict": packet, "hosted_owed": [u for u in unknown if u in hosted]}

    def _review_gate_check(self, packet, state, row, task, contract):
        """review_gate.validate from the scheduler's directory, in-process
        (read-only): (ok, message).  Missing module -> (False, why)."""
        ctl = self.roster.get("control") or {}
        cwd = ctl.get("cwd") or (str(Path(ctl["script"]).parent) if ctl.get("script") else None)
        if not cwd or not (Path(cwd) / "review_gate.py").exists():
            return False, "review_gate.py not found beside the scheduler"
        role = {"final_review": "final_review"}.get(contract.get("kind"), contract.get("kind"))
        target = (row.get("parameters") or {}).get("parent_contract_id") or task
        try:
            spec = importlib.util.spec_from_file_location("vp_review_gate", str(Path(cwd) / "review_gate.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            rec = mod.validate(str(packet), state, self.control.catalog(), target, role)
            return True, "PASS: %s" % json.dumps(rec, default=str)[:200]
        except Exception as exc:  # noqa: BLE001 -- the gate's refusal is the message
            return False, "%s: %s" % (type(exc).__name__, str(exc)[:300])

    def _write_review_result(self, wt, task, attempt, head, base, packet, gate_ok, gate_msg, rp):
        doc = {"item": task, "attempt": attempt, "commit": head, "base": base,
               "diff_stat": {"files": 2, "insertions": 0, "deletions": 0},
               "checks": [{"name": "review_gate.validate", "command": "review_gate.validate(<packet>)",
                           "exit": 0 if gate_ok else 1, "log": gate_msg[:400]}],
               "disputes": [], "blocked": None if gate_ok else gate_msg[:300],
               "notes": "D17 review packet: verdict %s; subject %s..; targets %s"
                        % (Path(packet).name, rp["base"][:12], ",".join(rp["targets"]))}
        try:
            (wt / ".vp" / "RESULT.json").write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            pass

    def _verdict_packet(self, task, row, contract, state, outcome, out_path, tdir, doc, targets=None, criteria=None):
        """review_gate packet from the review record; the scheduler validates it
        (runtime log under ~/.codex/sessions, reviewer == the task's child_id).
        `targets`/`criteria` (D17) widen the coverage map to every contract the
        packet names (SEC-REVIEW-S3 + L29 + L30)."""
        role = contract.get("kind")
        role = {"final_review": "final_review"}.get(role, role)
        cand = state.get("candidate") or {}
        target = (row.get("parameters") or {}).get("parent_contract_id") or task
        tcon = self.control.contract(target)
        trow = (state.get("tasks") or {}).get(target) or {}
        crit = list(dict.fromkeys((tcon.get("verification") or []) + (tcon.get("acceptance") or [])))
        coverage = {target: {c: "PASS" for c in crit}}
        for t in (targets or []):
            coverage.setdefault(t, {c: "PASS" for c in (criteria or {}).get(t) or []})
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
                  "coverage": coverage,
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
        # roster-v13 spells the per-turn ceilings by verb (build/grade/review/...)
        alias = {"builder": "build", "grader": "grade", "junior": "review", "reviewer": "review",
                 "integrator": "integrate", "final": "final", "security": "security"}.get(role)
        timeout_s = float(mm.get(role, mm.get(alias, mm.get("default", 45)))) * 60
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

    def _pack_owns(self, task):
        """a task some packet drives or closes is the pack's business: the
        frontier never auto-repairs it (the live run-state has 35 stale
        REPAIR_REQUIRED review rows the pack retires; a review is never repaired)"""
        if task in self.pack_by_task or task in self.pack:
            return True
        if self._pack_root(task) in self.pack:
            return True                       # a retry / -V13 descendant of a packet (bound or superseded)
        return any(task in p["closes"] or p["scheduler_task"] == task for p in self.pack.values())

    def _instantiate_repair(self, parent, tasks):
        prow = tasks.get(parent) or {}
        if (prow.get("kind") in REVIEW_KINDS) or self._pack_owns(parent):
            if ("norepair", parent) not in self._pack_logged:
                self._pack_logged.add(("norepair", parent))
                self.log("FRONTIER %s REPAIR_REQUIRED left to the pack / review flow (no auto-repair)" % parent)
            return False
        # a harvest-less hold (Chief-era row, no driver attempt) has nothing to repair from
        if not self._last_harvest(parent):
            return False
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


class DryRunProof(object):
    """Ladder / dry-run proof: no pytest, no CircleCI.  Answers PASS (or the
    scripted status per task) after `delay_s`, records proofs/<pid>.json like
    the real Proof, and keeps a live counter so concurrency is observable."""

    def __init__(self, run_root, statuses=None, delay_s=0.0, log=None):
        self.run_root = Path(run_root)
        self.statuses = dict(statuses or {})
        self.delay_s = float(delay_s)
        self.log = log or (lambda m: None)
        self.cfg = {}
        self.calls = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def run(self, task, pid, wt, base, cand, kind, paths, abort=None):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append({"task": task, "proof_id": pid, "sha": cand, "kind": kind, "ts": utc_ms()})
        try:
            end = time.monotonic() + self.delay_s
            while time.monotonic() < end:
                if abort and abort():
                    break
                time.sleep(0.05)
            status = self.statuses.get(task, "PASS")
            rec = {"status": status, "route": "dryrun", "proof_id": pid, "task": task, "sha": cand,
                   "kind": kind, "paths": list(paths or []), "failed_nodes":
                   ["control/dryrun/%s.txt::scripted" % task] if status == "FAIL_PRODUCT" else [],
                   "errors": {}, "ts": utc_ms(), "note": "dry run, no proof harness"}
            d = self.run_root / "proofs"
            try:
                d.mkdir(parents=True, exist_ok=True)
                (d / ("%s.json" % pid)).write_text(json.dumps(rec, indent=2, sort_keys=True),
                                                   encoding="utf-8")
            except OSError:
                pass
            self.log("PROOF %s %s route=dryrun -> %s" % (task, pid, status))
            return rec
        finally:
            with self._lock:
                self.active -= 1


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

    def preopen(self, spec):
        """codex-style pre-open: a fake thread id so `start` can bind it"""
        return "dry-thread-%s" % spec.item, "dry run"

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
    ap.add_argument("--roster", help="RUN_ROOT/roster.json (every command but init-run)")
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
    sub.add_parser("render", help="F14: write LEDGER.md now (the loop also does it on a timer)")
    rl = sub.add_parser("reload", help="D12: write RUN_ROOT/RELOAD; the running driver quiesces, "
                                       "reloads its code in place at active == 0, resumes")
    rl.add_argument("--reason", default="")
    rp = sub.add_parser("retry-packet", help="re-instantiate a packet whose task closed without a real "
                                             "verdict (e.g. RUNNER_CRASH -> INVALID_EVIDENCE) as <id>-R<n>")
    rp.add_argument("packet")
    rp.add_argument("--reason", required=True)
    ir = sub.add_parser("init-run", help="copy v13-pack/roster-v13.json -> RUN_ROOT/roster.json (lint, snapshot)")
    ir.add_argument("--source", required=True, help="the pack roster, e.g. v13-pack/roster-v13.json")
    ir.add_argument("--run-root", help="override the roster's run.run_root")
    ir.add_argument("--force", action="store_true", help="copy despite vplint errors")
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


def cmd_retry_packet(drv, args):
    try:
        rec = drv.request_packet_retry(args.packet, args.reason)
    except ValueError as exc:
        print(json.dumps({"status": "REFUSED", "error": str(exc)}))
        return 2
    print(json.dumps({"status": "REQUESTED", "retry": rec,
                      "note": "the loop instantiates <packet>-R<n> on its next pack reconcile "
                              "(alerts.pack_every_s, default 300 s)"}, indent=2))
    return 0


def cmd_reload(drv, args):
    f = drv.run_root / RELOAD_FILE
    f.write_text(json.dumps({"reason": args.reason, "requested_at": utc_ms(), "by": "cli"}) + "\n",
                 encoding="utf-8")
    hb = read_heartbeat(drv.run_root)
    print(json.dumps({"status": "REQUESTED", "marker": str(f),
                      "driver": {"pid": (hb or {}).get("pid"), "active": (hb or {}).get("active"),
                                 "code_version": (hb or {}).get("code_version")},
                      "note": "applied when active == 0; watch driver.log for 'RELOAD ok' / RELOAD_FAILED "
                              "and driver.heartbeat code_version"}, indent=2))
    return 0


SUBCOMMANDS = ("run", "drain", "undrain", "restart", "item", "comms", "seal", "render", "init-run",
               "retry-packet", "reload")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and not any(a in argv for a in SUBCOMMANDS):
        argv = argv[:2] + ["run"] + argv[2:] if argv[0] == "--roster" else argv
    args = build_parser().parse_args(argv)
    if args.cmd == "init-run":
        return cmd_init_run(args)
    if not args.roster:
        build_parser().error("--roster is required")
    if args.cmd == "run":
        runners = None
        if args.fake_runners:
            dry = DryRunRunner()
            runners = {"opencode": dry, "codex": dry, "claude": dry, "agy": dry}
        proof = DryRunProof(Path(args.roster).resolve().parent) if args.fake_runners else None
        drv = LaneDriver(args.roster, interval=args.interval, runners=runners, proof=proof,
                         bins={"osascript": shutil.which("osascript")} if shutil.which("osascript") else None)
        if args.fake_runners:
            drv.fake_runners = True
            proof.log = drv.log
            drv.log("FAKE RUNNERS in force: no model call, no proof harness, no network")
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
    if args.cmd == "retry-packet":
        return cmd_retry_packet(drv, args)
    if args.cmd == "reload":
        return cmd_reload(drv, args)
    if args.cmd == "render":
        out = drv.render()
        print(json.dumps({"status": "OK" if out else "FAILED", "ledger": str(out or "")}))
        return 0 if out else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
