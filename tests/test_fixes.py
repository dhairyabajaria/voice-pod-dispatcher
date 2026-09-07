"""Unit tests for the 2026-09-06 dispatcher fixes, run against COPIES of tonight's live
queue.json / pending.json / roster.json (fx/). Nothing here touches test-logs/driver."""
import importlib.util, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
FX = os.path.join(HERE, "fixtures")
TMP = tempfile.mkdtemp(prefix="dispfix-")

spec = importlib.util.spec_from_file_location("dstaged", os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); sys.modules["dstaged"] = D; spec.loader.exec_module(D)
D.HOLD_DIR = os.path.join(TMP, "hold"); os.makedirs(D.HOLD_DIR, exist_ok=True)

Q = json.load(open(os.path.join(FX, "queue.json")))
PEND = json.load(open(os.path.join(FX, "pending.json")))
ROSTER = json.load(open(os.path.join(FX, "roster.json")))
fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def mk(ledger=None):
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.dry = True; d.once = True
    d.state = {"pending": {}, "handled": {}, "parks": {}}
    d.roster = dict(ROSTER["executors"])
    d.cfg = ROSTER
    d._ledger = ledger if ledger is not None else {}; d._ledger_at = time.time()
    d.events = []
    d.emit = lambda *f: d.events.append(tuple(str(x) for x in f))
    d.escalations = []
    d.escalate = lambda t: d.escalations.append(t)
    d.notify_owner = lambda *a: None
    d.log = lambda *a: None
    d.c = lambda k, dflt=None: ROSTER.get(k, dflt)
    return d

# ---------- FIX 1: dep_ok ----------
ids = {it["id"] for it in Q["items"]}
arts = {it.get("artifact") for it in Q["items"]}
itemid_deps = sorted({d for it in Q["items"] for d in it.get("deps", []) if d in ids})
check("fixture really contains item-id deps", len(itemid_deps) >= 2, str(itemid_deps[:3]))

d = mk(ledger={"010.route-authority-clock": "MERGED 6b542f7"})
# (a) an item-id dep whose queue row is merged/landed resolves TRUE — old code returned False
by_id = {it["id"]: it for it in Q["items"]}
dep = "B.011.port-row-decoding"
ok, why = d.dep_ok(dep, Q)
check("item-id dep resolves via the queue row",
      ok is (by_id[dep].get("status") in ("merged", "landed")), f"{dep} status={by_id[dep].get('status')} why={why}")
check("...and the OLD behaviour was a silent block",
      not any(str(v).startswith(("LANDED","MERGED")) for k, v in d._ledger.items() if k == dep))

# (b) an item-id dep whose artifact IS merged in the ledger resolves TRUE even if the row lags
dep2 = "B.010.route-authority-clock"   # queue status 'reported', artifact merged in the ledger above
ok2, why2 = d.dep_ok(dep2, Q)
check("item-id dep resolves via its artifact's ledger status", ok2 is True, why2)

# (c) an unknown dep blocks AND is announced exactly once per hour
d.events.clear()
ok3, why3 = d.dep_ok("Z.999.does-not-exist", Q)
ok4, _ = d.dep_ok("Z.999.does-not-exist", Q)
check("unknown dep blocks", ok3 is False and ok4 is False, why3)
check("unknown dep emits DEP_UNRESOLVABLE once",
      [e[0] for e in d.events].count("DEP_UNRESOLVABLE") == 1, str(d.events))

# (d) eligible_item labels it so the board cannot read it as an ordinary wait
q2 = {"items": [{"id": "X", "status": "queued", "artifact": "x", "deps": ["Z.999.does-not-exist"]}]}
d.state["dep_unresolvable"] = {}
d.busy_worktrees = lambda q: set()
check("eligible_item returns nothing and labels the block", d.eligible_item(q2, "EXEC-B") is None
      and q2["items"][0]["blocked_on"] == ["Z.999.does-not-exist [DEP_UNRESOLVABLE]"],
      str(q2["items"][0].get("blocked_on")))

# (e) an ordinary artifact dep still behaves exactly as before
d2 = mk(ledger={"013.agent-release-aggregate": "MERGED abc"})
check("artifact dep unchanged (merged -> ok)", d2.dep_ok("013.agent-release-aggregate", Q)[0] is True)
d3 = mk(ledger={"013.agent-release-aggregate": "REPORTED"})
check("artifact dep unchanged (not merged -> blocked, resolvable)",
      d3.dep_ok("013.agent-release-aggregate", Q) == (False, "ledger:REPORTED"))

# ---------- FIX 2b: rows for sessions not in the roster ----------
d = mk()
for row in PEND["pending"]:
    d.state["pending"][row["session"]] = dict(row)
live = set(d.roster.values())
stale = [s for s in d.state["pending"] if not s.startswith("codex:") and s not in live]
check("tonight's rows include sessions absent from the roster", len(stale) == 3, str(len(stale)))
pending = d.state["pending"]
d.prune_offroster(pending)
check("first tick only records: nothing pruned yet (one bad discovery must not cost a row)",
      len(pending) == 4 and not d.events, str(len(pending)))
for sid in list(d.state["roster_seen"]):          # age the sightings past the grace window
    if sid not in live: d.state["roster_seen"][sid] -= 601
d.prune_offroster(pending)
check("prune leaves only the codex row", list(pending) == ["codex:CODEX-1"], str(list(pending)))
check("prune emits one PENDING_PRUNED per stale row",
      [e[0] for e in d.events].count("PENDING_PRUNED") == 3)
