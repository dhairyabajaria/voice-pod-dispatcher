"""Six hours produced nothing and every instrument said the system was healthy.

2026-09-07/08: every Claude session finished a turn and stopped, the Codex queue was empty, and the
dispatcher wrote a fresh heartbeat every five seconds throughout. THE HEARTBEAT MEASURES THE DAEMON,
NOT THE PROGRAMME — and nothing measured the programme at all.

BOSS's ruling 2026-09-08 was to build the DETECTOR, not the poker: waking a session is available to
a daemon (measured — the per-session socket accepts a connection from a process with no Claude
environment) but requires handing a daemon each session's live messaging token, which reverses the
`HANDLES` strip in `museadapter.child_env` and is the owner's decision, not ours.

What this file holds the detector to, each of which is a way it could lie comfortably:
  * a leftover socket whose process is gone must read as ENDED, never as a live-but-quiet session.
    There were six sockets on disk against far fewer sessions.
  * a recycled pid must not read as a session. The pid alone cannot tell you that.
  * "we could not measure this session" must never render as 0 minutes quiet, and must never be
    counted among the quiet. NOT MEASURED is a statement about our instrument.
  * the finding must carry the QUEUE, because a quiet session with nothing to do is not the same
    fault as a quiet session with work waiting — and the six lost hours were the first kind.
  * the census must not be able to abort the tick it is only observing.

Hermetic: a temporary socket directory, injected pid and mtime probes. No real sockets, no lsof, no
ps, no daemon, and nothing here can open a socket even by accident.
"""
import importlib.util, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location(
    "sessionwatch_t", os.path.join(HERE, os.pardir, "sessionwatch.py"))
SW = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(SW)

P, FAILED = 0, []
def ok(cond, what):
    global P
    if cond: P += 1; print(f"PASS {what}")
    else: FAILED.append(what); print(f"FAIL {what}")

TMP = tempfile.mkdtemp(prefix="sessionwatch-")
SOCKS = os.path.join(TMP, "cc-socks"); os.makedirs(SOCKS)
for pid in (100, 200, 300, 400):
    open(os.path.join(SOCKS, f"{pid}.sock"), "w").close()
open(os.path.join(SOCKS, "not-a-socket.txt"), "w").close()

NOW = 1_000_000.0
# 100 active, 200 quiet, 300 ended (pid gone), 400 live but its transcript cannot be found
CLAUDE = {100: True, 200: True, 300: False, 400: True}
MTIME = {100: NOW - 60, 200: NOW - 4000, 400: None}

def cen(now=NOW, claude=None, mt=None):
    return SW.census(dirs=(SOCKS,), is_claude=lambda p: (claude or CLAUDE).get(p),
                     mtime=lambda p: (mt or MTIME).get(p), now=now)

rows = cen()
by = {r["pid"]: r for r in rows}
ok(len(rows) == 4 and "not-a-socket.txt" not in json.dumps(rows),
   "MUST BITE: one row per SOCKET, and a non-socket file in the directory is not a session")
ok(by[300]["state"] == SW.ENDED,
   "MUST BITE: a socket whose process is gone reads ENDED — a leftover file is not a quiet session. "
   "Six sockets were on disk against far fewer live sessions")
ok(by[100]["state"] == SW.LIVE and by[100]["quiet_s"] == 60,
   "  a live session carries its measured quiet time")
ok(by[400]["state"] == SW.LIVE and by[400]["quiet_s"] is None,
   "MUST BITE: a live session whose transcript cannot be found is quiet=None — NOT MEASURED, never "
   "0, because 'we cannot see it' is not 'it just spoke'")

s = SW.summarise(rows, quiet_after_s=1800)
ok(s == {"sockets": 4, "live": 3, "ended": 1, "unknown": 0, "unmeasured": 1, "quiet": 1,
         "quietest_s": 4000},
   f"MUST BITE: the counts keep the states apart — 3 live, 1 ended, 1 live-but-unmeasured, and "
   f"exactly 1 counted quiet. The unmeasured session is NOT counted quiet: {s}")

unknown = cen(claude={100: None, 200: None, 300: None, 400: None})
ok(all(r["state"] == SW.UNKNOWN for r in unknown)
   and SW.summarise(unknown)["quiet"] == 0,
   "MUST BITE: when the liveness probe itself fails, every row is UNKNOWN and NOTHING is reported "
   "quiet — a broken instrument must not manufacture a finding")

