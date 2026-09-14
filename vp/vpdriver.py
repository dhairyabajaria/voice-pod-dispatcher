#!/usr/bin/env python3
"""vpdriver.py -- the v12 tick loop for the Voice Pod control layer.

    python3 vpdriver.py --roster RUN_ROOT/roster.json --loop --auto-assign --exit-on-stop
    python3 vpdriver.py --roster RUN_ROOT/roster.json --once

Plan v12 files 02/03/07.  The driver is the ONLY thing that moves item
state, and it does so only from records it has read from disk:

  status            role      runner    record                -> store verb
  ASSIGNED          driver    git       worktree               claim
  BUILDING          builder   opencode  .vp/RESULT.json        submit-result
  GRADING           junior    opencode  .vp/FINDINGS.json      findings
  JUNIOR_SATISFIED  senior    codex     .vp/REVIEW.json        review record (plan)
  PREPARING         driver    git       union-N worktree       union record + proof request
  PREPARED          proof     vpproof   proofs/<sha>/*.json    proof record
  PROOF_PENDING     (in flight)
  FINAL_REVIEW      final     claude    unions/N/FINAL.json    review record (code)
  APPROVED          owner (morning)

Every model is a leaf that returns a schema-validated record; empty output is
UNKNOWN, never OK; STOP is a file; every timestamp is read from the clock by
the process that writes the line.  stdlib only.
"""

from __future__ import annotations

import argparse
import fnmatch
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
import vpcircle   # noqa: E402
import vplint     # noqa: E402
import vpmerge    # noqa: E402
import vprunners  # noqa: E402
import vpschema   # noqa: E402
from vprunners import (STATUS_DONE, STATUS_INCOMPLETE, STATUS_PROGRESS_STOP,  # noqa: E402
                       STATUS_REFUSED, STATUS_ABORTED, PARK_STATUSES, TurnSpec)

__all__ = ["Driver", "Store", "StoreError", "BUILDER_PROMPT", "JUNIOR_PROMPT",
           "SENIOR_PROMPT", "FINAL_PROMPT"]

# --------------------------------------------------------------------------
# Fixed prompts.  Byte-identical across every turn so the provider caches the
# prefix; the per-item payload lives in the worktree files.  DO NOT
# interpolate anything here (K-13).
# --------------------------------------------------------------------------
BUILDER_PROMPT = (
    "Read .vp/PACKET.md and .vp/BENCHMARK.md. If .vp/FINDINGS.json exists "
    "repair only its FAIL lines. Follow the agent rules. Commit your work. "
    "Write .vp/RESULT.json per .vp/RESULT_SCHEMA.json and stop."
)
JUNIOR_PROMPT = (
    "Grade the current HEAD commit of this worktree against .vp/BENCHMARK.md "
    "line by line. Write .vp/FINDINGS.json per .vp/FINDINGS_SCHEMA.json with "
    "file:line evidence and stop. all_pass is true ONLY when every line is PASS; "
    "an UNKNOWN verdict is not PASS. Use UNKNOWN only for a line you genuinely "
    "cannot verify with the tools you have, and say why in its note. "
    "If the write is refused, print the complete "
    "FINDINGS JSON in a single ```json fence as your final message and stop."
)
JUNIOR_RERUN_HINT = (
    " Your previous grading of this same commit left %s UNKNOWN (%s). Verify "
    "those lines now with Read/Grep/git and give PASS or FAIL with file:line "
    "evidence; leave UNKNOWN only if it truly cannot be checked from this worktree."
)
SENIOR_PROMPT = (
    "You are the Senior reviewer. Read .vp/REVIEW_REQUEST.json (item, subject, "
    "base, candidate, reviewer, benchmark_ids, unverified_ids), then .vp/PACKET.md, "
    ".vp/BENCHMARK.md, .vp/RESULT.json and .vp/FINDINGS.json. Inspect "
    "`git diff <base>..<candidate>` and every file it touches. Token budget: "
    "read ONLY the diff, the touched files, the packet's owned_files and "
    "test_paths, and the specific callers/callees a finding needs; do not "
    "walk the repository or read unrelated modules or docs. Verify each "
    "benchmark id YOURSELF with file:line evidence; never trust FINDINGS.json. "
    "Record a finding for any defect, missing or adjacent-path test, "
    "forbidden-file change, or benchmark line not met; severity "
    "low|medium|high|critical; give a reproduce command when you have one; "
    "name the benchmark_line it affects. Verdict APPROVE only when every "
    "benchmark id is PASS with evidence and no finding of severity medium or "
    "higher carries a reproduce command; otherwise FINDINGS; BLOCKED only when "
    "the packet itself is wrong. Output ONLY the JSON object per the schema, "
    "copying item, subject, base, candidate and reviewer from "
    "REVIEW_REQUEST.json verbatim. No prose. Any id in unverified_ids was left "
    "UNKNOWN by the junior twice (it could not verify it with read-only tools); "
    "you must give it a PASS or FAIL verdict yourself with evidence."
)
FINAL_PROMPT = (
    "You are the Final reviewer of a union. Read .vp/FINAL_REQUEST.json "
    "(union, base, candidate, reviewer, items[] with packet, benchmark, review "
    "and findings paths, benchmark_ids). For EVERY item read its packet and "
    "benchmark, then inspect `git diff <base>..<candidate>` on this worktree. "
    "Verify every benchmark id of every item yourself with file:line evidence; "
    "the senior's review is input, not truth. Record a finding for any defect, "
    "interaction between items, forbidden-file change, migration collision, or "
    "benchmark line not met. Verdict APPROVE only when every benchmark id is "
    "PASS with evidence and no medium+ finding has a reproduce command. Output "
    "ONLY the JSON object per the schema, with item set to the union id and "
    "subject 'union', copying base, candidate and reviewer from "
    "FINAL_REQUEST.json verbatim. No prose."
)
RESUME_PROMPT = "continue"
# registry tests ride on every union proof (rendered artifacts belong to the
# merged tree, see _regenerate_union_artifacts)
class RegistryFailed(str):
    """Returned by _regenerate_union_artifacts when the environment registry
    writer refused or its delta could not be committed: build_union BLOCKs the
    union instead of proving a tree with a stale generated registry."""


UNION_ALWAYS_PATHS = ("platform/tests/test_environment_registry.py",
                      "deploy/tests/test_worker_packaging.py",
                      # corpus meta-test over agent/*.py: any agent change can red it
                      "agent/tests/test_log_privacy.py",
                      # TECHNICAL.md route/migration facts vs the live app: any new
                      # route or migration reds it on the merged tree
                      "platform/tests/test_docs_truth.py")


def _kind_of_always_path(p):
    if p.startswith("deploy/"):
        return "deploy"
    if p.startswith("agent/"):
        return "agent"
    return "platform"
PROBE_PROMPT = "Reply with exactly PONG and nothing else."

MAX_RESUMES = 3
FAIL_CAP = 3
FAIL_BACKOFF_S = (60, 120, 300)
DEFAULT_INTERVAL = 5.0
IST = timezone(timedelta(hours=5, minutes=30))

ROLE_FOR_STATUS = {
    "BUILDING": "builder", "GRADING": "junior", "JUNIOR_SATISFIED": "senior",
    "PREPARED": "proof", "FINAL_REVIEW": "final",
}
KIND_OF_ROLE = {"builder": "build", "junior": "grade", "infra": "infra",
                "senior": "review", "final": "final", "integrator": "integrate",
                "proof": "proof", "boss": "boss"}
OUTPUT_OF_ROLE = {"builder": "RESULT.json", "junior": "FINDINGS.json",
                  "infra": "RESULT.json", "senior": "REVIEW.json",
                  "final": "FINAL.json"}
PROMPT_OF_ROLE = {"builder": BUILDER_PROMPT, "junior": JUNIOR_PROMPT,
                  "infra": BUILDER_PROMPT, "senior": SENIOR_PROMPT,
                  "final": FINAL_PROMPT}
def circle_failed_nodes(failed_tests):
    """CircleCI `tests` items (junit xunit1 from pytest: file, classname,
    name, message) -> (sorted node ids, {node: message}).  `file` is the
    repo-relative path when the junit carried it; otherwise the dotted
    classname is unfolded (platform.tests.test_x[.TestFoo] -> path[::TestFoo])."""
    nodes, errors = set(), {}
    for _job, items in (failed_tests or {}).items():
        for t in items or []:
            if str(t.get("result") or "failure") not in ("failure", "error"):
                continue        # skipped/success never become nodes (belt and braces)
            name = str(t.get("name") or "").strip()
            path = str(t.get("file") or "").strip()
            cls = str(t.get("classname") or "").strip()
            if not path and cls:
                parts = cls.split(".")
                mod = [x for x in parts if x[:1].islower() or x[:1] == "_"]
                path = "/".join(mod) + ".py" if mod else ""
                klass = [x for x in parts[len(mod):] if x]
                if klass:
                    name = "::".join(klass + [name]) if name else "::".join(klass)
            node = ("%s::%s" % (path, name)) if path and name else (path or name or cls)
            if not node:
                continue
            nodes.add(node)
            msg = str(t.get("message") or "").strip()
            if msg and node not in errors:
                errors[node] = msg[:600]
    return sorted(nodes), errors


def findings_verdicts(path):
    """(doc, fail_ids, unknown_ids) of a FINDINGS.json, with all_pass RECOMPUTED
    from the per-line verdicts and written back.  The junior's own summary is
    never trusted: 7 of 109 junior records said all_pass true over an UNKNOWN
    line, each one an INCOMPLETE strike (SHIP-01 x4 -> BLOCKED, A2-1, CAT-01,
    A4-3, A3-1a)."""
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    lines = doc.get("lines") if isinstance(doc, dict) else None
    if not isinstance(lines, list):
        return doc, [], []
    fails = [str(l.get("id")) for l in lines if isinstance(l, dict) and l.get("verdict") == "FAIL"]
    unknown = [str(l.get("id")) for l in lines
               if isinstance(l, dict) and l.get("verdict") == "UNKNOWN"]
    computed = all(isinstance(l, dict) and l.get("verdict") == "PASS" for l in lines)
    if doc.get("all_pass") is not computed:
        doc["all_pass"] = computed
        path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return doc, fails, unknown


def validate_findings_recomputed(path):
    """The junior's validator: recompute all_pass first, then the schema."""
    try:
        findings_verdicts(path)
    except (OSError, ValueError):
        pass
    return vpschema.validate_findings(path)


VALIDATOR_OF_ROLE = {"builder": vpschema.validate_result,
                     "infra": vpschema.validate_result,
                     "junior": validate_findings_recomputed,
                     "senior": vpschema.validate_review,
                     "final": vpschema.validate_review}
# claude --json-schema must match the record the role's validator expects
SCHEMA_DOC_OF_ROLE = {"builder": vpschema.RESULT_SCHEMA_DOC,
                      "infra": vpschema.RESULT_SCHEMA_DOC,
                      "junior": vpschema.FINDINGS_SCHEMA_DOC,
                      "senior": vpschema.REVIEW_SCHEMA_DOC,
                      "final": vpschema.REVIEW_SCHEMA_DOC}


def utc_ms():
    return vprunners.utc_ms()


