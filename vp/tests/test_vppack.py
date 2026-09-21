#!/usr/bin/env python3
"""tests/test_vppack.py -- pack §4(c): packet -> scheduler-task binding (D9),
joint closers (§6b), owner gates, L42 rewire, dispatch records.  Pure vppack
tests plus one driver cycle over a tiny pack with fake runners."""

from __future__ import annotations

import json
import pytest
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP))
sys.path.insert(0, str(HERE))

import vplint  # noqa: E402
import vppack  # noqa: E402
from test_lanedriver import Env, FakeRunner, TurnOutcome, by_role, findings, git, result_ok, settle  # noqa: E402

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
    packet(pd, "P-GATED-HOSTED", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder",
           gate="DELIVERY-1", body="gated behind L00 (D34: only a -HOSTED twin is gated)")
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
    assert vppack.parent_for(pack["P-GATED-HOSTED"], t, pack) == ("L00", "body-cite")
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
    assert vppack.parameters_for(pack["P-GATED-HOSTED"], "JUNIOR_REVIEW", "L00", candidate={}) is None
    rv = vppack.parameters_for(pack["P-GATED-HOSTED"], "JUNIOR_REVIEW", "L00", candidate={"sha": "s", "tree": "t"},
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
    # D34: the gate holds only the <ID>-HOSTED twin; the base packet never consults it
    assert vppack.owner_gate_open(dict(pack["P-GATED-HOSTED"], id="P-GATED"), {}) is True
    twin = pack["P-GATED-HOSTED"]
    assert vppack.owner_gate_open(twin, {}) is False, "no owner_gates map = all closed for twins"
    assert vppack.owner_gate_open(twin, {"owner_gates": {"DELIVERY-1": False}}) is False
    assert vppack.owner_gate_open(twin, {"owner_gates": {"DELIVERY-1": True}}) is True
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
    assert set(drv.pack) == {"L02", "P-REGRADE-L00", "P-REGRADE-2", "P-GATED-HOSTED", "L04"} and drv.pack_lint == []
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
    assert "P-GATED-HOSTED" not in rows and "OWNER_GATE" in (env.run_root / "OWNER-ALERTS.md").read_text()
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
    assert rows["P-GATED-HOSTED"]["template_id"] == "TEST_GAP" and rows["P-GATED-HOSTED"]["parent_contract_id"] == "L00"
    assert (env.run_root / "packets" / "P-GATED-HOSTED.params.json").exists()


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
    # D161: X-R3 is in the list now.  This assertion used to read ["X", "X-R1"]
    # -- "root and lower retries only" -- which is precisely the defect: an
    # accepted R2 left the already-dispatched R3 stranded forever.
    assert LaneDriver.superseded_by("X-R2", t) == ["X", "X-R1", "X-R3"], (
        "the root and EVERY other retry, whichever side of the accepted index")
    assert LaneDriver.superseded_by("X-FIX-2", t) == ["X", "X-FIX-1", "X-R1", "X-R3"], "a fix supersedes every retry"
    assert LaneDriver.superseded_by("Y-R1", t) == [], "a CLAIMED root is left alone (F8)"
    assert LaneDriver.superseded_by("X", t) == [] and LaneDriver.superseded_by("XY", t) == []
    # the kind distinction survives the index going away: a plain retry still
    # never retires a -FIX- row, whichever way the two are numbered
    assert "X-FIX-1" not in LaneDriver.superseded_by("X-R2", t)
    assert "X-FIX-1" not in LaneDriver.superseded_by("X-R1", t)


def test_d161_an_accepted_early_retry_retires_the_later_ones_too():
    """D161, measured on L06 in run-v13-20260917: R1 went VERIFIED at
    2026-09-20T21:53 and R12 was dispatched at 2026-09-21T00:37, about three
    hours later.  The old rule retired only STRICTLY lower-numbered siblings, so
    every attempt numbered above the accepted one stayed live forever -- 11 of
    136 rows stranded that way.

    R1..R12 are attempts at the SAME work.  Once one is accepted the rest are
    pointless whichever way they are numbered.

    The two exclusions that must survive: RUNNING/CLAIMED rows are still left
    alone (F8), and they are excluded by the RETIRABLE state check, never by an
    index rule."""
    from lanedriver import LaneDriver
    t = {"L06": {"state": "REPAIR_REQUIRED"}, "L06-R1": {"state": "VERIFIED"},
         "L06-R2": {"state": "REPAIR_REQUIRED"}, "L06-R11": {"state": "READY"},
         "L06-R12": {"state": "RUNNING"}, "L06-R13": {"state": "BLOCKED"}}

    got = LaneDriver.superseded_by("L06-R1", t)

    assert got == ["L06", "L06-R11", "L06-R13", "L06-R2"], got
    assert "L06-R12" not in got, "a RUNNING sibling is still left alone (F8)"


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
    drv._integration_union_step = lambda state: None   # premise: no union of any kind exists yet
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
    drv.run_root = wt.parent / "run"
    (drv.run_root / "proofs").mkdir(parents=True)
    drv._proof_index = lambda: LaneDriver._proof_index(drv)
    # `**k` as well as `*a`: this stub only exists to bind `drv` as self, so it
    # must forward EVERYTHING.  Pinned at `*a` it silently pinned the arity too,
    # and D189 adding a `review=` kwarg at the call site broke here with a
    # TypeError -- in a file that never mentions _review_proofs' signature.  A
    # pass-through that cannot pass an argument through is a fake wearing a
    # forwarder's clothes.
    drv._review_proofs = lambda *a, **k: LaneDriver._review_proofs(drv, *a, **k)
    drv.PROOF_FIELDS = LaneDriver.PROOF_FIELDS
    plan = LaneDriver._review_packet_plan(drv, "RJU", row, {}, wt, "b" * 40, "c" * 40, {"tasks": {}})
    assert plan["targets"] == ["L42", "L06", "L07", "L99"] and plan["subject"] == "union"
    assert "L06 ok" in plan["review_benchmark"] and "<union>" not in plan["review_benchmark"]
    # D77 (§36 F.2): the head carries the PROOFS.json rule; with no records every entry is empty
    assert "PASS when .vp/PROOFS.json records a PASS proof" in plan["review_benchmark"]
    # D77a (Architect, after SEC-REVIEW-S3-FIXSET-R3 failed B108 on provenance): staleness AND provenance
    assert "do not FAIL the union on baseline staleness or provenance" in plan["review_benchmark"]
    assert "measured_commit, the test-ID diff and the merge count" in plan["review_benchmark"]
    assert [e["task"] for e in plan["proofs"]["entries"]] == ["L42", "L06", "L07", "L99"]
    assert all(e["proof_id"] is None and e["output_sha"] is None for e in plan["proofs"]["entries"])
    # with records: the newest PASS at the member's output sha wins over an older FAIL / a newer non-PASS
    sha6, sha7 = "6" * 40, "7" * 40
    # D170: proof ids are `proof-<task>-<ts>` in the real corpus -- all 471 of them.
    # These were `proof-L06-1`, which parses to the task `L06-1`, so the owner guard
    # correctly refused to hand L06 a proof belonging to something else. Realistic
    # ids rather than a looser guard: `-1` is a shape no driver writes.
    for pid, sha, status, ts in (("proof-L06-60919T010000", sha6, "FAIL_INFRA", "2026-09-19T01:00:00.000Z"),
                                 ("proof-L06-60919T020000", sha6, "PASS", "2026-09-19T02:00:00.000Z"),
                                 ("proof-L06-60919T030000", sha6, "UNKNOWN", "2026-09-19T03:00:00.000Z"),
                                 ("proof-L07-60919T020000", sha7, "FAIL_PRODUCT", "2026-09-19T02:00:00.000Z"),
                                 ("proof-TIP-60919T040000", "c" * 40, "PASS", "2026-09-19T04:00:00.000Z")):
        (drv.run_root / "proofs" / (pid + ".json")).write_text(json.dumps(
            {"proof_id": pid, "sha": sha, "status": status, "ts": ts, "kind": "platform", "route": "box",
             "paths": ["platform/tests/test_%s.py" % pid[6:9].lower()], "failed_nodes": ["x::y"] if "FAIL" in status else [],
             "log": "/logs/%s.log" % pid}))
    state = {"tasks": {"L06": {"output_sha": sha6}, "L07": {"output_sha": sha7}}}
    union = {"union": "union-9", "members": [{"task": "L06", "output_sha": sha6}, {"task": "L07", "output_sha": sha7}]}
    plan = LaneDriver._review_packet_plan(drv, "RJU", row, {}, wt, "b" * 40, "c" * 40, state, union=union)
    by = {e["task"]: e for e in plan["proofs"]["entries"]}
    assert (by["L06"]["proof_id"] == "proof-L06-60919T020000" and by["L06"]["status"] == "PASS"
            and by["L06"]["log"] == "/logs/proof-L06-60919T020000.log")
    assert by["L07"]["proof_id"] == "proof-L07-60919T020000" and by["L07"]["failed_nodes"] == ["x::y"]
    assert by["L42"]["proof_id"] is None and by["L99"]["proof_id"] is None
    assert by["<union tip>"]["proof_id"] == "proof-TIP-60919T040000" and plan["proofs"]["union"] == "union-9"
    assert "4 member record(s), 2 with a proof" in plan["review_benchmark"]
    # D168 adds `scope` + `proof_required` to every member entry. The exact-set check
    # stays exact on purpose: it is what stops the entry shape drifting silently, so a
    # new field is a deliberate edit here rather than a superset test that notices nothing.
    # D170 adds `only` to PROOF_FIELDS. Kept exact rather than relaxed to a superset:
    # the absence of `only` from this list is precisely what made 287 scoped proofs
    # read as full-suite passes, so the shape of this list is load-bearing and a
    # change to it should be a deliberate edit here.
    # D174 adds `scope_basis`: the REASON an entry is exempt travels with the
    # entry, so the packet can say why rather than only that.  Exact on purpose,
    # as above -- a new field is a deliberate edit here.
    assert set(plan["proofs"]["entries"][0]) == (
        {"task", "output_sha", "scope", "scope_basis", "proof_required"} | set(LaneDriver.PROOF_FIELDS))
    assert "only" in LaneDriver.PROOF_FIELDS
    # and the stub path is the fail-closed one: this caller has no member_scope, so every
    # MEMBER entry must come back SCORED rather than quietly exempt
    mem = [e for e in plan["proofs"]["entries"] if e["task"] != "<union tip>"]
    assert len(mem) == 4 and all(e["proof_required"] is True and e["scope"] == "?" for e in mem)
    assert "Proof scope: 4 of 4 scored, 0 exempt (not-required)" in plan["review_benchmark"]
    tip = [e for e in plan["proofs"]["entries"] if e["task"] == "<union tip>"][0]
    assert tip["scope"] == "-" and tip["proof_required"] is True, (
        "every entry carries the same fields; the tip is scored, never exempt")
    # D73 (§33): the head names the registered candidate and says the records are bound to it
    plan = LaneDriver._review_packet_plan(drv, "RJU", row, {}, wt, "b" * 40, "c" * 40,
                                          {"tasks": {}, "candidate": {"sha": "5deac821" + "0" * 32}})
    first = plan["review_benchmark"].splitlines()[0]
    assert first.startswith("Registered candidate: 5deac821") and "bound to it by design (D27)" in first
    assert plan["review_benchmark"].splitlines()[1].startswith("# Review subject: bbbbbbbbbbbb..cccccccccccc")


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


def _union_env(tmp_path, builder=_fix_builder, deps=("P-FIX-A", "P-FIX-B"), review_base=None):
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
    if review_base:
        pm.write_text(pm.read_text().replace("---\n", "---\nreview_base: %s\n" % review_base, 1))
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    codex = FakeCodex()
    oc = by_role({"builder": builder, "grader": findings("PASS"), "probe": builder})
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
    wt = env.tmp / "wt" / "REVIEW-FIXSET"
    disp = json.loads((wt / ".vp" / "DISPATCH.json").read_text())
    uid = disp["union"]
    n = uid.split("-")[1]
    members = env.run_root / "unions" / n / "members.json"
    assert members.exists(), (env.run_root / "driver.log").read_text()
    doc = json.loads(members.read_text())
    # D39: the review runs on the INTEGRATION union (every verified row), not a partial of its own
    assert doc["union"] == uid and doc["base_sha"] == sha and doc["for"] == ["INTEGRATION"]
    assert doc["branch"] == "vp/v13-%s" % uid and git(env.trunk, "rev-parse", doc["branch"]) == doc["union_sha"]
    # D29: a stale directory at the union path (v12 left union-1..80 of another repo there) is not fatal
    (env.tmp / "wt" / "union-1").mkdir(parents=True, exist_ok=True)
    (env.tmp / "wt" / "union-1" / "stale").write_text("v12")
    have = {m["task"]: m["output_sha"] for m in doc["members"]}
    assert have["P-FIX-A"] == rows["P-FIX-A"]["output_sha"] and have["P-FIX-B"] == rows["P-FIX-B"]["output_sha"]
    assert all(m["merge"] == "clean" for m in doc["members"])
    tip = doc["union_sha"]
    # the union tip is a real commit in trunk that contains both fixes and descends from the candidate
    assert git(env.trunk, "merge-base", "--is-ancestor", sha, tip) == ""
    assert {"platform/a.py", "platform/p_fix_a.py", "platform/p_fix_b.py"} <= set(
        git(env.trunk, "ls-tree", "--name-only", tip, "platform/").splitlines())
    assert "unions.jsonl" in {p.name for p in (env.run_root / "unions").iterdir()}
    # the review dispatched on the union tip: worktree HEAD == tip, subject = candidate..tip
    review = rows["REVIEW-FIXSET"]
    assert review["state"] != "READY", review
    assert disp["union_sha"] == tip and disp["union_base_sha"] == sha
    # D31: the worktree's records attest the reviewed tip, the registered candidate kept alongside
    utree = git(env.trunk, "rev-parse", tip + "^{tree}")
    assert (disp["candidate_sha"], disp["tree_sha"], disp["registered_candidate_sha"]) == (tip, utree, sha)
    con = json.loads((wt / ".vp" / "CONTRACT.json").read_text())["parameters"]
    assert (con["candidate_sha"], con["tree_sha"], con["registered_candidate_sha"], con["union"]) == (tip, utree, sha, uid)
    # D77: the reviewer's worktree carries the members' proof records and the prompt points at them
    proofs = json.loads((wt / ".vp" / "PROOFS.json").read_text())
    assert proofs["union"] == uid and proofs["subject_sha"] == tip
    assert {e["task"] for e in proofs["entries"]} >= {"P-FIX-A", "P-FIX-B"}
    assert {e["output_sha"] for e in proofs["entries"] if e["task"] in have} == set(have.values())
    assert ".vp/PROOFS.json" in lanedriver.REVIEW_PROMPT
    assert env.rows()["REVIEW-FIXSET"]["parameters"]["candidate_sha"] == sha, "the scheduler row is untouched"
    assert {"P-FIX-A", "P-FIX-B"} <= set(disp["union_members"])
    assert disp["owned_files"] == ["control/evidence/REVIEW-FIXSET/%s/verdict-packet.json" % uid]
    req = json.loads((wt / ".vp" / "REVIEW_REQUEST.json").read_text())
    assert (req["base"], req["candidate"], req["subject"]) == (sha, tip, "union")
    assert req["union"] == uid and {"P-FIX-A", "P-FIX-B"} <= set(req["union_members"])
    assert set(req["coverage_targets"]) >= {"L00", "P-FIX-A", "P-FIX-B"}
    assert codex.calls and Path(codex.calls[0].cwd) == wt
    # the verdict packet names the registered candidate (review_gate) AND the union it reviewed
    vp = json.loads(next((env.run_root / "turns" / "REVIEW-FIXSET").rglob("verdict-packet.json")).read_text())
    # D30: keyed by the union tip, the registered candidate kept alongside
    assert vp["candidate_sha"] == tip and vp["tree_sha"] == git(env.trunk, "rev-parse", tip + "^{tree}")
    assert vp["registered_candidate_sha"] == sha and vp["union"] == uid and vp["union_sha"] == tip
    # ... and the real review_gate accepts exactly that key for a non-final role
    import importlib.util
    from test_lanedriver import CONTROL_DIR
    spec = importlib.util.spec_from_file_location("review_gate_live", CONTROL_DIR / "review_gate.py")
    gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
    cand = json.loads(env.state.read_text())["candidate"]
    assert gate._union_subject(vp, cand, "junior") is True
    with pytest.raises(ValueError):
        gate._union_subject(vp, cand, "final_review") or (_ for _ in ()).throw(ValueError("final never"))
    log = (env.run_root / "driver.log").read_text()
    assert "UNION %s %s base=%s for INTEGRATION members=" % (uid, tip[:12], sha[:12]) in log
    assert "UNION %s is the integration union: %d member(s), 0 excluded" % (uid, len(doc["members"])) in log
    assert "UNION %s: REVIEW-FIXSET worktree at %s (%d members)" % (uid, tip[:12], len(doc["members"])) in log
    # refreshed, then idempotent: the newest integration union carries EVERY verified
    # non-review row (rows that verified after the review's union was cut included),
    # and a pass with no new member cuts nothing
    settle(drv, 3)
    integ = [json.loads(m.read_text()) for m in sorted((env.run_root / "unions").glob("*/members.json"),
                                                       key=lambda q: int(q.parent.name))
             if json.loads(m.read_text()).get("for") == ["INTEGRATION"]]
    rows = env.rows()
    import subprocess
    trunk_head = git(env.trunk, "rev-parse", "HEAD")

    def in_trunk(sha):
        return subprocess.run(["git", "-C", str(env.trunk), "merge-base", "--is-ancestor", sha, trunk_head],
                              capture_output=True).returncode == 0
    want = {t: r["output_sha"] for t, r in rows.items() if r.get("state") == "VERIFIED"
            and (r.get("kind") or "") not in ("junior", "security", "final_review", "adjudicator")
            and not in_trunk(r["output_sha"])}
    assert {m["task"]: m["output_sha"] for m in integ[-1]["members"]} == want
    before = sorted(d.name for d in (env.run_root / "unions").iterdir())
    settle(drv, 2)
    assert sorted(d.name for d in (env.run_root / "unions").iterdir()) == before


def test_union_re_review_on_a_later_tip_is_not_a_duplicate(tmp_path, monkeypatch):
    """§26 (2026-09-19): a `<union>` review's scheduler key includes the union tip it
    reviews, so the re-review owed after repairs land on union-N+1 is admitted while
    the REPAIR_REQUIRED review of union-N stands; the same tip is still a duplicate."""
    from test_lanedriver import register_candidate, CONTROL_DIR
    import lanedriver
    if "key_parts.append(parameters[\"union_sha\"])" not in (CONTROL_DIR / "orchestration_control.py").read_text():
        pytest.skip("pinned scheduler predates §26 (review_key without union_sha)")
    monkeypatch.setattr(lanedriver.LaneDriver, "_review_gate_check", lambda self, *a: (True, "PASS: fake gate"))
    env, drv, codex = _union_env(tmp_path)
    sha, tree = register_candidate(env)
    settle(drv, 6)
    integ = [json.loads(m.read_text()) for m in sorted((env.run_root / "unions").glob("*/members.json"),
                                                       key=lambda q: int(q.parent.name))
             if json.loads(m.read_text()).get("for") == ["INTEGRATION"]]
    assert integ, (env.run_root / "driver.log").read_text()
    # §26: a re-review of the same scope on a union tip is keyed by that tip -- not a
    # duplicate of the finished review (REVIEW-JUNIOR-S3-F1..F5 were refused as
    # "duplicate candidate/role/scope" on 2026-09-19 although their repairs had landed).
    # The scheduler half (review_key += union_sha) lands with the owner's re-pin; until
    # the pinned scheduler carries it these assertions are skipped, never faked.
    if "key_parts.append(parameters[\"union_sha\"])" not in (CONTROL_DIR / "orchestration_control.py").read_text():
        pytest.skip("pinned scheduler predates §26 (review_key without union_sha)")
    orig = env.rows()["REVIEW-FIXSET"]
    assert "union_sha" not in (orig["parameters"] or {}), "instantiated before any union existed: keyed as before"
    drv.request_packet_retry("REVIEW-FIXSET", "re-review on the refreshed union (§26)")
    settle(drv, 3)
    rows = env.rows()
    new = rows["REVIEW-FIXSET-R1"]
    assert new["parameters"]["union_sha"] == integ[-1]["union_sha"] and new["parameters"]["union"] == integ[-1]["union"]
    assert new["review_key"] != orig["review_key"], "keyed by the union tip"
    assert new["parameters"]["candidate_sha"] == sha, "the registered candidate still binds the row"
    assert "PACKET_INSTANTIATE_REFUSED REVIEW-FIXSET" not in (env.run_root / "driver.log").read_text()
    # the scheduler's rule itself: the same scope on the SAME union tip is still a duplicate
    # (the -R1 is PLANNED, not exempt); on another tip it is a new review
    import lanedriver
    pp = env.run_root / "packets" / "REVIEW-FIXSET-R1.params.json"
    same = json.loads(pp.read_text())
    dup = env.run_root / "packets" / "dup.params.json"
    other = dict(same, union_sha="f" * 40, union="union-99")
    dup.write_text(json.dumps(other))
    drv.control.instantiate("JUNIOR_REVIEW", "REVIEW-FIXSET-OTHER", "L00", "B", dup, ["L00"])
    assert env.rows()["REVIEW-FIXSET-OTHER"]["review_key"] not in (new["review_key"], orig["review_key"])
    with pytest.raises(lanedriver.ControlError, match="duplicate candidate/role/scope"):
        drv.control.instantiate("JUNIOR_REVIEW", "REVIEW-FIXSET-OTHER-2", "L00", "B", dup, ["L00"])


def test_union_review_is_held_until_a_union_contains_all_its_depends_on_rows(tmp_path):
    """§9(5a): a members.json that lacks one of the packet's depends_on rows
    (or carries a member at a stale output sha) does not dispatch the review."""
    from test_lanedriver import register_candidate
    import lanedriver
    env, drv, codex = _union_env(tmp_path)
    sha, tree = register_candidate(env)
    # the integrator is silenced: only a hand-written, incomplete union exists
    drv._union_step = lambda state: None
    drv._integration_union_step = lambda state: None
    settle(drv, 6)
    rows = env.rows()
    assert rows["P-FIX-A"]["state"] == "VERIFIED" and rows["P-FIX-B"]["state"] == "VERIFIED"
    (env.run_root / "unions" / "1").mkdir(parents=True)
    (env.run_root / "unions" / "1" / "members.json").write_text(json.dumps(
        {"union": "union-1", "n": 1, "base_sha": sha, "union_sha": sha,
         "members": [{"task": "P-FIX-A", "output_sha": rows["P-FIX-A"]["output_sha"]}]}))
    drv.clear_failures("REVIEW-FIXSET")           # re-evaluate now instead of after the 600 s hold
    settle(drv, 3)
    assert env.rows()["REVIEW-FIXSET"]["state"] == "READY", "held: never claimed"
    assert codex.calls == []
    alerts = (env.run_root / "alerts.jsonl").read_text()
    assert "REVIEW_UNION_INCOMPLETE" in alerts and "['P-FIX-B']" in alerts
    log = (env.run_root / "driver.log").read_text()
    assert "HOLD REVIEW-FIXSET 600s REVIEW_UNION_INCOMPLETE" in log
    # D29: the hold sticks across ticks (the scheduler's `ready` touches updated_at every call)
    assert log.count("HOLD REVIEW-FIXSET 600s") == 2, log.count("HOLD REVIEW-FIXSET 600s")   # once per re-evaluation, not per tick
    # a stale member sha is as good as a missing one
    (env.run_root / "unions" / "2").mkdir(parents=True)
    (env.run_root / "unions" / "2" / "members.json").write_text(json.dumps(
        {"union": "union-2", "n": 2, "base_sha": sha, "union_sha": sha,
         "members": [{"task": "P-FIX-A", "output_sha": rows["P-FIX-A"]["output_sha"]},
                     {"task": "P-FIX-B", "output_sha": "f" * 40}]}))
    p = drv.packet_for("REVIEW-FIXSET")
    union, missing = drv._union_for("REVIEW-FIXSET", p, env.rows())
    assert union is None and missing == ["P-FIX-B"]
    # a complete union dispatches it (a hand-made union: the hold is dropped by hand too;
    # the integrator's own union clears it itself, see the integrator test)
    doc = json.loads((env.run_root / "unions" / "2" / "members.json").read_text())
    doc["members"][1]["output_sha"] = rows["P-FIX-B"]["output_sha"]
    (env.run_root / "unions" / "2" / "members.json").write_text(json.dumps(doc))
    union, missing = drv._union_for("REVIEW-FIXSET", p, env.rows())
    assert union["union"] == "union-2" and missing == []
    drv.clear_failures("REVIEW-FIXSET")
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
    cons = [json.loads(c.read_text()) for c in sorted((env.run_root / "unions").glob("*/conflict.json"),
                                                      key=lambda q: int(q.parent.name))]
    mine = [c for c in cons if c["for"] == ["REVIEW-FIXSET"]]
    assert mine, cons
    con = mine[-1]
    assert con["status"] == "CONFLICT" and con["conflict"]["task"] == "P-FIX-B" and [m["task"] for m in con["members"]] == ["P-FIX-A"]
    assert "UNION_CONFLICT" in (env.run_root / "alerts.jsonl").read_text()
    # D39: the integration union excluded the conflicting members instead of failing outright
    integ = [json.loads(m.read_text()) for m in (env.run_root / "unions").glob("*/members.json")
             if json.loads(m.read_text()).get("for") == ["INTEGRATION"]]
    assert integ and integ[-1]["excluded"] and "INTEGRATION_CONFLICT" in (env.run_root / "alerts.jsonl").read_text()
    assert env.rows()["REVIEW-FIXSET"]["state"] == "READY" and codex.calls == []
    assert git(env.trunk, "branch", "--list", "vp/v13-%s" % con["union"]) == ""
    assert not (env.tmp / "wt" / "v13-unions" / con["union"]).exists()
    before = sorted(d.name for d in (env.run_root / "unions").iterdir())
    settle(drv, 2)
    assert sorted(d.name for d in (env.run_root / "unions").iterdir()) == before, "the failed member set is tried once"
    # D43: a refresh (a new member appears) re-excludes the known conflicts at their
    # sha without a merge attempt or a fresh INTEGRATION_CONFLICT alert
    n_alerts = (env.run_root / "alerts.jsonl").read_text().count("INTEGRATION_CONFLICT")
    n_conf = len(list((env.run_root / "unions").glob("*/conflict.json")))
    pd = Path(env.roster["run"]["pack_dir"])
    packet(pd, "P-FIX-C", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="fix C for L00")
    drv._pack_logged = set()
    settle(drv, 6)
    assert env.rows()["P-FIX-C"]["state"] == "VERIFIED"
    integ2 = sorted((json.loads(m.read_text()) for m in (env.run_root / "unions").glob("*/members.json")
                     if json.loads(m.read_text()).get("for") == ["INTEGRATION"]), key=lambda d: d["n"])[-1]
    # P-FIX-C (result_ok: edits a.py too) is a NEW conflict: tried, excluded, alerted once;
    # the members excluded before are re-excluded as `known` with no merge and no alert
    assert integ2["n"] > integ[-1]["n"]
    ex2 = {e["task"]: e for e in integ2["excluded"]}
    old = {e["task"] for e in integ[-1]["excluded"]}
    assert old <= set(ex2) and all(ex2[t].get("known") for t in old)
    assert "P-FIX-C" in ex2 and not ex2["P-FIX-C"].get("known")
    assert (env.run_root / "alerts.jsonl").read_text().count("INTEGRATION_CONFLICT") == n_alerts + 1
    new_conf = len(list((env.run_root / "unions").glob("*/conflict.json"))) - n_conf
    assert new_conf <= 2, "one integration try for P-FIX-C (+ the review's own partial), never the known members"


# -- D28 §10: a dependent build stands on its dependency's verified output ----------------------

def test_dependent_build_is_cut_from_its_dependency_output_and_the_row_records_the_stacked_base(tmp_path):
    """L30-MATRIX-139 built on trunk without L30-REGISTRY-DISCOVERY-R1's exclusion
    literal: the base of a build whose depends_on packet is VERIFIED is that
    packet's output_sha, recorded on the row (stacked_base/stacked_on), in
    dispatch.json and in bindings.jsonl."""
    pd = tmp_path / "pack"
    packet(pd, "P-DEP", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="first fix for L00")
    packet(pd, "P-STACKED", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", deps=["P-DEP"],
           body="builds on P-DEP for L00")
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    seen = {}

    def builder(spec, ab):
        wt = Path(spec.cwd)
        seen[spec.item] = {"head": git(wt, "rev-parse", "HEAD"), "base": (wt / ".vp" / "BASE").read_text().strip(),
                           "has_dep_file": (wt / "platform" / "p_dep.py").exists()}
        return _fix_builder(spec, ab)
    oc = by_role({"builder": builder, "grader": findings("PASS"), "probe": result_ok})
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 6)
    rows = env.rows()
    assert rows["P-DEP"]["state"] == "VERIFIED" and rows["P-STACKED"]["state"] == "VERIFIED", rows
    dep_out = rows["P-DEP"]["output_sha"]
    assert seen["P-DEP"]["base"] == env.base and seen["P-DEP"]["head"] == env.base
    assert seen["P-STACKED"]["base"] == dep_out and seen["P-STACKED"]["head"] == dep_out
    assert seen["P-STACKED"]["has_dep_file"], "the dependency's output is in the tree the builder starts from"
    row = rows["P-STACKED"]
    assert row["base_sha"] == dep_out and row["stacked_base"] == dep_out and row["stacked_on"] == ["P-DEP"]
    assert rows["P-DEP"]["stacked_base"] is None and rows["P-DEP"]["stacked_on"] == []
    assert git(env.trunk, "merge-base", "--is-ancestor", dep_out, row["output_sha"]) == ""
    disp = json.loads(next((env.run_root / "turns" / "P-STACKED").glob("*/dispatch.json")).read_text())
    assert disp["base_sha"] == dep_out and disp["stacked_base"] == dep_out and disp["stacked_on"] == ["P-DEP"]
    vdisp = json.loads((env.tmp / "wt" / "P-STACKED" / ".vp" / "DISPATCH.json").read_text())
    assert vdisp["stacked_base"] == dep_out
    log = (env.run_root / "driver.log").read_text()
    assert "STACKED on P-DEP (output of P-DEP (descends from []))" in log
    ops = [json.loads(l) for l in (env.run_root / "packets" / "bindings.jsonl").read_text().splitlines()]
    st = [o for o in ops if o.get("op") == "stacked"]
    assert st and st[0]["task"] == "P-STACKED" and st[0]["stacked_base"] == dep_out and st[0]["stacked_on"] == ["P-DEP"]
    ev = [json.loads(l) for l in (env.run_root / "control.jsonl").read_text().splitlines()
          if json.loads(l).get("verb") == "claim" and "P-STACKED" in json.loads(l)["argv"]]
    assert ev and "--stacked-base" in ev[0]["argv"]


