#!/usr/bin/env python3
"""tests/test_vpjourney.py -- the ledger derived from the event stream.

A synthetic run root: a catalog row L00 regraded by packet L00 (row L00-V13),
a new-work packet P-FIX with a retry (P-FIX CANCELLED -> P-FIX-R1 VERIFIED),
its hosted twin P-FIX-HOSTED (INVALID_EVIDENCE, retried by P-FIX-HOSTED-R1
BLOCKED but never retired), a review row JR-1 attached to L00 by
parent_contract_id, and an orphan row nobody can attribute."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import vpjourney  # noqa: E402

FM = """---
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
test_paths:
  - platform/a.py
proof_kind: platform
max_rounds: 2
v13_kind: {kind}
scheduler_task: {task}
closes: []
template: {template}
runner_role: {role}
hosted_owed: false
owner_gate: none
---

## Goal

{body}
"""

TS = "2026-09-18T%02d:%02d:00.000Z"


def ts(h, m=0):
    return TS % (h, m)


def packet(pd, id_, task, kind="regrade", template="none", role="grader", body="x", hosted=False):
    d = pd / id_
    d.mkdir(parents=True)
    (d / "PACKET.md").write_text(FM.format(id=id_, base="a" * 40, kind=kind, task=task, template=template, role=role, body=body))
    bm = "- B1 [evidence] [box] %s done — check: x\n" % id_
    if hosted:
        bm += "- B2 [evidence] [hosted] (gate: DELIVERY-1) %s hosted — check: y\n" % id_
    (d / "BENCHMARK.md").write_text(bm)


def build_run(tmp_path):
    run_root = tmp_path / "run"
    state_dir = tmp_path / "orchestration-state"
    events = state_dir / "events"
    events.mkdir(parents=True)
    pd = tmp_path / "pack"
    packet(pd, "L00", "L00", body="regrade L00")
    packet(pd, "P-FIX", "NEW:TEST_GAP", kind="repair", template="TEST_GAP", role="builder", body="fix", hosted=True)
    (run_root / "packets").mkdir(parents=True)
    (run_root / "proofs").mkdir()
    catalog = tmp_path / "lane-contracts.json"
    catalog.write_text(json.dumps({"tasks": [{"task_id": "L00"}]}))
    (run_root / "roster.json").write_text(json.dumps({
        "run": {"run_state": str(state_dir / "run-state.json"), "pack_dir": str(pd), "catalog": str(catalog),
                "run_root": str(run_root)}}))

    seq = [0]

    def ev(typ, task, created, **kw):
        seq[0] += 1
        e = {"sequence": seq[0], "type": typ, "task_id": task, "created_at": created, "event_id": "e%d" % seq[0]}
        e.update(kw)
        (events / ("%06d-e%d.json" % (seq[0], seq[0]))).write_text(json.dumps(e))
        return seq[0]

    ev("TASK_CLAIMED", "L00", ts(9), chief="A", base_sha="b" * 40)
    ev("TASK_STARTED", "L00", ts(9, 1), resolved_model="m1", resolved_effort="high")
    ev("TASK_COMPLETED", "L00", ts(9, 30), outcome="VERIFIED")
    ev("TASK_PROMOTED_TO_INTEGRATED", "L00", ts(9, 40), output_sha="c" * 40)
    ev("TEMPLATE_TASK_INSTANTIATED", "L00-V13", ts(10), template_id="REGRADE", parent_contract_id="L00")
    ev("TASK_COMPLETED", "L00-V13", ts(10, 30), outcome="VERIFIED")
    ev("TEMPLATE_TASK_INSTANTIATED", "JR-1", ts(10, 31), template_id="JUNIOR_REVIEW", parent_contract_id="L00")
    ev("TASK_RETIRED", "JR-1", ts(10, 40), closers=["L00-V13"])
    ev("TEMPLATE_TASK_INSTANTIATED", "P-FIX", ts(11), template_id="TEST_GAP", parent_contract_id="L00")
    ev("TASK_COMPLETED", "P-FIX", ts(11, 30), outcome="REPAIR_REQUIRED", reason="FAIL B1")
    ev("TEMPLATE_TASK_INSTANTIATED", "P-FIX-R1", ts(12), template_id="TEST_GAP", parent_contract_id="L00")
    ev("TASK_RETIRED", "P-FIX", ts(12, 1), closers=["P-FIX-R1"])
    ev("TASK_COMPLETED", "P-FIX-R1", ts(12, 30), outcome="VERIFIED")
    ev("TEMPLATE_TASK_INSTANTIATED", "P-FIX-HOSTED", ts(13), template_id="TEST_GAP", parent_contract_id="L00")
    ev("TASK_COMPLETED", "P-FIX-HOSTED", ts(13, 30), outcome="INVALID_EVIDENCE")
    ev("TEMPLATE_TASK_INSTANTIATED", "P-FIX-HOSTED-R1", ts(14), template_id="TEST_GAP", parent_contract_id="L00")
    ev("TASK_BLOCKED", "P-FIX-HOSTED-R1", ts(14, 5), reason="gate DELIVERY-1 closed", **{"class": "OWNER_GATE"})
    ev("TEMPLATE_TASK_INSTANTIATED", "ORPHAN-9", ts(15), template_id="TEST_GAP", parent_contract_id=None)
    ev("TASK_COMPLETED", "ORPHAN-9", ts(15, 10), outcome="VERIFIED")

    def row(task, state, dynamic=True, **kw):
        r = {"task_id": task, "state": state, "dynamic": dynamic, "updated_at": ts(16), "kind": "builder"}
        r.update(kw)
        return r

    tasks = {
        "L00": row("L00", "INTEGRATED", dynamic=False),
        "L00-V13": row("L00-V13", "VERIFIED", parameters={"packet_id": "L00"}, parent_contract_id="L00"),
        "JR-1": row("JR-1", "CANCELLED", parent_contract_id="L00", template_id="JUNIOR_REVIEW"),
        "P-FIX": row("P-FIX", "CANCELLED", parameters={"packet_id": "P-FIX"}, parent_contract_id="L00"),
        "P-FIX-R1": row("P-FIX-R1", "VERIFIED", parameters={"packet_id": "P-FIX"}, parent_contract_id="L00"),
        "P-FIX-HOSTED": row("P-FIX-HOSTED", "INVALID_EVIDENCE", parameters={"packet_id": "P-FIX-HOSTED"}, parent_contract_id="L00"),
        "P-FIX-HOSTED-R1": row("P-FIX-HOSTED-R1", "BLOCKED", parameters={"packet_id": "P-FIX-HOSTED"}, parent_contract_id="L00",
                               blocker={"class": "OWNER_GATE"}),
        "ORPHAN-9": row("ORPHAN-9", "VERIFIED"),
    }
    (state_dir / "run-state.json").write_text(json.dumps({"sequence": seq[0], "updated_at": ts(16), "tasks": tasks}))
    for task, rec in {
        "P-FIX-R1": {"packet": "P-FIX", "task": "P-FIX-R1", "retry_of": "P-FIX", "retry_reason": "amended B1"},
        "P-FIX-HOSTED-R1": {"packet": "P-FIX-HOSTED", "task": "P-FIX-HOSTED-R1", "retry_of": "P-FIX-HOSTED", "retry_reason": "gate"},
        "P-FIX": {"packet": "P-FIX", "task": "P-FIX"},
    }.items():
        (run_root / "packets" / ("%s.json" % task)).write_text(json.dumps(rec))
    with open(run_root / "costs.jsonl", "w") as fh:
        fh.write(json.dumps({"ts": ts(11, 10), "task": "P-FIX", "attempt": "P-FIX-a1", "n": 1, "round": 1, "role": "builder",
                             "runner": "opencode", "model": "muse", "server": "go2", "status": "DONE", "cost": 0.1, "duration_s": 60}) + "\n")
        fh.write(json.dumps({"ts": ts(12, 10), "task": "P-FIX-R1", "attempt": "P-FIX-R1-a1", "n": 1, "round": 1, "role": "grader",
                             "runner": "codex", "model": "gpt", "status": "DONE", "cost": 0.2, "duration_s": 30}) + "\n")
    rec_dir = run_root / "turns" / "P-FIX" / "P-FIX-a1"
    rec_dir.mkdir(parents=True)
    (rec_dir / "1-r1-builder-record.json").write_text(json.dumps({"effort": "xhigh", "model_seen": "muse-1.3", "server": "go2"}))
    (run_root / "proofs" / "proof-P-FIX-R1-1.json").write_text(json.dumps(
        {"proof_id": "proof-P-FIX-R1-1", "status": "PASS", "route": "box", "sha": "d" * 40, "ts": ts(12, 20), "counts": {"passed": 3}}))
    (run_root / "LEDGER.md").write_text(
        "# LEDGER\n\n| task | state | kind |\n| --- | --- | --- |\n"
        "| L00 | INTEGRATED | x |\n| L00-V13 | VERIFIED | x |\n| P-FIX | REPAIR_REQUIRED | x |\n")   # P-FIX is stale here
    with open(run_root / "control.jsonl", "w") as fh:
        fh.write(json.dumps({"ts": ts(9), "verb": "claim", "argv": ["--task", "L00"], "rc": 0, "seq_before": 0, "seq_after": 4}) + "\n")
        fh.write(json.dumps({"ts": ts(10), "verb": "complete", "argv": [], "rc": 0, "seq_before": 4, "seq_after": seq[0]}) + "\n")
    return run_root


def test_tail_reader_holds_a_torn_line_and_restarts_after_truncation(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_bytes(b'{"a": 1}\n{"a": 2')
    r = vpjourney.TailReader(p)
    r.CHUNK = 5                                       # force line splits across chunk boundaries
    assert [o["a"] for o in r.read_new()] == [1]
    assert r.buf == b'{"a": 2' and r.read_new() == []
    with open(p, "ab") as fh:
        fh.write(b'}\nnot json\n{"a": 3}\n')
    assert [o["a"] for o in r.read_new()] == [2, 3]
    assert r.bad == 1
    p.write_bytes(b'{"a": 9}\n')                      # rotated: shorter than the consumed offset
    assert [o["a"] for o in r.read_new()] == [9]
    assert r.offset == p.stat().st_size


def test_tail_reader_max_bytes_leaves_the_rest_for_the_next_call(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_bytes(b"".join(json.dumps({"i": i}).encode() + b"\n" for i in range(100)))
    r = vpjourney.TailReader(p)
    first = r.read_new(max_bytes=100)
    assert 0 < len(first) < 100
    rest = r.read_new()
    assert [o["i"] for o in first + rest] == list(range(100))


def test_roots_attribute_every_row_to_its_work_item(tmp_path):
    j = vpjourney.Journey(build_run(tmp_path))
    j.refresh(control=False)
    assert j.root_of("L00") == ("L00", "catalog", "catalog row")
    assert j.root_of("L00-V13")[:2] == ("L00", "catalog")          # regrade packet named after the row
    assert j.root_of("JR-1")[:2] == ("L00", "catalog")             # review attaches via parent_contract_id
    assert j.root_of("P-FIX-R1")[:2] == ("P-FIX", "packet")
    assert j.root_of("P-FIX-HOSTED-R1")[:2] == ("P-FIX", "packet")   # twin proves its parent packet
    assert j.root_of("ORPHAN-9")[1] == "orphan"
    roots = j.roots()
    assert set(roots) == {"L00", "P-FIX", "ORPHAN-9"}
    assert roots["L00"]["rows"] == ["L00", "L00-V13", "JR-1"]
    assert roots["L00"]["settled"] and roots["L00"]["state"] == "INTEGRATED"


def test_root_state_comes_from_the_live_rows_not_the_latest_or_a_zombie(tmp_path):
    j = vpjourney.Journey(build_run(tmp_path))
    j.refresh(control=False)
    r = j.roots()["P-FIX"]
    assert r["primary_row"] == "P-FIX-R1" and r["primary_state"] == "VERIFIED"   # the live retry, not CANCELLED P-FIX
    assert r["done"] and not r["settled"]
    assert r["hosted_state"] == "BLOCKED"
    assert r["open_rows"] == ["P-FIX-HOSTED-R1"]                    # P-FIX-HOSTED is superseded: not open
    assert r["superseded_open"] == ["P-FIX-HOSTED"]
    assert r["state"] == "BLOCKED" and r["owed"]


def test_scope_is_a_fixed_denominator_that_the_derived_roots_reconcile_to(tmp_path):
    j = vpjourney.Journey(build_run(tmp_path))
    j.refresh(control=False)
    sc = j.scope()
    assert sc["catalog_rows"] == 1 and sc["packet_dirs"] == 2
    assert sc["packets_on_catalog_rows"] == 1 and sc["packets_new_work"] == 1
    assert sc["work_items_fixed"] == 2 and sc["work_items_derived"] == 2
    assert sc["orphan_roots"] == ["ORPHAN-9"] and sc["retry_rows"] == 2
    s = j.summary()
    assert s["roots"] == 3 and s["settled"] == 2 and s["owed"] == 1 and s["hosted_open"] == 1   # L00 + the orphan


def test_journey_interleaves_events_turns_and_proofs_with_their_sources(tmp_path):
    j = vpjourney.Journey(build_run(tmp_path))
    j.refresh(control=False)
    jn = j.journey("P-FIX-R1")                                      # a row id resolves to its root
    assert jn["root"] == "P-FIX" and [a["task"] for a in jn["attempts"]] == ["P-FIX", "P-FIX-R1", "P-FIX-HOSTED", "P-FIX-HOSTED-R1"]
    first = jn["attempts"][0]["steps"]
    turn = [s for s in first if s["kind"] == "turn"][0]
    assert turn["model"] == "muse" and turn["effort"] == "xhigh" and turn["model_seen"] == "muse-1.3"
    assert "turns/P-FIX/P-FIX-a1/1-r1-builder-record.json" in turn["source"]
    assert [s["kind"] for s in first] == ["event", "turn", "event", "event"]
    r1 = jn["attempts"][1]
    assert r1["retry_of"] == "P-FIX" and r1["retry_reason"] == "amended B1"
    kinds = [s["kind"] for s in r1["steps"]]
    assert kinds == ["event", "turn", "proof", "event"]
    grader = [s for s in r1["steps"] if s["kind"] == "turn"][0]
    assert grader["effort"] is None                                  # no record on disk: nothing invented
    text = j.render_journey("P-FIX")
    # D149: the proof line now states what the PASS actually covered, between the
    # route and the verdict.  A reader must not have to open the record to learn
    # whether a green means one file or the whole suite.
    assert "proof dddddddddd on box [full suite] -> PASS (passed=3)" in text
    assert "retired -> CANCELLED (closers P-FIX-R1)" in text


def test_verify_reports_divergence_between_events_state_ledger_and_spans(tmp_path):
    run_root = build_run(tmp_path)
    j = vpjourney.Journey(run_root)
    j.refresh()
    rep = j.verify()
    checks = {(f["check"], f.get("task")) for f in rep["findings"]}
    assert ("superseded_still_open", "P-FIX-HOSTED") in checks
    assert ("ledger_vs_state", "P-FIX") in checks
    assert ("ledger_coverage", None) in checks
    assert ("root_identity", None) in checks
    assert rep["errors"] == 0 and rep["calls_read"] == 2
    assert not any(f["check"] == "control_spans" for f in rep["findings"])   # every sequence inside a call span
    # now the run-state drifts from its own events
    st = json.loads((j.state_path).read_text())
    st["tasks"]["P-FIX-R1"]["state"] = "REPAIR_REQUIRED"
    j.state_path.write_text(json.dumps(st))
    import os
    os.utime(j.state_path, (1, 1))
    j.refresh(control=False)
    rep = j.verify()
    bad = [f for f in rep["findings"] if f["check"] == "state_vs_events"]
    assert len(bad) == 1 and bad[0]["task"] == "P-FIX-R1" and "implies VERIFIED" in bad[0]["detail"]
    assert rep["errors"] == 1
    # and a hole in the sequence
    victim = sorted(j.events.dir.iterdir())[2]
    victim.unlink()
    j2 = vpjourney.Journey(run_root)
    j2.refresh(control=False)
    assert j2.events.gaps() == [3]
    assert any(f["check"] == "event_sequence" for f in j2.verify()["findings"])


def test_ledger_render_never_touches_run_state_states(tmp_path):
    j = vpjourney.Journey(build_run(tmp_path))
    j.refresh(control=False)
    out = j.render_ledger()
    assert out.startswith("# LEDGER (derived from orchestration-state/events")
    assert "| P-FIX | packet | BLOCKED | VERIFIED | BLOCKED | P-FIX-HOSTED-R1 | 4 |" in out
    assert "| L00 | catalog | INTEGRATED | INTEGRATED | - | - | 3 |" in out


def test_cli(tmp_path, capsys):
    run_root = build_run(tmp_path)
    assert vpjourney.main(["--run-root", str(run_root), "summary"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["roots"] == 3
    assert vpjourney.main(["--run-root", str(run_root), "verify", "--no-control"]) == 0
    assert "superseded_still_open" in capsys.readouterr().out
    assert vpjourney.main(["--run-root", str(run_root), "journey", "L00"]) == 0
    assert "L00 [catalog] INTEGRATED" in capsys.readouterr().out


def test_d149_a_proof_says_what_it_ran_and_a_failure_claims_nothing():
    """D149 (PROOF-SCOPE spec Part A req 2, board half). The board could show PASS
    but not whether that PASS covered one file or the whole suite -- the union-104
    shape. `only` is now carried through and rendered beside the verdict.
    """
    full = vpjourney.proof_scope_label({"status": "PASS", "only": None})
    scoped = vpjourney.proof_scope_label(
        {"status": "PASS", "only": "twin:3:platform/tests/test_a.py"})
    assert full == "full suite"
    assert scoped.startswith("scoped: ") and "test_a.py" in scoped
    assert full != scoped, "a scoped pass and a full pass must not render alike"

    # absence must never default to the STRONGEST claim -- the D146 mistake
    assert vpjourney.proof_scope_label({"status": "FAIL_PRODUCT", "only": None}) == "-"
    assert vpjourney.proof_scope_label({"status": "HELD", "only": None}) == "-"
    assert vpjourney.proof_scope_label({}) == "-"
    assert vpjourney.proof_scope_label(None) == "-"

    # `only` is a path list; a pipe would break any table this lands in
    assert "|" not in vpjourney.proof_scope_label({"status": "PASS", "only": "a.py|b.py"})
    # and an enormous list must not blow out the row
    long = vpjourney.proof_scope_label({"status": "PASS", "only": "x/" + "y" * 400 + ".py"})
    assert len(long) < 100 and long.endswith("..."), long


def test_d149_the_only_field_survives_the_proof_projection():
    """The label is worthless if the field is dropped before it reaches it: the
    projection in _refresh_proofs is an explicit allow-list, so `only` has to be
    named there or every proof silently reads as a full suite.  Asserted against
    the real allow-list read out of the source, not a copy of it -- a copy would
    agree with itself forever."""
    src = (Path(__file__).resolve().parent.parent / "vpjourney.py").read_text(encoding="utf-8")
    block = src.split("self.proofs[p[\"proof_id\"]] =", 1)[1].split("}", 1)[0]
    assert '"only"' in block, "vpjourney's proof projection dropped `only`"
    rec = {"proof_id": "proof-X-60921T000001", "status": "PASS", "route": "gha",
           "only": "twin:3:platform/tests/test_z.py", "sha": "a" * 40}
    kept = {k: rec.get(k) for k in
            ("proof_id", "status", "route", "sha", "ts", "kind", "counts", "rc",
             "account", "pipeline_id", "branch", "only")}
    assert kept["only"] == rec["only"], "the projection must carry `only`"
    assert vpjourney.proof_scope_label(kept).startswith("scoped: ")
