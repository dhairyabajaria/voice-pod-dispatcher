"""An executor holding a dispatched item while producing nothing must wake BOSS.

BOSS, 2026-09-07: EXEC-H sat silent for ~5 HOURS on an empty resume turn and was found by hand.
check_stall did not cover it, and the reason is the interesting part: once check_stall has posted
its one RESUME it returns "STALLED (resumed, watching)" on every later tick and never escalates
again. Watching is not a state anyone is told about, so a session that stays dead after its one
resume sits forever under a reassuring label.

check_dead is deliberately a SEPARATE, later judgement: check_stall decides whether to resume a
turn, this decides whether to wake a person. It fires on the fact that survives every explanation —
an item was dispatched, and for N minutes there has been no text, no running tool, no completion.

The negative controls carry as much weight as the must-bite: this escalation wakes BOSS, and one
that cries wolf on a healthy executor mid-pytest is one BOSS learns to ignore, which costs the real
one its signal. Hermetic: no server, no daemon, no clock dependency beyond a synthetic age."""
import importlib.util, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
import fixtures                      # item 9: the shared state redirect
spec = importlib.util.spec_from_file_location("dd", os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); sys.modules["dd"] = D; spec.loader.exec_module(D)
fixtures.redirect_state(D)   # item 9: never the LIVE state dir

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

ITEM = {"id": "B.010.example"}

def ms_ago(minutes):
    return int((time.time() - minutes * 60) * 1000)

def asst(minutes, parts=None, completed=False, error=None):
    info = {"id": f"m_a_{minutes}", "role": "assistant", "time": {"created": ms_ago(minutes)}}
    if completed:
        info["time"]["completed"] = ms_ago(minutes - 1)
    if error:
        info["error"] = error
    return {"info": info, "parts": parts if parts is not None else []}

def user(minutes, text="RESUME"):
    return {"info": {"id": f"m_u_{minutes}", "role": "user", "time": {"created": ms_ago(minutes)}},
            "parts": [{"type": "text", "text": text}]}

def daemon(compacting=False):
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {}
    d.events, d.escalations = [], []
    d.emit = lambda *f: d.events.append(f)
    d.escalate = lambda msg: d.escalations.append(msg)
    d.c = lambda k, default=None: {"dead_after_minutes": 30}.get(k, default)
    d.compaction_active = lambda sid: "cid" if compacting else None
    return d

# 1. THE MUST-BITE: EXEC-H's shape — an empty assistant turn, hours old, item dispatched
d = daemon()
lab = d.check_dead("EXEC-H", "ses_h", [user(300), asst(299)], ITEM)
check("MUST-BITE  an empty assistant turn older than the threshold escalates", lab is not None, lab)
check("  a STUCK event is emitted", [e[0] for e in d.events] == ["STUCK"], [e[0] for e in d.events])
ev = " ".join(str(x) for x in d.events[0])
check("  naming the executor, the item and how long", "EXEC-H" in ev and "B.010.example" in ev and "299" in ev, ev[:190])
check("  and BOSS is escalated to, not just logged at", len(d.escalations) == 1, d.escalations)
check("  the event says no auto-action was taken",
      "BOSS must decide" in ev and "No auto-action" in d.escalations[0], ev[:190])

# 2. FIRES ONCE. An escalation repeated every 5s is one nobody reads.
lab2 = d.check_dead("EXEC-H", "ses_h", [user(300), asst(299)], ITEM)
check("MUST-BITE  it fires ONCE per silent stretch, not once per tick",
      len(d.events) == 1 and len(d.escalations) == 1 and "escalated" in lab2, (len(d.events), lab2))

# 3. THE CONTROLS — each is a way this could cry wolf on a healthy executor
d = daemon()
running = [{"type": "tool", "tool": "bash", "state": {"status": "running"}}]
check("CONTROL  a busy executor mid-tool-call does NOT trip it",
      d.check_dead("EXEC-A", "ses_a", [user(300), asst(299, running)], ITEM) is None)
check("CONTROL  an executor that is producing TEXT does not trip it",
      d.check_dead("EXEC-A", "ses_a", [user(300), asst(299, [{"type": "text", "text": "working"}])], ITEM) is None)
check("CONTROL  a young empty turn does not trip it (inside the threshold)",
      d.check_dead("EXEC-A", "ses_a", [user(10), asst(9)], ITEM) is None)
check("CONTROL  a COMPLETED turn is not dead — other branches own it",
      d.check_dead("EXEC-A", "ses_a", [user(300), asst(299, completed=True)], ITEM) is None)
check("CONTROL  an ERRORED turn is not dead — the ERROR path owns it",
      d.check_dead("EXEC-A", "ses_a", [user(300), asst(299, error={"name": "APICallError"})], ITEM) is None)
check("MUST-BITE  an executor with NO dispatched item is idle, not stuck",
      d.check_dead("EXEC-A", "ses_a", [user(300), asst(299)], None) is None)
check("CONTROL  no events or escalations came from any of those", not d.events and not d.escalations,
      (d.events, d.escalations))

# 4. a session that never answered at all: no assistant message, aged from our own prompt
d = daemon()
lab = d.check_dead("EXEC-H", "ses_h", [user(120)], ITEM)
check("MUST-BITE  a session that produced NO assistant turn at all also escalates", lab is not None, lab)
check("  and the event says which of the two shapes it was",
      "no assistant turn at all" in " ".join(str(x) for x in d.events[0]), d.events[0][-1][:120])

# 5. a compaction is not death — COMPACTING owns that case, and a RESUME/escalation there is noise
d = daemon(compacting=True)
check("MUST-BITE  a session mid-compaction is not reported dead",
      d.check_dead("EXEC-D", "ses_d", [user(300), asst(299)], ITEM) is None)
check("  and nothing was emitted for it", not d.events and not d.escalations)

# 5b. once known dead, later ticks must not re-pay the compaction API call
d = daemon()
calls = []
real = d.compaction_active
d.compaction_active = lambda sid: (calls.append(sid), None)[1]
msgs = [user(300), asst(299)]
d.check_dead("EXEC-H", "ses_h", msgs, ITEM)
d.check_dead("EXEC-H", "ses_h", msgs, ITEM)
d.check_dead("EXEC-H", "ses_h", msgs, ITEM)
check("three ticks on a known-dead session cost ONE compaction lookup", len(calls) == 1, calls)

# 6. empty input must not crash the tick
d = daemon()
check("no messages at all does not fire and does not raise", d.check_dead("EXEC-A", "ses_a", [], ITEM) is None)
check("  and neither does None", d.check_dead("EXEC-A", "ses_a", None, ITEM) is None)

print("\nDEAD EXEC " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