def estimate_cost(pricing, runner, model, usage):
    """K-08: a runner that reports tokens but no USD (Codex) is priced from the
    roster `pricing` table. Returns (cost, est_cost, basis); `cost` is what the
    budget counts, `basis` says where it came from: reported | estimated |
    subscription (tokens priced, spend counted as 0) | unpriced.
    Codex output_tokens already include reasoning_output_tokens; cached input
    is a subset of input, so it is priced once at the cached rate."""
    usage = usage or {}
    if usage.get("cost") is not None:
        return usage["cost"], None, "reported"
    cfg = (pricing or {}).get(runner) or {}
    price = (cfg.get("models") or {}).get(model)
    if not price or usage.get("tokens_in") is None:
        return None, None, "unpriced"
    tin = float(usage.get("tokens_in") or 0)
    cached = min(float(usage.get("cache_read") or 0), tin)
    tout = float(usage.get("tokens_out") or 0)
    est = round(((tin - cached) * float(price.get("input_per_1m", 0))
                 + cached * float(price.get("cached_input_per_1m", 0))
                 + tout * float(price.get("output_per_1m", 0))) / 1e6, 6)
    if cfg.get("billing", "subscription") == "api":
        return est, est, "estimated"
    return 0.0, est, "subscription"


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Store -- vpctl over subprocess, always --json (the store's clock stamps rows)
# --------------------------------------------------------------------------

class StoreError(Exception):
    pass


class Store(object):
    def __init__(self, exec_, vpctl_cmd, run_root, cwd=None):
        self.exec = exec_
        self.vpctl_cmd = list(vpctl_cmd)
        self.run_root = Path(run_root)
        self.cwd = cwd
        self.calls = []
        self._lock = threading.Lock()

    def call(self, args, allow=(0,)):
        argv = list(self.vpctl_cmd) + ["--run-root", str(self.run_root)] + \
            [str(a) for a in args] + ["--json"]
        rc, out, err = self.exec.run(argv, cwd=self.cwd, timeout_s=120)
        with self._lock:
            self.calls.append((args[:2], rc))
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
                             % (" ".join(str(a) for a in args[:2]), rc,
                                (err or out).strip()[:400]))
        return rc, data

    # reads
    def report(self, what):
        _rc, data = self.call(["report", what])
        return data if isinstance(data, dict) else {}

    def report_items(self):
        items = self.report("items").get("items", [])
        return [i for i in items if isinstance(i, dict)]

    def unions(self):
        _rc, data = self.call(["union", "list"])
        return (data or {}).get("unions", []) if isinstance(data, dict) else []

    # writes
    def assign(self, item, group):
        return self.call(["assign", item, "--group", group], allow=(0, 3))

    def claim(self, item, role_id, worktree, expected_rev):
        return self.call(["claim", item, "--role", role_id, "--worktree", str(worktree),
                          "--expected-rev", str(expected_rev)])

    def pin(self, item, server):
        return self.call(["item", "pin", item, "--server", server])

    def block(self, item, reason):
        return self.call(["item", "block", item, "--reason", reason[:400]],
                         allow=(0, 3))

    def item_pause(self, item):
        return self.call(["item", "pause", item], allow=(0, 3))

    def turn_start(self, item, kind, session, server, agent, model, variant,
                   runner, driver_pid):
        args = ["turn", "start", item, "--kind", kind, "--session", session or "-",
                "--server", server or "-", "--agent", agent or "-", "--model",
                model or "-", "--variant", variant or "-", "--runner", runner,
                "--driver-pid", str(driver_pid)]
        _rc, data = self.call(args)
        return (data or {}).get("attempt_id")

    def turn_end(self, attempt_id, status, result_path, usage=None, detail="",
                 session=None):
        args = ["turn", "end", str(attempt_id), "--status", status,
                "--result", str(result_path or "-"), "--detail", (detail or "")[:400]]
        usage = usage or {}
        for flag, key in (("--tokens-in", "tokens_in"), ("--tokens-out", "tokens_out"),
                          ("--tokens-reason", "tokens_reason"),
                          ("--cache-read", "cache_read"), ("--cache-write", "cache_write"),
                          ("--cost", "cost")):
            if usage.get(key) is not None:
                args += [flag, str(usage[key])]
        if session:
            args += ["--session", session]
        return self.call(args)

    def submit_result(self, item, commit, result_path):
        return self.call(["submit-result", item, "--commit", commit,
                          "--result", str(result_path)])

    def findings(self, item, path, unverified_to_senior=False):
        args = ["findings", item, "--path", str(path)]
        if unverified_to_senior:
            args.append("--unverified-to-senior")
        _rc, data = self.call(args)
        return data or {}

    def review_record(self, item, subject, base, candidate, reviewer, reviewer_ref,
                      verdict, findings_path=None):
        args = ["review", "record", item, "--subject", subject, "--base", base,
                "--candidate", candidate, "--reviewer", reviewer,
                "--reviewer-ref", reviewer_ref, "--verdict", verdict]
        if findings_path:
            args += ["--findings", str(findings_path)]
        _rc, data = self.call(args)
        return (data or {}).get("review_id")

    def union_record(self, items, union_sha, base, worktree, branch, note=None):
        args = ["union", "record", "--items", ",".join(items), "--union", union_sha,
                "--base", base, "--worktree", str(worktree), "--branch", branch]
        if note:
            args += ["--note", note[:300]]
        _rc, data = self.call(args)
        return (data or {}).get("union_id")

    def union_status(self, union_id, status, note=None):
        args = ["union", "status", union_id, "--status", status]
        if note:
            args += ["--note", note[:300]]
        return self.call(args)

    def union_cancel(self, union_id, cause):
        _rc, data = self.call(["union", "cancel", union_id, "--cause", cause[:300]])
        return (data or {}).get("reset", []) if isinstance(data, dict) else []

    def proof_request(self, candidate, base, kind, paths):
        args = ["proof", "request", candidate, "--base", base, "--kind", kind]
        if paths:
            args += ["--paths", ",".join(paths)]
        _rc, data = self.call(args)
        return (data or {}).get("proof_id")

    def proof_record(self, proof_id, status, counts_path=None, artifacts=None,
                     pipeline_id=None, workflow_id=None):
        args = ["proof", "record", proof_id, "--status", status]
        if counts_path:
            args += ["--counts", str(counts_path)]
        if artifacts:
            args += ["--artifacts", str(artifacts)]
        if pipeline_id:
            args += ["--pipeline-id", str(pipeline_id)]
        if workflow_id:
            args += ["--workflow-id", str(workflow_id)]
        return self.call(args)

    def escalate(self, kind, item, attempt, detail):
        args = ["escalate", "--kind", kind, "--item", item or "-",
                "--detail", (detail or "")[:400]]
        if attempt:
            args += ["--attempt", str(attempt)]
        try:
            rc, _ = self.call(args, allow=(0, 2, 3))
            return rc == 0
        except StoreError:
            return False

    def alert(self, kind, text, item=None):
        args = ["alert", "--kind", kind, "--text", text[:500]]
        if item:
            args += ["--item", item]
        try:
            self.call(args)
            return True
        except StoreError:
            return False

    def reconcile(self, live_pids):
        _rc, data = self.call(["run", "reconcile", "--live-pids",
                               ",".join(str(p) for p in live_pids) or "0"])
        return data or {}

    def run_finish(self, reason):
        return self.call(["run", "finish", "--reason", reason], allow=(0, 3))

    def render(self):
        return self.call(["render"], allow=(0, 3))


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

