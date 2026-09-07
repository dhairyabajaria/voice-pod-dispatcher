"""A report that flips itself out of the match set, and a skip that shouts every 5 seconds.

BOSS, 2026-09-07 03:26:43, on the new build:
  `AUTO_GATE_SKIPPED EXEC-F … item=- EXEC-F holds no dispatched or rework item` — about the very
  item that had just reported. feed() sets status `reported` on the REPORT READY message BEFORE the
  auto-gate check runs, and both current_item() and report_trigger_ok() excluded `reported`. So every
  report auto-flipped ITSELF out of the match set: the gate could fire on the first pass and NEVER
  again — which is exactly when a corrected second report arrives. BOSS hand-launched r14.

  And EXEC-D/EXEC-E emitted a skip every tick on the same stale message ids: the tick re-feeds an
  already-handled idle executor on every pass, so a skip raised inside feed() fires every 5 seconds
  into the log BOSS reads.

Hermetic: temp CN, no server, no gate, no box."""
import importlib.util, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def load(mod, fname):
    spec = importlib.util.spec_from_file_location(
        mod + str(time.time_ns()), os.path.join(HERE, os.pardir, fname))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

AG = load("agf", "autogate.py")
REPORT = ("REPORT READY: `audit/plan-execution-2026-09-04/reports/r14.md` @ `2b64f195`\n"
          "INTERRUPT-TEST: platform/tests/test_floor.py::test_interrupted_between_writes\n")
IDLE = "REPORT READY: idle — nothing to do."

# ---------------------------------------------------------------- the rule
ok, why = AG.report_trigger_ok({"status": "reported"}, REPORT, lambda r: True)
check("MUST-BITE  a `reported` item with a real report path IS gateable — the flip happens first",
      ok, why)
check("  dispatched and rework still are",
      all(AG.report_trigger_ok({"status": s}, REPORT, lambda r: True)[0] for s in ("dispatched", "rework")))
ok, why = AG.report_trigger_ok({"status": "merged"}, REPORT, lambda r: True)
check("CONTROL  a merged item is still refused", not ok and "merged" in why, why)
ok, why = AG.report_trigger_ok({"status": "reported"}, IDLE, lambda r: True)
check("MUST-BITE  and the idle-ack protection did NOT move with it — the path evidence still refuses",
      not ok and "no report path" in why, why)

# ---------------------------------------------------------------- end to end, twice
def build(status="rework"):
    root = tempfile.mkdtemp(prefix="flip-")
    state = os.path.join(root, "test-logs", "driver")
    for d in ("gates", "items"):
        os.makedirs(os.path.join(state, d))
    os.makedirs(os.path.join(root, "wt", "audit", "plan-execution-2026-09-04", "reports"))
    open(os.path.join(root, "wt", "audit", "plan-execution-2026-09-04", "reports", "r14.md"), "w").write("#\n")
    os.environ["CN"] = root
    D = load("dfl", "dispatcher.py")
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {"handled": {}, "autogate": {}, "parks": {}, "auto": {}, "pending": {}}
    d.dry = False
    d.posts, d.events, d.logs, d.launched = [], [], [], []
    d.emit = lambda *f: d.events.append(f)
    d.escalate = lambda m: None
    d.log = lambda m: d.logs.append(m)
    d.post_prompt = lambda sid, text: (d.posts.append((sid, text)) or True)
    d.roster = {"EXEC-F": "ses_f"}
    d.cfg = {}
    d.c = lambda k, default=None: default
    d.gates_running = lambda: 0
    d.sha_at_lane_head = lambda item: (True, "2b64f19526b38cfe")
    d.eligible_item = lambda q, name: None
    D.subprocess.Popen = lambda argv, **kw: d.launched.append(argv)
    q = {"items": [{"id": "B.010.ci-collection-floor", "status": status, "dispatched_to": "EXEC-F",
                    "worktree": os.path.join(root, "wt"), "lane": "lane/ci",
                    "proof_files": ["platform/tests/test_x.py"]}]}
    return root, d, q

# BOSS's exact sequence: report -> refused (a build with no interrupt line) -> corrected report
root, d, q = build()
d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "m1",
       "REPORT READY: `audit/plan-execution-2026-09-04/reports/r14.md` @ `2b64f195`\n")
check("the first, incomplete report is refused and the item is left `reported`",
      q["items"][0]["status"] == "reported" and not d.launched, (q["items"][0]["status"], d.launched))
check("MUST-BITE  while `reported` the item is STILL the executor's current work — this is the "
      "match BOSS's 03:26:43 skip could not make",
      (d.current_item(q, "EXEC-F") or {}).get("id") == "B.010.ci-collection-floor",
      d.current_item(q, "EXEC-F"))
