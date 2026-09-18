#!/usr/bin/env python3
"""tests/test_vppack.py -- pack §4(c): packet -> scheduler-task binding (D9),
joint closers (§6b), owner gates, L42 rewire, dispatch records.  Pure vppack
tests plus one driver cycle over a tiny pack with fake runners."""

from __future__ import annotations

import json
import pytest
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP))
sys.path.insert(0, str(HERE))

import vppack  # noqa: E402
from test_lanedriver import Env, FakeRunner, by_role, findings, git, result_ok, settle  # noqa: E402

FM = """---
item: {id}
title: {title}
group: 1
base_sha: {base}
depends_on: [{deps}]
releases: []
critical: false
owned_files:
  - control/evidence/{id}/v13/REGRADE.md
forbidden_files: []
test_paths:
  - platform/a.py
proof_kind: platform
max_rounds: 2
v13_kind: {kind}
scheduler_task: {task}
closes: [{closes}]
template: {template}
runner_role: {role}
hosted_owed: false
owner_gate: {gate}
---

## Goal

{body}

## Steps

1. do it
"""

BM = "- B1 [evidence] [box] {id} regraded — check: control/evidence/{id}/v13/REGRADE.md\n"


def packet(pack_dir, id_, task, kind="regrade", template="none", role="grader", closes=(), deps=(),
           gate="none", body="regrade L00", base="a" * 40):
    d = pack_dir / id_
    d.mkdir(parents=True)
    (d / "PACKET.md").write_text(FM.format(id=id_, title="packet %s" % id_, base=base, deps=", ".join(deps),
                                           kind=kind, task=task, closes=", ".join(closes), template=template,
                                           role=role, gate=gate, body=body))
    (d / "BENCHMARK.md").write_text(BM.format(id=id_))


def tiny_pack(tmp_path):
    pd = tmp_path / "pack"
    packet(pd, "L02", "L02", kind="repair", role="builder", closes=["JX"])            # direct: L02 is unfinished
    packet(pd, "P-REGRADE-L00", "L00", closes=["JX"])                                 # dynamic once L00 finishes
    packet(pd, "P-REGRADE-2", "NEW:EVIDENCE_RECOVERY", template="EVIDENCE_RECOVERY", role="probe",
           closes=["JX"], deps=["P-REGRADE-L00"], body="second closer of JX for L00")
    packet(pd, "P-GATED", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder",
           gate="DELIVERY-1", body="gated behind L00")
    packet(pd, "L04", "L04", kind="chain", role="probe", deps=["P-REGRADE-L00"])       # rewired direct task
    return pd


def tasks_fixture():
    return {"L00": {"state": "VERIFIED"}, "L02": {"state": "WAITING_DEPENDENCY"},
            "L04": {"state": "WAITING_DEPENDENCY", "depends_on": ["L00"]},
            "JX": {"state": "REPAIR_REQUIRED", "parent_contract_id": "L00", "kind": "builder"}}


def test_bind_modes_and_ids(tmp_path):
    pack, lint = vppack.load_pack(tiny_pack(tmp_path))
    assert lint == [] and len(pack) == 5
    t = tasks_fixture()
    assert vppack.bind(pack["L02"], t) == ("direct", "L02")
    assert vppack.bind(pack["P-REGRADE-L00"], t) == ("dynamic", "P-REGRADE-L00", "EVIDENCE_RECOVERY")
    assert vppack.bind(pack["P-REGRADE-2"], t) == ("dynamic", "P-REGRADE-2", "EVIDENCE_RECOVERY")
    assert vppack.bind(pack["L04"], t) == ("direct", "L04")
    # a packet whose id equals its finished task runs as <X>-V13; chain without template holds
    t["L02"]["state"] = "INTEGRATED"
    assert vppack.bind(pack["L02"], t) == ("dynamic", "L02-V13", "REPAIR")
    t["L04"]["state"] = "BLOCKED"
    assert vppack.bind(pack["L04"], t)[0] == "hold"
    assert vppack.bind(pack["L02"], {})[0] == "hold"
    assert vppack.topo_order(pack).index("P-REGRADE-L00") < vppack.topo_order(pack).index("P-REGRADE-2")


