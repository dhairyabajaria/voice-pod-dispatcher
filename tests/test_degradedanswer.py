"""ITEM 6: BOSS's answers to CODEX questions were stranded behind the opencode health check.

BOSS, 2026-09-10 21:48, REOPENING his own closure of item 3 six hours after making it — and the
reopening is the finding. At 19:59 he closed item 3 on the strength of `test_serverdown.py`: the
degraded tick dispatches codex, ages rows, escalates, writes the board and writes the heartbeat, so
"the early return drops nothing". It drops something. `apply_answer_requests(q)` has exactly ONE
caller, `dispatcher.py:2801`, and it sits AFTER the degraded return. He filed a valid answer for the
parked `B.B2.renumber-015a-scheduler-252` at 21:46 and three ticks passed with the file untouched and
no ANSWERED or ANSWER_REFUSED event — measured, not reasoned.

**A green licenses only the paths it walks.** test_serverdown.py proved five things and was read as
proving a sixth. That is `negative-control-scope-lesson` applied to a whole proof set.

AND A CODEX QUESTION IS ANSWERED BY BOSS, NOT BY THE OPENCODE SERVER. Holding it behind that server's
health is the identical mistake as holding codex dispatch behind it — the one the degraded path was
built to undo, arriving a second time through a different door.

Hermetic: temp state dir, no server, no network, no worktree, no codex binary.
"""
import importlib.util, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixtures                      # item 9: the shared state redirect
P, F = 0, []
def ok(c, w):
    global P
    if c: P += 1; print(f"PASS {w}")
    else: F.append(w); print(f"FAIL {w}")

TMP = tempfile.mkdtemp(prefix="degansw-")


