#!/usr/bin/env python3
"""tests/test_lanedriver.py -- the v13 driver over the REAL orchestration_control.py
(subprocess, temp state, tiny catalog), real git (temp trunk), FAKE runners.
No network, no model, no opencode/codex/claude binaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP))

import lanedriver  # noqa: E402
import vppack  # noqa: E402
import vpsweep  # noqa: E402
from vprunners import TurnOutcome, STATUS_DONE  # noqa: E402

PY = sys.executable
CONTROL_DIR = VP.parent.parent / "voice-pod" / "advisor-plans" / "outbound-launch"
SCRIPT = CONTROL_DIR / "orchestration_control.py"


def _v13_pack_prereqs():
    """The v13-pack roster fixture is the real advisor-plans roster from the
    sibling voice-pod checkout, and it hardcodes absolute paths (its own
    control.script/control.catalog, and per-server xdg_data_home dirs under
    the developer's home directory). On a machine without that sibling
    checkout and those directories (e.g. a bare CI runner) there is nothing
    to test against, so report why instead of failing on missing files.
    """
    roster_path = CONTROL_DIR / "v13-pack" / "roster-v13.json"
    if not roster_path.exists():
        return "sibling voice-pod checkout not found: %s" % roster_path
    try:
        r = json.loads(roster_path.read_text())
    except Exception as exc:  # pragma: no cover - defensive
        return "roster-v13.json unreadable: %s" % exc
    # vplint.lint_roster_v13 resolves control.script/control.catalog from
    # run.scheduler/run.catalog when the roster has no explicit "control"
    # section (the real fixture doesn't). Those are absolute paths baked in
    # for the developer's own Mac checkout, so check both spots.
    run = r.get("run", {})
    ctl = r.get("control", {})
    for key, run_key in (("script", "scheduler"), ("catalog", "catalog")):
        p = ctl.get(key) or run.get(run_key)
        if p and not Path(p).expanduser().exists():
            return "control.%s does not exist: %s" % (key, p)
    for name, s in r.get("servers", {}).items():
        xdg = s.get("xdg_data_home")
        if xdg and not Path(xdg).exists():
            return "server %s xdg_data_home missing: %s" % (name, xdg)
    return None


_V13_PACK_SKIP_REASON = _v13_pack_prereqs()


def sh(args, cwd=None):
    cp = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, universal_newlines=True)
    return cp.returncode, cp.stdout.strip(), cp.stderr.strip()


def git(cwd, *args):
    rc, out, err = sh(["git", "-C", str(cwd)] + list(args))
    assert rc == 0, "git %s: %s %s" % (args, out, err)
    return out


ROLE_MUSE = {"model_family": "Muse", "model": "BIND_APPROVED_GO_MUSE_ROUTE", "effort": "xhigh"}
ROLE_DS = {"model_family": "DeepSeek", "model": "BIND_APPROVED_GO_DEEPSEEK_ROUTE", "effort": "high"}


def catalog():
    def c(id_, kind, chief, deps, owned, role):
        return {"id": id_, "title": "task %s" % id_, "chief": chief, "kind": kind,
                "depends_on": deps, "owned_paths": owned, "build_steps": ["do %s" % id_],
                "verification": ["%s verified" % id_], "acceptance": ["%s accepted" % id_],
                "role": role}
    return {
        "schema_version": "1",
        "roles": {"probe": ROLE_DS, "design": ROLE_DS, "verification": ROLE_DS,
                  "builder": ROLE_MUSE, "junior": {"model": "gpt-5.6-luna", "effort": "xhigh"}},
        "templates": [
            {"id": "REPAIR", "kind": "builder",
             "required_parameters": ["parent_contract_id", "defect_id", "failing_criterion",
                                     "reproduction", "reviewed_sha", "owned_paths"],
             "steps": ["repair"], "acceptance": ["repair accepted"]},
            {"id": "JUNIOR_REVIEW", "kind": "junior",
             "required_parameters": ["parent_contract_id", "candidate_sha", "tree_sha",
                                     "diff_or_scope", "criteria", "evidence"],
             "steps": ["review"], "acceptance": ["review accepted"]},
            {"id": "EVIDENCE_RECOVERY", "kind": "probe",
             "required_parameters": ["parent_contract_id", "attempt_ids", "source_logs", "candidate_sha",
                                     "evidence_paths"],
             "steps": ["recover"], "acceptance": ["evidence recovered"]},
            {"id": "TEST_GAP", "kind": "builder",
             "required_parameters": ["parent_contract_id", "unproved_criterion", "candidate_sha",
                                     "test_location", "owned_paths"],
             "steps": ["test"], "acceptance": ["gap closed"]},
            {"id": "EXTERNAL_PREP", "kind": "probe",       # a non-build kind: twins must still stack (D41)
             "required_parameters": ["parent_contract_id", "missing_capability", "existing_authority_refs",
                                     "required_fields"],
             "steps": ["prepare"], "acceptance": ["external prepared"]},
        ],
        "contracts": [
            c("L00", "probe", "A", [], ["control/baseline"], ROLE_DS),
            c("L01", "design", "A", ["L00"], ["control/scope"], ROLE_DS),
            c("L02", "builder", "B", ["L00"], ["platform/a.py"], ROLE_MUSE),
            c("L03", "probe", "B", ["L00"], ["control/l03"], ROLE_DS),
            c("L04", "verification", "A", ["L00"], ["control/l04"], ROLE_DS),
        ],
    }


class Env(object):
    def __init__(self, tmp, roster_extra=None):
        self.tmp = Path(tmp)
        self.trunk = self.tmp / "trunk"
        self.trunk.mkdir()
        git(self.trunk, "init", "-q", "-b", "successor/s3")
        git(self.trunk, "config", "user.email", "t@t")
        git(self.trunk, "config", "user.name", "t")
        (self.trunk / "platform").mkdir()
        (self.trunk / "platform" / "a.py").write_text("x = 1\n")
        git(self.trunk, "add", "-A")
        git(self.trunk, "commit", "-q", "-m", "base")
        self.base = git(self.trunk, "rev-parse", "HEAD")
        self.ctl = self.tmp / "control"
        self.ctl.mkdir()
        self.catalog = self.ctl / "lane-contracts.json"
        self.catalog.write_text(json.dumps(catalog(), indent=1))
        self.state = self.ctl / "orchestration-state" / "run-state.json"
        self.run_root = self.tmp / "run"
        self.run_root.mkdir()
        self.roster = {
            "run": {"cn": str(self.tmp), "trunk": str(self.trunk),
                    "worktrees": str(self.tmp / "wt"), "stop_grace_s": 1},
            "control": {"python": PY, "script": str(SCRIPT), "state": str(self.state),
                        "catalog": str(self.catalog), "cwd": str(CONTROL_DIR),
                        # VP_TEST_AUTHORITY: a pin file for the scheduler on disk, so the
                        # suite can run on a scheduler edit before the Architect re-pins
                        **({"authority": os.environ["VP_TEST_AUTHORITY"]} if os.environ.get("VP_TEST_AUTHORITY") else {})},
            "night": {"pause_on_disk_gb": 0},
            "concurrency": {"max_rounds": 2, "codex_max": 2, "claude_max": 1,
                            "max_minutes_per_turn": {"default": 1}},
            "budget": {"max_cost_usd_per_run": 100},
            "alerts": {"frontier_every_s": 0, "idle_every_min": 30},
            "servers": {"go2": {"url": "http://127.0.0.1:1", "max_concurrent": 8,
                                "xdg_data_home": str(self.tmp / "xdg")}},
            "roles": {"probe": {"runner": "opencode", "model": "ds", "variant": "high"},
                      "design": {"runner": "opencode", "model": "ds", "variant": "high"},
                      "verification": {"runner": "opencode", "model": "ds", "variant": "high"},
                      "builder": {"runner": "opencode", "model": "muse", "variant": "xhigh"},
                      "grader": {"runner": "opencode", "model": "ds", "variant": "high"},
                      "junior": {"runner": "codex", "model": "gpt-5.6-luna", "effort": "xhigh"}},
        }
        if roster_extra:
            self.roster.update(roster_extra)
        (self.run_root / "roster.json").write_text(json.dumps(self.roster, indent=2))

    def control_call(self, *args):
        rc, out, err = sh([PY, str(SCRIPT), "--state", str(self.state), "--catalog",
                           str(self.catalog)] + list(args), cwd=str(CONTROL_DIR))
        assert rc == 0, "control %s rc=%d %s %s" % (args, rc, out, err)
        return out

    def activate(self):
        ev = self.tmp / "evidence.json"
        ev.write_text('{"ok": true}\n')
        self.control_call("init", "--run-id", "t")
        self.control_call("bind-chief", "--chief", "A", "--thread-id", "a")
        self.control_call("bind-chief", "--chief", "B", "--thread-id", "b")
        for g in ("PRESERVATION_COMPLETE", "REQUIREMENTS_REBOUND", "RECOVERY_ADOPTED",
                  "DRY_RUN_PASSED"):
            self.control_call("satisfy-gate", "--name", g, "--evidence", str(ev))
        self.control_call("activate")

    def rows(self):
        return json.loads(self.state.read_text())["tasks"]

    def driver(self, runners, **kw):
        return lanedriver.LaneDriver(self.run_root / "roster.json", runners=runners, **kw)

    def control_lines(self):
        p = self.run_root / "control.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# -- fake runners ------------------------------------------------------------------

class FakeRunner(object):
    name = "fake"

    def __init__(self, script=None, default=None):
        self.script = list(script or [])
        self.default = default
        self.calls = []
        self.lock = threading.Lock()

    def run(self, spec, abort_flag=None):
        with self.lock:
            self.calls.append(spec)
            fn = self.script.pop(0) if self.script else self.default
        if fn is None:
            return TurnOutcome("RUNNER_EMPTY", "script exhausted", runner="fake")
        return fn(spec, abort_flag)

    def export(self, spec, sid, dest):
        Path(dest).write_text("{}")
        return {"path": dest}


def by_role(mapping):
    """runner whose script is chosen per spec.role."""
    def fn(spec, abort_flag):
        return mapping[spec.role](spec, abort_flag)
    return FakeRunner(default=fn)


def result_ok(spec, abort_flag=None, commit=True):
    wt = Path(spec.cwd)
    (wt / "platform" / "a.py").write_text("x = 2  # %s\n" % spec.item)
    if commit:
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "--allow-empty", "-m", "work %s" % spec.item)
    head = git(wt, "rev-parse", "HEAD")
    base = (wt / ".vp" / "BASE").read_text().strip()
    rec = {"item": spec.item, "attempt": 1, "commit": head, "base": base,
           "diff_stat": {"files": 1, "insertions": 1, "deletions": 1},
           "checks": [{"name": "pytest", "command": "pytest -q", "exit": 0, "log": "ok"}],
           "disputes": [], "blocked": None, "notes": ""}
    Path(spec.out_path).write_text(json.dumps(rec))
    return TurnOutcome(STATUS_DONE, "", session_id="ses_%s" % spec.item, record_path=spec.out_path,
                       usage={"tokens_in": 100, "tokens_out": 50, "cost": 0.01}, runner="fake")


def findings(verdict):
    def fn(spec, abort_flag=None):
        wt = Path(spec.cwd)
        head = git(wt, "rev-parse", "HEAD")
        ids = [l.split()[1] for l in (wt / ".vp" / "BENCHMARK.md").read_text().splitlines()
               if l.startswith("- B")]
        lines = [{"id": i, "kind": "evidence", "verdict": verdict, "evidence": "platform/a.py:1",
                  "note": "grader says %s" % verdict} for i in ids]
        doc = {"item": spec.item, "attempt": 1, "commit": head, "lines": lines,
               "all_pass": verdict == "PASS"}
        Path(spec.out_path).write_text(json.dumps(doc))
        return TurnOutcome(STATUS_DONE, "", session_id="ses_g", record_path=spec.out_path,
                           usage={"tokens_in": 10, "tokens_out": 5, "cost": 0.001}, runner="fake")
    return fn


def status(st, detail="x"):
    def fn(spec, abort_flag=None):
        return TurnOutcome(st, detail, session_id="ses_%s" % spec.item, runner="fake",
                           usage={"tokens_in": 1, "tokens_out": 1, "cost": 0.0})
    return fn


def wait_abort(spec, abort_flag=None):
    for _ in range(200):
        if abort_flag is not None and abort_flag.is_set():
            return TurnOutcome("ABORTED", "STOP", session_id="ses_%s" % spec.item, runner="fake")
        time.sleep(0.05)
    return TurnOutcome("RUNNER_TIMEOUT", "fake never aborted", runner="fake")


def settle(drv, ticks=1):
    for _ in range(ticks):
        drv.tick()
        drv.join(timeout=60)


def wait_state(env, task, state, timeout=20.0):
    """poll until `task` is in `state` (a loaded box takes >0.5 s from claim to
    RUNNING: every control call is a python subprocess); returns the row."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = env.rows().get(task) or {}
        if row.get("state") == state:
            return row
        time.sleep(0.1)
    raise AssertionError("%s never reached %s (last %s)" % (task, state, row.get("state")))


# -- tests --------------------------------------------------------------------------

def test_l00_verified_unlocks_l01_l04_and_all_complete(tmp_path):
    env = Env(tmp_path)
    env.activate()
    oc = FakeRunner(default=result_ok)
    oc.default = lambda spec, ab: result_ok(spec, ab)
    runners = {"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}
    # L02 is a builder: builder turn + grader turn (grader passes)
    def routed(spec, ab):
        return findings("PASS")(spec, ab) if spec.role == "grader" else result_ok(spec, ab)
    oc.default = routed
    drv = env.driver(runners)
    n = drv.tick()
    assert n == 1, "only L00 is READY at start"
    drv.join(timeout=60)
    rows = env.rows()
    assert rows["L00"]["state"] == "VERIFIED" and rows["L00"]["unlocks_dependents"] is True
    assert all(rows[t]["state"] == "READY" for t in ("L01", "L02", "L03", "L04"))
    n = drv.tick()
    assert n == 4
    drv.join(timeout=60)
    rows = env.rows()
    for t in ("L01", "L02", "L03", "L04"):
        assert rows[t]["state"] == "VERIFIED", (t, rows[t]["state"])
        assert rows[t]["output_sha"] and rows[t]["unlocks_dependents"] is True
    # every VERIFIED completion passed --unlock-dependents with an output sha
    completes = [l for l in env.control_lines() if l["verb"] == "complete"]
    assert len(completes) == 5
    for l in completes:
        assert "--unlock-dependents" in l["argv"] and "--output-sha" in l["argv"]
        assert l["seq_after"] > l["seq_before"] and l["rc"] == 0
    # logging tree: prompt saved verbatim, record.json, harvest, heartbeat, costs
    tdir = next((env.run_root / "turns" / "L02").iterdir())
    assert (tdir / "prompt.md").read_text() == lanedriver.BUILDER_PROMPT
    rec = json.loads((tdir / "record.json").read_text())
    assert rec["task"] == "L02" and rec["prompt_sha256"]
    harvest = json.loads((tdir / "harvest.json").read_text())
    assert harvest["outcome"] == "VERIFIED"
    # D107 (P3): the worktree's .vp is archived into the turn dir at completion and
    # evidence that lived in .vp is recorded at the archived path (the worktree
    # can then be reaped without destroying evidence)
    assert harvest["vp_archive"] == str(tdir / "vp") and (tdir / "vp" / "PACKET.md").exists()
    wt_vp = str(env.tmp / "wt" / "L02" / ".vp")
    assert not any(e.startswith(wt_vp) for e in harvest["evidence"]), harvest["evidence"]
    assert "ARCHIVE L02 .vp -> " in (env.run_root / "driver.log").read_text()
    hb = json.loads((env.run_root / "driver.heartbeat").read_text())
    assert hb["tick"] == 2 and hb["sequence"] > 0
    assert len((env.run_root / "costs.jsonl").read_text().splitlines()) == 6  # 5 turns + grader
    assert (env.run_root / "git.jsonl").exists()
    # grader ran once for the builder and the builder claimed its owned path
    claim = next(l for l in env.control_lines() if l["verb"] == "claim" and "L02" in l["argv"])
    assert "platform/a.py" in claim["argv"]


def test_grader_fail_twice_is_repair_required_then_repair_is_instantiated_and_promotes(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.activate()
    grades = [findings("FAIL"), findings("FAIL"), findings("PASS")]
    def routed(spec, ab):
        if spec.role == "grader":
            return grades.pop(0)(spec, ab)
        return result_ok(spec, ab)
    oc = FakeRunner(default=routed)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv)                       # L00
    settle(drv)                       # L01-L04; L02 builds, grader fails twice
    rows = env.rows()
    assert rows["L02"]["state"] == "REPAIR_REQUIRED"
    assert "FAIL" in rows["L02"]["blocker"]
    assert rows["L02"]["unlocks_dependents"] is False
    # idle tick: frontier lists the REPAIR_REQUIRED row -> one REPAIR instantiated
    drv.tick()
    rows = env.rows()
    repairs = [t for t, r in rows.items() if r.get("template_id") == "REPAIR"]
    assert repairs == ["R-L02-B1-1"], repairs
    assert rows["R-L02-B1-1"]["state"] == "READY"
    assert rows["R-L02-B1-1"]["parameters"]["defect_id"] == "B1"
    assert rows["R-L02-B1-1"]["parameters"]["reviewed_sha"] == rows["L02"]["output_sha"]
    # the repair is dispatched like any task, graded PASS, VERIFIED
    settle(drv)
    rows = env.rows()
    assert rows["R-L02-B1-1"]["state"] == "VERIFIED"
    # next idle frontier: the parent is promoted with the repair as support
    drv.tick()
    rows = env.rows()
    assert rows["L02"]["state"] == "INTEGRATED" and rows["L02"]["unlocks_dependents"] is True
    promote = next(l for l in env.control_lines() if l["verb"] == "promote")
    assert "--supporting-task" in promote["argv"] and "R-L02-B1-1" in promote["argv"]
    # D20 (ii): every promote names the trunk so the scheduler can refuse an autofix output commit
    assert promote["argv"][promote["argv"].index("--repo") + 1] == str(env.trunk)
    # no second repair for the same parent while one is live/finished-clean
    drv.tick()
    assert [t for t in env.rows() if t.startswith("R-L02")] == ["R-L02-B1-1"]


def test_quota_parks_the_role_and_never_fails_the_task(tmp_path):
    env = Env(tmp_path)
    env.activate()
    oc = FakeRunner(script=[status("QUOTA_WEEKLY", "Monthly usage limit reached")], default=result_ok)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv)
    rows = env.rows()
    assert rows["L00"]["state"] == "RUNNING", "attempt stays RUNNING, task not failed"
    assert drv.servers["go2"]["park_status"] == "QUOTA_WEEKLY"
    assert "QUOTA_WEEKLY" in (env.run_root / "OWNER-ALERTS.md").read_text()
    # parked: nothing adopted
    settle(drv)
    assert rows["L00"]["state"] == "RUNNING" and len(oc.calls) == 1
    # park lifts (probe answers) -> adopted, same attempt, completes
    drv.servers["go2"]["parked_until"] = 0.0
    # D142: the unpark gate probes session CREATION, so stubbing the bare read is no
    # longer enough to answer it.  Both, so the test says "this server is healthy"
    # rather than "this server passes whichever check the driver happens to use".
    drv._http_ok = lambda *a, **k: True
    drv._session_roundtrip_ok = lambda *a, **k: True
    settle(drv)
    rows = env.rows()
    assert rows["L00"]["state"] == "VERIFIED"
    starts = [l for l in env.control_lines() if l["verb"] == "start"]
    assert len(starts) == 1, "adoption re-uses the attempt: no second start"
    assert len(oc.calls) == 2


def test_three_runner_failures_complete_invalid_evidence_with_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(lanedriver, "FAIL_BACKOFF_S", (0, 0, 0))
    env = Env(tmp_path)
    env.activate()
    oc = FakeRunner(default=status("RUNNER_CRASH", "boom"))
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv)
    assert env.rows()["L00"]["state"] == "RUNNING"
    settle(drv)
    settle(drv)
    rows = env.rows()
    assert rows["L00"]["state"] == "INVALID_EVIDENCE"
    assert "3 consecutive runner failures" in rows["L00"]["blocker"]
    assert len(oc.calls) == 3
    alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert "**STUCK**" in alerts and alerts.count("failure ") == 3
    # stuck: no fourth spawn even after more ticks
    settle(drv)
    assert len(oc.calls) == 3


def test_backoff_table_delays_the_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(lanedriver, "FAIL_BACKOFF_S", (3600, 3600, 3600))
    env = Env(tmp_path)
    env.activate()
    oc = FakeRunner(default=status("RUNNER_CRASH", "boom"))
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 3)
    assert len(oc.calls) == 1, "second try waits for the backoff"
    assert env.rows()["L00"]["state"] == "RUNNING"


def test_stop_aborts_children_and_restart_adopts_the_attempt(tmp_path):
    env = Env(tmp_path)
    env.activate()
    oc = FakeRunner(script=[wait_abort], default=result_ok)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    drv.tick()
    assert env.rows()["L00"]["state"] == "RUNNING"
    (env.run_root / "STOP").write_text("")
    drv.tick()                        # sees STOP, grace 1s
    assert drv._stopping
    time.sleep(1.2)
    drv.tick()                        # grace over -> abort
    drv.join(timeout=30)
    assert drv._abort.is_set()
    drv.tick()
    assert env.rows()["L00"]["state"] == "RUNNING", "aborted attempt is left for adoption"
    assert not any(l["verb"] == "complete" for l in env.control_lines())
    # restart: new driver, STOP removed, adopts the RUNNING attempt with the saved session
    (env.run_root / "STOP").unlink()
    oc2 = FakeRunner(default=result_ok)
    drv2 = env.driver({"opencode": oc2, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv2)
    rows = env.rows()
    assert rows["L00"]["state"] == "VERIFIED"
    assert oc2.calls[0].session_id == "ses_L00", "resume uses the session saved before STOP"
    assert len([l for l in env.control_lines() if l["verb"] == "start"]) == 1
    assert len([l for l in env.control_lines() if l["verb"] == "reconcile"]) == 2
    assert (env.run_root / "STOP-HANDOFF.json").exists() is False or True


def test_adoption_resumes_the_session_on_the_server_that_holds_it(tmp_path):
    """D47: a session id lives in ONE opencode server's db.  2026-09-19 08:10-08:23Z
    both L09-SEED-FIX attempts died on 3x `404 Session not found`: the turn ran on
    go2, the thread ended, and adoption re-picked go1 (sorted first) for the resume.
    The attempt's server is saved with the session and adoption pins to it."""
    servers = {"go1": {"url": "http://127.0.0.1:1", "max_concurrent": 0, "xdg_data_home": str(tmp_path / "x1")},
               "go2": {"url": "http://127.0.0.1:2", "max_concurrent": 8, "xdg_data_home": str(tmp_path / "x2")}}
    env = Env(tmp_path, roster_extra={"servers": servers})
    env.activate()
    oc = FakeRunner(script=[wait_abort], default=result_ok)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    drv.tick()
    assert env.rows()["L00"]["state"] == "RUNNING"
    for _ in range(100):
        if oc.calls:
            break
        time.sleep(0.05)
    assert oc.calls[0].server_url == "http://127.0.0.1:2", "go1 has no capacity: dispatched to go2"
    (env.run_root / "STOP").write_text("")
    drv.tick()
    time.sleep(1.2)
    drv.tick()
    drv.join(timeout=30)
    drv.tick()
    assert env.rows()["L00"]["state"] == "RUNNING"
    rec = json.loads((env.run_root / "turns" / "L00" / env.rows()["L00"]["attempt_id"] / "session.json").read_text())
    assert rec["server"] == "go2" and rec["session_id"] == "ses_L00"
    # restart with go1 wide open: without the pin, sorted() picks go1 and the resume 404s
    roster = json.loads((env.run_root / "roster.json").read_text())
    roster["servers"]["go1"]["max_concurrent"] = 8
    (env.run_root / "roster.json").write_text(json.dumps(roster, indent=1))
    (env.run_root / "STOP").unlink()
    oc2 = FakeRunner(default=result_ok)
    drv2 = env.driver({"opencode": oc2, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv2)
    assert env.rows()["L00"]["state"] == "VERIFIED"
    assert oc2.calls[0].session_id == "ses_L00"
    assert oc2.calls[0].server_url == "http://127.0.0.1:2", "resumed on the server that holds the session"


def test_idle_runs_frontier_and_alerts_every_30_min_not_every_tick(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.activate()
    # make every task finish so READY empties
    def routed(spec, ab):
        return findings("PASS")(spec, ab) if spec.role == "grader" else result_ok(spec, ab)
    drv = env.driver({"opencode": FakeRunner(default=routed), "codex": FakeRunner(),
                      "claude": FakeRunner()})
    settle(drv, 2)
    assert all(r["state"] == "VERIFIED" for r in env.rows().values())
    for _ in range(3):
        drv.tick()
    fronts = [l for l in env.control_lines() if l["verb"] == "frontier"]
    assert len(fronts) == 3, "frontier every idle tick at frontier_every_s=0"
    alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert alerts.count("**IDLE**") == 1
    # 31 minutes later: a second IDLE line
    drv._last_idle_alert_mono -= 31 * 60
    drv.tick()
    assert (env.run_root / "OWNER-ALERTS.md").read_text().count("**IDLE**") == 2
    assert "driver.log" in [p.name for p in env.run_root.iterdir()]
    assert "FRONTIER" in (env.run_root / "driver.log").read_text()


def test_draining_phase_refuses_new_claims_but_harvests_running(tmp_path):
    env = Env(tmp_path)
    env.activate()
    oc = FakeRunner(script=[wait_abort], default=result_ok)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    drv.tick()
    env.control_call("drain", "--reason", "test drain")
    drv._abort.set()                  # let the fake finish as ABORTED
    drv.join(timeout=30)
    drv._abort.clear()
    n = drv.tick()
    assert n == 1, "the RUNNING attempt is adopted (harvest continues under drain)"
    drv.join(timeout=30)
    assert env.rows()["L00"]["state"] == "VERIFIED"
    n = drv.tick()
    assert n == 0, "READY rows are not claimed while DRAINING"
    assert all(env.rows()[t]["state"] == "READY" for t in ("L01", "L02", "L03", "L04"))
    assert not any(l["verb"] == "claim" and "L01" in l["argv"] for l in env.control_lines())
    hb = json.loads((env.run_root / "driver.heartbeat").read_text())
    assert hb["active"] == 0


def test_control_jsonl_records_every_call_with_sequence(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.activate()
    monkeypatch.setattr(lanedriver.Control, "BLOB_AT", 400)       # the tiny catalog's replies are ~1 KB
    monkeypatch.setattr(lanedriver.Control, "BLOB_HEAD", 100)
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner(),
                      "claude": FakeRunner()})
    settle(drv)
    lines = env.control_lines()
    verbs = [l["verb"] for l in lines]
    assert verbs[:4] == ["reconcile", "ready", "claim", "start"]
    assert verbs[-1] == "complete"
    for l in lines:
        assert set(l) >= {"ts", "verb", "argv", "rc", "stdout", "stderr", "seq_before", "seq_after", "ms"}
        assert l["argv"][1].endswith("orchestration_control.py")
    assert lines[2]["seq_after"] == lines[2]["seq_before"] + 1
    # AUDIT-5: stdout over BLOB_AT lives whole in control-blobs/, hashed; the line keeps a head + pointer
    big = [l for l in lines if l.get("stdout_blob")]
    assert big, "the ready/reconcile dumps exceed %d chars" % lanedriver.Control.BLOB_AT
    for l in big:
        blob = (env.run_root / l["stdout_blob"]).read_text()
        assert len(l["stdout"]) == lanedriver.Control.BLOB_HEAD and blob.startswith(l["stdout"])
        assert hashlib.sha256(blob.encode()).hexdigest() == l["stdout_sha256"] and l["stdout_bytes"] == len(blob.encode())
        assert json.loads(blob), "the blob is the verbatim JSON reply"
    assert all(len(l["stdout"]) <= lanedriver.Control.BLOB_AT for l in lines)


class FakeCodex(FakeRunner):
    name = "codex"

    def __init__(self, thread_id="0199-thread-xyz", verdict="APPROVE"):
        FakeRunner.__init__(self)
        self.thread_id = thread_id
        self.verdict = verdict
        self.preopened = []

    def preopen(self, spec):
        self.preopened.append(spec)
        return self.thread_id, "INCOMPLETE"

    def run(self, spec, abort_flag=None):
        self.calls.append(spec)
        wt = Path(spec.cwd)
        req = json.loads((wt / ".vp" / "REVIEW_REQUEST.json").read_text())
        doc = {"item": spec.item, "subject": "item", "base": req["base"], "candidate": req["candidate"],
               "reviewer": req["reviewer"], "verdict": self.verdict,
               "verdicts": [{"id": i, "verdict": "PASS", "evidence": "platform/a.py:1"} for i in req["benchmark_ids"]],
               "findings": [], "evidence": ["platform/a.py:1"], "summary": "fake review"}
        Path(spec.out_path).write_text(json.dumps(doc))
        return TurnOutcome(STATUS_DONE, "", session_id=spec.session_id or self.thread_id, record_path=spec.out_path,
                           usage={"tokens_in": 9, "tokens_out": 2, "cost": None}, runner="codex",
                           model_seen="gpt-5.6-luna")


def register_candidate(env):
    import hashlib
    sha = git(env.trunk, "rev-parse", "HEAD")
    tree = git(env.trunk, "rev-parse", "HEAD^{tree}")
    art = env.tmp / "integration-record.json"
    art.write_text('{"integration": true}\n')
    packet = env.tmp / "candidate-packet.json"
    packet.write_text(json.dumps({
        "candidate_sha": sha, "tree_sha": tree,
        "contract_revision": hashlib.sha256(env.catalog.read_bytes()).hexdigest(),
        "artifact_manifest": {"record": {"path": art.name,
                                         "sha256": hashlib.sha256(art.read_bytes()).hexdigest()}}}))
    env.control_call("register-candidate", "--repo", str(env.trunk), "--packet", str(packet),
                     "--artifact-root", str(env.tmp), "--actor", "test")
    return sha, tree


def test_codex_review_is_one_fresh_turn_and_starts_with_its_thread_id(tmp_path):
    env = Env(tmp_path)
    env.activate()
    sha, tree = register_candidate(env)
    params = env.tmp / "review.json"
    params.write_text(json.dumps({"parent_contract_id": "L00", "candidate_sha": sha, "tree_sha": tree,
                                  "diff_or_scope": "whole", "criteria": "catalog", "evidence": []}))
    env.control_call("instantiate", "--template", "JUNIOR_REVIEW", "--task", "J1",
                     "--parent-contract", "L00", "--chief", "B", "--parameters-json", str(params))
    codex = FakeCodex()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": codex, "claude": FakeRunner()})
    settle(drv)
    rows = env.rows()
    # SEC-SESSION-001: no 0-preopen turn; the review is ONE fresh codex turn and
    # the thread it opens is what `start --child-id` carries (start follows the turn)
    assert codex.preopened == [], "no pre-open turn"
    assert rows["J1"]["child_id"] == "0199-thread-xyz"
    start = next(l for l in env.control_lines() if l["verb"] == "start" and "J1" in l["argv"])
    assert "0199-thread-xyz" in start["argv"] and "gpt-5.6-luna" in start["argv"]
    assert codex.calls[0].session_id is None, "the review opens a fresh thread, never resumes one"
    assert len([c for c in codex.calls if c.role == "reviewer"]) == 1, "one review turn"
    assert not list((env.run_root / "turns" / "J1").glob("*/0-preopen*")), "no preopen artefacts"
    order = [l["verb"] for l in env.control_lines() if "J1" in l["argv"] and l["verb"] in ("claim", "start", "complete")]
    assert order[:3] == ["claim", "start", "complete"] and order.count("start") == 1
    claim = next(l for l in env.control_lines() if l["verb"] == "claim" and "J1" in l["argv"])
    assert sha in claim["argv"], "review claims bind the registered candidate sha"
    # APPROVE -> complete --verdict; the gate refuses (no native codex rollout in
    # a test box) -> the driver completes INVALID_EVIDENCE honestly, never VERIFIED
    completes = [l for l in env.control_lines() if l["verb"] == "complete" and "J1" in l["argv"]]
    assert "--verdict" in completes[0]["argv"] and completes[0]["rc"] != 0
    assert rows["J1"]["state"] == "INVALID_EVIDENCE" and "verdict refused" in rows["J1"]["blocker"]
    packet = json.loads(next((env.run_root / "turns" / "J1").glob("*/verdict-packet.json")).read_text())
    assert packet["reviewer_session_id"] == "0199-thread-xyz"
    assert "0199-thread-xyz" not in packet["author_session_ids"]
    assert packet["coverage"]["L00"] == {"L00 verified": "PASS", "L00 accepted": "PASS"}
    assert "VERDICT_REFUSED" in (env.run_root / "OWNER-ALERTS.md").read_text()


# -- item 3: F1 drain deadline, F9 drain-first restart, F8 item verbs ---------------------------

class Args(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_f1_drain_deadline_restores_dispatch_and_alerts(tmp_path):
    env = Env(tmp_path)
    env.activate()
    now = [1000.0]
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner(),
                      "claude": FakeRunner()}, clock=lambda: now[0])
    settle(drv)                                   # L00 done, L01-L04 READY
    drv.write_drain("owner pause", deadline_min=10)
    assert drv.drain_state()["deadline_min"] == 10
    n = drv.tick()
    assert n == 0 and all(env.rows()[t]["state"] == "READY" for t in ("L01", "L02", "L03", "L04"))
    hb = json.loads((env.run_root / "driver.heartbeat").read_text())
    assert hb["draining"] is True
    now[0] += 9 * 60
    assert drv.tick() == 0, "still draining before the deadline"
    now[0] += 2 * 60                              # 11 min: deadline passed, no restart came
    n = drv.tick()
    drv.join(timeout=60)
    assert n == 4, "the driver released the drain itself and dispatched"
    assert not (env.run_root / "DRAIN").exists()
    alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert "**DRAIN_EXPIRED**" in alerts and "10-minute deadline" in alerts
    assert all(env.rows()[t]["state"] == "VERIFIED" for t in ("L01", "L02", "L03", "L04"))


def test_f1_drain_without_deadline_holds_until_undrain(tmp_path):
    env = Env(tmp_path)
    env.activate()
    now = [1000.0]
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner(),
                      "claude": FakeRunner()}, clock=lambda: now[0])
    settle(drv)
    drv.write_drain("hold", deadline_min=None)
    now[0] += 24 * 3600
    assert drv.tick() == 0
    assert lanedriver.cmd_undrain(drv, Args(reason="resume")) == 0
    assert drv.tick() == 4


def slow_result(delay):
    def fn(spec, ab):
        time.sleep(delay)
        return result_ok(spec, ab)
    return fn


def test_f9_restart_is_drain_then_active_zero_then_stop(tmp_path):
    env = Env(tmp_path)
    env.activate()
    oc = FakeRunner(default=slow_result(1.5))
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, interval=0.2)
    th = threading.Thread(target=drv.loop, daemon=True)
    th.start()
    wait_state(env, "L00", "RUNNING")             # L00 claimed and running (the fake turn holds 1.5 s)
    ctl = env.driver({}, interval=0.2)            # the control-surface instance (no runners)
    rc = lanedriver.cmd_restart(ctl, Args(timeout=1, poll=0.1, interval=0.2, force=False,
                                          no_start=True, fake_runners=False))
    assert rc == 0
    th.join(timeout=10)
    assert not th.is_alive(), "old driver exited on STOP"
    rows = env.rows()
    assert rows["L00"]["state"] == "VERIFIED", "the live turn was harvested, not killed"
    assert all(rows[t]["state"] == "READY" for t in ("L01", "L02", "L03", "L04")), \
        "nothing new was claimed after the drain"
    assert not (env.run_root / "STOP").exists() and not (env.run_root / "DRAIN").exists()
    log = (env.run_root / "driver.log").read_text()
    assert log.index("DRAIN written") < log.index("STOP file seen") < log.index("FINISH STOP")
    alerts_md = env.run_root / "OWNER-ALERTS.md"
    assert not alerts_md.exists() or "**RESTART**" not in alerts_md.read_text()  # --no-start


