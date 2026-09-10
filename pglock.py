"""The third lock nobody counted: pgserver's global postgres start/stop mutex. Read it, never take it.

★'s writeup, `docs/ops/2026-09-09/PGSERVER-GLOBAL-LOCK.md`: `pgserver` serialises EVERY PostgreSQL
start and stop on this machine through ONE lockfile, held with no timeout. It is a CLASS attribute
at a fixed per-user path, so every `PostgresServer` in every process shares it — across worktrees,
checkouts, xdist workers and unrelated projects. **The pgdata path never enters the lock identity.**
It cost one run 102 minutes of invisible waiting: near-zero CPU, no children, indistinguishable from
slow.

WHY THE BOARD NEEDS THIS. `box.lock.d` and `portal.lock.d` do not know this lock exists, so the
board can show a run "holding the box" that is in fact stalled behind a process holding no project
lock at all — `board-reports-what-it-sent`, third lock edition. See also `two-locks-one-cpu`: a mutex
is not a scheduler.

READ-ONLY BY CONSTRUCTION, AND THAT CONSTRAINT CHOSE THE MECHANISM.
  * `lsof` was the obvious probe and IT DOES NOT WORK HERE. Measured 2026-09-10 against a process
    genuinely holding the lock: lsof's lock field reports `l ` (no lock) for BOTH `flock` and POSIX
    `lockf` on this macOS. It lists who has the file OPEN, which conflates the holder with everyone
    queued behind it — precisely the distinction that matters, reported backwards.
  * A non-blocking `flock`/`lockf` attempt would ACQUIRE the lock when it is free. That is taking
    the lock, and it is forbidden: this daemon must never be the thing that serialises a test run.
  * `fcntl(F_GETLK)` TESTS for a conflicting lock and NEVER acquires one — and it returns the
    holder's pid. Verified 2026-09-10 both ways: HELD by the exact pid while a holder ran, FREE the
    moment it exited.

F_GETLK sees POSIX record locks, which is what `fasteners.InterProcessLock` uses on posix — the same
family pgserver takes. It would not see a `flock`-style lock, and that limit is stated rather than
hidden: if a future pgserver switched families this would report FREE while the lock was held, so
the finding says which mechanism it measured.

NOTHING HERE ACTS. It reports.
"""
from __future__ import annotations

import fcntl
import os
import struct
import subprocess

#: Resolved on this box 2026-09-10 and confirmed present. The authoritative expression is
#: `platformdirs.user_runtime_path('python_PostgresServer') / '.lockfile'` — but platformdirs is
#: NOT importable by /usr/bin/python3, which is the interpreter the daemon runs under, so the path
#: is written out rather than derived. If platformdirs ever changes its macOS runtime dir this goes
#: stale, which is why `probe()` reports the path it looked at and says when the file is absent
#: instead of reporting a free lock.
LOCK_PATH = os.path.expanduser(
    "~/Library/Caches/TemporaryItems/python_PostgresServer/.lockfile")

# macOS `struct flock`: off_t l_start; off_t l_len; pid_t l_pid; short l_type; short l_whence;
_FLOCK_FMT = "qqihh"
_F_GETLK, _F_WRLCK, _F_UNLCK, _SEEK_SET = 7, 3, 2, 0

HELD, FREE, UNMEASURED = "HELD", "FREE", "UNMEASURED"


