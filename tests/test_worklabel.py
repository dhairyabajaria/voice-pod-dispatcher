"""The board announced two re-assignments that never happened, and BOSS caused them by being helpful.

2026-09-07: `working_item` scans an executor's six newest USER messages for any id the queue already
holds, LONGEST FIRST, so a rework that cites a sibling item for context reports the sibling. BOSS
mentioned B.010.portal-platform-contention-mutex (38 chars) inside EXEC-G's rework and
B.012.published-aggregates-append-mutable (41 chars) inside EXEC-J's; both outran the subject of the
message and the board read "RE-ASSIGNED" for two executors that had switched nothing.

The rule now: IF THE MESSAGE NAMES THE ITEM WE DISPATCHED, THAT IS THE ITEM. Length only breaks ties
between the others — and it must keep doing so, because `…-custody` is a prefix of `…-custody-r3`
and a shortest-first match reports the parent while the executor builds the rework.

What this does NOT fix, and no code can: BOSS's EXEC-G rework wrote "B.014a.artifact-custody: REWORK
4" without the `-r3`, so the id it was about never appeared in the text. That one is closed by the
standing rule that every dispatch names its own item id verbatim.

The wider pattern, second instance in three days: OUR OWN PROMPTS MATCH OUR OWN DETECTORS. Last time
a liveness grep counted Codex processes whose prompt text quoted `./.venv/bin/python -m pytest` as
live pytest runs. Here the detector reads message text we write ourselves.

Hermetic: a stub session object, no daemon, no HTTP.
"""
import importlib.util, os, sys, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location("dsp", os.path.join(HERE, os.pardir, "dispatcher.py"))
DSP = importlib.util.module_from_spec(spec); sys.modules["dsp"] = DSP; spec.loader.exec_module(DSP)

IDS = ["B.014a.artifact-custody", "B.014a.artifact-custody-r3",
       "B.010.portal-platform-contention-mutex", "B.012.trigger-builder",
       "B.012.published-aggregates-append-mutable"]
Q = {"items": [{"id": i} for i in IDS]}

def stub(text):
    """A Dispatcher with just enough state to answer working_item, and no network."""
    s = types.SimpleNamespace(state={})
    s.last_messages = lambda sid, n: [{"info": {"role": "user"},
                                       "parts": [{"text": text}]}]
    s.log = lambda *a, **k: None
    return s

def wi(text, dispatched, msg_id="m1"):
    return DSP.Dispatcher.working_item(stub(text), "sid", Q, msg_id, dispatched=dispatched)

# --- BOSS's actual case ------------------------------------------------------------------------
EXEC_G = ("B.014a.artifact-custody-r3: REWORK 4. Note that B.010.portal-platform-contention-mutex "
          "covers the mutex half of the same defect and is a separate item.")
check("MUST-BITE  a rework that CITES a sibling still reports the item we dispatched — this is the "
      "board saying RE-ASSIGNED for an executor that switched nothing",
      wi(EXEC_G, "B.014a.artifact-custody-r3") == "B.014a.artifact-custody-r3", wi(EXEC_G, "B.014a.artifact-custody-r3"))
check("MUST-BITE  CONTROL: the cited sibling really is LONGER, so this case does exercise the "
      "tie-break rather than passing because the old ordering happened to agree",
      len("B.010.portal-platform-contention-mutex") > len("B.014a.artifact-custody-r3"),
      (len("B.010.portal-platform-contention-mutex"), len("B.014a.artifact-custody-r3")))

# --- a REAL re-assignment must still be seen ------------------------------------------------------
REASSIGN = "Stop B.014a and pick up B.010.portal-platform-contention-mutex instead."
check("MUST-BITE  CONTROL: when the message names ONLY another item, that item still wins — the "
      "preference must not pin the label to the queue and re-create the stale label this exists to fix",
      wi(REASSIGN, "B.012.trigger-builder") == "B.010.portal-platform-contention-mutex",
      wi(REASSIGN, "B.012.trigger-builder"))

