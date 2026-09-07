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
    dp.dry = False; dp.observing = False; dp.observe_logged = False
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
    dp.provider_error_summary = lambda: {}
    dp.reload_cfg = lambda: None
    dp.poll_lockwatch = lambda: None
    dp.codex_spawn = lambda slot, item: (spawned.append((slot, item["id"])), True)[1]
    return DP, dp


ITEM = [{"id": "C1", "status": "queued", "executor": "CODEX", "worktree": os.path.join(TMP, "wt"),
         "lane": "l", "title": "t"}]

# ---------------------------------------------------------------- the server is down
logs, events, saved, spawned, boards = [], [], [], [], []
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
for _lst in (logs, events, saved, spawned, boards): _lst.clear()
time.sleep(1.05)
dp.tick()
ok(not any(e[0] == "SERVER_DOWN" for e in events),
   "the one-shot EVENT correctly does not repeat — an event is a statement about a moment")
ok(dp.state.get("degraded", {}).get("for_s", 0) > first["for_s"],
   "MUST BITE: but the CONDITION is re-asserted with a fresh duration on every degraded tick. "
   "Sixteen hours of outage were announced once, yesterday, and were silent afterwards")
ok(spawned == [("CODEX-1", "C1")], "and codex keeps being dispatched on every degraded tick")

# ------------------------------------------------ THE BOARD, which froze for nineteen hours
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


# ---------------------------------------------------------------- the server comes back
logs, events, saved, spawned, boards = [], [], [], [], []
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
logs, events, saved, spawned, boards = [], [], [], [], []
DP3, dp3 = daemon(ITEM, False, spawned)
dp3.server_down_since = time.time() - 3600
dp3.codex_spawn = lambda slot, item: dp3.refuse_spawn(slot, "staggered: waiting 14s behind the last spawn")
dp3.tick()
st = dp3.state.get("degraded", {}).get("codex", {})
ok("REFUSED C1" in st.get("CODEX-1", "") and "staggered" in st["CODEX-1"],
   "MUST BITE: a REFUSED spawn says so and gives the reason, where it used to render as 'idle: no "
   "eligible CODEX item' — the same string as an empty queue")

logs, events, saved, spawned, boards = [], [], [], [], []
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
