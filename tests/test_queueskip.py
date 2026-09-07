"""Why a queued item was passed over, and a wrong checkout claim refused by name.

BOSS, 2026-09-07: B.010.perf-regression-relative-check sat `queued` for six minutes with its
executor showing "idle:PROGRESS_STOP", pending empty, deps empty, worktree on disk, no gate running.
Nothing dispatched, and afterwards the cause could not be established AT ALL — eligible_item() skips
on three paths and only lane_occupied said anything, so the hand-dispatch cleared blocked_on and
nothing had been written down. The unprovability is the defect: the next stall costs the same six
minutes plus an investigation that ends in "likeliest".

BOSS also owns the row itself — that item pointed at another item's checkout, on a lane name that is
not a branch, and two hours earlier another row named a worktree that did not exist at all. Those are
errors in the row, not waits: a wait clears itself and an error does not. So they escalate.

Hermetic: temp CN, no server, no gate, no git."""
import importlib.util, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

root = tempfile.mkdtemp(prefix="qskip-")
os.makedirs(os.path.join(root, "test-logs", "driver", "gates"))
WT = os.path.join(root, "wt-a")
os.makedirs(WT)
os.environ["CN"] = root
spec = importlib.util.spec_from_file_location(
    "dqs" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); spec.loader.exec_module(D)

def dispatcher():
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {}
    d.events, d.escalations = [], []
    d.emit = lambda *f: d.events.append(f)
    d.escalate = lambda m: d.escalations.append(m)
    d.log = lambda m: None
    d.running_gate_items = lambda: set()
    d.dep_ok = lambda dep, q: (False, "MISSING")
    return d

def item(**kw):
    base = {"id": "B.010.follow-up", "status": "queued", "executor": "EXEC-B",
            "worktree": WT, "lane": "lane/follow-up"}
    base.update(kw); return base

def kinds(d): return [e[0] for e in d.events]

# ---------------------------------------------------------------- the wrong-claim refusals
d = dispatcher()
q = {"items": [item(worktree=os.path.join(root, "gone"))]}
check("MUST-BITE  a worktree that does not exist on disk is REFUSED, not dispatched",
      d.eligible_item(q, "EXEC-B") is None, d.eligible_item(q, "EXEC-B"))
check("  the skip is spoken, naming the path", kinds(d) == ["QUEUED_SKIPPED"]
      and "does not exist on disk" in str(d.events[0][-1]), d.events)
check("MUST-BITE  and it ESCALATES — an error in the row does not clear itself the way a wait does",
      len(d.escalations) == 1 and "will NOT be dispatched" in d.escalations[0], d.escalations)

d = dispatcher()
q = {"items": [item(), {"id": "B.010.earlier", "status": "gating", "worktree": WT,
                        "lane": "lane/earlier", "dispatched_to": "EXEC-B"}]}
check("MUST-BITE  a row claiming ANOTHER unfinished item's worktree is refused",
      d.eligible_item(q, "EXEC-B") is None)
check("  and the reason NAMES the item that holds it, with its status",
      "B.010.earlier" in str(d.events[0][-1]) and "gating" in str(d.events[0][-1]), d.events[0][-1])
check("  escalated too — BOSS wrote both of these rows and asked not to be able to miss it",
      len(d.escalations) == 1, d.escalations)

for st in ("merged", "held", "done", "landed"):
    d = dispatcher()
    q = {"items": [item(), {"id": "B.010.earlier", "status": st, "worktree": WT}]}
    check(f"CONTROL  a {st} item does NOT hold the worktree — the row is fine and dispatches",
          (d.eligible_item(q, "EXEC-B") or {}).get("id") == "B.010.follow-up", st)

d = dispatcher()
q = {"items": [item(worktree="(as in Part 1.3)")]}
check("the placeholder worktree is not a claim, and is not refused",
      (d.eligible_item(q, "EXEC-B") or {}).get("id") == "B.010.follow-up")

# ---------------------------------------------------------------- the silent skips now speak
d = dispatcher()
q = {"items": [item(deps=["B.010.something"])]}
check("MUST-BITE  a deps skip is SPOKEN — it was silent, and that silence cost six minutes",
      d.eligible_item(q, "EXEC-B") is None and kinds(d) == ["QUEUED_SKIPPED"]
      and "deps" in str(d.events[0][-1]), (kinds(d), d.events[-1:]))

d = dispatcher()
d.busy_worktrees = lambda q: {WT}
q = {"items": [item()]}
check("MUST-BITE  a `worktree busy` skip is spoken too", d.eligible_item(q, "EXEC-B") is None
      and "busy" in str(d.events[0][-1]), d.events)
check("  and it does NOT escalate — a busy worktree is a wait, and it clears itself",
      d.escalations == [], d.escalations)

# once per item and reason, not once per tick
d = dispatcher()
q = {"items": [item(deps=["x"])]}
for _ in range(6):
    d.eligible_item(q, "EXEC-B")
check("MUST-BITE  six ticks on the same item and reason speak ONCE",
      len([e for e in d.events if e[0] == "QUEUED_SKIPPED"]) == 1, kinds(d))
d.state["queued_skip"] = {}
d.dep_ok = lambda dep, q: (True, "")
d.busy_worktrees = lambda q: {WT}
d.eligible_item(q, "EXEC-B")
check("  a CHANGED reason for the same item speaks again — dedupe must not silence news",
      len([e for e in d.events if e[0] == "QUEUED_SKIPPED"]) == 2,
      [e[-1][:40] for e in d.events])

# ---------------------------------------------------------------- the PROGRESS_STOP feed gap
check("MUST-BITE  PROGRESS_STOP and STUCK are still not in FEED_KINDS — the gap this fix covers",
      "PROGRESS_STOP" not in D.FEED_KINDS and "STUCK" not in D.FEED_KINDS, D.FEED_KINDS)
src = open(os.path.join(HERE, os.pardir, "dispatcher.py"), errors="ignore").read()
i = src.find('if kind == "PROGRESS_STOP":')
j = src.find('n = int(self.state["auto"].get(sid, 0))', i)
seg = src[i:j]
check("MUST-BITE  an executor holding NO item is fed at PROGRESS_STOP instead of auto-continued",
      "self.feed(" in seg and "current_item" in seg, seg[:200])
check("  and the check sits BEFORE the counter is read, so the turn it excuses is not counted",
      i < j and "self.feed(" in seg)

shutil.rmtree(root, ignore_errors=True)
print(("FAILED: " + ", ".join(fails)) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
