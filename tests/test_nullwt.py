"""A row with no worktree does not fail to check — it checks the wrong directory.

    os.path.isfile(os.path.join(item.get("worktree") or "", rel))

With no worktree that is `os.path.join("", rel)`, which is `rel`, which os.path resolves against THE
DAEMON'S OWN CWD. The report-path check then reports "none exist at the lane head" — a sentence about
a lane head it never looked at. Usually False; and had the daemon been started from a checkout, TRUE
for somebody else's file. BOSS hit this with a wrong item in the row as well, so the two causes never
separated; this one fires alone.

No worktree means CANNOT CHECK. A refusal, never a path — and the report stays with BOSS, because
"we could not check" is the one verdict a human has to see.

And at the other end: refuse to DISPATCH such a row at all. Keyed on the FIELD, never on the prompt
text — the standing "Rules that bite" boilerplate contains the word `worktree` and sits in 40 of the
item files, so a prose grep passes every row. That guard could not fail, which is the class we keep
finding; `(named in the item prompt)` in the field says the same thing checkably.

Hermetic: a Dispatcher built with __new__, temp dirs, no HTTP."""
import importlib.util, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL
spec = importlib.util.spec_from_file_location("dnw", os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); sys.modules["dnw"] = D; spec.loader.exec_module(D)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

d = D.Dispatcher.__new__(D.Dispatcher)

# --- the checker refuses rather than resolving against the daemon's cwd -------------------------
chk, why = d.path_checker({"id": "B.010.x"})
check("MUST-BITE  no worktree yields NO checker and a reason — an unchecked path must never be "
      "reported as a missing one",
      chk is None and "CANNOT BE CHECKED" in why, (chk, why))
chk2, why2 = d.path_checker({"id": "B.010.x", "worktree": "(named in the item prompt)"})
check("  a row that defers to the prompt is the same refusal, and says which case it is",
      chk2 is None and "defers to the item prompt" in why2, why2)
chk3, why3 = d.path_checker({"id": "B.010.x", "worktree": "/nonexistent/checkout"})
check("MUST-BITE  a worktree that is not on disk is also CANNOT CHECK, not `does not exist` — the "
      "two sentences are about different things and only one of them is about the report",
      chk3 is None and "not evidence that the report is missing" in why3, why3)

# THE DEFECT ITSELF, reproduced: the old expression finds a file in the daemon's cwd.
tmp = tempfile.mkdtemp(prefix="nullwt-")
os.makedirs(os.path.join(tmp, "audit"))
open(os.path.join(tmp, "audit", "report.md"), "w").write("# not the lane head\n")
here = os.getcwd()
try:
    os.chdir(tmp)
    old = os.path.isfile(os.path.join({"worktree": None}.get("worktree") or "", "audit/report.md"))
    real, _ = d.path_checker({"id": "B.010.x"})
    check("MUST-BITE  REPRODUCTION: with no worktree the OLD expression answers True about a file "
          "in the daemon's cwd, and the new one refuses to answer at all",
          old is True and real is None, (old, real))
finally:
    os.chdir(here)
    shutil.rmtree(tmp, ignore_errors=True)

# --- a real worktree still resolves inside it ----------------------------------------------------
wt = tempfile.mkdtemp(prefix="nullwt-ok-")
os.makedirs(os.path.join(wt, "audit"))
open(os.path.join(wt, "audit", "r.md"), "w").write("# report\n")
chk4, why4 = d.path_checker({"id": "B.010.x", "worktree": wt})
check("CONTROL  a row with a real checkout gets a working checker — without this, a refusal that "
      "refused everything would pass every check above",
      chk4 is not None and chk4("audit/r.md") is True and chk4("audit/missing.md") is False,
      (why4, chk4("audit/r.md") if chk4 else None))
shutil.rmtree(wt, ignore_errors=True)

# --- dispatch-time: the row is refused, and the opt-out is honoured -------------------------------
q = {"items": [{"id": "B.010.nowt", "status": "queued"}]}
claim = d.worktree_claim(q, q["items"][0])
check("MUST-BITE  a queued row with NO worktree field is refused at dispatch — everything after "
      "dispatch reads that field, and with it empty each answers about the wrong directory",
      claim and claim[0] == "UNSET", claim)
check("  and the refusal names the one-step fix, both halves of it",
      claim and "`worktree`" in claim[1] and "named in the item prompt" in claim[1], claim[1])
