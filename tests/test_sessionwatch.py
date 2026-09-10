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
import fixtures                      # item 9: the shared state redirect
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
ok(shape["dispatchable"] == 2 and shape["in_flight"] == 1 and shape["awaiting_boss"] == 1
   and shape["refused"] == 1 and shape["outstanding"] == 5,
   f"  the queue shape separates FOUR things: a worker could take this now / it is in flight / a "
   f"person must act / it was dispatched and REFUSED. `refused` used to be folded into "
   f"awaiting_boss, which hid three failed audits behind rows genuinely waiting on BOSS: {shape}")
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
    fixtures.redirect_state(mod)   # item 9: never the LIVE state dir
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

# --- EXECUTORS_UNAVAILABLE: the third fault ------------------------------------------------------
# BOSS, 2026-09-08/10: three audits dispatched at 11:59 were ALL refused on QUOTA within two
# minutes. He told ★ and the owner they were running and did not learn otherwise for nineteen
# minutes. Neither existing finding would have caught it: "nothing dispatchable" was technically
# true and completely false (the queue was not empty — three items had been dispatched and failed),
# and nobody was failing to pick work up. The executors could not work at all.
NOW2 = 2_000_000.0
def mstate(**profs):
    return {"unhealthy": {p: dict(v) for p, v in profs.items()}}

ALL_HELD = mstate(
    **{"muse-go-1": {"class": "QUOTA", "scope": "rolling", "until": NOW2 + 3600, "why": "wall"},
       "muse-go-2": {"class": "QUOTA", "scope": "weekly", "until": NOW2 + 7 * 86400, "why": "wall"},
       "muse-go-3": {"class": "AUTH", "until": 0, "why": "401"}})
THREE = ("muse-go-1", "muse-go-2", "muse-go-3")

rh = SW.route_health(ALL_HELD, profiles=THREE, now=NOW2)
ok(rh["all_held"] and not rh["available"] and len(rh["held"]) == 3,
   "MUST BITE: every route held reads as all_held with nothing available")
byp = {h["profile"]: h for h in rh["held"]}
ok(byp["muse-go-1"]["cls"] == "QUOTA" and byp["muse-go-1"]["expires_in_s"] == 3600
   and byp["muse-go-3"]["cls"] == "AUTH",
   "MUST BITE: each held route carries its REASON and its EXPIRY — QUOTA waits for a clock and AUTH "
   "waits for a human, and those need different actions")
ok(byp["muse-go-3"]["expires_in_s"] is None,
   "MUST BITE: a hold with no expiry is None, NOT 0. `0 min left` reads as 'about to clear' when it "
   "means 'until a human clears it' — the opposite")

# THE EXPIRED-HOLD CASE, which is the live state on this machine right now.
EXPIRED = mstate(**{p: {"class": "QUOTA", "until": NOW2 - 3600, "why": "lapsed"} for p in THREE})
rh_exp = SW.route_health(EXPIRED, profiles=THREE, now=NOW2)
ok(not rh_exp["all_held"] and len(rh_exp["available"]) == 3,
   "MUST BITE: EXPIRED holds are not holds. The live state carries three QUOTA records that lapsed "
   "54 hours ago, and a census reading the raw map would report a dead programme")
ok(SW.route_health({}, profiles=THREE, now=NOW2)["all_held"] is False
   and SW.route_health(ALL_HELD, profiles=(), now=NOW2)["all_held"] is False,
   "  an empty health map is not all-held, and neither is an EMPTY route set — `all([])` is True "
   "and would have declared a daemon with no routes configured to be fully blocked")

# the held-ness rule is the DAEMON'S, not a second copy
called = {"n": 0}
def _spy(state, now):
    called["n"] += 1
    return {}
SW.route_health(ALL_HELD, profiles=THREE, now=NOW2, held_now=_spy)
ok(called["n"] == 1,
   "  held-ness comes from an injectable rule, and the default is museadapter.unhealthy_now — the "
   "same one select_profile dispatches on. A board that computes its own answer eventually "
   "disagrees with the daemon, and the disagreement is invisible")

