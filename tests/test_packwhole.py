"""Whole product files where they fit, labelled as files, and a record of what the reviewer saw.

Approved by BOSS 2026-09-07 on nine real candidates (133 product files, their live worktrees):
  - packing every product file whole costs a median 6.6x its hunks (median 811k against a 180k
    budget, 8 of 9 candidates over) — REJECTED;
  - upgrading to whole files only where they FIT shows 52% of product files in full, keeps every
    candidate inside budget (median 168,752, max 179,735), and leaves the rest exactly the hunks
    they have today — BUILT.

Two conditions came with it, and both are failures if they lapse:
  - a whole file must be LABELLED a full file. A reviewer reading one inside a section it believes
    to be a diff reports unchanged lines as findings, and BOSS then holds a lane on a defect that
    does not exist.
  - the packed set is logged per gate. Without it, "the reviewer missed it" and "the reviewer never
    saw it" are the same observation.

Hermetic: no agy, no git, no gate — whole_file is a dict lookup."""
import importlib.util, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location(
    "gpw" + str(time.time_ns()), os.path.join(HERE, os.pardir, "gatereview2.py"))
G = importlib.util.module_from_spec(spec); spec.loader.exec_module(G)

def stanza(path, body_lines=6):
    b = "".join(f"+line {i}\n" for i in range(body_lines))
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,{body_lines} @@\n{b}")

SMALL, BIG, LOG = "platform/core/pay.py", "platform/core/huge.py", "audit/x/run.log"
diff = stanza(SMALL) + stanza(BIG) + stanza(LOG, 3)
WHOLE = {SMALL: "def pay():\n    return 1\n" * 20,          # small: fits
         BIG: "x = 1\n" * 40000,                            # ~240k: never fits
         LOG: "log line\n" * 50}

def pack(budget, whole=WHOLE):
    return G.pack_diff(diff, budget=budget, whole_file=lambda p: whole.get(p))

out, kept, dropped, note, up = pack(20000)
check("MUST-BITE  a product file whose whole text FITS is upgraded",
      [p for p, _ in up] == [SMALL], up)
check("MUST-BITE  the whole file is LABELLED a full file, not dressed as a diff",
      "FULL FILE (not a diff)" in out and SMALL in out.split("FULL FILE")[1][:80], out[:200])
check("  and closed, so the next stanza is not read as part of it",
      f"===== end of {SMALL} =====" in out, out[-200:])
check("  its diff stanza is GONE — the reviewer sees the file once, not twice",
      out.count(f"--- a/{SMALL}") == 0, out.count(f"--- a/{SMALL}"))
check("MUST-BITE  a file too big to fit keeps its HUNK — nothing is lost against today",
      f"+++ b/{BIG}" in out, [l for l in out.splitlines() if BIG in l][:2])
check("MUST-BITE  the pack still fits the budget after upgrading", len(out) <= 20000, len(out))

# the budget is the whole point: with no room, nothing is upgraded and the pack is unchanged
base_out, base_kept, _, _, base_up = G.pack_diff(diff, budget=20000)
out2, _, _, _, up2 = pack(len(base_out) + 10)
check("MUST-BITE  with no spare budget NOTHING is upgraded and the pack is byte-identical to today",
      up2 == [] and out2 == base_out, (up2, len(out2), len(base_out)))
check("CONTROL  and with no whole_file at all the packer is exactly today's",
      base_up == [] and G.pack_diff(diff, budget=20000)[0] == base_out)

# Cheapest first: the budget buys as many whole files as it can. The DIFF ORDER is deliberately the
# REVERSE of the size order — with the two agreeing, an unsorted loop passes this check while
# spending the whole budget on the first big file it meets.
MANY = {f"platform/core/f{i}.py": "y\n" * (100 * (i + 1)) for i in range(4)}
d2 = "".join(stanza(p) for p in sorted(MANY, key=lambda p: -len(MANY[p])))
o2, k2, dr2, n2, up3 = G.pack_diff(d2, budget=1400, whole_file=lambda p: MANY.get(p))
bought = {p for p, _ in up3}
smallest = set(sorted(MANY, key=lambda p: len(MANY[p]))[:len(bought)])
check("MUST-BITE  cheapest upgrades first — the files bought are the SMALLEST ones, whatever order "
      "they appear in the diff", bought == smallest and 0 < len(bought) < len(MANY),
      sorted((len(MANY[p]), p) for p in bought))
check("  and the result still fits", len(o2) <= 1400, len(o2))

# a file that cannot be read keeps its hunk, silently
_, _, _, _, up4 = G.pack_diff(diff, budget=20000, whole_file=lambda p: None)
check("a deleted or binary file (no body) keeps its hunk rather than failing the pack", up4 == [])
def boom(p):
    raise OSError("git show blew up")
try:
    up5 = G.pack_diff(diff, budget=20000, whole_file=boom)[4]
except Exception as e:
    up5 = f"{type(e).__name__}: {e}"          # a crash here would kill the whole review
check("MUST-BITE  and a whole_file that RAISES does not take the review down with it", up5 == [], up5)

# non-product files are never upgraded, whatever room is left
_, _, _, _, up6 = G.pack_diff(diff, budget=200000, whole_file=lambda p: WHOLE.get(p))
check("MUST-BITE  an audit log is never shown whole, even with the budget to spare",
      LOG not in [p for p, _ in up6], up6)

# the record BOSS reads
ps = G.packed_set(kept, dropped, up)
check("MUST-BITE  the packed set marks the upgraded file FULL", f"FULL" in ps and SMALL in ps, ps)
check("  and the rest hunk", "hunk" in ps, ps)
check("  one line per file, kept and dropped alike",
      len(ps.splitlines()) == len(kept) + len(dropped), (len(ps.splitlines()), len(kept), len(dropped)))
big_ps = G.packed_set([], [("x.py (TAIL ONLY)", 5)], [])
check("  a head-truncated file is marked TAIL, not FULL and not dropped", "TAIL" in big_ps, big_ps)

print(("FAILED: " + ", ".join(fails)) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
