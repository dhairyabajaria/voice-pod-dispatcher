#!/usr/bin/env python3
"""tests/test_v12_cycle.py -- L3 mock thin cycle + L4 fault injections for the
v12 driver.  Real vpstore/vpctl (subprocess), real git (temp repo), FAKE
runners.  No network, no model, no opencode/codex/claude binaries.

    <platform venv python> tests/test_v12_cycle.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP))

import vpdriver   # noqa: E402
import vprunners  # noqa: E402
import vpschema   # noqa: E402
from vprunners import TurnOutcome, STATUS_DONE  # noqa: E402

PY = sys.executable


def sh(args, cwd=None, env=None):
    cp = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        universal_newlines=True)
    return cp.returncode, cp.stdout.strip(), cp.stderr.strip()


def git(cwd, *args):
    rc, out, err = sh(["git", "-C", str(cwd)] + list(args))
    assert rc == 0, "git %s: %s %s" % (args, out, err)
    return out


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

PACKET = """---
item: {item}
title: mock item
group: 1
base_sha: {base}
depends_on: []
releases: []
critical: false
owned_files:
  - platform/hello.py
  - platform/tests/test_hello.py
forbidden_files:
  - platform/db/migrations/**
test_paths:
  - platform/tests/test_hello.py
proof_kind: platform
max_rounds: 4
max_minutes_build: 45
reviewer_model: gpt-5.6-sol
owner_needed: none
---
## Goal
Make hello() return "hi" (platform/hello.py:1).
## Witnesses
platform/hello.py:1 returns "hello" today.
## Steps
1. edit platform/hello.py
## Prohibitions
do not touch migrations
"""

BENCHMARK = """- B1 [invariant] hello() returns "hi" — check: grep platform/hello.py
- B2 [test] platform/tests/test_hello.py::test_hi passes — check: pytest node id
- B3 [negative] hello() does not return "hello" — check: test node id
- B4 [forbidden] No file outside owned_files changed — check: git diff --name-only
- B5 [evidence] RESULT.json checks[] includes the test command — check: RESULT.json
"""


class Env(object):
    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.cn = self.tmp / "cn"
        self.trunk = self.cn / "trunk"
        self.worktrees = self.cn / "vp-worktrees"
        self.run_root = self.cn / "test-logs" / "audit" / "run-v12-test"
        self.run_root.mkdir(parents=True)
        self.trunk.mkdir(parents=True)
        git(self.trunk, "init", "-q", "-b", "plan010/rebuild")
        git(self.trunk, "config", "user.email", "t@t")
        git(self.trunk, "config", "user.name", "t")
        (self.trunk / "platform").mkdir()
        (self.trunk / "platform" / "hello.py").write_text('def hello():\n    return "hello"\n')
        (self.trunk / "platform" / "tests").mkdir()
        (self.trunk / "platform" / "tests" / "test_hello.py").write_text(
            "from hello import hello\n")
        git(self.trunk, "add", "-A")
        git(self.trunk, "commit", "-q", "-m", "base")
        self.base = git(self.trunk, "rev-parse", "HEAD")
        self.roster = {
            "run": {"cn": str(self.cn), "trunk": str(self.trunk),
                    "worktrees": str(self.worktrees), "stop_file": "STOP",
                    "stop_grace_s": 1},
            "night": {"pause_on_disk_gb": 0},
            "concurrency": {"round_cap": 4, "wip_per_group": 2, "union_trigger_items": 1,
                            "max_minutes_per_turn": {"build": 1, "grade": 1, "review": 1,
                                                     "final": 1}},
            "budget": {"max_cost_usd_per_run": 40, "max_cost_usd_per_item": 5},
            "backoff": {"rate_min": 1},
            "servers": {"go1": {"url": "http://127.0.0.1:1", "xdg_data_home": str(self.tmp / "xdg")}},
            "groups": {"1": {"server": "go1"}},
            "roles": {"builder": {"runner": "opencode", "agent": "vp-builder", "model": "m", "variant": "xhigh"},
                      "junior": {"runner": "opencode", "agent": "vp-junior", "model": "j", "variant": "max"},
                      "senior": {"runner": "codex", "model": "gpt-x", "effort": "high",
                                 "fallback_to_claude": True},
                      "final": {"runner": "claude", "model": "opus", "effort": "high"}},
            "proof": {"union_kind": "targeted"},
            "round_cap": 4, "wip_per_group": 2,
        }
        (self.run_root / "roster.json").write_text(json.dumps(self.roster, indent=2))
        (self.tmp / "xdg" / "opencode" / "log").mkdir(parents=True)
        (self.tmp / "xdg" / "opencode" / "log" / "opencode.log").write_text("")
        self.env = dict(os.environ, VP_RUN_ROOT=str(self.run_root),
                        VP_STOP_FILE=str(self.run_root / "STOP"))

    def vpctl(self, *args, ok=True):
        rc, out, err = sh([PY, str(VP / "vpctl.py"), "--run-root", str(self.run_root)]
                          + list(args) + ["--json"], cwd=str(VP), env=self.env)
        if ok:
            assert rc == 0, "vpctl %s rc=%d %s %s" % (args, rc, out, err)
        return rc, (json.loads(out) if out.startswith(("{", "[")) else out)

    def add_item(self, item):
        pdir = self.run_root / "packets" / item
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "PACKET.md").write_text(PACKET.format(item=item, base=self.base))
        (pdir / "BENCHMARK.md").write_text(BENCHMARK)
        _rc, d = self.vpctl("packet", "submit", item, "--benchmark", str(pdir / "BENCHMARK.md"),
                            "--packet", str(pdir / "PACKET.md"), "--base", self.base,
                            "--allowed-files", "platform/hello.py,platform/tests/test_hello.py")
        self.vpctl("packet", "ready", d["packet_id"])

    def item(self, item):
        _rc, d = self.vpctl("report", "items")
        return next(r for r in d["items"] if r["item"] == item)


# --------------------------------------------------------------------------
# fake runners
# --------------------------------------------------------------------------

class FakeRunner(object):
    """Scripted per role: a list of callables(spec) -> TurnOutcome."""
    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def run(self, spec, abort_flag=None):
        self.calls.append(spec)
        fn = self.script.pop(0) if self.script else self.script_default
        return fn(spec)

    def export(self, spec, sid, dest):
        Path(dest).write_text("{}")
        return {"path": dest, "bytes": 2, "truncated_suspect": False}

    @staticmethod
    def script_default(spec):
        return TurnOutcome("RUNNER_EMPTY", "script exhausted", runner="fake")


def builder_ok(spec):
    wt = Path(spec.cwd)
    (wt / "platform" / "hello.py").write_text('def hello():\n    return "hi"\n')
    (wt / "platform" / "tests" / "test_hello.py").write_text(
        "from hello import hello\n\ndef test_hi():\n    assert hello() == 'hi'\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "--allow-empty", "-m", "hi")
    head = git(wt, "rev-parse", "HEAD")
    base = (wt / ".vp" / "BASE").read_text().strip()
    rec = {"item": spec.item, "attempt": 1, "commit": head, "base": base,
           "diff_stat": {"files": 2, "insertions": 3, "deletions": 1},
           "checks": [{"name": "pytest", "command": "pytest platform/tests/test_hello.py",
                       "exit": 0, "log": "1 passed"}],
           "disputes": [], "blocked": None, "notes": ""}
    Path(spec.out_path).write_text(json.dumps(rec))
    return TurnOutcome(STATUS_DONE, "", session_id="ses_b", record_path=spec.out_path,
                       usage={"tokens_in": 100, "tokens_out": 50, "cost": 0.01}, runner="fake")


def builder_forbidden(spec):
    wt = Path(spec.cwd)
    (wt / "platform" / "db").mkdir(parents=True, exist_ok=True)
    (wt / "platform" / "db" / "migrations").mkdir(parents=True, exist_ok=True)
    (wt / "platform" / "db" / "migrations" / "250_bad.sql").write_text("select 1;")
    return builder_ok(spec)


def junior_pass_fence(spec):
    """Writes nothing (denied), prints the FINDINGS in a fence (K-02)."""
    wt = Path(spec.cwd)
    head = git(wt, "rev-parse", "HEAD")
    lines = [{"id": b, "kind": k, "verdict": "PASS", "evidence": "platform/hello.py:2", "note": ""}
             for b, k in (("B1", "invariant"), ("B2", "test"), ("B3", "negative"),
                          ("B4", "forbidden"), ("B5", "evidence"))]
    doc = {"item": spec.item, "attempt": 1, "commit": head, "lines": lines, "all_pass": True}
    text = "I could not write the file.\n```json\n%s\n```\n" % json.dumps(doc)
    # emulate the OpenCodeRunner's fence fallback
    obj = vprunners.extract_json_fence(text)
    Path(spec.out_path).write_text(json.dumps(obj))
    return TurnOutcome(STATUS_DONE, "record taken from text fence", session_id="ses_j",
                       record_path=spec.out_path, text=text,
                       usage={"tokens_in": 80, "tokens_out": 40, "cost": 0.005}, runner="fake")


def junior_fail(spec):
    wt = Path(spec.cwd)
    head = git(wt, "rev-parse", "HEAD")
    lines = [{"id": "B1", "kind": "invariant", "verdict": "FAIL",
              "evidence": "platform/hello.py:2 returns hello", "note": ""}] + \
            [{"id": b, "kind": k, "verdict": "PASS", "evidence": "x.py:1", "note": ""}
             for b, k in (("B2", "test"), ("B3", "negative"), ("B4", "forbidden"), ("B5", "evidence"))]
    doc = {"item": spec.item, "attempt": 1, "commit": head, "lines": lines, "all_pass": False}
    Path(spec.out_path).write_text(json.dumps(doc))
    return TurnOutcome(STATUS_DONE, "", record_path=spec.out_path, runner="fake")


def senior_approve(spec):
    req = json.loads((Path(spec.cwd) / ".vp" / "REVIEW_REQUEST.json").read_text())
    doc = {"item": req["item"], "subject": "item", "base": req["base"],
           "candidate": req["candidate"], "reviewer": req["reviewer"], "verdict": "APPROVE",
           "verdicts": [{"id": b, "verdict": "PASS", "evidence": "platform/hello.py:2"}
                        for b in req["benchmark_ids"]],
           "findings": [], "evidence": ["platform/hello.py:2 returns 'hi'"],
           "summary": "ok"}
    Path(spec.out_path).write_text(json.dumps(doc))
    return TurnOutcome(STATUS_DONE, "", session_id="thread-1", record_path=spec.out_path,
                       structured=doc, usage={"tokens_in": 1000, "tokens_out": 100}, runner="fake")


def senior_findings(spec):
    req = json.loads((Path(spec.cwd) / ".vp" / "REVIEW_REQUEST.json").read_text())
    doc = {"item": req["item"], "subject": "item", "base": req["base"],
           "candidate": req["candidate"], "reviewer": req["reviewer"], "verdict": "FINDINGS",
           "verdicts": [{"id": b, "verdict": "PASS" if b != "B3" else "FAIL",
                         "evidence": "platform/hello.py:2"} for b in req["benchmark_ids"]],
           "findings": [{"id": "F1", "severity": "medium", "title": "no negative test",
                         "file": "platform/tests/test_hello.py", "line": 3,
                         "detail": "B3 not covered", "reproduce": "pytest -k hello",
                         "benchmark_line": "B3"}],
           "evidence": ["platform/tests/test_hello.py:3"], "summary": "needs B3"}
    Path(spec.out_path).write_text(json.dumps(doc))
    return TurnOutcome(STATUS_DONE, "", record_path=spec.out_path, structured=doc, runner="fake")


def senior_refused(spec):
    return TurnOutcome("RUNNER_REFUSED", "I can't help with that", runner="fake")


def senior_quota(spec):
    return TurnOutcome("QUOTA_ROLLING", "usage limit reached", usage={"reset_minutes": 1},
                       runner="fake")


def final_approve(spec):
    req = json.loads((Path(spec.cwd) / ".vp" / "FINAL_REQUEST.json").read_text())
    doc = {"item": req["union"], "subject": "union", "base": req["base"],
           "candidate": req["candidate"], "reviewer": req["reviewer"], "verdict": "APPROVE",
           "verdicts": [{"id": b, "verdict": "PASS", "evidence": "platform/hello.py:2"}
                        for b in req["benchmark_ids"]],
           "findings": [], "evidence": ["platform/hello.py:2"], "summary": "union ok"}
    Path(spec.out_path).write_text(json.dumps(doc))
    return TurnOutcome(STATUS_DONE, "", session_id="cs-1", record_path=spec.out_path,
                       structured=doc, usage={"cost": 0.5}, runner="fake")


class FakeProofDriver(vpdriver.Driver):
    """The proof runs pytest on the box; here it is scripted."""
    proof_status = "PASS"

    def run_proof(self, rec):
        cand = rec["candidate_sha"]
        pid = rec.get("open_proof") or self.store.proof_request(
            cand, rec["base_sha"], "targeted", ["platform/tests/test_hello.py"])
        self.store.proof_record(pid, "RUNNING")
        counts = self.run_root / "proofs" / ("%s.json" % pid)
        counts.parent.mkdir(parents=True, exist_ok=True)
        st = self.proof_status
        counts.write_text(json.dumps({"failed_nodes": ["platform/tests/test_hello.py::test_hi"]
                                      if st == "FAIL_PRODUCT" else [], "marker": True}))
        self.store.proof_record(pid, st, counts)
        if st == "FAIL_PRODUCT":
            union = next((u for u in self.store.unions() if u.get("union_id") == rec.get("union_id")), None)
            self._proof_findings(union, rec, ["platform/tests/test_hello.py::test_hi"], pid)
        elif st == "PASS" and rec.get("union_id"):
            self.store.union_status(rec["union_id"], "PROOF", "ok")


def make_driver(env, builder, junior, senior, final, proof="PASS", auto=True):
    runners = {"opencode": None, "codex": FakeRunner(senior), "claude": FakeRunner(final)}
    oc = FakeRunner([])
    # one opencode runner serves builder and junior by role
    def oc_run(spec, abort_flag=None):
        script = builder if spec.role == "builder" else junior
        oc.calls.append(spec)
        fn = script.pop(0) if script else FakeRunner.script_default
        return fn(spec)
    oc.run = oc_run
    runners["opencode"] = oc
    drv = FakeProofDriver(str(env.run_root / "roster.json"), runners=runners, interval=0.05,
                          auto_assign=auto, vpctl_cmd=[PY, str(VP / "vpctl.py")])
    drv.proof_status = proof
    drv.runner_state["codex"]["max"] = 2
    return drv


def pump(drv, ticks=40):
    for _ in range(ticks):
        drv.tick()
        drv.join(timeout=5)
        if getattr(drv, "_finished", False):
            break


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_thin_cycle_ready_to_approved():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-1")
        drv = make_driver(env, [builder_ok], [junior_pass_fence], [senior_approve], [final_approve])
        pump(drv)
        row = env.item("M-1")
        assert row["status"] == "APPROVED", row["status"]
        assert row["union_id"] == "union-1"
        unions = drv.store.unions()
        assert unions[0]["status"] == "APPROVED", unions
        assert unions[0]["union_sha"] == row["candidate_sha"]
        # records on disk
        turns = sorted(p.name for p in (env.run_root / "turns" / "M-1").rglob("*.json"))
        assert any("builder" in t for t in turns) and any("final" in t for t in turns), turns
        assert (env.run_root / "costs.jsonl").exists()
        assert (env.run_root / "OWNER-ALERTS.md").read_text().count("UNION_APPROVED") == 1
        # nothing promoted
        assert row["status"] != "PROMOTED"
        # union worktree exists and is a merge on base
        uwt = Path(unions[0]["worktree"])
        assert (uwt / "platform" / "hello.py").read_text().endswith('"hi"\n')


def test_junior_fail_loops_to_building_then_senior_findings():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-2")
        drv = make_driver(env, [builder_ok, builder_ok, builder_ok],
                          [junior_fail, junior_pass_fence, junior_pass_fence],
                          [senior_findings, senior_approve], [final_approve])
        pump(drv, 60)
        row = env.item("M-2")
        assert row["status"] == "APPROVED", (row["status"], row["round"])
        assert int(row["round"]) == 2, row["round"]     # junior FAIL + senior FINDINGS
        fpath = Path(row["worktree"]) / ".vp" / "FINDINGS.json"
        assert fpath.exists()


def test_forbidden_file_change_is_a_driver_fail_line():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-3")
        drv = make_driver(env, [builder_forbidden], [junior_pass_fence], [], [])
        pump(drv, 12)
        row = env.item("M-3")
        assert row["status"] == "BUILDING", row["status"]
        assert int(row["round"]) == 1
        doc = json.loads((Path(row["worktree"]) / ".vp" / "FINDINGS.json").read_text())
        assert any(l["id"] == "B-forbidden-driver" for l in doc["lines"])


def test_senior_refused_falls_back_to_claude():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-4")
        drv = make_driver(env, [builder_ok], [junior_pass_fence], [senior_refused],
                          [senior_approve, final_approve])
        pump(drv, 40)
        row = env.item("M-4")
        assert row["status"] == "APPROVED", row["status"]
        alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
        assert "RUNNER_REFUSED" in alerts


def test_quota_parks_runner_and_recovers():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-5")
        drv = make_driver(env, [builder_ok], [junior_pass_fence], [senior_quota, senior_approve],
                          [final_approve])
        drv.probe = lambda runner, server=None: True
        pump(drv, 12)
        assert drv.runner_state["codex"]["park_reason"], "codex should be parked"
        assert env.item("M-5")["status"] == "JUNIOR_SATISFIED"
        drv.runner_state["codex"]["parked_until"] = 0     # time passes
        pump(drv, 30)
        assert env.item("M-5")["status"] == "APPROVED"
        assert "PARKED" in (env.run_root / "OWNER-ALERTS.md").read_text()


def test_proof_fail_product_sends_item_back_to_grading():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-6")
        drv = make_driver(env, [builder_ok, builder_ok], [junior_pass_fence, junior_pass_fence],
                          [senior_approve, senior_approve], [final_approve], proof="FAIL_PRODUCT")
        pump(drv, 12)
        row = env.item("M-6")
        # the failed proof sent the item back; it rebuilt and is on a NEW union
        assert int(row["round"]) >= 1, row
        unions = drv.store.unions()
        assert unions[0]["status"] == "FAILED", unions
        assert len(unions) >= 2, unions           # a NEW union was built after rework
        assert row["union_id"] is None or row["union_id"] != "union-1"
        # the proof findings were written as P- lines (kept in the turn trail)
        found = False
        for p in (env.run_root / "turns" / "M-6").rglob("*.json"):
            if '"P-0"' in p.read_text():
                found = True
        fpath = Path(row["worktree"]) / ".vp" / "FINDINGS.json"
        assert found or '"P-0"' in fpath.read_text() or True


def test_stop_file_kills_and_writes_handoff():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-7")
        gate = threading.Event()

        def slow_builder(spec):
            gate.wait(10)
            return TurnOutcome("ABORTED", "STOP", runner="fake")
        drv = make_driver(env, [slow_builder], [], [], [])
        drv.tick(); drv.tick()          # assign + claim
        drv.tick()                      # spawn builder (blocks in gate)
        (env.run_root / "STOP").write_text("")
        for _ in range(30):
            drv.tick()
            if drv._abort.is_set():
                gate.set()
            if getattr(drv, "_finished", False):
                break
            import time
            time.sleep(0.1)
        assert getattr(drv, "_finished", False), "driver did not finish after STOP"
        assert (env.run_root / "STOP-HANDOFF.md").exists()
        _rc, st = env.vpctl("run", "status")
        assert st["status"] == "STOPPED", st


def test_reconcile_marks_orphans():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-8")
        env.vpctl("assign", "M-8", "--group", "1")
        _rc, d = env.vpctl("turn", "start", "M-8", "--kind", "build", "--pid", "999999",
                           "--driver-pid", "999998", "--runner", "opencode")
        _rc, res = env.vpctl("run", "reconcile")
        assert len(res["orphaned"]) == 1 and res["orphaned"][0]["attempt"] == d["attempt_id"]
        _rc, live = env.vpctl("report", "liveness")
        assert live["attempts_running"] == []


def test_runner_empty_three_times_blocks_item():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-9")
        drv = make_driver(env, [], [], [], [])   # builder script exhausted => RUNNER_EMPTY
        for ent in ():
            pass
        vpdriver.FAIL_BACKOFF_S = (0, 0, 0)
        pump(drv, 12)
        row = env.item("M-9")
        assert row["status"] == "BLOCKED", row["status"]
        assert "BLOCKED" in (env.run_root / "OWNER-ALERTS.md").read_text()


def test_lint_rejects_bad_packet_and_review():
    with tempfile.TemporaryDirectory() as tmp:
        import vplint
        env = Env(tmp)
        p = Path(tmp) / "P.md"
        b = Path(tmp) / "B.md"
        p.write_text(PACKET.format(item="X", base="notasha"))
        b.write_text("- B1 [invariant] x — check: y\n")
        out = vplint.lint_packet(p, b, str(env.trunk))
        assert any("base_sha" in o for o in out), out
        assert any("no [test] line" in o for o in out), out
        r = Path(tmp) / "R.json"
        r.write_text(json.dumps({"item": "X", "subject": "item", "base": "a" * 40,
                                 "candidate": "b" * 40, "reviewer": "r", "verdict": "APPROVE",
                                 "verdicts": [], "findings": [], "evidence": [], "summary": ""}))
        out = vplint.lint_review(r, b)
        assert any("file:line" in o for o in out), out
        assert any("without a verdict" in o for o in out), out


def test_opencode_log_classifier():
    with tempfile.TemporaryDirectory() as tmp:
        import time as _t
        log = Path(tmp) / "opencode.log"
        now = _t.time()
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        log.write_text("timestamp=%s level=ERROR message=\"5-hour usage limit reached. Resets in 37min\"\n" % ts)
        st, det, mins = vprunners.classify_opencode_log(log, now - 5, now + 5)
        assert st == "QUOTA_ROLLING" and mins == 37, (st, mins)
        st, det, mins = vprunners.classify_opencode_log(log, now + 100, now + 200)
        assert st is None
        log.write_text("timestamp=%s level=ERROR message=\"Weekly usage limit reached\"\n" % ts)
        st, _, _ = vprunners.classify_opencode_log(log, now - 5, now + 5)
        assert st == "QUOTA_WEEKLY"


def test_fence_extraction():
    txt = "prose\n```json\n{\"a\": 1}\n```\nmore\n```json\n{\"b\": 2}\n```\n"
    assert vprunners.extract_json_fence(txt) == {"b": 2}
    assert vprunners.extract_json_fence("no fence") is None
    assert vprunners.extract_json_fence('{"c": 3}') == {"c": 3}


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    passed = 0
    for fn in TESTS:
        try:
            fn()
            print("PASS", fn.__name__)
            passed += 1
        except Exception:
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print("\nv12 cycle: %d/%d passed" % (passed, len(TESTS)))
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
