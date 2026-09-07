"""A missing binary must be a measurement, not a crash (gate 66276, 2026-09-06 21:36).

The lane worktree had no `platform/.venv`, so the provenance probe exec'd an interpreter that did
not exist; subprocess raised FileNotFoundError, it propagated out of main, and the gate died AFTER
the box, the reviewers and the sweep were paid for and BEFORE the gate file was written. rc=1 from
an uncaught exception is not a verdict. Hermetic: no box, no git, no subprocess beyond /bin/echo."""
import importlib.util, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("mg7", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mg7"] = MG; spec.loader.exec_module(MG)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

# 1. sh() on a binary that does not exist — the exact call that killed gate 66276
missing = os.path.join(tempfile.mkdtemp(prefix="novenv-"), "platform", ".venv", "bin", "python")
rc, out = MG.sh([missing, "-c", "import core"])
check("sh() on a missing binary returns instead of raising", isinstance(rc, int), str(rc))
check("  rc is 127, the shell's own 'command not found'", rc == 127, str(rc))
check("  and the output names the error and the path",
      "FileNotFoundError" in out and "python" in out, out[:110])

# 2. it still runs real commands, and still reports a real non-zero rc
rc, out = MG.sh(["/bin/echo", "hello"])
check("MUST-PASS  a working command is unaffected", rc == 0 and "hello" in out, f"{rc} {out!r}")
rc, _ = MG.sh(["/bin/sh", "-c", "exit 3"])
check("MUST-PASS  a real non-zero exit is still reported as itself, not as 127", rc == 3, str(rc))

# 3. the provenance guard: a scratch checkout with no venv is a NAMED FAIL, not an obscure one
rows = []
def rec(n, ok, d): rows.append((n, ok, d))
root = tempfile.mkdtemp(prefix="novenv2-")
os.makedirs(os.path.join(root, "platform"))
check("fixture: the scratch checkout really has no venv",
      not os.path.exists(os.path.join(root, "platform", ".venv", "bin", "python")))
# the refusal text the gate actually ships (resolve_venv drives it; see section 4)
MG.TRUNK = root                       # no venv anywhere
label, refusal = MG.resolve_venv(root, "/lane/voicepod-lane-ci-guard-residuals")
rec("proofs", False, f"NOT RUN: {refusal}. Build the lane's venv (or point the item at a worktree "
                     f"that has one) and rerun; NO test was executed, so no result is implied.")
check("the missing venv is recorded as a FAIL row", rows and rows[0][1] is False)
check("  naming the lane and what to do",
      "voicepod-lane-ci-guard-residuals" in rows[0][2] and "Build the lane's venv" in rows[0][2])
check("  and saying explicitly that NO test ran, so no result is implied",
      "NO test was executed" in rows[0][2] and "no result is implied" in rows[0][2])
# The three checks above assert a string THIS FILE wrote — they prove the test can format a
# sentence. The only link to the shipped code used to be `"resolve_venv(" in getsource(_run_proofs)`,
# and a mutation run on 2026-09-06 showed that check stays GREEN when the call is commented out:
# getsource cannot tell live code from a comment. So the wiring is measured by RUNNING the proof
# path instead, with the box lock pointed at a temp directory (the real box is never taken) and
# every subprocess stubbed.
def wiring_row():
    root = tempfile.mkdtemp(prefix="venvwire-")
    lane = os.path.join(root, "lane"); os.makedirs(os.path.join(lane, "platform"))
    MG.CN = root
    MG.LOCK = os.path.join(root, "boxlock")        # NEVER the real box
    scratch = os.path.join(root, "voicepod-gate-ITEM")
    def fake_sh(argv, **kw):
        a = " ".join(str(x) for x in argv)
        if MG.CENSUS in a:                    # the census script, whatever it is called
            return 0, "count: 0\n"
        if "worktree" in a and "add" in a:
            os.makedirs(os.path.join(scratch, "platform"), exist_ok=True)   # no .venv in it
            return 0, ""
        return 0, ""
    real_sh = MG.sh
    MG.sh = fake_sh
    out = []
    try:
        MG._run_proofs({"proof_files": ["tests/test_x.py"], "artifact": "x"}, lane,
                       "a" * 40, "a" * 40, "ITEM",
                       lambda n, ok, d: out.append((n, ok, d)), lambda t: None,
                       "r.md", "x")
    finally:
        MG.sh = real_sh
        shutil.rmtree(root, ignore_errors=True)
    return [r for r in out if r[0] == "proofs"]

wired = wiring_row()
check("MUST-BITE  the SHIPPED proof path refuses a venv-less checkout (run, not read)",
      len(wired) == 1 and wired[0][1] is False, str(wired)[:200])
det = wired[0][2] if wired else ""
check("  and the refusal the GATE emits says NO test was executed",
      "NO test was executed" in det and "no result is implied" in det, det[:150])
check("  and tells the reader what to do about it", "Build the lane's venv" in det, det[:150])
check("  and it is the venv refusal, not some other early return",
      det.startswith("NOT RUN:") and "venv" in det, det[:80])
shutil.rmtree(root, ignore_errors=True)
shutil.rmtree(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(missing)))), ignore_errors=True)

