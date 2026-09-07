"""precheck, the interrupt-test refusal, and dispatched_at on the GATE line.

Owner directive 2026-09-07 03:2x — build INTO auto-gate, not beside it:
  (a) `dispatcherctl.sh precheck <item>` gives the executor the adversarial reviewer's reading of its
      DRAFT diff, once per rework round. The gate's own Codex call must stay COLD: a fresh
      invocation, the same prompt, and nothing the pre-check writes may reach the gate's input. If
      Codex is walled the PRE-CHECK is dropped first — the gate keeps Codex.
  (b) A REPORT READY without exactly one INTERRUPT-TEST line is not gated, and the executor is told
      in one line what to add. 3 of the last 7 gate [high]s were multi-step writes with a green happy
      path, which is what that artefact is for.
  (c) GATE_* lines carry dispatched_at, so the last N gates can be split by when their item was
      dispatched — events.log records the gate time and never the dispatch time.

Hermetic: temp CN, no Codex call (the reviewer invocation is stubbed), no gate, no box."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, time

# `subprocess` is one module object for the whole process: stubbing Popen for the dispatcher harness
# also stubs it for this file's own subprocess.run calls. Kept here so it can be put back.
REAL_POPEN = subprocess.Popen

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); import fixturelog as FL
CTL = os.path.join(HERE, os.pardir, "dispatcherctl.sh")

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def load(mod, fname):
    spec = importlib.util.spec_from_file_location(
        mod + str(time.time_ns()), os.path.join(HERE, os.pardir, fname))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

# ---------------------------------------------------------------- (b) the interrupt-test rule
AG = load("agi", "autogate.py")
ok, why = AG.interrupt_test_ok("REPORT READY: x\nINTERRUPT-TEST: platform/tests/test_a.py::test_mid\n")
check("MUST-BITE  a named file::test passes", ok, why)
ok, why = AG.interrupt_test_ok("REPORT READY\nINTERRUPT-TEST: N/A — pure read path, no multi-step write\n")
check("MUST-BITE  N/A WITH a reason passes — the escape hatch is real", ok, why)
ok, why = AG.interrupt_test_ok("REPORT READY\nINTERRUPT-TEST: N/A\n")
check("MUST-BITE  a bare `N/A` is REFUSED — the reason is the point of the hatch",
      not ok and "no reason" in why, why)
ok, why = AG.interrupt_test_ok("REPORT READY: report at audit/x/reports/y.md, proofs green")
check("MUST-BITE  a report with no line at all is refused", not ok and "names no INTERRUPT-TEST" in why, why)
ok, why = AG.interrupt_test_ok("a\nINTERRUPT-TEST: a.py::t1\nINTERRUPT-TEST: b.py::t2\n")
check("MUST-BITE  TWO lines is refused — the rule is exactly one",
      not ok and "2 INTERRUPT-TEST" in why, why)
ok, why = AG.interrupt_test_ok("INTERRUPT-TEST: test_thing\n")
check("  a bare word is not a test — file::name or N/A", not ok and "names no test" in why, why)
check("every refusal is tagged no-interrupt-test, as the reason BOSS greps for",
      all(AG.interrupt_test_ok(t)[1].startswith("no-interrupt-test")
          for t in ("", "INTERRUPT-TEST: N/A\n", "INTERRUPT-TEST: x\nINTERRUPT-TEST: y\n")))

# ---------------------------------------------------------------- (b) wired into the trigger
REPORT = ("REPORT READY: `audit/plan-execution-2026-09-04/reports/r12.md` @ `e66e442f`\n"
          "INTERRUPT-TEST: platform/tests/test_a.py::test_crash_between_steps\n")
NO_LINE = "REPORT READY: `audit/plan-execution-2026-09-04/reports/r12.md` @ `e66e442f`\n"

def build():
    root = tempfile.mkdtemp(prefix="precheck-")
    state = os.path.join(root, "test-logs", "driver")
    for d in ("gates", "items", "precheck"):
        os.makedirs(os.path.join(state, d))
    os.makedirs(os.path.join(root, "wt", "audit", "plan-execution-2026-09-04", "reports"))
    open(os.path.join(root, "wt", "audit", "plan-execution-2026-09-04", "reports", "r12.md"), "w").write("#\n")
    open(os.path.join(state, "items", "B.010.x.md"), "w").write("# item body\n")
    os.environ["CN"] = root
    D = load("dpc", "dispatcher.py")
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {"handled": {}, "autogate": {}, "parks": {}, "auto": {}, "pending": {}}
    d.dry = False
    d.posts, d.events, d.logs = [], [], []
    d.emit = lambda *f: d.events.append(f)
    d.escalate = lambda m: None
    d.log = lambda m: d.logs.append(m)
    d.post_prompt = lambda sid, text: (d.posts.append((sid, text)) or True)
    d.roster = {"EXEC-A": "ses_a"}
    d.cfg = {}
    d.c = lambda k, default=None: default
    d.gates_running = lambda: 0
    d.sha_at_lane_head = lambda item: (True, "e66e442f26b38cfe")
    d.eligible_item = lambda q, name: None
    D.subprocess.Popen = lambda argv, **kw: None
    q = {"items": [{"id": "B.010.x", "status": "rework", "dispatched_to": "EXEC-A",
                    "worktree": os.path.join(root, "wt"), "lane": "lane/x",
                    "proof_files": ["platform/tests/test_x.py"]}]}
    return root, d, q

root, d, q = build()
d.feed(q, "EXEC-A", "ses_a", "REPORT_READY", "m1", REPORT)
check("CONTROL  a report WITH the line still gates", "AUTO_GATE" in [e[0] for e in d.events],
      [e[0] for e in d.events])
shutil.rmtree(root, ignore_errors=True)

root, d, q = build()
d.feed(q, "EXEC-A", "ses_a", "REPORT_READY", "m1", NO_LINE)
kinds = [e[0] for e in d.events]
check("MUST-BITE  a report WITHOUT the line is NOT gated", "AUTO_GATE" not in kinds, kinds)
check("  it is skipped with reason=no-interrupt-test",
      kinds == ["AUTO_GATE_SKIPPED"] and "no-interrupt-test" in str(d.events[0][-1]), d.events)
check("MUST-BITE  the executor is told, in one message, exactly what to add",
      len(d.posts) == 1 and "INTERRUPT-TEST:" in d.posts[0][1] and "N/A" in d.posts[0][1],
      d.posts[0][1][:160] if d.posts else d.posts)
check("  and told it need not redo the work", d.posts and "nothing else needs redoing" in d.posts[0][1])
check("MUST-BITE  the pending row STAYS — a real report BOSS should see waiting, unlike an idle ack",
      d.withdraw_idle_row({"ses_a": {}}, "EXEC-A", "ses_a", "REPORT_READY", "m1") is False)
shutil.rmtree(root, ignore_errors=True)

# an idle ack is judged an idle ack, NOT "missing an artefact"
root, d, q = build()
d.feed(q, "EXEC-A", "ses_a", "REPORT_READY", "m1", "REPORT READY: idle — lane clean, awaiting work.")
check("MUST-BITE  an idle ack is still refused as 'no report path', not as a missing interrupt test",
      "no report path" in str(d.events[0][-1]), d.events[0][-1])
check("  and the executor is NOT lectured about an artefact for a report it never made",
      d.posts == [], d.posts)
shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- (a) precheck
def tree():
    root = tempfile.mkdtemp(prefix="pc-")
    state = os.path.join(root, "test-logs", "driver")
    for sub in ("gates", "items", "precheck"):
        os.makedirs(os.path.join(state, sub))
    os.makedirs(os.path.join(root, "wt"))
    json.dump({"items": [{"id": "B.010.x", "status": "dispatched", "artifact": "A",
                          "worktree": os.path.join(root, "wt"), "lane": "lane/x"}]},
              open(os.path.join(state, "queue.json"), "w"))
    open(os.path.join(state, "items", "B.010.x.md"), "w").write("# item body\n")
    os.environ["CN"] = root
    return root, state

root, state = tree()
PC = load("pc", "precheck.py")
calls = []
PC.MG.sh = lambda argv, cwd=None, timeout=600, env=None: (calls.append((argv, cwd, bool(env)))
                                                          or (0, "Verdict: needs-attention\n[high] x.py: leak\n"))
PC.MG.recorded_wall = lambda now=None: None
PC.gate_running_for = lambda i: False
rc = PC.main(["B.010.x"])
body = open(os.path.join(state, "items", "B.010.x.md")).read()
check("MUST-BITE  precheck writes the findings into the item file", rc == 0 and "[high] x.py: leak" in body, (rc, body[-80:]))
check("  under a PRE-CHECK header that says it is NOT a gate",
      "## PRE-CHECK 0" in body and "no proofs ran" in body, body[-300:])
check("  and warns that the gate reviews COLD", "reviews this candidate COLD" in body, body[-300:])
check("MUST-BITE  it runs in the LANE WORKTREE with the codex PATH env",
      calls and calls[0][1] == os.path.join(root, "wt") and calls[0][2] is True, calls)
check("MUST-BITE  the prompt is the SAME text the gate uses — the gate's reviewer is not primed",
      calls and "find authority gaps, tenancy leaks, money errors, fail-open paths." in calls[0][0][-1],
      calls[0][0][-1][:80] if calls else calls)
check("  and it reviews the branch against trunk", "plan010/rebuild" in calls[0][0], calls[0][0])
check("  a PRECHECK event is logged", "PRECHECK" in open(os.path.join(state, "events.log")).read())

# once per rework round
n = len(calls)
rc2 = PC.main(["B.010.x"])
check("MUST-BITE  a SECOND pre-check on the same round is refused and calls nobody",
      rc2 == 1 and len(calls) == n, (rc2, len(calls)))
open(os.path.join(state, "items", "B.010.x.md"), "a").write("\n## REWORK 1 (auto) — x\n")
rc3 = PC.main(["B.010.x"])
check("MUST-BITE  a NEW rework round allows one more", rc3 == 0 and len(calls) == n + 1, (rc3, len(calls)))

# the wall drops the PRE-CHECK, not the gate. A fresh round first, so the refusal under test is the
# WALL and not "already run this round" — a check that passes on the wrong refusal measures nothing.
open(os.path.join(state, "items", "B.010.x.md"), "a").write("\n## REWORK 2 (auto) — x\n")
PC.MG.recorded_wall = lambda now=None: (time.strftime("%Y-%m-%d"), "someone")
n = len(calls)
rc4 = PC.main(["B.010.x"])
check("MUST-BITE  a RECORDED wall drops the pre-check BEFORE any call — the gate keeps Codex",
      rc4 == 3 and len(calls) == n, (rc4, len(calls)))
PC.MG.recorded_wall = lambda now=None: None
open(os.path.join(state, "items", "B.010.x.md"), "a").write("\n## REWORK 3 (auto) — x\n")
PC.MG.sh = lambda argv, cwd=None, timeout=600, env=None: (calls.append(argv) or
                                                          (0, "You've hit your usage limit. Try again after Sep 9th."))
before = open(os.path.join(state, "items", "B.010.x.md")).read()
rc5 = PC.main(["B.010.x"])
after = open(os.path.join(state, "items", "B.010.x.md")).read()
check("MUST-BITE  a wall DISCOVERED mid-call writes nothing to the item file", rc5 == 3 and after == before,
      (rc5, len(after) - len(before)))
check("  and is NOT retried — the retry belongs to the gate",
      len([c for c in calls if isinstance(c, list)]) == 1, len(calls))

# a gate for this item is running: the cold call must not race
PC.gate_running_for = lambda i: True
n = len(calls)
rc6 = PC.main(["B.010.x"])
check("MUST-BITE  precheck refuses while a gate for the same item runs", rc6 == 1 and len(calls) == n, rc6)
PC.gate_running_for = lambda i: False
check("an unknown item is refused by name", PC.main(["B.010.nope"]) == 1)
shutil.rmtree(root, ignore_errors=True)

# ctl carries the verb
subprocess.Popen = REAL_POPEN          # the harness above stubbed it; ctl must really run
r = subprocess.run(["/bin/zsh", CTL, "precheck"], capture_output=True, text=True)
check("ctl precheck with no id is a usage error", r.returncode == 2 and "precheck <item-id>" in r.stdout + r.stderr,
      (r.returncode, (r.stdout + r.stderr)[:80]))

# ---------------------------------------------------------------- (c) dispatched_at on GATE lines
MG = load("mgd", "mergegate.py")
src = open(os.path.join(HERE, os.pardir, "mergegate.py"), errors="ignore").read()
# 2026-09-07: this was `src.find('emit("GATE_" + verdict')` and it went red when the emit learned to
# name a third kind (GATE_INCOMPLETE) — a rename, loud, with the behaviour untouched. That is the
# failure mode tests/README warns about in a source-read. The REAL proof now lives in test_notrun.py,
# which runs the gate end to end and asserts dispatched_at on the line it actually emitted; this
# stays only as a cheap anchor that the field is still assembled here.
i = src.find("emit(kind, item_id")
check("  the GATE_ line still assembles dispatched_at (end-to-end proof: test_notrun.py)",
      i != -1 and "dispatched_at=" in src[i:i + 300], i)
j = src.find('emit("GATE_CRASHED"')
check("  and so does GATE_CRASHED, which has no item in hand", j != -1 and "dispatched_at" in src[j:j + 220], j)
root2, state2 = tree()
check("MUST-BITE  dispatched_at_of reads the queue, and says '-' rather than crashing when absent",
      MG.dispatched_at_of("B.010.x") == "-" and MG.dispatched_at_of("nope") == "-")
q2 = json.load(open(os.path.join(state2, "queue.json")))
q2["items"][0]["dispatched_at"] = "2026-09-07 02:23:00"
json.dump(q2, open(os.path.join(state2, "queue.json"), "w"))
MG2 = load("mgd2", "mergegate.py")
check("  and returns the real value when it is there",
      MG2.dispatched_at_of("B.010.x") == "2026-09-07 02:23:00", MG2.dispatched_at_of("B.010.x"))
shutil.rmtree(root2, ignore_errors=True)

print(("FAILED: " + ", ".join(fails)) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
