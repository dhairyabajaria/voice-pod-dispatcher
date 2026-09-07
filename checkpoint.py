#!/usr/bin/env python3
"""checkpoint.py [since-sha] — the night's state as markdown, generated from evidence.

BOSS, 2026-09-07: "I need it generated from evidence, not from my memory of the night, because I
have been wrong three times about my own queue rows alone."

TWO RULES, and they are the whole design:
  1. It reports what it can MEASURE and prints "not measured" where it cannot — never an inferred
     value, and never a blank that reads as zero. A blank cell in a report is read as "none", which
     is a claim; "not measured" is the truth when the source does not carry the fact.
  2. It is re-runnable. BOSS runs it again after 09:00 and the owner sees the same shape with newer
     numbers, so nothing here depends on when it is run or on state left by a previous run.

Sources, all read-only: the queue, events.log, the trunk git log, and the ledger if one is present.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CN = os.environ.get("CN", "/Users/dhairyabajaria/Claude Code/Calling New")
D = os.path.join(CN, "test-logs", "driver")
QUEUE = os.path.join(D, "queue.json")
EVENTS = os.path.join(D, "events.log")
GATES = os.path.join(D, "gates")
TRUNK_REPO = os.path.join(CN, "voice-pod")
TRUNK = "plan010/rebuild"
SINCE = "0dbc9ae1"
RULES_LANDED = "2026-09-07 02:23"      # when the standing rules landed (item-3 split point)
NM = "_not measured_"

UNFINISHED = ("dispatched", "rework", "reported", "gating", "gated")


def sh(args, cwd=None):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def load_queue():
    try:
        return json.load(open(QUEUE)).get("items", []), None
    except Exception as e:
        return [], f"{QUEUE} unreadable: {e}"


def first_line(*vals):
    """The first non-empty value's first line, or None. Never invents one."""
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[0].strip()
    return None


