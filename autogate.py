"""Auto-gate and auto-rework: the decision half, kept pure so it can be tested without a daemon.

BOSS + ★, 2026-09-07 (throughput diagnosis). Today every REPORT_READY waits for BOSS to launch a
gate by hand, and every gate FAIL waits for BOSS to read Codex's findings and re-prompt by hand.
Both are mechanical when the answer is unambiguous, and both cost the item a whole human round trip.

WHAT IS AUTOMATED IS DELIBERATELY NARROW. The daemon may run a gate, and it may hand a FAILING
candidate its reviewer's own findings and ask for another pass. It may NOT decide anything about a
candidate that PASSED: merges stay BOSS's, and a PASS is escalated, never acted on. The asymmetry is
the point — an unnecessary rework costs one executor turn, an unnecessary merge costs trunk.

Every function here takes data and returns a decision. Nothing in this module posts, writes or
launches; dispatcher.py does that, so the rules can be tested against recorded gate files.
"""
import os
import re

# Codex verdicts that are a REAL reviewer opinion about the candidate. Anything else — an outage, a
# wall, an unreadable answer, a crash — is not a finding and must never drive an automatic rework:
# reworking on a non-answer teaches the executor that the gate is noise.
REAL_VERDICTS = ("needs-attention", "reject", "needs_attention", "request-changes")
MAX_AUTO_FAILS = 2          # 3rd consecutive fail goes to BOSS


def parse_gate_md(text):
    """-> dict(verdict, rows{name: (ok, detail)}, codex_detail, disagree, sha).

    Reads the gate's own file rather than re-deriving anything: the .md is what BOSS reads, so a
    decision taken from it can always be checked against what a human would have seen.
    """
    out = {"verdict": "", "rows": {}, "codex_detail": "", "disagree": False, "sha": "",
           "undeclared": []}
    lines = (text or "").splitlines()
    if lines:
        m = re.match(r"#\s*GATE\s+(\S+)\s+[-—]+\s*(.+?)\s*(?:[-—]+\s*(.*))?$", lines[0])
        if m:
            out["verdict"] = m.group(2).strip()
    for ln in lines:
        # NOT RUN is a third row status (mergegate, 2026-09-07). It MUST be parsed, not skipped:
        # an unparsed row is invisible to failed_rows() and to every reason string built from it,
        # so a gate that never ran its proofs would have read here exactly like one that ran them.
        # The value is True / False / None, and None is neither a pass nor a failure.
        # NOT DECLARED is a FOURTH status (mergegate, 2026-09-07 08:3x). Note the alternation order:
        # `NOT RUN|NOT DECLARED` would match "NOT" first on a NOT DECLARED row and leave " DECLARED"
        # to the detail group, so the row would parse as neither status and the item's missing queue
        # field would read as a row that simply is not there.
        m = re.match(r"\s*[-*]\s*\*\*(.+?)\*\*\s*[:=]\s*(PASS|FAIL|NOT DECLARED|NOT RUN)\s*[-—]*\s*(.*)$", ln)
        if m:
            st = m.group(2)
            out["rows"][m.group(1).strip()] = (None if st.startswith("NOT ") else st == "PASS",
                                               m.group(3).strip())
            if st == "NOT DECLARED":
                out["undeclared"].append(m.group(1).strip())
        if "DISAGREE" in ln:
            out["disagree"] = True
        m2 = re.match(r"\s*sha\s+([0-9a-f]{7,40})", ln)
        if m2:
            out["sha"] = m2.group(1)
    row = out["rows"].get("codex adversarial review")
    out["codex_detail"] = row[1] if row else ""
    return out


def codex_opinion(detail):
    """-> (is_a_real_opinion, verdict_word). A wall, an outage or a non-answer is not an opinion."""
    d = detail or ""
    if not d or "WALLED" in d or "NOT RUN" in d.upper() or "not run" in d or "still running" in d:
        return False, "NONE"
    if "rc=" in d and not re.search(r"\brc=0\b", d):
        return False, "NONE"          # the reviewer did not exit clean; its text is not a verdict
    m = re.search(r"verdict=([a-z-]+)", d, re.I)
    v = (m.group(1) or "").lower() if m else ""
    if not v or v in ("none", "none found (fail-closed)"):
        return False, "NONE"
    return (v in REAL_VERDICTS), v


