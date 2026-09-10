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
import json
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


# --------------------------------------------------------------------------- the population
# ITEM 4 (BOSS, 2026-09-10 20:07). `verdict()` was correct and scored the wrong set. The tick's loop
# ran over `state["codex"]` — THE DAEMON'S OWN RUNS — so a box run this daemon did not spawn could
# never be STALLED. The 102-minute wait of 2026-09-10 was WORKER-1's run; it would have been
# invisible to the instrument built to see it. A guard defined over one file instead of its
# population, one layer up.
#
# THE SUBJECT IS THE WAITER, NOT THE POSTMASTER. Measured on this box 2026-09-10 20:01: the four
# live postmasters (99529/99549/99571/99605) all have PPID 1 — reparented to launchd — so they lead
# nowhere upward, and a live postmaster proves the start ALREADY SUCCEEDED. The process that queues
# on the global mutex is the python inside `PostgresServer.__enter__` / `.cleanup()`. Postmasters
# are corroboration; they are never the population.

#: Primary source. Each run writes one of these beside its pgdata. Measured shape 2026-09-10:
#: {"owner_pid": 99359, "owner_started_at": 1789049991.749204, "pgdata": ..., "root": ...,
#:  "suite_root": ".../voicepod-lane-004-pgserver-lock/platform", "version": 1}
MARKER_NAME = ".voicepod-test-postgres-owner.json"
MARKER_GLOB = "voicepod-test-pg-session-*"

#: Secondary source: who holds the project's box lock, for ATTRIBUTION only. Measured shape:
#:   owner=lane-004-pgserver-lock / pid=99291 / what=full platform suite -n 4 on b40ac02c
#: Read only. This daemon never writes, removes or releases the box lock — standing rule.
BOX_OWNER_PATH = os.path.expanduser(
    "~/Claude Code/Calling New/test-logs/box.lock.d/owner")

NO_RUNS = "NO_RUNS"


def marker_root(root=None):
    """Where the per-run markers live. Reported, never assumed — same discipline as LOCK_PATH."""
    import tempfile
    return root or tempfile.gettempdir()


def _ps_lstart(pid):
    p = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                       capture_output=True, text=True, timeout=5)
    return p.stdout if p.returncode == 0 else ""


