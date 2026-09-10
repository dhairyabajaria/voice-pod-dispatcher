"""The acting half of auto-gate: what actually gets written and posted, and how often.

autogate.py decides; dispatcher.py acts. The decisions are tested next door — this file tests the
things only the driver can get wrong, and the worst of them is BOSS's: a rework prompt sent TWICE
for the same gate file. An executor that is handed the same findings again has no way to tell a
repeat from a new result, and will redo work it already did.

Hermetic: a temp CN, no server (post_prompt is captured), no gate, no box, no git."""
import importlib.util, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
import fixtures                      # item 9: the shared state redirect

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

CODEX = "## Findings\n\n[high] platform/core/pay.py: the guard is never reached\n"
GATE_FAIL = ("# GATE B.010.x — FAIL — 2026-09-07 02:00\n\n"
             "- **sha on lane head**: PASS — report sha 26b38cfe1f head 26b38cfe1f\n"
             "- **proofs**: PASS — 77 passed\n"
             "- **codex adversarial review**: FAIL — rc=0 verdict=needs-attention blockers=1['[high]']\n")
GATE_PASS = ("# GATE B.010.x — PASS — 2026-09-07 02:00\n\n"
             "- **codex adversarial review**: PASS — rc=0 verdict=approve blockers=0\n")

def build(gate_body=GATE_FAIL, started_ago=60):
    root = tempfile.mkdtemp(prefix="autodrv-")
    state = os.path.join(root, "test-logs", "driver")
    for d in ("gates", "items"):
        os.makedirs(os.path.join(state, d))
    open(os.path.join(state, "EXECUTOR_STANDING_RULES.md"), "w").write("STANDING RULES BODY")
    open(os.path.join(state, "items", "B.010.x.md"), "w").write("# item body\n")
    g = os.path.join(state, "gates")
    open(os.path.join(g, "B.010.x.md"), "w").write(gate_body)
    open(os.path.join(g, "B.010.x.codex.txt"), "w").write(CODEX)
    os.environ["CN"] = root
    spec = importlib.util.spec_from_file_location(
        "dag" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
    D = importlib.util.module_from_spec(spec); spec.loader.exec_module(D)
    fixtures.redirect_state(D)   # item 9: never the LIVE state dir
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {"autogate": {"B.010.x": {"sha": "26b38cfe1f", "started": time.time() - started_ago,
                                        "mode": "box"}}}
    d.dry = False
    d.posts, d.events, d.escalations, d.logs = [], [], [], []
    d.emit = lambda *f: d.events.append(f)
    d.escalate = lambda m: d.escalations.append(m)
    d.log = lambda m: d.logs.append(m)
    d.post_prompt = lambda sid, text: (d.posts.append((sid, text)) or True)
    d.roster = {"EXEC-A": "ses_a"}
    d.c = lambda k, default=None: default
    q = {"items": [{"id": "B.010.x", "status": "gated", "dispatched_to": "EXEC-A",
                    "worktree": "/tmp/wt", "lane": "lane/x", "proof_files": ["tests/test_x.py"]}]}
    return root, state, d, q, D

# 1. the automated path, end to end
root, state, d, q, D = build()
d.collect_gates(q)
check("MUST-BITE  a FAIL with a real verdict posts exactly ONE rework prompt", len(d.posts) == 1, len(d.posts))
check("  and emits exactly one AUTO_REWORK event",
      [e[0] for e in d.events] == ["AUTO_REWORK"], [e[0] for e in d.events])
check("  the item is set to rework", q["items"][0]["status"] == "rework", q["items"][0]["status"])
body = open(os.path.join(state, "items", "B.010.x.md")).read()
check("MUST-BITE  the ITEM FILE carries the findings verbatim", CODEX.strip() in body, body[-140:])
check("  under a REWORK 1 header", "## REWORK 1 (auto," in body, body[-200:])
check("  and the original item body is not clobbered", body.startswith("# item body"), body[:40])
check("the prompt leads with the standing rules", d.posts[0][1].startswith("STANDING RULES BODY"))
check("  and went to the ORIGINATING executor's session", d.posts[0][0] == "ses_a", d.posts[0][0])
check("BOSS was NOT escalated for an ordinary auto-rework", not d.escalations, d.escalations)

# 2. BOSS'S CONTROL: never twice for the same gate file.
d.collect_gates(q); d.collect_gates(q)
check("MUST-BITE  re-running collect on the SAME gate file posts nothing further",
      len(d.posts) == 1 and len(d.events) == 1, (len(d.posts), len(d.events)))
body2 = open(os.path.join(state, "items", "B.010.x.md")).read()
check("  and the item file is not appended to twice", body2.count("## REWORK") == 1, body2.count("## REWORK"))
shutil.rmtree(root, ignore_errors=True)

# 3. a PASS is escalated and NOTHING is posted or written
root, state, d, q, D = build(GATE_PASS)
d.collect_gates(q)
check("MUST-BITE  a PASS posts NO prompt", not d.posts, d.posts)
check("  escalates to BOSS instead", len(d.escalations) == 1, d.escalations)
check("  emits AUTO_GATE_ESCALATED", [e[0] for e in d.events] == ["AUTO_GATE_ESCALATED"], [e[0] for e in d.events])
check("  leaves the item reported, not reworked", q["items"][0]["status"] == "reported", q["items"][0]["status"])
check("MUST-BITE  and does NOT touch the item file",
      open(os.path.join(state, "items", "B.010.x.md")).read() == "# item body\n")
shutil.rmtree(root, ignore_errors=True)

# 4. a gate file OLDER than the launch is not this run's result — acting on it would report a
#    stale verdict as if it were the new one.
root, state, d, q, D = build(started_ago=-3600)   # launched an hour in the FUTURE of the file
d.collect_gates(q)
check("MUST-BITE  a gate file older than the launch is ignored, not acted on",
      not d.posts and not d.events, (d.posts, d.events))
check("  and the item stays gated, waiting", q["items"][0]["status"] == "gated", q["items"][0]["status"])
shutil.rmtree(root, ignore_errors=True)

# 5. if the prompt cannot be posted, the item must NOT be left claiming a rework was sent
root, state, d, q, D = build()
d.post_prompt = lambda sid, text: False
d.collect_gates(q)
check("MUST-BITE  a failed POST escalates instead of silently marking rework",
      len(d.escalations) == 1 and q["items"][0]["status"] != "rework",
      (d.escalations, q["items"][0]["status"]))
check("  and says the item file was already updated, so BOSS knows the state",
      "item file was updated" in d.escalations[0], d.escalations[0])
shutil.rmtree(root, ignore_errors=True)

# 6. a gate that never wrote a file is escalated once, not waited on forever
root, state, d, q, D = build()
os.remove(os.path.join(state, "gates", "B.010.x.md"))
d.state["autogate"]["B.010.x"]["started"] = time.time() - 4 * 3600
d.collect_gates(q)
check("a gate with no file after 3h is escalated", len(d.escalations) == 1, d.escalations)
check("  and is dropped, so it is not escalated every tick",
      "B.010.x" not in d.state["autogate"], d.state["autogate"])
shutil.rmtree(root, ignore_errors=True)

print("\nAUTOGATE DRIVER " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
