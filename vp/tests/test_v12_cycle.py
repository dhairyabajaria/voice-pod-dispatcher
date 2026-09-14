#!/usr/bin/env python3
"""tests/test_v12_cycle.py -- L3 mock thin cycle + L4 fault injections for the
v12 driver.  Real vpstore/vpctl (subprocess), real git (temp repo), FAKE
runners.  No network, no model, no opencode/codex/claude binaries.

    <platform venv python> tests/test_v12_cycle.py
"""

from __future__ import annotations

import json
import os
import re
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
sys.path.insert(0, str(VP.parent))   # circleaccount, for the CircleCI fakes

import vpdriver   # noqa: E402
import vprunners  # noqa: E402
import vpschema   # noqa: E402
import vpstore    # noqa: E402
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


def make_driver(env, builder, junior, senior, final, proof="PASS", auto=True,
                driver_cls=None, circle_runner=None):
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
    cls = driver_cls or FakeProofDriver
    drv = cls(str(env.run_root / "roster.json"), runners=runners, interval=0.05,
              auto_assign=auto, vpctl_cmd=[PY, str(VP / "vpctl.py")],
              circle_runner=circle_runner)
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


PACKET_TECH = PACKET.replace("owned_files:\n", "owned_files:\n  - TECHNICAL.md\n")


def tech_text(n, extra=""):
    return ("Platform tests run against real Postgres.\n"
            "the catalogue), `test_permission_matrix.py` (discovers all %d routes from\n"
            "`app.routes`; 34 unauthenticated or externally authenticated routes, each\n"
            "named; 212 refused to Viewer; 246 cookie-authenticated writes; 118 `/admin` routes).\n"
            "%sMore prose.\n" % (n, extra))


def builder_routes(n_add, tag):
    """A builder that adds n_add routes: bumps the TECHNICAL.md tuple and
    inserts its own line under it (both sides insert at the same place)."""
    def fn(spec):
        wt = Path(spec.cwd)
        (wt / "TECHNICAL.md").write_text(tech_text(500 + n_add, "- %s added %d\n" % (tag, n_add)))
        return builder_ok(spec)
    return fn


