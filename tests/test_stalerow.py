"""A pending row can be about work that is already FINISHED (2026-09-06 21:13).

CODEX-1's 02:40 REPORT_READY sat on the board eleven hours after its artifact had LANDED (capped)
on trunk, and BOSS dispatched a worker from it. Fixtures are the recorded pending.json row and the
recorded queue.json; hermetic, no API, no live state, nothing pruned."""
import importlib.util, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("dstale", os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); sys.modules["dstale"] = D; spec.loader.exec_module(D)
FX = os.path.join(HERE, "fixtures")
Q = json.load(open(os.path.join(FX, "queue.json")))
PEND = json.load(open(os.path.join(FX, "pending.json")))["pending"]

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def mk(ledger=None):
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.dry, d.once = True, True
    d.state = {"pending": {}, "handled": {}}
    d.events = []
    d.emit = lambda *f: d.events.append(tuple(str(x) for x in f))
    d.log = lambda *a: None
    d.c = lambda k, dflt=None: dflt
    d._ledger, d._ledger_at = (ledger or {}), 9e18
    return d

CODEX = [r for r in PEND if r["executor"] == "CODEX-1"][0]
by_id = {it["id"]: it for it in Q["items"]}

# the fixture has to be the trap's shape
check("fixture: the CODEX-1 row is a REPORT_READY carrying no item field",
      CODEX["kind"] == "REPORT_READY" and "item" not in CODEX)
check("fixture: its item is only recoverable from the msg_id (a log filename)",
      "B.010.ci-required-green" in CODEX["msg_id"], CODEX["msg_id"])
check("fixture: and that item really is finished in the queue",
      by_id["B.010.ci-required-green"]["status"] in ("landed", "merged", "done"),
      by_id["B.010.ci-required-green"]["status"])

# 1. the row is identified and tagged
d = mk()
it = d.item_of_pending(CODEX, Q)
check("the item is recovered from the msg_id", it and it["id"] == "B.010.ci-required-green", str(it and it["id"]))
check("  the longest matching id wins, not a prefix of it",
      it["id"] != "B.010.ci-required-green-if-guard")
why = d.stale_reason(CODEX, Q)
check("the row is tagged STALE", bool(why) and why.startswith("STALE"), str(why))
check("  naming the item and its state",
      "B.010.ci-required-green" in why and ("LANDED" in why or "MERGED" in why), str(why))

# 2. tag_stale: board tag, one event, NOT pruned
d = mk()
for r in PEND:
    d.state["pending"][r["session"]] = dict(r)
before = len(d.state["pending"])
status = {r["executor"]: "idle: no eligible CODEX item" for r in PEND}
d.tag_stale(d.state["pending"], Q, status)
kinds = [e[0] for e in d.events]
check("exactly one PENDING_STALE is emitted for the one stale row", kinds.count("PENDING_STALE") == 1, str(kinds))
check("  the board line carries the tag, with the item and its state, not a bare word",
      "[STALE" in status["CODEX-1"] and "B.010.ci-required-green" in status["CODEX-1"],
      status["CODEX-1"])
check("  the row is NOT pruned", len(d.state["pending"]) == before)
check("  and the row itself records why", d.state["pending"]["codex:CODEX-1"]["stale"].startswith("STALE"))
n = len(d.events)
d.tag_stale(d.state["pending"], Q, dict(status))
check("  a second tick does not re-emit", len(d.events) == n)

# 3. MUST-PASS: rows about unfinished work stay untagged.
# This one bit during calibration: a bare substring test matched the two-character item `Q2`
# inside `msg_072017ec1001VBvMlAe6TX60Q2` and declared EXEC-E's live ERROR row finished work.
d = mk()
others = {r["session"]: dict(r) for r in PEND if r["executor"] != "CODEX-1"}
d.state["pending"] = others
d.tag_stale(others, Q, {})
check("MUST-PASS  the three ERROR rows are not tagged stale",
      all(p.get("stale") is None for p in others.values()) and not d.events,
      str([p.get("stale") for p in others.values()]))

