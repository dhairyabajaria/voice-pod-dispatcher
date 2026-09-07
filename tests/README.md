# dispatcher/tests — reruns for every dispatcher/** fix

Hermetic. No box, no Postgres, no network, no model call, no live state: each test builds its own
temp git repos or loads FIXTURES, and nothing here writes to test-logs/driver/.

    ../dispatcherctl.sh selftest        # all of them, one line, exit 0 = all green

## THE RULE, program-wide (BOSS, 2026-09-07)

**Any test asserting that something did NOT happen ships with a positive control that makes it
happen, in the same test file — or the negative assertion is not evidence.**

It was recorded after the fourth instance in one night of a check that cannot fail on the bug it is
named after, and the third that only a mutation exposed. The one that produced the rule: a test
named "AUTOPILOT does not merge a gate whose proofs never ran" pointed TRUNK at the lane worktree,
checked out at the lane branch, so a merge would have committed onto the lane and `git log
plan010/rebuild` could not have seen it either way. The most dangerous mutation in that change
scored ZERO reds, and the test was green throughout. `test_notrun.py` case E is what the rule looks
like in practice: a run with everything green that MERGES FOR REAL, sitting beside the cases that
must not.

The other three instances, for the shape rather than the detail: `NM in cell` asserting that a cell
is not blank, satisfied by a blank; a grep that could not tell live code from a commented-out
corpse; and a wall test that passed on the WRONG refusal. Same family every time — the assertion was
true, and it was true for a reason that had nothing to do with the behaviour it was named after.

- `test_fixes.py`       dispatcher.py — dep_ok resolution + DEP_UNRESOLVABLE, off-roster and
                        superseded-ERROR pruning, DEAD classification, feed-hold release.
- `test_cutmarker.py`   dispatcher.py — a PLAN/REPORT READY that lands inside a provider-cut turn:
                        recovery from behind our own RESUME, the cut-turn marker scan, and the
                        negative control that the scan alone would have missed 2026-09-06 20:45.
- `test_autogate.py`    autogate.py — WHAT the daemon may decide alone. The asymmetry is the
                        design and is asserted: it may hand a FAILING candidate its reviewer's own
                        findings, and may NEVER act on a PASS. An unnecessary rework costs one
                        executor turn; an unnecessary merge costs trunk.
- `test_autogate_driver.py` dispatcher.py — what actually gets written and posted. The worst driver
                        bug is a rework prompt sent TWICE for one gate file: the executor cannot
                        tell a repeat from a new result and redoes work it already did.
- `test_manualgate.py`  dispatcherctl.sh + dispatcher.py — `gate <item-id> [--no-box]`, the
                        hand-named gate that puts WORKER-1/WORKER-2 lanes through the SAME machinery
                        as the automatic ones. Two silent failures: a manual gate launched while
                        `auto_gate` is off would run and be read by nobody; and a rework "posted" to
                        a lane that has no opencode session would leave the findings in the item
                        file with nobody told. Also proves --no-box cannot skip the sha check or
                        jump the box queue.
- `test_restartdrop.py` dispatcher.py — what a RESTART drops. A REPORT_READY that lands while the
                        daemon is restarting is handled by neither process (the old one marks it
                        handled and exits, the new one skips it); roster.json was read once at
                        startup, so a changed threshold did nothing until a restart while being
                        called live; and current_item() matched only `dispatched`, so a `rework`
                        item could never reach the auto-gate branch that lists it. Also pins the
                        90 s stall threshold to the sample that decided it.
- `test_gatesilence.py` dispatcher.py — every non-launch says WHY. A report that was never gated
                        looked exactly like one nobody had made: a deferred launch was a log line,
                        never an event, and an executor holding no dispatched/rework item produced
                        nothing at all. Also: an idle ack the trigger has judged not a report raises
                        no pending row, while a REAL report that could not launch keeps its row —
                        withdrawing that one would hide the case that needs a human.
