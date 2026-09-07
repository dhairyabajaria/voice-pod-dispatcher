"""A PLAN READY / REPORT READY that lands inside a provider-cut turn (2026-09-06 20:45, EXEC-D).

Fixture: fixtures/execd_cut_turn.json — the real message that was declared cut at 20:45:44, then
COMPLETED at 20:50:55 with a full PLAN READY, plus the dispatcher's own RESUME as the last message.
Hermetic: the API is stubbed, nothing is posted, no live state is read or written."""
import importlib.util, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("dlive", os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); sys.modules["dlive"] = D; spec.loader.exec_module(D)
FX = json.load(open(os.path.join(HERE, "fixtures", "execd_cut_turn.json")))["messages"]
SID = "ses_f92c5cfedffeYU6iVctg0oiskT"

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def mk(resumed=True):
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.dry, d.once = True, True
    d.state = {"pending": {}, "handled": {}, "stall": {}, "stall_resumed": ({SID: time.time()} if resumed else {})}
    d.events, d.reviews, d.prompts = [], [], []
    d.emit = lambda *f: d.events.append(tuple(str(x) for x in f))
    d.log = lambda *a: None
    d.escalate = lambda t: None
    d.c = lambda k, dflt=None: dflt
    d.last_messages = lambda sid, n: list(FX)
    d.start_plan_review = lambda *a: d.reviews.append(a)
    d.post_prompt = lambda sid, txt: (d.prompts.append(txt) or True)
    return d

Q = {"items": [{"id": "B.010.gap-answer-portal-guard-r8", "status": "dispatched",
                "dispatched_to": "EXEC-D", "artifact": "010.gap-answer-portal-guard"}]}

# The fixture must really be the shape the bug needs, or the test proves nothing.
last = FX[-1]["info"]
check("fixture: the LAST message is the dispatcher's RESUME, not the PLAN READY",
      last["role"] == "user", last["id"])
check("fixture: the PLAN READY message COMPLETED (it was not dead, only unseen)",
      bool(FX[0]["info"]["time"]["completed"]) and D.PLAN_RE.search(FX[0]["parts"][0]["text"]) is not None)

# 1. it is found, exactly once, and the plan review is started.
# The flag is redirected to a temp path: a test that reads test-logs/driver/ passes or fails for
# reasons that have nothing to do with the code, and must never write there either.
import tempfile
D.PLANREVIEW_FLAG = os.path.join(tempfile.mkdtemp(prefix="cutmarker-"), "PLANREVIEW")
open(D.PLANREVIEW_FLAG, "w").close()
d = mk()
lab = d.recover_missed_marker("EXEC-D", SID, Q)
kinds = [e[0] for e in d.events]
check("the PLAN READY completed behind the RESUME is recovered", kinds.count("PLAN_READY") == 1, str(kinds))
check("  the event says RECOVERED", any("RECOVERED" in e[4] for e in d.events if e[0] == "PLAN_READY"))
check("  the plan review is started for the dispatched item",
      len(d.reviews) == 1 and d.reviews[0][2] == "B.010.gap-answer-portal-guard-r8", str(d.reviews))
check("  the board label says so", "recovered" in (lab or ""), str(lab))
n = len(d.events)
d.recover_missed_marker("EXEC-D", SID, Q)
check("  a second tick does not re-fire it", len(d.events) == n)

# 2. it never looks past the last message unless WE resumed this session
d = mk(resumed=False)
check("no resume on record -> no scan at all", d.recover_missed_marker("EXEC-D", SID, Q) is None and not d.events)

# 3. a message this daemon already handled is not re-announced
d = mk(); d.state["handled"][SID] = FX[0]["info"]["id"]
check("an already-handled message is skipped", d.recover_missed_marker("EXEC-D", SID, Q) is None and not d.events)

# 4. check_stall announces a marker that HAD landed in the cut turn ...
def cut_msg(text):
    return {"info": {"id": "msg_cut", "role": "assistant", "time": {"created": 0}, "finish": None},
            "parts": ([{"type": "text", "text": text}] if text else [])}
def stall_twice(d, m):
    d.stall_shape = lambda mm: (True, "fp")
    d.check_stall("EXEC-D", SID, m, Q["items"][0])          # first sighting: records
    d.state["stall"][SID]["since"] = time.time() - 300      # age it past the threshold
    d.state["stall"][SID]["polls"] = 9
    return d.check_stall("EXEC-D", SID, m, Q["items"][0])
d = mk(resumed=False); stall_twice(d, cut_msg("PLAN READY — something\nbody"))
check("a cut turn that ALREADY carried the marker logs CUT_TURN_MARKER",
      [e[0] for e in d.events].count("CUT_TURN_MARKER") == 1, str([e[0] for e in d.events]))

# 5. ... and honestly does NOT fire on the 20:45 shape, which had no text at all.
d = mk(resumed=False); stall_twice(d, cut_msg(""))
check("the real 20:45 shape (no text at stall time) does NOT fire it — part-scanning alone would "
      "have missed this incident", "CUT_TURN_MARKER" not in [e[0] for e in d.events],
      str([e[0] for e in d.events]))
check("  (and it is the STALLED_TURN path that runs instead)", "STALLED_TURN" in [e[0] for e in d.events])

# 6. the resume prompt tells the executor to re-post
check("RESUME prompt asks for a verbatim re-post of a cut PLAN/REPORT READY",
      "RE-POST IT VERBATIM" in D.STALL_RESUME_PROMPT and "LAST message" in D.STALL_RESUME_PROMPT)

print("\nCUT MARKER " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