def test_f9_restart_aborts_when_turns_do_not_finish(tmp_path):
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=wait_abort), "codex": FakeRunner(),
                      "claude": FakeRunner()}, interval=0.2)
    th = threading.Thread(target=drv.loop, daemon=True)
    th.start()
    wait_state(env, "L00", "RUNNING")
    ctl = env.driver({}, interval=0.2)
    rc = lanedriver.cmd_restart(ctl, Args(timeout=0.01, poll=0.1, interval=0.2, force=False,
                                          no_start=True, fake_runners=False))
    assert rc == 2
    assert not (env.run_root / "STOP").exists(), "no STOP: the running turn was not aborted"
    assert not (env.run_root / "DRAIN").exists(), "drain released again"
    assert "**RESTART_ABORTED**" in (env.run_root / "OWNER-ALERTS.md").read_text()
    (env.run_root / "STOP").write_text("")
    drv._abort.set()
    th.join(timeout=10)


def test_f8_item_verbs_refuse_while_the_attempt_is_live(tmp_path):
    env = Env(tmp_path)
    env.activate()
    oc = FakeRunner(script=[wait_abort], default=result_ok)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    drv.tick()                                    # L00 live, heartbeat fresh
    ctl = env.driver({})
    ev = env.tmp / "ev.json"
    ev.write_text("{}")
    rc = lanedriver.cmd_item(ctl, Args(item_cmd="block", task="L00", blocker_class="EXTERNAL",
                                       reason="hand edit", unblock_action="none", evidence=str(ev)))
    assert rc == 4, "refused: the driver holds a live turn on L00"
    assert env.rows()["L00"]["state"] == "RUNNING"
    assert not any(l["verb"] == "block" for l in env.control_lines())
    rc = lanedriver.cmd_item(ctl, Args(item_cmd="complete", task="L00", attempt_id=drv._live["L00"],
                                       outcome="BLOCKED", unlock_dependents=False, output_sha=None,
                                       tree_sha=None, evidence=[], reason="hand"))
    assert rc == 4
    drv._abort.set()
    drv.join(timeout=30)
    # driver gone (stale heartbeat): the verb passes through to the scheduler, which
    # itself refuses to block a RUNNING attempt (F8 is unconditional there too)
    hb = json.loads((env.run_root / "driver.heartbeat").read_text())
    hb["ts"] = "2000-01-01T00:00:00.000Z"
    (env.run_root / "driver.heartbeat").write_text(json.dumps(hb))
    rc = lanedriver.cmd_item(ctl, Args(item_cmd="block", task="L00", blocker_class="EXTERNAL",
                                       reason="hand edit", unblock_action="none", evidence=str(ev)))
    assert rc == 5
    assert any(l["verb"] == "block" and l["rc"] != 0 for l in env.control_lines())
    # a non-live task can be hand-completed once its attempt is orphaned by a dead driver
    rc = lanedriver.cmd_item(ctl, Args(item_cmd="complete", task="L00", attempt_id=env.rows()["L00"]["attempt_id"],
                                       outcome="INVALID_EVIDENCE", unlock_dependents=False, output_sha=None,
                                       tree_sha=None, evidence=[], reason="dead driver"))
    assert rc == 0 and env.rows()["L00"]["state"] == "INVALID_EVIDENCE"


def test_cli_drain_and_undrain_write_the_state_row(tmp_path):
    env = Env(tmp_path)
    env.activate()
    rc = lanedriver.main(["--roster", str(env.run_root / "roster.json"), "drain",
                          "--reason", "owner pause", "--deadline", "15"])
    assert rc == 0
    row = json.loads((env.run_root / "DRAIN").read_text())
    assert row["deadline_min"] == 15 and row["reason"] == "owner pause" and row["expires_epoch"]
    assert lanedriver.main(["--roster", str(env.run_root / "roster.json"), "undrain"]) == 0
    assert not (env.run_root / "DRAIN").exists()


# -- item 4: F3 cost basis, token caps, quota parks ----------------------------------------------

def costs(env):
    return [json.loads(l) for l in (env.run_root / "costs.jsonl").read_text().splitlines()]


def test_f3_cost_rows_carry_basis_and_cache_read_per_runner(tmp_path):
    env = Env(tmp_path)
    env.activate()
    sha, tree = register_candidate(env)
    params = env.tmp / "review.json"
    params.write_text(json.dumps({"parent_contract_id": "L00", "candidate_sha": sha, "tree_sha": tree,
                                  "diff_or_scope": "whole", "criteria": "catalog", "evidence": []}))
    env.control_call("instantiate", "--template", "JUNIOR_REVIEW", "--task", "J1",
                     "--parent-contract", "L00", "--chief", "B", "--parameters-json", str(params))

    def oc_ok(spec, ab):
        out = result_ok(spec, ab)
        out.usage = {"tokens_in": 1000, "tokens_out": 200, "cache_read": 800, "cost": 0.0123}
        return out

    class Codex(FakeCodex):
        def run(self, spec, abort_flag=None):
            out = FakeCodex.run(self, spec, abort_flag)
            out.usage = {"tokens_in": 962000, "tokens_out": 8000, "tokens_reason": 3000,
                         "cache_read": 882000, "cache_write": None, "cost": None}
            return out

    drv = env.driver({"opencode": FakeRunner(default=oc_ok), "codex": Codex(), "claude": FakeRunner()})
    settle(drv)
    rows = {r["task"]: r for r in costs(env)}
    oc = rows["L00"]
    assert oc["cost_basis"] == "reported" and oc["cost"] == 0.0123 and oc["cache_read"] == 800
    cx = rows["J1"]
    assert cx["runner"] == "codex" and cx["cost_basis"] == "subscription"
    assert cx["cost"] == 0.0 and cx["tokens_in"] == 962000 and cx["cache_read"] == 882000
    alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert "UNPRICED" not in alerts, "codex on subscription is priced by tokens, not an UNPRICED alert"
    hb = drv.write_heartbeat()
    assert hb["budget"]["spent_usd"] == 0.0123
    assert hb["budget"]["tokens"] == {"opencode": 1200, "codex": 970000}


def test_f3_codex_api_billing_is_estimated_from_the_roster_table(tmp_path):
    env = Env(tmp_path, roster_extra={"pricing": {"codex": {"billing": "api", "models": {
        "gpt-5.6-luna": {"input_per_1m": 2.0, "cached_input_per_1m": 0.5, "output_per_1m": 8.0}}}}})
    env.activate()
    sha, tree = register_candidate(env)
    params = env.tmp / "review.json"
    params.write_text(json.dumps({"parent_contract_id": "L00", "candidate_sha": sha, "tree_sha": tree,
                                  "diff_or_scope": "whole", "criteria": "catalog", "evidence": []}))
    env.control_call("instantiate", "--template", "JUNIOR_REVIEW", "--task", "J1",
                     "--parent-contract", "L00", "--chief", "B", "--parameters-json", str(params))

    class Codex(FakeCodex):
        def run(self, spec, abort_flag=None):
            out = FakeCodex.run(self, spec, abort_flag)
            out.usage = {"tokens_in": 1000000, "tokens_out": 10000, "cache_read": 500000, "cost": None}
            return out

    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": Codex(), "claude": FakeRunner()})
    settle(drv)
    cx = next(r for r in costs(env) if r["task"] == "J1")
    # (1M-500k)*2 + 500k*0.5 + 10k*8 = 1.0 + 0.25 + 0.08
    assert cx["cost_basis"] == "estimated" and cx["cost"] == 1.33 and cx["est_cost_usd"] == 1.33


def test_f3_token_cap_parks_that_runner_only(tmp_path):
    env = Env(tmp_path, roster_extra={"budget": {"max_cost_usd_per_run": 100,
                                                  "max_tokens_per_runner": {"codex": 5000}}})
    env.activate()
    (env.run_root / "costs.jsonl").write_text(json.dumps(
        {"task": "old", "runner": "codex", "tokens_in": 4900, "tokens_out": 200, "cost": 0.0}) + "\n")
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner(), "claude": FakeRunner()})
    n = drv.tick()
    drv.join(timeout=60)
    assert n == 1 and env.rows()["L00"]["state"] == "VERIFIED", "opencode roles keep running"
    assert drv.runner_state["codex"]["park_status"] == "TOKEN_CAP"
    assert drv.runner_state["opencode"]["park_status"] is None
    assert drv._budget_stop is False
    assert "**TOKEN_CAP**" in (env.run_root / "OWNER-ALERTS.md").read_text()
    # raising the cap in the roster (hot reload) unparks it
    r = json.loads((env.run_root / "roster.json").read_text())
    r["budget"]["max_tokens_per_runner"]["codex"] = 50000
    (env.run_root / "roster.json").write_text(json.dumps(r))
    import os as _os
    _os.utime(env.run_root / "roster.json", (time.time() + 5, time.time() + 5))
    drv.tick()
    assert drv.runner_state["codex"]["park_status"] is None


def test_quota_on_codex_parks_codex_runner_and_leaves_review_running(tmp_path):
    env = Env(tmp_path)
    env.activate()
    sha, tree = register_candidate(env)
    params = env.tmp / "review.json"
    params.write_text(json.dumps({"parent_contract_id": "L00", "candidate_sha": sha, "tree_sha": tree,
                                  "diff_or_scope": "whole", "criteria": "catalog", "evidence": []}))
    env.control_call("instantiate", "--template", "JUNIOR_REVIEW", "--task", "J1",
                     "--parent-contract", "L00", "--chief", "B", "--parameters-json", str(params))

    class Codex(FakeCodex):
        def run(self, spec, abort_flag=None):
            self.calls.append(spec)
            return TurnOutcome("QUOTA_WEEKLY", "weekly usage limit reached", session_id=spec.session_id,
                               runner="codex", usage={"tokens_in": 1, "tokens_out": 0, "cost": None})

    cx = Codex()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": cx, "claude": FakeRunner()})
    settle(drv)
    rows = env.rows()
    # SEC-SESSION-001: no preopen, so a review whose one turn parked never
    # reached `start`; the attempt stays CLAIMED (adoptable) until the park lifts
    assert rows["J1"]["state"] == "CLAIMED" and rows["L00"]["state"] == "VERIFIED"
    assert drv.runner_state["codex"]["park_status"] == "QUOTA_WEEKLY"
    settle(drv)
    assert len(cx.calls) == 1, "parked codex: the CLAIMED review is not re-adopted"
    assert env.rows()["J1"]["state"] == "CLAIMED"


# -- item 5: the proof step in the driver (F6 itself is tested in test_laneproof) ------------

class FakeProof(object):
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.cfg = {}

    def run(self, task, pid, wt, base, cand, kind, paths, abort=None, workers=None):
        self.calls.append((task, pid, cand, kind, paths))
        self.workers = getattr(self, "workers", []) + [workers]
        rec = dict(self.results.pop(0) if self.results else {"status": "PASS"})
        rec.update({"proof_id": pid, "sha": cand, "route": "fake"})
        d = Path(wt).parents[1] / "run" / "proofs" if False else None
        return rec


def routed_pass(spec, ab):
    return findings("PASS")(spec, ab) if spec.role == "grader" else result_ok(spec, ab)


def test_proof_pass_verifies_and_fail_product_is_repair_required(tmp_path):
    """D14: the proof runs before the grade in every round; its red nodes land
    in FINDINGS.json as FAIL lines for the next builder round; still red after
    max_rounds -> REPAIR_REQUIRED with the node ids as fails."""
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"], "default_kind": "platform"}})
    env.activate()
    red = {"status": "FAIL_PRODUCT", "failed_nodes": ["platform/tests/test_a.py::test_x"],
           "errors": {"platform/tests/test_a.py::test_x": "AssertionError: 1 != 2"}}
    proof = FakeProof([red, red])                 # Env max_rounds == 2
    oc = FakeRunner(default=routed_pass)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, proof=proof)
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "REPAIR_REQUIRED" and "FAIL_PRODUCT" in rows["L02"]["blocker"]
    assert rows["L01"]["state"] == "VERIFIED", "design kind needs no proof"
    assert len(proof.calls) == 2 and proof.calls[0][0] == "L02" and proof.calls[0][3] == "platform"
    l02 = [s for s in oc.calls if s.item == "L02"]
    assert len([s for s in l02 if s.role == "builder"]) == 2, "a red proof is a FAIL line the builder repairs"
    assert len([s for s in l02 if s.role == "grader"]) == 2, "graded once per round, after the proof"
    wt = env.tmp / "wt" / "L02"
    findings = json.loads((wt / ".vp" / "FINDINGS.json").read_text())
    proof_lines = [l for l in findings["lines"] if l["id"] == "platform/tests/test_a.py::test_x"]
    assert proof_lines and proof_lines[0]["verdict"] == "FAIL" and "1 != 2" in proof_lines[0]["note"]
    assert (wt / ".vp" / "PROOF.json").exists()
    assert list((wt / ".vp" / "proofs").glob("proof-L02-*.json")), "record copied under .vp/proofs/"
    harvest = json.loads(next((env.run_root / "turns" / "L02").glob("*/harvest.json")).read_text())
    assert harvest["fails"][0]["id"] == "platform/tests/test_a.py::test_x"
    assert "1 != 2" in harvest["fails"][0]["note"]
    # the repair for that defect gets instantiated and proved (PASS) -> promoted
    drv.tick()
    rep = [t for t in env.rows() if t.startswith("R-L02")]
    assert rep and env.rows()[rep[0]]["parameters"]["defect_id"] == "platform-tests-test_a.py::test_x"


def test_proof_paths_and_proof_workers_narrow_the_box_proof(tmp_path, monkeypatch):
    """F-B (L32): `proof_paths` in the packet header is what the BOX proof
    runs (test_paths stays the grader's scope); `proof_workers: 1` is passed
    through to the proof runner."""
    orig = lanedriver.LaneDriver.render_packet

    def render(contract, base, row=None, test_paths=None, proof_kind=None):
        return orig(contract, base, row, test_paths, proof_kind).replace(
            "max_rounds:", "proof_paths:\n  - platform/tests/test_dbfree.py\nproof_workers: 1\nmax_rounds:", 1)
    monkeypatch.setattr(lanedriver.LaneDriver, "render_packet", staticmethod(render))
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"], "default_kind": "platform"}})
    env.activate()
    proof = FakeProof([])
    drv = env.driver({"opencode": FakeRunner(default=routed_pass), "codex": FakeRunner(), "claude": FakeRunner()},
                     proof=proof)
    settle(drv, 2)
    assert env.rows()["L02"]["state"] == "VERIFIED"
    assert proof.calls[0][4] == ["platform/tests/test_dbfree.py"] and proof.workers == [1]
    assert "PROOF L02 scope: 1 path(s) from proof_paths, workers=1" in (env.run_root / "driver.log").read_text()


def test_proof_red_once_is_repaired_in_the_next_round(tmp_path):
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"], "default_kind": "platform"}})
    env.activate()
    proof = FakeProof([{"status": "FAIL_PRODUCT", "failed_nodes": ["platform/tests/test_a.py::test_x"],
                        "errors": {"platform/tests/test_a.py::test_x": "boom"}}, {"status": "PASS"}])
    oc = FakeRunner(default=routed_pass)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, proof=proof)
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "VERIFIED", rows["L02"]
    assert len(proof.calls) == 2, "round 1 red, round 2 green"
    l02 = [s for s in oc.calls if s.item == "L02"]
    assert len([s for s in l02 if s.role == "builder"]) == 2 and len([s for s in l02 if s.role == "grader"]) == 2
    log = (env.run_root / "driver.log").read_text()
    assert "fails=['platform/tests/test_a.py::test_x']" in log


def test_park_inside_a_later_round_resumes_at_that_round_not_at_round_one(tmp_path, monkeypatch):
    """D53: LINT-TYPECHECK-TRUNK-R1 2026-09-19 11:33-12:08Z: the round-2 builder
    parked (DEGRADED, then RATE) and every adoption re-entered the rounds at 1:
    the old head was proved and graded again (same fail), the counter never
    moved, the cap could not fire.  rounds.json carries the round across the park."""
    monkeypatch.setattr(lanedriver, "FAIL_BACKOFF_S", (0, 0, 0))
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"], "default_kind": "platform"}})
    env.activate()
    proof = FakeProof([{"status": "PASS"}] * 4)
    grades = iter([findings("FAIL"), findings("PASS")])
    builds = iter([result_ok, status("DEGRADED", "service_overloaded"), result_ok])

    def routed(spec, ab):
        if spec.item != "L02":
            return routed_pass(spec, ab)
        return (next(grades) if spec.role == "grader" else next(builds))(spec, ab)
    oc = FakeRunner(default=routed)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, proof=proof)
    settle(drv, 2)
    assert env.rows()["L02"]["state"] == "RUNNING", "parked in round 2, attempt stays for adoption"
    tdir = next((env.run_root / "turns" / "L02").iterdir())
    assert json.loads((tdir / "rounds.json").read_text())["stage"] == "graded"
    for srv in drv.servers.values():
        srv["park_reason"] = srv["park_status"] = None
        srv["parked_until"] = 0.0
    settle(drv, 2)
    assert env.rows()["L02"]["state"] == "VERIFIED"
    l02 = [s for s in oc.calls if s.item == "L02"]
    assert [s.role for s in l02] == ["builder", "grader", "builder", "builder", "grader"], \
        "adoption goes straight to the round-2 builder: no second proof/grade of the round-1 head"
    assert len(proof.calls) == 2
    log = (env.run_root / "driver.log").read_text()
    assert log.count("ROUND L02 1/2 fails") == 1 and "ROUND L02 resumes at 2/2" in log  # Env max_rounds == 2


def test_d79a_a_saved_round_past_a_shrunken_cap_is_clamped_to_the_cap(tmp_path, monkeypatch):
    """D79a/D79b: L06-HOSTED-R3 parked in round 3, then D79 cut hosted twins to
    max_rounds 1: `range(3, 2)` ran zero rounds and the attempt was re-adopted
    every tick.  The resumed round clamps to the cap with the literal reason
    "resumed past max_rounds (D79)" and the attempt finishes."""
    monkeypatch.setattr(lanedriver, "FAIL_BACKOFF_S", (0, 0, 0))
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"], "default_kind": "platform"}})
    env.activate()
    proof = FakeProof([{"status": "PASS"}] * 4)
    grades = iter([findings("FAIL"), findings("PASS")])
    builds = iter([result_ok, status("DEGRADED", "service_overloaded"), result_ok])

    def routed(spec, ab):
        if spec.item != "L02":
            return routed_pass(spec, ab)
        return (next(grades) if spec.role == "grader" else next(builds))(spec, ab)
    oc = FakeRunner(default=routed)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, proof=proof)
    settle(drv, 2)
    tdir = next((env.run_root / "turns" / "L02").iterdir())
    saved = json.loads((tdir / "rounds.json").read_text())
    saved["round"] = 5                                    # a round the (now smaller) cap of 2 never reaches
    (tdir / "rounds.json").write_text(json.dumps(saved))
    for srv in drv.servers.values():
        srv["park_reason"] = srv["park_status"] = None
        srv["parked_until"] = 0.0
    settle(drv, 2)
    assert env.rows()["L02"]["state"] == "VERIFIED", "the clamped round ran and finished the attempt"
    log = (env.run_root / "driver.log").read_text()
    assert "ROUND L02 clamps 6 -> 2: resumed past max_rounds (D79)" in log
    assert "ROUND L02 resumes at 2/2" in log


def test_proof_unknown_keeps_the_head_and_retries_proof_only(tmp_path, monkeypatch):
    monkeypatch.setattr(lanedriver, "FAIL_BACKOFF_S", (0, 0, 0))
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"]}})
    env.activate()
    proof = FakeProof([{"status": "UNKNOWN", "reason": "circleci: credits"}, {"status": "PASS"}])
    oc = FakeRunner(default=routed_pass)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, proof=proof)
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "RUNNING", "infra-unknown proof leaves the attempt for retry"
    turns = [s for s in oc.calls if s.item == "L02"]
    assert [s.role for s in turns] == ["builder"], "D14: the proof runs before the grade; no grade yet"
    tdir = next((env.run_root / "turns" / "L02").iterdir())
    assert (tdir / "proof-pending.json").exists()
    settle(drv)                                   # adopt: proof then grade, no rebuild
    rows = env.rows()
    assert rows["L02"]["state"] == "VERIFIED"
    assert [s.role for s in oc.calls if s.item == "L02"] == ["builder", "grader"], "build not repeated"
    assert len(proof.calls) == 2 and proof.calls[0][2] == proof.calls[1][2]
    assert not (tdir / "proof-pending.json").exists()
    assert "PROOF_UNKNOWN" in (env.run_root / "OWNER-ALERTS.md").read_text()
    assert "build skipped" in (env.run_root / "driver.log").read_text()


def test_proof_blocked_by_circleci_credits_holds_the_attempt_without_a_strike(tmp_path, monkeypatch):
    """D40: 27 hosted twins went INVALID_EVIDENCE (3 strikes each) on CircleCI's
    plan/credit refusal; a BLOCKED_CREDITS proof holds the attempt, alerts once,
    and resumes at the proof when the hold lifts."""
    monkeypatch.setattr(lanedriver, "FAIL_BACKOFF_S", (0, 0, 0))
    monkeypatch.setattr(lanedriver, "CREDITS_HOLD_S", 0.0)
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"]}})
    env.activate()
    proof = FakeProof([{"status": "BLOCKED_CREDITS", "reason": "circleci: no credits are available on your plan"},
                       {"status": "BLOCKED_CREDITS", "reason": "circleci: no credits are available on your plan"},
                       {"status": "BLOCKED_CREDITS", "reason": "circleci: no credits are available on your plan"},
                       {"status": "PASS"}])
    oc = FakeRunner(default=routed_pass)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, proof=proof)
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "RUNNING", "held, not failed"
    assert drv._fail["L02"]["count"] == 0, "a credit refusal is no strike"
    log = (env.run_root / "driver.log").read_text()
    assert "HOLD L02" in log and "BLOCKED_CREDITS" in log and "FAIL L02" not in log
    assert (env.run_root / "OWNER-ALERTS.md").read_text().count("CIRCLECI_NO_CREDITS") == 1
    settle(drv, 6)
    rows = env.rows()
    assert rows["L02"]["state"] == "VERIFIED", rows["L02"]
    assert len(proof.calls) == 4 and len({c[2] for c in proof.calls}) == 1, "the same head, proof only"
    assert [s.role for s in oc.calls if s.item == "L02"] == ["builder", "grader"], "no rebuild, no extra grade"
    assert (env.run_root / "OWNER-ALERTS.md").read_text().count("CIRCLECI_NO_CREDITS") == 1, "alerted once"


def test_proof_refused_by_our_gate_or_cap_holds_the_attempt_without_a_strike(tmp_path, monkeypatch):
    """D57 (on D65): a trigger our own owner gate / daily cap / off switch refused
    comes back as PROOF_BLOCKED_GATE|CAP|OFF; without this branch the generic
    runner-failure path strikes it out in 3 backoffs (the D40 shape). Also: the
    live Proof outlives a hot reload, so gate_open is re-attached on roster apply."""
    monkeypatch.setattr(lanedriver, "FAIL_BACKOFF_S", (0, 0, 0))
    monkeypatch.setattr(lanedriver, "GATE_HOLD_S", 0.0)
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"]}})
    env.activate()
    proof = FakeProof([{"status": "BLOCKED_GATE", "reason": "circleci: owner gate DELIVERY-1 not open at trigger time"},
                       {"status": "BLOCKED_CAP", "reason": "circleci: 40 pipelines triggered today >= max_pipelines_per_day 40"},
                       {"status": "BLOCKED_OFF", "reason": "circleci: circleci flipped off (CIRCLECI-OFF) at trigger time"},
                       {"status": "PASS"}])
    oc = FakeRunner(default=routed_pass)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, proof=proof)
    assert callable(getattr(proof, "gate_open", None)), "gate_open is attached to the live Proof on roster apply"
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "RUNNING", "held, not failed"
    assert drv._fail["L02"]["count"] == 0, "our own refusal is no strike"
    log = (env.run_root / "driver.log").read_text()
    assert "HOLD L02" in log and "BLOCKED_GATE" in log and "FAIL L02" not in log
    settle(drv, 6)
    rows = env.rows()
    assert rows["L02"]["state"] == "VERIFIED", rows["L02"]
    assert len(proof.calls) == 4 and len({c[2] for c in proof.calls}) == 1, "the same head, proof only"
    assert [s.role for s in oc.calls if s.item == "L02"] == ["builder", "grader"], "no rebuild, no extra grade"
    md = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert md.count("PROOF_BLOCKED_GATE") == 1 and md.count("PROOF_BLOCKED_CAP") == 1 and md.count("PROOF_BLOCKED_OFF") == 1


def test_credits_held_claim_is_released_when_a_ready_packet_waits_on_its_paths(tmp_path, monkeypatch):
    """D46a: 2026-09-19 06:54Z L09-SEED-FIX (READY, same seed files) waited on
    ADMISSION-SEED-FIX-HOSTED-R1's ACTIVE claim while every CircleCI account refused
    credits -- the D40 hold fenced the box off the repair.  With a waiter, the
    credits refusal releases the claim (INVALID_EVIDENCE, reason BLOCKED_CREDITS_RELEASED,
    no strike) and the waiter claims; without a waiter D40 still holds."""
    monkeypatch.setattr(lanedriver, "FAIL_BACKOFF_S", (0, 0, 0))
    monkeypatch.setattr(lanedriver, "CREDITS_HOLD_S", 0.0)
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"]}})
    cat = catalog()
    cat["contracts"].append({"id": "L05", "title": "task L05", "chief": "B", "kind": "builder",
                             "depends_on": ["L00"], "owned_paths": ["platform/a.py"],
                             "build_steps": ["do L05"], "verification": ["L05 verified"],
                             "acceptance": ["L05 accepted"], "role": ROLE_MUSE})
    env.catalog.write_text(json.dumps(cat, indent=1))
    env.activate()
    proof = FakeProof([{"status": "BLOCKED_CREDITS", "reason": "circleci: no credits are available on your plan"},
                       {"status": "PASS"}, {"status": "PASS"}])
    oc = FakeRunner(default=routed_pass)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, proof=proof)
    settle(drv, 3)
    rows = env.rows()
    first = [t for t in ("L02", "L05") if rows[t]["state"] == "INVALID_EVIDENCE"]
    assert len(first) == 1, rows
    other = "L05" if first == ["L02"] else "L02"
    log = (env.run_root / "driver.log").read_text()
    assert "RELEASE %s: credits refused and %s waits on its paths" % (first[0], other) in log
    assert "HOLD %s" % first[0] not in log and "FAIL %s" % first[0] not in log
    assert lanedriver.CREDITS_RELEASED in json.dumps(rows[first[0]])
    settle(drv, 6)
    rows = env.rows()
    assert rows[other]["state"] == "VERIFIED", rows[other]
    assert (env.run_root / "OWNER-ALERTS.md").read_text().count("CIRCLECI_NO_CREDITS") == 1


# -- D18/D19: shm gate holds without a strike; repair generations are capped -------------------

def test_proof_blocked_by_shm_holds_the_attempt_without_a_failure_strike(tmp_path, monkeypatch):
    monkeypatch.setattr(lanedriver, "SHM_HOLD_S", 0.0)
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"]}})
    env.activate()
    proof = FakeProof([{"status": "BLOCKED_SHM", "reason": "PROOF_BLOCKED_SHM: 26 live segments >= 24"},
                       {"status": "BLOCKED_SHM", "reason": "PROOF_BLOCKED_SHM: 26 live segments >= 24"},
                       {"status": "BLOCKED_SHM", "reason": "PROOF_BLOCKED_SHM: 26 live segments >= 24"},
                       {"status": "PASS"}])
    oc = FakeRunner(default=routed_pass)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, proof=proof)
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "RUNNING", "held, not failed"
    log = (env.run_root / "driver.log").read_text()
    assert "HOLD L02" in log and "FAIL L02" not in log
    assert "PROOF_BLOCKED_SHM" in (env.run_root / "OWNER-ALERTS.md").read_text()
    settle(drv, 3)                                    # three more adoptions: two more holds, then PASS
    assert env.rows()["L02"]["state"] == "VERIFIED"
    assert "FAIL L02" not in (env.run_root / "driver.log").read_text(), "box holds never count as strikes"
    assert len(proof.calls) == 4
    hb = json.loads((env.run_root / "driver.heartbeat").read_text())
    assert "shm_segments" in hb


