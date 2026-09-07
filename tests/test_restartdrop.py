"""What a daemon restart drops, and what it must not.

Three defects, all of which look like nothing at all from outside:

  1. A REPORT_READY that arrives while the daemon is restarting is handled by NEITHER process — the
     old one writes handled[sid] and exits, the new one reads that map and skips it. Measured live
     by BOSS 2026-09-07 (EXEC-G, B.014a.artifact-custody-r3, 02:55:26): a real report at the lane
     head, gated by nobody, with no event saying so.
  2. roster.json was read ONCE at startup, so a threshold or a flag changed in the file did nothing
     until a restart — while being described as live.
  3. current_item() matched only `dispatched`, so an item in `rework` was nobody's current work:
     autogate.GATEABLE_STATUS has always listed rework, but feed() could never reach the branch.

Hermetic: a temp CN, no server (message fetches are stubbed), no gate, no box, no git."""
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

AG = load("agr", "autogate.py")

REPORT_TXT = ("REPORT READY: B.014a.artifact-custody-r3 — report at "
              "audit/plan-execution/reports/B.014a.artifact-custody-r3.md, proofs green.")
IDLE_TXT = "REPORT READY: idle — lane clean (merged 6b542f7), no lock held. Awaiting next dispatch."

# ------------------------------------------------------------------ the rule
ok, why = AG.recovery_ok({"status": "reported"}, REPORT_TXT, lambda r: True)
check("MUST-BITE  a `reported` item with a real report path IS recoverable", ok, why)
ok, why = AG.recovery_ok({"status": "rework"}, REPORT_TXT, lambda r: True)
check("  so is one still in rework", ok, why)
ok, why = AG.recovery_ok({"status": "reported"}, IDLE_TXT, lambda r: True)
check("MUST-BITE  an IDLE-ACK carrying the marker is NOT recovered — the same evidence the live "
      "trigger demands", not ok and "no report path" in why, why)
ok, why = AG.recovery_ok({"status": "reported"}, REPORT_TXT, lambda r: False)
check("MUST-BITE  a named report that does not EXIST at the lane head is not recovered",
      not ok and "none exist" in why, why)
ok, why = AG.recovery_ok({"status": "merged"}, REPORT_TXT, lambda r: True)
check("a merged item is not recovered", not ok and "merged" in why, why)
# These two lists agreed on `reported` from 03:2x onward — see test_autogate.py for why it joined
# the automatic list. What recovery still allows and the live trigger does not is the SILENCE around
# it: recovery runs with no message to judge beyond the one already handled.
check("recovery and the live trigger agree on `reported`, and neither recovers a finished item",
      "reported" in AG.RECOVERABLE_STATUS and "reported" in AG.GATEABLE_STATUS
      and not any(s in AG.RECOVERABLE_STATUS for s in ("merged", "held", "done")),
      (AG.RECOVERABLE_STATUS, AG.GATEABLE_STATUS))

# ------------------------------------------------------------------ harness
def tree(roster=None):
    root = tempfile.mkdtemp(prefix="restart-")
    os.makedirs(os.path.join(root, "dispatcher"))
    state = os.path.join(root, "test-logs", "driver")
    for d in ("gates", "items"):
        os.makedirs(os.path.join(state, d))
    os.makedirs(os.path.join(root, "wt", "audit", "plan-execution", "reports"))
    open(os.path.join(root, "wt", "audit", "plan-execution", "reports",
                      "B.014a.artifact-custody-r3.md"), "w").write("# report\n")
    json.dump(roster if roster is not None else {"poll_seconds": 5, "stall_after_seconds": 90},
              open(os.path.join(root, "dispatcher", "roster.json"), "w"))
    return root, state

def msg(mid, text, ms=None):
    return {"info": {"id": mid, "role": "assistant", "time": {"created": ms or time.time() * 1000,
                                                              "completed": ms or time.time() * 1000}},
            "parts": [{"type": "text", "text": text}]}