# ARTIFACTS ARE SHARED BY DESIGN (BOSS, 2026-09-06, after this tagged EXEC-A2's LIVE report).
# Every A.<x> audit shares its artifact with the B.<x> build it audits, and every rework shares its
# parent's. Once the item resolves, ITS OWN STATUS is the sole authority; the artifact must not be
# read, or live work is retired under a finished sibling.
FINAL = {"merged", "landed", "done"}
shared = {}
for _it in Q["items"]:
    shared.setdefault(_it.get("artifact"), []).append(_it)
pairs = [(a, v) for a, v in shared.items()
         if a and len(v) > 1 and any(x.get("status") in FINAL for x in v)
         and any(x.get("status") not in FINAL for x in v)]
check("fixture: the queue really does share artifacts across finished and unfinished items",
      len(pairs) >= 3, f"{len(pairs)} shared artifacts")

audit = next(i for i in Q["items"] if i["id"] == "A.012.sequence-ended-event")
check("fixture: A.012.sequence-ended-event is `reported` under a MERGED artifact",
      audit["status"] == "reported"
      and any(x["status"] == "merged" for x in shared[audit["artifact"]] if x["id"] != audit["id"]))
d = mk(ledger={"012.sequence-ended-event": "MERGED 6b542f7 2026-09-06"})
row = {"executor": "EXEC-X", "session": "s", "kind": "REPORT_READY", "msg_id": "x",
       "item": "A.012.sequence-ended-event", "artifact": "012.sequence-ended-event"}
check("MUST-PASS  a `reported` A-item whose artifact is MERGED stays untagged",
      d.stale_reason(row, Q) is None, str(d.stale_reason(row, Q)))

r3 = {"executor": "EXEC-Y", "session": "s2", "kind": "REPORT_READY", "msg_id": "y",
      "item": "B.010.ci-collection-floor", "artifact": "010.ci"}
check("fixture: B.010.ci-collection-floor is dispatched under a shared artifact",
      next(i for i in Q["items"] if i["id"] == "B.010.ci-collection-floor")["status"] == "dispatched")
d = mk(ledger={"010.ci": "HELD — awaiting rework"})
check("MUST-PASS  a dispatched item whose parent artifact is HELD stays untagged",
      d.stale_reason(r3, Q) is None)
d = mk(ledger={"010.ci": "LANDED ec56a115 2026-09-06"})
check("MUST-PASS  and it stays untagged even when that artifact has LANDED",
      d.stale_reason(r3, Q) is None, str(d.stale_reason(r3, Q)))

# the ledger remains the evidence of last resort for a row whose item is GONE from the queue
orphan = {"executor": "EXEC-Z", "session": "s3", "kind": "REPORT_READY", "msg_id": "z",
          "item": "B.010.renamed-away", "artifact": "010.route-authority-clock"}
d = mk(ledger={"010.route-authority-clock": "BUILT — awaiting gate"})
check("an orphaned row whose artifact is BUILT is not stale", d.stale_reason(orphan, Q) is None)
d2 = mk(ledger={"010.route-authority-clock": "LANDED 6b542f7 2026-09-06"})
check("  the same orphaned row IS stale once that artifact lands",
      (d2.stale_reason(orphan, Q) or "").startswith("STALE"), str(d2.stale_reason(orphan, Q)))
check("  and the tag says the item is gone and quotes the ledger",
      "no longer in the queue" in (d2.stale_reason(orphan, Q) or "")
      and "per the ledger" in (d2.stale_reason(orphan, Q) or ""))
d3 = mk(ledger={"010.route-authority-clock": "LANDED 6b542f7"})
check("  an orphaned row with NO artifact recorded is left alone, not guessed at",
      d3.stale_reason({"executor": "E", "session": "s4", "msg_id": "q", "item": "gone"}, Q) is None)

# 4. an unresolvable row is left alone rather than guessed at
d = mk()
check("a row that matches no item is not tagged",
      d.stale_reason({"executor": "NOBODY", "msg_id": "nothing-here"}, Q) is None)

print("\nSTALE ROW " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
