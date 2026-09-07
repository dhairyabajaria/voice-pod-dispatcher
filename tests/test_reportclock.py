"""mergegate.report_clock_row — the report header time vs the wall clock at REPORT READY.

Fixture is the real 2026-09-06 case BOSS hit by hand: EXEC-F's header said 21:00 IST while the
clock read 20:58:52. Advisory by contract, so every assertion also checks it went through warn()
and never touched a PASS/FAIL. Hermetic: no files, no git, no clock dependence (now is injected)."""
import importlib.util, os, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("mg3", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mg3"] = MG; spec.loader.exec_module(MG)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

NOW = datetime(2026, 9, 6, 20, 58, 52)
def run(header):
    rows = []
    ahead = MG.report_clock_row("B.010.x-report.md", header, rows.append, now=NOW)
    return ahead, (rows[0] if rows else "")

# 1. the real case: 21:00 IST at 20:58:52 — 68 s ahead, which no minute-rounding explains
ahead, row = run("# REPORT B.010.ci-collection-floor — 2026-09-06 21:00 IST\n\n## 1. Branch and head sha\n")
check("the 20:58:52 / 21:00 IST case is caught", ahead is not None and ahead > 0 and "AHEAD OF THE WALL CLOCK" in row,
      f"{int(ahead)}s — {row[:110]}")
check("  the row names both times and the file", "21:00" in row and "20:58:52" in row and "B.010.x-report.md" in row)

# 2. a header one minute BEHIND is not a finding, and rounding alone can never trip it
ahead, row = run("# REPORT x — 2026-09-06 20:58 IST\n")
check("a header at 20:58 is consistent, not a finding", "AHEAD" not in row and "consistent" in row, row[:90])
ahead, row = run("# REPORT x — 2026-09-06 20:59 IST\n")
check("a header rounded up to the next minute (59s) does NOT fire", "AHEAD" not in row, row[:90])

# 3. a future DATE is caught even with no time on the header
ahead, row = run("# REPORT B.010.x — 010.x — 2026-09-07 IST\n")
check("a future date-only header is caught", "AHEAD" in row, row[:110])
ahead, row = run("# REPORT B.010.x — 010.x — 2026-09-06 IST\n")
check("today's date-only header is not", "AHEAD" not in row, row[:90])

# 4. an unreadable header is REPORTED, never silently passed
ahead, row = run("# REPORT B.010.x — no timestamp anywhere in this header\n\n## 1. Branch\n")
check("no timestamp -> the row says NOT CHECKED", ahead is None and "NOT CHECKED" in row, row[:110])
check("  and says explicitly that it is not a pass", "not a pass" in row)

# 5. it only reads the header, not the body (a report quoting a future time in section 6 is not this bug)
body = "# REPORT x — 2026-09-06 20:50 IST\n" + "\n".join(f"line {i}" for i in range(40)) + "\n2026-09-09 23:59 IST\n"
ahead, row = run(body)
check("a timestamp far down the body is not read", "AHEAD" not in row, row[:90])

# 6. advisory by construction: the row goes to warn(), which cannot touch state["ok"]
import inspect
src = inspect.getsource(MG.report_clock_row)
code = src.split('"""')[2] if src.count('"""') >= 2 else src     # body only; the docstring names rec()
check("report_clock_row's body never calls rec()", "rec(" not in code)
# The wiring used to be checked with `"report_clock_row(reps[-1], txt, warn)" in getsource(_main)`.
# A mutation run on 2026-09-06 showed that check stays GREEN when the call is COMMENTED OUT —
# getsource cannot tell live code from a comment — and it went red on two behaviour-preserving
# refactors (a kwarg, a renamed local). Wrong on both sides. So the wiring is measured by running
# the gate and looking at the file it writes.
import json, shutil, subprocess, tempfile
def git(*a, cwd): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
def wfile(root, rel, txt):
    q = os.path.join(root, rel); os.makedirs(os.path.dirname(q), exist_ok=True); open(q, "w").write(txt)

root = tempfile.mkdtemp(prefix="clockwire-")
wt = os.path.join(root, "lane"); os.makedirs(wt)
git("init", "-q", "-b", "plan010/rebuild", cwd=wt)
git("config", "user.email", "t@t", cwd=wt); git("config", "user.name", "t", cwd=wt)
wfile(wt, "platform/core/x.py", "x = 1\n"); git("add", "-A", cwd=wt); git("commit", "-qm", "base", cwd=wt)
git("checkout", "-q", "-b", "lane/x", cwd=wt)
wfile(wt, "platform/core/x.py", "x = 2\n")
# a report dated in the FUTURE: the row must fire, and must NOT change the verdict
wfile(wt, "audit/plan-execution-2026-09-04/reports/ITEM-x-r1.md",
      "# REPORT ITEM — x — 2099-01-01 12:00 IST\n\n## 1. Branch\nhead.\n")
git("add", "-A", cwd=wt); git("commit", "-qm", "candidate", cwd=wt)
state = os.path.join(root, "test-logs", "driver"); gates = os.path.join(state, "gates"); os.makedirs(gates)
queue = os.path.join(state, "queue.json")
json.dump({"items": [{"id": "ITEM", "artifact": "x", "status": "reported", "worktree": wt,
                      "lane": "lane/x", "scope": ["**"], "proof_files": ["tests/test_x.py"]}]}, open(queue, "w"))
MG.CN, MG.TRUNK, MG.QUEUE, MG.GATES = root, wt, queue, gates
MG.EVENTS = os.path.join(state, "events.log"); MG.D = state
MG.run_merge_preflight = lambda *a, **k: None
MG.run_yaml_parse_row = lambda *a, **k: None
MG.run_citesweep_isolated = lambda *a, **k: None
MG.run_citesweep = lambda *a, **k: None
MG._run_proofs = lambda *a, **k: None
sys.argv = ["mergegate.py", "ITEM", "--no-codex", "--no-agy"]
with FL.prefixed('gate run: future-dated report'):
    MG.main()
gmd = open(os.path.join(gates, "ITEM.md")).read()
check("MUST-BITE  the gate ACTUALLY writes a report-clock row (run, not read)",
      "report clock" in gmd, gmd[:120])
check("  and it fired on the future-dated report",
      "AHEAD OF THE WALL CLOCK" in gmd, str([l for l in gmd.split("\n") if "report clock" in l][:1])[:160])
check("  the row is advisory: a clock finding alone does not make the gate FAIL on that row",
      not any(l.startswith("- **report clock") for l in gmd.split("\n")),
      str([l[:60] for l in gmd.split("\n") if "report clock" in l][:1]))
shutil.rmtree(root, ignore_errors=True)

print("\nREPORT CLOCK " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
