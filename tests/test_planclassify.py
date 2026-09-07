"""A PLAN filed as a REPORT, because neither marker started its line.

Measured on the live board 2026-09-07 03:41:02. EXEC-D wrote:

    Posting the PLAN block. PLAN READY: B.010.circleci-three-reds-triage (EXEC-D)
    ...
    Never merge; end REPORT READY or QUESTION.

PLAN_RE is line-anchored, so the mid-line "PLAN READY" at offset 24 did not match. REPORT_RE matches
ANYWHERE, so it matched the handbook's own closing instruction at offset 1765 — and the plan was
classified REPORT_READY. Consequences: the plan review never runs, and if such a plan happened to
name a report path that exists at the lane head, the gate would launch on an unbuilt item.

The fixture is the REAL message, saved from the session, not a reconstruction — a paraphrase would
be testing my summary of the bug instead of the bug.

Hermetic: no server, no queue, no gate."""
import importlib.util, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location(
    "dpl" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); spec.loader.exec_module(D)

def kind(text, after_compaction=False):
    m = {"info": {"id": "m", "role": "assistant", "time": {"created": 1, "completed": 1}},
         "parts": [{"type": "text", "text": text}]}
    return D.Dispatcher.classify(m, after_compaction)[0]

REAL = open(os.path.join(HERE, "fixtures", "exec_d_plan_midline.txt"), errors="ignore").read()
check("MUST-BITE  EXEC-D's REAL 03:41:02 message is a PLAN, not a report", kind(REAL) == "PLAN_READY",
      kind(REAL))
check("  the fixture really is the shape that broke it: neither marker at a line start",
      not D.PLAN_RE.search(REAL) and not D.REPORT_LINE_RE.search(REAL))
check("  with PLAN first and REPORT far later",
      D.PLAN_ANY_RE.search(REAL).start() < D.REPORT_RE.search(REAL).start(),
      (D.PLAN_ANY_RE.search(REAL).start(), D.REPORT_RE.search(REAL).start()))

# the anchored rule still outranks position — this is what the new branch must not break
check("MUST-BITE  a line-start REPORT READY still wins over a mid-line PLAN mention",
      kind("I wrote the PLAN READY block earlier.\nREPORT READY: audit/x/reports/y.md") == "REPORT_READY",
      kind("I wrote the PLAN READY block earlier.\nREPORT READY: audit/x/reports/y.md"))
check("MUST-BITE  a line-start PLAN READY still wins over a later report mention",
      kind("PLAN READY\n1. do the thing\nthen end at REPORT READY.") == "PLAN_READY")
check("  and a plain report is still a report",
      kind("REPORT READY: audit/x/reports/y.md @ abc1234") == "REPORT_READY")

# the mirror case: mid-line REPORT first, PLAN mentioned later
check("MUST-BITE  mid-line REPORT first, PLAN narrated later, is still a REPORT",
      kind("Done — REPORT READY: audit/x/reports/y.md. This followed the PLAN READY block above.")
      == "REPORT_READY",
      kind("Done — REPORT READY: audit/x/reports/y.md. This followed the PLAN READY block above."))

# a post-compaction summary must not become a PLAN either
# The control that caught a regression in my first version of this fix: without `not
# after_compaction` on the new branch, a summary NARRATING both markers came out PLAN_READY — a
# description of past work read as a claim about the present, which is exactly what that guard is for.
SUMMARY = "Summary: I posted PLAN READY, then REPORT READY, then stopped."
check("MUST-BITE  a post-compaction summary narrating both markers is NEITHER a plan nor a report",
      kind(SUMMARY, after_compaction=True) not in ("REPORT_READY", "PLAN_READY"),
      kind(SUMMARY, after_compaction=True))
check("  and the same text OUTSIDE compaction is still read normally",
      kind(SUMMARY) == "PLAN_READY", kind(SUMMARY))

print(("FAILED: " + ", ".join(fails)) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
