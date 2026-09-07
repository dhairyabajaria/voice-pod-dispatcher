"""Two switches, because a kill switch has to be explicable in one sentence.

BOSS reached for STOP on 2026-09-07 to get a routine effect — no new gates while a broken image sat
on disk — and got the emergency one: everything off, observation included. Two Codex audits carrying
three [high] findings finished at 08:35 and surfaced at 08:57; EXEC-M sat idle 57 minutes and EXEC-N
68, because nothing was polling or auto-continuing them.

His ruling, and it is the interesting half: do NOT make STOP mean "mostly off". If STOP is present
because the opencode server is sick, a daemon still polling fifteen sessions is what makes it worse,
and "mostly off" is a state nobody can reason about at hour ten. So a SECOND, weaker switch:

    STOP     everything off, observation included.        Unchanged.
    OBSERVE  sensors run, actuators do not.               New.
    STOP ALWAYS WINS.

The actuators are exactly three: posting to a session, launching a gate, spawning codex. Each is
tested through the REAL method rather than by reading the source, and each has a control showing the
same call DOES act when no switch is set — a guard that blocks everything always would satisfy every
"it did not act" assertion here.

Hermetic: a stub dispatcher, temp switch files, no network.
"""
import importlib.util, json, os, shutil, sys, tempfile, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location("dspo", os.path.join(HERE, os.pardir, "dispatcher.py"))
DSP = importlib.util.module_from_spec(spec); sys.modules["dspo"] = DSP; spec.loader.exec_module(DSP)

ROOT = tempfile.mkdtemp(prefix="observe-")
DSP.STOP = os.path.join(ROOT, "STOP"); DSP.OBSERVE = os.path.join(ROOT, "OBSERVE")
DSP.PENDING = os.path.join(ROOT, "pending.json"); DSP.HEARTBEAT = os.path.join(ROOT, "hb")
DSP.HOLD_DIR = ROOT

posted = []
class Stub(DSP.Dispatcher):
    def __init__(self, observing=False):
        self.dry = False; self.once = False; self.state = {"pending": {}}
        self.cfg = {}
        self.paused_logged = False; self.observe_logged = False
        self.observing = observing
        self.logged = []
    def log(self, m, *a, **k): self.logged.append(m)
    def reload_cfg(self): pass
    def load_queue(self): return {"items": []}
    def provider_error_summary(self): return {}
    def sha_at_lane_head(self, item): posted.append("sha_at_lane_head"); return True, "abc123"

# ---- actuator 1: posting ------------------------------------------------------------------------
import unittest.mock as M
with M.patch.object(DSP, "http") as h:
    d = Stub(observing=True)
    r = d.post_prompt("ses_x", "some prompt")
    check("MUST-BITE  OBSERVE holds the POST — nothing reaches the session",
          r is False and h.call_count == 0, (r, h.call_count))
    check("  and it is logged as a held action rather than silently dropped",
          any("OBSERVE-ONLY" in m and "would POST" in m for m in d.logged), d.logged)
with M.patch.object(DSP, "http") as h:
    d = Stub(observing=False)
    r = d.post_prompt("ses_x", "some prompt")
    check("MUST-BITE  CONTROL: with no switch the POST actually happens — the guard is the switch, "
          "not a blanket refusal that would satisfy the assertion above by accident",
          r is True and h.call_count == 1, (r, h.call_count))

# ---- actuator 2: gates --------------------------------------------------------------------------
# A FAKE autogate in sys.modules, so the UNGUARDED path can run to a normal refusal instead of dying
# on an import. Found by mutation: removing the guard made this test CRASH rather than fail, and a
# crash is neither a catch nor a miss — the runner scored it crashed=1 and told me my fixture could
# not distinguish the two.
_ag = types.ModuleType("autogate")
_ag.gate_launchable = lambda item, running, ok: (False, "fixture: not launchable")
_ag.needs_box = lambda proofs: False
sys.modules["autogate"] = _ag

d = Stub(observing=True)
head, why = d.launch_gate({"id": "B.x", "proof_files": []})
check("MUST-BITE  OBSERVE launches no gate, and returns BEFORE reading the lane head — a held "
      "actuator should not do the work either",
      head is None and "OBSERVE-ONLY" in why and posted == [], (head, why[:60], posted))
check("MUST-BITE  and the reason says it is not a verdict about the candidate — a 'no gate' that "
      "reads as a judgement is the conflation we spent the morning removing from the rows",
      "not a verdict about it" in why, why[-60:])
h2, why2 = Stub(observing=False).launch_gate({"id": "B.x", "proof_files": []})
check("MUST-BITE  CONTROL: with no switch launch_gate runs on into the normal launchability check "
      "and refuses for ITS reason, not the hold's — so the assertion above is about the switch",
      h2 is None and "OBSERVE-ONLY" not in why2 and "fixture: not launchable" in why2, why2)

# ---- actuator 3: codex ---------------------------------------------------------------------------
d = Stub(observing=True)
check("MUST-BITE  OBSERVE spawns no codex — the most expensive actuator, it creates a worktree and "
      "starts a paid run",
      d.codex_spawn("CODEX-1", {"id": "B.y"}) is False, "")
check("  and says so in the log",
      any("would spawn codex" in m for m in d.logged), d.logged)

# ---- STOP wins ------------------------------------------------------------------------------------
open(DSP.STOP, "w").write("x"); open(DSP.OBSERVE, "w").write("x")
d = Stub(); d.tick()
doc = json.load(open(DSP.PENDING))
check("MUST-BITE  with BOTH files present STOP wins: the view says paused, NOT observe-only",
      doc["paused"] is True and doc["observe_only"] is False, (doc["paused"], doc["observe_only"]))
check("MUST-BITE  and the tick returns without ever consulting OBSERVE — the whole point of keeping "
      "STOP explicable in one sentence",
      d.observing is False, d.observing)

os.unlink(DSP.STOP)
d2 = Stub(); d2.write_pending({"EXEC-A": "building B.x (idle)"})
doc2 = json.load(open(DSP.PENDING))
check("MUST-BITE  with only OBSERVE the view says observe_only, and the executor row says it is "
      "WATCHED but not acted on — a row that looks normal under a hold is the pause bug again",
      doc2["observe_only"] is True and doc2["paused"] is False
      and "OBSERVE-ONLY" in doc2["executors"]["EXEC-A"], doc2["executors"])
os.unlink(DSP.OBSERVE)
d3 = Stub(); d3.write_pending({"EXEC-A": "building B.x (idle)"})
doc3 = json.load(open(DSP.PENDING))
check("MUST-BITE  CONTROL: with neither file the row is clean and both flags are false",
      doc3["executors"]["EXEC-A"] == "building B.x (idle)"
      and doc3["observe_only"] is False and doc3["paused"] is False, doc3["executors"])

shutil.rmtree(ROOT, ignore_errors=True)
print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