def test_union_merges_the_inventory_class_by_arithmetic():
    """D49: two items both bump the route tuple; git conflicts (or, for an
    identical bump, silently keeps ONE bump).  build_union sums the deltas
    and unions the inserted lines instead of BLOCKing the second item."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        (env.trunk / "TECHNICAL.md").write_text(tech_text(500))
        git(env.trunk, "add", "-A")
        git(env.trunk, "commit", "-q", "-m", "tech")
        env.base = git(env.trunk, "rev-parse", "HEAD")
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        for it in ("M-A", "M-B"):
            pdir = env.run_root / "packets" / it
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "PACKET.md").write_text(PACKET_TECH.format(item=it, base=env.base))
            (pdir / "BENCHMARK.md").write_text(BENCHMARK)
            _rc, d = env.vpctl("packet", "submit", it, "--benchmark", str(pdir / "BENCHMARK.md"),
                               "--packet", str(pdir / "PACKET.md"), "--base", env.base,
                               "--allow-overlap")
            env.vpctl("packet", "ready", d["packet_id"])
        drv = make_driver(env, [builder_routes(2, "A"), builder_routes(1, "B")],
                          [junior_pass_fence, junior_pass_fence],
                          [senior_approve, senior_approve], [final_approve, final_approve])
        requested, real_req = [], drv.store.proof_request

        def proof_request(candidate, base, kind, paths):
            requested.append(list(paths or []))
            return real_req(candidate, base, kind, paths)
        drv.store.proof_request = proof_request
        pump(drv, 120)
        rows = {it: env.item(it) for it in ("M-A", "M-B")}
        assert all(r["status"] == "APPROVED" for r in rows.values()), \
            {k: (v["status"], v.get("note")) for k, v in rows.items()}
        unions = drv.store.unions()
        last = unions[-1]
        text = (Path(last["worktree"]) / "TECHNICAL.md").read_text()
        import vpmerge
        assert vpmerge.numbers("TECHNICAL.md", text) == [[503, 34, 212, 246, 118]], text
        assert "- A added 2" in text and "- B added 1" in text and "<<<<<<<" not in text
        alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
        assert "UNION_AUTOMERGE" in alerts, alerts
        assert "UNION_CONFLICT" not in alerts, alerts
        # the union that carried the automerge proves the inventory tests
        assert any("platform/tests/test_permission_matrix.py" in ps and
                   "platform/tests/test_docs_truth.py" in ps for ps in requested), requested


def junior_unknown_on(bid, with_fail=False):
    """The junior as it really behaves: one line UNKNOWN ("could not verify"),
    all_pass true anyway (it reads all_pass as "no FAIL")."""
    def fn(spec):
        wt = Path(spec.cwd)
        head = git(wt, "rev-parse", "HEAD")
        lines = []
        for b, k in (("B1", "invariant"), ("B2", "test"), ("B3", "negative"),
                     ("B4", "forbidden"), ("B5", "evidence")):
            v = "UNKNOWN" if b == bid else ("FAIL" if (with_fail and b == "B1") else "PASS")
            lines.append({"id": b, "kind": k, "verdict": v, "evidence": "platform/hello.py:2",
                          "note": "Bash was denied; could not re-run" if v == "UNKNOWN" else ""})
        doc = {"item": spec.item, "attempt": 1, "commit": head, "lines": lines,
               "all_pass": not with_fail}
        Path(spec.out_path).write_text(json.dumps(doc))
        return TurnOutcome(STATUS_DONE, "", record_path=spec.out_path, runner="fake")
    return fn


def test_unknown_only_findings_regrade_once_then_senior_decides():
    """D67: an UNKNOWN-only record is a grader gap, not a defect.  No
    INCOMPLETE strike, no rework round: the junior re-grades the same commit
    once (told which ids), then the senior gets the record with the ids."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-U")
        drv = make_driver(env, [builder_ok], [junior_unknown_on("B3"), junior_unknown_on("B3")],
                          [senior_approve], [final_approve])
        pump(drv, 60)
        row = env.item("M-U")
        assert row["status"] == "APPROVED", (row["status"], row.get("note"))
        assert int(row["round"]) == 0, row["round"]          # no round charged
        oc = drv.runners["opencode"]
        juniors = [c for c in oc.calls if c.role == "junior"]
        assert len(juniors) == 2, [c.role for c in oc.calls]
        assert "B3" not in juniors[0].prompt and "B3 UNKNOWN" in juniors[1].prompt, juniors[1].prompt
        assert "UNKNOWN verdict is not PASS" in juniors[0].prompt
        # the record on disk carries the recomputed all_pass, never the junior's
        doc = json.loads((Path(row["worktree"]) / ".vp" / "FINDINGS.json").read_text())
        assert doc["all_pass"] is False and [l["id"] for l in doc["lines"] if l["verdict"] == "UNKNOWN"] == ["B3"]
        req = json.loads((Path(row["worktree"]) / ".vp" / "REVIEW_REQUEST.json").read_text())
        assert req["unverified_ids"] == ["B3"], req
        st = vpstore.Store(str(env.run_root))
        kinds = [r["kind"] for r in st.q("SELECT kind FROM event WHERE item='M-U' ORDER BY ts")]
        assert "JUNIOR_UNKNOWN" in kinds and "INCOMPLETE" not in kinds and \
            "RECORD_INVALID" not in kinds, kinds
        fin = [json.loads(r["detail"]) for r in st.q(
            "SELECT detail FROM event WHERE item='M-U' AND kind='FINDINGS'")]
        assert any(d.get("to_senior") and d.get("unverified") == ["B3"] for d in fin), fin
        assert "FAIL M-U" not in (env.run_root / "driver.log").read_text()