def age(ts):
    """Minutes since an ISO-ish 'YYYY-MM-DD HH:MM:SS', or None if it cannot be parsed."""
    try:
        d = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    return int((datetime.now() - d).total_seconds() // 60)


# ------------------------------------------------------------------ 1. merged tonight
MERGE_RE = re.compile(r"merge\s+(\S+)\s+@\s*([0-9a-f]{7,40})\s*[-—]+\s*(.*)$")
VERDICT_RE = re.compile(r"Codex Verdict:\s*([^.]+)", re.I)


def merged_section(since=None):
    since = since or SINCE
    if not os.path.isdir(os.path.join(TRUNK_REPO, ".git")):
        return [f"{NM} — no trunk checkout at {TRUNK_REPO}"], []
    rc, out = sh(["git", "log", "--merges", "--format=%H%x00%s", f"{since}..{TRUNK}"], cwd=TRUNK_REPO)
    if rc != 0:
        return [f"{NM} — `git log {since}..{TRUNK}` failed: {out.strip()[:200]}"], []
    rows = []
    for ln in out.splitlines():
        if "\x00" not in ln:
            continue
        merge_sha, subject = ln.split("\x00", 1)
        m = MERGE_RE.search(subject)
        item = m.group(1) if m else NM
        lane = m.group(2) if m else NM
        vm = VERDICT_RE.search(subject)
        verdict = vm.group(1).strip() if vm else NM
        # Rule 3 is a claim about EVIDENCE, so it is read from the record, never inferred from the
        # fact of a merge: a merge with no non-builder audit and no named passing test on the merged
        # tree is MERGED, not LANDED. Only an explicit LANDED in the commit says otherwise.
        up = subject.upper()
        if "NOT LANDED" in up or "MERGED, NOT LANDED" in up:
            state = "MERGED (explicitly not landed)"
        elif re.search(r"\bLANDED\b", up):
            state = "LANDED"
        else:
            state = "MERGED"
        rows.append({"item": item, "lane": lane, "merge": merge_sha[:10], "verdict": verdict,
                     "state": state})
    return None, rows


# ------------------------------------------------------------------ 4. the item-3 measurement
GATE_LINE = re.compile(r"^(\S+ \S+)\t(GATE_[A-Z]+)\tGATE\t-\t(\S+)\t(.*)$")
DISPATCHED_IN_LINE = re.compile(r"dispatched_at=([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8})")
CODEX_IN_LINE = re.compile(r"codex adversarial review=(NOT RUN|\w+)", re.I)
# NOT DECLARED before NOT RUN, and both before \w+: `\w+` alone captures the bare word "NOT" from a
# NOT DECLARED row, which then counts as its own state and appears in no column (2026-09-07).
PROOFS_IN_LINE = re.compile(r"(?<!portal )proofs=(NOT DECLARED|NOT RUN|\w+)", re.I)


def gate_ratio(items, n=20):
    """PASS:FAIL for the last n GATE_* events, split by the item's dispatch time.

    dispatched_at rides on the GATE line only since 2026-09-07 03:38; for older lines it is looked
    up in the queue, and where NEITHER carries it the gate is counted as unmeasured rather than
    dropped into whichever cell would look tidier.
    """
    by_id = {i["id"]: i for i in items}
    gates = []
    try:
        for ln in open(EVENTS, errors="ignore"):
            m = GATE_LINE.match(ln.rstrip("\n"))
            if not m:
                continue
            when, kind, item_id, detail = m.groups()
            dm = DISPATCHED_IN_LINE.search(detail)
            disp = dm.group(1) if dm else (by_id.get(item_id, {}).get("dispatched_at") or None)
            cm = CODEX_IN_LINE.search(detail)
            pm = PROOFS_IN_LINE.search(detail)
            gates.append({"when": when, "kind": kind, "item": item_id, "dispatched_at": disp,
                          "codex": (cm.group(1).upper() if cm else None),
                          "proofs": (pm.group(1).upper() if pm else None)})
    except OSError as e:
        return None, f"{EVENTS} unreadable: {e}"
    return gates[-n:], None


def split_gates(gates, at=RULES_LANDED):
    before = {"PASS": 0, "FAIL": 0, "INCOMPLETE": 0, "OTHER": 0}
    after = {"PASS": 0, "FAIL": 0, "INCOMPLETE": 0, "OTHER": 0}
    unmeasured = []
    for g in gates:
        cell = {"GATE_PASS": "PASS", "GATE_FAIL": "FAIL",
                "GATE_INCOMPLETE": "INCOMPLETE"}.get(g["kind"], "OTHER")
        if not g["dispatched_at"]:
            unmeasured.append(g)
            continue
        (before if g["dispatched_at"] < at else after)[cell] += 1
    return before, after, unmeasured


def split_component(gates, field, at=RULES_LANDED):
    before = {"PASS": 0, "FAIL": 0, "NOT RUN": 0, "NOT DECLARED": 0}
    after = {"PASS": 0, "FAIL": 0, "NOT RUN": 0, "NOT DECLARED": 0}
    missing = 0
    for g in gates:
        v = g.get(field)
        if not g["dispatched_at"] or v not in ("PASS", "FAIL", "NOT RUN", "NOT DECLARED"):
            missing += 1
            continue
        (before if g["dispatched_at"] < at else after)[v] += 1
    return before, after, missing


def ratio(cell):
    p, f = cell["PASS"], cell["FAIL"]
    if p + f == 0:
        return NM
    return f"{p}:{f}"


# ------------------------------------------------------------------ status vs evidence
STALE_HOLD_MIN = 360        # 6h. Printed with every finding, so the threshold is never implicit.


def gate_processes():
    """Item ids with a mergegate process alive right now. -> (set, None) or (None, why).

    Read-only, and it must be able to say it does not know: if `ps` cannot be read, every `gating`
    row would look abandoned, which is the loudest possible version of the mistake this whole
    report exists to avoid — an absence of evidence printed as evidence of absence.
    """
    rc, out = sh(["ps", "-eo", "pid=,args="])
    if rc != 0:
        return None, "`ps` could not be read"
    live = set()
    for ln in out.splitlines():
        if "dispatcher/mergegate.py" not in ln or " grep " in ln:
            continue
        parts = ln.split()
        # TWO STAGES, and here the false positive is the DANGEROUS direction. BOSS, 2026-09-07: an
        # argv grep for `./.venv/bin/python -m pytest` returned 6 with ONE pytest running, because
        # the handoff instructions we hand every Codex auditor quote that command and the whole
        # prompt sits in argv. Our own instruction text now matches our own monitoring greps.
        # If a prompt ever quotes "dispatcher/mergegate.py" — this file's own reports do — a
        # phantom "gate is running" would make the ghost-gate check below SKIP a genuinely
        # abandoned row. A reporter that misses a finding because of a string in somebody's prompt
        # is worse than one that reports too many, so the pid's own comm is resolved.
        # `ps -p <pid> -o comm=` as the ONLY field comes back untruncated; in the multi-column form
        # macOS truncates comm to 16 chars, which is how the "corrected" version of this predicate
        # under-counts and reports live things as dead.
        rc2, comm = sh(["ps", "-p", parts[0], "-o", "comm="])
        if rc2 != 0 or not os.path.basename(comm.strip()).lower().startswith("python"):
            continue
        for i, tok in enumerate(parts):
            if tok.endswith("mergegate.py") and i + 1 < len(parts):
                live.add(parts[i + 1])
    return live, None


def disagreements(items):
    """Rows whose STATUS and whose EVIDENCE do not agree. -> (findings, checks_run, unmeasured)

    BOSS, 2026-09-07: when a classification bug is fixed, the rows it already wrote are still wrong
    and nothing looks for them. B.010.circleci-three-reds-triage sat at `reported` with no sha from
    03:41:02 — written by the PLAN-classified-as-REPORT bug — while its executor carried on
    building, and the two 23-hour `gated` rows surfaced only because this report happened to print
    them. The fix stops new instances; nothing was looking for the residue.

    Every check below names the rows it EXAMINED as well as the ones it faulted. A clean list and a
    check that never ran look identical otherwise, and that confusion is the house failure.
    """
    live = [i for i in items if str(i.get("status") or "").lower() in UNFINISHED]
    findings, checks, unmeasured = [], [], []

    def sha_of(i):
        return i.get("sha") or i.get("report_sha") or i.get("gate_sha")

    reported = [i for i in live if str(i.get("status")).lower() == "reported"]
    checks.append(f"`reported` with no sha recorded anywhere ({len(reported)} row(s) examined)")
    for i in reported:
        if not sha_of(i):
            findings.append((i["id"], "status `reported` but no sha in `sha`, `report_sha` or "
                                      "`gate_sha` — the row says a candidate exists and no evidence "
                                      "of one is recorded"))

    gating = [i for i in live if str(i.get("status")).lower() in ("gating", "gated")]
    procs, why = gate_processes()
    if procs is None:
        unmeasured.append(f"`gating`/`gated` with no gate file and no gate process — {why}")
    else:
        checks.append(f"`gating`/`gated` with neither a gate file nor a running gate "
                      f"({len(gating)} row(s) examined)")
        for i in gating:
            if i["id"] in procs or os.path.isfile(os.path.join(GATES, f"{i['id']}.md")):
                continue
            findings.append((i["id"], f"status `{i.get('status')}` but no "
                                      f"gates/{i['id']}.md and no mergegate process for it — "
                                      f"nothing is gating this and nothing did"))

    checks.append(f"one executor holding two unfinished rows ({len(live)} row(s) examined)")
    held = {}
    for i in live:
        who = i.get("dispatched_to")
        if who:
            held.setdefault(str(who), []).append(i["id"])
    for who, ids in sorted(held.items()):
        if len(ids) > 1:
            findings.append((", ".join(sorted(ids)),
                             f"{who} holds {len(ids)} unfinished rows at once — an executor works "
                             f"one item, so at most one of these is real"))

    checks.append(f"two unfinished rows on one worktree ({len(live)} row(s) examined)")
    trees = {}
    for i in live:
        wt = str(i.get("worktree") or "")
        if wt and not wt.startswith("("):
            trees.setdefault(wt, []).append(i["id"])
    for wt, ids in sorted(trees.items()):
        if len(ids) > 1:
            findings.append((", ".join(sorted(ids)),
                             f"both claim the worktree {wt} — one of them is building on top of "
                             f"the other's tree"))

    dated = [i for i in live if age(i.get("dispatched_at")) is not None]
    checks.append(f"held longer than {STALE_HOLD_MIN} min ({len(dated)} row(s) with a dispatch "
                  f"time; {len(live) - len(dated)} without one, not measurable)")
    for i in dated:
        a = age(i.get("dispatched_at"))
        if a > STALE_HOLD_MIN:
            findings.append((i["id"], f"status `{i.get('status')}` and held {a} min "
                                      f"({a // 60}h) — longer than the {STALE_HOLD_MIN} min this "
                                      f"report calls stale; the threshold is a prompt to look, "
                                      f"not a verdict"))

    checks.append(f"worktree path missing from disk ({len(live)} row(s) examined)")
    for i in live:
        wt = str(i.get("worktree") or "")
        if wt and not wt.startswith("(") and not os.path.isdir(wt):
            findings.append((i["id"], f"its worktree {wt} does not exist on disk"))

    return findings, checks, unmeasured


# ------------------------------------------------------------------ the ledger, named not read
LEDGER_REL = os.path.join("plans", "EXECUTION_LEDGER.md")


def ledger_copies():
    """Where a ledger exists on this box. NOT a source for anything above — an inventory.

    BOSS asked (2026-09-07) that the report name the lane copy rather than stay silent about the
    ledger. It is named and not read on purpose: there is no copy at the CN root, and a lane's copy
    is a snapshot from whenever that lane branched. Quietly promoting one lane's ledger to "the
    program's ledger" is exactly the inferred value rule 1 forbids — and citesweep already hit this
    one the other way round, reporting a row MISSING because it read a lane copy instead of trunk.
    """
    root = os.path.join(CN, LEDGER_REL)
    out = []
    try:
        for name in sorted(os.listdir(CN)):
            d = os.path.join(CN, name)
            f = os.path.join(d, LEDGER_REL)
            if os.path.isdir(d) and os.path.isfile(f):
                out.append((name, f, os.path.getmtime(f)))
    except OSError:
        return os.path.isfile(root), None
    return os.path.isfile(root), out


# ------------------------------------------------------------------ the document
def render(items, q_err):
    L = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L.append(f"# Overnight checkpoint — {now}")
    L.append("")
    L.append(f"Generated by `dispatcherctl.sh checkpoint` from the queue, events.log and the trunk "
             f"git log. Every figure below is measured; anything the sources do not carry is printed "
             f"as {NM} rather than left blank or inferred.")
    if q_err:
        L += ["", f"> **{q_err}** — every queue-derived section below is {NM}."]
    L.append("")

    # 1
    L.append("## 1. Merged tonight")
    L.append("")
    err, rows = merged_section()
    if err:
        L += err + [""]
    elif not rows:
        L += [f"No merge commits on `{TRUNK}` since `{SINCE}`.", ""]
    else:
        L.append("| item | lane sha | merge sha | reviewer verdict | Rule 3 |")
        L.append("|---|---|---|---|---|")
        for r in rows:
            L.append(f"| {r['item']} | `{r['lane'][:10]}` | `{r['merge']}` | {r['verdict']} | {r['state']} |")
        L += ["",
              "**Rule 3.** MERGED means the change is on trunk. LANDED additionally requires a "
              "non-builder audit and a named passing test ON THE MERGED TREE. A row saying MERGED is "
              "not a claim of completion — it is the absence of that second evidence, and the column "
              "is read from the merge commit's own record rather than inferred from the merge.", ""]

    # 2
    L.append("## 2. Held / in rework")
    L.append("")
    stuck = [i for i in items if str(i.get("status", "")).lower() in ("held", "rework")]
    if not stuck:
        L += ["Nothing held or in rework." if not q_err else NM, ""]
    else:
        L.append("| item | status | round | reason |")
        L.append("|---|---|---|---|")
        for i in sorted(stuck, key=lambda i: i["id"]):
            rnd = i.get("rework_round")
            reason = first_line(i.get("gate_verdict"), i.get("hold_reason"), i.get("note_boss"),
                                i.get("boss_note"), i.get("note"))
            L.append(f"| {i['id']} | {i.get('status')} | {rnd if rnd is not None else NM} | "
                     f"{(reason[:150] + '…') if reason and len(reason) > 150 else (reason or NM)} |")
        L.append("")

    # 3
    L.append("## 3. In flight")
    L.append("")
    live = [i for i in items if str(i.get("status", "")).lower() in UNFINISHED]
    if not live:
        L += ["Nothing in flight." if not q_err else NM, ""]
    else:
        L.append("| executor | item | status | held for |")
        L.append("|---|---|---|---|")
        for i in sorted(live, key=lambda i: (str(i.get("dispatched_to")), i["id"])):
            a = age(i.get("dispatched_at"))
            L.append(f"| {i.get('dispatched_to') or NM} | {i['id']} | {i.get('status')} | "
                     f"{str(a) + ' min' if a is not None else NM} |")
        L.append("")

    # 4
    L.append("## 4. Gate outcomes before and after the standing rules")
    L.append("")
    gates, g_err = gate_ratio(items)
    if g_err:
        L += [f"{NM} — {g_err}", ""]
    else:
        before, after, unmeasured = split_gates(gates)
        L.append(f"Last {len(gates)} `GATE_*` events, split by whether the item's `dispatched_at` "
                 f"was before or after **{RULES_LANDED}**, when the standing rules landed.")
        L.append("")
        L.append("| dispatched | PASS:FAIL | passes | fails | incomplete (a check did not run) | other |")
        L.append("|---|---|---|---|---|---|")
        for lbl, c in ((f"before {RULES_LANDED}", before), (f"after {RULES_LANDED}", after)):
            L.append(f"| {lbl} | {ratio(c)} | {c['PASS']} | {c['FAIL']} | {c['INCOMPLETE']} | {c['OTHER']} |")
        L.append("")
        L.append("An INCOMPLETE gate is in neither the pass nor the fail column: a check that could "
                 "not run is not a check that failed, and it is never a pass. The PASS:FAIL ratio "
                 "counts only gates where every check produced a result.")
        L.append("")
        L.append(f"Sample size {len(gates)} gate event(s)"
                 + (f"; **{len(unmeasured)} event(s), across "
                    f"{len({g['item'] for g in unmeasured})} item(s), carry no dispatch time in "
                    f"either the event or the queue and are counted in NEITHER cell** ("
                    + ", ".join(sorted({g["item"] for g in unmeasured}))[:200] + ")."
                    if unmeasured else "; every one carries a dispatch time."))
        # The overall verdict is the AND of every check, so it can sit at 0 passes for a reason
        # that has nothing to do with review quality — a --no-box gate scores proofs=FAIL by
        # construction. Both component splits below are read from the same event text.
        for field, label in (("codex", "Codex adversarial review"), ("proofs", "proofs")):
            cb, ca, miss = split_component(gates, field)
            L.append("")
            L.append(f"Component: **{label}**, same events, same split.")
            L.append("")
            # "never declared" is its OWN column. Folded into "did not run" it would read as a box
            # problem, and the fix is a missing queue field on the item — a different person, a
            # different action (BOSS, 2026-09-07: he nearly reworked a 7/7-green lane over his own
            # omission because the row said FAIL).
            L.append("| dispatched | PASS:FAIL | passes | fails | did not run | never declared |")
            L.append("|---|---|---|---|---|---|")
            L.append(f"| before {RULES_LANDED} | {ratio(cb)} | {cb['PASS']} | {cb['FAIL']} | "
                     f"{cb['NOT RUN']} | {cb['NOT DECLARED']} |")
            L.append(f"| after {RULES_LANDED} | {ratio(ca)} | {ca['PASS']} | {ca['FAIL']} | "
                     f"{ca['NOT RUN']} | {ca['NOT DECLARED']} |")
            if miss:
                L.append("")
                L.append(f"{miss} of the {len(gates)} event(s) do not record `{label}` together with "
                         f"a dispatch time and are in neither cell.")
        L.append("")
        if len(gates) < 20:
            L.append("")
            L.append(f"> The request was for 20. events.log holds {len(gates)}; this is what there "
                     f"is, not a padded sample.")
        L.append("")

    # 5
    L.append("## 5. Open for the owner")
    L.append("")
    owner = [i for i in items
             if re.match(r"\s*BLOCKED:\s*owner", str(i.get("parked_reason") or ""), re.I)]
    if not owner:
        L += [f"No queue row carries a `BLOCKED:owner` marker" + ("." if not q_err else f" — {NM}."), ""]
    else:
        for i in sorted(owner, key=lambda i: i["id"]):
            reason = first_line(i.get("parked_reason")) or NM
            L.append(f"- **{i['id']}** — {reason}")
        L.append("")

    # 7 (printed before the ledger note, which is an appendix)
    L.append("## 6. Rows whose status and evidence disagree")
    L.append("")
    if q_err:
        L += [f"{NM} — the queue could not be read.", ""]
    else:
        found, checks, unmeasured = disagreements(items)
        if found:
            L.append("| row(s) | what disagrees |")
            L.append("|---|---|")
            for who, what in found:
                L.append(f"| {who} | {what} |")
        else:
            L.append("Nothing found. **This is a result, not a silence** — the checks below ran and "
                     "faulted no row:")
        L.append("")
        for c in checks:
            L.append(f"- {c}")
        for u in unmeasured:
            L.append(f"- {NM}: {u}")
        L.append("")
        L.append("A fixed classification bug stops writing bad rows; it does not repair the ones it "
                 "already wrote, and nothing else looks for them. These checks are that look. They "
                 "compare a row's status against evidence that would have to exist if the status "
                 "were true — nothing here is a judgement about the work.")
        L.append("")

    # 6
    L.append("## 7. The ledger")
    L.append("")
    root_has, copies = ledger_copies()
    if copies is None:
        L += [f"{NM} — {CN} could not be listed.", ""]
    else:
        L.append(f"**No `{LEDGER_REL}` exists at the CN root** ({CN})."
                 if not root_has else
                 f"A `{LEDGER_REL}` exists at the CN root ({CN}).")
        L.append("")
        L.append("Nothing in sections 1-5 is derived from a ledger. A lane's copy is a snapshot from "
                 "whenever that lane branched, so reading one as the program's ledger would be an "
                 "inferred value, not a measured one. These are named so you can go and read the "
                 "right one yourself:")
        L.append("")
        if not copies:
            L.append(f"- no `{LEDGER_REL}` found in any directory directly under the CN root")
        else:
            for name, f, mt in sorted(copies, key=lambda c: -c[2])[:12]:
                L.append(f"- `{name}` — {os.path.relpath(f, CN)}, last modified "
                         f"{datetime.fromtimestamp(mt):%Y-%m-%d %H:%M:%S}")
            if len(copies) > 12:
                L.append(f"- ...and {len(copies) - 12} more, newest first above")
        L.append("")

    L.append("---")
    L.append(f"Sources: `{os.path.relpath(QUEUE, CN)}`, `{os.path.relpath(EVENTS, CN)}`, "
             f"`git log {SINCE}..{TRUNK}` in `{os.path.relpath(TRUNK_REPO, CN)}`. "
             f"Re-runnable: `./dispatcher/dispatcherctl.sh checkpoint`.")
    return "\n".join(L) + "\n"


def main(argv):
    global SINCE
    if argv and argv[0].strip():
        SINCE = argv[0].strip()
    items, q_err = load_queue()
    sys.stdout.write(render(items, q_err))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