class Driver(object):

    def __init__(self, roster_path, exec_=None, vpctl_cmd=None, store=None,
                 runners=None, interval=DEFAULT_INTERVAL, bins=None, circle_runner=None,
                 auto_assign=False, exit_on_stop=True, clock=None):
        self.roster_path = Path(roster_path).resolve()
        self.run_root = self.roster_path.parent
        self.roster = json.loads(self.roster_path.read_text(encoding="utf-8"))
        self.exec = exec_ or vprunners.Exec()
        self.interval = float(interval)
        self.bins = dict(bins or {})
        self.auto_assign = bool(auto_assign)
        self.exit_on_stop = bool(exit_on_stop)
        self.clock = clock or time.time

        here = Path(__file__).resolve().parent
        self.here = here
        self.vpctl_cmd = list(vpctl_cmd) if vpctl_cmd else [sys.executable, str(here / "vpctl.py")]
        self.store = store or Store(self.exec, self.vpctl_cmd, self.run_root, cwd=str(here))

        run = self.roster.get("run", {})
        self.cn = Path(os.path.expanduser(run.get("cn") or self.roster.get("cn") or
                                          str(self.run_root.parents[2])))
        self.trunk = Path(os.path.expanduser(run.get("trunk") or self.roster["trunk"]))
        self.worktrees_root = Path(os.path.expanduser(run.get("worktrees") or
                                                      self.roster["worktrees"]))
        self.stop_file = self.run_root / (run.get("stop_file") or "STOP")
        self.stop_grace_s = int(run.get("stop_grace_s", 120))
        self.heartbeat_path = self.run_root / "driver.heartbeat"
        self.turns_root = self.run_root / "turns"
        self.costs_path = self.run_root / "costs.jsonl"
        self.log_path = self.run_root / "driver.log"

        self.night = self.roster.get("night", {})
        self.conc = self.roster.get("concurrency", {})
        self.budget = self.roster.get("budget", {})
        self.pricing = self.roster.get("pricing", {})
        self.backoff = self.roster.get("backoff", {})
        self.roles = self.roster.get("roles", {})
        self.proof_cfg = self.roster.get("proof", {})
        self.circle_runner = circle_runner        # tests inject a fake; None -> vpcircle.Runner()
        self.groups = {str(k): (v if isinstance(v, dict) else {"server": v})
                       for k, v in self.roster.get("groups", {}).items()}

        self.servers = {}
        for name, cfg in self.roster.get("servers", {}).items():
            url = cfg.get("url") or ("http://127.0.0.1:%d" % int(cfg.get("port", 0)))
            self.servers[name] = {
                "name": name, "url": url,
                "xdg": os.path.expanduser(cfg.get("xdg_data_home") or cfg.get("data") or ""),
                "max_concurrent": int(cfg.get("max_concurrent", 2)),
                "active": 0, "parked_until": 0.0, "park_reason": None,
                "park_status": None,
            }
        self.runner_state = {
            "opencode": {"active": 0, "max": 99, "parked_until": 0.0, "park_reason": None},
            "codex": {"active": 0, "max": int(self.conc.get("codex_max", 2)),
                      "parked_until": 0.0, "park_reason": None},
            "claude": {"active": 0, "max": int(self.conc.get("claude_max", 2)),
                       "parked_until": 0.0, "park_reason": None},
            "proof": {"active": 0, "max": int(self.conc.get("max_proofs_in_flight", 1)),
                      "parked_until": 0.0, "park_reason": None},
        }
        self.runners = runners or {
            "opencode": vprunners.OpenCodeRunner(self.exec, self.bins.get("opencode", "opencode")),
            "codex": vprunners.CodexRunner(self.exec, self.bins.get("codex", "codex")),
            "claude": vprunners.ClaudeRunner(self.exec, self.bins.get("claude", "claude")),
        }
        self.git_bin = self.bins.get("git", "git")

        self._lock = threading.Lock()
        self._live_items = set()
        self._threads = []
        self._fail = {}
        self._unknown_rerun = {}      # item -> (rev, commit) already re-graded once
        self._delivery_failed = set() # items whose last DONE turn struck at delivery
        self._stopping = False
        self._stop_started = None
        self._abort = threading.Event()
        self._reconciled = False
        self._trunk_sha_seen = None
        self._alerted = set()
        self._proof_backoff = {}
        self._proof_infra_n = {}
        self._proof_cancel = set()    # union shas whose proof result must be discarded (D83)
        self._box_active = 0          # box proof legs running (D103 overflow routing)
        self._union_lock = threading.Lock()
        self._last_union_mono = time.monotonic()
        self._proofs_paused = False
        self._budget_stop = False
        self.tick_count = 0
        self._child_pids = set()

    # -- logging ------------------------------------------------------------

    def log(self, msg):
        line = "%s %s" % (utc_ms(), msg)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def alert_once(self, key, kind, text, item=None):
        if key in self._alerted:
            return
        self._alerted.add(key)
        self.store.alert(kind, text, item)
        self.log("ALERT %s %s" % (kind, text))

    def alert_store_down(self, key, text):
        """The store itself is refusing; store.alert would be swallowed, so the
        owner line is appended directly when the store cannot take it."""
        if key in self._alerted:
            return
        self._alerted.add(key)
        self.log("ALERT STORE_UNAVAILABLE %s" % text)
        if self.store.alert("STORE_UNAVAILABLE", text):
            return
        line = "- %s **STORE_UNAVAILABLE** — %s (line written by the driver; the store " \
               "could not record it)\n" % (utc_ms(), text[:500])
        try:
            with open(self.run_root / "OWNER-ALERTS.md", "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass

    # -- clock / windows ----------------------------------------------------

    def ist_now(self):
        return datetime.fromtimestamp(self.clock(), tz=IST)

    def _after(self, hhmm):
        if not hhmm:
            return False
        h, m = [int(x) for x in str(hhmm).split(":")]
        now = self.ist_now()
        # the window is "after HH:MM in the morning" -- only meaningful 00:00-12:00
        return now.hour < 12 and (now.hour, now.minute) >= (h, m)

    def dispatch_open(self):
        return not self._after(self.night.get("last_dispatch"))

    def proofs_open(self):
        return not self._after(self.night.get("last_proof")) and not self._proofs_paused

    def drain_due(self):
        return self._after(self.night.get("close_out"))

    # -- disk / budget ------------------------------------------------------

    def disk_gb(self):
        try:
            st = shutil.disk_usage(str(self.cn))
            return st.free / (1024 ** 3)
        except OSError:
            return None

    def spent_usd(self, item=None):
        total = 0.0
        try:
            with open(self.costs_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if item and rec.get("item") != item:
                        continue
                    total += float(rec.get("cost") or 0.0)
        except OSError:
            pass
        return round(total, 4)

    def _append_cost(self, rec):
        try:
            with self._lock:
                with open(self.costs_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")
        except OSError:
            pass

    # -- servers / runners --------------------------------------------------

    def server_for_group(self, group_no):
        g = self.groups.get(str(group_no)) or self.groups.get("infra") or {}
        name = g.get("server")
        if name is None and self.servers:
            name = sorted(self.servers)[0]
        return name

    def _parked(self, state):
        return state["parked_until"] > time.monotonic()

    def _park(self, state, minutes, reason, status, key, what):
        state["parked_until"] = time.monotonic() + minutes * 60
        state["park_reason"] = reason
        state["park_status"] = status
        self.log("PARK %s %s for %dm: %s" % (what, status, minutes, reason))
        self.store.alert("PARKED", "%s %s parked %d min: %s" % (status, what, minutes,
                                                              reason[:200]))

    def _try_acquire(self, server, runner):
        with self._lock:
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

    def park_after(self, outcome, server, runner):
        """K-15: park the server (opencode) or the runner (codex/claude)."""
        st = outcome.status
        mins = None
        rm = outcome.usage.get("reset_minutes") if outcome.usage else None
        if st == "QUOTA_WEEKLY":
            mins = int(self.backoff.get("weekly_park_min", 6 * 60))
        elif st == "QUOTA_ROLLING":
            mins = (rm or 60) + int(self.backoff.get("quota_rolling_extra_min", 2))
        elif st == "RATE":
            mins = int(self.backoff.get("rate_min", 20))
        elif st == "DEGRADED":
            mins = int(self.backoff.get("degraded_min", 10))
        elif st == "AUTH":
            mins = int(self.backoff.get("auth_park_min", 120))
        if mins is None:
            return
        what = server if (runner == "opencode" and server) else runner
        state = self.servers[server] if (runner == "opencode" and server) else \
            self.runner_state[runner]
        self._park(state, mins, outcome.detail, st, what, what)

    def probe(self, runner, server=None):
        """Before unpark (K-15): cheap liveness; True = ok to unpark."""
        if runner == "opencode" and server:
            return self._http_ok(self.servers[server]["url"] + "/session")
        if runner == "codex":
            return self._http_ok(self.roster.get("codex", {}).get(
                "router_health", "http://127.0.0.1:4202/health"))
        if runner == "claude":
            spec = TurnSpec("probe", "-", str(self.run_root), PROBE_PROMPT,
                            self.roles.get("integrator", {}).get("model", "sonnet"),
                            max_turns=1, max_budget_usd=0.2, timeout_s=120,
                            log_dir=str(self.run_root / "probes"), tag=utc_ms())
            out = self.runners["claude"].run(spec)
            return out.status in (STATUS_DONE, STATUS_INCOMPLETE) and "PONG" in (out.text or "")
        return True

    def _http_ok(self, url, timeout=5.0):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.status < 500
        except Exception:
            return False

    def _maybe_unpark(self):
        for name, srv in self.servers.items():
            if srv["park_reason"] and not self._parked(srv):
                if self.probe("opencode", name):
                    self.log("UNPARK %s" % name)
                    srv["park_reason"] = None
                    srv["park_status"] = None
                else:
                    srv["parked_until"] = time.monotonic() + 300
        for name, rs in self.runner_state.items():
            if rs["park_reason"] and not self._parked(rs):
                if self.probe(name):
                    self.log("UNPARK runner %s" % name)
                    rs["park_reason"] = None
                else:
                    rs["parked_until"] = time.monotonic() + 300

    # -- per-item driver-side failure backoff -------------------------------

    def item_is_backed_off(self, item, rev):
        with self._lock:
            ent = self._fail.get(item)
            if ent is None:
                return False
            if ent.get("rev") != rev:
                self._fail.pop(item, None)
                return False
            if ent.get("stuck"):
                return True
            return time.monotonic() < ent.get("next_try", 0.0)

    def note_item_failure(self, item, rev, detail, attempt=None, kind="STALLED"):
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
        self.log("FAIL %s %d/%d %s" % (item, count, FAIL_CAP, detail[:200]))
        if count >= FAIL_CAP:
            self.store.block(item, "%d consecutive failures: %s" % (count, detail[:300]))
            self.store.alert("BLOCKED", "%s blocked after %d failures: %s"
                             % (item, count, detail[:200]), item)
        else:
            self.store.escalate(kind, item, attempt, "failure %d/%d: %s"
                                % (count, FAIL_CAP, detail))
        return count

    def clear_item_failures(self, item):
        with self._lock:
            self._fail.pop(item, None)

    # -- git helpers --------------------------------------------------------

    def git(self, args, cwd=None, timeout_s=300):
        return self.exec.run([self.git_bin] + list(args), cwd=cwd, timeout_s=timeout_s)

    def head_sha(self, path):
        rc, out, _ = self.git(["-C", str(path), "rev-parse", "HEAD"])
        return out.strip() if rc == 0 and out.strip() else None

    def trunk_sha(self):
        return self.head_sha(self.trunk)

    # -- worktree -----------------------------------------------------------

    def worktree_path(self, item):
        return self.worktrees_root / item

    def ensure_worktree(self, rec):
        item = rec["item"]
        wt = Path(rec.get("worktree") or self.worktree_path(item))
        if wt.exists() and not (wt / ".git").exists():
            raise StoreError("worktree path %s exists but is not a worktree (K-23)" % wt)
        if not wt.exists():
            base = rec.get("base_sha")
            if not base:
                raise StoreError("item %s has no base_sha" % item)
            rc, _out, _ = self.git(["-C", str(self.trunk), "cat-file", "-e",
                                    base + "^{commit}"])
            if rc != 0:
                raise StoreError("item %s base %s is not a commit in trunk" % (item, base))
            wt.parent.mkdir(parents=True, exist_ok=True)
            rc, out, err = self.git(["-C", str(self.trunk), "worktree", "add",
                                     str(wt), "-b", "vp/%s" % item, base])
            if rc != 0 and not wt.exists():
                raise StoreError("worktree add failed for %s: %s"
                                 % (item, (err or out).strip()[:300]))
        for rel in ("agent/.venv", "portal/node_modules", "platform/.venv"):
            link = wt / rel
            target = self.trunk / rel
            try:
                link.parent.mkdir(parents=True, exist_ok=True)
                if not link.exists() and not link.is_symlink() and target.exists():
                    os.symlink(str(target), str(link))
            except OSError:
                pass
        self._write_vp_files(wt, rec)
        return wt

    def _write_vp_files(self, wt, rec):
        vp = wt / ".vp"
        vp.mkdir(parents=True, exist_ok=True)
        for key, name in (("packet_path", "PACKET.md"), ("benchmark_path", "BENCHMARK.md")):
            src = rec.get(key)
            dst = vp / name
            if src and Path(src).exists() and Path(src).stat().st_size > 0:
                dst.write_text(Path(src).read_text(encoding="utf-8"), encoding="utf-8")
            elif dst.exists() and dst.stat().st_size > 0:
                pass
            else:
                raise StoreError("item %s: no usable %s (%r)" % (rec.get("item"), name, src))
        for name, doc in (("RESULT_SCHEMA.json", vpschema.RESULT_SCHEMA_DOC),
                          ("FINDINGS_SCHEMA.json", vpschema.FINDINGS_SCHEMA_DOC),
                          ("REVIEW_SCHEMA.json", vpschema.REVIEW_SCHEMA_DOC)):
            onfile = self.here / "schemas" / name
            text = onfile.read_text(encoding="utf-8") if onfile.exists() \
                else json.dumps(doc, indent=2)
            (vp / name).write_text(text, encoding="utf-8")
        if rec.get("base_sha"):
            (vp / "BASE").write_text(rec["base_sha"] + "\n", encoding="utf-8")
        # keep .vp out of the product diff: git reads info/exclude from the
        # COMMON dir (shared by every worktree of the trunk), not the worktree's
        # own gitdir.
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

    def packet_header(self, rec):
        p = rec.get("packet_path")
        if not p or not Path(p).exists():
            return {}
        hdr, _ = vplint.parse_front_matter(Path(p).read_text(encoding="utf-8"))
        return hdr or {}

    # -- auto-assign (K-14) ---------------------------------------------------

    def auto_assign_tick(self, items):
        if not self.auto_assign or self._stopping or self._budget_stop:
            return 0
        if not self.dispatch_open():
            return 0
        by_item = {r["item"]: r for r in items}
        ready = [r for r in items if (r.get("status") == "READY" and r.get("packet_id"))]

        def key(r):
            h = self.packet_header(r)
            return (0 if h.get("critical") else 1, r.get("ts") or "")

        n = 0
        wip_cap = int(self.conc.get("wip_per_group", 2))
        active = ("ASSIGNED", "BUILDING", "GRADING", "JUNIOR_SATISFIED")
        wip = {}
        for r in items:
            if r.get("status") in active and r.get("group_no") is not None:
                wip[str(r["group_no"])] = wip.get(str(r["group_no"]), 0) + 1
        for r in sorted(ready, key=key):
            hdr = self.packet_header(r)
            deps = hdr.get("depends_on") or []
            ok = True
            for d in deps:
                st = (by_item.get(d) or {}).get("status")
                if st not in ("PROMOTED", "APPROVED"):
                    ok = False
                    break
            if not ok:
                continue
            if not self._deps_in_base(r, hdr, deps, by_item):
                continue
            group = hdr.get("group") or r.get("group_no")
            if group is None:
                continue
            server = self.server_for_group(group)
            if not server or self._parked(self.servers[server]):
                continue
            if wip.get(str(group), 0) >= wip_cap:
                continue  # group full; try next tick, no store call, no log line
            rc, data = self.store.assign(r["item"], group)
            if rc == 0:
                self.store.pin(r["item"], server)
                self.log("ASSIGN %s -> group %s (%s)" % (r["item"], group, server))
                wip[str(group)] = wip.get(str(group), 0) + 1
                n += 1
            else:
                key_ = "assign-refused:%s" % r["item"]
                if key_ not in self._alerted:
                    self._alerted.add(key_)
                    self.log("assign refused %s: %s" % (r["item"], json.dumps(data)[:200]))
        return n

    def _deps_in_base(self, r, hdr, deps, by_item):
        """D75: a packet whose base_sha predates a PROMOTED/APPROVED
        dependency's candidate sends the builder into a precondition STOP
        (A3-3 looped six times).  Refuse to assign, alert DEP_BASE_STALE
        once per (item, dep, base); the orchestrator re-bases by hand with a
        BASE NOTE (packets carry measured line numbers and counts, so an
        automatic base swap would only move the staleness into the literals)."""
        base = str(hdr.get("base_sha") or r.get("base_sha") or "")
        if not base:
            return True
        for d in deps:
            cand = (by_item.get(d) or {}).get("candidate_sha")
            if not cand:
                continue
            rc, _out, err = self.git(["-C", str(self.trunk), "merge-base", "--is-ancestor",
                                      cand, base])
            if rc == 0:
                continue
            if rc != 1:
                self.log("dep base check %s/%s rc=%s: %s" % (r["item"], d, rc, (err or "")[:120]))
                continue
            self.alert_once("dep-base:%s:%s:%s" % (r["item"], d, base[:12]), "DEP_BASE_STALE",
                            "%s: packet base %s does not contain %s's candidate %s; not "
                            "assigning -- re-base the packet (BASE NOTE) and re-submit"
                            % (r["item"], base[:12], d, cand[:12]))
            return False
        return True

    # -- the tick -------------------------------------------------------------

    def stop_requested(self):
        return self.stop_file.exists()

    def write_heartbeat(self, extra=None):
        with self._lock:
            live = sorted(self._live_items)
            servers = {n: {"active": s["active"], "parked": self._parked(s),
                           "park_status": s["park_status"], "park_reason": s["park_reason"]}
                       for n, s in self.servers.items()}
            runners = {n: {"active": s["active"], "parked": self._parked(s),
                           "park_reason": s["park_reason"]}
                       for n, s in self.runner_state.items()}
        payload = {
            "ts": utc_ms(), "mono": time.monotonic(), "pid": os.getpid(),
            "tick": self.tick_count, "stopping": bool(self._stopping),
            "run_root": str(self.run_root), "live_items": live,
            "active_by_server": {n: s["active"] for n, s in servers.items()},
            "servers": servers, "runners": runners,
            "budget": {"spent_usd": self.spent_usd(),
                       "max_usd": self.budget.get("max_cost_usd_per_run"),
                       "stopped": self._budget_stop},
            "disk_gb": round(self.disk_gb() or 0, 1),
            "proofs_open": self.proofs_open(), "dispatch_open": self.dispatch_open(),
            "ist": self.ist_now().strftime("%H:%M"),
            "trunk_sha": self._trunk_sha_seen,
        }
        if extra:
            payload.update(extra)
        try:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.heartbeat_path.with_suffix(".heartbeat.tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(str(tmp), str(self.heartbeat_path))
        except OSError:
            pass
        return payload

    def _guards(self):
        """Disk, budget, trunk movement.  Returns False when nothing may start."""
        gb = self.disk_gb()
        floor = float(self.night.get("pause_on_disk_gb", 15))
        if gb is not None and gb < floor:
            if not self._proofs_paused:
                self._proofs_paused = True
                self.store.alert("DISK", "free disk %.1f GB < %.0f GB: proofs and new "
                                 "worktrees paused" % (gb, floor))
        elif self._proofs_paused:
            self._proofs_paused = False
            self.log("disk recovered: %.1f GB" % gb)
        cap = self.budget.get("max_cost_usd_per_run")
        if cap is not None and self.spent_usd() >= float(cap):
            if not self._budget_stop:
                self._budget_stop = True
                self.store.alert("BUDGET", "run budget %.2f USD reached: no new turns"
                                 % float(cap))
        sha = self.trunk_sha()
        if sha:
            if self._trunk_sha_seen is None:
                self._trunk_sha_seen = sha
            elif sha != self._trunk_sha_seen:
                self.alert_once("trunk:" + sha, "TRUNK_MOVED",
                                "trunk HEAD %s -> %s while the run is live"
                                % (self._trunk_sha_seen[:12], sha[:12]))
                self._trunk_sha_seen = sha
        return not self._budget_stop

    def tick(self):
        self.tick_count += 1
        if self.stop_requested() and not self._stopping:
            self._stopping = True
            self._stop_started = time.monotonic()
            self.log("STOP file seen; grace %ds" % self.stop_grace_s)
        if not self._reconciled:
            try:
                res = self.store.reconcile([os.getpid()])
            except StoreError as exc:
                self.log("reconcile failed: %s" % exc)
                self.alert_once("reconcile", "RECONCILE_FAILED",
                                "reconcile of RUNNING attempts failed (%s); attempts left by "
                                "a dead driver stay RUNNING; retrying every tick"
                                % str(exc)[:200])
            else:
                self._reconciled = True
                if res.get("orphaned"):
                    self.log("RECONCILE orphaned %s" % json.dumps(res["orphaned"])[:400])
                    self.store.alert("ORPHANED", "%d RUNNING attempts from a dead driver "
                                     "marked ORPHANED" % len(res["orphaned"]))
        self.write_heartbeat()
        if self._stopping:
            self._stop_step()
            return 0
        if self.drain_due():
            if not self._stop_started:
                self.log("close_out reached; draining")
                self.store.alert("CLOSE_OUT", "night close-out reached; draining")
                self._stopping = True
                self._stop_started = time.monotonic()
            return 0
        may_start = self._guards()
        self._maybe_unpark()
        try:
            items = self.store.report_items()
        except StoreError as exc:
            self.log("report items failed: %s" % exc)
            self.alert_store_down("store:report_items",
                                  "report items failed (%s); the driver idles every tick "
                                  "until the store answers again" % str(exc)[:200])
            return 0
        self._alerted.discard("store:report_items")
        if may_start:
            self.auto_assign_tick(items)
            try:
                items = self.store.report_items()
            except StoreError as exc:
                self.log("report items failed: %s" % exc)
                self.alert_store_down("store:report_items",
                                      "report items failed (%s); the driver idles every "
                                      "tick until the store answers again" % str(exc)[:200])
                return 0
        spawned = 0
        for rec in self._fair_order(items):
            if self._stopping or not may_start:
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
                if self._proofs_paused:
                    continue
                try:
                    wt = self.ensure_worktree(rec)
                    self.store.claim(item, "builder%s" % rec.get("group_no"), wt, rec["rev"])
                    self.log("CLAIM %s %s" % (item, wt))
                except StoreError as exc:
                    self.note_item_failure(item, rec.get("rev"), str(exc))
                continue
            if status == "PREPARING":
                continue          # handled in bulk below
            role = ROLE_FOR_STATUS.get(status)
            if role is None:
                continue
            if rec.get("running_attempt"):
                continue
            if role == "proof":
                if not self.proofs_open():
                    continue
                if rec.get("open_proof") and self._proof_thread_live(rec.get("candidate_sha")):
                    continue
            if role == "final" and rec.get("union_id"):
                # one final review per union: skip if a sibling item is already live
                sibs = [r["item"] for r in items
                        if r.get("union_id") == rec.get("union_id") and r["item"] != item]
                with self._lock:
                    if any(x in self._live_items for x in sibs):
                        continue
            if self._spawn_role(rec, role):
                spawned += 1
        if may_start and not self._stopping:
            self._reconcile_chain()
            self._maybe_build_union(items)
        return spawned

    def _fair_order(self, items):
        """K-22: lowest round first, then oldest activity."""
        return sorted(items, key=lambda r: (int(r.get("round") or 0), r.get("ts") or ""))

    # -- role spawning ----------------------------------------------------------

    def _runner_for_role(self, role):
        return (self.roles.get(role) or {}).get("runner") or (
            "opencode" if role in ("builder", "junior", "infra") else
            "codex" if role == "senior" else "claude")

    def _spawn_role(self, rec, role):
        item = rec["item"]
        runner = "proof" if role == "proof" else self._runner_for_role(role)
        server = None
        if runner == "opencode":
            server = rec.get("pinned_server") or self.server_for_group(rec.get("group_no"))
            if server not in self.servers:
                return False
        if not self._try_acquire(server, runner):
            return False
        with self._lock:
            self._live_items.add(item)
        th = threading.Thread(target=self._turn_thread, args=(dict(rec), role, server, runner),
                              name="vp-%s-%s" % (role, item), daemon=True)
        self._threads.append(th)
        th.start()
        return True

    def _turn_thread(self, rec, role, server, runner):
        item = rec["item"]
        try:
            if role == "proof":
                self.run_proof(rec)
                self.clear_item_failures(item)
            else:
                self._delivery_failed.discard(item)
                out = self.run_turn(rec, role, server, runner)
                # a DONE turn whose delivery struck (no commit, invalid record)
                # must keep its count, or three strikes never arrive (A3-3
                # looped on 1/3 for six attempts)
                if out is not None and out.status == STATUS_DONE and \
                        item not in self._delivery_failed:
                    self.clear_item_failures(item)
        except Exception as exc:          # never kill the daemon
            try:
                self.note_item_failure(item, rec.get("rev"), "%s: %s" % (type(exc).__name__, exc))
            except Exception:
                pass
            self.log("EXC %s %s: %s" % (item, role, exc))
        finally:
            self._release(server, runner)
            with self._lock:
                self._live_items.discard(item)

    def _proof_thread_live(self, candidate):
        return any(t.is_alive() and t.name == "vp-proof-%s" % candidate for t in self._threads)

    # -- one turn ----------------------------------------------------------------

    def _reload_roster_if_changed(self):
        """Hot-reload roles/budget/concurrency/proof from the roster file (mtime)."""
        try:
            m = self.roster_path.stat().st_mtime
        except OSError:
            return
        if m == getattr(self, "_roster_mtime", None):
            return
        try:
            data = json.loads(self.roster_path.read_text(encoding="utf-8"))
        except ValueError:
            return
        self._roster_mtime = m
        self.roster = data
        self.roles = data.get("roles", {})
        self.budget = data.get("budget", {})
        self.pricing = data.get("pricing", {})
        self.conc = data.get("concurrency", {})
        self.proof_cfg = data.get("proof", {})
        with self._lock:
            self.runner_state["proof"]["max"] = int(self.conc.get("max_proofs_in_flight", 1))
        self.log("roster reloaded (mtime changed)")

    def _spec_for(self, rec, role, server, runner, wt, prompt, session_id, tag, n):
        self._reload_roster_if_changed()
        rcfg = self.roles.get(role) or {}
        out_path = wt / ".vp" / OUTPUT_OF_ROLE[role]
        mm = self.conc.get("max_minutes_per_turn", {})
        timeout_s = float(mm.get(KIND_OF_ROLE[role], 45)) * 60
        log_dir = self.turns_root / rec["item"] / tag
        base = dict(variant=rcfg.get("variant"), agent=rcfg.get("agent"),
                    session_id=session_id, out_path=str(out_path),
                    timeout_s=timeout_s, log_dir=str(log_dir), tag=str(n),
                    validator=VALIDATOR_OF_ROLE.get(role),
                    title="%s %s r%s" % (rec["item"], KIND_OF_ROLE[role], rec.get("round", 0)))
        if runner == "opencode":
            srv = self.servers[server]
            return TurnSpec(role, rec["item"], wt, prompt, rcfg.get("model"),
                            server_url=srv["url"], xdg_data_home=srv["xdg"],
                            expect_fence=(role == "junior"), **base)
        if runner == "codex":
            model = rcfg.get("model")
            if rec.get("critical") and rcfg.get("model_critical"):
                model = rcfg["model_critical"]
            return TurnSpec(role, rec["item"], wt, prompt, model, effort=rcfg.get("effort"),
                            sandbox=rcfg.get("sandbox", "read-only"),
                            schema_path=str(self.here / "schemas" / "REVIEW_SCHEMA.json"),
                            **base)
        # claude
        effort = rcfg.get("effort")
        if rec.get("critical") and rcfg.get("effort_critical"):
            effort = rcfg["effort_critical"]
        settings = self.run_root / "claude-settings" / ("%s.json" % role)
        allowed = []
        if settings.exists():
            try:
                allowed = json.loads(settings.read_text(encoding="utf-8")).get(
                    "permissions", {}).get("allow", [])
            except ValueError:
                allowed = []
        schema_doc = SCHEMA_DOC_OF_ROLE.get(role, vpschema.REVIEW_SCHEMA_DOC)
        model = rcfg.get("model", "opus")
        if rec.get("critical") and rcfg.get("model_critical"):
            model = rcfg["model_critical"]
        return TurnSpec(role, rec["item"], wt, prompt, model,
                        effort=effort, schema_text=json.dumps(schema_doc),
                        settings_path=str(settings) if settings.exists() else None,
                        allowed_tools=allowed, max_turns=rcfg.get("max_turns", 30),
                        max_budget_usd=self.budget.get("claude_max_budget_usd_per_call", 3),
                        add_dirs=[str(self.run_root)], **base)

    def run_turn(self, rec, role, server, runner):
        item = rec["item"]
        wt = Path(rec.get("worktree") or self.worktree_path(item))
        if role == "final":
            wt = self._union_worktree_for(rec)
        if not wt.exists():
            raise StoreError("worktree missing for %s: %s" % (item, wt))
        self._write_vp_files(wt, rec) if role in ("builder", "junior") else None
        out_path = wt / ".vp" / OUTPUT_OF_ROLE[role]
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass
        if role == "senior":
            self._write_review_request(wt, rec)
        if role == "final":
            self._write_final_request(wt, rec)
        rcfg = self.roles.get(role) or {}
        kind = KIND_OF_ROLE[role]
        model = rcfg.get("model")
        # the tick's item snapshot can be stale by a transition (a senior's
        # review+findings moved SEC-01d1 GRADING->BUILDING 36 ms before a
        # grader spawned on the GRADING snapshot); re-read before spending a turn
        if role in ROLE_FOR_STATUS.values():
            fresh = next((r for r in self.store.report_items() if r["item"] == item), None)
            if fresh and ROLE_FOR_STATUS.get(fresh.get("status")) != role:
                self.log("SKIP %s %s: status is %s now" % (item, role, fresh.get("status")))
                return
        attempt_id = self.store.turn_start(item, kind, rec.get("session_id"), server,
                                           rcfg.get("agent"), model, rcfg.get("variant"),
                                           runner, os.getpid())
        sid = rec.get("session_id") if role in ("builder", "junior") else None
        n, resumes, outcome = 0, 0, None
        while True:
            n += 1
            prompt = PROMPT_OF_ROLE[role] if n == 1 else RESUME_PROMPT
            if role == "junior" and n == 1:
                hint = self._unknown_rerun.get(item)
                if hint and hint[0] == rec.get("rev"):
                    prompt += JUNIOR_RERUN_HINT % (", ".join(hint[2]), hint[3][:200])
            spec = self._spec_for(rec, role, server, runner, wt, prompt, sid, attempt_id, n)
            outcome = self.runners[runner].run(spec, abort_flag=self._abort)
            sid = outcome.session_id or sid
            if self._stopping and outcome.status != STATUS_DONE:
                outcome.status, outcome.detail = STATUS_ABORTED, "STOP"
            cost, est_cost, basis = estimate_cost(self.pricing, runner, spec.model,
                                                  outcome.usage)
            if basis != "reported" and outcome.usage is not None:
                outcome.usage["cost"] = cost
            if basis == "unpriced" and (outcome.usage or {}).get("tokens_in") is not None:
                self.alert_once("pricing:%s:%s" % (runner, spec.model), "UNPRICED",
                                "%s model %s reports tokens but no USD and the roster has "
                                "no pricing.%s.models[%s]; its spend counts as nothing "
                                "toward the run budget" % (runner, spec.model, runner,
                                                            spec.model))
            self._write_turn_record(rec, attempt_id, n, role, runner, server, spec, outcome)
            self._append_cost({"ts": utc_ms(), "item": item, "attempt": attempt_id, "n": n,
                               "role": role, "runner": runner, "model": spec.model,
                               "server": server, "cost": cost or 0.0,
                               "est_cost_usd": est_cost, "cost_basis": basis,
                               "tokens_in": (outcome.usage or {}).get("tokens_in"),
                               "tokens_out": (outcome.usage or {}).get("tokens_out"),
                               "cache_read": (outcome.usage or {}).get("cache_read"),
                               "status": outcome.status, "duration_s": outcome.duration_s})
            if outcome.status == STATUS_PROGRESS_STOP and not self._stopping \
                    and runner == "opencode" and resumes < MAX_RESUMES:
                resumes += 1
                continue
            break
        if runner == "opencode" and sid and outcome.status != STATUS_ABORTED:
            exp = self.runners["opencode"].export(
                spec, sid, str(self.turns_root / item / attempt_id / ("%d-export.json" % n)))
            outcome.export_path = exp["path"]
            if exp.get("truncated_suspect"):
                self.log("export of %s is exactly 65536 bytes -- suspect" % sid)
        status = outcome.status
        if status == STATUS_PROGRESS_STOP:
            status = "STUCK"
        self.store.turn_end(attempt_id, status, outcome.record_path or outcome.log_path,
                            outcome.usage, outcome.detail, session=sid)
        self.log("TURN %s %s %s -> %s %s" % (item, role, attempt_id, status, outcome.detail[:120]))
        if outcome.status == STATUS_DONE:
            self._deliver(rec, role, wt, Path(outcome.record_path), attempt_id, outcome)
            return outcome
        if outcome.status == STATUS_ABORTED:
            return outcome
        if outcome.status in PARK_STATUSES:
            self.park_after(outcome, server, runner)
            return outcome
        if outcome.status == STATUS_REFUSED and role == "senior" and \
                (self.roles.get("senior") or {}).get("fallback_to_claude"):
            self.store.escalate("RUNNER_REFUSED", item, attempt_id, outcome.detail)
            self.store.alert("RUNNER_REFUSED", "%s senior review refused by codex; "
                             "falling back to claude" % item, item)
            return self._senior_fallback(rec, wt)
        if outcome.status == "RUNNER_DENIED":
            self.store.block(item, "role %s denied a tool it needed: %s"
                             % (role, outcome.detail[:300]))
            self.store.alert("RUNNER_DENIED", "%s %s: %s" % (item, role, outcome.detail[:200]),
                             item)
            return outcome
        per_item_cap = self.budget.get("max_cost_usd_per_item")
        if per_item_cap is not None and self.spent_usd(item) >= float(per_item_cap):
            self.store.block(item, "item budget %.2f USD reached" % float(per_item_cap))
            self.store.alert("BUDGET", "%s reached its item budget" % item, item)
            return outcome
        self.note_item_failure(item, rec.get("rev"), "%s: %s" % (status, outcome.detail),
                               attempt_id, kind=status if status in
                               ("RUNNER_TIMEOUT", "RUNNER_CRASH", "RUNNER_REFUSED",
                                "MODEL_MISMATCH", "SESSION_MISMATCH", "RECORD_INVALID",
                                "INCOMPLETE", "STUCK") else "STALLED")
        return outcome

    def _senior_fallback(self, rec, wt):
        """O10: route the senior review to Claude Opus headless (recorded as
        runner=claude, reviewer 'claude-fallback')."""
        rec = dict(rec)
        self.roles.setdefault("senior_fallback", dict(self.roles.get("final") or {},
                                                      runner="claude"))
        saved = self.roles.get("senior")
        self.roles["senior"] = dict(self.roles["senior_fallback"], runner="claude")
        try:
            if not self._try_acquire(None, "claude"):
                return None
            try:
                return self.run_turn(rec, "senior", None, "claude")
            finally:
                self._release(None, "claude")
        finally:
            self.roles["senior"] = saved

    # -- records / requests ---------------------------------------------------

    def _write_turn_record(self, rec, attempt_id, n, role, runner, server, spec, outcome):
        d = self.turns_root / rec["item"] / str(attempt_id)
        d.mkdir(parents=True, exist_ok=True)
        payload = outcome.to_dict()
        payload.update({
            "item": rec["item"], "attempt": attempt_id, "n": n, "role": role,
            "runner": runner, "server": server, "round": rec.get("round", 0),
            "model": spec.model, "variant": spec.variant, "effort": spec.effort,
            "prompt_sha256": sha256_text(spec.prompt), "cwd": spec.cwd,
            "written_ts": utc_ms(),
        })
        path = d / ("%d-%s.json" % (n, role))
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str),
                        encoding="utf-8")
        return path

    def _benchmark_ids(self, wt):
        b = wt / ".vp" / "BENCHMARK.md"
        try:
            return vplint.benchmark_ids(b)
        except OSError:
            return []

    def _write_review_request(self, wt, rec):
        base = rec.get("base_sha") or (wt / ".vp" / "BASE").read_text().strip()
        cand = rec.get("candidate_sha") or self.head_sha(wt)
        rcfg = self.roles.get("senior") or {}
        model = rcfg.get("model_critical") if (rec.get("critical") and rcfg.get("model_critical")) \
            else rcfg.get("model")
        runner_name = rcfg.get("runner", "codex")
        if runner_name == "claude":
            runner_name = "claude-fallback"       # O10: independence reduced, alerted
        unverified = []
        try:
            _doc, _fails, unverified = findings_verdicts(wt / ".vp" / "FINDINGS.json")
        except (OSError, ValueError):
            pass
        req = {"item": rec["item"], "subject": "item", "base": base, "candidate": cand,
               "reviewer": "%s:%s" % (runner_name, model),
               "benchmark_ids": self._benchmark_ids(wt),
               "unverified_ids": unverified,
               "files": {"packet": ".vp/PACKET.md", "benchmark": ".vp/BENCHMARK.md",
                         "result": ".vp/RESULT.json", "findings": ".vp/FINDINGS.json"},
               "ts": utc_ms()}
        (wt / ".vp" / "REVIEW_REQUEST.json").write_text(json.dumps(req, indent=2, sort_keys=True),
                                                        encoding="utf-8")
        return req

    def _union_worktree_for(self, rec):
        uid = rec.get("union_id")
        for u in self.store.unions():
            if u.get("union_id") == uid and u.get("worktree"):
                return Path(u["worktree"])
        raise StoreError("item %s has no union worktree (union_id=%s)" % (rec["item"], uid))

    def _write_final_request(self, wt, rec):
        uid = rec.get("union_id")
        union = next((u for u in self.store.unions() if u.get("union_id") == uid), None)
        if not union:
            raise StoreError("unknown union %s" % uid)
        items = []
        ids = []
        by_item = {r["item"]: r for r in self.store.report_items()}
        for it in union["items"]:
            r = by_item.get(it, {})
            iwt = Path(r.get("worktree") or self.worktree_path(it))
            ids += ["%s:%s" % (it, b) for b in self._benchmark_ids(iwt)]
            items.append({"item": it, "packet": str(iwt / ".vp" / "PACKET.md"),
                          "benchmark": str(iwt / ".vp" / "BENCHMARK.md"),
                          "review": str(iwt / ".vp" / "REVIEW.json"),
                          "findings": str(iwt / ".vp" / "FINDINGS.json"),
                          "result": str(iwt / ".vp" / "RESULT.json"),
                          "candidate_before_union": self.head_sha(iwt)})
        rcfg = self.roles.get("final") or {}
        req = {"union": uid, "subject": "union", "base": union["base_sha"],
               "candidate": union["union_sha"],
               "reviewer": "claude:%s" % rcfg.get("model", "opus"),
               "items": items, "benchmark_ids": ids, "ts": utc_ms()}
        (wt / ".vp").mkdir(parents=True, exist_ok=True)
        (wt / ".vp" / "FINAL_REQUEST.json").write_text(json.dumps(req, indent=2, sort_keys=True),
                                                       encoding="utf-8")
        for name, doc in (("REVIEW_SCHEMA.json", vpschema.REVIEW_SCHEMA_DOC),):
            (wt / ".vp" / name).write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return req

    # -- delivery ---------------------------------------------------------------

    def _changed_files(self, wt, base):
        rc, out, _ = self.git(["-C", str(wt), "diff", "--name-only", "%s..HEAD" % base])
        return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []

    def _owned_ok(self, rec, wt):
        """F-O6: every changed file must match owned_files; returns (ok, bad)."""
        hdr = self.packet_header(rec)
        owned = hdr.get("owned_files") or rec.get("allowed_files") or []
        base = rec.get("base_sha")
        if not owned or not base:
            return True, []
        bad = [f for f in self._changed_files(wt, base)
               if not f.startswith(".vp/") and
               not any(fnmatch.fnmatch(f, o) or f == o or f.startswith(o.rstrip("*"))
                       for o in owned)]
        return (not bad), bad

    def _deliver(self, rec, role, wt, record, attempt_id, outcome):
        item = rec["item"]
        if role in ("builder", "infra"):
            commit, blocked = None, None
            try:
                doc = json.loads(record.read_text(encoding="utf-8"))
                commit = doc.get("commit")
                blocked = doc.get("blocked")
            except (OSError, ValueError):
                commit = None
            head = self.head_sha(wt)
            if blocked and (not head or head == rec.get("base_sha")):
                # the packet told the builder to STOP (a precondition on its
                # base failed): that is a packet/base problem for the
                # orchestrator, not a retry (A3-3: six identical attempts)
                self._delivery_failed.add(item)
                self.store.block(item, "builder blocked: %s" % str(blocked)[:300])
                self.store.alert("BUILDER_BLOCKED", "%s: the builder stopped as the packet "
                                 "instructs: %s -- fix the packet/base and unblock"
                                 % (item, str(blocked)[:200]), item)
                self.log("BLOCKED %s builder: %s" % (item, str(blocked)[:200]))
                return
            if commit and head and not head.startswith(commit) and not commit.startswith(head):
                self.log("RESULT commit %s != HEAD %s; using HEAD" % (commit, head))
            commit = head or commit
            if not commit or commit == rec.get("base_sha"):
                self._delivery_failed.add(item)
                self.note_item_failure(item, rec.get("rev"), "builder produced no commit",
                                       attempt_id, kind="RUNNER_EMPTY")
                return
            self.store.submit_result(item, commit, record)
            return
        if role == "junior":
            ok, bad = self._owned_ok(rec, wt)
            if not ok:
                self._append_findings_fail(record, "B-forbidden-driver", "forbidden",
                                           "files outside owned_files changed: %s"
                                           % ", ".join(bad[:10]))
            doc, fails, unknown = findings_verdicts(record)
            lint = vplint.lint_findings(record, wt / ".vp" / "BENCHMARK.md", wt)
            errs = [l for l in lint if l.startswith("ERROR")]
            if errs:
                self._delivery_failed.add(item)
                self.store.escalate("RECORD_INVALID", item, attempt_id, "; ".join(errs)[:400])
                self.note_item_failure(item, rec.get("rev"), "findings invalid: %s"
                                       % "; ".join(errs)[:300], attempt_id, kind="RECORD_INVALID")
                return
            if unknown and not fails:
                # an UNKNOWN-only record is a grader gap, not a defect: no
                # strike, no rework round.  Re-grade the same commit once
                # with the ids named; if they stay UNKNOWN the senior decides.
                key = (rec.get("rev"), doc.get("commit") or rec.get("candidate_sha"))
                prev = self._unknown_rerun.get(item)
                if not prev or prev[:2] != key:
                    why = "; ".join("%s: %s" % (l.get("id"), (l.get("note") or l.get("evidence") or "")[:80])
                                    for l in doc.get("lines", []) if l.get("verdict") == "UNKNOWN")
                    self._unknown_rerun[item] = key + (list(unknown), why)
                    self.store.escalate("JUNIOR_UNKNOWN", item, attempt_id,
                                        "junior left %s UNKNOWN; re-grading once" % ", ".join(unknown))
                    self.log("UNKNOWN %s %s -> junior re-run once" % (item, ",".join(unknown)))
                    return
                res = self.store.findings(item, record, unverified_to_senior=True)
                self._unknown_rerun.pop(item, None)
                self.log("FINDINGS %s unverified=%s -> %s (senior decides)"
                         % (item, ",".join(unknown), res.get("status")))
                return
            self._unknown_rerun.pop(item, None)
            res = self.store.findings(item, record)
            self.log("FINDINGS %s all_pass=%s -> %s" % (item, res.get("all_pass"), res.get("status")))
            return
        if role == "senior":
            self._deliver_review(rec, wt, record, attempt_id, subject="plan")
            return
        if role == "final":
            self._deliver_final(rec, wt, record, attempt_id)
            return

    def _append_findings_fail(self, record, fid, kind, text):
        try:
            doc = json.loads(record.read_text(encoding="utf-8"))
            doc.setdefault("lines", []).append({"id": fid, "kind": kind, "verdict": "FAIL",
                                                "evidence": text, "note": "driver"})
            doc["all_pass"] = False
            record.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def _deliver_review(self, rec, wt, record, attempt_id, subject):
        item = rec["item"]
        doc = json.loads(record.read_text(encoding="utf-8"))
        lint = vplint.lint_review(record, wt / ".vp" / "BENCHMARK.md")
        errs = [l for l in lint if l.startswith("ERROR")]
        if errs:
            self._delivery_failed.add(item)
            self.store.escalate("RECORD_INVALID", item, attempt_id, "; ".join(errs)[:400])
            self.note_item_failure(item, rec.get("rev"), "review invalid: %s"
                                   % "; ".join(errs)[:300], attempt_id, kind="RECORD_INVALID")
            return
        verdict = {"APPROVE": "APPROVED", "FINDINGS": "FINDINGS", "BLOCKED": "BLOCKED"}[doc["verdict"]]
        reviews_dir = self.run_root / "reviews" / item
        reviews_dir.mkdir(parents=True, exist_ok=True)
        saved = reviews_dir / ("%s-%s.json" % (attempt_id, subject))
        saved.write_text(record.read_text(encoding="utf-8"), encoding="utf-8")
        rid = self.store.review_record(item, subject, doc["base"], doc["candidate"],
                                       "senior", doc["reviewer"], verdict, saved)
        self.log("REVIEW %s %s %s -> %s" % (item, subject, rid, verdict))
        if verdict == "FINDINGS":
            # the findings become the next round's FAIL lines (06.5)
            fpath = wt / ".vp" / "FINDINGS.json"
            self._findings_from_review(doc, rec, fpath)
            res = self.store.findings(item, fpath)
            self.log("FINDINGS(from senior) %s -> %s round %s" % (item, res.get("status"),
                                                                  res.get("round")))
        elif verdict == "BLOCKED":
            self.store.alert("BLOCKED", "%s: senior blocked the packet: %s"
                             % (item, doc.get("summary", "")[:200]), item)

    def _findings_from_review(self, doc, rec, fpath):
        lines = []
        for v in doc.get("verdicts", []):
            lines.append({"id": v["id"], "kind": "evidence", "verdict": v["verdict"],
                          "evidence": v.get("evidence", ""), "note": "senior"})
        for i, f in enumerate(doc.get("findings", [])):
            lines.append({"id": "S-%s" % (f.get("id") or i), "kind": "invariant",
                          "verdict": "FAIL",
                          "evidence": "%s:%s %s -- %s%s" % (
                              f.get("file"), f.get("line"), f.get("title"),
                              f.get("detail", "")[:300],
                              (" reproduce: " + f["reproduce"]) if f.get("reproduce") else ""),
                          "note": "senior [%s] %s" % (f.get("severity"), f.get("benchmark_line"))})
        if not lines:
            lines.append({"id": "S-0", "kind": "invariant", "verdict": "FAIL",
                          "evidence": doc.get("summary", "senior FINDINGS"), "note": "senior"})
        out = {"item": rec["item"], "attempt": 0, "commit": doc["candidate"],
               "lines": lines, "all_pass": False}
        fpath.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")

    def _deliver_final(self, rec, wt, record, attempt_id):
        doc = json.loads(record.read_text(encoding="utf-8"))
        uid = rec.get("union_id")
        union = next((u for u in self.store.unions() if u.get("union_id") == uid), None)
        if not union:
            raise StoreError("unknown union %s" % uid)
        req = json.loads((wt / ".vp" / "FINAL_REQUEST.json").read_text(encoding="utf-8"))
        ok, errs = vpschema.validate_review_obj(doc, req.get("benchmark_ids"))
        if not ok:
            self.store.escalate("RECORD_INVALID", rec["item"], attempt_id, "; ".join(errs)[:400])
            self.note_item_failure(rec["item"], rec.get("rev"), "final review invalid: %s"
                                   % "; ".join(errs)[:300], attempt_id, kind="RECORD_INVALID")
            return
        verdict = {"APPROVE": "APPROVED", "FINDINGS": "FINDINGS", "BLOCKED": "BLOCKED"}[doc["verdict"]]
        udir = self.run_root / "unions" / uid
        udir.mkdir(parents=True, exist_ok=True)
        saved = udir / ("FINAL-%s.json" % attempt_id)
        saved.write_text(record.read_text(encoding="utf-8"), encoding="utf-8")
        for it in union["items"]:
            try:
                rid = self.store.review_record(it, "code", doc["base"], doc["candidate"],
                                               "final", doc["reviewer"], verdict, saved)
                self.log("FINAL %s %s -> %s" % (it, rid, verdict))
            except StoreError as exc:
                self.log("final review record refused for %s: %s" % (it, exc))
                self.store.alert("FINAL_RECORD_REFUSED",
                                 "%s: final verdict %s for %s could not be recorded (%s); "
                                 "the item has no final review row even if the union is "
                                 "marked %s" % (it, verdict, uid, str(exc)[:200], verdict),
                                 it)
        self.store.union_status(uid, verdict, doc.get("summary", "")[:200])
        if verdict == "APPROVED":
            self.store.alert("UNION_APPROVED", "%s (%s) approved by final review; promotion "
                             "needs the owner" % (uid, ",".join(union["items"])))
        elif verdict == "FINDINGS":
            by_item = {r["item"]: r for r in self.store.report_items()}
            for it in union["items"]:
                r = by_item.get(it)
                if not r or r.get("status") != "GRADING":
                    continue
                iwt = Path(r.get("worktree") or self.worktree_path(it))
                fpath = iwt / ".vp" / "FINDINGS.json"
                mine = dict(doc)
                mine["findings"] = [f for f in doc.get("findings", [])
                                    if it in (f.get("benchmark_line") or "") or
                                    len(union["items"]) == 1]
                mine["verdicts"] = [v for v in doc.get("verdicts", [])
                                    if v["id"].startswith(it + ":") or len(union["items"]) == 1]
                for v in mine["verdicts"]:
                    v["id"] = v["id"].split(":", 1)[-1]
                self._findings_from_review(mine, r, fpath)
                self.store.findings(it, fpath)
            self.store.alert("FINAL_FINDINGS", "%s: final review disagreed; items back to "
                             "building" % uid)

    # -- union (K-21) -----------------------------------------------------------

    # -- speculative union chain (D83) --------------------------------------------
    #
    # With concurrency.max_unions_in_flight > 1 the driver behaves like a merge
    # queue: union N+1 is built on union N's sha while N is still proving, so
    # every union contains its predecessors and promotion stays one
    # fast-forward per union, in chain order (the store refuses N+1 before N).
    # If a predecessor dies (FAILED / FINDINGS / BLOCKED / CANCELLED) every
    # union stacked on it is CANCELLED: its proof result is discarded (the
    # CircleCI workflow is cancelled; a box proof runs out but is ignored), its
    # items go back to PREPARING with no strike, and the next tick rebuilds them
    # on the new tip.  At max_unions_in_flight == 1 none of this can trigger
    # and the base rule is the pre-D83 one (last APPROVED union).

    UNION_LIVE = ("BUILT", "PROOF", "APPROVED")
    UNION_DEAD = ("FAILED", "FINDINGS", "BLOCKED", "CANCELLED")

    def chain_mode(self):
        return int(self.conc.get("max_unions_in_flight", 2)) > 1

    def _chain_tip(self):
        unions = self.store.unions()
        if self.chain_mode():
            live = [u for u in unions if u.get("status") in self.UNION_LIVE]
        else:
            live = [u for u in unions if u.get("status") == "APPROVED"]
        return live[-1] if live else None

    def _reconcile_chain(self):
        if not self.chain_mode():
            return []
        try:
            unions = self.store.unions()
        except StoreError:
            return []
        by_sha = {u["union_sha"]: u for u in unions}
        cancelled = []
        for u in unions:                      # ordered by no: a cascade resolves in one pass
            if u.get("status") not in self.UNION_LIVE:
                continue
            pred = by_sha.get(u.get("base_sha"))
            if not pred or pred["union_id"] == u["union_id"]:
                continue
            if pred.get("status") in self.UNION_DEAD:
                if self._cancel_union(u, "%s is %s" % (pred["union_id"], pred["status"])):
                    u["status"] = "CANCELLED"
                    cancelled.append(u["union_id"])
        return cancelled

    def _cancel_union(self, u, cause):
        uid, cand = u["union_id"], u["union_sha"]
        with self._lock:
            self._proof_cancel.add(cand)
        try:
            reset = self.store.union_cancel(uid, cause)
        except StoreError as exc:
            self.log("UNION %s cancel refused: %s" % (uid, exc))
            self.store.alert("UNION_CANCEL_REFUSED", "%s stacked on a dead predecessor (%s) "
                             "could not be cancelled: %s" % (uid, cause, str(exc)[:200]))
            return False
        self.log("UNION %s CANCELLED (%s): %s -> PREPARING" % (uid, cause, ",".join(reset)))
        self.store.alert("UNION_CANCELLED", "%s cancelled: %s. Items %s back to PREPARING "
                         "(no strike); rebuilt on the new chain tip next tick"
                         % (uid, cause, ",".join(reset) or "-"))
        self._last_union_mono = 0.0           # rebuild without waiting for the trigger window
        return True

    def _proof_cancelled(self, cand):
        with self._lock:
            return cand in self._proof_cancel

    def _maybe_build_union(self, items):
        prep = [r for r in items if r.get("status") == "PREPARING" and not r.get("union_id")]
        if not prep:
            return None
        trig_n = int(self.conc.get("union_trigger_items", 3))
        trig_h = float(self.conc.get("union_trigger_hours", 2))
        in_flight = [u for u in self.store.unions() if u.get("status") in ("BUILT", "PROOF")]
        if len(in_flight) >= int(self.conc.get("max_unions_in_flight", 2)):
            return None
        if len(prep) < trig_n and (time.monotonic() - self._last_union_mono) < trig_h * 3600:
            return None
        if not self._union_lock.acquire(blocking=False):
            return None
        try:
            return self.build_union(prep)
        finally:
            self._union_lock.release()

    def build_union(self, prep):
        base = self.trunk_sha()
        tip = self._chain_tip()
        if tip:
            base = tip["union_sha"]      # stack on the last approved union (or, in chain
                                         # mode, the last live one: see _chain_tip)
        no = len(self.store.unions()) + 1
        branch = "vp/union-%d" % no
        wt = self.worktrees_root / ("union-%d" % no)
        if wt.exists():
            raise StoreError("union worktree %s already exists (K-23)" % wt)
        rc, out, err = self.git(["-C", str(self.trunk), "worktree", "add", str(wt), "-b",
                                 branch, base])
        if rc != 0:
            raise StoreError("union worktree add failed: %s" % (err or out)[:300])
        for rel in ("agent/.venv", "portal/node_modules", "platform/.venv"):
            try:
                if (self.trunk / rel).exists():
                    os.symlink(str(self.trunk / rel), str(wt / rel))
            except OSError:
                pass
        merged, conflicted, automerged = [], [], {}
        order = sorted(prep, key=lambda r: (len(self.packet_header(r).get("depends_on") or []),
                                            r.get("ts") or ""))
        for r in order:
            cand = r.get("candidate_sha")
            ours = self.head_sha(wt)
            rc, out, err = self.git(["-C", str(wt), "merge", "--no-ff", "--no-edit", "-m",
                                     "union-%d: %s" % (no, r["item"]), cand])
            if rc != 0:
                # D49: the additive-inventory class (TECHNICAL.md route tuple,
                # the closed-count asserts, the enumerating dicts) is merged
                # by arithmetic; anything else is a real conflict as before.
                done = self._automerge(wt, no, r["item"], ours, cand)
                if done is None:
                    self.git(["-C", str(wt), "merge", "--abort"])
                    conflicted.append(r["item"])
                    self.store.block(r["item"], "union-%d merge conflict on %s: %s"
                                     % (no, cand[:12], (out or err)[:200]))
                    self.store.alert("UNION_CONFLICT", "%s conflicts in union-%d"
                                     % (r["item"], no), r["item"])
                    continue
                automerged[r["item"]] = done
            else:
                fixed = vpmerge.fix_clean_merge(self.git, wt, ours, cand)
                if fixed:
                    rc2, out2, err2 = self.git(["-C", str(wt), "commit", "--amend",
                                               "--no-edit"])
                    if rc2 != 0:
                        self.log("union-%d amend after re-sum failed for %s: %s"
                                 % (no, r["item"], (err2 or out2)[:200]))
                    else:
                        automerged[r["item"]] = fixed
                        self.log("UNION-%d re-summed %s: %s" % (no, r["item"], json.dumps(fixed)))
            merged.append(r["item"])
        if not merged:
            self.git(["-C", str(self.trunk), "worktree", "remove", "--force", str(wt)])
            self.git(["-C", str(self.trunk), "branch", "-D", branch])
            return None
        dup = self._migration_collision(wt, base)
        if dup:
            for r in order:
                if r["item"] in merged:
                    self.store.block(r["item"], "migration number collision in union-%d: %s"
                                     % (no, dup))
            self.store.alert("UNION_CONFLICT", "union-%d migration collision: %s" % (no, dup))
            self.git(["-C", str(self.trunk), "worktree", "remove", "--force", str(wt)])
            self.git(["-C", str(self.trunk), "branch", "-D", branch])
            return None
        regen_note = self._regenerate_union_artifacts(wt, no)
        if isinstance(regen_note, RegistryFailed):
            # D80 follow-up: a registry writer that refuses (rc != 0) or whose
            # delta cannot be committed is a BUILD failure of the union, not a
            # note on it.  Proving "whatever registry the merge left" measures
            # a tree no candidate carries.  BLOCK every merged item with the
            # writer's message and cut no proof; the union number is consumed.
            for r in order:
                if r["item"] in merged:
                    self.store.block(r["item"], "union-%d environment registry: %s"
                                     % (no, str(regen_note)[:300]))
            self.store.alert("UNION_REGISTRY_FAILED",
                             "union-%d not proved: %s" % (no, str(regen_note)[:300]))
            self.git(["-C", str(self.trunk), "worktree", "remove", "--force", str(wt)])
            self.git(["-C", str(self.trunk), "branch", "-D", branch])
            return None
        union_sha = self.head_sha(wt)
        uid = self.store.union_record(merged, union_sha, base, wt, branch, note=regen_note)
        self._last_union_mono = time.monotonic()
        paths = []
        by = {r["item"]: r for r in prep}
        for it in merged:
            paths += self.packet_header(by[it]).get("test_paths") or []
        # generated artifacts are a property of the union, not of any one item:
        # every union proof carries the registry tests (SAFE-09 lesson: a new
        # module alone reds them, and no item's targeted set would notice)
        paths += list(self.proof_cfg.get("union_always_paths") or UNION_ALWAYS_PATHS)
        if automerged:
            # the arithmetic is only as good as the proof that checks it
            paths += list(vpmerge.PROOF_PATHS)
            self.store.alert("UNION_AUTOMERGE", "union-%d merged the inventory class by "
                             "arithmetic: %s" % (no, json.dumps(automerged)[:400]))
        kind = self.proof_cfg.get("union_kind", "targeted")
        pid = self.store.proof_request(union_sha, base, kind, sorted(set(paths)))
        self.log("UNION %s %s base=%s items=%s proof=%s" % (uid, union_sha[:12], base[:12],
                                                            ",".join(merged), pid))
        return uid

    def _automerge(self, wt, no, item, ours, cand):
        """Resolve a failed merge when every conflict is in vpmerge's class.
        Returns {path: note} and leaves the merge committed, or None with
        the index untouched (the caller aborts)."""
        try:
            done = vpmerge.resolve_merge(self.git, wt, ours, cand)
        except vpmerge.Refused as exc:
            self.log("union-%d %s: automerge refused: %s" % (no, item, exc))
            return None
        rc, out, err = self.git(["-C", str(wt), "commit", "--no-edit"])
        if rc != 0:
            self.log("union-%d %s: commit after automerge failed: %s"
                     % (no, item, (err or out)[:200]))
            return None
        self.log("UNION-%d automerged %s: %s" % (no, item, json.dumps(done)))
        return done

    def _regenerate_union_artifacts(self, wt, no):
        """Integrator step (K-21): rendered artifacts are a property of the
        merged tree. Run the registry writer on the union; commit the delta
        as the integrator, not as any item. Returns a note, None, or a
        RegistryFailed (the writer refused or its delta could not be
        committed) which build_union turns into a BLOCK of the union."""
        py = wt / "platform" / ".venv" / "bin" / "python"
        script = wt / "deploy" / "environment_registry.py"
        if not (py.exists() and script.exists()):
            return None
        rc, out, err = self.exec.run([str(py), str(script), "--write"], cwd=str(wt),
                                     timeout_s=120)
        if rc != 0:
            self.log("UNION union-%d registry --write rc=%s: %s" % (no, rc, (err or out)[:200]))
            return RegistryFailed("registry --write failed rc=%s: %s"
                                  % (rc, (err or out).strip()[:200]))
        rc, out, _ = self.git(["-C", str(wt), "status", "--porcelain", "--",
                               "deploy/environment_registry.generated.json", "DEPLOYMENT.md"])
        changed = [l[3:] for l in out.splitlines() if l.strip()]
        if not changed:
            return None
        self.git(["-C", str(wt), "add", "--"] + changed)
        rc, out, err = self.git(["-C", str(wt), "commit", "-q", "-m",
                                 "union-%d: regenerate environment registry (integrator)" % no])
        if rc != 0:
            self.log("UNION union-%d registry commit failed: %s" % (no, (err or out)[:200]))
            return RegistryFailed("regenerated registry (%s) could not be committed: %s"
                                  % (",".join(changed), (err or out).strip()[:200]))
        self.log("UNION union-%d integrator regenerated %s" % (no, ",".join(changed)))
        return "integrator regenerated %s" % ",".join(changed)

    def _migration_collision(self, wt, base):
        rc, out, _ = self.git(["-C", str(wt), "diff", "--name-only", "--diff-filter=A",
                               "%s..HEAD" % base])
        if rc != 0:
            return None
        nums = {}
        for f in out.splitlines():
            if "platform/db/migrations/" in f:
                name = Path(f).name
                num = name.split("_", 1)[0]
                if num.isdigit():
                    nums.setdefault(num, []).append(name)
        dups = {k: v for k, v in nums.items() if len(v) > 1}
        return json.dumps(dups) if dups else None

    # -- proof (K-09) -----------------------------------------------------------

    def run_proof(self, rec):
        item = rec["item"]
        cand = rec.get("candidate_sha")
        uid = rec.get("union_id")
        union = next((u for u in self.store.unions() if u.get("union_id") == uid), None)
        wt = Path(union["worktree"]) if union else Path(rec.get("worktree") or self.worktree_path(item))
        pid = rec.get("open_proof")
        hdr = self.packet_header(rec)
        # paths grouped by proof kind: a union may carry platform + portal + deploy items
        by_kind = {}
        item_kinds = set()          # the items' OWN proof kinds (not the always-paths)
        if union:
            by = {r["item"]: r for r in self.store.report_items()}
            for it in union["items"]:
                h = self.packet_header(by.get(it, {}))
                k = h.get("proof_kind") or "platform"
                item_kinds.add(k)
                by_kind.setdefault(k, set()).update(h.get("test_paths") or [])
            # rendered artifacts belong to the merged tree: every union proof
            # carries the registry tests (see _regenerate_union_artifacts)
            for p in (self.proof_cfg.get("union_always_paths") or UNION_ALWAYS_PATHS):
                by_kind.setdefault(_kind_of_always_path(p), set()).add(p)
        else:
            item_kinds.add(hdr.get("proof_kind") or "platform")
            by_kind[hdr.get("proof_kind") or "platform"] = set(hdr.get("test_paths") or [])
        by_kind = {k: sorted(v) for k, v in by_kind.items() if k != "docs" or not v}
        paths = sorted(set(p for v in by_kind.values() for p in v))
        base = union["base_sha"] if union else rec.get("base_sha")
        kind = self.proof_cfg.get("union_kind", "targeted") if union else "targeted"
        # infra backoff: never hammer the box for the same candidate
        bo = self._proof_backoff.get(cand)
        if bo and time.monotonic() < bo:
            return
        if not pid:
            pid = self.store.proof_request(cand, base, kind, paths)
        run_kinds = [k for k in by_kind if k != "docs"]
        if not run_kinds:
            counts = self.run_root / "proofs" / ("%s-docs.json" % pid)
            counts.parent.mkdir(parents=True, exist_ok=True)
            counts.write_text(json.dumps({"reason": "docs: no proof"}), encoding="utf-8")
            self.store.proof_record(pid, "PASS", counts)
            return
        cc = self.proof_cfg.get("circleci") or {}
        where, why = self._route_proof(kind, sorted(item_kinds), cc)
        if where == "circleci":
            # off-box: the whole suite on CircleCI (workflow full-suite); the
            # box lock is not taken, so these run in parallel with box proofs
            self.log("PROOF %s %s route=circleci (%s)" % (item, pid, why))
            return self.run_proof_circleci(rec, union, uid, pid, cand, wt, kind, paths, cc)
        if why:
            self.log("PROOF %s %s route=box (%s)" % (item, pid, why))
        with self._lock:
            self._box_active += 1
        try:
            return self._run_proof_box(rec, union, uid, pid, cand, wt, kind, paths, by_kind,
                                       run_kinds, item)
        finally:
            with self._lock:
                self._box_active = max(0, self._box_active - 1)

    # -- proof routing (D103) -----------------------------------------------------
    #
    # roster proof.circleci.mode:
    #   "off"      (default, or enabled false and no mode)  every proof on the box
    #   "swap"     (enabled true and no mode: the trial adapter)  every proof whose
    #              kind is in proof.circleci.kinds goes off-box; the box idles
    #   "overflow" the box takes a proof when it has a free box slot
    #              (proof.box_slots, default 1); otherwise CircleCI.  Real
    #              parallelism: max_proofs_in_flight must be > box_slots.
    # In every mode: a proof carrying an agent-KIND ITEM stays on the box until
    # the CI image carries the agent venv (pipeline 206: 255 skips "agent
    # interpreter not present"); the agent test in UNION_ALWAYS_PATHS does not
    # count, or nothing would ever leave the box.  And
    # proof.circleci.max_pipelines_per_day (default 60) is a hard cap -- past it
    # the proof runs on the box and the owner is alerted once per day.

    def _circle_mode(self, cc):
        mode = cc.get("mode")
        if mode in ("off", "swap", "overflow"):
            return mode
        return "swap" if cc.get("enabled") else "off"

    def _pipelines_today(self):
        """Pipelines this driver triggered today (UTC), from the append-only
        ledger run_root/proofs/circleci-pipelines.jsonl (survives restarts)."""
        p = self.run_root / "proofs" / "circleci-pipelines.jsonl"
        if not p.exists():
            return 0
        today = time.strftime("%Y-%m-%d", time.gmtime())
        n = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith('{"ts": "%s' % today):
                n += 1
        return n

    def _note_pipeline(self, pid, pipeline_id, account, cand):
        p = self.run_root / "proofs" / "circleci-pipelines.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 "proof": pid, "pipeline": pipeline_id, "account": account,
                                 "sha": cand}) + "\n")

    def _route_proof(self, kind, item_kinds, cc):
        """-> ("box"|"circleci", reason).  item_kinds = the items' own proof_kind values."""
        mode = self._circle_mode(cc)
        if mode == "off":
            return "box", ""
        if kind not in (cc.get("kinds") or ["full"]):
            return "box", "kind %s not in circleci.kinds" % kind
        if "agent" in item_kinds:
            return "box", "agent-kind item stays on the box (CI image lacks the agent venv)"
        cap = int(cc.get("max_pipelines_per_day", 60))
        n = self._pipelines_today()
        if n >= cap:
            day = time.strftime("%Y-%m-%d", time.gmtime())
            self.alert_once("circleci-cap-%s" % day, "CIRCLECI_DAILY_CAP",
                            "proof.circleci.max_pipelines_per_day=%d reached (%d today, UTC); "
                            "proofs run on the box until midnight UTC or a roster change"
                            % (cap, n))
            return "box", "daily cap %d reached (%d today)" % (cap, n)
        if mode == "swap":
            return "circleci", "mode swap"
        with self._lock:
            busy = self._box_active
        slots = int(self.proof_cfg.get("box_slots", 1))
        if busy < slots:
            return "box", "overflow: box slot free (%d/%d busy)" % (busy, slots)
        return "circleci", "overflow: box busy (%d/%d)" % (busy, slots)

    def _run_proof_box(self, rec, union, uid, pid, cand, wt, kind, paths, by_kind, run_kinds,
                       item):
        threading.current_thread().name = "vp-proof-%s" % cand
        self.log("PROOF %s %s %s kinds=%s paths=%d" % (item, pid, cand[:12],
                                                       ",".join(run_kinds), len(paths)))
        attempt_id = self.store.turn_start(item, "proof", None, None, "vpproof",
                                           "+".join(run_kinds), kind, "proof", os.getpid())
        timeout = float(self.proof_cfg.get("full_box_timeout_min" if kind == "full"
                                           else "targeted_timeout_min", 40)) * 60 + \
            float(self.conc.get("proof_wait_max_min", 90)) * 60 + 120
        multi = len(run_kinds) > 1
        results = {}
        rc_last, err_last, out_last = 0, "", ""
        for pk in run_kinds:
            sub_id = ("%s-%s" % (pid, pk)) if multi else pid
            argv = [sys.executable, str(self.here / "vpproof.py"), "run",
                    "--run-root", str(self.run_root), "--worktree", str(wt), "--sha", cand,
                    "--proof-id", sub_id, "--kind", kind, "--proof-kind", pk,
                    "--paths", ",".join(by_kind[pk]), "--cn", str(self.cn)]
            if multi:
                argv.append("--no-record")
            if self._proof_cancelled(cand):
                break
            rc, out, err = self.exec.run(argv, cwd=str(self.here), timeout_s=timeout)
            rc_last, err_last, out_last = rc, err, out
            res = {}
            try:
                res = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
            except ValueError:
                res = {}
            results[pk] = (rc, res)
            if rc == 124 or not res:
                break
        if self._proof_cancelled(cand):
            # D83: the union was cancelled while this ran; the result is for a
            # tree nobody will promote.  The subprocess was NOT killed (a killed
            # pytest leaves Postgres/shm behind on the shared box); it ran out.
            self.store.turn_end(attempt_id, "STALLED", None, None, "CANCELLED (union chain)")
            self.log("PROOF %s %s -> CANCELLED (union cancelled; result discarded)" % (item, pid))
            return
        # aggregate
        status, failed, logs, counts_all = "PASS", [], [], {}
        for pk, (rc, res) in results.items():
            st = res.get("status") or "UNKNOWN"
            counts_all[pk] = res.get("counts", {})
            if res.get("log"):
                logs.append(res["log"])
            failed += (res.get("counts") or {}).get("failed_nodes") or []
            if st == "FAIL_PRODUCT":
                status = "FAIL_PRODUCT"
            elif st in ("FAIL_INFRA", "UNKNOWN") and status != "FAIL_PRODUCT":
                status = st
        if any(rc == 124 or not res for rc, res in results.values()) or not results:
            status = "UNKNOWN"
        self.store.turn_end(attempt_id, "DONE" if status in ("PASS", "FAIL_PRODUCT") else
                            "STALLED", logs[0] if logs else None, None,
                            "%s %s" % (status, json.dumps(counts_all)[:300]))
        if status == "UNKNOWN" and (rc_last == 124 or not any(r for _, r in results.values())):
            try:
                self.store.proof_record(pid, "UNKNOWN")
            except StoreError:
                pass
            self._note_proof_infra(cand, union, rec, pid, "no result (rc %s): %s"
                                   % (rc_last, (err_last or out_last)[:200]))
            return
        if multi:
            cpath = self.run_root / "proofs" / ("%s-union.json" % pid)
            cpath.parent.mkdir(parents=True, exist_ok=True)
            cpath.write_text(json.dumps({"status": status, "kinds": counts_all,
                                         "failed_nodes": failed, "logs": logs},
                                        indent=2, sort_keys=True), encoding="utf-8")
            try:
                self.store.proof_record(pid, status, cpath)
            except StoreError as exc:
                self.log("proof record refused for %s: %s" % (pid, exc))
                self.store.alert("PROOF_RECORD_REFUSED",
                                 "%s: proof %s came out %s but the store refused the record "
                                 "(%s); item state and the proof table now disagree"
                                 % (item, pid, status, str(exc)[:200]), item)
        self.log("PROOF %s %s -> %s" % (item, pid, status))
        if status == "FAIL_PRODUCT":
            self._proof_findings(union, rec, failed, pid, self._proof_errors(logs, failed))
        elif status in ("FAIL_INFRA", "UNKNOWN"):
            self._note_proof_infra(cand, union, rec, pid, json.dumps(counts_all)[:200])
        elif status == "PASS":
            self._proof_backoff.pop(cand, None)
            self._proof_infra_n.pop(cand, None)
            if union:
                self.store.union_status(uid, "PROOF", "proof %s PASS" % pid)

    def run_proof_circleci(self, rec, union, uid, pid, cand, wt, kind, paths, cc):
        """Proof adapter for CircleCI (roster proof.circleci).  Push the
        candidate as its own branch, trigger the pipeline with the full-suite
        parameter, poll to completion, classify with vpcircle, and record the
        proof exactly as a box proof is recorded (same downstream: findings,
        infra backoff, union PROOF).  Every subprocess goes through
        vpcircle.Runner so tests never touch git remotes or the network."""
        item = rec["item"]
        runner = self.circle_runner or vpcircle.Runner()
        prefix = cc.get("branch_prefix", "vp/proof/")
        branch = "%s%s-%s" % (prefix, pid, cand[:12])
        param = cc.get("param") or self.proof_cfg.get("circleci_param", "run_full_suite")
        threading.current_thread().name = "vp-proof-%s" % cand
        self.log("PROOF %s %s %s -> circleci branch=%s paths=%d"
                 % (item, pid, cand[:12], branch, len(paths)))
        attempt_id = self.store.turn_start(item, "proof", None, None, "circleci",
                                           "full-suite", kind, "proof", os.getpid())
        pipeline_id, account, workflow_id, res = None, None, None, None
        status, failed, errors, counts = "UNKNOWN", [], {}, {}
        try:
            try:
                # preflight: the chosen account must see the project BEFORE a
                # branch is pushed (a push alone fires the project's all-pushes
                # probe; trial attempt 1 paid that for a 404 from the wrong account)
                ok, why = vpcircle.project_visible(runner, cc.get("account"))
                if not ok:
                    raise RuntimeError("preflight: %s" % why)
                rc, out, err = self.git(["-C", str(wt), "branch", "-f", branch, cand])
                if rc != 0:
                    raise RuntimeError("git branch -f %s failed: %s" % (branch, (err or out)[:200]))
                vpcircle.push_branch(wt, branch, runner)
                trig = vpcircle.trigger(branch, {param: True}, runner, cc.get("account"))
                pipeline_id, account = trig["pipeline_id"], trig["account"]
                self._note_pipeline(pid, pipeline_id, account, cand)
                self.store.proof_record(pid, "RUNNING", pipeline_id=pipeline_id)
                self.log("PROOF %s %s circleci pipeline %s (account %s)"
                         % (item, pid, pipeline_id, account))
                res = vpcircle.poll(pipeline_id, interval=int(cc.get("poll_interval_s", 60)),
                                    deadline_s=int(cc.get("deadline_min", 90)) * 60,
                                    runner=runner, account=account,
                                    abort=lambda: self._proof_cancelled(cand))
            except vpcircle.Cancelled:
                # D83: the union was cancelled under us; stop the spend, discard
                done = vpcircle.cancel_pipeline(pipeline_id, runner, account)
                self.store.turn_end(attempt_id, "STALLED", None, None,
                                    "CANCELLED (union chain) circleci %s" % pipeline_id)
                self.log("PROOF %s %s -> CANCELLED (union cancelled; circleci pipeline %s, "
                         "workflows cancelled: %s)" % (item, pid, pipeline_id, ",".join(done) or "-"))
                return
            except Exception as exc:           # any off-box failure is UNKNOWN + backoff, never a crash
                counts = {"reason": "circleci: %s: %s" % (type(exc).__name__, str(exc)[:300]),
                          "pipeline_id": pipeline_id,
                          "account": account, "branch": branch}
                cpath = self.run_root / "proofs" / ("%s-circleci.json" % pid)
                cpath.parent.mkdir(parents=True, exist_ok=True)
                cpath.write_text(json.dumps(counts, indent=2, sort_keys=True), encoding="utf-8")
                try:
                    self.store.proof_record(pid, "UNKNOWN", cpath, pipeline_id=pipeline_id)
                except StoreError:
                    pass
                self.store.turn_end(attempt_id, "STALLED", None, None,
                                    "UNKNOWN %s" % counts["reason"][:200])
                self.log("PROOF %s %s -> UNKNOWN (circleci: %s)" % (item, pid, str(exc)[:200]))
                self._note_proof_infra(cand, union, rec, pid, counts["reason"])
                return
            if self._proof_cancelled(cand):
                self.store.turn_end(attempt_id, "STALLED", None, None,
                                    "CANCELLED (union chain) circleci %s" % pipeline_id)
                self.log("PROOF %s %s -> CANCELLED (union cancelled; result discarded)" % (item, pid))
                return
            cls = vpcircle.classify(res["jobs"], res["failed_tests"])
            status = cls["status"]
            workflow_id = (res["workflows"][0].get("id") if res.get("workflows") else None)
            failed, errors = circle_failed_nodes(res["failed_tests"])
            out_dir = vpcircle.record(self.run_root, cand,
                                      {"pipeline_id": pipeline_id, "account": account,
                                       "branch": branch, "proof_id": pid,
                                       "workflows": res["workflows"]},
                                      res["jobs"], res["failed_tests"], cls)
            counts = {"status": status, "reds": cls["reds"], "failed_nodes": failed,
                      "pipeline_id": pipeline_id, "workflow_id": workflow_id,
                      "account": account, "branch": branch, "paths": paths,
                      "jobs": [{"name": j.get("name"), "status": j.get("status"),
                                "job_number": j.get("job_number")} for j in res["jobs"]]}
            cpath = self.run_root / "proofs" / ("%s-circleci.json" % pid)
            cpath.write_text(json.dumps(counts, indent=2, sort_keys=True), encoding="utf-8")
            try:
                self.store.proof_record(pid, status, cpath, str(out_dir),
                                        pipeline_id=pipeline_id, workflow_id=workflow_id)
            except StoreError as exc:
                self.log("proof record refused for %s: %s" % (pid, exc))
                self.store.alert("PROOF_RECORD_REFUSED",
                                 "%s: circleci proof %s came out %s but the store refused the "
                                 "record (%s)" % (item, pid, status, str(exc)[:200]), item)
            self.store.turn_end(attempt_id, "DONE" if status in ("PASS", "FAIL_PRODUCT")
                                else "STALLED", str(cpath), None,
                                "%s circleci %s" % (status, pipeline_id))
            self.log("PROOF %s %s -> %s (circleci pipeline %s, %d reds)"
                     % (item, pid, status, pipeline_id, len(cls["reds"])))
            if status == "FAIL_PRODUCT":
                self._proof_findings(union, rec, failed, pid, errors)
            elif status in ("FAIL_INFRA", "UNKNOWN"):
                self._note_proof_infra(cand, union, rec, pid, json.dumps(cls["reds"])[:200])
            elif status == "PASS":
                self._proof_backoff.pop(cand, None)
                self._proof_infra_n.pop(cand, None)
                if union:
                    self.store.union_status(uid, "PROOF", "proof %s PASS (circleci %s)"
                                            % (pid, pipeline_id))
        finally:
            if cc.get("delete_branch_after", True):
                try:
                    runner.git(["push", "origin", "--delete", branch], cwd=wt)
                except Exception as exc:          # cleanup must never change the verdict
                    self.log("circleci branch cleanup %s: %s" % (branch, exc))
                self.git(["-C", str(wt), "branch", "-D", branch])

    def _note_proof_infra(self, cand, union, rec, pid, detail):
        """FAIL_INFRA/UNKNOWN: back off 2/5/10 min, then pause the items and alert."""
        n = self._proof_infra_n.get(cand, 0) + 1
        self._proof_infra_n[cand] = n
        waits = (120, 300, 600)
        item = rec["item"]
        if n >= 3:
            items = union["items"] if union else [item]
            for it in items:
                try:
                    self.store.item_pause(it)
                except StoreError:
                    pass
            self.store.alert("PROOF_INFRA", "%s proof %s FAIL_INFRA x%d: items %s PAUSED "
                             "until the Architect resumes them: %s"
                             % (item, pid, n, ",".join(items), detail), item)
            self._proof_backoff.pop(cand, None)
            self._proof_infra_n.pop(cand, None)
            return
        self._proof_backoff[cand] = time.monotonic() + waits[min(n, len(waits)) - 1]
        self.store.alert("PROOF_INFRA", "%s proof %s FAIL_INFRA (try %d, retry in %ds): %s"
                         % (item, pid, n, waits[min(n, len(waits)) - 1], detail), item)

    @staticmethod
    def _proof_errors(logs, failed):
        """Per failed node, the pytest `E ` lines from its failure section so a
        builder that cannot run pytest still sees the cause (SAFE-10 lesson:
        five nodes red on one missing fixture import, findings said only
        'failed')."""
        out = {}
        text = ""
        for lp in logs:
            try:
                text += Path(lp).read_text(encoding="utf-8", errors="replace") + "\n"
            except OSError:
                continue
        if not text:
            return out
        for node in failed:
            name = node.rsplit("::", 1)[-1]
            errs, active = [], False
            for line in text.splitlines():
                if line.startswith("___") and name in line:
                    active, errs = True, []
                elif active and line.startswith("___"):
                    break
                elif active and line.startswith("E "):
                    errs.append(line[2:].strip())
            if not errs:
                errs = [l[2:].strip() for l in text.splitlines()
                        if l.startswith("E ") and name in l]
            seen, uniq = set(), []
            for e in errs:
                if e and e not in seen:
                    seen.add(e)
                    uniq.append(e)
            if uniq:
                out[node] = " | ".join(uniq[:4])[:600]
        return out

    def _proof_findings(self, union, rec, failed, pid, errors=None):
        """Map failed node ids to items by test_paths; write FAIL lines; the
        store already moved the items to GRADING."""
        errors = errors or {}
        items = union["items"] if union else [rec["item"]]
        by = {r["item"]: r for r in self.store.report_items()}
        owned = {it: (self.packet_header(by.get(it) or rec).get("test_paths") or [])
                 for it in items}
        claimed = set(f for f in failed for ps in owned.values() if any(f.startswith(p) for p in ps))
        # a full-suite red outside every item's test_paths (a shared registry,
        # a neighbour file) is nobody's by prefix; every item in the union
        # sees it, otherwise the store has moved them to GRADING with stale findings
        orphans = [f for f in failed if f not in claimed]
        for it in items:
            r = by.get(it) or rec
            paths = owned[it]
            mine = [f for f in failed if any(f.startswith(p) for p in paths)] + orphans
            if not mine:
                continue
            iwt = Path(r.get("worktree") or self.worktree_path(it))
            fpath = iwt / ".vp" / "FINDINGS.json"
            lines = [{"id": "P-%d" % i, "kind": "test", "verdict": "FAIL",
                      "evidence": "proof %s: %s failed%s"
                                  % (pid, f, (" — " + errors[f]) if errors.get(f) else ""),
                      "note": "vpproof: the proof runner ran this node on the union "
                              "candidate; fix the cause in your worktree (you cannot run "
                              "pytest in the build box, so read the error text above)"}
                     for i, f in enumerate(mine)]
            out = {"item": it, "attempt": 0, "commit": r.get("candidate_sha") or "0" * 7,
                   "lines": lines, "all_pass": False}
            fpath.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
            try:
                self.store.findings(it, fpath)
            except StoreError as exc:
                self.log("proof findings refused for %s: %s" % (it, exc))
                self.store.alert("FINDINGS_REFUSED",
                                 "%s: proof %s FAIL findings are on disk at %s but the store "
                                 "refused them (%s); the item was not sent back to building"
                                 % (it, pid, fpath, str(exc)[:200]), it)

    # -- STOP (K-18) --------------------------------------------------------------

    def _stop_step(self):
        elapsed = time.monotonic() - (self._stop_started or time.monotonic())
        alive = [t for t in self._threads if t.is_alive()]
        if alive and elapsed < self.stop_grace_s:
            return
        if alive and not self._abort.is_set():
            self.log("STOP grace over; killing %d turns" % len(alive))
            self._abort.set()
            return
        if alive:
            return
        self._finish("stop" if self.stop_requested() else "close_out")

    def _finish(self, reason):
        if getattr(self, "_finished", False):
            return
        self._finished = True
        try:
            self.store.run_finish(reason)
        except StoreError:
            pass
        try:
            self.store.render()
        except StoreError:
            pass
        self.write_stop_handoff(reason)
        self.write_heartbeat({"finished": reason})
        self.log("FINISHED %s" % reason)

    def write_stop_handoff(self, reason):
        try:
            summary = self.store.report("summary")
            pending = self.store.report("pending")
        except StoreError:
            summary, pending = {}, {}
        lines = ["# STOP-HANDOFF", "", "reason: %s" % reason, "ts: %s" % utc_ms(),
                 "run_root: %s" % self.run_root, "", "## items by status", ""]
        for k, v in sorted((summary.get("items_by_status") or {}).items()):
            lines.append("- %s: %s" % (k, v))
        lines += ["", "## attempts by runner", ""]
        for r in summary.get("attempts_by_runner") or []:
            lines.append("- %s: %s attempts, cost %.4f, in %s out %s cache %s"
                         % (r.get("runner"), r.get("attempts"), r.get("cost") or 0,
                            r.get("tokens_in"), r.get("tokens_out"), r.get("cache_read")))
        lines += ["", "## unions", ""]
        for u in summary.get("unions") or []:
            lines.append("- %s %s %s items=%s" % (u.get("union_id"), u.get("status"),
                                                  (u.get("union_sha") or "")[:12],
                                                  ",".join(u.get("items") or [])))
        lines += ["", "## pending", ""]
        for it in pending.get("items") or []:
            lines.append("- %s %s round=%s" % (it.get("item"), it.get("status"), it.get("round")))
        lines += ["", "## resume", "",
                  "rm '%s'" % self.stop_file,
                  "python3 '%s' --run-root '%s' run reconcile" % (self.here / "vpctl.py", self.run_root),
                  "python3 '%s' --run-root '%s' run resume --note 'owner said resume'"
                  % (self.here / "vpctl.py", self.run_root),
                  "caffeinate -dims python3 '%s' --roster '%s' --loop --auto-assign --exit-on-stop"
                  % (self.here / "vpdriver.py", self.roster_path), ""]
        (self.run_root / "STOP-HANDOFF.md").write_text("\n".join(lines), encoding="utf-8")

    # -- loops --------------------------------------------------------------------

    def join(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        for th in list(self._threads):
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            th.join(left)
        self._threads = [t for t in self._threads if t.is_alive()]

    def run_once(self):
        self.tick()
        self.join()
        self.write_heartbeat()
        return 0

    def loop(self, max_ticks=None):
        ticks = 0
        while True:
            self.tick()
            ticks += 1
            self._threads = [t for t in self._threads if t.is_alive()]
            if getattr(self, "_finished", False):
                break
            if max_ticks is not None and ticks >= max_ticks:
                break
            time.sleep(self.interval)
        self.join(timeout=self.interval * 6)
        self.write_heartbeat()
        return 0


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="vpdriver.py", description="Voice Pod v12 driver")
    ap.add_argument("--roster", required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--auto-assign", action="store_true")
    ap.add_argument("--exit-on-stop", action="store_true")
    ap.add_argument("--max-ticks", type=int, default=None)
    ap.add_argument("--log", default=None)
    args = ap.parse_args(argv)
    drv = Driver(args.roster, interval=args.interval, auto_assign=args.auto_assign,
                 exit_on_stop=args.exit_on_stop)
    if args.log:
        drv.log_path = Path(args.log)
    drv.log("driver start pid=%d roster=%s" % (os.getpid(), args.roster))
    if args.once:
        return drv.run_once()
    return drv.loop(max_ticks=args.max_ticks)


if __name__ == "__main__":
    raise SystemExit(main())
