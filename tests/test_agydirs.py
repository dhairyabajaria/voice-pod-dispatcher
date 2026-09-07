"""The files an item's CI work actually lives in must count as product code to agy.

BOSS + ★, 2026-09-07: every NO REVIEW row tonight was one CI item. PRODUCT_DIRS listed only
platform/, portal/ and agent/, so deploy/, .circleci/, .github/ and scripts/ ranked 1 — above the
logs but below nothing that mattered — and in a diff dominated by audit/ the row "0 product files"
was literally true and completely useless.

The directory list here is MEASURED, from the last 300 trunk commits: audit/ 437, plans/ 72,
platform/ 33, docs/ 30, deploy/ 8, portal/ 4, .circleci/ 2, .github/ 2, scripts/ 1."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("gr3", os.path.join(HERE, os.pardir, "gatereview2.py"))
G = importlib.util.module_from_spec(spec); sys.modules["gr3"] = G; spec.loader.exec_module(G)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

# 1. the four that caused tonight's NO REVIEW rows
for path in ("deploy/tests/test_ci_required_gates.py", ".circleci/config.yml",
             ".github/workflows/ci.yml", "scripts/test_circleci_config.py"):
    check(f"MUST-BITE  {path} counts as product code", G.rank(path) == 0, G.rank(path))

# 2. THE CORRECTION: BOSS asked for "ci/", which does not exist in this repo. Assert the real
#    directory is covered, and that a bare "ci/" path is NOT silently treated as product — if the
#    list had been taken as dictated, the NO REVIEW rows would have persisted while looking fixed.
check("the repo's real CI directory is .circleci/, and it is covered", G.rank(".circleci/config.yml") == 0)

# 3. CONTROL: the ranking still works — audit/plans/logs stay at the bottom, docs in the middle.
for path, want in (("audit/plan-execution/report.md", 3), ("plans/EXECUTION_LEDGER.md", 3),
                   ("test-logs/driver/events.log", 3), ("docs/ops/x.md", 2),
                   ("platform/core/pay.py", 0), ("something/else.txt", 1)):
    check(f"CONTROL  rank({path}) == {want}", G.rank(path) == want, G.rank(path))

# 4. END TO END: a CI-only candidate now gets a REVIEW instead of NO REVIEW.
def stanza(path, n):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n" + ("+x\n" * (n // 3))
diff = stanza("audit/plan-execution/report.md", 9000) + stanza("deploy/tests/test_ci.py", 600)
# pack_diff returns a fifth value since 2026-09-07 — the list of files UPGRADED to whole
# text (test_packwhole.py). Unpacked here because this file is about ranking and dropping.
packed, kept, dropped, note, _up = G.pack_diff(diff, budget=3000)
check("MUST-BITE  a CI file is packed ahead of audit/ now",
      "deploy/tests/test_ci.py" in [p for p, _ in kept], [p for p, _ in kept])
v = {"verdict": "approve", "verdict_line": "Verdict: approve", "findings": [],
     "full_bytes": 9600, "kept": kept, "dropped": dropped}
r = G.row(v, "", codex_ran=True)
check("MUST-BITE  and the row is a REVIEW with a count, not NO REVIEW",
      "NO REVIEW" not in r and "product file(s)" in r, r)

# 5. CONTROL: a truly product-free diff is still NO REVIEW — this change must not manufacture one.
d2 = stanza("audit/x.md", 300) + stanza("docs/y.md", 300)
_, k2, dr2, _, _ = G.pack_diff(d2, budget=3000)
v2 = dict(v, kept=k2, dropped=dr2)
check("CONTROL  a diff of only audit/ and docs/ is STILL NO REVIEW",
      "NO REVIEW" in G.row(v2, "", codex_ran=True), G.row(v2, "", codex_ran=True))

print("\nAGY DIRS " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
