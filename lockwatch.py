#!/usr/bin/env python3
"""Box-lock watchdog — DETECTION ONLY. It never releases, deletes, or writes the lock.

  lockwatch.py --once                 one poll, print what it would emit
  lockwatch.py --watch [--interval 5] standalone loop (no daemon restart needed)
  lockwatch.py --selftest             must-pass + must-bite 4/4 calibration
  lockwatch.py --status               print the current lock snapshot and stored state

WHY (BOSS, 2026-09-05 16:19). `$CN/test-logs/box.lock.d` changed hands IN PLACE: the directory's
mtime stayed at 16:15:44 (EXEC-F's mkdir) while `owner` was rewritten at 16:19:08 with EXEC-G's
token. No successful mkdir happened, so the mutual exclusion the whole box protocol rests on did
not hold, and nothing anywhere said so. Two sessions believing they own one Postgres is the
failure that [[voicepod-pytest-liveness-and-buffering-trap]] is about; this makes it audible.

The lock protocol it watches (mergegate.run_proofs, and the same recipe by hand):
    mkdir box.lock.d            <- the ONLY mutual exclusion; fails if held
    write box.lock.d/owner      <- immediately after, so owner mtime ~= dir mtime
    ...                         <- hold
    rm owner; rmdir box.lock.d  <- release_if_mine, never a recursive delete

FOUR ANOMALIES, all logged to events.log as LOCK_ANOMALY, each fired EXACTLY ONCE per episode:
  a OWNER_MOVED_IN_PLACE   owner text changed while the dir mtime did NOT — ownership moved with no
                           mkdir. The real defect; the other three are context. An owner change with
                           the dir mtime BUMPED is a recreated dir, i.e. a real mkdir, i.e. a
                           legitimate handoff: that is (d) RACE_CHURN, not this. (Corrected
                           2026-09-06 after a live false positive at 20:36:37.)
  b HALF_RELEASED          dir present, owner absent, for > 60 s — a release that stopped halfway,
                           or an acquirer that died between mkdir and the owner write. The next
                           acquirer's mkdir fails forever against a lock nobody holds.
  c LONG_HOLD              one token held > 45 min (informational). Carries whether the census
                           actually sees a live pytest, because a long hold with NO live pytest is
                           an abandoned lock and a long hold with one is just a slow suite.
  d RACE_CHURN             dir mtime moved BACKWARDS, or the dir was recreated within 2 s of an
                           observed release, or the owner changed WITH the dir mtime bumped — a
                           release and re-acquire that both fell inside one poll (informational).

COLD-START INFERENCE, and why (a) has two detectors. A watchdog started after the incident has no
previous snapshot, so a transition-only detector is blind to exactly the event that motivated it —
it would have reported the 16:19 lock as perfectly healthy. But an in-place move leaves a mark in
the filesystem that survives: a normal acquire writes `owner` milliseconds after the mkdir, so
`owner_mtime - dir_mtime` is sub-second, while today's lock shows a 3m24s gap. On the first
observation of a lock, that skew alone raises (a) — labelled `cold-start inference` in the event,
because inferring a transition from a timestamp gap is weaker evidence than watching it happen and
BOSS should be able to tell the two apart without reading this file.

DETECTION ONLY, STRUCTURALLY. This module opens the lock path with `os.stat` and one `open(...)`
for reading. There is no code path here that writes, creates, or removes anything under
box.lock.d — only its own state file under test-logs/driver/. A future edit that adds one turns a
watchdog into a second actor racing the thing it watches.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

CN = os.environ.get("CN", "/Users/dhairyabajaria/Claude Code/Calling New")
D = os.path.join(CN, "test-logs", "driver")
EVENTS = os.path.join(D, "events.log")
LOCK = os.path.join(CN, "test-logs", "box.lock.d")
STATE = os.path.join(D, "lockwatch.json")
PIDFILE = os.path.join(D, "lockwatch.pid")
CENSUS = os.path.join(CN, "live_pytest.py")

ACQUIRE_SKEW_S = 5.0     # owner written this long after mkdir is not one acquire
HALF_RELEASED_S = 60.0
LONG_HOLD_S = 45 * 60.0
CHURN_S = 2.0


def now_local():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def snapshot(lock=LOCK, t=None):
    """Everything one poll records. Pure reads: stat, isdir, and one read of `owner`."""
    s = {"t": t if t is not None else time.time(), "exists": os.path.isdir(lock),
         "dir_mtime": None, "owner_exists": False, "owner_mtime": None, "owner_text": None}
    if not s["exists"]:
        return s
    try:
        s["dir_mtime"] = os.stat(lock).st_mtime
    except OSError:
        s["exists"] = False
        return s
    op = os.path.join(lock, "owner")
    try:
        st = os.stat(op)
        s["owner_exists"] = True
        s["owner_mtime"] = st.st_mtime
        s["owner_text"] = open(op, errors="ignore").read().strip()
    except OSError:
        pass
    return s


def census_live():
    """-> (n_or_None, text). Gate on the parsed `count:`, NEVER on the exit code: the census tool
    exits 0 whether the box is busy or free ([[voicepod-pytest-liveness-and-buffering-trap]]), so
    reading $? here would report every long hold as idle."""
    try:
        r = subprocess.run(["/usr/bin/python3", CENSUS], capture_output=True, text=True, timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001 — census is context for an informational event
        return None, f"census failed: {type(e).__name__}: {e}"
    m = re.search(r"^count:\s*(\d+)", out, re.M)
    if not m:
        return None, f"census printed no `count:` line ({out.strip()[:60]!r})"
    n = int(m.group(1))
    if n:
        return n, f"census count={n} (a live pytest is running — a slow suite, not a stuck lock)"
    # `count=0` says ONE thing: no pytest is running. It does NOT say the holder is gone. The
    # first version of this line read "NO live pytest — lock looks abandoned", and BOSS refuted it
    # within the hour (2026-09-05 16:47): the opencode server had died at 16:42:59 and EXEC-G was
    # FROZEN mid-rework still holding the box — count=0 with a live holder that resumes believing
    # it owns the lock. A watchdog is allowed to report a measurement; it is not allowed to
    # convert one into a conclusion that invites someone to release a lock it cannot see the
    # owner of. Naming the readings it cannot tell apart is the honest form.
    return n, ("census count=0 (NO live pytest — the holder may be FROZEN, dead, or finished; this "
               "measurement CANNOT tell those apart. Do NOT release on this alone: check the "
               "holder's session and the opencode server first)")


def ts(x):
    return datetime.fromtimestamp(x).strftime("%H:%M:%S") if x else "-"


def standalone_alive():
    """True while a `--watch` process is running. The daemon hook defers to it.

    Two watchers sharing one state file would race the fired-set and could double-report or, worse,
    each mark an episode fired and both stay quiet. Rather than depend on the operator killing one
    at exactly the right moment in a restart sequence — the standalone must stay up until the
    daemon actually restarts, or the box goes unwatched during the gap — the two are made mutually
    exclusive here, so either kill order is safe and forgetting to kill is safe too.
    """
    try:
        pid = int(open(PIDFILE).read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)          # signal 0: existence check only, never touches the process
        return pid != os.getpid()
    except OSError:
        try:
            os.unlink(PIDFILE)   # stale pidfile from a killed watcher
        except OSError:
            pass
        return False


class LockWatch:
    """One poll per tick. `emit(kind, sub, token, detail)` receives each anomaly."""

    def __init__(self, emit, lock=LOCK, state_path=STATE, census=census_live):
        self.emit = emit
        self.lock = lock
        self.state_path = state_path
        self.census = census
        self.st = {}
        if state_path and os.path.exists(state_path):
            try:
                self.st = json.load(open(state_path))
            except Exception:
                self.st = {}
        self.st.setdefault("prev", None)
        self.st.setdefault("fired", {})      # episode key -> when, so each fires exactly once
        self.st.setdefault("owner_absent_since", None)
        self.st.setdefault("absent_at", None)   # last time the dir was observed MISSING

    def save(self):
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            json.dump(self.st, open(self.state_path + ".tmp", "w"), indent=1)
            os.replace(self.state_path + ".tmp", self.state_path)
        except Exception:
            pass

    def _fire(self, key, sub, token, detail):
        """Exactly once per episode. `key` identifies the EPISODE, not the poll."""
        if key in self.st["fired"]:
            return False
        self.st["fired"][key] = now_local()
        self.emit("LOCK_ANOMALY", sub, token or "-", detail)
        return True

    def poll(self, snap=None):
        cur = snap if snap is not None else snapshot(self.lock)
        prev = self.st.get("prev")
        tok = (cur.get("owner_text") or "").split()[0] if cur.get("owner_text") else None

        if not cur["exists"]:
            self.st["absent_at"] = cur["t"]
            self.st["owner_absent_since"] = None
            # A released lock ends every episode: the NEXT acquire is a new one and must be able to
            # raise the same anomalies again. Keeping the fired-set forever would make this watchdog
            # report each anomaly once in its lifetime and then go quiet — the shape of a guard that
            # looks installed and is not.
            self.st["fired"] = {}
            self.st["prev"] = cur
            self.save()
            return

        # (d) race churn
        if prev and prev.get("exists") and prev.get("dir_mtime") and cur["dir_mtime"] < prev["dir_mtime"] - 0.001:
            self._fire(f"backwards:{cur['dir_mtime']}", "RACE_CHURN", tok,
                       f"dir mtime moved BACKWARDS {ts(prev['dir_mtime'])} -> {ts(cur['dir_mtime'])} "
                       f"(informational; a recreated lock dir, or a clock step)")
        if self.st.get("absent_at") and cur["dir_mtime"] and \
                0 <= cur["dir_mtime"] - self.st["absent_at"] <= CHURN_S:
            self._fire(f"churn:{cur['dir_mtime']}", "RACE_CHURN", tok,
                       f"lock recreated {cur['dir_mtime'] - self.st['absent_at']:.2f}s after it was "
                       f"observed released (dir mtime {ts(cur['dir_mtime'])}); informational")

        # (b) half-released: dir present, no owner
        if not cur["owner_exists"]:
            if self.st.get("owner_absent_since") is None:
                self.st["owner_absent_since"] = cur["t"]
            held = cur["t"] - self.st["owner_absent_since"]
            if held > HALF_RELEASED_S:
                self._fire(f"half:{cur['dir_mtime']}", "HALF_RELEASED", None,
                           f"lock dir present since {ts(cur['dir_mtime'])} with NO owner file for "
                           f"{int(held)}s — nobody holds it and every mkdir will fail")
            self.st["prev"] = cur
            self.save()
            return
        self.st["owner_absent_since"] = None

        # (a) ownership moved in place. ONE episode key for both detectors: after an observed
        # transition the skew is still on disk, so keying them separately reported the same move
        # twice (measured in calibration — 2 events over 3 polls).
        key = f"inplace:{cur['dir_mtime']}:{cur['owner_text']}"
        moved = prev and prev.get("exists") and prev.get("owner_text") and cur["owner_text"] \
            and prev["owner_text"] != cur["owner_text"] \
            and not (self.st.get("absent_at") and self.st["absent_at"] >= prev["t"])
        if moved:
            # NOT keyed on the dir mtime being unchanged. Writing `owner` in place leaves the dir
            # mtime alone (today's 16:19 lock: dir 16:15:44, owner 16:19:08) but a mover that
            # rm's and recreates `owner` DOES bump it, and that variant is the same defect. What
            # actually distinguishes a handover from a release is whether the dir ever went away:
            # a real release rmdir's it, and this branch only runs when we never saw it absent.
            same = prev.get("dir_mtime") and abs(prev["dir_mtime"] - cur["dir_mtime"]) < 0.001
            if same:
                self._fire(key, "OWNER_MOVED_IN_PLACE", tok,
                           f"owner changed WITHOUT the lock dir ever being released: dir mtime "
                           f"unchanged at {ts(cur['dir_mtime'])}, "
                           f"owner {ts(prev.get('owner_mtime'))} -> {ts(cur['owner_mtime'])}; "
                           f"was {prev['owner_text'][:60]!r} now {cur['owner_text'][:60]!r} (observed transition)")
            else:
                # 2026-09-06 20:36:37, and it fired live: EXEC-C released a single-test lock at
                # 20:36:22 and EXEC-E's `sleep 180; if mkdir` succeeded at 20:36:34, both inside one
                # 5 s poll, so the watcher never saw the dir absent and read a legitimate handoff as
                # the breach. The DETAIL already said "owner recreated, not rewritten"; the LABEL did
                # not, and a label is what gets acted on — this one would have justified a false
                # abort. A BUMPED dir mtime is proof a real mkdir happened, which is proof the dir was
                # gone, which is the mutual exclusion holding. That is churn, not a breach.
                # OWNER_MOVED_IN_PLACE is now reserved for a rewrite with the dir mtime UNCHANGED:
                # the case where no mkdir can have occurred. Do NOT close this gap by shortening the
                # poll — a 1 s poll still misses a sub-second handoff and costs CPU on a shared box.
                self.st["fired"][key] = now_local()   # the same episode cannot also fire as (a)
                self._fire(f"handoff:{cur['dir_mtime']}:{cur['owner_text']}", "RACE_CHURN", tok,
                           f"owner changed between two polls and the lock dir was RECREATED (dir mtime "
                           f"bumped {ts(prev.get('dir_mtime'))} -> {ts(cur['dir_mtime'])}, owner "
                           f"{ts(prev.get('owner_mtime'))} -> {ts(cur['owner_mtime'])}): a release and a "
                           f"fresh mkdir inside one {int(cur['t'] - prev['t']) if prev.get('t') else '?'}s "
                           f"poll, so the dir was gone and mutual exclusion HELD. Informational — not an "
                           f"in-place move, do not abort on it; "
                           f"was {prev['owner_text'][:60]!r} now {cur['owner_text'][:60]!r}")
        elif cur["owner_mtime"] and cur["dir_mtime"] and \
                cur["owner_mtime"] - cur["dir_mtime"] > ACQUIRE_SKEW_S:
            self._fire(key, "OWNER_MOVED_IN_PLACE", tok,
                       f"owner written {cur['owner_mtime'] - cur['dir_mtime']:.0f}s AFTER the mkdir "
                       f"(dir {ts(cur['dir_mtime'])}, owner {ts(cur['owner_mtime'])}) — an acquire writes "
                       f"owner immediately, so this lock changed hands in place; cold-start inference, "
                       f"not an observed transition; owner {cur['owner_text'][:60]!r}")

        # (c) long hold
        if cur["owner_mtime"] and cur["t"] - cur["owner_mtime"] > LONG_HOLD_S:
            key = f"long:{cur['owner_text']}"
            if key not in self.st["fired"]:
                _, ctext = self.census()
                self._fire(key, "LONG_HOLD", tok,
                           f"token has held the box for {int((cur['t'] - cur['owner_mtime']) / 60)} min "
                           f"(since {ts(cur['owner_mtime'])}); {ctext}; informational")

        self.st["prev"] = cur
        self.save()


def file_emitter(events=EVENTS, echo=False):
    def emit(kind, sub, token, detail):
        line = "\t".join([now_local(), kind, "LOCK", sub, token or "-", detail])
        if echo:
            print(line, flush=True)  # unbuffered: a redirected watchdog whose log stays empty reads as dead
        try:
            os.makedirs(os.path.dirname(events), exist_ok=True)
            with open(events, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass
    return emit


# --- calibration -------------------------------------------------------------------------------
def selftest():
    """Must-pass: a normal acquire -> hold -> release cycle fires NOTHING. Must-bite: one synthetic
    anomaly of each kind fires EXACTLY once. Both halves are required — a watchdog that fires on
    everything passes the bite half alone, and one that fires on nothing passes the pass half."""
    import shutil
    import tempfile
    root = tempfile.mkdtemp(prefix="lockwatch-cal-")
    lock = os.path.join(root, "box.lock.d")
    fired = []
    ok = True

    def emit(kind, sub, token, detail):
        fired.append((kind, sub, detail))

    def fresh():
        fired.clear()
        sp = os.path.join(root, f"state-{time.time_ns()}.json")
        return LockWatch(emit, lock, sp, census=lambda: (0, "census count=0 (STUBBED)"))

    def acquire(token, skew=0.0, t=None):
        # The dir's mtime is stamped LAST: creating `owner` inside it bumps the directory's own
        # mtime, so stamping the dir first (the first version of this harness) silently reset
        # dir_mtime to real-now and made two of the four bites unreachable — the harness looked
        # like a passing watchdog with no detectors.
        os.mkdir(lock)
        t = t or time.time()
        open(os.path.join(lock, "owner"), "w").write(token)
        os.utime(os.path.join(lock, "owner"), (t + skew, t + skew))
        os.utime(lock, (t, t))

    def release():
        shutil.rmtree(lock, ignore_errors=True)

    try:
        # ---- MUST-PASS: acquire, three quiet polls, release, two more.
        w = fresh()
        acquire("gate-alpha-1-1000")
        for i in range(3):
            w.poll(snapshot(lock, t=time.time() + i))
        release()
        for i in range(2):
            w.poll(snapshot(lock, t=time.time() + 10 + i))
        print(f"MUST-PASS  normal acquire -> 3 polls -> release -> 2 polls: {len(fired)} events {fired}")
        print(f"  -> must-pass {'CLEAN' if not fired else 'DIRTY'}")
        ok &= not fired

        # ---- MUST-BITE (a): owner rewritten, dir mtime untouched.
        w = fresh()
        base = time.time()
        acquire("EXEC-F-tok-1", t=base)
        w.poll(snapshot(lock, t=base))
        op = os.path.join(lock, "owner")
        open(op, "w").write("EXEC-G-tok-2")
        os.utime(op, (base + 204, base + 204))
        os.utime(lock, (base, base))          # the defect: dir mtime did NOT move
        for i in range(3):                    # three polls, must fire exactly once
            w.poll(snapshot(lock, t=base + 205 + i))
        a = [f for f in fired if f[1] == "OWNER_MOVED_IN_PLACE"]
        print(f"MUST-BITE  [a OWNER_MOVED_IN_PLACE] {len(a)} fired over 3 polls")
        for f in a:
            print(f"  BIT: {f[2][:150]}")
        ok &= len(a) == 1
        release()

        # ---- MUST-BITE (a'): cold start on a lock already moved in place (today's 16:19 shape).
        w = fresh()
        base = time.time() - 300
        acquire("EXEC-G-coldstart", skew=204.0, t=base)
        for i in range(3):
            w.poll(snapshot(lock, t=time.time() + i))
        a2 = [f for f in fired if f[1] == "OWNER_MOVED_IN_PLACE"]
        print(f"MUST-BITE  [a' cold-start inference] {len(a2)} fired over 3 polls")
        for f in a2:
            print(f"  BIT: {f[2][:150]}")
        ok &= len(a2) == 1
        release()

        # ---- MUST-NOT-BITE (a''): the live 20:36:37 false positive, with its real timestamps and
        # owner tokens. EXEC-C held from 20:36:22, released; EXEC-E's mkdir succeeded at 20:36:34;
        # the watcher polled at :22 and :37 and never saw the dir absent. Ground truth measured at
        # 20:37:12: EXEC-C pid 18495 dead, census count 1 (EXEC-E's pytest only), EXEC-C's later
        # attempts print HELD. Must produce RACE_CHURN and NOT OWNER_MOVED_IN_PLACE.
        w = fresh()
        base = time.time()
        acquire("EXEC-C-A010RLS-X2-1788664200 host=box pid=18495", t=base)        # 20:36:22
        w.poll(snapshot(lock, t=base))
        release()                                                                 # 20:36:2x-:34
        acquire("exec-e-Q7r2proof-1788664594 host=box pid=17406", t=base + 12)     # 20:36:34, dir RECREATED
        for i in range(3):                       # polls at 20:36:37 and after; absence never seen
            w.poll(snapshot(lock, t=base + 15 + i))
        a3 = [f for f in fired if f[1] == "OWNER_MOVED_IN_PLACE"]
        ch = [f for f in fired if f[1] == "RACE_CHURN"]
        print(f"MUST-NOT-BITE [a'' live 20:36:37 handoff] OWNER_MOVED_IN_PLACE={len(a3)} (want 0), "
              f"RACE_CHURN={len(ch)} (want 1)")
        for f in ch:
            print(f"  BIT: {f[2][:170]}")
        for f in a3:
            print(f"  WRONGLY BIT: {f[2][:170]}")
        ok &= len(a3) == 0 and len(ch) == 1
        release()

        # ---- MUST-BITE (b): dir with no owner, > 60 s.
        w = fresh()
        base = time.time()
        os.mkdir(lock)
        os.utime(lock, (base, base))
        w.poll(snapshot(lock, t=base))
        w.poll(snapshot(lock, t=base + 30))    # under the threshold: must stay quiet
        under = len(fired)
        for i in range(3):
            w.poll(snapshot(lock, t=base + 61 + i))
        b = [f for f in fired if f[1] == "HALF_RELEASED"]
        print(f"MUST-BITE  [b HALF_RELEASED] {under} at 30s (want 0), {len(b)} fired over 3 polls past 60s")
        for f in b:
            print(f"  BIT: {f[2][:150]}")
        ok &= len(b) == 1 and under == 0
        release()

        # ---- MUST-BITE (c): one token held > 45 min.
        w = fresh()
        base = time.time() - 46 * 60
        acquire("EXEC-H-longhold", t=base)
        for i in range(3):
            w.poll(snapshot(lock, t=time.time() + i))
        c = [f for f in fired if f[1] == "LONG_HOLD"]
        print(f"MUST-BITE  [c LONG_HOLD] {len(c)} fired over 3 polls")
        for f in c:
            print(f"  BIT: {f[2][:150]}")
        ok &= len(c) == 1
        release()

        # ---- MUST-BITE (d): dir recreated within 2 s of an observed release.
        w = fresh()
        base = time.time()
        acquire("EXEC-I-churn", t=base)
        w.poll(snapshot(lock, t=base))
        release()
        w.poll(snapshot(lock, t=base + 1))     # observed absent
        acquire("EXEC-J-churn", t=base + 2)    # recreated 1 s later
        for i in range(3):
            w.poll(snapshot(lock, t=base + 3 + i))
        d = [f for f in fired if f[1] == "RACE_CHURN"]
        print(f"MUST-BITE  [d RACE_CHURN] {len(d)} fired over 3 polls")
        for f in d:
            print(f"  BIT: {f[2][:150]}")
        ok &= len(d) == 1
        release()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("\nSELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    a = sys.argv[1:]
    if "--selftest" in a:
        return selftest()
    if "--status" in a:
        s = snapshot()
        print(json.dumps({**s, "dir_mtime_h": ts(s["dir_mtime"]), "owner_mtime_h": ts(s["owner_mtime"]),
                          "skew_s": (s["owner_mtime"] - s["dir_mtime"]) if s["owner_mtime"] and s["dir_mtime"] else None},
                         indent=1))
        print("stored state:", json.dumps(json.load(open(STATE)), indent=1) if os.path.exists(STATE) else "NONE")
        return 0
    if "--once" in a:
        LockWatch(file_emitter(echo=True)).poll()
        return 0
    if "--watch" in a:
        iv = float(a[a.index("--interval") + 1]) if "--interval" in a else 5.0
        w = LockWatch(file_emitter(echo=True))
        os.makedirs(D, exist_ok=True)
        open(PIDFILE, "w").write(str(os.getpid()))
        import atexit
        atexit.register(lambda: os.path.exists(PIDFILE) and os.unlink(PIDFILE))
        print(f"lockwatch: polling {LOCK} every {iv}s -> {EVENTS} (detection only, pid {os.getpid()})", flush=True)
        while True:
            try:
                w.poll()
            except Exception as e:  # noqa: BLE001 — a watchdog must outlive its own bugs
                print(f"{now_local()} lockwatch poll error: {type(e).__name__}: {e}", flush=True)
            time.sleep(iv)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
