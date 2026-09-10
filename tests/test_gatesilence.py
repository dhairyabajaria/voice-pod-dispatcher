"""Every non-launch says why, and an idle ack raises no row for BOSS.

BOSS, 2026-09-07, two live cases:

  - EXEC-F's REPORT_READY at 02:58:53 for B.010.ci-collection-floor (status rework, report path real
    and present at lane head e66e442f) produced neither AUTO_GATE nor AUTO_GATE_SKIPPED for 90 s.
    Silence is the defect: a report that was never gated looked exactly like one nobody had made.
    Two roads led there — an item whose status made it nobody's current work, and a deferred launch
    that was a `self.log` line only, never an event.
  - Two REPORT_READYs that were only idle acks each raised a pending row BOSS then cleared by hand.
    Once the trigger has judged a message not a report, it must not also become BOSS's inbox item.

Hermetic: temp CN, no server, no gate, no box."""
import importlib.util, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); import fixturelog as FL
import fixtures                      # item 9: the shared state redirect

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

# EXEC-F's real 02:58:53 message, plus the INTERRUPT-TEST line the owner directive of 2026-09-07
# 03:2x made mandatory: without it the report is refused before any of the rules THIS file measures
# are reached, and every check below would pass on the wrong refusal. The refusal itself is measured
# in test_precheck.py.
REPORT = ("REPORT READY: `audit/plan-execution-2026-09-04/reports/B.010.ci-collection-floor-r12.md` "
          "@ `e66e442f` (product `c0740503`: detector + tests)\n"
          "INTERRUPT-TEST: platform/tests/test_collection_floor.py::test_interrupted_between_writes")
IDLE = "REPORT READY: idle — lane clean (merged 6b542f7), no lock held. Awaiting next dispatch."

def build(status="rework", gates_running=0, sha_ok=True):
    root = tempfile.mkdtemp(prefix="silence-")
    state = os.path.join(root, "test-logs", "driver")
    for d in ("gates", "items"):
        os.makedirs(os.path.join(state, d))
    os.makedirs(os.path.join(root, "wt", "audit", "plan-execution-2026-09-04", "reports"))
    open(os.path.join(root, "wt", "audit", "plan-execution-2026-09-04", "reports",
                      "B.010.ci-collection-floor-r12.md"), "w").write("# report\n")
    open(os.path.join(state, "items", "B.010.ci-collection-floor.md"), "w").write("# item\n")
    os.environ["CN"] = root
    spec = importlib.util.spec_from_file_location(
        "dsil" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
    D = importlib.util.module_from_spec(spec); spec.loader.exec_module(D)
    fixtures.redirect_state(D)   # item 9: never the LIVE state dir
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {"handled": {}, "autogate": {}, "parks": {}, "auto": {}, "pending": {}}
    d.dry = False
    d.posts, d.events, d.escalations, d.logs = [], [], [], []
    d.emit = lambda *f: d.events.append(f)
    d.escalate = lambda m: d.escalations.append(m)
    d.log = lambda m: d.logs.append(m)
    d.post_prompt = lambda sid, text: (d.posts.append((sid, text)) or True)
    d.roster = {"EXEC-F": "ses_f"}
    d.cfg = {}
    d.c = lambda k, default=None: default
    d.gates_running = lambda: gates_running
    d.sha_at_lane_head = lambda item: (sha_ok, "e66e442f26b38cfe1f" if sha_ok else "")
    d.eligible_item = lambda q, name: None
    D.subprocess.Popen = lambda argv, **kw: None
    q = {"items": [{"id": "B.010.ci-collection-floor", "status": status, "dispatched_to": "EXEC-F",
                    "worktree": os.path.join(root, "wt"), "lane": "lane/ci",
                    "proof_files": ["platform/tests/test_x.py"]}]}
    return root, D, d, q

def kinds(d):
    return [e[0] for e in d.events]

# 1. the happy path still launches (control: the rest is about NOT launching)
root, D, d, q = build()
d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "msg_1", REPORT)
check("CONTROL  a real report on a rework item still LAUNCHES", "AUTO_GATE" in kinds(d), kinds(d))
check("  the backticked path in the real message was parsed",
      d._report_verdict[1] is True, d._report_verdict)
shutil.rmtree(root, ignore_errors=True)