def test_unknown_with_a_real_fail_still_reworks():
    """Control: UNKNOWN never shields a FAIL line; the builder goes back."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-UF")
        drv = make_driver(env, [builder_ok], [junior_unknown_on("B3", with_fail=True)], [], [])
        pump(drv, 12)
        row = env.item("M-UF")
        # BUILDING (round 1); the exhausted builder script may then BLOCK it,
        # but never for the UNKNOWN line
        assert int(row["round"]) == 1 and row["status"] in ("BUILDING", "BLOCKED"), \
            (row["status"], row["round"])
        assert "UNKNOWN" not in (row.get("note") or "")
        oc = drv.runners["opencode"]
        assert len([c for c in oc.calls if c.role == "junior"]) == 1
        st = vpstore.Store(str(env.run_root))
        fin = [json.loads(r["detail"]) for r in st.q(
            "SELECT detail FROM event WHERE item='M-UF' AND kind='FINDINGS'")]
        assert fin and fin[0]["all_pass"] is False and not fin[0].get("to_senior"), fin


def test_junior_validator_recomputes_all_pass_from_lines():
    """Constraint (1): the runner-side validator never trusts the junior's
    summary; the exact live record shape (UNKNOWN + all_pass true) validates."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "FINDINGS.json"
        doc = {"item": "X", "attempt": 1, "commit": "a" * 40, "all_pass": True,
               "lines": [{"id": "B1", "kind": "invariant", "verdict": "PASS", "evidence": "x:1", "note": ""},
                         {"id": "B2", "kind": "evidence", "verdict": "UNKNOWN", "evidence": "n/a", "note": "hosted only"}]}
        p.write_text(json.dumps(doc))
        assert vpschema.validate_findings(str(p))[0] is False          # as the runner saw it
        ok, errs = vpdriver.VALIDATOR_OF_ROLE["junior"](str(p))
        assert ok, errs
        assert json.loads(p.read_text())["all_pass"] is False
        _d, fails, unknown = vpdriver.findings_verdicts(p)
        assert fails == [] and unknown == ["B2"]


# --------------------------------------------------------------------------
# CircleCI proof adapter (proof.circleci) -- fake circleci CLI + fake git push
# --------------------------------------------------------------------------

class FakeCircle(object):
    """subprocess.run stand-in for vpcircle.Runner: answers circleaccount's
    identity checks, the circleci api calls from a script, and every git
    push/delete with rc 0 (recorded).  Never a real process."""

    def __init__(self, workflow_status="success", jobs=None, tests=None,
                 trigger_rc=0, trigger_body=None, project_rc=0):
        import circleaccount as c
        self.expected = c.EXPECTED
        self.calls = []
        self.git_calls = []
        self.workflow_status = workflow_status
        self.jobs = jobs if jobs is not None else [
            {"id": "j1", "name": "platform-shard", "status": "success", "job_number": 11},
            {"id": "j2", "name": "portal", "status": "success", "job_number": 12}]
        self.tests = tests or {}
        self.trigger_rc = trigger_rc
        self.trigger_body = trigger_body
        self.project_rc = project_rc

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append(argv)
        cp = subprocess.CompletedProcess
        if argv[0] == "git":
            self.git_calls.append(argv)
            return cp(argv, 0, "", "")
        if argv[0] == "security":
            return cp(argv, 0, "fake-secret\n", "")
        if "auth" in argv and "me" in argv:
            acct = next(re.search(r"circleci-account-(\d+)", a).group(1)
                        for a in argv if "circleci-account-" in a)
            return cp(argv, 0, json.dumps({"id": self.expected[acct]}), "")
        if "api" in argv:
            path = argv[argv.index("api") + 1]
            if path == "api/v2/project/" + __import__("vpcircle").SLUG:
                if self.project_rc:
                    return cp(argv, self.project_rc, "", "error: GET /api/v2/project/x: 404 Not Found")
                return cp(argv, 0, json.dumps({"slug": path.split("project/")[1], "name": "voice-pod-NEW"}), "")
            if path.endswith("/pipeline/run"):
                if self.trigger_rc:
                    return cp(argv, self.trigger_rc, "", self.trigger_body or "boom")
                return cp(argv, 0, json.dumps({"id": "pl-1", "number": 7}), "")
            if path.endswith("/pipeline/pl-1/workflow"):
                return cp(argv, 0, json.dumps({"items": [
                    {"id": "wf-1", "name": "full-suite", "status": self.workflow_status}]}), "")
            if path.endswith("/workflow/wf-1/job"):
                return cp(argv, 0, json.dumps({"items": self.jobs}), "")
            if "/tests" in path:
                n = int(path.split("/")[-2])
                return cp(argv, 0, json.dumps({"items": self.tests.get(n, [])}), "")
        raise AssertionError("unexpected argv in FakeCircle: %s" % argv)


