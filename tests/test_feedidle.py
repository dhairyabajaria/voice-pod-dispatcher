"""An executor that stopped on a progress line and was handled once could never be fed again.

2026-09-07: EXEC-M sat at `idle:PROGRESS_STOP` for 88 minutes. Two items were assigned to it by
name, then re-queued unassigned, then two more added. None of it could ever have reached it.

The PROGRESS_STOP branch that feeds an idle executor sits BELOW `state["handled"][sid] = msg_id`, so
it runs only on the first tick that classifies a message. Every later tick enters the already-handled
guard, which gated on `FEED_KINDS = ("REPORT_READY", "QUESTION", "ACK")` — PROGRESS_STOP is not in it
— matched nothing, wrote an age label and continued. The fix I had already written for exactly this
constant was therefore unreachable from the second tick onward, and my own comment describing the
bug sat beside code that still had it, one branch earlier.

The class, in BOSS's words: a guard excluded from the proof set is indistinguishable from no guard. A
test for the first-tick branch passes; the path never runs. **So the case that matters here is the
ALREADY-HANDLED second tick, not a fresh stop** — which is the state every real executor is in by
the time any of this matters.

Hermetic: a real Dispatcher instance with current_item/gated_item_for stubbed, no network.
"""
import importlib.util, os, sys, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location("dspf", os.path.join(HERE, os.pardir, "dispatcher.py"))
DSP = importlib.util.module_from_spec(spec); sys.modules["dspf"] = DSP; spec.loader.exec_module(DSP)

class Stub(DSP.Dispatcher):
    def __init__(self, gated=None):
        self.cfg = {}; self.state = {}; self.dry = True
        self._gated = gated
    def gated_item_for(self, q, name): return self._gated

Q = {"items": []}
d = Stub()

check("MUST-BITE  an already-handled PROGRESS_STOP with no item IS fed — the 88-minute state, and "
      "the whole defect: the first-tick branch that handles this is unreachable by then",
      d.feed_when_idle(Q, "EXEC-M", "PROGRESS_STOP", None) is True, "")
check("MUST-BITE  so is STUCK, which my own comment named in the same breath as PROGRESS_STOP",
      d.feed_when_idle(Q, "EXEC-M", "STUCK", None) is True, "")
check("MUST-BITE  CONTROL: the three original FEED_KINDS still feed — the fix must not have replaced "
      "the old condition with a new one",
      all(d.feed_when_idle(Q, "E", k, None) is True for k in DSP.FEED_KINDS), DSP.FEED_KINDS)
check("MUST-BITE  CONTROL: an executor that HOLDS AN ITEM is never fed, whatever the kind — feeding "
      "a busy executor a second item is the failure this guard exists to prevent",
      not any(d.feed_when_idle(Q, "E", k, {"id": "B.x"})
              for k in ("PROGRESS_STOP", "STUCK", *DSP.FEED_KINDS)), "")
check("MUST-BITE  an executor WAITING ON ITS OWN GATE is not fed at PROGRESS_STOP — it holds no "
      "current item but it is not free, and handing it a new one would start a build behind a "
      "verdict it has not seen",
      Stub(gated={"id": "B.gated"}).feed_when_idle(Q, "E", "PROGRESS_STOP", None) is False, "")
check("MUST-BITE  CONTROL: the gate check applies ONLY to the new kinds — widening the old ones "
      "would be an unrequested behaviour change smuggled into a fix",
      all(Stub(gated={"id": "B.gated"}).feed_when_idle(Q, "E", k, None) is True for k in DSP.FEED_KINDS),
      DSP.FEED_KINDS)
check("  a kind nobody listed is still not fed, so the predicate is a whitelist and not a default-yes",
      d.feed_when_idle(Q, "E", "ERROR", None) is False, "")
check("MUST-BITE  CONTROL: PROGRESS_STOP is genuinely absent from FEED_KINDS — if it were ever added "
      "there, the first two checks above would pass for a reason that has nothing to do with this fix",
      "PROGRESS_STOP" not in DSP.FEED_KINDS and "STUCK" not in DSP.FEED_KINDS, DSP.FEED_KINDS)

# the call site must actually use it: a predicate nobody calls is the same shape as the bug — a fix
# that exists and a path that never reaches it.
src = open(os.path.join(HERE, os.pardir, "dispatcher.py")).read()
blk = src[src.index('if self.state["handled"].get(sid) == msg_id:'):][:1200]
check("MUST-BITE  the ALREADY-HANDLED block calls the predicate — the bug was a correct fix in a "
      "branch this block shadows, so the fix has to live where the shadowing happens",
      "self.feed_when_idle(q, name, kind, cur)" in blk, blk[:80])
check("  and no longer gates that feed on FEED_KINDS alone",
      "if kind in FEED_KINDS and fed_this_tick" not in blk, "")

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