def start_time(pid, *, runner=None):
    """Process start time as a unix float. -> float | None (the pid is gone, or ps declined).

    This is the pid-reuse discriminator. `owner_started_at` in the marker is the OWNER PROCESS's
    creation time, not the moment the file was written — verified 2026-09-10: marker
    1789049991.749204 against `ps -o lstart=` "Thu Sep 10 19:49:51 2026" = 1789049991.0, equal to
    the second. So the pair (pid, start_time) identifies the process, and a recycled pid does not
    inherit a dead run's row.
    """
    import time as _time
    run = runner or _ps_lstart
    try:
        raw = run(pid)
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    try:
        return _time.mktime(_time.strptime(raw.strip(), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def box_owner(path=None):
    """Who holds the project box lock. -> dict | None. Read-only, and absence is not an error.

    The box lock does not know the pgserver mutex exists (that is the whole finding), so this is
    used for ATTRIBUTION — putting a name on a row — never to decide whether a run exists.
    """
    path = path or BOX_OWNER_PATH
    try:
        with open(path) as fh:
            raw = fh.read()
    except OSError:
        return None
    out = {"path": path}
    for line in raw.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out if len(out) > 1 else None


def read_markers(root=None):
    """Every per-run marker on disk, unfiltered. -> (list[dict], notes: list[str]).

    Unfiltered ON PURPOSE: staleness is decided in `population()` where it can be counted and said
    out loud. A reader that silently drops rows cannot tell you it dropped any.
    """
    import glob as _glob
    root = marker_root(root)
    if not os.path.isdir(root):
        return None, [f"marker root is not a readable directory: {root}"]
    found, notes = [], []
    for d in sorted(_glob.glob(os.path.join(root, MARKER_GLOB))):
        p = os.path.join(d, MARKER_NAME)
        if not os.path.exists(p):
            notes.append(f"{os.path.basename(d)}: session dir with no marker — not counted")
            continue
        try:
            with open(p) as fh:
                m = json.load(fh)
        except (OSError, ValueError) as e:
            notes.append(f"{os.path.basename(d)}: unreadable marker ({type(e).__name__}) — "
                         f"NOT a free slot, just one we could not read")
            continue
        m["_marker"] = p
        found.append(m)
    return found, notes


def postmasters(*, runner=None):
    """pgdata -> postmaster pid, for CORROBORATION only. -> dict.

    A run with its postmasters up has already got through the mutex. A run holding the box with NO
    postmaster of its own and no CPU is the 102-minute shape. This never contributes a row.
    """
    run = runner or _ps_all
    out = {}
    try:
        raw = run()
    except Exception:
        return out
    for line in (raw or "").splitlines():
        if "/pgserver/" not in line or "-D " not in line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        tail = parts[1].split("-D ", 1)[1]
        out.setdefault(tail.split(" -", 1)[0].strip(), int(parts[0]))
    return out


def _ps_all():
    p = subprocess.run(["/bin/ps", "-eo", "pid=,args="],
                       capture_output=True, text=True, timeout=10)
    return p.stdout if p.returncode == 0 else ""


def population(*, root=None, box_path=None, lstart=None, ps_all=None, tolerance_s=2.0):
    """The runs on this box that could be queued on the pgserver mutex. -> (runs, notes, state).

    `state` is NO_RUNS, UNMEASURED, or None when there are rows.

    G1 A STALE MARKER IS NOT A RUN. A marker whose owner_pid is dead is debris, and it must produce
       NO ROW — not LIVE, not STALLED, not an UNMEASURED row. Live example on this box while this
       was written: `voicepod-test-pg-session-master-npl4h1nh`, owner_pid 41101, dead, suite_root
       under another session's `wt-premerge`. Without this guard the very first thing this change
       does is invent a permanently stalled run out of a leftover file.
    G2 PID REUSE. (pid, start_time) identifies the process; the marker's word about a pid it no
       longer owns is not evidence.
    G4 ABSENCE NEVER RENDERS AS CLEAN — but it does not render as UNMEASURED either when we could
       genuinely look. An unreadable marker root is UNMEASURED ("we could not see"). A readable root
       with no live runs is NO_RUNS ("we looked, there is nothing"), which is a FACT and is rendered
       as its own line. Calling an idle box UNMEASURED would be crying wolf on every quiet tick and
       would teach the reader to skip the row — the failure this whole detector exists to avoid,
       arrived from the other side.
    G8 EVERY ROW CARRIES WHOSE RUN IT IS. `suite_root` always; the box owner's `owner=` name when it
       matches, so a STALLED row has somebody to address.
    """
    markers, notes = read_markers(root)
    if markers is None:
        return [], notes, UNMEASURED
    owner = box_owner(box_path)
    pm = postmasters(runner=ps_all)
    runs, stale = [], 0
    for m in markers:
        pid = m.get("owner_pid")
        base = os.path.basename(m.get("root") or os.path.dirname(m.get("_marker", "")))
        if not pid:
            notes.append(f"{base}: marker names no owner_pid — not counted")
            continue
        st = start_time(pid, runner=lstart)
        if st is None:                                                        # G1
            stale += 1
            notes.append(f"{base}: owner_pid {pid} is GONE — stale marker, no row (debris, not a "
                         f"run; this daemon does not remove it)")
            continue
        claimed = m.get("owner_started_at")
        if claimed is not None and abs(float(claimed) - st) > tolerance_s:    # G2
            stale += 1
            notes.append(f"{base}: pid {pid} is alive but started {st:.0f}, marker claims "
                         f"{float(claimed):.0f} — PID REUSE, no row")
            continue
        suite = m.get("suite_root") or "(no suite_root in marker)"
        label = None
        if owner and owner.get("owner") and owner["owner"] in suite:          # G8
            label = owner["owner"]
        runs.append({
            "pid": int(pid), "started_at": st, "suite_root": suite,
            "pgdata": m.get("pgdata"), "session": base,
            "owner": label, "source": "marker",
            "postmaster": pm.get(m.get("pgdata")),                            # G5, corroboration
        })
    if stale:
        notes.append(f"{stale} stale marker(s) skipped — a marker on disk is a claim, not a run")
    if not runs:
        return [], notes, NO_RUNS
    return runs, notes, None