def daemon(root, msgs, handled, status="rework"):
    os.environ["CN"] = root
    D = load("dmr", "dispatcher.py")
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {"handled": handled, "autogate": {}}
    d.dry = False
    d.posts, d.events, d.escalations, d.logs = [], [], [], []
    d.emit = lambda *f: d.events.append(f)
    d.escalate = lambda m: d.escalations.append(m)
    d.log = lambda m: d.logs.append(m)
    d.roster = {"EXEC-G": "ses_g"}
    d.cfg = {}
    d.c = lambda k, default=None: default
    d.gates_running = lambda: 0
    d.sha_at_lane_head = lambda item: (True, "26b38cfe1f26b38cfe1f")
    d.last_messages = lambda sid, n: msgs
    q = {"items": [{"id": "B.014a.artifact-custody-r3", "status": status,
                    "dispatched_to": "EXEC-G", "worktree": os.path.join(root, "wt"),
                    "lane": "lane/014a-assets", "proof_files": ["platform/tests/test_x.py"]}]}
    return D, d, q

def run(D, d, q):
    popens = []
    D.subprocess.Popen = lambda argv, **kw: popens.append(argv)
    d.recover_missed_reports(q)
    return popens

# ------------------------------------------------------------------ the drop
root, state = tree()
m = msg("msg_r3", REPORT_TXT)
D, d, q = daemon(root, [m], {"ses_g": "msg_r3"})
popens = run(D, d, q)
check("MUST-BITE  a report handled by a daemon that died IS gated on the next startup",
      len(popens) == 1 and "B.014a.artifact-custody-r3" in popens[0], popens)
check("  and it is recorded as REPORT_RECOVERED, so the drop is visible in the log",
      [e[0] for e in d.events] == ["MANUAL_GATE", "REPORT_RECOVERED"] or
      "REPORT_RECOVERED" in [e[0] for e in d.events], [e[0] for e in d.events])
d2 = daemon(root, [m], {"ses_g": "msg_r3"})[1]
d2.state["recovered"] = {"B.014a.artifact-custody-r3": "msg_r3"}
d2.gates_running, d2.sha_at_lane_head = (lambda: 0), (lambda i: (True, "26b38cfe1f26b38cfe1f"))
d2.last_messages = lambda sid, n: [m]
p2 = []
D.subprocess.Popen = lambda argv, **kw: p2.append(argv)
d2.recover_missed_reports(q)
check("MUST-BITE  a report already recovered by an earlier restart is not gated again",
      p2 == [], p2)

# an UNHANDLED report is left to the ordinary path, not double-gated
D, d, q = daemon(root, [m], {"ses_g": "msg_other"})
check("MUST-BITE  an UNHANDLED report is left alone — the normal tick owns it", run(D, d, q) == [], "")

# a gate file newer than the report means it was already gated
D, d, q = daemon(root, [m], {"ses_g": "msg_r3"})
open(os.path.join(state, "gates", "B.014a.artifact-custody-r3.md"), "w").write("# GATE\n")
check("MUST-BITE  a gate file newer than the report blocks recovery — never twice",
      run(D, d, q) == [], "")
os.remove(os.path.join(state, "gates", "B.014a.artifact-custody-r3.md"))

# an idle-ack must not be resurrected
D, d, q = daemon(root, [msg("msg_idle", IDLE_TXT)], {"ses_g": "msg_idle"})
check("MUST-BITE  an idle-ack is not gated by the recovery pass either", run(D, d, q) == [], "")

# a gate in flight
D, d, q = daemon(root, [m], {"ses_g": "msg_r3"})
d.state["autogate"] = {"B.014a.artifact-custody-r3": {"sha": "x", "started": time.time()}}
check("a gate already in flight blocks recovery", run(D, d, q) == [], "")

# runs once per process
# Measured by the WORK it does, not by the launch it produces: the `recovered` map would suppress a
# second launch on its own, so counting launches cannot tell a once-per-process guard from no guard.
D, d, q = daemon(root, [m], {"ses_g": "msg_r3"})
calls = []
inner = d.last_messages
d.last_messages = lambda sid, n: (calls.append(sid) or inner(sid, n))
run(D, d, q)
d.recover_missed_reports(q); d.recover_missed_reports(q)
check("MUST-BITE  the pass runs ONCE per process — later ticks do not even fetch messages",
      len(calls) == 1, len(calls))