def test_divergent_dependency_outputs_hold_the_build_until_the_integrator_cuts_their_union(tmp_path):
    """two VERIFIED deps neither of which descends from the other: no guessed
    merge -- STACKED_BASE_MISSING hold, then the integrator's union is the base"""
    pd = tmp_path / "pack"
    packet(pd, "P-FIX-A", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="fix A for L00")
    packet(pd, "P-FIX-B", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="fix B for L00")
    packet(pd, "P-ON-BOTH", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder",
           deps=["P-FIX-A", "P-FIX-B"], body="builds on both fixes for L00")
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    seen = {}

    def builder(spec, ab):
        wt = Path(spec.cwd)
        seen[spec.item] = {"head": git(wt, "rev-parse", "HEAD"),
                           "files": sorted(p.name for p in (wt / "platform").glob("p_*.py"))}
        return _fix_builder(spec, ab)
    oc = by_role({"builder": builder, "grader": findings("PASS"), "probe": result_ok})
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    drv._union_step = lambda state: None          # first: no integrator -> the build holds
    drv._integration_union_step = lambda state: None
    settle(drv, 6)
    rows = env.rows()
    assert rows["P-FIX-A"]["state"] == "VERIFIED" and rows["P-FIX-B"]["state"] == "VERIFIED"
    assert rows["P-ON-BOTH"]["state"] == "READY" and "P-ON-BOTH" not in seen
    alerts = (env.run_root / "alerts.jsonl").read_text()
    assert "STACKED_BASE_MISSING" in alerts and "divergent dependency outputs ['P-FIX-A', 'P-FIX-B']" in alerts
    assert "HOLD P-ON-BOTH 600s STACKED_BASE_MISSING" in (env.run_root / "driver.log").read_text()
    # the integrator cuts the integration union (D39: every verified row, the two fixes among
    # them) and lifts the hold itself; the build stands on its tip
    del drv._union_step
    del drv._integration_union_step
    settle(drv, 6)
    rows = env.rows()
    assert rows["P-ON-BOTH"]["state"] == "VERIFIED", rows["P-ON-BOTH"]
    docs = [json.loads(m.read_text()) for m in sorted((env.run_root / "unions").glob("*/members.json"),
                                                      key=lambda q: int(q.parent.name))]
    # the build stood on the integration union of that moment (a newer one, carrying
    # P-ON-BOTH itself, is cut once it verifies: the union is kept refreshed)
    doc = [d for d in docs if d.get("union_sha") == rows["P-ON-BOTH"]["stacked_base"]][0]
    assert doc["for"] == ["INTEGRATION"] and {"P-FIX-A", "P-FIX-B"} <= {m["task"] for m in doc["members"]}
    assert "P-ON-BOTH" in {m["task"] for m in docs[-1]["members"]}, "refreshed after the build verified"
    assert seen["P-ON-BOTH"]["head"] == doc["union_sha"] and {"p_fix_a.py", "p_fix_b.py"} <= set(seen["P-ON-BOTH"]["files"])
    assert rows["P-ON-BOTH"]["stacked_base"] == doc["union_sha"] and rows["P-ON-BOTH"]["stacked_on"] == ["P-FIX-A", "P-FIX-B"]