q2 = {"items": [{"id": "B.010.deferred", "status": "queued", "worktree": "(named in the prompt)"}]}
check("MUST-BITE  CONTROL: a row that SAYS it is deferring to the prompt is honoured and dispatches "
      "— the refusal is about an absent decision, not about rows without a checkout",
      d.worktree_claim(q2, q2["items"][0]) is None, d.worktree_claim(q2, q2["items"][0]))
wt2 = tempfile.mkdtemp(prefix="nullwt-live-")
q3 = {"items": [{"id": "B.010.real", "status": "queued", "worktree": wt2}]}
check("CONTROL  an ordinary row with a real checkout still dispatches",
      d.worktree_claim(q3, q3["items"][0]) is None)
shutil.rmtree(wt2, ignore_errors=True)

# --- ORDER: a checkout is only load-bearing once a path has to be resolved -----------------------
# My first version refused on the missing checkout BEFORE reading the text, which turned every idle
# ack on a worktree-less row into a pending row for BOSS — the noise the reportheld work had just
# removed. A control in test_reportheld caught it. The property is pinned HERE, where the refusal is.
import time as _t

def fed(text, worktree):
    root = tempfile.mkdtemp(prefix="nullwt-feed-")
    state = os.path.join(root, "test-logs", "driver")
    for sub in ("gates", "items"):
        os.makedirs(os.path.join(state, sub))
    os.environ["CN"] = root
    sp = importlib.util.spec_from_file_location(
        "dnwf" + str(_t.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
    M = importlib.util.module_from_spec(sp); sp.loader.exec_module(M)
    x = M.Dispatcher.__new__(M.Dispatcher)
    x.state = {"handled": {}, "autogate": {}, "parks": {}, "auto": {}, "pending": {},
               "gate_skipped": {}, "stale_seen": {}}
    x.dry = False
    x.events = []
    x.emit = lambda *f: x.events.append(f)
    x.escalate = lambda m: None
    x.log = lambda m: None
    x.post_prompt = lambda sid, t: True
    x.roster = {"EXEC-F": "ses_f"}
    x.c = lambda k, default=None: default
    x.gates_running = lambda: 0
    x.eligible_item = lambda q, name: None
    it = {"id": "B.010.x", "status": "rework", "dispatched_to": "EXEC-F", "lane": "lane/x"}
    if worktree is not None:
        it["worktree"] = worktree
    x.feed({"items": [it]}, "EXEC-F", "ses_f", "REPORT_READY", "m1", text)
    shutil.rmtree(root, ignore_errors=True)
    return x, M

IDLE = "REPORT READY: idle — lane clean (merged 6b542f7), no lock held. Awaiting next dispatch."
REPORT = ("REPORT READY: `audit/plan-execution-2026-09-04/reports/B.010.x-r1.md` @ `e66e442f`\n"
          "INTERRUPT-TEST: platform/tests/test_x.py::test_interrupted")

x, M = fed(IDLE, None)
check("MUST-BITE  an IDLE ACK on a worktree-less row is still judged `names no report path` — that "
      "verdict needs no checkout, and refusing before reading the text hands BOSS a row per idle ack",
      x._report_verdict[1] is False and "names no report path" in x._report_verdict[2],
      x._report_verdict)
x, M = fed(REPORT, None)
check("MUST-BITE  ...while a report that NAMES a path on the same row is UNGATEABLE, because that "
      "is the verdict the checkout is load-bearing for",
      x._report_verdict[1] == M.UNGATEABLE and "CANNOT BE CHECKED" in x._report_verdict[2],
      x._report_verdict)

# --- the two call sites actually use the checker --------------------------------------------------
# CAN THE PATH REACH THE GUARD. A checker nothing calls is a checker that guards nothing, and the
# defect being fixed lived in the CALL SITES, not in a helper.
src = open(os.path.join(HERE, os.pardir, "dispatcher.py"), errors="ignore").read()
check("MUST-BITE  the old cwd-resolving expression is gone from the whole module — a helper beside "
      "an unchanged call site fixes nothing",
      'os.path.join(it.get("worktree") or "", rel)' not in src
      and 'os.path.join(cur.get("worktree") or "", rel)' not in src)
check("MUST-BITE  feed() records UNGATEABLE when it cannot check, so the report stays with BOSS "
      "instead of being withdrawn as an idle ack",
      "self._report_verdict = (msg_id, UNGATEABLE, cwhy)" in src)
check("MUST-BITE  the restart recovery pass SAYS it could not check instead of taking its silent "
      "path — that silence means `an ordinary old report`, which is a judgement made by looking",
      "RECOVERY_CANNOT_CHECK" in src)

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