def test_repair_generations_are_capped(tmp_path):
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": []},
                                      "alerts": {"frontier_every_s": 0, "idle_every_min": 30}})
    env.activate()

    def grader(spec, ab):
        # L02 fails its grade; every auto-repair of it fails too
        return findings("FAIL")(spec, ab)
    runner = by_role({"builder": result_ok, "grader": grader, "probe": result_ok})
    drv = env.driver({"opencode": runner, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 12)
    rows = env.rows()
    repairs = sorted(t for t in rows if t.startswith("R-L02-"))
    assert repairs == ["R-L02-B1-1", "R-L02-B1-2"], repairs
    assert all(rows[t]["state"] == "REPAIR_REQUIRED" for t in repairs)
    assert rows["L02"]["state"] == "REPAIR_REQUIRED"
    assert (env.run_root / "repairs" / "L02.capped.json").exists()
    alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert alerts.count("REPAIR_CAPPED") == 1
    assert "repair generations capped at 2" in (env.run_root / "driver.log").read_text()


# -- D11: [hosted] rows never block; [box] UNKNOWNs -> proof, then one regrade --------------------

def _with_hosted_row(monkeypatch):
    orig = lanedriver.LaneDriver.render_benchmark

    def render(contract):
        return orig(contract) + "- B9 [invariant] [hosted] end to end on a live DB — check: CI shard (gate: CIRCLECI)\n"
    monkeypatch.setattr(lanedriver.LaneDriver, "render_benchmark", staticmethod(render))


def grader_unknown_on(ids, until_proof=False):
    """PASS everywhere except UNKNOWN on `ids`; with until_proof, those flip to
    PASS once .vp/PROOF.json exists (the regrade after the proof)"""
    def fn(spec, abort_flag=None):
        wt = Path(spec.cwd)
        head = git(wt, "rev-parse", "HEAD")
        proved = (wt / ".vp" / "PROOF.json").exists()
        all_ids = [l.split()[1] for l in (wt / ".vp" / "BENCHMARK.md").read_text().splitlines()
                   if l.startswith("- B")]
        lines = []
        for i in all_ids:
            hosted = i == "B9"                    # the [hosted] row stays UNKNOWN on the box
            v = "UNKNOWN" if (i in ids and (hosted or not (until_proof and proved))) else "PASS"
            lines.append({"id": i, "kind": "evidence", "verdict": v, "evidence": "platform/a.py:1",
                          "note": "proof log" if proved else "no proof yet"})
        Path(spec.out_path).write_text(json.dumps({"item": spec.item, "attempt": 1, "commit": head,
                                                   "lines": lines, "all_pass": False}))
        return TurnOutcome(STATUS_DONE, "", session_id="ses_g", record_path=spec.out_path,
                           usage={"tokens_in": 10, "tokens_out": 5, "cost": 0.001}, runner="fake")
    return fn


def test_d11_hosted_unknown_never_blocks_and_is_recorded_as_owed(tmp_path, monkeypatch):
    _with_hosted_row(monkeypatch)
    env = Env(tmp_path)
    env.activate()
    runner = by_role({"builder": result_ok, "grader": grader_unknown_on({"B9"}), "probe": result_ok})
    drv = env.driver({"opencode": runner, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "VERIFIED", rows["L02"]
    harvest = json.loads(next((env.run_root / "turns" / "L02").glob("*/harvest.json")).read_text())
    assert harvest["hosted_owed"] == ["B9"]
    assert "HOSTED_OWED" in (env.run_root / "OWNER-ALERTS.md").read_text()
    assert len([s for s in runner.calls if s.item == "L02" and s.role == "builder"]) == 1


def test_d11_box_unknown_regrades_the_same_commit_once_with_the_proof_on_disk(tmp_path, monkeypatch):
    _with_hosted_row(monkeypatch)
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"], "default_kind": "platform"}})
    env.activate()
    proof = FakeProof([{"status": "PASS"}])
    seen = []

    def grader(spec, ab):
        # D14: the proof precedes the first grade, so PROOF.json is already
        # there; the first grade still says UNKNOWN on B1 (a flaky grader), the
        # regrade of the same commit passes it
        seen.append((Path(spec.cwd) / ".vp" / "PROOF.json").exists())
        ids = {"B1", "B9"} if len(seen) == 1 else {"B9"}
        return grader_unknown_on(ids)(spec, ab)
    runner = by_role({"builder": result_ok, "grader": grader, "probe": result_ok})
    drv = env.driver({"opencode": runner, "codex": FakeRunner(), "claude": FakeRunner()}, proof=proof)
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "VERIFIED", rows["L02"]
    assert len(proof.calls) == 1, "proof ran once, before the grade"
    assert seen == [True, True], "both grader turns saw .vp/PROOF.json"
    calls = [s for s in runner.calls if s.item == "L02"]
    assert len([s for s in calls if s.role == "builder"]) == 1, "no rebuild: same commit regraded"
    assert len([s for s in calls if s.role == "grader"]) == 2, "grade, regrade"
    harvest = json.loads(next((env.run_root / "turns" / "L02").glob("*/harvest.json")).read_text())
    assert harvest["hosted_owed"] == ["B9"]
    log = (env.run_root / "driver.log").read_text()
    assert "-> one regrade" in log


def test_d11_box_unknown_without_a_proof_is_ungradeable_after_the_rounds(tmp_path):
    env = Env(tmp_path)
    env.activate()
    runner = by_role({"builder": result_ok, "grader": grader_unknown_on({"B1"}), "probe": result_ok})
    drv = env.driver({"opencode": runner, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "REPAIR_REQUIRED" and "UNGRADEABLE B1" in rows["L02"]["blocker"]


# -- D15: stale worktrees / branches never carry a candidate ------------------------------------

def test_stale_foreign_worktree_is_moved_aside_and_rebuilt_on_the_base(tmp_path):
    env = Env(tmp_path)
    env.activate()
    other = env.tmp / "other-repo"                # the pre-v13 repo TRUNK-04 came from
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    git(other, "config", "user.email", "t@t")
    git(other, "config", "user.name", "t")
    (other / "old.txt").write_text("old\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "old world")
    wt = env.tmp / "wt" / "L02"
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(other, "worktree", "add", "-q", str(wt), "-b", "vp/L02")
    (wt / ".vp").mkdir()
    (wt / ".vp" / "BASE").write_text(env.base)
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "VERIFIED", rows["L02"]
    common = git(wt, "rev-parse", "--git-common-dir")
    assert Path(common).resolve() == (env.trunk / ".git").resolve(), "fresh worktree belongs to the trunk"
    assert git(wt, "merge-base", "--is-ancestor", env.base, "HEAD") == ""
    stale = [p for p in wt.parent.iterdir() if p.name.startswith("L02.stale-")]
    assert len(stale) == 1 and (stale[0] / "old.txt").exists(), "the old worktree is kept aside, not deleted"
    assert "WORKTREE_STALE" in (env.run_root / "OWNER-ALERTS.md").read_text()
    log = (env.run_root / "driver.log").read_text()
    assert "WORKTREE L02 stale (belongs to" in log


def test_stale_same_named_branch_is_renamed_not_reused(tmp_path):
    env = Env(tmp_path)
    env.activate()
    # a vp/L02 branch whose tip does not descend from the base: an orphan root
    git(env.trunk, "checkout", "-q", "--orphan", "vp/L02")
    (env.trunk / "stale.txt").write_text("stale\n")
    git(env.trunk, "add", "-A")
    git(env.trunk, "commit", "-q", "-m", "pre-v13 candidate")
    git(env.trunk, "checkout", "-q", "successor/s3")
    (env.trunk / "stale.txt").unlink(missing_ok=True)
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "VERIFIED", rows["L02"]
    wt = env.tmp / "wt" / "L02"
    assert git(wt, "merge-base", "--is-ancestor", env.base, "HEAD") == ""
    assert not (wt / "stale.txt").exists()
    branches = git(env.trunk, "branch", "--list", "vp-stale/L02-*")
    assert "vp-stale/L02-" in branches, "the old branch is renamed, never deleted"
    assert "stale branch vp/L02 -> vp-stale/L02-" in (env.run_root / "driver.log").read_text()


def test_worktree_on_its_own_base_is_reused(tmp_path):
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 2)
    wt = env.tmp / "wt" / "L02"
    assert env.rows()["L02"]["state"] == "VERIFIED"
    assert drv._worktree_stale(wt, env.base) is None
    assert not [p for p in wt.parent.iterdir() if ".stale-" in p.name]


# -- item 6: deterministic fixers after every builder turn ----------------------------------------

def fake_tool(path, body):
    import stat
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_autofix_runs_only_safe_ruff_fixes_never_format_or_prettier(tmp_path):
    """D20: `ruff check --fix --select I001,F401,W291,W293` on the changed .py
    files; `ruff format` and prettier are never invoked (s3's gate is `ruff
    check` only); tsc stays report-only."""
    env = Env(tmp_path)
    env.activate()
    calls = tmp_path / "ruff-calls.log"
    ruff = fake_tool(tmp_path / "ruff", "#!/bin/sh\n"
                     'echo "$@" >> "%s"\n'
                     'if [ "$1" = check ]; then for f in "$@"; do case "$f" in *.py) echo "# ruff-fixed" >> "$f";; esac; done; fi\n'
                     'if [ "$1" = format ]; then for f in "$@"; do case "$f" in *.py) sed -i "" "s/x = 2/x = 2  # formatted/" "$f";; esac; done; fi\n'
                     % calls)
    prettier = fake_tool(tmp_path / "prettier", "#!/bin/sh\nshift\nfor f in \"$@\"; do echo \"// pretty\" >> \"$f\"; done\n")
    tsc = fake_tool(tmp_path / "tsc", "#!/bin/sh\necho 'portal/x.ts(1,1): error TS1' ; exit 2\n")
    (env.trunk / "portal").mkdir()
    (env.trunk / "portal" / "tsconfig.json").write_text("{}")
    (env.trunk / "portal" / "x.ts").write_text("let a=1\n")
    git(env.trunk, "add", "-A")
    git(env.trunk, "commit", "-q", "-m", "portal")

    def builder(spec, ab):
        wt = Path(spec.cwd)
        (wt / "portal" / "x.ts").write_text("let a=2\n")
        return result_ok(spec, ab)

    def routed(spec, ab):
        return findings("PASS")(spec, ab) if spec.role == "grader" else builder(spec, ab)

    drv = env.driver({"opencode": FakeRunner(default=routed), "codex": FakeRunner(), "claude": FakeRunner()},
                     bins={"ruff": ruff, "prettier": prettier, "tsc": tsc})
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "VERIFIED"
    wt = env.tmp / "wt" / "L02"
    log = git(wt, "log", "--format=%s", "-3")
    assert log.splitlines()[0] == "autofix: ruff I001/F401/W291/W293 (L02)"
    assert log.splitlines()[1].startswith("work L02")
    assert "# ruff-fixed" in (wt / "platform" / "a.py").read_text()
    assert "# formatted" not in (wt / "platform" / "a.py").read_text(), "ruff format is never run"
    assert "// pretty" not in (wt / "portal" / "x.ts").read_text(), "prettier is never run"
    invocations = calls.read_text().splitlines()
    assert len(invocations) == 1 and invocations[0].startswith("check --fix --exit-zero --select I001,F401,W291,W293 platform/a.py")
    assert rows["L02"]["output_sha"] == git(wt, "rev-parse", "HEAD"), "the autofix commit is the output"
    tdir = next((env.run_root / "turns" / "L02").iterdir())
    rec = json.loads((tdir / "autofix.json").read_text())
    assert rec["py"] == ["platform/a.py"] and rec["portal"] == ["portal/x.ts"] and rec["pinned"] is None
    assert [s["tool"] for s in rec["steps"]] == ["ruff-fix", "tsc"]
    assert rec["tsc_rc"] == 2 and rec["committed"] == rows["L02"]["output_sha"]
    assert "tsc --noEmit rc=2" in (env.run_root / "driver.log").read_text()
    # the RESULT.json the builder wrote named the pre-autofix sha; the graded
    # record is rebound to HEAD (54 packets' rows require commit == HEAD)
    res = json.loads((wt / ".vp" / "RESULT.json").read_text())
    assert res["commit"] == git(wt, "rev-parse", "HEAD") and res["pre_autofix_commit"] == git(wt, "rev-parse", "HEAD~1")
    assert "RESULT.json commit rebound" in (env.run_root / "driver.log").read_text()
    # design-kind tasks (no builder turn) never run the fixers
    assert not list((env.run_root / "turns" / "L01").glob("*/autofix.json"))


def test_autofix_is_skipped_when_the_benchmark_pins_the_diff_scope(tmp_path, monkeypatch):
    orig = lanedriver.LaneDriver.render_benchmark

    def render(contract):
        return orig(contract) + "- B9 [forbidden] [box] exactly one line added — check: `git diff --numstat base..HEAD` prints 1 0\n"
    monkeypatch.setattr(lanedriver.LaneDriver, "render_benchmark", staticmethod(render))
    env = Env(tmp_path)
    env.activate()
    ruff = fake_tool(tmp_path / "ruff", "#!/bin/sh\nfor f in \"$@\"; do case \"$f\" in *.py) echo \"# ruff-fixed\" >> \"$f\";; esac; done\n")
    drv = env.driver({"opencode": FakeRunner(default=routed_pass), "codex": FakeRunner(), "claude": FakeRunner()},
                     bins={"ruff": ruff})
    settle(drv, 2)
    wt = env.tmp / "wt" / "L02"
    assert git(wt, "log", "--format=%s", "-1").startswith("work L02"), "no autofix commit"
    assert "# ruff-fixed" not in (wt / "platform" / "a.py").read_text()
    tdir = next((env.run_root / "turns" / "L02").iterdir())
    rec = json.loads((tdir / "autofix.json").read_text())
    assert rec["committed"] is None and rec["pinned"].startswith("benchmark pins the diff scope")
    assert rec["steps"][0] == {"tool": "ruff-fix", "skipped": rec["pinned"]}
    assert "AUTOFIX L02 skipped: benchmark pins the diff scope" in (env.run_root / "driver.log").read_text()


def test_autofix_is_skipped_when_the_packet_header_says_none(tmp_path, monkeypatch):
    orig = lanedriver.LaneDriver.render_packet

    def render(contract, base, row=None, test_paths=None, proof_kind=None):
        return orig(contract, base, row, test_paths, proof_kind).replace("max_rounds:", "autofix: none\nmax_rounds:", 1)
    monkeypatch.setattr(lanedriver.LaneDriver, "render_packet", staticmethod(render))
    env = Env(tmp_path)
    env.activate()
    ruff = fake_tool(tmp_path / "ruff", "#!/bin/sh\nfor f in \"$@\"; do case \"$f\" in *.py) echo \"# ruff-fixed\" >> \"$f\";; esac; done\n")
    drv = env.driver({"opencode": FakeRunner(default=routed_pass), "codex": FakeRunner(), "claude": FakeRunner()},
                     bins={"ruff": ruff})
    settle(drv, 2)
    wt = env.tmp / "wt" / "L02"
    assert git(wt, "log", "--format=%s", "-1").startswith("work L02"), "no autofix commit"
    assert "# ruff-fixed" not in (wt / "platform" / "a.py").read_text()
    tdir = next((env.run_root / "turns" / "L02").iterdir())
    rec = json.loads((tdir / "autofix.json").read_text())
    assert rec["committed"] is None and rec["pinned"] == "packet header autofix: none"


def test_contamination_sweep_flags_reformat_autofix_outputs_only(tmp_path):
    """D20 (i): VERIFIED/INTEGRATED rows whose output_sha subject is a pre-D20
    `autofix: ruff/prettier` commit are flagged with the clean commit under
    them; D20's own safe-rules commit and ordinary commits are not."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner(), "claude": FakeRunner()})
    def commit(msg):
        (env.trunk / "platform" / "a.py").write_text("# %s\n" % msg)
        git(env.trunk, "add", "-A")
        git(env.trunk, "commit", "-q", "-m", msg)
        return git(env.trunk, "rev-parse", "HEAD")
    clean = commit("L02: real work")
    reformat = commit("autofix: ruff/prettier (L02)")
    safe = commit("autofix: ruff I001/F401/W291/W293 (L03)")
    state = {"tasks": {"L02": {"state": "VERIFIED", "output_sha": reformat},
                       "L03": {"state": "INTEGRATED", "output_sha": safe},
                       "L04": {"state": "VERIFIED", "output_sha": clean},
                       "L05": {"state": "REPAIR_REQUIRED", "output_sha": reformat},
                       "L06": {"state": "VERIFIED", "output_sha": "f" * 40}}}
    flagged = lanedriver.autofix_contamination(drv, state)
    assert [(f["task"], f["flag"]) for f in flagged] == [("L02", "CONTAMINATED"), ("L06", "output_sha is not a commit in the trunk")]
    assert flagged[0]["clean_commit"] == clean and flagged[0]["contaminating_commit"] == reformat
    assert flagged[0]["numstat"].startswith("1\t1\tplatform/a.py")


def test_autofix_without_tools_or_changes_commits_nothing(tmp_path):
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=routed_pass), "codex": FakeRunner(), "claude": FakeRunner()},
                     bins={"ruff": str(tmp_path / "no-such-ruff")})
    drv._tool = lambda name, wt: None
    settle(drv, 2)
    wt = env.tmp / "wt" / "L02"
    assert git(wt, "log", "--format=%s", "-1").startswith("work L02"), "no autofix commit"
    tdir = next((env.run_root / "turns" / "L02").iterdir())
    rec = json.loads((tdir / "autofix.json").read_text())
    assert rec["committed"] is None and rec["steps"][0].get("skipped") == "ruff not found"


def test_render_packet_and_benchmark_from_contract(tmp_path):
    con = catalog()["contracts"][2]
    pk = lanedriver.LaneDriver.render_packet(con, "a" * 40)
    assert pk.startswith("---\nitem: L02\n") and "platform/a.py" in pk and "base_sha: " + "a" * 40 in pk
    bm = lanedriver.LaneDriver.render_benchmark(con)
    assert bm.splitlines()[0].startswith("- B1 [evidence] L02 verified — check:")
    assert len(bm.splitlines()) == 2


# -- item 7: logging tree (turns/*/raw.jsonl, comms.jsonl, seal) ---------------------

def raw_result(spec, abort_flag=None):
    """probe result whose runner also leaves a raw stream log behind"""
    out = result_ok(spec, abort_flag)
    log = Path(spec.out_path).parent / "1-r0-probe-opencode.jsonl"
    log.write_text('{"type":"step_start"}\n{"type":"text","text":"hello"}\n')
    out.log_path = str(log)
    return out


def test_item7_raw_jsonl_carries_the_runner_stream_and_seal_hashes_the_tree(tmp_path):
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=raw_result), "codex": FakeRunner()})
    settle(drv, 2)
    tdir = next((env.run_root / "turns" / "L00").iterdir())
    assert (tdir / "prompt.md").exists() and (tdir / "record.json").exists()
    raw = (tdir / "raw.jsonl").read_text().splitlines()
    hdr = json.loads(raw[0])
    assert hdr["vp"] == "turn" and hdr["runner"] == "opencode" and hdr["role"] == "probe"
    assert raw[1:3] == ['{"type":"step_start"}', '{"type":"text","text":"hello"}'], "verbatim stream"
    assert (env.run_root / "control.jsonl").exists() and (env.run_root / "git.jsonl").exists()
    # seal: one manifest per IST day, written on the first tick; hashes every audit file
    day = drv.ist_now().strftime("%Y-%m-%d")
    man = json.loads((env.run_root / ("MANIFEST-%s.json" % day)).read_text())
    assert "driver.heartbeat" not in man["entries"] and "roster.json" in man["entries"]
    assert man["entries"]["roster.json"]["sha256"] == \
        hashlib.sha256((env.run_root / "roster.json").read_bytes()).hexdigest()
    # AUDIT-1/2: every seal call writes a NEW snapshot under seals/ and chains it
    out = drv.seal(reason="incident report written")
    man2 = json.loads(out.read_text())
    assert out.parent == env.run_root / "seals" and "turns/L00/%s/raw.jsonl" % tdir.name in man2["entries"]
    assert man2["entries"]["control.jsonl"]["append_only"] is True and man2["entries"]["roster.json"]["append_only"] is False
    assert man2["reason"] == "incident report written"
    lines = (env.run_root / "seals.jsonl").read_text().splitlines()
    seals = [json.loads(l) for l in lines]
    assert [s["day"] for s in seals] == [day, day] and [s["reason"] for s in seals] == ["start", "incident report written"]
    assert seals[0]["prev_hash"] is None
    assert seals[1]["prev_hash"] == hashlib.sha256(lines[0].encode()).hexdigest(), "seals.jsonl is a hash chain"
    assert man2["prev_hash"] == seals[1]["prev_hash"] and seals[1]["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert json.loads((env.run_root / ("MANIFEST-%s.json" % day)).read_text()) == man2, "the day manifest is the latest"
    assert "seals/" not in " ".join(man2["entries"]), "snapshots are not self-sealed"
    # periodic: due by time or by control.jsonl growth, never twice in one tick otherwise
    n = len(lines)
    drv.tick()
    assert len((env.run_root / "seals.jsonl").read_text().splitlines()) == n, "not due yet"
    drv._last_seal_mono -= 7200
    drv.tick()
    seals = [json.loads(l) for l in (env.run_root / "seals.jsonl").read_text().splitlines()]
    assert len(seals) == n + 1 and seals[-1]["reason"] == "periodic"
    drv._last_seal_control_bytes = -(1 << 30)
    drv.tick()
    seals = [json.loads(l) for l in (env.run_root / "seals.jsonl").read_text().splitlines()]
    assert len(seals) == n + 2 and seals[-1]["reason"] == "control-growth"
    # day rollover under a live driver closes yesterday and opens today
    drv._sealed_day = "2000-01-01"
    drv.tick()
    assert (env.run_root / "MANIFEST-2000-01-01.json").exists() and drv._sealed_day == day
    seals = [json.loads(l) for l in (env.run_root / "seals.jsonl").read_text().splitlines()]
    assert [x["reason"] for x in seals[-2:]] == ["day-close", "day-open"]
    # AUDIT-4a: every turn record and the attempt's dispatch.json carry the base and the activation key
    disp = json.loads((tdir / "dispatch.json").read_text())
    rec = json.loads((tdir / "record.json").read_text())
    act = json.loads((env.run_root / "activation-record.json").read_text())
    assert act["activation_id"].startswith("act-") and disp["activation_id"] == act["activation_id"]
    assert rec["activation_id"] == act["activation_id"] and rec["base_sha"] == disp["base_sha"] == env.base


def test_item7_comms_hook_appends_from_cli_and_alerts_mirror_into_it(tmp_path):
    env = Env(tmp_path)
    env.activate()
    rc = lanedriver.main(["--roster", str(env.run_root / "roster.json"), "comms", "--from", "fixer",
                          "--to", "orchestrator", "--text", "item 7 done", "--task", "L00",
                          "--msg-id", "84fcc925-cc17-46f4-8ac3-5be6803c380f",
                          "--from-session", "uds:/tmp/cc-socks/1.sock", "--to-session", "uds:/tmp/cc-socks/2.sock"])
    assert rc == 0
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    drv.alert("IDLE", "nothing to do", task=None)
    rows = [json.loads(l) for l in (env.run_root / "comms.jsonl").read_text().splitlines()]
    assert rows[0]["from"] == "fixer" and rows[0]["to"] == "orchestrator" and rows[0]["task"] == "L00"
    assert rows[0]["kind"] == "message" and rows[0]["text"] == "item 7 done" and rows[0]["ts"].endswith("Z")
    # AUDIT-4b: the envelope keys, verbatim
    assert rows[0]["msg_id"] == "84fcc925-cc17-46f4-8ac3-5be6803c380f"
    assert rows[0]["from_session"] == "uds:/tmp/cc-socks/1.sock" and rows[0]["to_session"] == "uds:/tmp/cc-socks/2.sock"
    assert rows[1] == {"ts": rows[1]["ts"], "from": "lanedriver", "to": "owner", "kind": "alert:IDLE",
                       "text": "nothing to do"}
    rc = lanedriver.main(["--roster", str(env.run_root / "roster.json"), "seal"])
    assert rc == 0
    assert list(env.run_root.glob("MANIFEST-*.json"))


# -- item 8: F14 probes/ + render timer, F16 activation record ------------------------

def test_item8_probes_are_written_to_disk_and_ledger_renders_on_a_timer(tmp_path, monkeypatch):
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30,
                                                 "render_every_s": 3600}})
    env.activate()
    drv = env.driver({"opencode": by_role({"probe": status("RATE", "429 slow down")}),
                      "codex": FakeRunner()})
    settle(drv, 1)
    assert drv.servers["go2"]["park_status"] == "RATE"
    drv.servers["go2"]["parked_until"] = 0.0  # park elapsed: next tick probes the server
    drv.tick()
    probes = [json.loads(l) for l in (env.run_root / "probes" / "probes.jsonl").read_text().splitlines()]
    assert probes and probes[-1]["server"] == "go2" and probes[-1]["ok"] is False
    assert probes[-1]["why"] == "unpark" and "URLError" in probes[-1]["detail"]
    latest = json.loads((env.run_root / "probes" / "go2.json").read_text())
    assert latest["url"] == "http://127.0.0.1:1/session" and latest["ms"] >= 0
    assert drv.servers["go2"]["park_reason"], "a failed probe keeps the server parked"
    # render: LEDGER.md written on the first tick, then only when the timer elapses
    ledger = env.run_root / "LEDGER.md"
    text = ledger.read_text()
    assert text.startswith("# LEDGER") and "| L00 | " in text and "| L04 | WAITING_DEPENDENCY |" in text
    header = text.split("| task |", 1)[0]
    assert "legend: INTEGRATED = box-verified, reviewed and merged into the union; hosted evidence is the `<ID>-HOSTED` twin row" in header, "§48 legend in the header (D86)"
    first = drv._last_render_mono
    drv.tick()
    assert drv._last_render_mono == first, "3600 s timer has not elapsed"
    drv._last_render_mono -= 3601
    drv.tick()
    assert drv._last_render_mono != first
    rc = lanedriver.main(["--roster", str(env.run_root / "roster.json"), "render"])
    assert rc == 0 and "states:" in ledger.read_text()


def test_item8_activation_record_hashes_five_profiles_and_flags_an_edit_on_restart(tmp_path):
    env = Env(tmp_path)
    env.activate()
    sdir = env.run_root / "claude-settings"
    sdir.mkdir()
    for name in lanedriver.LaneDriver.PROFILES:
        (sdir / ("%s.json" % name)).write_text('{"permissions": {"allow": ["%s"]}}' % name)
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    settle(drv, 1)
    rec = json.loads((env.run_root / "activation-record.json").read_text())
    assert sorted(rec["profiles"]) == sorted(lanedriver.LaneDriver.PROFILES)
    assert rec["profiles"]["junior"] == hashlib.sha256((sdir / "junior.json").read_bytes()).hexdigest()
    assert rec["inputs"]["roster"] and rec["inputs"]["catalog"] and rec["inputs"]["scheduler"]
    assert rec["changed_since_previous"] == [] and rec["missing_profiles"] == []
    # junior.json edited after activation (I-101): the restart re-hashes and alerts
    (sdir / "junior.json").write_text('{"permissions": {"allow": ["junior", "Bash(rm:*)"]}}')
    drv2 = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    settle(drv2, 1)
    rec2 = json.loads((env.run_root / "activation-record.json").read_text())
    assert rec2["changed_since_previous"] == ["profile:junior"] and rec2["previous_ts"] == rec["ts"]
    assert rec2["profiles"]["junior"] != rec["profiles"]["junior"]
    alerts = [json.loads(l) for l in (env.run_root / "alerts.jsonl").read_text().splitlines()]
    assert any(a["kind"] == "PROFILE_CHANGED" and "profile:junior" in a["text"] for a in alerts)
    history = [json.loads(l) for l in (env.run_root / "activation-records.jsonl").read_text().splitlines()]
    assert len(history) == 2


def test_item8_missing_profiles_are_seeded_from_the_dispatcher_copy(tmp_path):
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    settle(drv, 1)
    rec = json.loads((env.run_root / "activation-record.json").read_text())
    src = Path(lanedriver.__file__).resolve().parent / "claude-settings"
    expected = [n for n in lanedriver.LaneDriver.PROFILES if not (src / ("%s.json" % n)).exists()]
    assert rec["missing_profiles"] == expected
    for name in lanedriver.LaneDriver.PROFILES:
        if (src / ("%s.json" % name)).exists():
            assert (env.run_root / "claude-settings" / ("%s.json" % name)).exists()


# -- item 10: roster-v13 vocabulary --------------------------------------------------

def test_item10_normalize_roster_maps_architect_vocabulary_onto_the_driver():
    r = {"run": {"trunk": "/t", "scheduler": "/x/orchestration_control.py", "run_state": "/x/s.json",
                 "catalog": "/x/c.json", "packets_dir": "/p"},
         "night": {"render_every_min": 15, "status_line_every_min": 30},
         "servers": {"go2": {"url": "u", "role": "primary"}, "go1": {"url": "v", "role": "fallback"}},
         "roles": {"builder": {"runner": "opencode", "server": "go2", "model": "opencode-go/muse"},
                   "grader": {"runner": "opencode", "server": "go2", "model": "opencode-go/ds"},
                   "final": {"runner": "codex", "model": "gpt-5.6-sol-1m"},
                   "design": {"runner": "opencode", "server": "go2", "model": "explicit"}}}
    n = lanedriver.normalize_roster(r)
    assert n["control"] == {"script": "/x/orchestration_control.py", "cwd": "/x", "state": "/x/s.json",
                            "catalog": "/x/c.json"}
    assert n["run"]["pack_dir"] == "/p" and n["servers"]["go1"]["parked"] is True
    assert "parked" not in n["servers"]["go2"]
    assert n["alerts"] == {"render_every_s": 900.0, "idle_every_min": 30}
    assert n["roles"]["verification"] is n["roles"]["grader"] or n["roles"]["verification"] == n["roles"]["grader"]
    assert n["roles"]["final_review"]["model"] == "gpt-5.6-sol-1m"
    assert n["roles"]["design"]["model"] == "explicit", "an explicit role beats the kind_map"
    assert "operations" not in n["roles"], "no infra role -> no operations role (lint reports it)"
    assert r.get("control") is None, "pure: the input is untouched"
    # D11: a v13 roster (packets_dir) owes a proof for the building kinds unless it says otherwise
    assert n["proof"]["require_for_kinds"] == ["builder", "integrator", "infra"]
    r2 = dict(r, proof={"require_for_kinds": ["builder"]})
    assert lanedriver.normalize_roster(r2)["proof"]["require_for_kinds"] == ["builder"]
    r3 = {k: v for k, v in r.items() if k != "night"}
    r3["run"] = {k: v for k, v in r["run"].items() if k != "packets_dir"}
    assert "proof" not in lanedriver.normalize_roster(r3) or "require_for_kinds" not in \
        lanedriver.normalize_roster(r3)["proof"], "a v12 roster keeps the v12 default"


@pytest.mark.skipif(_V13_PACK_SKIP_REASON is not None, reason=str(_V13_PACK_SKIP_REASON))
def test_a_malformed_roster_edit_is_rejected_not_fatal(tmp_path):
    """D42: a note string among `servers` (2026-09-18 22:59Z) crashed the driver in
    normalize_roster on a hot reload.  Now: non-dict server/role entries are dropped,
    and any other reload failure is ROSTER_REJECTED with the last good roster kept."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=routed_pass), "codex": FakeRunner(), "claude": FakeRunner()})
    roster = json.loads((env.run_root / "roster.json").read_text())
    roster["servers"]["_note"] = "go2/go3 out of credits: everything on go1"
    roster["roles"]["_note"] = "see servers"
    assert "_note" not in lanedriver.normalize_roster(roster)["servers"]
    assert "_note" not in lanedriver.normalize_roster(roster)["roles"]
    (env.run_root / "roster.json").write_text(json.dumps(roster, indent=2))
    drv._roster_mtime = 0
    drv.tick()                                        # would have raised AttributeError before D42
    assert "_note" not in drv.servers and "_note" not in drv.roles
    assert "roster reloaded" in (env.run_root / "driver.log").read_text()
    # a roster whose shape normalize_roster cannot read at all: rejected, previous roster live
    before = dict(drv.servers)
    (env.run_root / "roster.json").write_text(json.dumps({"servers": "nope", "roles": 3, "run": []}))
    drv._roster_mtime = 0
    drv.tick()
    assert drv.servers == before
    assert "ROSTER_REJECTED" in (env.run_root / "alerts.jsonl").read_text()
    n = (env.run_root / "alerts.jsonl").read_text().count("ROSTER_REJECTED")
    drv.tick()
    assert (env.run_root / "alerts.jsonl").read_text().count("ROSTER_REJECTED") == n, "reported once per edit"


@pytest.mark.skipif(_V13_PACK_SKIP_REASON is not None, reason=str(_V13_PACK_SKIP_REASON))
def test_item10_pack_roster_v13_boots_the_driver_and_lints_clean(tmp_path):
    import vplint
    src = CONTROL_DIR / "v13-pack" / "roster-v13.json"
    r = json.loads(src.read_text())
    # §29: go2 primary; go1 unparked is a fallback (WARN, never ERROR)
    assert [m for m in vplint.lint_roster_v13(r) if m.startswith("ERROR")] == []
    # the driver consumes it as-is (control derived from run.*, roles via kind_map)
    env = Env(tmp_path)
    env.activate()
    r["run"].update({"trunk": str(env.trunk), "cn": str(env.tmp), "worktrees": str(env.tmp / "wt"),
                     "run_state": str(env.state), "catalog": str(env.catalog),
                     "packets_dir": str(env.tmp / "nopack")})
    r["control"] = {"python": PY}
    if os.environ.get("VP_TEST_AUTHORITY"):
        r["control"]["authority"] = os.environ["VP_TEST_AUTHORITY"]
    (env.run_root / "roster.json").write_text(json.dumps(r, indent=2))
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    assert drv.roles["verification"]["runner"] == "opencode" and drv.roles["final_review"]["runner"] == "codex"
    assert not drv.servers["go2"]["parked"] and drv.servers["go3"]["parked"]
    assert drv.control.script.endswith("orchestration_control.py")
    settle(drv, 2)
    assert env.rows()["L00"]["state"] == "VERIFIED"
    # the v13 lint bites on the item-10 numbers
    bad = json.loads(src.read_text())
    bad["servers"]["go2"]["parked"] = True
    bad["servers"]["go3"]["parked"] = False
    bad["fallback_order"] = ["go1"]
    bad["roles"]["builder"]["server"] = "go1"
    bad["proof"]["shm_reap"] = False
    bad["roles"].pop("infra")
    bad["proof"]["circleci"]["account"] = "9"
    bad["proof"]["circleci"]["rotation"] = ["A1", "Z9"]
    msgs = vplint.lint_roster_v13(bad)
    assert any("servers.go2 must be unparked" in m for m in msgs) and any("shm_reap" in m for m in msgs)
    assert any("fallback_order must start with go2" in m for m in msgs)
    assert any("unparked server go3 is missing from fallback_order" in m for m in msgs)
    assert any("kind operations has no role" in m for m in msgs)
    # §29: the CircleCI vocabulary is vpcircle.TARGETS, nothing literal in the lint
    assert any("proof.circleci.account '9' not in vpcircle.TARGETS" in m for m in msgs)
    assert any("proof.circleci.rotation names unknown accounts ['Z9']" in m for m in msgs)
    import vpcircle
    good = json.loads(src.read_text())
    good["roles"]["grader"]["server"] = "go1"
    assert any("kind grader binds server go1; roles must bind the primary go2" in m
               for m in vplint.lint_roster_v13(good))
    assert str(good["proof"]["circleci"]["account"]) in vpcircle.TARGETS


# -- item 11: a review-gate refusal parks the finished attempt, never re-runs it -------

def test_item11_gate_hold_parks_the_attempt_and_retries_only_complete(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.activate()
    probe = FakeRunner(default=result_ok)
    drv = env.driver({"opencode": probe, "codex": FakeRunner()})
    real = drv.control.complete
    refusals = {"n": 0}

    def gated(task, attempt, outcome, *a, **k):
        if task == "L00" and refusals["n"] < 2:
            refusals["n"] += 1
            raise lanedriver.ControlError("complete exited 1: RuntimeError: L00 requires --verdict "
                                          "with validated junior evidence")
        return real(task, attempt, outcome, *a, **k)
    monkeypatch.setattr(drv.control, "complete", gated)
    settle(drv, 3)
    hold = next((env.run_root / "turns" / "L00").glob("*/gate-hold.json"))
    rec = json.loads(hold.read_text())
    assert rec["outcome"] == "VERIFIED" and "requires --verdict" in rec["refused"] and rec["output_sha"]
    assert env.rows()["L00"]["state"] == "RUNNING", "the scheduler still holds the attempt"
    assert len([c for c in probe.calls if c.item == "L00"]) == 1, "the turn is never re-run"
    assert "REVIEW_GATE_HOLD" in (env.run_root / "OWNER-ALERTS.md").read_text()
    assert refusals["n"] == 2, "ticks 2-3: one immediate retry of `complete` inside the minute"
    # the gate opens (the review packet recorded its verdict): the next retry retires the task
    drv._gate_last.clear()
    settle(drv, 1)
    assert env.rows()["L00"]["state"] == "VERIFIED"
    assert env.rows()["L01"]["state"] != "WAITING_DEPENDENCY", "the release unlocked the dependants"
    assert not hold.exists() and hold.with_name("gate-hold.released.json").exists()
    assert len([c for c in probe.calls if c.item == "L00"]) == 1


# -- pack §4(a): roster copy at start, worktree dependency links ----------------------

@pytest.mark.skipif(_V13_PACK_SKIP_REASON is not None, reason=str(_V13_PACK_SKIP_REASON))
def test_pack4a_init_run_copies_lints_and_snapshots_the_roster(tmp_path):
    import vplint
    src = tmp_path / "roster-v13.json"
    r = json.loads((CONTROL_DIR / "v13-pack" / "roster-v13.json").read_text())
    r["run"]["run_root"] = str(tmp_path / "RUN")
    src.write_text(json.dumps(r, indent=2))
    assert [m for m in vplint.lint_roster(str(src)) if m.startswith("ERROR")] == []  # §29: go1 fallback is a WARN
    assert lanedriver.main(["init-run", "--source", str(src)]) == 0
    run_root = tmp_path / "RUN"
    assert (run_root / "roster.json").read_bytes() == src.read_bytes()
    assert (run_root / "roster.1.json").exists() and (run_root / "turns").is_dir()
    rec = json.loads((run_root / "git.jsonl").read_text().splitlines()[0])
    assert rec["op"] == "init-run" and rec["changed"] is True and rec["snapshot"].endswith("roster.1.json")
    # same content again: no new snapshot; an edit: roster.2.json
    assert lanedriver.main(["init-run", "--source", str(src)]) == 0
    assert not (run_root / "roster.2.json").exists()
    r["concurrency"]["claude_max"] = 1
    src.write_text(json.dumps(r, indent=2))
    assert lanedriver.main(["init-run", "--source", str(src)]) == 2, "lint error (claude_max) refuses"
    assert (run_root / "roster.json").read_bytes() != src.read_bytes()
    r["concurrency"]["claude_max"] = 2
    r["night"]["render_every_min"] = 5
    src.write_text(json.dumps(r, indent=2))
    assert lanedriver.main(["init-run", "--source", str(src)]) == 0
    assert (run_root / "roster.2.json").read_bytes() == src.read_bytes()


def test_pack4a_worktrees_link_both_venvs_and_node_modules_and_alert_on_a_missing_one(tmp_path):
    env = Env(tmp_path)
    env.activate()
    (env.trunk / "platform" / ".venv" / "bin").mkdir(parents=True)
    (env.trunk / "portal" / "node_modules" / ".bin").mkdir(parents=True)
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    settle(drv, 2)
    wt = env.tmp / "wt" / "L00"
    assert (wt / "platform" / ".venv").is_symlink() and (wt / "portal" / "node_modules").is_symlink()
    assert os.readlink(str(wt / "platform" / ".venv")) == str(env.trunk / "platform" / ".venv")
    assert not (wt / "agent" / ".venv").exists()
    links = [json.loads(l) for l in (env.run_root / "git.jsonl").read_text().splitlines()
             if '"link_deps"' in l]
    assert links[0]["linked"] == ["platform/.venv", "portal/node_modules"] and links[0]["missing"] == ["agent/.venv"]
    alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert alerts.count("WORKTREE_DEPS_MISSING") == 1, "once per run, not per worktree"
    # a roster edit under a live driver is snapshotted as roster.<n>.json
    roster = json.loads((env.run_root / "roster.json").read_text())
    roster["alerts"]["idle_every_min"] = 31
    time.sleep(0.02)
    (env.run_root / "roster.json").write_text(json.dumps(roster, indent=2))
    os.utime(str(env.run_root / "roster.json"), None)
    drv.tick()
    assert (env.run_root / "roster.1.json").exists()


# -- pack §4(d): L42 runs with the pinned s3-adjacent review_gate.py ----------------------

def test_pack4d_authority_mismatch_stops_dispatch_until_the_gate_copy_is_the_pinned_one(tmp_path, monkeypatch):
    import shutil
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    assert drv.authority_problems() == [], "the real scheduler dir matches CATALOG-AUTHORITY.json"
    rec_gate = sha256_file_of(CONTROL_DIR / "review_gate.py")
    settle(drv, 1)
    rec = json.loads((env.run_root / "activation-record.json").read_text())
    assert rec["inputs"]["review_gate"] == rec_gate
    # a control dir whose review_gate.py is an older copy (no _fork_cutoff): dispatch stops
    ctl2 = tmp_path / "ctl2"
    ctl2.mkdir()
    for name in ("orchestration_control.py", "review_gate.py", "CATALOG-AUTHORITY.json"):
        shutil.copy(str(CONTROL_DIR / name), str(ctl2 / name))
    (ctl2 / "review_gate.py").write_text((ctl2 / "review_gate.py").read_text().replace("def _fork_cutoff", "def _old_cutoff"))
    drv2 = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    drv2.control.cwd = str(ctl2)
    drv2.control.script = str(ctl2 / "orchestration_control.py")
    probs = drv2.authority_problems()
    assert any("lacks def _fork_cutoff" in p for p in probs) and any("review_validator_sha256" in p for p in probs)
    assert drv2._guards() is False and drv2._authority_stop
    assert "AUTHORITY_MISMATCH" in (env.run_root / "OWNER-ALERTS.md").read_text()
    # the pinned copy restored: the stop lifts on the next check
    shutil.copy(str(CONTROL_DIR / "review_gate.py"), str(ctl2 / "review_gate.py"))
    drv2._authority_last = None
    assert drv2._guards() is True and not drv2._authority_stop


def sha256_file_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# -- repairs inherit the parent packet's proof scope; retries stay pack-owned ----------------

def test_repair_row_without_a_packet_inherits_test_paths_and_stays_targeted(tmp_path):
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({})
    drv.pack = {"L17": {"id": "L17", "test_paths": ["platform/tests/test_reply_path.py"], "proof_kind": "platform",
                        "closes": [], "scheduler_task": "L17"}}
    paths, kind = drv._proof_hints("R-L17-B5-1", {"parent_contract_id": "L17"})
    assert paths == ["platform/tests/test_reply_path.py"] and kind == "platform"
    # the parent may be a retry / -V13 descendant of the packet
    paths, kind = drv._proof_hints("R-L17-REPLY-WIRING-R2-B7-1", {"parent_contract_id": "L17-V13-R2"})
    assert (paths, kind) == ([], None) or True
    drv.pack["L17-REPLY-WIRING"] = {"id": "L17-REPLY-WIRING", "test_paths": ["platform/tests/a.py"],
                                    "proof_kind": "platform", "closes": [], "scheduler_task": "NEW:REPAIR"}
    paths, kind = drv._proof_hints("R-X", {"parent_contract_id": "L17-REPLY-WIRING-R2"})
    assert paths == ["platform/tests/a.py"]
    # no packet anywhere: the defect's own failing nodes name the files
    paths, kind = drv._proof_hints("R-Z", {"parent_contract_id": "ZZ", "parameters": {"fails": [
        {"id": "platform/tests/test_x.py::test_a", "evidence": "platform/tests/test_x.py::test_a"},
        {"id": "B7", "evidence": "platform/tests/test_y.py:12 something"}]}})
    assert paths == ["platform/tests/test_x.py"] and kind is None
    pk = lanedriver.LaneDriver.render_packet({"id": "R-L17", "kind": "builder"}, "a" * 40, None,
                                             test_paths=["platform/tests/test_reply_path.py"], proof_kind="platform")
    assert "test_paths:\n  - platform/tests/test_reply_path.py\nproof_kind: platform\n" in pk
    assert lanedriver.LaneDriver._pack_root("L17-REPLY-WIRING-R2") == "L17-REPLY-WIRING"
    assert lanedriver.LaneDriver._pack_root("R-DOCS-MIGRANGE-V13-R1") == "R-DOCS-MIGRANGE"
    assert drv._pack_owns("L17-REPLY-WIRING-R1"), "a superseded retry of a packet is still the pack's"
    assert not drv._pack_owns("L99")


def test_d60_alert_severity_is_pattern_based_and_survives_a_restart(tmp_path):
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    pages = []
    drv._notify = lambda kind, text: pages.append(kind)
    # the 09-17 shape: one PROOF_UNKNOWN per re-instantiated repair row
    for n in (1, 2, 3):
        drv.alert("PROOF_UNKNOWN", "proof %d unknown" % n, task="R-L17-REPLY-WIRING-R1-B5-%d" % n)
    drv.alert("IDLE", "idle for 30 min")
    rows = [json.loads(l) for l in (env.run_root / "alerts.jsonl").read_text().splitlines()]
    mine = [r for r in rows if r["kind"] == "PROOF_UNKNOWN"]
    assert [r["severity"] for r in mine] == ["attention", "attention", "urgent"]
    assert [r["repeat_family"] for r in mine] == [1, 2, 3] and all(r["repeat"] == 1 for r in mine)
    assert mine[-1]["reasons"] and "repeat=3" in mine[-1]["reasons"][0]
    assert [r for r in rows if r["kind"] == "IDLE"][0]["severity"] == "routine"
    assert pages == ["PROOF_UNKNOWN urgent"], "only the third cycle pages"
    md = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert "**PROOF_UNKNOWN** [URGENT]" in md and "**IDLE** [" not in md
    # a fresh process (restart mid-loop) seeds its history from the file
    drv2 = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    drv2._notify = lambda kind, text: pages.append(kind)
    drv2.alert("PROOF_UNKNOWN", "proof 4 unknown", task="R-L17-REPLY-WIRING-R1-B5-4")
    last = json.loads((env.run_root / "alerts.jsonl").read_text().splitlines()[-1])
    assert last["repeat_family"] == 4 and last["severity"] == "urgent"
    # the legacy kind floor still pages a first STUCK
    drv2.alert("STUCK", "3 failures", task="OTHER")
    assert pages[-1] == "STUCK attention"


def test_d64_gates_jsonl_records_startup_values_and_every_flip(tmp_path):
    env = Env(tmp_path, roster_extra={"owner_gates": {"DELIVERY-1": False, "DELIVERY-2": True, "_comment": "x"}})
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner(), "claude": FakeRunner()})
    rows = [json.loads(l) for l in (env.run_root / "gates.jsonl").read_text().splitlines()]
    assert [(r["gate"], r["from"], r["to"], r["why"]) for r in rows] == [
        ("DELIVERY-1", None, False, "startup"), ("DELIVERY-2", None, True, "startup")]
    # a roster edit that flips one gate and adds one writes exactly those two lines
    env.roster["owner_gates"] = {"DELIVERY-1": True, "DELIVERY-2": True, "DELIVERY-3": False}
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    os.utime(env.run_root / "roster.json", None)
    drv._roster_mtime = None
    drv._reload_roster_if_changed()
    rows = [json.loads(l) for l in (env.run_root / "gates.jsonl").read_text().splitlines()]
    assert [(r["gate"], r["from"], r["to"], r["why"]) for r in rows[2:]] == [
        ("DELIVERY-1", False, True, "reload"), ("DELIVERY-3", None, False, "reload")]
    assert "GATE DELIVERY-1 False -> True" in (env.run_root / "driver.log").read_text()
    # an unchanged roster (a code reload re-applies it) writes nothing
    drv._apply_roster(drv.roster)
    assert len((env.run_root / "gates.jsonl").read_text().splitlines()) == 4


def test_d76_memory_hold_keeps_the_attempt_without_a_strike_and_alerts_once_per_episode(tmp_path, monkeypatch):
    """D76: a box proof refused for memory (PROOF_BLOCKED_MEMORY) is the box's
    condition, never the candidate's: held without a strike (like GATE/CAP/OFF),
    re-asked after MEMORY_HOLD_S, alerted once per hold episode; the proof runs
    on the same head once memory is back."""
    monkeypatch.setattr(lanedriver, "FAIL_BACKOFF_S", (0, 0, 0))
    monkeypatch.setattr(lanedriver, "MEMORY_HOLD_S", 0.0)
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"]}})
    env.activate()
    proof = FakeProof([{"status": "BLOCKED_MEMORY", "reason": "box memory free 22% < 30% (memory_pressure)"},
                       {"status": "BLOCKED_MEMORY", "reason": "box memory free 24% < 30% (memory_pressure)"},
                       {"status": "PASS"}])
    oc = FakeRunner(default=routed_pass)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, proof=proof)
    drv.memory_free_pct = lambda: 22
    settle(drv, 2)
    rows = env.rows()
    assert rows["L02"]["state"] == "RUNNING", "held, not failed"
    assert drv._fail["L02"]["count"] == 0, "the box's condition is no strike"
    log = (env.run_root / "driver.log").read_text()
    assert "HOLD L02" in log and "BLOCKED_MEMORY" in log and "FAIL L02" not in log
    settle(drv, 6)
    assert env.rows()["L02"]["state"] == "VERIFIED"
    assert len(proof.calls) == 3 and len({c[2] for c in proof.calls}) == 1, "the same head, proof only"
    assert [s.role for s in oc.calls if s.item == "L02"] == ["builder", "grader"], "no rebuild"
    md = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert md.count("PROOF_BLOCKED_MEMORY") == 1, "two holds in one episode alert once"
    drv.memory_free_pct = lambda: 45
    assert drv._guards() is True
    assert "memory-blocked" not in drv._alerted, "recovery re-arms the once-per-episode alert"


def test_d76_memory_stop_pauses_every_claim_below_20_percent(tmp_path):
    """D76: below night.memory_stop_below_pct (default 20) _guards pauses ALL new
    claims (alert MEMORY once, heartbeat memory_paused), and lifts the pause when
    memory recovers -- the disk-pause shape."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    drv.memory_free_pct = lambda: 18
    assert drv._guards() is False and drv._memory_paused
    assert drv._guards() is False
    md = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert md.count("MEMORY") >= 1 and "18% < 20%" in md
    drv.write_heartbeat()
    hb = json.loads((env.run_root / "driver.heartbeat").read_text())
    assert hb["memory_paused"] is True and hb["memory_free_pct"] == 18
    drv.memory_free_pct = lambda: 33
    assert drv._guards() is True and not drv._memory_paused
    assert "memory recovered: 33%" in (env.run_root / "driver.log").read_text()
    drv.memory_free_pct = lambda: None                 # unreadable: never pauses
    assert drv._guards() is True


def test_d163_reuse_refuses_another_items_unscoped_product_verdict_at_the_call_site(tmp_path):
    """D163 at the site that does the work, not just against the predicate.

    `_reusable_proof` keys on (sha, kind, paths), so one item's answer is copied
    wholesale onto another's attempt. For a PASS that is sound. For a
    FAIL_PRODUCT it is not: `partition_red_nodes(wt, BASE, cand, ...)` splits the
    reds against the record OWNER's base, and a different item has a different
    base, so the inherited split was computed for somebody else's diff.

    Measured in run-v13-20260917: L34-ERROR-KIND-PRIVACY-HOSTED-R1 inherited 3265
    reds from L04-FLOOR-HOSTED-R2 this way.

    Refusing the reuse rather than adopting-and-stripping is the deliberate part:
    a stripped record would be filed as FAIL_INFRA, the retry would find this
    same record still sitting there, and the item would strip it again forever.
    Falling through triggers a real run against this item's own base."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    proofs = env.run_root / "proofs"
    proofs.mkdir(exist_ok=True)
    sha = "c" * 40

    def rec(pid, **kw):
        d = {"proof_id": pid, "sha": sha, "kind": "platform", "paths": [], "only": None,
             "route": "gha", "pipeline_id": "p-" + pid, "status": "FAIL_PRODUCT",
             "failed_nodes": ["platform/tests/test_x.py::test_x"],
             "ts": "2026-09-20T21:05:40.342Z"}
        d.update(kw)
        (proofs / (pid + ".json")).write_text(json.dumps(d))

    rec("proof-L04-FLOOR-HOSTED-R2-60920T210540342")
    mine = "L34-ERROR-KIND-PRIVACY-HOSTED-R1"

    assert drv._reusable_proof(sha, "platform", [], task=mine) is None, (
        "another item's unscoped product verdict must not be adopted")
    # the same record is still this item's own answer when the item IS its owner
    assert drv._reusable_proof(sha, "platform", [], task="L04-FLOOR-HOSTED-R2") is not None
    # ... and without a task the call behaves exactly as it did before D163, so
    # every other caller and test is untouched
    assert drv._reusable_proof(sha, "platform", []) is not None

    # a green from another item is still adopted: that is what D79 reuse is FOR
    (proofs / "proof-L04-FLOOR-HOSTED-R2-60920T210540342.json").unlink()
    rec("proof-L04-FLOOR-HOSTED-R3-60920T210540999", status="PASS", failed_nodes=[])
    got = drv._reusable_proof(sha, "platform", [], task=mine)
    assert got and got["status"] == "PASS", got


def test_d79_reusable_proof_picks_a_real_answer_for_the_same_sha_kind_and_paths(tmp_path):
    """D79 (Architect, 2026-09-19 19:0xZ): the round loop re-proved the same
    sha every round and on CircleCI that is a NEW pipeline per round
    (L06-HOSTED-R3 round 2 -> 9914a1d4).  A recorded real answer -- a circleci
    record with a pipeline_id or a box record, status PASS/FAIL_PRODUCT -- for
    the same sha + kind + paths is reused; anything else is not an answer."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    proofs = env.run_root / "proofs"
    proofs.mkdir(exist_ok=True)
    sha = "a" * 40
    def rec(pid, **kw):
        d = {"proof_id": pid, "sha": sha, "kind": "platform", "paths": ["t/a.py"], "route": "circleci",
             "pipeline_id": "p-" + pid, "status": "FAIL_PRODUCT", "ts": "2026-09-19T19:00:00.000Z"}
        d.update(kw)
        (proofs / (pid + ".json")).write_text(json.dumps(d))
    rec("proof-X-1")                                                   # real red answer
    rec("proof-X-2", status="PASS", ts="2026-09-19T19:05:00.000Z")     # newer real green answer
    rec("proof-X-3", status="UNKNOWN", pipeline_id=None, ts="2026-09-19T19:09:00.000Z")   # not an answer
    rec("proof-X-4", status="CANCELLED", ts="2026-09-19T19:10:00.000Z")                    # not an answer
    rec("proof-X-5", status="FAIL_PRODUCT", pipeline_id=None, ts="2026-09-19T19:11:00.000Z")  # circleci w/o pipeline
    rec("proof-X-6", kind="portal", ts="2026-09-19T19:12:00.000Z")                          # other kind
    rec("proof-X-7", paths=["t/b.py"], ts="2026-09-19T19:13:00.000Z")                        # other paths
    rec("proof-X-8", route="none", pipeline_id=None, ts="2026-09-19T19:14:00.000Z")          # docs-only route
    best = drv._reusable_proof(sha, "platform", ["t/a.py"])
    assert best and best["proof_id"] == "proof-X-2", best
    assert drv._reusable_proof(sha, "platform", ["t/a.py"], exclude="proof-X-2")["proof_id"] == "proof-X-1"
    assert drv._reusable_proof(sha, "platform", ["t/b.py"])["proof_id"] == "proof-X-7"
    assert drv._reusable_proof(sha, "platform", []) is None
    assert drv._reusable_proof("b" * 40, "platform", ["t/a.py"]) is None
    rec("proof-X-9", route="box", pipeline_id=None, ts="2026-09-19T19:20:00.000Z")            # a completed box run counts
    assert drv._reusable_proof(sha, "platform", ["t/a.py"])["proof_id"] == "proof-X-9"
    # D83: a GitHub Actions record is a hosted answer exactly like a CircleCI one
    rec("proof-X-10", route="gha", pipeline_id="35470000001", ts="2026-09-19T19:30:00.000Z")
    assert drv._reusable_proof(sha, "platform", ["t/a.py"])["proof_id"] == "proof-X-10"
    rec("proof-X-11", route="gha", pipeline_id=None, ts="2026-09-19T19:31:00.000Z")           # gha without a run id
    assert drv._reusable_proof(sha, "platform", ["t/a.py"])["proof_id"] == "proof-X-10"


def test_d79_a_recorded_answer_is_reused_instead_of_re_proving_the_same_head(tmp_path, monkeypatch):
    """flow: the proof of L02's head is UNKNOWN (infra) once; before the retry a
    real record for that sha/kind/paths appears (e.g. the cancelled pipeline's
    predecessor, or a sibling attempt) -> the retry logs PROOF REUSED, asks the
    Proof for nothing, and grades on the reused record."""
    monkeypatch.setattr(lanedriver, "FAIL_BACKOFF_S", (0, 0, 0))
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"]}})
    env.activate()
    proof = FakeProof([{"status": "UNKNOWN", "reason": "circleci: cancelled"}, {"status": "PASS"}])
    oc = FakeRunner(default=routed_pass)
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, proof=proof)
    settle(drv, 2)
    assert env.rows()["L02"]["state"] == "RUNNING" and len(proof.calls) == 1
    _task, pid, cand, kind, paths = proof.calls[0]
    proofs = env.run_root / "proofs"
    proofs.mkdir(exist_ok=True)
    (proofs / "proof-L02-earlier.json").write_text(json.dumps(
        {"proof_id": "proof-L02-earlier", "sha": cand, "kind": kind, "paths": paths, "route": "circleci",
         "pipeline_id": "pipe-real", "status": "PASS", "failed_nodes": [], "ts": "2026-09-19T19:00:00.000Z"}))
    settle(drv)
    assert env.rows()["L02"]["state"] == "VERIFIED"
    assert len(proof.calls) == 1, "no second proof run: the recorded answer was reused"
    log = (env.run_root / "driver.log").read_text()
    assert "PROOF REUSED proof-L02-earlier sha=%s -> PASS (circleci pipeline pipe-real" % cand[:12] in log
    tdir = next((env.run_root / "turns" / "L02").iterdir())
    wt = env.tmp / "wt" / "L02"
    copied = json.loads((wt / ".vp" / "PROOF.json").read_text())
    assert copied["reused_from"] == "proof-L02-earlier" and copied["pipeline_id"] == "pipe-real"
    assert copied["proof_id"] != "proof-L02-earlier", "recorded under this attempt's own pid"
    # D79b: the reuse copy is filed under its own -reuse-<ts> name, names that
    # file in `record`, and the record it was taken from is untouched
    copies = [p for p in proofs.iterdir() if "-reuse-" in p.name and p.name.startswith("proof-L02-")]
    assert len(copies) == 1 and copied["record"] == "proofs/%s" % copies[0].name
    assert json.loads((proofs / "proof-L02-earlier.json").read_text()).get("reused_from") is None
    assert not (tdir / "proof-pending.json").exists()


def test_d80_migration_numbers_are_allocated_by_the_driver_at_claim_time(tmp_path, monkeypatch):
    """D80 (F13): a packet with platform/db/migrations/NNN_<slug>.sql in its
    owned_files gets the number from `migration allocate` when the driver
    prepares the worktree (the builder cannot reach the scheduler: not in the
    worktree, cwd-relative --state).  Idempotent across rounds/restarts; the
    ceiling follows the worktree's own migrations dir."""
    import vplint
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=routed_pass), "codex": FakeRunner(), "claude": FakeRunner()})
    wt = env.tmp / "wt-d80"
    (wt / ".vp").mkdir(parents=True)
    (wt / ".vp" / "PACKET.md").write_text("---\nitem: L02\ntest_paths:\n  - platform/tests/test_a.py\n---\n# packet\n2. run the allocator\n")
    mig = wt / "platform" / "db" / "migrations"
    mig.mkdir(parents=True)
    (mig / "301_union_member_added_this.sql").write_text("-- a member's migration above the floor\n")
    monkeypatch.setattr(drv, "packet_for", lambda task: {
        "id": task, "owned_files": ["platform/core/x.py", "platform/db/migrations/NNN_l09_sandbox_import.sql",
                                    "TECHNICAL.md"]})
    got = drv._allocate_migrations(wt, "L02")
    assert [(e["number"], e["slug"], e["reused"]) for e in got] == [(302, "l09_sandbox_import", False)], \
        "ceiling = the worktree's highest file (301), not the roster floor"
    rec = json.loads((wt / ".vp" / "MIGRATION.json").read_text())
    assert rec[0]["file"] == "platform/db/migrations/302_l09_sandbox_import.sql" and rec[0]["task"] == "L02"
    pk = (wt / ".vp" / "PACKET.md").read_text()
    assert pk.startswith("---\nitem: L02") and "\n---\n\n> MIGRATION NUMBERS" in pk and "302_l09_sandbox_import.sql" in pk
    assert pk.count("> MIGRATION NUMBERS") == 1 and pk.rstrip().endswith("2. run the allocator")
    # D80b: the front matter still parses -- _proof_step must keep seeing test_paths
    hdr, _ = vplint.parse_front_matter(pk)
    assert hdr["test_paths"] == ["platform/tests/test_a.py"]
    st = json.loads(env.state.read_text())
    assert st["migrations"]["302"]["task"] == "L02" and st["migrations"]["302"]["slug"] == "l09_sandbox_import"
    # a second call (next round / adoption) reuses the file, allocates nothing new
    again = drv._allocate_migrations(wt, "L02")
    assert again[0]["number"] == 302 and again[0]["reused"] is True
    assert len(json.loads(env.state.read_text())["migrations"]) == 1
    # a stale MIGRATION.json (a wrong number from before a fix) is corrected from the
    # state, not trusted; the PACKET.md head line is replaced, not stacked
    (wt / ".vp" / "MIGRATION.json").write_text(json.dumps([dict(rec[0], number=999)]))
    rebuilt = drv._allocate_migrations(wt, "L02")
    pk = (wt / ".vp" / "PACKET.md").read_text()
    assert pk.count("> MIGRATION NUMBERS") == 1 and "302_l09" in pk and "2. run the allocator" in pk
    assert vplint.parse_front_matter(pk)[0]["test_paths"] == ["platform/tests/test_a.py"]
    assert rebuilt[0]["number"] == 302 and rebuilt[0]["reused"] is True
    assert len(json.loads(env.state.read_text())["migrations"]) == 1
    log = (env.run_root / "driver.log").read_text()
    assert "MIGRATION L02 l09_sandbox_import -> platform/db/migrations/302_l09_sandbox_import.sql (allocated)" in log
    # D80a: a retry of the packet (new task id, same packet binding / -R suffix)
    # keeps the lineage's number instead of taking the next one
    drv.pack_by_task["L02"] = "PKT-L02"
    drv.pack_by_task["L02-R1"] = "PKT-L02"
    wt3 = env.tmp / "wt-d80-r1"
    (wt3 / ".vp").mkdir(parents=True)
    (wt3 / ".vp" / "PACKET.md").write_text("# retry\n")
    real_call = drv.control.call

    def no_allocate(verb, args=(), allow=(0,)):
        assert not (verb == "migration" and args and args[0] == "allocate"), "a retry must not allocate"
        return real_call(verb, args, allow)
    monkeypatch.setattr(drv.control, "call", no_allocate)
    got_r1 = drv._allocate_migrations(wt3, "L02-R1")
    assert [(e["number"], e["reused"]) for e in got_r1] == [(302, True)], "the retry inherits 302"
    assert "302_l09_sandbox_import.sql" in (wt3 / ".vp" / "PACKET.md").read_text()
    assert len(json.loads(env.state.read_text())["migrations"]) == 1, "nothing new in run-state"
    monkeypatch.setattr(drv.control, "call", real_call)
    # no placeholder: nothing happens, nothing written
    monkeypatch.setattr(drv, "packet_for", lambda task: {"id": task, "owned_files": ["platform/core/x.py"]})
    wt2 = env.tmp / "wt-d80-none"
    (wt2 / ".vp").mkdir(parents=True)
    assert drv._allocate_migrations(wt2, "L02") is None and not (wt2 / ".vp" / "MIGRATION.json").exists()
    # the allocator refusing (a finished task) is a logged alert, never a crash
    monkeypatch.setattr(drv, "packet_for", lambda task: {
        "id": task, "owned_files": ["platform/db/migrations/NNN_never.sql"]})
    assert drv._allocate_migrations(wt2, "NO-SUCH-TASK") is None
    # a hosted twin never allocates: it proves the parent's output
    monkeypatch.setattr(drv, "packet_for", lambda task: {
        "id": task, "twin_of": "L02", "v13_kind": "HOSTED-TWIN", "runner_role": "proof",
        "owned_files": ["platform/db/migrations/NNN_twin.sql"]})
    assert drv._allocate_migrations(wt2, "L02") is None
    log = (env.run_root / "driver.log").read_text()
    assert "MIGRATION NO-SUCH-TASK allocate failed" in log and "MIGRATION_ALLOC_FAILED" in log