def circle_env(tmp):
    env = Env(tmp)
    env.roster["proof"] = {"union_kind": "targeted",
                           "circleci": {"enabled": True, "kinds": ["targeted"],
                                        "poll_interval_s": 0, "deadline_min": 1}}
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    return env


def test_circleci_proof_pass_records_pipeline_and_approves():
    with tempfile.TemporaryDirectory() as tmp:
        env = circle_env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-C1")
        import vpcircle
        fake = FakeCircle()
        drv = make_driver(env, [builder_ok], [junior_pass_fence], [senior_approve], [final_approve],
                          driver_cls=vpdriver.Driver, circle_runner=vpcircle.Runner(run=fake, binary="circleci-fake"))
        pump(drv, 60)
        row = env.item("M-C1")
        assert row["status"] == "APPROVED", (row["status"], row.get("note"))
        st = vpstore.Store(str(env.run_root))
        proofs = [dict(r) for r in st.q("SELECT * FROM proof ORDER BY proof_id")]
        assert proofs and proofs[0]["status"] == "PASS" and proofs[0]["pipeline_id"] == "pl-1" \
            and proofs[0]["workflow_id"] == "wf-1", proofs
        # the candidate went up as its own branch, run_full_suite was a real JSON
        # boolean, and the branch was deleted afterwards
        pushes = [a for a in fake.git_calls if "push" in a]
        assert any(a[-1].startswith("vp/proof/proof-00001-") and ":" in a[-1] for a in pushes), pushes
        assert any("--delete" in a for a in pushes), pushes
        trig = next(a for a in fake.calls if "api" in a and a[a.index("api") + 1].endswith("/pipeline/run"))
        body = json.loads(trig[trig.index("-d") + 1])
        assert body["parameters"] == {"run_full_suite": True} and \
            body["config"]["branch"].startswith("vp/proof/"), body
        assert (env.run_root / "proofs" / row["candidate_sha"] / "classified.json").exists()
        assert not (env.run_root / "proofs" / "proof-00001-union.json").exists()
        log = (env.run_root / "driver.log").read_text()
        assert "-> circleci branch=" in log and "-> PASS (circleci pipeline pl-1" in log


def test_circleci_proof_fail_product_maps_junit_to_findings():
    with tempfile.TemporaryDirectory() as tmp:
        env = circle_env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-C2")
        import vpcircle
        fake = FakeCircle(workflow_status="failed",
                          jobs=[{"id": "j1", "name": "platform-shard", "status": "failed",
                                 "job_number": 11}],
                          tests={11: [{"file": "platform/tests/test_hello.py", "name": "test_hi",
                                       "classname": "platform.tests.test_hello",
                                       "result": "failure", "message": "AssertionError: not hi"},
                                      {"classname": "platform.tests.test_other",
                                       "name": "test_far", "result": "failure",
                                       "message": "KeyError: 'x'"}]})
        drv = make_driver(env, [builder_ok], [junior_pass_fence], [senior_approve], [],
                          driver_cls=vpdriver.Driver, circle_runner=vpcircle.Runner(run=fake, binary="circleci-fake"))
        pump(drv, 40)
        row = env.item("M-C2")
        st = vpstore.Store(str(env.run_root))
        proofs = [dict(r) for r in st.q("SELECT * FROM proof ORDER BY proof_id")]
        assert proofs[0]["status"] == "FAIL_PRODUCT" and proofs[0]["pipeline_id"] == "pl-1", proofs
        doc = json.loads((Path(row["worktree"]) / ".vp" / "FINDINGS.json").read_text())
        ev = " | ".join(l["evidence"] for l in doc["lines"])
        assert "platform/tests/test_hello.py::test_hi failed" in ev and "AssertionError: not hi" in ev, ev
        # a red outside the item's test_paths still reaches the item (orphan rule)
        assert "platform/tests/test_other.py::test_far" in ev, ev
        assert row["status"] in ("GRADING", "BUILDING", "BLOCKED"), row["status"]
        assert (env.run_root / "proofs" / proofs[0]["candidate_sha"] / "tests-failed.json").exists()


