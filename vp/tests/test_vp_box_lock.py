"""D76b: two-tier box lock (BULK-RULING §21.2 amendment 4).

Acceptance (Architect, 2026-09-19): two small proofs overlap and both pass; a
full proof waits until both slots are free and then holds box.lock.d + both
slots; an external `vp_box_lock exclusive` call blocks every driver proof; a
dead holder is reaped with an alert, never waited on forever."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP))

import vp_box_lock as bl  # noqa: E402
import vpproof  # noqa: E402

NOSLEEP = lambda s: None  # noqa: E731


def held(cn):
    return sorted(n for n in bl.LOCK_NAMES if (cn / n).exists())


def test_tier_for_full_or_many_paths_is_exclusive_else_slot():
    assert bl.tier_for("full", []) == bl.EXCLUSIVE
    assert bl.tier_for("targeted", ["p%d" % i for i in range(9)]) == bl.EXCLUSIVE
    assert bl.tier_for("targeted", ["p%d" % i for i in range(8)]) == bl.SLOT
    assert bl.tier_for("targeted", ["p1"], {"targeted_max_paths": 0}) == bl.EXCLUSIVE
    assert bl.tier_for("targeted", ["p%d" % i for i in range(12)], {"targeted_max_paths": 12}) == bl.SLOT


def test_two_small_proofs_overlap_a_third_waits_and_a_portal_small_takes_portal(tmp_path):
    a = bl.BoxLocks(tmp_path, "s1", "small-a", tier=bl.SLOT)
    b = bl.BoxLocks(tmp_path, "s2", "small-b", tier=bl.SLOT)
    assert a.acquire(0, sleep=NOSLEEP) == (True, "")
    assert b.acquire(0, sleep=NOSLEEP) == (True, "")
    assert held(tmp_path) == sorted(bl.SLOTS), "two smalls hold one slot each, never box.lock.d"
    assert a.held == [bl.SLOTS[0]] and b.held == [bl.SLOTS[1]]
    c = bl.BoxLocks(tmp_path, "s3", "small-c", tier=bl.SLOT)
    ok, why = c.acquire(0, sleep=NOSLEEP)
    assert not ok and "both slots busy" in why and c.held == []
    a.release()
    assert c.acquire(0, sleep=NOSLEEP)[0] and c.held == [bl.SLOTS[0]], "a freed slot is taken"
    b.release(); c.release()
    assert held(tmp_path) == []
    p = bl.BoxLocks(tmp_path, "s4", "small-portal", tier=bl.SLOT, proof_kind="portal")
    assert p.acquire(0, sleep=NOSLEEP)[0] and p.held == [bl.SLOTS[0], bl.PORTAL]
    p.release()


def test_exclusive_waits_for_both_slots_then_holds_box_and_both_slots(tmp_path):
    a = bl.BoxLocks(tmp_path, "s1", "small-a", tier=bl.SLOT)
    assert a.acquire(0, sleep=NOSLEEP)[0]
    full = bl.BoxLocks(tmp_path, "f", "full-1", tier=bl.EXCLUSIVE)
    ok, why = full.acquire(0, sleep=NOSLEEP)
    assert not ok and "small-a" in why
    assert held(tmp_path) == [bl.SLOTS[0]], "a waiting exclusive holds nothing (no partial hold)"
    a.release()
    assert full.acquire(0, sleep=NOSLEEP) == (True, "")
    assert full.held == [bl.BOX, bl.SLOTS[0], bl.SLOTS[1], bl.PORTAL], "fixed order: box, slot-0, slot-1, portal"
    owner = json.loads((tmp_path / bl.BOX / "owner.json").read_text())
    assert owner["tier"] == bl.EXCLUSIVE and owner["proof_id"] == "full-1"
    # while it holds, neither a small nor another exclusive gets in
    assert not bl.BoxLocks(tmp_path, "s", "small-b", tier=bl.SLOT).acquire(0, sleep=NOSLEEP)[0]
    assert not bl.BoxLocks(tmp_path, "f", "full-2", tier=bl.EXCLUSIVE).acquire(0, sleep=NOSLEEP)[0]
    full.release()
    assert held(tmp_path) == []


def test_external_exclusive_cli_blocks_every_driver_proof(tmp_path):
    """the shared helper: `vp_box_lock.py exclusive -- <cmd>` holds box + both
    slots for the command's lifetime; a driver small proof cannot start meanwhile."""
    marker = tmp_path / "held.json"
    probe = ("import json,sys,time,pathlib; sys.path.insert(0,%r); import vp_box_lock as bl; "
             "pathlib.Path(%r).write_text(json.dumps(sorted(n for n in bl.LOCK_NAMES if (pathlib.Path(%r)/n).exists()))); "
             "time.sleep(0.2)" % (str(VP), str(marker), str(tmp_path)))
    rc = subprocess.call([sys.executable, str(VP / "vp_box_lock.py"), "exclusive", "--cn", str(tmp_path),
                          "--wait-max-min", "0", "--", sys.executable, "-c", probe])
    assert rc == 0
    assert json.loads(marker.read_text()) == sorted([bl.BOX, bl.SLOTS[0], bl.SLOTS[1], bl.PORTAL])
    assert held(tmp_path) == [], "released after the command"
    # and a held exclusive (simulated by a live holder) refuses the CLI: EX_TEMPFAIL, command never runs
    a = bl.BoxLocks(tmp_path, "s", "small-a", tier=bl.SLOT)
    assert a.acquire(0, sleep=NOSLEEP)[0]
    never = tmp_path / "never"
    rc = subprocess.call([sys.executable, str(VP / "vp_box_lock.py"), "exclusive", "--cn", str(tmp_path),
                          "--wait-max-min", "0", "--", sys.executable, "-c", "open(%r,'w').write('x')" % str(never)])
    assert rc == bl.EX_TEMPFAIL and not never.exists()
    a.release()
    out = subprocess.check_output([sys.executable, str(VP / "vp_box_lock.py"), "status", "--cn", str(tmp_path)])
    assert all(not v["held"] for v in json.loads(out).values())