# --- the pid check, which is what stops a recycled pid reading as a session --------------------
ok(SW.pid_is_claude_session(1, runner=lambda p: "claude\n") is True
   and SW.pid_is_claude_session(1, runner=lambda p: "Python\n") is False,
   "MUST BITE: liveness is not enough — the process must actually BE claude, or a recycled pid "
   "reports as a live session")
ok(SW.pid_is_claude_session(1, runner=lambda p: "") is False
   and SW.pid_is_claude_session(1, runner=lambda p: None) is None,
   "  and 'ps returned no row' (dead) is kept apart from 'the probe failed' (unknown)")
def _boom(pid): raise OSError("ps exploded")
ok(SW.pid_is_claude_session(1, runner=_boom) is None,
   "  a probe that raises is UNKNOWN, not False — an exception must not read as 'session ended'")

# --- the pid->transcript mapping, and the reason the FIRST design was inert ----------------------
# The first cut asked lsof which .jsonl the session held open. It passed every check on this page
# against injected probes and reported "quiet NOT MEASURED" for ALL SIX real sessions, because a
# session does not hold its transcript open — it appends and closes. A detector that could never
# fire, fully green. So these drive the real parsing, and the live smoke at the bottom drives the
# real probes.
ok(SW.session_uuid(1, runner=lambda p: "claude --resume=ca7bff25-78bf-4383-b127-2c1cbcb36fc7 -x")
   == "ca7bff25-78bf-4383-b127-2c1cbcb36fc7",
   "MUST BITE: the session id is parsed out of the process argv — that is the pid-to-transcript "
   "mapping, and it needs no credential and no lsof")
ok(SW.session_uuid(1, runner=lambda p: "claude --resume ca7bff25-78bf-4383-b127-2c1cbcb36fc7")
   == "ca7bff25-78bf-4383-b127-2c1cbcb36fc7",
   "  both spellings, `--resume=x` and `--resume x`")
ok(SW.session_uuid(1, runner=lambda p: "claude --output-format stream-json") is None
   and SW.session_uuid(1, runner=lambda p: "") is None,
   "MUST BITE: a session started fresh rather than resumed carries no id, and that is None — NOT "
   "MEASURED. It must not fall back to the newest file in the directory, which would attribute one "
   "session's work to another")
def _argsboom(pid): raise OSError("ps exploded")
ok(SW.session_uuid(1, runner=_argsboom) is None, "  and a probe that raises is None, not a crash")

PROJ = os.path.join(TMP, "projects"); os.makedirs(os.path.join(PROJ, "projA"))
UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
tp = os.path.join(PROJ, "projA", UID + ".jsonl"); open(tp, "w").close()
os.utime(tp, (NOW - 900, NOW - 900))
ok(SW.transcript_mtime(1, uuid_of=lambda p: UID, projects_root=PROJ) == NOW - 900,
   "MUST BITE: the transcript is found by the session's OWN id under the projects root, and its "
   "mtime is the activity signal")
ok(SW.transcript_mtime(1, uuid_of=lambda p: "no-such-id", projects_root=PROJ) is None,
   "  a transcript we cannot find is None, never a time")
os.makedirs(os.path.join(PROJ, "projB"))
open(os.path.join(PROJ, "projB", UID + ".jsonl"), "w").close()
ok(SW.transcript_mtime(1, uuid_of=lambda p: UID, projects_root=PROJ) is None,
   "MUST BITE: two projects carrying the same id is NOT a time either — it refuses rather than "
   "picking one, the same rule as the rollout resolver")
ok(SW.transcript_mtime(1, uuid_of=lambda p: None, projects_root=PROJ) is None,
   "  and no id at all short-circuits to NOT MEASURED")

# --- the queue shape, which is half of every finding --------------------------------------------
ITEMS = [{"status": "queued"}, {"status": "queued"}, {"status": "dispatched"},
         {"status": "reported"}, {"status": "merged"}, {"status": "broken"}]
shape = SW.queue_shape(ITEMS)
ok(shape["dispatchable"] == 2 and shape["in_flight"] == 1 and shape["awaiting_boss"] == 2
   and shape["outstanding"] == 5,
   f"  the queue shape separates 'a worker could take this now' from 'this is waiting on BOSS' "
   f"and excludes terminal rows from outstanding: {shape}")
