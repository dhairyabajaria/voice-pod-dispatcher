"""One portal suite at a time, and a red that is OURS must never read like a red that is THEIRS.

BOSS measured it on 2026-09-07 across 27 gate portal runs: the 24 that had the box to themselves
were ALL GREEN; the 3 that overlapped another gate's portal run were ALL RED. The overlap set and
the red set were the same set, no exceptions in either direction. His control — trunk's own portal
suite, 833 tests, green alone; the same tree run twice concurrently produced 5 and 4 failures,
different tests each time, all wait-timeout shaped. A known-green tree goes red purely from
concurrency, and each invented red costs an executor a full rework round.

Portal proofs were classified box-free, so the daemon's one-gate-at-a-time deferral never covered
them. This file pins the three things that fix it: the lock serialises, a gate that cannot get the
lock records NOT RUN rather than running anyway, and an overlap that happens regardless is DISCLOSED
in the gate file.

Hermetic: temp CN, no vitest, no npm, no box."""
import importlib.util, os, shutil, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location("mgp", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mgp"] = MG; spec.loader.exec_module(MG)

ROOT = tempfile.mkdtemp(prefix="portallock-")
os.makedirs(os.path.join(ROOT, "test-logs"))
MG.CN = ROOT
MG.PORTAL_LOCK = os.path.join(ROOT, "test-logs", "portal.lock.d")

# The lock path follows CN at CALL time. At 06:18 it did not: tests/test_portal_row.py sets MG.CN
# after import, the constant had already been bound to the real CN, and the dispatcher selftest sat
# blocked behind a LIVE gate's portal run (B.016a.analytics-ui, pid 36227). A suite that can take
# the production lock stalls every gate on the box for as long as it runs.
MG.PORTAL_LOCK = None
MG.CN = ROOT
check("MUST-BITE  the lock path follows CN at call time — a test that repoints CN must not be able "
      "to reach, or take, the production lock",
      MG.portal_lock_dir().startswith(ROOT), MG.portal_lock_dir())
MG.CN = "/nowhere/at/all"
check("  it is re-derived on every call, not cached from import",
      MG.portal_lock_dir().startswith("/nowhere/at/all"), MG.portal_lock_dir())
MG.CN = ROOT
MG.PORTAL_LOCK = os.path.join(ROOT, "test-logs", "portal.lock.d")

# ---------------------------------------------------------------- the lock itself
ok, waited = MG.acquire_portal_lock("portal-A-pid=%d-1" % os.getpid())
check("the first gate takes the lock", ok is True and waited < 1, (ok, waited))
ok2, why = MG.acquire_portal_lock("portal-B-pid=%d-2" % os.getpid(), wait_max=1, sleep=0.2)
check("MUST-BITE  a second gate does NOT get it while the first holds it",
      ok2 is False and "held the portal lock" in why, (ok2, why))

held = []
def second():
    got, w = MG.acquire_portal_lock("portal-C-pid=%d-3" % os.getpid(), wait_max=30, sleep=0.2)
    held.append((got, w))
t = threading.Thread(target=second); t.start()
time.sleep(1.0)
check("MUST-BITE  it QUEUES rather than failing — waiting is the point, a concurrent run is wrong",
      not held, held)
MG.release_portal_lock("portal-A-pid=%d-1" % os.getpid())
t.join(timeout=20)
check("  ...and it acquires the moment the first releases",
      held and held[0][0] is True and held[0][1] >= 0.5, held)
MG.release_portal_lock("portal-C-pid=%d-3" % os.getpid())
check("CONTROL  the lock is really gone after a release — otherwise every check above is vacuous",
      not os.path.isdir(MG.PORTAL_LOCK) and MG.acquire_portal_lock("portal-D-pid=1-4", wait_max=1)[0],
      os.path.isdir(MG.PORTAL_LOCK))
MG.release_portal_lock("portal-D-pid=1-4")

# a lock whose owner process is GONE and which is old must be broken, or one crash wedges every
# later gate; a lock whose owner is ALIVE must never be stolen, however old.
os.mkdir(MG.PORTAL_LOCK)
open(os.path.join(MG.PORTAL_LOCK, "owner"), "w").write("portal-DEAD-pid=999999-1")
os.utime(os.path.join(MG.PORTAL_LOCK, "owner"), (time.time() - 4000, time.time() - 4000))
got, w = MG.acquire_portal_lock("portal-E-pid=%d-5" % os.getpid(), wait_max=3, sleep=0.2)
check("MUST-BITE  an OLD lock whose owner process is dead is broken — a crashed gate must not wedge "
      "every later portal run", got is True, (got, w))
MG.release_portal_lock("portal-E-pid=%d-5" % os.getpid())
os.mkdir(MG.PORTAL_LOCK)
open(os.path.join(MG.PORTAL_LOCK, "owner"), "w").write("portal-LIVE-pid=%d-1" % os.getpid())
os.utime(os.path.join(MG.PORTAL_LOCK, "owner"), (time.time() - 9000, time.time() - 9000))
got, why = MG.acquire_portal_lock("portal-F-pid=%d-6" % os.getpid(), wait_max=1, sleep=0.2)
check("MUST-BITE  CONTROL: a lock whose owner is ALIVE is never stolen, however old — a live "
      "20-minute suite must not be interrupted by an impatient gate", got is False, (got, why))
shutil.rmtree(MG.PORTAL_LOCK)

# ---------------------------------------------------------------- the row a blocked gate writes
rows = []
def rec(name, passed, detail): rows.append((name, passed, detail))
warns = []
def warn(t): warns.append(t)

os.mkdir(MG.PORTAL_LOCK)
open(os.path.join(MG.PORTAL_LOCK, "owner"), "w").write("portal-OTHER-pid=%d-1" % os.getpid())
wt = os.path.join(ROOT, "wt"); os.makedirs(os.path.join(wt, "portal", "node_modules"))
MG.PORTAL_WAIT_MAX = 1
MG.run_portal_proofs(["portal/src/x.test.tsx"], wt, "B.016a.analytics-ui", rec, warn=warn)
check("MUST-BITE  a gate that cannot get the lock records NOT RUN, never FAIL — running it anyway "
      "is what produced the three false reds",
      rows and rows[-1][1] is None and "NOT RUN" in rows[-1][2], rows[-1:])
check("  and the row says WHY, so the reader is not left guessing at a missing suite",
      "held the portal lock" in rows[-1][2], rows[-1][2][:150])
check("CONTROL  no vitest was run — the check above must not pass by accidentally running one",
      not [f for f in os.listdir(os.path.join(ROOT, "test-logs")) if f.endswith("-portal.log")],
      os.listdir(os.path.join(ROOT, "test-logs")))
shutil.rmtree(MG.PORTAL_LOCK)

# ---------------------------------------------------------------- overlap disclosure
d = os.path.join(ROOT, "test-logs")
mine = os.path.join(d, "20260907-0506-gate-B.016a.analytics-ui-portal.log")
open(mine, "w").write("x")
other = os.path.join(d, "20260907-0508-gate-B.015a.operational-raw-views-portal.log")
open(other, "w").write("x")
solo = os.path.join(d, "20260907-0400-gate-B.010.earlier-portal.log")
open(solo, "w").write("x")
import datetime as dt
start = dt.datetime(2026, 9, 7, 5, 6).timestamp()
end = dt.datetime(2026, 9, 7, 5, 14).timestamp()
os.utime(other, (dt.datetime(2026, 9, 7, 5, 16).timestamp(),) * 2)
os.utime(solo, (dt.datetime(2026, 9, 7, 4, 2).timestamp(),) * 2)
ov = MG.overlapping_portal_runs(mine, start, end)
check("MUST-BITE  a portal run that overlapped another is DETECTED, by the same reconstruction "
      "BOSS did by hand", any("operational-raw-views" in x for x in ov), ov)
check("MUST-BITE  CONTROL: a run that did NOT overlap is not reported — otherwise the disclosure "
      "fires on every gate and means nothing", not any("earlier" in x for x in ov), ov)
check("  and the run never reports ITSELF as an overlap",
      not any("analytics-ui" in x for x in ov), ov)

# ---------------------------------------------------------------- a gate QUEUED on the box says so
# 2026-09-07 06:0x: BOSS killed two HEALTHY gates believing the agy leg had hung. They were 43 and 28
# minutes into a legitimate box wait behind EXEC-G's build (lock held from 05:13:48), and both had
# already paid for their Codex reviews. Nothing said they were waiting: no event, no file, and the
# .md is written only at the end — so a queued gate and a hung one were the same observation. The
# cap is 60 minutes, which is longer than anybody's patience, so the wait has to announce itself.
emits = []
MG.emit = lambda kind, item, detail: emits.append((kind, item, detail))
MG.LOCK = os.path.join(ROOT, "test-logs", "box.lock.d")
os.mkdir(MG.LOCK)
open(os.path.join(MG.LOCK, "owner"), "w").write("EXEC-G-RW5b-1788738228 pid=95921")
MG.time = type("T", (), {"sleep": staticmethod(lambda _s: None), "time": time.time})()
rows2 = []
def rec2(name, passed, detail): rows2.append((name, passed, detail))
it = {"id": "B.017a.backfills-r2", "proof_files": ["tests/test_backfills.py"]}
MG._run_proofs(it, os.path.join(ROOT, "wt"), "abc1234", "abc1234", "B.017a.backfills-r2", rec2)
kinds = [k for k, _i, _d in emits]
check("MUST-BITE  a gate queued on the box SAYS SO — waiting and hanging must not be the same "
      "observation, which is what cost two healthy gates tonight",
      "GATE_WAITING_FOR_BOX" in kinds, kinds[:3])
first = [d for k, _i, d in emits if k == "GATE_WAITING_FOR_BOX"][0]
check("  it names the box owner, so the reader can see WHO it is behind",
      "EXEC-G" in first and "95921" in first, first[:120])
check("MUST-BITE  ...and it says the gate is alive and what killing it costs",
      "ALIVE" in first and "Codex review" in first, first[:200])
check("MUST-BITE  it repeats while waiting — one line at minute zero is gone from the screen by "
      "minute forty",
      len([k for k in kinds if k == "GATE_WAITING_FOR_BOX"]) >= 6,
      len([k for k in kinds if k == "GATE_WAITING_FOR_BOX"]))
check("  giving up after the cap is its own event, not a silence",
      "GATE_GAVE_UP_ON_BOX" in kinds, kinds[-2:])
check("MUST-BITE  and the row is NOT RUN, never FAIL — the box being busy says nothing about the "
      "candidate",
      rows2 and rows2[-1][1] is None and "box busy" in rows2[-1][2], rows2[-1:])
check("  which names the owner too", rows2 and "EXEC-G" in rows2[-1][2], rows2[-1:])

shutil.rmtree(ROOT, ignore_errors=True)
print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
