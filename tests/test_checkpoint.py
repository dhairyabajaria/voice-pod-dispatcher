"""The checkpoint reporter's two rules, made to bite.

BOSS's rules were: (1) report what you can measure and print "not measured" where you cannot —
never an inferred value, and never a blank that reads as zero; (2) be re-runnable, so the same
inputs give the same shape and only the numbers move.

Both are testable, and both fail SILENTLY if they break: a blank cell looks like a report, and a
figure carried over from a previous run looks like a measurement. So every check here reads the
GENERATED MARKDOWN, not the helper functions — the document is what BOSS and the owner read.

Hermetic: a temporary CN with its own queue.json, events.log and a real four-commit git repo. No
live board, no daemon, no network."""
import importlib.util, os, re, shutil, subprocess, sys, tempfile, time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

NMTXT = "not measured"


def ago(minutes):
    """Fixture dispatch times are written RELATIVE TO NOW.

    An absolute timestamp here is a time bomb: the stale-hold check compares a row against the
    wall clock, so a "this row is NOT stale" control written as a literal passes in the morning
    and reds at lunchtime with nothing changed. Measured 2026-09-07 10:08 — B.010.reported-ok was
    written as 04:00:00, crossed STALE_HOLD_MIN at 10:00, and the CONTROL that proves the sha
    check does not fault every `reported` row went red for a reason that had nothing to do with
    shas. Rows meant to be stale are far past the threshold; rows meant to be fresh are far
    inside it, so neither side sits near a boundary the clock can walk across."""
    return (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")



def ratio_of(cell):
    return C.ratio(cell)


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond:
        fails.append(name)


def git(repo, *args):
    return subprocess.run(("git",) + args, cwd=repo, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def build_trunk(root):
    """A real repo with real merge commits — the parser reads `git log --merges`, so a fake log file
    would test the regex and not the command."""
    repo = os.path.join(root, "voice-pod")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "plan010/rebuild")
    open(os.path.join(repo, "f"), "w").write("0\n")
    git(repo, "add", "f"); git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    subjects = [
        ("lane-a", "merge B.010.alpha @ aaaaaaa1 — proofs PASS. Codex Verdict: approve."),
        ("lane-b", "merge B.010.beta @ bbbbbbb2 — Codex Verdict: needs-attention, 1 [high]. "
                   "MERGED, NOT LANDED (Rule 3: GitHub run on the pushed tree is the verification)."),
        ("lane-c", "merge B.010.gamma @ ccccccc3 — LANDED: WORKER-1 audit + named test green on the "
                   "merged tree. Codex Verdict: approve."),
        ("lane-d", "a merge with a subject nobody parsed"),
    ]
    for i, (branch, subj) in enumerate(subjects):
        git(repo, "checkout", "-q", "-b", branch, base)
        open(os.path.join(repo, branch), "w").write("x\n")
        git(repo, "add", branch); git(repo, "commit", "-qm", f"work {i}")
        git(repo, "checkout", "-q", "plan010/rebuild")
        git(repo, "merge", "-q", "--no-ff", branch, "-m", subj)
    return repo, base


ROOT = tempfile.mkdtemp(prefix="ckpt-")
REPO, BASE = build_trunk(ROOT)
D = os.path.join(ROOT, "test-logs", "driver")
os.makedirs(D)

QUEUE = {"items": [
    # in flight, with a dispatch time only in the queue — the pre-03:38 GATE lines carry none
    # ABSOLUTE on purpose: this one is placed against checkpoint.RULES_LANDED, a fixed constant,
    # so it has to sit before 2026-09-07 02:23. It is therefore also permanently stale — which is
    # harmless, because nothing here asserts that it is fresh.
    {"id": "B.010.old", "status": "gated", "dispatched_to": "EXEC-B",
     "dispatched_at": "2026-09-07 01:00:00"},
    # in rework, no rework_round recorded: the round cell must say so, not sit blank
    {"id": "B.010.noround", "status": "rework", "dispatched_to": "EXEC-E",
     "gate_verdict": "r4 FAIL: two [high] confirmed\nsecond line must not appear"},
    {"id": "B.010.round7", "status": "rework", "rework_round": 7, "hold_reason": "held for the pair"},
    # dispatch time nowhere at all
    {"id": "B.010.nowhere", "status": "dispatched", "dispatched_to": "EXEC-F"},
    {"id": "A.010.ownerq", "status": "parked",
     "parked_reason": "BLOCKED:owner — Article 17 question"},
    {"id": "B.010.parked-not-owner", "status": "parked",
     "parked_reason": "waiting on the CI pair, no owner input needed"},
    {"id": "B.010.merged", "status": "merged"},
    # --- section 6 fixtures: status vs evidence -------------------------------------------------
    # the residue of the PLAN-classified-as-REPORT bug: `reported`, no sha anywhere
    {"id": "B.010.residue", "status": "reported", "dispatched_to": "EXEC-D",
     "dispatched_at": ago(400)},
    # the control: `reported` WITH a sha is a perfectly ordinary row and must NOT be faulted
    {"id": "B.010.reported-ok", "status": "reported", "dispatched_to": "EXEC-H",
     "report_sha": "abc1234def", "dispatched_at": ago(30)},
    # gated with no gate file and no gate process — the 23-hour rows
    {"id": "B.010.ghost-gate", "status": "gated", "dispatched_to": "CODEX-2",
     "dispatched_at": ago(1810)},
    # ...and the control: gated WITH a gate file on disk
    {"id": "B.010.real-gate", "status": "gated", "dispatched_to": "EXEC-J",
     "dispatched_at": ago(30)},
    # one executor holding two, on one worktree
    {"id": "B.010.twin-a", "status": "dispatched", "dispatched_to": "EXEC-K",
     "worktree": "/tmp/ckpt-twin", "dispatched_at": ago(20)},
    {"id": "B.010.twin-b", "status": "dispatched", "dispatched_to": "EXEC-K",
     "worktree": "/tmp/ckpt-twin", "dispatched_at": ago(19)},
]}
import json
json.dump(QUEUE, open(os.path.join(D, "queue.json"), "w"))

EV = [
    # before the rules: dispatch time from the QUEUE only, codex FAIL
    "2026-09-07 01:30:00\tGATE_FAIL\tGATE\t-\tB.010.old\tsha on lane head=PASS; proofs=PASS; "
    "codex adversarial review=FAIL",
    # after the rules: dispatch time on the LINE, codex PASS, proofs FAIL (a --no-box gate)
    "2026-09-07 04:10:03\tGATE_FAIL\tGATE\t-\tB.010.beta\tdispatched_at=2026-09-07 03:57:44; "
    "portal proofs=PASS; proofs=FAIL; codex adversarial review=PASS",
    "2026-09-07 04:25:45\tGATE_PASS\tGATE\t-\tB.010.gamma\tdispatched_at=2026-09-07 04:18:26; "
    "proofs=PASS; codex adversarial review=PASS",
    # a check that could not run: neither a pass nor a failure, in the headline or the component
    "2026-09-07 04:28:00\tGATE_INCOMPLETE\tGATE\t-\tB.010.gamma\tdispatched_at=2026-09-07 04:19:00; "
    "proofs=NOT RUN; codex adversarial review=PASS",
    # no dispatch time in either place: must land in NEITHER cell
    "2026-09-07 04:30:00\tGATE_FAIL\tGATE\t-\tB.010.nowhere\tproofs=FAIL; "
    "codex adversarial review=FAIL",
    "2026-09-07 04:31:00\tAUTO_GATE\tGATE\t-\tB.010.nowhere\tnot a GATE_ verdict line",
]
open(os.path.join(D, "events.log"), "w").write("\n".join(EV) + "\n")

os.makedirs(os.path.join(D, "gates"), exist_ok=True)
open(os.path.join(D, "gates", "B.010.real-gate.md"), "w").write("# GATE B.010.real-gate — PASS — x\n")
os.environ["CN"] = ROOT
spec = importlib.util.spec_from_file_location(
    "ckpt" + str(time.time_ns()), os.path.join(HERE, os.pardir, "checkpoint.py"))
C = importlib.util.module_from_spec(spec); spec.loader.exec_module(C)
C.SINCE = BASE
C.GATES = os.path.join(D, "gates")
_REAL_GATE_PROCESSES = C.gate_processes
C.gate_processes = lambda: (set(), None)   # hermetic: no live box is consulted
md = C.render(*C.load_queue())
print(md if os.environ.get("SHOW") else "")


def row(item):
    for ln in md.splitlines():
        if ln.startswith("|") and item in ln:
            return ln
    return ""


# ---------------------------------------------------------------- 1. merged tonight, Rule 3
check("MUST-BITE  every merge since the base appears",
      all(i in md for i in ("B.010.alpha", "B.010.beta", "B.010.gamma")),
      [i for i in ("B.010.alpha", "B.010.beta", "B.010.gamma") if i not in md])
check("MUST-BITE  Rule 3 is read from the record: an explicit NOT LANDED says so",
      "not landed" in row("B.010.beta").lower(), row("B.010.beta"))
check("MUST-BITE  a merge with no landing evidence is MERGED, never LANDED",
      "MERGED" in row("B.010.alpha") and "LANDED" not in row("B.010.alpha").replace("MERGED", ""),
      row("B.010.alpha"))
check("MUST-BITE  an explicit LANDED is reported as LANDED",
      row("B.010.gamma").rstrip().endswith("LANDED |"), row("B.010.gamma"))
check("  the recorded reviewer verdict is carried, not restated",
      "needs-attention" in row("B.010.beta"), row("B.010.beta"))
bad = [ln for ln in md.splitlines()
       if ln.startswith("|") and not ln.startswith("|---") and not all(cells(ln))]
check("MUST-BITE  no table cell anywhere in the document is blank", not bad, bad[:2])
check("MUST-BITE  the unparseable merge subject is reported as not-measured, not omitted",
      sum(1 for ln in md.splitlines() if ln.startswith("|") and NMTXT in ln) >= 1)

# ---------------------------------------------------------------- 2/3. held, in flight
check("MUST-BITE  a missing rework_round prints not-measured, not an empty cell",
      NMTXT in row("B.010.noround") and all(cells(row("B.010.noround"))),
      row("B.010.noround"))
check("  a recorded round is printed",
      "| 7 |" in row("B.010.round7"), row("B.010.round7"))
check("MUST-BITE  the reason is one line — a multi-line verdict cannot break the table",
      "second line must not appear" not in md)
check("MUST-BITE  an in-flight row with no dispatch time says so instead of showing 0 min",
      NMTXT in row("B.010.nowhere") and "0 min" not in row("B.010.nowhere")
      and all(cells(row("B.010.nowhere"))),
      row("B.010.nowhere"))
check("  a merged item is not reported as in flight or held",
      "B.010.merged" not in md)

# ---------------------------------------------------------------- 4. the split
g, err = C.gate_ratio(list(QUEUE["items"]))
check("  only GATE_ verdict lines are counted", err is None and len(g) == 5, (err, len(g)))
before, after, unmeasured = C.split_gates(g)
check("MUST-BITE  a queue-only dispatch time still places the gate (pre-03:38 lines carry none)",
      before["FAIL"] == 1 and before["PASS"] == 0, before)
check("MUST-BITE  a GATE_INCOMPLETE is counted as neither a pass nor a failure",
      after["INCOMPLETE"] == 1 and after["PASS"] == 1 and after["FAIL"] == 1, after)
check("  and the PASS:FAIL ratio therefore excludes it", ratio_of(after) == "1:1", ratio_of(after))
check("MUST-BITE  a line-borne dispatch time places the gate after the rules",
      after["PASS"] == 1 and after["FAIL"] == 1 and after["OTHER"] == 0, after)
check("MUST-BITE  a gate with no dispatch time anywhere is in NEITHER cell",
      len(unmeasured) == 1 and unmeasured[0]["item"] == "B.010.nowhere",
      [u["item"] for u in unmeasured])
check("MUST-BITE  and the report SAYS it dropped them, with the count and the names",
      "NEITHER cell" in md and "B.010.nowhere" in md.split("## 5")[0])
cb, ca, miss = C.split_component(g, "codex")
check("MUST-BITE  the component split reads the reviewer's own result from the event",
      (cb["PASS"], cb["FAIL"], ca["PASS"], ca["FAIL"]) == (0, 1, 3, 0), (cb, ca))
pb, pa, _m = C.split_component(g, "proofs")
check("MUST-BITE  a NOT RUN component is counted as NOT RUN — never folded into passes or fails",
      pa["NOT RUN"] == 1 and pa["PASS"] == 1 and pa["FAIL"] == 1, pa)
check("  the document shows the did-not-run column rather than hiding it",
      "did not run" in md and "incomplete (a check did not run)" in md)
check("  a sample under 20 is declared, not padded",
      "The request was for 20" in md and "Sample size 5" in md)

# ---------------------------------------------------------------- 5. owner
check("MUST-BITE  only BLOCKED:owner rows are open for the owner",
      "A.010.ownerq" in md.split("## 5")[1] and "B.010.parked-not-owner" not in md)

# ---------------------------------------------------------------- 6. status vs evidence
sec = md.split("## 6.")[1].split("## 7.")[0]

def faulted(item):
    return any(item in ln for ln in sec.splitlines() if ln.startswith("|") and not ln.startswith("|---"))

check("MUST-BITE  a `reported` row with no sha anywhere is faulted — the residue a fixed "
      "classification bug leaves behind, which nothing else looks for",
      faulted("B.010.residue"), sec.strip()[:200])
check("CONTROL  a `reported` row WITH a sha is NOT faulted — without this, a check that faults "
      "every reported row would look identical",
      not faulted("B.010.reported-ok"))
check("MUST-BITE  a `gated` row with no gate file and no gate process is faulted",
      faulted("B.010.ghost-gate"))
check("CONTROL  a `gated` row whose gate file EXISTS is not faulted", not faulted("B.010.real-gate"))
# The two CONTROLs above are only controls while their rows are INSIDE the stale window: a stale
# finding faults the row for a reason the control is not about, and the control reds without the
# thing it guards having changed. This is the check that keeps the fixture's clock honest.
_fresh = {i["id"]: C.age(i["dispatched_at"]) for i in QUEUE["items"]
          if i["id"] in ("B.010.reported-ok", "B.010.real-gate", "B.010.twin-a", "B.010.twin-b")}
check("MUST-BITE  every row a CONTROL needs unfaulted is far inside the stale window — an absolute "
      "timestamp here reds the controls by the clock alone",
      all(a is not None and a < C.STALE_HOLD_MIN - 120 for a in _fresh.values()),
      [f"{k}={v} min of {C.STALE_HOLD_MIN}" for k, v in _fresh.items()])
check("MUST-BITE  one executor holding two unfinished rows is faulted",
      faulted("B.010.twin-a") and "EXEC-K" in sec, sec)
check("MUST-BITE  two rows claiming one worktree is faulted", "/tmp/ckpt-twin" in sec)
check("MUST-BITE  a row held far longer than the threshold is faulted, and the threshold is PRINTED "
      "so it is never an implicit judgement",
      faulted("B.010.ghost-gate") and str(C.STALE_HOLD_MIN) in sec, C.STALE_HOLD_MIN)
# ">= 4 mentions" was satisfied with one check silently stripped of its count (the mutation scored
# zero). Count the BULLETS instead: every one of them must carry a number.
bullets = [l for l in sec.splitlines() if l.startswith("- ") and NMTXT not in l]
sized = [l for l in bullets if "row(s)" in l]
check("MUST-BITE  EVERY check names how many rows it examined — a clean list and a check that never "
      "ran are otherwise the same text",
      bullets and len(sized) == len(bullets), [l for l in bullets if l not in sized])

# A process whose PROMPT quotes "dispatcher/mergegate.py" is not a running gate. Here the false
# positive is the dangerous direction: a phantom gate makes the ghost-row check skip a genuinely
# abandoned row, and this reporter would go quiet about exactly the class it was built to find.
C.gate_processes = _REAL_GATE_PROCESSES
real_sh = C.sh
def fake_ps(argv, **k):
    if argv[:2] == ["ps", "-eo"]:
        return 0, ("  111 /usr/bin/python3 /x/dispatcher/mergegate.py B.010.real --no-box\n"
                   "  222 /x/bin/codex exec review ... run: /x/dispatcher/mergegate.py B.010.ghost\n")
    pid = argv[argv.index("-p") + 1]
    return 0, {"111": "/usr/bin/python3", "222": "/x/bin/codex"}[pid] + "\n"
C.sh = fake_ps
procs, why = C.gate_processes()
check("MUST-BITE  a Codex process whose PROMPT quotes the gate command is NOT counted as a running "
      "gate — our own instruction text matches our own monitoring greps, and here a phantom gate "
      "SILENCES the abandoned-row check",
      procs == {"B.010.real"}, (procs, why))
C.sh = real_sh
C.gate_processes = lambda: (set(), None)

# `ps` unreadable must degrade to not-measured. Reporting every gating row as abandoned because the
# process list could not be read is the loudest possible version of this report's own failure mode.
C.gate_processes = lambda: (None, "`ps` could not be read")
blind = C.render(*C.load_queue()).split("## 6.")[1].split("## 7.")[0]
check("MUST-BITE  when the process list cannot be read, the gate check says NOT MEASURED and faults "
      "nobody — an absence of evidence is not evidence of absence",
      NMTXT in blind and "no mergegate process" not in blind,
      [l for l in blind.splitlines() if NMTXT in l][:2])
check("  ...and the OTHER checks still run and still report", "EXEC-K" in blind)
C.gate_processes = lambda: (set(), None)

# a clean board must say so as a RESULT, listing what it looked at
clean = C.disagreements([{"id": "x", "status": "merged"}])
check("MUST-BITE  a clean board yields no findings but still names its checks",
      clean[0] == [] and len(clean[1]) >= 4, clean[1][:2])

# ---------------------------------------------------------------- 7. the ledger, named not read
sec6 = md.split("## 6.")[1]
check("MUST-BITE  the report says NO ledger exists at the CN root rather than staying silent",
      "No " in sec6 and "root" in sec6, sec6.strip().splitlines()[:2])
check("MUST-BITE  and it says nothing above was derived from a ledger",
      "Nothing in sections 1-5 is derived from a ledger" in sec6)
os.makedirs(os.path.join(ROOT, "lane-012", "plans"))
open(os.path.join(ROOT, "lane-012", "plans", "EXECUTION_LEDGER.md"), "w").write("ARTIFACT: x\n")
withlane = C.render(*C.load_queue()).split("## 7.")[1]
check("MUST-BITE  a lane copy is NAMED, with its directory, so BOSS can go and read the right one",
      "lane-012" in withlane and "EXECUTION_LEDGER.md" in withlane,
      withlane.strip().splitlines()[-3:])
# put the tree back exactly as it was: the re-runnability check below compares two renders, and a
# fixture left behind by an earlier check would make them differ for a reason that is not a bug.
shutil.rmtree(os.path.join(ROOT, "lane-012"))

# ---------------------------------------------------------------- the two rules themselves
check("MUST-BITE  no section is silently empty: every heading has content under it",
      all(len([x for x in md.split(h)[1].strip().splitlines() if x.strip()]) > 0
          for h in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.")))
second = C.render(*C.load_queue())
strip = lambda t: re.sub(r"# Overnight checkpoint — .*", "", t)
check("MUST-BITE  re-runnable: same inputs, same document (only the run stamp moves)",
      strip(second) == strip(md))

# a source that is GONE must not fake a zero
shutil.rmtree(REPO)
gone = C.render(*C.load_queue())
check("MUST-BITE  a missing trunk checkout is not-measured, never 'no merges tonight'",
      NMTXT in gone.split("## 2.")[0] and "No merge commits" not in gone,
      gone.split("## 2.")[0].splitlines()[-2:])
os.rename(os.path.join(D, "queue.json"), os.path.join(D, "queue.json.away"))
noq = C.render(*C.load_queue())
check("MUST-BITE  an unreadable queue is announced at the top, not rendered as an empty board",
      "unreadable" in noq and "Nothing in flight" not in noq)

shutil.rmtree(ROOT, ignore_errors=True)
print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