ok(SW.queue_shape([])["outstanding"] == 0 and SW.queue_shape(None)["total"] == 0,
   "  an empty or absent queue does not raise")

# --- THE FINDING ---------------------------------------------------------------------------------
EMPTY = SW.queue_shape([{"status": "reported"}, {"status": "parked"}])
key, text = SW.finding(rows, EMPTY, quiet_after_s=1800)
ok(key == "SESSIONS_QUIET_QUEUE_EMPTY",
   "MUST BITE: quiet sessions + NOTHING dispatchable + work outstanding is its own finding — that "
   "is the exact 2026-09-07/08 shape, and a poke into an empty queue would not have fixed it")
ok("2 item(s) outstanding" in text and "NOTHING dispatchable" in text,
   f"  and it carries the queue with it, so nobody has to go look: {text}")

WORK = SW.queue_shape([{"status": "queued"}])
ok(SW.finding(rows, WORK, quiet_after_s=1800)[0] == "SESSIONS_QUIET_WORK_WAITING",
   "MUST BITE: quiet sessions WITH dispatchable work is a DIFFERENT finding — one is 'nobody has "
   "anything to do', the other is 'somebody is not picking it up', and they need different actions")

busy = cen(mt={100: NOW - 10, 200: NOW - 10, 400: None})
ok(SW.finding(busy, EMPTY, quiet_after_s=1800) is None,
   "MUST BITE: CONTROL — no quiet session means NO finding. A detector that always finds something "
   "is not a detector")
ok(SW.finding([], EMPTY, quiet_after_s=1800) is None,
   "  and no sessions at all is silence, not a finding about zero sessions")
only_unmeasured = cen(mt={100: None, 200: None, 400: None})
ok(SW.finding(only_unmeasured, EMPTY, quiet_after_s=1800) is None,
   "MUST BITE: three live sessions we cannot measure produces NO quiet finding — the detector "
   "never converts its own blindness into a claim about the programme")

# --- the board line ------------------------------------------------------------------------------
lines = SW.board_lines(rows, EMPTY, quiet_after_s=1800)
ok(any("1 ended socket(s)" in l for l in lines) and any("NOT MEASURED" in l for l in lines),
   f"MUST BITE: the board names every state including the ones we could not measure: {lines[0]}")
ok(SW.board_lines([], EMPTY) == ["sessions: NOT MEASURED — no socket directory found; this row is "
                                 "blind, not empty"],
   "MUST BITE: with no socket directory the board says BLIND, not nothing — nothing reads as calm, "
   "which is the failure this whole file exists for")
ok(not any("pid 300" in l for l in lines),
   "  and an ENDED socket gets no per-session row, so a leftover cannot be mistaken for a session")