def finding_classes(codex_text):
    """The distinct defect classes a Codex review names. Used only to spot a REPEAT.

    Deliberately coarse — severity plus the file or symbol it names. Two reviews that both say
    "[high] platform/core/pay.py" are the same class for this purpose even if the wording differs;
    an executor that has been told twice about the same file and still fails is not converging, and
    another automatic round trip will not help.
    """
    out = []
    for m in re.finditer(r"\[(critical|high|medium|low)\][^\n]{0,200}", codex_text or "", re.I):
        seg = m.group(0)
        f = re.search(r"([\w./-]+\.(?:py|ts|tsx|yml|yaml|sql|sh))", seg)
        out.append(f"{m.group(1).lower()}:{f.group(1) if f else seg[:40].strip().lower()}")
    return sorted(set(out))


def failed_rows(gate):
    """Names of the gate rows that FAILED, in file order. The answer to "failed on what?".

    `if not ok` would sweep a NOT RUN row (ok is None) in here and report a check that could not run
    as a check that failed — the very confusion this status exists to end."""
    return [n for n, (ok, _d) in (gate.get("rows") or {}).items() if ok is False]


def not_run_rows(gate):
    """Names of the gate rows that did not run. Never counted as passes, never as failures."""
    return [n for n, (ok, _d) in (gate.get("rows") or {}).items() if ok is None]


def decide(gate, codex_text, history):
    """What to do with a finished gate. -> (action, reason).

    action is one of: "rework" (hand the findings back), "escalate" (BOSS decides), "none".
    `history` is this item's prior auto-gate record: {"fails": int, "classes": [..]}.

    Every escalate branch below is a case where the machine cannot tell the difference between a
    candidate problem and a system problem. Handing findings back in any of them would be asking an
    executor to fix something it did not do.
    """
    verdict = (gate.get("verdict") or "").upper()
    if verdict.startswith("PASS"):
        return "escalate", "GATE PASS — BOSS decides merges"
    if verdict.startswith("INCOMPLETE"):
        # Nothing failed and something did not run. There are no findings to hand back, and an
        # auto-rework here would ask an executor to fix a check the operator or the box skipped.
        # It is also not a pass: it goes to BOSS, exactly as a PASS does, and for the opposite
        # reason — a PASS is escalated because the machine may not merge, this because nobody has
        # measured the thing yet.
        # Two reasons a gate is INCOMPLETE, and they are handed to DIFFERENT people: an unrun check
        # is re-run when the box frees, an undeclared one is a missing queue field that only the
        # dispatcher of the item can fix. Reporting the second as "did not run" sends BOSS to look
        # for a box problem that does not exist — which is the confusion that created this status.
        und = list(gate.get("undeclared") or [])
        nr = [n for n in not_run_rows(gate) if n not in und]
        why = "; ".join(x for x in (f"{', '.join(nr)} did not run" if nr else "",
                                    f"{', '.join(und)} {'was' if len(und) == 1 else 'were'} never declared "
                                    f"by the item" if und else "")
                        if x) or "a check produced no result"
        tail = (" — the item's queue row is missing the field the row names, so this is a defect in "
                "the ITEM, not in the lane") if und else ""
        return "escalate", (f"GATE INCOMPLETE — nothing failed, but {why}, so this candidate is "
                            f"unproven, not approved{tail}")
    if "NOT COMPLETED" in verdict or "CRASH" in verdict:
        return "escalate", "the gate did not complete — no verdict about the candidate exists"
    if gate.get("disagree"):
        return "escalate", "the two reviewers DISAGREE — a rework would pick a side the gate did not"
    real, v = codex_opinion(gate.get("codex_detail", ""))
    if not real and v == "approve":
        # Measured 2026-09-07 04:10:03 on the FIRST daemon-launched gate: Codex APPROVED and the
        # gate failed on proofs, and this branch told BOSS "the Codex row carries no reviewer
        # opinion (verdict=approve) — nothing was measured about this candidate by the reviewer".
        # Every word of that is false. The ACTION was right — an approve carries no findings, so
        # there is nothing to hand back — but a reason that misdescribes the evidence is worse than
        # no reason: BOSS reads it instead of the gate file, and would go looking for a reviewer
        # outage that did not happen.
        failed = failed_rows(gate)
        return "escalate", (f"Codex APPROVED this candidate; the gate failed on "
                            f"{', '.join(failed) if failed else 'another row'} — there are no "
                            f"reviewer findings to hand back, so BOSS decides")
    if not real:
        return "escalate", (f"the Codex row carries no reviewer opinion (verdict={v or 'none'}) — "
                            f"nothing was measured about this candidate by the reviewer")
    fails = int((history or {}).get("fails", 0)) + 1
    if fails > MAX_AUTO_FAILS:
        return "escalate", (f"this is consecutive auto-fail #{fails} — the item is not converging "
                            f"and another automatic round trip is unlikely to change that")
    new = finding_classes(codex_text)
    repeated = sorted(set(new) & set((history or {}).get("classes") or []))
    if repeated:
        return "escalate", (f"Codex names the same finding class again ({', '.join(repeated[:3])}) — "
                            f"the previous rework did not land it")
    return "rework", f"Codex {v} with {len(new)} finding class(es) — handing them back verbatim"


