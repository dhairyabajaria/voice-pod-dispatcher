"""ITEM 4: `verdict()` was right and scored the wrong set.

BOSS, 2026-09-10 20:07. The tick's pgserver loop ran over `self.state["codex"]` — THE DAEMON'S OWN
SPAWNS. A box run this daemon did not start could therefore never be STALLED, and the 102-minute
wait the whole probe was built for was WORKER-1's run. **The instrument could not see its own
founding incident.** A guard defined over one file instead of over its population, one layer up.

THE SUBJECT IS THE WAITER, NOT THE POSTMASTER. Measured 2026-09-10 20:01: every live postmaster on
this box has PPID 1 (reparented to launchd), and a live postmaster proves the start ALREADY
SUCCEEDED. The process that queues on the global mutex is the python inside
`PostgresServer.__enter__`/`.cleanup()`. Postmasters corroborate; they never contribute a row.

EVERY CHECK HERE DRIVES `tick()`. Asserting on `pglock.verdict()` directly would prove the verdict
function, which was never at risk — the defect IS the call site's `for slot, run in
state["codex"].items()`. So the daemon is driven with a STOP file present (redirected into this
test's own temp dir — a test never touches a live control file), which runs `poll_sessions()` and
returns. The rows are read out of `state["sessions"]`, where the board reads them.

Hermetic: fake marker dirs under a temp root, injected `ps` readers, injected lock probe. No live
control file, no real process, no kill, nothing on the box is touched.
"""
import importlib.util, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, os.pardir))
import pglock                                                   # noqa: E402

# CAPTURED ONCE, BEFORE ANY PATCH. `DP.pglock` IS this same module object, so patching
# `DP.pglock.population` rebinds it globally — and a per-case `real = pglock.population` captures
# the PREVIOUS case's lambda, which then calls itself. First red of this file: every check after
# PC1 died on `TypeError: <lambda>() got an unexpected keyword argument 'root'`, and the negative
# control PASSED VACUOUSLY inside the wreck, for a reason that had nothing to do with the claim in
# its name. The instrument, not the product — again.
REAL_POPULATION = pglock.population
REAL_BOX_OWNER = pglock.box_owner

P, F = 0, []
def ok(c, w):
    global P
    if c: P += 1; print(f"PASS {w}")
    else: F.append(w); print(f"FAIL {w}")

TMP = tempfile.mkdtemp(prefix="pgpop-")
MARKERS = os.path.join(TMP, "markers"); os.makedirs(MARKERS)
BOXDIR = os.path.join(TMP, "box.lock.d"); os.makedirs(BOXDIR)
BOX = os.path.join(BOXDIR, "owner")
open(BOX, "w").write("owner=lane-004-pgserver-lock\npid=99291\n"
                     "what=full platform suite -n 4 on b40ac02c\n")

T0 = 1789049991.0
LSTART_FMT = "%a %b %d %H:%M:%S %Y"


def marker(name, pid, started, suite="/x/voicepod-lane-004-pgserver-lock/platform"):
    d = os.path.join(MARKERS, f"voicepod-test-pg-session-{name}")
    os.makedirs(d, exist_ok=True)
    json.dump({"owner_pid": pid, "owner_started_at": started,
               "pgdata": os.path.join(d, "pgdata"), "root": d,
               "suite_root": suite, "version": 1},
              open(os.path.join(d, pglock.MARKER_NAME), "w"))
    return d


def lstarts(table):
    """Injected `ps -p <pid> -o lstart=`. An absent pid returns "" — the pid is gone."""
    def run(pid):
        t = table.get(int(pid))
        return "" if t is None else time.strftime(LSTART_FMT, time.localtime(t))
    return run


