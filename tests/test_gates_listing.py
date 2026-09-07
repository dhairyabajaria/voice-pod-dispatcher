"""`dispatcherctl.sh gates` — the read-only gate listing that replaces `ls -t $STATE/gates`.

The listing's only job is to tell the truth about files it did not write, so its failure mode is a
FILE THAT SILENTLY DOES NOT APPEAR: a malformed header, an unreadable file or a crash mid-listing
all read exactly like "no such gate ran", which is the same green-means-didn't-look shape that has
cost us gates before. Every check below is about a file surviving into the output.

Hermetic: a temp CN with hand-written gate files, the REAL dispatcherctl.sh, no live state read."""
import os, re, subprocess, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
CTL = os.path.join(HERE, os.pardir, "dispatcherctl.sh")

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def ctl(root, *args):
    p = subprocess.run(["/bin/zsh", CTL, *args], capture_output=True, text=True,
                       env=dict(os.environ, CN=root))
    return p.returncode, p.stdout + p.stderr

def sandbox():
    root = tempfile.mkdtemp(prefix="gateslist-")
    g = os.path.join(root, "test-logs", "driver", "gates"); os.makedirs(g)
    return root, g

def write(g, name, text, age_min=0):
    p = os.path.join(g, name)
    open(p, "w").write(text)
    t = time.time() - age_min * 60
    os.utime(p, (t, t))
    return p

GOOD = """# GATE B.010.example — PASS — 2026-09-06 21:25:41
gate code 2026-09-06 21:25:41 md5:d6de412c [mergegate 281c7b38 · gatereview2 a6920aac]
"""
FAILING = """# GATE B.010.broken — FAIL — 2026-09-06 20:04:08

- **codex adversarial review**: FAIL — found a real defect
- **merge preflight**: FAIL — conflicts with trunk
- **proofs**: PASS — 12 tests
gate code 2026-09-06 20:04:08 md5:139a0e00 [mergegate 7c95c73a]
"""

# 1. the ordinary listing
root, g = sandbox()
write(g, "B.010.example.md", GOOD, age_min=5)
write(g, "B.010.broken.md", FAILING, age_min=40)
rc, out = ctl(root, "gates")
check("lists both gates and exits 0", rc == 0 and "B.010.example" in out and "B.010.broken" in out, out[:200])
check("  reports each verdict", "PASS" in out and "FAIL" in out)
check("  names the FAILED rows, not just the verdict",
      "codex adversarial review" in out and "merge preflight" in out, out)
check("  does NOT list a PASS row among the failures", "proofs" not in out.split("gate code")[0], out)
check("  carries the provenance stamp", "md5:d6de412c" in out and "mergegate 281c7b38" in out)
check("  newest first", out.index("B.010.example") < out.index("B.010.broken"), out)
check("  shows an age for each", re.search(r"\b5m\b", out) and re.search(r"\b40m\b", out), out)

# 2. THE MUST-BITE: a file whose header we cannot parse must still be NAMED.
root, g = sandbox()
write(g, "B.010.example.md", GOOD, age_min=5)
write(g, "B.010.garbled.md", "this file has no GATE header at all\nsome other text\n", age_min=6)
rc, out = ctl(root, "gates")
check("a header-less gate file is still named (silence would read as 'never ran')",
      rc == 0 and "B.010.garbled" in out, out)
check("  and is marked unparsed rather than given a verdict",
      "UNPARSED" in out and not re.search(r"B\.010\.garbled\s+(PASS|FAIL)", out), out)
check("  one bad file does not suppress the good ones", "B.010.example" in out and "PASS" in out)

# 3. count argument, and the truthful "N of M" footer
root, g = sandbox()
for i in range(5):
    write(g, "B.010.g%d.md" % i, GOOD.replace("B.010.example", "B.010.g%d" % i), age_min=i)
rc, out = ctl(root, "gates", "2")
shown = [l for l in out.splitlines() if l.startswith("B.010.g")]
check("count argument limits the rows", rc == 0 and len(shown) == 2, shown)
check("  and the footer says how many were NOT shown", "2 of 5 gate file(s)" in out, out)
rc, out = ctl(root, "gates", "notanumber")
check("a non-numeric count is refused, not silently defaulted", rc == 2 and "usage" in out.lower(), out)