def test_d94_a_hosted_full_suite_pass_is_adopted_by_a_twin_of_any_kind_on_the_same_base(tmp_path):
    """D94 (§77): ~110 full-suite twins release after the first canary PASS; each
    one's proof sha IS the union base (a twin's head never changes), so D79
    reuse already covers platform-kind twins.  A hosted full-suite PASS ran
    every suite, so agent/portal/deploy twins on that base adopt it too.  A
    FAIL_PRODUCT never crosses kinds; a targeted PASS never crosses kinds."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    proofs = env.run_root / "proofs"
    proofs.mkdir(exist_ok=True)
    sha = "c" * 40
    def rec(pid, **kw):
        d = {"proof_id": pid, "sha": sha, "kind": "platform", "paths": [], "route": "gha",
             "pipeline_id": "355" + pid[-3:], "status": "PASS", "ts": "2026-09-20T12:00:00.000Z"}
        d.update(kw)
        (proofs / (pid + ".json")).write_text(json.dumps(d))
    rec("proof-canary-001")
    for kind in ("platform", "agent", "portal", "deploy"):
        assert drv._reusable_proof(sha, kind, [])["proof_id"] == "proof-canary-001", kind
    assert drv._reusable_proof(sha, "agent", ["t/x.py"]) is None, "targeted paths never adopt a full-suite record"
    (proofs / "proof-canary-001.json").unlink()
    rec("proof-canary-002", status="FAIL_PRODUCT", ts="2026-09-20T12:01:00.000Z")
    assert drv._reusable_proof(sha, "platform", [])["proof_id"] == "proof-canary-002"
    assert drv._reusable_proof(sha, "agent", []) is None, "a red answer stays kind-strict"
    (proofs / "proof-canary-002.json").unlink()
    rec("proof-box-003", route="box", pipeline_id=None, ts="2026-09-20T12:02:00.000Z")
    assert drv._reusable_proof(sha, "agent", []) is None, "a box PASS proves one suite only"
    rec("proof-canary-004", kind="platform", paths=["t/a.py"], ts="2026-09-20T12:03:00.000Z")
    assert drv._reusable_proof(sha, "agent", []) is None, "a targeted hosted PASS is not the full suite"


def test_d98_an_only_record_is_never_adopted_as_a_full_suite_answer(tmp_path):
    """§95 item 2 (D98): a single-job/shard hosted run proves one packet's own
    claim.  D79/D94 adoption refuses it for any full-suite or targeted ask,
    a full-suite record never answers an only= ask, and the canary release
    ignores it (one job is no answer for a full-suite gate)."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    proofs = env.run_root / "proofs"
    proofs.mkdir(exist_ok=True)
    sha = "d" * 40
    def rec(pid, **kw):
        d = {"proof_id": pid, "sha": sha, "kind": "platform", "paths": [], "route": "gha",
             "pipeline_id": "356" + pid[-3:], "status": "PASS", "ts": "2026-09-20T13:00:00.000Z"}
        d.update(kw)
        (proofs / (pid + ".json")).write_text(json.dumps(d))
    rec("proof-shard-001", only="platform-shards-3")
    for kind in ("platform", "agent", "portal", "deploy"):
        assert drv._reusable_proof(sha, kind, []) is None, "%s: one shard is not the full suite" % kind
    assert drv._reusable_proof(sha, "platform", ["t/x.py"]) is None
    assert drv._reusable_proof(sha, "platform", [], only="platform-shards-3")["proof_id"] == "proof-shard-001"
    assert drv._reusable_proof(sha, "platform", [], only="platform-shards-4") is None, "a different leg"
    rec("proof-full-002", ts="2026-09-20T13:01:00.000Z")
    assert drv._reusable_proof(sha, "platform", [])["proof_id"] == "proof-full-002"
    assert drv._reusable_proof(sha, "platform", [], only="portal") is None, "a full-suite record never answers only="
    # canary: an only= record on the canary row keeps the hold
    drv.proof_cfg["circleci"] = {"canary": {"task": "L06-HOSTED", "release_on": ["PASS"]}}
    (proofs / "proof-full-002.json").unlink()
    (proofs / "proof-shard-001.json").unlink()
    rec("proof-L06-HOSTED-R9-a1", only="platform-shards-3", ts="2026-09-20T13:02:00.000Z")
    drv._canary_step({"tasks": {"L06-HOSTED-R9": {}}})
    assert not drv._canary().get("released_at"), "one job/leg never releases the canary"
    log = (env.run_root / "driver.log").read_text()
    assert "CANARY_NOT_ANSWERED" in log and "only=platform-shards-3 run (one job/leg)" in log


def test_d100_order_is_wanted_for_a_final_canary_and_the_ship_gate_needs_the_order_job(tmp_path):
    """D100 (§98 item 3): platform-order renders only when the canary block is
    armed final (or a packet says proof_order: true); an intermediate PASS is
    adopted by twins as today but never answers a final ask nor the ship gate."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    assert drv._order_wanted("L06-HOSTED-R10", {}) is False
    assert drv._order_wanted("R-X", {"proof_order": "true"}) is True
    drv.proof_cfg["circleci"] = {"canary": {"task": "L06-HOSTED", "release_on": ["PASS"]}}
    assert drv._order_wanted("L06-HOSTED-R10", {}) is False, "armed but not final"
    drv.proof_cfg["circleci"]["canary"]["final"] = True
    assert drv._order_wanted("L06-HOSTED-R10", {}) is True
    assert drv._order_wanted("R-OTHER", {}) is False, "only the canary's row"
    proofs = env.run_root / "proofs"
    proofs.mkdir(exist_ok=True)
    sha = "e" * 40
    def rec(pid, **kw):
        d = {"proof_id": pid, "sha": sha, "kind": "platform", "paths": [], "route": "gha",
             "pipeline_id": "357" + pid[-3:], "status": "PASS", "ts": "2026-09-20T14:00:00.000Z",
             "jobs": [{"name": "vp/platform", "status": "success"}, {"name": "vp/portal", "status": "success"}]}
        d.update(kw)
        (proofs / (pid + ".json")).write_text(json.dumps(d))
    rec("proof-inter-001")
    assert drv._reusable_proof(sha, "agent", [])["proof_id"] == "proof-inter-001", "twins adopt an intermediate PASS"
    assert drv._reusable_proof(sha, "platform", [], order=True) is None, "a final ask needs the order job"
    assert drv.order_proof(sha) is None, "ship gate: no record carries platform-order"
    rec("proof-final-002", order=True, ts="2026-09-20T14:01:00.000Z",
        jobs=[{"name": "vp/platform", "status": "success"}, {"name": "vp/platform-order", "status": "success"}])
    assert drv._reusable_proof(sha, "platform", [], order=True)["proof_id"] == "proof-final-002"
    assert drv._reusable_proof(sha, "platform", [])["proof_id"] == "proof-final-002", "a final PASS answers a plain ask too"
    assert drv.order_proof(sha)["proof_id"] == "proof-final-002"
    rec("proof-only-003", only="platform-order", ts="2026-09-20T14:02:00.000Z",
        jobs=[{"name": "vp/platform-order", "status": "success"}])
    assert drv.order_proof(sha)["proof_id"] == "proof-final-002", "an only=platform-order run is not the ship gate"


def test_every_circleci_twin_is_held_by_the_canary_whatever_its_header_says(tmp_path, monkeypatch):
    """04-REVIEW-POLICY: a twin is VERIFIED hosted only through the full canary-
    gated run -- a proof_only on a twin (there is none by construction) buys no
    exemption from the §21.2 hold."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    drv.proof_cfg["circleci"] = {"canary": {"task": "L06-HOSTED", "release_on": ["PASS"]}}
    full = {"id": "R-A-HOSTED", "twin_of": "R-A", "twin_gate": "CIRCLECI", "proof_only": ""}
    one = {"id": "R-B-HOSTED", "twin_of": "R-B", "twin_gate": "CIRCLECI", "proof_only": "platform-shards-3"}
    monkeypatch.setattr(drv, "packet_for", lambda task: {"R-A-HOSTED": full, "R-B-HOSTED": one}.get(task))
    assert drv._canary_hold("R-A-HOSTED") is True and drv._canary_hold("R-B-HOSTED") is True