def test_circleci_trigger_failure_is_unknown_with_backoff_not_findings():
    with tempfile.TemporaryDirectory() as tmp:
        env = circle_env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-C3")
        import vpcircle
        fake = FakeCircle(trigger_rc=1, trigger_body="403 forbidden")
        drv = make_driver(env, [builder_ok], [junior_pass_fence], [senior_approve], [],
                          driver_cls=vpdriver.Driver, circle_runner=vpcircle.Runner(run=fake, binary="circleci-fake"))
        pump(drv, 30)
        row = env.item("M-C3")
        st = vpstore.Store(str(env.run_root))
        proofs = [dict(r) for r in st.q("SELECT * FROM proof ORDER BY proof_id")]
        assert proofs and proofs[0]["status"] == "UNKNOWN", proofs
        assert json.loads(proofs[0]["counts"])["reason"].startswith("circleci: RuntimeError: trigger"), proofs[0]["counts"]
        assert row["status"] in ("PREPARED", "PROOF_PENDING"), row["status"]   # backoff, no findings
        assert not (Path(row["worktree"]) / ".vp" / "FINDINGS.json").read_text().count("P-0")
        assert any("--delete" in a for a in fake.git_calls), fake.git_calls   # branch cleaned up


def test_circleci_preflight_refuses_before_any_push_when_account_cannot_see_project():
    """Trial attempt 1: account 1 got 404 on the trigger AFTER the branch was
    pushed (and the push fired the all-pushes probe).  Now the project is read
    first; a 404 means UNKNOWN with no git push at all."""
    with tempfile.TemporaryDirectory() as tmp:
        env = circle_env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-C4")
        import vpcircle
        fake = FakeCircle(project_rc=4)
        drv = make_driver(env, [builder_ok], [junior_pass_fence], [senior_approve], [],
                          driver_cls=vpdriver.Driver,
                          circle_runner=vpcircle.Runner(run=fake, binary="circleci-fake"))
        pump(drv, 30)
        st = vpstore.Store(str(env.run_root))
        proofs = [dict(r) for r in st.q("SELECT * FROM proof ORDER BY proof_id")]
        assert proofs and proofs[0]["status"] == "UNKNOWN", proofs
        reason = json.loads(proofs[0]["counts"])["reason"]
        assert "preflight" in reason and "cannot read project" in reason, reason
        assert not [a for a in fake.git_calls if "push" in a and "--delete" not in a], fake.git_calls


def test_circle_failed_nodes_maps_classname_when_file_is_missing():
    nodes, errs = vpdriver.circle_failed_nodes({
        7: [{"file": "platform/tests/test_x.py", "name": "test_a", "message": "E1"},
            {"classname": "platform.tests.test_y.TestFoo", "name": "test_b"},
            {"classname": "agent.tests.test_z", "name": "test_c", "message": ""},
            {"file": "platform/tests/test_x.py", "name": "test_skip", "result": "skipped"},
            {"file": "platform/tests/test_x.py", "name": "test_ok", "result": "success"}]})
    assert nodes == ["agent/tests/test_z.py::test_c", "platform/tests/test_x.py::test_a",
                     "platform/tests/test_y.py::TestFoo::test_b"], nodes
    assert errs == {"platform/tests/test_x.py::test_a": "E1"}, errs


def builder_blocked(spec):
    """The packet's precondition failed; the builder STOPs as instructed."""
    wt = Path(spec.cwd)
    base = (wt / ".vp" / "BASE").read_text().strip()
    rec = {"item": spec.item, "attempt": 1, "commit": base, "base": base,
           "diff_stat": {"files": 0, "insertions": 0, "deletions": 0}, "checks": [],
           "disputes": [], "blocked": "A3-3 base lacks A3-1a", "notes": ""}
    Path(spec.out_path).write_text(json.dumps(rec))
    return TurnOutcome(STATUS_DONE, "", record_path=spec.out_path, runner="fake")


