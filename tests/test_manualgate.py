"""`dispatcherctl.sh gate <item-id>` — the hand-named gate, and the relayed rework it produces.

BOSS, 2026-09-07: "every lane gets one gate logic". WORKER-1/WORKER-2 report to BOSS by message, so
the daemon's REPORT_READY trigger never sees them and their gates were hand-run — a second, unlike
implementation of the thing the auto path does. This verb sends them through the SAME machinery.

Two failures this file exists to bite, both of which look like success from outside:

  1. A manual gate launched while `auto_gate` is false. The off switch is BOSS's remedy for a
     misbehaving auto-gate, and collect_gates sat behind the same flag — so the gate would run for
     an hour, write its file, and be read by nobody.
  2. A rework "posted to the executor" for a lane that has no opencode session. post_prompt would
     fail (or worse, succeed against a stale roster entry) and the findings would sit in the item
     file with nobody told.

Hermetic: a temp CN, no server, no gate, no box, no git — ctl is run with CN overridden."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); import fixturelog as FL
import fixtures                      # item 9: the shared state redirect
CTL = os.path.join(HERE, os.pardir, "dispatcherctl.sh")

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def load(mod, fname):
    spec = importlib.util.spec_from_file_location(
        mod + str(time.time_ns()), os.path.join(HERE, os.pardir, fname))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    if 'dispatcher.py' in fname:
        fixtures.redirect_state(m)   # item 9: never the LIVE state dir
    return m

AG = load("ag", "autogate.py")

# ---------------------------------------------------------------- the rules
check("MUST-BITE  a `reported` item IS hand-gateable", AG.manual_gate_ok({"status": "reported"}, False)[0])
check("  so is dispatched and rework",
      all(AG.manual_gate_ok({"status": s}, False)[0] for s in ("dispatched", "rework")))
ok, why = AG.manual_gate_ok({"status": "merged"}, False)
check("MUST-BITE  a MERGED item is refused, with the status named", not ok and "merged" in why, why)
ok, why = AG.manual_gate_ok({"status": "reported"}, True)
check("MUST-BITE  an item whose gate is already in flight is refused", not ok and "in flight" in why, why)
check("a missing item is refused, not crashed", AG.manual_gate_ok(None, False)[0] is False)
# Both lists carry `reported` since 2026-09-07 03:2x (feed() sets it before the automatic check
# runs, so excluding it there gated nothing). The manual verb's difference was never really the
# status — it is that report_trigger_ok is not consulted at all: BOSS naming an item is the
# judgement that check exists to make.
check("both lists accept `reported`, and neither accepts a finished item",
      "reported" in AG.MANUAL_GATEABLE_STATUS and "reported" in AG.GATEABLE_STATUS
      and not any(s in AG.MANUAL_GATEABLE_STATUS for s in ("merged", "held", "done")),
      (AG.MANUAL_GATEABLE_STATUS, AG.GATEABLE_STATUS))
check("MUST-BITE  WORKER-1/WORKER-2/BOSS are relayed, opencode executors are not",
      all(AG.is_relayed(x) for x in ("WORKER-1", "worker-2", "BOSS"))
      and not any(AG.is_relayed(x) for x in ("EXEC-A", "EXEC-F", "")),
      [AG.is_relayed(x) for x in ("WORKER-1", "worker-2", "BOSS", "EXEC-A", "")])

# ---------------------------------------------------------------- ctl writes a request
def tree():
    root = tempfile.mkdtemp(prefix="mangate-")
    os.makedirs(os.path.join(root, "dispatcher"))
    os.makedirs(os.path.join(root, "test-logs", "driver", "gates"))
    os.makedirs(os.path.join(root, "test-logs", "driver", "items"))
    json.dump({"items": [
        {"id": "B.010.w1", "status": "reported", "dispatched_to": "WORKER-1",
         "worktree": os.path.join(root, "wt"), "lane": "lane/w1",
         "proof_files": ["dispatcher/tests/test_x.py"]},
        {"id": "B.010.done", "status": "merged", "dispatched_to": "EXEC-A"}]},
        open(os.path.join(root, "test-logs", "driver", "queue.json"), "w"))
    return root

def ctl(root, *args):
    r = subprocess.run(["/bin/zsh", CTL, *args], capture_output=True, text=True,
                       env={**os.environ, "CN": root})
    return r.returncode, (r.stdout + r.stderr)

root = tree()
# 2026-09-07: this fixture used to write the queue at <root>/dispatcher/queue.json, which is where
# the ctl verb read it — and where the LIVE queue has never been. Both were wrong in the same
# direction, so the test was green and `dispatcherctl.sh gate` had never once worked in production.
# The control below is what makes this file able to notice: a queue that exists ONLY at the old path
# must produce a refusal, not a request.
wrongonly = tempfile.mkdtemp(prefix="mangate-wrong-")
os.makedirs(os.path.join(wrongonly, "dispatcher"))
os.makedirs(os.path.join(wrongonly, "test-logs", "driver"))
json.dump({"items": [{"id": "B.010.w1", "status": "reported"}]},
          open(os.path.join(wrongonly, "dispatcher", "queue.json"), "w"))
rc_w, out_w = ctl(wrongonly, "gate", "B.010.w1")
check("MUST-BITE  ctl reads the LIVE queue (test-logs/driver), not dispatcher/queue.json — a queue "
      "present only at the old path must refuse, not launch",
      rc_w != 0 and not [f for f in os.listdir(os.path.join(wrongonly, "test-logs", "driver", "gatereq"))
                         if f.endswith(".json")],
      (rc_w, FL.flat(out_w)[:160]))
shutil.rmtree(wrongonly, ignore_errors=True)

rc, out = ctl(root, "gate", "B.010.w1")
reqs = os.listdir(os.path.join(root, "test-logs", "driver", "gatereq"))
check("ctl gate writes exactly one request file", rc == 0 and len(reqs) == 1, (rc, reqs, out))
req = json.load(open(os.path.join(root, "test-logs", "driver", "gatereq", reqs[0])))
check("  naming the item, box not overridden", req["item"] == "B.010.w1" and req["no_box"] is False, req)
check("  and it says the daemon launches it, not ctl", "daemon launches it" in out, out)
rc, out = ctl(root, "gate", "B.010.w1", "--no-box")
req = json.load(open(os.path.join(root, "test-logs", "driver", "gatereq", reqs[0])))
check("--no-box is carried into the request", req["no_box"] is True, req)
rc, out = ctl(root, "gate", "B.010.done")
check("MUST-BITE  ctl refuses a merged item and writes nothing new",
      rc == 1 and "merged" in out and len(os.listdir(os.path.join(root, "test-logs", "driver", "gatereq"))) == 1,
      (rc, FL.flat(out)))
rc, out = ctl(root, "gate", "B.010.nope")
check("an unknown id is refused by name", rc == 1 and "B.010.nope" in out, (rc, FL.flat(out)))
rc, out = ctl(root, "gate")
check("no id at all is a usage error", rc == 2 and "usage" in out, (rc, FL.flat(out)))
shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- the daemon applies it
def daemon(root, status="reported", who="WORKER-1"):
    os.environ["CN"] = root
    D = load("dmg", "dispatcher.py")
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {}
    d.dry = False
    d.posts, d.events, d.escalations, d.logs, d.launched = [], [], [], [], []
    d.emit = lambda *f: d.events.append(f)
    d.escalate = lambda m: d.escalations.append(m)
    d.log = lambda m: d.logs.append(m)
    d.post_prompt = lambda sid, text: (d.posts.append((sid, text)) or True)
    d.roster = {"EXEC-A": "ses_a"}
    d.c = lambda k, default=None: default
    d.gates_running = lambda: 0
    d.sha_at_lane_head = lambda item: (True, "26b38cfe1f26b38cfe1f")
    q = {"items": [{"id": "B.010.w1", "status": status, "dispatched_to": who,
                    "worktree": os.path.join(root, "wt"), "lane": "lane/w1",
                    "proof_files": ["dispatcher/tests/test_x.py"]}]}
    return D, d, q

def request(root, item="B.010.w1", **kw):
    p = os.path.join(root, "test-logs", "driver", "gatereq")
    os.makedirs(p, exist_ok=True)
    json.dump({"item": item, **kw}, open(os.path.join(p, item + ".json"), "w"))
    return p

root = tree()
D, d, q = daemon(root)
p = request(root)
popens = []
D.subprocess.Popen = lambda argv, **kw: popens.append(argv)
d.apply_gate_requests(q)
check("MUST-BITE  the daemon launches mergegate for the named item",
      len(popens) == 1 and popens[0][1].endswith("mergegate.py") and popens[0][2] == "B.010.w1",
      popens)
check("MUST-BITE  the box is TAKEN by default — this item's proof is a .py file",
      "--no-box" not in popens[0], popens[0])
check("  the request file is consumed, so the next tick cannot relaunch it",
      os.listdir(p) == [], os.listdir(p))
check("  it is recorded as MANUAL_GATE, not AUTO_GATE",
      [e[0] for e in d.events] == ["MANUAL_GATE"], [e[0] for e in d.events])
check("  and tagged source=manual in state", d.state["autogate"]["B.010.w1"].get("source") == "manual",
      d.state["autogate"])
check("MUST-BITE  a manual gate in flight keeps collect_gates alive with auto_gate OFF",
      d.manual_gates_live() is True)
d.state["autogate"]["B.010.w1"]["source"] = "report"
check("  an automatic one does not (the flag still governs it)", d.manual_gates_live() is False)

D, d, q = daemon(root)
request(root, no_box=True); popens = []
D.subprocess.Popen = lambda argv, **kw: popens.append(argv)
d.apply_gate_requests(q)
check("MUST-BITE  --no-box on the request reaches mergegate's argv",
      len(popens) == 1 and popens[0][-1] == "--no-box", popens)
check("  and the mode records the override", "--no-box" in d.state["autogate"]["B.010.w1"].get("mode", ""),
      d.state["autogate"])

# refusals
D, d, q = daemon(root, status="merged")
request(root); D.subprocess.Popen = lambda argv, **kw: popens.append(argv)
d.apply_gate_requests(q)
check("MUST-BITE  the daemon re-checks the live queue and refuses a merged item",
      len(popens) == 1 and [e[0] for e in d.events] == ["MANUAL_GATE_REFUSED"],
      (len(popens), [e[0] for e in d.events]))
check("  and BOSS is told why", d.escalations and "merged" in d.escalations[0], d.escalations)

D, d, q = daemon(root)
request(root, item="B.010.ghost")
d.apply_gate_requests(q)
check("an id that vanished from the queue is refused, not crashed",
      [e[0] for e in d.events] == ["MANUAL_GATE_REFUSED"] and len(popens) == 1,
      [e[0] for e in d.events])

D, d, q = daemon(root)
request(root); d.sha_at_lane_head = lambda item: (False, "")
d.apply_gate_requests(q)
check("MUST-BITE  --no-box cannot skip the sha-at-lane-head check",
      len(popens) == 1 and "not the lane head" in " ".join(str(x) for x in d.escalations),
      d.escalations)

D, d, q = daemon(root)
request(root, no_box=True); d.gates_running = lambda: 1
q["items"][0]["proof_files"] = ["platform/tests/test_x.py"]
d.apply_gate_requests(q)
check("MUST-BITE  --no-box cannot jump the box queue for a .py-proof item",
      len(popens) == 1 and "box" in " ".join(str(x) for x in d.escalations),
      d.escalations)
shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- the relayed rework
CODEX = "## Findings\n\n[high] platform/core/pay.py: the guard is never reached\n"
GATE_FAIL = ("# GATE B.010.w1 — FAIL — 2026-09-07 02:00\n\n"
             "- **sha on lane head**: PASS — report sha 26b38cfe1f head 26b38cfe1f\n"
             "- **codex adversarial review**: FAIL — rc=0 verdict=needs-attention blockers=1['[high]']\n")
root = tree()
state = os.path.join(root, "test-logs", "driver")
open(os.path.join(state, "items", "B.010.w1.md"), "w").write("# item body\n")
open(os.path.join(state, "gates", "B.010.w1.md"), "w").write(GATE_FAIL)
open(os.path.join(state, "gates", "B.010.w1.codex.txt"), "w").write(CODEX)
open(os.path.join(state, "EXECUTOR_STANDING_RULES.md"), "w").write("STANDING RULES BODY")
D, d, q = daemon(root, status="gated", who="WORKER-1")
d.state = {"autogate": {"B.010.w1": {"sha": "26b38cfe1f", "started": time.time() - 60,
                                     "mode": "box", "source": "manual"}}}
d.state["pending"] = {}
d.collect_gates(q)
body = open(os.path.join(state, "items", "B.010.w1.md")).read()
check("MUST-BITE  a relayed lane's findings still land in the item file",
      CODEX.strip() in body and "## REWORK 1" in body, body[-120:])
check("MUST-BITE  and NOTHING is posted — WORKER-1 has no opencode session", d.posts == [], d.posts)
row = d.state["pending"].get("relay:B.010.w1")
check("MUST-BITE  a PENDING ROW is raised instead, so BOSS learns there is something to relay",
      bool(row), list(d.state["pending"]))
check("  the row names the item file to relay from", row and "B.010.w1.md" in row["excerpt"], row)
check("  and says the daemon cannot prompt it", row and "cannot prompt" in row["excerpt"], row)
check("  the event is AUTO_REWORK_RELAY, never AUTO_REWORK",
      [e[0] for e in d.events] == ["AUTO_REWORK_RELAY"], [e[0] for e in d.events])
check("  the item moves to rework, exactly as an opencode lane would",
      q["items"][0]["status"] == "rework", q["items"][0]["status"])
check("  and the fail is counted, so MAX_AUTO_FAILS still applies to relayed lanes",
      d.state.get("autogate_hist", {}).get("B.010.w1", {}).get("fails") == 1, d.state.get("autogate_hist"))

# the same gate against an opencode lane still POSTS — the relay branch must not swallow everyone
open(os.path.join(state, "items", "B.010.w1.md"), "w").write("# item body\n")
D, d, q = daemon(root, status="gated", who="EXEC-A")
d.state = {"autogate": {"B.010.w1": {"sha": "26b38cfe1f", "started": time.time() - 60,
                                     "mode": "box", "source": "manual"}}, "pending": {}}
d.collect_gates(q)
check("MUST-BITE  an opencode lane is still POSTED to, not turned into a row",
      len(d.posts) == 1 and not d.state["pending"], (d.posts, d.state["pending"]))
shutil.rmtree(root, ignore_errors=True)

print(("FAILED: " + ", ".join(fails)) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