# Executors the daemon cannot prompt: they are Claude sessions with no opencode session id, so
# BOSS relays to them by message. Their reworks become a PENDING ROW instead of a post.
RELAYED_EXECUTORS = ("WORKER-1", "WORKER-2", "BOSS")


def is_relayed(executor):
    """Does this executor need BOSS to relay, rather than a direct prompt?

    BOSS, 2026-09-07: auto-gate's entry point is the daemon's REPORT_READY detection on OpenCode
    sessions, so WORKER-1's lanes never enter it and BOSS hand-launches those gates and hand-writes
    their reworks. The GATE half is identical for every lane; only the delivery differs. Sending
    "posted to the executor" for a session that cannot be posted to would be the worse failure — the
    findings would sit in the item file with nobody told.
    """
    return str(executor or "").upper() in RELAYED_EXECUTORS


def rework_block(n, when, sha, codex_text):
    """The block appended to the ITEM FILE. Codex's text is included VERBATIM and unsummarised.

    Verbatim matters: a summary is a second reading of a review that already had to be read once,
    and every paraphrase is a chance to drop the sentence the executor needed. The header names the
    sha so a reader can tell which candidate the findings are about.
    """
    return (f"\n\n## REWORK {n} (auto, {when}) — gate at {sha or 'unknown sha'}\n\n"
            f"The merge gate FAILED this candidate. Codex's review is reproduced verbatim below; it "
            f"has not been summarised, filtered or ranked. Address every finding or say in your "
            f"report why it is not a defect.\n\n"
            f"```\n{(codex_text or '').strip()}\n```\n")


def rework_prompt(item_id, n, sha, codex_text, standing_rules):
    """What the executor is told. Standing rules FIRST, then the findings."""
    return (f"{standing_rules}\n\n"
            f"DISPATCHER AUTO-REWORK {n} for {item_id} — the merge gate FAILED the candidate at "
            f"{sha or 'the reported sha'} and this block is already appended to your item file as "
            f"'## REWORK {n}'. Codex's findings are reproduced VERBATIM below.\n\n"
            f"Work the findings in your existing worktree and lane, re-run your proof files, and end "
            f"at REPORT READY as usual. If a finding is NOT a defect, say so in the report and give "
            f"the measurement that shows it — do not silently skip it.\n\n"
            f"```\n{(codex_text or '').strip()}\n```\n")


# A report path as the executors are told to write them (Part 5.3): reports/<item>-<artifact>.md,
# with or without the audit/plan-execution-.../ prefix.
REPORT_PATH_RE = re.compile(r"[\w./-]*reports/[\w.+-]+\.md")

