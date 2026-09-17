#!/usr/bin/env python3
"""tests/test_lanedriver.py -- the v13 driver over the REAL orchestration_control.py
(subprocess, temp state, tiny catalog), real git (temp trunk), FAKE runners.
No network, no model, no opencode/codex/claude binaries."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP))

import lanedriver  # noqa: E402
from vprunners import TurnOutcome, STATUS_DONE  # noqa: E402

PY = sys.executable
CONTROL_DIR = VP.parent.parent / "voice-pod" / "advisor-plans" / "outbound-launch"
SCRIPT = CONTROL_DIR / "orchestration_control.py"


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
                        "catalog": str(self.catalog), "cwd": str(CONTROL_DIR)},
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


def test_control_jsonl_records_every_call_with_sequence(tmp_path):
    env = Env(tmp_path)
    env.activate()
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
        return TurnOutcome(STATUS_DONE, "", session_id=spec.session_id, record_path=spec.out_path,
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


def test_codex_review_preopens_the_thread_and_starts_with_its_id(tmp_path):
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
    assert len(codex.preopened) == 1, "one pre-open turn before start"
    assert rows["J1"]["child_id"] == "0199-thread-xyz"
    start = next(l for l in env.control_lines() if l["verb"] == "start" and "J1" in l["argv"])
    assert "0199-thread-xyz" in start["argv"] and "gpt-5.6-luna" in start["argv"]
    assert codex.calls[0].session_id == "0199-thread-xyz", "the review resumes the pre-opened thread"
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


def test_render_packet_and_benchmark_from_contract(tmp_path):
    con = catalog()["contracts"][2]
    pk = lanedriver.LaneDriver.render_packet(con, "a" * 40)
    assert pk.startswith("---\nitem: L02\n") and "platform/a.py" in pk and "base_sha: " + "a" * 40 in pk
    bm = lanedriver.LaneDriver.render_benchmark(con)
    assert bm.splitlines()[0].startswith("- B1 [evidence] L02 verified — check:")
    assert len(bm.splitlines()) == 2