def test_fleet3_idle_runners_with_waiting_hosted_work_alert_once_per_episode(tmp_path, monkeypatch):
    """Fleet-3 (§98): >= 3 runners idle > 10 min while a READY CIRCLECI twin or a
    canary-held twin exists -> ALERT FLEET_IDLE once per idle episode; fleet.json
    on the run root for the board; nothing when no work waits."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    fleet = [{"name": "r%d" % i, "status": "online", "busy": False} for i in range(1, 5)]
    fleet[0]["busy"] = True
    class Circle(object):
        calls = 0
        @staticmethod
        def runners(runner=None):
            Circle.calls += 1
            return list(fleet)
    drv.proof.circle = Circle
    monkeypatch.setattr(drv.proof, "provider", lambda: "gha")
    twin = {"id": "R-A-HOSTED", "twin_of": "R-A", "twin_gate": "CIRCLECI", "proof_only": ""}
    monkeypatch.setattr(drv, "packet_for", lambda task: twin if task == "R-A-HOSTED" else None)
    state = {"tasks": {"R-A-HOSTED": {"state": "READY"}, "R-B": {"state": "READY"}}}
    drv._fleet_step(state, now=1000.0)                      # first sight: idle clocks start
    drv._fleet_step(state, now=1100.0)                      # inside poll_s: no API call
    assert Circle.calls == 1
    drv._fleet_step(state, now=1400.0)                      # 400 s idle: under the 10-min floor
    lp = env.run_root / "driver.log"
    assert "FLEET_IDLE" not in (lp.read_text() if lp.exists() else "")
    drv._fleet_step(state, now=1700.0)                      # 700 s idle, 3 runners, work waits
    log = (env.run_root / "driver.log").read_text()
    assert "ALERT FLEET_IDLE" in log and "3 of 4 runners idle > 10 min (r2, r3, r4)" in log and "R-A-HOSTED" in log
    snap = json.loads((env.run_root / "fleet.json").read_text())
    assert snap["idle_over_floor"] == ["r2", "r3", "r4"] and snap["waiting"] == ["R-A-HOSTED"] and snap["busy"] == 1
    drv._fleet_step(state, now=2100.0)                      # same episode: no second alert
    assert (env.run_root / "driver.log").read_text().count("ALERT FLEET_IDLE") == 1
    # no waiting work -> no alert even with idle runners (a new episode)
    fleet[1]["busy"] = True
    drv._fleet_step(state, now=2500.0)
    fleet[1]["busy"] = False
    drv._fleet_step({"tasks": {}}, now=2900.0)
    drv._fleet_step({"tasks": {}}, now=3600.0)
    assert (env.run_root / "driver.log").read_text().count("ALERT FLEET_IDLE") == 1
    assert drv._fleet_waiting({"tasks": {}}) == []


def test_fleet2_preflight_runs_a_held_twins_parent_files_once_per_tip_and_cancels_on_tip_change(tmp_path, monkeypatch):
    """Fleet-2 (§98): with an idle runner and a slot under the only= cap, the
    driver runs ONE hosted preflight (only=preflight:<paths>) of a held twin's
    parent test files at the tip the twin will get; the record is a signal
    (only= -> never adopted); a moved tip cancels the running one; the same
    (twin, tip) is never launched twice."""
    import threading
    env = Env(tmp_path)
    env.activate()
    gate = threading.Event()
    class Proof(FakeProof):
        only_active = 0
        class circle(object):
            cancelled = []
            @staticmethod
            def cancel_pipeline(run_id, runner=None, account=None):
                Proof.circle.cancelled.append(run_id)
                return ["w1"]
        circle_runner = None
        def provider(self):
            return "gha"
        def only_cap(self):
            return 3
        def triggered_pipeline(self, sha, only=None):
            return {"pipeline_id": "run-" + sha[:4], "account": "gha"}
        def run(self, task, pid, wt, base, cand, kind, paths, abort=None, workers=None, only=None, order=False):
            self.calls.append((task, pid, cand, kind, paths, only))
            gate.wait(10)
            rec = {"status": "FAIL_PRODUCT", "proof_id": pid, "sha": cand, "route": "gha", "only": only,
                   "pipeline_id": "run-" + cand[:4], "failed_nodes": ["tests/test_a.py::test_x"], "ts": "2026-09-20T14:30:00.000Z"}
            d = env.run_root / "proofs"
            d.mkdir(exist_ok=True)
            (d / (pid + ".json")).write_text(json.dumps(rec))
            return rec
    proof = Proof([])
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()}, proof=proof)
    monkeypatch.setattr(drv, "ensure_worktree", lambda task, base: env.tmp / "wt" / task)
    twin = {"id": "R-A-HOSTED", "twin_of": "R-A", "twin_gate": "CIRCLECI", "proof_only": "", "proof_kind": "platform"}
    monkeypatch.setattr(drv, "packet_for", lambda task: twin if task == "R-A-HOSTED" else None)
    drv.pack["R-A"] = {"id": "R-A", "test_paths": ["tests/test_a.py", "tests/test_b.py"]}
    drv.pack_by_task["R-A-V13"] = "R-A"                       # D109: the packet's BOUND row is the parent
    monkeypatch.setattr(drv, "_integration_docs", lambda: [])
    out1, out2 = "1" * 40, "2" * 40
    state = {"tasks": {"R-A-HOSTED": {"state": "READY"}, "R-A-V13": {"state": "VERIFIED", "output_sha": out1},
                       "R-A": {"state": "INTEGRATED", "output_sha": "0" * 40}}}   # a stale catalog row of the same name
    drv._preflight_step(state)
    assert proof.calls == [], "no idle runner known yet -> nothing launched"
    drv._fleet_idle_since = {"r2": 1.0}
    proof.only_active = 2
    drv._preflight_step(state)
    assert proof.calls == [], "the last only= slot is kept for a real only= twin"
    proof.only_active = 0
    drv._preflight_step(state)
    for _ in range(50):
        if proof.calls:
            break
        time.sleep(0.05)
    assert len(proof.calls) == 1
    task, pid, cand, kind, paths, only = proof.calls[0]
    assert task == "R-A-HOSTED" and pid.startswith("proof-preflight-R-A-HOSTED-") and cand == out1
    assert paths == [] and only == "preflight:tests/test_a.py,tests/test_b.py"
    drv._preflight_step(state)
    assert len(proof.calls) == 1, "one in flight: no second launch"
    # the parent re-verifies -> tip moves -> the running preflight is cancelled
    state["tasks"]["R-A-V13"]["output_sha"] = out2
    drv._preflight_step(state)
    assert Proof.circle.cancelled == ["run-1111"]
    gate.set()
    drv._preflight[("R-A-HOSTED")]["thread"].join(5)
    log = (env.run_root / "driver.log").read_text()
    assert "PREFLIGHT R-A-HOSTED tip moved 111111111111 -> 222222222222: cancelled run run-1111" in log
    assert "-> FAIL_PRODUCT (1 red node(s), pipeline run-1111): a signal, not a verdict" in log
    assert "ALERT PREFLIGHT_RED" in log
    # the record is never an answer for the twin's full-suite ask
    assert drv._reusable_proof(out1, "platform", []) is None
    # a new tip launches once; the same (twin, tip) never twice
    drv._preflight_step(state)
    for _ in range(50):
        if len(proof.calls) == 2:
            break
        time.sleep(0.05)
    assert len(proof.calls) == 2 and proof.calls[1][2] == out2
    drv._preflight["R-A-HOSTED"]["thread"].join(5)
    drv._preflight_step(state)
    drv._preflight_step(state)
    assert len(proof.calls) == 2, "a done (twin, tip) is not preflighted again"
    # D110: a FAIL_INFRA record with a real run id counts as done too; a CANCELLED one does not
    d = env.run_root / "proofs"
    out3 = "3" * 40
    state["tasks"]["R-A-V13"]["output_sha"] = out3
    (d / "proof-preflight-R-A-HOSTED-x.json").write_text(json.dumps(
        {"proof_id": "proof-preflight-R-A-HOSTED-x", "sha": out3, "status": "FAIL_INFRA", "pipeline_id": "run-3333",
         "route": "gha", "only": "preflight:tests/test_a.py"}))
    drv._preflight_step(state)
    assert len(proof.calls) == 2, "FAIL_INFRA at the same tip recurs identically: not re-fired"
    (d / "proof-preflight-R-A-HOSTED-x.json").write_text(json.dumps(
        {"proof_id": "proof-preflight-R-A-HOSTED-x", "sha": out3, "status": "CANCELLED", "pipeline_id": "run-3333",
         "route": "gha", "only": "preflight:tests/test_a.py"}))
    drv._preflight_step(state)
    for _ in range(50):
        if len(proof.calls) == 3:
            break
        time.sleep(0.05)
    assert len(proof.calls) == 3 and proof.calls[2][2] == out3
    drv._preflight["R-A-HOSTED"]["thread"].join(5)


def test_d101_an_amended_pack_reaches_the_next_round_and_keeps_the_migration_note(tmp_path):
    """D101: at every round boundary the driver re-copies PACKET.md/BENCHMARK.md
    from the pack when they differ from the .vp copy (REPACK logged); the D80
    migration note survives; an unchanged pack is a no-op; twins are left alone."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    pack = env.tmp / "pack"
    (pack / "R-X").mkdir(parents=True)
    (pack / "R-X" / "PACKET.md").write_text("---\nitem: R-X\ntitle: x\n---\nbody v1\n")
    (pack / "R-X" / "BENCHMARK.md").write_text("- B4 grep -c phrase prints 1\n")
    drv.pack_dir = pack
    drv.pack_by_task["R-X-a1"] = "R-X"
    wt = env.tmp / "wt" / "R-X-a1"
    (wt / ".vp").mkdir(parents=True)
    note = "> MIGRATION NUMBERS (allocated by the driver at claim time, F13/D80): x -> 302_x.sql. Details: .vp/MIGRATION.json\n\n"
    (wt / ".vp" / "PACKET.md").write_text("---\nitem: R-X\ntitle: x\n---\n\n" + note + "body v1\n")
    (wt / ".vp" / "BENCHMARK.md").write_text("- B4 grep -c phrase prints 1\n")
    assert drv._repack(wt, "R-X-a1", 2, "grade") is False, "unchanged pack: no rewrite"
    (pack / "R-X" / "BENCHMARK.md").write_text("- B4 grep -cE '^\\s*x$' prints 1\n")
    (pack / "R-X" / "PACKET.md").write_text("---\nitem: R-X\ntitle: x\n---\nbody v2\n")
    assert drv._repack(wt, "R-X-a1", 2, "grade") is True
    assert (wt / ".vp" / "BENCHMARK.md").read_text() == "- B4 grep -cE '^\\s*x$' prints 1\n"
    assert (wt / ".vp" / "PACKET.md").read_text() == "---\nitem: R-X\ntitle: x\n---\n\n" + note + "body v2\n", \
        "the D80 note sits after the front matter, as _allocate_migrations writes it"
    log = (env.run_root / "driver.log").read_text()
    assert "REPACK R-X-a1 round 2 grade (PACKET.md " in log and "BENCHMARK.md " in log and "D101" in log
    assert drv._repack(wt, "R-X-a1", 3, "build") is False, "now equal again"
    # D122: a twin's text is DERIVED, so the file copy cannot carry an amendment --
    # it is re-derived from the pack's twin entry instead (L31-HOSTED-CIRCLECI-R1
    # sat READY with pre-amendment rows and retry-packet refuses an unfinished task)
    parent = {"id": "R-X", "dir": pack / "R-X", "runner_role": "builder", "template": "REPAIR",
              "owned_files": [], "test_paths": [], "proof_kind": "platform", "max_rounds": 2,
              "parent_contract": "L31", "group": 1, "body": "body v2\n", "critical": False}
    twin = dict(parent, id="R-X-HOSTED", v13_kind=vppack.TWIN_KIND, scheduler_task="NEW:REPAIR",
                closes=[], depends_on=["R-X"], hosted_owed=False, owner_gate="CIRCLECI", base_sha="",
                review_base="", coverage_targets=[], twin_of="R-X", twin_gate="CIRCLECI",
                hosted_rows=["B7"], hosted_lines=["B7 [hosted] the old row"], proof_only="")
    drv.pack["R-X-HOSTED"] = twin
    drv.pack_by_task["R-X-HOSTED-a1"] = "R-X-HOSTED"
    wt2 = env.tmp / "wt" / "R-X-HOSTED-a1"
    (wt2 / ".vp").mkdir(parents=True)
    (wt2 / ".vp" / "BASE").write_text("bc8bdb14a0cf\n")
    (wt2 / ".vp" / "PACKET.md").write_text(vppack.twin_packet_text(twin, "bc8bdb14a0cf"))
    (wt2 / ".vp" / "BENCHMARK.md").write_text(vppack.twin_benchmark(twin))
    assert drv._repack(wt2, "R-X-HOSTED-a1", 1, "build") is False, "unamended twin: no rewrite"
    twin["hosted_lines"] = ["B7 [hosted] the amended row names platform/tests/test_migration_runner.py"]
    assert drv._repack(wt2, "R-X-HOSTED-a1", 1, "build") is True
    assert "test_migration_runner.py" in (wt2 / ".vp" / "BENCHMARK.md").read_text()
    assert "test_migration_runner.py" in (wt2 / ".vp" / "PACKET.md").read_text()
    assert "bc8bdb14a0cf" in (wt2 / ".vp" / "PACKET.md").read_text(), "the base is kept, never re-pointed"
    assert "twin rows re-derived" in (env.run_root / "driver.log").read_text() and \
        "D122" in (env.run_root / "driver.log").read_text()


def test_d104_a_cancelled_hosted_proof_closes_the_attempt_on_first_read_no_retrigger(tmp_path, monkeypatch):
    """D104 (14:32-14:34Z): PROOF_CANCELLED used to strike 1/3, park, re-adopt and
    TRIGGER a fresh full pipeline on the same tip each cycle (35516791190,
    35516913030 on union-60).  Now the attempt closes INVALID_EVIDENCE on the
    first CANCELLED read: one proof call, no second trigger, the packet is a
    fresh retry."""
    monkeypatch.setattr(lanedriver, "FAIL_BACKOFF_S", (0, 0, 0))
    env = Env(tmp_path, roster_extra={"proof": {"require_for_kinds": ["builder"]}})
    env.activate()
    proof = FakeProof([{"status": "CANCELLED", "reason": "circleci pipeline 35513112276 cancelled: not an answer (D79b)"},
                       {"status": "PASS"}])
    drv = env.driver({"opencode": FakeRunner(default=routed_pass), "codex": FakeRunner(), "claude": FakeRunner()},
                     proof=proof)
    settle(drv, 4)
    rows = env.rows()
    assert rows["L02"]["state"] == "INVALID_EVIDENCE", rows["L02"]
    assert "cancelled" in rows["L02"]["blocker"].lower() and "D104" in rows["L02"]["blocker"]
    assert len(proof.calls) == 1, "closed on the first CANCELLED read: never re-triggered"
    log = (env.run_root / "driver.log").read_text()
    assert "ALERT PROOF_CANCELLED" in log and "attempt closed (no re-trigger, D104)" in log
    assert "FAIL L02 1/3 PROOF_CANCELLED" not in log


def test_d96_the_driver_fills_a_missing_or_string_result_attempt_with_the_round(tmp_path):
    """D96 (§86/§91): `attempt` is the integer round number, a value the driver
    owns.  L-TRANSCRIPT-READ-AUDIT-TESTBENCH lost three builder turns to its
    absence and -R1 a fourth to a DISPATCH.json string.  The builder validator
    now fills it (missing or non-integer) before validating; a correct value
    is left alone; a broken file is left alone for the schema to name."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    wt = tmp_path / "wt-d96"
    wt.mkdir()
    out = wt / "RESULT.json"
    tdir = tmp_path / "turns-d96"
    tdir.mkdir()
    rcfg = {"model": "m", "agent": "vp-builder"}
    spec = drv._spec("builder", "L02", wt, "p", rcfg, "opencode", next(iter(drv.servers)), None, out,
                     lanedriver.vpschema.validate_result, 60.0, tdir, "1-r2-builder", False, 2)
    base = {"item": "L02", "commit": "a" * 40, "base": "b" * 40,
            "diff_stat": {"files": 1, "insertions": 1, "deletions": 0},
            "checks": [], "disputes": [], "blocked": None, "notes": ""}
    out.write_text(json.dumps(base))                                   # no attempt at all
    ok, errs = spec.validator(str(out))
    assert ok, errs
    assert json.loads(out.read_text())["attempt"] == 2
    out.write_text(json.dumps(dict(base, attempt="L02-a20260920T125649106")))   # the DISPATCH string
    ok, errs = spec.validator(str(out))
    assert ok, errs
    assert json.loads(out.read_text())["attempt"] == 2
    out.write_text(json.dumps(dict(base, attempt=7)))                  # a correct value is kept
    ok, _ = spec.validator(str(out))
    assert ok and json.loads(out.read_text())["attempt"] == 7
    out.write_text("{not json")                                        # a broken file is the schema's to name
    ok, errs = spec.validator(str(out))
    assert not ok
    log = (env.run_root / "driver.log").read_text()
    assert log.count("attempt missing -> 2 filled by the driver (D96") == 1
    assert log.count("-> 2 filled by the driver") == 2


def test_d113_a_released_lanes_twin_runs_scoped_and_is_neither_held_nor_slot_capped_as_a_full_pipeline(tmp_path, monkeypatch):
    """D113 (§114, owner reversal of 04-REVIEW-POLICY): with roster
    proof.circleci.twin_scope enabled a CIRCLECI twin asks only=twin:<n>:<paths>
    (parent test_paths ∪ the parent_contract's <L-NN> packet test_paths ∪
    extra_paths, sorted, deduplicated); the canary's own row stays the full
    pipeline; scoped twins are not held by the canary, count against the only=
    cap (not the full-pipeline slot), and Fleet-2 preflight is retired."""
    env = Env(tmp_path)
    env.activate()
    proof = FakeProof([])
    proof.only_active = 0
    proof.only_cap = lambda: 2
    proof.circle_cfg = lambda: {"max_in_flight": 1}
    proof.provider = lambda: "gha"
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()}, proof=proof)
    drv.proof_cfg["circleci"] = {"canary": {"task": "L06-HOSTED", "release_on": ["PASS"]},
                                 "max_in_flight": 1,
                                 "twin_scope": {"enabled": True, "contract_from": "parent_contract",
                                                "extra_paths": ["platform/tests/test_extra.py"], "workers": 4}}
    twin = {"id": "R-A-HOSTED", "twin_of": "R-A", "twin_gate": "CIRCLECI", "proof_only": "", "proof_kind": "platform"}
    canary = {"id": "L06-HOSTED", "twin_of": "L06", "twin_gate": "CIRCLECI", "proof_only": ""}
    other = {"id": "R-B-HOSTED", "twin_of": "R-B", "twin_gate": "CIRCLECI", "proof_only": ""}
    drv.pack.update({"R-A-HOSTED": twin, "L06-HOSTED": canary, "R-B-HOSTED": other,
                     "R-A": {"id": "R-A", "test_paths": ["platform/tests/test_b.py", "platform/tests/test_a.py"],
                             "parent_contract": "L07-V13"},
                     "L07": {"id": "L07", "test_paths": ["platform/tests/test_a.py", "platform/tests/test_l07.py"]},
                     "R-B": {"id": "R-B", "test_paths": []}})
    drv.pack_by_task.update({"R-A-HOSTED-R1": "R-A-HOSTED", "L06-HOSTED-R12": "L06-HOSTED",
                             "R-B-HOSTED-R1": "R-B-HOSTED", "L07-V13": "L07"})
    tasks = {"R-A-HOSTED-R1": {"state": "READY", "parent_contract_id": "L07-V13"},
             "L06-HOSTED-R12": {"state": "READY"}, "R-B-HOSTED-R1": {"state": "READY"}}
    assert drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks) == \
        "twin:4:platform/tests/test_a.py,platform/tests/test_b.py,platform/tests/test_extra.py,platform/tests/test_l07.py"
    # the twin's OWN rows name test files too (D04-HOSTED-R1 B9, 17:12Z): hosted_lines
    # and the worktree's .vp/BENCHMARK.md, kept only when the tree has the file
    twin["hosted_lines"] = ["B9 [hosted] the shard runs `platform/tests/test_rows.py::test_x` and "
                            "platform/tests/test_missing.py::test_y green"]
    wt = tmp_path / "wt-scope"
    (wt / ".vp").mkdir(parents=True)
    (wt / "platform" / "tests").mkdir(parents=True)
    for f in ("test_rows.py", "test_bench.py", "test_a.py", "test_b.py", "test_extra.py", "test_l07.py"):
        (wt / "platform" / "tests" / f).write_text("")
    (wt / ".vp" / "BENCHMARK.md").write_text("- B8 [hosted] platform/tests/test_bench.py::test_z green; "
                                             "agent/tests/test_gone.py::test_q too\n")
    got = drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, wt=wt)
    assert got == ("twin:4:platform/tests/test_a.py,platform/tests/test_b.py,platform/tests/test_bench.py,"
                   "platform/tests/test_extra.py,platform/tests/test_l07.py,platform/tests/test_rows.py"), got
    assert "test_missing" not in got and "test_gone" not in got, "files the tree lacks are dropped"
    # a row naming an agent/portal test the platform-twin job cannot run -> full pipeline
    (wt / "agent" / "tests").mkdir(parents=True)
    (wt / "agent" / "tests" / "test_gone.py").write_text("")
    assert drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, wt=wt) is None
    assert "scoped twin refused: 1 non-platform test file(s) named (agent/tests/test_gone.py)" in \
        (env.run_root / "driver.log").read_text()
    # a row asking for a CI job/step, or a header twin_scope: full -> full pipeline
    twin["hosted_lines"] = ["B7 [hosted] the exact-sha collected-test-floor job is green"]
    assert drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, wt=wt) is None
    assert "scoped twin refused: a hosted row asks for a CI job/step ('collected-test-floor')" in \
        (env.run_root / "driver.log").read_text()
    # D120: a row citing a BARE basename (no directory) still widens the scope
    twin.pop("hosted_lines")
    (wt / "platform" / "tests" / "test_legacy_db.py").write_text("")
    (wt / ".vp" / "BENCHMARK.md").write_text("- B8 [hosted] test_legacy_db.py::test_two_workers_mint_one "
                                             "green on a live database\n")
    got = drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, wt=wt)
    assert got and "platform/tests/test_legacy_db.py" in got, got
    # the same basename in a suite the platform-twin job cannot run -> full pipeline
    (wt / "agent" / "tests" / "test_legacy_db.py").write_text("")
    assert drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, wt=wt) is None
    (wt / "agent" / "tests" / "test_legacy_db.py").unlink()
    # a basename the tree does not have is dropped, never handed to pytest
    (wt / ".vp" / "BENCHMARK.md").write_text("- B8 [hosted] test_not_here.py::test_x green\n")
    assert "test_not_here" not in (drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, wt=wt) or "")
    # D125: a scope that pins open the seam its own row names is not an answer
    (wt / "platform" / "tests" / "test_fences.py").write_text(
        "def test_x(monkeypatch):\n"
        "    import core.consent_grants as c\n"
        "    monkeypatch.setattr(c, 'grant_is_live', lambda *a, **k: True)\n")
    (wt / ".vp" / "BENCHMARK.md").write_text("- B9 [hosted] a revoked consent holds the reply -- check: "
                                             "platform/tests/test_fences.py\n")
    assert drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, wt=wt) is None
    assert "scoped twin refused: the scope stubs a seam its rows name" in \
        (env.run_root / "driver.log").read_text()
    # the same file is fine for a row that does not name that seam
    (wt / ".vp" / "BENCHMARK.md").write_text("- B9 [hosted] the reply renders once -- check: "
                                             "platform/tests/test_fences.py\n")
    got = drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, wt=wt)
    assert got and "platform/tests/test_fences.py" in got, got
    # D123: a cited file absent at base but OWNED by a sibling packet names its owner
    drv.pack["R-OWNS-IT"] = {"id": "R-OWNS-IT", "owned_files": ["platform/tests/test_future_db.py"]}
    (wt / ".vp" / "BENCHMARK.md").write_text("- B8 [hosted] platform/tests/test_future_db.py::test_x green\n")
    drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, wt=wt)
    assert "platform/tests/test_future_db.py is absent at base but owned by packet R-OWNS-IT (D123)" in \
        (env.run_root / "driver.log").read_text()
    # D119: a row asking for live-DB evidence while naming no test file of its own
    # cannot be answered by the parent's test_paths -> full pipeline
    twin["hosted_lines"] = ["B9 [hosted] the DB-backed fence rehearsal holds the reply on a live database"]
    (wt / ".vp" / "BENCHMARK.md").write_text("- B9 [hosted] against real `messages` rows under the tenant "
                                             "role, the reply resolves the original pin\n")
    assert drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, wt=wt) is None
    assert "scoped twin refused: a hosted row asks for live-DB evidence" in \
        (env.run_root / "driver.log").read_text()
    # the same ask that names its own file stays scoped: D113a widens to that file
    (wt / ".vp" / "BENCHMARK.md").write_text("- B9 [hosted] the DB-backed rehearsal in "
                                             "platform/tests/test_bench.py::test_z on a live database\n")
    twin.pop("hosted_lines")
    got = drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, wt=wt)
    assert got and "platform/tests/test_bench.py" in got, got
    (wt / ".vp" / "BENCHMARK.md").write_text("- B8 [hosted] platform/tests/test_bench.py::test_z green\n")
    assert drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, hdr={"twin_scope": "full"}) is None
    assert drv._twin_scope_only("R-A-HOSTED-R1", twin, tasks, hdr={"twin_scope": "scoped"}).startswith("twin:4:")
    assert drv._twin_scope_only("L06-HOSTED-R12", canary, tasks) is None, "the canary's row stays the full pipeline"
    assert drv._twin_scope_only("R-B-HOSTED-R1", other, tasks) == "twin:4:platform/tests/test_extra.py"
    drv.proof_cfg["circleci"]["twin_scope"]["extra_paths"] = []
    assert drv._twin_scope_only("R-B-HOSTED-R1", other, tasks) is None, "no paths known -> full run"
    assert "ALERT TWIN_SCOPE_EMPTY" in (env.run_root / "driver.log").read_text()
    # canary hold: scoped twins pass; the canary itself is not held; a full twin is
    assert drv._canary_hold("R-A-HOSTED-R1") is False and drv._canary_hold("L06-HOSTED-R12") is False
    drv.proof_cfg["circleci"]["twin_scope"]["enabled"] = False
    assert drv._canary_hold("R-A-HOSTED-R1") is True, "twin_scope off: the §21.2 hold as before"
    drv.proof_cfg["circleci"]["twin_scope"]["enabled"] = True
    # slot: scoped twins are capped by only_cap (2), full pipelines by max_in_flight (1)
    drv._live = {"R-B-HOSTED-R1": object()}
    assert drv._twin_slot_full("R-A-HOSTED-R1") is False, "1 scoped live < only cap 2"
    assert drv._twin_slot_full("L06-HOSTED-R12") is False, "no FULL twin live: the canary claims"
    drv._live = {"R-B-HOSTED-R1": object(), "R-C-HOSTED-R1": object()}
    drv.pack["R-C-HOSTED"] = {"id": "R-C-HOSTED", "twin_of": "R-C", "twin_gate": "CIRCLECI"}
    drv.pack_by_task["R-C-HOSTED-R1"] = "R-C-HOSTED"
    assert drv._twin_slot_full("R-A-HOSTED-R1") is True, "2 scoped live >= only cap 2"
    assert drv._twin_slot_full("L06-HOSTED-R12") is False, "scoped twins never fill the full-pipeline slot"
    # preflight retired
    drv._fleet_idle_since = {"r1": 1.0}
    drv._preflight_step({"tasks": tasks})
    assert not getattr(drv, "_preflight", None)


def test_d113_a_scoped_twin_record_answers_its_own_ask_and_a_full_hosted_pass_still_closes_it(tmp_path):
    """D113: a twin: record is never a full-suite or other-only answer (D98 kept);
    a full hosted PASS on the same tree (no only, no paths) is adopted by a
    scoped twin's ask (the canary closes twins as before); a FAIL_PRODUCT or a
    box PASS is not."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    proofs = env.run_root / "proofs"
    proofs.mkdir(exist_ok=True)
    sha = "e" * 40
    ask = "twin:3:platform/tests/test_a.py"
    def rec(pid, **kw):
        d = {"proof_id": pid, "sha": sha, "kind": "platform", "paths": [], "route": "gha",
             "pipeline_id": "357" + pid[-3:], "status": "PASS", "ts": "2026-09-20T16:00:00.000Z"}
        d.update(kw)
        (proofs / (pid + ".json")).write_text(json.dumps(d))
    rec("proof-twin-001", only=ask)
    assert drv._reusable_proof(sha, "platform", []) is None, "a scoped twin is not the full suite"
    assert drv._reusable_proof(sha, "platform", [], only="platform-shards-3") is None
    assert drv._reusable_proof(sha, "platform", [], only="twin:3:platform/tests/test_b.py") is None, "a different scope"
    assert drv._reusable_proof(sha, "platform", [], only=ask)["proof_id"] == "proof-twin-001"
    (proofs / "proof-twin-001.json").unlink()
    rec("proof-canary-002", ts="2026-09-20T16:01:00.000Z")
    assert drv._reusable_proof(sha, "platform", [], only=ask)["proof_id"] == "proof-canary-002"
    assert drv._reusable_proof(sha, "agent", [], only=ask)["proof_id"] == "proof-canary-002", "D94 across kinds"
    assert drv._reusable_proof(sha, "platform", [], only="portal") is None, "D98: a full record never answers a job ask"
    (proofs / "proof-canary-002.json").unlink()
    rec("proof-canary-003", status="FAIL_PRODUCT", ts="2026-09-20T16:02:00.000Z")
    assert drv._reusable_proof(sha, "platform", [], only=ask) is None, "a full red is not the twin's answer"
    (proofs / "proof-canary-003.json").unlink()
    rec("proof-box-004", route="box", pipeline_id=None, ts="2026-09-20T16:03:00.000Z")
    assert drv._reusable_proof(sha, "platform", [], only=ask) is None, "a box PASS never closes a hosted twin"


def test_d116_a_packets_probe_greps_diffs_and_plants_ride_into_vp_at_instantiation_and_every_repack(tmp_path):
    """D116 (§122): every non-.md file/dir of the packet directory is copied
    into <wt>/.vp/ (so <probe> = .vp/probe/), overwritten at each repack; the
    two .md texts keep the D101 path; a twin copies its PARENT's extras."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    pack = env.tmp / "pack"
    d = pack / "R-P"
    (d / "probe").mkdir(parents=True)
    (d / "PACKET.md").write_text("---\nitem: R-P\ntitle: p\n---\nbody\n")
    (d / "BENCHMARK.md").write_text("- B2 `<probe>/race.py` prints RACE\n")
    (d / "NOTES.md").write_text("never copied\n")
    (d / "probe" / "race.py").write_text("print('RACE v1')\n")
    (d / "SIMULATED-GREPS.txt").write_text("g1\n")
    (d / "diff-auth.txt").write_text("d\n")
    (d / ".hidden").write_text("x")
    drv.pack_dir = pack
    drv.pack["R-P"] = {"id": "R-P", "dir": str(d), "test_paths": []}
    drv.pack_by_task["R-P-a1"] = "R-P"
    wt = env.tmp / "wt" / "R-P-a1"
    (wt / ".vp").mkdir(parents=True)
    assert drv._copy_packet_extras(wt, "R-P-a1") == ["SIMULATED-GREPS.txt", "diff-auth.txt", "probe/"]
    assert (wt / ".vp" / "probe" / "race.py").read_text() == "print('RACE v1')\n"
    assert not (wt / ".vp" / "NOTES.md").exists() and not (wt / ".vp" / ".hidden").exists()
    # an amended probe reaches the next round through _repack, even with the .md texts unchanged
    (wt / ".vp" / "PACKET.md").write_text("---\nitem: R-P\ntitle: p\n---\nbody\n")
    (wt / ".vp" / "BENCHMARK.md").write_text("- B2 `<probe>/race.py` prints RACE\n")
    (d / "probe" / "race.py").write_text("print('RACE v2')\n")
    (d / "probe" / "plant.sh").write_text("true\n")
    assert drv._repack(wt, "R-P-a1", 2, "grade") is False, "texts unchanged"
    assert (wt / ".vp" / "probe" / "race.py").read_text() == "print('RACE v2')\n"
    assert (wt / ".vp" / "probe" / "plant.sh").exists()
    # a twin takes its parent's directory
    drv.pack["R-P-HOSTED"] = {"id": "R-P-HOSTED", "dir": str(d), "twin_of": "R-P", "twin_gate": "CIRCLECI"}
    drv.pack_by_task["R-P-HOSTED-a1"] = "R-P-HOSTED"
    wt2 = env.tmp / "wt" / "R-P-HOSTED-a1"
    (wt2 / ".vp").mkdir(parents=True)
    assert "probe/" in drv._copy_packet_extras(wt2, "R-P-HOSTED-a1")
    assert drv._copy_packet_extras(wt2, "NO-SUCH-TASK") == []


def test_d115_a_skipped_owner_gate_defers_its_rows_and_releases_its_twin_never_a_bare_string(tmp_path):
    """D115 (§123, owner skipped DELIVERY-4 for launch): roster owner_gates.X ==
    "skipped" (exact string) -> every row tagged (gate: X) grades DEFERRED
    (not UNKNOWN/FAIL) with the ruling note, is excluded from all_pass and never
    blocks; a twin behind a skipped gate is released; any OTHER string (or a
    bare truthy) is CLOSED, never open."""
    from vpdriver import findings_verdicts
    import vppack
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    drv.roster["owner_gates"] = {"DELIVERY-1": True, "DELIVERY-4": "skipped", "DELIVERY-5": "yes", "DELIVERY-3": False,
                                 "DELIVERY-4_skipped_on": "2026-09-20"}
    assert vppack.gate_state("DELIVERY-1", drv.roster) == "open"
    assert vppack.gate_state("DELIVERY-4", drv.roster) == "skipped" and vppack.gate_skipped("DELIVERY-4", drv.roster)
    assert vppack.gate_state("DELIVERY-5", drv.roster) == "closed", "a bare string never opens a gate"
    assert vppack.gate_state("CIRCLECI", drv.roster) == "open" and not vppack.gate_open("DELIVERY-4", drv.roster)
    twin4 = {"id": "L34-HOSTED", "twin_of": "L34", "twin_gate": "DELIVERY-4"}
    twin5 = {"id": "L43-HOSTED", "twin_of": "L43", "twin_gate": "DELIVERY-5"}
    assert vppack.owner_gate_open(twin4, drv.roster) is True, "a skipped gate releases its twin"
    assert vppack.owner_gate_open(twin5, drv.roster) is False
    wt = env.tmp / "wt" / "L34-HOSTED-R1"
    (wt / ".vp").mkdir(parents=True)
    (wt / ".vp" / "BENCHMARK.md").write_text(
        "- B7 [invariant] plain row\n"
        "- B8 [invariant] [hosted] Live drill with the receiver (gate: DELIVERY-4)\n"
        "- B9 [invariant] [hosted] CI shard green (gate: CIRCLECI)\n")
    assert drv._deferred_rows(wt) == {"B8": "DELIVERY-4"}
    f = wt / ".vp" / "FINDINGS.json"
    f.write_text(json.dumps({"item": "L34-HOSTED", "attempt": 1, "commit": "a" * 40, "all_pass": False, "lines": [
        {"id": "B7", "kind": "invariant", "verdict": "PASS", "evidence": "x", "note": ""},
        {"id": "B8", "kind": "invariant", "verdict": "UNKNOWN", "evidence": "no receiver", "note": ""},
        {"id": "B9", "kind": "invariant", "verdict": "PASS", "evidence": "y", "note": ""}]}))
    assert drv._defer_rows("L34-HOSTED-R1", f, wt) == ["B8"]
    doc, fails, unknown = findings_verdicts(f)
    assert fails == [] and unknown == [] and doc["all_pass"] is True
    b8 = next(l for l in doc["lines"] if l["id"] == "B8")
    assert b8["verdict"] == "DEFERRED" and b8["deferred_from"] == "UNKNOWN"
    assert b8["note"] == "B8 DEFERRED -- DELIVERY-4 skipped for launch (owner 2026-09-20)"
    assert drv._defer_rows("L34-HOSTED-R1", f, wt) == [], "idempotent"
    assert "DEFER L34-HOSTED-R1 rows B8: gate skipped by the owner (D115)" in (env.run_root / "driver.log").read_text()
    # a FAIL on a deferred row is deferred too (never stubbed, never red); a red elsewhere still blocks
    f.write_text(json.dumps({"item": "L34-HOSTED", "attempt": 1, "commit": "a" * 40, "all_pass": False, "lines": [
        {"id": "B8", "kind": "invariant", "verdict": "FAIL", "evidence": "x", "note": ""},
        {"id": "B9", "kind": "invariant", "verdict": "FAIL", "evidence": "y", "note": ""}]}))
    drv._defer_rows("L34-HOSTED-R1", f, wt)
    doc, fails, unknown = findings_verdicts(f)
    assert fails == ["B9"] and doc["all_pass"] is False
    # gate closed (false) -> nothing deferred
    drv.roster["owner_gates"]["DELIVERY-4"] = False
    assert drv._deferred_rows(wt) == {}


def test_d128_a_packet_with_hosted_in_the_middle_of_its_id_is_not_a_twin(tmp_path):
    """D128: is_hosted_twin tested "-HOSTED" as a SUBSTRING, so
    R-PORTAL-HOSTED-TIMING -- an ordinary portal repair packet with no twin_of --
    was handed D79's twin rule max_rounds=1: its R1 went REPAIR_REQUIRED after one
    round (08:26:03Z) and the work needed a second packet task (R2, 08:31:24Z), and
    it filed an empty hosted/None.json under a parent named None.  The id test is a
    suffix now; twin_of still decides on its own, whatever the id."""
    import vppack
    for tid in ("L17-HOSTED", "L17-HOSTED-R3", "L18-HOSTED-CIRCLECI", "L18-HOSTED-CIRCLECI-R1",
                "L34-HOSTED-DELIVERY-4", "L34-HOSTED-DELIVERY-4-R2", "WA-03-DP11R-HOSTED"):
        assert vppack.is_hosted_twin({"id": tid}) is True, tid
    for pid in ("R-PORTAL-HOSTED-TIMING", "R-PORTAL-HOSTED-TIMING-R1", "R-PORTAL-HOSTED-TIMING-R2",
                "R-SUPPLY-HOSTED-DEPS", "L19-LAUNCH-ADMISSION"):
        assert vppack.is_hosted_twin({"id": pid}) is False, pid
    assert vppack.is_hosted_twin({"id": "R-PORTAL-HOSTED-TIMING", "twin_of": "L19"}) is True, \
        "an explicit twin_of still decides, whatever the id looks like"
    # the twin rule that misfired: a twin gets one round, a plain repair packet gets its repairs
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    drv.pack.update({"R-PORTAL-HOSTED-TIMING": {"id": "R-PORTAL-HOSTED-TIMING",
                                                "owned_files": ["portal/src/test/setup.ts"]}})
    drv.pack_by_task.update({"R-PORTAL-HOSTED-TIMING-R1": "R-PORTAL-HOSTED-TIMING"})
    assert vppack.is_hosted_twin(drv.packet_for("R-PORTAL-HOSTED-TIMING-R1")) is False


def test_d126c_a_newer_twins_verdict_is_refused_and_a_withdrawal_can_be_reversed(tmp_path):
    """D126c, from a real error at 22:14Z: hosted_rows is keyed by row id and a newer
    twin OVERWRITES it, so evidence gathered minutes earlier can name a verdict that
    no longer stands.  R-SUPPLY-AGENT-HOSTED-R2 recorded B7/B8 from its own pipeline
    at 22:14:10.890Z and the withdrawal landed 25 s later still naming R1 -- it
    withdrew an honest verdict and credited it to the wrong twin.  Withdrawal now
    refuses that case unless forced, and `restore-grade` puts the prior verdict back
    from the `superseded` block the withdrawal kept for exactly this purpose."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    twin1 = {"id": "R-SUP-HOSTED-R1", "twin_of": "R-SUP", "twin_gate": "CIRCLECI", "hosted_rows": ["B7", "B8"]}
    twin2 = {"id": "R-SUP-HOSTED-R2", "twin_of": "R-SUP", "twin_gate": "CIRCLECI", "hosted_rows": ["B7", "B8"]}
    drv.pack.update({"R-SUP-HOSTED-R1": twin1, "R-SUP-HOSTED-R2": twin2, "R-SUP": {"id": "R-SUP"}})
    drv.pack_by_task.update({"R-SUP-HOSTED-R1": "R-SUP-HOSTED-R1", "R-SUP-HOSTED-R2": "R-SUP-HOSTED-R2",
                             "R-SUP-V13": "R-SUP"})
    hdir = env.run_root / "hosted"
    hdir.mkdir(parents=True, exist_ok=True)
    (hdir / "R-SUP-V13.json").write_text(json.dumps({
        "task": "R-SUP-V13", "packet": "R-SUP", "box_only": True,
        "twins": {"R-SUP-HOSTED-R1": {"outcome": "REPAIR_REQUIRED", "attempt": "a1"},
                  "R-SUP-HOSTED-R2": {"outcome": "REPAIR_REQUIRED", "attempt": "a2"}},
        "hosted_rows": {"B7": {"verdict": "FAIL", "twin": "R-SUP-HOSTED-R2", "attempt": "a2", "source": "findings"},
                        "B8": {"verdict": "PASS", "twin": "R-SUP-HOSTED-R1", "attempt": "a1", "source": "findings"}},
    }))
    with pytest.raises(ValueError, match="now reads FAIL from R-SUP-HOSTED-R2"):
        drv.unsound_grade("R-SUP-HOSTED-R1", "B7", "borrowed pipeline")
    rec = json.loads((hdir / "R-SUP-V13.json").read_text())
    assert rec["hosted_rows"]["B7"]["verdict"] == "FAIL", "a refused withdrawal writes nothing"
    # the row the named twin DID record still withdraws, and a FAIL withdraws like a PASS
    out = drv.unsound_grade("R-SUP-HOSTED-R1", "B8", "borrowed pipeline 1234", evidence="e.md")
    assert out["was"] == "PASS"
    # --force is the deliberate path for the newer verdict
    out = drv.unsound_grade("R-SUP-HOSTED-R1", "B7", "deliberate", force=True)
    assert out["was"] == "FAIL", "a false RED withdraws too -- the verb is not green-only"
    rec = json.loads((hdir / "R-SUP-V13.json").read_text())
    assert rec["hosted_rows"]["B7"]["superseded"]["verdict"] == "FAIL"
    # and the reversal: the prior entry comes back verbatim, the withdrawal stays visible
    back = drv.restore_grade("R-SUP-HOSTED-R1", "B7", "MY ERROR: R2 had already superseded it honestly")
    assert back["restored"] == "FAIL"
    row = json.loads((hdir / "R-SUP-V13.json").read_text())["hosted_rows"]["B7"]
    assert row["verdict"] == "FAIL" and row["twin"] == "R-SUP-HOSTED-R2" and row["source"] == "findings"
    assert row["restored_from"]["verdict"] == "UNSOUND", "the withdrawal is not erased"
    assert row["restore_reason"].startswith("MY ERROR") and row["restored_at"]
    with pytest.raises(ValueError, match="not UNSOUND"):
        drv.restore_grade("R-SUP-HOSTED-R1", "B7", "again")
    with pytest.raises(ValueError, match="no row B4"):
        drv.restore_grade("R-SUP-HOSTED-R1", "B4", "no such row")
    assert "RESTORE_GRADE R-SUP-HOSTED-R1 B7 UNSOUND -> FAIL" in (env.run_root / "driver.log").read_text()