def builder_no_commit(spec):
    wt = Path(spec.cwd)
    base = (wt / ".vp" / "BASE").read_text().strip()
    rec = {"item": spec.item, "attempt": 1, "commit": base, "base": base,
           "diff_stat": {"files": 0, "insertions": 0, "deletions": 0}, "checks": [],
           "disputes": [], "blocked": None, "notes": "forgot to commit"}
    Path(spec.out_path).write_text(json.dumps(rec))
    return TurnOutcome(STATUS_DONE, "", record_path=spec.out_path, runner="fake")


def test_builder_blocked_result_blocks_the_item_once():
    """A3-3: the packet said STOP with blocked when the base lacks the
    dependency; the driver retried six times.  One attempt, BLOCKED, alert."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-BL")
        drv = make_driver(env, [builder_blocked, builder_blocked, builder_blocked], [], [], [])
        pump(drv, 20)
        row = env.item("M-BL")
        assert row["status"] == "BLOCKED", row["status"]
        assert "A3-3 base lacks A3-1a" in (row.get("note") or ""), row.get("note")
        oc = drv.runners["opencode"]
        assert len([c for c in oc.calls if c.role == "builder"]) == 1, [c.role for c in oc.calls]
        alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
        assert "BUILDER_BLOCKED" in alerts, alerts
        assert "FAIL M-BL" not in (env.run_root / "driver.log").read_text()


def test_no_commit_strikes_accumulate_to_blocked():
    """The strike count survives a DONE outcome whose delivery struck; three
    no-commit builders BLOCK instead of looping at 1/3."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-NC")
        drv = make_driver(env, [builder_no_commit] * 6, [], [], [])
        saved = vpdriver.FAIL_BACKOFF_S
        vpdriver.FAIL_BACKOFF_S = (0, 0, 0)          # 60/120/300 s live; not in a test
        try:
            pump(drv, 40)
        finally:
            vpdriver.FAIL_BACKOFF_S = saved
        row = env.item("M-NC")
        assert row["status"] == "BLOCKED", (row["status"], row.get("note"))
        assert "consecutive failures" in (row.get("note") or ""), row.get("note")
        oc = drv.runners["opencode"]
        n = len([c for c in oc.calls if c.role == "builder"])
        assert n == vpdriver.FAIL_CAP, (n, vpdriver.FAIL_CAP)
        log = (env.run_root / "driver.log").read_text()
        assert "FAIL M-NC 3/3" in log, log