# a roster that failed to load must never prune anything
d5 = mk()
for row in PEND["pending"]: d5.state["pending"][row["session"]] = dict(row)
d5.c = lambda k, dflt=None: ({} if k == "executors" else dflt)
d5.roster = {}
d5.prune_offroster(d5.state["pending"])
check("an empty explicit roster prunes nothing", len(d5.state["pending"]) == 4 and not d5.events)

# ---------- FIX 2c: DEAD classification ----------
d = mk()
row = dict([r for r in PEND["pending"] if r["executor"] == "EXEC-A"][0])
check("EXEC-A's payload is recognised as a non-retryable 400", D.Dispatcher._nonretryable_400(row) is True)
check("a REPORT_READY row is not", D.Dispatcher._nonretryable_400(
      [r for r in PEND["pending"] if r["kind"] == "REPORT_READY"][0]) is False)
d.state["pending"]["s1"] = row
d.roster = {"EXEC-A": "s1"}          # in-roster: the DEAD path applies
d.hold_reason = lambda n: None
d.age_and_escalate(d.state["pending"])
check("persistent 400 becomes DEAD", row.get("dead") is True and row["stall_class"] == "DEAD")
check("...emits EXECUTOR_DEAD once and escalates once",
      [e[0] for e in d.events].count("EXECUTOR_DEAD") == 1 and len(d.escalations) == 1)
n_esc, n_ev = len(d.escalations), len(d.events)
d.age_and_escalate(d.state["pending"])
check("...and never escalates again", len(d.escalations) == n_esc and len(d.events) == n_ev)
check("board label says DEAD, not 'waiting for BOSS'",
      "DEAD" in D.Dispatcher._idle_label(row, "ERROR", row["since_ms"])
      and "waiting for BOSS" not in D.Dispatcher._idle_label(row, "ERROR", row["since_ms"]),
      D.Dispatcher._idle_label(row, "ERROR", row["since_ms"]))
# an OFF-ROSTER row is on its way out and must not escalate at all
d6 = mk(); r6 = dict([r for r in PEND["pending"] if r["executor"] == "EXEC-A"][0])
d6.state["pending"][r6["session"]] = r6; d6.hold_reason = lambda n: None
d6.age_and_escalate(d6.state["pending"])
check("an off-roster row is neither escalated nor marked dead",
      not r6.get("dead") and not d6.escalations and r6["stall_class"] == "OFF_ROSTER", str(d6.escalations))

# a retryable/other error is untouched by the DEAD path
d4 = mk(); other = dict(row); other["excerpt"] = '{"statusCode": 503, "isRetryable": true}'
other.update(escalated=2, esc_count=1, last_esc_min=0, dead=None)
d4.state["pending"]["s2"] = other; d4.roster = {"EXEC-A": "s2"}; d4.hold_reason = lambda n: None
d4.boss_activity_since = lambda ms: ("UNSEEN", "test")
d4.undelivered_answer = lambda n: None
d4.age_and_escalate(d4.state["pending"])
check("a retryable error is NOT marked dead", not other.get("dead"))

# ---------- FIX 2a: an ERROR row superseded by a later clean message ----------
d = mk()
row = dict([r for r in PEND["pending"] if r["executor"] == "EXEC-E"][0])
sid = row["session"]; d.state["pending"][sid] = row
check("same message id -> no prune (it is still the live error)",
      d.prune_superseded_error(d.state["pending"], "EXEC-E", sid, "ERROR", row["msg_id"]) is False)
check("a later ERROR -> no prune", d.prune_superseded_error(d.state["pending"], "EXEC-E", sid, "ERROR", "msg_newer") is False)
check("a later clean message -> pruned",
      d.prune_superseded_error(d.state["pending"], "EXEC-E", sid, "QUESTION", "msg_newer") is True
      and sid not in d.state["pending"])
check("...and it is announced as CLEARED", [e[0] for e in d.events].count("CLEARED") == 1)
d.events.clear()
check("a REPORT_READY row is left alone",
      d.prune_superseded_error(d.state["pending"], "CODEX-1", "codex:CODEX-1", "IDLE", "x") is False)

# ---------- FIX 3: .feed hold release ----------
d = mk(); d.dry = False   # release_hold is a no-op in dry mode by design; HOLD_DIR is redirected to TMP
open(os.path.join(D.HOLD_DIR, "EXEC-B.feed"), "w").write("paused by circuit breaker\n")
open(os.path.join(D.HOLD_DIR, "EXEC-B"), "w").write("BOSS hold\n")
d.release_hold("EXEC-B")
check("release_hold removes the .feed hold", not os.path.exists(os.path.join(D.HOLD_DIR, "EXEC-B.feed")))
check("release_hold still removes the BOSS hold", not os.path.exists(os.path.join(D.HOLD_DIR, "EXEC-B")))
check("release_hold emits HOLD_RELEASED", [e[0] for e in d.events].count("HOLD_RELEASED") == 1, str(d.events))
check("release_hold resets the breaker window", d.state["parks"]["EXEC-B"] == [])
# and a held executor is visible on the board
open(os.path.join(D.HOLD_DIR, "EXEC-C.feed"), "w").write("x\n")
labels = {k: v + (" [FEED HELD]" if os.path.exists(os.path.join(D.HOLD_DIR, f"{k}.feed")) else "")
          for k, v in {"EXEC-B": "building X busy", "EXEC-C": "idle:QUESTION"}.items()}
check("board shows FEED HELD for a held executor",
      labels["EXEC-C"].endswith("[FEED HELD]") and "FEED HELD" not in labels["EXEC-B"], str(labels))

shutil.rmtree(TMP, ignore_errors=True)
print(("\nALL PASS" if not fails else "\nFAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
