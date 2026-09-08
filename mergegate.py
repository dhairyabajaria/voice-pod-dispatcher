#!/usr/bin/env python3
"""Mechanical merge gate (owner order 2026-09-05). No model decides anything here.

  mergegate.py <item-id> [--no-box] [--no-codex] [--no-agy] [--autopilot]

Runs four checks on a queue item whose executor reported: (1) the report sha is on the lane
branch; (2) every changed file is inside the item's declared scope globs and outside the global
denylist; (3) the declared proof files pass, targeted, under the box-lock protocol, log head-lined;
(4) Codex adversarial review raises nothing HIGH/CRITICAL/P0/P1. Writes gates/<item>.md, emits
GATE_PASS / GATE_FAIL to events.log, and merges to trunk --no-ff ONLY when AUTOPILOT is on
(file test-logs/driver/AUTOPILOT or --autopilot) — otherwise the merge stays a BOSS action.

It also writes two ADVISORY rows that CANNOT change PASS/FAIL (BOSS, 2026-09-05 16:15):

  agy second opinion  gatereview2.py — an independent agy review of the SAME diff, run in
                      parallel with Codex, output gates/<item>.agy.txt. Fail-OPEN. When the two
                      reviewers disagree the row and the events.log GATE line carry DISAGREE and
                      BOSS reads both before ruling. Off-switch: test-logs/driver/GATEREVIEW2.
  citations           citesweep.py — every NNN:LINE / NNN_name.sql / path:line / path::test in the
                      report and the ledger row, resolved INSIDE the scratch worktree at the
                      candidate sha, output gates/<item>.cites.md.

Both are appended straight to `checks` and never through `rec()`, which is the only thing that
touches `state["ok"]`. That is the whole mechanism keeping them advisory: giving either of them a
rec() call would make an advisory reviewer a veto and break the 16:15 ruling.
"""
import fnmatch
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import traceback
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CN = os.environ.get("CN", "/Users/dhairyabajaria/Claude Code/Calling New")
TRUNK = os.path.join(CN, "voicepod-plan010-rebuild")
D = os.path.join(CN, "test-logs", "driver")
QUEUE = os.path.join(D, "queue.json")
GATES = os.path.join(D, "gates")
EVENTS = os.path.join(D, "events.log")
LOCK = os.path.join(CN, "test-logs", "box.lock.d")
# The portal suite needs its own lock, for a reason that has nothing to do with Postgres.
#
# BOSS measured it on 2026-09-07 across 27 gate portal runs: the 24 that had the box to themselves
# were ALL GREEN, and the 3 whose time ranges overlapped another gate's portal run were ALL RED —
# the overlap set and the red set were the same set, no exceptions either way. His control: trunk's
# whole portal suite alone is 833 tests, exit 0; the SAME tree run twice concurrently produced 5 and
# 4 failures, different tests each time, every one wait-timeout shaped ("Unable to find role=...").
# A known-green tree goes red purely from concurrency.
#
# So the gate was manufacturing its own reds, and each one costs an executor a full rework round on
# a defect that does not exist. Portal proofs were classified box-free, so the daemon's "one gate at
# a time" deferral never applied to them, and we went from 6 to 14 executors tonight.
#
# Fixing the portal suite's concurrency-safety is real work and it belongs to an executor. Not
# running two of them at once is ours, and it is one lock.
# Resolved at CALL time, not import time. tests/test_portal_row.py sets MG.CN to a temp dir AFTER
# import — with an import-time constant it therefore reached the REAL lock, and at 06:18 the
# dispatcher selftest sat blocked behind a live gate's portal run (B.016a.analytics-ui, pid 36227).
# A test suite that can take the production lock is worse than the bug it was testing: it would
# stall every gate on the box for as long as the suite ran.
PORTAL_LOCK = None      # set only by tests that want an explicit path


def portal_lock_dir():
    return PORTAL_LOCK or os.path.join(CN, "test-logs", "portal.lock.d")
PORTAL_WAIT_MAX = 2400          # 40 min. A portal suite is ~2-4 min, so this is a queue, not a stall.
CENSUS = os.path.join(CN, "live_pytest.py")
NODE = "/Users/dhairyabajaria/.local/node-v22.11.0-darwin-arm64/bin/node"
CODEX = os.path.expanduser("~/.claude/plugins/marketplaces/openai-codex/plugins/codex/scripts/codex-companion.mjs")
# Paths every lane may touch regardless of declared scope (reports, logs, resume index, its own ledger rows).
ALWAYS_OK = ["audit/plan-execution-*/**", "plans/resume/**", "plans/EXECUTION_LEDGER.md", "TECHNICAL.md", "docs/**"]
# Never mechanically merged: deploy/secrets/CI, this daemon, and the four foundation files BOSS merges by hand.
DENY = ["deploy/**", ".env*", ".github/**", "dispatcher/**", "platform/tests/conftest.py",
        "platform/testsupport/**", "platform/core/tenancy.py", "platform/core/db.py"]
# Severity scanning: count FINDING MARKERS, never the bare word in prose.
#
# 2026-09-05 18:11, audit-residuals r6: Codex returned `Verdict: approve` with the body "No
# substantive merge blocker found ... No material findings", and the old scan counted 2 blockers —
# it matched `\bBLOCKER\b` inside a NEGATION, twice (once in the prose, once in the JSON echo of
# the same sentence). The gate FAILED a candidate its reviewer had approved, and the same phantom
# count drove a DISAGREE of "0 agy / 2 codex". The old negation-stripper only knew the exact phrase
# "no (material) blockers found", so any other wording of the same all-clear became evidence
# against the candidate. A reviewer saying it found nothing must never read as a finding.
#
# So: strip the all-clear phrasings FIRST, then count only shapes a finding actually takes —
# a bracketed tag, a line-start label, an explicit "Block merge", or "severity: high/critical".
NEGATED = re.compile(
    r"\bno\b[^.\n]{0,60}?\bblockers?\b"                 # "no blocker", "no substantive merge blocker"
    r"|\bno\b[^.\n]{0,60}?\bfindings?\b"                # "No material findings"
    r"|\bblockers?\b\s*[:\-]\s*(?:none|n/?a|0)\b"       # "Blockers: none"
    r"|\b(?:0|zero)\s+blockers?\b"
    r"|\bwithout\s+(?:any\s+)?blockers?\b",
    re.I)
# A finding wears one of these. Bare P0/CRITICAL/BLOCKER in running prose is NOT one of them.
BLOCKERS = re.compile(r"\bBlock merge\b"
                      r"|\[(?:medium|high|critical|blocker|p0|p1)\]"        # [high], [blocker]
                      r"|(?m:^\s*(?:[-*]\s*)?(?:P0|P1|CRITICAL|BLOCKER)\b\s*[:\-])"  # "BLOCKER: ..." as a label
                      r"|severity\W{0,3}(?:high|critical)", re.I)


def severity_hits(text):
    """Finding markers in a review body, with the all-clear phrasings removed first."""
    return BLOCKERS.findall(NEGATED.sub(" ", text or ""))


# The reviewer's usage wall. Its own words, from the 22:08 fixture:
#   "You've hit your usage limit. Visit … to purchase more credits or try again at Sep 9th, 2026 10:40 PM."
# Kept as two independent patterns because the sentence is the vendor's, not ours, and half of it
# changing must not silently turn a WALL back into an anonymous parse error.
WALL_RE = re.compile(r"you'?ve hit your usage limit|usage limit\.? visit", re.I)
WALL_UNTIL_RE = re.compile(r"try again at ([^.\n]+)", re.I)


def parse_wall_date(text):
    """The reviewer's own reset time as a datetime, or None if we cannot read it.

    None means "cannot prove the wall is still up", and every caller must then MAKE THE CALL. The
    vendor writes this string, not us; a format change must cost one wasted call, never a skipped
    review that is recorded as walled.
    """
    t = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", (text or "").strip())
    for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def recorded_wall(now=None):
    """A still-current usage wall recorded by an EARLIER gate. -> (until, gate_name) or None.

    BOSS 2026-09-06 23:1x: while Codex is walled, every gate pays a call (and, since the one-retry
    rule, two) to learn something the previous gate already wrote down. The reset time is in the
    reviewer's own message, so a wall whose date has NOT passed is still up.

    Two deliberate asymmetries, both erring toward MAKING the call:
      - an unparseable or absent date does not skip;
      - a date that has passed does not skip, even by a second.
    The cost of a needless call is seconds. The cost of a wrongly skipped review is a gate that
    reports a reviewer outage while the reviewer was available — and unlike the wall, that is a
    claim nobody can check afterwards.
    """
    now = now or datetime.now()
    try:
        files = [f for f in os.listdir(GATES) if f.endswith(".codex.txt")]
    except OSError:
        return None
    files.sort(key=lambda f: os.path.getmtime(os.path.join(GATES, f)), reverse=True)
    # ONLY THE NEWEST RECORD DECIDES. An earlier version scanned down the list for the first wall
    # it could find, so a wall from Sunday outvoted a SUCCESSFUL review from ten minutes ago — the
    # gate would report "walled" for days after the wall lifted, and the evidence for that claim
    # would be a file the reviewer had already contradicted. The most recent reviewer answer is the
    # only one that describes the reviewer's state now.
    if not files:
        return None
    f = files[0]
    try:
        txt = open(os.path.join(GATES, f), errors="ignore").read(4000)
    except OSError:
        return None
    if not WALL_RE.search(txt):
        return None
    m = WALL_UNTIL_RE.search(txt)
    until = parse_wall_date(m.group(1)) if m else None
    if until and until > now:
        return until, f[: -len(".codex.txt")]
    return None


def codex_failure_kind(rc, out):
    """Name WHY a Codex review produced no verdict. Returns (kind, detail).

    All three of these failed closed already and will keep failing closed — this changes nothing
    about the verdict, only about whether BOSS can tell from the listing which of them happened.
    A wall means "ask again later", a parse error means "the reviewer answered and we could not read
    it", and an empty answer means "nothing came back at all". Collapsing them into one FAIL made
    the row unable to distinguish an outage we must wait out from a defect in our own parsing.
    """
    if WALL_RE.search(out or ""):
        m = WALL_UNTIL_RE.search(out)
        until = m.group(1).strip() if m else "an unstated time (the reset time was not in the message)"
        return "WALLED", f"CODEX WALLED until {until}"
    if not (out or "").strip():
        return "EMPTY", "CODEX RETURNED NOTHING — no output at all (rc=%s)" % rc
    return "", ""


def codex_verdict(out):
    """Fail-CLOSED: only an explicit `verdict: approve` passes.

    2026-09-05: the old rule passed on the ABSENCE of matched severity tokens, so a review reading
    `Verdict: needs-attention` with a `[high]` finding scored severity-hits=0 and the gate printed
    PASS on a merge candidate that then reached trunk. Absence of a token you thought to grep for is
    not evidence of approval. Reading the verdict also survives the companion truncating the body.
    """
    m = re.search(r'"?verdict"?\s*[:=]\s*"?([a-z-]+)', out, re.I)
    verdict = m.group(1).lower() if m else None
    blockers = severity_hits(out)
    return (verdict == "approve" and not blockers), verdict, blockers


# --- provenance: hash each module AS IT IS LOADED, never at write time -------------------------
# Root cause of the 18:16 corruption: the stamp read all three files from disk in the LAST lines of
# the run. Gates 47599 (18:15:43) and 47907 (18:16:21) both executed the 17:40:30 image, an edit
# landed at 18:16:34, and both stamped hashes for code they never ran. A check-then-edit habit
# cannot fix this — any window, however short, is a window, and the editor is not even the same
# process. Recording the hash at LOAD time removes the class: whatever happens to the files
# afterwards, the stamp names the bytes this process actually executed.
# The .md is still written LAST (crash recovery is unchanged); only the measurement moves earlier.
LOADED = {}


def note_loaded(name):
    """Record <name>.py's md5+mtime as it is at import. Never raises: provenance must not fail a gate."""
    if name in LOADED:
        return
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
        LOADED[name] = (hashlib.md5(open(p, "rb").read()).hexdigest()[:8], os.path.getmtime(p))
    except OSError:
        LOADED[name] = ("MISSING!", 0)


note_loaded("mergegate")  # this file is already loaded: hashing it now is exact


def codex_bin():
    """The codex binary, from roster.json's `codex_bin`, falling back to node's own bin dir.

    BOSS, 2026-09-07: a gate launched from BOSS's shell found Codex; the same gate launched by the
    daemon came back "Codex CLI is not installed". Nothing was missing — the companion resolves the
    BARE NAME `codex` from PATH (lib/codex.mjs getCodexAvailability -> binaryAvailable("codex")),
    and launchd's PATH has no node bin directory. So every auto-gate's Codex row was NONE and BOSS
    re-ran each by hand, which is the whole value of auto-gate spent on a missing PATH entry.

    Read from roster.json rather than pinned here: BOSS already keeps `codex_bin` there for the
    daemon's own Codex runs, and one place to change a path beats two that can disagree.
    """
    try:
        v = json.load(open(os.path.join(D, "roster.json"))).get("codex_bin")
    except Exception:
        v = None
    return v or os.path.join(os.path.dirname(NODE), "codex")


def codex_env():
    """os.environ plus the codex/node bin directories on PATH. Never removes anything."""
    env = os.environ.copy()
    parts = env.get("PATH", "/usr/bin:/bin").split(":")
    for d in (os.path.dirname(codex_bin()), os.path.dirname(NODE)):
        if d and d not in parts:
            parts.insert(0, d)
    env["PATH"] = ":".join(parts)
    return env


# --- the primary adversarial reviewer's ROUTE, pinned in the INVOCATION ------------------------
#
# Plan 003 §3 requires the primary gate reviewer to be Astra on an explicit native route, recording
# model, provider, effort and actual session identity. The plugin wrapper cannot do it, and the
# reason is in its own option surface, not in a suspicion:
#
#   codex-companion.mjs handleReviewCommand -> valueOptions ["base","scope","model","cwd"]
#     — `model` is accepted, `effort` IS NOT.
#   its adversarial branch calls runAppServerTurn({prompt, model, sandbox, outputSchema, onProgress})
#     — no effort, while the sibling task path passes `effort: request.effort`.
#   lib/codex.mjs runAppServerTurn sends `model: options.model ?? null, effort: options.effort ?? null`.
#
# So through the wrapper the model can be pinned and the effort cannot. Pinning half of it would let
# a gate record "Astra route" while the effort still came from ~/.codex/config.toml — a guard
# excluded from its own proof set. We therefore call the Codex CLI directly and both values come
# from the invocation.
#
# WHAT THE ROLLOUTS SAY, measured 2026-09-08 rather than inferred: of five sessions under
# ~/.codex/sessions carrying our "Merge gate for" prompt, FOUR ran gpt-6-astra at medium and ONE ran
# gpt-5.6-luna at xhigh. The honest statement about history is that the route was UNPINNED AND
# DRIFTED between at least two model/effort pairs — not that it was always the config default.
# Worse, only 5 of ~146 gate runs left a rollout at all: the wrapper's review path starts its thread
# with `ephemeral: true`, so the other ~141 reviews have no session record and their identity is
# unrecoverable. `codex exec` persists unless asked not to, so from here every review has one.
#
# medium is DECIDED, not open (Plan 003 §3 + BOSS 2026-09-08): preserve an existing explicitly
# configured Astra effort, else pin medium as the documented baseline. The four measured Astra gate
# reviews all ran medium, so medium IS the observed baseline. Do not raise it here.
ASTRA_MODEL = "gpt-6-astra"
ASTRA_EFFORT = "medium"
# The wrapper's own review schema. Reused deliberately: the companion renders THIS structure into
# the markdown every existing parser in this file was built against, so keeping the schema keeps one
# output contract instead of inventing a second one.
REVIEW_SCHEMA = os.path.expanduser(
    "~/.claude/plugins/marketplaces/openai-codex/plugins/codex/schemas/review-output.schema.json")