# -- D32: stacked-or-wait check, FAIL_AFTER_RETRY, OWED_RULINGS -------------------------------

def test_build_whose_scheduler_dependency_is_a_verified_fix_not_in_its_base_is_held(tmp_path, monkeypatch):
    """Advisor gap 2: a row whose scheduler depends_on names a pack-bound fix
    that is VERIFIED and not in trunk must stand on it or wait -- never build
    on trunk (WA-01-DP09-R2, L32-BINDINGS-TG went red exactly that way)."""
    import lanedriver
    pd = tmp_path / "pack"
    packet(pd, "P-DEP", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="first fix for L00")
    packet(pd, "P-STACKED", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", deps=["P-DEP"],
           body="builds on P-DEP for L00")
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    oc = by_role({"builder": _fix_builder, "grader": findings("PASS"), "probe": result_ok})
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    # the stack plan is silenced (as if the packet-level stacking did not carry the dep)
    monkeypatch.setattr(lanedriver.LaneDriver, "_stack_plan", lambda self, task, tasks: None)
    settle(drv, 6)
    rows = env.rows()
    assert rows["P-DEP"]["state"] == "VERIFIED"
    assert rows["P-STACKED"]["state"] == "READY", rows["P-STACKED"]["state"]
    alerts = (env.run_root / "alerts.jsonl").read_text()
    assert "STACK_REQUIRED" in alerts and "['P-DEP']" in alerts
    assert "HOLD P-STACKED 600s STACK_REQUIRED" in (env.run_root / "driver.log").read_text()
    # with the real stack plan the same row builds on the dependency
    monkeypatch.undo()
    drv.clear_failures("P-STACKED")
    settle(drv, 4)
    rows = env.rows()
    assert rows["P-STACKED"]["state"] == "VERIFIED" and rows["P-STACKED"]["stacked_on"] == ["P-DEP"]


