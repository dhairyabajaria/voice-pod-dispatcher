"""run_all.sh must FAIL if a test file exists in its directory that it does not run.

2026-09-06 21:47: I landed test_gate_crashed.py and did not add it to run_all.sh. The suite stayed
green with a check missing from it — an unwired test is invisible, which is the same
absence-reads-as-a-pass shape the tests themselves exist to catch. I found it by remembering to
look. BOSS's ask: make it structural, so it cannot recur silently.

Hermetic: a temp directory with stub files and a COPY of the real run_all.sh, driven with
--census-only so the census is exercised without running the suite.

NOTE on $HERE: run_all.sh derives its directory from its own path (${0:A:h}). Running the copy that
sits in a scratch directory censuses THAT directory — I did exactly this while building the census
and got 27 phantom "unwired" files that were my own staging copies. So the copy under test is placed
in a directory whose contents this test controls completely, and nothing else is put there."""
import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
RUNNER = os.path.join(HERE, "run_all.sh")

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def census(files):
    d = tempfile.mkdtemp(prefix="census-")
    shutil.copy(RUNNER, os.path.join(d, "run_all.sh"))
    for f in files:
        open(os.path.join(d, f), "w").write("import sys\nsys.exit(0)\n")
    p = subprocess.run(["/bin/zsh", os.path.join(d, "run_all.sh"), "--census-only"],
                       capture_output=True, text=True)
    shutil.rmtree(d, ignore_errors=True)
    return p.returncode, p.stdout + p.stderr

# 1. THE MUST-BITE: a file the runner does not run
rc, out = census(["test_fixes.py", "test_totally_unwired.py"])
check("MUST-BITE  an unwired test file makes the runner FAIL", rc != 0, rc)
check("  and the output NAMES the file, so the fix is obvious", "test_totally_unwired.py" in out, out[:220])
check("  and says what to do about it", "Add each to run_all.sh" in out or "not a test" in out, out[:220])
check("  a file the runner DOES run is not named as unwired", "test_fixes.py\n" not in out.split("UNWIRED")[-1].split("Add each")[0], out[:220])

# 2. THE CONTROL: without it, a census that failed on everything would pass check 1.
rc, out = census(["test_fixes.py", "test_cutmarker.py"])
check("CONTROL  a directory whose files are all wired PASSES", rc == 0, (rc, out[:160]))
check("  and says so", "all test files are wired" in out, out[:160])

# 3. an empty directory is not a failure (the glob must not match its own pattern literally)
rc, out = census([])
check("no test files at all is not a failure", rc == 0, (rc, out[:120]))
check("  and does not report the literal glob as a file", "test_*.py" not in out.split("census:")[0], out[:160])

# 4. the real tests directory is clean — the census's own claim about the tree we ship
p = subprocess.run(["/bin/zsh", RUNNER, "--census-only"], capture_output=True, text=True)
check("the SHIPPED tests directory has no unwired files", p.returncode == 0,
      (p.returncode, (p.stdout + p.stderr)[:200]))

print("\nRUNNER CENSUS " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