# --- the finding, and its RANK -------------------------------------------------------------------
QUIET_EMPTY = SW.queue_shape([{"status": "reported"}, {"status": "broken"}, {"status": "broken"}])
key, text = SW.finding(rows, QUIET_EMPTY, quiet_after_s=1800, routes=rh)
ok(key == "EXECUTORS_UNAVAILABLE",
   "MUST BITE: all-routes-held OUTRANKS every quiet finding, because it EXPLAINS them. Reporting "
   "'nothing dispatchable' here would be true about the wrong subject, and a finding that is true "
   "about the wrong subject gets acted on")
ok("QUOTA" in text and "AUTH" in text and "until a human clears it" in text,
   f"  and it carries every reason and expiry into the one line: {text}")
ok("2 REFUSED" in text,
   f"MUST BITE: the REFUSED count travels with it. Three dispatched-and-refused items must not read "
   f"as a healthy empty queue: {text}")
ok("muse-go-1, muse-go-2, muse-go-3" in text,
   "MUST BITE: the log NAMES the route set it considered, so 'every route' cannot be read as a "
   "wider or narrower claim than the one measured")
ok(SW.finding(rows, QUIET_EMPTY, quiet_after_s=1800, routes=rh_exp)[0]
   != "EXECUTORS_UNAVAILABLE",
   "  CONTROL: with the same sessions and queue but routes AVAILABLE, this finding does not fire")
ok(SW.finding(rows, QUIET_EMPTY, quiet_after_s=1800, routes=None)[0] != "EXECUTORS_UNAVAILABLE",
   "  CONTROL: no route census at all is NOT all-held — absence must not manufacture the finding")

# --- REFUSED is its own column -------------------------------------------------------------------
sh2 = SW.queue_shape([{"status": "broken"}, {"status": "reported"}, {"status": "parked"}])
ok(sh2["refused"] == 1 and sh2["awaiting_boss"] == 2,
   f"MUST BITE: `broken` is counted separately and is NO LONGER folded into awaiting_boss — an item "
   f"dispatched and refused is neither waiting for a worker nor waiting for a person: {sh2}")

# --- the board says which, and says NOT MEASURED when it does not know ---------------------------
bl = SW.board_lines(rows, QUIET_EMPTY, routes=rh)
ok(any("ALL HELD" in l for l in bl) and any("AUTH" in l for l in bl),
   f"MUST BITE: the board shows the routes and marks all-held loudly: {[l for l in bl if 'routes' in l]}")
ok(any("refused" in l for l in bl), "  and the queue half of the board shows the refused count")
ok(any("routes: NOT MEASURED" in l for l in SW.board_lines(rows, QUIET_EMPTY, routes=None)),
   "MUST BITE: with no route census the board says NOT MEASURED rather than printing nothing — "
   "nothing reads as calm")
ok(any("all 3 available" in l for l in SW.board_lines(rows, QUIET_EMPTY, routes=rh_exp)),
   "  CONTROL: healthy routes render as available rather than being omitted")

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

# --- THE ROUTE CENSUS CALL SITE, driven through poll_sessions ------------------------------------
# Deleting the route census from the daemon, and inventing a route set when none is configured, BOTH
# scored MISSED against every check above: they all drive route_health and finding directly, which
# proves the functions and never the path to them. This drives the daemon.
mod, d = daemon()
mod.sessionwatch.census = lambda **k: rows
d.cfg = dict(d.cfg, codex_routes={"CODEX-1": "muse-go-1", "CODEX-2": "muse-go-2"})
d.state["muse"] = {"unhealthy": {
    "muse-go-1": {"class": "QUOTA", "scope": "rolling", "until": time.time() + 3600, "why": "wall"},
    "muse-go-2": {"class": "AUTH", "until": 0, "why": "401"}}}
d.load_queue = lambda: {"items": [{"status": "broken"}, {"status": "broken"}]}
d.poll_sessions()
ok(d.state["sessions"].get("routes") is not None
   and d.state["sessions"]["routes"]["all_held"] is True,
   "MUST BITE: the daemon actually CENSUSES the routes and lands the result in state — with both "
   "configured routes held, all_held is true")
ok(d._evs and d._evs[-1][0] == "EXECUTORS_UNAVAILABLE",
   f"MUST BITE: and it emits the third finding rather than a quiet one: {d._evs[-1][0] if d._evs else None}")
ok("muse-go-1, muse-go-2" in d._evs[-1][-1],
   "  naming the route set it considered, taken from the roster's own codex_routes")