# ---- 4. the venv SOURCE is a decision, and it is recorded (BOSS, 2026-09-06) ----
import hashlib
def tree_with(dep_pyproject, dep_lock, venv=False):
    r = tempfile.mkdtemp(prefix="venvsrc-")
    sp = os.path.join(r, "platform"); os.makedirs(sp)
    open(os.path.join(sp, "pyproject.toml"), "w").write(dep_pyproject)
    open(os.path.join(sp, "uv.lock"), "w").write(dep_lock)
    if venv:
        os.makedirs(os.path.join(sp, ".venv", "bin"))
        open(os.path.join(sp, ".venv", "bin", "python"), "w").write("#!/bin/sh")
    return r

# a lane that HAS its own venv keeps using it — trunk is never consulted
scratch = tree_with("a", "b", venv=True); MG.TRUNK = tree_with("DIFFERENT", "x", venv=True)
check("a scratch with its own venv reports source `lane`", MG.resolve_venv(scratch, "/lane") == ("lane", None))

# no lane venv + identical dependency spec -> trunk's venv, and the row says WHY it is acceptable
scratch = tree_with("a", "b"); MG.TRUNK = tree_with("a", "b", venv=True)
label, refusal = MG.resolve_venv(scratch, "/lane/voicepod-lane-x")
check("an identical dependency spec permits trunk's venv", refusal is None and label.startswith("trunk"), str((label, refusal)))
check("  the label names the evidence, not just the source",
      "pyproject.toml + uv.lock identical" in label, label)
check("  and the venv is actually linked into the scratch",
      os.path.exists(os.path.join(scratch, "platform", ".venv", "bin", "python")))

# MUST-REFUSE: a DIFFERENT dependency spec — a venv built for other deps makes a green meaningless
scratch = tree_with("a", "b"); MG.TRUNK = tree_with("a", "DIFFERENT", venv=True)
label, refusal = MG.resolve_venv(scratch, "/lane/voicepod-lane-x")
check("MUST-REFUSE  a differing dependency spec refuses trunk's venv", label is None and refusal)
check("  naming both digests and why it matters",
      "dependency spec differs" in refusal and "meaningless" in refusal, refusal)
check("  and nothing is linked", not os.path.exists(os.path.join(scratch, "platform", ".venv")))

# MUST-REFUSE: no venv anywhere
scratch = tree_with("a", "b"); MG.TRUNK = tree_with("a", "b")
label, refusal = MG.resolve_venv(scratch, "/lane/voicepod-lane-ci-guard-residuals")
check("MUST-REFUSE  no venv on the lane and none on trunk", label is None and "neither does trunk" in (refusal or ""), str(refusal))
check("  and it names the lane worktree BOSS has to fix", "voicepod-lane-ci-guard-residuals" in refusal)

# ---- 5. ANY exception in the proof path is a FAIL ROW, never the end of the gate ----
rows = []
def rec2(n, ok, d): rows.append((n, ok, d))
boom = MG._run_proofs
MG._run_proofs = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic: the probe exploded"))
try:
    out = MG.run_proofs({"proof_files": ["tests/test_x.py"]}, "/lane", "sha", "head", "I", rec2)
finally:
    MG._run_proofs = boom
check("MUST-BITE  an exception inside the proof path returns instead of propagating", out is None)
check("  and is recorded as a FAIL row", rows and rows[0][0] == "proofs" and rows[0][1] is False, str(rows[:1]))
check("  naming the exception", "RuntimeError" in rows[0][2] and "the probe exploded" in rows[0][2], rows[0][2][:120])
check("  calling it a GATE defect, not a candidate result",
      "GATE defect" in rows[0][2] and "NO test result is implied" in rows[0][2], rows[0][2][:200])

print("\nMISSING VENV " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