# Statuses whose items are still IN FLIGHT and can legitimately produce a report to gate. Anything
# else — merged, held, done, parked, broken, queued — must never be gated by an incoming message.
# `reported` is here since 2026-09-07 03:2x. feed() sets that status on the REPORT READY message
# before this check runs, so the status a report arrives with is `reported` by the time anyone looks;
# refusing it here meant a gate could fire only on the first pass and never on a corrected second
# report. The idle-ack protection this list was carrying is not lost — it lives in the report-path
# evidence below, which is the half that actually distinguishes a candidate from a sentence. The
# statuses that must NOT gate (merged, held, done, parked) are excluded upstream by current_item().
GATEABLE_STATUS = ("dispatched", "rework", "reported")


def report_trigger_ok(item, text, exists):
    """May this REPORT_READY launch a gate? -> (bool, reason).

    BOSS, 2026-09-07, measured live: EXEC-B answered an "are you idle?" note with
        "REPORT READY: idle — lane ... clean (merged ...), no lock held ... Awaiting next dispatch."
    and the board raised REPORT_READY. Under auto-gate that launches a real gate — Codex run, box
    queue and all — on an item that was already MERGED. The marker is a token an executor can write
    in a sentence about anything; it is not evidence that a report exists.

    So the trigger needs a fact, not a token: a report path that EXISTS at the lane head, on an item
    that is still in flight. `exists` is a callable so this stays testable without a checkout.
    """
    st = str(item.get("status") or "").lower()
    if st not in GATEABLE_STATUS:
        return False, (f"the item is {st or 'unknown'}, not {'/'.join(GATEABLE_STATUS)} — a message "
                       f"on a finished item is an idle note, not a candidate")
    paths = REPORT_PATH_RE.findall(text or "")
    if not paths:
        return False, ("the message names no report path — 'REPORT READY' in a sentence is a token, "
                       "not evidence that a report exists")
    for rel in paths:
        if exists(rel):
            return True, f"report {rel} exists at the lane head"
    return False, (f"the message names {len(paths)} report path(s) but none exist at the lane head "
                   f"({', '.join(paths[:2])}) — nothing to gate")


# A report that arrived while the daemon was restarting is handled by neither process: the old one
# logged it as handled, the new one reads that map and skips it. BOSS, 2026-09-07, first live case —
# EXEC-G's REPORT_READY for B.014a.artifact-custody-r3 at 02:55:26, real report at the lane head, and
# no gate launched by anyone. Left alone, every restart of ours silently drops whatever reported
# during it, and the drop is invisible: the board shows a normal item and no event says otherwise.
RECOVERABLE_STATUS = ("dispatched", "rework", "reported")


def recovery_ok(item, text, exists):
    """May a startup pass re-launch the gate for this already-handled REPORT_READY? -> (bool, why).

    The evidence is the same one report_trigger_ok demands — a report path that EXISTS at the lane
    head — because that is the half that separates a candidate from an executor writing the marker in
    a sentence about something else, and recovery must not resurrect the idle-ack bug the trigger
    guard fixed.

    The status half is necessarily weaker: the old process set `reported` before it died, so the
    pre-report status is gone. `reported` is therefore accepted HERE and refused in the automatic
    path, where it would mean "a message on an item we just finished with".
    """
    st = str(item.get("status") or "").lower()
    if st not in RECOVERABLE_STATUS:
        return False, f"the item is {st or 'unknown'} — nothing to recover"
    paths = REPORT_PATH_RE.findall(text or "")
    if not paths:
        return False, "the message names no report path — an idle note, not a dropped candidate"
    for rel in paths:
        if exists(rel):
            return True, f"report {rel} exists at the lane head and no gate ran for it"
    return False, (f"the message names {len(paths)} report path(s) but none exist at the lane head "
                   f"({', '.join(paths[:2])})")


# Owner directive 2026-09-07 03:2x, in EXECUTOR_STANDING_RULES.md: 3 of the last 7 gate [high]s
# were multi-step writes with a green happy path, so a report must name the interrupt test that
# stops the process BETWEEN the steps — or say N/A with a reason. Exactly one line, because two
# lines is an executor hedging and none is the artefact missing.
INTERRUPT_RE = re.compile(r"^\s*INTERRUPT-TEST:\s*(\S.*?)\s*$", re.M)
NA_RE = re.compile(r"^N/A\b\s*[-—:]\s*\S", re.I)

