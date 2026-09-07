"""A timed-out proof run said "NO test result is implied" while its own log held 277 passes.

2026-09-07, B.015a.journey-sole-scheduler: the gate hit its 3600s wall at 87% and reported
`proofs: FAIL — NOT RUN: ... this is a GATE defect, not a candidate result. NO test result is
implied.` The log it had just written said `collected 314 items` and 277 progress characters, all
dots, zero failures. The claim was false and the evidence was already on disk.

BOSS's counterfactual is the reason this is a defect and not a tuning knob: had those dots been
`FF`, the row would have been BYTE-IDENTICAL. A genuine red and a slow green were indistinguishable,
in the bad direction — a real failure reading as "the gate saw nothing". Output that cannot
distinguish its own causes, with the distinguishing information captured, complete, and discarded.

Three states now, the same shape as the NOT DECLARED split: failures in the partial log are a real
FAIL about the candidate; no failures is a GATE limit and never a rework; nothing parseable is the
old line, which is then actually true.

Hermetic: log text as fixtures, no pytest, no box.
"""
import importlib.util, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location("mgt", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mgt"] = MG; spec.loader.exec_module(MG)

# The REAL log's shape, wrapping included, taken from the 08:17 run.
REAL = """HEAD abc status:[] token=t scratch=/x start=t
collected 314 items

tests/test_campaign_journey_scheduler_ownership.py ..................... [  6%]
......                                                     \x20             [  8%]
tests/test_tenant_graph_integrity.py ..........            \x20             [ 11%]
tests/test_migration_runner.py ..........................................[ 24%]
tests/test_erasure.py ..........................
"""

pt = MG.partial_pytest(REAL)
check("MUST-BITE  the partial log is parsed at all — `collected` and the progress characters",
      pt["collected"] == 314 and pt["done"] > 0, pt)
check("MUST-BITE  WRAPPED continuation lines are counted. pytest wraps at the terminal width and the "
      "continuation carries no filename; a filename-anchored count saw 143 of the real log's 277 and "
      "would have understated the work by nearly half while looking precise",
      pt["done"] == 21 + 6 + 10 + 42 + 26, pt)
# progress CHARACTERS only, on filename-headed lines only — the old rule. Counting the whole
# remainder would include the `[ 24%]` marker and pad the number, which is how my first version of
# this control managed to be larger than the thing it was supposed to be smaller than.
anchored = sum(sum(1 for c in m.group(1) if c in ".FEsxXu") for m in
               (re.match(r"^tests/\S+\.py\s+(.*)$", l) for l in REAL.split("\n")) if m)
check("MUST-BITE  CONTROL: a filename-anchored count really is LOWER on this fixture, so the check "
      "above is measuring the wrap handling and not passing by coincidence",
      anchored < pt["done"], (anchored, pt["done"]))

# --- the three states, read off the function the gate actually calls -----------------------------
ok, d = MG.timeout_row(pt, "/x.log", 5400)
check("MUST-BITE  no failures in the partial output is NOT RUN, not FAIL — the lane must not be "
      "reworked for the gate's own wall",
      ok is None and "GATE limit" in d and "not a candidate defect" in d, d[:120])
check("  and it reports both numbers, so a reader can see how close it got",
      f"{pt['done']} of {pt['collected']} test(s)" in d and f"{pt['passed']} passed" in d, d[:90])

bad = dict(pt, failed=2)
okb, db = MG.timeout_row(bad, "/x.log", 5400)
check("MUST-BITE  failures in the partial output are a FAIL about the candidate — this is the case "
      "that was invisible, and the one where the old row read as 'the gate saw nothing'",
      okb is False and "PARTIAL and FAILING" in db, db[:100])
check("MUST-BITE  CONTROL: the two rows are DISTINGUISHABLE — the whole defect was that a red and a "
      "slow green produced byte-identical text",
      d != db and ("FAILING" in db) != ("FAILING" in d), (d[:40], db[:40]))

empty = MG.partial_pytest("HEAD abc\nfatal: something died before collection\n")
oke, de = MG.timeout_row(empty, "/x.log", 5400)
check("MUST-BITE  a log with no progress at all is NOT RUN and says NOTHING was measured — the old "
      "line, which is true only in this third case",
      oke is None and "no pytest progress" in de and "NOTHING about this candidate" in de, de[:100])
check("MUST-BITE  a timeout NEVER yields a pass, in any of the three states",
      all(x is not True for x in (ok, okb, oke)), (ok, okb, oke))

# --- failure characters are recognised, not just dots ---------------------------------------------
mixed = MG.partial_pytest("collected 4 items\n\ntests/test_x.py .F.E\n")
check("MUST-BITE  F and E are counted as failure and error rather than as progress",
      mixed["failed"] == 1 and mixed["error"] == 1 and mixed["passed"] == 2, mixed)

check("  the wall is a named constant and larger than the 4150s this candidate measured, so the "
      "same suite does not immediately hit it again",
      MG.PROOF_TIMEOUT_S >= 4150, MG.PROOF_TIMEOUT_S)

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
