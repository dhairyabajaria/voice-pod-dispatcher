"""mergegate.render_gate_md — the gate file's row rendering, tested against a REAL gate file.

Fixture fixtures/gate_ci_collection_floor.md is the .md written by gate 30128 at 21:03:14, the first
run on mergegate b5f3fcec: five rec() rows (one FAIL), two warn() rows, and the three-line header
with the `gate code` stamp. The test parses it back into (name, status, detail) triples, re-renders,
and requires the result to match the file BYTE FOR BYTE — a renderer that can reproduce a real gate
file is one whose row shapes are actually the ones BOSS reads. Hermetic; `now()` is stubbed."""
import importlib.util, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("mg4", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mg4"] = MG; spec.loader.exec_module(MG)
FIX = open(os.path.join(HERE, "fixtures", "gate_ci_collection_floor.md")).read()

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

lines = FIX.split("\n")
head, rows = lines[:3], [l for l in lines[3:] if l.startswith("- ")]
m = re.match(r"# GATE (\S+) — (\S+) — (.+)$", head[0])
item, verdict, when = m.group(1), m.group(2), m.group(3)
m2 = re.match(r"sha (\S+)  lane (\S+)  worktree (.+)$", head[1])
sha, lane, wt = m2.group(1), m2.group(2), m2.group(3)
stamp = head[2][len("gate code "):]

parsed = []
for l in rows:
    mm = re.match(r"- \*\*(.+?)\*\*: (\S+) — (.*)$", l)
    parsed.append((mm.group(1), mm.group(2), mm.group(3)) if mm else (None, "WARN", l[2:]))

check("fixture: it is a real gate file with both row shapes",
      sum(1 for n, _, _ in parsed if n) >= 4 and any(n is None for n, _, _ in parsed),
      f"{sum(1 for n,_,_ in parsed if n)} named rows, {sum(1 for n,_,_ in parsed if n is None)} verbatim rows")
check("fixture: it carries a FAIL row and a FAIL verdict",
      verdict == "FAIL" and any(s == "FAIL" for n, s, _ in parsed if n))
check("fixture: the third header line is the gate code stamp naming all three modules",
      head[2].startswith("gate code ") and all(k in stamp for k in ("mergegate", "gatereview2", "citesweep")))

MG.now = lambda: when          # the only non-deterministic input
out = MG.render_gate_md(item, verdict, sha, lane, wt, stamp, parsed)
check("re-rendering the parsed rows reproduces the file BYTE FOR BYTE", out == FIX,
      "" if out == FIX else f"first difference at char {next((i for i, (a, b) in enumerate(zip(out, FIX)) if a != b), min(len(out), len(FIX)))}")

# the two row shapes, asserted directly rather than only through the round trip
one = MG.render_gate_md("I", "PASS", "abc", "lane/x", "/wt", "stamp",
                        [("scope", "PASS", "13 files"), (None, "WARN", "agy second opinion: approve — ADVISORY")])
check("a rec() row is bold-named with its status", "- **scope**: PASS — 13 files" in one)
check("a warn() row prints verbatim, with no name and no status",
      "- agy second opinion: approve — ADVISORY" in one and "**agy" not in one and "WARN" not in one)
check("the header is exactly three lines then a blank", one.split("\n")[3] == "" and one.startswith("# GATE I — PASS — "))
check("the stamp is on the third line", one.split("\n")[2] == "gate code stamp")
check("the file ends with a newline", one.endswith("\n") and not one.endswith("\n\n"))

# a detail containing the separator must survive: several real rows do
sep = MG.render_gate_md("I", "FAIL", "abc", "lane/x", "/wt", "s",
                        [("merge preflight", "FAIL", "MERGE CONFLICTS — in 1 file(s): ['x'] — abort")])
check("a detail containing ' — ' is not truncated",
      sep.strip().endswith("MERGE CONFLICTS — in 1 file(s): ['x'] — abort"), sep.strip()[-70:])

print("\nGATE MD " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