def test_d63_base_invariant_holds_a_claim_whose_base_lacks_a_verified_dependency(tmp_path, monkeypatch):
    """D63: with every stacking patch blind (no stack plan, no D32 check) the
    dispatch-time invariant alone refuses to claim P-STACKED on a trunk that
    does not contain P-DEP's VERIFIED output; with the patches back it builds
    stacked and the invariant stays silent; with the invariant itself silenced
    the row is claimed on bare trunk (the guard is on the path, not decorative)."""
    import lanedriver
    pd = tmp_path / "pack"
    packet(pd, "P-DEP", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="first fix for L00")
    packet(pd, "P-STACKED", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", deps=["P-DEP"],
           body="builds on P-DEP for L00")
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    oc = by_role({"builder": _fix_builder, "grader": findings("PASS"), "probe": result_ok})
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    monkeypatch.setattr(lanedriver.LaneDriver, "_stack_plan", lambda self, task, tasks: None)
    monkeypatch.setattr(lanedriver.LaneDriver, "_unstacked_deps", lambda self, row, tasks, plan: [])
    settle(drv, 6)
    rows = env.rows()
    assert rows["P-DEP"]["state"] == "VERIFIED"
    assert rows["P-STACKED"]["state"] == "READY", rows["P-STACKED"]["state"]
    alerts = (env.run_root / "alerts.jsonl").read_text()
    assert "BASE_INVARIANT" in alerts and "P-DEP@%s" % rows["P-DEP"]["output_sha"][:12] in alerts
    assert "STACK_REQUIRED" not in alerts, "the D32 check was silenced: the invariant caught it alone"
    assert "HOLD P-STACKED 600s BASE_INVARIANT" in (env.run_root / "driver.log").read_text()
    # the stacking patches back: the row builds on the dependency, the invariant agrees
    monkeypatch.undo()
    drv.clear_failures("P-STACKED")
    settle(drv, 4)
    rows = env.rows()
    assert rows["P-STACKED"]["state"] == "VERIFIED" and rows["P-STACKED"]["stacked_on"] == ["P-DEP"]
    assert (env.run_root / "alerts.jsonl").read_text().count("BASE_INVARIANT") == 1
    # a fresh run with the invariant silenced and the patches blind claims on bare trunk:
    # the negative control that proves the guard above did the holding
    pd2 = tmp_path / "pack2"
    packet(pd2, "Q-DEP", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="fix for L00")
    packet(pd2, "Q-STACKED", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", deps=["Q-DEP"],
           body="builds on Q-DEP for L00")
    (tmp_path / "two").mkdir()
    env2 = Env(tmp_path / "two", roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                               "proof": {"require_for_kinds": []}})
    env2.roster["run"]["pack_dir"] = str(pd2)
    (env2.run_root / "roster.json").write_text(json.dumps(env2.roster, indent=2))
    env2.activate()
    drv2 = env2.driver({"opencode": by_role({"builder": _fix_builder, "grader": findings("PASS"), "probe": result_ok}),
                        "codex": FakeRunner(), "claude": FakeRunner()})
    monkeypatch.setattr(lanedriver.LaneDriver, "_stack_plan", lambda self, task, tasks: None)
    monkeypatch.setattr(lanedriver.LaneDriver, "_unstacked_deps", lambda self, row, tasks, plan: [])
    monkeypatch.setattr(lanedriver.LaneDriver, "_base_invariant", lambda self, task, row, base, stacked, tasks: [])
    settle(drv2, 6)
    rows2 = env2.rows()
    assert rows2["Q-STACKED"]["state"] == "VERIFIED" and not rows2["Q-STACKED"].get("stacked_on"), rows2["Q-STACKED"]
    assert "BASE_INVARIANT" not in (env2.run_root / "alerts.jsonl").read_text()


def test_a_retry_going_red_alerts_fail_after_retry(tmp_path):
    pd = tmp_path / "pack"
    packet(pd, "P-GAP", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="gap for L00")
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    runner = by_role({"builder": result_ok, "grader": findings("FAIL"), "probe": result_ok})
    drv = env.driver({"opencode": runner, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 4)
    assert env.rows()["P-GAP"]["state"] == "REPAIR_REQUIRED"
    alerts = (env.run_root / "alerts.jsonl").read_text()
    assert "FAIL_AFTER_RETRY" not in alerts, "the first round is not a retry"
    drv.request_packet_retry("P-GAP", "again")
    settle(drv, 4)
    assert env.rows()["P-GAP-R1"]["state"] == "REPAIR_REQUIRED"
    line = next(json.loads(l) for l in (env.run_root / "alerts.jsonl").read_text().splitlines()
                if json.loads(l).get("kind") == "FAIL_AFTER_RETRY")
    assert line["task"] == "P-GAP-R1" and "REPAIR_REQUIRED" in line["text"]


def test_idle_with_rows_awaiting_a_ruling_alerts_once_per_idle_stretch(tmp_path):
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "owed_rulings_after_s": 0}})
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner(), "claude": FakeRunner()})
    drv._idle_since = "2026-09-18T00:00:00.000Z"
    drv._owed_rulings_check({"REPAIR_REQUIRED": 1, "INVALID_EVIDENCE": 2}, {"A": {"state": "REPAIR_REQUIRED"},
                                                                          "B": {"state": "INVALID_EVIDENCE"},
                                                                          "C": {"state": "VERIFIED"}})
    drv._owed_rulings_check({"REPAIR_REQUIRED": 1, "INVALID_EVIDENCE": 2}, {"A": {"state": "REPAIR_REQUIRED"}})
    lines = [json.loads(l) for l in (env.run_root / "alerts.jsonl").read_text().splitlines()
             if json.loads(l).get("kind") == "OWED_RULINGS"]
    # D178: the alert now counts PROBLEMS, not attempts -- 64 owed rows in the
    # live run were 19 distinct problems, a 3.4x over-report that was read as
    # "48 stale ancestors never auto-retired". Measured: ZERO had a VERIFIED
    # successor, so retiring them would have deleted the attempt depth instead
    # of fixing anything.
    #
    # D178a: this fixture is ALSO the case where the two sources disagree --
    # `counts` says 3 owed while the task map holds 2 owed rows (C is VERIFIED).
    # The pre-D178 text quoted "3 row(s)" beside a list of two and made that
    # invisible. Both numbers are now named, and the disagreement is stated.
    assert len(lines) == 1, lines
    txt = lines[0]["text"]
    assert "2 problem(s) across 2 row(s)" in txt, txt
    assert "frontier counts 3" in txt and "the two views disagree" in txt, txt
    assert "A, B" in txt, txt
    # nothing owed, or not idle long enough: silent
    drv._owed_rulings_check({"VERIFIED": 5}, {})
    drv2 = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner(), "claude": FakeRunner()})
    drv2.alerts_cfg["owed_rulings_after_s"] = 1800
    drv2._idle_since = drv2._idle_since or __import__("lanedriver").utc_ms()
    drv2._owed_rulings_check({"REPAIR_REQUIRED": 1}, {"A": {"state": "REPAIR_REQUIRED"}})
    assert sum(1 for l in (env.run_root / "alerts.jsonl").read_text().splitlines()
               if json.loads(l).get("kind") == "OWED_RULINGS") == 1


# -- D33 §13: closure is a state -- the sweep retires targets the harvest-time hook missed ------

def test_closure_sweep_retires_a_target_whose_closer_verified_before_the_hook_could_act(tmp_path):
    """L31-V13/L34-V13 -> JR-REVIEW-18/18B/20: a closer's `closes` is honoured
    from the rows on every reconcile, not only at the closer's harvest."""
    pd = tmp_path / "pack"
    packet(pd, "P-REGRADE-L00", "L00", closes=["JX"])
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    params = env.tmp / "p.json"
    params.write_text(json.dumps({"parent_contract_id": "L00", "unproved_criterion": "x", "candidate_sha": env.base,
                                  "test_location": "platform/a.py", "owned_paths": ["platform/a.py"]}))
    env.control_call("instantiate", "--template", "TEST_GAP", "--task", "JX", "--parent-contract", "L00",
                     "--chief", "B", "--parameters-json", str(params))
    env.control_call("block", "--task", "JX", "--blocker-class", "EXTERNAL", "--reason", "old", "--unblock-action", "n/a",
                     "--evidence", str(params))
    oc = by_role({"builder": result_ok, "grader": findings("PASS"), "probe": result_ok})
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    drv._pack_closure = lambda task, harvest, wt, only=None: None     # the harvest-time hook is asleep
    drv._closure_sweep = lambda state: None                           # and so is the sweep
    settle(drv, 6)
    rows = env.rows()
    # the packet bound DIRECT to L00 (unfinished at bind time): L00's own verification is the closer's
    assert rows["L00"]["state"] == "VERIFIED" and rows["JX"]["state"] == "BLOCKED", "nothing retired JX at harvest"
    del drv._pack_closure                                             # the real hook and sweep are back
    del drv._closure_sweep
    drv._pack_last_mono = None
    drv.tick()
    rows = env.rows()
    assert rows["JX"]["state"] == "CANCELLED" and rows["JX"]["blocker"]["reason"] == "closed by packet P-REGRADE-L00"
    log = (env.run_root / "driver.log").read_text()
    assert "PACK closure-sweep P-REGRADE-L00 (L00 VERIFIED): retire ['JX'] promote []" in log
    closures = [json.loads(l) for l in (env.run_root / "packets" / "closures.jsonl").read_text().splitlines()]
    assert any(c.get("sweep") and c["retired"] == ["JX"] for c in closures)
    # idempotent: a second sweep does nothing
    drv._pack_last_mono = None
    drv.tick()
    assert log.count("closure-sweep") == (env.run_root / "driver.log").read_text().count("closure-sweep")


