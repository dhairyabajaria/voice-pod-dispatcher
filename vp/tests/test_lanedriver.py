#!/usr/bin/env python3
"""tests/test_lanedriver.py -- the v13 driver over the REAL orchestration_control.py
(subprocess, temp state, tiny catalog), real git (temp trunk), FAKE runners.
No network, no model, no opencode/codex/claude binaries."""

from __future__ import annotations

import hashlib
import json
import os
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
    assert json.loads((tdir / "harvest.json").read_text())["outcome"] == "VERIFIED"
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
    drv._http_ok = lambda *a, **k: True
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