SESSIONS_ROOT = os.path.expanduser("~/.codex/sessions")


REVIEW_PROMPT = (
    "Adversarial merge-gate review. Review ONLY the changes the candidate commit `{sha}` "
    "introduces on top of `{base}`: run `git diff {base}...{sha}` yourself and read the files it "
    "names. Do not review pre-existing code that this candidate did not touch, and do not review "
    "anything outside that diff — the working tree may hold later work that is not this "
    "candidate.\n\n"
    "{focus}\n\n"
    "Answer with the JSON object required by the output schema and nothing else: `verdict` is "
    "`approve` only if you would merge this as it stands, otherwise `needs-attention`; every "
    "finding carries a severity, the file and the line range you actually read."
)


def codex_review_argv(base, sha, focus, schema, last_msg, model=ASTRA_MODEL, effort=ASTRA_EFFORT):
    """The exact argv for one pinned gate review. Pure, so the pins are testable without a call.

    WHY `exec` AND NOT `exec review`, both measured 2026-09-08 rather than assumed:
      * `codex exec review --base X "<prompt>"` is refused by the CLI itself — "the argument
        '--base <BRANCH>' cannot be used with '[PROMPT]'". Native scoping and our adversarial focus
        are mutually exclusive there.
      * and its `--output-schema` is accepted but NOT honoured: the run returned prose with `[P2]`
        markers and NO `Verdict:` line at all. Fed to this file's fail-closed parser that is a FAIL
        on every candidate, and `[P2]` is not one of the severity markers severity_hits() counts.
    So the scoping moves into the prompt, where the base is named explicitly, and the answer shape
    stays the schema every parser here already reads.

    The cwd is passed by the caller (sh(..., cwd=wt)) rather than -C: Plan 003 §5 warns that parent
    options must precede a subcommand, and not needing the flag beats needing it in the right place.
    """
    return [codex_bin(), "exec",
            "--strict-config",              # an unrecognised config KEY becomes an error, not a
                                            # silently ignored flag: measured 2026-09-08, a run with
                                            # this flag exits 0, so `model_reasoning_effort` is a key
                                            # this CLI version knows. Without it, a renamed setting
                                            # would leave the effort inherited and the row cheerful.
            "-m", model,
            "-c", f"model_reasoning_effort={effort}",
            "-s", "read-only",
            "--output-schema", schema,
            "-o", last_msg,
            "--json",
            REVIEW_PROMPT.format(base=base, sha=sha, focus=focus)]


def session_id_from_jsonl(text):
    """The session/thread id the CLI reported on stdout, or None. NEVER a newest-file guess.

    Plan 003 §5 ("wrong model self-report"): identity comes from metadata tied to the ACTUAL session
    id, "not from report prose or a loose latest-file search". A run whose id we cannot read is
    NOT MEASURED — which is a row BOSS can act on, unlike a plausible id belonging to somebody
    else's session.

    Shapes are accepted defensively because the event names belong to the CLI, not to us, and a
    version bump that renames them must degrade to NOT MEASURED rather than to a wrong id.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        pay = d.get("payload") if isinstance(d.get("payload"), dict) else d
        for key in ("session_id", "thread_id", "conversation_id"):
            v = pay.get(key) or d.get(key)
            if isinstance(v, str) and len(v) >= 8:
                return v
        if str(d.get("type") or "").split(".")[0] in ("thread", "session") and isinstance(pay.get("id"), str):
            return pay["id"]
    return None


def rollout_for_session(session_id, root=SESSIONS_ROOT):
    """The rollout file for THIS session id. -> (path, "") or (None, why).

    Refuses on 0 and on more than 1, for the same reason the lane resolver does: a review whose
    provenance we cannot pin to exactly one file has no provenance, and picking one of two is how a
    record acquires somebody else's identity.
    """
    if not session_id:
        return None, (
            "the CLI reported NO SESSION ID on stdout. What was looked for, so the next reader has a "
            "thread to pull rather than a blocked merge: a JSONL line from `codex exec --json` "
            "carrying `thread_id`, `session_id` or `conversation_id` — measured 2026-09-08 on cli "
            "0.153.2, the first line is `{\"type\": \"thread.started\", \"thread_id\": \"...\"}`. "
            "If the CLI renamed that event, this is a one-line fix in session_id_from_jsonl(); if it "
            "printed nothing, the run itself did not start")
    hits = sorted(glob.glob(os.path.join(root, "*", "*", "*", f"*{session_id}*.jsonl")))
    if not hits:
        return None, (f"no rollout file under {root} carries session id {session_id} — the run may "
                      f"have been ephemeral, in which case its identity is unrecoverable")
    if len(hits) > 1:
        return None, (f"{len(hits)} rollout files carry session id {session_id}; refusing to choose "
                      f"between them: {', '.join(os.path.basename(h) for h in hits[:3])}")
    return hits[0], ""


def provenance_from_rollout(path):
    """model / provider / effort / cli_version / session id, read from the rollout. -> dict.

    Measured field locations (2026-09-08, cli 0.153.4): `session_meta.payload` carries session_id,
    model_provider and cli_version but NOT the model; the model and effort are on `turn_context`.
    Both are read, and anything missing stays absent rather than being filled in with a default —
    a provenance record that quietly supplies the value it failed to find is worse than none.
    """
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                pay = d.get("payload") or {}
                if d.get("type") == "session_meta":
                    for src, dst in (("session_id", "session_id"), ("model_provider", "provider"),
                                     ("cli_version", "cli_version"), ("cwd", "cwd")):
                        if pay.get(src) and dst not in out:
                            out[dst] = pay[src]
                elif d.get("type") == "turn_context":
                    for src, dst in (("model", "model"), ("effort", "effort")):
                        if pay.get(src) and dst not in out:
                            out[dst] = pay[src]
                if {"model", "effort", "provider", "session_id"} <= set(out):
                    break
    except OSError as e:
        out["read_error"] = f"{type(e).__name__}: {e}"
    return out


def run_route_provenance_row(it, rec, warn):
    """The route the reap read back, carried onto the gate. Records at most one row.

    The gate CANNOT re-derive this: by the time it runs the daemon's run state is gone and the
    rollout window has passed, so provenance is whatever the reap wrote on the item.

    BOSS, 2026-09-08: REFUSED (the provider declined) and NOT MEASURED (we failed to observe) are
    facts about different subjects, and conflating them cost the first real Muse result — a sweep
    that succeeded outright was recorded broken because the CLI printed no session id. The same
    split applies here, one level up:

        FAIL      the session records a route nobody asked for   -> the candidate is wrong
        NOT RUN   we could not establish what ran                -> our instrument, not the work
        PASS      the session's own metadata matches the intent

    NOT RUN blocks the merge exactly as any unrun check does — an attempt whose route is unknown
    cannot be cited as evidence for the route it intended — while never calling the work bad.
    """
    if it.get("route_mismatch"):
        rec("route verified", False, str(it["route_mismatch"]))
    elif it.get("route_unverified"):
        rec("route verified", None, "the daemon could not establish what actually ran, so there is "
                                    "no provenance to gate on: " + str(it["route_unverified"]))
    elif it.get("route_verified"):
        rec("route verified", True, "the reap read the route back from the session's own metadata")
    elif str(it.get("dispatched_to", "")).startswith("CODEX") or it.get("executor") == "CODEX":
        # Deliberately advisory and NOT gating: rows dispatched before the reap recorded provenance
        # carry no field at all, and turning every one of them INCOMPLETE would change the verdict
        # of work that predates the check. Visible and named, rather than silent.
        warn("route verified: NO ROW — this item carries none of route_verified/route_unverified/"
             "route_mismatch, so it was dispatched before the reap recorded route provenance. "
             "Advisory only; re-dispatch under the current daemon to gate on it.")


def route_row(prov, why="", model=ASTRA_MODEL, effort=ASTRA_EFFORT):
    """Is this review's identity what we pinned? -> (True | False | None, text).

    MEASURED END TO END, 2026-09-08, cli 0.153.2. A `codex exec` run writes a `turn_context` event
    carrying `model` and `effort`, and a `session_meta` event carrying session_id, model_provider,
    cli_version and cwd. So Plan 003 §5's requirement — identity from session metadata tied to the
    actual session id — IS satisfiable, and both pins are demonstrably honoured: a probe run with
    `-m gpt-5.6-luna -c model_reasoning_effort=medium` recorded luna/MEDIUM, while the config default
    is low. The flags reach the session, and the session says so.

    One correction to my own first reading, because it was in this docstring as a measured fact and
    it was wrong: I concluded that exec sessions carry no turn_context at all. They do. The rollout
    I had checked came from `codex exec review`, the SUBCOMMAND, which records none — I attributed a
    subcommand's gap to exec in general after looking at exactly one file. The fallback branch below
    survives that correction on its own merits: a CLI version that stops recording the model must
    degrade to a stated limitation, never to a confident label.

    `model_context_window` is NOT a substitute discriminator and was checked before being trusted:
    gpt-6-astra and gpt-5.6-luna both report 258400.

    Three outcomes. None (INCOMPLETE) is for a run we cannot tie to a session at all — the ~141
    ephemeral wrapper reviews' situation, which must never read as a pass. False is route DRIFT and
    is loud. True is a pinned, identified run.
    """
    if not prov or not prov.get("session_id"):
        return None, (
            f"route NOT MEASURED — {why or 'no session metadata was found for this run'}. "
            f"The review may be fine; nothing ties it to a session, so its identity is unverified, "
            f"and an unverified identity is not an Astra review. This blocks the merge ON PURPOSE: "
            f"the alternative is what we found on 2026-09-08, ~141 gate reviews whose identity is "
            f"unrecoverable and every one of them reading as a pass. Where to look, in order: the "
            f"`thread.started` line on the CLI's stdout (session_id_from_jsonl), then a rollout file "
            f"named after that id under {SESSIONS_ROOT} (rollout_for_session), then `session_meta` "
            f"and `turn_context` inside it (provenance_from_rollout).")
    got_m, got_e = prov.get("model"), prov.get("effort")
    ident = (f"session={prov['session_id']} provider={prov.get('provider', 'unrecorded')} "
             f"cli={prov.get('cli_version', 'unrecorded')}")
    if got_m or got_e:
        if got_m != model or got_e != effort:
            return False, (f"ROUTE DRIFT — pinned {model}/{effort} in the invocation, the session "
                           f"metadata records model={got_m} effort={got_e}. {ident}")
        return True, f"model={got_m} effort={got_e} (from session metadata) {ident}"
    # No model/effort in the metadata: a CLI change, not a normal run. Say exactly that.
    return True, (f"model={model} effort={effort} PINNED IN THE INVOCATION and accepted under "
                  f"--strict-config, but NOT confirmed from metadata — this run's session records "
                  f"no model or effort, which is a change from cli 0.153.2 and worth looking at. "
                  f"{ident}")


def render_review(last_message):
    """The schema's JSON rendered into the markdown shape every parser here already reads.

    The companion did this rendering and we are no longer calling the companion. Doing it ourselves
    keeps ONE output contract: `Verdict: <v>` and `- [severity] title (file:lines)`, which is what
    codex_verdict() and severity_hits() were built against and what the stored .codex.txt files look
    like. Feeding raw JSON to those parsers instead would keep the verdict working and silently
    score every finding at zero, because `"severity": "high"` does not match the bracketed marker —
    a review that reported four [high] findings would have printed `blockers=0`.

    Unparseable input is returned VERBATIM: the fail-closed verdict check must see whatever actually
    came back, not an empty string that hides it.
    """
    txt = (last_message or "").strip()
    try:
        d = json.loads(txt)
        assert isinstance(d, dict) and "verdict" in d
    except Exception:  # noqa: BLE001 — any unparseable answer is passed through untouched
        return last_message or ""
    lines = ["# Codex Adversarial Review", "", f"Verdict: {d.get('verdict')}", ""]
    if d.get("summary"):
        lines += [str(d["summary"]), ""]
    findings = d.get("findings") or []
    lines.append("Findings:" if findings else "Findings: none reported.")
    for f in findings:
        if not isinstance(f, dict):
            continue
        where = f.get("file") or "?"
        if f.get("line_start"):
            where += f":{f['line_start']}" + (f"-{f['line_end']}" if f.get("line_end") else "")
        lines.append(f"- [{f.get('severity', 'unspecified')}] {f.get('title', '(no title)')} ({where})")
        for extra in ("body", "recommendation"):
            if f.get(extra):
                lines.append(f"  {f[extra]}")
    if d.get("next_steps"):
        lines += ["", "Next steps:"] + [f"- {s}" for s in d["next_steps"]]
    return "\n".join(lines) + "\n"


def frozen_checkout(wt, sha, tag, deps=()):
    """A detached worktree at the PINNED candidate sha. -> (path, "") or (None, why).

    Plan 003 §4 A2: "Python, portal and reviewers inspect the pinned candidate/base, not a moving
    worktree." The Python proofs already did (they build their own scratch checkout at `sha`, and
    the reason is on that code: the daemon dispatches the lane's NEXT item into the lane worktree,
    so it is dirty for the whole of the next build). The reviewer and the portal proofs did not —
    both were handed `wt`, the live lane worktree.

    A failure here RETURNS A REASON and never falls back silently: reviewing the wrong tree while
    reporting the right sha is the exact confusion this exists to remove.
    """
    swt = os.path.join(CN, f"voicepod-{tag}-{os.path.basename(wt)}")
    sh(["git", "-C", wt, "worktree", "remove", "--force", swt], timeout=180)
    rc, out = sh(["git", "-C", wt, "worktree", "add", "--detach", swt, sha], timeout=300)
    if rc != 0:
        return None, f"could not build a detached checkout at {sha[:10]}: {out.strip()[-160:]}"
    for rel in deps:
        src, dst = os.path.join(wt, rel), os.path.join(swt, rel)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.symlink(os.path.realpath(src), dst)
            except OSError:
                pass          # a missing dep is the runner's problem to report, not this helper's
    return swt, ""


def drop_checkout(wt, path):
    """Remove a frozen checkout. Never raises: cleanup must not turn a verdict into a crash."""
    if path:
        sh(["git", "-C", wt, "worktree", "remove", "--force", path], timeout=180)


def dispatched_at_of(item_id):
    """The item's dispatched_at, read fresh. Used by the crash path, which has no `it` in hand."""
    try:
        for i in json.load(open(QUEUE)).get("items", []):
            if i.get("id") == item_id:
                return i.get("dispatched_at") or "-"
    except Exception:
        pass
    return "-"


def codex_precheck():
    """(rc, out) when Codex cannot possibly run, else None. Module level so it can be TESTED.

    "Codex CLI is not installed. Install it with `npm install -g @openai/codex`" sent BOSS looking
    for a missing package when the binary was on disk and only the PATH was wrong. A gate that
    cannot review must say WHICH path it looked at.
    """
    b = codex_bin()
    if not os.path.exists(b):
        return 127, f"CODEX BINARY NOT FOUND at {b} (roster.json codex_bin) — nothing was run"
    return None