def test_parent_dependencies_parameters_and_closure_plan(tmp_path):
    pack, _ = vppack.load_pack(tiny_pack(tmp_path))
    t = tasks_fixture()
    assert vppack.parent_for(pack["P-REGRADE-L00"], t, pack) == ("L00", "scheduler_task")
    assert vppack.parent_for(pack["P-REGRADE-2"], t, pack) == ("L00", "depends_on:P-REGRADE-L00")
    assert vppack.parent_for(pack["P-GATED"], t, pack) == ("L00", "body-cite")
    assert vppack.dependency_tasks(pack["P-REGRADE-2"], pack, t) is None, "dependency not instantiated yet"
    # D23: only an OWNER-GATED dependency packet whose own row is accepted is
    # satisfied by that row (L34-ACK-DEDUP behind L34's DELIVERY-4 regrade)
    assert vppack.dependency_tasks(pack["P-REGRADE-2"], pack, t, gated={"P-REGRADE-L00"}) == ["L00"]
    t["P-REGRADE-L00"] = {"state": "READY"}
    assert vppack.dependency_tasks(pack["P-REGRADE-2"], pack, t) == ["P-REGRADE-L00"]
    assert vppack.dependency_tasks(pack["L04"], pack, t) == ["P-REGRADE-L00"]
    params = vppack.parameters_for(pack["P-REGRADE-2"], "EVIDENCE_RECOVERY", "L00")
    assert params["parent_contract_id"] == "L00" and params["candidate_sha"] == "a" * 40
    assert params["evidence_paths"] == ["control/evidence/P-REGRADE-2/v13/REGRADE.md"] and params["closes"] == ["JX"]
    assert vppack.parameters_for(pack["P-GATED"], "JUNIOR_REVIEW", "L00", candidate={}) is None
    rv = vppack.parameters_for(pack["P-GATED"], "JUNIOR_REVIEW", "L00", candidate={"sha": "s", "tree": "t"},
                               covered=["L06"])
    assert (rv["candidate_sha"], rv["tree_sha"], rv["covered_rows"], rv["criteria"]) == ("s", "t", ["L06"], "catalog")
    # §6b: three closers of JX; the plan retires only when the OTHER two are accepted
    assert vppack.closers_of("JX", pack) == ["L02", "P-REGRADE-2", "P-REGRADE-L00"]
    plan = vppack.closure_plan(pack["P-REGRADE-L00"], pack, t)
    assert plan["retire"] == [] and plan["pending"] == {"JX": ["L02", "P-REGRADE-2"]}
    t["L02"]["state"] = "VERIFIED"
    t["P-REGRADE-2"] = {"state": "VERIFIED"}
    bindings = {"L02": "L02", "P-REGRADE-2": "P-REGRADE-2", "P-REGRADE-L00": "P-REGRADE-L00"}
    assert vppack.closure_plan(pack["P-REGRADE-L00"], pack, t)["retire"] == [], \
        "without the driver's binding memory a finished direct packet looks unbound"
    assert vppack.closure_plan(pack["P-REGRADE-L00"], pack, t, bindings)["retire"] == ["JX"]
    # a packet that closes its own scheduler_task promotes instead of retiring
    own = dict(pack["L02"], closes=["L02"], scheduler_task="L02")
    assert vppack.closure_plan(own, {"L02": own}, {"L02": {"state": "REPAIR_REQUIRED"}})["promote"] == ["L02"]
    assert vppack.closure_plan(own, {"L02": own}, {"L02": {"state": "BLOCKED"}})["retire"] == ["L02"]
    assert vppack.owner_gate_open(pack["P-GATED"], {}) is False
    assert vppack.owner_gate_open(pack["P-GATED"], {"owner_gates": {"DELIVERY-1": True}}) is True
    assert vppack.substitute_union(dict(pack["L02"], owned_files=["x/<union>/y"]), "union-7") == ["x/union-7/y"]


def test_driver_binds_the_pack_retires_the_joint_closer_and_gates_and_rewires(tmp_path):
    pd = tiny_pack(tmp_path)
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "packet": {"rewire": ["L04"]},
                                      "proof": {"require_for_kinds": []}})   # binding test, no proof harness
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    # JX: a repair task already in REPAIR_REQUIRED (adopted before activation,
    # like the Chief-era rows) -- the thing the pack retires
    ev = tmp_path / "evidence.json"
    ev.write_text('{"ok": true}\n')
    env.control_call("init", "--run-id", "t")
    params = tmp_path / "jx.json"
    params.write_text(json.dumps({"parent_contract_id": "L00", "defect_id": "DP-X", "failing_criterion": "c",
                                  "reproduction": "r", "reviewed_sha": "a" * 40, "owned_paths": ["control/jx"]}))
    env.control_call("instantiate", "--template", "REPAIR", "--task", "JX", "--parent-contract", "L00",
                     "--chief", "B", "--parameters-json", str(params))
    env.control_call("adopt", "--task", "JX", "--outcome", "REPAIR_REQUIRED", "--output-sha", "a" * 40,
                     "--tree-sha", "b" * 40, "--evidence", str(ev), "--reason", "old hold")
    # L00 finished in the Chief era: its regrade packet must run as a dynamic task
    env.control_call("adopt", "--task", "L00", "--outcome", "VERIFIED", "--unlock-dependents",
                     "--output-sha", env.base, "--tree-sha", "b" * 40, "--evidence", str(ev), "--reason", "chief era")
    env.control_call("bind-chief", "--chief", "A", "--thread-id", "a")
    env.control_call("bind-chief", "--chief", "B", "--thread-id", "b")
    for g in ("PRESERVATION_COMPLETE", "REQUIREMENTS_REBOUND", "RECOVERY_ADOPTED", "DRY_RUN_PASSED"):
        env.control_call("satisfy-gate", "--name", g, "--evidence", str(ev))
    env.control_call("activate")
    runner = by_role({"probe": result_ok, "builder": result_ok, "grader": findings("PASS")})
    drv = env.driver({"opencode": runner, "codex": FakeRunner()})
    assert set(drv.pack) == {"L02", "P-REGRADE-L00", "P-REGRADE-2", "P-GATED", "L04"} and drv.pack_lint == []
    drv.tick()                                       # tick 1: the pack is bound before any dispatch
    rows = env.rows()
    assert drv.pack_by_task["L02"] == "L02" and drv.pack_by_task["L04"] == "L04"
    assert json.loads((env.run_root / "packets" / "L02.json").read_text())["mode"] == "direct"
    # P-REGRADE-L00 is dynamic (L00 finished); P-REGRADE-2 followed its dependency
    assert rows["P-REGRADE-L00"]["dynamic"] and rows["P-REGRADE-L00"]["template_id"] == "EVIDENCE_RECOVERY"
    assert rows["P-REGRADE-L00"]["parent_contract_id"] == "L00" and drv.pack_by_task["P-REGRADE-L00"] == "P-REGRADE-L00"
    assert rows["P-REGRADE-2"]["depends_on"] == ["P-REGRADE-L00"]
    rec = json.loads((env.run_root / "packets" / "P-REGRADE-2.json").read_text())
    assert rec["mode"] == "dynamic" and rec["parent_how"] == "depends_on:P-REGRADE-L00"
    # L04 was rewired to the pack's dependency set before it started: it now waits for the regrade
    assert rows["L04"]["depends_on"] == ["P-REGRADE-L00"] and rows["L04"]["rewired"]["from"] == ["L00"]
    assert json.loads((env.run_root / "packets" / "bindings.jsonl").read_text().splitlines()[-1])["op"] == "rewire"
    drv.join(timeout=60)
    settle(drv, 6)
    rows = env.rows()
    assert rows["L04"]["state"] == "VERIFIED", "unlocked by the regrade, then run"
    # every dispatched packet turn carries .vp/DISPATCH.json
    disp = json.loads((env.tmp / "wt" / "L02" / ".vp" / "DISPATCH.json").read_text())
    assert disp["packet"] == "L02" and disp["union"].startswith("union-L02-a") and disp["closes"] == ["JX"]
    assert (env.tmp / "wt" / "L02" / ".vp" / "PACKET.md").read_text().startswith("---\nitem: L02\n")
    # the owner gate holds P-GATED until the roster says DELIVERY-1 is delivered
    assert "P-GATED" not in rows and "OWNER_GATE" in (env.run_root / "OWNER-ALERTS.md").read_text()
    settle(drv, 8)
    rows = env.rows()
    assert {rows[t]["state"] for t in ("L02", "P-REGRADE-L00", "P-REGRADE-2")} == {"VERIFIED"}
    # §6b: JX had three closers; retired exactly once, by the last one, the
    # earlier ones wrote closes_pending
    assert rows["JX"]["state"] == "CANCELLED" and rows["JX"]["blocker"]["class"] == "RETIRED"
    closures = [json.loads(l) for l in (env.run_root / "packets" / "closures.jsonl").read_text().splitlines()]
    retired = [c for c in closures if c["retired"]]
    assert len(retired) == 1 and retired[0]["retired"] == ["JX"]
    pend = [c for c in closures if c["pending"]]
    assert len(pend) == 2 and all("JX" in c["pending"] for c in pend)
    first = pend[0]["task"]
    res = json.loads((env.tmp / "wt" / first / ".vp" / "RESULT.json").read_text())
    assert "JX" in res["closes_pending"]
    # open the gate: P-GATED is instantiated on the next pack step
    roster = json.loads((env.run_root / "roster.json").read_text())
    roster["owner_gates"] = {"DELIVERY-1": True}
    (env.run_root / "roster.json").write_text(json.dumps(roster, indent=2))
    import os
    os.utime(str(env.run_root / "roster.json"), None)
    drv._roster_mtime = 0
    settle(drv, 4)
    rows = env.rows()
    assert rows["P-GATED"]["template_id"] == "TEST_GAP" and rows["P-GATED"]["parent_contract_id"] == "L00"
    assert (env.run_root / "packets" / "P-GATED.params.json").exists()


