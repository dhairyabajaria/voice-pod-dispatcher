#!/usr/bin/env python3
"""tests/test_vppack.py -- pack §4(c): packet -> scheduler-task binding (D9),
joint closers (§6b), owner gates, L42 rewire, dispatch records.  Pure vppack
tests plus one driver cycle over a tiny pack with fake runners."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP))
sys.path.insert(0, str(HERE))

import vppack  # noqa: E402
from test_lanedriver import Env, FakeRunner, by_role, findings, result_ok, settle  # noqa: E402

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
                                      "packet": {"rewire": ["L04"]}})
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
    env = Env(tmp_path, roster_extra={"alerts": {"frontier_every_s": 0, "idle_every_min": 30, "pack_every_s": 0}})
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