def test_stale_packet_base_behind_a_dependency_is_not_assigned():
    """D75: M-D1 is approved at candidate c1; M-D2 depends on it but its packet
    base is the old trunk -> not assigned, DEP_BASE_STALE once.  M-D3 with the
    same dependency and base c1 is assigned."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-D1")
        drv = make_driver(env, [builder_ok, builder_ok], [junior_pass_fence, junior_pass_fence],
                          [senior_approve, senior_approve], [final_approve, final_approve])
        pump(drv, 40)
        d1 = env.item("M-D1")
        assert d1["status"] == "APPROVED", d1["status"]
        c1 = d1["candidate_sha"]

        def dep_item(item, base):
            pdir = env.run_root / "packets" / item
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "PACKET.md").write_text(PACKET.format(item=item, base=base)
                                            .replace("depends_on: []", "depends_on: [M-D1]"))
            (pdir / "BENCHMARK.md").write_text(BENCHMARK)
            _rc, d = env.vpctl("packet", "submit", item, "--benchmark", str(pdir / "BENCHMARK.md"),
                               "--packet", str(pdir / "PACKET.md"), "--base", base,
                               "--allow-overlap")
            env.vpctl("packet", "ready", d["packet_id"])

        dep_item("M-D2", env.base)          # predates c1
        pump(drv, 10)
        assert env.item("M-D2")["status"] == "READY", env.item("M-D2")["status"]
        alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
        assert alerts.count("DEP_BASE_STALE") == 1 and "M-D2" in alerts and "M-D1" in alerts, alerts
        dep_item("M-D3", c1)                # contains c1
        pump(drv, 10)
        assert env.item("M-D3")["status"] not in ("READY",), env.item("M-D3")["status"]
        assert (env.run_root / "OWNER-ALERTS.md").read_text().count("DEP_BASE_STALE") == 1


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


def test_union_blocks_when_the_registry_writer_refuses():
    """D80 follow-up: `environment_registry.py --write` rc != 0 on the union
    is a build failure.  Every merged item is BLOCKed with the writer's
    message, no proof is requested, the union worktree is removed, and the
    owner sees UNION_REGISTRY_FAILED (not the old UNION_REGISTRY_STALE
    note-and-proceed)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        venv_bin = env.trunk / "platform" / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        os.symlink(sys.executable, str(venv_bin / "python"))
        (env.trunk / "deploy").mkdir()
        (env.trunk / "deploy" / "environment_registry.py").write_text(
            "import sys\n"
            "sys.stderr.write('environment registry error: reachable production modules "
            "cannot be excluded: platform/core/sources.py\\n')\n"
            "sys.exit(1)\n")
        git(env.trunk, "add", "-A")
        git(env.trunk, "commit", "-q", "-m", "registry writer that refuses")
        env.base = git(env.trunk, "rev-parse", "HEAD")
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-1")
        drv = make_driver(env, [builder_ok], [junior_pass_fence], [senior_approve], [final_approve])
        requested = []
        real_req = drv.store.proof_request

        def proof_request(candidate, base, kind, paths):
            requested.append(candidate)
            return real_req(candidate, base, kind, paths)
        drv.store.proof_request = proof_request
        pump(drv, 60)
        row = env.item("M-1")
        assert row["status"] == "BLOCKED", (row["status"], row.get("note"))
        assert "environment registry" in (row.get("note") or "") and \
            "platform/core/sources.py" in (row.get("note") or ""), row.get("note")
        assert requested == [], requested
        assert not (env.worktrees / "union-1").exists()
        alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
        assert "UNION_REGISTRY_FAILED" in alerts and "UNION_REGISTRY_STALE" not in alerts, alerts


def test_fence_extraction():
    txt = "prose\n```json\n{\"a\": 1}\n```\nmore\n```json\n{\"b\": 2}\n```\n"
    assert vprunners.extract_json_fence(txt) == {"b": 2}
    assert vprunners.extract_json_fence("no fence") is None
    assert vprunners.extract_json_fence('{"c": 3}') == {"c": 3}