- `test_laneheld.py`    dispatcher.py — a lane stays occupied while anyone is reading or writing
                        its head. `gating` (the status BOSS's hand-launched gates write) and
                        `rework` were both missing from LANE_HOLDING, so the daemon dispatched onto
                        a lane whose worktree a live gate was running proofs in — an edit mid-gate
                        corrupts the proof run and the head row, and neither looks like a failure.
                        The running mergegate list is checked AS WELL AS the status, and a process
                        probe that fails returns unknown (holds) rather than an empty set (frees).
- `test_codexpath.py`   mergegate.py — the daemon could not find the codex binary. The companion
                        resolves the BARE NAME from PATH and launchd's PATH has no node bin dir, so
                        every auto-gate's Codex row came back "not installed" for a binary that was
                        on disk. Measured against the real binary and the real plist PATH, with a
                        control that the same call fails without the fix.
- `test_precheck.py`    precheck.py + autogate.py + mergegate.py — the owner's 03:2x directive, built
                        INTO auto-gate. `precheck <item>` gives the executor the adversarial
                        reviewer's reading of its DRAFT diff, once per rework round, and the gate's
                        own Codex call stays COLD (fresh invocation, same prompt, findings written
                        outside the lane worktree). A wall drops the PRE-CHECK, never the gate, and
                        is not retried — the retry belongs to the gate. Also: a REPORT READY without
                        exactly one INTERRUPT-TEST line is not gated and the executor is told in one
                        line what to add (an idle ack is still judged an idle ack, not a missing
                        artefact); and GATE_* lines carry dispatched_at.
- `test_reportedflip.py` dispatcher.py — feed() sets `reported` on the REPORT READY message BEFORE
                        the auto-gate check runs, and both current_item() and report_trigger_ok()
                        excluded `reported`: every report auto-flipped ITSELF out of the match set,
                        so the gate could fire on the first pass and never on a corrected second
                        one. Also: one AUTO_GATE_SKIPPED per message and cause, not one per tick —
                        the tick re-feeds an idle executor every 5s, and a reason nobody can scroll
                        past is not a reason.
- `test_planclassify.py` dispatcher.py — a PLAN filed as a REPORT. PLAN_RE is line-anchored and
                        REPORT_RE matches anywhere, so EXEC-D's "Posting the PLAN block. PLAN READY:"
                        (mid-line, offset 24) lost to the handbook's own "end REPORT READY" at offset
                        1765. No plan review ran, and a plan naming an existing report path would
                        have started a gate on an unbuilt item. Fixture is the REAL message. The
                        anchored rule still outranks position, and the post-compaction guard applies
                        to the new branch too — a control caught that it did not, at first.
- `test_waitongate.py`  dispatcher.py — an executor waiting on its own gate is not stalled. EXEC-F
                        was told to wait (correctly), answered "waiting", and that one-word turn
                        drew AUTO-CONTINUE 1/3, 2/3, 3/3 and then STUCK: three prompts and an
                        escalation for an executor doing what it was told. The continue machinery is
                        for a build that stopped mid-step; an item under gate is not one. The check
                        runs BEFORE the counter is read, and the running mergegate list counts as
                        well as the status.
- `test_packwhole.py`   gatereview2.py — product files are shown WHOLE where the budget allows,
                        cheapest upgrade first: measured at 52% of product files on nine real
                        candidates, with every candidate still inside 180k and the rest keeping the
                        hunks they had. A whole file is LABELLED a full file — a reviewer reading one
                        as a diff reports unchanged lines as findings — and the packed set is logged
                        per gate, which is what separates "the reviewer missed it" from "the reviewer
                        never saw it".
- `test_queueskip.py`   dispatcher.py — why a queued item was passed over. eligible_item() skipped
                        on three paths and only one spoke, so an item sat queued for six minutes and
                        the cause could not be established afterwards at all: the hand-dispatch had
                        cleared blocked_on and nothing was written down. Every skip now says why,
                        once per item and reason. A worktree that does not exist, or one another
                        UNFINISHED item already claims, is refused by name AND escalated — those are
                        errors in the row, and unlike a wait they do not clear themselves. Also pins
                        the PROGRESS_STOP feed gap: FEED_KINDS excludes it, so an executor holding
                        no item is fed there rather than prompted to continue nothing.
- `test_deadexec.py`    dispatcher.py — an executor holding a dispatched item while producing no
                        text, no running tool and no completion for N minutes raises STUCK and wakes
                        BOSS, once. The controls carry as much weight as the must-bite: an
                        escalation that cries wolf on a healthy executor mid-pytest is one BOSS
                        learns to ignore, which costs the real one its signal.
- `test_questiontool.py` dispatcher.py — a question asked through OpenCode's `question` TOOL raises
                        QUESTION instead of reading as BUSY forever, with the options rendered into
                        the event. Also pins the 01:12 REPORT_READY misfire to its MEASURED cause (a
                        compaction summary narrating the marker, not a heading).
- `test_compaction.py`  dispatcher.py — an auto-compaction must not be read as a provider cut; the
                        must-bite that a genuine cut still stalls is asserted in the same file.
- `test_stalerow.py`    dispatcher.py — a pending row about work that already landed is tagged, not
                        pruned; includes the calibration failure that a bare substring match on the
                        msg_id tagged the wrong row.
- `test_clearrow.py`    dispatcherctl.sh `clear` + the daemon side that applies it. Runs the REAL
                        ctl against a temp CN (the script takes CN from the environment for this).
- `test_laneoccupied.py` dispatcher.py — a lane whose earlier item is `reported` or `gated` must not
                        take a second build; `held`/`merged` free it. Fixture is a COPY of the live
                        queue at the 22:45:45 defect. The MUST-PASS carries as much weight as the
                        must-bite: the same lane holds `held` items, and a guard that blocked on
                        those would deadlock the lane while looking like it worked.
- `test_execlabel.py`   dispatcher.py — the status line names the item an executor was last TOLD to
                        work, or admits it is unconfirmed. Message text is DATA: an id is used only
                        if the queue already holds it, and an executor's own claim to have switched
                        does not move the label.
- `test_noturn.py`      dispatcher.py — a session that has begun NO TURN since we prompted
                        it. Keyed on the session clock, not on messages, because a session
                        that emits nothing has no message to read and no kind to branch on.
                        must-bite: a live turn is never death however long the clock has
                        sat, and a session with no time.updated is NOT MEASURED, not well.
- `test_reportheld.py`  dispatcher.py — the three states a REPORT_READY verdict can have. A real
                        report nothing could gate KEEPS its pending row; only the trigger's own
                        `not a report` withdraws as IDLE_ACK; a report arriving while its own
                        gate runs is withdrawn under its own name.
- `test_nullwt.py`      dispatcher.py — a row with no worktree does not fail to check, it checks
                        the daemon's own cwd. No worktree is CANNOT CHECK, never `the report
                        does not exist`; and such a row is refused at dispatch, keyed on the
                        FIELD because a prose grep for `worktree` passes every item file.
- `test_preflight.py`   mergegate.py — run_merge_preflight against real temp git repos.
- `test_portal_row.py`  mergegate.py — portal row: whole suite vs selected files (subprocess stubbed).
- `test_reportclock.py` mergegate.py — the report header time vs the wall clock at REPORT READY
                        (advisory row; `now` is injected so the test does not depend on the clock).
                        The WIRING half drives the real gate over a future-dated report — see
                        "Prove wiring by running it" below.
- `test_yamlparse.py`   mergegate.py — every YAML file in a candidate must parse, against the REAL
                        unparseable ci.yml from r5. Needs PyYAML, which /usr/bin/python3 has and a
                        homebrew python3 may not — the runner pins the interpreter for this reason.
- `test_sweepworktree.py` mergegate.py — a box-free gate sweeps a DETACHED checkout at the candidate
                        sha, cleans it up, and is unaffected by a dirty lane worktree.
- `test_missingvenv.py` mergegate.py — a missing binary is a measurement, not a crash; a lane with no
                        platform/.venv gets a named FAIL row instead of killing the gate. Runs the
                        REAL _run_proofs with the box lock redirected — see "Running the real path".
- `test_codexwall.py`   mergegate.py — the Codex row names WHY there is no review (WALLED with the
                        reset time / EMPTY / unreadable), and the retry is capped at one and used
                        only for the wall. Counts real reviewer calls; the control is that a
                        NON-wall failure is called exactly ONCE.
- `test_gates_listing.py` dispatcherctl.sh `gates`. Drives the REAL script against a temp CN. Its
                        subject is what does NOT appear: an unparseable or unreadable gate file must
                        be NAMED, because silence reads exactly like "that gate never ran".
- `test_gate_survives.py` mergegate.py — drives the REAL main() with the proof path forced to raise
                        and requires the .md AND the GATE_FAIL event to exist anyway.
- `test_gate_crashed.py` mergegate.py — a gate that does NOT finish leaves `<item>.CRASHED.md` and a
                        GATE_CRASHED event, never an `<item>.md`; and a raise inside the proof path
                        is still an ordinary FAIL, so the two layers never both fire.
- `test_agydirs.py`     gatereview2.py — the dirs an item's CI work lives in (deploy/, .circleci/,
                        .github/, scripts/) count as product code to agy. The list is MEASURED from
                        trunk's own history, not dictated.
- `test_agynoreview.py` gatereview2.py — an agy verdict that read no product code renders NO REVIEW,
                        never an approve, and DISAGREE treats it as absent. Also pins the packer
                        ordering: audit/ over budget must still show agy the product diff.
- `test_wallskip.py`    mergegate.py — a still-current wall recorded by the previous gate skips the
                        Codex call (row still FAILs). Both asymmetries err toward MAKING the call,
                        and only the NEWEST .codex.txt decides.
- `test_gate_md.py`     mergegate.py — the gate .md's row rendering, round-tripped BYTE FOR BYTE
                        against a real gate file (fixtures/gate_ci_collection_floor.md).
- `test_ledgertrunk.py` citesweep.py — the ledger half reads TRUNK's EXECUTION_LEDGER, never the
                        candidate's copy, and NAMES which one answered. Fixture is the real ARTIFACT
                        block that produced the false "row is missing" at 23:0x.
- `test_citesweep_zero.py` citesweep.py — a zero names its own causes; the separate path/sha columns.
- `test_runner_census.py` run_all.sh itself — the runner FAILS if a test_*.py in this directory is
                        not run by it. An unwired test is invisible: the suite goes green with a
                        check missing from it. `--census-only` exits after the census so this test
                        can drive the shipped script without running the suite recursively.

- `fixtures/`           COPIES of the live queue.json / pending.json / roster.json taken at
                        2026-09-06 20:37, when the three stale ERROR rows and the nine item-id deps
                        were on the board. They are the recorded shape the fixes were built against.
                        Never point a test at test-logs/driver/ — a test that reads live state
                        passes or fails for reasons that have nothing to do with the code.

## Running the real path without touching the real box

`test_missingvenv.py` sets `MG.LOCK` to a path inside its own temp directory before calling
`_run_proofs`, and stubs `MG.sh` so the census and `git worktree add` answer without running.

THIS IS DELIBERATE — DO NOT "FIX" IT by pointing LOCK back at the real lock or by dropping the stub.
The refusal being tested lives past the box acquisition, so the only ways to reach it are to take
the real box (forbidden: a test must never contend with a live gate for the one scarce resource on
this machine) or to redirect the lock. Redirecting it exercises the shipped code path exactly as
written while the real box stays untouched and free. `MG.CENSUS` is matched by value, not by the
word "census", because it is computed from CN at import time and a test that sets CN afterwards does
not change it — an earlier version of this stub missed and the gate refused at the census instead,
which looked like a pass until the follow-up assertions disagreed.

## Prove wiring by running it, not by reading the source

Measured 2026-09-06 by mutating mergegate.py: THREE of our four `inspect.getsource(<function>)`
assertions were BLIND. Comment the wiring out, leave the original as a trailing comment, and all
three files stayed green — including one that counted `sh(argv` occurrences to prove a retry was
capped at one, which had counted a comment. `getsource` cannot tell live code from a commented-out
corpse, so it is blind to the most common way a behaviour dies: being disabled rather than deleted.
The same assertions were LOUD on two behaviour-preserving refactors (a kwarg, a renamed local).

So: when the claim is "X is wired to Y", RUN it and look at what it produced — the rec() row, the
gate .md, the number of calls. Source-reading is acceptable only for a negative claim about a single
function's body (one such remains, in test_reportclock), where a rename is loud and there is no
wiring to lose. A check that is blind to deletion and loud on a rename is not measuring what its
name says.

test_packfull.py — pack occupancy, and the arithmetic that nearly reported a working packer as
broken. BOSS's first pass over six manifests summed every row INCLUDING the NONE (dropped) entries
and produced "1718% occupancy, 95% non-product"; the corrected figures are median 37%, non-product
24% of sent bytes, 7.0 MB correctly dropped. So occupancy() is ONE function with ONE test rather
than arithmetic repeated per call site. The warning fires at 90% because backfills-r2 sent 99% of
budget on the lane carrying a ruled [high] — one commit from silent omission, with the same coverage
line printed either way. Note the floor-not-round check: `:.0f` printed 99.7% as "100% of budget",
which reads as AT the limit, and that is the one number a reader acts on differently.

test_restartguard.py — a restart must not be the same command as "destroy every in-flight gate and
audit". Three properties: `stop` kills a RESOLVED pid (never `pkill -f`, which matches any shell
that merely mentions the path); `restart` refuses while children are live, names them with their
ages, and needs --force; `status` compares the daemon's start time with dispatcher.py's mtime and
says STALE. That last one exists because a daemon running old code behaves plausibly — the only
symptom on 2026-09-07 was one executor idle for 27 minutes. Note `--dry-run`: without it the
no-children branch could not be exercised without stopping a real daemon, and a guard whose safe
path is untested is half a guard. Note also that the no-pkill check strips COMMENT lines first — it
matched the comment explaining the ban, and that false positive gets "fixed" by deleting the
explanation.

test_migcollide.py — the union check, and the fact that its answer EXPIRES. Trunk holds
249_auxiliary_not_sent_authority.sql; the egress and route-authority lanes hold
249_connection_authority_clock.sql. Different filenames, so git merges them clean and db.py refuses
the duplicate only at runtime — we believed no gate could see it, and run_merge_preflight already
does, on both lanes, today. Two controls keep it meaningful: a number both trees hold with the SAME
filename is not a collision (lane and trunk share 001..N by construction, so a naive intersection
reports everything), and a lane that took the next free number is clean. The last checks are about
provenance: the row names BOTH the lane sha and the trunk sha, and each is compared to the head it
is supposed to name — a regex for ten hex digits passes on the same value printed twice, so a third
check requires the two to differ. Both shas, not trunk's alone, because the verdict is about a PAIR
and the first expiry we actually hit came from the lane side: the egress lane renumbered 249 -> 250
within the hour and the FAIL went stale. A stale FAIL reads exactly like a live one, same as a
stale PASS.

test_prooftimeout.py — a timed-out proof run said "NO test result is implied" while its own log
held 277 passes. On 2026-09-07 B.015a.journey-sole-scheduler hit the 3600s wall at 87% and reported
`NOT RUN ... this is a GATE defect`; the log it had just written said `collected 314 items` with 277
progress characters, all dots. BOSS's counterfactual is why this is a defect and not a tuning knob:
had those dots been `FF`, the row would have been BYTE-IDENTICAL — a real red and a slow green
indistinguishable, in the bad direction. Three states now: failures in the partial output are a FAIL
about the candidate (the case that was invisible), no failures is NOT RUN and a GATE limit that must
never send a lane into rework, and nothing parseable is the old line, true only in that third case.
The wall is 5400s, above the ~4150s this candidate measured.

The parser counts WRAPPED continuation lines: pytest wraps at the terminal width and the
continuation carries no filename, so a filename-anchored count saw 143 of the real log's 277 —
understating the work by nearly half while looking precise. Its control asserts the anchored count
is genuinely lower on the fixture, after a first version of that control counted the `[ 24%]` marker
too and came out larger than the thing it was supposed to bound.

test_siblingmig.py — the collision no gate on any lane could see. On 2026-09-07 migration 250 was
held by THREE lanes at once (egress, erasure, journey) while trunk held no 25x at all: every lane
correctly took next-free, every lane was individually contiguous, and every preflight passed
honestly, because the preflight compares a lane against TRUNK and never against another lane. The
advisory names the other unmerged lanes holding the number, and ADVISORY IS THE DESIGN — a row that
failed here would punish three lanes for each doing the correct thing. The row also carries "DO NOT
RENUMBER ON THIS ROW — ASK BOSS" and the reason, because the failure mode is a helpful executor
renumbering itself and leaving two lanes disagreeing about which is stale. The discriminating
control is 249, which every lane holds under the SAME filename and which must NOT be reported; its
mirror is the lane that took 251 and is reported against nobody.

Two of its mutations scored MISSED and both are recorded here rather than papered over. Removing the
"do not compare the lane with itself" skip changes nothing, and should: a lane compared with itself
holds every number under the same filename, so the same-filename filter already excludes it — the
skip is an optimisation and its MUST-BITE label was deleted rather than justified with an invented
case. Removing `if rc != 0: continue` also scored MISSED, and that one IS load-bearing: `sh()` merges
stderr into its output and git's errors name paths, so `error: unable to read
platform/migrations/250_ghost.sql` parses as migration 250 and a failed sibling read would inject
PHANTOM numbers into the advisory. The control measures the parser directly, because the temp-dir
cases cannot reach it. A MISSED is a question, not a verdict.

test_reportproofs.py — the gate asks the REPORT before it declares NOT DECLARED. A queue row
without `proof_files` is a prediction written before the work; the report is what the lane says it
actually proved (BOSS, 2026-09-07, after shipping three such rows). Two things make it safe rather
than a guess: an EXISTENCE FILTER — a report can name a file that was renamed away, and adopting it
hands pytest a missing path, producing a red about the gate wearing the shape of a red about the
candidate — and DISCLOSURE, a WARN row plus a clause in the proofs detail saying the files came from
the report and not the queue. Controls: the excluded file must really have been named (or the check
passes because the pattern missed it), a report naming no tests must adopt nothing, and with no
proofs from either source the row must still be NOT DECLARED. The fixture holds SIX files because a
two-file one let `sorted(out)` -> `list(out)` score MISSED: order is the pytest argv and the row
text, and set-iteration order is not stable between runs.

test_nullwt.py — `os.path.join(item.get("worktree") or "", rel)` with no worktree is
`os.path.join("", rel)`, which is `rel`, which os.path resolves against the daemon's own cwd. The
report-path check then says "none exist at the lane head" about a lane head it never looked at —
usually False, and True for somebody else's file if the daemon was started inside a checkout. The
reproduction is in the file: chdir into a temp tree holding `audit/report.md` and the old expression
answers True. No worktree means CANNOT CHECK, and feed() records that as UNGATEABLE so the report
stays with BOSS rather than being withdrawn as an idle ack.
The dispatch-time half is keyed on the FIELD and the reason is worth keeping: the obvious version —
refuse unless the item prompt names a checkout — is unbuildable, because the standing "Rules that
bite" boilerplate contains the word `worktree` and sits in 40 of the item files. Any grep for it
passes every row: a guard that cannot fail. `(named in the item prompt)` in the field is the same
intention, checkable, and it is honoured and skipped.

test_reportheld.py — `False` was doing two jobs and a real report vanished under the wrong one.
`_report_verdict[1] = False` meant both "the trigger judged this text is not a report at all" (an idle
ack: withdraw the row, correctly) and "this IS a report and there was nothing to gate it against" (a
finished artifact with nothing pointing at it). The withdrawal keyed on `is False`, so the second wore
the first's clothes: three finished reports were swallowed as IDLE_ACK on 2026-09-07 and each was
found by hand. Three states now, and the middle one is the one that is easy to lose — a report
arriving while its OWN gate runs is still withdrawn, because a verdict is already coming, but it is
recorded as REPORT_WHILE_GATED rather than filed under "there was no report".
Note what the first draft of this file did NOT test. Every check drove withdraw_idle_row with a
verdict handed to it, which proves the guard works when reached and says nothing about whether feed()
ever sets the new value on the real paths: reverting both assignments to `False` scored MISSED. The
feed()-level checks were added for that, and they are the ones that defend the three lost reports.
CAN THE PATH REACH THE GUARD is a different question from does the guard work.

test_noturn.py — the same hole one level deeper. EXEC-M held a dispatched item and the board said
`building` for 11 minutes while the session was dead: the answer was posted at 10:08:12, the session's
`time.updated` never moved off it, and NO ASSISTANT TURN EVER BEGAN. check_stall measures an inert
assistant turn, and there was none to be inert; check_dead would have reached it at 30 minutes. The
executor could not be fed either, for the reason feedidle documents one level up — the tick branches
on a message's KIND and a session that emits nothing has no kind. So this detector reads the SESSION
clock, which our own post bumps: if it has not moved since we prompted, nothing has happened at all.
It is deliberately narrow — it fires only when no assistant turn is newer than the newest user
message — because the false positive that would make it worthless is a 40-minute pytest inside a live
turn, and that control is asserted, not described. The clock comes from the `/session?limit=200`
listing the daemon already fetches, so it costs no new call; a session the listing does not carry is
announced as NOT MEASURED rather than passing silently.

test_feedidle.py — the same bug, in the second location, shadowing the fix for the first. An
executor that stops on a progress line and is handled once could never be fed again: the
PROGRESS_STOP branch that feeds an idle executor sits BELOW `state["handled"][sid] = msg_id`, so it
runs only on the tick that first classifies a message, and every later tick enters the already-handled
guard, which gated on `FEED_KINDS` — a tuple PROGRESS_STOP is not in. EXEC-M sat there for 88 minutes
on 2026-09-07 while four items were queued for it. The comment describing the bug sat one branch
below code that still had it.

So the load-bearing case here is the ALREADY-HANDLED second tick, never a fresh stop: a fresh
PROGRESS_STOP takes the other branch and passes today, which is what made the first fix look
complete. A guard excluded from the proof set is indistinguishable from no guard. Controls: the three
original FEED_KINDS still feed (the fix must not replace the old condition), an executor holding an
item is never fed whatever the kind, an executor waiting on its OWN GATE is not fed at PROGRESS_STOP
(it holds no current item but is not free), the gate check applies only to the new kinds so no
unrequested behaviour change rides along, and PROGRESS_STOP is asserted genuinely absent from
FEED_KINDS — if it were ever added there the first two checks would pass for an unrelated reason.

test_observe.py — two switches, because a kill switch has to be explicable in one sentence. BOSS
reached for STOP on 2026-09-07 to get a routine effect (no new gates while a broken image sat on
disk) and got the emergency one. His ruling was NOT to soften STOP: if STOP is present because the
opencode server is sick, a daemon still polling fifteen sessions is what makes it worse, and "mostly
off" is a state nobody can reason about at hour ten. So STOP is unchanged — everything off,
observation included — and OBSERVE is a second, weaker hold: sensors run, actuators do not. STOP
always wins, and the tick returns before OBSERVE is ever consulted.

The actuators are exactly three — post to a session, launch a gate, spawn codex — and each is driven
through the REAL method with a control showing the same call DOES act when no switch is set, because
a guard that blocked everything always would satisfy every "it did not act" assertion here. The
gate's guard sits above `import autogate`: a held actuator should do none of the work, not stop at
the last step. That test carries a fake `autogate` in sys.modules for one reason — mutation showed
that removing the guard made the test CRASH on the import rather than fail, and a crash is neither a
catch nor a miss.

test_pausedview.py — a paused dispatcher wrote an EMPTY executor map, and an empty map reads as a
quiet board. BOSS paused dispatch on 2026-09-07 to give the 09:18 apply a window and priced the
pause as free because the status view showed everyone busy — the view is fed by the mechanism he
paused. In those 44 minutes two Codex audits finished carrying three [high] findings and raised
nothing, EXEC-M sat idle 57 minutes and EXEC-N 68. Not-observed and nothing-to-observe produced
identical output: the shift's recurring shape, an absence rendering as a fact. The paused view now
carries one row saying the state is UNMEASURED, for how long, what is not happening (no polling, no
auto-continue, no escalation) and where to look instead. It does NOT resume observation while
paused — what a kill switch means is BOSS's decision, not a detail. The control is that the
unpaused view still renders real executor rows, so the banner did not swallow the normal path.

test_worklabel.py — the board announced two re-assignments that never happened, because a rework
that CITES a sibling item outranked the item it was about (longest id first). The rule now: if the
message names the item we dispatched, that is the item; length only breaks ties among the others.
The controls are the risk — a message naming only another item must still win, `…-custody` must
still lose to `…-custody-r3`, and an id the queue does not hold must still be ignored, which is what
stops a message inventing an item. One case exists only because the mutation runner found it: with
`if dispatched in ids` mutated away the suite still passed, since a queue-absent id prepended to the
search order only matters when it is ALSO IN THE TEXT and no case put it there.

test_undeclared.py — the fourth row status. `proofs: FAIL — item declares no proof_files` was
reporting a defect in the ITEM as a defect in the LANE: on 2026-09-07 BOSS read that row on
B.015a and nearly sent a rework over his own missing queue field, while the lane had measured 7/7
green. FAIL means the declared tests ran red (fix the lane); NOT RUN means the box never came free
(re-run it); NOT DECLARED means nobody said what to measure (fix the queue row). NOT DECLARED is not
a pass — it blocks the merge exactly as NOT RUN does — and the verdict WORD stays INCOMPLETE for
both, because autogate, the checkpoint reporter and every grep on events.log key on that word. The
controls are the load-bearing half: a real red must still be FAIL, a clean gate must still be a bare
PASS, and a genuine NOT RUN must still say "did not run" and nothing about a declaration. Two traps
are pinned by name: the regex alternation must try NOT DECLARED before NOT RUN (otherwise "NOT"
matches and " DECLARED" lands in the detail group, so the row parses as neither status), and rec()
keeps THREE positional arguments — adding a `field=` keyword to it broke five test files at once
while the gate itself still worked.

test_mutate.py — mutate.py's own refusals. The runner exists because three harnesses produced
misleading numbers on 2026-09-07: a SyntaxError scoring as "zero reds" (an UNDER-count), an
`except Exception` scoring every crash as a catch (an OVER-count, and the direction that
manufactures confidence), and a guard-removal that threw instead of failing. The load-bearing
assertions here are about what the runner REFUSES TO PRINT — a crash must not produce a count, a
missing marker must not produce a count — so each is paired with a control showing the same command
DOES produce a count when the refusal is not due. Without that pairing, a runner that scored nothing
ever would pass every one of them; it did exactly that on the first run, and the pairing is what
caught it. Two more controls came out of real defects in the runner itself: a bare-`assert` test
prints a traceback and is still a CATCH (classification is by exception TYPE, never by the word
Traceback), and a same-length mutation inside the same second must not vanish into a stale .pyc.
Two facts there, with different scopes: the STALENESS is universal — size + mtime-to-the-second is
CPython's default on every interpreter, reproduced on platform/.venv/bin/python — while the CACHE
LOCATION is not. The venv has pycache_prefix = None, so deleting __pycache__ beside the module
genuinely works there; Apple's /usr/bin/python3, which runs dispatcher/, sets it to
~/Library/Caches/com.apple.python, where a scan beside the module removes nothing and reports
success. State only the second half and someone concludes the venv is safe because the directory is
empty. It fails toward MISSED — a hole that is not there, so we rework a test that was fine — and
the restore has the mirror exposure. Note which rule actually catches it: rule 3. A marker proves
the mutated line ran without knowing that .pyc files exist.

test_boxlock.py — the box lock's dead-owner break. The break is a CONJUNCTION and every clause is
tested through its own refusal, because a conjunction tested only through its true branch has
untested clauses. The clause that matters: a dead token does not prove an idle box. On 2026-09-07
EXEC-G's token named a corpse (pid 95921) while EXEC-G had a LIVE pytest (pid 45246) — breaking on
the dead pid alone would have started a second Postgres cluster on top of a live one, which
corrupts both runs while looking green in neither. An unreadable `ps` therefore counts as BUSY: a
false "busy" costs a wait, a false "idle" costs two clusters. Note the recheck fixture — injecting
the re-acquire at the wrong point passed the break and looked like a code bug when it was a fixture
bug; it has to land during the sleep, where a real one would.

test_portallock.py — one portal suite at a time. BOSS measured 27 gate portal runs on 2026-09-07:
the 24 that ran alone were ALL GREEN and the 3 that overlapped another gate's portal run were ALL
RED — the overlap set and the red set were the same set. His control ran trunk's own green suite
twice concurrently and got 5 and 4 failures, different tests each time, all wait-timeout shaped. The
gate was manufacturing its own reds, and each one costs an executor a rework round on a defect that
does not exist. Three properties here: the lock serialises (and QUEUES rather than failing), a gate
that cannot get it records NOT RUN rather than running anyway, and an overlap that happens despite
the lock is DISCLOSED in the gate file. Note the two controls that keep the lock honest in both
directions — a dead owner's lock IS broken so one crash cannot wedge every later gate, and a LIVE
owner's lock is never stolen however old it looks.

test_answer.py — `dispatcherctl.sh answer`: the reply to an executor's QUESTION and the un-park of
its item are ONE operation. Order is the design and is asserted in both directions — a failed post
must leave the row PARKED, and the row must not move first. Also the file that caught
`dispatcherctl.sh gate` reading a queue path that has never existed: see test_manualgate's
"reads the LIVE queue" control, added the same day.

test_notrun.py — a check that COULD NOT RUN is not a check that FAILED. Drives the real gate end to
end four times (unrun, unrun beside a real failure, autopilot, box-free) and holds BOSS's two rules:
a NOT RUN is never a pass in the header, the event kind, the exit code or a merge; and the verdict
never reads PASS to someone who would take it as proven. Read case E before touching case C: the
autopilot mutation scored ZERO reds until trunk became a separate clone, because TRUNK was the lane
worktree checked out at the lane branch, so a merge could not have been seen either way. Case E is
the control that proves the harness can see a merge at all.

test_checkpoint.py — the checkpoint reporter's two rules: print "not measured" where a source does
not carry a fact (never a blank, which reads as zero), and be re-runnable. Every assertion reads the
GENERATED MARKDOWN rather than a helper's return value, because the document is what BOSS and the
owner actually read. Note the shape of the not-measured checks: `NM in cell` is VACUOUS once NM is
the empty string, so the blank-cell mutation redded one assertion out of four until they were
rewritten to assert the literal words AND a non-empty cell. Hermetic — a temp CN with its own
queue.json, events.log and a real four-commit git repo, so `git log --merges` is really run.
Section 6 (status vs evidence) carries the rule from the top of this file in miniature: every
faulted-row check has a control row beside it that must NOT be faulted, and the `ps`-unreadable case
asserts the report says NOT MEASURED and faults nobody — reporting every gating row as abandoned
because the process list could not be read is the loudest possible form of this report's own failure
mode.

Each file prints PASS/FAIL per assertion and exits non-zero if any failed.
