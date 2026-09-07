"""run_portal_proofs command construction, with subprocess.run stubbed — no vitest, no npm.
The point of the row is WHICH tests it ran, so that is what is asserted."""
import importlib.util, os, sys, tempfile, types

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("mg2", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mg2"] = MG; spec.loader.exec_module(MG)

root = tempfile.mkdtemp(prefix="portalrow-")
MG.CN = root
wt = os.path.join(root, "lane"); os.makedirs(os.path.join(wt, "portal", "node_modules"))
os.makedirs(os.path.join(root, "test-logs"), exist_ok=True)
calls = []
def fake_run(cmd, **kw):
    calls.append(cmd)
    kw["stdout"].write("Test Files  42 passed (42)\nTests  511 passed (511)\n")
    return types.SimpleNamespace(returncode=0)
MG.subprocess.run = fake_run

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

rows = []
MG.run_portal_proofs(["portal/src/lib/permissions.test.ts"], wt, "I1", lambda n, ok, d: rows.append((n, ok, d)))
check("selected mode passes the named file to vitest",
      calls[0] == ["npx", "vitest", "run", "src/lib/permissions.test.ts"], str(calls[0]))
check("  row says selected", "selected" in rows[0][2], rows[0][2][:90])

calls.clear(); rows.clear()
MG.run_portal_proofs(["portal/src/lib/permissions.test.ts"], wt, "I2", lambda n, ok, d: rows.append((n, ok, d)), whole=True)
check("whole mode runs vitest with NO file arguments",
      calls[0] == ["npx", "vitest", "run"], str(calls[0]))
check("  typecheck still runs", calls[1] == ["npm", "run", "typecheck"], str(calls[1]))
check("  row says WHOLE SUITE and carries the counts",
      "WHOLE SUITE" in rows[0][2] and "511 passed" in rows[0][2], rows[0][2][:120])

# a portal file in the diff promotes the row even when the item names no .tsx proof
calls.clear(); rows.clear()
MG.run_proofs({"proof_files": []}, wt, "sha", "head", "I3", lambda n, ok, d: rows.append((n, ok, d)),
              portal_touched=["portal/src/routes/Integrations.tsx"])
check("a portal/** file in the diff runs the suite with no named .tsx proof",
      calls and calls[0] == ["npx", "vitest", "run"], str(calls[:1]))
check("  and the gate does not record 'item declares no proof_files'",
      not any(n == "proofs" for n, _, _ in rows), str(rows))

# no portal file and no .tsx proof: unchanged behaviour, no vitest at all
calls.clear(); rows.clear()
MG.run_proofs({"proof_files": []}, wt, "sha", "head", "I4", lambda n, ok, d: rows.append((n, ok, d)))
# The STATUS changed on 2026-09-07, the behaviour did not: an item that declares no proof_files
# records NOT DECLARED (MG.UNDECLARED), never False. `proofs: FAIL — item declares no proof_files`
# was indistinguishable from "the declared tests ran and were red", and BOSS nearly reworked a lane
# that had measured 7/7 green over his own missing queue field. Do not "fix" this back to False.
check("no portal involvement leaves the row alone", not calls and len(rows) == 1
      and rows[0][0] == "proofs" and rows[0][1] is MG.UNDECLARED, str(rows))
check("  and the row is NOT DECLARED rather than FAIL — a missing queue field is a defect in the "
      "ITEM, and must not read as a measurement of the candidate",
      rows and rows[0][1] is not False and "defect in the ITEM" in rows[0][2], str(rows))

import shutil; shutil.rmtree(root, ignore_errors=True)
print("\nPORTAL ROW " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