def daemon(*, lock, cpu, alive, codex=None, pop=None):
    """A daemon whose tick() runs poll_sessions() and then stops on its OWN STOP file."""
    spec = importlib.util.spec_from_file_location(
        "dsp" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
    DP = importlib.util.module_from_spec(spec); spec.loader.exec_module(DP)
    st = os.path.join(TMP, "state" + str(time.time_ns())); os.makedirs(st)
    DP.STATE_DIR = st
    DP.HEARTBEAT = os.path.join(st, "heartbeat")
    DP.QUEUE = os.path.join(st, "queue.json")
    DP.STOP = os.path.join(st, "STOP"); open(DP.STOP, "w").write("stop")
    DP.OBSERVE = os.path.join(st, "OBSERVE")
    DP.PENDING = os.path.join(st, "pending.json")
    DP.EVENTS = os.path.join(st, "events.log")

    # The real population(), with only its ENVIRONMENT injected — the guards under test are its
    # own. `pop` overrides it entirely, and is used for exactly one thing: the negative control.
    pglock.population = REAL_POPULATION          # undo the previous case, always
    if pop is None:
        DP.pglock.population = lambda: REAL_POPULATION(root=MARKERS, box_path=BOX,
                                                       lstart=lstarts(alive), ps_all=lambda: "")
    else:
        DP.pglock.population = pop
    DP.pglock.probe = lambda path=None: lock
    DP.pglock.cpu_seconds = lambda pid, **kw: cpu.get(int(pid))
    DP.pglock.start_time = lambda pid, **kw: alive.get(int(pid))
    # HONOURS ITS ARGUMENT. A stub that swallowed `root` and always returned MARKERS made PC6 —
    # the "we could not look" case — silently read a directory that DOES exist, so UNMEASURED came
    # back as NO_RUNS. The stub had quietly changed the subject under test.
    DP.pglock.marker_root = lambda root=None: root or MARKERS
    DP.pglock.box_owner = lambda path=None: REAL_BOX_OWNER(BOX)

    DP.sessionwatch.census = lambda **kw: []
    DP.sessionwatch.summarise = lambda rows, **kw: {"sockets": 0, "live": 0, "quiet": 0}
    DP.sessionwatch.queue_shape = lambda items, **kw: {}
    DP.sessionwatch.board_lines = lambda *a, **kw: []
    DP.sessionwatch.finding = lambda *a, **kw: None

    dp = DP.Dispatcher.__new__(DP.Dispatcher)
    dp.dry = False; dp.once = False; dp.observing = False; dp.observe_logged = False
    dp.paused_logged = False; dp.paused_since = None
    dp.cfg = {}
    dp.state = {"pending": {}, "codex": codex or {}}
    dp.reload_cfg = lambda: None
    dp.write_heartbeat = lambda *a, **k: None
    dp.write_pending = lambda *a, **k: None
    dp.load_queue = lambda: {"items": []}
    dp.log = lambda *a, **k: None
    dp.emit = lambda *a: events.append(a)
    dp.save_state = lambda: None
    dp.provider_error_summary = lambda: {}
    return DP, dp


def two_ticks(dp):
    """A stall needs two consecutive samples; one sample cannot tell working from waiting."""
    dp.tick(); dp.tick()
    return dp.state.get("sessions", {})


HELD_BY_FOREIGN = {"path": "/l", "state": pglock.HELD, "pid": 7777, "why": "held by 7777"}
FREE = {"path": "/l", "state": pglock.FREE, "pid": None, "why": "free"}

# ------------------------------------------------------------------- PC1  the founding incident
events = []
marker("gw0", 5001, T0)
DP, dp = daemon(lock=HELD_BY_FOREIGN, cpu={5001: 12.0}, alive={5001: T0})
s = two_ticks(dp)
rows = s.get("runs", [])
r = rows[0] if rows else {}
ok(len(rows) == 1 and r.get("state") == pglock.STALLED,
   "PC1 MUST BITE: a FOREIGN run — one this daemon never spawned — with the global lock held by "
   "somebody else and no CPU burned between two ticks scores STALLED. This is the 102-minute wait "
   "of 2026-09-10, which the old population could not represent at all")
ok(r.get("slot") == "lane-004-pgserver-lock" and "lane-004" in (r.get("item") or ""),
   "G8: and the row NAMES whose run it is, from the box owner and the marker's suite_root — a "
   "STALLED row addressed to nobody is a verdict without an owner")
ok(any("STALLED" in l and "lane-004-pgserver-lock" in l for l in s.get("board", [])),
   "and it reaches the BOARD, which is where a human would ever see it")

# ---------------------------------------------------- the NEGATIVE CONTROL for the whole change
# The identical tick, with the population the code had BEFORE item 4: the daemon's own codex runs.
# If this does not go silent, PC1 proves nothing about the population — it would be passing for
# some other reason, and the proof set would not be reaching the code under test.
events = []
DP, dp = daemon(lock=HELD_BY_FOREIGN, cpu={5001: 12.0}, alive={5001: T0},
                pop=lambda: ([], [], pglock.NO_RUNS))
s_old = two_ticks(dp)
ok(not [x for x in s_old.get("runs", []) if x["state"] == pglock.STALLED],
   "NEGATIVE CONTROL: with the OLD population (this daemon's own spawns only, here empty) the very "
   "same stalled foreign run produces NO row. PC1 is therefore a statement about the population, "
   "not about verdict()")
ok(any("none observed" in l for l in s_old.get("board", [])),
   "G4: and the empty population SAYS it looked and found nothing, rather than rendering as "
   "silence — an absent row is how this failure stayed invisible")

# ------------------------------------------------------------------------ PC2  the holder itself
events = []
DP, dp = daemon(lock={"path": "/l", "state": pglock.HELD, "pid": 5001, "why": "held by 5001"},
                cpu={5001: 12.0}, alive={5001: T0})
s = two_ticks(dp)
ok(s["runs"][0]["state"] == pglock.LIVE and "HOLDS" in s["runs"][0]["why"],
   "PC2 MUST BITE: the run that IS the lock holder is LIVE, never STALLED. It is the thing "
   "everyone else waits for — a different row and a different action")

# ------------------------------------------------------------------------- PC3  working, not idle
events = []
DP, dp = daemon(lock=HELD_BY_FOREIGN, cpu={5001: 12.0}, alive={5001: T0})
dp.tick()
DP.pglock.cpu_seconds = lambda pid, **kw: 99.0          # burned 87s between the two ticks
dp.tick()
ok(dp.state["sessions"]["runs"][0]["state"] == pglock.LIVE,
   "PC3: lock held by somebody else, but this run burned CPU between the ticks — working, not "
   "waiting. The stall signature is idleness, and it stays a correlation")

# --------------------------------------------------------------- PC4  G1, a stale marker is debris
events = []
marker("dead", 6002, T0 - 9000)
DP, dp = daemon(lock=HELD_BY_FOREIGN, cpu={}, alive={5001: T0})      # 6002 is not alive
s = two_ticks(dp)
ok(not any(x["pid"] == 6002 for x in s["runs"]),
   "PC4 MUST BITE (G1): a marker whose owner_pid is GONE produces NO ROW — not LIVE, not STALLED, "
   "not an UNMEASURED row. It is debris. Without this the first thing this change does is invent a "
   "permanently stalled run out of a leftover file")
ok(any("stale" in n and "6002" in n for n in
       s.get("pgserver_population", {}).get("notes", [])),
   "and the skip is COUNTED and said out loud — a reader that silently drops rows cannot tell you "
   "it dropped any")

# --------------------------------------------------------------------------- PC5  G2, pid reuse
events = []
marker("reused", 6003, T0)
DP, dp = daemon(lock=HELD_BY_FOREIGN, cpu={6003: 1.0},
                alive={5001: T0, 6003: T0 + 4000})    # alive, but a DIFFERENT process
s = two_ticks(dp)
ok(not any(x["pid"] == 6003 for x in s["runs"]),
   "PC5 MUST BITE (G2): a pid that is alive but whose start time does not match the marker is a "
   "RECYCLED pid, not that run. The marker's word about a pid it no longer owns is not evidence")
ok(any("REUSE" in n for n in s.get("pgserver_population", {}).get("notes", [])),
   "and it says pid reuse, not 'stale' — two different facts about two different processes")

# --------------------------------------------------- PC6  G4, could not look vs looked and found
events = []
DP, dp = daemon(lock=FREE, cpu={}, alive={},
                pop=lambda: REAL_POPULATION(root=os.path.join(TMP, "does-not-exist"),
                                            box_path=BOX, lstart=lstarts({}), ps_all=lambda: ""))
s = two_ticks(dp)
ok(s.get("pgserver_population", {}).get("state") == pglock.UNMEASURED,
   "PC6 MUST BITE (G4): an unreadable marker root is UNMEASURED — 'we could not look' is not "
   "'nothing is running', and this detector exists because absence kept rendering as a fact")
ok(any("NOT MEASURED" in l for l in s.get("board", [])),
   "and the board says NOT MEASURED rather than printing an empty run list under a free lock")

# ------------------------------------------------------ PC7  BOSS's own live debris, by its shape
# The real stale marker on this box while this was written: `...-master-npl4h1nh`, owner_pid 41101,
# dead, suite_root under another session's wt-premerge. Reproduced here by SHAPE so the check
# survives BOSS removing his marker; the LIVE smoke against the real one is run separately and
# reported to him before he removes it.
events = []
marker("master-shape", 41101, 1789024893.696295,
       suite="/private/tmp/claude-501/.../cc1792a6/scratchpad/wt-premerge/platform")
DP, dp = daemon(lock=HELD_BY_FOREIGN, cpu={}, alive={5001: T0})
s = two_ticks(dp)
ok(not any(x["pid"] == 41101 for x in s["runs"]),
   "PC7 (G1, live shape): another session's abandoned marker — dead owner_pid, foreign suite_root "
   "— contributes no row, and this daemon does not remove it. Not ours to clean up")

# ------------------------------------------------------- G5  a postmaster is not evidence of work
ok(REAL_POPULATION.__globals__["postmasters"](runner=lambda: (
    "99529 /x/site-packages/pgserver/pginstall/bin/postgres -D /tmp/a/pgdata -h  -k /y\n"
    "50328 /some/other/python -m pytest\n")) == {"/tmp/a/pgdata": 99529},
   "G5: postmasters are read by pgdata for CORROBORATION only — measured 2026-09-10, every one of "
   "them has PPID 1, so they lead nowhere upward and a live one only proves the start SUCCEEDED")

# ------------------------------------------------------------- G6  the CPU baseline keys on (pid, start)
events = []
marker("recycle", 6004, T0)
DP, dp = daemon(lock=HELD_BY_FOREIGN, cpu={6004: 50.0}, alive={5001: T0, 6004: T0})
dp.tick()
keys = list(dp.state["pgserver_cpu"])
ok(all(":" in k for k in keys) and any(k.startswith("6004:") for k in keys),
   "G6 MUST BITE: the CPU baseline is keyed on (pid, start_time), not on pid. A recycled pid would "
   "otherwise inherit the dead run's CPU reading and come out LIVE on a delta spanning two "
   "different processes")

# -------------------------------------------------------- G7  read-only, including the new inputs
before = sorted(os.listdir(MARKERS)) + sorted(os.listdir(BOXDIR))
events = []
DP, dp = daemon(lock=HELD_BY_FOREIGN, cpu={5001: 12.0}, alive={5001: T0})
two_ticks(dp)
ok(sorted(os.listdir(MARKERS)) + sorted(os.listdir(BOXDIR)) == before,
   "G7: the census writes nothing — not a marker, not the box owner file, not a removal of the "
   "stale marker it just skipped. Reading the box lock is not permission to touch it")
ok(open(BOX).read().startswith("owner=lane-004"),
   "and the box owner file is byte-unchanged")

# ------------------------------------------------------------ the finding, and where it goes
events = []
DP, dp = daemon(lock=HELD_BY_FOREIGN, cpu={5001: 12.0}, alive={5001: T0})
dp.tick()
dp.state.pop("sessions_last_emit", None)
dp.tick()
ok(any(e[0] == "RUN_STALLED" for e in events),
   "MUST BITE: a stalled foreign run EMITS RUN_STALLED. A row nobody is told about is the board "
   "reporting what it sent, one more time")
ok(any("lane-004-pgserver-lock" in str(e) and "5001" in str(e) for e in events
       if e[0] == "RUN_STALLED"),
   "and the event carries the owner AND the pid, so it can be acted on without a second query")

# ------------------------------------------------- a crash in the population must not take the tick
events = []
def boom():
    raise RuntimeError("marker root exploded")
DP, dp = daemon(lock=FREE, cpu={}, alive={}, pop=boom)
dp.tick()
ok(dp.state.get("sessions", {}).get("error", "").startswith("RuntimeError"),
   "a sensor that raises records the error and never aborts the tick it is only observing")

print(f"\n{P} passed, {len(F)} failed")
for x in F: print("  FAILED:", x)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if F else 0)