def probe(path=None):
    """Who holds the pgserver lock right now. -> dict. NEVER acquires it.

    {"path", "state": HELD|FREE|UNMEASURED, "pid": int|None, "why": str}

    `UNMEASURED` is its own state and is never rendered as FREE. A missing lockfile, an unreadable
    one, or a kernel that declines the query are all "we do not know", and "we do not know" must not
    read as "nothing is waiting" — that is the absence-as-a-fact shape this whole detector exists
    for.
    """
    path = path or LOCK_PATH
    if not os.path.exists(path):
        return {"path": path, "state": UNMEASURED, "pid": None,
                "why": ("no lockfile at this path — either pgserver has never run for this user or "
                        "platformdirs resolves it elsewhere now. NOT the same as a free lock")}
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        return {"path": path, "state": UNMEASURED, "pid": None,
                "why": f"cannot open the lockfile to test it: {type(e).__name__}: {e}"}
    try:
        packed = struct.pack(_FLOCK_FMT, 0, 0, 0, _F_WRLCK, _SEEK_SET)
        res = fcntl.fcntl(fd, _F_GETLK, packed)
        _start, _len, pid, ltype, _whence = struct.unpack(_FLOCK_FMT, res)
    except OSError as e:
        return {"path": path, "state": UNMEASURED, "pid": None,
                "why": f"F_GETLK refused: {type(e).__name__}: {e}"}
    finally:
        os.close(fd)
    if ltype == _F_UNLCK:
        return {"path": path, "state": FREE, "pid": None,
                "why": "F_GETLK reports no conflicting POSIX lock"}
    return {"path": path, "state": HELD, "pid": pid or None,
            "why": (f"F_GETLK reports a POSIX write lock held by pid {pid or 'unknown'} — every "
                    f"postgres start and stop on this machine queues behind it, with no timeout")}


def cpu_seconds(pid, *, runner=None):
    """Cumulative CPU time for a pid, in seconds. -> float | None (NOT MEASURED).

    The stall signature is near-zero CPU while apparently busy, so this is the second half of the
    evidence. `ps -o time=` gives [dd-]hh:mm:ss.
    """
    run = runner or _ps_time
    try:
        raw = run(pid)
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    txt = raw.strip().split()[0]
    days = 0
    if "-" in txt:
        d, txt = txt.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            return None
    parts = txt.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    return secs + days * 86400


def _ps_time(pid):
    p = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "time="],
                       capture_output=True, text=True, timeout=5)
    return p.stdout if p.returncode == 0 else ""


LIVE, STALLED = "LIVE", "STALLED"


def verdict(run_pid, lock, cpu_now, cpu_prev, *, idle_delta_s=0.5):
    """Is this run working, or queued behind the global postgres lock? -> (state, why).

    THREE STATES, and the third is the point: LIVE / STALLED / UNMEASURED. A run that is stalled
    here looks exactly like a slow one — near-zero CPU, no children, a held project lock — and the
    board currently calls it busy.

    THIS IS A CORRELATION AND IT SAYS SO. Two facts are measured: the global lock is held by a
    process that is NOT this run, and this run burned effectively no CPU between two consecutive
    samples. That is the signature of waiting on it; it is not proof of causation, and the text
    never claims otherwise. A run can also be idle for its own reasons.

    A run that IS the holder is never STALLED — it is the thing everyone else is waiting for, which
    is a different row and a different action.
    """
    if lock is None or lock.get("state") == UNMEASURED:
        return UNMEASURED, f"the pgserver lock could not be read: {(lock or {}).get('why', 'no probe')}"
    if cpu_now is None or cpu_prev is None:
        return UNMEASURED, ("no CPU delta yet — a stall needs two consecutive samples, and one "
                            "sample cannot tell working from waiting")
    delta = cpu_now - cpu_prev
    if lock["state"] != HELD:
        return LIVE, f"pgserver lock free; {delta:.1f}s CPU since the last tick"
    if lock.get("pid") and int(lock["pid"]) == int(run_pid):
        return LIVE, (f"this run HOLDS the pgserver lock (pid {run_pid}) — everything else on the "
                      f"machine that starts or stops postgres is queued behind it")
    if delta > idle_delta_s:
        return LIVE, (f"pgserver lock held by pid {lock.get('pid')}, but this run burned "
                      f"{delta:.1f}s CPU since the last tick — it is working, not waiting")
    return STALLED, (f"pgserver lock HELD by pid {lock.get('pid')} and this run burned {delta:.1f}s "
                     f"CPU since the last tick. That is the signature of waiting on the global "
                     f"postgres mutex — 102 minutes of it on 2026-09-10 — but it is a correlation, "
                     f"not proof: a run idle for its own reasons looks the same.")