def test_dead_holder_is_reaped_with_an_alert_and_a_live_one_is_not(tmp_path):
    (tmp_path / bl.SLOTS[0]).mkdir()
    (tmp_path / bl.SLOTS[0] / "owner.json").write_text(json.dumps(
        {"pid": 4194304, "token": "t", "mono_epoch": 0, "proof_id": "ghost", "tier": bl.SLOT}))
    (tmp_path / bl.SLOTS[1]).mkdir()
    (tmp_path / bl.SLOTS[1] / "owner.json").write_text(json.dumps(
        {"pid": os.getpid(), "token": "t2", "mono_epoch": 0, "proof_id": "alive", "tier": bl.SLOT}))
    alerts, logs = [], []
    a = bl.BoxLocks(tmp_path, "s", "small-a", log=logs.append, tier=bl.SLOT, alert=lambda k, t: alerts.append((k, t)),
                    alive=lambda pid: int(pid) == os.getpid())
    assert a.acquire(0, sleep=NOSLEEP)[0] and a.held == [bl.SLOTS[0]], "the dead holder's slot is reaped and taken"
    assert alerts and alerts[0][0] == "BOX_LOCK_REAPED" and "ghost" in alerts[0][1]
    assert any("reaping" in m for m in logs)
    assert json.loads((tmp_path / bl.SLOTS[1] / "owner.json").read_text())["proof_id"] == "alive", "live holder untouched"
    a.release()


def test_vpproof_keeps_its_lock_names_and_computes_the_tier():
    assert vpproof.BoxLocks is bl.BoxLocks and vpproof.lock_status is bl.lock_status
    assert vpproof.LOCK_NAMES == bl.LOCK_NAMES
    import lanedriver
    assert "vp_box_lock" in lanedriver.RELOAD_ORDER
    assert lanedriver.RELOAD_ORDER.index("vp_box_lock") < lanedriver.RELOAD_ORDER.index("vpproof")


