"""One run waited 102 minutes behind a lock no instrument knew existed.

★, `docs/ops/2026-09-09/PGSERVER-GLOBAL-LOCK.md`: `pgserver` serialises EVERY postgres start and
stop on this machine through one lockfile, held with no timeout, at a fixed per-user path. It is a
CLASS attribute, so every `PostgresServer` in every process shares it — across worktrees, checkouts,
xdist workers and unrelated projects — and the pgdata path never enters the lock identity. A run
queued behind it shows near-zero CPU, no children, and a perfectly healthy project lock: the board
calls it busy. `board-reports-what-it-sent`, third lock edition.

WHAT THIS FILE HOLDS THE PROBE TO, and the first two are why the obvious implementation is absent:

  * IT MUST NEVER ACQUIRE THE LOCK. A non-blocking `flock`/`lockf` attempt takes it when it is
    free, and this daemon must not become the thing that serialises a test run. `F_GETLK` tests for
    a conflicting lock and never takes one.
  * `lsof` DOES NOT WORK HERE, measured rather than assumed: against a process genuinely holding
    the lock, lsof's lock field reports `l ` (no lock) for BOTH `flock` and POSIX `lockf` on this
    macOS. It lists who has the file OPEN, conflating the holder with everyone queued behind it —
    the one distinction that matters, reported backwards.
  * UNMEASURED IS NEVER RENDERED AS FREE. A missing lockfile, an unreadable one, or a refused query
    are all "we do not know", and that must not read as "nothing is waiting".
  * The run that HOLDS the lock is not stalled. It is what everyone else is waiting for — a
    different row and a different action.
  * STALLED IS A CORRELATION AND SAYS SO. Two facts are measured; causation is not claimed.

The holder cases below use a REAL POSIX lock taken by a REAL subprocess against a temp file — not a
fixture dict. The mechanism is the thing under test, and a stubbed lock would prove the parser and
not the probe. That is the trap this detector already fell into once.
"""
import importlib.util, os, subprocess, sys, tempfile, textwrap, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("pglock_t", os.path.join(HERE, os.pardir, "pglock.py"))
PG = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(PG)

P, FAILED = 0, []
def ok(cond, what):
    global P
    if cond: P += 1; print(f"PASS {what}")
    else: FAILED.append(what); print(f"FAIL {what}")

TMP = tempfile.mkdtemp(prefix="pglock-")
LF = os.path.join(TMP, ".lockfile")
open(LF, "w").close()

HOLDER = textwrap.dedent("""
    import fcntl, sys, time
    fd = open(sys.argv[1], "a")
    fcntl.lockf(fd, fcntl.LOCK_EX)
    sys.stdout.write("locked\\n"); sys.stdout.flush()
    time.sleep(float(sys.argv[2]))
""")

# ---- FREE, and the file exists ------------------------------------------------------------------
p = PG.probe(LF)
ok(p["state"] == PG.FREE and p["pid"] is None,
   "MUST BITE: an unheld lockfile reports FREE with no holder")

# ---- HELD, against a REAL lock taken by a REAL process -------------------------------------------
proc = subprocess.Popen([sys.executable, "-c", HOLDER, LF, "8"], stdout=subprocess.PIPE, text=True)
ok(proc.stdout.readline().strip() == "locked", "  CONTROL: the holder subprocess really took the lock")
p = PG.probe(LF)
ok(p["state"] == PG.HELD and p["pid"] == proc.pid,
   f"MUST BITE: F_GETLK names the ACTUAL holder pid — {p['pid']} vs the real {proc.pid}. This is "
   f"the distinction lsof cannot make on this machine")

# ---- and it did NOT take the lock: the holder still has it, and a second probe agrees ------------
p2 = PG.probe(LF)
ok(p2["state"] == PG.HELD and p2["pid"] == proc.pid,
   "MUST BITE: probing twice does not disturb the lock — F_GETLK tests, it never acquires")
still = subprocess.run([sys.executable, "-c",
                        "import fcntl,sys\n"
                        "fd=open(sys.argv[1],'a')\n"
                        "try:\n"
                        "    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); print('TOOK IT')\n"
                        "except OSError: print('still held')\n", LF],
                       capture_output=True, text=True)
