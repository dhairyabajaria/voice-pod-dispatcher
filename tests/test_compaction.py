"""A compaction must not be read as a provider-cut turn (2026-09-06 21:03:59, EXEC-D).

Fixture fixtures/execd_compaction.json is the real three-message window: the pre-compaction
assistant turn, the auto-compaction (user role, `compaction` part, 21:04:52), and the
post-compaction assistant turn as a poll sees it — created, NO parts, not completed, which is
byte-for-byte the shape the stall detector calls a cut. Hermetic: API stubbed, nothing posted."""
import importlib.util, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
import fixtures                      # item 9: the shared state redirect
spec = importlib.util.spec_from_file_location("dcomp", os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); sys.modules["dcomp"] = D; spec.loader.exec_module(D)
fixtures.redirect_state(D)   # item 9: never the LIVE state dir
FX = json.load(open(os.path.join(HERE, "fixtures", "execd_compaction.json")))["messages"]
SID = "ses_f92c5cfedffeYU6iVctg0oiskT"
INERT = FX[-1]                      # the post-compaction turn, as the daemon saw it
ITEM = {"id": "B.010.gap-answer-portal-guard-r8"}

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def mk(window):
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.dry, d.once = True, True
    d.state = {"pending": {}, "handled": {}, "stall": {}, "stall_resumed": {}, "compacting": {}}
    d.events, d.prompts, d.escalations = [], [], []
    d.emit = lambda *f: d.events.append(tuple(str(x) for x in f))
    d.log = lambda *a: None
    d.escalate = lambda t: d.escalations.append(t)
    d.c = lambda k, dflt=None: dflt
    d.last_messages = lambda sid, n: list(window)
    d.post_prompt = lambda sid, txt: (d.prompts.append(txt) or True)
    return d

def stall_twice(d, m):
    """Two polls: the first records the shape, the second is past the threshold and decides."""
    d.check_stall("EXEC-D", SID, m, ITEM)
    if SID in d.state["stall"]:
        d.state["stall"][SID].update(since=time.time() - 300, polls=9)
    return d.check_stall("EXEC-D", SID, m, ITEM)

# The fixture has to be the bug's shape or the test proves nothing.
check("fixture: the middle message is a user turn carrying a `compaction` part",
      FX[1]["info"]["role"] == "user" and any(p["type"] == "compaction" for p in FX[1]["parts"]))
check("fixture: the post-compaction turn has NO parts and is not completed — a cut turn's shape",
      INERT["parts"] == [] and not INERT["info"]["time"]["completed"])
d0 = mk(FX)
check("fixture: stall_shape() really does classify it as a stall (without the guard it fires)",
      d0.stall_shape(INERT)[0] is True)

# 1. the real case: the guard suppresses everything and posts nothing
d = mk(FX)
lab = stall_twice(d, INERT)
kinds = [e[0] for e in d.events]
check("a compacting session is NOT declared stalled", "STALLED_TURN" not in kinds, str(kinds))
check("  no RESUME is posted into the compaction", d.prompts == [], str(d.prompts))
check("  nothing is escalated", d.escalations == [], str(d.escalations))
check("  COMPACTING is emitted once, naming the compaction message",
      kinds.count("COMPACTING") == 1 and FX[1]["info"]["id"] in [e[3] for e in d.events], str(d.events[:1]))
check("  the board says so", "COMPACTING" in (lab or ""), str(lab))
n = len(d.events)
stall_twice(d, INERT)
check("  a later poll does not re-announce it", len(d.events) == n)

# 2. MUST-BITE PRESERVED: an ordinary session with the same inert shape still stalls.
# The newest user message here is a plain prompt, which is the only difference.
plain = [FX[0], {"info": {"id": "msg_plain", "role": "user", "time": {"created": 0}},
                 "parts": [{"type": "text", "text": "RESUME (dispatcher): continue."}]}, INERT]
d = mk(plain)
stall_twice(d, INERT)
kinds = [e[0] for e in d.events]
check("MUST-BITE  a genuine cut turn still fires STALLED_TURN", "STALLED_TURN" in kinds, str(kinds))
check("  and still posts exactly one RESUME", len(d.prompts) == 1)
check("  and COMPACTING is not claimed", "COMPACTING" not in kinds)

# 3. compaction_active() is only consulted at the decision, and reads the NEWEST user message
d = mk(FX)
check("compaction_active finds the compaction", d.compaction_active(SID) == FX[1]["info"]["id"])
d = mk(plain)
check("  and returns None when the newest user message is an ordinary prompt",
      d.compaction_active(SID) is None)
d = mk([FX[1]])         # compaction is the only message in the window
check("  a window of just the compaction still reports it", d.compaction_active(SID) == FX[1]["info"]["id"])

# 4. the episode ends on the first turn that looks alive again
d = mk(FX)
stall_twice(d, INERT)
alive = {"info": {"id": "msg_alive", "role": "assistant", "time": {"created": 0}},
         "parts": [{"type": "text", "text": "continuing the rebase now"}]}
d.check_stall("EXEC-D", SID, alive, ITEM)
check("a live turn clears the compaction episode", SID not in d.state["compacting"])

print("\nCOMPACTION " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
