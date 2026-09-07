"""`False` was doing two jobs, and a real report disappeared under the wrong one.

BOSS, 2026-09-07: three finished reports (EXEC-J 04:57, EXEC-H 05:10, one earlier) were swallowed as
IDLE_ACK — "holds no dispatched, rework or reported item" — and each was found by hand. The reason is
one value: `_report_verdict[1] = False` meant BOTH

  * the trigger judged this text is not a report at all  (an idle ack: withdraw the row, correctly)
  * this IS a report and there was nothing to gate it against  (a finished artifact with nothing
    pointing at it — the case that most needs a human)

and the withdrawal keyed on `is False`, so the second wore the first's clothes. The board then said
"idle ack, not a report" about a report.

Three states now. The one that is easy to lose is the middle one: a report arriving while its OWN
gate runs is withdrawn too — a verdict is already coming and a row would only ask BOSS to wait for
something already happening — but it is recorded as REPORT_WHILE_GATED, because filing a real report
under "there was no report" is the same confusion in a smaller place.

Hermetic: a Dispatcher built with __new__, a hand-built queue, no HTTP and no gate."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL
spec = importlib.util.spec_from_file_location("drh", os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); sys.modules["drh"] = D; spec.loader.exec_module(D)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def dp(verdict):
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {"stale_seen": {}}
    d.events = []
    d.emit = lambda *a: d.events.append(a)
    d.log = lambda *a, **k: None
    d._report_verdict = ("m1", verdict, "why-text")
    return d

def kinds(d):
    return [e[0] for e in d.events]

# --- BOSS's case: a real report nothing could gate KEEPS its row --------------------------------
d = dp(D.UNGATEABLE)
row = {"ses_f": {"executor": "EXEC-F", "kind": "REPORT_READY", "msg_id": "m1"}}
took = d.withdraw_idle_row(row, "EXEC-F", "ses_f", "REPORT_READY", "m1")
check("MUST-BITE  a REAL report with nothing to gate it against KEEPS its pending row — this is the "
      "one that vanished three times on 2026-09-07",
      took is False and "ses_f" in row, (took, list(row)))
check("MUST-BITE  ...and it is NOT recorded as an idle ack: a report filed under `there was no "
      "report` is exactly the confusion that lost it",
      "IDLE_ACK" not in kinds(d), kinds(d))

# --- the idle ack still behaves as it always did ------------------------------------------------
d = dp(False)
row = {"ses_f": {"executor": "EXEC-F", "kind": "REPORT_READY", "msg_id": "m1"}}
took = d.withdraw_idle_row(row, "EXEC-F", "ses_f", "REPORT_READY", "m1")
check("CONTROL  the trigger's own `not a report` verdict still withdraws, still as IDLE_ACK — a fix "
      "that simply stopped withdrawing would pass the check above and hand BOSS back the rows he "
      "was clearing by hand",
      took is True and row == {} and "IDLE_ACK" in kinds(d), (took, row, kinds(d)))

# --- the middle state: withdrawn, but under its own name ----------------------------------------
d = dp(D.GATE_RUNNING)
row = {"ses_f": {"executor": "EXEC-F", "kind": "REPORT_READY", "msg_id": "m1"}}
took = d.withdraw_idle_row(row, "EXEC-F", "ses_f", "REPORT_READY", "m1")
check("MUST-BITE  a report arriving while its OWN gate runs is withdrawn — a row would ask BOSS to "
      "wait for a verdict that is already coming",
      took is True and row == {}, (took, row))
check("MUST-BITE  ...but recorded as REPORT_WHILE_GATED, never IDLE_ACK — the report was real",
      "REPORT_WHILE_GATED" in kinds(d) and "IDLE_ACK" not in kinds(d), kinds(d))

# --- a gated report still keeps its row (True is unchanged) -------------------------------------
d = dp(True)
row = {"ses_f": {"executor": "EXEC-F", "kind": "REPORT_READY", "msg_id": "m1"}}
check("CONTROL  a True verdict is untouched by all of this",
      d.withdraw_idle_row(row, "EXEC-F", "ses_f", "REPORT_READY", "m1") is False and "ses_f" in row)

# --- a verdict from another message never touches this row --------------------------------------
d = dp(False)
check("CONTROL  a verdict recorded for a DIFFERENT message withdraws nothing",
      d.withdraw_idle_row({"ses_f": {}}, "EXEC-F", "ses_f", "REPORT_READY", "m-other") is False)

# --- the reason reaches BOSS instead of being computed and dropped ------------------------------
d = D.Dispatcher.__new__(D.Dispatcher)
d.state = {}
d.roster = {"EXEC-F": "ses_f"}
d.c = lambda k, default=None: {"escalate_after_minutes": [10, 20], "escalation_repeat_minutes": 20,
                               "executors": {}}.get(k, default)
d.escalations = []
d.escalate = lambda t: d.escalations.append(t)
d.notify_owner = lambda *a, **k: None
d.log = lambda *a, **k: None
d.hold_reason = lambda name: None
d.undelivered_answer = lambda name: None
d.boss_activity_since = lambda ms: ("NO_SIGHT", "BOSS has not been active")
d._nonretryable_400 = lambda p: False
import time as _t
pending = {"ses_f": {"executor": "EXEC-F", "kind": "REPORT_READY", "msg_id": "m1",
                     "since_ms": (_t.time() - 25 * 60) * 1000, "since_local": "05:10",
                     "escalated": 0, "esc_count": 0, "last_esc_min": 0,
                     "esc_base_ms": (_t.time() - 25 * 60) * 1000,
                     "not_gated": "EXEC-F holds no dispatched, rework or reported item"}}
d.age_and_escalate(pending)
check("MUST-BITE  the escalation SAYS why nothing gated it — otherwise it reads `REPORT_READY "
      "waiting 25 min` and BOSS has to go and find out what we already computed",
      d.escalations and "NOT GATED" in d.escalations[0]
      and "holds no dispatched" in d.escalations[0], d.escalations[:1])
# CONTROL: an ordinary row must not grow the clause, or it means nothing wherever it appears.
d.escalations = []
# A FRESH row, not a copy of the one above: age_and_escalate MUTATES the row it escalates
# (escalated=2, esc_count=1), so a copy would decline to escalate at all and the control would pass
# on an empty list — a check that cannot fail, in the file about output that cannot fail to look fine.
plain = {"ses_g": {"executor": "EXEC-G", "kind": "REPORT_READY", "msg_id": "m2",
                   "since_ms": (_t.time() - 25 * 60) * 1000, "since_local": "05:10",
                   "escalated": 0, "esc_count": 0, "last_esc_min": 0,
                   "esc_base_ms": (_t.time() - 25 * 60) * 1000}}
d.roster = {"EXEC-G": "ses_g"}
d.age_and_escalate(plain)
check("CONTROL  a row with no such reason does not grow the clause — and it DID escalate, so the "
      "control is about the clause and not about an escalation that never happened",
      d.escalations and "NOT GATED" not in d.escalations[0], d.escalations[:1])

# --- CAN THE PATH REACH THE GUARD? -------------------------------------------------------------
# Everything above tests withdraw_idle_row with a verdict I hand it. That proves the guard works when
# reached and says NOTHING about whether feed() ever sets UNGATEABLE on the real paths. Measured:
# reverting those two assignments to `False` was MISSED by every check above and by test_gatesilence.
# So these two drive feed() itself, and they are the checks that actually defend BOSS's three lost
# reports.
import json, shutil, tempfile, time as _time

def fed(status, dispatched_to="EXEC-F"):
    """feed() a real REPORT_READY through a temp CN, and hand back the verdict it recorded."""
    root = tempfile.mkdtemp(prefix="held-")
    state = os.path.join(root, "test-logs", "driver")
    for sub in ("gates", "items"):
        os.makedirs(os.path.join(state, sub))
    os.makedirs(os.path.join(root, "wt", "audit", "plan-execution-2026-09-04", "reports"))
    open(os.path.join(root, "wt", "audit", "plan-execution-2026-09-04", "reports",
                      "B.010.ci-collection-floor-r12.md"), "w").write("# report\n")
    os.environ["CN"] = root
    sp = importlib.util.spec_from_file_location(
        "dfed" + str(_time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
    M = importlib.util.module_from_spec(sp); sp.loader.exec_module(M)
    d = M.Dispatcher.__new__(M.Dispatcher)
    d.state = {"handled": {}, "autogate": {}, "parks": {}, "auto": {}, "pending": {},
               "gate_skipped": {}, "stale_seen": {}}
    d.dry = False
    d.events, d.escalations, d.posts = [], [], []
    d.emit = lambda *f: d.events.append(f)
    d.escalate = lambda m: d.escalations.append(m)
    d.log = lambda m: None
    d.post_prompt = lambda sid, text: (d.posts.append((sid, text)) or True)
    d.roster = {"EXEC-F": "ses_f"}
    d.c = lambda k, default=None: default
    d.gates_running = lambda: 0
    d.eligible_item = lambda q, name: None
    q = {"items": [{"id": "B.010.ci-collection-floor", "status": status,
                    "dispatched_to": dispatched_to, "worktree": os.path.join(root, "wt"),
                    "lane": "lane/ci"}]}
    d.feed(q, "EXEC-F", "ses_f", "REPORT_READY", "m1",
           "REPORT READY: `audit/plan-execution-2026-09-04/reports/B.010.ci-collection-floor-r12.md` "
           "@ `e66e442f`\nINTERRUPT-TEST: platform/tests/test_x.py::test_interrupted")
    shutil.rmtree(root, ignore_errors=True)
    return d, M

# (a) the exact sentence from 04:57 and 05:10: the row is parked, the report is finished work
d, M = fed("parked")
check("MUST-BITE  feed() itself records UNGATEABLE for a report on a PARKED row — reverting this "
      "assignment to False is invisible to every check above, and it is what lost the reports",
      d._report_verdict[1] == M.UNGATEABLE, d._report_verdict)
row = {"ses_f": {"executor": "EXEC-F", "kind": "REPORT_READY", "msg_id": "m1"}}
check("  ...so the row survives the withdrawal the tick runs next",
      d.withdraw_idle_row(row, "EXEC-F", "ses_f", "REPORT_READY", "m1") is False and "ses_f" in row)

# (b) an executor holding nothing this report could be gated against
d, M = fed("merged")
check("MUST-BITE  feed() records UNGATEABLE when the executor holds no dispatched, rework or "
      "reported item — BOSS's `holds no dispatched...` sentence, which rendered as an idle ack",
      d._report_verdict[1] == M.UNGATEABLE, d._report_verdict)

# (c) CONTROL: an idle ack down the same function still lands on False, or the two assignments above
# could simply have been made unconditional.
d, M = fed("rework")
d._report_verdict = None
d.feed({"items": [{"id": "B.010.ci-collection-floor", "status": "rework", "dispatched_to": "EXEC-F",
                   "worktree": "/nonexistent", "lane": "lane/ci"}]},
       "EXEC-F", "ses_f", "REPORT_READY", "m2",
       "REPORT READY: idle — lane clean (merged 6b542f7), no lock held. Awaiting next dispatch.")
check("CONTROL  an idle ack through the SAME function is still False, not UNGATEABLE — otherwise "
      "the fix is `never withdraw anything` wearing a new name",
      d._report_verdict[1] is False, d._report_verdict)

# --- the tick keeps the row AND stamps the reason ------------------------------------------------
src = open(os.path.join(HERE, os.pardir, "dispatcher.py"), errors="ignore").read()
i = src.find("if kind in FEED_KINDS and fed_this_tick < max_feed:")
window = src[i:i + 1400]
check("MUST-BITE  the tick stamps the reason onto the row it keeps — measured in the call site, "
      "because a reason that never reaches `pending` cannot reach BOSS",
      i != -1 and 'pending[sid]["not_gated"]' in window and "UNGATEABLE" in window, i)

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