def test_review_packet_waits_for_a_candidate_then_carries_the_union_dispatch_record(tmp_path):
    from test_lanedriver import FakeCodex, register_candidate
    pd = tmp_path / "pack"
    packet(pd, "REVIEW-UNION", "NEW:JUNIOR_REVIEW", kind="review", template="JUNIOR_REVIEW", role="junior",
           body="one Luna review per union of L00")
    (pd / "REVIEW-UNION" / "PACKET.md").write_text(
        (pd / "REVIEW-UNION" / "PACKET.md").read_text().replace(
            "  - control/evidence/REVIEW-UNION/v13/REGRADE.md",
            "  - control/evidence/REVIEW-UNION/<union>/verdict-packet.json"))
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    codex = FakeCodex()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": codex})
    settle(drv, 2)
    assert "REVIEW-UNION" not in env.rows(), "no registered candidate: the review waits"
    assert any("waits for a registered candidate" in l for l in (env.run_root / "driver.log").read_text().splitlines())
    sha, tree = register_candidate(env)
    settle(drv, 3)
    rows = env.rows()
    row = rows["REVIEW-UNION"]
    assert row["template_id"] == "JUNIOR_REVIEW" and row["parameters"]["candidate_sha"] == sha
    assert row["parameters"]["diff_or_scope"] == "union:REVIEW-UNION" and row["parameters"]["covered_rows"] == []
    disp = json.loads((env.tmp / "wt" / "REVIEW-UNION" / ".vp" / "DISPATCH.json").read_text())
    assert disp["packet"] == "REVIEW-UNION" and disp["candidate_sha"] == sha and disp["review_task_id"] == "REVIEW-UNION"
    assert disp["owned_files"] == ["control/evidence/REVIEW-UNION/%s/verdict-packet.json" % disp["union"]]
    assert disp["union"] == "union-%s" % row["attempt_id"]
    # the fake verdict is refused by the gate (no native rollout): INVALID_EVIDENCE, never PASS
    assert row["state"] == "INVALID_EVIDENCE"

    # -- retry-packet: the closed task was our bug, not a verdict -------------------
    # a restart alone must not re-bind the packet to anything new
    drv2 = env.driver({"opencode": FakeRunner(default=result_ok), "codex": codex})
    drv2._pack_reconcile()
    assert drv2.pack_by_task == {"REVIEW-UNION": "REVIEW-UNION"} and "REVIEW-UNION-R1" not in env.rows()
    rec = drv2.request_packet_retry("REVIEW-UNION", "RUNNER_CRASH from the codex argv bug")
    assert rec["previous_task"] == "REVIEW-UNION" and rec["previous_state"] == "INVALID_EVIDENCE"
    marker = env.run_root / "packets" / "REVIEW-UNION.retry.json"
    assert marker.exists()
    settle(drv2, 3)
    rows = env.rows()
    new = rows["REVIEW-UNION-R1"]
    assert new["template_id"] == "JUNIOR_REVIEW" and new["parameters"]["candidate_sha"] == sha
    assert new["review_key"] == row["review_key"], "same candidate/role/scope; admitted because the old one is INVALID_EVIDENCE"
    assert not marker.exists() and drv2.pack_by_task == {"REVIEW-UNION-R1": "REVIEW-UNION"}
    prec = json.loads((env.run_root / "packets" / "REVIEW-UNION.json").read_text())
    assert prec["task"] == "REVIEW-UNION-R1" and prec["retry_of"] == "REVIEW-UNION"
    assert (env.run_root / "packets" / "REVIEW-UNION-R1.params.json").exists()
    ops = [json.loads(l).get("op") for l in (env.run_root / "packets" / "bindings.jsonl").read_text().splitlines()]
    assert "retry-requested" in ops
    assert "PACKET_RETRIED" in (env.run_root / "alerts.jsonl").read_text()
    # the retry ran (same fake verdict -> INVALID_EVIDENCE again) and a fresh driver keeps the R1 binding
    assert new["state"] == "INVALID_EVIDENCE"
    drv3 = env.driver({"opencode": FakeRunner(default=result_ok), "codex": codex})
    drv3._pack_reconcile()
    assert drv3.pack_by_task == {"REVIEW-UNION-R1": "REVIEW-UNION"} and "REVIEW-UNION-R2" not in env.rows()
    # a second retry numbers R2; an accepted/unfinished task is refused up front
    drv3.request_packet_retry("REVIEW-UNION", "again")
    settle(drv3, 3)
    assert "REVIEW-UNION-R2" in env.rows() and drv3.pack_by_task == {"REVIEW-UNION-R2": "REVIEW-UNION"}
    import pytest
    with pytest.raises(ValueError):
        drv3.request_packet_retry("NOPE", "x")


