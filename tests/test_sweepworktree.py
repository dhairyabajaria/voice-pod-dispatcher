"""mergegate.run_citesweep_isolated — a box-free gate sweeps a DETACHED checkout, not the lane.

The lane worktree is where the daemon dispatches the lane's NEXT item, so a gate that never took the
box was resolving citations against a tree that may carry uncommitted work or later commits (BOSS,
2026-09-06, on the 21:29 gate's row). Hermetic: builds its own git repo, no box, no network."""
import importlib.util, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("mg6", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mg6"] = MG; spec.loader.exec_module(MG)
sys.path.insert(0, os.path.join(HERE, os.pardir))

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def git(*a, cwd): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
def write(root, rel, txt):
    p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(txt)

REPORT = ("# REPORT B.010.x — 010.x — 2026-09-06 IST\n\n## 1. Branch\n"
          "Migration `247_thing.sql` and `247:120` are the citations.\n")

def lane():
    """A lane worktree with a report and the migration it cites, committed."""
    root = tempfile.mkdtemp(prefix="sweepwt-")
    wt = os.path.join(root, "lane"); os.makedirs(wt)
    git("init", "-q", "-b", "main", cwd=wt)
    git("config", "user.email", "t@t", cwd=wt); git("config", "user.name", "t", cwd=wt)
    write(wt, "audit/plan-execution-2026-09-04/reports/B.010.x-010.x-r1.md", REPORT)
    write(wt, "platform/db/migrations/247_thing.sql", "-- x\n" * 400)
    git("add", "-A", cwd=wt); git("commit", "-qm", "candidate", cwd=wt)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True).stdout.strip()
    MG.CN = root
    return root, wt, sha

def sweep(wt, sha):
    rows = []
    MG.GATES = os.path.join(os.path.dirname(wt), "gates"); os.makedirs(MG.GATES, exist_ok=True)
    ok = MG.run_citesweep_isolated(wt, "B.010.x", None, "010.x", sha, rows.append)
    return ok, (rows[0] if rows else "")

# 1. the clean case: it sweeps a detached checkout, says so, and leaves nothing behind
root, wt, sha = lane()
ok, row = sweep(wt, sha)
check("the sweep runs and resolves the citations", ok is True and "citations:" in row, row[:120])
check("  the row says a DETACHED CHECKOUT at the sha, not the lane worktree",
      f"a detached checkout at {sha[:10]}" in row and "LANE worktree" not in row, row[-90:])
left = os.path.join(root, "voicepod-cites-B.010.x")
wtlist = subprocess.run(["git", "worktree", "list"], cwd=wt, capture_output=True, text=True).stdout
check("  the temporary worktree is GONE from disk", not os.path.exists(left), left)
check("  and is not left registered in git", "voicepod-cites" not in wtlist, wtlist)
clean_row = row
shutil.rmtree(root, ignore_errors=True)

# 2. THE POINT OF THE CHANGE: a dirty lane worktree does not change the result
root, wt, sha = lane()
os.remove(os.path.join(wt, "platform/db/migrations/247_thing.sql"))          # the lane's next build
write(wt, "audit/plan-execution-2026-09-04/reports/B.010.x-010.x-r1.md",
      REPORT + "\nAnd a later, uncommitted citation to `999:1`.\n")
ok, row = sweep(wt, sha)
check("a DIRTY lane worktree does not change the sweep result",
      row.split(" — resolved")[0] == clean_row.split(" — resolved")[0], f"{row[:110]!r}")
check("  the deleted migration is still resolved (it exists at the sha)", "0 unresolved" in row, row[:110])
check("  the uncommitted extra citation is not counted", "1 checked" in row or "2 checked" in row, row[:110])
check("  and the temporary worktree is gone again",
      not os.path.exists(os.path.join(root, "voicepod-cites-B.010.x")))
shutil.rmtree(root, ignore_errors=True)

# 3. a checkout that cannot be built falls back to the lane, and SAYS why
root, wt, sha = lane()
ok, row = sweep(wt, "0" * 40)
check("an unbuildable checkout falls back to the LANE worktree",
      "LANE worktree" in row and "NOT a detached checkout" in row, row[-140:])
check("  and names the reason rather than implying a clean sweep",
      "could not be built" in row, row[-140:])
shutil.rmtree(root, ignore_errors=True)

print("\nSWEEP WORKTREE " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
