"""A pack at 99% of budget must SAY SO, and a dropped byte must never be counted as a sent one.

BOSS measured six packs on 2026-09-07. Corrected figures: median occupancy 37%, non-product 24% of
sent bytes, 7,022,137 bytes correctly dropped — backfills-r2 alone carried a 3.3 MB committed log.
The packer is behaving. But backfills-r2 SENT 153,985 product bytes, 99% of the 180,000 budget, and
it is the lane carrying the [high] he ruled on: one commit from the reviewer silently losing files,
with the same coverage line printed either way.

His FIRST pass summed every manifest row including the NONE entries and produced "1718% occupancy,
95% non-product" — he was about to report the packer as catastrophically over budget. The manifest
header says NONE means dropped; he read the sizes without the kinds. That is why occupancy() is one
function with one test rather than arithmetic repeated at each call site, and why the first check
below is the one that would have caught him.

Hermetic: no model call, no gate."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location("gr2f", os.path.join(HERE, os.pardir, "gatereview2.py"))
G = importlib.util.module_from_spec(spec); spec.loader.exec_module(G)

BUD = G.MAX_DIFF
def v(kept, dropped=(), verdict="approve", findings=()):
    return {"kept": list(kept), "dropped": list(dropped), "verdict_line": verdict,
            "findings": list(findings), "full_bytes": sum(n for _p, n in kept)
                                                      + sum(n for _p, n in dropped)}

# ---------------------------------------------------------------- occupancy counts SENT only
real = v([("platform/core/backfills.py", 153985), ("audit/x/reports/r.md", 25408)],
         dropped=[("test-logs/20260905-1704-B.017a.r2-combined-14f98b1c.log", 3300000),
                  ("test-logs/other.log", 701996)])
sent, occ = G.occupancy(real)
check("MUST-BITE  occupancy counts only what was SENT — summing the DROPPED rows too is the error "
      "that produced '1718% occupancy' and nearly reported a working packer as broken",
      sent == 179393 and 0.99 < occ < 1.0, (sent, round(occ, 3)))
check("  and it is measured against the real budget, not a hardcoded guess",
      abs(occ - 179393 / BUD) < 1e-9, (BUD, occ))
check("MUST-BITE  the percentage is FLOORED, never rounded: 99.7% must not print as '100% of "
      "budget', which reads as AT the limit and is the one number a reader acts on differently",
      G.pct(occ) == 99 and G.pct(1.0) == 100 and G.pct(0.899) == 89, (G.pct(occ), G.pct(0.899)))

# ---------------------------------------------------------------- the row says so at 90%+
r = G.row(real)
check("MUST-BITE  a pack at 99% SAYS SO in the gate row — the coverage line reads identically "
      "whether the pack is roomy or one commit from dropping files",
      "PACK NEAR FULL" in r and "99%" in r, r[-260:])
check("  it says nothing was lost from THIS review, so it is not misread as an omission",
      "Nothing was lost from THIS review" in r, r[-200:])
check("  and it names what to do — check coverage, split the candidate",
      "split the candidate" in r, r[-120:])

# ---------------------------------------------------------------- CONTROL: a roomy pack is quiet
small = v([("platform/core/x.py", 3543), ("audit/x/r.md", 3938)])
r2 = G.row(small)
check("MUST-BITE  CONTROL: a 4% pack does NOT warn — a warning on every gate is a warning on none",
      "PACK NEAR FULL" not in r2, r2[-160:])
check("  but the occupancy is printed either way, so the number is readable before it is a problem",
      "pack 4% of budget" in r2, r2[-160:])

# the boundary, both sides, since NEAR_FULL is the whole claim
just_under = v([("platform/core/x.py", int(BUD * 0.89))])
just_over = v([("platform/core/x.py", int(BUD * 0.91))])
check("MUST-BITE  the threshold bites at the boundary: 89% quiet, 91% loud",
      "PACK NEAR FULL" not in G.row(just_under) and "PACK NEAR FULL" in G.row(just_over),
      (G.occupancy(just_under)[1], G.occupancy(just_over)[1]))

# ---------------------------------------------------------------- the header, above the verdict
hdr = "\n".join(G.input_header(real))
check("MUST-BITE  the pack size is in the INPUT HEADER, above the verdict — a reader who meets the "
      "verdict first has already formed a view",
      "179,393" in hdr and "% of budget" in hdr, hdr[:200])
check("  and the near-full warning is there too", "PACK NEAR FULL" in hdr, hdr[:400])
hdr2 = "\n".join(G.input_header(small))
check("CONTROL  a roomy pack's header carries the number but no warning",
      "% of budget" in hdr2 and "PACK NEAR FULL" not in hdr2, hdr2[:200])

# a pack that ALREADY dropped product files must keep saying THAT, not be replaced by the near-full note
lost = v([("platform/core/a.py", int(BUD * 0.95))], dropped=[("platform/core/b.py", 50000)])
hdr3 = "\n".join(G.input_header(lost))
check("MUST-BITE  a pack that already omitted PRODUCT files still leads with that — near-full is "
      "the warning BEFORE the loss, never a softer replacement for it",
      "PRODUCT FILES WERE OMITTED" in hdr3, hdr3[:300])

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
