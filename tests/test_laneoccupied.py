"""A lane under review must not take a second build (BOSS, measured 2026-09-06 22:45:45).

EXEC-G posted REPORT READY for B.014a.artifact-custody-r3 and IN THE SAME SECOND the daemon
dispatched B.014a.capability-descriptor to the same executor, the same lane/014a-assets and the same
worktree, while r3 was `reported` and ungated. The second build writes the lane head that r3's
report names, so a merge of r3 carries a half-built second item — and the gate's "sha on lane head"
row FAILS for a reason that is our scheduling, not the lane's. A finding that reads like the
candidate's fault is worse than no finding.

busy_worktrees() did not cover it: it tracks `dispatched` only, and r3 had moved past that. The
uncovered window is exactly the review, which is the longest part of an item's life.

Fixture is a COPY of the live queue.json at the defect, trimmed to the 014a lane, with
capability-descriptor put back to `queued` — its state at the instant the daemon picked it.

THE MUST-PASS matters as much as the must-bite here: the same lane also carries items in `held`, and
`held`/`merged` are BOSS's word that the earlier item is off the lane. A guard that blocked on those
would deadlock this lane permanently, and would look exactly like a working guard from the outside.
"""
import importlib.util, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
import fixtures                      # item 9: the shared state redirect
spec = importlib.util.spec_from_file_location("dlane", os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); sys.modules["dlane"] = D; spec.loader.exec_module(D)
fixtures.redirect_state(D)   # item 9: never the LIVE state dir
QF = os.path.join(HERE, "fixtures", "queue_lane_occupied.json")

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def daemon():
    d = D.Dispatcher.__new__(D.Dispatcher)          # no __init__: no server, no files, no threads
    d.state = {}
    d.events = []
    d.emit = lambda *f: d.events.append(f)
    d.dry = True
    # lane_occupied now consults the RUNNING mergegate list as well as the status (2026-09-07: a
    # `gating` item freed its lane while a gate was running proofs in that worktree). Stubbed empty
    # here, because a hermetic test must not depend on what happens to be running on the box — this
    # file's fixture ids are real ones, and a live gate for B.014a.artifact-custody-r3 turned the
    # `held`/`merged` MUST-PASS rows red while measuring nothing about the code. The live-gate rule
    # itself is measured in test_laneheld.py.
    d.running_gate_items = lambda: set()
    # eligible_item() now REFUSES a row whose worktree is missing from disk or claimed by another
    # unfinished item (2026-09-07: BOSS wrote both kinds of row). This fixture's paths are fictional
    # and its two items deliberately SHARE a lane, so both refusals fire here and the real escalate()
    # would run — which needs a live Dispatcher. Stubbed: the refusals are measured in
    # test_queueskip.py, and this file is about lane_occupied.
    d.escalate = lambda m: d.escalations.append(m)
    d.escalations = []
    d.worktree_claim = lambda q, it: None
    return d

def q():
    return json.load(open(QF))

CAP, R3 = "B.014a.capability-descriptor", "B.014a.artifact-custody-r3"

# 1. THE MUST-BITE: the exact 22:45:45 pair
d, queue = daemon(), q()
picked = d.eligible_item(queue, "EXEC-G")
check("MUST-BITE  the queued item is NOT dispatched while the lane's earlier item is `reported`",
      picked is None or picked["id"] != CAP, picked and picked["id"])
cap = [i for i in queue["items"] if i["id"] == CAP][0]
check("  it stays QUEUED — it is waiting on BOSS, not broken", cap["status"] == "queued", cap["status"])
check("  and the board says WHICH item holds the lane",
      any(R3 in b for b in cap.get("blocked_on", [])), cap.get("blocked_on"))
kinds = [e[0] for e in d.events]
check("MUST-BITE  exactly one LANE_OCCUPIED event", kinds.count("LANE_OCCUPIED") == 1, kinds)
evt = next((e for e in d.events if e[0] == "LANE_OCCUPIED"), None)
ev = " ".join(str(x) for x in evt) if evt else ""
check("  naming BOTH items", CAP in ev and R3 in ev, ev[:170])
check("  and the earlier item's state, so BOSS knows what to change", "reported" in ev, ev[:170])

# 2. no event storm: the daemon ticks every 5s and a review runs for tens of minutes
d.eligible_item(q(), "EXEC-G"); d.eligible_item(q(), "EXEC-G")
check("  three ticks still produce ONE event, not three",
      [e[0] for e in d.events].count("LANE_OCCUPIED") == 1, [e[0] for e in d.events])

# 3. THE MUST-PASS: held / merged do NOT occupy a lane
d, queue = daemon(), q()
for it in queue["items"]:
    if it["id"] == R3:
        it["status"] = "held"
picked = d.eligible_item(queue, "EXEC-G")
check("MUST-PASS  a lane whose earlier item is `held` DOES dispatch",
      picked is not None and picked["id"] == CAP, picked and picked["id"])
check("  and no LANE_OCCUPIED is emitted", "LANE_OCCUPIED" not in [e[0] for e in d.events], d.events)

d, queue = daemon(), q()
for it in queue["items"]:
    if it["id"] == R3:
        it["status"] = "merged"
picked = d.eligible_item(queue, "EXEC-G")
check("MUST-PASS  a lane whose earlier item is `merged` DOES dispatch",
      picked is not None and picked["id"] == CAP, picked and picked["id"])

# 4. `gated` holds the lane too — the gate is measuring that head right now
d, queue = daemon(), q()
for it in queue["items"]:
    if it["id"] == R3:
        it["status"] = "gated"
picked = d.eligible_item(queue, "EXEC-G")
check("a lane whose earlier item is `gated` is occupied", picked is None or picked["id"] != CAP,
      picked and picked["id"])

# 5. the guard is about the LANE, not the executor: a different executor must not slip past it
d, queue = daemon(), q()
for it in queue["items"]:
    if it["id"] == CAP:
        it["executor"] = "EXEC-B"
picked = d.eligible_item(queue, "EXEC-B")
check("MUST-BITE  a DIFFERENT executor on the same lane is blocked too",
      picked is None or picked["id"] != CAP, picked and picked["id"])

# 6. CONTROL: an unrelated lane is untouched — a guard that blocked everything passes every check above
d, queue = daemon(), q()
queue["items"].append({"id": "B.999.other", "status": "queued", "lane": "lane/999-other",
                       "worktree": "/tmp/voicepod-lane-999", "executor": "EXEC-B", "deps": []})
picked = d.eligible_item(queue, "EXEC-B")
check("CONTROL  an item on a DIFFERENT lane still dispatches",
      picked is not None and picked["id"] == "B.999.other", picked and picked["id"])

# 7. a placeholder worktree must not make every item share one "lane"
d, queue = daemon(), q()
for it in queue["items"]:
    it["lane"] = None
    it["worktree"] = "(as in Part 1.3)"
picked = d.eligible_item(queue, "EXEC-G")
check("CONTROL  the placeholder worktree is not treated as a shared lane",
      picked is not None and picked["id"] == CAP, picked and picked["id"])

print("\nLANE OCCUPIED " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