# -- D36 §16: builders/graders of a stacked row are told what `base` means -------------------

def test_stacked_row_prompts_carry_the_base_rule_and_unstacked_rows_do_not(tmp_path):
    pd = tmp_path / "pack"
    packet(pd, "P-DEP", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="first fix for L00")
    packet(pd, "P-STACKED", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", deps=["P-DEP"],
           body="builds on P-DEP for L00")
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    prompts = {}

    def builder(spec, ab):
        prompts.setdefault(spec.item, {})["builder"] = spec.prompt
        return _fix_builder(spec, ab)

    def grader(spec, ab):
        prompts.setdefault(spec.item, {})["grader"] = spec.prompt
        return findings("PASS")(spec, ab)
    oc = by_role({"builder": builder, "grader": grader, "probe": result_ok})
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 6)
    rows = env.rows()
    assert rows["P-STACKED"]["state"] == "VERIFIED" and rows["P-STACKED"]["stacked_on"] == ["P-DEP"]
    dep_out = rows["P-DEP"]["output_sha"]
    for role in ("builder", "grader"):
        assert "BASE RULE" not in prompts["P-DEP"][role], "an unstacked row gets no note"
        note = prompts["P-STACKED"][role]
        assert "BASE RULE" in note and dep_out[:12] in note and ("a" * 12) in note   # packet base_sha named
        assert "reports UNKNOWN, never FAIL" in note


BM_HOSTED = ("- B1 [evidence] [box] {id} regraded — check: control/evidence/{id}/v13/REGRADE.md\n"
             "- B2 [invariant] [hosted] full CircleCI matrix green on the sha (gate: CIRCLECI)\n"
             "- B3 [invariant] [hosted] restore leg runs on the VPS (gate: DELIVERY-2A)\n"
             "- B4 [negative] [hosted] deploy places no provider call (gate: DELIVERY-2A)\n")


def test_hosted_twins_one_per_gate_held_by_their_own_gate(tmp_path):
    """D37 (PACKET-FORMAT §6, §19): a packet with [hosted] rows yields one
    synthetic twin per gate; template by gate; a twin is held by ITS gate,
    never the header's; its benchmark is exactly its rows."""
    pd = tmp_path / "pack"
    packet(pd, "P-PAR", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", gate="DELIVERY-2B",
           body="parent for L00")
    (pd / "P-PAR" / "BENCHMARK.md").write_text(BM_HOSTED.format(id="P-PAR"))
    packet(pd, "P-BOX", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="box only for L00")
    pack, lint = vppack.load_pack(pd)
    assert lint == [] and set(pack) == {"P-PAR", "P-BOX", "P-PAR-HOSTED-CIRCLECI", "P-PAR-HOSTED-DELIVERY-2A"}
    cc, va = pack["P-PAR-HOSTED-CIRCLECI"], pack["P-PAR-HOSTED-DELIVERY-2A"]
    assert cc["template"] == "TEST_GAP" and cc["hosted_rows"] == ["B2"] and cc["test_paths"] == [] and cc["max_rounds"] == 1
    assert va["template"] == "EXTERNAL_PREP" and va["hosted_rows"] == ["B3", "B4"] and va["test_paths"] == ["platform/a.py"]
    assert cc["depends_on"] == va["depends_on"] == ["P-PAR"] and cc["closes"] == [] and not cc["hosted_owed"]
    assert vppack.bind(cc, {}) == ("dynamic", "P-PAR-HOSTED-CIRCLECI", "TEST_GAP")
    assert vppack.topo_order(pack).index("P-PAR") < vppack.topo_order(pack).index("P-PAR-HOSTED-DELIVERY-2A")
    # the header says DELIVERY-2B (closed); the twins go by their rows' gates
    roster = {"owner_gates": {"DELIVERY-1": True, "DELIVERY-2A": True, "DELIVERY-2B": False}}
    assert vppack.owner_gate_open(pack["P-PAR"], roster) and vppack.owner_gate_open(cc, roster)
    assert vppack.owner_gate_open(va, roster)
    assert not vppack.owner_gate_open(va, {"owner_gates": {"DELIVERY-2B": True}})
    assert not vppack.owner_gate_open(cc, {"owner_gates": {"CIRCLECI": True}}), "CIRCLECI rows open with DELIVERY-1"
    assert not vppack.owner_gate_open(cc, {}), "no map = closed"
    bm = vppack.twin_benchmark(va)
    assert "B3 " in bm and "B4 " in bm and "B1 " not in bm and "B2 " not in bm
    txt = vppack.twin_packet_text(va, "c" * 40)
    hdr, rest = vplint.parse_front_matter(txt)
    assert hdr["item"] == "P-PAR-HOSTED-DELIVERY-2A" and hdr["base_sha"] == "c" * 40 and hdr["owner_gate"] == "DELIVERY-2A"
    assert hdr["depends_on"] == ["P-PAR"] and hdr["test_paths"] == ["platform/a.py"] and hdr["twin_of"] == "P-PAR"
    assert hdr["v13_kind"] == "repair" and hdr["proof_kind"] == "platform", "the rest of the parent header is kept"
    assert "<!-- HOSTED TWIN P-PAR-HOSTED-DELIVERY-2A" in txt and "voicepod-vps.internal" in txt
    assert "## Goal" in txt and "item: P-PAR\n" not in txt, "the parent's BODY follows the preamble"
    cc_hdr, _ = vplint.parse_front_matter(vppack.twin_packet_text(cc, "c" * 40))
    assert cc_hdr["test_paths"] == [] and cc_hdr["max_rounds"] == 1, "no targeted paths: the proof is the full suite"
    # a single-gate packet's twin keeps the bare -HOSTED name (§19(1))
    packet(pd, "P-ONE", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="one gate for L00")
    (pd / "P-ONE" / "BENCHMARK.md").write_text(
        "- B1 [evidence] [box] x — check: y\n- B2 [invariant] [hosted] y (gate: CIRCLECI)\n")
    pack, lint = vppack.load_pack(pd)
    assert "P-ONE-HOSTED" in pack and lint == []
    # a hosted row without a gate joins no twin and is a lint error
    (pd / "P-ONE" / "BENCHMARK.md").write_text("- B2 [invariant] [hosted] y\n")
    pack, lint = vppack.load_pack(pd)
    assert "P-ONE-HOSTED" not in pack and any("P-ONE B2 is [hosted] but names no (gate" in l for l in lint)


def test_hosted_twin_proves_the_integration_union_tip_once_the_required_repairs_are_carried(tmp_path):
    """D46 (Architect §22/§24): with roster packet.twin_base = integration_union, a
    full-suite twin waits (no strike) until every `requires` row is VERIFIED and the
    integration union carries it and the parent; then it is cut from the union tip,
    not the parent's output alone (trunk's full suite is red until those land)."""
    pd = tmp_path / "pack"
    packet(pd, "P-PAR", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", gate="DELIVERY-2B",
           body="parent for L00")
    (pd / "P-PAR" / "BENCHMARK.md").write_text(BM_HOSTED.format(id="P-PAR"))
    packet(pd, "P-FIX", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", gate="none",
           body="the seed fix for L00")
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "packet": {"twin_dependents": [],
                                                 "twin_base": {"kind": "integration_union", "requires": ["P-FIX"]}},
                                      "owner_gates": {"DELIVERY-1": True},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    seen = {}
    gate = {"P-FIX": True}          # P-FIX's builder is refused until the test opens it

    def builder(spec, ab):
        if gate.get(spec.item):
            return TurnOutcome("RUNNER_EMPTY", "held by the test", runner="fake")
        wt = Path(spec.cwd)
        seen[spec.item] = {"base": (wt / ".vp" / "BASE").read_text().strip()}
        return _fix_builder(spec, ab)
    oc = by_role({"builder": builder, "grader": findings("PASS"), "probe": builder})
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 8)
    rows = env.rows()
    assert rows["P-PAR"]["state"] == "VERIFIED"
    par_out = rows["P-PAR"]["output_sha"]
    assert rows["P-PAR-HOSTED-CIRCLECI"]["state"] in ("READY", "PLANNED", "WAITING_DEPENDENCY"), rows["P-PAR-HOSTED-CIRCLECI"]
    log = (env.run_root / "driver.log").read_text()
    assert "TWIN_BASE_WAIT P-PAR-HOSTED-CIRCLECI: requires not VERIFIED ['P-FIX']" in log
    assert "P-PAR-HOSTED-CIRCLECI" not in seen, "never cut from the parent output alone"
    # the repair lands, the integration union carries both, the twin proves the union tip
    gate["P-FIX"] = False
    drv._fail.pop("P-FIX", None)
    settle(drv, 10)
    rows = env.rows()
    assert rows["P-FIX"]["state"] == "VERIFIED"
    cc = rows["P-PAR-HOSTED-CIRCLECI"]
    assert cc["state"] == "VERIFIED", cc
    docs = [d for d in drv._integration_docs() if d.get("status") == "BUILT"]
    # cut from the integration union that was the tip at claim time: it carries the
    # parent and the repair (the union re-cut after the twin verified carries the twin too)
    tip = next(d for d in docs if d["union_sha"] == cc["stacked_base"])
    assert {m["task"] for m in tip["members"]} >= {"P-PAR", "P-FIX"} and tip["union_sha"] != par_out
    assert set(cc["stacked_on"]) >= {"P-PAR", "P-FIX"}
    assert seen["P-PAR-HOSTED-CIRCLECI"]["base"] == tip["union_sha"]


def test_d59_twin_union_base_stacks_on_the_carried_retry_row_not_the_packet_name(tmp_path, monkeypatch):
    """D59: 2026-09-19 14:36-14:42Z L09-SEED-FIX-HOSTED struck out 3/3 and
    LINT-TYPECHECK-TRUNK-HOSTED 2/3 on `--stacked-on L09-SEED-FIX: not a row with
    an output_sha`: twin_base.requires names the PACKET, the VERIFIED row was the
    retry L09-SEED-FIX-R4, and D46 passed the packet name to the scheduler as a
    stacked_on row.  The carried ROW goes to the claim."""
    import lanedriver
    monkeypatch.setattr(lanedriver, "FAIL_BACKOFF_S", (0, 0, 0))
    pd = tmp_path / "pack"
    packet(pd, "P-PAR", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", gate="DELIVERY-2B",
           body="parent for L00")
    (pd / "P-PAR" / "BENCHMARK.md").write_text(BM_HOSTED.format(id="P-PAR"))
    packet(pd, "P-FIX", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", gate="none",
           body="the seed fix for L00")
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "packet": {"twin_dependents": [],
                                                 "twin_base": {"kind": "integration_union", "requires": ["P-FIX"]}},
                                      "owner_gates": {"DELIVERY-1": True},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    crash = {"P-FIX": True}         # the first P-FIX row strikes out; its retry P-FIX-R1 is the one that lands

    def builder(spec, ab):
        if crash.get(spec.item):
            return TurnOutcome("RUNNER_CRASH", "boom", runner="fake")
        return _fix_builder(spec, ab)
    oc = by_role({"builder": builder, "grader": findings("PASS"), "probe": builder})
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 10)
    rows = env.rows()
    assert rows["P-PAR"]["state"] == "VERIFIED" and rows["P-FIX"]["state"] == "INVALID_EVIDENCE", rows["P-FIX"]
    drv.request_packet_retry("P-FIX", "crash was infra; retry")
    settle(drv, 12)
    rows = env.rows()
    assert rows["P-FIX-R1"]["state"] == "VERIFIED", rows.get("P-FIX-R1")
    cc = rows["P-PAR-HOSTED-CIRCLECI"]
    assert cc["state"] == "VERIFIED", (cc, (env.run_root / "driver.log").read_text()[-3000:])
    assert "P-FIX-R1" in cc["stacked_on"] and "P-FIX" not in cc["stacked_on"], cc["stacked_on"]
    log = (env.run_root / "driver.log").read_text()
    assert "not a row with an output_sha" not in log, "the packet name never reaches --stacked-on"


