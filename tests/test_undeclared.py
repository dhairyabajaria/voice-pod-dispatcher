"""`proofs: FAIL — item declares no proof_files` was reporting a defect in the ITEM as a defect in
the LANE, and BOSS nearly acted on it.

2026-09-07 08:2x, B.015a.operational-raw-views-server-proof rendered `scope: FAIL — item declares no
scope` and `proofs: FAIL — item declares no proof_files`. Both were missing queue fields on BOSS's
own row. EXEC-N's actual measurement was 7/7 green with three break-tests red at the clauses they
name. The rendered row was indistinguishable from "the declared tests ran and were red", and a
rework for good work was one reading away.

So a fourth row status, on the same principle that split NOT RUN out of FAIL:

    FAIL          the declared tests ran and were red      -> fix the lane
    NOT RUN       the box never came free                  -> re-run when it does
    NOT DECLARED  the item never said what to measure      -> fix the queue row
    PASS          it ran and it was green

NOT DECLARED is not a pass: it blocks the merge exactly as NOT RUN does, because in both cases
nothing was proven. The verdict WORD stays INCOMPLETE for both — autogate, the checkpoint reporter
and every grep on events.log key on that word — and the distinction lives in the parenthetical, the
row, and the queue field the row names.

Hermetic: no board, no box, no git.
"""
import importlib.util, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, os.pardir, rel))
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m)
    return m

MG = load("mgu", "mergegate.py")
AG = load("agu", "autogate.py")
CP = load("cpu", "checkpoint.py")

# --- the verdict rule itself, called on the module, never re-derived here ----------------------
v, why = MG.verdict_of(True, [], ["proofs", "scope"])
check("MUST-BITE  an item that declared nothing is INCOMPLETE, never FAIL — the whole finding",
      v.startswith("INCOMPLETE") and "not declared" in v and not v.startswith("FAIL"), v)
check("MUST-BITE  and it names the rows, so the reader knows which queue fields to add",
      "proofs" in v and "scope" in v, v)
check("MUST-BITE  CONTROL: a real red is still FAIL — the new status must not launder a failing lane",
      MG.verdict_of(False, [], ["proofs"])[0].startswith("FAIL"), MG.verdict_of(False, [], ["proofs"])[0])
check("MUST-BITE  CONTROL: nothing wrong is still a bare PASS, with no parenthetical",
      MG.verdict_of(True, [], []) == ("PASS", ""), MG.verdict_of(True, [], []))
both = MG.verdict_of(True, ["proofs"], ["scope"])[0]
check("MUST-BITE  both reasons travel when both apply, and they are worded differently — one is "
      "re-run when the box frees, the other is a field somebody must add",
      "proofs not run" in both and "scope not declared" in both, both)
check("  the verdict WORD stays INCOMPLETE, so autogate and the checkpoint reporter keep working "
      "without learning a fifth word",
      both.startswith("INCOMPLETE"), both)

# --- the sentinel ------------------------------------------------------------------------------
check("MUST-BITE  UNDECLARED is an object, not a string — `passed is UNDECLARED` must not be "
      "satisfiable by a detail that happens to contain the word",
      not isinstance(MG.UNDECLARED, str) and repr(MG.UNDECLARED) == "UNDECLARED", repr(MG.UNDECLARED))

# --- the proofs call site, driven through the real function -------------------------------------
rows = []
# a THREE-argument fake rec, exactly like every other test file's: rec's signature is part of its
# contract with them, and a `field=` keyword on it broke five of them at once.
MG._run_proofs({"proof_files": []}, "/nonexistent-wt", "sha", "head", "B.test",
               lambda n, ok, d: rows.append((n, ok, d)))
check("MUST-BITE  an item with no proof_files records UNDECLARED, not False — measured through the "
      "real _run_proofs, not by reading the source",
      rows and rows[-1][1] is MG.UNDECLARED, rows[-1:])
check("MUST-BITE  and it names the queue field to add, so the fix lands on the row and not the code",
      rows and "`proof_files`" in rows[-1][2] and "defect in the ITEM" in rows[-1][2], rows[-1:])

