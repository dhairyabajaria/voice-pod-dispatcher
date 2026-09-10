"""A lane stays occupied while anyone is reading or writing its head.

BOSS, 2026-09-07: LANE_OCCUPIED released when B.014a.artifact-custody-r3 moved to `gating`, and the
daemon dispatched EXEC-G onto lane/014a-assets while gate 46103 was running platform proofs in that
exact worktree. An edit mid-gate corrupts the proof run and the head row silently — the gate measures
a tree that is changing under it and then reports a verdict about a sha that no longer describes what
it tested. Nothing in that failure looks like a failure.

`gating` is the status BOSS's own hand-launched gates write; the list only knew `reported` and
`gated`. `rework` was missing for the same reason — the executor is actively writing the lane.

The status is a record, so the running mergegate list is checked as well: a crashed gate leaves
`gating` behind, and a gate launched before the status was written runs against a lane the queue
still calls something else.

Hermetic: temp CN, no server, no gate, no box — the process probe is stubbed."""
import importlib.util, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); import fixturelog as FL
import fixtures                      # item 9: the shared state redirect

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

root = tempfile.mkdtemp(prefix="laneheld-")
os.makedirs(os.path.join(root, "test-logs", "driver", "gates"))
os.environ["CN"] = root
spec = importlib.util.spec_from_file_location(
    "dlh" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); spec.loader.exec_module(D)
fixtures.redirect_state(D)   # item 9: never the LIVE state dir

def dispatcher(gate_ids=frozenset()):
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {}
    d.running_gate_items = lambda: (None if gate_ids is None else set(gate_ids))
    return d

LANE, WT = "lane/014a-assets", "/tmp/wt-014a"
def queue(other_status, other_lane=LANE, other_wt=WT):
    return {"items": [
        {"id": "B.014a.capability-descriptor", "status": "queued", "lane": LANE, "worktree": WT},
        {"id": "B.014a.artifact-custody-r3", "status": other_status,
         "lane": other_lane, "worktree": other_wt}]}

def held(status, gate_ids=frozenset()):
    q = queue(status)
    return dispatcher(gate_ids).lane_occupied(q, q["items"][0])

# THE DEFECT
check("MUST-BITE  an item in `gating` HOLDS the lane — this is the one that let EXEC-G in",
      held("gating") is not None, held("gating"))
check("MUST-BITE  an item in `rework` holds it too — the executor is writing that head",
      held("rework") is not None, held("rework"))
check("  dispatched holds it", held("dispatched") is not None)
check("  reported and gated still hold it (unchanged)",
      held("reported") is not None and held("gated") is not None)

# BOSS's release list, and only it
for st in ("merged", "held", "done"):
    check(f"  `{st}` releases the lane — BOSS's word that the item is off it", held(st) is None, st)
check("  `landed` releases it as well", held("landed") is None)
for st in ("queued", "parked", "broken"):
    check(f"  `{st}` never occupied it", held(st) is None, st)

# THE NEGATIVE CONTROL BOSS ASKED FOR
h = held("gating", {"B.014a.artifact-custody-r3"})
check("MUST-BITE  NEGATIVE CONTROL: `gating` with a LIVE mergegate does not free the lane",
      h is not None and h[0]["id"] == "B.014a.artifact-custody-r3", h)

# the process list is checked AS WELL AS the status, not instead of it
h = held("merged", {"B.014a.artifact-custody-r3"})
check("MUST-BITE  a LIVE gate holds the lane even when the STATUS says the item is merged",
      h is not None, h)
check("CONTROL  the same merged item with NO live gate frees it", held("merged") is None)

# a live gate for a DIFFERENT item must not block this lane
check("a live gate for an unrelated item does not hold this lane",
      held("merged", {"B.099.something-else"}) is None)

# the probe failing is fail-CLOSED, and only for the tick it fails on
check("MUST-BITE  an unreadable process list holds the lane rather than guessing it free",
      held("merged", None) is not None, held("merged", None))

# it is the lane/worktree that matches, not the item name
q = queue("gating", other_lane="lane/other", other_wt="/tmp/other")
check("an item on a DIFFERENT lane and worktree does not hold this one",
      dispatcher().lane_occupied(q, q["items"][0]) is None)
q = queue("gating", other_lane="lane/other")
h = dispatcher().lane_occupied(q, q["items"][0])
check("  but a shared WORKTREE alone is enough — the gate runs proofs in the tree, not the ref",
      h is not None and h[1] == "worktree", h)

# a placeholder worktree is not a path and must not match another placeholder
q = {"items": [{"id": "a", "status": "queued", "worktree": "(as in Part 1.3)"},
               {"id": "b", "status": "gating", "worktree": "(as in Part 1.3)"}]}
check("the '(as in Part 1.3)' placeholder is not treated as a shared worktree",
      dispatcher().lane_occupied(q, q["items"][0]) is None)

# running_gate_items parses argv, not a guess
d = D.Dispatcher.__new__(D.Dispatcher)
class FakeRun:
    stdout = ("/usr/bin/python3 /x/dispatcher/mergegate.py B.010.one --no-box\n"
              "/usr/bin/python3 /x/dispatcher/mergegate.py B.010.two\n"
              "grep dispatcher/mergegate.py\n"
              "/bin/zsh -c ps -eo args= | grep dispatcher/mergegate.py\n")
D.subprocess.run = lambda *a, **k: FakeRun()
ids = d.running_gate_items()
check("MUST-BITE  running_gate_items reads the item id from argv, and skips grep/zsh lines",
      ids == {"B.010.one", "B.010.two"}, ids)

def boom(*a, **k):
    raise OSError("ps unavailable")
D.subprocess.run = boom
check("MUST-BITE  when ps ITSELF fails, running_gate_items returns None (unknown), never an empty "
      "set — an empty set is indistinguishable from 'no gates are running' and frees every lane",
      d.running_gate_items() is None, d.running_gate_items())

shutil.rmtree(root, ignore_errors=True)
print(("FAILED: " + ", ".join(fails)) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