def test_hosted_twin_runs_on_the_parents_verified_output_and_records_per_row_verdicts(tmp_path):
    """D37 driver half: the twin is instantiated once the parent has a VERIFIED
    output, cut from exactly that sha (§19(3)), its worktree carries the twin
    packet/benchmark, L42-class dependents wait on it (§19(4)), and its
    verdicts land on the parent's hosted record with box_only (§19(5))."""
    pd = tmp_path / "pack"
    packet(pd, "P-PAR", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", gate="DELIVERY-2B",
           body="parent for L00")
    (pd / "P-PAR" / "BENCHMARK.md").write_text(BM_HOSTED.format(id="P-PAR"))
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "packet": {"twin_dependents": ["L04"]},
                                      "owner_gates": {"DELIVERY-1": True},
                                      "proof": {"require_for_kinds": []}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    seen = {}

    def builder(spec, ab):
        wt = Path(spec.cwd)
        seen[spec.item] = {"base": (wt / ".vp" / "BASE").read_text().strip(),
                           "packet": (wt / ".vp" / "PACKET.md").read_text(),
                           "bench": (wt / ".vp" / "BENCHMARK.md").read_text()}
        return _fix_builder(spec, ab)
    oc = by_role({"builder": builder, "grader": findings("PASS"), "probe": builder})
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    drv.tick()
    rows = env.rows()
    assert "P-PAR" in rows and "P-PAR-HOSTED-CIRCLECI" not in rows, "no twin before the parent's output exists"
    assert "P-PAR-HOSTED-CIRCLECI waits: parent P-PAR" in (env.run_root / "driver.log").read_text()
    settle(drv, 8)
    rows = env.rows()
    assert rows["P-PAR"]["state"] == "VERIFIED"
    par_out = rows["P-PAR"]["output_sha"]
    cc = rows["P-PAR-HOSTED-CIRCLECI"]
    assert cc["state"] == "VERIFIED" and cc["template_id"] == "TEST_GAP" and cc["depends_on"] == ["P-PAR"]
    assert cc["stacked_base"] == par_out and cc["stacked_on"] == ["P-PAR"] and cc["base_sha"] == par_out
    assert seen["P-PAR-HOSTED-CIRCLECI"]["base"] == par_out
    assert "<!-- HOSTED TWIN P-PAR-HOSTED-CIRCLECI" in seen["P-PAR-HOSTED-CIRCLECI"]["packet"]
    assert seen["P-PAR-HOSTED-CIRCLECI"]["packet"].startswith("---\n")
    assert [l.split()[1] for l in seen["P-PAR-HOSTED-CIRCLECI"]["bench"].splitlines() if l.startswith("- B")] == ["B2"]
    params = json.loads((env.run_root / "packets" / "P-PAR-HOSTED-CIRCLECI.params.json").read_text())
    assert params["candidate_sha"] == par_out and "(gate: CIRCLECI)" in params["unproved_criterion"]
    # the DELIVERY-2A twin waits for its own gate, not the header's DELIVERY-2B
    assert "P-PAR-HOSTED-DELIVERY-2A" not in rows
    alerts = (env.run_root / "OWNER-ALERTS.md").read_text()
    assert "P-PAR-HOSTED-DELIVERY-2A waits for DELIVERY-2A" in alerts
    # L04 (roster packet.twin_dependents) now waits on the twin as well
    assert "P-PAR-HOSTED-CIRCLECI" in rows["L04"]["depends_on"]
    # §19(5): per-row verdicts on the parent's hosted record; box_only until every twin is VERIFIED
    rec = json.loads((env.run_root / "hosted" / "P-PAR.json").read_text())
    assert rec["hosted_rows"]["B2"]["verdict"] == "PASS" and rec["hosted_rows"]["B2"]["twin"] == "P-PAR-HOSTED-CIRCLECI"
    assert rec["box_only"] is True and rec["twins_expected"] == ["P-PAR-HOSTED-CIRCLECI", "P-PAR-HOSTED-DELIVERY-2A"]
    # open DELIVERY-2A: the second twin instantiates, runs on the same parent output, clears box_only
    roster = json.loads((env.run_root / "roster.json").read_text())
    roster["owner_gates"] = {"DELIVERY-1": True, "DELIVERY-2A": True}
    (env.run_root / "roster.json").write_text(json.dumps(roster, indent=2))
    drv._roster_mtime = 0
    settle(drv, 8)
    rows = env.rows()
    va = rows["P-PAR-HOSTED-DELIVERY-2A"]
    # D41: EXTERNAL_PREP is a non-build kind (probe here, infra live) and still stands on the parent output
    assert va["state"] == "VERIFIED" and va["template_id"] == "EXTERNAL_PREP" and va["stacked_base"] == par_out
    assert va["kind"] == "probe" and va["stacked_on"] == ["P-PAR"]
    assert seen["P-PAR-HOSTED-DELIVERY-2A"]["base"] == par_out
    # L04 ran once its first twin dependency verified; a dependent that already
    # finished is not rewired (only PLANNED/WAITING/READY rows are)
    assert rows["L04"]["state"] == "VERIFIED" and "P-PAR-HOSTED-CIRCLECI" in rows["L04"]["depends_on"]
    log = (env.run_root / "driver.log").read_text()
    assert "PACK rewired L04.depends_on += P-PAR-HOSTED-CIRCLECI (hosted twin)" in log
    rec = json.loads((env.run_root / "hosted" / "P-PAR.json").read_text())
    # D45: the EXTERNAL_PREP twin ran as a probe turn -- no grader, no FINDINGS.json -- so its
    # rows are UNKNOWN (source "default"), never a PASS inferred from the VERIFIED outcome
    # (L31/L34 B9, 2026-09-18); box_only clears only when every hosted row is PASS
    assert {k: v["verdict"] for k, v in rec["hosted_rows"].items()} == {"B2": "PASS", "B3": "UNKNOWN", "B4": "UNKNOWN"}
    assert {k: v["source"] for k, v in rec["hosted_rows"].items()} == {"B2": "findings", "B3": "default", "B4": "default"}
    assert rec["box_only"] is True
    # ... and a graded twin clears it
    fpath = env.tmp / "graded" / "FINDINGS.json"
    fpath.parent.mkdir()
    fpath.write_text(json.dumps({"lines": [{"id": "B3", "verdict": "PASS"}, {"id": "B4", "verdict": "PASS"}]}))
    drv._twin_record("P-PAR-HOSTED-DELIVERY-2A", "VERIFIED", fpath, "a-graded")
    rec = json.loads((env.run_root / "hosted" / "P-PAR.json").read_text())
    assert {k: v["verdict"] for k, v in rec["hosted_rows"].items()} == {"B2": "PASS", "B3": "PASS", "B4": "PASS"}
    assert rec["box_only"] is False


def test_d58_union_review_with_a_review_base_reviews_the_union_tip_from_that_base(tmp_path, monkeypatch):
    """§30: REVIEW-JUNIOR-S3-F1..F5 carry review_base (the s3 delta's base) AND
    <union>; _union_review excluded review_base packets, so all five reviewed the
    trunk delta again although union-17 existed.  Now: candidate = union tip,
    base = review_base, subject = union."""
    from test_lanedriver import FakeCodex, register_candidate, git
    import lanedriver
    monkeypatch.setattr(lanedriver.LaneDriver, "_review_gate_check", lambda self, *a: (True, "PASS: fake gate"))
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "proof": {"require_for_kinds": []}})
    # review_base = the first trunk commit; the registered candidate is a later one (the s3 delta shape)
    (env.trunk / "platform" / "a.py").write_text("x = 3  # delta\n")
    git(env.trunk, "commit", "-qam", "delta")
    pd = tmp_path / "pack"
    packet(pd, "P-FIX-A", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="fix A for L00")
    packet(pd, "P-FIX-B", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="fix B for L00")
    packet(pd, "REVIEW-FIXSET", "NEW:JUNIOR_REVIEW", kind="review", template="JUNIOR_REVIEW", role="junior",
           deps=["P-FIX-A", "P-FIX-B"], body="one review of the union of the fixes of L00")
    pm = pd / "REVIEW-FIXSET" / "PACKET.md"
    pm.write_text(pm.read_text().replace("  - control/evidence/REVIEW-FIXSET/v13/REGRADE.md",
                                         "  - control/evidence/REVIEW-FIXSET/<union>/verdict-packet.json")
                  .replace("---\n", "---\ncoverage_targets: [<union>, L00]\nreview_base: %s\n" % env.base, 1))
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    codex = FakeCodex()
    oc = by_role({"builder": _fix_builder, "grader": findings("PASS"), "probe": _fix_builder})
    drv = env.driver({"opencode": oc, "codex": codex, "claude": FakeRunner()})
    sha, _tree = register_candidate(env)
    assert sha != env.base
    settle(drv, 6)
    rows = env.rows()
    assert rows["P-FIX-A"]["state"] == "VERIFIED" and rows["P-FIX-B"]["state"] == "VERIFIED", rows
    wt = env.tmp / "wt" / "REVIEW-FIXSET"
    disp = json.loads((wt / ".vp" / "DISPATCH.json").read_text())
    tip = disp["union_sha"]
    assert tip and tip != sha, "dispatched on a union tip"
    req = json.loads((wt / ".vp" / "REVIEW_REQUEST.json").read_text())
    assert req["candidate"] == tip, "the union tip is the candidate, not the trunk"
    assert req["base"] == env.base, "the diff base is review_base, not the union's base"
    assert req["subject"] == "union" and req["union"] == disp["union"]
    assert "<union>" not in req["coverage_targets"] and {"P-FIX-A", "P-FIX-B"} <= set(req["union_members"])


BM_CIRCLECI_ONLY = ("- B1 [evidence] [box] {id} regraded — check: control/evidence/{id}/v13/REGRADE.md\n"
                    "- B2 [invariant] [hosted] full CircleCI matrix green on the sha (gate: CIRCLECI)\n")