def sh(cmd, cwd=None, timeout=600, env=None):
    """Run a command. A MISSING BINARY IS A RESULT, NOT A CRASH.

    2026-09-06 21:36, gate 66276: the lane worktree had no `platform/.venv`, so the provenance probe
    exec'd an interpreter that did not exist, subprocess raised FileNotFoundError, and it propagated
    out of run_proofs and out of main — killing the gate after the box, the reviewers and the sweep
    had all been paid for, and BEFORE render_gate_md(). rc=1 from an uncaught exception is not a
    verdict, and there was no gate file at all for BOSS to read. Every caller here already handles a
    non-zero rc; none of them can handle an exception. 127 is the shell's own "command not found".
    """
    try:
        # stdin=DEVNULL, added 2026-09-08. Measured: `codex exec` with a prompt ARGUMENT still
        # prints "Reading additional input from stdin..." — Plan 003 §5's third pilot finding, now
        # in our own reviewer call. subprocess.run INHERITS stdin, so a gate started from a terminal
        # hands the CLI a live tty and it can block forever on input nobody will type. Every command
        # this runner launches is non-interactive; a closed stdin makes that explicit rather than
        # depending on how the gate happened to be started.
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except OSError as e:
        return 127, f"{type(e).__name__}: {e}"


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def emit(kind, item, detail):
    with open(EVENTS, "a") as f:
        f.write("\t".join([now(), kind, "GATE", "-", item, detail]) + "\n")


def matches(path, globs):
    for g in globs:
        if fnmatch.fnmatch(path, g):
            return True
        if g.endswith("/**") and path.startswith(g[:-3] + "/"):
            return True
    return False


def lock_owner():
    try:
        return open(os.path.join(LOCK, "owner")).read().strip()
    except OSError:
        return None


BOX_STALE_S = 1800        # a lock younger than this is never examined, however dead it looks
BOX_RECHECK_S = 10        # the gap across which the owner token must not change


def pytest_pids():
    """Real pytest processes on this box. -> (pids, evidence, sure).

    TWO STAGES, and both exist because BOSS hit the one-stage versions in both directions on
    2026-09-07:

      * Grepping the full command line alone returned SIX when exactly ONE pytest was running. The
        other five were Codex processes whose PROMPT TEXT quotes `./.venv/bin/python -m pytest` —
        the handoff instructions we give every auditor contain that string, and the whole prompt
        sits in argv. So the count is non-zero whenever any Codex slot is busy, which is nearly
        always: the lock would never be released. Wasteful, not harmful.
      * Filtering on `ps -eo comm` instead: in the MULTI-COLUMN form macOS truncates comm to 16
        chars, so a live run reads `./.venv/bin/pyth` and a regex anchored on `python$` misses it.
        That predicate returns 0 while a pytest is alive — it breaks a legitimately held lock and
        starts a second Postgres cluster on top of a live one. Dangerous, and it is exactly what
        you reach for after being burned by the first.

    So: find CANDIDATES by command line, then resolve each candidate's own comm with `ps -p <pid>
    -o comm=`, which as the ONLY requested field comes back untruncated. Codex resolves to
    comm=codex and drops out; the wrapper shell resolves to /bin/zsh and drops out. Measured on the
    live board: 6 candidates, 1 real.

    The general shape is not about ps at all: OUR OWN INSTRUCTION TEXT IS NOW LONG ENOUGH AND
    SPECIFIC ENOUGH TO MATCH OUR OWN MONITORING GREPS. Any check that greps argv for a command we
    also quote in a prompt will match the prompt.

    `sure` is False when anything could not be resolved — the caller must treat that as BUSY.
    """
    rc, out = sh(["ps", "-eo", "pid=,args="])
    if rc != 0:
        return [], "`ps` could not be read", False
    cands = [l.strip().split()[0] for l in out.splitlines()
             if re.search(r"(-m\s+pytest\b|/bin/pytest\b)", l) and " grep " not in l]
    real, unresolved = [], 0
    for pid in cands:
        rc2, comm = sh(["ps", "-p", pid, "-o", "comm="])
        comm = comm.strip()
        if rc2 != 0 and comm == "":
            continue            # the process exited between the two stages: genuinely gone
        if not comm:
            unresolved += 1     # readable but empty: unknown, and unknown counts as busy
            continue
        base = os.path.basename(comm)
        if re.match(r"^python[0-9.]*$", base) or base == "pytest":
            real.append((pid, comm))
    ev = (f"{len(cands)} argv candidate(s), {len(real)} real"
          + (f", {unresolved} unresolved" if unresolved else "")
          + (": " + ", ".join(f"pid={p} comm={c}" for p, c in real[:3]) if real else ""))
    return [p for p, _c in real], ev, unresolved == 0


def any_pytest_running():
    """(busy, evidence). Unknown is BUSY: a false 'busy' costs a wait, a false 'idle' costs two
    clusters on one data directory."""
    pids, ev, sure = pytest_pids()
    if not sure:
        return True, ev + " — could not resolve every candidate, treated as BUSY"
    return bool(pids), ev


def break_dead_box_lock():
    """Break the box lock ONLY under the full conjunction. -> (broken, why).

    BOSS's condition, and every clause earns its place:
      * the owner token names a pid and that pid is DEAD (an unnamed pid is not a dead one);
      * the lock is older than BOX_STALE_S (a young lock is somebody mid-setup);
      * NO pytest is running anywhere on the box (the token is not the box — see any_pytest_running);
      * the owner token is UNCHANGED across a BOX_RECHECK_S gap, so a holder that re-acquires
        properly in the meantime is left alone. This is the clause that makes the break safe to
        automate: it stands down rather than racing.
    The gate was already emitting the evidence that the holder was dead every ten minutes — the
    owner string carries `pid=` — and then waiting for it anyway, for up to an hour.
    """
    owner, age = lock_owner(), 0
    try:
        age = time.time() - os.path.getmtime(os.path.join(LOCK, "owner"))
    except OSError:
        return False, "no owner file — nothing to break"
    if not owner:
        return False, "no owner text — refusing to break a lock nobody claims"
    m = re.search(r"pid=(\d+)", owner)
    pid = m.group(1) if m else "unnamed"
    if not m:
        return False, f"owner names no pid ({owner[:60]}) — unknown is not dead"
    if age < BOX_STALE_S:
        return False, f"lock is only {int(age)}s old (< {BOX_STALE_S}s)"
    if portal_pid_alive(owner):
        return False, f"owner pid {pid} is ALIVE"
    busy, ev = any_pytest_running()
    if busy:
        return False, (f"owner={owner[:70]} pid={pid} DEAD age={int(age)}s pytest=[{ev}] — a dead "
                       f"token does not prove an idle box, and a second cluster on top of a live "
                       f"one corrupts both runs. NOT BROKEN.")
    time.sleep(BOX_RECHECK_S)
    if lock_owner() != owner:
        return False, "the owner token CHANGED during the recheck — somebody re-acquired properly"
    busy2, ev2 = any_pytest_running()
    if busy2:
        return False, f"owner={owner[:70]} pid={pid} DEAD — a pytest appeared during the recheck ({ev2})"
    if lock_owner() != owner:
        # Read ONCE MORE immediately before the removal. The window between the recheck and the
        # unlink is small, but it is the window in which the removal actually happens, and a test
        # that injected a re-acquire there found nothing looking. Cheap; closes it.
        return False, "the owner token changed between the recheck and the removal"
    try:
        os.remove(os.path.join(LOCK, "owner"))
        os.rmdir(LOCK)
    except OSError as e:
        return False, f"could not remove the lock: {e}"
    return True, (f"owner={owner} pid={pid} DEAD age={int(age)}s pytest=[{ev2}] token unchanged "
                  f"across {BOX_RECHECK_S}s — BROKEN. Inputs are logged rather than the verdict "
                  f"alone: if this call is ever wrong, the row says which way it went.")


def release_if_mine(token):
    """Never a bare recursive delete: remove our own owner file, then the empty directory."""
    if lock_owner() == token:
        os.remove(os.path.join(LOCK, "owner"))
        os.rmdir(LOCK)


def portal_lock_holder():
    """(owner-text, age-seconds) for the portal lock, or (None, 0)."""
    try:
        txt = open(os.path.join(portal_lock_dir(), "owner")).read().strip()
        return txt, time.time() - os.path.getmtime(os.path.join(portal_lock_dir(), "owner"))
    except OSError:
        return None, 0


