#!/usr/bin/env python3
"""tests/test_vpsweep.py -- the sweep that finds packets which never became rows.

The class under guard is silent by construction: a packet with no derivable
parent sits on disk, lints clean, and no row is ever created, so there is
nothing to be red. The sweep is the only thing that would notice, which makes
its OWN failure modes the thing to test hardest -- a sweep that cannot fire and
a sweep that finds nothing print the same line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import vpsweep  # noqa: E402

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

BASE = "a" * 40


def _packet(pack_dir, pid, task, extra="", body=""):
    d = pack_dir / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "PACKET.md").write_text(
        FM.format(id=pid, base=BASE, task=task, extra=extra, body=body), encoding="utf-8")
    return d


def _run_state(tmp_path, tasks):
    p = tmp_path / "run-state.json"
    p.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return p


def test_a_bound_packet_is_not_reported_and_the_examined_count_is_real(tmp_path):
    pack = tmp_path / "PACKETS"
    _packet(pack, "L01", "L01")
    rs = _run_state(tmp_path, {"L01": {"state": "READY"}})
    res = vpsweep.sweep(pack, rs, {})
    assert res["examined"] == 1, "the count must describe what was scanned"
    assert [r["packet"] for r in res["bound"]] == ["L01"]
    assert res["stranded"] == [] and res["waiting"] == []


def test_a_packet_with_no_derivable_parent_is_stranded(tmp_path):
    """THE class. `NEW:` makes it dynamic, the body names no lane id, and no
    parent_contract header -> the driver can never instantiate it, and nothing
    anywhere goes red. If this assertion ever stops failing for the wrong
    reasons, the sweep has become decorative."""
    pack = tmp_path / "PACKETS"
    _packet(pack, "ORPHAN-1", "NEW:ORPHAN-1", body="No lane is named here at all.")
    rs = _run_state(tmp_path, {"L01": {"state": "READY"}})
    res = vpsweep.sweep(pack, rs, {})
    assert res["examined"] == 1
    stranded = [r["packet"] for r in res["stranded"]]
    assert stranded == ["ORPHAN-1"], res
    assert "parent_contract" in res["stranded"][0]["reason"]


def _twin_pair(tmp_path, parent_state, parent_out):
    """A REAL twin: synthesised by vppack.hosted_twins from the parent's
    BENCHMARK.md `[hosted] (gate: X)` row -- NOT a directory with `twin_of:` in
    its front matter. load_pack builds packets from an explicit allow-list that
    does not include `twin_of`, so a hand-written twin directory is silently an
    ordinary packet. My first version of this test did exactly that and proved
    nothing (third allow-list-drops-a-field bug of the night)."""
    pack = tmp_path / "PACKETS"
    d = _packet(pack, "L17-FIX", "L17-FIX")
    (d / "BENCHMARK.md").write_text(
        "## Benchmark\n\n- B1 proves the thing [hosted] (gate: DELIVERY-1)\n", encoding="utf-8")
    rs = _run_state(tmp_path, {"L17-FIX": {"state": parent_state, "output_sha": parent_out}})
    # The durable binding, as the driver keeps it. Without it `bound_task` falls
    # back to bind(), which for a FINISHED parent with no template returns
    # "hold" -- so the parent resolves to nothing and its twin waits forever.
    # Reality has this file; a fixture without it tests a system that cannot
    # exist.
    pdir = tmp_path / "packets"
    pdir.mkdir(exist_ok=True)
    (pdir / "L17-FIX.json").write_text(json.dumps({"task": "L17-FIX"}), encoding="utf-8")
    # the twin's gate must be OPEN or it parks on the gate check and this test
    # passes without ever reaching the twin-parent logic it exists to cover
    roster = {"owner_gates": {"DELIVERY-1": True}}
    return pack, rs, roster


def test_a_twin_is_never_its_own_parent(tmp_path):
    """Regression, found by the sweep's own first run against live data.

    My first twin check asked whether any task id started with `<twin_of>-`.
    `L17-REPLY-WIRING-FIX-1-HOSTED` starts with `L17-REPLY-WIRING-FIX-1-`, so
    the twin matched ITSELF as its parent and three packets that were correctly
    waiting were reported stranded. Prefix-matching ids is banned here exactly
    as it is in twin_base.requires and proof-record lookup.
    """
    pack, rs, roster = _twin_pair(tmp_path, "READY", None)
    res = vpsweep.sweep(pack, rs, roster, run_root=tmp_path)
    assert "L17-FIX-HOSTED" in {r["packet"] for r in res["waiting"]}, res
    assert "L17-FIX-HOSTED" not in {r["packet"] for r in res["stranded"]}, res

    # control: prove the twin's id IS a prefix match of its parent, so the old
    # buggy check would have matched it -- otherwise this passes vacuously
    assert "L17-FIX-HOSTED".startswith("L17-FIX" + "-")


def test_an_accepted_parent_lets_the_twin_stop_waiting(tmp_path):
    """The other polarity. If `waiting` were returned unconditionally the test
    above would pass forever, so this pins that the twin leaves that bucket once
    the parent has an accepted output."""
    pack, rs, roster = _twin_pair(tmp_path, "VERIFIED", "b" * 40)
    res = vpsweep.sweep(pack, rs, roster, run_root=tmp_path)
    assert "L17-FIX-HOSTED" not in {r["packet"] for r in res["waiting"]}, res


def test_a_closed_owner_gate_parks_the_twin_before_the_parent_is_consulted(tmp_path):
    """And the gate arm itself: with the gate shut the twin waits on the GATE,
    which is why the tests above must open it explicitly."""
    pack, rs, _ = _twin_pair(tmp_path, "VERIFIED", "b" * 40)
    res = vpsweep.sweep(pack, rs, {"owner_gates": {"DELIVERY-1": False}}, run_root=tmp_path)
    reason = next(r["reason"] for r in res["waiting"] if r["packet"] == "L17-FIX-HOSTED")
    assert "owner gate" in reason, reason


def test_a_newly_placed_packet_is_unseen_not_stranded(tmp_path):
    """R-PORTAL-OPTION-RACE-AND-TIMEOUT-OVERRIDES was placed 4 minutes before the
    sweep first ran and picked up 3 minutes later. Calling that stranded trains
    people to ignore the word on exactly the newest packets."""
    pack = tmp_path / "PACKETS"
    _packet(pack, "BRAND-NEW", "NEW:BRAND-NEW", body="No lane named.")
    rs = _run_state(tmp_path, {"L01": {"state": "READY"}})
    log = tmp_path / "driver.log"
    log.write_text("2026-09-21T00:00:00Z PACK scanned something else\n", encoding="utf-8")
    res = vpsweep.sweep(pack, rs, {}, driver_log=str(log))
    assert [r["packet"] for r in res["unseen"]] == ["BRAND-NEW"], res
    assert res["stranded"] == []
    assert res["driver_log_read"] is True

    # and once the driver has mentioned it, the same packet IS stranded
    log.write_text("2026-09-21T00:01:00Z PACK dir changed: +['BRAND-NEW']\n", encoding="utf-8")
    res2 = vpsweep.sweep(pack, rs, {}, driver_log=str(log))
    assert [r["packet"] for r in res2["stranded"]] == ["BRAND-NEW"], res2
    assert res2["unseen"] == []


def test_without_a_driver_log_the_sweep_says_it_cannot_tell(tmp_path):
    """`unseen` is 0 when no log was read -- which must not be presented as
    "nothing was newly placed"."""
    pack = tmp_path / "PACKETS"
    _packet(pack, "L01", "L01")
    rs = _run_state(tmp_path, {"L01": {"state": "READY"}})
    res = vpsweep.sweep(pack, rs, {})
    assert res["driver_log_read"] is False
    assert "cannot be told apart" in vpsweep.render(res)


def test_a_scan_that_examined_nothing_is_an_error_not_a_clean_bill(tmp_path):
    """Three times on this run a guard reported zero because it measured zero.
    An empty or wrong pack dir must be loud."""
    empty = tmp_path / "PACKETS"
    (empty / "not-a-packet").mkdir(parents=True)
    rs = _run_state(tmp_path, {"L01": {"state": "READY"}})
    with pytest.raises(vpsweep.SweepUnusable) as exc:
        vpsweep.sweep(empty, rs, {})
    assert "examined nothing" in str(exc.value)

    with pytest.raises(vpsweep.SweepUnusable):
        vpsweep.sweep(tmp_path / "does-not-exist", rs, {})


def test_an_empty_run_state_cannot_strand_every_packet(tmp_path):
    """The failure that would make the sweep maximally wrong: read an empty or
    half-written run-state and report all 292 packets stranded."""
    pack = tmp_path / "PACKETS"
    _packet(pack, "L01", "L01")
    for payload in ({"tasks": {}}, {}, {"tasks": None}):
        rs = tmp_path / "empty.json"
        rs.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(vpsweep.SweepUnusable):
            vpsweep.sweep(pack, rs, {})


def test_a_twin_waiting_on_a_cancelled_parent_is_not_just_waiting(tmp_path):
    """`waiting` implies "it may still resolve". A parent row that is CANCELLED
    will never produce an output, so its twin waits forever and nothing anywhere
    goes red -- the same silence the sweep exists to break. Live: two rows,
    L17-REPLY-WIRING-FIX-1-HOSTED and L17-REPLY-WIRING-HOSTED.

    Deliberately NOT raised as `stranded`: a CANCELLED row usually has a
    successor in blocker.closers and the packet may rebind, so this is reported
    for a human to judge rather than alarmed on.
    """
    pack, rs, roster = _twin_pair(tmp_path, "CANCELLED", None)
    res = vpsweep.sweep(pack, rs, roster, run_root=tmp_path)
    assert [r["packet"] for r in res["blocked_parent"]] == ["L17-FIX-HOSTED"], res
    assert "L17-FIX-HOSTED" not in {r["packet"] for r in res["waiting"]}
    assert res["stranded"] == [], "a terminal parent is reported, not alarmed on"
    assert "never produce an output" in vpsweep.render(res)

    # the discriminating arm: an UNFINISHED parent is ordinary waiting, so the
    # bucket is not just "every twin without an output"
    pack2, rs2, roster2 = _twin_pair(tmp_path / "b", "READY", None)
    res2 = vpsweep.sweep(pack2, rs2, roster2, run_root=tmp_path / "b")
    assert res2["blocked_parent"] == []
    assert "L17-FIX-HOSTED" in {r["packet"] for r in res2["waiting"]}