def test_d71_canary_gate_holds_every_other_circleci_twin_until_the_canary_answers(tmp_path):
    """§21.2 (Architect, 2026-09-19): roster proof.circleci.canary armed -> only the
    canary's CIRCLECI twin claims; the rest are held (no claim, no strike).  A
    proof record without a real pipeline_id/answer (FAIL_INFRA) keeps the hold and
    raises CANARY_NOT_ANSWERED; a record with pipeline_id + PASS releases: the
    driver writes released_at/outcome into the roster and the held twin claims."""
    pd = tmp_path / "pack"
    for pid in ("P-A", "P-B"):
        packet(pd, pid, "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", gate="CIRCLECI",
               body="parent %s for L00" % pid)
        (pd / pid / "BENCHMARK.md").write_text(BM_CIRCLECI_ONLY.format(id=pid))
    canary = {"task": "P-A-HOSTED", "armed_at": "2026-09-19T15:00:00Z", "released_at": None,
              "release_on": ["PASS"], "outcome": None, "reason": "§21.2 test"}
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0},
                                      "packet": {"twin_dependents": []},
                                      "owner_gates": {"DELIVERY-1": True},
                                      "proof": {"require_for_kinds": [], "circleci": {"canary": canary}}})
    env.roster["run"]["pack_dir"] = str(pd)
    (env.run_root / "roster.json").write_text(json.dumps(env.roster, indent=2))
    env.activate()
    assert [m for m in vplint.lint_roster_v13(env.roster) if "proof.circleci.canary" in m] == []
    oc = by_role({"builder": _fix_builder, "grader": findings("PASS"), "probe": _fix_builder})
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 10)
    rows = env.rows()
    assert rows["P-A"]["state"] == "VERIFIED" and rows["P-B"]["state"] == "VERIFIED"
    assert rows["P-A-HOSTED"]["state"] == "VERIFIED", "the canary's own twin claims"
    assert rows["P-B-HOSTED"]["state"] in ("READY", "PLANNED", "WAITING_DEPENDENCY"), rows["P-B-HOSTED"]
    log = (env.run_root / "driver.log").read_text()
    assert "HOLD: CANARY_PENDING P-A-HOSTED holds P-B-HOSTED" in log
    assert "FAIL P-B-HOSTED" not in log and drv._fail.get("P-B-HOSTED", {}).get("count", 0) == 0
    proofs = env.run_root / "proofs"
    proofs.mkdir(exist_ok=True)
    # a non-answer: no pipeline, infra failure -> hold stays, CANARY_NOT_ANSWERED once
    (proofs / "proof-P-A-HOSTED-1.json").write_text(json.dumps(
        {"proof_id": "proof-P-A-HOSTED-1", "status": "FAIL_INFRA", "pipeline_id": None,
         "route": "circleci", "reason": "circleci: no credits"}))
    settle(drv, 2)
    alerts = (env.run_root / "alerts.jsonl").read_text()
    assert alerts.count("CANARY_NOT_ANSWERED") == 1 and "CANARY_RELEASED" not in alerts
    assert env.rows()["P-B-HOSTED"]["state"] != "VERIFIED"
    assert json.loads((env.run_root / "roster.json").read_text())["proof"]["circleci"]["canary"]["released_at"] is None
    # a real answer: pipeline_id + PASS -> released, recorded in the roster, the held twin claims
    time.sleep(0.05)
    # §38 / D71b: a red answer on a real pipeline (FAIL_PRODUCT, 2026-09-19 19:03Z pipeline
    # 722a0b89) is a real answer -- CANARY_ANSWERED_RED once -- but KEEPS the hold
    (proofs / "proof-P-A-HOSTED-2.json").write_text(json.dumps(
        {"proof_id": "proof-P-A-HOSTED-2", "status": "FAIL_PRODUCT", "pipeline_id": "pipe-red", "route": "circleci",
         "failed_nodes": ["t.py::a", "t.py::b"]}))
    settle(drv, 4)
    rows = env.rows()
    assert rows["P-B-HOSTED"]["state"] == "READY", "a red canary keeps the hold"
    alerts = (env.run_root / "alerts.jsonl").read_text()
    assert alerts.count("CANARY_ANSWERED_RED") == 1 and "FAIL_PRODUCT on pipeline pipe-red" in alerts and "2 red node(s)" in alerts
    assert alerts.count("CANARY_NOT_ANSWERED") == 1, "a red answer is not 'not answered'"
    assert json.loads((env.run_root / "roster.json").read_text())["proof"]["circleci"]["canary"]["released_at"] is None
    # PASS on a real pipeline releases
    time.sleep(0.05)
    (proofs / "proof-P-A-HOSTED-3.json").write_text(json.dumps(
        {"proof_id": "proof-P-A-HOSTED-3", "status": "PASS", "pipeline_id": "pipe-123", "route": "circleci"}))
    settle(drv, 8)
    rows = env.rows()
    assert rows["P-B-HOSTED"]["state"] == "VERIFIED", rows["P-B-HOSTED"]
    log = (env.run_root / "driver.log").read_text()
    assert "CANARY_RELEASED P-A-HOSTED: pipeline pipe-123 -> PASS" in log
    rc = json.loads((env.run_root / "roster.json").read_text())["proof"]["circleci"]["canary"]
    assert rc["released_at"] and rc["outcome"] == "PASS" and rc["pipeline_id"] == "pipe-123"
    assert (env.run_root / "alerts.jsonl").read_text().count("CANARY_RELEASED") == 1
    assert "ROSTER_REJECTED" not in log, "the driver's own roster write re-applies clean"
    # the lint rule
    bad = json.loads(json.dumps(env.roster))
    bad["proof"]["circleci"]["canary"] = {"task": "P-A", "release_on": ["MAYBE"], "armed_at": "yesterday"}
    msgs = vplint.lint_roster_v13(bad)
    assert any("is not a hosted twin" in m for m in msgs) and any("release_on" in m for m in msgs)
    bad["proof"]["circleci"]["canary"]["release_on"] = ["PASS", "FAIL"]      # §38: FAIL may not release
    assert any("release_on must be [PASS]" in m for m in vplint.lint_roster_v13(bad))
    assert any("armed_at" in m for m in msgs)
    bad["proof"]["circleci"]["canary"] = {"task": "P-ZZ-HOSTED"}
    assert any("no packet P-ZZ" in m for m in vplint.lint_roster_v13(bad))


def test_d72_ruled_sticky_exclusion_keeps_a_member_out_of_every_integration_union(tmp_path):
    """§31 verb 1: R-L18-TOCTOU-C is SUBSUMED_BY_TRUNK (patch-id twin of a trunk
    commit); the ruling lives in RUN_ROOT/unions/exclusions.json and every cut
    excludes that member at that sha as `known`, with the ruling as `why`, no
    merge attempt, no INTEGRATION_CONFLICT; a re-verified member (new sha) is
    merged again."""
    from test_lanedriver import register_candidate
    env, drv, codex = _union_env(tmp_path)
    sha, _tree = register_candidate(env)
    settle(drv, 6)
    rows = env.rows()
    assert rows["P-FIX-A"]["state"] == "VERIFIED" and rows["P-FIX-B"]["state"] == "VERIFIED"
    integ = sorted((json.loads(m.read_text()) for m in (env.run_root / "unions").glob("*/members.json")
                    if json.loads(m.read_text()).get("for") == ["INTEGRATION"]), key=lambda d: d["n"])
    assert integ and {m["task"] for m in integ[-1]["members"]} >= {"P-FIX-A", "P-FIX-B"} and not integ[-1].get("excluded")
    # the ruling: P-FIX-B is subsumed by trunk at its current sha
    (env.run_root / "unions" / "exclusions.json").write_text(json.dumps({
        "P-FIX-B": {"output_sha": rows["P-FIX-B"]["output_sha"], "ruling": "§31",
                    "reason": "SUBSUMED_BY_TRUNK: output is the patch-id twin of trunk c0aff64f", "ts": "2026-09-19T15:00:00Z"}}))
    pd = Path(env.roster["run"]["pack_dir"])
    packet(pd, "P-FIX-C", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="fix C for L00")
    drv._pack_logged = set()
    n_alerts = (env.run_root / "alerts.jsonl").read_text().count("INTEGRATION_CONFLICT")
    settle(drv, 6)
    assert env.rows()["P-FIX-C"]["state"] == "VERIFIED"
    integ2 = sorted((json.loads(m.read_text()) for m in (env.run_root / "unions").glob("*/members.json")
                     if json.loads(m.read_text()).get("for") == ["INTEGRATION"]), key=lambda d: d["n"])[-1]
    assert integ2["n"] > integ[-1]["n"]
    ex = {e["task"]: e for e in integ2.get("excluded") or []}
    assert set(ex) == {"P-FIX-B"} and ex["P-FIX-B"]["known"] and "SUBSUMED_BY_TRUNK" in ex["P-FIX-B"]["why"]
    assert {m["task"] for m in integ2["members"]} >= {"P-FIX-A", "P-FIX-C"} and "P-FIX-B" not in {m["task"] for m in integ2["members"]}
    assert (env.run_root / "alerts.jsonl").read_text().count("INTEGRATION_CONFLICT") == n_alerts, "a ruling is not a conflict"
    assert "P-FIX-B" not in git(env.trunk, "log", "--format=%s", "%s..%s" % (sha, integ2["union_sha"]))


def test_d75_codex_max_is_bounded_not_pinned():
    """D75: the owner raised concurrency.codex_max 3 -> 20 (2026-09-19); the
    roster lint bounds it to 1..20 (claude_max still pinned at 2) instead of
    refusing every value but 3, which had rejected the edit."""
    def msgs(cm, clm=2):
        r = {"run": {"scheduler": "x"}, "concurrency": {"codex_max": cm, "claude_max": clm}}
        return [m for m in vplint.lint_roster_v13(r) if "codex_max" in m]
    assert msgs(20) == [] and msgs(3) == [] and msgs(1) == []
    assert any("codex_max 1..20" in m for m in msgs(21))
    assert any("codex_max 1..20" in m for m in msgs(0))
    assert any("claude_max 2" in m for m in msgs(3, 3))


def test_d98_a_twin_header_never_carries_proof_only():
    """§95 item 2 (D98): `proof_only` narrows a packet's own proof to one hosted
    job/leg; a twin is the full suite of the union tip, so the parent's
    proof_only never reaches the twin's header (nor proof_paths without test_paths)."""
    body = ("---\nitem: R-X\ntitle: x\nproof_kind: platform\nproof_only: platform-shards-3\n"
            "proof_paths:\n  - platform/tests/test_a.py\nproof_workers: 1\n---\nbody\n")
    packet = {"body": body, "id": "R-X-HOSTED", "title": "x", "twin_gate": "G", "max_rounds": 2,
              "twin_of": "R-X", "depends_on": [], "test_paths": [], "hosted_rows": []}
    head, rest = vppack.twin_front_matter(packet, "a" * 40)
    assert "proof_only" not in head and "proof_paths" not in head and "proof_workers" not in head
    assert "proof_kind: platform" in head and rest == "body\n"


def test_a_twin_never_carries_proof_only_even_when_the_parent_names_one_for_it():
    """04-REVIEW-POLICY (owner, 2026-09-20): VERIFIED hosted only through the full
    canary-gated run.  Neither the parent's proof_only nor a twin_proof_only
    line reaches the CIRCLECI twin; the twin's own proof_only is empty."""
    body = ("---\nitem: R-X\ntitle: x\nproof_kind: platform\nproof_only: portal\n"
            "twin_proof_only: platform-shards-3\n---\nbody\n")
    packet = {"body": body, "id": "R-X-HOSTED", "title": "x", "twin_gate": "CIRCLECI", "max_rounds": 1,
              "twin_of": "R-X", "depends_on": [], "test_paths": [], "hosted_rows": [], "proof_only": ""}
    head, _ = vppack.twin_front_matter(packet, "a" * 40)
    assert "proof_only" not in head


