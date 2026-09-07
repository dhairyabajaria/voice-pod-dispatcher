"""BOSS's second must-bite: an exception the run_proofs wrapper CANNOT reach must still leave a file.

The wrapper added after gate 66276 covers exactly one function. A raise in run_merge_preflight, the
reviewers, the citation sweep or the renderer still took the process down with no file, no event and
no verdict — an absence that reads exactly like "no gate was ever launched", which is what cost BOSS
ten minutes of diagnosis on 66276.

BOSS's ruling on the shape, and it is better than what I proposed: the crash file is
`<item>.CRASHED.md`, NEVER `<item>.md`, so `<item>.md` keeps meaning "a gate ran to completion" and
a directory listing shows a crash for what it is. The event is GATE_CRASHED, never GATE_FAIL.

This file therefore drives the REAL main() twice, and the second run is the control that keeps the
first honest: a raise INSIDE run_proofs must still produce the ordinary .md + GATE_FAIL and must NOT
produce a .CRASHED.md. One layer each, one event each, no double file. Without that control, a crash
guard that fired on EVERYTHING would pass the first half and quietly destroy the ordinary path.

Hermetic: own git repo, no box, no reviewers, no network."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("mgc", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mgc"] = MG; spec.loader.exec_module(MG)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def git(*a, cwd): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
def write(root, rel, txt):
    p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(txt)

def sandbox():
    root = tempfile.mkdtemp(prefix="gatecrash-")
    wt = os.path.join(root, "lane"); os.makedirs(wt)
    git("init", "-q", "-b", "plan010/rebuild", cwd=wt)
    git("config", "user.email", "t@t", cwd=wt); git("config", "user.name", "t", cwd=wt)
    write(wt, "platform/core/x.py", "x = 1\n")
    git("add", "-A", cwd=wt); git("commit", "-qm", "base", cwd=wt)
    git("checkout", "-q", "-b", "lane/x", cwd=wt)
    write(wt, "platform/core/x.py", "x = 2\n")
    write(wt, "audit/plan-execution-2026-09-04/reports/ITEM-x-r1.md",
          "# REPORT ITEM — x — 2026-09-06 IST\n\n## 1. Branch\nhead.\n")
    git("add", "-A", cwd=wt); git("commit", "-qm", "candidate", cwd=wt)
    state = os.path.join(root, "test-logs", "driver"); gates = os.path.join(state, "gates")
    os.makedirs(gates)
    queue = os.path.join(state, "queue.json")
    json.dump({"items": [{"id": "ITEM", "artifact": "x", "status": "reported", "worktree": wt,
                          "lane": "lane/x", "scope": ["**"], "proof_files": ["tests/test_x.py"]}]},
              open(queue, "w"))
    MG.CN, MG.TRUNK, MG.QUEUE, MG.GATES = root, wt, queue, gates
    MG.EVENTS = os.path.join(state, "events.log")
    MG.D = state
    # everything that needs the world is stubbed; the raise is injected per scenario.
    MG.run_merge_preflight = lambda *a, **k: None
    MG.run_yaml_parse_row = lambda *a, **k: None
    MG.run_citesweep_isolated = lambda *a, **k: None
    MG.run_citesweep = lambda *a, **k: None
    MG._run_proofs = lambda *a, **k: None
    sys.argv = ["mergegate.py", "ITEM", "--no-codex", "--no-agy"]
    return root, gates, state

def boom(msg):
    return lambda *a, **k: (_ for _ in ()).throw(RuntimeError(msg))

# ---------------------------------------------------------------- 1. the unreachable-by-wrapper raise
root, gates, state = sandbox()
MG.run_merge_preflight = boom("synthetic: preflight exploded where the wrapper cannot see it")
with FL.prefixed('gate run: raise outside run_proofs'):
    rc = MG.main()
crashed = os.path.join(gates, "ITEM.CRASHED.md")
ordinary = os.path.join(gates, "ITEM.md")
events = open(MG.EVENTS).read() if os.path.isfile(MG.EVENTS) else ""

check("the gate RETURNS rather than dying on an exception outside run_proofs", isinstance(rc, int), str(rc))
check("MUST-BITE  <item>.CRASHED.md exists (66276 left nothing at all)", os.path.isfile(crashed), crashed)
check("MUST-BITE  <item>.md was NOT written — it still means 'ran to completion'",
      not os.path.exists(ordinary), sorted(os.listdir(gates)))
body = open(crashed).read() if os.path.isfile(crashed) else ""
check("  the crash file names the exception type AND its message",
      "RuntimeError" in body and "preflight exploded" in body, body[:200])
check("  it says NOT COMPLETED rather than carrying a PASS/FAIL verdict",
      "NOT COMPLETED" in body and "— PASS —" not in body and "— FAIL —" not in body,
      body.split("\n")[0])
check("  it says the rows are only what had been measured, and calls it a GATE defect",
      "what had been measured" in body and "GATE defect, not a candidate result" in body)
check("  it carries the rows measured BEFORE the raise (a bare traceback would not locate the crash)",
      "sha on lane head" in body, [l[:60] for l in body.split("\n") if "sha on lane head" in l])
check("  it carries the traceback", "Traceback (most recent call last)" in body)
check("  and the provenance stamp, so the crash names the code image that produced it",
      "gate code " in body and "mergegate " in body)
check("MUST-BITE  a GATE_CRASHED event was emitted", "GATE_CRASHED" in events, events.strip()[-200:])
check("  and NOT GATE_FAIL — an accident must not read as an opinion", "GATE_FAIL" not in events, events.strip()[-200:])
check("  the event names the item and points at the file", "ITEM" in events and "CRASHED" in events)
shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- 2. THE CONTROL: raise inside run_proofs
# A guard that fired on everything would pass every check above while destroying the ordinary path.
root, gates, state = sandbox()
MG._run_proofs = boom("synthetic: the probe exploded inside the wrapper's reach")
with FL.prefixed('gate run: raise inside run_proofs'):
    rc = MG.main()
crashed = os.path.join(gates, "ITEM.CRASHED.md")
ordinary = os.path.join(gates, "ITEM.md")
events = open(MG.EVENTS).read() if os.path.isfile(MG.EVENTS) else ""

check("CONTROL  a raise INSIDE run_proofs still writes the ORDINARY <item>.md", os.path.isfile(ordinary), ordinary)
check("CONTROL  and NO .CRASHED.md — one layer, one file, never both",
      not os.path.exists(crashed), sorted(os.listdir(gates)))
obody = open(ordinary).read() if os.path.isfile(ordinary) else ""
check("  its verdict is FAIL, not NOT COMPLETED", obody.startswith("# GATE ITEM — FAIL") and "NOT COMPLETED" not in obody.split("\n")[0], obody.split("\n")[0])
check("  the proofs row names the exception and implies no test result",
      "RuntimeError" in obody and "NO test result is implied" in obody)
check("CONTROL  GATE_FAIL was emitted, and NOT GATE_CRASHED",
      "GATE_FAIL" in events and "GATE_CRASHED" not in events, events.strip()[-200:])
shutil.rmtree(root, ignore_errors=True)

print("\nGATE CRASHED " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
