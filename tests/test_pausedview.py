"""A paused dispatcher rendered an EMPTY executor map, and an empty map reads as a quiet board.

2026-09-07: BOSS paused dispatch to give me a gate-free window, and sized the pause as free because
the status view showed every executor busy. The view is fed by the mechanism he paused. During those
44 minutes two Codex audits finished at 08:35 carrying three [high] findings and raised nothing,
EXEC-M sat idle for 57 minutes and EXEC-N for 68, because nothing was polling or auto-continuing
them — and `write_pending({})` had blanked the executors map, so the board could not have shown any
of it either way.

The same shape as everything else this shift: AN ABSENCE RENDERING AS A FACT. Not-observed and
nothing-to-observe produced identical output.

This does NOT resume observation while paused — that is a decision about what a kill switch means,
and it is BOSS's. It makes the paused view say UNKNOWN, and for how long.

Hermetic: a stub dispatcher, a temp STOP file, no network, no daemon.
"""
import importlib.util, json, os, shutil, sys, tempfile, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location("dspp", os.path.join(HERE, os.pardir, "dispatcher.py"))
DSP = importlib.util.module_from_spec(spec); sys.modules["dspp"] = DSP; spec.loader.exec_module(DSP)

ROOT = tempfile.mkdtemp(prefix="pausedview-")
DSP.STOP = os.path.join(ROOT, "STOP")
DSP.PENDING = os.path.join(ROOT, "pending.json")
DSP.HEARTBEAT = os.path.join(ROOT, "heartbeat")
DSP.HOLD_DIR = ROOT

class Stub(DSP.Dispatcher):
    def __init__(self):
        self.dry = False; self.once = False
        self.state = {"pending": {}}
        self.paused_logged = False
        self.logged = []
    def log(self, m, *a, **k): self.logged.append(m)
    def reload_cfg(self): pass
    def load_queue(self): return {"items": []}
    def provider_error_summary(self): return {}

def tick_paused():
    d = Stub()
    open(DSP.STOP, "w").write("paused")
    d.tick()
    return json.load(open(DSP.PENDING)), d

doc, paused_stub = tick_paused()
check("MUST-BITE  a paused tick still writes the view, and the view is NOT an empty executor map — "
      "an empty map is indistinguishable from a board with nothing happening on it",
      doc["executors"] != {}, doc["executors"])
row = " ".join(doc["executors"].values())
check("MUST-BITE  the row says the state is UNMEASURED, in words, not by omission",
      "PAUSED" in row and "NOT being read" in row, row[:120])
check("MUST-BITE  and it says a quiet board here means unmeasured rather than idle — the exact "
      "inference BOSS drew when he priced the pause as free",
      "UNMEASURED, not idle" in row, row[-120:])
check("MUST-BITE  it names what is NOT happening: no auto-continue, no escalation. EXEC-M sat 57 "
      "minutes and EXEC-N 68 because of precisely those two",
      "auto-continued" in row and "escalation" in row, row[:200])
check("  and it points the reader at the executors' own logs instead of this view",
      "own logs" in row, row[-80:])
check("MUST-BITE  `paused` is still true in the document, so a machine reader is not relying on the "
      "prose either",
      doc["paused"] is True, doc["paused"])
# I wrote this one as `... or True` first — a check that cannot fail, in a file whose whole subject
# is output that cannot fail to look fine. Deleted and replaced with the real assertion.
check("  the pause is still logged, exactly once, on the stub that actually ticked",
      sum(1 for m in paused_stub.logged if "PAUSED" in m) == 1, paused_stub.logged)

# CONTROL: the same writer with real executor status must still render it normally — the change must
# not have replaced the live view with a banner.
d = Stub()
os.unlink(DSP.STOP)
d.write_pending({"EXEC-A": "building B.x (idle)"})
live = json.load(open(DSP.PENDING))
check("MUST-BITE  CONTROL: unpaused, the view still renders the REAL executor rows — the banner is "
      "for the paused path only and must not have swallowed the normal one",
      live["executors"] == {"EXEC-A": "building B.x (idle)"} and live["paused"] is False,
      live["executors"])

shutil.rmtree(ROOT, ignore_errors=True)
print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