ok(still.stdout.strip() == "still held",
   "MUST BITE: an INDEPENDENT process still cannot take the lock after our probes — proof the probe "
   "left the holder in place rather than stealing and returning it")

# ---- the verdict, on a real held lock ------------------------------------------------------------
st, why = PG.verdict(999999, p, 10.0, 10.0)
ok(st == PG.STALLED and "correlation" in why,
   "MUST BITE: lock held by someone else + no CPU delta = STALLED, and the text says it is a "
   "correlation rather than claiming causation")
ok(PG.verdict(proc.pid, p, 10.0, 10.0)[0] == PG.LIVE,
   "MUST BITE: the run that HOLDS the lock is never STALLED — it is what the others are waiting "
   "for, which is a different row and a different action")
ok(PG.verdict(999999, p, 20.0, 10.0)[0] == PG.LIVE,
   "MUST BITE: a run burning real CPU while the lock is held is WORKING, not waiting — near-zero "
   "CPU is half the evidence and the half that stops this firing on every busy machine")
ok(PG.verdict(999999, p, 10.0, None)[0] == PG.UNMEASURED,
   "MUST BITE: one sample is never a stall — a delta needs two consecutive ticks, and the first "
   "tick after a restart must not accuse a run that has only just started")

proc.wait()
ok(PG.probe(LF)["state"] == PG.FREE,
   "  CONTROL: the moment the holder exits the lock reads FREE — the probe tracks reality and is "
   "not reporting a cached or sticky value")

# ---- UNMEASURED is never FREE --------------------------------------------------------------------
gone = PG.probe(os.path.join(TMP, "no-such-lockfile"))
ok(gone["state"] == PG.UNMEASURED and "NOT the same as a free lock" in gone["why"],
   "MUST BITE: a missing lockfile is UNMEASURED, not FREE. If platformdirs ever moves the path, a "
   "silent FREE would tell everyone the machine is unblocked while it is not")
ok(PG.verdict(1, gone, 10.0, 10.0)[0] == PG.UNMEASURED,
   "  and an unmeasured lock yields an unmeasured verdict rather than a confident LIVE")
ok(PG.verdict(1, None, 10.0, 10.0)[0] == PG.UNMEASURED, "  a missing probe likewise")

# ---- cpu_seconds, on the DEFAULT runner against real processes ------------------------------------
ok(PG.cpu_seconds(os.getpid()) is not None and PG.cpu_seconds(os.getpid()) >= 0,
   "LIVE SMOKE: cpu_seconds runs its DEFAULT `ps` against this very process and returns a real "
   "number — the injected-probe version of this check is how a detector ships inert")
ok(PG.cpu_seconds(999999) is None,
   "  and a dead pid is None (NOT MEASURED), never 0.0 — 0.0 would read as 'burned no CPU', which "
   "is exactly the stall signature, and a dead run would be reported as stalled forever")
ok(PG.cpu_seconds(1, runner=lambda pid: "1-02:03:04") == 86400 + 3600 * 2 + 60 * 3 + 4,
   "  the [dd-]hh:mm:ss form parses, including the day field a long-running holder will have")
ok(PG.cpu_seconds(1, runner=lambda pid: "12:34.56") == 12 * 60 + 34.56,
   "  and the mm:ss.ff form ps actually prints for short-lived runs")
ok(PG.cpu_seconds(1, runner=lambda pid: "nonsense") is None
   and PG.cpu_seconds(1, runner=lambda pid: "") is None,
   "  unparseable output is None rather than a wrong number")

# ---- the real path is the one the writeup resolved -----------------------------------------------
ok(PG.LOCK_PATH.endswith("python_PostgresServer/.lockfile"),
   f"  the module points at pgserver's own per-user path: {PG.LOCK_PATH}")
live = PG.probe()
ok(live["state"] in (PG.HELD, PG.FREE, PG.UNMEASURED) and live["path"] == PG.LOCK_PATH,
   f"LIVE SMOKE: the real lock on this machine probes cleanly -> {live['state']}")

print(f"\n{P} passed, {len(FAILED)} failed")
for f in FAILED: print("  FAILED:", f)
import shutil; shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