# --- the wiring: the census must not be able to abort the tick ------------------------------------
def daemon():
    spec = importlib.util.spec_from_file_location(
        "dspsw" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    d = mod.Dispatcher.__new__(mod.Dispatcher)
    d.state = {}
    d.cfg = {"session_quiet_minutes": 30, "session_quiet_repeat_minutes": 30}
    d.dry = False
    d.c = lambda k, dflt=None: d.cfg.get(k, dflt)
    d._logs, d._evs = [], []
    d.log = lambda *a, **k: d._logs.append(a[0] if a else "")
    d.emit = lambda *a: d._evs.append(a)
    d.load_queue = lambda: {"items": [{"status": "reported"}]}
    return mod, d

mod, d = daemon()
mod.sessionwatch.census = lambda **k: (_ for _ in ()).throw(RuntimeError("sensor exploded"))
# GUARDED: if the catch is narrowed, the exception escapes. Letting it escape here would replace a
# clean FAIL with a traceback, and a crash is neither a catch nor a miss.
_escaped = None
try:
    d.poll_sessions()
except Exception as _e:
    _escaped = f"{type(_e).__name__}: {_e}"
ok(_escaped is None, f"MUST BITE: the exception does not ESCAPE poll_sessions — got {_escaped}")
ok(d.state.get("sessions", {}).get("error", "").startswith("RuntimeError"),
   "MUST BITE: a census that raises is CAUGHT and recorded — a sensor must never abort the tick it "
   "is only observing")
ok(any("session census failed" in l for l in d._logs) and not d._evs,
   "  and it says so in the log without emitting a finding it does not have")

mod, d = daemon()
mod.sessionwatch.census = lambda **k: rows
d.poll_sessions()
ok(d._evs and d._evs[0][0] == "SESSIONS_QUIET_QUEUE_EMPTY",
   "MUST BITE: driven through poll_sessions — the daemon actually emits the finding. Proving the "
   "module proves the module, which was never the thing at risk")
ok(d.state["sessions"]["board"] and d.state["sessions"]["summary"]["live"] == 3,
   "  and the census lands in state, which is what the board reads")
n = len(d._evs)
d.poll_sessions()
ok(len(d._evs) == n,
   "MUST BITE: the same finding does not re-emit every five seconds — a line repeated 720 times an "
   "hour is noise, and noise is how a real one gets scrolled away")
d.load_queue = lambda: {"items": [{"status": "queued"}]}
d.poll_sessions()
ok(len(d._evs) == n + 1 and d._evs[-1][0] == "SESSIONS_QUIET_WORK_WAITING",
   "MUST BITE: but a CHANGE of finding emits immediately — the rate limit is on the key, not on the "
   "clock, so 'nothing to do' becoming 'not picking it up' is never held back")

# --- THE TICK MUST ACTUALLY CALL IT --------------------------------------------------------------
# Removing `self.poll_sessions()` from tick() scored MISSED against every check above: they all
# drive poll_sessions directly, which proves the method and never the path to it. So this drives the
# REAL tick. The STOP branch is used deliberately — the census is placed BEFORE that early return,
# because a paused daemon is exactly when a human most needs to know the sessions went quiet, and
# that placement is itself the thing under test.
mod, d = daemon()
mod.sessionwatch.census = lambda **k: rows
called = {"n": 0}
_real = d.poll_sessions
def _counting():
    called["n"] += 1
    return _real()
d.poll_sessions = _counting
d.reload_cfg = lambda: None
d.write_heartbeat = lambda *a, **k: None
d.write_pending = lambda *a, **k: None
d.paused_logged = True
d.paused_since = time.time()
# THE STOP PATH IS REDIRECTED INTO THE TEMP DIR, NEVER THE LIVE ONE. The first cut of this check
# created and deleted the REAL test-logs/driver/STOP — the owner's kill switch. Nothing was harmed
# (the daemon was dispatching before and after, and never logged PAUSED), but had STOP been set
# deliberately, the cleanup would have SILENTLY RESUMED a daemon somebody paused on purpose. A test
# that can operate the kill switch is a worse defect than the one it is testing for.
mod.STOP = os.path.join(TMP, "STOP")
open(mod.STOP, "a").close()           # force the early return, so nothing else in tick runs
d.tick()
ok(called["n"] == 1,
   "MUST BITE: tick() calls the census — and BEFORE the STOP early return, so a paused daemon still "
   "reports that the sessions have gone quiet. Deleting the call site scored MISSED until this "
   "check drove the real tick instead of the method")
ok(d.state.get("sessions", {}).get("summary", {}).get("live") == 3,
   "  and the census it ran is the one that lands in state")

# --- LIVE SMOKE: the DEFAULT probes, against this machine ----------------------------------------
# EVERY CHECK ABOVE INJECTS ITS PROBES, AND THAT IS EXACTLY HOW THE FIRST VERSION SHIPPED INERT.
# This one runs the real `ps` and the real projects directory. It is environment-dependent by
# design: dispatcher/ has no CI, so a local green is the only evidence this will ever have, and a
# detector nobody has watched work on the real machine is not a detector.
live = SW.census()
measured = [r for r in live if r["state"] == SW.LIVE and r["quiet_s"] is not None]
ok(live, "LIVE SMOKE: the real census finds sockets on this machine (if this fails, the socket "
         "directory moved — check SOCK_DIRS against the CLI binary's own regexes)")
ok(measured,
   f"LIVE SMOKE — MUST BITE: at least one real session's quiet time is actually MEASURED. This is "
   f"the check the first design failed: it was green everywhere above and returned NOT MEASURED for "
   f"all six real sessions. Got {len(measured)} measured of {len(live)} socket(s)")
ok(all(isinstance(r["quiet_s"], int) and r["quiet_s"] >= 0 for r in measured),
   "  and the measured values are real non-negative seconds, not a sentinel")

print(f"\n{P} passed, {len(FAILED)} failed")
for f in FAILED: print("  FAILED:", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
