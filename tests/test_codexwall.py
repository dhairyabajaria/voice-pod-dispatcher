"""The Codex row must say WHY there is no review: WALLED, EMPTY, or a genuine unreadable answer.

2026-09-06 22:08, gate 83676: Codex answered "You've hit your usage limit … try again at Sep 9th,
2026 10:40 PM". The gate failed closed, correctly — but the row read `verdict=NONE FOUND
(fail-closed)`, which is the same text an unparseable review produces. From the listing BOSS could
not tell a three-day reviewer outage from a defect in our own parsing, and those two demand opposite
responses: wait vs look. Fail-closed is UNCHANGED here; only the row's ability to name its cause is.

Fixture is that gate's real .codex.txt, copied — never read live.
Retry policy under test (BOSS): never SKIP the call while walled, because the wall costs a second to
discover and may have lifted; retry exactly ONCE, and only for the wall."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("mgw", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mgw"] = MG; spec.loader.exec_module(MG)
WALLED = open(os.path.join(HERE, "fixtures", "codex_walled.txt")).read()

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

# 1. the real fixture is recognised, and the reset time is carried out of it
kind, why = MG.codex_failure_kind(1, WALLED)
check("MUST-BITE  the 22:08 fixture is classified WALLED", kind == "WALLED", (kind, why))
check("  and the row names the reset time from the reviewer's own message",
      "Sep 9th, 2026 10:40 PM" in why, why)
check("  in BOSS's requested form: 'CODEX WALLED until <date>'", why.startswith("CODEX WALLED until "), why)

# 2. a wall with no reset time must still be a wall, and must not invent one
kind, why = MG.codex_failure_kind(1, "You've hit your usage limit. Visit https://x to purchase more credits.")
check("a wall with no reset time is still WALLED", kind == "WALLED", (kind, why))
check("  and says the time was not given rather than inventing one",
      "not in the message" in why and "202" not in why, why)

# 3. empty vs unreadable — two different silences
check("no output at all is EMPTY, not WALLED", MG.codex_failure_kind(1, "")[0] == "EMPTY")
check("  whitespace only is EMPTY too", MG.codex_failure_kind(1, "\n  \n")[0] == "EMPTY")
kind, why = MG.codex_failure_kind(0, "# Codex Adversarial Review\n\nSome prose, no verdict line.\n")
check("MUST-BITE  an unreadable-but-present review is NOT dressed up as an outage",
      kind == "" and why == "", (kind, why))

# 4. THE CONTROL: a good review must be untouched by any of this
good = '"verdict": "approve"\nNo findings.\n'
check("CONTROL  a real approving review is not classified as a failure",
      MG.codex_failure_kind(0, good) == ("", ""), MG.codex_failure_kind(0, good))
ok, verdict, blockers = MG.codex_verdict(good)
check("CONTROL  and still passes codex_verdict", ok and verdict == "approve", (ok, verdict))
# and the wall must never be readable AS a verdict
ok, verdict, blockers = MG.codex_verdict(WALLED)
check("MUST-BITE  the walled output can never be read as an approval", not ok, (ok, verdict))

# 5. the retry policy is measured by COUNTING THE CALLS, not by reading the source.
# This block used to assert `'codex_failure_kind(rc, out)[0] == "WALLED"' in getsource(_main)` and
# `getsource(...).count("sh(argv") == 2`. A mutation run on 2026-09-06 replaced the guard with
# `if False:` and left the original as a trailing comment: all five source-reading checks stayed
# GREEN, including the one that counted calls — it had counted a comment. getsource cannot tell live
# code from a commented-out corpse, so what it proves is that a string is in a file. The two
# end-to-end scenarios below count real calls instead, which is the claim we actually care about.

# 6. END TO END: what BOSS actually reads is the rendered row, not the classifier. Drive the real
# gate with the reviewer stubbed to the walled fixture and require the .md to name the wall.
import json, shutil, subprocess, tempfile
def git(*a, cwd): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
def wfile(root, rel, txt):
    q = os.path.join(root, rel); os.makedirs(os.path.dirname(q), exist_ok=True); open(q, "w").write(txt)

root = tempfile.mkdtemp(prefix="codexwall-")
wt = os.path.join(root, "lane"); os.makedirs(wt)
git("init", "-q", "-b", "plan010/rebuild", cwd=wt)
git("config", "user.email", "t@t", cwd=wt); git("config", "user.name", "t", cwd=wt)
wfile(wt, "platform/core/x.py", "x = 1\n"); git("add", "-A", cwd=wt); git("commit", "-qm", "base", cwd=wt)
git("checkout", "-q", "-b", "lane/x", cwd=wt)
wfile(wt, "platform/core/x.py", "x = 2\n")
wfile(wt, "audit/plan-execution-2026-09-04/reports/ITEM-x-r1.md",
      "# REPORT ITEM — x — 2026-09-06 IST\n\n## 1. Branch\nhead.\n")
git("add", "-A", cwd=wt); git("commit", "-qm", "candidate", cwd=wt)
state = os.path.join(root, "test-logs", "driver"); gates = os.path.join(state, "gates"); os.makedirs(gates)
queue = os.path.join(state, "queue.json")
json.dump({"items": [{"id": "ITEM", "artifact": "x", "status": "reported", "worktree": wt,
                      "lane": "lane/x", "scope": ["**"], "proof_files": ["tests/test_x.py"]}]}, open(queue, "w"))
MG.CN, MG.TRUNK, MG.QUEUE, MG.GATES = root, wt, queue, gates
MG.EVENTS = os.path.join(state, "events.log"); MG.D = state
MG.run_merge_preflight = lambda *a, **k: None
MG.run_yaml_parse_row = lambda *a, **k: None
MG.run_citesweep_isolated = lambda *a, **k: None
MG.run_citesweep = lambda *a, **k: None
MG._run_proofs = lambda *a, **k: None
calls = []
real_sh = MG.sh
def fake_sh(argv, **kw):
    if any("Adversarial merge-gate review" in str(a) for a in argv):   # the review argv no longer
                                                    # names the plugin subcommand
                                                    # (Plan 003 A2: it is `codex exec`)
        calls.append(argv)
        return 1, WALLED
    return real_sh(argv, **kw)
MG.sh = fake_sh
sys.argv = ["mergegate.py", "ITEM", "--no-agy"]
with FL.prefixed('gate run: walled reviewer'):
    MG.main()
body = open(os.path.join(gates, "ITEM.md")).read()
row = [l for l in body.split("\n") if l.startswith("- **codex adversarial review**")]
row = row[0] if row else ""
check("END-TO-END  the gate .md row names the wall and its reset time",
      "CODEX WALLED until Sep 9th, 2026 10:40 PM" in row, row[:190])
check("  and says plainly that NO review was performed", "NO review was performed" in row)
check("  and calls it a reviewer outage, not a finding about the candidate",
      "reviewer outage, not a finding" in row)
# 2026-09-07, BOSS ruling: a wall is an OUTAGE, and this row itself said so ("a reviewer outage, not
# a finding") while carrying the word FAIL, which tells BOSS something false about the candidate.
# It is NOT RUN now. What that costs is where the fail-closed guarantee LIVES: it no longer rests on
# this row's value, it rests on "INCOMPLETE never merges" — so BOSS made the conversion conditional
# on pinning that separately. It is pinned in test_notrun.py cases F/G/H, which assert against a
# merge that really does not happen, with case E as the control that really does. If you are here
# because you are about to change the merge rule, that is the file this row depends on.
check("  the row is NOT RUN, never FAIL — a reviewer outage is not a finding (guarantee: test_notrun F/G/H)",
      ": NOT RUN" in row and ": FAIL" not in row, row[:90])
check("END-TO-END  the wall was retried exactly once — two calls, not one, not three",
      len(calls) == 2, len(calls))
check("  and the row admits the retry", "retried once" in row)
shutil.rmtree(root, ignore_errors=True)

# 7. THE CONTROL for the retry: a NON-wall failure must be called exactly ONCE. Without this, a
# retry-everything policy would pass every check above — the wall scenario cannot distinguish
# "retried because walled" from "retried because it failed".
root = tempfile.mkdtemp(prefix="codexnowall-")
wt = os.path.join(root, "lane"); os.makedirs(wt)
git("init", "-q", "-b", "plan010/rebuild", cwd=wt)
git("config", "user.email", "t@t", cwd=wt); git("config", "user.name", "t", cwd=wt)
wfile(wt, "platform/core/x.py", "x = 1\n"); git("add", "-A", cwd=wt); git("commit", "-qm", "base", cwd=wt)
git("checkout", "-q", "-b", "lane/x", cwd=wt)
wfile(wt, "platform/core/x.py", "x = 2\n")
wfile(wt, "audit/plan-execution-2026-09-04/reports/ITEM-x-r1.md",
      "# REPORT ITEM — x — 2026-09-06 IST\n\n## 1. Branch\nhead.\n")
git("add", "-A", cwd=wt); git("commit", "-qm", "candidate", cwd=wt)
state = os.path.join(root, "test-logs", "driver"); gates = os.path.join(state, "gates"); os.makedirs(gates)
queue = os.path.join(state, "queue.json")
json.dump({"items": [{"id": "ITEM", "artifact": "x", "status": "reported", "worktree": wt,
                      "lane": "lane/x", "scope": ["**"], "proof_files": ["tests/test_x.py"]}]}, open(queue, "w"))
MG.CN, MG.TRUNK, MG.QUEUE, MG.GATES = root, wt, queue, gates
MG.EVENTS = os.path.join(state, "events.log"); MG.D = state
calls2 = []
UNREADABLE = "# Codex Adversarial Review\n\nProse with no verdict line at all.\n"
def fake_sh2(argv, **kw):
    if any("Adversarial merge-gate review" in str(a) for a in argv):   # the review argv no longer
                                                    # names the plugin subcommand
                                                    # (Plan 003 A2: it is `codex exec`)
        calls2.append(argv)
        return 1, UNREADABLE
    return real_sh(argv, **kw)
MG.sh = fake_sh2
sys.argv = ["mergegate.py", "ITEM", "--no-agy"]
with FL.prefixed('gate run: unreadable reviewer'):
    MG.main()
row2 = [l for l in open(os.path.join(gates, "ITEM.md")).read().split("\n")
        if l.startswith("- **codex adversarial review**")]
row2 = row2[0] if row2 else ""
check("CONTROL  an unreadable (non-wall) review is called ONCE — the retry is for the wall alone",
      len(calls2) == 1, len(calls2))
check("  it is still a FAIL — fail-closed is unchanged", ": FAIL" in row2, row2[:90])
check("  and it is NOT reported as an outage", "WALLED" not in row2 and "retried once" not in row2, row2[:140])
MG.sh = real_sh
shutil.rmtree(root, ignore_errors=True)

print("\nCODEX WALL " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
