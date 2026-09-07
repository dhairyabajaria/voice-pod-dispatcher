"""The queue row is a prediction; the report is what the lane says it actually proved.

BOSS, 2026-09-07: he shipped three items with no `proof_files` and the gate declared NOT DECLARED on
work that was measured. His words: "the executor knows what it proved; my row is a prediction." So
the gate asks the REPORT before declaring NOT DECLARED.

The load-bearing part is not the fallback, it is the EXISTENCE FILTER and the disclosure. A report
can name a test file that was renamed, never committed, or invented; adopting one hands pytest a
path that does not exist, and that red wears the shape of a red about the candidate. And a
substitution nobody can see is a guess — the gate says in a row, and again in the proofs detail,
that the files came from the report and not from the queue.

Hermetic: a temp worktree, no board, no git.
"""
import importlib.util, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location("mgr", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mgr"] = MG; spec.loader.exec_module(MG)

WT = tempfile.mkdtemp(prefix="reportproofs-")
os.makedirs(os.path.join(WT, "platform", "tests"))
# SIX present files, not two. A mutation replacing `sorted(out)` with `list(out)` scored MISSED
# against a two-file fixture: set iteration order matched sorted order often enough to pass. Order
# is not cosmetic — it is the pytest argv and the row text, and it must not vary between runs of the
# same gate. With six names the chance of an unsorted set landing in sorted order is 1 in 720.
for f in ("test_present.py", "test_also_present.py", "test_zulu.py", "test_alpha.py",
          "test_mike.py", "test_bravo.py"):
    open(os.path.join(WT, "platform", "tests", f), "w").write("# a real test file\n")

REPORT = """## 6. PROOFS
Ran `platform/tests/test_zulu.py`, tests/test_mike.py, platform/tests/test_present.py — 7 passed.
Also tests/test_also_present.py, tests/test_bravo.py, platform/tests/test_alpha.py,
and platform/tests/test_renamed_away.py from the earlier round.
"""

got = MG.proofs_from_report(REPORT, WT)
check("MUST-BITE  the report's test files are adopted, with or without the platform/ prefix",
      set(got) == {"tests/test_" + n + ".py" for n in
                   ("present", "also_present", "zulu", "alpha", "mike", "bravo")}, got)
check("MUST-BITE  and the order is SORTED, not set-iteration order — this list becomes the pytest "
      "argv and the text of the row, and it must not vary between two runs of the same gate",
      got == sorted(got), got)
check("MUST-BITE  a file the report NAMES but the candidate does not contain is excluded — adopting "
      "it hands pytest a missing path, and that red reads as a red about the candidate",
      "tests/test_renamed_away.py" not in got, got)
check("MUST-BITE  CONTROL: that file really was named in the report text, so the check above is not "
      "passing because the pattern simply missed it",
      "test_renamed_away.py" in REPORT and
      "tests/test_renamed_away.py" in [m.group(1) for m in MG.REPORT_PROOF_RE.finditer(REPORT)],
      [m.group(1) for m in MG.REPORT_PROOF_RE.finditer(REPORT)])
check("MUST-BITE  CONTROL: a report naming no test files adopts nothing — the fallback must not "
      "invent a proof set out of prose",
      MG.proofs_from_report("## 6. PROOFS\nRan the suite. All green.\n", WT) == [], "")
check("  CONTROL: an empty report is handled without raising",
      MG.proofs_from_report("", WT) == [] and MG.proofs_from_report(None, WT) == [], "")

# and when the report gives us nothing, the NOT DECLARED status still fires: the fallback must not
# have replaced an honest refusal with a silent empty pass.
rows = []
MG._run_proofs({"proof_files": []}, WT, "sha", "head", "B.test",
               lambda n, ok, d: rows.append((n, ok, d)))
check("MUST-BITE  with no proofs from either source the row is still NOT DECLARED — the fallback "
      "adds a source, it does not remove the refusal",
      rows and rows[-1][1] is MG.UNDECLARED, rows[-1:])

shutil.rmtree(WT, ignore_errors=True)
print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