# CONTROL: a daemon with NO routes configured must report NOT MEASURED, never all-held.
mod, d = daemon()
mod.sessionwatch.census = lambda **k: rows
d.cfg = dict(d.cfg, codex_routes={})
d.state["muse"] = {}
d.poll_sessions()
ok(d.state["sessions"].get("routes") is None,
   "MUST BITE: with nothing configured the route census is NOT MEASURED — inventing an empty set "
   "would let the daemon declare itself fully blocked on the strength of having no routes at all")
ok(not any(e[0] == "EXECUTORS_UNAVAILABLE" for e in d._evs),
   "  and it emits no all-held finding from that absence")

# --- THE PGSERVER LOCK CALL SITE, driven through poll_sessions -----------------------------------
# Deleting the daemon's lock probe scored MISSED against every check in test_pglock.py: they all
# drive pglock directly. Same lesson as the route census one screen up — the function was never the
# thing at risk.
mod, d = daemon()
mod.sessionwatch.census = lambda **k: rows
probed = {"n": 0}
_realprobe = mod.pglock.probe
def _counting_probe(*a, **k):
    probed["n"] += 1
    return _realprobe(*a, **k)
mod.pglock.probe = _counting_probe
# THE BOX POPULATION IS STUBBED OUT HERE, and that is the point of this comment rather than a
# convenience. After item 4 the run set comes from the box's own markers, so leaving it live would
# make this check read the REAL markers under $TMPDIR — it would pass or fail depending on whether
# somebody else's suite happened to be running, and its first row would be their run, not ours.
# A test never depends on live state it does not own. The subject here is narrower and unchanged:
# the daemon's own tracked codex run still gets a verdict against the lock.
mod.pglock.population = lambda: ([], [], mod.pglock.NO_RUNS)
d.state["codex"] = {"CODEX-1": {"pid": os.getpid(), "item": "A.x"}}
d.poll_sessions()
ok(probed["n"] == 1 and d.state["sessions"].get("pgserver_lock") is not None,
   "MUST BITE: the daemon PROBES the pgserver lock every tick and lands the result in state — "
   "box.lock.d and portal.lock.d do not know this lock exists")
ok(any(r["pid"] == os.getpid() for r in (d.state["sessions"].get("runs") or [])),
   "  and it gives every tracked run a verdict against that lock — asserted by MEMBERSHIP, not by "
   "position: the box population is prepended now, so runs[0] is whoever the box is running")
ok(any("pgserver lock" in l for l in d.state["sessions"]["board"]),
   f"MUST BITE: and the board carries a pgserver row: "
   f"{[l for l in d.state['sessions']['board'] if 'pgserver' in l]}")
ok(d.state.get("pgserver_cpu"),
   "  the CPU sample is stored for the next tick — a stall needs two, and one tick cannot tell "
   "working from waiting")

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

# The ROUTE census on its DEFAULT held-ness rule — no injected `held_now`, so this exercises the
# real museadapter.unhealthy_now against this machine's real state file. The rest of the route
# checks inject that rule, which is how a detector ships inert: the injected path works and the
# default one is never run.
_live_state = os.path.expanduser("~/Claude Code/Calling New/test-logs/driver/state.json")
if os.path.isfile(_live_state):
    _mst = (json.load(open(_live_state)) or {}).get("muse", {})
    _profs = sorted({p for p in (_mst.get("affinity") or {}).values() if p}) or ["muse-go-1"]
    _rh = SW.route_health(_mst, profiles=_profs)
    ok(set(_rh["considered"]) == set(_profs)
       and len(_rh["held"]) + len(_rh["available"]) == len(_profs),
       f"LIVE SMOKE — MUST BITE: route_health runs on its DEFAULT rule against the real state file "
       f"and accounts for every route exactly once: {len(_rh['held'])} held, "
       f"{len(_rh['available'])} available of {len(_profs)}")
    ok(all(h["expires_in_s"] is None or h["expires_in_s"] >= 0 for h in _rh["held"]),
       "  and every real hold's expiry is a non-negative duration or an explicit None — the live "
       "state carries QUOTA records that lapsed 54 hours ago, and those must read as AVAILABLE")
else:
    ok(False, "LIVE SMOKE: the live state file was not found — this check measured nothing")

print(f"\n{P} passed, {len(FAILED)} failed")
for f in FAILED: print("  FAILED:", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