INTERRUPT_FIX = ("Add exactly one line to your REPORT READY: `INTERRUPT-TEST: <file::test_name>` "
                 "naming a test that stops the process BETWEEN two write steps, or "
                 "`INTERRUPT-TEST: N/A — <one-line reason this change has no multi-step write>`. "
                 "Then re-report; nothing else needs redoing.")


def interrupt_test_ok(text):
    """Does this report carry exactly one usable INTERRUPT-TEST line? -> (bool, reason).

    A bare `N/A` with no reason is refused: the reason is the whole point of the escape hatch, and
    "N/A" alone is indistinguishable from not having thought about it.
    """
    hits = INTERRUPT_RE.findall(text or "")
    if not hits:
        return False, ("no-interrupt-test: the report names no INTERRUPT-TEST line (owner directive, "
                       "EXECUTOR_STANDING_RULES.md)")
    if len(hits) > 1:
        return False, (f"no-interrupt-test: the report names {len(hits)} INTERRUPT-TEST lines and the "
                       f"rule is exactly one — say which test, or one N/A with its reason")
    v = hits[0]
    if v.upper().startswith("N/A"):
        if not NA_RE.match(v):
            return False, ("no-interrupt-test: `N/A` with no reason — the reason is the whole point "
                           "of the escape hatch; write `N/A — <why this change has no multi-step write>`")
        return True, f"interrupt test waived: {v[:120]}"
    if "::" not in v:
        return False, (f"no-interrupt-test: `{v[:80]}` names no test — the form is "
                       f"`<file>::<test_name>`, or `N/A — <reason>`")
    return True, f"interrupt test named: {v[:120]}"


def needs_box(proof_files):
    """Does this item's gate need the box? Mirrors mergegate: only `.py` proofs take it."""
    return any(str(p).endswith(".py") for p in (proof_files or []))


# `dispatcherctl.sh gate <id>` is BOSS naming an item by hand. It goes through the SAME machinery
# as the automatic path (gate_launchable -> mergegate -> decide -> rework_block), and differs in
# exactly one place: report_trigger_ok is not consulted.
#
# That check exists to tell a real candidate from an executor writing "REPORT READY" in a sentence
# about something else, on a stream of messages nobody read. BOSS typing the item id IS that
# judgement, already made by the only party the check was protecting. Requiring a report path here
# would refuse precisely the lanes this verb is for — WORKER-1/WORKER-2, whose reports arrive as
# messages to BOSS and never as an executor message the daemon can regex.
#
# `reported` is IN this list and absent from GATEABLE_STATUS for the same reason: the automatic path
# sets `reported` itself a moment before it decides, so allowing it there would let an idle note on
# a finished item start a gate. Named by hand, `reported` is the normal state to gate from.
MANUAL_GATEABLE_STATUS = ("dispatched", "rework", "reported")


def manual_gate_ok(item, in_flight):
    """May a hand-named gate launch for this item? -> (bool, reason).

    Only the item-shaped facts. Whether the sha is the lane head and whether the box is free are
    still gate_launchable's, so the manual verb cannot skip a control the daemon applies.
    """
    if not item:
        return False, "no such item in the queue"
    st = str(item.get("status") or "").lower()
    if st not in MANUAL_GATEABLE_STATUS:
        return False, (f"the item is {st or 'unknown'} — gate only "
                       f"{'/'.join(MANUAL_GATEABLE_STATUS)}; a merged or abandoned item has "
                       f"nothing to gate")
    if in_flight:
        return False, ("a gate for this item is already in flight — a second one would run against "
                       "the same lane and both would stamp the same gate file")
    return True, f"named by hand, status={st}"


def gate_launchable(item, gates_running, sha_ok):
    """-> (bool, reason). Whether the daemon may launch a gate for this item right now."""
    if not sha_ok:
        return False, "the report's sha is not the lane head — nothing stable to gate"
    if not item.get("worktree") or not item.get("lane"):
        return False, "the item has no worktree/lane"
    if gates_running and needs_box(item.get("proof_files")):
        return False, ("a gate is already running and this one needs the box — queued rather than "
                       "launched, so two gates never contend for it")
    return True, "box-free" if not needs_box(item.get("proof_files")) else "box"