# --- the rendered row ---------------------------------------------------------------------------
md = MG.render_gate_md("B.test", "INCOMPLETE (proofs not declared)", "abc1234", "lane", "/wt", "st",
                       [("proofs", "NOT DECLARED", "item declares no proof_files — add `proof_files` "
                                                   "to this item's queue row")])
check("MUST-BITE  the .md row reads NOT DECLARED and never the word FAIL — this is the exact string "
      "BOSS read as 'the declared tests ran and were red'",
      "**proofs**: NOT DECLARED" in md and "FAIL" not in md, md.splitlines()[-1][:120])

# --- autogate must parse it ----------------------------------------------------------------------
GATE_UND = ("# GATE B.015a — INCOMPLETE (scope, proofs not declared) — t\nsha abc1234def\n\n"
            "- **scope**: NOT DECLARED — item declares no scope (12 files changed) — add `scope` to "
            "this item's queue row\n"
            "- **proofs**: NOT DECLARED — item declares no proof_files — add `proof_files` to this "
            "item's queue row\n"
            "- **merge preflight**: PASS — merges clean\n")
g = AG.parse_gate_md(GATE_UND)
check("MUST-BITE  autogate PARSES a NOT DECLARED row — an unparsed row is invisible to every reason "
      "string built from it, so a missing field would read as a row that simply is not there",
      set(g["rows"]) == {"scope", "proofs", "merge preflight"}, sorted(g["rows"]))
check("MUST-BITE  the alternation order matters: matching `NOT` first would leave ' DECLARED' in the "
      "DETAIL group. The detail must start with the real text",
      g["rows"]["proofs"][1].startswith("item declares no proof_files"), g["rows"]["proofs"][1][:60])
check("MUST-BITE  a NOT DECLARED row is NOT a failed row — reporting it as one is the rework BOSS "
      "nearly sent",
      AG.failed_rows(g) == [], AG.failed_rows(g))
check("MUST-BITE  and it is NOT a pass either",
      g["rows"]["proofs"][0] is None and g["rows"]["merge preflight"][0] is True, g["rows"])

act, reason = AG.decide(g, "", {})
check("MUST-BITE  the escalation says the item never declared it, and does NOT say it did not run — "
      "'did not run' sends BOSS looking for a box problem that does not exist",
      act == "escalate" and "never declared by the item" in reason and "did not run" not in reason, reason)
check("  and it says whose defect it is, so the fix lands on the queue row",
      "defect in the ITEM" in reason, reason[-90:])

GATE_NR = ("# GATE B.014a — INCOMPLETE (proofs not run) — t\nsha abc1234def\n\n"
           "- **proofs**: NOT RUN — not run — box busy > 60 min (owner EXEC-I)\n")
act2, reason2 = AG.decide(AG.parse_gate_md(GATE_NR), "", {})
check("MUST-BITE  CONTROL: a genuine NOT RUN still says 'did not run' and says nothing about a "
      "declaration — the two must not have collapsed into one message",
      "did not run" in reason2 and "declared" not in reason2, reason2)

# --- the checkpoint reporter's event-line regex ----------------------------------------------------
m = CP.PROOFS_IN_LINE.search("GATE_INCOMPLETE B.015a scope=NOT DECLARED; proofs=NOT DECLARED; codex=approve")
check("MUST-BITE  the event-line regex captures the whole status. `\\w+` alone captures the bare word "
      "'NOT', which then counts as its own state and appears in no column",
      m and m.group(1).upper() == "NOT DECLARED", m.group(1) if m else None)
m2 = CP.PROOFS_IN_LINE.search("GATE_INCOMPLETE B.014a proofs=NOT RUN; codex=approve")
check("  CONTROL: NOT RUN still captures whole, so the added alternative did not shadow it",
      m2 and m2.group(1).upper() == "NOT RUN", m2.group(1) if m2 else None)
check("MUST-BITE  the reporter counts NOT DECLARED in its own column and not inside 'did not run'",
      "NOT DECLARED" in CP.split_component([], "proofs")[0], sorted(CP.split_component([], "proofs")[0]))

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