shutil.rmtree(root, ignore_errors=True)

# ------------------------------------------------------------------ current_item covers rework
root, state = tree()
D, d, q = daemon(root, [m], {}, status="rework")
check("MUST-BITE  an item in `rework` IS the executor's current item",
      (d.current_item(q, "EXEC-G") or {}).get("id") == "B.014a.artifact-custody-r3",
      d.current_item(q, "EXEC-G"))
q["items"][0]["status"] = "merged"
check("  a merged item is not", d.current_item(q, "EXEC-G") is None)
q["items"][0]["status"] = "dispatched"
check("  a dispatched one still is", d.current_item(q, "EXEC-G") is not None)

# ------------------------------------------------------------------ roster.json per tick
rp = os.path.join(root, "dispatcher", "roster.json")
D, d, q = daemon(root, [m], {})
d.cfg = {}
d.reload_cfg()
check("MUST-BITE  with no override it reads the REAL roster.json, not a test file",
      d.cfg.get("stall_after_seconds") == 90 and "_comment" in d.cfg, sorted(d.cfg)[:4])
d.cfg, d._cfg_mtime, d.cfg_path = {}, None, rp
d.reload_cfg()
check("MUST-BITE  reload_cfg reads roster.json", d.cfg.get("stall_after_seconds") == 90, d.cfg)
d.events.clear()
d.reload_cfg()
check("  an unchanged file is a stat, not an event", d.events == [], d.events)
time.sleep(0.02)
json.dump({"poll_seconds": 5, "stall_after_seconds": 45, "auto_gate": False}, open(rp, "w"))
os.utime(rp, (time.time() + 1, time.time() + 1))
d.reload_cfg()
check("MUST-BITE  a CHANGED value is picked up without a restart",
      d.cfg.get("stall_after_seconds") == 45 and d.cfg.get("auto_gate") is False, d.cfg)
check("  and the change is named in the event, both old and new",
      d.events and "90" in str(d.events[-1]) and "45" in str(d.events[-1]), d.events[-1:])
time.sleep(0.02)
open(rp, "w").write("{ truncated")
os.utime(rp, (time.time() + 2, time.time() + 2))
d.events.clear()
try:
    d.reload_cfg(); kept = (d.cfg or {}).get("stall_after_seconds")
except Exception as e:
    kept = f"{type(e).__name__}: {e}"
check("MUST-BITE  a half-written roster.json is REFUSED, not applied — it must never blank the config",
      kept == 45 and bool(d.events) and d.events[0][0] == "ROSTER_RELOAD_REFUSED",
      (kept, [e[0] for e in d.events]))
shutil.rmtree(root, ignore_errors=True)

# ------------------------------------------------------------------ the 90 s stall threshold
# BOSS set 90 s on 2026-09-07 after a 15-minute sample found healthy pauses at 40 s and 48 s that a
# 5-minute sample had missed. The number is pinned here so a future edit has to face the measurement:
# the sample's censoring limit is a LOWER bound, so moving BELOW 90 needs more sampling, not a
# smaller number.
root, state = tree()
live = json.load(open(os.path.join(HERE, os.pardir, "roster.json")))
check("MUST-BITE  the live stall threshold is 90 s, as measured", live.get("stall_after_seconds") == 90,
      live.get("stall_after_seconds"))
D, d, q = daemon(root, [m], {})
d.cfg = {"stall_after_seconds": 90}
d.c = lambda k, default=None: d.cfg.get(k, default)
check("  a 48 s pause — the longest healthy one measured — is under the threshold",
      48 < float(d.c("stall_after_seconds", 120)))
check("  and 120 s is still over it", 120 > float(d.c("stall_after_seconds", 120)))
shutil.rmtree(root, ignore_errors=True)

print(("FAILED: " + ", ".join(fails)) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