# 2. THE SILENCE: a deferred launch must emit, not just log
root, D, d, q = build(gates_running=1)          # box busy, this item needs the box
d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "msg_1", REPORT)
check("MUST-BITE  a DEFERRED launch emits AUTO_GATE_SKIPPED — silence was the defect",
      "AUTO_GATE_SKIPPED" in kinds(d), kinds(d))
check("  and the reason names the box queue", any("box" in str(e[-1]) for e in d.events), d.events)
shutil.rmtree(root, ignore_errors=True)

# a box-FREE item is NOT blocked by a running box gate (BOSS asked whether this rule was too wide)
root, D, d, q = build(gates_running=1)
q["items"][0]["proof_files"] = ["audit/notes.md"]
d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "msg_1", REPORT)
check("MUST-BITE  a box-FREE report launches while a BOX gate runs — the rule is already narrow",
      "AUTO_GATE" in kinds(d), kinds(d))
shutil.rmtree(root, ignore_errors=True)

# 3. a report from an executor holding NO item: also speaks
root, D, d, q = build(status="merged")           # nothing dispatched/rework for EXEC-F
d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "msg_1", REPORT)
check("MUST-BITE  a report with no dispatched/rework item emits SKIPPED instead of nothing",
      "AUTO_GATE_SKIPPED" in kinds(d), kinds(d))
check("  naming the executor and the reason",
      any("holds no dispatched" in str(e[-1]) for e in d.events), d.events)
shutil.rmtree(root, ignore_errors=True)

# 4. sha not at lane head: still speaks
root, D, d, q = build(sha_ok=False)
d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "msg_1", REPORT)
check("MUST-BITE  a sha that is not the lane head emits SKIPPED",
      "AUTO_GATE_SKIPPED" in kinds(d) and "AUTO_GATE" not in kinds(d), kinds(d))
shutil.rmtree(root, ignore_errors=True)

# 5. the idle ack: judged, and no row raised
root, D, d, q = build()
d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "msg_idle", IDLE)
check("MUST-BITE  an idle ack is judged NOT a report", d._report_verdict[1] is False, d._report_verdict)
check("  and it emits SKIPPED with the reason", "AUTO_GATE_SKIPPED" in kinds(d), kinds(d))
check("  and NOTHING was launched", "AUTO_GATE" not in kinds(d), kinds(d))

# the withdrawal itself, exercised through the function the tick calls
pending = {"ses_f": {"executor": "EXEC-F", "kind": "REPORT_READY", "msg_id": "msg_idle"}}
d.events.clear(); d._report_verdict = None
d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "msg_idle", IDLE)
took = d.withdraw_idle_row(pending, "EXEC-F", "ses_f", "REPORT_READY", "msg_idle")
check("MUST-BITE  the judged idle ack leaves BOSS no pending row to clear by hand",
      took is True and pending == {}, (took, pending))
check("  and the withdrawal is recorded as IDLE_ACK, so it is not a silent disappearance",
      "IDLE_ACK" in kinds(d), kinds(d))

# and a REAL report that merely could not launch KEEPS its row — BOSS must still see it
root2, D2, d2, q2 = build(sha_ok=False)
d2._report_verdict = None
d2.feed(q2, "EXEC-F", "ses_f", "REPORT_READY", "msg_real", REPORT)
keep = {"ses_f": {"executor": "EXEC-F", "kind": "REPORT_READY", "msg_id": "msg_real"}}
took2 = d2.withdraw_idle_row(keep, "EXEC-F", "ses_f", "REPORT_READY", "msg_real")
check("MUST-BITE  a REAL report that could not launch is NOT withdrawn — it is BOSS's to see",
      took2 is False and "ses_f" in keep, (took2, keep))
check("  a verdict from a DIFFERENT message never withdraws this row",
      d.withdraw_idle_row({"ses_f": {}}, "EXEC-F", "ses_f", "REPORT_READY", "msg_other") is False)
shutil.rmtree(root, ignore_errors=True); shutil.rmtree(root2, ignore_errors=True)

# 6. the withdrawal is CALLED by the tick — measured by removing the call, not by reading for it
src = open(os.path.join(HERE, os.pardir, "dispatcher.py"), errors="ignore").read()
i = src.find("if kind in FEED_KINDS and fed_this_tick < max_feed:")
check("MUST-BITE  the tick calls withdraw_idle_row after feed()",
      i != -1 and "self.withdraw_idle_row(" in src[i:i + 900], i)

print(("FAILED: " + ", ".join(fails)) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
