"""An agy verdict that read no product code must render as NO REVIEW, never as an approve.

BOSS + ★, 2026-09-06 23:1x, over the last six gate runs: three printed
`Verdict: approve … [reviewed 0 product file(s)]` (heredoc x3, guard-residuals 2145/2301) and one
saw a single file. An approve that read nothing is not a weak second opinion — it is a verdict about
a different artifact wearing an approval, rendered identically to an earned one and sitting right
next to the finding count, which is where a reader stops.

Also here: the packer's ORDERING (BOSS's (B) must-bite) — a diff whose audit/ half alone exceeds the
budget must still show agy the product files. Hermetic: no agy call, no network; the packer and the
renderers are exercised directly."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("gr2", os.path.join(HERE, os.pardir, "gatereview2.py"))
G = importlib.util.module_from_spec(spec); sys.modules["gr2"] = G; spec.loader.exec_module(G)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def stanza(path, body_bytes):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n" + ("+x\n" * (body_bytes // 3))

APPROVE = {"verdict": "approve", "verdict_line": "Verdict: approve", "findings": [],
           "full_bytes": 1000}

# ---------------------------------------------------------------- A. the false approve
# 1. packed nothing but audit/ while product files were dropped: the 23:1x row
v = dict(APPROVE, kept=[("audit/report.md", 900)], dropped=[("platform/core/pay.py", 4000)])
r = G.row(v, "", codex_ran=True)
check("MUST-BITE  a zero-product pack renders NO REVIEW, not a verdict",
      "NO REVIEW" in r and "approve" not in r, r)
check("  and names the cause as starvation, with the dropped product file",
      "starved" in r and "platform/core/pay.py" in r, r)
check("  and says the model's answer was discarded", "discarded" in r, r)

# 2. THE OTHER CAUSE, reported as itself: a candidate with no product files at all.
#    Calling this "audit/ starved the diff" would send a reader hunting a packer bug that is not
#    there — the heredoc item's diff is CI config, not platform code.
# NOTE 2026-09-07: this example used to be .circleci/config.yml. That directory became PRODUCT
# when the agy input filter was widened, so it stopped being an example of "no product files" —
# the test's premise changed under it, which is the right outcome and the wrong assertion. A docs
# file is the durable example: docs/ is rank 2 and is not code anybody ships.
v2 = dict(APPROVE, kept=[("docs/ops/runbook.md", 900)], dropped=[])
r2 = G.row(v2, "", codex_ran=True)
check("MUST-BITE  a candidate with NO product files is also NO REVIEW", "NO REVIEW" in r2, r2)
check("  but the cause is stated honestly, not blamed on starvation",
      "no product code to review" in r2 and "starved" not in r2, r2)

# 3. THE CONTROL: a real review must be untouched — a rule that fired on everything would pass 1-2
v3 = dict(APPROVE, kept=[("platform/core/pay.py", 900)], dropped=[])
r3 = G.row(v3, "", codex_ran=True)
check("CONTROL  a review that DID read product code still prints its verdict",
      "approve" in r3 and "NO REVIEW" not in r3, r3)
check("  and states full coverage as a fact", "reviewed all 1 product file(s)" in r3, r3)

# 4. coverage denominator: 1 of 4 must not read like 1 of 1
v4 = dict(APPROVE, kept=[("platform/core/pay.py", 900)],
          dropped=[("platform/core/a.py", 10), ("platform/core/b.py", 10), ("audit/x.md", 10)])
r4 = G.row(v4, "", codex_ran=True)
check("MUST-BITE  partial coverage prints N of M, not a bare N",
      "reviewed 1 of 3 product file(s)" in r4, r4)

# 5. DISAGREE must treat an unearned verdict as ABSENT — otherwise an approve that read nothing
#    can cancel a real Codex finding, and the label goes quiet exactly when it matters.
d, why = G.disagreement(v, "needs-attention", "codex found a [high] issue", codex_ran=True)
check("MUST-BITE  a no-review agy verdict never counts as a disagreement", d is False, (d, why))
d3, why3 = G.disagreement(v3, "needs-attention", "codex text", codex_ran=True)
check("CONTROL  a REAL agy approve vs codex needs-attention still disagrees", d3 is True, (d3, why3))

# ---------------------------------------------------------------- B. packer ordering
# BOSS's must-bite: audit/ alone exceeds the budget; the product diff must still reach agy.
big_audit = stanza("audit/plan-execution/report.md", 9000)
prod = stanza("platform/core/pay.py", 600)
# pack_diff returns a fifth value since 2026-09-07 — the list of files UPGRADED to whole
# text (test_packwhole.py). Unpacked here because this file is about ranking and dropping.
packed, kept, dropped, note, _up = G.pack_diff(big_audit + prod, budget=3000)
kp = [p for p, _ in kept]
check("MUST-BITE  audit/ alone over budget: the PRODUCT file is still packed",
      "platform/core/pay.py" in kp, kp)
check("  and the audit file is the one dropped",
      any(p.startswith("audit/") for p, _ in dropped), [p for p, _ in dropped])
check("  the truncation is stated, naming what was left out",
      "NOT INCLUDED" in note and "audit/" in note, note[:160])
# A THIRD route to the same false approve, found while writing this test rather than predicted:
# a diff consisting ONLY of an oversized audit file packs NOTHING AT ALL — a rank-3 chunk over
# budget is dropped whole, and the head-truncation rescue applies to rank 0 only. review() does not
# refuse (its guard needs had_code non-empty), so agy would be asked to review an EMPTY diff and
# could answer `approve`. The row must catch it.
packed2, kept2, dropped2, _, _ = G.pack_diff(big_audit, budget=3000)
check("an all-audit oversized diff packs NOTHING — measured, not assumed",
      not kept2 and not packed2.strip(), (len(kept2), len(packed2)))
v5 = dict(APPROVE, kept=kept2, dropped=dropped2)
check("MUST-BITE  and that empty pack renders NO REVIEW rather than an approve",
      "NO REVIEW" in G.row(v5, "", codex_ran=True), G.row(v5, "", codex_ran=True))

print("\nAGY NO REVIEW " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