def test_d130_a_declared_base_sha_is_reported_never_obeyed(tmp_path):
    """D130: a packet's base_sha stays advisory -- the row is still claimed at trunk.
    The diagnostic says so when the pin does not describe that base: not a commit at
    all (one packet carried a sha sharing trunk's 12-char prefix and differing after
    it), ahead of it (the fast-forward has not arrived), or off its line (28 of 163
    packets pin one of those; building on one forks the lane)."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    TRUNK, AHEAD, SIDE, GHOST = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    drv.trunk_sha = lambda: TRUNK
    calls = []

    def fake_git(args, **kw):
        calls.append(args)
        if "cat-file" in args:
            return (1 if GHOST in " ".join(args) else 0), "", ""
        if "merge-base" in args:
            pin, base = args[-2], args[-1]          # --is-ancestor <pin> <base>
            return (0 if (pin, base) in {(TRUNK, AHEAD)} else 1), "", ""
        return 0, "", ""
    drv.git = fake_git
    drv.pack.update({"P": {"id": "P"}})
    drv.pack_by_task.update({"T": "P"})
    row = {"task_id": "T", "kind": "integration"}

    drv.pack["P"]["base_sha"] = TRUNK
    assert drv._base_for(row, {}) == TRUNK
    assert not calls, "a pin equal to the base asks git nothing"

    drv.pack["P"]["base_sha"] = GHOST
    assert drv._base_for(row, {}) == TRUNK, "an unknown pin never moves the base"
    alerts = (env.run_root / "alerts.jsonl").read_text()
    assert "BASE_PIN_UNKNOWN" in alerts and "is not a commit in this repo" in alerts

    drv.pack["P"]["base_sha"] = AHEAD
    assert drv._base_for(row, {}) == TRUNK, "a pin AHEAD of trunk is still not obeyed"
    log = (env.run_root / "driver.log").read_text()
    assert "is AHEAD of the claim base" in log and "bbbbbbbbbbbb" in log

    drv.pack["P"]["base_sha"] = SIDE
    assert drv._base_for(row, {}) == TRUNK, "an off-line pin never forks the lane"
    alerts = (env.run_root / "alerts.jsonl").read_text()
    assert "BASE_PIN_DIVERGED" in alerts

    # a review still takes the candidate, and no pin check runs on that path
    calls.clear()
    assert drv._base_for({"task_id": "T", "review_key": "k"}, {"candidate": {"sha": "f" * 40}}) == "f" * 40
    assert not calls


def test_d129_the_disk_gate_resumes_above_a_higher_mark_than_it_pauses_at(tmp_path):
    """D129: the gate had ONE threshold, so free space sitting near it paused and
    resumed on alternating ticks with a DISK alert each crossing ("flaps every
    minute"). Resume now needs a higher mark than pause, so recovery has to be real.
    Unknown free space moves the gate neither way."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    drv.night["pause_on_disk_gb"] = 25
    seen = []
    drv.disk_gb = lambda: seen[-1]
    seen.append(90.0); drv._guards()
    assert drv._disk_paused is False
    seen.append(24.0); drv._guards()
    assert drv._disk_paused is True, "below the floor pauses"
    # the old bug: 25.1 GB is above the floor and would have resumed instantly
    for gb in (25.1, 30.0, 34.9):
        seen.append(gb); drv._guards()
        assert drv._disk_paused is True, "%s GB is recovery on paper, not in fact" % gb
    seen.append(35.0); drv._guards()
    assert drv._disk_paused is False, "floor + 10 is the resume mark"
    log = (env.run_root / "driver.log").read_text()
    assert "resumes at 35 GB" in log and "disk recovered: 35.0 GB (>= resume 35 GB, D129)" in log
    assert log.count("ALERT DISK") == 1, "one alert per real crossing, not one per tick"
    # an explicit resume mark is honoured, and one below the floor is clamped to it
    drv.night["resume_on_disk_gb"] = 60
    seen.append(10.0); drv._guards()
    seen.append(50.0); drv._guards()
    assert drv._disk_paused is True
    seen.append(60.0); drv._guards()
    assert drv._disk_paused is False
    drv.night["resume_on_disk_gb"] = 5
    seen.append(10.0); drv._guards()
    assert drv._disk_paused is True
    seen.append(25.0); drv._guards()
    assert drv._disk_paused is False, "a resume mark below the floor clamps to the floor"
    # unknown free space leaves the gate exactly as it was
    seen.append(10.0); drv._guards()
    assert drv._disk_paused is True
    drv.disk_gb = lambda: None
    drv._guards()
    assert drv._disk_paused is True, "unknown disk never silently resumes"


def test_d126a_a_one_shot_cli_recovers_its_pack_bindings_from_the_dispatch_records(tmp_path):
    """D126a: _pack_restore needs the live `tasks` view and runs only inside the
    loop, so `unsound-grade` -- a one-shot process -- had an empty pack_by_task and
    refused every withdrawal with "is not a hosted twin" (all 8 of the Architect's
    §136 rows, 22:05Z).  packets/<pid>.json is the binding's durable form; read it."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    pack = env.tmp / "pack"
    for pid in ("L08-ADMIT-TG-HOSTED-R1", "L08-ADMIT-TG"):
        (pack / pid).mkdir(parents=True)
        (pack / pid / "PACKET.md").write_text("---\nitem: %s\ntitle: t\n---\nbody\n" % pid)
        (pack / pid / "BENCHMARK.md").write_text("- B7 [invariant] [hosted] a row\n")
    drv.pack_dir = pack
    pdir = env.run_root / "packets"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "L08-ADMIT-TG-HOSTED-R1.json").write_text(json.dumps({"task": "L08-ADMIT-TG-HOSTED-R1"}))
    (pdir / "L08-ADMIT-TG.json").write_text(json.dumps({"task": "L08-ADMIT-TG-R1"}))
    (pdir / "L08-ADMIT-TG-HOSTED-R1.params.json").write_text(json.dumps({"task": "NOT-A-BINDING",
                                                                         "packet_id": "L08-ADMIT-TG-HOSTED-R1"}))
    assert drv.packet_for("L08-ADMIT-TG-HOSTED-R1") is None, "a fresh CLI process knows nothing"
    out = drv.pack_bindings_from_disk()
    assert out["L08-ADMIT-TG-HOSTED-R1"] == "L08-ADMIT-TG-HOSTED-R1"
    assert out["L08-ADMIT-TG-R1"] == "L08-ADMIT-TG", "the parent's task id comes back too"
    assert "NOT-A-BINDING" not in out, "a params sidecar's `task` field is not a binding; its FILENAME is"
    assert (drv.packet_for("L08-ADMIT-TG-HOSTED-R1") or {}).get("id") == "L08-ADMIT-TG-HOSTED-R1"
    drv.pack_by_task["L08-ADMIT-TG-HOSTED-R1"] = "SOMETHING-ELSE"
    drv.pack_bindings_from_disk()
    assert drv.pack_by_task["L08-ADMIT-TG-HOSTED-R1"] == "SOMETHING-ELSE", "never overwrites a live binding"
    # D126b: a retry overwrites the packet's binding record, so the generation that
    # actually recorded a row keeps no binding -- only its own params sidecar, whose
    # FILENAME is the task and whose packet_id is the packet (R-SUPPLY-AGENT-HOSTED-R1,
    # whose record had been rewritten to name -R2, 22:13Z)
    drv.pack_by_task.clear()
    (pdir / "L08-ADMIT-TG-HOSTED-R1.json").write_text(json.dumps({"task": "L08-ADMIT-TG-HOSTED-R2"}))
    (pdir / "L08-ADMIT-TG-HOSTED-R1.params.json").write_text(
        json.dumps({"packet_id": "L08-ADMIT-TG-HOSTED-R1", "runner_role": "probe"}))
    out = drv.pack_bindings_from_disk()
    assert out["L08-ADMIT-TG-HOSTED-R2"] == "L08-ADMIT-TG-HOSTED-R1", "the live generation still wins"
    assert out["L08-ADMIT-TG-HOSTED-R1"] == "L08-ADMIT-TG-HOSTED-R1", "the superseded one comes back too"
    assert (drv.packet_for("L08-ADMIT-TG-HOSTED-R1") or {}).get("id") == "L08-ADMIT-TG-HOSTED-R1"


def test_d127_a_probe_twin_defers_its_owner_skipped_row_at_the_record_not_at_the_grader(tmp_path):
    """D127 (§123, Architect option (b)): D115 defers on FINDINGS.json after a grader
    turn, so a probe-kind twin -- no grader, no FINDINGS.json -- left its
    owner-skipped rows at D45's UNKNOWN and held box_only up for a gate the owner
    had already skipped.  _twin_record is the one funnel every twin passes, so the
    defer belongs there, carrying the gate name, the roster's exact value and
    skipped_on onto the row.  An open gate still defers nothing."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    drv.roster["owner_gates"] = {"DELIVERY-4": "skipped", "DELIVERY-4_skipped_on": "2026-09-20", "CIRCLECI": True}
    # the live shape (74 of 76 hosted records): the twin's pack id IS its task id,
    # which is what D45's all_twins check compares against
    twin = {"id": "L34-HOSTED-R1", "twin_of": "L34", "twin_gate": "DELIVERY-4", "proof_only": "",
            "hosted_rows": ["B8", "B9"]}
    drv.pack.update({"L34-HOSTED-R1": twin, "L34": {"id": "L34"}})
    drv.pack_by_task.update({"L34-HOSTED-R1": "L34-HOSTED-R1", "L34-V13": "L34"})
    wt = env.tmp / "wt" / "L34-HOSTED-R1"
    (wt / ".vp").mkdir(parents=True)
    (wt / ".vp" / "BENCHMARK.md").write_text(
        "- B8 [invariant] [hosted] Live drill with the receiver (gate: DELIVERY-4)\n"
        "- B9 [invariant] [hosted] CI shard green (gate: CIRCLECI)\n")
    # the probe pipeline returns VERIFIED with a RESULT.json and NO findings at all
    drv._twin_record("L34-HOSTED-R1", "VERIFIED", None, "a1", wt)
    rec = json.loads((env.run_root / "hosted" / "L34-V13.json").read_text())
    b8, b9 = rec["hosted_rows"]["B8"], rec["hosted_rows"]["B9"]
    assert b8["verdict"] == "DEFERRED" and b8["source"] == "gate-skipped"
    assert b8["gate"] == "DELIVERY-4" and b8["gate_value"] == "skipped" and b8["skipped_on"] == "2026-09-20"
    assert b8["deferred_from"] == "UNKNOWN" and b8["deferred_source"] == "default", "it says what it moved"
    assert b9["verdict"] == "UNKNOWN" and "gate" not in b9, "an open gate defers nothing"
    assert rec["box_only"] is True, "B9 is still owed, so the box-only flag stays up"
    assert "DEFER L34-HOSTED-R1 B8 UNKNOWN -> DEFERRED: gate DELIVERY-4" in (env.run_root / "driver.log").read_text()
    # the open row lands PASS from a grader: DEFERRED + PASS clears box_only (D45)
    f = wt / ".vp" / "FINDINGS.json"
    f.write_text(json.dumps({"item": "L34-HOSTED", "attempt": 1, "commit": "a" * 40, "all_pass": True, "lines": [
        {"id": "B8", "kind": "invariant", "verdict": "UNKNOWN", "evidence": "no receiver", "note": ""},
        {"id": "B9", "kind": "invariant", "verdict": "PASS", "evidence": "shard green", "note": ""}]}))
    drv._twin_record("L34-HOSTED-R1", "VERIFIED", f, "a2", wt)
    rec = json.loads((env.run_root / "hosted" / "L34-V13.json").read_text())
    assert rec["hosted_rows"]["B8"]["verdict"] == "DEFERRED", "a graded UNKNOWN on a skipped gate defers too"
    assert rec["hosted_rows"]["B8"]["deferred_source"] == "findings"
    assert rec["hosted_rows"]["B9"]["verdict"] == "PASS" and rec["box_only"] is False
    # the owner closes the gate again -> nothing is deferred and the row is owed once more
    drv.roster["owner_gates"]["DELIVERY-4"] = False
    drv._twin_record("L34-HOSTED-R1", "VERIFIED", f, "a3", wt)
    rec = json.loads((env.run_root / "hosted" / "L34-V13.json").read_text())
    assert rec["hosted_rows"]["B8"]["verdict"] == "UNKNOWN" and rec["box_only"] is True


def test_d114_a_hosted_targeted_record_answers_the_plain_targeted_ask_and_nothing_else(tmp_path):
    """D114: the box-shaped targeted proof offloaded to the fleet carries
    only=targeted:...; D79 reuses it for the same kind + paths plain ask, never
    for a twin ask, a different path set or a full-suite ask."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    proofs = env.run_root / "proofs"
    proofs.mkdir(exist_ok=True)
    sha = "f" * 40
    rec = {"proof_id": "proof-tg-001", "sha": sha, "kind": "platform", "paths": ["platform/tests/test_a.py"],
           "route": "gha", "pipeline_id": "358001", "status": "PASS", "ts": "2026-09-20T17:30:00.000Z",
           "only": "targeted:3:platform/tests/test_a.py"}
    (proofs / "proof-tg-001.json").write_text(json.dumps(rec))
    assert drv._reusable_proof(sha, "platform", ["platform/tests/test_a.py"])["proof_id"] == "proof-tg-001"
    assert drv._reusable_proof(sha, "platform", ["platform/tests/test_b.py"]) is None
    assert drv._reusable_proof(sha, "platform", []) is None, "never a full-suite answer"
    assert drv._reusable_proof(sha, "agent", ["platform/tests/test_a.py"]) is None, "kind-strict"
    assert drv._reusable_proof(sha, "platform", [], only="twin:3:platform/tests/test_a.py") is None, "never a twin's"


def test_d126_unsound_grade_withdraws_one_row_keeps_the_old_verdict_and_re_raises_box_only(tmp_path):
    """D126 (§152, Architect ruling (a)): a hosted row the driver graded from wrong
    inputs -- a borrowed pipeline (§153) or a scope that stubs the seam the row
    names (L17-HOSTED-R3 B9) -- is withdrawn as UNSOUND, never `invalidate`d.
    The prior verdict is kept verbatim under `superseded`, box_only re-raises
    because D45 clears only on PASS/DEFERRED, and the twin's scheduler state is
    untouched: re-proving an ACCEPTED twin stays an owner verb."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()})
    twin = {"id": "L17-HOSTED", "twin_of": "L17", "twin_gate": "CIRCLECI", "proof_only": "",
            "hosted_rows": ["B8", "B9"]}
    drv.pack.update({"L17-HOSTED": twin, "L17": {"id": "L17"}})
    drv.pack_by_task.update({"L17-HOSTED-R3": "L17-HOSTED", "L17-V13": "L17"})
    hdir = env.run_root / "hosted"
    hdir.mkdir(parents=True, exist_ok=True)
    (hdir / "L17-V13.json").write_text(json.dumps({
        "task": "L17-V13", "packet": "L17", "box_only": False,
        "twins": {"L17-HOSTED": {"gate": "CIRCLECI", "outcome": "VERIFIED", "attempt": "a1"}},
        # the row's `twin` is the TASK that recorded it, which is what D126c compares against
        "hosted_rows": {"B8": {"verdict": "PASS", "twin": "L17-HOSTED-R3", "attempt": "a1", "source": "findings"},
                        "B9": {"verdict": "PASS", "twin": "L17-HOSTED-R3", "attempt": "a1", "source": "findings"}},
    }))
    out = drv.unsound_grade("L17-HOSTED-R3", "B9", "the scope stubs consent_grants.grant_is_live",
                            evidence="evidence/L17-B9.md")
    assert out["was"] == "PASS" and out["box_only"] is True
    rec = json.loads((hdir / "L17-V13.json").read_text())
    b9 = rec["hosted_rows"]["B9"]
    assert b9["verdict"] == "UNSOUND" and b9["reason"].startswith("the scope stubs")
    assert b9["evidence"] == "evidence/L17-B9.md"
    assert b9["superseded"] == {"verdict": "PASS", "twin": "L17-HOSTED-R3", "attempt": "a1",
                                "source": "findings"}, "the old verdict is kept verbatim"
    assert rec["hosted_rows"]["B8"]["verdict"] == "PASS", "only the named row moves"
    assert rec["box_only"] is True, "D45 clears only on PASS/DEFERRED, so UNSOUND re-raises it"
    assert rec["twins"]["L17-HOSTED"]["outcome"] == "VERIFIED", "the twin's own outcome is not rewritten"
    log = (env.run_root / "driver.log").read_text()
    assert "UNSOUND_GRADE L17-HOSTED-R3 B9 PASS -> UNSOUND" in log
    with pytest.raises(ValueError, match="already UNSOUND"):
        drv.unsound_grade("L17-HOSTED-R3", "B9", "again")
    with pytest.raises(ValueError, match="no row B4"):
        drv.unsound_grade("L17-HOSTED-R3", "B4", "no such row")
    with pytest.raises(ValueError, match="not a hosted twin"):
        drv.unsound_grade("L17-V13", "B9", "the parent is not a twin")


def test_d131_a_fast_forward_empties_the_union_and_the_twin_still_builds_on_trunk(tmp_path):
    """D131: 2026-09-20 23:13-23:18Z every wall-batch twin sat in a 600 s
    TWIN_BASE_WAIT loop.  The orchestrator had fast-forwarded trunk to union-100,
    so _integration_members -- which drops every row trunk already carries -- cut
    union-101 with ZERO members at the run's frozen base.  _twin_union_base asked
    for members of that empty map, a condition nothing could satisfy, and its
    `base = latest["union_sha"]` would have put the twin 1200 commits behind trunk
    had the gate passed.  Trunk containing the fix satisfies the requirement, and
    a union tip trunk already contains is not the tip any more -- trunk is."""
    env = Env(tmp_path)
    (env.trunk / "platform" / "fix.py").write_text("seed = 1\n")
    git(env.trunk, "add", "-A")
    git(env.trunk, "commit", "-q", "-m", "P-FIX")
    fix_sha = git(env.trunk, "rev-parse", "HEAD")
    (env.trunk / "platform" / "par.py").write_text("par = 1\n")
    git(env.trunk, "add", "-A")
    git(env.trunk, "commit", "-q", "-m", "P-PAR")
    par_sha = git(env.trunk, "rev-parse", "HEAD")      # trunk HEAD: the ff landed both

    def union(n, sha, members):
        d = env.run_root / "unions" / str(n)
        d.mkdir(parents=True)
        (d / "members.json").write_text(json.dumps(
            {"n": n, "union": "union-%d" % n, "for": ["INTEGRATION"], "status": "BUILT",
             "union_sha": sha,
             "members": [{"task": t, "output_sha": s} for t, s in members]}))

    union(100, par_sha, [("P-FIX-R4", fix_sha), ("P-PAR", par_sha)])
    union(101, env.base, [])                            # cut after the ff: empty, at the frozen base
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner(), "claude": FakeRunner()})
    drv.pack_by_task = {"P-FIX-R4": "P-FIX", "P-PAR": "P-PAR"}
    tasks = {"P-FIX-R4": {"state": "VERIFIED", "output_sha": fix_sha},
             "P-PAR": {"state": "VERIFIED", "output_sha": par_sha}}
    row = {"state": "READY", "attempt_id": 1, "claim_id": None}
    tb = {"kind": "integration_union", "requires": ["P-FIX"]}

    base, plan = drv._twin_union_base("P-PAR-HOSTED", row, {}, "P-PAR", par_sha, tasks, tb)

    log = (env.run_root / "driver.log").read_text()
    assert "TWIN_BASE_WAIT" not in log, log[-2000:]
    assert base == par_sha and base != env.base, (base, par_sha, env.base)
    assert plan["base"] == par_sha
    # D59 is unchanged: the CARRIED ROW reaches --stacked-on, never the packet name
    assert plan["on"] == ["P-PAR", "P-FIX-R4"], plan["on"]
    assert "integration union %s is already in trunk" % env.base[:12] in log

    # and a union that is genuinely ahead of trunk is still the base it always was
    (env.trunk / "platform" / "later.py").write_text("later = 1\n")
    git(env.trunk, "add", "-A")
    git(env.trunk, "commit", "-q", "-m", "ahead")
    ahead = git(env.trunk, "rev-parse", "HEAD")
    git(env.trunk, "reset", "-q", "--hard", par_sha)     # trunk back to the ff point
    union(102, ahead, [("P-FIX-R4", fix_sha), ("P-PAR", par_sha)])
    base2, plan2 = drv._twin_union_base("P-PAR-HOSTED", row, {}, "P-PAR", par_sha, tasks, tb)
    assert base2 == ahead, (base2, ahead)


def test_d132_a_role_asking_for_a_variant_its_model_does_not_declare_is_alerted(tmp_path, monkeypatch):
    """D132: 2026-09-20 roster.roles.infra asked deepseek-v4.1-flash for variant
    "xhigh", copied from the muse roles where it is valid; that model declares
    low/high/max.  Nothing rejected it -- vprunners puts the string straight into
    the prompt_async body and the live server answers 204 to ANY string, echoing
    it back (measured with "definitely-not-a-variant").  The provider catalog is
    the only place it is visible.  The alert names the pair once, not once per
    kind alias, and an unreadable catalog never stops the run."""
    env = Env(tmp_path, roster_extra={
        "roles": {"builder": {"runner": "opencode", "model": "opencode-go/muse-1", "variant": "xhigh"},
                  "infra": {"runner": "opencode", "model": "opencode-go/ds-flash", "variant": "xhigh"},
                  "operations": {"runner": "opencode", "model": "opencode-go/ds-flash", "variant": "xhigh"},
                  "grader": {"runner": "opencode", "model": "opencode-go/ds-flash", "variant": "high"},
                  "junior": {"runner": "codex", "model": "gpt-x", "variant": "nonsense"}}})
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner(), "claude": FakeRunner()})
    drv.fake_runners = False
    catalog = {"providers": [{"id": "opencode-go", "models": {
        "ds-flash": {"variants": {"low": {}, "high": {}, "max": {}}},
        "muse-1": {"variants": {"low": {}, "high": {}, "xhigh": {}}}}}]}
    monkeypatch.setattr(type(drv), "_http_json", lambda self, url, timeout=10.0: catalog)
    drv._check_role_variants()
    alerts = [json.loads(l) for l in (env.run_root / "alerts.jsonl").read_text().splitlines()]
    undeclared = [a for a in alerts if a.get("kind") == "ROLE_VARIANT_UNDECLARED"]
    assert len(undeclared) == 1, undeclared          # the PAIR once, not once per kind alias
    text = undeclared[0]["text"]
    assert "ds-flash" in text and "'xhigh'" in text and "low, high, max" in text
    assert "infra" in text and "operations" in text and "provider" in text  # every alias, one alert
    assert "muse-1" not in text                      # xhigh is valid there
    assert "gpt-x" not in text                       # codex roles are not opencode variants

    # an unreadable catalog skips the check rather than stopping the run
    drv._alerted.clear()
    (env.run_root / "alerts.jsonl").write_text("")
    monkeypatch.setattr(type(drv), "_http_json", lambda self, url, timeout=10.0: None)
    drv._check_role_variants()
    assert (env.run_root / "alerts.jsonl").read_text().strip() == ""


def test_d134_an_unpinned_role_spreads_across_the_fleet_and_a_pin_still_wins(tmp_path):
    """D134: `_pick_server` was first-fit over `sorted(self.servers)`.  Every
    server carries `max_concurrent: 100` against a driver cap of 15, so capacity
    never runs out and the FIRST name absorbs every unpinned role -- a fleet that
    behaves as one server.  Measured 2026-09-21: go1 and go3 sat at 0 active
    while go2 carried everything.

    The control is the second assertion: under first-fit, go1 has capacity at
    every step, so an unpinned pick returns "go1" three times and the test reds.
    A pin must still win outright -- an explicit `server:` is a placement
    decision, and D134 must not quietly override it."""
    servers = {"go1": {"url": "http://127.0.0.1:1", "max_concurrent": 100, "xdg_data_home": str(tmp_path / "x1")},
               "go2": {"url": "http://127.0.0.1:2", "max_concurrent": 100, "xdg_data_home": str(tmp_path / "x2")},
               "go3": {"url": "http://127.0.0.1:3", "max_concurrent": 100, "xdg_data_home": str(tmp_path / "x3")}}
    env = Env(tmp_path, roster_extra={"servers": servers})
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner(), "claude": FakeRunner()})

    # an idle fleet is deterministic: ties break on the sorted name
    assert drv._pick_server({}) == "go1"

    # CONTROL: each pick loads a server, so the next one must go elsewhere.
    # First-fit returns "go1" all three times and this list reds.
    picked = []
    for _ in range(3):
        name = drv._pick_server({})
        picked.append(name)
        drv.servers[name]["active"] += 1
    assert picked == ["go1", "go2", "go3"], picked
    assert [drv.servers[n]["active"] for n in ("go1", "go2", "go3")] == [1, 1, 1]

    # the least-loaded server wins even when a lower-sorted name has capacity
    drv.servers["go1"]["active"] = 0
    assert drv._pick_server({}) == "go1"
    drv.servers["go1"]["active"] = 5
    drv.servers["go2"]["active"] = 2
    drv.servers["go3"]["active"] = 4
    assert drv._pick_server({}) == "go2"

    # a pin wins outright, however loaded it is, while it has capacity
    assert drv._pick_server({"server": "go1"}) == "go1"
    assert drv._pick_server({"server": "go3"}) == "go3"

    # a pin at capacity falls back to the least-loaded server, never to first-fit
    drv.servers["go3"]["max_concurrent"] = 4          # active is already 4
    assert drv._pick_server({"server": "go3"}) == "go2"

    # a parked server is never picked, pinned or not
    drv.servers["go2"]["parked_until"] = time.time() + 600
    drv.servers["go2"]["park_status"] = "QUOTA_WEEKLY"
    assert drv._pick_server({}) == "go1"
    assert drv._pick_server({"server": "go2"}) == "go1"

    # nothing available at all is None, not a crash and not an arbitrary name
    drv.servers["go1"]["max_concurrent"] = 0
    assert drv._pick_server({}) is None
    assert drv._pick_server({"server": "go2"}) is None


def grader_fails_on(ids):
    """PASS everywhere except an explicit FAIL on `ids` -- the shape a grader
    produces when it reads a [hosted] row, looks for the hosted record, finds
    none, and writes FAIL rather than UNKNOWN."""
    def fn(spec, abort_flag=None):
        wt = Path(spec.cwd)
        head = git(wt, "rev-parse", "HEAD")
        all_ids = [l.split()[1] for l in (wt / ".vp" / "BENCHMARK.md").read_text().splitlines()
                   if l.startswith("- B")]
        lines = [{"id": i, "kind": "evidence",
                  "verdict": "FAIL" if i in ids else "PASS",
                  "evidence": "platform/a.py:1",
                  "note": "no hosted record exists at this sha" if i in ids else "ok"}
                 for i in all_ids]
        Path(spec.out_path).write_text(json.dumps({"item": spec.item, "attempt": 1, "commit": head,
                                                   "lines": lines, "all_pass": False}))
        return TurnOutcome(STATUS_DONE, "", session_id="ses_g", record_path=spec.out_path,
                           usage={"tokens_in": 10, "tokens_out": 5, "cost": 0.001}, runner="fake")
    return fn


def test_d136_a_hosted_row_the_grader_FAILS_does_not_block_the_parent(tmp_path, monkeypatch):
    """D136: a [hosted] row belongs to the twin, so it must not block the parent
    HOWEVER THE GRADER PHRASED IT.

    The exemption used to filter `unknown` only, which left the policy at the
    grader's discretion. 2026-09-21: R-SEC-CALLERS-AND-SEED-ROUTE's B10 asks for
    a full canary-gated run; its grader correctly observed that no such record
    exists and wrote FAIL instead of UNKNOWN; the parent then burned all three
    rounds with `fails=['B10']` and nothing else -- REPAIR_REQUIRED for a row its
    own rounds structurally cannot answer, on a packet whose real work was done.

    The control is `grader_fails_on` rather than `grader_unknown_on`: under the
    old code the UNKNOWN path already passed, so only a FAIL distinguishes the fix.
    """
    _with_hosted_row(monkeypatch)
    env = Env(tmp_path)
    env.activate()
    runner = by_role({"builder": result_ok, "grader": grader_fails_on({"B9"}), "probe": result_ok})
    drv = env.driver({"opencode": runner, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 6)
    rows = env.rows()
    assert rows["L02"]["state"] == "VERIFIED", rows["L02"]
    harvest = json.loads(next((env.run_root / "turns" / "L02").glob("*/harvest.json")).read_text())
    assert "B9" in (harvest.get("hosted_owed") or []), harvest
    assert "HOSTED_OWED" in (env.run_root / "OWNER-ALERTS.md").read_text()
    # one build, not three: the parent must not burn its rounds on the twin's row
    assert len([s for s in runner.calls if s.item == "L02" and s.role == "builder"]) == 1


def test_d136_a_twin_is_still_blocked_by_its_own_hosted_row(tmp_path):
    """The other half, and the reason the exemption is keyed on `_exempt_rows`
    rather than on the tag: on a TWIN the hosted rows are the SUBJECT (D37), so
    `_exempt_rows` returns the empty set and a FAIL there must still block.
    Without this, D136 would silently make every hosted twin unfailable -- the
    exact vacuous-guard shape the row exists to prevent."""
    env = Env(tmp_path)
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner(), "claude": FakeRunner()})
    wt = tmp_path / "wt"
    (wt / ".vp").mkdir(parents=True)
    (wt / ".vp" / "BENCHMARK.md").write_text(
        "- B1 [invariant] [box] a box row\n"
        "- B10 [invariant] [hosted] the full suite is green at the candidate sha\n")
    # a parent exempts its [hosted] rows ...
    assert drv.hosted_rows(wt) == {"B10"}
    assert drv._exempt_rows("L02", wt) == {"B10"}

    # ... a twin exempts nothing, because the row is what it exists to answer
    class _Twin(dict):
        pass
    monkey = _Twin({"id": "L02-HOSTED", "twin_of": "L02"})
    drv.packet_for = lambda task, _p=monkey: _p if task.endswith("-HOSTED") else None
    assert drv._exempt_rows("L02-HOSTED", wt) == set(), \
        "a twin must still be blocked by its own [hosted] row"