d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "m2", REPORT)
check("MUST-BITE  the CORRECTED report on that same `reported` item LAUNCHES — this is the one that "
      "could never fire", len(d.launched) == 1, (len(d.launched), [e[0] for e in d.events]))
check("  and the item moves on to `gated`, so a third report does not re-launch it",
      q["items"][0]["status"] == "gated" and d.current_item(q, "EXEC-F") is None,
      q["items"][0]["status"])
shutil.rmtree(root, ignore_errors=True)

# a merged item is NOT the executor's current work — the exclusion that keeps this safe
root, d, q = build(status="merged")
check("CONTROL  a merged item is not current, so no report can gate it",
      d.current_item(q, "EXEC-F") is None)
shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- one skip per message
root, d, q = build(status="merged")          # no current item: BOSS's item=- skip
for _ in range(5):
    d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "stale_msg", REPORT)
skips = [e for e in d.events if e[0] == "AUTO_GATE_SKIPPED"]
check("MUST-BITE  five ticks on the SAME stale message emit ONE skip, not five",
      len(skips) == 1, len(skips))
d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "another_msg", REPORT)
check("  a NEW message speaks again", len([e for e in d.events if e[0] == "AUTO_GATE_SKIPPED"]) == 2,
      [e[3] for e in d.events if e[0] == "AUTO_GATE_SKIPPED"])
check("MUST-BITE  a changed CAUSE on the same message speaks again — dedupe must not silence news",
      d.skip_gate("EXEC-F", "ses_f", "another_msg", "item=-", "a completely different reason") is True)
check("  and repeating that cause does not",
      d.skip_gate("EXEC-F", "ses_f", "another_msg", "item=-", "a completely different reason") is False)
shutil.rmtree(root, ignore_errors=True)

# the executor is corrected once, not every 5 seconds
root, d, q = build()
NO_LINE = "REPORT READY: `audit/plan-execution-2026-09-04/reports/r14.md` @ `2b64f195`\n"
for _ in range(4):
    d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "m1", NO_LINE)
check("MUST-BITE  the executor is told what to add ONCE, not once per tick", len(d.posts) == 1, len(d.posts))
check("  and the skip was logged once too",
      len([e for e in d.events if e[0] == "AUTO_GATE_SKIPPED"]) == 1,
      [e[0] for e in d.events])
shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- a report that lands MID-GATE
# BOSS's ruling 2026-09-07: `gated` stays OUT of CURRENT_STATUS — the gate in flight owns the sha.
# But then the item is nobody's current work, and the old message told the executor it "holds no
# dispatched or rework item", which is false and invites it to resend the report it just sent.
root, d, q = build(status="gated")
d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "m9", REPORT)
skips = [e for e in d.events if e[0] == "AUTO_GATE_SKIPPED"]
check("MUST-BITE  a report arriving MID-GATE is skipped, not gated again",
      len(skips) == 1 and not d.launched, (skips, d.launched))
check("MUST-BITE  and the cause NAMES the running gate's item, not 'holds no item'",
      "B.010.ci-collection-floor" in str(skips[0][4]) and "already running" in str(skips[0][5]),
      skips[0][4:])
# "wait for the verdict" became "wait;" on 2026-09-07 03:5x, when the note also had to say "do NOT
# reply": EXEC-F answered the first version with "waiting", and that one-word turn drew three
# AUTO-CONTINUEs and a STUCK. See test_waitongate.py for the cascade this wording is half of.
check("MUST-BITE  the executor is told to WAIT, and explicitly not to resend or reply",
      len(d.posts) == 1 and "Do NOT resend" in d.posts[0][1] and "do NOT reply" in d.posts[0][1]
      and "wait" in d.posts[0][1],
      d.posts[0][1][:160] if d.posts else d.posts)
for _ in range(3):
    d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "m9", REPORT)
check("  and told once, not every tick",
      len(d.posts) == 1 and len([e for e in d.events if e[0] == "AUTO_GATE_SKIPPED"]) == 1,
      (len(d.posts), len([e for e in d.events if e[0] == "AUTO_GATE_SKIPPED"])))
root3, d3, q3 = build(status="merged")
d3.feed(q3, "EXEC-F", "ses_f", "REPORT_READY", "mz", REPORT)
check("CONTROL  an executor with NO item at all still gets the other message, and no wait note",
      "holds no dispatched" in str(d3.events[-1][-1]) and d3.posts == [], (d3.events[-1][-1], d3.posts))
shutil.rmtree(root3, ignore_errors=True)
shutil.rmtree(root, ignore_errors=True)

print(("FAILED: " + ", ".join(fails)) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