# -- D16: a verified retry / fix retires the rows it supersedes ---------------------------------

def test_superseded_by_pure():
    from lanedriver import LaneDriver
    t = {"X": {"state": "REPAIR_REQUIRED"}, "X-R1": {"state": "INVALID_EVIDENCE"}, "X-R2": {"state": "VERIFIED"},
         "X-R3": {"state": "READY"}, "X-FIX-1": {"state": "REPAIR_REQUIRED"}, "X-FIX-2": {"state": "VERIFIED"},
         "XY": {"state": "REPAIR_REQUIRED"}, "X-HOSTED": {"state": "READY"}, "Y-R1": {"state": "RUNNING"},
         "Y": {"state": "CLAIMED"}}
    assert LaneDriver.superseded_by("X-R2", t) == ["X", "X-R1"], "root and lower retries only"
    assert LaneDriver.superseded_by("X-FIX-2", t) == ["X", "X-FIX-1", "X-R1", "X-R3"], "a fix supersedes every retry"
    assert LaneDriver.superseded_by("Y-R1", t) == [], "a CLAIMED root is left alone (F8)"
    assert LaneDriver.superseded_by("X", t) == [] and LaneDriver.superseded_by("XY", t) == []


def test_verified_retry_retires_the_superseded_base_row(tmp_path):
    pd = tmp_path / "pack"
    packet(pd, "P-GAP", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="gap for L00")
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    seen = []

    def grader(spec, ab):
        seen.append(spec.item)
        # the first task's grades FAIL (both rounds); its retry passes
        return findings("FAIL" if spec.item == "P-GAP" else "PASS")(spec, ab)
    runner = by_role({"builder": result_ok, "grader": grader, "probe": result_ok})
    drv = env.driver({"opencode": runner, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 4)
    rows = env.rows()
    assert rows["P-GAP"]["state"] == "REPAIR_REQUIRED", rows["P-GAP"]
    drv.request_packet_retry("P-GAP", "driver bug, not a verdict")
    settle(drv, 4)
    rows = env.rows()
    assert rows["P-GAP-R1"]["state"] == "VERIFIED", rows["P-GAP-R1"]
    assert rows["P-GAP"]["state"] == "CANCELLED" and rows["P-GAP"]["blocker"]["class"] == "RETIRED"
    assert rows["P-GAP"]["blocker"]["reason"] == "SUPERSEDED_BY:P-GAP-R1"
    assert rows["P-GAP"]["blocker"]["closers"] == ["P-GAP-R1"]
    log = (env.run_root / "driver.log").read_text()
    assert "SUPERSEDED by P-GAP-R1: P-GAP retired" in log
    closures = [json.loads(l) for l in (env.run_root / "packets" / "closures.jsonl").read_text().splitlines()]
    assert any(c.get("op") == "supersede" and c["retired"] == ["P-GAP"] for c in closures)


def test_retry_packet_regrade_grades_the_same_commit_without_a_builder_round(tmp_path):
    """D21: after a benchmark amendment, `retry-packet --regrade <sha>` makes
    <id>-R<n> reset its worktree to that exact commit, carry the previous
    RESULT.json, and go straight to proof + grade: no builder turn, no autofix."""
    pd = tmp_path / "pack"
    packet(pd, "P-GAP", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="gap for L00")
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    builds = []

    def builder(spec, ab):
        builds.append(spec.item)
        return result_ok(spec, ab)

    def grader(spec, ab):
        return findings("FAIL" if spec.item == "P-GAP" else "PASS")(spec, ab)
    runner = by_role({"builder": builder, "grader": grader, "probe": result_ok})
    drv = env.driver({"opencode": runner, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 4)
    rows = env.rows()
    assert rows["P-GAP"]["state"] == "REPAIR_REQUIRED"
    built = git(env.tmp / "wt" / "P-GAP", "rev-parse", "HEAD")
    n_builds = len(builds)
    # an unknown sha is refused up front
    with pytest.raises(ValueError):
        drv.request_packet_retry("P-GAP", "x", regrade_sha="0" * 40)
    rec = drv.request_packet_retry("P-GAP", "B3 amended: regrade the same commit", regrade_sha=built[:12])
    assert rec["regrade_sha"] == built
    settle(drv, 4)
    rows = env.rows()
    assert rows["P-GAP-R1"]["state"] == "VERIFIED", rows["P-GAP-R1"]
    assert rows["P-GAP-R1"]["output_sha"] == built, "the regraded commit is the output"
    assert len(builds) == n_builds, "no builder turn"
    wt = env.tmp / "wt" / "P-GAP-R1"
    assert git(wt, "rev-parse", "HEAD") == built
    assert json.loads((wt / ".vp" / "RESULT.json").read_text())["commit"] == built
    # a RESULT.json the driver had rebound to an autofix head is carried when
    # its pre_autofix_commit is the regrade sha, and rebound back
    prev_res = env.tmp / "wt" / "P-GAP" / ".vp" / "RESULT.json"
    doc = json.loads(prev_res.read_text())
    doc.update({"commit": "f" * 40, "pre_autofix_commit": built})
    prev_res.write_text(json.dumps(doc))
    wt2 = env.tmp / "wt" / "P-GAP-R9"
    git(env.trunk, "worktree", "add", "-q", "--detach", str(wt2), built)
    (wt2 / ".vp").mkdir()
    (wt2 / ".vp" / "BASE").write_text(env.base)
    assert drv._regrade_reset("P-GAP-R9", wt2, {"sha": built, "retry_of": "P-GAP"}) is None
    carried = json.loads((wt2 / ".vp" / "RESULT.json").read_text())
    assert carried["commit"] == built and "rebound from autofix head ffffffffffff" in carried["notes"]
    log = (env.run_root / "driver.log").read_text()
    assert "REGRADE P-GAP-R1 on %s (build + autofix skipped; retry_of P-GAP)" % built[:12] in log
    assert "REGRADE P-GAP-R1 carried P-GAP/.vp/RESULT.json" in log
    assert not list((env.run_root / "turns" / "P-GAP-R1").glob("*/1-r1-builder-*"))
    assert not list((env.run_root / "turns" / "P-GAP-R1").glob("*/autofix.json"))
    prec = json.loads((env.run_root / "packets" / "P-GAP.json").read_text())
    assert prec["task"] == "P-GAP-R1" and prec["regrade_sha"] == built
    assert rows["P-GAP"]["state"] == "CANCELLED" and rows["P-GAP"]["blocker"]["reason"] == "SUPERSEDED_BY:P-GAP-R1"


def test_supersession_rewires_dependents_of_the_superseded_row(tmp_path):
    """D23 (F-C): L27-V13/L28-V13 waited on CANCELLED L2728-LEAF while
    L2728-LEAF-R3 was VERIFIED -- a retired row never unlocks.  On
    supersession every unstarted dependent of the superseded row is rewired
    to the superseding one; already-CANCELLED parents are covered by the sweep."""
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(), "codex": FakeRunner(), "claude": FakeRunner()})
    calls = []

    def fake_call(verb, args=(), allow=(0,)):
        calls.append((verb, list(args)))
        return 0, {}
    drv.control.call = fake_call
    tasks = {"P-GAP": {"state": "REPAIR_REQUIRED"},
             "P-GAP-R1": {"state": "VERIFIED"},
             "L27-V13": {"state": "WAITING_DEPENDENCY", "depends_on": ["P-GAP", "L03"]},
             "L28-V13": {"state": "WAITING_DEPENDENCY", "depends_on": ["P-GAP"]},
             "DONE": {"state": "VERIFIED", "depends_on": ["P-GAP"]},
             "RUN": {"state": "RUNNING", "depends_on": ["P-GAP"]}}
    assert drv._supersede("P-GAP-R1", tasks) == ["P-GAP"]
    assert calls[0] == ("retire", ["--task", "P-GAP", "--closer", "P-GAP-R1", "--reason", "SUPERSEDED_BY:P-GAP-R1"])
    rewires = [(a[1], a[3:a.index("--reason")]) for v, a in calls if v == "rewire"]
    assert rewires == [("L27-V13", ["P-GAP-R1", "L03"]), ("L28-V13", ["P-GAP-R1"])], "unstarted dependents only"
    assert tasks["L27-V13"]["depends_on"] == ["P-GAP-R1", "L03"]
    ops = [json.loads(l) for l in (env.run_root / "packets" / "closures.jsonl").read_text().splitlines()]
    assert [o["op"] for o in ops] == ["supersede", "supersede-rewire", "supersede-rewire"]
    # the sweep: parent already CANCELLED (retired before this rule), dependent still waiting
    calls.clear()
    tasks2 = {"Q": {"state": "CANCELLED"}, "Q-R2": {"state": "VERIFIED"},
              "DEP": {"state": "WAITING_DEPENDENCY", "depends_on": ["Q"]}}
    drv._supersede_sweep({"tasks": tasks2})
    assert [v for v, _a in calls] == ["rewire"] and tasks2["DEP"]["depends_on"] == ["Q-R2"]
    assert "SUPERSEDED by Q-R2: DEP.depends_on Q -> Q-R2" in (env.run_root / "driver.log").read_text()