def test_load_pack_gives_the_circleci_twin_no_proof_only(tmp_path):
    d = tmp_path / "R-X"
    d.mkdir()
    (d / "PACKET.md").write_text("---\nitem: R-X\ntitle: x\nrunner_role: builder\nowned_files:\n  - platform/a.py\n"
                                 "test_paths:\n  - platform/tests/test_a.py\nproof_only: portal\n"
                                 "twin_proof_only: platform-shards-3\n---\nbody\n")
    # D169: the tag goes in the row's LEADING tag block, which is where all 1110
    # hosted rows on disk carry it.  This fixture used to write `- B1 the hosted row
    # [hosted] ...` -- a shape the real corpus never produces -- so it was asserting
    # against a row no packet author writes.
    (d / "BENCHMARK.md").write_text("- B1 [evidence] [hosted] the hosted row (gate: CIRCLECI)\n")
    pack, lint = vppack.load_pack(tmp_path)[:2]
    assert pack["R-X"]["proof_only"] == "portal" and pack["R-X-HOSTED"]["proof_only"] == ""
    assert "proof_only" not in vppack.twin_packet_text(pack["R-X-HOSTED"], "a" * 40)


def test_d145_a_not_in_scope_section_never_supplies_the_parent():
    """Naming what a packet must AVOID is the opposite of naming its parent.

    R-MIG034-AGENTS-ROLE-REFUSAL-DB was given parent `L11` because its
    `## Not in scope` section correctly warned against colliding with
    `L11-MIGRATION-266-BLOCKING-HOSTED-R2`. Measured over the live pack, exactly
    two packets' first cite came out of such a section -- that one and
    REVIEW-JUNIOR-UNION -- and both were wrong.
    """
    strip = vppack._body_without_negative_sections

    body = ("## Goal\nRefuse a role the agents table must not grant.\n\n"
            "## Not in scope\n- `L11-MIGRATION-266-BLOCKING-HOSTED-R2`, do not collide.\n")
    assert "L11" not in strip(body)
    assert "Refuse a role" in strip(body), "the positive prose must survive"

    # the section ENDS at the next heading of the same or higher level: a cite after
    # it is still found, or the stripper would swallow the rest of the packet
    body2 = body + "\n## Steps\nExtend the L07 fence.\n"
    assert "L07" in strip(body2) and "L11" not in strip(body2)
    body3 = ("## A\n### Not in scope\n- avoid L11\n### Steps\nextend L07\n")
    assert "L07" in strip(body3) and "L11" not in strip(body3)

    # a cite in ordinary prose is untouched -- this is a blacklist, not a whitelist,
    # because parent_for falls through to None and PACKET_NO_PARENT is fatal
    assert "L07" in strip("## Goal\nThis continues L07's fence.\n")

    # the spellings a packet actually uses for the same idea
    for heading in ("## Not in scope", "## Out of scope", "## Non-goals", "## NON-GOALS",
                    "## Do not touch", "## Forbidden", "## Anti-goals", "## 3. Out of Scope"):
        assert "L11" not in strip("%s\n- avoid L11\n" % heading), heading

    # ...and one it does not: a blacklist cannot be complete, and saying so here is
    # cheaper than discovering it from a wrong parent
    assert "L11" in strip("## Things we will not do\n- avoid L11\n"), (
        "unanticipated negative headings still leak -- deliberate: the alternative "
        "(whitelisting trusted sections) turns every unanticipated POSITIVE heading "
        "into a fatal PACKET_NO_PARENT")


# -- D163: a product verdict is not adopted across items when nothing scopes it ----------------

def _fp(proof_id, nodes, **kw):
    rec = {"proof_id": proof_id, "status": "FAIL_PRODUCT", "route": "gha", "pipeline_id": "p1",
           "kind": "platform", "paths": [], "only": None, "failed_nodes": list(nodes), "ts": "1"}
    rec.update(kw)
    return rec


def test_d163_proof_task_recovers_the_item_from_a_proof_id():
    """D163. `pid` is built as `proof-<task>-<attempt[-15:]>`, and some records
    carry a further `-p<sha>` leg, so both tails come off.

    The empty return is load-bearing: one `reused_from` in run-v13-20260917 is a
    free-text provenance note ("Fixer 2026-09-19T19:14:17Z: the per-atte..."),
    not a proof id at all. It must decline to parse rather than be mistaken for
    an item."""
    from lanedriver import LaneDriver as D

    assert D.proof_task("proof-L34-ERROR-KIND-PRIVACY-HOSTED-R1-60920T210858201") \
        == "L34-ERROR-KIND-PRIVACY-HOSTED-R1"
    assert D.proof_task("proof-L06-HOSTED-R3-60919T182758282-p722a0b89") == "L06-HOSTED-R3"
    assert D.proof_task("proof-L04-FLOOR-PROOF-BRANCH-HOSTED-R1-60920T172722305") \
        == "L04-FLOOR-PROOF-BRANCH-HOSTED-R1"
    assert D.proof_task("Fixer 2026-09-19T19:14:17Z: the per-attempt record") == ""
    assert D.proof_task(None) == "" and D.proof_task("") == ""


def test_d163_unattributable_for_names_the_four_real_cases_and_no_others():
    """D163's predicate, against the exact population measured in
    run-v13-20260917. Eighteen records carry reused_from/restored_by; twelve are
    PASS and stay reusable, and of the six FAIL_PRODUCT ones only four are this
    defect.

    The two that are NOT are the reason the predicate is what it is:
      * L06-HOSTED-R3 reusing its OWN earlier record -- same item, so the
        partition it inherits was computed against its own base and is valid;
      * a record whose provenance is free text, which names no item to compare.

    A predicate that flagged either would refuse reuses that are sound, and
    would cost a real pipeline each time."""
    from lanedriver import LaneDriver as D

    # the four
    assert D.unattributable_for(
        "L34-ERROR-KIND-PRIVACY-HOSTED-R1",
        _fp("proof-L04-FLOOR-HOSTED-R2-60920T210540342", ["a::t"] * 3265)) == "L04-FLOOR-HOSTED-R2"
    assert D.unattributable_for(
        "L33-HOSTED-R2", _fp("proof-L19-HOSTED-R2-60920T210555466", ["a::t"])) == "L19-HOSTED-R2"
    assert D.unattributable_for(
        "L04-FLOOR-HOSTED-R2",
        _fp("proof-L04-FLOOR-PROOF-BRANCH-HOSTED-R1-60920T172722305", ["a::t"])) \
        == "L04-FLOOR-PROOF-BRANCH-HOSTED-R1", "a longer item id is not the same item"
    assert D.unattributable_for(
        "R-COVERAGE-RELATIVE-FILES-HOSTED-R1",
        _fp("proof-L29-ENROLL-FENCE-HOSTED-R2-60920T210000000", ["a::t"] * 11)) \
        == "L29-ENROLL-FENCE-HOSTED-R2"

    # ... and the two that are not
    assert D.unattributable_for(
        "L06-HOSTED-R3", _fp("proof-L06-HOSTED-R3-60919T182758282-p722a0b89", ["a::t"] * 62)) == "", \
        "the same item's own earlier record: its partition IS against this base"
    assert D.unattributable_for(
        "L06-HOSTED-R3", _fp("Fixer 2026-09-19T19:14:17Z: restored", ["a::t"] * 62)) == "", \
        "free-text provenance names no item to compare against"


def test_d163_a_green_full_run_is_still_adopted_by_anyone():
    """D163's load-bearing control, and the reason the predicate is not simply
    'adopted from another item'.

    A PASS on this exact tree is green for everyone and there is nothing to
    attribute -- twelve of the eighteen adopted records in run-v13-20260917 are
    exactly that, and they are the whole point of D79 reuse. Refusing them would
    trigger a real pipeline per item for an answer already in hand."""
    from lanedriver import LaneDriver as D

    green = _fp("proof-L04-FLOOR-HOSTED-R2-60920T210540342", [])
    green["status"] = "PASS"
    assert D.unattributable_for("L34-ERROR-KIND-PRIVACY-HOSTED-R1", green) == ""
    # a FAIL_PRODUCT that somehow carries no red nodes has nothing to mis-attribute
    assert D.unattributable_for("L34-X", _fp("proof-L04-FLOOR-HOSTED-R2-60920T1", [])) == ""


def test_d163_a_scoped_record_is_left_to_the_d140_filter():
    """D163 control: a record that declares what it ran can be re-scoped, and
    D140 is what does it. This branch is only for the case where NOTHING scopes
    the verdict. Both spellings of a scope count -- `only` and `paths` -- which
    is the narrowing the box-run population forced earlier tonight."""
    from lanedriver import LaneDriver as D

    scoped = _fp("proof-L04-FLOOR-HOSTED-R2-60920T210540342", ["a::t"],
                 only="twin:3:platform/tests/test_a.py")
    assert D.unattributable_for("L34-X", scoped) == ""
    with_paths = _fp("proof-L04-FLOOR-HOSTED-R2-60920T210540342", ["a::t"],
                     paths=["platform/tests/test_a.py"])
    assert D.unattributable_for("L34-X", with_paths) == ""


def test_d169_a_runtime_tag_is_a_tag_not_a_word_in_the_prose():
    """Both polarities, because a narrowing tested only on the negative case can match
    nothing at all and pass vacuously.

    The real row: L17-REGISTRY-REPLY-PINS B10 is [box] and its own prose explains why
    it is not hosted. The sentence justifying the classification flipped it, and the
    row then errored for naming no (gate: X).
    """
    from vppack import hosted_row_gates

    l17_b10 = ("- B10 [invariant] [box] `deploy/tests/test_worker_packaging.py::test_x` passes "
               "— check: run that node. This row is `[box]` deliberately. The claim was "
               "previously the second half of a compound `[hosted]` row, which a kind:platform "
               "twin is structurally incapable of seeing. Making it `[hosted]` again would only "
               "relocate the same unanswerability.\n")
    genuine = "- B3 [evidence] [hosted] the shards pass — check: the pipeline (gate: CIRCLECI)\n"

    both = hosted_row_gates(l17_b10 + genuine)
    assert "B10" not in both, (
        "prose ABOUT [hosted] must not classify the row as hosted: %r" % (both.get("B10"),))
    assert "B3" in both and both["B3"][0] == "CIRCLECI", (
        "and a genuine hosted row must still be found, with its gate: %r" % (both,))

    # the negative case alone would pass against a pattern that matches nothing, so
    # pin that the positive case is what discriminates
    assert hosted_row_gates(l17_b10) == {}
    assert set(hosted_row_gates(genuine)) == {"B3"}

    # the tag block is the LEADING run of tags: a tag after prose is prose
    assert hosted_row_gates("- B4 [box] runs the thing [hosted] later — check: x\n") == {}
    assert set(hosted_row_gates("- B5 [hosted] first — check: y (gate: O1)\n")) == {"B5"}

    # the gate is still read from the whole line -- (gate: X) lives in the check clause,
    # long past the tag block, so only the runtime tag is position-bound
    assert hosted_row_gates("- B6 [evidence] [hosted] x — check: y (gate: DELIVERY-2)\n")["B6"][0] == "DELIVERY-2"

    # and a hosted row with no gate still reports None rather than vanishing: that is
    # the lint error path, and swallowing the row would hide it
    assert hosted_row_gates("- B7 [hosted] x — check: y\n")["B7"][0] is None