# 4. degradation: nothing to list, and no directory at all
root, g = sandbox()
rc, out = ctl(root, "gates")
check("an empty gates dir says so and exits 0", rc == 0 and "no gate files" in out, out)
root2 = tempfile.mkdtemp(prefix="gateslist-nodir-")
rc, out = ctl(root2, "gates")
check("a missing gates dir degrades to a named line, no traceback",
      rc == 0 and "no gate listing" in out and "Traceback" not in out, out)

# 5. the listing is READ-ONLY: byte-identical files and no new ones.
root, g = sandbox()
p = write(g, "B.010.example.md", GOOD, age_min=5)
before = (open(p).read(), os.path.getmtime(p), sorted(os.listdir(g)))
ctl(root, "gates")
after = (open(p).read(), os.path.getmtime(p), sorted(os.listdir(g)))
check("listing mutates nothing — same bytes, same mtime, no new files", before == after)

# 6. the .cites.md sidecar is not a gate and must not be listed as one
root, g = sandbox()
write(g, "B.010.example.md", GOOD, age_min=5)
write(g, "B.010.example.cites.md", "citation sweep output\n", age_min=5)
rc, out = ctl(root, "gates")
check("the .cites.md sidecar is not listed as a gate",
      out.count("B.010.example") == 1 and "cites" not in out, out)

# 6b. a shouted cause is carried into the listing. BOSS's ask, 22:2x: the codex row could not
# distinguish a reviewer outage from a finding, and "open the file to find out" is the cost the
# listing exists to remove.
root, g = sandbox()
write(g, "B.010.walled.md", """# GATE B.010.walled — FAIL — 2026-09-06 22:08:25

- **codex adversarial review**: FAIL — CODEX WALLED until Sep 9th, 2026 10:40 PM (retried once) — NO review was performed, so nothing about this candidate was checked by Codex.
- **proofs**: FAIL — 3 test(s) failed in platform/tests/test_x.py
gate code 2026-09-06 22:08:25 md5:aaaaaaaa [mergegate a94b178f]
""", age_min=3)
rc, out = ctl(root, "gates")
check("a shouted cause is carried into the listing", "CODEX WALLED until Sep 9th" in out, out)
check("  the row name is kept alongside it", "codex adversarial review (CODEX WALLED" in out, out)
check("  a lower-case detail is NOT guessed at — the row name stands alone",
      re.search(r"proofs(?!\s*\()", out) and "3 test(s)" not in out, out)

# 6c. ONE WORD saying which KIND of failure, so the listing answers "must I open this?".
# Measured over the 48 gate files on disk when this was written: codex 18, proofs 13,
# sha-on-lane-head 3, portal 1. 18 of 26 failures are the reviewer row, and while Codex is walled
# that says nothing about the candidate — separating REVIEW from a real red is the whole value.
def kind_row(body, name="B.010.k"):
    """-> (the KIND COLUMN, the whole line). The column, not the line: an earlier version of this
    test matched " WALLED " anywhere in the row and passed on a line whose KIND was REVIEW — the
    word appeared in the failure DETAIL. A check that cannot tell the column from the text beside it
    is not checking the column."""
    root, g = sandbox()
    write(g, name + ".md", body, age_min=1)
    rc, out = ctl(root, "gates")
    line = next((l for l in out.splitlines() if l.startswith(name)), "")
    # Read the column by the HEADER's own offsets. Splitting on whitespace put "COMPLETED" in the
    # KIND slot for the two-word verdict NOT COMPLETED — the classifier was right and the test was
    # wrong, which is the more dangerous way round.
    hdr = next((l for l in out.splitlines() if l.lstrip().startswith("ITEM")), "")
    i, j = hdr.find("KIND"), hdr.find("AGE")
    kind = line[i:j].strip() if (line and 0 <= i < j) else ""
    return kind, (line or out)

def gate(verdict, *fail_rows):
    return (f"# GATE B.010.k — {verdict} — 2026-09-07 01:00\n\n"
            + "".join(f"- **{n}**: FAIL — {d}\n" for n, d in fail_rows)
            + "gate code 2026-09-07 01:00:00 md5:abcd1234 [mergegate 2a99135e]\n")

kind, line = kind_row(gate("FAIL", ("proofs", "3 test(s) failed")))
check("a proof red is classified PROOFS", kind == "PROOFS", (kind, line))
kind, line = kind_row(gate("FAIL", ("codex adversarial review", "rc=0 verdict=needs-attention blockers=1")))
check("a reviewer objection is classified REVIEW", kind == "REVIEW", (kind, line))
kind, line = kind_row(gate("FAIL", ("codex adversarial review",
                                    "CODEX WALLED until Sep 9, 2026 10:40 PM (not retried)")))