def test_supersede_sweep_retires_rows_verified_before_the_rule_existed(tmp_path):
    env = Env(tmp_path)
    env.activate()
    params = env.tmp / "p.json"
    params.write_text(json.dumps({"parent_contract_id": "L00", "unproved_criterion": "x", "candidate_sha": env.base,
                                  "test_location": "platform/a.py", "owned_paths": ["platform/a.py"]}))
    for t in ("G", "G-R1", "G-R2"):
        env.control_call("instantiate", "--template", "TEST_GAP", "--task", t, "--parent-contract", "L00",
                         "--chief", "B", "--parameters-json", str(params))
    env.control_call("block", "--task", "G", "--blocker-class", "EXTERNAL", "--reason", "old", "--unblock-action", "n/a",
                     "--evidence", str(params))
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner(), "claude": FakeRunner()})
    drv._supersede = lambda task, tasks=None: []    # pretend the completion hook did not exist yet
    settle(drv, 2)                                  # L00 verified -> G* READY; G-R1, G-R2 run and verify
    settle(drv, 3)
    rows = env.rows()
    assert rows["G-R2"]["state"] == "VERIFIED" and rows["G-R1"]["state"] == "VERIFIED"
    assert rows["G"]["state"] == "BLOCKED", "nothing retired it at completion time"
    del drv._supersede                              # the real rule is back
    drv._pack_last_mono = None
    drv.tick()                                      # the sweep runs on the pack cadence
    rows = env.rows()
    assert rows["G"]["state"] == "CANCELLED" and rows["G"]["blocker"]["reason"] == "SUPERSEDED_BY:G-R1"
    assert rows["G-R1"]["state"] == "VERIFIED", "an accepted lower retry is never retired"
    # idempotent: a second sweep retires nothing more
    assert drv._supersede_sweep({"tasks": rows}) == []


