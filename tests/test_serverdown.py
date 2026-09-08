"""A tick that can do no work must not look like a healthy idle one.

BOSS, 2026-09-08, after finding that ZERO Muse workers had ever run under the daemon: the tick pings
`/global/health` on the opencode server and, on any exception, calls `save_state()` and returns —
before `codex_tick` and before every dispatch path. The opencode server had been down since
2026-09-07 13:13:42, so the tick had returned early for roughly sixteen hours.

**Codex workers have no functional dependency on that server.** They are `codex exec` subprocesses on
another provider. They were blocked by a health check for a service they never use, which means the
routing, the wiring, the restarts and the config were all correct and all irrelevant.

THE WORST PART, AND WHAT MOST OF THIS FILE IS ABOUT. `save_state()` runs before the return and the
heartbeat is written at the TOP of the tick. So state.json was fresh, the heartbeat was seconds old,
and every liveness signal on the board was being written BY THE CODE PATH THAT DOES NOTHING. A
daemon that could do no work was indistinguishable from a healthy idle one — for sixteen hours.

So the checks here are not only "does codex still dispatch". They are: does the do-nothing path SAY
SO, in the same signals that were lying, on EVERY tick rather than once.

Hermetic: no server, no network, no worktree, no codex binary.
"""
import importlib.util, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
P, F = 0, []
def ok(c, w):
    global P
    if c: P += 1; print(f"PASS {w}")
    else: F.append(w); print(f"FAIL {w}")

TMP = tempfile.mkdtemp(prefix="serverdown-")
os.makedirs(os.path.join(TMP, "items"))
open(os.path.join(TMP, "items", "C1.md"), "w").write("brief")