def daemon(items, *, reqs=(), spawned=None):
    spec = importlib.util.spec_from_file_location(
        "dspa" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
    DP = importlib.util.module_from_spec(spec); spec.loader.exec_module(DP)
    fixtures.redirect_state(DP)   # item 9: never the LIVE state dir
    st = os.path.join(TMP, "s" + str(time.time_ns())); os.makedirs(st)
    DP.STATE_DIR = st
    DP.HEARTBEAT = os.path.join(st, "heartbeat")
    DP.QUEUE = os.path.join(st, "queue.json"); DP.QUEUE_LOCK = os.path.join(st, "queue.lock")
    DP.STOP = os.path.join(st, "STOP"); DP.OBSERVE = os.path.join(st, "OBSERVE")
    DP.QUEUE_HOLD = os.path.join(st, "HOLD"); DP.EVENTS = os.path.join(st, "events.log")
    DP.CODEX_DIR = st
    DP.ITEMS_DIR = os.path.join(st, "items"); os.makedirs(DP.ITEMS_DIR)
    open(os.path.join(DP.ITEMS_DIR, "C1.md"), "w").write("brief")
    DP.ANSWER_REQ_DIR = os.path.join(st, "answerreq"); os.makedirs(DP.ANSWER_REQ_DIR)
    DP.GATE_REQ_DIR = os.path.join(st, "gatereq"); os.makedirs(DP.GATE_REQ_DIR)
    DP.GATES_DIR = os.path.join(st, "gates"); os.makedirs(DP.GATES_DIR)
    DP.HOLD_DIR = os.path.join(st, "holds"); os.makedirs(DP.HOLD_DIR)
    DP.HOLDCLEAR_DIR = os.path.join(st, "holdclear"); os.makedirs(DP.HOLDCLEAR_DIR)
    DP.PENDING = os.path.join(st, "pending.json")
    json.dump({"items": items}, open(DP.QUEUE, "w"))
    for i, r in enumerate(reqs):
        json.dump(r, open(os.path.join(DP.ANSWER_REQ_DIR, f"r{i}.json"), "w"))

    def http(method, path, **kw):
        raise OSError("connection refused")     # opencode is DOWN for every check in this file
    DP.http = http

    dp = DP.Dispatcher.__new__(DP.Dispatcher)
    dp.dry = False; dp.once = False; dp.observing = False; dp.observe_logged = False
    dp.cfg = {"codex_slots": 1, "codex_bin": "/x/codex", "codex_spawn_stagger_seconds": 0,
              "max_dispatch_per_tick": 2, "codex_model": "m", "codex_effort": "medium"}
    dp.state = {"pending": {}, "parks": {}, "codex": {}, "server_down_emitted": False}
    dp._spawn_refusal = {}
    dp.server_down_since = time.time() - 3600
    dp.paused_since = None; dp.paused_logged = False
    dp.last_resync = time.time(); dp.roster = {}
    dp.log = lambda *a, **k: logs.append(a[0] if a else "")
    dp.emit = lambda *a: events.append(a)
    dp.escalate = lambda t: escalated.append(t)
    dp.notify_owner = lambda *a, **k: None
    dp.save_state = lambda: None
    dp.write_pending = lambda m: None
    dp.hold_reason = lambda who: None
    dp.undelivered_answer = lambda who: None
    dp.boss_activity_since = lambda ms: ("NO_ACTIVITY", "nothing since")
    dp.provider_error_summary = lambda: {}
    dp.reload_cfg = lambda: None
    dp.poll_lockwatch = lambda: None
    dp.poll_sessions = lambda: None
    dp.codex_spawn = lambda slot, item: ((spawned if spawned is not None else []).append(item["id"])
                                         or True)
    dp._rm = lambda p: os.remove(p)
    return DP, dp


PARKED = [{"id": "C1", "status": "parked", "executor": "CODEX", "dispatched_to": "CODEX-1",
           "worktree": os.path.join(TMP, "wt"), "lane": "l", "title": "t"}]
ANSWER = {"item": "C1", "text": "yes, take 252 on lane 8dbe3192", "relayed_by_hand": True}

# ------------------------------------------------------------------ POSITIVE CONTROL
logs, events, escalated = [], [], []
DP, dp = daemon([dict(PARKED[0])], reqs=[ANSWER])
dp.tick()
ok(any(e[0] == "ANSWERED" for e in events),
   "PC MUST BITE: a degraded tick CONSUMES an answer request for a parked CODEX row and emits "
   "ANSWERED. Three real ticks passed on 2026-09-10 with BOSS's answer file untouched, because the "
   "only caller sits behind the opencode early return")
q_after = json.load(open(DP.QUEUE))["items"][0]
ok(q_after["status"] != "parked",
   "PC MUST BITE: and the ROW MOVES — an ANSWERED event over a row still parked is the side-channel "
   "drift this whole mechanism was built to stop")
ok(not os.listdir(DP.ANSWER_REQ_DIR),
   "  the request file is consumed, so it cannot re-apply on every degraded tick")

# ------------------------------------------------------------------ NEGATIVE CONTROL
logs, events, escalated = [], [], []
DP, dp = daemon([dict(PARKED[0], status="dispatched")], reqs=[ANSWER])
dp.tick()
ok(any(e[0] == "ANSWER_REFUSED" for e in events) and not any(e[0] == "ANSWERED" for e in events),
   "NC MUST BITE: the same file against a row that is NOT parked is REFUSED, on the degraded tick "
   "exactly as on the healthy one. Reaching the consumer is not permission to relax it")
ok(json.load(open(DP.QUEUE))["items"][0]["status"] == "dispatched",
   "  and the row is UNCHANGED — a refusal that moved something is not a refusal")
ok(any("dispatched, not parked" in str(e) for e in events),
   "  and the refusal names what the item IS, so BOSS is not sent back to the queue file")

# ------------------------------------------------- an answered row goes back to WORK, not to the pool
# MY OWN RED, AND THE PRODUCT WAS RIGHT. The first version of this check asserted that an un-parked
# row is DISPATCHED in the same tick, and it failed: an answered row returns to `dispatched` (the
# executor was already working on it and has now been told the answer), so it is deliberately NOT
# eligible for a fresh spawn. Asserting the convenient thing would have pinned a defect — a
# re-dispatch here means the same item running twice. The ordering claim belongs on the SOURCE, and
# it is asserted there at the bottom of this file.
logs, events, escalated, spawned = [], [], [], []
DP, dp = daemon([dict(PARKED[0])], reqs=[ANSWER], spawned=spawned)
dp.tick()
ok(json.load(open(DP.QUEUE))["items"][0]["status"] == "dispatched" and spawned == [],
   "MUST BITE: an answered row returns to DISPATCHED and is NOT re-spawned — the executor already "
   "holds it and has just been told the answer. A second spawn would run the same item twice")

# ...unless the executor has since been given something else, and then it must not be double-booked.
logs, events, escalated, spawned = [], [], [], []
DP, dp = daemon([dict(PARKED[0]),
                 {"id": "C9", "status": "dispatched", "executor": "CODEX",
                  "dispatched_to": "CODEX-1", "worktree": os.path.join(TMP, "wt9"),
                  "lane": "l", "title": "t"}], reqs=[ANSWER], spawned=spawned)
dp.tick()
row = next(i for i in json.load(open(DP.QUEUE))["items"] if i["id"] == "C1")
ok(row["status"] == "queued" and row.get("executor") == "CODEX-1" and not row.get("dispatched_to"),
   "MUST BITE: a question frees the executor and the dispatcher fills the gap, so by the time the "
   "answer lands the slot may hold something else. The row goes back QUEUED AND PINNED rather than "
   "restored to dispatched — restoring it would leave CODEX-1 holding two rows at once, and this "
   "guard has to hold on the degraded path too, not only where it was written")
ok(any("NOT restored" in str(e) for e in events),
   "  and the event says the restore was declined and why, rather than moving the row silently")

# --------------------------------------------------- a malformed request must not cancel the codex pass
logs, events, escalated, spawned = [], [], [], []
DP, dp = daemon([{"id": "C2", "status": "queued", "executor": "CODEX",
                  "worktree": os.path.join(TMP, "wt"), "lane": "l", "title": "t"}],
                reqs=[{"item": "", "text": ""}], spawned=spawned)
dp.tick()
ok(spawned == ["C2"],
   "MUST BITE: a malformed answer request does NOT cancel codex dispatch. Sharing one try/except "
   "would let a single bad file re-create the sixteen-hour outage through a different door")
ok(any(e[0] == "ANSWER_REFUSED" for e in events), "  and the bad file is still refused, not ignored")

# -------------------------------------- and it raises: the consumer is isolated from the codex pass
logs, events, escalated, spawned = [], [], [], []
DP, dp = daemon([{"id": "C3", "status": "queued", "executor": "CODEX",
                  "worktree": os.path.join(TMP, "wt"), "lane": "l", "title": "t"}], spawned=spawned)
def boom(q):
    raise RuntimeError("answerreq exploded")
dp.apply_answer_requests = boom
dp.tick()
ok(spawned == ["C3"] and any("answer requests failed" in l for l in logs),
   "MUST BITE: if the answer consumer RAISES, codex still dispatches and the failure is logged. A "
   "sensor that takes the actuator with it is worse than the gap it replaced")

# ---------------------------------------------------------------------- no request, no noise
logs, events, escalated = [], [], []
DP, dp = daemon([dict(PARKED[0])])
dp.tick()
ok(not any(e[0] in ("ANSWERED", "ANSWER_REFUSED") for e in events),
   "CONTROL: with no request file the degraded tick emits nothing about answers — a consumer that "
   "speaks when there is nothing to consume trains the reader to ignore it")

# ------------------------------------------------------------------ SOURCE ORDER, as BOSS asked
src = open(os.path.join(HERE, os.pardir, "dispatcher.py")).read()
ret = src.index("self.save_state()\n            return")
ok(src.index("def codex_only_pass") < ret,
   "the codex-only pass is defined before the degraded return it serves")
body_start = src.index("def codex_only_pass")
body_end = src.index("    # -- one tick", body_start)
body = src[body_start:body_end]
ok("self.apply_answer_requests(q)" in body,
   "MUST BITE (BOSS's source-order ask): the ANSWER CONSUMER is inside codex_only_pass, which runs "
   "BEFORE the degraded early return — not after it, where its only other caller sits")
def _before(hay, a, b):
    """a appears before b, and a MISSING term is a clean False rather than a ValueError.

    `str.index` RAISES when the term is absent, so a mutation that deletes the call under test
    ends this file in a traceback instead of a red — and a crash is neither a catch nor a miss.
    Measured while proving these very checks: removing `apply_answer_requests` from the degraded
    pass gave 11 clean reds and then a `ValueError: substring not found`.
    """
    return a in hay and b in hay and hay.index(a) < hay.index(b)

ok(_before(body, "self.apply_answer_requests(q)", "self.codex_tick("),
   "  and it precedes codex_tick inside that pass, asserted on the source and not only on behaviour")
ok(_before(body, "self.queue_lock()", "self.apply_answer_requests(q)"),
   "  and it runs under the queue lock this pass already holds — a second lock would be a deadlock, "
   "not a guard")


# ============================== ITEM 7: the other two things stranded by position ==================
# BOSS, 2026-09-10 22:12, on the shape rather than the instance. The early return is DEFINED by what
# it protects (opencode) and SCOPED by position (everything after it). `apply_gate_requests` and
# `collect_gates` touch no `http(`, no roster and no session — a merge gate is a subprocess — so they
# were held back by nothing but where they sat in the function.

# ---------------------------------------------------- PC: a hand-named gate LAUNCHES on a degraded tick
logs, events, escalated = [], [], []
DP, dp = daemon([{"id": "G1", "status": "reported", "executor": "CODEX",
                  "dispatched_to": "CODEX-1", "worktree": os.path.join(TMP, "wt"),
                  "lane": "l", "title": "t"}])
json.dump({"item": "G1"}, open(os.path.join(DP.GATE_REQ_DIR, "g.json"), "w"))
launched = []
dp.launch_gate = lambda it, no_box=False, source="": (launched.append((it["id"], source)), "head")[1:]
dp.state["autogate"] = {}
dp.tick()
ok(launched and launched[0][0] == "G1",
   "PC MUST BITE (item 7): a hand-named gate request LAUNCHES on a degraded tick. A merge gate is a "
   "subprocess and needs nothing from the opencode server — it was stranded by position alone, and "
   "BOSS's `dispatcherctl.sh gate` has been silently doing nothing for 30 hours of outage")
ok(not os.listdir(DP.GATE_REQ_DIR),
   "  and the request file is consumed, so it cannot re-launch on every tick")

# NC: a request for an item in no state to be gated is REFUSED on the degraded tick, as on the healthy one
logs, events, escalated = [], [], []
DP, dp = daemon([{"id": "G2", "status": "queued", "executor": "CODEX",
                  "worktree": os.path.join(TMP, "wt"), "lane": "l", "title": "t"}])
json.dump({"item": "G2"}, open(os.path.join(DP.GATE_REQ_DIR, "g.json"), "w"))
launched = []
dp.launch_gate = lambda it, no_box=False, source="": (launched.append(it["id"]), "head")[1:]
dp.state["autogate"] = {}
dp.tick()
ok(not launched and any(e[0] == "MANUAL_GATE_REFUSED" for e in events),
   "NC MUST BITE: a gate request for a row that is not gateable is REFUSED on the degraded tick "
   "exactly as on the healthy one. Reaching the consumer is not permission to relax it")

# ---------------------------------------------------- PC: a FINISHED gate is COLLECTED on a degraded tick
logs, events, escalated = [], [], []
DP, dp = daemon([{"id": "G3", "status": "reported", "executor": "CODEX",
                  "dispatched_to": "CODEX-1", "worktree": os.path.join(TMP, "wt"),
                  "lane": "l", "title": "t"}])
collected = []
dp.collect_gates = lambda q: collected.append(len(q["items"]))
dp.tick()
ok(collected == [1],
   "PC MUST BITE (item 7): finished gates are COLLECTED on a degraded tick. Launching a gate nobody "
   "collects is worse than not launching it — the findings never reach anyone, which is the "
   "'message and state are one action' failure with a subprocess in the middle")

# --------------------------------- each of the three has its OWN guard: one raising must not stop the others
logs, events, escalated, spawned = [], [], [], []
DP, dp = daemon([{"id": "C7", "status": "queued", "executor": "CODEX",
                  "worktree": os.path.join(TMP, "wt"), "lane": "l", "title": "t"}], spawned=spawned)
def bang(*a, **k):
    raise RuntimeError("gate requests exploded")
dp.apply_gate_requests = bang
collected = []
dp.collect_gates = lambda q: collected.append(1)
dp.tick()
ok(spawned == ["C7"] and collected == [1] and any("gate requests failed" in l for l in logs),
   "MUST BITE: one try/except PER FUNCTION. A raising gate consumer must not cancel the answer "
   "consumer, the gate collection or codex dispatch — three functions behind one guard is one "
   "function's worth of protection")

# ---------------------------------------------------------------- source order, on the same helper
ok(_before(body, "self.apply_gate_requests(q)", "self.apply_answer_requests(q)"),
   "item 7 source order: gate requests precede answer requests, as on the healthy path")
ok(_before(body, "self.apply_answer_requests(q)", "self.collect_gates(q)"),
   "  and collection comes after both, so a gate launched by this tick is collected by a later one "
   "rather than half-collected by this one")
ok(_before(body, "self.queue_lock()", "self.apply_gate_requests(q)"),
   "  and all of it runs under the queue lock this pass already holds")
ok("self.clear_feed_holds_for_answered" not in body,
   "MUST BITE: clear_feed_holds_for_answered is NOT here, and its absence is deliberate — it "
   "REMOVES hold/<EXEC>.feed, and the owner's stop currently depends on hold/CODEX-1.feed staying. "
   "Moving it would have built an automatic path to deleting the hold BOSS ordered kept. Held "
   "pending his ruling; see the CHANGELOG")

print(f"\n{P} passed, {len(F)} failed")
for x in F: print("  FAILED:", x)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if F else 0)