check("MUST-BITE  a WALL is not classified as a reviewer opinion", kind == "WALLED", (kind, line))
kind, line = kind_row(gate("FAIL", ("sha on lane head", "report sha not on the lane head")))
check("a malformed submission is classified SCOPE", kind == "SCOPE", (kind, line))
kind, line = kind_row(gate("NOT COMPLETED", ("proofs", "whatever")))
check("MUST-BITE  a crashed gate is CRASH regardless of its rows", kind == "CRASH", (kind, line))

# precedence: a real red must not be hidden behind the reviewer row that fails on almost every gate
kind, line = kind_row(gate("FAIL", ("codex adversarial review", "verdict=needs-attention"),
                           ("proofs", "3 test(s) failed")))
check("MUST-BITE  PROOFS outranks REVIEW — a real red is never reported as a review failure",
      kind.startswith("PROOFS"), (kind, line))
check("  and the '+' says other kinds failed too", kind == "PROOFS+", kind)

# CONTROL: a passing gate gets no kind at all. Without this, a classifier that printed a word
# unconditionally would pass every check above.
kind, line = kind_row("# GATE B.010.k — PASS — 2026-09-07 01:00\ngate code x md5:a [mergegate y]\n")
check("CONTROL  a PASS row carries no failure kind", kind == "-", (kind, line))

# CONTROL: the failed row NAMES are still printed — the word classifies, it does not replace them
kind, line = kind_row(gate("FAIL", ("proofs", "3 test(s) failed")))
check("CONTROL  the row names survive beside the kind", "proofs" in line, line)

# 6d. a row that MEASURED NOTHING is not a red. BOSS, 2026-09-07: guard-residuals r8 listed as
# PROOFS+ while its proofs row read "not run (--no-box)" — he ran the two files by hand, 77 passed,
# and the real hold was Codex. Every box-free gate would otherwise be listed as a proof failure.
kind, line = kind_row(gate("FAIL", ("proofs", "not run (--no-box)")))
check("MUST-BITE  a deliberately skipped proof row is SKIPPED, not PROOFS", kind == "SKIPPED", (kind, line))
kind, line = kind_row(gate("FAIL", ("codex adversarial review", "not run (--no-codex)")))
check("  and a skipped reviewer likewise", kind == "SKIPPED", (kind, line))

# the REAL r8 shape: a skipped proof beside a genuine Codex objection. The word must name the
# thing that is actually holding the item.
kind, line = kind_row(gate("FAIL", ("proofs", "not run (--no-box)"),
                           ("codex adversarial review", "rc=0 verdict=needs-attention blockers=2['[high]']")))
check("MUST-BITE  r8's shape reads REVIEW — the real hold — not PROOFS", kind.startswith("REVIEW"),
      (kind, line))

# TWO WORDS, because these are different facts and one word would merge them: a gate that TRIED and
# could not measure is a defect somebody must fix, unlike a flag that says do not bother.
for det in ("NOT RUN: the proof path raised RuntimeError — this is a GATE defect",
            "NOT RUN: the lane worktree has no platform/.venv",
            "box busy > 60 min",
            "census=count: 2"):
    kind, line = kind_row(gate("FAIL", ("proofs", det)))
    check(f"a gate that TRIED and could not measure is NOT-RUN [{det[:28]}]", kind == "NOT-RUN", (kind, line))

# CONTROL: a REAL proof red must still be PROOFS — otherwise this whole change hides reds
kind, line = kind_row(gate("FAIL", ("proofs", "3 test(s) failed in platform/tests/test_x.py")))
check("CONTROL  a real proof red is still PROOFS, not swallowed by the new kinds", kind == "PROOFS",
      (kind, line))
kind, line = kind_row(gate("FAIL", ("proofs", "not run (--no-box)"),
                           ("portal proofs", "2 test(s) failed")))
check("CONTROL  a real red beside a skipped row still wins", kind.startswith("PROOFS"), (kind, line))

# 7. usage text advertises the verb (an undiscoverable verb is an unused one)
rc, out = ctl(tempfile.mkdtemp(prefix="gateslist-usage-"), "bogusverb")
check("the usage line advertises `gates`", "gates" in out, out)

print("\n%d check(s) failed" % len(fails) if fails else "\nall checks passed")
raise SystemExit(1 if fails else 0)