def daemon(queue_items, health_ok, spawned):
    spec = importlib.util.spec_from_file_location(
        "dsp" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
    DP = importlib.util.module_from_spec(spec); spec.loader.exec_module(DP)
    DP.STATE_DIR = TMP
    DP.HEARTBEAT = os.path.join(TMP, "heartbeat")
    DP.QUEUE = os.path.join(TMP, "queue.json")
    DP.QUEUE_LOCK = os.path.join(TMP, "queue.lock")
    DP.STOP = os.path.join(TMP, "STOP"); DP.OBSERVE = os.path.join(TMP, "OBSERVE")
    DP.QUEUE_HOLD = os.path.join(TMP, "HOLD")
    DP.EVENTS = os.path.join(TMP, "events.log")
    DP.CODEX_DIR = TMP; DP.ITEMS_DIR = os.path.join(TMP, "items")
    json.dump({"items": queue_items}, open(DP.QUEUE, "w"))

    def http(method, path, **kw):
        if path == "/global/health" and not health_ok:
            raise OSError("connection refused")
        return {}
    DP.http = http
    dp = DP.Dispatcher.__new__(DP.Dispatcher)
    dp.dry = False; dp.once = False; dp.observing = False; dp.observe_logged = False
    dp.cfg = {"codex_slots": 1, "codex_bin": "/x/codex", "codex_spawn_stagger_seconds": 0,
              "max_dispatch_per_tick": 2, "codex_model": "gpt-6-astra", "codex_effort": "medium"}
    dp.state = {"pending": {}, "parks": {}, "codex": {}, "server_down_emitted": False}
    dp._spawn_refusal = {}
    dp.server_down_since = None
    dp.paused_since = None; dp.last_resync = time.time(); dp.roster = {"EXEC-A": "s1"}
    dp.log = lambda *a, **k: logs.append(a[0] if a else "")
    dp.emit = lambda *a: events.append(a)
    dp.escalate = lambda *a: None
    dp.notify_owner = lambda *a, **k: None
    dp.save_state = lambda: saved.append(1)
    dp.write_pending = lambda m: boards.append(m)
    dp.hold_reason = lambda who: None
    dp.notify_owner = lambda *a, **k: notified.append(a)
    dp.escalate = lambda text: escalated.append(text)
    dp.undelivered_answer = lambda who: None
    dp.boss_activity_since = lambda ms: ("NO_ACTIVITY", "BOSS has done nothing since")
    dp.roster = {"EXEC-J": "ses_j"}
    dp.provider_error_summary = lambda: {}
    dp.reload_cfg = lambda: None
    dp.poll_lockwatch = lambda: None
    dp.codex_spawn = lambda slot, item: (spawned.append((slot, item["id"])), True)[1]
    return DP, dp


ITEM = [{"id": "C1", "status": "queued", "executor": "CODEX", "worktree": os.path.join(TMP, "wt"),
         "lane": "l", "title": "t"}]

# ---------------------------------------------------------------- the server is down
logs, events, saved, spawned, boards, notified, escalated = [], [], [], [], [], [], []
DP, dp = daemon(ITEM, False, spawned)
dp.server_down_since = time.time() - 3600
dp.tick()
ok(spawned == [("CODEX-1", "C1")],
   "MUST BITE: with the opencode server DOWN, a CODEX item is still dispatched — codex workers are "
   "subprocesses on another provider and share nothing with that server. This is the sixteen-hour "
   "outage in which zero Muse workers ever ran")
ok(dp.state.get("degraded", {}).get("reason", "").startswith("opencode server unreachable"),
   "the tick leaves a DEGRADED mark in state, not just a one-shot event")
# Read defensively THROUGHOUT: a mutation that removes a field should fail a CHECK, not raise a
# KeyError. A crash is neither a catch nor a miss, and a proof set that crashes under mutation
# cannot tell you which one it was.
ok(dp.state.get("degraded", {}).get("for_s", 0) >= 3600 and dp.state.get("degraded", {}).get("as_of"),
   "carrying how long it has been true and when that was last asserted")
hb = open(os.path.join(TMP, "heartbeat")).read()
ok("DEGRADED" in hb and "CODEX dispatch only" in hb,
   "MUST BITE: the HEARTBEAT ITSELF says the tick was degraded. This is the signal that lied for "
   "sixteen hours — fresh, seconds old, written on the way out of a tick that could do no work")

# the mark must be re-asserted EVERY tick, not once
first = dict(dp.state["degraded"])
dp.state["server_down_emitted"] = True          # the SERVER_DOWN event will not fire again
# CLEARED IN PLACE, not rebound. `spawned = []` makes a NEW list while the daemon's stub still holds
# the old one, so the second tick's dispatch lands somewhere this check cannot see — and the check
# then fails for a reason that has nothing to do with the daemon. Same shape as every other fixture
# defect tonight: the instrument, not the product.
for _lst in (logs, events, saved, spawned, boards, notified, escalated): _lst.clear()
time.sleep(1.05)
dp.tick()
ok(not any(e[0] == "SERVER_DOWN" for e in events),
   "the one-shot EVENT correctly does not repeat — an event is a statement about a moment")
ok(dp.state.get("degraded", {}).get("for_s", 0) > first["for_s"],
   "MUST BITE: but the CONDITION is re-asserted with a fresh duration on every degraded tick. "
   "Sixteen hours of outage were announced once, yesterday, and were silent afterwards")
ok(spawned == [("CODEX-1", "C1")], "and codex keeps being dispatched on every degraded tick")

# ------------------------------------------------ THE BOARD, which froze for 16.6 hours
# Measured 2026-09-08: pending.json was last written 2026-09-07 12:49:14 because this path returns
# before write_pending(). `dispatcherctl status` printed fourteen EXEC rows of yesterday's state and
# `CODEX-1 idle: no eligible CODEX item` over a Muse worker that had been running for 91 seconds.
# Nothing was guessing — the file was old, and nothing said so. A stale row is worse than a missing
# one, because a row is an assertion.
ok(len(boards) == 1,
   "MUST BITE: the degraded tick WRITES the board. Returning before write_pending() freezes it at "
   "its last value, and a frozen board renders exactly like a live one")
b = boards[0] if boards else {}
ok(b.get("CODEX-1", "").startswith("dispatched")
   or "REFUSED" in b.get("CODEX-1", "") or "idle" in b.get("CODEX-1", ""),
   "the CODEX rows carry this tick's real status, not the last one before the outage")
ok(any("UNMEASURED" in v and "NOT being read" in v for v in b.values()),
   "MUST BITE: the opencode rows are REPLACED by a row saying they are unmeasured — a stale "
   "executor row is an assertion about an executor nobody is reading")
ok(not any(k.startswith("EXEC-") for k in b),
   "and yesterday's EXEC rows do not stand alongside it")


# --------------------------------- THE AGE COLUMN, and the escalation behind the same early return
# 2026-09-08. The board printed `EXEC-J REPORT_READY since 12:24:11 ... age 25 min` while the row was
# 1025 minutes old. `age_min` is written by age_and_escalate(), which sits behind this early return,
# so the number was frozen at 12:49 the previous day. The line contradicted ITSELF — `since 12:24`
# and `age 25 min` cannot both be true — and that contradiction was the only honest thing on it.
# Two real REPORT_READY rows waited seventeen hours behind that number, escalation included.
OLD_MS = int((time.time() - 17 * 3600) * 1000)
logs2, events2, saved2, spawned2, boards2, notified2, escalated2 = [], [], [], [], [], [], []
DP5, dp5 = daemon(ITEM, False, spawned2)
dp5.server_down_since = time.time() - 3600
dp5.state["pending"] = {"ses_j": {"executor": "EXEC-J", "session": "ses_j", "kind": "REPORT_READY",
                                  "msg_id": "m1", "since_ms": OLD_MS, "since_local": "12:24:11",
                                  "excerpt": "", "escalated": 1, "esc_count": 1, "last_esc_min": 20,
                                  "esc_base_ms": OLD_MS, "age_min": 25}}
dp5.write_pending = lambda m: boards2.append(dict(m))
dp5.notify_owner = lambda *a, **k: notified2.append(a)
dp5.escalate = lambda text: escalated2.append(text)
dp5.hold_reason = lambda who: None
dp5.undelivered_answer = lambda who: None
dp5.boss_activity_since = lambda ms: ("NO_ACTIVITY", "nothing since")
dp5.roster = {"EXEC-J": "ses_j"}
dp5.tick()
row = dp5.state["pending"]["ses_j"]
ok(row["age_min"] >= 1000,
   "MUST BITE: a degraded tick AGES the pending rows. The displayed 25 minutes was a stored value "
   "frozen when the sensor died, printed beside `since 12:24:11` on the same line")
# One assertion, not an `or` of two: `A and B or C` binds as `(A and B) or C`, so a check written
# that way can pass on a branch that proves nothing about the claim in its name.
ok(len(escalated2) == 1 and "EXEC-J" in escalated2[0] and "REPORT_READY" in escalated2[0],
   "MUST BITE: and it ESCALATES. escalate() only appends to a file — it never needed the opencode "
   "server — so two reports waiting on BOSS sat seventeen hours with nobody told")

# the roster is refreshed on the healthy path, so while degraded it may be empty
logs2.clear(); escalated2.clear()
DP6, dp6 = daemon(ITEM, False, [])
dp6.server_down_since = time.time() - 3600
dp6.cfg["executors"] = {"EXEC-J": "ses_j"}
dp6.roster = {}
dp6.state["pending"] = {"ses_j": dict(row, escalated=0, esc_count=0, last_esc_min=0,
                                      stall_class=None)}
dp6.write_pending = lambda m: None
dp6.notify_owner = lambda *a, **k: None
dp6.escalate = lambda text: escalated2.append(text)
dp6.hold_reason = lambda who: None
dp6.undelivered_answer = lambda who: None
dp6.boss_activity_since = lambda ms: ("NO_ACTIVITY", "nothing since")
dp6.tick()
ok(dp6.state["pending"]["ses_j"].get("stall_class") != "OFF_ROSTER",
   "MUST BITE: an EMPTY roster while degraded does not silently classify every row as OFF_ROSTER. "
   "The roster is refreshed on the healthy path, so an empty one here means UNKNOWN, not absent — "
   "and a silent no-op is the failure this whole area keeps producing")
ok(bool(escalated2), "and the row still escalates rather than being skipped")


# THE REAL write_pending, not the stub. Every check above replaces it, so its body was never
# executed and a mutation that removed the render-time aging scored MISSED — a guard on a path the
# proof set does not enter, which is the same defect being fixed one layer down.
DP7, dp7 = daemon(ITEM, False, [])
DP7.PENDING = os.path.join(TMP, "pending_real.json")
DP7.HOLD_DIR = os.path.join(TMP, "holds"); os.makedirs(DP7.HOLD_DIR, exist_ok=True)
dp7.state["pending"] = {"ses_j": {"executor": "EXEC-J", "session": "ses_j", "kind": "REPORT_READY",
                                  "msg_id": "m1", "since_ms": OLD_MS, "since_local": "12:24:11",
                                  "excerpt": "", "escalated": 1, "esc_count": 1,
                                  "esc_base_ms": OLD_MS,
                                  "age_min": 25}}          # the frozen value the board printed
dp7.provider_error_summary = lambda: {}
dp7.load_queue = lambda: {"items": []}
del dp7.write_pending                                       # use the real one
dp7.write_pending({"CODEX-1": "building X busy"})
emitted = json.load(open(DP7.PENDING))["pending"][0]
ok(emitted["age_min"] >= 1000 and emitted["since_local"] == "12:24:11",
   "MUST BITE: the board EMITS a derived age, not the stored one. `since 12:24:11` beside "
   "`age 25 min` was a line contradicting itself, and the contradiction was the only true thing on "
   "it — a rendered age must be computed at the moment of rendering")


# ------------------------------------------- releasing a hold that should never have been recorded
# A hold is durable BY DESIGN: an AUTH hold waits for a human, because retrying a refused key just
# refuses again. That makes a WRONG hold durable too — and on 2026-09-08 one was, when a worker's
# finding containing "until logout/401" was read as an authentication failure and muse-go-1 was taken
# out of service. A REQUEST FILE rather than an edit, because the daemon rewrites state.json every
# tick and a hand edit under a live daemon is a lost update waiting to happen.
DP8, dp8 = daemon(ITEM, False, [])
DP8.HOLDCLEAR_DIR = os.path.join(TMP, "holdclear"); os.makedirs(DP8.HOLDCLEAR_DIR, exist_ok=True)
dp8.server_down_since = time.time() - 60
dp8.state["muse"] = {"unhealthy": {"muse-go-1": {"class": "AUTH", "until": 0, "since": time.time(),
                                                 "why": "a 401 inside a finding"}}}
dp8._rm = lambda p: os.remove(p)
dp8.write_pending = lambda m: None
json.dump({"profile": "muse-go-1", "why": "the 401 was in the worker's own finding"},
          open(os.path.join(DP8.HOLDCLEAR_DIR, "muse-go-1.json"), "w"))
evs = []
dp8.emit = lambda *a: evs.append(a)
dp8.tick()
ok("muse-go-1" not in dp8.state["muse"].get("unhealthy", {}),
   "MUST BITE: a hold-clear request RELEASES the key — a wrong hold is as durable as a right one, "
   "and there was no sanctioned way to undo it")
ok(any(e[0] == "HOLD_CLEARED" for e in evs),
   "MUST BITE: and the release is an EVENT. A key silently returning to service is how nobody ever "
   "learns the hold was wrong")
ok(not os.listdir(DP8.HOLDCLEAR_DIR), "the request is consumed, so it cannot re-apply every tick")

DP9, dp9 = daemon(ITEM, False, [])
DP9.HOLDCLEAR_DIR = os.path.join(TMP, "holdclear2"); os.makedirs(DP9.HOLDCLEAR_DIR, exist_ok=True)
dp9.server_down_since = time.time() - 60
dp9.state["muse"] = {}
dp9._rm = lambda p: os.remove(p)
dp9.write_pending = lambda m: None
json.dump({"profile": "muse-go-2"}, open(os.path.join(DP9.HOLDCLEAR_DIR, "x.json"), "w"))
lg = []
dp9.log = lambda *a, **k: lg.append(a[0] if a else "")
dp9.tick()
ok(any("not held" in str(x) for x in lg),
   "clearing a hold that does not exist says so rather than passing silently")


# ---------------------------------------------------------------- the server comes back
logs, events, saved, spawned, boards, notified, escalated = [], [], [], [], [], [], []
DP2, dp2 = daemon(ITEM, True, spawned)
dp2.state["degraded"] = {"reason": "stale"}
try:
    dp2.tick()
except Exception:
    pass        # the healthy path runs far past this fixture; the mark is cleared before that
ok("degraded" not in dp2.state,
   "MUST BITE: a healthy tick CLEARS the mark — a condition that is no longer true must stop being "
   "reported, or the board learns to ignore it")

# ---------------------------------------------------------- three states, not one string
logs, events, saved, spawned, boards, notified, escalated = [], [], [], [], [], [], []
DP3, dp3 = daemon(ITEM, False, spawned)
dp3.server_down_since = time.time() - 3600
dp3.codex_spawn = lambda slot, item: dp3.refuse_spawn(slot, "staggered: waiting 14s behind the last spawn")
dp3.tick()
st = dp3.state.get("degraded", {}).get("codex", {})
ok("REFUSED C1" in st.get("CODEX-1", "") and "staggered" in st["CODEX-1"],
   "MUST BITE: a REFUSED spawn says so and gives the reason, where it used to render as 'idle: no "
   "eligible CODEX item' — the same string as an empty queue")

logs, events, saved, spawned, boards, notified, escalated = [], [], [], [], [], [], []
DP4, dp4 = daemon([], False, spawned)
dp4.server_down_since = time.time() - 3600
dp4.tick()
ok(dp4.state.get("degraded", {}).get("codex", {}).get("CODEX-1") == "idle: no CODEX item queued",
   "an EMPTY queue says empty, and says nothing about refusals")

src = open(os.path.join(HERE, os.pardir, "dispatcher.py")).read()
ok(src.index("codex_only_pass()") < src.index("self.save_state()\n            return")
   and src.index("self.write_pending(dict(") < src.index("self.save_state()\n            return"),
   "the codex pass AND the board write both happen BEFORE the early return, not after it")

print(f"\n{P} passed, {len(F)} failed")
for x in F: print("  FAILED:", x)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if F else 0)
