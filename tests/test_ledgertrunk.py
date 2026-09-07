"""The citation sweep's LEDGER half must read TRUNK, never the candidate's copy.

BOSS, 2026-09-06 23:0x: citesweep reported `artifact 010.circleci-heredoc-escape matches no ARTIFACT
block` AFTER BOSS had committed that very row to trunk. The sweep had resolved "against the scratch
worktree at e4381654" — the LANE's copy of plans/EXECUTION_LEDGER.md, a snapshot from whenever that
lane branched, which necessarily predates a row written minutes ago.

The report is the candidate's claim and is rightly read at the candidate sha. The ledger is NOT the
candidate's: trunk's copy is the sole source of truth for it. A false "your ledger row is missing"
costs more than a missed citation, because the obvious response is to go and write a row that
already exists.

Fixture is the REAL ARTIFACT block for that artifact, copied out of trunk's ledger. Hermetic: two
temp trees, no live tree is read.
"""
import importlib.util, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("csl", os.path.join(HERE, os.pardir, "citesweep.py"))
CS = importlib.util.module_from_spec(spec); sys.modules["csl"] = CS; spec.loader.exec_module(CS)
ROW = open(os.path.join(HERE, "fixtures", "ledger_trunk_row.md")).read()
ART = "010.circleci-heredoc-escape"

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def tree(ledger_text=None, report=None):
    """A checkout. `ledger_text=None` means the file exists but WITHOUT this artifact's row —
    the lane's real condition: an old but perfectly valid ledger."""
    r = tempfile.mkdtemp(prefix="ledgertrunk-")
    os.makedirs(os.path.join(r, "plans"))
    open(os.path.join(r, "plans", "EXECUTION_LEDGER.md"), "w").write(
        ledger_text if ledger_text is not None else "ARTIFACT: 009.something-older\nSTATUS: MERGED\n")
    rd = os.path.join(r, "audit", "plan-execution-2026-09-04", "reports")
    os.makedirs(rd)
    open(os.path.join(rd, "ITEM-x-r1.md"), "w").write(report or "# REPORT\n\nNo citations here.\n")
    return r

LANE, TRUNK = tree(), tree(ROW)          # lane predates the row; trunk has it

# 1. THE MUST-BITE: the exact 23:0x false finding
res = CS.sweep("ITEM", LANE, None, ART, "e4381654", ledger_root=TRUNK)
check("MUST-BITE  the ledger row is FOUND when the lane's copy predates it",
      res["ledger_found"] is True, res.get("ledger_found"))
check("  so the sweep does NOT claim the row is missing",
      "matches no" not in CS.row(res), CS.row(res)[:160])
check("  and the ledger's OWN text was swept, not skipped",
      any("ARTIFACT: " + ART in o[0] for o in res["opened"]), [o[0] for o in res["opened"]])

# 2. the source is stated. A fix a reader cannot see is a fix that gets undone.
check("MUST-BITE  the output NAMES which ledger answered", res.get("ledger_source") == "trunk",
      res.get("ledger_source"))
check("  and the opened-file line says so too",
      any("read from trunk" in o[0] for o in res["opened"]), [o[0] for o in res["opened"]])

# 3. THE NEGATIVE CONTROL: reading the lane must still reproduce the false finding. Without this,
#    a sweep that found every artifact everywhere would pass check 1.
res_lane = CS.sweep("ITEM", LANE, None, ART, "e4381654")
check("CONTROL  reading the LANE still reports the row missing — the defect is real and reproduced",
      res_lane["ledger_found"] is False, res_lane.get("ledger_found"))
check("  and that path names the swept tree, not trunk",
      res_lane.get("ledger_source") == "the swept tree", res_lane.get("ledger_source"))

# 4. a genuinely absent artifact must STILL be reported missing — the fix must not hide real gaps
res_absent = CS.sweep("ITEM", LANE, None, "010.no-such-artifact", "e4381654", ledger_root=TRUNK)
check("MUST-BITE  an artifact that is genuinely in NO ledger is still reported missing",
      res_absent["ledger_found"] is False, res_absent.get("ledger_found"))
check("  and the message names the tree it read, so the reader is not sent to the wrong file",
      "trunk" in CS.row(res_absent), CS.row(res_absent)[:200])

# 5. an unreadable trunk ledger must be NAMED, never silently fall back to the lane's copy —
#    a quiet fallback is the bug being fixed, wearing a different hat.
gone = tempfile.mkdtemp(prefix="noledger-")
res_gone = CS.sweep("ITEM", LANE, None, ART, "e4381654", ledger_root=gone)
check("a missing trunk ledger does NOT silently fall back to the candidate's copy",
      res_gone["ledger_found"] is False, res_gone.get("ledger_found"))
check("  and says the trunk ledger is the thing that was missing",
      "DOES NOT EXIST" in str(res_gone.get("ledger_source")), res_gone.get("ledger_source"))

# 6. CONTROL: the REPORT half is untouched — it must still be read from the candidate.
LANE2 = tree(None, "# REPORT\n\nSee `plans/EXECUTION_LEDGER.md` and `audit/x.md:12`.\n")
res_rep = CS.sweep("ITEM", LANE2, None, ART, "e4381654", ledger_root=TRUNK)
check("CONTROL  the report is still read from the CANDIDATE tree",
      res_rep["report"] and res_rep["report"].endswith("ITEM-x-r1.md"), res_rep.get("report"))
check("  and its citations are still resolved against the candidate's files",
      res_rep["root"] == os.path.abspath(LANE2), res_rep.get("root"))

for d in (LANE, TRUNK, LANE2, gone):
    shutil.rmtree(d, ignore_errors=True)
print("\nLEDGER TRUNK " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
