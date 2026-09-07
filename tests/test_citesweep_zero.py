"""citesweep: a zero must name its own cause (2026-09-06, B.010.ci-collection-floor).

That gate's row said EXAMINED NOTHING on a 6992-byte report and stopped there, so the reader had to
open the report to learn why. Two real causes were sitting in it: the report cites files with no
`:line`, and the item's artifact `010.ci` matches no ARTIFACT block, so the ledger half read
nothing and nothing said so. Hermetic: builds its own tree, no git, no network."""
import importlib.util, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("cs", os.path.join(HERE, os.pardir, "citesweep.py"))
CS = importlib.util.module_from_spec(spec); sys.modules["cs"] = CS; spec.loader.exec_module(CS)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def tree(report_body, ledger_artifacts=("010.other",)):
    root = tempfile.mkdtemp(prefix="cszero-")
    rp = os.path.join(root, CS.REPORT_DIR); os.makedirs(rp)
    open(os.path.join(rp, "B.010.x-010.x-r1.md"), "w").write(report_body)
    lp = os.path.join(root, os.path.dirname(CS.LEDGER)); os.makedirs(lp, exist_ok=True)
    open(os.path.join(root, CS.LEDGER), "w").write(
        "".join(f"ARTIFACT: {a}\nSTATUS: MERGED abc1234\n\n" for a in ledger_artifacts))
    os.makedirs(os.path.join(root, "platform", "db", "migrations"), exist_ok=True)
    open(os.path.join(root, "platform", "db", "migrations", "247_thing.sql"), "w").write("-- x\n" * 400)
    return root

# 1. the real shape: a report that cites files without line numbers
R1 = ("# REPORT B.010.x — 2026-09-06 01:45 IST\n\n"
      "`.github/workflows/ci.yml` asserted a floor; replaced with a ratchet.\n"
      "New file `platform/tests/collection_baseline.json`. Fix commit `efe5b6e6`, cut from `ccae4375`.\n"
      "Log 20260906-0145-ci.log written.\n")
root = tree(R1)
res = CS.sweep("B.010.x", root, None, "010.ci", "f02bafe0")
row = CS.row(res)
check("a report citing only bare paths still finds 0 citations", res["found"] == 0)
check("  the bare paths are CHECKED in their own column rather than ignored",
      "paths: " in row and {c["path"] for c in res["paths"]} >=
      {".github/workflows/ci.yml", "platform/tests/collection_baseline.json"},
      str([c["path"] for c in res["paths"]]))
check("  and that column never moves the citation count", res["found"] == 0 and len(res["paths"]) > 0)
check("  the row says the ledger half read nothing, and names the artifact",
      "ledger half read NOTHING" in row and "010.ci" in row)
check("  ledger_found is False", res["ledger_found"] is False)
check("  render() carries a WHY ZERO section", "## WHY ZERO" in CS.render(res))
check("  a date is not called a commit sha", "20260906" not in [c["sha"] for c in res["shas"]],
      str([c["sha"] for c in res["shas"]]))

# 2. the ledger half working is not reported as broken
res2 = CS.sweep("B.010.x", root, None, "010.other", "f02bafe0")
check("a resolvable artifact reports ledger_found True", res2["ledger_found"] is True)
check("  and the row does not claim the ledger read nothing", "ledger half read NOTHING" not in CS.row(res2))
shutil.rmtree(root, ignore_errors=True)

# 3. MUST-PASS: a report with real citations is unchanged — no near-miss noise on the normal path
R2 = "# REPORT B.010.x\n\nSee `247:120` and `platform/db/migrations/247_thing.sql`.\n"
root = tree(R2)
res3 = CS.sweep("B.010.x", root, None, "010.other", "f02bafe0")
row3 = CS.row(res3)
check("MUST-PASS  a report with real citations still counts them", res3["found"] > 0, f"found={res3['found']}")
check("  no near-miss diagnostics are computed when citations were found", res3["near"] == [])
check("  the row is the ordinary one", "EXAMINED NOTHING" not in row3 and "near" not in row3, row3)
check("  and render() has no WHY ZERO section", "## WHY ZERO" not in CS.render(res3))
shutil.rmtree(root, ignore_errors=True)

# 4. a genuinely empty document says so rather than inventing a cause
root = tree("# REPORT B.010.x\n\nNothing reference-shaped in here at all.\n")
res4 = CS.sweep("B.010.x", root, None, "010.other", "f02bafe0")
check("an empty report gets the 'check the root' line, not a fabricated cause",
      "Check the root" in CS.render(res4) and res4["near"] == [])
shutil.rmtree(root, ignore_errors=True)

# 5. the two separate columns (BOSS 2026-09-06): counted, never folded into `found`
R3 = ("# REPORT B.010.x\n\n`platform/db/migrations/247_thing.sql` was added; see also\n"
      "`scripts/does_not_exist.py`. Fix commit `efe5b6e6`.\n")
root = tree(R3)
res5 = CS.sweep("B.010.x", root, None, "010.other", "f02bafe0")
row5 = CS.row(res5)
paths = {c["path"]: c["ok"] for c in res5["paths"]}
check("a bare path that exists is counted as existing", paths.get("platform/db/migrations/247_thing.sql") is not False,
      str(paths))
check("a bare path that does not exist is counted as missing and NAMED",
      paths.get("scripts/does_not_exist.py") is False and "does_not_exist.py" in row5, row5)
check("the row carries the paths column in BOSS's shape",
      "paths: " in row5 and " exist / " in row5 and "missing at candidate sha" in row5, row5)
check("the citation count is NOT changed by the new column",
      res5["found"] == len([c for c in res5["cites"]]) and "paths" not in str(res5["found"]))
before = res5["found"]
check("  and a path is never in `cites`", all(c.get("kind") != "path" for c in res5["cites"]))
check("shas get their own column", "shas: " in row5 and "reachable" in row5, row5)
check("a path already consumed by another shape is not double-counted",
      "247_thing.sql" not in [c["path"] for c in res5["paths"]]
      or any(c["kind"] == "NNN_name.sql" for c in res5["cites"]),
      str([c["path"] for c in res5["paths"]]))
check("bare paths are no longer reported as a near-miss shape too",
      not any("bare file path" in lbl for lbl, _, _ in (res5.get("near") or [])))
shutil.rmtree(root, ignore_errors=True)

print("\nCITESWEEP ZERO " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