# --- longest-first must survive for everything else -----------------------------------------------
PREFIX = "Continue B.014a.artifact-custody-r3 from the review."
check("MUST-BITE  CONTROL: longest-first still breaks ties among the others — `…-custody` is a "
      "PREFIX of `…-custody-r3`, and a shortest match reports the parent while the rework is built",
      wi(PREFIX, "B.012.trigger-builder") == "B.014a.artifact-custody-r3", wi(PREFIX, "B.012.trigger-builder"))

# --- message text is DATA -------------------------------------------------------------------------
check("MUST-BITE  an id the queue does not hold is ignored, so nothing in a message can invent or "
      "rename an item",
      wi("Work on B.999.invented-by-the-message now.", "B.012.trigger-builder") is None,
      wi("Work on B.999.invented-by-the-message now.", "B.012.trigger-builder"))
check("MUST-BITE  and a dispatched id that is NOT in the queue cannot be preferred into existence",
      wi("Nothing relevant here.", "B.998.not-in-queue") is None, "")
# FOUND BY MUTATION, 2026-09-07: dropping the `if dispatched in ids` guard scored MISSED. Every case
# above still passed, because a queue-absent id prepended to the search order only matters when that
# id is ALSO IN THE TEXT — and no case put it there. The guard is the only thing keeping "message
# text is data" true for the dispatched id, and it was untested until the runner said so.
STALE_DISPATCH = ("Finish B.997.retired-item first, then B.012.trigger-builder.")
check("MUST-BITE  a queue-absent dispatched id that DOES appear in the text is still refused — the "
      "preference must not become a way for a stale queue row to name an item that no longer exists",
      wi(STALE_DISPATCH, "B.997.retired-item") == "B.012.trigger-builder",
      wi(STALE_DISPATCH, "B.997.retired-item"))

# --- the cache must not answer with a stale ranking ------------------------------------------------
s = stub(EXEC_G)
a = DSP.Dispatcher.working_item(s, "sid", Q, "m1", dispatched="B.010.portal-platform-contention-mutex")
b = DSP.Dispatcher.working_item(s, "sid", Q, "m1", dispatched="B.014a.artifact-custody-r3")
check("MUST-BITE  the same message with a DIFFERENT dispatched id re-ranks — `dispatched` is part of "
      "the cache key, because it changes while an executor sits silent",
      a == "B.010.portal-platform-contention-mutex" and b == "B.014a.artifact-custody-r3", (a, b))

# --- the label itself ------------------------------------------------------------------------------
# a real subclass, so exec_label reaches the REAL working_item rather than a stand-in: the label is
# the thing BOSS reads, and a test that stubs the lookup out proves nothing about what he sees.
class L(DSP.Dispatcher):
    def __init__(self, text):
        self.state = {}
        self.last_messages = lambda sid, n: [{"info": {"role": "user"}, "parts": [{"text": text}]}]
    def log(self, *a, **k):
        pass

lab = L(REASSIGN)
out = DSP.Dispatcher.exec_label(lab, "EXEC-G", "sid", Q, {"id": "B.012.trigger-builder"}, "(idle)", "m1")
check("MUST-BITE  a genuine re-assignment still prints BOTH ids — the label that made this cost a "
      "minute instead of an hour",
      "RE-ASSIGNED" in out and "B.010.portal-platform-contention-mutex" in out
      and "B.012.trigger-builder" in out, out)
lab2 = L(EXEC_G)
out2 = DSP.Dispatcher.exec_label(lab2, "EXEC-G", "sid", Q, {"id": "B.014a.artifact-custody-r3"},
                                 "(idle)", "m1")
check("MUST-BITE  and BOSS's case prints NO re-assignment at all",
      "RE-ASSIGNED" not in out2 and "B.014a.artifact-custody-r3" in out2, out2)

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