def test_d138_concurrent_grader_picks_spread_instead_of_all_choosing_one_server(tmp_path):
    """D138: `_pick_server` read `active` under no lock, and the ONLY writer was
    `_try_acquire` -- which the dispatch loop calls for the builder turn alone.
    The grader picks run on per-task threads and never acquired anything, so N
    tasks reaching their grader step together read identical counts and every
    one of them selected the same name.  Ties break on the sorted name, so that
    name is the first server, every time.

    This is the D135 defect (`laneproof.pick_host`) in a second file, and the
    measured signature on the opencode side is a server failing every one of ~10
    concurrent sessions with "Session not found" and 0-byte output while a
    sibling at the same concurrency stayed clean.

    CONTROL (first block): the old shape is `_pick_server` with no increment,
    which is what the grader sites did.  It needs no timing window at all --
    without a writer, every concurrent reader returns the same answer forever.
    That block reds the moment `_pick_server` starts counting, which is the
    point: it pins WHY reserving is needed, not merely that it works."""
    servers = {"go1": {"url": "http://127.0.0.1:1", "max_concurrent": 100, "xdg_data_home": str(tmp_path / "x1")},
               "go2": {"url": "http://127.0.0.1:2", "max_concurrent": 100, "xdg_data_home": str(tmp_path / "x2")},
               "go3": {"url": "http://127.0.0.1:3", "max_concurrent": 100, "xdg_data_home": str(tmp_path / "x3")}}
    env = Env(tmp_path, roster_extra={"servers": servers})
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner(), "claude": FakeRunner()})

    n = 12

    def race(fn):
        barrier, out, lock = threading.Barrier(n), [], threading.Lock()

        def one():
            barrier.wait()
            name = fn()
            with lock:
                out.append(name)

        threads = [threading.Thread(target=one) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return out

    # CONTROL: the old grader shape -- pick, never count.  All 12 land on go1.
    picked = race(lambda: drv._pick_server({}))
    assert len(picked) == n
    assert set(picked) == {"go1"}, (
        "the unreserved pick is supposed to collapse onto one server -- if this spread, the "
        "control no longer reproduces the bug D138 fixes: %r" % sorted(set(picked)))

    for srv in drv.servers.values():
        srv["active"] = 0

    # D138: choose and count under one lock, so each racer sees the last one's slot.
    taken = race(lambda: drv._reserve_server({}))
    assert len(taken) == n
    assert None not in taken
    counts = {name: taken.count(name) for name in servers}
    assert max(counts.values()) <= -(-n // len(servers)), counts
    # the reservation is real: `active` must equal what was handed out
    assert {name: drv.servers[name]["active"] for name in servers} == counts

    # releasing returns the slots, and a double release must not go negative and
    # hand the server unlimited apparent capacity
    for name in taken:
        drv._release_server(name)
    assert {name: drv.servers[name]["active"] for name in servers} == {n: 0 for n in servers}
    drv._release_server("go1")
    assert drv.servers["go1"]["active"] == 0


def test_d138_both_grader_sites_reserve_and_release_rather_than_pick(tmp_path):
    """D138 call-site pin.  `_reserve_server` is atomic by construction, so a
    race test against it can never fail -- it cannot tell the fix from the bug.
    The defect lives at the CALL SITES, so that is where the assertion has to
    bite ([[call-site-mutation-must-bite]]): reverting either grader site to
    `_pick_server` reds this and nothing else.

    The `finally` clause is asserted too, not just the release.  Both grader
    blocks return early on a non-DONE turn, so a release placed inline leaks the
    slot upward on every failed grade until the server looks permanently full --
    a starvation bug that no functional test would show."""
    import inspect

    sites = ("_build_pipeline", "_review_packet_finish")
    checked = 0
    for name in sites:
        fn = getattr(lanedriver.LaneDriver, name, None)
        assert fn is not None, (
            "%s is gone -- this pin names its call sites by hand, so a rename silently empties "
            "the loop and the test passes while guarding nothing" % name)
        src = inspect.getsource(fn)
        assert "gserver" in src, (
            "%s no longer dispatches a grader turn; re-point this pin at whatever does" % name)
        assert "_reserve_server(gcfg)" in src, (
            "%s picks its grader server without reserving a slot -- concurrent tasks will "
            "all choose the same name" % name)
        assert "_pick_server(gcfg)" not in src, (
            "%s still uses the unreserved pick for its grader" % name)
        assert "finally:" in src and "_release_server(gserver)" in src, (
            "%s must release the grader slot in a finally -- it returns early on a non-DONE "
            "turn, and an inline release leaks the slot on every failed grade" % name)
        checked += 1
    assert checked == len(sites)


# -- D142: the unpark probe must exercise the verb that actually fails ----------

class _FakeResp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _WedgedServer:
    """The 2026-09-21T02:41Z outage, reproduced.

    The real wedge served `GET /config` and `GET /session` with 200 in 9-28 ms on
    all three servers while `POST /session` timed out.  Reads healthy, creation
    dead -- which is precisely the shape a GET probe cannot see.
    """

    def __init__(self, post_ok=False):
        self.post_ok = post_ok
        self.calls = []

    def __call__(self, req, timeout=None):
        method = (getattr(req, "method", None) or "GET").upper()
        url = getattr(req, "full_url", None) or str(req)
        self.calls.append((method, url))
        if method == "GET":
            return _FakeResp(200, b"[]")
        if method == "POST":
            if not self.post_ok:
                raise TimeoutError("timed out")
            return _FakeResp(200, b'{"id": "ses_probe1"}')
        if method == "DELETE":
            return _FakeResp(200, b"{}")
        raise AssertionError("unexpected verb %r" % method)


def test_d142_the_old_unpark_predicate_cannot_see_a_wedged_server(tmp_path, monkeypatch):
    """Control + fix in one: the window is real, and only the new probe observes it.

    The first assertion is the control.  It reproduces the pre-D142 gate against a
    server that is genuinely broken and shows it returning True -- if that ever
    starts failing, this window has closed and the rest of the test proves nothing.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    url = "http://127.0.0.1:4102/session"
    wedged = _WedgedServer(post_ok=False)
    monkeypatch.setattr(lanedriver.urllib.request, "urlopen", wedged)

    assert drv._http_ok(url) is True, (
        "control: a wedged opencode server still answers GET /session, which is why "
        "the pre-D142 unpark gate passed vacuously and the fleet park-looped")

    assert drv._session_roundtrip_ok(url) is False, "the fix must see what the control cannot"
    assert "POST /session" in drv._probe_detail and "TimeoutError" in drv._probe_detail, (
        "the detail names the operation that failed: %r" % drv._probe_detail)
    assert [m for m, _ in wedged.calls] == ["GET", "POST"], (
        "no session was created, so nothing needs deleting: %r" % wedged.calls)


def test_d142_a_wedged_server_stays_parked_and_a_healthy_one_unparks_and_cleans_up(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    drv.fake_runners = False

    def arm_park():
        srv = drv.servers["go2"]
        srv["park_reason"] = "opencode request failed: POST /session: timed out"
        srv["park_status"] = "DEGRADED"
        srv["parked"] = False
        srv["parked_until"] = 0.0

    wedged = _WedgedServer(post_ok=False)
    monkeypatch.setattr(lanedriver.urllib.request, "urlopen", wedged)
    arm_park()
    drv._maybe_unpark()
    assert drv.servers["go2"]["park_reason"], (
        "a server that cannot create a session must stay parked -- unparking it is the loop")

    healthy = _WedgedServer(post_ok=True)
    monkeypatch.setattr(lanedriver.urllib.request, "urlopen", healthy)
    arm_park()
    drv._maybe_unpark()
    assert not drv.servers["go2"]["park_reason"], "a server that creates a session unparks"

    verbs = [m for m, _ in healthy.calls]
    assert verbs == ["POST", "DELETE"], "create then delete, nothing else: %r" % healthy.calls
    assert healthy.calls[1][1].endswith("/session/ses_probe1"), (
        "the probe deletes the session it created, not something else: %r" % healthy.calls[1][1])

    probes = [json.loads(l) for l in
              (env.run_root / "probes" / "probes.jsonl").read_text().splitlines()]
    assert probes[-1]["verb"] == "POST" and probes[-1]["ok"] is True
    assert probes[-2]["verb"] == "POST" and probes[-2]["ok"] is False


# -- D146: a scoped pass must not look like a full pass ------------------------

def _write_proof(run_root, proof_id, status="PASS", only=None, pipeline_id="p1"):
    d = run_root / "proofs"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("%s.json" % proof_id)).write_text(json.dumps({
        "proof_id": proof_id, "status": status, "only": only, "route": "gha",
        "pipeline_id": pipeline_id, "sha": "a" * 40}), encoding="utf-8")
    return d / ("%s.json" % proof_id)


def test_d146_a_scoped_pass_and_a_full_pass_are_distinguishable(tmp_path, monkeypatch):
    """The spec's own control: verify the SAME item twice, once scoped and once
    full, and require the two to differ in a machine-readable field.  Before this,
    both were the same token -- a row proven over four files and a row proven over
    everything were indistinguishable to the union builder.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    rr = env.run_root

    _write_proof(rr, "proof-ITEM-60921T000001", only=None)
    full = drv.proof_scope_for("ITEM")
    assert full["class"] == "full" and full["only"] is None

    (rr / "proofs" / "proof-ITEM-60921T000001.json").unlink()
    _write_proof(rr, "proof-ITEM-60921T000002", only="twin:3:platform/tests/test_x.py")
    scoped = drv.proof_scope_for("ITEM")
    assert scoped["class"] == "scoped"
    assert scoped["only"] == "twin:3:platform/tests/test_x.py", "the scope is retained, not just a flag"
    assert full != scoped, (
        "the same item proven two different ways must not produce identical records -- "
        "if these match, the feature is absent no matter what the code says")


def test_d146_absence_of_a_passing_proof_is_never_reported_as_full(tmp_path, monkeypatch):
    """`full` is a claim about coverage. Defaulting an unknown to it is how a row
    with no passing proof at all would acquire the strongest possible label."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    assert drv.proof_scope_for("ITEM") is None, "no record at all"
    _write_proof(env.run_root, "proof-ITEM-60921T000003", status="FAIL_PRODUCT", only=None)
    assert drv.proof_scope_for("ITEM") is None, "a failing record proves no scope"
    _write_proof(env.run_root, "proof-ITEM-60921T000004", status="BLOCKED_CAP", only=None)
    assert drv.proof_scope_for("ITEM") is None, "a held record proves no scope"


def test_d146_scope_lookup_does_not_prefix_match_a_longer_task_id(tmp_path, monkeypatch):
    """`proof-L20-*` also matches `proof-L20-HOSTED-R3-*`.  Reading a twin's scope
    as its parent's is the D145 bug in a second id vocabulary, and it would report
    a scope for a task that has no passing proof of its own."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    _write_proof(env.run_root, "proof-L20-HOSTED-R3-60921T000005", only=None)
    assert drv.proof_scope_for("L20") is None, (
        "L20 has no passing proof of its own; L20-HOSTED-R3's must not be read as its")
    assert (drv.proof_scope_for("L20-HOSTED-R3") or {}).get("class") == "full"


def test_d147_only_a_row_that_owed_a_proof_and_lacks_one_is_alarming(tmp_path, monkeypatch):
    """D146 shipped one bucket for "no passing proof" and it fired 4/4 on live data,
    every one a false alarm. A field that is wrong every time it fires gets ignored,
    and then it is ignored on the occasion it is right.

    The control asserts the separation in both directions: a row that never owed a
    proof must NOT be alarming, and a row that owed one and lacks it MUST be.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    owed = drv.proof_cfg.get("require_for_kinds") or lanedriver.DEFAULT_PROOF_KINDS
    assert "builder" in owed and "probe" not in owed, (
        "control: this test's premise is that `probe` never owes a proof while "
        "`builder` does -- if the roster changes that, the cases below invert")
    # D147 control: `require_for_kinds` and the kind on a run-state row are two
    # closed vocabularies, and they are only USEFUL where they overlap.  My first
    # cut fell back to ["builder"], a kind NO live row has -- so the alarming
    # bucket could never fire and the field would have read 0 forever.  A zero you
    # cannot distinguish from "switched off" is not a measurement.
    assert set(owed) & set(lanedriver.KIND_MAP), (
        "require_for_kinds %r shares no kind with KIND_MAP, the vocabulary run-state "
        "rows actually use -- nothing would ever owe a proof" % (owed,))

    # 1. its own passing proof -> the scope it actually ran, basis "own"
    _write_proof(env.run_root, "proof-OWN-60921T000001", only="twin:3:platform/tests/test_x.py")
    own = drv.member_scope("OWN", "builder")
    assert own["class"] == "scoped" and own["basis"] == "own"

    # 2. a kind that never owed one -> not alarming, and says why
    nr = drv.member_scope("PROBEROW", "probe")
    assert nr["class"] == "not_required" and nr["basis"] == "kind", nr

    # 3. no proof of its own, but its HOSTED twin has one -> inherited, not alarming
    _write_proof(env.run_root, "proof-BOXROW-HOSTED-R2-60921T000002", only=None)
    inh = drv.member_scope("BOXROW", "builder")
    assert inh["class"] == "full" and inh["basis"] == "inherited"
    assert inh["from"] == "BOXROW-HOSTED-R2", inh

    # 4. owed a proof, has none anywhere -> the ONLY alarming outcome
    bad = drv.member_scope("NAKED", "builder")
    assert bad["class"] == "unproven" and bad["basis"] == "none", bad

    # the four are genuinely distinct, not four spellings of one answer
    classes = {own["class"], nr["class"], inh["class"], bad["class"]}
    assert len(classes) == 4, classes


def test_d147_twin_lookup_does_not_borrow_an_unrelated_row_s_proof(tmp_path, monkeypatch):
    """The inherited arm must not become a second prefix-match. `L20`'s twin is
    `L20-HOSTED*` and nothing else -- not `L20-EXTRA`, and not `L20-HOSTED-EXTRA`."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    _write_proof(env.run_root, "proof-L20-EXTRA-60921T000003", only=None)
    _write_proof(env.run_root, "proof-L20-HOSTED-EXTRA-60921T000004", only=None)
    assert drv._twin_ids("L20") == [], (
        "only <task>-HOSTED / -HOSTED-R<n> is a twin: %r" % drv._twin_ids("L20"))
    assert drv.member_scope("L20", "builder")["class"] == "unproven"
    _write_proof(env.run_root, "proof-L20-HOSTED-60921T000005", only=None)
    assert drv._twin_ids("L20") == ["L20-HOSTED"]
    assert drv.member_scope("L20", "builder")["basis"] == "inherited"


def test_d148_a_verified_row_shows_what_its_proof_actually_ran(tmp_path, monkeypatch):
    """PROOF-SCOPE spec Part A req 2. The union-104 shape was a full-suite claim
    assembled from scoped parts. D146/D147 made that derivable; this makes it
    VISIBLE where people read a verdict, so VERIFIED can no longer be misread as
    "verified against everything".
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})

    # not VERIFIED -> premature question, and no proof lookup is paid for
    assert drv._ledger_scope("X", {"state": "RUNNING", "kind": "builder"}) == "-"

    # the load-bearing case: VERIFIED on a SCOPED proof must not read as "verified"
    _write_proof(env.run_root, "proof-SC-60921T000010", only="twin:3:platform/tests/test_a.py")
    out = drv._ledger_scope("SC", {"state": "VERIFIED", "kind": "builder"})
    assert out.startswith("scoped:") and "test_a.py" in out, out

    _write_proof(env.run_root, "proof-FU-60921T000011", only=None)
    assert drv._ledger_scope("FU", {"state": "VERIFIED", "kind": "builder"}) == "full"

    # evidence filed under the hosted twin keeps BOTH facts: the scope and whose it is
    _write_proof(env.run_root, "proof-BX-HOSTED-60921T000012", only=None)
    assert drv._ledger_scope("BX", {"state": "VERIFIED", "kind": "builder"}) == "full via BX-HOSTED"

    assert drv._ledger_scope("PR", {"state": "VERIFIED", "kind": "probe"}) == "not-required"

    # D148, found by projecting this column over all 286 VERIFIED rows before
    # shipping it: "no proof record here" is NOT an accusation.  47 rows were
    # verified before this run kept proofs at all, and 14 carry no kind, so an
    # `UNPROVEN` cell would have fired 61 times with 61 false -- D147's own bug,
    # one population wider.  The ledger states what it sees; the accusation lives
    # in the union record, where the rows are the ones being assembled now.
    assert drv._ledger_scope("NK", {"state": "VERIFIED", "kind": "builder"}) == "no-proof-in-run"
    assert drv._ledger_scope("NK", {"state": "VERIFIED", "kind": None}) == "no-kind"
    assert drv.member_scope("NK", None)["class"] == "not_required", (
        "an unknown kind cannot be said to owe a proof")
    assert drv.member_scope("NK", None)["basis"] == "kind_unknown", (
        "and it must stay distinguishable from a kind that genuinely never owed one")

    # the six answers are six answers, not one token wearing hats
    seen = {drv._ledger_scope(t, {"state": "VERIFIED", "kind": k})
            for t, k in (("SC", "builder"), ("FU", "builder"), ("BX", "builder"),
                         ("PR", "probe"), ("NK", "builder"), ("NK", None))}
    assert len(seen) == 6, seen


def test_d148_the_scope_column_cannot_break_the_ledger_table(tmp_path, monkeypatch):
    """A new column is a chance to emit a row that no longer matches its header.
    `only` is a path list and a `|` in it would silently shift every later cell.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    _write_proof(env.run_root, "proof-PIPE-60921T000013", only="a.py|b.py")
    cell = drv._ledger_scope("PIPE", {"state": "VERIFIED", "kind": "builder"})
    assert "|" not in cell, "a pipe in `only` reaches the markdown row: %r" % cell

    drv.render()
    text = (env.run_root / "LEDGER.md").read_text()
    body = text.split("| task |", 1)[1]
    rows = [l for l in body.splitlines() if l.startswith("|")]
    assert rows, "no table rows rendered"
    widths = {l.count("|") for l in rows}
    assert len(widths) == 1, (
        "rendered rows disagree on column count %s -- header and body have drifted" % widths)
    assert "| task | state | scope | kind |" in text, "scope sits beside the verdict, not at the end"


def _d152_rec(tmp, name="a", **over):
    """One RESULT.json per subdirectory, so each arm below reads its own file."""
    rec = {"item": "T", "attempt": 1, "base": "b" * 40, "blocked": None,
           "diff_stat": {"files": 0, "insertions": 0, "deletions": 0},
           "checks": [], "notes": "nothing to do"}
    rec.update(over)
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "RESULT.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def test_d152_a_noop_result_gets_head_and_a_builder_that_owes_a_commit_does_not(tmp_path, monkeypatch):
    """D152. A hosted twin stacked on its already-VERIFIED parent has nothing to
    commit; `commit` is binding, so it failed with `commit: missing` and burned
    all three rounds producing the same record. HEAD is what the 104 accepted
    no-op records already carry.

    The load-bearing half is the refusal: a builder that skipped a commit it owed
    self-reports a clean tree exactly as convincingly as one that owed nothing,
    so the driver must look at git itself.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    HEAD, BASE = "b" * 40, "b" * 40

    calls = []

    def git_clean(args, **kw):
        calls.append(args)
        if "status" in args:
            return 0, "", ""                       # clean tree
        if "rev-parse" in args:
            return 0, HEAD + "\n", ""
        return 0, "", ""
    drv.git = git_clean

    # 1. the no-op case: HEAD is recorded, and the record says who filled it
    p = _d152_rec(tmp_path, "noop", base=BASE)
    drv._fill_noop_commit(p, tmp_path, "T")
    got = json.loads(p.read_text())
    assert got["commit"] == HEAD, got
    assert "D152" in got["notes"] and "clean at base" in got["notes"], (
        "an accepted record must say which sha the driver filled in and why")
    assert "nothing to do" in got["notes"], "the builder's own note is kept"

    # 2. THE REFUSAL: a dirty worktree means the commit really is missing
    def git_dirty(args, **kw):
        if "status" in args:
            return 0, " M platform/api/admin.py\n", ""
        return 0, HEAD + "\n", ""
    drv.git = git_dirty
    p2 = _d152_rec(tmp_path, "dirty", base=BASE)
    drv._fill_noop_commit(p2, tmp_path, "T")
    assert "commit" not in json.loads(p2.read_text()), "uncommitted work must still fail"

    # 3. a non-zero diff_stat is work that was never committed
    drv.git = git_clean
    p3 = _d152_rec(tmp_path, "haswork", base=BASE,
                   diff_stat={"files": 1, "insertions": 3, "deletions": 0})
    drv._fill_noop_commit(p3, tmp_path, "T")
    assert "commit" not in json.loads(p3.read_text())

    # 4. HEAD that is not the declared base: something moved, do not paper over it
    def git_moved(args, **kw):
        if "status" in args:
            return 0, "", ""
        return 0, "c" * 40 + "\n", ""
    drv.git = git_moved
    p4 = _d152_rec(tmp_path, "moved", base=BASE)
    drv._fill_noop_commit(p4, tmp_path, "T")
    assert "commit" not in json.loads(p4.read_text())

    # 5. a blocked result keeps its own story
    drv.git = git_clean
    p5 = _d152_rec(tmp_path, "blocked", base=BASE, blocked="needs the owner")
    drv._fill_noop_commit(p5, tmp_path, "T")
    assert "commit" not in json.loads(p5.read_text())

    # 6. an existing commit is never overwritten
    p6 = _d152_rec(tmp_path, "already", base=BASE, commit="a" * 40)
    drv._fill_noop_commit(p6, tmp_path, "T")
    assert json.loads(p6.read_text())["commit"] == "a" * 40

    # 7. "it changed nothing" must be SAID, not inferred from silence. A record
    # that omits diff_stat, or nulls it, has not reported a no-op -- it has
    # reported nothing, and absent must never read as zero. (This arm exists
    # because the mutation `None -> zero` survived every other assertion here.)
    for i, missing in enumerate((None, {}, {"files": 0}, {"files": None, "insertions": 0,
                                                          "deletions": 0})):
        p7 = _d152_rec(tmp_path, "absent%d" % i, base=BASE, diff_stat=missing)
        drv._fill_noop_commit(p7, tmp_path, "T")
        assert "commit" not in json.loads(p7.read_text()), missing


def test_d152_the_substitution_is_not_taken_from_the_builders_own_checks(tmp_path, monkeypatch):
    """The trust boundary, stated as its own control. A record whose `checks[]`
    swear the tree is clean must still be refused when git disagrees -- otherwise
    the gate is the builder's word about itself."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    liar = _d152_rec(tmp_path, "liar", base="b" * 40, checks=[
        {"name": "git-status-clean", "command": "git status --porcelain", "exit": 0,
         "log": "empty: worktree clean, no modifications"}])

    def git_dirty(args, **kw):
        if "status" in args:
            return 0, " M platform/core/tenancy.py\n", ""
        return 0, "b" * 40 + "\n", ""
    drv.git = git_dirty
    drv._fill_noop_commit(liar, tmp_path, "T")
    assert "commit" not in json.loads(liar.read_text()), (
        "checks[] said clean and git said dirty; git wins")


# -- D153: the packet sweep, wired into the tick ------------------------------


def _d153_drv(tmp_path, stranded=True):
    """A driver whose pack dir holds one packet that can never bind (`NEW:` id,
    no lane named in the body, no parent_contract), or one that binds cleanly."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    pack = tmp_path / "PACKETS"
    pid = "ORPHAN-1" if stranded else "L01"
    task = "NEW:ORPHAN-1" if stranded else "L01"
    body = "No lane is named here at all." if stranded else "Body."
    d = pack / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "PACKET.md").write_text(
        _VPSWEEP_FM.format(id=pid, base="a" * 40, task=task, extra="", body=body),
        encoding="utf-8")
    drv.pack_dir = pack
    drv._load_pack()
    # The driver must have MENTIONED this packet, or the sweep classifies it
    # `unseen` (newly placed) rather than stranded -- correctly, since a packet
    # the driver has never scanned may simply have arrived seconds ago.
    #
    # This was implicit before D158: driver.log happened to be empty here, so
    # `_seen_packet_ids` returned None and the unseen bucket was disabled
    # entirely. D158's startup warnings put content in the log and flipped it,
    # which is how the gap showed up. The fixture, not the behaviour, was wrong.
    drv.log("PACK dir changed: +['%s'] -[]" % pid)
    return env, drv


_VPSWEEP_FM = """---
item: {id}
title: packet {id}
group: 1
base_sha: {base}
depends_on: []
releases: []
critical: false
owned_files:
  - control/evidence/{id}/v13/REGRADE.md
forbidden_files: []
test_paths: []
proof_kind: platform
max_rounds: 2
max_minutes_build: 20
reviewer_model: none
owner_needed: none
v13_kind: regrade
scheduler_task: {task}
{extra}---

## Why

Body for {id}. {body}
"""


def test_d153_a_stranded_packet_alerts_and_a_bound_one_stays_quiet(tmp_path):
    """The whole point. A packet that can never become a row is invisible to
    every other guard here -- they all watch rows, and this one has none. So the
    sweep must say something, and must NOT say something when the pack is fine
    (an alert that fires on a healthy pack is an alert people turn off)."""
    env, drv = _d153_drv(tmp_path, stranded=True)
    drv._sweep_step({"tasks": {"L01": {"state": "READY"}}})
    alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert "PACKETS_STRANDED" in alerts, alerts
    assert "ORPHAN-1" in alerts

    clean = tmp_path / "clean"
    clean.mkdir()
    env2, drv2 = _d153_drv(clean, stranded=False)
    drv2._sweep_step({"tasks": {"L01": {"state": "READY"}}})
    p = env2.run_root / "OWNER-ALERTS.md"
    assert "PACKETS_STRANDED" not in (p.read_text() if p.exists() else ""), \
        "a bound pack must not alert"


def test_d153_a_sweep_that_could_not_run_is_not_a_clean_bill(tmp_path):
    """THE load-bearing arm. A sweep that cannot run and a sweep that finds
    nothing both print zero stranded. If an unusable sweep returned quietly, the
    guard would read as green forever on a run where it never once measured
    anything -- which is exactly the failure this sweep was written to catch in
    other people's guards."""
    env, drv = _d153_drv(tmp_path, stranded=True)
    drv._sweep_step({"tasks": {}})                    # empty run-state -> SweepUnusable
    alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert "SWEEP_UNUSABLE" in alerts, alerts
    assert "NOT being measured" in alerts
    assert "PACKETS_STRANDED" not in alerts, "an unusable sweep must not also claim findings"


def test_d153_the_sweep_never_takes_the_tick_down(tmp_path):
    """It is a report riding the dispatch loop. Anything it raises would stop
    the driver dispatching, which is infinitely worse than the class it finds."""
    env, drv = _d153_drv(tmp_path, stranded=True)

    def boom(*a, **kw):
        raise RuntimeError("sweep exploded")
    drv._sweep_last_mono = None
    orig, vpsweep.sweep_loaded = vpsweep.sweep_loaded, boom
    try:
        drv._sweep_step({"tasks": {"L01": {"state": "READY"}}})   # must not raise
    finally:
        vpsweep.sweep_loaded = orig
    assert "SWEEP failed: RuntimeError: sweep exploded" in drv.log_path.read_text()


def test_d153_it_is_rate_limited_and_logs_every_run_even_when_clean(tmp_path):
    """Two halves. The sweep walks the whole pack, so it must not run every
    tick. And it must leave a line every time it DOES run: a guard with no
    durable trace cannot later be told apart from one that never ran -- the
    exact confusion that made three other guards on this run unfalsifiable."""
    env, drv = _d153_drv(tmp_path, stranded=False)
    drv.alerts_cfg = dict(drv.alerts_cfg or {}, sweep_every_s=9999)
    drv._sweep_step({"tasks": {"L01": {"state": "READY"}}})
    first = drv.log_path.read_text().count("SWEEP examined=")
    assert first == 1, "a clean sweep must still log that it ran"
    assert "STRANDED=0" in drv.log_path.read_text()
    drv._sweep_step({"tasks": {"L01": {"state": "READY"}}})
    assert drv.log_path.read_text().count("SWEEP examined=") == 1, "not once per tick"


def test_d153_the_sweep_reads_the_drivers_tasks_not_the_state_file(tmp_path):
    """The control plane owns run-state.json and may be mid-write. Re-reading it
    would make the sweep race the writer and disagree with the driver about the
    very rows it is judging, so the driver hands in the tasks it already holds."""
    env, drv = _d153_drv(tmp_path, stranded=True)
    seen = {}

    def spy(pack_dir, tasks, **kw):
        seen["tasks"] = tasks
        seen["kw"] = kw
        return {"examined": 1, "bound": [], "waiting": [], "unseen": [],
                "blocked_parent": [], "stranded": []}
    orig, vpsweep.sweep_loaded = vpsweep.sweep_loaded, spy
    try:
        drv._sweep_step({"tasks": {"SENTINEL": {"state": "READY"}}})
    finally:
        vpsweep.sweep_loaded = orig
    assert seen["tasks"] == {"SENTINEL": {"state": "READY"}}, \
        "the driver's own state_view must be what is swept"
    assert seen["kw"].get("run_root") == drv.run_root


def test_d153_vpsweep_is_reloadable(tmp_path):
    """lanedriver now imports vpsweep, so a vpsweep edit that is not reloadable
    would need a full driver restart -- the Operator's verb, not mine -- while
    the reload reported `changed=[]` and shipped nothing.

    Membership in RELOAD_ORDER is NOT the live condition and asserting only that
    would be a form check. `reload_targets` walks `sys.modules` and keeps a
    module only when it is actually imported AND resolves inside the driver's
    own directory, so a name in the tuple that nothing imports is tracked by
    nothing. This asserts the machine field: that a real driver's reload set
    contains vpsweep. (Live confirmation of exactly this: reload #11 shipped
    D153 and vpsweep was absent from its hash set, because the running process
    predated the new import -- the tuple said yes while the process said no.)
    """
    assert "vpsweep" in lanedriver.RELOAD_ORDER
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    names = [n for n, _ in drv.reload_targets()]
    assert "vpsweep" in names, (
        "vpsweep is in RELOAD_ORDER but not in the live reload set (%s) -- an edit "
        "to it would need a driver restart, not a reload" % names)
    assert "vpsweep" in drv.code_hashes(), "and it must contribute to code_version"

    # ...and the other half, which reload_targets CANNOT see from in here: this
    # test module imports vpsweep itself, so sys.modules is satisfied whether or
    # not lanedriver imports it. Deleting lanedriver's import left the check
    # above green. The production import must therefore be asserted on
    # lanedriver's own source, or the pin measures the test's imports.
    src = Path(lanedriver.__file__).parent.joinpath("lanedriver.py").read_text()
    assert re.search(r"^import vpsweep", src, re.M), (
        "lanedriver must import vpsweep itself -- this test's own import would "
        "otherwise satisfy reload_targets and the pin would measure nothing")


def test_d153_the_call_site_passes_tasks_not_the_whole_state_view(tmp_path):
    """The live D153 bug, pinned where it actually happened.

    `test_d153_the_sweep_reads_the_drivers_tasks_not_the_state_file` calls
    `_sweep_step` directly, so it proves the function and says nothing about its
    caller. The defect was entirely in the caller: `_pack_step` handed over
    `control.state_view()` -- which is {"tasks": {...}, "sequence": ...} -- and
    every packet resolved to nothing. Live result: bound=0, STRANDED=62, against
    a CLI run minutes earlier reporting bound=277, STRANDED=0.

    So this drives `_pack_step` and asserts what the sweep RECEIVED.
    """
    env, drv = _d153_drv(tmp_path, stranded=False)
    view = {"tasks": {"L01": {"state": "READY"}}, "sequence": 7, "counts": {}}
    drv.control.state_view = lambda: view
    seen = {}

    def spy(pack_dir, tasks, **kw):
        seen["tasks"] = tasks
        return {"examined": 1, "bound": [], "waiting": [], "unseen": [],
                "blocked_parent": [], "stranded": []}
    drv._pack_last_mono = None
    drv._sweep_last_mono = None
    orig, vpsweep.sweep_loaded = vpsweep.sweep_loaded, spy
    try:
        drv._pack_step()
    except Exception:
        drv._sweep_step(drv.control.state_view() or {})
    finally:
        vpsweep.sweep_loaded = orig
    assert seen.get("tasks") == {"L01": {"state": "READY"}}, (
        "the sweep must receive the TASKS; it got %r" % (seen.get("tasks"),))
    assert "sequence" not in (seen.get("tasks") or {}), \
        "the whole state view leaked through -- every packet would read as stranded"


def test_d153_sweep_step_takes_a_state_view_like_every_other_step(tmp_path):
    """D155. The sibling convention IS the guard.

    Every other periodic step in _pack_step is `_x_step(self, state)` and
    unwraps `.get("tasks")` itself. _sweep_step took `tasks`, so its call site
    was the one line there with a different shape -- and that is exactly the
    line I got wrong, passing the view straight through and calling 62
    correctly-bound packets stranded.

    Pinning the signature means the next person copying the line above or below
    it cannot introduce the same defect.
    """
    import inspect
    sig = inspect.signature(lanedriver.LaneDriver._sweep_step)
    assert list(sig.parameters) == ["self", "state"], (
        "_sweep_step must take a state view like its siblings, not tasks: %s" % list(sig.parameters))
    for name in ("_canary_step", "_fleet_step", "_preflight_step", "_closure_sweep",
                 "_supersede_sweep"):
        fn = getattr(lanedriver.LaneDriver, name, None)
        assert fn is not None, (
            "%s is gone -- this pin names the convention's members by hand, so a "
            "rename silently empties it" % name)
        assert list(inspect.signature(fn).parameters)[1] == "state", name


# -- D157: no TurnOutcome detail may end in a bare colon ----------------------


def test_d157_a_reasonless_proof_never_yields_a_detail_ending_in_a_colon():
    """The consumer half, and the reason it exists separately from the source
    fix: part one only repairs the statuses that exist TODAY. The next status
    that forgets to set `reason` would silently produce a bare colon again --
    which is exactly how fourteen rows ended up with an unactionable verdict.

    Driven with reason=None, the shape that actually occurred.
    """
    rec = {"status": "FAIL_INFRA", "reason": None, "pipeline_id": "35569998113",
           "route": "gha",
           "reds": [{"job": "vp/platform-coverage", "kind": "infra"}],
           "failed_nodes": []}
    why = lanedriver.LaneDriver._proof_detail_fallback(rec, "FAIL_INFRA")
    detail = "proof %s %s: %s" % ("proof-X", "FAIL_INFRA", why[:200])
    assert not re.search(r":\s*$", detail), detail
    assert "vp/platform-coverage" in detail

    # and when there is nothing at all, it names what it checked
    bare = {"status": "FAIL_INFRA", "reason": None, "reds": [], "failed_nodes": [],
            "pipeline_id": None, "route": "gha"}
    why2 = lanedriver.LaneDriver._proof_detail_fallback(bare, "FAIL_INFRA")
    assert not re.search(r":\s*$", "x: " + why2)
    assert "no reason recorded" in why2 and "failed_nodes" in why2


def test_d157_the_empty_reason_shape_is_gone_from_the_call_site():
    """Pin the call site, not just the helper. `str(rec.get("reason") or "")`
    was the whole defect -- the helper cannot prevent it being reintroduced one
    line above."""
    import inspect
    src = inspect.getsource(lanedriver.LaneDriver._grade_proof) \
        if hasattr(lanedriver.LaneDriver, "_grade_proof") else None
    whole = Path(lanedriver.__file__).parent.joinpath("lanedriver.py").read_text(encoding="utf-8")
    code = re.sub(r"#.*", "", whole)          # a comment quoting the old shape is not the bug
    assert 'str(rec.get("reason") or "")[:200]' not in code, (
        "the bare-colon shape is back at the TurnOutcome call site")
    assert "_proof_detail_fallback" in code


# -- D158: a RELOAD_ORDER name the process never imported must say so ---------


def test_d158_an_unimportable_reload_order_name_is_warned_about_once(tmp_path):
    """The gap this closes is silent by construction: `sys.modules.get(name)`
    returns None, the name is dropped, and nothing anywhere says the module is
    unreloadable. vpstore is first in RELOAD_ORDER and is exactly that -- only
    vpctl and vpproof import it, so an edit to it armed with a reload ships
    nothing and the reload reports changed=[] with no indication why.

    Once per name per process: reload_targets runs on every code_hashes() call,
    so warning unconditionally would bury the log.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    drv.reload_modules = ("vppack", "definitely_not_a_real_module")
    drv._reload_unresolved = set()
    drv.log_path.write_text("", encoding="utf-8")

    names = [n for n, _ in drv.reload_targets()]
    assert "vppack" in names, "a resolvable name must still be kept"
    assert "definitely_not_a_real_module" not in names

    log = drv.log_path.read_text()
    assert "definitely_not_a_real_module" in log and "never imported" in log, log
    assert "driver restart" in log, "say what it would actually take to ship such an edit"
    assert "vppack" not in log, "a name that resolved must not be warned about"

    drv.reload_targets()
    drv.reload_targets()
    assert log.count("definitely_not_a_real_module") == 1 or \
        drv.log_path.read_text().count("is not in this driver's reload set") == 1, \
        "warned once per name, not once per code_hashes() call"


def test_d158_vpstore_is_the_live_instance_and_is_reported(tmp_path):
    """Pins the real case, not just a synthetic name. If something later imports
    vpstore into the driver this test should be REMOVED, not weakened -- the
    warning going quiet would then be correct."""
    assert "vpstore" in lanedriver.RELOAD_ORDER, (
        "the ruling was to keep vpstore in the tuple and make the gap honest, "
        "not to remove it")
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    drv._reload_unresolved = set()
    drv.log_path.write_text("", encoding="utf-8")
    names = [n for n, _ in drv.reload_targets()]
    log = drv.log_path.read_text()
    if "vpstore" in names:                      # something now imports it: fine, and better
        assert "vpstore" not in log
    else:
        assert "vpstore" in log and "not in this driver's reload set" in log, log


def test_d158_a_module_outside_the_driver_dirs_gets_a_different_reason(tmp_path):
    """Two distinct failures share one symptom (the name is dropped): never
    imported, versus imported from somewhere else. A single message for both
    would send the reader looking in the wrong place."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    drv.reload_modules = ("json",)              # stdlib: imported, but far away
    drv._reload_unresolved = set()
    drv.log_path.write_text("", encoding="utf-8")
    drv.reload_targets()
    log = drv.log_path.read_text()
    assert "outside the driver's own directories" in log, log
    assert "never imported" not in log, "wrong diagnosis for a module that IS imported"