# -- D17: a v13 review packet reviews the SUBJECT, then the driver writes the verdict and grades ---

def test_union_review_with_an_empty_subject_is_held_not_dispatched(tmp_path):
    """D21: coverage_targets [<union>] + no review_base + no covered rows + no
    unions/*/members.json -> the review is held (REVIEW_SUBJECT_EMPTY), no
    reviewer turn is spent; it dispatches once a union exists."""
    from test_lanedriver import FakeCodex, register_candidate
    pd = tmp_path / "pack"
    packet(pd, "REVIEW-UNION", "NEW:JUNIOR_REVIEW", kind="review", template="JUNIOR_REVIEW", role="junior",
           body="one Luna review per union of L00")
    pm = pd / "REVIEW-UNION" / "PACKET.md"
    pm.write_text(pm.read_text().replace("  - control/evidence/REVIEW-UNION/v13/REGRADE.md",
                                         "  - control/evidence/REVIEW-UNION/<union>/verdict-packet.json")
                  .replace("---\n", "---\ncoverage_targets: [<union>]\n", 1))
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    codex = FakeCodex()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": codex})
    register_candidate(env)
    settle(drv, 3)
    row = env.rows()["REVIEW-UNION"]
    assert row["state"] == "READY", "held: never claimed"
    assert not (env.run_root / "turns" / "REVIEW-UNION").exists(), "no reviewer turn spent"
    log = (env.run_root / "driver.log").read_text()
    assert "REVIEW_SUBJECT_EMPTY" in (env.run_root / "alerts.jsonl").read_text()
    assert "HOLD REVIEW-UNION 600s REVIEW_SUBJECT_EMPTY" in log
    # a union appears: the hold is dropped on the next ready pass
    (env.run_root / "unions" / "1").mkdir(parents=True)
    (env.run_root / "unions" / "1" / "members.json").write_text(json.dumps({"items": [{"task": "L00"}]}))
    drv._fail.pop("REVIEW-UNION", None)
    settle(drv, 3)
    assert env.rows()["REVIEW-UNION"]["state"] != "READY"
    assert (env.run_root / "turns" / "REVIEW-UNION").exists()


def test_review_packet_reviews_the_subject_then_writes_the_verdict_and_grades(tmp_path, monkeypatch):
    from test_lanedriver import FakeCodex, register_candidate
    import lanedriver
    pd = tmp_path / "pack"
    packet(pd, "SEC-REV", "NEW:JUNIOR_REVIEW", kind="review", template="JUNIOR_REVIEW", role="junior",
           body="security review of the delta of L00")
    pm = pd / "SEC-REV" / "PACKET.md"
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    # a second trunk commit: the subject is base..candidate = first..second commit
    (env.trunk / "platform" / "a.py").write_text("x = 2\n")
    git = __import__("test_lanedriver").git
    git(env.trunk, "commit", "-qam", "delta")
    pm.write_text(pm.read_text().replace("owner_gate: none\n", "owner_gate: none\nreview_base: %s\n"
                                                                "coverage_targets: [L00, L01]\n" % env.base))
    pm.write_text(pm.read_text().replace("  - control/evidence/SEC-REV/v13/REGRADE.md",
                                         "  - control/evidence/SEC-REV/v13/verdict-packet.json"))
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    monkeypatch.setattr(lanedriver.LaneDriver, "_review_gate_check", lambda self, *a: (True, "PASS: fake gate"))
    codex = FakeCodex()
    seen = {}

    def grader(spec, ab):
        wt = Path(spec.cwd)
        seen["benchmark"] = (wt / ".vp" / "BENCHMARK.md").read_text()
        seen["verdict_in_tree"] = (wt / "control" / "evidence" / "SEC-REV" / "v13" / "verdict-packet.json").exists()
        seen["result"] = json.loads((wt / ".vp" / "RESULT.json").read_text())
        return findings("PASS")(spec, ab)
    oc = by_role({"builder": result_ok, "grader": grader, "probe": result_ok})
    drv = env.driver({"opencode": oc, "codex": codex})
    settle(drv, 2)
    register_candidate(env)
    settle(drv, 3)
    rows = env.rows()
    row = rows["SEC-REV"]
    wt = env.tmp / "wt" / "SEC-REV"
    req = json.loads((wt / ".vp" / "REVIEW_REQUEST.json").read_text())
    assert req["base"] == env.base and req["candidate"] != env.base, "review_base..candidate, not an empty diff"
    assert req["coverage_targets"] == ["L00", "L01"] and req["packet"] == "SEC-REV"
    rev = codex.calls[-1]
    assert rev.role == "reviewer"
    review = json.loads((wt / ".vp" / "REVIEW.json").read_text())
    ids = [v["id"] for v in review["verdicts"]]
    assert ids and all(i in req["benchmark_ids"] for i in ids)
    assert "# Review subject" not in seen["benchmark"], "the packet's own benchmark is back for the grader"
    assert seen["verdict_in_tree"], "verdict-packet.json committed at the packet's owned path before the grade"
    assert seen["result"]["checks"][0]["name"] == "review_gate.validate" and seen["result"]["checks"][0]["exit"] == 0
    assert git(wt, "log", "--oneline", "-1").endswith("SEC-REV: v13 junior verdict packet")
    vp = json.loads((wt / "control" / "evidence" / "SEC-REV" / "v13" / "verdict-packet.json").read_text())
    assert set(vp["coverage"]) >= {"L00", "L01"}
    # the scheduler's own gate still runs on complete: no native rollout -> refused -> INVALID_EVIDENCE
    assert row["state"] == "INVALID_EVIDENCE" and "verdict refused" in row["blocker"]
    assert "VERDICT_REFUSED" in (env.run_root / "OWNER-ALERTS.md").read_text()


