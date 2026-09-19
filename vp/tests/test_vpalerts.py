#!/usr/bin/env python3
"""tests/test_vpalerts.py -- severity from pattern, not from kind."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vpalerts  # noqa: E402

T0 = datetime(2026, 9, 17, 20, 32, tzinfo=timezone.utc)


def ts(minutes):
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def alert(kind, task, minutes, text=""):
    return {"ts": ts(minutes), "kind": kind, "task": task, "text": text or ("%s %s" % (task, kind))}


def test_phantom_loop_escalates_by_family_even_though_task_ids_change():
    # the 2026-09-17 incident: PROOF_UNKNOWN on B5-1, B5-2, B5-3 ... every ~30 min
    stream = [alert("PROOF_UNKNOWN", "R-L17-REPLY-WIRING-R1-B5-%d" % n, 30 * (n - 1)) for n in range(1, 8)]
    tiers = [res["severity"] for _a, res in vpalerts.annotate(stream)]
    assert tiers[0] == vpalerts.ATTENTION          # first failure-class alert: look, no page
    assert tiers[1] == vpalerts.ATTENTION          # repeat=2 in the family
    assert tiers[2] == vpalerts.URGENT             # third cycle inside 6h -> page
    assert all(t == vpalerts.URGENT for t in tiers[2:])
    last = list(vpalerts.annotate(stream))[-1][1]
    assert last["repeat"] == 1                     # exact task id never repeats ...
    assert last["repeat_family"] == 7              # ... the family does
    assert last["family"] == "R-L17-REPLY-WIRING/PROOF_UNKNOWN"


def test_idle_pings_stay_routine_however_often_they_repeat():
    stream = [alert("IDLE", None, 30 * n, "idle for %d min" % (30 * n)) for n in range(1, 25)]
    assert {res["severity"] for _a, res in vpalerts.annotate(stream)} == {vpalerts.ROUTINE}


def test_idle_with_growing_backlog_is_attention_then_urgent():
    stream = [alert("IDLE", None, 30 * n, "idle") for n in range(1, 6)]
    backlog = [(ts(0), 3), (ts(60), 5), (ts(120), 9)]
    res = [r for _a, r in vpalerts.annotate(stream, backlog=backlog)]
    assert res[-1]["backlog_delta"] == 6
    assert res[-1]["severity"] == vpalerts.URGENT
    assert any("backlog" in r for r in res[-1]["reasons"])


def test_static_kind_alone_does_not_decide():
    # a single STUCK is attention (first failure), the third within the window is urgent
    stream = [alert("STUCK", "L30-MATRIX", 0), alert("STUCK", "L30-MATRIX", 40), alert("STUCK", "L30-MATRIX", 80)]
    tiers = [r["severity"] for _a, r in vpalerts.annotate(stream)]
    assert tiers == [vpalerts.ATTENTION, vpalerts.ATTENTION, vpalerts.URGENT]


def test_repeats_outside_the_window_do_not_count():
    stream = [alert("STUCK", "X", 0), alert("STUCK", "X", 7 * 60), alert("STUCK", "X", 14 * 60)]
    res = [r for _a, r in vpalerts.annotate(stream)]
    assert [r["repeat"] for r in res] == [1, 1, 1]
    assert res[-1]["severity"] == vpalerts.ATTENTION


def test_long_open_streak_is_urgent_by_age():
    # a 2h repeat window keeps repeat<=2 while the 2h streak gap keeps the
    # 100-min cadence one open streak: at t=300m the item has been open 5h
    cfg = {"window_s": 2 * 3600}
    stream = [alert("HOSTED_OWED", "L22-HOSTED", 100 * n) for n in range(4)]
    res = [r for _a, r in vpalerts.annotate(stream, cfg=cfg)]
    assert res[-1]["repeat"] == 2
    assert res[-1]["age_s"] == 300 * 60
    assert res[-1]["severity"] == vpalerts.URGENT
    assert any(r.startswith("open") for r in res[-1]["reasons"])


def test_floor_urgent_kinds_page_on_first_sight():
    res = vpalerts.assess(alert("BUDGET", None, 0, "spent 401 of 400"), [])
    assert res["severity"] == vpalerts.URGENT
    assert "stops the driver" in res["reasons"][0]


def test_subject_falls_back_to_first_word_of_text():
    a = {"kind": "PACKET_PARENT_GUESSED", "task": None, "ts": ts(0),
         "text": "AUTH-ORACLE parent RECOVERY-JUNIOR-T3R3 derived by name"}
    assert vpalerts.subject_of(a) == "AUTH-ORACLE"
    assert vpalerts.family_of("L17-REPLY-WIRING-R1-B5-3") == "L17-REPLY-WIRING"
    assert vpalerts.family_of("ADMISSION-SEED-FIX-HOSTED-R1") == "ADMISSION-SEED-FIX-HOSTED"


def test_config_overrides_from_roster_block():
    cfg = vpalerts.config({"repeat_urgent": 2, "floor_urgent": ["DISK"], "unknown": 1})
    assert cfg["repeat_urgent"] == 2
    assert cfg["floor_urgent"] == {"DISK"}
    assert "unknown" not in cfg
    stream = [alert("STUCK", "X", 0), alert("STUCK", "X", 10)]
    assert [r["severity"] for _a, r in vpalerts.annotate(stream, cfg=cfg)][-1] == vpalerts.URGENT


def test_unparseable_timestamps_do_not_crash():
    stream = [{"kind": "STUCK", "task": "X", "ts": "garbage", "text": ""},
              {"kind": "STUCK", "task": "X", "ts": None, "text": ""}]
    out = list(vpalerts.annotate(stream))
    assert len(out) == 2 and all(r["severity"] in vpalerts.TIERS for _a, r in out)