def portal_pid_alive(owner):
    """Is the pid named in an owner string still running? Unknown -> True (never break on a guess)."""
    m = re.search(r"pid=(\d+)", owner or "")
    if not m:
        return True
    try:
        os.kill(int(m.group(1)), 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def acquire_portal_lock(token, wait_max=None, sleep=5):
    """Serialise portal suites across gates. -> (True, waited) or (False, why).

    Deliberately the same shape as the box lock: mkdir is the atomic operation, the owner file names
    who holds it, and nothing here ever deletes a directory it does not own. A lock whose owner
    PROCESS IS GONE and which is older than 30 min is broken and SAID to be broken — a gate crash
    must not wedge every later portal run, but a live 20-minute suite must not be stolen either.
    """
    wait_max = PORTAL_WAIT_MAX if wait_max is None else wait_max
    t0 = time.time()
    while True:
        try:
            os.mkdir(portal_lock_dir())
            open(os.path.join(portal_lock_dir(), "owner"), "w").write(token)
            return True, time.time() - t0
        except FileExistsError:
            owner, age = portal_lock_holder()
            if owner and age > 1800 and not portal_pid_alive(owner):
                try:
                    os.remove(os.path.join(portal_lock_dir(), "owner"))
                    os.rmdir(portal_lock_dir())
                    print(f"broke a dead portal lock held by {owner} for {int(age)}s", file=sys.stderr)
                    continue
                except OSError:
                    pass
            if time.time() - t0 > wait_max:
                return False, (f"another gate has held the portal lock for {int(age)}s "
                               f"({owner or 'owner unknown'}) and this gate waited "
                               f"{int(time.time() - t0)}s")
            time.sleep(sleep)


def release_portal_lock(token):
    if portal_lock_holder()[0] == token:
        os.remove(os.path.join(portal_lock_dir(), "owner"))
        os.rmdir(portal_lock_dir())


PORTAL_LOG_RE = re.compile(r"^(\d{8}-\d{4})-gate-(.+)-portal\.log$")


def overlapping_portal_runs(this_log, start_ts, end_ts):
    """Other gate portal runs whose [start, end] overlaps ours. -> list of "item (HH:MM-HH:MM)".

    The lock above should make this list empty forever. It is measured anyway, and printed when it
    is not, because a lock can be broken as dead, bypassed by a hand-run gate, or simply not held on
    some path nobody thought about — and a red produced by our own scheduling must never read the
    same as a red in the executor's code. Start comes from the filename (minute granularity, so it
    can round UP by up to 59 s) and end from the mtime, which is the same reconstruction BOSS did by
    hand; it stops working if logs are rotated, which is why the gate row now carries its own
    start/end as well.
    """
    out = []
    d = os.path.join(CN, "test-logs")
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        m = PORTAL_LOG_RE.match(name)
        if not m or name == os.path.basename(this_log):
            continue
        path = os.path.join(d, name)
        try:
            other_start = datetime.strptime(m.group(1), "%Y%m%d-%H%M").timestamp()
            other_end = os.path.getmtime(path)
        except (ValueError, OSError):
            continue
        if other_start <= end_ts and start_ts <= other_end:
            out.append(f"{m.group(2)} ({datetime.fromtimestamp(other_start):%H:%M}-"
                       f"{datetime.fromtimestamp(other_end):%H:%M})")
    return sorted(out)


def node_env():
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(NODE) + ":" + env.get("PATH", "/usr/bin:/bin")  # launchd PATH has no node/npx
    return env


def run_portal_proofs(files, wt, item_id, rec, whole=False, warn=None):
    """Portal proofs are BOX-FREE (vitest, no Postgres) — vitest plus one typecheck.

    Added 2026-09-05 after BOSS found a portal-only item (010.gap-answer-portal-guard) whose .tsx proof
    was being handed to pytest. node_modules is symlinked to the canonical checkout exactly as trunk does.

    WHOLE SUITE when the candidate touches portal/** (BOSS, 2026-09-06, gate hole from the 01:10
    finding). This row used to run only the item's named .tsx proofs — 25 selected tests — so 011's
    and 014a's merges both passed while `src/lib/permissions.test.ts` was red on trunk: the portal
    role table was missing `approval:decide` and the five `asset.*` permissions that auth.py grants,
    i.e. paid features absent from customer navigation, and no gate looked. The suite is ~1.5 s per
    file and takes nothing scarce, so selecting files here bought nothing and cost a merge.
    """
    portal = os.path.join(wt, "portal")
    if not os.path.isdir(portal):
        rec("portal proofs", False, "no portal/ in this worktree")
        return
    nm = os.path.join(portal, "node_modules")
    if not os.path.exists(nm):
        try:
            os.symlink(os.path.join(CN, "voice-pod", "portal", "node_modules"), nm)
        except OSError as e:
            rec("portal proofs", False, f"cannot link node_modules: {e}")
            return
    rel = [f[len("portal/"):] if f.startswith("portal/") else f for f in files]
    sel = [] if whole else rel
    what = "WHOLE SUITE" if whole else f"selected {rel}"
    # One portal suite at a time, box-free or not. Waiting is the whole point: a queued gate is
    # slower, a concurrent one is WRONG, and a wrong red costs an executor a round.
    token = f"portal-{item_id}-pid={os.getpid()}-{int(time.time())}"
    got, info = acquire_portal_lock(token)
    if not got:
        # NOT RUN, never FAIL — the same ruling as a reviewer outage (2026-09-07). Running it anyway
        # would produce exactly the manufactured red this lock exists to stop.
        rec("portal proofs", None, f"NOT RUN — {info}. Running it concurrently is what produced "
                                   f"three false reds tonight, so the gate declines to.")
        return
    waited = info
    start_ts = time.time()
    log = os.path.join(CN, "test-logs", f"{datetime.now():%Y%m%d-%H%M}-gate-{item_id}-portal.log")
    try:
        with open(log, "w") as f:
            f.write(f"portal proofs {what} cwd={portal} start={now()} "
                    f"waited_for_portal_lock={int(waited)}s\n")
        with open(log, "a") as f:
            r1 = subprocess.run(["npx", "vitest", "run", *sel], cwd=portal, stdout=f,
                                stderr=subprocess.STDOUT, timeout=1800, env=node_env())
            f.write("\n--- npm run typecheck ---\n")
            f.flush()
            r2 = subprocess.run(["npm", "run", "typecheck"], cwd=portal, stdout=f,
                                stderr=subprocess.STDOUT, timeout=1800, env=node_env())
    finally:
        release_portal_lock(token)
    end_ts = time.time()
    out = open(log, errors="ignore").read()
    summ = [l.strip() for l in out.split("\n") if re.search(r"Tests\s+\d+|Test Files\s+\d+", l)]
    # The times ride in the ROW, not only in the log filename and its mtime: BOSS had to
    # reconstruct tonight's timeline from those two, which stops working the moment logs rotate.
    timing = (f"start={datetime.fromtimestamp(start_ts):%H:%M:%S} "
              f"end={datetime.fromtimestamp(end_ts):%H:%M:%S} "
              f"({int(end_ts - start_ts)}s, queued {int(waited)}s behind another portal run)")
    rec("portal proofs", r1.returncode == 0 and r2.returncode == 0,
        f"{what} — vitest rc={r1.returncode} typecheck rc={r2.returncode} "
        f"{' | '.join(summ[-2:])} {timing} log={log}")
    overlap = overlapping_portal_runs(log, start_ts, end_ts)
    if overlap and warn:
        warn(f"portal proofs CONCURRENCY: this suite overlapped {len(overlap)} other gate portal "
             f"run(s) — {', '.join(overlap[:4])}. Measured 2026-09-07: every overlapping portal run "
             f"went red and every solo one went green, on the same trees. **A red in this row may be "
             f"ours, not the candidate's** — re-run it alone before believing it. The portal lock "
             f"should make this impossible, so its appearance is itself a defect to chase.")


# A minute-granularity header can legitimately round UP by up to 59 s, so 60 s is the smallest
# threshold that cannot fire on rounding alone. EXEC-F's 2026-09-06 header said 21:00 IST at a wall
# clock of 20:58:52 — 68 s ahead, which no rounding explains.
CLOCK_TOLERANCE_S = 60


def report_clock_row(report_name, txt, warn, now=None):
    """ADVISORY: is the report's header time ahead of the wall clock at REPORT READY?

    A header written in the future is a small thing that quietly poisons big ones: it is the
    timestamp BOSS reads when reconstructing what happened in what order, and every ordering
    argument built on it inherits the error. Advisory by contract — it goes through warn(), never
    rec(), so it cannot change PASS/FAIL (BOSS, 2026-09-06).

    An unparseable header is REPORTED, not skipped. A check that stays silent when it could not
    read its input is indistinguishable from a check that read it and found nothing wrong.
    """
    now = now or datetime.now()
    head = "\n".join((txt or "").split("\n")[:30])
    cands = []
    for m in re.finditer(r"(\d{4}-\d{2}-\d{2})[ T]+(\d{1,2}):(\d{2})", head):
        d = datetime.strptime(m.group(1), "%Y-%m-%d")
        cands.append((d.replace(hour=int(m.group(2)) % 24, minute=int(m.group(3))), m.group(0)))
    for m in re.finditer(r"(?<![\d:])(\d{1,2}):(\d{2})\s*(?:IST|hrs)?\b", head):
        cands.append((now.replace(hour=int(m.group(1)) % 24, minute=int(m.group(2)),
                                  second=0, microsecond=0), m.group(0).strip()))
    for m in re.finditer(r"(?<![\d-])(\d{4}-\d{2}-\d{2})(?![ T]+\d)", head):
        d = datetime.strptime(m.group(1), "%Y-%m-%d")
        cands.append((d, m.group(1)))       # date-only header: only a FUTURE DATE is a finding
    if not cands:
        warn(f"report clock: NOT CHECKED — no timestamp found in the first 30 lines of "
             f"{report_name}. This row is not a pass; the header could not be read.")
        return None
    when, litr = max(cands, key=lambda c: c[0])
    ahead = (when - now).total_seconds()
    if ahead > CLOCK_TOLERANCE_S:
        warn(f"report clock AHEAD OF THE WALL CLOCK: {report_name} header says {litr!r} "
             f"({when:%Y-%m-%d %H:%M}) but the gate read it at {now:%Y-%m-%d %H:%M:%S} — "
             f"{int(ahead)}s in the future. Every ordering argument that quotes this header "
             f"inherits the error; have the executor rewrite the header from `date`. ADVISORY.")
    else:
        warn(f"report clock: {litr!r} vs gate {now:%H:%M:%S} — consistent "
             f"({int(ahead)}s ahead, inside the {CLOCK_TOLERANCE_S}s minute-rounding tolerance)."
             if ahead > 0 else
             f"report clock: {litr!r} vs gate {now:%H:%M:%S} — consistent ({int(-ahead)}s behind).")
    return ahead


MIG_DIR = "platform/db/migrations"
MIG_RE = re.compile(r"(?:^|/)((\d{3,4})_[^/]+)$")


PROGRESS_HEAD_RE = re.compile(r"^(?:platform/)?tests/\S+\.py\s+(.*)$")
PROGRESS_TAIL_RE = re.compile(r"^[.FEsxXu\s]+$")
PCT_RE = re.compile(r"\[\s*\d+%\]\s*$")
COLLECTED_RE = re.compile(r"^collected (\d+) items", re.M)
PROOF_TIMEOUT_S = 5400


def partial_pytest(out):
    """What a KILLED-OR-TIMED-OUT pytest log already proves. -> {collected, done, passed, failed, ...}

    2026-09-07, B.015a.journey-sole-scheduler: the gate hit its 3600s wall and reported "NOT RUN ...
    NO test result is implied". The log it had just written said `collected 314 items` and 273 dots
    with ZERO failures — killed at 87%, about nine minutes short. The claim was false and the
    evidence was already on disk.

    Run the counterfactual and it is worse than a wasted hour: had those dots been `FF`, the row
    would have been BYTE-IDENTICAL. A genuine red and a slow green were indistinguishable, and in
    the bad direction — a real failure reading as "the gate saw nothing". This function exists so
    the verdict can distinguish its own causes without re-running anything.

    Progress characters, not the summary: a timed-out run never reaches `=== short test summary ===`,
    which is exactly why the summary-line regex found nothing to report.
    """
    got = {"collected": 0, "done": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0}
    m = COLLECTED_RE.search(out or "")
    if m:
        got["collected"] = int(m.group(1))
    # WRAPPED LINES COUNT. pytest wraps its progress at the terminal width, and the continuation
    # lines carry no filename — they start with dots at column 0. A filename-anchored regex saw 143
    # of the 273 characters in the real 2026-09-07 log, which would have made the row understate the
    # work by nearly half while looking precise.
    for raw in (out or "").split("\n"):
        line = PCT_RE.sub("", raw.rstrip())
        m = PROGRESS_HEAD_RE.match(line)
        chars = m.group(1) if m else (line if line.strip() and PROGRESS_TAIL_RE.match(line) else "")
        for ch in chars:
            if ch not in ".FEsxXu":
                continue
            got["done"] += 1
            if ch == ".":
                got["passed"] += 1
            elif ch == "F":
                got["failed"] += 1
            elif ch == "E":
                got["error"] += 1
            elif ch == "s":
                got["skipped"] += 1
    return got


def timeout_row(pt, log, wall):
    """A timed-out proof run's row. -> (passed, detail), where passed is False or None, never True.

    Three states, and only the third is the "nothing was measured" the old line claimed for all of
    them. A FUNCTION so the test reads the row the gate writes rather than a copy of the rule.
    """
    where = (f"{pt['done']} of {pt['collected'] or '?'} test(s) ran before the {wall}s wall "
             f"({pt['passed']} passed, {pt['failed']} failed, {pt['error']} error, "
             f"{pt['skipped']} skipped)")
    if pt["failed"] + pt["error"]:
        return False, (f"PARTIAL and FAILING — {where}. The run was cut short, but these failures "
                       f"are a real result about this candidate: they were recorded before the "
                       f"wall. Fix them; the untested remainder is a separate question. log={log}")
    if pt["done"]:
        return None, (f"NOT RUN (timed out) — {where}, with NO failures among them. This is a GATE "
                      f"limit, not a candidate defect: do not send the lane into rework for it. "
                      f"The suite needs more wall time or a parallel run. log={log}")
    return None, (f"NOT RUN (timed out) — the log holds no pytest progress at all, so the run died "
                  f"before or during collection and NOTHING about this candidate was measured. "
                  f"log={log}")


def _migrations(cmd, cwd):
    """-> {number: {full filename}} from a `git ls-*` listing. The FULL name, because a row that
    says `247: lane ['lane_also.sql'] vs trunk ['trunk_took_it.sql']` makes the reader reconstruct
    which file is which number, and this row is read while deciding whether to merge."""
    rc, txt = sh(cmd, cwd=cwd, timeout=120)
    if rc != 0:
        # sh() MERGES STDERR, and git's errors quote back the paths we handed it. Measured
        # 2026-09-07 with real commands:
        #   git cat-file -p platform/migrations/250_ghost.sql
        #     -> fatal: Not a valid object name platform/migrations/250_ghost.sql
        #     -> parses as {'250': {'250_ghost.sql'}}
        # These numbers reach HARD FAIL rows — MIGRATION COLLISION and DUPLICATE migration numbers —
        # so a phantom here refuses a merge over a migration nobody holds and sends a person hunting
        # a lane that does not exist. `{}` is wrong in the safe direction: a failed listing can then
        # only fail to FIND a collision, never invent one.
        # The three real call sites use ls-tree/ls-files with a DIRECTORY pathspec, whose failures
        # ("not a git repository", "Not a valid object name <ref>") do not end in a migration
        # filename and parse to {} anyway — so this is latent, not a defect anyone has seen. It
        # lands because the sibling ADVISORY already had this guard while the merge-blocking path
        # did not, and that asymmetry is the wrong way round at any probability.
        return {}
    return _migrations_from_text(txt)


def _migrations_from_text(txt):
    """The parsing half, so a caller that already has a listing does not re-run git for it."""
    out = {}
    for ln in (txt or "").split("\n"):
        m = MIG_RE.search(ln.strip())
        if m:
            out.setdefault(m.group(2), set()).add(m.group(1))
    return out


def migration_collisions(lane_m, trunk_m):
    """Numbers both trees hold under DIFFERENT filenames. -> sorted list.

    Extracted 2026-09-07 so the test calls THIS rather than re-deriving it. Its first version was a
    one-line comprehension inside the preflight, and the test that "covered" it recomputed the same
    expression — so a mutation replacing the whole rule with `set(lane) & set(trunk)` scored ZERO
    reds. A test that re-implements the logic it checks is testing itself.

    ★'s correction is the rule: a bare intersection always reports a collision, because lane and
    trunk share 001..N by construction. The question is per number, do the two trees hold different
    filenames.
    """
    return sorted(n for n in set(lane_m) & set(trunk_m) if lane_m[n] != trunk_m[n])


def sibling_migration_clashes(item_id, lane_m, queue_path=None):
    """Numbers this lane shares with ANOTHER UNMERGED LANE. -> [(number, our file, lane, their file)]

    ADVISORY, and it must stay advisory: taking the next free number is CORRECT behaviour, and all
    the lanes doing it independently is not a defect in any of them. BOSS assigns the numbers at the
    merge turn; this row only makes the queue of assignments VISIBLE at gate time instead of at the
    moment two of them meet.

    THE ROW MUST TELL THE READER NOT TO ACT ON IT (BOSS, 2026-09-07). The failure mode is a helpful
    executor: it sees `250: this lane vs B.erasure`, renumbers itself to 251 unprompted, and now two
    lanes disagree about which of them is stale — a worse state than the one this row exists to
    surface. Seeing the clash is useful; assigning is BOSS's.

    2026-09-07: migration 250 was taken by THREE lanes at once — egress
    (250_connection_authority_clock), erasure (250_erase_retention_task_subject_arrays) and journey
    (250_campaign_journey_sole_scheduler_slice1) — with trunk holding no 25x at all. Each lane was
    individually contiguous and each gate's preflight passed honestly, because the preflight compares
    the lane against TRUNK and never against another lane. Nothing in the system could see it.

    Failures here are swallowed on purpose: a sibling worktree may be mid-rebase, gone, or busy, and
    an advisory row must never be the thing that reddens a gate.
    """
    out = []
    try:
        q = json.load(open(queue_path or QUEUE))
    except Exception:  # noqa: BLE001 — no queue, no advisory; never a failure
        return out
    for other in q.get("items", []):
        if other.get("id") == item_id or other.get("status") in ("merged", "landed"):
            continue
        owt = other.get("worktree")
        if not owt or not os.path.isdir(owt):
            continue
        rc, o = sh(["git", "-C", owt, "ls-tree", "-r", "--name-only", "HEAD", MIG_DIR], None, timeout=60)
        if rc != 0:
            # LOAD-BEARING, not decoration: sh() merges stderr into its output and git's errors name
            # paths, so a line ending in `.../250_ghost.sql` parses as migration 250. Without this
            # skip a failed sibling read would contribute PHANTOM numbers to a collision advisory.
            continue
        their = _migrations_from_text(o)
        for n in sorted(set(lane_m) & set(their)):
            if lane_m[n] != their[n]:
                out.append((n, sorted(lane_m[n]), other.get("id"), sorted(their[n])))
    return out


def sibling_clash_row(sib):
    """The advisory's text. A FUNCTION so the test reads the row an executor will read.

    The instruction not to act on it is the load-bearing half and it is asserted, not assumed: a row
    that reports a clash without saying who resolves it invites the helpful renumber that makes the
    clash worse.
    """
    return ("migration numbers also held by UNMERGED SIBLING LANES (advisory, not a defect in this "
            "lane — each lane correctly took next-free): "
            + "; ".join(f"{n}: this lane {mine} vs {oid} {theirs}" for n, mine, oid, theirs in sib)
            + ". DO NOT RENUMBER ON THIS ROW — ASK BOSS. He assigns numbers at the merge turn, and a "
              "lane that renumbers itself because it read this row leaves two lanes disagreeing "
              "about which one is stale. Seeing the clash is useful; acting on it is BOSS's. The "
              "preflight row above compares this lane against TRUNK only and cannot see any of these.")


def run_merge_preflight(wt, head, item_id, rec, warn=None):
    """HARD check: does the candidate actually merge into trunk, and does the UNION collide?

    §5.4, and it is not hypothetical. Every other check in this gate measures the LANE. A migration
    number is only unique in the tree it lands in, so a lane that legitimately took 247 in isolation
    collides with a trunk that took 247 elsewhere, and nothing in a lane-only gate can see it —
    tonight a second 247 would have PASSed. `core/db.py:518` refuses a gap as hard as a duplicate,
    so the collision is not cosmetic: it stops the runner.

    Built in a DISPOSABLE integration worktree off trunk, merged --no-commit and always aborted, so
    neither trunk nor the lane is touched. Proofs are deliberately NOT run on the merged tree here:
    that costs the box, and it is a separate decision (BOSS, 2026-09-06).

    ★'s correction is load-bearing: `comm -12` over the two trees' migration NUMBERS always reports
    a collision, because lane and trunk share 001..248 by construction. The question is per number,
    do the two trees hold DIFFERENT filenames.
    """
    integ = os.path.join(CN, f"voicepod-integ-{item_id}")
    ref = head
    if sh(["git", "-C", TRUNK, "cat-file", "-e", head + "^{commit}"])[0] != 0:
        # a lane in a different repo (the Codex checkouts have their own .git): bring the commit in
        if sh(["git", "-C", TRUNK, "fetch", "--no-tags", wt, head], timeout=600)[0] != 0:
            rec("merge preflight", False, f"cannot reach candidate {head[:10]} from trunk and fetching it failed")
            return
        ref = "FETCH_HEAD"

    # Pre-merge, and computed even if the merge conflicts: same number, different filename.
    lane_m = _migrations(["git", "-C", wt, "ls-tree", "-r", "--name-only", head, MIG_DIR], None)
    trunk_m = _migrations(["git", "-C", TRUNK, "ls-tree", "-r", "--name-only", "plan010/rebuild", MIG_DIR], None)
    collide = migration_collisions(lane_m, trunk_m)
    coll_txt = "; ".join(f"{n}: lane {sorted(lane_m[n])} vs trunk {sorted(trunk_m[n])}" for n in collide)
    # ADVISORY, and separate from the row above because it is a different question with a different
    # answer. The row above asks "does this lane collide with trunk", which is a merge blocker. This
    # asks "who else is holding this number right now", which is not a defect in anyone — it is the
    # queue of merge-turn assignments, made visible before two of them meet rather than after.
    if warn:
        sib = sibling_migration_clashes(item_id, lane_m)
        if sib:
            warn(sibling_clash_row(sib))

    sh(["git", "-C", TRUNK, "worktree", "remove", "--force", integ], timeout=180)
    rc, out = sh(["git", "-C", TRUNK, "worktree", "add", "--detach", integ, "plan010/rebuild"], timeout=300)
    if rc != 0:
        rec("merge preflight", False, f"cannot build the integration worktree: {out.strip()[-160:]}")
        return
    try:
        rc, mout = sh(["git", "merge", "--no-commit", "--no-ff", ref], cwd=integ, timeout=600)
        conflicts = []
        if rc != 0:
            _, u = sh(["git", "diff", "--name-only", "--diff-filter=U"], cwd=integ, timeout=120)
            conflicts = [f for f in u.split("\n") if f.strip()]
        dupes = []
        if rc == 0:
            merged = _migrations(["git", "ls-files", MIG_DIR], integ)
            dupes = sorted(f"{n}: {sorted(v)}" for n, v in merged.items() if len(v) > 1)
        ok = rc == 0 and not conflicts and not dupes and not collide
        why = []
        if conflicts:
            why.append(f"MERGE CONFLICTS in {len(conflicts)} file(s): {conflicts[:8]}")
        elif rc != 0:
            why.append(f"merge failed with no conflicted paths: {mout.strip()[-200:]}")
        if collide:
            why.append(f"MIGRATION COLLISION (same number, different file): {coll_txt}")
        if dupes:
            why.append(f"DUPLICATE migration numbers in the merged tree: {dupes}")
        # WHICH PAIR. This row is the only check that measures the UNION rather than the lane, so its
        # answer expires when EITHER side moves, and an expired verdict reads exactly like a live one
        # — in both directions. Measured 2026-09-07: the egress and route-authority lanes both held
        # 249_connection_authority_clock.sql while trunk held 249_auxiliary_not_sent_authority.sql,
        # and this check reported that collision on both lanes; their earlier gates had passed
        # honestly, because trunk had not taken 249 yet. Then the egress lane renumbered 249 -> 250
        # and the FAIL went stale within the hour (BOSS reproduced it: lane ec91da2a COLLIDE=['249'],
        # lane 81fbf545 COLLIDE=none). A stale FAIL is as unreadable as a stale PASS, and that one
        # expired because the LANE moved, not trunk. So name BOTH shas, not trunk's alone.
        _, tsha = sh(["git", "-C", TRUNK, "rev-parse", "plan010/rebuild"], timeout=60)
        against = (f" [measured on lane {head[:10]} against trunk {tsha.strip()[:10]}; "
                   f"this verdict expires when either moves]")
        rec("merge preflight", ok,
            (f"merges clean into plan010/rebuild; {len(_migrations(['git', 'ls-files', MIG_DIR], integ))} "
             f"migration numbers, no duplicates, no collisions" if ok else " | ".join(why)) + against)
    finally:
        sh(["git", "merge", "--abort"], cwd=integ, timeout=120)
        sh(["git", "-C", TRUNK, "worktree", "remove", "--force", integ], timeout=180)


YAML_EXT = (".yml", ".yaml")


def run_yaml_parse_row(wt, sha, files, warn):
    """ADVISORY: does every YAML file in the candidate actually PARSE at the candidate sha?

    2026-09-06 (BOSS, gate 47414): a rebase left a commit subject inside `.github/workflows/ci.yml`,
    so the file did not parse at all — and the gate ran the collection detector against it, got `[]`,
    and no row noticed. Codex found it by reading. A file that cannot be parsed cannot enforce
    anything, and every green measured through it means nothing; that is worth one row.

    Parsed from `git show <sha>:<path>`, never the working tree, which the lane's next build may
    already have dirtied. Advisory: it goes through warn(), never rec(). If PyYAML is unavailable
    the row SAYS the check did not run — the whole point is to notice a file nobody could read.
    """
    cands = [f for f in files if f.lower().endswith(YAML_EXT)]
    if not cands:
        return None
    try:
        import yaml
    except ImportError as e:  # noqa: BLE001
        warn(f"YAML parse: NOT CHECKED — PyYAML unavailable ({e}); {len(cands)} YAML file(s) in the "
             f"candidate were NOT parsed. This row is not a pass.")
        return None
    bad, ok, gone = [], 0, 0
    for f in cands:
        rc, txt = sh(["git", "-C", wt, "show", f"{sha}:{f}"], timeout=120)
        if rc != 0:
            gone += 1                     # deleted or renamed at this sha: nothing to parse
            continue
        try:
            list(yaml.safe_load_all(txt))
            ok += 1
        except yaml.YAMLError as e:
            m = getattr(e, "problem_mark", None)
            where = f":{m.line + 1}:{m.column + 1}" if m else ""
            bad.append(f"{f}{where} — {str(getattr(e, 'problem', e)).strip()[:90]}")
    if bad:
        warn(f"YAML parse FAILED on {len(bad)} of {len(cands)} candidate YAML file(s): "
             + "; ".join(bad[:3])
             + ". A file that does not parse enforces NOTHING, so any check that read it — a "
               "detector, a workflow gate — measured nothing and said so quietly. ADVISORY.")
    else:
        warn(f"YAML parse: {ok} candidate YAML file(s) parse clean at {sha[:10]}"
             + (f" ({gone} not present at this sha)" if gone else "") + ".")
    return not bad


def code_stamp():
    """The provenance stamp for whatever this run produced. Never raises.

    "not-loaded" is the honest answer for a module this run never imported (--no-agy, or a gate that
    bailed before the sweep). Hashing it from disk here would name code that had no part in this
    verdict, which is the same lie in a smaller font. Per-module hashes as well as the combined one,
    so a reader can tell WHICH module changed rather than only that something did.
    """
    mods, hs, newest = [], [], 0
    for _n in ("mergegate", "gatereview2", "citesweep"):
        _h, _mt = LOADED.get(_n, ("not-loaded", 0))
        newest = max(newest, _mt)
        hs.append(_h)
        mods.append(f"{_n} {_h}")
    return (f"{datetime.fromtimestamp(newest or time.time()):%Y-%m-%d %H:%M:%S}"
            f" md5:{hashlib.md5(''.join(hs).encode()).hexdigest()[:8]} [{' · '.join(mods)}]")


REPORT_PROOF_RE = re.compile(r"(?:platform/)?(tests/test_\w+\.py)")


def proofs_from_report(txt, wt):
    """Test files the REPORT names that ACTUALLY EXIST in the candidate. -> sorted list.

    BOSS, 2026-09-07: "the executor knows what it proved; my row is a prediction." A queue row
    without `proof_files` is his omission, and the report written by the lane is a better source for
    the same fact — so the gate asks the report before it declares NOT DECLARED.

    The existence filter is the point. A report can name a test file that was renamed, never
    committed, or invented, and adopting one would turn "nobody said what to measure" into "pytest
    was handed a path that does not exist" — a red about the gate wearing the shape of a red about
    the candidate. Only files present in the candidate worktree are adopted, and the row says the
    proofs came from the report rather than from the queue, because a substitution nobody can see is
    a guess.
    """
    out = set()
    for m in REPORT_PROOF_RE.finditer(txt or ""):
        rel = m.group(1)
        if os.path.exists(os.path.join(wt, "platform", rel)):
            out.add(rel)
    return sorted(out)


UNDECL_FIX = ("item declares no {what} — add `{what}` to this item's queue row; this is a defect in "
              "the ITEM, not in the lane, and no test result about the candidate is implied")


def verdict_of(ok, not_run, undeclared):
    """The headline verdict and its reason. -> (verdict, why)

    A FUNCTION, so tests exercise this rule instead of re-deriving it: a test that recomputes the
    expression it checks scores zero reds against a mutation replacing the rule outright (measured
    2026-09-07 on the migration-collision check, twice in one night).

    Three ways a gate is not a PASS and they are acted on differently — FAIL is fixed in the lane,
    NOT RUN is re-run when the box frees, NOT DECLARED is a missing field in the item's queue row.
    The verdict WORD collapses the last two into INCOMPLETE deliberately: autogate, the checkpoint
    reporter and every grep on events.log key on that word, so a fifth word would make an unproven
    gate invisible to all three until each learned it. The reason travels in the parenthetical.
    """
    unrun = ", ".join(n for n in (not_run or []) if n)
    undecl = ", ".join(n for n in (undeclared or []) if n)
    why = "; ".join(x for x in (f"{unrun} not run" if unrun else "",
                                f"{undecl} not declared" if undecl else "") if x)
    if not ok:
        return "FAIL" + (f" ({why})" if why else ""), why
    if why:
        return f"INCOMPLETE ({why})", why
    return "PASS", why


class _Undeclared:
    """Sentinel for rec(): the item never said what to measure. Not a class of failure.

    A distinct object rather than a string, so `passed is UNDECLARED` cannot be satisfied by a
    detail string that happens to say "undeclared".
    """
    def __repr__(self):
        return "UNDECLARED"


UNDECLARED = _Undeclared()


def render_gate_md(item_id, verdict, sha, lane, wt, stamp, checks):
    """The gate .md, as text. Extracted 2026-09-06 so it can be tested against a REAL gate file.

    Two row shapes and the difference is load-bearing: a rec() row prints `- **name**: STATUS —
    detail` and is part of the verdict; a warn() row has no name and prints its own text verbatim,
    so what BOSS reads is the reviewer's words rather than a re-wrap of them. The header is three
    lines and the `gate code` stamp is the last of them — see the stamp block's own comment for why
    it is written last of everything.
    """
    out = [f"# GATE {item_id} — {verdict} — {now()}",
           f"sha {sha}  lane {lane}  worktree {wt}",
           f"gate code {stamp}", ""]
    for n_, s_, d_ in checks:
        out.append(f"- {d_}" if n_ is None else f"- **{n_}**: {s_} — {d_}")
    return "\n".join(out) + "\n"


def run_citesweep(root, item_id, report_rel, artifact, sha, warn, where):
    """Resolve the report's and ledger row's citations against `root`. WARNING row, never a FAIL.

    Called from run_proofs' finally, so it still runs when the proofs bailed out early — a census
    that found the box busy says nothing about whether the citations are stale, and losing the
    sweep to an unrelated early return would quietly produce the one output this tool must never
    produce: a zero that means "did not look".
    """
    try:
        import citesweep
        note_loaded("citesweep")  # hashed at ITS import, which may be an hour after process start
        # The REPORT is resolved against the candidate — it is the candidate's claim. The LEDGER is
        # read from TRUNK, which is the sole source of truth for it: a lane's copy is a snapshot
        # from whenever that lane branched, and reading it made the sweep report a ledger row as
        # MISSING that BOSS had already committed (2026-09-06 23:0x, 010.circleci-heredoc-escape).
        res = citesweep.sweep(item_id, root, report_rel, artifact, sha, ledger_root=TRUNK)
        with open(os.path.join(GATES, f"{item_id}.cites.md"), "w") as f:
            f.write(citesweep.render(res))
        warn(citesweep.row(res) + f" — resolved against {where}")
        return True
    except Exception as e:  # noqa: BLE001 — a sweep that cannot run is a warning, never a FAIL
        warn(f"citations: NOT RUN — {type(e).__name__}: {e}")
        return False


DEP_FILES = ("pyproject.toml", "uv.lock")


def dep_digest(platform_dir):
    """md5 of the dependency spec (pyproject.toml + uv.lock). None if either is missing."""
    h = hashlib.md5()
    for fn in DEP_FILES:
        p = os.path.join(platform_dir, fn)
        if not os.path.isfile(p):
            return None
        h.update(open(p, "rb").read())
    return h.hexdigest()[:12]


def resolve_venv(scratch, wt):
    """Give the scratch checkout a venv, and SAY WHERE IT CAME FROM. -> (label, refusal or None).

    2026-09-06 (BOSS): the venv is symlinked from the lane worktree, and WORKER-1's lane never built
    one — it ran box-free deploy tests against voice-pod's venv — so the gate exec'd an interpreter
    that did not exist. The venv source was never a decision; it was whatever the lane happened to
    have. Now it is decided and recorded:

      lane      — the lane's own venv (unchanged, and still preferred: it is the one the executor used)
      trunk     — trunk's venv, but ONLY when the candidate's dependency spec is byte-identical to
                  trunk's (pyproject.toml + uv.lock). A venv built for different dependencies would
                  produce a green that means nothing, which is worse than a NOT RUN.
      refusal   — no usable venv, with the reason, so the row can say NOT RUN instead of crashing.
    """
    sp = os.path.join(scratch, "platform")
    if os.path.exists(os.path.join(sp, ".venv", "bin", "python")):
        return "lane", None
    tsp = os.path.join(TRUNK, "platform")
    if not os.path.exists(os.path.join(tsp, ".venv", "bin", "python")):
        return None, (f"the lane worktree {os.path.basename(wt)} has no platform/.venv and neither "
                      f"does trunk — there is nothing to run the suite with")
    cand, trunk_d = dep_digest(sp), dep_digest(tsp)
    if cand is None or trunk_d is None:
        return None, (f"the lane has no platform/.venv, and the dependency spec "
                      f"({' + '.join(DEP_FILES)}) could not be read "
                      f"{'in the candidate' if cand is None else 'on trunk'}, so trunk's venv cannot "
                      f"be shown to match this candidate")
    if cand != trunk_d:
        return None, (f"the lane has no platform/.venv, and trunk's cannot be used: the dependency "
                      f"spec differs (candidate {cand} vs trunk {trunk_d}). A venv built for other "
                      f"dependencies would make any green here meaningless")
    try:
        os.symlink(os.path.realpath(os.path.join(tsp, ".venv")), os.path.join(sp, ".venv"))
    except OSError as e:
        return None, f"trunk's venv matches but could not be linked: {e}"
    return f"trunk ({' + '.join(DEP_FILES)} identical, {cand})", None


def run_citesweep_isolated(wt, item_id, report_rel, artifact, sha, warn):
    """Sweep in a DISPOSABLE detached checkout at the candidate sha, for gates that never take the box.

    2026-09-06 (BOSS): the box path already sweeps inside its scratch worktree, so its citations are
    resolved against exactly the tree the proofs measured. A box-free item (no `.py` proof files) or
    `--no-box` had no such tree and fell back to the LANE worktree — which the daemon dispatches the
    lane's NEXT item into, so it can carry uncommitted work or later commits. The row said so, but
    honest is weaker than correct when correct is a `worktree add` away: citesweep is read-only and
    needs no lock, so build a checkout for it and throw it away.

    Falls back to the lane worktree, with the old wording plus the reason, if the checkout cannot be
    built — a tree that will not build is exactly when a reader must not be told it was used.
    """
    swt = os.path.join(CN, f"voicepod-cites-{item_id}")
    sh(["git", "-C", wt, "worktree", "remove", "--force", swt], timeout=180)
    rc, out = sh(["git", "-C", wt, "worktree", "add", "--detach", swt, sha], timeout=300)
    if rc != 0:
        return run_citesweep(wt, item_id, report_rel, artifact, sha, warn,
                             f"the LANE worktree {os.path.basename(wt)} (NOT a detached checkout at "
                             f"{sha[:10]}) — the detached checkout could not be built: "
                             f"{out.strip()[-120:]}")
    try:
        return run_citesweep(swt, item_id, report_rel, artifact, sha, warn,
                             f"a detached checkout at {sha[:10]}")
    finally:
        sh(["git", "-C", wt, "worktree", "remove", "--force", swt], timeout=180)


def run_proofs(it, wt, sha, head, item_id, rec, warn=None, report_rel=None, artifact=None,
               portal_touched=(), ctx=None):
    """Wrapper: an exception in here is a FAIL ROW, never the end of the gate.

    2026-09-06, gate 66276: a FileNotFoundError from one probe propagated out of main and killed the
    gate after the box, both reviewers and the sweep had been paid for — no proofs row, no gate file,
    no GATE_ event, exit 1. A verdict-shaped silence. Whatever goes wrong in the proof path, BOSS
    must still get a gate file and an event saying so (BOSS, 2026-09-06).
    """
    try:
        return _run_proofs(it, wt, sha, head, item_id, rec, warn, report_rel, artifact,
                           portal_touched, ctx)
    except Exception as e:  # noqa: BLE001 — deliberately broad: the alternative is no gate file at all
        rec("proofs", False,
            f"NOT RUN: the proof path raised {type(e).__name__}: {str(e)[:200]} — this is a GATE "
            f"defect, not a candidate result. NO test result is implied. "
            f"{traceback.format_exc().strip().splitlines()[-2].strip()[:160]}")
        return None


def _run_proofs(it, wt, sha, head, item_id, rec, warn=None, report_rel=None, artifact=None,
                portal_touched=(), ctx=None):
    proofs = it.get("proof_files", [])
    if isinstance(proofs, str):  # a string here would be splatted per character (queuectl defect, 2026-09-05)
        try:
            proofs = json.loads(proofs)
        except json.JSONDecodeError:
            proofs = [p for p in re.split(r"[\s,]+", proofs) if p]
        it["proof_files"] = proofs
    portal_proofs = [p for p in proofs if p.endswith((".ts", ".tsx"))]
    proofs = [p for p in proofs if p.endswith(".py")]
    if portal_proofs or portal_touched:
        # any portal/** file in the diff promotes this row to the whole suite, named .tsx proofs or not
        #
        # IN A FROZEN CHECKOUT AT THE CANDIDATE SHA (Plan 003 §4 A2). The Python proofs below have
        # always built one; portal's ran in the LANE worktree, which the daemon dispatches the
        # lane's next item into — so a portal row could measure later commits and uncommitted work
        # and report them under this candidate's sha. Vitest is box-free and needs nothing scarce,
        # so the checkout costs a `worktree add` and nothing else.
        pwt, pwhy = frozen_checkout(wt, sha, f"portal-{item_id}", deps=("portal/node_modules",))
        if not pwt:
            # Never a quiet fallback to the moving tree: a result measured somewhere other than the
            # sha it is filed under is worse than a missing result, because it looks like evidence.
            rec("portal proofs", False,
                f"NOT RUN: {pwhy}. The lane worktree was NOT used as a substitute — a portal result "
                f"measured on a tree that is not {sha[:10]} would be filed under a sha it never ran "
                f"against.")
        else:
            try:
                run_portal_proofs(portal_proofs, pwt, item_id, rec, whole=bool(portal_touched),
                                  warn=warn)
            finally:
                drop_checkout(wt, pwt)
    if not proofs:
        # a portal-only item is fully measured without ever taking the box
        if not portal_proofs and not portal_touched:
            rec("proofs", UNDECLARED, UNDECL_FIX.format(what="proof_files"))
        return
    # From here the box path is committed: if it returns early (busy box, census, worktree add) the
    # sweep must NOT pretend it can build a checkout — that is cases 3 and 4, which keep the lane
    # fallback and its wording.
    it["_box_attempted"] = True
    token = f"gate-{item_id}-{os.getpid()}-{int(time.time())}"
    waited = 0
    while True:
        try:
            os.mkdir(LOCK)
            break
        except FileExistsError:
            broke, why = break_dead_box_lock()
            if broke:
                # A silent break is indistinguishable from a lock that was never taken (BOSS).
                emit("GATE_BROKE_DEAD_BOX_LOCK", item_id, why)
                print(f"broke a dead box lock — {why}", file=sys.stderr)
                continue
            if waited == 0 or waited % 600 == 0:
                # A gate queued on the box was INVISIBLE: no event, no file, and the .md is written
                # only at the end, so it looked exactly like a hung process. BOSS killed two healthy
                # gates on 2026-09-07 06:0x believing the agy leg had hung — they were 43 and 28
                # minutes into a legitimate box wait behind EXEC-G's build, which had held the lock
                # since 05:13:48. Their Codex reviews were already paid for. Say it, and keep saying
                # it every 10 minutes, so waiting and hanging stop looking the same.
                emit("GATE_WAITING_FOR_BOX", item_id,
                     f"waited={waited}s of 3600s max; box owner={lock_owner() or 'unknown'}; "
                     f"not breaking it because: {why}; "
                     f"this gate is ALIVE and queued, not hung — its .md is written only after the "
                     f"proofs run. Killing it discards a Codex review that has already been paid for.")
            time.sleep(30)
            waited += 30
            if waited > 3600:
                emit("GATE_GAVE_UP_ON_BOX", item_id,
                     f"waited 3600s; box owner={lock_owner() or 'unknown'} — proofs NOT RUN")
                rec("proofs", None, f"not run — box busy > 60 min (owner {lock_owner() or 'unknown'})")
                return
    if waited:
        emit("GATE_GOT_BOX", item_id, f"acquired after {waited}s in the queue")
    open(os.path.join(LOCK, "owner"), "w").write(token)
    if ctx is not None:
        ctx["token"] = token       # so the crash guard can release a lock this run is still holding
    scratch = None
    try:
        _, cen = sh(["/usr/bin/python3", CENSUS])
        n = re.search(r"^count:\s*(\d+)", cen, re.M)
        if not (n and n.group(1) == "0"):
            rec("proofs", None, f"not run — box occupied, census={cen.strip()[:40]}")
            return
        # Prove in a DETACHED worktree at the candidate sha, never in the lane worktree: the daemon
        # dispatches the lane's next item into it, so it is dirty for the whole of the next build
        # (BOSS, 2026-09-05 — capability-descriptor's gate ran nothing and reported dirty=True).
        # It also makes provenance exact: these proofs ran at this sha and nothing else.
        scratch = os.path.join(CN, f"voicepod-gate-{item_id}")
        sh(["git", "-C", wt, "worktree", "remove", "--force", scratch], timeout=120)
        rc, out = sh(["git", "-C", wt, "worktree", "add", "--detach", scratch, sha], timeout=180)
        if rc != 0:
            rec("proofs", False, f"cannot create scratch worktree at {sha[:10]}: {out.strip()[-160:]}")
            return
        for rel in ("platform/.venv", "agent/.venv", "portal/node_modules"):
            src, dst = os.path.join(wt, rel), os.path.join(scratch, rel)
            if os.path.exists(src) and not os.path.exists(dst):
                os.symlink(os.path.realpath(src), dst)
        sp = os.path.join(scratch, "platform")
        venv_src, refusal = resolve_venv(scratch, wt)
        if refusal:
            rec("proofs", False,
                f"NOT RUN: {refusal}. Build the lane's venv (or point the item at a worktree that "
                f"has one) and rerun; NO test was executed, so no result is implied.")
            return
        # Negative control on the provenance itself: the venv carries dependencies only (no editable
        # install), so `core` must resolve INSIDE the scratch tree. If it resolves anywhere else the
        # run would silently measure another checkout — refuse rather than report a green.
        rc, where = sh([os.path.join(sp, ".venv/bin/python"), "-c", "import core; print(core.__file__)"], cwd=sp)
        if rc != 0 or not os.path.realpath(where.strip()).startswith(os.path.realpath(scratch)):
            rec("proofs", False, f"provenance check failed: core resolves to {where.strip()[:110] or 'ERROR'}")
            return
        _, sst = sh(["git", "status", "--short", "-uno"], cwd=scratch)
        _, shead = sh(["git", "rev-parse", "HEAD"], cwd=scratch)
        if sst.strip() or not shead.strip().startswith(sha[:8]):
            rec("proofs", False, f"scratch not clean at sha: dirty={bool(sst.strip())} head={shead.strip()[:10]}")
            return
        # STILL OURS? Setup between the mkdir and the first test takes ~15-25 s (census, worktree
        # add, provenance, status) and nothing re-read the lock across it.
        #
        # This guard was written after audit-residuals r6 on 2026-09-05, but NOT for the reason the
        # first draft of this comment gave. I inferred from a lockwatch RACE_CHURN event that another
        # taker had released a lock it did not own; BOSS measured it and the inference was WRONG.
        # Actual sequence: BOSS released at 18:09:06; a temp worker mkdir'd at 18:09:12 and was gone
        # by 18:09:15; this gate's 30-s poll (from token epoch 17:47:15 -> ...18:08:45, 18:09:15,
        # 18:09:45) took the box cleanly at 18:09:15. EVERY transition in that window was an atomic
        # mkdir after a genuine release. The pytest died because the temp worker's cleanup ran
        # `pkill -f pytest` — a PATTERN kill that does not care who holds the lock, now a written
        # BOX-QUEUE rule, and not something any lock check can prevent.
        #
        # The guard stays because the window is real: nothing re-verified the token between
        # acquiring and spending an hour of box time. It simply did not fire here, and saying so is
        # the point — a guard credited with catching an incident it never caught is how a check
        # survives long after it has stopped meaning anything.
        # RACE_CHURN is an INFORMATIONAL lockwatch event: it says a dir was recreated soon after a
        # release, NOT who released it. Reading blame into it was my error, not the tool's.
        if lock_owner() != token:
            rec("proofs", False,
                f"LOCK STOLEN before pytest started — token in owner is {lock_owner()!r}, ours is "
                f"{token!r}. NOT a candidate defect: the box changed hands during this gate's setup, "
                f"so nothing was measured. Re-run the gate. Cause is one of: our owner file was "
                f"overwritten or removed by another taker, or our own release ran early — read "
                f"lockwatch's OWNER_MOVED_IN_PLACE / HALF_RELEASED lines before assigning blame. "
                f"RACE_CHURN alone does NOT show a stolen lock.")
            return
        log = os.path.join(CN, "test-logs", f"{datetime.now():%Y%m%d-%H%M}-gate-{item_id}-{sha[:8]}.log")
        timed_out = False
        with open(log, "w") as f:
            f.write(f"HEAD {shead.strip()} status:[] token={token} scratch={scratch} start={now()}\n")
        with open(log, "a") as f:
            try:
                r = subprocess.run(["./.venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-rA", *proofs],
                                   cwd=sp, stdout=f, stderr=subprocess.STDOUT, timeout=PROOF_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                # The wall is not a verdict, but the log is EVIDENCE and it is already written. Three
                # outcomes, and only the third is the "nothing was measured" the old line claimed for
                # all of them. Handled HERE rather than in run_proofs' catch-all because `log` — the
                # only thing that can tell the cases apart — is in scope only here.
                f.flush()
                timed_out = True
                r = None
        # A KILLED run measured nothing and must never be rendered as red tests. audit-residuals r6
        # printed `rc=-15 reds=0 summary=NONE`, which reads like a failing candidate to anyone
        # scanning gate files; the real cause was an external `pkill -f pytest`. Say "killed by
        # signal N" in words, and say plainly that it is not a measurement (BOSS, 2026-09-05 18:2x).
        if r is not None and r.returncode < 0:
            sig = -r.returncode
            if lock_owner() != token:
                rec("proofs", False,
                    f"killed by signal {sig} AND the box lock changed hands during the run (owner is "
                    f"now {lock_owner()!r}, ours was {token!r}) — NOT a candidate measurement; "
                    f"rerun. log={log}")
            else:
                rec("proofs", False,
                    f"killed by signal {sig} — NOT a candidate measurement; rerun. The box lock was "
                    f"still ours, so this was an external kill: a PATTERN kill such as `pkill -f "
                    f"pytest` reaches any run regardless of the lock. No test result is implied — "
                    f"reds=0 here means the run never got far enough to produce one. log={log}")
            return
        out = open(log, errors="ignore").read()
        if timed_out:
            passed_, detail = timeout_row(partial_pytest(out), log, PROOF_TIMEOUT_S)
            rec("proofs", passed_, detail)
            return
        reds = re.findall(r"^(FAILED|ERROR) (?:platform/)?tests/.*$", out, re.M)
        summ = [l for l in out.split("\n") if re.search(r"\d+ (passed|failed|error)", l)]
        # THIS LOG MERGES STDERR (stderr=subprocess.STDOUT above), so any line beginning
        # `ERROR tests/...` counts as a red whoever wrote it. rc=0 WITH reds is therefore not a
        # finding about the candidate — it is two signals disagreeing, and the honest row says so.
        # It still does not PASS: an unexplained disagreement is not evidence of success. But it must
        # not read as "your tests failed" either, because the cost of that misreading is a rework
        # round plus a box acquisition, which is the scarcest thing we have (BOSS, 2026-09-07).
        contradiction = (r.returncode == 0 and reds)
        rec("proofs", r.returncode == 0 and not reds,
            f"rc={r.returncode} reds={len(reds)} summary={summ[-1].strip() if summ else 'NONE'} "
            f"venv={venv_src} log={log}"
            + (f" | CONTRADICTION: pytest EXITED 0 and {len(reds)} line(s) still match the red "
               f"pattern ({', '.join(x if isinstance(x, str) else str(x) for x in reds[:3])}). This "
               f"log merges stderr, so a line beginning `ERROR tests/...` from any source counts. "
               f"NOT a claim that your tests failed — read the log before reworking anything."
               if contradiction else "")
            + (f" [proof files came from the REPORT, not the queue row: "
               f"{', '.join(it['_proofs_from_report'][:6])}]" if it.get("_proofs_from_report") else ""))
    finally:
        if scratch:
            # The sweep runs at the candidate sha, BEFORE teardown: this checkout is the only place
            # the citations can be resolved against exactly the tree the proofs just measured.
            if warn and os.path.exists(os.path.join(scratch, ".git")):
                it["_cites_done"] = run_citesweep(scratch, item_id, report_rel, artifact, sha, warn,
                                                  f"the scratch worktree at {sha[:10]}")
            for rel in ("platform/.venv", "agent/.venv", "portal/node_modules"):
                p = os.path.join(scratch, rel)
                if os.path.islink(p):
                    os.unlink(p)  # never let `worktree remove` follow a symlink into the lane's venv
            sh(["git", "-C", wt, "worktree", "remove", "--force", scratch], timeout=180)
        release_if_mine(token)


def main():
    """Crash guard. A gate that does not finish must still leave something a reader can find.

    2026-09-06, gate 66276: an exception took the gate down after the box, both reviewers and the
    sweep were paid for, leaving no file and no event — BOSS spent ten minutes diagnosing an absence.
    Fixed at the source (run_proofs no longer lets an exception escape), but the source was one line
    out of hundreds, so this is the backstop for the rest.

    BOSS's ruling, and it is better than what I proposed: the crash file is `<item>.CRASHED.md`,
    NEVER `<item>.md`. So `<item>.md` keeps meaning "a gate ran to completion" — a directory listing
    shows a crash for what it is instead of quietly weakening the one invariant every reader of the
    gates directory relies on. The event is GATE_CRASHED, never GATE_FAIL, so a Monitor can tell an
    opinion from an accident.
    """
    ctx = {}
    try:
        return _main(ctx)
    except Exception as e:  # noqa: BLE001 — the whole point is that nothing escapes uncounted
        item_id = ctx.get("item_id") or (sys.argv[1:] or ["UNKNOWN"])[0]
        rows = list(ctx.get("checks") or [])
        rows.append((None, "WARN", f"**NOT COMPLETED** — the gate raised {type(e).__name__}: "
                                   f"{str(e)[:200]}. Rows above are what had been measured when it "
                                   f"died; everything below them was never run. This is a GATE "
                                   f"defect, not a candidate result."))
        rows.append((None, "WARN", "```\n" + traceback.format_exc().strip()[-1800:] + "\n```"))
        path = os.path.join(GATES, f"{item_id}.CRASHED.md")
        try:
            os.makedirs(GATES, exist_ok=True)
            with open(path, "w") as f:
                f.write(render_gate_md(item_id, "NOT COMPLETED", ctx.get("sha", "?"),
                                       ctx.get("lane", "?"), ctx.get("wt", "?"), code_stamp(), rows))
        except Exception:  # noqa: BLE001 — a failure to write the crash file must not hide the crash
            traceback.print_exc()
        if ctx.get("token"):
            release_if_mine(ctx["token"])      # belt and braces: run_proofs' finally already does this
        try:
            emit("GATE_CRASHED", item_id, f"dispatched_at={dispatched_at_of(item_id)}; "
                 f"{type(e).__name__}: {str(e)[:160]} — no verdict; "
                                          f"nothing was measured beyond the rows in {path}")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        print(f"NOT COMPLETED — {type(e).__name__}: {e}", file=sys.stderr)
        print(f"wrote {path}", file=sys.stderr)
        return 2


def _main(ctx):
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    item_id = args[0]
    nobox = "--no-box" in args
    nocodex = "--no-codex" in args
    autopilot = "--autopilot" in args or os.path.exists(os.path.join(D, "AUTOPILOT"))
    q = json.load(open(QUEUE))
    it = next((i for i in q["items"] if i["id"] == item_id), None)
    if not it:
        print("no such item")
        return 2
    wt, lane = it.get("worktree"), it.get("lane")
    ctx.update(item_id=item_id, wt=wt, lane=lane)
    if not it.get("proof_files") and it.get("artifact"):
        # The ledger row's PROOF lines name the test files — a measured source, not a guess.
        blk, on = [], False
        for ln in open(os.path.join(TRUNK, "plans", "EXECUTION_LEDGER.md"), errors="ignore"):
            if ln.startswith("ARTIFACT:") and ln.split()[1] == it["artifact"]:
                on = True
            if on:
                blk.append(ln)
                if ln.startswith("```") and len(blk) > 1:
                    break
        it["proof_files"] = sorted({m.replace("platform/", "")
                                    for m in re.findall(r"platform/tests/test_\w+\.py", "".join(blk))})
    if not wt or not os.path.isdir(wt):
        print("item has no usable worktree path")
        return 2
    checks = []
    ctx["checks"] = checks          # the crash path renders whatever has been recorded by then
    state = {"ok": True, "not_run": [], "undeclared": []}

    def rec(name, passed, detail):
        """passed=True PASS, passed=False FAIL, passed=None NOT RUN, passed=UNDECLARED NOT DECLARED.

        BOSS, 2026-09-07 08:2x: `proofs: FAIL — item declares no proof_files` is indistinguishable
        from "the declared tests ran and were red", and he nearly sent a rework to EXEC-N over his
        own missing queue field — the lane had measured 7/7 green with three break-tests red at the
        clauses they name. The row was reporting a defect in the ITEM as a defect in the LANE.

        So the same split we already made one level down: NOT RUN is "the box never came free", and
        NOT DECLARED is "nobody said what to measure". Neither is a failure of the candidate, and
        neither is a pass — an undeclared row blocks the merge exactly as an unrun one does, because
        in both cases nothing was proven. The detail names the queue key to add, so the reader fixes
        the row instead of the code.

        NOTE the signature: three positional arguments, no keywords. Every fake rec in the test suite
        is a 3-arg lambda, and adding `field=` to this one broke five test files at once while the
        gate itself still worked — a change to a callback's shape is a change to every caller's.

        BOSS, 2026-09-07: "a check that could not run is not a check that failed". `--no-box` used
        to record `proofs: FAIL — not run`, which pinned the headline verdict at FAIL on every gate
        of the night and made GATE_PASS:GATE_FAIL a metric that could not move — the reviewer signal
        underneath it (0:9 before the rules, 7:1 after) was invisible until the checkpoint reporter
        split the components out.

        NOT RUN is NOT a pass. It never contributes to state["ok"], it is never merged on, and it
        never yields the word PASS on its own: a gate with nothing failed and something unrun is
        INCOMPLETE. The distinction being tracked here is only "did this check produce a result",
        never "was the result good".
        """
        if passed is UNDECLARED:
            state["undeclared"].append(name)
            checks.append((name, "NOT DECLARED", detail))
            return
        if passed is None:
            state["not_run"].append(name)
            checks.append((name, "NOT RUN", detail))
            return
        state["ok"] = state["ok"] and passed
        checks.append((name, "PASS" if passed else "FAIL", detail))

    def warn(row_text):
        """An advisory row. Deliberately does NOT touch state["ok"] — see the module docstring.
        `None` for the name makes the .md print the row verbatim, so the text BOSS reads in the
        gate file is the reviewer's own words and not a re-wrap of them."""
        checks.append((None, "WARN", row_text))

    # (1) the sha the report names is on the lane head
    _, head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    head = head.strip()
    sha = head
    ctx["sha"] = sha
    rep_dir = os.path.join(wt, "audit", "plan-execution-2026-09-04", "reports")
    reps = []
    if os.path.isdir(rep_dir):
        reps = sorted((f for f in os.listdir(rep_dir) if f.startswith(item_id + "-")),
                      key=lambda f: os.path.getmtime(os.path.join(rep_dir, f)))
    if reps:
        txt = open(os.path.join(rep_dir, reps[-1]), errors="ignore").read()
        report_clock_row(reps[-1], txt, warn)
        # The queue row is a prediction written before the work; the report is what the lane says it
        # actually proved. Ask the report before declaring NOT DECLARED — but say so in a row, so
        # nobody reads a gate that measured report-named files as a gate that measured declared ones.
        if not it.get("proof_files"):
            from_report = proofs_from_report(txt, wt)
            if from_report:
                it["proof_files"] = from_report
                it["_proofs_from_report"] = list(from_report)
                warn(f"proof files: taken from the REPORT, not from the queue row — the item still "
                     f"declares none. Adopted {len(from_report)} file(s) that exist in the "
                     f"candidate: {', '.join(from_report[:6])}. Add them to the queue row.")
        sec1 = re.search(r"## 1\.(.*?)(?=\n## 2\.|\Z)", txt, re.S)
        hexes = re.findall(r"\b[0-9a-f]{8,40}\b", sec1.group(1) if sec1 else txt[:3000])
        # A report committed after its product commit names an older sha in §1 by construction (BOSS,
        # 2026-09-05 13:30): prefer any §1 sha that IS the head, else the last one named.
        if any(head.startswith(h) for h in hexes):
            sha = head
        else:
            # §1 also names the trunk base; keep only lane commits (ancestor of head, not of trunk)
            # and take the one nearest head — the product commit the report describes.
            best = None
            for h in dict.fromkeys(hexes):
                on_lane = sh(["git", "merge-base", "--is-ancestor", h, head], cwd=wt)[0] == 0
                on_trunk = sh(["git", "merge-base", "--is-ancestor", h, "plan010/rebuild"], cwd=wt)[0] == 0
                if on_lane and not on_trunk:
                    _, n = sh(["git", "rev-list", "--count", f"{h}..{head}"], cwd=wt)
                    dist = int(n.strip() or 0)
                    if best is None or dist < best[0]:
                        best = (dist, h)
            if best:
                sha = best[1]
    rc, _ = sh(["git", "merge-base", "--is-ancestor", sha, head], cwd=wt)
    _, tail = sh(["git", "diff", "--name-only", sha, head], cwd=wt)
    tail = [f for f in tail.split("\n") if f.strip()]
    docs_only = all(matches(f, ALWAYS_OK) for f in tail)
    rec("sha on lane head", rc == 0 and docs_only,
        f"report sha {sha[:10]} head {head[:10]} report={reps[-1] if reps else 'NONE'}; "
        f"tail after sha: {len(tail)} file(s) {'docs-only' if docs_only else 'INCLUDES PRODUCT: ' + str([f for f in tail if not matches(f, ALWAYS_OK)])}")
    sha = head  # proofs and scope are measured on the lane HEAD, which the tail check just bound to the report

    # (2) scope: every changed file inside the declared globs. Denylisted files do not fail the gate —
    # a migration on a populated table MUST edit core/db.py's registry (BOSS, 13:30) — they withhold the merge.
    _, mb = sh(["git", "merge-base", "plan010/rebuild", head], cwd=wt)
    _, files = sh(["git", "diff", "--name-only", mb.strip(), head], cwd=wt)
    files = [f for f in files.split("\n") if f.strip()]
    scope = it.get("scope", [])
    denied = [f for f in files if matches(f, DENY)]
    outside = [f for f in files if not (matches(f, scope) or matches(f, ALWAYS_OK) or matches(f, DENY))]
    if not scope:
        rec("scope", UNDECLARED, UNDECL_FIX.format(what="scope") + f" ({len(files)} files changed)")
    else:
        rec("scope", not outside, f"{len(files)} files; scope={scope}; OUTSIDE={outside}")
    run_route_provenance_row(it, rec, warn)
    hand_merge = bool(denied)
    checks.append(("hand-merge required", "YES" if hand_merge else "no",
                   f"foundation/denylisted files touched: {denied} — BOSS merges by hand" if hand_merge else "none"))

    # (2c) every YAML file in the candidate must parse — advisory, and it runs before the reviewers
    # so an unparseable workflow is named in the row rather than found by reading the diff.
    run_yaml_parse_row(wt, head, files, warn)

    # (2b) merge preflight — the union with trunk, not the lane (§5.4)
    run_merge_preflight(wt, head, item_id, rec, warn)

    # (4b) agy second opinion — STARTED FIRST so it overlaps the box proofs and the Codex review
    # rather than adding its own minutes to the gate's wall clock. It reads the same merge-base
    # diff Codex is given. Advisory: its result reaches `checks` through warn(), never rec().
    report_rel = os.path.join("audit", "plan-execution-2026-09-04", "reports", reps[-1]) if reps else None
    _, cand_diff = sh(["git", "diff", mb.strip(), head], cwd=wt, timeout=300)
    agy_box = {}
    noagy = "--no-agy" in args

    def run_agy():
        try:
            import gatereview2
            note_loaded("gatereview2")  # hashed at ITS import, not at write time
            # Whole-file upgrades read from the CANDIDATE SHA, not the working tree: the tree can
            # move under a gate (an executor's next commit), and a reviewer shown a file the gate
            # did not measure produces findings about code that is not in the candidate.
            def whole_file(path, _sha=sha, _wt=wt):
                rc_, body = sh(["git", "show", f"{_sha}:{path}"], cwd=_wt, timeout=60)
                return body if rc_ == 0 else None

            agy_box["v"] = gatereview2.review(item_id, cand_diff, it.get("artifact", ""),
                                              whole_file=whole_file)
            ps = agy_box["v"].get("packed_set")
            if ps:
                # BOSS, 2026-09-07: without this, "the reviewer missed it" and "the reviewer never
                # saw it" are the same observation. One file per gate, next to the gate's own .md.
                open(os.path.join(GATES, f"{item_id}.packed.txt"), "w").write(
                    f"# what the agy reviewer was shown for {item_id} at {sha[:10]}\n"
                    f"# FULL = whole file, hunk = diff stanza, TAIL = head-truncated, NONE = dropped\n"
                    + ps + "\n")
        except Exception as e:  # noqa: BLE001 — fail-OPEN: a second opinion is never a blocker
            agy_box["v"] = {"error": f"{type(e).__name__}: {e}"}

    agy_thread = None
    if not noagy:
        agy_thread = threading.Thread(target=run_agy, name=f"agy-{item_id}", daemon=True)
        agy_thread.start()

    # (4a) Codex adversarial review — STARTED HERE, alongside agy and BEFORE the box wait.
    # 2026-09-06 (BOSS): it used to run after run_proofs, so its ~5-15 min sat behind however long
    # the box was held by someone else — the review is read-only and needs nothing scarce, while the
    # box is the scarce thing. Running both reviewers during the wait takes them off the gate's wall
    # clock entirely (~10 min/gate). The thread does NOT call rec(): `state["ok"] = state["ok"] and
    # passed` is a read-modify-write, and a lost update in a gate's verdict is not a bug worth
    # risking to save a join. It parks its result and the main thread records it below, so the row
    # order in the .md is identical to before.
    codex_box = {}

    def run_codex():
        try:
            pre = codex_precheck()
            if pre:
                codex_box.update(rc=pre[0], out=pre[1])
                return
            focus = (f"Merge gate for {item_id} ({it.get('artifact', '')}): find authority gaps, "
                     f"tenancy leaks, money errors, fail-open paths.")
            last_msg = os.path.join(GATES, f"{item_id}.codex.json")
            argv = codex_review_argv("plan010/rebuild", sha, focus, REVIEW_SCHEMA, last_msg)
            # The reviewer reads the PINNED CANDIDATE, not the lane worktree: the daemon dispatches
            # the lane's next item into that tree, so by the time a review runs it can hold later
            # commits and uncommitted work that are not this candidate (Plan 003 §4 A2). A checkout
            # that cannot be built is a FAIL row, never a quiet fallback to the moving tree —
            # reviewing one tree while reporting another sha is the confusion being removed.
            rwt, rwhy = frozen_checkout(wt, sha, f"review-{item_id}")
            if not rwt:
                codex_box["error"] = f"frozen checkout for the review: {rwhy}"
                return
            codex_box["reviewed_in"] = rwt
            rc, out = sh(argv, cwd=rwt, timeout=1500, env=codex_env())
            # BOSS 2026-09-06: never SKIP the call while walled — the wall costs about a second to
            # discover and it may have lifted early, whereas a skip is a decision made on stale
            # information. But retry it exactly ONCE: a wall that is still up a second later is up,
            # and a parse error or an empty answer is not made truer by asking again, so the retry
            # is for the wall alone.
            if codex_failure_kind(rc, out)[0] == "WALLED":
                codex_box["retried"] = True
                rc2, out2 = sh(argv, cwd=rwt, timeout=1500, env=codex_env())
                if codex_failure_kind(rc2, out2)[0] != "WALLED":
                    rc, out = rc2, out2          # the wall lifted between the two calls
            # IDENTITY BEFORE CONTENT. The review body is what the model said; the rollout is what
            # the CLI recorded. Plan 003 §5 takes model/provider/effort from metadata tied to the
            # ACTUAL session id, never from the review's own prose — a report claiming a model does
            # not make it the model.
            sid_run = session_id_from_jsonl(out)
            roll, roll_why = rollout_for_session(sid_run)
            codex_box["prov"] = provenance_from_rollout(roll) if roll else {}
            codex_box["prov_why"] = roll_why
            # The structured answer goes through OUR renderer into the one markdown contract every
            # parser in this file reads. `out` is JSONL events, not the review.
            body = ""
            try:
                body = open(last_msg).read()
            except OSError as e:
                codex_box["body_why"] = f"{type(e).__name__}: {e}"
            rendered = render_review(body)
            codex_box.update(rc=rc, out=rendered or out, events=out)
            # written as soon as it finishes, so a later crash still leaves the review on disk
            open(os.path.join(GATES, f"{item_id}.codex.txt"), "w").write(rendered or out)
        except Exception as e:  # noqa: BLE001 — a review that cannot run is a FAIL, never a pass
            codex_box["error"] = f"{type(e).__name__}: {e}"

    codex_thread = None
    wall = recorded_wall() if not nocodex else None
    if wall:
        # Skip the call, not the row: fail-closed stands and the row is still a FAIL.
        codex_box["skipped_wall"] = wall
    elif not nocodex:
        codex_thread = threading.Thread(target=run_codex, name=f"codex-{item_id}", daemon=True)
        codex_thread.start()

    # (3) targeted proofs on the box
    if nobox:
        # Two very different situations wore the same row until 2026-09-07. The daemon passes
        # --no-box to EVERY box-free item (dispatcher.py: `if no_box or not needs_box(...)`), so an
        # item that declares no .py proofs at all recorded `proofs: FAIL — not run` and could never
        # gate anything but FAIL — the portal row beside it might have measured the whole suite.
        # An item that DOES declare .py proofs and was run --no-box has real proofs nobody ran.
        # The first is not applicable; the second did not run. Neither is a failure, and only the
        # second is missing evidence, so only the second holds the verdict at INCOMPLETE.
        py_proofs = [p_ for p_ in (it.get("proof_files") or []) if str(p_).endswith(".py")]
        if py_proofs:
            rec("proofs", None, f"not run (--no-box) — {len(py_proofs)} declared .py proof file(s) "
                                f"went unmeasured: {', '.join(map(str, py_proofs[:4]))}")
        elif [f for f in files if f.startswith("portal/")] or \
                [p_ for p_ in (it.get("proof_files") or []) if str(p_).endswith((".ts", ".tsx"))]:
            run_proofs(it, wt, sha, head, item_id, rec, warn, report_rel, it.get("artifact"),
                       portal_touched=[f for f in files if f.startswith("portal/")], ctx=ctx)
        else:
            rec("proofs", UNDECLARED, UNDECL_FIX.format(what="proof_files"))
    else:
        run_proofs(it, wt, sha, head, item_id, rec, warn, report_rel, it.get("artifact"),
                   portal_touched=[f for f in files if f.startswith("portal/")], ctx=ctx)
    if not it.get("_cites_done"):
        # No scratch worktree existed (portal-only item, --no-box, or the worktree add failed), so
        # the sweep falls back to the lane worktree — which the daemon may already have dirtied
        # with the lane's NEXT build. Still worth running, and the row says which tree it read, so
        # a citation resolved here is never mistaken for one resolved at the candidate sha.
        run_citesweep(wt, item_id, report_rel, it.get("artifact"), sha, warn,
                      f"the LANE worktree {os.path.basename(wt)} (NOT a detached checkout at {sha[:10]})")

    # (4a cont.) collect the Codex review started before the box wait. Fail-CLOSED, unchanged: a
    # review that did not run, errored, timed out or produced no verdict line is a FAIL.
    codex_out, codex_v = "", ""
    if nocodex:
        rec("codex adversarial review", None, "not run (--no-codex)")
    elif codex_box.get("skipped_wall"):
        until, where = codex_box["skipped_wall"]
        rec("codex adversarial review", None,
            f"CODEX WALLED until {until:%b %-d, %Y %-I:%M %p} (not retried — wall recorded at "
            f"{where}) — NO review was performed, so nothing about this candidate was checked by "
            f"Codex. This is a reviewer outage, not a finding.")
    else:
        codex_thread.join(timeout=1800)
        if codex_thread.is_alive():
            rec("codex adversarial review", None,
                "NOT RUN — still running after 1800s; the gate joined and gave up waiting. No "
                "review of this candidate exists. It cannot merge: an unrun check is never a pass.")
        elif "error" in codex_box:
            rec("codex adversarial review", False, f"error {codex_box['error']}")
        else:
            rc, out = codex_box.get("rc", 1), codex_box.get("out", "")
            ok_review, verdict, blockers = codex_verdict(out)
            codex_out, codex_v = out, verdict or ""
            kind, why = codex_failure_kind(rc, out)
            retried = " (retried once)" if codex_box.get("retried") else ""
            if kind and not ok_review:
                # A WALL or an EMPTY answer is an OUTAGE, not a finding: nothing about this
                # candidate was examined, so there is nothing to fail it on. NOT RUN, which cannot
                # merge and cannot be auto-reworked. An `error` row above stays FAIL on purpose —
                # that one is an exception in OUR runner, a gate defect, and a defect should be loud.
                rec("codex adversarial review", None,
                    f"{why}{retried} — NO review was performed, so nothing about this candidate was "
                    f"checked by Codex. This is a reviewer outage, not a finding. rc={rc} ({len(out)} chars)")
            else:
                rec("codex adversarial review", rc == 0 and ok_review,
                    f"rc={rc} verdict={verdict or 'NONE FOUND (fail-closed)'}{retried} "
                    f"blockers={len(blockers)}{sorted(set(b.lower() for b in blockers))[:4]} ({len(out)} chars)")
            # The reviewer's IDENTITY is its own row whenever a call was ATTEMPTED — including the
            # outage branch above, where the call happened and produced nothing readable. It is a
            # separate question from what the review said, Plan 003 §6.3 accepts on it, and a row
            # that only appears when something is wrong is a row nobody learns to read.
            # NOT --no-codex and not a recorded wall: no call was made, so there is no identity to
            # record and the review row already says the check did not run.
            # A NOT MEASURED identity records as INCOMPLETE, which cannot merge. That is deliberate
            # and it is the fail-closed direction Plan 003 §8 asks for ("do not accept ... wrong
            # model identity"), but it means an unreadable session id stops merges rather than
            # quietly labelling the route — the loudest possible way to find out that this parsing
            # is wrong, which is what it should be while it is new.
            ok_route, route_why = route_row(codex_box.get("prov"), codex_box.get("prov_why", ""))
            rec("codex reviewer route", ok_route,
                f"pinned {ASTRA_MODEL}/{ASTRA_EFFORT} in the invocation; reviewed a detached "
                f"checkout at {sha[:10]}, not the lane worktree; {route_why}")
    # The review's frozen checkout is removed once, HERE — after the join, so it survives the wall
    # retry, and outside the thread, so a thread that died still gets its tree cleaned up.
    drop_checkout(wt, codex_box.get("reviewed_in"))

    # (4b cont.) collect the second opinion and compare it with Codex. Joined AFTER the Codex block
    # so the two reviews overlap; the join is bounded because a hung reviewer must not hold the gate.
    disagree = ""
    if agy_thread:
        agy_thread.join(timeout=1200)
        agy_v = agy_box.get("v") or {"error": "agy still running after the gate finished (joined at 1200s)"}
        try:
            import gatereview2
            codex_ran = bool(codex_out or codex_v)
            disagree = gatereview2.disagreement(agy_v, codex_v, codex_out, codex_ran)[1]
            open(os.path.join(GATES, f"{item_id}.agy.txt"), "w").write(
                gatereview2.render(item_id, agy_v, codex_v, codex_out, disagree))
            warn(gatereview2.row(agy_v, disagree, codex_ran))
        except Exception as e:  # noqa: BLE001 — fail-OPEN
            warn(f"agy second opinion: agy: unavailable — {type(e).__name__}: {e}")
    else:
        warn("agy second opinion: SKIPPED — --no-agy")

    # The verdict, and the reason a check is missing travels WITH it (BOSS's two rules, 2026-09-07):
    # a NOT RUN is never counted as a pass anywhere, and the word PASS never reaches a human who
    # would read it as "this was proven".
    #
    # BOSS offered "PASS (proofs not run)" as an acceptable form; this is the stronger of the two he
    # named. A bare-eyed reader skims the first word, and every tool that has ever counted outcomes
    # here — autogate, the checkpoint reporter, a grep on events.log — keys on it too. INCOMPLETE
    # cannot be misread as proven by either. Overrule me and it is a one-line change.
    # Both reasons travel, and they are worded differently because they are ACTED ON differently: an
    # unrun check is re-run, an undeclared one is fixed in the queue row. The VERDICT WORD stays
    # INCOMPLETE for both — autogate, the checkpoint reporter and every grep on events.log key on
    # that word, and inventing a fifth one would make an unproven gate invisible to all three until
    # each learned it. The distinction lives in the parenthetical and in the row.
    verdict, why = verdict_of(state["ok"], state["not_run"], state["undeclared"])
    # Stamp the code that produced this verdict. A running gate uses the image it started with, so
    # after any fix there is a window where a PASS means something older (2026-09-05: telling the
    # fail-closed gates from the absence-of-tokens ones needed process start times vs file mtime).
    # KEEP THIS BLOCK LAST. The run log and .codex.txt above are each written as soon as their own
    # step finishes, so a crash here (2026-09-05: a missing `import hashlib` on this very stamp)
    # still leaves both on disk — BOSS recovered org-deletion r3 and member-egress r2 entirely from
    # those files after ~10 box-minutes each were otherwise about to be thrown away. Writing the
    # .md earlier "to be safe" would remove the one property that made that recovery possible.
    # ALL THREE MODULES, not just this file (BOSS, 2026-09-05 17:2x). The stamp used to hash
    # mergegate.py alone, so the 17:14 fix to gatereview2.py's diff packing — the one that turned a
    # reviewer reading only pytest logs back into a reviewer reading the code — moved nothing in
    # any gate file. A stamp that covers some of the code it vouches for reads exactly like one
    # that covers all of it. Per-module hashes as well as the combined one, so a reader can tell
    # WHICH module changed rather than only that something did.
    # Never raises: an unreadable sibling degrades to MISSING!. This block is LAST and a crash here
    # destroys the .md after the proofs and reviews are already paid for — which is precisely what
    # a missing `import hashlib` did to two gates earlier today.
    stamp = code_stamp()
    with open(os.path.join(GATES, f"{item_id}.md"), "w") as f:
        f.write(render_gate_md(item_id, verdict, sha, lane, wt, stamp, checks))
    it.update({"status": "gated", "gate": verdict, "gate_sha": sha, "gated_at": now(),
               "gate_not_run": list(state["not_run"]),
               "gate_undeclared": list(state["undeclared"])})
    # DISAGREE rides on the GATE line itself: BOSS reads events.log, and a disagreement that only
    # appears inside the .md is one BOSS has to already suspect before going to look for it.
    # dispatched_at rides on the GATE line (BOSS, 2026-09-07): splitting the last N gates by when
    # their item was DISPATCHED — before or after a rule change — is the only way to tell whether the
    # rule moved the outcome, and events.log carries the gate time, never the dispatch time.
    # The EVENT KIND is the one thing counted by tools, so it carries the distinction rather than
    # burying it in the detail: GATE_PASS means every check ran and passed, and nothing else does.
    kind = "GATE_FAIL" if not state["ok"] else (
        "GATE_INCOMPLETE" if (state["not_run"] or state["undeclared"]) else "GATE_PASS")
    emit(kind, item_id,
         f"dispatched_at={it.get('dispatched_at') or '-'}; "
         + "; ".join(f"{n_}={s_}" for n_, s_, _ in checks if n_)
         + (f"; DISAGREE ({disagree})" if disagree else ""))

    # The merge itself happens only under AUTOPILOT; otherwise BOSS merges by hand from the gate file.
    # Codex-built items are NEVER auto-merged regardless of the flag (BOSS ruling 2026-09-05 13:25): the
    # gate cannot prove Rule 7 for a builder that never watched its own removal control go red.
    codex_built = str(it.get("dispatched_to", "")).startswith("CODEX") or it.get("executor") == "CODEX"
    if state["ok"] and (state["not_run"] or state["undeclared"]) and autopilot:
        emit("MERGE_SKIPPED", item_id,
             f"INCOMPLETE — nothing failed, but {why}; a check that produced no result is not a "
             f"pass and is never merged on. BOSS decides.")
    mergeable = state["ok"] and not state["not_run"] and not state["undeclared"]
    if mergeable and autopilot and codex_built:
        emit("MERGE_SKIPPED", item_id, "Codex-built: BOSS reads diff + §11 and merges by hand (ruling 13:25)")
    elif mergeable and autopilot and hand_merge:
        emit("MERGE_SKIPPED", item_id, f"denylisted files touched {denied}: BOSS merges by hand")
    if mergeable and autopilot and not codex_built and not hand_merge:
        _, st = sh(["git", "status", "--short", "-uno"], cwd=TRUNK)
        if st.strip():
            emit("MERGE_SKIPPED", item_id, "trunk has tracked modifications; BOSS merges by hand")
        else:
            rc, out = sh(["git", "merge", "--no-ff", sha, "-m",
                          f"merge({lane}): {item_id} — mechanical gate PASS\n\nGate file test-logs/driver/gates/{item_id}.md"],
                         cwd=TRUNK)
            if rc != 0:
                sh(["git", "merge", "--abort"], cwd=TRUNK)
                emit("MERGE_CONFLICT", item_id, out[-200:].replace("\n", " "))
            else:
                _, msha = sh(["git", "rev-parse", "--short", "HEAD"], cwd=TRUNK)
                it.update({"status": "merged", "merge_sha": msha.strip(), "merged_at": now()})
                emit("MERGED", item_id, msha.strip())

    # Write under the shared flock and against a FRESH read: this run took minutes, and the daemon and
    # queuectl have edited queue.json since it was loaded — writing the stale copy would clobber them.
    import fcntl
    with open(QUEUE + ".lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        fresh = json.load(open(QUEUE))
        for cur in fresh["items"]:
            if cur["id"] == item_id:
                cur.update({k: it[k] for k in ("status", "gate", "gate_not_run", "gate_undeclared", "gate_sha", "gated_at", "merge_sha", "merged_at",
                                            "proof_files") if k in it})
        json.dump(fresh, open(QUEUE + ".tmp", "w"), indent=1)
        os.replace(QUEUE + ".tmp", QUEUE)
    print(verdict)
    for n_, s_, d_ in checks:
        print(f"  {d_}" if n_ is None else f"  {n_}: {s_} — {d_}")
    # 0 PASS, 1 FAIL, 3 INCOMPLETE. A caller that checks `rc == 0` must not read an
    # unrun check as success — that is the same mistake in shell form.
    return 0 if (state["ok"] and not state["not_run"] and not state["undeclared"]) \
        else (3 if state["ok"] else 1)


if __name__ == "__main__":
    sys.exit(main())
