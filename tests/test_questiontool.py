"""A question asked through OpenCode's `question` TOOL must raise QUESTION, not read as BUSY.

BOSS, 2026-09-07 01:14: EXEC-D asked through the tool instead of writing "QUESTION" in text. The
turn stays INCOMPLETE while it waits for an answer, so classify() hit its `if not completed: BUSY`
shortcut and the board showed a working executor for an unknown length of time. A question nobody is
told about is the most expensive thing an executor can do: it is idle while looking productive,
which is worse than a stall — a stall eventually escalates, this does not.

Also here: the 01:12 REPORT_READY misfire. Its stated cause (a "## Objective" heading) is NOT what
fired — measured below — so this file pins the real one.

Fixtures are the recorded part shapes. Hermetic: no server, no daemon."""
import importlib.util, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
import fixtures                      # item 9: the shared state redirect
spec = importlib.util.spec_from_file_location("dq", os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); sys.modules["dq"] = D; spec.loader.exec_module(D)
fixtures.redirect_state(D)   # item 9: never the LIVE state dir
FX = json.load(open(os.path.join(HERE, "fixtures", "execd_question_tool.json")))["messages"]
ASK = FX[-1]

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

C = D.Dispatcher.classify

# 1. THE MUST-BITE: the recorded 01:14 message
kind, mid, when, finish, exc = C(ASK)
check("MUST-BITE  a waiting `question` tool part classifies as QUESTION, not BUSY",
      kind == "QUESTION", kind)
check("  the excerpt carries the question text, so BOSS can answer without opening the session",
      "Migration 231 collides" in exc, exc)
check("  and the OPTIONS, which are the whole point of the tool",
      "Renumber to 232" in exc and "Rebase the lane" in exc, exc)

# 2. NEGATIVE CONTROL: an ordinary completed tool part must not fire. Without this, every tool call
#    an executor makes would raise a question and the queue would be unusable.
done = {"info": {"id": "m2", "role": "assistant", "time": {"created": 1, "completed": 2}},
        "parts": [{"type": "tool", "tool": "bash", "state": {"status": "completed", "output": "ok"}}]}
check("CONTROL  an ordinary completed tool part does NOT fire", C(done)[0] != "QUESTION", C(done)[0])
running = {"info": {"id": "m3", "role": "assistant", "time": {"created": 1}},
           "parts": [{"type": "tool", "tool": "bash", "state": {"status": "running"}}]}
check("CONTROL  a RUNNING non-question tool is still BUSY", C(running)[0] == "BUSY", C(running)[0])

# 3. an ANSWERED question is history, not a live one — otherwise every turn re-raises it
answered = {"info": {"id": "m4", "role": "assistant", "time": {"created": 1, "completed": 2}},
            "parts": [{"type": "tool", "tool": "question",
                       "state": {"status": "completed", "output": "Renumber to 232",
                                 "input": {"questions": [{"question": "?"}]}}}]}
check("MUST-BITE  an ANSWERED question tool does not re-raise QUESTION",
      C(answered)[0] != "QUESTION", C(answered)[0])

# 4. only the NEWEST part counts: a question answered earlier in the turn is not the live state
mixed = {"info": {"id": "m5", "role": "assistant", "time": {"created": 1}},
         "parts": [{"type": "tool", "tool": "question",
                    "state": {"status": "completed", "output": "yes", "input": {"questions": [{"question": "old"}]}}},
                   {"type": "tool", "tool": "bash", "state": {"status": "running"}}]}
check("a question answered EARLIER in the same turn does not fire", C(mixed)[0] != "QUESTION", C(mixed)[0])

# 5. a user message is never a question from the executor
user = {"info": {"id": "m6", "role": "user", "time": {"created": 1}}, "parts": ASK["parts"]}
check("CONTROL  the same parts on a USER message are QUEUED, not QUESTION", C(user)[0] == "QUEUED", C(user)[0])

# 6. malformed input must not crash the tick — a classifier that raises takes the daemon's poll down
weird = {"info": {"id": "m7", "role": "assistant", "time": {"created": 1}},
         "parts": [{"type": "tool", "tool": "question", "state": {"status": "pending", "input": {}}}]}
k, _, _, _, e = C(weird)
check("a question tool with no readable questions still fires, and says so",
      k == "QUESTION" and "no readable question" in e, (k, e))

# ---------------------------------------------------------------- the 01:12 REPORT_READY misfire
def asst(text):
    return {"info": {"id": "mX", "role": "assistant", "time": {"created": 1, "completed": 2}},
            "parts": [{"type": "text", "text": text}]}

check("MEASURED  a bare '## Objective' heading never matched REPORT_READY in the first place",
      C(asst("## Objective\n\nRebuild the CI guard.\n"))[0] != "REPORT_READY",
      C(asst("## Objective\n\nRebuild the CI guard.\n"))[0])
SUMMARY = "## Objective\n\nRebuild the CI guard. Prior turns posted REPORT READY for r2 and awaited the gate.\n"
check("MEASURED  the real cause is the summary NARRATING the marker",
      C(asst(SUMMARY))[0] == "REPORT_READY", C(asst(SUMMARY))[0])
check("MUST-BITE  the same text AFTER a compaction is not a report",
      C(asst(SUMMARY), after_compaction=True)[0] != "REPORT_READY",
      C(asst(SUMMARY), after_compaction=True)[0])
check("CONTROL  a REAL report after a compaction is still a report — the guard must not swallow one",
      C(asst("REPORT READY — B.010.x\n\nDone.\n"), after_compaction=True)[0] == "REPORT_READY",
      C(asst("REPORT READY — B.010.x\n\nDone.\n"), after_compaction=True)[0])
check("CONTROL  an ordinary narrated report with no compaction still fires as before",
      C(asst(SUMMARY))[0] == "REPORT_READY")

print("\nQUESTION TOOL " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