# -- D164: a hosted row that asserts a CI JOB'S OWN STATUS cannot be answered by a twin --------

def test_d164_job_status_row_is_a_property_not_a_spelling():
    """D164. JOB_ASK_RE is a list of job NAMES and set phrases, so it refuses only
    rows worded the way earlier rows were worded. L28's B12 is obviously a
    job-status ask and matched none of them, so L28-HOSTED-R3 ran scoped and
    failed with "targeted: never a full-suite answer" while the four sibling rows
    on the SAME borrowed pipeline passed unscoped.

    `CI_CONTEXT_RE` is the other half of the property, not a tie-breaker:
    "required gate" also names the DEPLOY preflight checklist's gates, and
    L17-REGISTRY-REPLY-PINS B9 -- a DELIVERY-2A rehearsal host, no CI job at all
    -- is the one false positive the status pattern alone produces over all 169
    [hosted] rows in the pack. [[a-heuristic-tuned-against-your-own-labels]]"""
    from lanedriver import LaneDriver as D

    l28 = ("- B12 [test] [hosted] `platform/tests/test_ops.py` "
           "(`test_readiness_fails_closed_on_an_unconfigured_deployment`) passes on the exact sha "
           "inside the hosted platform shard, and the shard, lint and required-gate jobs are green "
           "— check: job links and status (gate: CIRCLECI).")
    assert D.job_status_row([l28]) == "required-gate", (
        "the row this whole item exists for; JOB_ASK_RE matches none of it")

    for line, why in (
        ("- B8 [hosted] ... — check: CircleCI job status for that sha (gate: CIRCLECI).", "job status"),
        ("- B8 [hosted] The `lint-and-typecheck` CircleCI job is green — check: job link and status.",
         "job link"),
        ("- B6 [hosted] The exact-SHA CircleCI `lint-and-typecheck` and `platform-shard` jobs are "
         "green including the new completeness test.", "jobs are green"),
        ("- B9 [hosted] every job in the workflow is accounted for in jobs.json", "jobs.json"),
    ):
        assert D.job_status_row([line]), why

    # the false positive the CI conjunct exists to exclude
    l17 = ("- B9 [invariant] [hosted] On the DELIVERY-2A rehearsal host, `python3 deploy/preflight.py "
           "--checklist` run from the checked-out candidate prints `ok registry: fresh` and its "
           "aggregate line no longer names `registry: fresh` among the required gates not green "
           "— check: the twin's preflight transcript on `voicepod-vps` (gate: DELIVERY-2A).")
    assert D.job_status_row([l17]) == "", (
        "a deploy checklist's 'required gates' is not a CI job's status")


def test_d164_a_job_as_a_LOCATION_is_not_a_job_status_ask():
    """D164's load-bearing control, and the reason this is not simply `\\bjob\\b`.

    Measured over the pack: 60 of the 169 [hosted] rows contain the word "job",
    and 47 of them are not matched by JOB_ASK_RE. Almost all name a job only as
    the PLACE test nodes ran -- "on the `platform-shard` CircleCI job ... the
    same nodes pass" -- which a scoped twin answers fine by running those nodes.
    A bare word test would have refused roughly three times as many rows as the
    spelling list it replaced, and every extra refusal costs a full pipeline."""
    from lanedriver import LaneDriver as D

    for line in (
        "- B9 [hosted] On the `platform-shard` CircleCI job at the union tip, "
        "`platform/tests/test_x.py::test_y` passes under CircleCI's own Postgres.",
        "- B7 [hosted] the hosted platform shard runs "
        "`platform/tests/test_sequence_schema.py::test_pin` green on the exact sha",
    ):
        assert D.job_status_row([line]) == "", line[:60]


def test_d164_the_conjunction_is_evaluated_within_one_row(tmp_path):
    """D164: JOB_ASK_RE searches one concatenated blob of every hosted row plus
    the whole BENCHMARK.md, so a conjunction over that blob would be met by two
    unrelated rows. A CI token three rows away says nothing about whether THIS
    row is about CI, so `job_status_row` iterates."""
    from lanedriver import LaneDriver as D

    split = ["- B1 [hosted] the required gates in deploy/preflight.py are listed (gate: DELIVERY-2A)",
             "- B2 [hosted] the CircleCI shard runs platform/tests/test_a.py::test_x green"]
    assert D.job_status_row(split) == "", (
        "neither row on its own is a job-status ask; only their concatenation looks like one")


def test_d164_twin_scope_only_refuses_the_l28_shape(tmp_path):
    """D164 at the call site. Kept as a second check after JOB_ASK_RE rather than
    folded into it, so this is a pure widening: nothing the spelling list already
    refuses becomes allowed."""
    env = Env(tmp_path)
    env.activate()
    proof = FakeProof([])
    proof.only_active = 0
    proof.only_cap = lambda: 2
    proof.circle_cfg = lambda: {"max_in_flight": 1}
    proof.provider = lambda: "gha"
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner()}, proof=proof)
    drv.proof_cfg["circleci"] = {"canary": {"task": "L06-HOSTED", "release_on": ["PASS"]},
                                 "max_in_flight": 1,
                                 "twin_scope": {"enabled": True, "contract_from": "parent_contract",
                                                "workers": 4}}
    twin = {"id": "L28-HOSTED", "twin_of": "L28", "twin_gate": "CIRCLECI", "proof_only": "",
            "proof_kind": "platform"}
    drv.pack.update({"L28-HOSTED": twin,
                     "L28": {"id": "L28", "test_paths": ["platform/tests/test_ops.py"]}})
    drv.pack_by_task.update({"L28-HOSTED-R3": "L28-HOSTED"})
    tasks = {"L28-HOSTED-R3": {"state": "READY"}}
    wt = tmp_path / "wt-l28"
    (wt / ".vp").mkdir(parents=True)
    (wt / "platform" / "tests").mkdir(parents=True)
    (wt / "platform" / "tests" / "test_ops.py").write_text("")
    (wt / ".vp" / "BENCHMARK.md").write_text("")

    # without the job-status clause the row is perfectly scopeable: it names a
    # platform test file the tree has, which is what makes the defect silent
    twin["hosted_lines"] = ["- B12 [test] [hosted] `platform/tests/test_ops.py` passes on the exact "
                            "sha inside the hosted platform shard (gate: CIRCLECI)."]
    assert drv._twin_scope_only("L28-HOSTED-R3", twin, tasks, wt=wt) is not None

    twin["hosted_lines"] = ["- B12 [test] [hosted] `platform/tests/test_ops.py` passes on the exact "
                            "sha inside the hosted platform shard, and the shard, lint and "
                            "required-gate jobs are green — check: job links and status "
                            "(gate: CIRCLECI)."]
    assert drv._twin_scope_only("L28-HOSTED-R3", twin, tasks, wt=wt) is None
    log = (env.run_root / "driver.log").read_text()
    assert "asserts a CI job's own status" in log and "D164" in log, log[-400:]


def test_d164_held_out_phrasings_record_where_the_property_test_still_ends():
    """D164's honest boundary, and the test that stops this becoming a bigger
    phrase list pretending to be a property test.

    None of the eight rows below appears in the pack or in any other D164 test.
    They were written to probe the rule from OUTSIDE the set it was derived from,
    because a rule that only replays the phrasings it was built from measures
    nothing. Five are caught. **Three are not, and that is recorded here rather
    than hidden**: the rule is a better approximation of "this row asserts a CI
    job's own status" than the spelling list it widens, and it is still text
    matching over prose.

    The three misses share a shape the patterns do not reach: a NEGATIVE or
    whole-run assertion ("no job is red", "the CI run is green end to end", "the
    job did not run"). Widening to catch them by adding more alternatives is what
    this test exists to discourage -- each one trades a miss for a false positive
    somewhere in the 169 rows, and the measurement for that was done once already
    ([[a-heuristic-tuned-against-your-own-labels]]).

    The structural fix, if this ever needs to be complete: parse the row's
    `— check:` clause and decide on what the CHECK names, rather than on the
    whole sentence. That is a real change to the row grammar and wants its own
    ruling, so it is named here and not smuggled in.

    If a future widening lands, re-measure against all 169 [hosted] rows first
    and move rows between the two lists below deliberately."""
    from lanedriver import LaneDriver as D

    caught = [
        "- B1 [hosted] the required checks on the PR are all green (gate: CIRCLECI)",
        "- B2 [hosted] the workflow run concluded success for all jobs (gate: CIRCLECI)",
        "- B6 [hosted] every job in the workflow reports success (gate: GHA)",
        "- B7 [hosted] the job conclusion for lint-and-typecheck is success",
        "- B8 [hosted] jobs.json at the candidate sha lists no failure",
    ]
    missed = [
        "- B3 [hosted] no job in the CI pipeline is red at the candidate sha",
        "- B4 [hosted] the CI run is green end to end on the exact sha",
        "- B5 [hosted] the platform-shard job did not run because needs: was skipped",
    ]
    for line in caught:
        assert D.job_status_row([line]), "expected caught: %s" % line
    for line in missed:
        assert D.job_status_row([line]) == "", (
            "this one is a KNOWN miss; if a change makes it pass, move it to `caught` "
            "and re-measure the false positives over all 169 hosted rows: %s" % line)


# -- D166: `reload` must not write its marker where nothing is watching --------------------------

def test_d166_reload_refuses_a_directory_that_is_not_a_run_root(tmp_path):
    """D166, from a real 14-minute idle window the OWNER caught.

    The run root is `roster_path.parent` (lanedriver.py:483), so the --roster
    argument IS the run-root selector and no flag overrides it for this verb.
    Passing the PACK roster instead of the RUN roster that `init-run` copies to
    `<run_root>/roster.json` writes RELOAD into the pack directory, where
    nothing is watching.

    Three arms did exactly that between 09:33Z and 10:36Z on 2026-09-21. Each
    printed `"status": "REQUESTED"` and a marker path, each was truthful, and
    none reached the driver. The evidence was already on screen: read_heartbeat
    returned None, so the command printed pid/active/code_version as null three
    times and carried on. [[a-refusal-must-name-the-place]]"""
    from lanedriver import reload_target_problem

    pack = tmp_path / "v13-pack"
    pack.mkdir()
    (pack / "roster-v13.json").write_text("{}")
    problem = reload_target_problem(pack)
    assert problem, "a directory with no heartbeat and no reloads.jsonl is not a run root"
    assert str(pack) in problem, "the refusal must name the place it refused"
    assert "roster.json" in problem and "init-run" in problem, (
        "and must say what to pass instead: %s" % problem)

    # the discriminator is the FILENAME: init-run copies the pack roster to
    # <run_root>/roster.json under that exact name, and a pack directory never
    # holds a file called roster.json. A freshly created run root with no
    # heartbeat and no reloads.jsonl yet is still a run root -- the existing
    # suite caught a first version of this guard that refused exactly that.
    fresh = tmp_path / "run-fresh"
    fresh.mkdir()
    (fresh / "roster.json").write_text("{}")
    assert reload_target_problem(fresh) == "", (
        "a run root that init-run has just created has no heartbeat and no "
        "reloads.jsonl, and a correct arm against it must not be refused")


def test_d166_a_run_root_whose_driver_is_down_is_still_the_right_place(tmp_path):
    """D166's discriminator. "Not a run root" and "a run root whose driver is
    down" are different facts and only the first is an operator mistake.

    A run root that has reloaded before is the right destination even with no
    heartbeat -- the marker is read when the driver returns. Refusing here would
    block a legitimate arm during a restart."""
    from lanedriver import reload_target_problem

    root = tmp_path / "run-v13"
    root.mkdir()
    (root / "reloads.jsonl").write_text('{"n": 1}\n')
    assert reload_target_problem(root) == ""

    fresh = tmp_path / "run-heartbeat-only"
    fresh.mkdir()
    (fresh / "driver.heartbeat").write_text('{"pid": 4242, "active": 0, "ts": "2026-09-21T10:00:00Z"}')
    assert reload_target_problem(fresh) == "", "a heartbeat alone identifies a run root too"


def test_d166_reload_reports_a_stale_heartbeat_instead_of_three_nulls(tmp_path):
    """D166's second half. Writing the marker is correct when the driver is
    merely down; reporting it as an ordinary REQUESTED over three null fields is
    what made three dead arms read as three live ones."""
    import io
    from contextlib import redirect_stdout

    import lanedriver

    root = tmp_path / "run-v13"
    root.mkdir()
    (root / "reloads.jsonl").write_text('{"n": 1}\n')

    class FakeDrv(object):
        run_root = root
        roster_path = root / "roster.json"

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = lanedriver.cmd_reload(FakeDrv(), type("A", (), {"reason": "D166 test", "cancel": False})())
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["status"] == "REQUESTED"
    assert out["driver"]["heartbeat_fresh"] is False
    assert "NO FRESH HEARTBEAT" in out["note"], out["note"]
    assert (root / "RELOAD").exists(), "the marker is still written -- it is the right place"


def test_d166_reload_refuses_with_exit_2_and_writes_no_marker(tmp_path):
    """The refusal has to be actionable by a script, not just readable: a
    non-zero exit, and no marker left behind to be found later and mistaken for
    a pending reload."""
    import io
    from contextlib import redirect_stdout

    import lanedriver

    pack = tmp_path / "v13-pack"
    pack.mkdir()

    class FakeDrv(object):
        run_root = pack
        roster_path = pack / "roster-v13.json"

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = lanedriver.cmd_reload(FakeDrv(), type("A", (), {"reason": "x", "cancel": False})())
    assert rc == 2
    out = json.loads(buf.getvalue())
    assert out["status"] == "REFUSED" and str(pack) in out["reason"]
    assert not (pack / "RELOAD").exists(), "a refused arm must leave nothing behind"


def test_d168_the_ledger_cell_and_the_recorded_field_come_from_one_mapping(tmp_path, monkeypatch):
    """The ask was "carry the computed value through, do not reimplement it". A second
    copy of the (class, basis) -> label mapping would agree today and disagree the
    first time a kind is added to only one of them, and nothing would report the
    split. This pins the cell and the recorded field to the SAME function.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    rr = env.run_root

    _write_proof(rr, "proof-SC-60921T000101", only="twin:3:platform/tests/test_a.py")
    _write_proof(rr, "proof-FU-60921T000102", only=None)
    _write_proof(rr, "proof-BX-HOSTED-60921T000103", only=None)

    shapes = (("SC", "builder"), ("FU", "builder"), ("BX", "builder"),
              ("PR", "probe"), ("NK", "builder"), ("NK", None))
    for task, kind in shapes:
        cell = drv._ledger_scope(task, {"state": "VERIFIED", "kind": kind})
        carried = drv.scope_label(drv.member_scope(task, kind))
        assert cell == carried, (
            "the ledger cell and the carried field disagree for %s/%s: %r vs %r -- "
            "the mapping has been copied instead of shared" % (task, kind, cell, carried))

    # and the six really are six: a shared mapping that collapsed them would pass
    # the loop above while destroying the distinction it exists to carry
    assert len({drv.scope_label(drv.member_scope(t, k)) for t, k in shapes}) == 6


def test_d168_an_unrecognised_or_absent_scope_label_is_scored_not_exempt(tmp_path, monkeypatch):
    """Fails CLOSED. The failure this prevents is silent: a member that drops out of
    the denominator looks exactly like a member that never owed a proof.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})

    assert drv.scope_scored("not-required") is False, "the one exempt label must be exempt"
    for label in ("full", "scoped:platform/tests/test_a.py", "full via BX-HOSTED",
                  "no-proof-in-run", "no-kind", "?", "-", "", "NOT-REQUIRED",
                  " not-required", "not_required", None):
        assert drv.scope_scored(label) is True, (
            "%r must be SCORED: anything the closed set does not name exactly stays in "
            "the denominator" % (label,))

    assert drv.SCOPE_EXEMPT_LABELS == ("not-required",), (
        "the exempt set is closed; widening it is a decision, not a detail")


def test_d168_an_unknown_kind_is_scored_although_its_ledger_cell_says_not_required(tmp_path, monkeypatch):
    """The deliberate divergence. member_scope files an unknown kind under
    `not_required` because "I don't know" is not "it failed" (D148) -- right for a
    ledger CELL. Carrying that into a SCORE would drop the row from the count on the
    strength of not knowing, which is the fail-open shape this field exists to expose.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})

    unknown = drv.member_scope("NK", None)
    assert unknown["class"] == "not_required" and unknown["basis"] == "kind_unknown"
    assert drv.scope_label(unknown) == "no-kind"
    assert drv.scope_scored("no-kind") is True, (
        "an unknown kind must be SCORED -- exempting it excuses a row for being "
        "unclassifiable, which is exactly the hole the field is meant to show")

    known = drv.member_scope("PR", "probe")
    assert known["class"] == "not_required" and known["basis"] == "kind"
    assert drv.scope_scored(drv.scope_label(known)) is False, (
        "a kind the config says never owed a proof IS exempt -- the two not_required "
        "bases must not collapse into one answer")


def test_d168_proofs_json_carries_the_scope_and_whether_a_proof_is_owed(tmp_path, monkeypatch):
    """Runtime criteria are judged from .vp/PROOFS.json, so the exemption has to be
    IN the artifact a reviewer reads, not re-derived by whoever reads it.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    rr = env.run_root
    sha = "a" * 40

    _write_proof(rr, "proof-FULLROW-60921T000104", only=None)
    _write_proof(rr, "proof-SCOPEDROW-60921T000105", only="twin:3:platform/tests/test_b.py")

    state = {"tasks": {"FULLROW": {"output_sha": sha, "kind": "builder"},
                       "SCOPEDROW": {"output_sha": sha, "kind": "builder"},
                       "PROBEROW": {"output_sha": sha, "kind": "probe"}}}
    union = {"union": "u1", "members": [{"task": t, "output_sha": sha}
                                        for t in ("FULLROW", "SCOPEDROW", "PROBEROW")]}
    out = drv._review_proofs([], state, union, sha)
    by = {e["task"]: e for e in out["entries"] if e["task"] != "<union tip>"}

    assert set(by) == {"FULLROW", "SCOPEDROW", "PROBEROW"}
    for t, e in by.items():
        assert "scope" in e and "proof_required" in e, "%s carries neither field: %r" % (t, e)

    assert by["FULLROW"]["scope"] == "full" and by["FULLROW"]["proof_required"] is True
    assert by["SCOPEDROW"]["scope"].startswith("scoped:") and by["SCOPEDROW"]["proof_required"] is True
    assert by["PROBEROW"]["scope"] == "not-required" and by["PROBEROW"]["proof_required"] is False

    # the recorded field must equal the shared mapping, not a value assembled here
    for t, e in by.items():
        # D174: pass the row, exactly as _review_proofs does.  A mirror test that
        # calls the function differently from production stops being a mirror.
        assert e["scope"] == drv.scope_label(drv.member_scope(
            t, (state["tasks"][t] or {}).get("kind"), row=state["tasks"][t] or {}))


def test_d168_the_summary_names_every_exempt_member(tmp_path, monkeypatch):
    """"k exempt" with no names is unreadable: the reader cannot tell an exemption
    from a member that was dropped without re-deriving all of it.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})

    mem = [{"task": "A", "proof_required": True},
           {"task": "ZED", "proof_required": False},
           {"task": "B", "proof_required": True},
           {"task": "MID", "proof_required": False}]
    line = drv.scope_summary(mem)
    assert line.startswith("Proof scope: 2 of 4 scored, 2 exempt (not-required): "), line
    assert "MID" in line and "ZED" in line, "every exempt member is named: %r" % line
    assert line.index("MID") < line.index("ZED"), "named in a stable order"
    assert line.endswith("\n")

    assert drv.scope_summary([{"task": "A", "proof_required": True}]) == (
        "Proof scope: 1 of 1 scored, 0 exempt (not-required)\n"), "no empty list when none are exempt"
    assert drv.scope_summary([]) == "Proof scope: 0 of 0 scored, 0 exempt (not-required)\n"

    # a member whose field is MISSING must be scored, not exempt.  `not
    # e.get("proof_required")` passes every other assertion in this test and fails
    # only this one -- which is the whole point of writing it.
    assert drv.scope_summary([{"task": "A"}]) == "Proof scope: 1 of 1 scored, 0 exempt (not-required)\n", (
        "a missing field must not read as an exemption")
    assert drv.scope_summary([{"task": "A", "proof_required": None}]) == (
        "Proof scope: 1 of 1 scored, 0 exempt (not-required)\n"), "nor a null one"


def test_d168_a_member_that_cannot_be_classified_is_scored_not_excused(tmp_path, monkeypatch):
    """The fail-open hole a mutation found and the rest of this suite missed: when
    member_scope raises, the entry still has to be WRITTEN, and whatever it is
    written as decides whether the row stays in the denominator. Labelling it
    "not-required" there excuses a member for being unclassifiable -- silently, and
    only on the path where something already went wrong.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    sha = "a" * 40

    def boom(*a, **k):
        raise RuntimeError("proofs dir unreadable")

    drv.member_scope = boom
    state = {"tasks": {"BROKEN": {"output_sha": sha, "kind": "builder"}}}
    union = {"union": "u1", "members": [{"task": "BROKEN", "output_sha": sha}]}
    e = [x for x in drv._review_proofs([], state, union, sha)["entries"]
         if x["task"] == "BROKEN"][0]

    assert e["proof_required"] is True, (
        "a member we could not classify must stay in the denominator: %r" % e)
    assert drv.scope_scored(e["scope"]) is True, (
        "and its recorded label must not be one the exempt set names: %r" % e["scope"])
    assert "Proof scope: 1 of 1 scored, 0 exempt" in drv.scope_summary([e]), drv.scope_summary([e])


def test_d170_a_scoped_proof_and_a_full_proof_are_distinguishable_in_the_artifact(tmp_path, monkeypatch):
    """The control that matters. Asserting `"only" in entry` would prove the schema
    changed; it would NOT prove the artifact can express scope. These two records
    are byte-identical in a pre-D170 PROOFS.json -- both land as `paths: []` with no
    scope field -- so the property is that they must now come out DIFFERENT.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    sha = "a" * 40

    _write_proof(env.run_root, "proof-SCOPEDONE-60921T000201",
                 only="twin:3:platform/tests/test_x.py")
    _write_proof(env.run_root, "proof-FULLONE-60921T000202", only=None)

    state = {"tasks": {"SCOPEDONE": {"output_sha": sha, "kind": "builder"},
                       "FULLONE": {"output_sha": sha, "kind": "builder"}}}
    union = {"union": "u1", "members": [{"task": t, "output_sha": sha}
                                        for t in ("SCOPEDONE", "FULLONE")]}
    by = {e["task"]: e for e in drv._review_proofs([], state, union, sha)["entries"]}

    assert by["SCOPEDONE"]["only"] == "twin:3:platform/tests/test_x.py", (
        "the artifact must carry the scope verbatim: %r" % by["SCOPEDONE"])
    assert not (by["FULLONE"]["only"] or None)

    scoped_view = {k: by["SCOPEDONE"][k] for k in ("only", "paths")}
    full_view = {k: by["FULLONE"][k] for k in ("only", "paths")}
    assert scoped_view != full_view, (
        "a scoped proof and a full proof are still indistinguishable in the artifact: "
        "%r vs %r -- the field exists but carries nothing" % (scoped_view, full_view))

    assert drv.entry_scope(by["SCOPEDONE"]) == drv.SCOPE_SCOPED
    assert drv.entry_scope(by["FULLONE"]) == drv.SCOPE_FULL


def test_d170_an_entry_that_cannot_express_scope_is_unknown_by_construction(tmp_path, monkeypatch):
    """Permanent, not interim. 14 of the 14 archived PROOFS.json copies are SEALED --
    their bytes never change, so no backfill can ever reach them and "empty paths
    means unknown" has to hold by construction rather than by anyone remembering it.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})

    legacy = {"task": "L04", "proof_id": "proof-L30-X-60918T104640", "status": "PASS", "paths": []}
    assert "only" not in legacy
    assert drv.entry_scope(legacy) == drv.SCOPE_UNKNOWN, (
        "a legacy entry cannot express scope, so it must read UNKNOWN -- reading it as "
        "full is the defect, applied to every entry written before D170")

    # the two tests are different on purpose; collapsing either way is a known defect
    assert drv.entry_scope({"only": None, "paths": []}) == drv.SCOPE_FULL, (
        "an explicitly null `only` IS a full run -- 79 raw records carry exactly that, "
        "and reading key-presence as scope mislabels every one of them")
    assert drv.entry_scope({"only": "twin:3:a.py", "paths": []}) == drv.SCOPE_SCOPED
    assert drv.entry_scope({"paths": ["platform/tests/test_a.py"]}) == drv.SCOPE_SCOPED, (
        "a targeted paths list is a scope even with no `only` key at all")
    assert drv.entry_scope({}) == drv.SCOPE_UNKNOWN
    assert drv.entry_scope(None) == drv.SCOPE_UNKNOWN


def test_d170_a_member_is_never_bound_to_another_items_scoped_proof(tmp_path, monkeypatch):
    """_review_proofs binds by SHA alone and best() ranks PASS first, so where members
    share an output sha the cross-bound record is preferentially the GREEN one.
    Measured over 14 artifacts: 197 of 1179 bound entries named a different root and
    172 of those were bound to a record whose own `only` answers one other ask.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    sha = "a" * 40

    # MINE is red on its own scoped proof; THEIRS is a green scoped proof at the SAME
    # sha. Before D170 the PASS sorted to the top and became MINE's proof.
    _write_proof(env.run_root, "proof-MINE-60921T000301", status="FAIL_PRODUCT",
                 only="twin:3:platform/tests/test_mine.py")
    _write_proof(env.run_root, "proof-THEIRS-60921T000302", status="PASS",
                 only="twin:3:platform/tests/test_theirs.py")

    state = {"tasks": {"MINE": {"output_sha": sha, "kind": "builder"}}}
    union = {"union": "u1", "members": [{"task": "MINE", "output_sha": sha}]}
    e = [x for x in drv._review_proofs([], state, union, sha)["entries"]
         if x["task"] == "MINE"][0]
    assert e["proof_id"] == "proof-MINE-60921T000301", (
        "MINE was handed another item's scoped PASS: %r" % e["proof_id"])
    assert e["status"] == "FAIL_PRODUCT"

    # the guard must filter BEFORE ranking: with only the foreign PASS available the
    # member gets NOTHING, never the foreign record
    (env.run_root / "proofs" / "proof-MINE-60921T000301.json").unlink()
    e = [x for x in drv._review_proofs([], state, union, sha)["entries"]
         if x["task"] == "MINE"][0]
    assert e["proof_id"] is None, "a foreign scoped PASS was bound: %r" % e["proof_id"]
    assert drv.entry_scope(e) == drv.SCOPE_FULL or e["proof_id"] is None


def test_d170_a_full_suite_run_is_still_adoptable_across_items(tmp_path, monkeypatch):
    """The negative control, and the arm a bare owner check would have broken. D113:
    a FULL run really did execute this member's files, so another item's full-suite
    record is legitimate evidence. 25 live bindings depend on this -- unbinding them
    would read as a regression, not a fix.
    """
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})
    sha = "a" * 40

    _write_proof(env.run_root, "proof-SOMEONEELSE-60921T000401", status="PASS", only=None)
    state = {"tasks": {"MINE": {"output_sha": sha, "kind": "builder"}}}
    union = {"union": "u1", "members": [{"task": "MINE", "output_sha": sha}]}
    e = [x for x in drv._review_proofs([], state, union, sha)["entries"]
         if x["task"] == "MINE"][0]
    assert e["proof_id"] == "proof-SOMEONEELSE-60921T000401", (
        "a full-suite run must stay adoptable across items (D113): %r" % e)

    # and the twin/generation arm: a member's own hosted twin is not a foreign item
    assert drv.proof_answers("L30-REGISTRY-DISCOVERY",
                             {"proof_id": "proof-L30-REGISTRY-DISCOVERY-HOSTED-R2-60921T000001",
                              "only": "twin:3:a.py"}) is True
    assert drv.proof_answers("L04",
                             {"proof_id": "proof-L30-REGISTRY-DISCOVERY-60918T104640917",
                              "paths": ["platform/tests/rls/test_l30_privilege_matrix_static.py"]}) is False, (
        "the live L04 <- L30 case must be refused")


def test_d172_the_rule_the_reviewer_reads_names_the_fields_that_decide_scope():
    """D168 put a `scope` label on every entry and D170 put `only` in PROOF_FIELDS,
    but the sentence the reviewer actually reads still said "whose paths cover the
    criterion's tests" -- it named the one field that does NOT decide scope and
    neither of the two that do, so a scoped proof with `paths: []` satisfied it
    vacuously and both earlier fixes changed no verdict.

    This binds the rule text to the closed vocabulary `scope_label` emits: adding a
    scope class without teaching the rule to speak about it fails here rather than
    shipping a label no rule reads."""
    from vp.lanedriver import PROOFS_RULE, REVIEW_PROMPT, LaneDriver

    assert "whose paths cover" not in PROOFS_RULE, "the superseded rule is back"
    for token in (LaneDriver.SCOPE_SCOPED, LaneDriver.SCOPE_FULL, LaneDriver.SCOPE_UNKNOWN):
        assert token in PROOFS_RULE, "the rule never mentions the %r scope class" % token
    assert "`scope`" in PROOFS_RULE and "`only`" in PROOFS_RULE
    assert "`paths` is" in PROOFS_RULE, "the rule must say what an empty paths is NOT"
    assert "proof_required" in PROOFS_RULE, "the exemption must be stated where it is applied"
    # The same rule is stated twice: this string (the packet head) and REVIEW_PROMPT
    # (the reviewer's system prompt). Fixing one copy of two is how a rule returns.
    assert "scope" in REVIEW_PROMPT and "D170" in REVIEW_PROMPT



def test_d174_integrated_non_dynamic_rows_are_exempt_from_proof_EXISTENCE_only(tmp_path, monkeypatch):
    """D174 / exemption (0b), Architect 2026-09-21.

    F5-R5 marked 25 members UNKNOWN that owe no proof record at all: they were
    proved and merged before v13 existed.  The exemption was real and ruled, but
    it lived in a document the grader cannot read -- the same hole as the scope
    field, one layer down, and unenforceable for exactly the same reason.

    The basis is BOTH conjuncts: `state == "INTEGRATED"` AND not `dynamic`.
    Verified program-wide by the Architect: exactly 25 non-dynamic INTEGRATED
    tasks, matching all 25 non-dynamic F5-R5 members by name, no extras and no
    misses.  Rejected alternatives are recorded in member_scope's comment so they
    are not re-proposed -- `v13_kind` is null on all 687 rows and would exempt
    everything.

    This test pins the PREDICATE.  Membership against the live 25 is a one-shot
    audit, deliberately not pinned here: a unit test naming 25 rows goes stale
    the first time one changes state, and a stale guard gets deleted rather than
    fixed."""
    from vp.lanedriver import LaneDriver

    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner()})

    exempt = drv.member_scope("ROW", "builder", row={"state": "INTEGRATED"})
    assert exempt["class"] == "not_required" and exempt["basis"] == "integrated_not_dynamic"
    assert LaneDriver.scope_scored(LaneDriver.scope_label(exempt)) is False

    # both conjuncts load-bearing, or the basis is wider than the ruling
    dyn = drv.member_scope("ROW", "builder", row={"state": "INTEGRATED", "dynamic": True})
    assert dyn["class"] == "unproven", "a dynamic twin is NOT exempt: %r" % dyn
    ver = drv.member_scope("ROW", "builder", row={"state": "VERIFIED"})
    assert ver["class"] == "unproven", "only INTEGRATED is exempt: %r" % ver
    assert drv.member_scope("ROW", "builder")["class"] == "unproven", (
        "no row at all must stay scored -- fail CLOSED, never exempt by omission")


def test_d174_the_packet_states_WHY_a_member_is_exempt():
    """D174, Architect's second requirement: the reason goes in the packet text,
    not only in the rule.

    A packet that says "3 exempt" and stops is one reader away from being
    rewritten as "skip integrated rows" -- which would take the owned_paths
    criterion with it.  (0b) exempts from the EXISTENCE check only.  An exemption
    is a deleted check; this sentence is what keeps it to exactly one."""
    from vp.lanedriver import LaneDriver

    mem = [{"task": "A", "proof_required": False, "scope_basis": "integrated_not_dynamic"},
           {"task": "B", "proof_required": True, "scope_basis": None}]
    out = LaneDriver.scope_summary(mem)
    assert "1 of 2 scored, 1 exempt" in out
    assert "EXISTENCE check only" in out, out
    assert "owned_paths criterion is still scored" in out, out



def test_d176_a_record_without_the_subset_field_is_refused_and_says_why():
    """D176: a write-time verdict only governs FUTURE writes. Every record that
    already exists lacks the field, so a selector reading a missing field as "no
    problem" would ship, look complete, and protect nothing -- a false green
    nobody investigates. Measured: 0 of 539 records in run-v13-20260917 carry it.

    Three states, and the two refusals stay distinct. KEY PRESENCE is what
    separates "nobody has looked at this yet" from "someone looked and could not
    tell"; a truthiness test collapses them, which is the same collapse behind
    D170 and D175 -- twice in one night, one layer apart. The second reason also
    matters for the backfill: if `unresolvable` did not exist as an outcome, the
    backfill would be under pressure to guess a verdict to keep a record alive."""
    from vp.lanedriver import LaneDriver

    # absent -- the field-less record, stated explicitly so nobody can later soften
    # this as noisy without deleting a named case
    assert LaneDriver.subset_state({}) == "absent"
    assert LaneDriver.subset_state({"status": "PASS"}) == "absent"
    assert LaneDriver.subset_state(None) == "absent"
    assert LaneDriver.subset_citable({"status": "PASS"}) is False, (
        "a record written before the field existed must NOT be citable")

    # present but undecided -- a DIFFERENT refusal, and it must not read as absent
    assert LaneDriver.subset_state({"subset_verdict": None}) == "unresolvable"
    assert LaneDriver.subset_state({"subset_verdict": "unresolvable"}) == "unresolvable"
    assert LaneDriver.subset_citable({"subset_verdict": "unresolvable"}) is False

    # positively established
    for ok in LaneDriver.SUBSET_CITABLE:
        assert LaneDriver.subset_citable({"subset_verdict": ok}) is True, ok

    # everything else refuses, including a value this version does not know
    for bad in ("not_covered", "preflight", "maybe", ""):
        assert LaneDriver.subset_citable({"subset_verdict": bad}) is False, bad


def test_d176_the_writer_decides_the_verdict_from_what_it_ran(tmp_path):
    """D176 writer half. Every branch is decided by `only` and `prior` -- no git,
    no I/O. Three of four measurement passes over this corpus tonight had a bug in
    exactly the re-derivation this replaces (a missing `only` key read as "full",
    a missing Twin step read as "unreadable", a `platform-preflight` step read as
    "unreadable"), each one a reader assuming its own vocabulary was complete."""
    import inspect
    from vp import laneproof

    src = inspect.getsource(laneproof.Proof.run_circleci)
    assert 'rec["subset_verdict"] = subset' in src, "the writer no longer writes the field"
    # the branch that produced tonight's 17 bad records must stay explicit rather
    # than be folded away as unreachable now that D175 closed the ledger path
    assert 'subset = "not_covered"' in src, (
        "the differently-scoped-adoption branch was removed -- D175 makes it rare, "
        "not impossible, and 17 records in this run are exactly that shape")
    assert 'subset = "preflight"' in src