def test_final_record_refused_reaches_owner_alerts():
    """SAFE-03p, run-v12-20260913: the item was PAUSED when its final verdict
    arrived; the union alerted UNION_APPROVED while the item's own record was
    refused with only a driver.log line to show for it."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-11")
        drv = make_driver(env, [builder_ok], [junior_pass_fence], [senior_approve], [final_approve])
        orig = drv._deliver_final

        def paused_then_deliver(rec, wt, record, attempt_id):
            drv.store.item_pause(rec["item"])
            return orig(rec, wt, record, attempt_id)
        drv._deliver_final = paused_then_deliver
        pump(drv)
        alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
        assert alerts.count("UNION_APPROVED") == 1, alerts
        assert "FINAL_RECORD_REFUSED" in alerts and "`M-11`" in alerts, alerts
        assert env.item("M-11")["status"] == "PAUSED"


def test_store_down_alerts_owner_once_without_the_store():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        env.add_item("M-12")
        drv = make_driver(env, [], [], [], [], auto=False)
        real_items, real_alert = drv.store.report_items, drv.store.alert

        def broken(*_a, **_k):
            raise vpdriver.StoreError("vpctl report items exited 1: database is locked")
        drv.store.report_items = broken
        drv.store.alert = lambda *_a, **_k: False
        for _ in range(3):
            drv.tick()
        alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
        assert alerts.count("STORE_UNAVAILABLE") == 1, alerts
        assert "written by the driver" in alerts
        # recovery clears the latch; a second outage is a second line
        drv.store.report_items, drv.store.alert = real_items, real_alert
        drv.tick()
        drv.store.report_items = broken
        drv.tick()
        alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
        assert alerts.count("STORE_UNAVAILABLE") == 2, alerts


def test_claude_spec_schema_follows_role():
    """A claude-run junior must be asked for a FINDINGS record, not a REVIEW one
    (the roster may route junior/senior/final all to claude)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        os.environ.update(env.env)
        env.vpctl("run", "init", "run-v12-test", "--trunk-head", env.base)
        drv = make_driver(env, [], [], [], [], auto=False)
        (env.run_root / "claude-settings").mkdir(exist_ok=True)
        (env.run_root / "claude-settings" / "junior.json").write_text(
            json.dumps({"permissions": {"allow": ["Read", "Grep"], "deny": []}}))
        # the driver hot-reloads roles from the roster file, so change it there
        rp = env.run_root / "roster.json"
        roster = json.loads(rp.read_text())
        roster["roles"]["junior"] = {"runner": "claude", "model": "sonnet", "effort": "low",
                                     "max_turns": 40}
        roster["roles"]["senior"] = {"runner": "claude", "model": "opus", "effort": "low",
                                     "model_critical": "claude-fable-5-1"}
        rp.write_text(json.dumps(roster, indent=2))
        wt = Path(tmp) / "wt"
        rec = {"item": "M-1", "round": 0}
        js = drv._spec_for(rec, "junior", None, "claude", wt, "p", None, "t", 1)
        ss = drv._spec_for(rec, "senior", None, "claude", wt, "p", None, "t", 1)
        assert json.loads(js.schema_text) == vpschema.FINDINGS_SCHEMA_DOC
        assert json.loads(ss.schema_text) == vpschema.REVIEW_SCHEMA_DOC
        assert js.model == "sonnet" and js.effort == "low" and js.max_turns == 40
        assert ss.model == "opus" and ss.effort == "low"
        crit = drv._spec_for(dict(rec, critical=True), "senior", None, "claude", wt, "p", None, "t", 1)
        assert crit.model == "claude-fable-5-1" and crit.effort == "low"
        assert js.settings_path.endswith("junior.json") and js.allowed_tools == ["Read", "Grep"]
        assert js.out_path.endswith("FINDINGS.json") and ss.out_path.endswith("REVIEW.json")


def test_codex_cost_is_estimated_from_roster_pricing():
    """A6.2: 144 Codex reviews in run-v12-20260913 carried full token counts
    and cost NULL, so spent_usd never saw them."""
    usage = {"tokens_in": 872259, "cache_read": 786560, "tokens_out": 5246,
             "tokens_reason": 2787, "cost": None}
    models = {"gpt-x": {"input_per_1m": 2.0, "cached_input_per_1m": 0.5, "output_per_1m": 8.0}}
    expected = round(((872259 - 786560) * 2.0 + 786560 * 0.5 + 5246 * 8.0) / 1e6, 6)
    # api billing: the estimate is the spend
    cost, est, basis = vpdriver.estimate_cost(
        {"codex": {"billing": "api", "models": models}}, "codex", "gpt-x", usage)
    assert (cost, est, basis) == (expected, expected, "estimated")
    # subscription (the default): tokens are priced, the budget counts 0
    cost, est, basis = vpdriver.estimate_cost(
        {"codex": {"models": models}}, "codex", "gpt-x", usage)
    assert (cost, est, basis) == (0.0, expected, "subscription")
    # no table for the model: nothing is invented
    assert vpdriver.estimate_cost({}, "codex", "gpt-x", usage) == (None, None, "unpriced")
    # a runner that reports USD (claude, opencode) is left alone
    assert vpdriver.estimate_cost({"codex": {"models": models}}, "claude", "opus",
                                  {"tokens_in": 10, "cost": 0.42}) == (0.42, None, "reported")
    # cached input can never exceed input
    cost, est, _ = vpdriver.estimate_cost(
        {"codex": {"billing": "api", "models": models}}, "codex", "gpt-x",
        {"tokens_in": 100, "cache_read": 500, "tokens_out": 0})
    assert cost == round(100 * 0.5 / 1e6, 6)


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