def test_d76b_1_small_proofs_yield_to_a_waiting_exclusive_so_it_cannot_starve(tmp_path):
    """D76b-1 (Expert Coder): mkdir locks are not FIFO -- a newcomer small proof
    could win a freed slot ahead of a waiting full proof forever.  The exclusive
    keeps box.lock.d while it waits and small proofs yield whenever box.lock.d
    exists, so the slots drain to the exclusive."""
    a = bl.BoxLocks(tmp_path, "s1", "small-a", tier=bl.SLOT)
    assert a.acquire(0, sleep=NOSLEEP)[0]
    full = bl.BoxLocks(tmp_path, "f", "full-1", tier=bl.EXCLUSIVE)
    rounds = []
    def one_round(_s):
        rounds.append(1)
        if len(rounds) == 1:
            # while the exclusive waits it holds box.lock.d; a newcomer small yields
            assert full.held == [bl.BOX] and (tmp_path / bl.BOX).exists()
            b = bl.BoxLocks(tmp_path, "s2", "small-b", tier=bl.SLOT)
            ok, why = b.acquire(0, sleep=NOSLEEP)
            assert not ok and "yielding" in why and b.held == []
            a.release()                        # the running small finishes
        if len(rounds) > 3:
            raise AssertionError("exclusive never got the slots")
    assert full.acquire(60, sleep=one_round) == (True, "")
    assert full.held == [bl.BOX, bl.SLOTS[0], bl.SLOTS[1], bl.PORTAL]
    full.release()
    assert held(tmp_path) == []
    # a timed-out exclusive leaves nothing behind (box.lock.d is not leaked)
    c = bl.BoxLocks(tmp_path, "s3", "small-c", tier=bl.SLOT)
    assert c.acquire(0, sleep=NOSLEEP)[0]
    full2 = bl.BoxLocks(tmp_path, "f", "full-2", tier=bl.EXCLUSIVE)
    assert not full2.acquire(0, sleep=NOSLEEP)[0] and full2.held == [] and not (tmp_path / bl.BOX).exists()
    c.release()


def test_d76b_1b_exclusive_killed_while_holding_box_lock_is_reaped_by_the_next_small_proof(tmp_path):
    """acceptance (b): a real exclusive holder process is killed while it holds
    box.lock.d (+ both slots); the next small proof reaps the corpse's locks
    with a BOX_LOCK_REAPED alert and runs instead of yielding forever."""
    import signal
    import time
    holder = subprocess.Popen([sys.executable, str(VP / "vp_box_lock.py"), "exclusive", "--cn", str(tmp_path),
                               "--wait-max-min", "1", "--proof-id", "doomed-full", "--", sys.executable, "-c",
                               "import time; time.sleep(60)"])
    for _ in range(100):
        if (tmp_path / bl.BOX).exists() and all((tmp_path / n).exists() for n in bl.SLOTS):
            break
        time.sleep(0.05)
    assert json.loads((tmp_path / bl.BOX / "owner.json").read_text())["proof_id"] == "doomed-full"
    holder.send_signal(signal.SIGKILL)
    holder.wait(10)
    assert (tmp_path / bl.BOX).exists(), "the corpse's locks are still on disk"
    alerts, logs = [], []
    small = bl.BoxLocks(tmp_path, "s", "small-after", log=logs.append, tier=bl.SLOT,
                        alert=lambda k, t: alerts.append((k, t)))
    ok, why = small.acquire(0, sleep=NOSLEEP)
    assert ok, why
    assert small.held == [bl.SLOTS[0]]
    kinds = [k for k, _ in alerts]
    assert kinds and set(kinds) == {"BOX_LOCK_REAPED"} and any("doomed-full" in t for _, t in alerts)
    assert any(bl.BOX in t for _, t in alerts) and any(bl.SLOTS[0] in t for _, t in alerts)
    assert not (tmp_path / bl.BOX).exists(), "box.lock.d reaped, not yielded to"
    small.release()
    assert held(tmp_path) == sorted([bl.SLOTS[1], bl.PORTAL]), "the corpse's other locks are reaped lazily by whoever needs them next"