def test_review_packet_union_placeholder_resolves_to_covered_rows():
    from lanedriver import LaneDriver
    import types
    drv = types.SimpleNamespace()
    drv.git = lambda args: (0, "", "")
    drv.control = types.SimpleNamespace(contract=lambda t, row=None: {"verification": ["%s ok" % t], "acceptance": []})
    drv.alert_once = lambda *a, **k: None
    wt = Path(__import__("tempfile").mkdtemp()) / "wt"
    (wt / ".vp").mkdir(parents=True)
    (wt / ".vp" / "PACKET.md").write_text("---\nitem: RJU\ncoverage_targets: [<union>, L99]\n---\nbody\n")
    (wt / ".vp" / "BENCHMARK.md").write_text("- B1 [evidence] [box] x — check: y\n")
    row = {"parameters": {"parent_contract_id": "L42", "covered_rows": ["L06", "L07"], "diff_or_scope": "union:RJU"}}
    plan = LaneDriver._review_packet_plan(drv, "RJU", row, {}, wt, "b" * 40, "c" * 40, {"tasks": {}})
    assert plan["targets"] == ["L42", "L06", "L07", "L99"] and plan["subject"] == "union"
    assert "L06 ok" in plan["review_benchmark"] and "<union>" not in plan["review_benchmark"]


# -- D27 §9(5a)/(5b): the union integrator and the union-complete dispatch precondition ---------

def _fix_builder(spec, ab):
    """a builder that lands a distinct file per packet and leaves platform/a.py
    alone (so two fixes merge cleanly; result_ok edits a.py line 1 per item)"""
    wt = Path(spec.cwd)
    (wt / "platform" / ("%s.py" % spec.item.lower().replace("-", "_"))).write_text("fixed = %r\n" % spec.item)
    out = result_ok(spec, ab, commit=False)
    git(wt, "checkout", "--", "platform/a.py")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "fix %s" % spec.item)
    rec = json.loads(Path(spec.out_path).read_text())
    rec["commit"] = git(wt, "rev-parse", "HEAD")
    Path(spec.out_path).write_text(json.dumps(rec))
    return out


def _union_env(tmp_path, builder=_fix_builder, deps=("P-FIX-A", "P-FIX-B")):
    from test_lanedriver import FakeCodex
    pd = tmp_path / "pack"
    packet(pd, "P-FIX-A", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="fix A for L00")
    packet(pd, "P-FIX-B", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="fix B for L00")
    packet(pd, "REVIEW-FIXSET", "NEW:JUNIOR_REVIEW", kind="review", template="JUNIOR_REVIEW", role="junior",
           deps=list(deps), body="one review of the union of the fixes of L00")
    pm = pd / "REVIEW-FIXSET" / "PACKET.md"
    pm.write_text(pm.read_text().replace("  - control/evidence/REVIEW-FIXSET/v13/REGRADE.md",
                                         "  - control/evidence/REVIEW-FIXSET/<union>/verdict-packet.json")
                  .replace("---\n", "---\ncoverage_targets: [<union>, L00]\n", 1))
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    codex = FakeCodex()
    oc = by_role({"builder": builder, "grader": findings("PASS"), "probe": result_ok})
    drv = env.driver({"opencode": oc, "codex": codex, "claude": FakeRunner()})
    return env, drv, codex


def test_union_integrator_cuts_a_union_of_the_verified_fixes_and_the_review_runs_on_its_tip(tmp_path, monkeypatch):
    from test_lanedriver import register_candidate
    import lanedriver
    monkeypatch.setattr(lanedriver.LaneDriver, "_review_gate_check", lambda self, *a: (True, "PASS: fake gate"))
    env, drv, codex = _union_env(tmp_path)
    sha, tree = register_candidate(env)
    settle(drv, 6)
    rows = env.rows()
    assert rows["P-FIX-A"]["state"] == "VERIFIED" and rows["P-FIX-B"]["state"] == "VERIFIED", rows
    members = env.run_root / "unions" / "1" / "members.json"
    assert members.exists(), (env.run_root / "driver.log").read_text()
    doc = json.loads(members.read_text())
    assert doc["union"] == "union-1" and doc["base_sha"] == sha and doc["for"] == ["REVIEW-FIXSET"]
    assert {m["task"]: m["output_sha"] for m in doc["members"]} == {
        "P-FIX-A": rows["P-FIX-A"]["output_sha"], "P-FIX-B": rows["P-FIX-B"]["output_sha"]}
    assert all(m["merge"] == "clean" for m in doc["members"])
    tip = doc["union_sha"]
    # the union tip is a real commit in trunk that contains both fixes and descends from the candidate
    assert git(env.trunk, "merge-base", "--is-ancestor", sha, tip) == ""
    assert sorted(git(env.trunk, "ls-tree", "--name-only", tip, "platform/").splitlines()) == [
        "platform/a.py", "platform/p_fix_a.py", "platform/p_fix_b.py"]
    assert "unions.jsonl" in {p.name for p in (env.run_root / "unions").iterdir()}
    # the review dispatched on the union tip: worktree HEAD == tip, subject = candidate..tip
    review = rows["REVIEW-FIXSET"]
    assert review["state"] != "READY", review
    wt = env.tmp / "wt" / "REVIEW-FIXSET"
    disp = json.loads((wt / ".vp" / "DISPATCH.json").read_text())
    assert disp["union"] == "union-1" and disp["union_sha"] == tip and disp["union_base_sha"] == sha
    assert sorted(disp["union_members"]) == ["P-FIX-A", "P-FIX-B"]
    assert disp["owned_files"] == ["control/evidence/REVIEW-FIXSET/union-1/verdict-packet.json"]
    req = json.loads((wt / ".vp" / "REVIEW_REQUEST.json").read_text())
    assert (req["base"], req["candidate"], req["subject"]) == (sha, tip, "union")
    assert req["union"] == "union-1" and sorted(req["union_members"]) == ["P-FIX-A", "P-FIX-B"]
    assert set(req["coverage_targets"]) >= {"L00", "P-FIX-A", "P-FIX-B"}
    assert codex.calls and Path(codex.calls[0].cwd) == wt
    # the verdict packet names the registered candidate (review_gate) AND the union it reviewed
    vp = json.loads(next((env.run_root / "turns" / "REVIEW-FIXSET").rglob("verdict-packet.json")).read_text())
    assert vp["candidate_sha"] == sha and vp["union"] == "union-1" and vp["union_sha"] == tip
    log = (env.run_root / "driver.log").read_text()
    assert "UNION union-1 %s base=%s for REVIEW-FIXSET members=P-FIX-A,P-FIX-B" % (tip[:12], sha[:12]) in log
    assert "UNION union-1: REVIEW-FIXSET worktree at %s (2 members)" % tip[:12] in log
    # idempotent: a later pass cuts no second union for the same member shas
    settle(drv, 2)
    assert not (env.run_root / "unions" / "2").exists()


