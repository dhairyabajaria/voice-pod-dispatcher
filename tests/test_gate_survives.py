"""BOSS's must-bite: a gate whose proof path RAISES must still write its .md and emit GATE_FAIL.

Gate 66276 (2026-09-06 21:36) died on a FileNotFoundError after the box, both reviewers and the
sweep were paid for — no proofs row, no gate file, no GATE_ event, exit 1. A verdict-shaped silence.
This drives the REAL main() end to end against a temp checkout, with the proof path forced to raise,
and requires the file and the event to exist. Hermetic: own git repo, no box, no reviewers, no network."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("mgs", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mgs"] = MG; spec.loader.exec_module(MG)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def git(*a, cwd): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
def write(root, rel, txt):
    p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(txt)

root = tempfile.mkdtemp(prefix="gatesurvive-")
wt = os.path.join(root, "lane"); os.makedirs(wt)
git("init", "-q", "-b", "plan010/rebuild", cwd=wt)
git("config", "user.email", "t@t", cwd=wt); git("config", "user.name", "t", cwd=wt)
write(wt, "platform/core/x.py", "x = 1\n")
git("add", "-A", cwd=wt); git("commit", "-qm", "base", cwd=wt)
git("checkout", "-q", "-b", "lane/x", cwd=wt)
write(wt, "platform/core/x.py", "x = 2\n")
write(wt, "audit/plan-execution-2026-09-04/reports/ITEM-x-r1.md", "# REPORT ITEM — x — 2026-09-06 IST\n\n## 1. Branch\nhead.\n")
git("add", "-A", cwd=wt); git("commit", "-qm", "candidate", cwd=wt)
head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True).stdout.strip()

state = os.path.join(root, "test-logs", "driver"); gates = os.path.join(state, "gates")
os.makedirs(gates)
queue = os.path.join(state, "queue.json")
json.dump({"items": [{"id": "ITEM", "artifact": "x", "status": "reported", "worktree": wt,
                      "lane": "lane/x", "scope": ["**"], "proof_files": ["tests/test_x.py"]}]},
          open(queue, "w"))
MG.CN, MG.TRUNK, MG.QUEUE, MG.GATES = root, wt, queue, gates
MG.EVENTS = os.path.join(state, "events.log")
MG.D = state

# everything that needs the world is stubbed EXCEPT the thing under test: the proof path raises.
MG.run_merge_preflight = lambda *a, **k: None
MG.run_yaml_parse_row = lambda *a, **k: None
MG.run_citesweep_isolated = lambda *a, **k: None
MG.run_citesweep = lambda *a, **k: None
MG._run_proofs = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError(
    "[Errno 2] No such file or directory: '.../platform/.venv/bin/python'"))

sys.argv = ["mergegate.py", "ITEM", "--no-codex", "--no-agy"]
with FL.prefixed('gate run: proof path forced to raise'):
    rc = MG.main()

md = os.path.join(gates, "ITEM.md")
check("the gate still RETURNS a verdict rather than dying", isinstance(rc, int), str(rc))
check("MUST-BITE  the gate file EXISTS even though the proof path raised", os.path.isfile(md), md)
body = open(md).read() if os.path.isfile(md) else ""
check("  its verdict is FAIL", body.startswith("# GATE ITEM — FAIL") and "NOT COMPLETED" not in body.split("\n")[0], body.split("\n")[0])
check("  the proofs row names the exception and calls it a GATE defect",
      "FileNotFoundError" in body and "GATE defect" in body and "NO test result is implied" in body,
      str([l for l in body.split("\n") if l.startswith("- **proofs**")][:1])[:150])
check("  and the stamp block is present, so the file names the code that produced it",
      "gate code " in body and "mergegate " in body)
events = open(MG.EVENTS).read() if os.path.isfile(MG.EVENTS) else ""
check("MUST-BITE  a GATE_FAIL event was emitted", "GATE_FAIL" in events, events.strip()[-160:])
check("  and it names the item", "ITEM" in events)
shutil.rmtree(root, ignore_errors=True)

print("\nGATE SURVIVES " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