def test_union_review_is_held_until_a_union_contains_all_its_depends_on_rows(tmp_path):
    """§9(5a): a members.json that lacks one of the packet's depends_on rows
    (or carries a member at a stale output sha) does not dispatch the review."""
    from test_lanedriver import register_candidate
    import lanedriver
    env, drv, codex = _union_env(tmp_path)
    sha, tree = register_candidate(env)
    # the integrator is silenced: only a hand-written, incomplete union exists
    drv._union_step = lambda state: None
    settle(drv, 6)
    rows = env.rows()
    assert rows["P-FIX-A"]["state"] == "VERIFIED" and rows["P-FIX-B"]["state"] == "VERIFIED"
    (env.run_root / "unions" / "1").mkdir(parents=True)
    (env.run_root / "unions" / "1" / "members.json").write_text(json.dumps(
        {"union": "union-1", "n": 1, "base_sha": sha, "union_sha": sha,
         "members": [{"task": "P-FIX-A", "output_sha": rows["P-FIX-A"]["output_sha"]}]}))
    settle(drv, 3)
    assert env.rows()["REVIEW-FIXSET"]["state"] == "READY", "held: never claimed"
    assert codex.calls == []
    alerts = (env.run_root / "alerts.jsonl").read_text()
    assert "REVIEW_UNION_INCOMPLETE" in alerts and "['P-FIX-B']" in alerts
    assert "HOLD REVIEW-FIXSET 600s REVIEW_UNION_INCOMPLETE" in (env.run_root / "driver.log").read_text()
    # a stale member sha is as good as a missing one
    (env.run_root / "unions" / "2").mkdir(parents=True)
    (env.run_root / "unions" / "2" / "members.json").write_text(json.dumps(
        {"union": "union-2", "n": 2, "base_sha": sha, "union_sha": sha,
         "members": [{"task": "P-FIX-A", "output_sha": rows["P-FIX-A"]["output_sha"]},
                     {"task": "P-FIX-B", "output_sha": "f" * 40}]}))
    p = drv.packet_for("REVIEW-FIXSET")
    union, missing = drv._union_for("REVIEW-FIXSET", p, env.rows())
    assert union is None and missing == ["P-FIX-B"]
    # a complete union dispatches it (hold dropped on the next ready pass)
    doc = json.loads((env.run_root / "unions" / "2" / "members.json").read_text())
    doc["members"][1]["output_sha"] = rows["P-FIX-B"]["output_sha"]
    (env.run_root / "unions" / "2" / "members.json").write_text(json.dumps(doc))
    union, missing = drv._union_for("REVIEW-FIXSET", p, env.rows())
    assert union["union"] == "union-2" and missing == []
    drv._fail.pop("REVIEW-FIXSET", None)
    settle(drv, 3)
    assert env.rows()["REVIEW-FIXSET"]["state"] != "READY"
    assert codex.calls and json.loads((env.tmp / "wt" / "REVIEW-FIXSET" / ".vp" / "DISPATCH.json").read_text())["union"] == "union-2"


def test_union_conflict_is_recorded_and_the_review_stays_held(tmp_path):
    """two fixes editing the same line: no members.json, a conflict.json + UNION_CONFLICT
    alert naming the member, the review held; the same member set is not retried every pass"""
    from test_lanedriver import register_candidate
    env, drv, codex = _union_env(tmp_path, builder=result_ok)   # result_ok edits platform/a.py line 1 per item
    sha, tree = register_candidate(env)
    settle(drv, 6)
    rows = env.rows()
    assert rows["P-FIX-A"]["state"] == "VERIFIED" and rows["P-FIX-B"]["state"] == "VERIFIED"
    assert not (env.run_root / "unions" / "1" / "members.json").exists()
    con = json.loads((env.run_root / "unions" / "1" / "conflict.json").read_text())
    assert con["status"] == "CONFLICT" and con["conflict"]["task"] == "P-FIX-B" and [m["task"] for m in con["members"]] == ["P-FIX-A"]
    assert "UNION_CONFLICT" in (env.run_root / "alerts.jsonl").read_text()
    assert env.rows()["REVIEW-FIXSET"]["state"] == "READY" and codex.calls == []
    assert git(env.trunk, "branch", "--list", "vp/union-1") == ""
    assert not (env.tmp / "wt" / "union-1").exists()
    settle(drv, 2)
    assert not (env.run_root / "unions" / "2").exists(), "the failed member set is tried once"
