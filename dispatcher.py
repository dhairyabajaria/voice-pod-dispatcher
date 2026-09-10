#!/usr/bin/env python3
"""Dispatcher — always-on driver for the Voice Pod executor sessions.

No model calls. Polls the local OpenCode server, classifies every executor turn-end, auto-continues
progress-line stops (bounded), queues REPORT READY / QUESTION / ERROR / STUCK events for BOSS in
events.log + pending.json, and escalates by age. See README.md next to this file.

Usage: dispatcher.py            run forever (launchd / nohup)
       dispatcher.py --once     one tick, then exit
       dispatcher.py --dry-run  never post prompts, never notify, never persist (combine with --once)
       --ignore-stop            dry-run only: exercise a tick even while the STOP file pauses the live daemon
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

# Its own directory, so `import museadapter` works when this file is loaded BY PATH rather than as a
# module on sys.path — which is how every test in tests/ loads it (spec_from_file_location). Without
# this the wiring imported fine for the daemon and raised ModuleNotFoundError in eleven existing
# test files at once. My own wiring test passed throughout, because it put the directory on the path
# itself: a fixture that repairs the condition it is meant to observe.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import museadapter
import sessionwatch
import pglock
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # launchd starts this with an unrelated cwd; sibling modules must still import
CN = os.environ.get("CN", "/Users/dhairyabajaria/Claude Code/Calling New")
BASE = os.environ.get("OPENCODE_BASE", "http://127.0.0.1:4096")
ROSTER_PATH = os.path.join(HERE, "roster.json")
STATE_DIR = os.path.join(CN, "test-logs", "driver")
EVENTS = os.path.join(STATE_DIR, "events.log")
PENDING = os.path.join(STATE_DIR, "pending.json")
ESCALATIONS = os.path.join(STATE_DIR, "escalations.log")
INBOX = os.path.join(STATE_DIR, "OWNER_INBOX.md")
LOG = os.path.join(STATE_DIR, "dispatcher.log")
STATE = os.path.join(STATE_DIR, "state.json")
HEARTBEAT = os.path.join(STATE_DIR, "heartbeat")
STOP = os.path.join(STATE_DIR, "STOP")
# TWO SWITCHES, and the reason they are two is that a kill switch has to be explicable in ONE
# SENTENCE (BOSS, 2026-09-07). STOP means EVERYTHING OFF, observation included — if it is present
# because the opencode server is sick, the last thing wanted is a daemon still polling fifteen
# sessions. "Mostly off" is a state nobody can reason about at hour ten.
#
# OBSERVE is the weaker hold, and it exists because BOSS reached for the emergency switch to get a
# routine effect: he wanted NO NEW GATES while a broken image sat on disk, not everything stopped.
# Under OBSERVE the SENSORS RUN and the ACTUATORS DO NOT — poll, classify, emit REPORT_READY and
# QUESTION events, escalate; post nothing, dispatch nothing, auto-continue nothing, launch no gates.
# On 2026-09-07 two Codex audits carrying three [high] findings finished at 08:35 under STOP and
# surfaced at 08:57; under OBSERVE they would have surfaced at 08:35.
#
# STOP ALWAYS WINS. It is checked first and returns before OBSERVE is consulted.
OBSERVE = os.path.join(STATE_DIR, "OBSERVE")
HOLD_DIR = os.path.join(STATE_DIR, "hold")
# `dispatcherctl.sh clear` drops one request file here per row BOSS wants cleared. It is a request,
# not an edit: pending lives in the daemon's in-memory state and is rewritten every tick, so a file
# edited from outside would be silently overwritten by the next save_state() — the single writer has
# to stay the daemon (2026-09-06).
CLEAR_DIR = os.path.join(STATE_DIR, "clear")
# Same request-file protocol for `dispatcherctl.sh gate`: ctl validates and writes, the daemon
# launches and — crucially — is then the process watching for the gate file it produced.
GATE_REQ_DIR = os.path.join(STATE_DIR, "gatereq")
# Same protocol again for `dispatcherctl.sh answer`. A QUESTION parks its item so the question waits
# for BOSS; answering it by hand through the opencode API left the park in place four times on
# 2026-09-07, and `parked` is not gateable — EXEC-J built a finished artifact whose REPORT READY the
# daemon then skipped with "holds no dispatched, rework or reported item". The post and the un-park
# have to be ONE operation or they drift, and only the daemon may write the queue row.
ANSWER_REQ_DIR = os.path.join(STATE_DIR, "answerreq")
HOLDCLEAR_DIR = os.path.join(STATE_DIR, "holdclear")
# Queue feeding (owner order 2026-09-05): BOSS writes queue.json + items/<id>.md; the daemon hands the next
# eligible item to an executor the moment it goes idle, so no executor waits on a round-trip for work.
QUEUE = os.path.join(STATE_DIR, "queue.json")
QUEUE_LOCK = QUEUE + ".lock"  # flock held by the daemon across load→save of a tick, and by queuectl.py for every edit
ITEMS_DIR = os.path.join(STATE_DIR, "items")
GATES_DIR = os.path.join(STATE_DIR, "gates")
QUEUE_HOLD = os.path.join(STATE_DIR, "hold", "QUEUE")  # touch to stop feeding without stopping the daemon
# Pre-build PLAN review (owner-approved 2026-09-05, change 1). OFF unless this file exists, so the
# whole feature is reversible with one `rm` and cannot strand executors if it misbehaves.
PLANREVIEW_FLAG = os.path.join(STATE_DIR, "PLANREVIEW")
LEDGER = os.path.join(CN, "voicepod-plan010-rebuild", "plans", "EXECUTION_LEDGER.md")
ACK_RE = re.compile(r"ACK REOPEN")  # reply to the 2026-09-05 reopen order: idle and fed, never escalated
FEED_KINDS = ("REPORT_READY", "QUESTION", "ACK")

# What `_report_verdict[1]` can say about a REPORT_READY, and what each one does to the pending row
# BOSS reads. Until 2026-09-07 there were only True and False, and FALSE WAS DOING TWO JOBS: "the
# trigger judged this text is not a report at all" (an idle ack — withdraw the row, nobody needs to
# see it) and "this IS a report and there was nothing to gate it against" (a finished artifact with
# nothing pointing at it — the case that most needs a human). The second wore the first's clothes and
# rendered as IDLE_ACK, so a real report disappeared from the board silently. Three of them did, on
# 2026-09-07, and each was found by hand.
#   False        the trigger's own judgement: not a report -> row withdrawn, IDLE_ACK
#   GATE_RUNNING a gate for that item is already running   -> row withdrawn, REPORT_WHILE_GATED
#                (a verdict IS coming on its own; BOSS has nothing to do)
#   UNGATEABLE   a real report nothing could gate          -> ROW KEPT, and it escalates
#   True         gated, or a real report held for paperwork -> row kept, as before
GATE_RUNNING = "gate-running"
UNGATEABLE = "ungateable"
CODEX_DIR = os.path.join(STATE_DIR, "codex")  # one log per Codex run
CODEX_PROMPT = (
    "You are {slot}, a Codex executor in the Voice Pod program. Before anything else read, in this order: "
    "{trunk}/plans/OPENCODE_EXECUTION_HANDOFF_2026-09-04.md Parts 0, 2, 3, 5 (the absolute rules, how to run tests "
    "under the one-box protocol, git rules, the report format), then {trunk}/HANDOFF_2026-09-05.md. You work ONLY in "
    "this worktree ({worktree}); test-logs and the repository's .git are writable for logs and commits. "
    "YOUR SANDBOX CANNOT RUN THE BOX (BOSS ruling 2026-09-05 12:45): `ps`, `ipcs` and `sysctl` are denied here, so the "
    "Part 2.2 preflight is impossible and you must NEVER start a Postgres-backed pytest, never touch box.lock.d, and never "
    "ask a QUESTION about it. Your contract is BUILD, BOX-FREE: read, design, write product code and the proof tests, run only "
    "box-free checks (`./.venv/bin/python -m pytest --collect-only -q <files>`, `--setup-plan`, `./.venv/bin/ruff check`) — "
    "but NEVER collect `tests/test_rls_completeness.py`: its module-level `_discover_at_collection_time()` boots a "
    "disposable Postgres and replays every migration AT COLLECTION, so collecting it is a BOX ACTION even though it "
    "prints like an ordinary collect. Pass `--ignore=tests/test_rls_completeness.py` whenever you collect a directory "
    "or any set that could include it (BOSS ruling 2026-09-05 14:10), "
    "commit with explicit paths, and end with `REPORT READY` whose report §6 says `PROOFS NOT RUN (Codex box-free)` and "
    "§15 lists the exact proof files; the mechanical merge gate runs them on the box and returns REWORK if they red. "
    "platform/.venv is already provisioned (do not run uv). Never `git add -A`, never `git stash`. Your FINAL message must "
    "begin with `REPORT READY` (report written per Part 5.3 and committed) or with a `QUESTION` block about the ITEM itself; "
    "nothing else counts as a turn end.\n\n"
)  # WAITING holds a BOSS gate (Part 7) — never fed over; ERROR/STUCK go to BOSS  # BOSS: `echo "<reason>" > hold/EXEC-X` suppresses escalation for that executor's current item

REPORT_RE = re.compile(r"REPORT READY")
PLAN_RE = re.compile(r"(^|\n)\s*\**PLAN READY\**")
# Unanchored twin of PLAN_RE, the mirror of REPORT_RE. Only consulted when NEITHER marker is
# at a line start; see classify().
PLAN_ANY_RE = re.compile(r"PLAN READY")
# Both markers are instructed to be the FIRST line of the turn's final message (Part 5.1a / 5.3),
# so classification compares which one appears EARLIEST at a line start, not merely "present
# anywhere". A plan block routinely narrates its own later steps ("...then REPORT READY when
# done"), and a bare `.search()` for REPORT READY anywhere in the text made that narrative outrank
# the actual PLAN READY stop marker (BOSS caught this live on EXEC-F 2026-09-05 15:12: the plan
# began "PLAN READY — ..." but later said "REPORT READY then wait for MERGE-APPROVED", and the old
# `PLAN_RE.search(text) and not REPORT_RE.search(text)` guard turned that into a false REPORT_READY
# — the item flipped to reported with no sha and the executor was dispatched onto the next item
# with no review ever posted).
REPORT_LINE_RE = re.compile(r"(^|\n)\s*\**REPORT READY\**")
QUESTION_RE = re.compile(r"\bQUESTION\b")
# Legitimate "stop and wait" endings ordered by a BOSS verdict (2026-09-04 23:14 false positive on EXEC-F):
# these are proper stops, filed as WAITING — never auto-continued, never escalated.
WAITING_RE = re.compile(r"WAITING ON GATE|REWORK READY|(^|\n)\s*\**STOP\**\s*($|\n)")
SERVER_DOWN_AFTER_S = 30

# Posted ONCE per stall episode (BOSS's wording, 2026-09-05). It tells the session to trust the
# FILES rather than its own memory of the cut turn, and — because a cut can land mid-box-run —
# to re-verify its lock token before touching Postgres. Never re-posted inside the re-resume
# window: see check_stall.
STALL_RESUME_PROMPT = (
    "RESUME (dispatcher, {hhmm} IST): your previous turn was cut off by a provider error; "
    "nothing on disk is lost; re-read your item prompt, measure your worktree (git status, "
    "git log -3, your test-logs) and continue from what the FILES show; if you held the box "
    "lock, re-verify your token in test-logs/box.lock.d/owner before any run and re-acquire if "
    "it is not yours; IF YOU POSTED A `PLAN READY` OR `REPORT READY` BLOCK IN THE CUT TURN, "
    "RE-POST IT VERBATIM as the first thing in this turn — the dispatcher reads only the LAST "
    "message in the session, so a block that landed behind this resume was never seen and no "
    "review was run on it; end on REPORT READY / QUESTION."
)

# A provider 503 is not a stalled turn and not a dead session: the turn ENDED, carrying an error
# the provider itself marks retryable. Same body as the stall resume, one clause added so the
# executor knows why it was interrupted (BOSS wording, 2026-09-05 18:5x).
ERROR_RESUME_PROMPT = STALL_RESUME_PROMPT.replace(
    "your previous turn was cut off by a provider error;",
    "the provider returned a transient error and your previous turn was cut off;")

CONTINUE_PROMPT = (
    "DISPATCHER (mechanical relay, not BOSS): your last turn ended on a progress line without "
    "REPORT READY or a QUESTION block. Verify your on-disk state first (git status --short, "
    "git log -1 in your worktree) and continue the current item from where the disk says you are. "
    "End only at REPORT READY or a QUESTION block; poll long runs inside the turn. "
    "This is auto-continue {n} of {max}; after {max} BOSS is escalated instead."
)


PLAN_FIRST = (
    "\nPLAN REVIEW IS ACTIVE FOR THIS ITEM — see HANDOFF Part 5.1a `The PLAN block answers five questions` "
    "(ratified 2026-09-05, trunk 23520433), which is the authority for this block. Do NOT write code yet. End "
    "THIS turn at `PLAN READY` followed by your PLAN block, and answer these five explicitly — they are "
    "the five of Part 5.1a, which have actually caused rework here: (1) what PRE-EXISTING rows does this not reach; "
    "(2) for each write in this path, does a failure fail open or closed; (3) is every new function/trigger "
    "actually called from production, and by what; (4) if the process dies between two steps, what state is "
    "left and what reconciles it; (5) what does your named proof claim that it would not actually "
    "demonstrate. `n/a` with a reason is a valid answer. An automated reviewer replies within ~2 minutes "
    "with any gaps; you then build in the SAME session. Do not take the box or commit anything this turn.\n"
)


# The box recipe lives HERE, in the prompt every box-capable executor actually receives, and not
# only in plans/OPENCODE_EXECUTION_HANDOFF Part 2.2 — which carries the PRE-FLIGHT checks (census,
# ipcs, memory, clean tree) but has never carried an acquire/release recipe at all. Between 17:02
# and 17:12 on 2026-09-05 that gap cost four in-place `owner` overwrites (EXEC-E, WORKER-1, EXEC-F)
# and one pytest that ran six minutes holding no lock, because each executor worked from whatever
# it remembered. Rules rewritten by BOSS at 17:20 in test-logs/BOX-QUEUE.md; same rules, delivered
# where they cannot be missed.
# It is a FIELD, not a constant spliced into ITEM_PROMPT, because ITEM_PROMPT is also used to build
# the Codex prompt — and CODEX_PROMPT orders Codex executors to never touch box.lock.d at all.
# Handing them a detailed acquire recipe in the same message would be a contradiction, so the Codex
# call site passes "".
BOX_RECIPE = (
    "BOX LOCK — READ THIS EVEN IF YOU THINK YOU KNOW IT. These rules were rewritten 2026-09-05 17:20 "
    "(test-logs/BOX-QUEUE.md, which WINS over any older text or any recipe you remember) after four "
    "in-place owner overwrites and one six-minute lockless pytest between 17:02 and 17:12.\n"
    "1. ACQUIRE ATOMICALLY, in ONE shell line — never `mkdir -p`, and never a gap between the mkdir "
    "and the owner write (a two-minute gap at 17:02 made a LIVE lock look stale and three takers "
    "overwrote it):\n"
    "     mkdir \"$CN/test-logs/box.lock.d\" && echo \"<your-unique-token> $(date -u +%FT%TZ)\" > \"$CN/test-logs/box.lock.d/owner\"\n"
    "   The `mkdir` FAILING is the lock working. If the && does not run, you do not have the box.\n"
    "2. IF THE DIRECTORY EXISTS — with or WITHOUT an owner file — you did not get it. WAIT and retry "
    "at 30-second polls. An owner-less dir is indistinguishable from a slow acquirer, so you cannot "
    "tell stale from live: NEVER reclaim it, NEVER write `owner` into a dir you did not create, "
    "NEVER overwrite another token. Only BOSS reclaims. Log the observation and keep polling.\n"
    "3. CENSUS AFTER ACQUIRING, not before: `/usr/bin/python3 \"$CN/live_pytest.py\"`. Gate on the "
    "PARSED `count:` line, never on the exit status — it exits 0 whether the box is busy or free. "
    "If it is not `count: 0`, someone is running without the lock: RELEASE your own token and go "
    "back to polling. Do not run. This census is the last guard and it is the one that held today.\n"
    "4. RELEASE = `rm` the owner file, then `rmdir` the directory — only while the token in it is "
    "still YOURS, and never a bare `rm -rf`.\n"
    "5. HOLD ONLY WHILE A RUN IS LIVE. Edit cycles between runs happen WITHOUT the lock: release, "
    "edit, re-acquire at 30-second polls. A long hold with `count: 0` starves everyone queued behind "
    "you and will be named in your report's §5.\n"
)

ITEM_PROMPT = (
    "DISPATCHER QUEUE ITEM {id} — AUTHORIZED WORK under the owner's reopen order of 12:06 IST 2026-09-05 "
    "(test-logs/BOSS-QUEUE-ORDER.md and the REOPEN message in this session), which supersedes the 06:48 close-out. "
    "The queue file is test-logs/driver/queue.json (cat it if in doubt). {parked}Start this item now under OPENCODE_EXECUTION_HANDOFF Part 5 (PLAN block first, "
    "report per 5.3 as reports/{id}-<artifact>.md, end at REPORT READY or a QUESTION block). "
    "Worktree: {worktree}. Lane: {lane}. Scope (the merge gate refuses product files outside these globs; your report, logs, "
    "resume index, TECHNICAL.md and your own ledger row are ALWAYS allowed): {scope}. "
    "Proof files (targeted, never a sweep — Part 2.1): {proof}.\n{box}{planfirst}\n{body}"
)


# ----------------------------------------------------------------------------- helpers
def now_local():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ms_local(ms):
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")
    except Exception:
        return "?"


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def http(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(BASE + path, method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else None


def text_of(m):
    """Full text of a message. `classify` returns only a short excerpt, but a plan review needs
    the whole block — reviewing a truncated plan would flag its missing tail as a gap."""
    return " ".join(p.get("text", "") for p in m.get("parts", []) if p.get("type") == "text").strip()


def clean_excerpt(text, n=300):
    text = re.sub(r"[\t\r\n]+", " ", text)
    text = re.sub(r"[^\x20-\x7e -￿]", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:n]


# ----------------------------------------------------------------------------- daemon
def pglock_board_lines(lock, runs, pop_state=None, notes=None):
    """The pgserver lock row. An UNMEASURED probe says so; it never renders as free.

    The RUN rows below it are the ITEM 4 change. They used to come from `state["codex"]` — this
    daemon's own spawns — so a box run nobody here started could not appear at all, and an empty
    list rendered as silence. Now the population comes from the box, and its ABSENCE has two
    different sentences: NOT MEASURED (we could not look) and none observed (we looked).
    """
    if lock.get("state") == pglock.UNMEASURED:
        out = [f"  pgserver lock: NOT MEASURED — {lock.get('why', '')[:90]}"]
    elif lock.get("state") == pglock.HELD:
        out = [f"  pgserver lock: HELD by pid {lock.get('pid')} — every postgres start/stop on this "
               f"machine queues behind it, no timeout"]
    else:
        out = ["  pgserver lock: free"]
    if pop_state == pglock.UNMEASURED:
        out.append("    runs: NOT MEASURED — the marker root could not be read, so this says "
                   "nothing about what is running")
    elif pop_state == pglock.NO_RUNS:
        out.append("    runs: none observed on the box")
    for r in runs:
        who = r.get("slot") or "?"
        if r["state"] != pglock.LIVE:
            out.append(f"    {who} pid {r['pid']} {r['state']}: {r['why'][:110]}")
        else:
            out.append(f"    {who} pid {r['pid']} LIVE — {r.get('item', '')[-58:]}")
    for n in (notes or []):
        out.append(f"    note: {n[:120]}")
    return out


class Dispatcher:
    def __init__(self, once=False, dry=False):
        self.once = once
        self.dry = dry
        os.makedirs(STATE_DIR, exist_ok=True)
        os.makedirs(HOLD_DIR, exist_ok=True)
        os.makedirs(CLEAR_DIR, exist_ok=True)
        os.makedirs(GATE_REQ_DIR, exist_ok=True)
        os.makedirs(ANSWER_REQ_DIR, exist_ok=True)
        os.makedirs(HOLDCLEAR_DIR, exist_ok=True)
        os.makedirs(ITEMS_DIR, exist_ok=True)
        os.makedirs(GATES_DIR, exist_ok=True)
        os.makedirs(CODEX_DIR, exist_ok=True)
        if not (once or dry):
            # single-instance lock: a second daemon (launchd, nohup, BOSS) exits at once instead of double-prompting
            import fcntl
            self._lock = open(os.path.join(STATE_DIR, "dispatcher.lock"), "w")
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print("another Dispatcher already holds test-logs/driver/dispatcher.lock — exiting", file=sys.stderr)
                sys.exit(0)
            self._lock.write(f"{os.getpid()}\n")
            self._lock.flush()
        self.cfg = load_json(ROSTER_PATH, {})
        self.state = load_json(STATE, {})
        self.state.setdefault("handled", {})
        self.state.setdefault("auto", {})
        self.state.setdefault("pending", {})
        self.state.setdefault("server_down_emitted", False)
        self._spawn_refusal = {}
        self.state.setdefault("codex", {})
        self.state.setdefault("parks", {})
        self.started_ms = int(time.time() * 1000)
        self.roster = {}
        self.dirs = {}
        self.session_seen = {}      # sid -> (session time.updated ms, when we read it)
        self.last_resync = 0
        self.server_down_since = None
        self.paused_logged = False
        self.observe_logged = False
        self.observing = False

    # -- config
    def c(self, key, default):
        return self.cfg.get(key, default)

    # -- output
    def log(self, msg):
        line = f"{now_local()} {msg}"
        if self.once or self.dry:
            print(line)
        if self.dry:
            return
        try:
            if os.path.exists(LOG) and os.path.getsize(LOG) > 5 * 1024 * 1024:
                os.replace(LOG, LOG + ".1")
            with open(LOG, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def emit(self, *fields):
        line = "\t".join([now_local()] + [str(x) for x in fields])
        self.log("EVENT " + line.replace("\t", " | "))
        if self.dry:
            return
        with open(EVENTS, "a") as f:
            f.write(line + "\n")

    def notify_owner(self, title, text):
        self.log(f"NOTIFY {title}: {text}")
        if self.dry:
            return
        try:
            with open(INBOX, "a") as f:
                f.write(f"- {now_local()} **{title}** — {text}\n")
        except Exception:
            pass
        try:
            safe = text.replace('"', "'")[:200]
            subprocess.run(
                ["osascript", "-e", f'display notification "{safe}" with title "Dispatcher: {title}"'],
                timeout=5, capture_output=True,
            )
        except Exception:
            pass

    def escalate(self, text):
        self.log(f"ESCALATE {text}")
        if self.dry:
            return
        with open(ESCALATIONS, "a") as f:
            f.write(f"{now_local()}\t{text}\n")

    # -- opencode
    def post_prompt(self, sid, text):
        """POST a prompt to a session. -> True if it was delivered, False if it was NOT.

        THE FAILURE SIGNAL IS THE RETURN VALUE, NEVER AN EXCEPTION. Every failure below is caught
        here, so a caller written as `try: self.post_prompt(...) except Exception:` has a handler
        that can never run. apply_answer_requests was exactly that shape: an undelivered answer
        un-parked its row and emitted ANSWERED, and the record said an executor had been told
        something nobody told it. Callers must check the result.

        The reason is recorded on the instance because it existed and went only to the log — the
        caller could tell THAT a post failed but not WHY, so the event it wrote for BOSS had to be
        vague about the one thing he needed to act on.
        """
        self._last_post_error = ""
        if getattr(self, "observing", False):
            self.log(f"OBSERVE-ONLY: would POST to {sid}: {text[:60]}...")
            self._last_post_error = ("the daemon is in OBSERVE-ONLY mode, so no post was attempted "
                                     "at all — this is a held actuator, not a delivery failure")
            return False
        if self.dry:
            self.log(f"DRY-RUN would POST prompt_async to {sid}: {text[:80]}...")
            return True
        body = {"parts": [{"type": "text", "text": text}]}
        if self.c("opencode_model", None):
            body["model"] = self.c("opencode_model", None)  # a session created over the API otherwise gets the server default (no tool support)
        try:
            http("POST", f"/session/{sid}/prompt_async", body, timeout=15)
            self.log(f"POSTED prompt_async to {sid}")
            return True
        except Exception as e:
            self.log(f"POST FAILED to {sid}: {e}")
            self._last_post_error = f"{type(e).__name__}: {e}"
            return False

    def roster_refresh(self):
        explicit = dict(self.c("executors", {}))
        discovered = {}
        try:
            sessions = http("GET", "/session?limit=200", timeout=10) or []
            prefix = self.c("auto_discover_prefix", "EXEC-")
            known = set(explicit.values())
            for s in sessions:
                sid = s.get("id")
                self.dirs[sid] = s.get("directory", "")
                # SESSION-LEVEL liveness, kept here because this listing already carries it: a
                # session that never begins a turn has no message to read, so `time.updated` is the
                # only thing that moves. Stored with the time we read it, because a value refreshed
                # every resync_seconds is up to that stale and a silence figure that hides its own
                # measurement age is the wrong kind of confident.
                upd = ((s.get("time") or {}).get("updated"))
                if upd:
                    if not hasattr(self, "session_seen"):
                        self.session_seen = {}
                    self.session_seen[sid] = (float(upd), time.time())
                title = s.get("title") or ""
                if prefix and title.startswith(prefix) and sid not in known:
                    name = title.split()[0]
                    if name not in explicit and name not in discovered:
                        discovered[name] = sid
        except Exception as e:
            self.log(f"roster discovery failed: {e}")
        new = {**discovered, **explicit}
        if new != self.roster:
            self.log("roster: " + ", ".join(f"{k}={v[-8:]}" for k, v in sorted(new.items())))
        self.roster = new
        self.last_resync = time.time()

    def last_message(self, sid):
        try:
            msgs = http("GET", f"/session/{sid}/message?limit=1", timeout=10) or []
            return msgs[-1] if msgs else None
        except Exception as e:
            self.log(f"message fetch failed for {sid}: {e}")
            return "ERR"

    # OpenCode's `question` TOOL. EXEC-D asked BOSS a question through it at 01:14 instead of
    # writing one in text: the turn stays INCOMPLETE while it waits for an answer, so the daemon
    # read BUSY and the board showed a working executor for an unknown length of time. A question
    # nobody is told about is the most expensive thing an executor can do — it is idle and looks
    # productive, which is worse than a stall, because a stall eventually escalates.
    QUESTION_WAITING_STATES = ("pending", "running", "waiting", "asking", "input-required")

    @staticmethod
    def question_part(m):
        """The waiting `question` tool part of an assistant message, or None.

        Only the NEWEST part counts: a question answered earlier in the same turn is history, and
        treating it as live would re-raise every question the turn ever asked.
        """
        if (m.get("info", m) or {}).get("role") != "assistant":
            return None
        parts = [p for p in (m.get("parts") or []) if isinstance(p, dict)]
        if not parts:
            return None
        last = parts[-1]
        if last.get("type") != "tool" or last.get("tool") != "question":
            return None
        st = last.get("state") or {}
        status = str(st.get("status") or "").lower()
        if st.get("output") or status in ("completed", "complete", "error", "cancelled", "canceled"):
            return None            # answered or dead: not waiting on anybody
        if status and status not in Dispatcher.QUESTION_WAITING_STATES:
            return None
        return last

    @staticmethod
    def render_question(part):
        """The question and its options as one readable block for the event and BOSS's queue."""
        qs = ((part.get("state") or {}).get("input") or {}).get("questions") or []
        out = []
        for q in qs:
            if isinstance(q, str):
                out.append(q)
                continue
            text = q.get("question") or q.get("text") or q.get("header") or ""
            opts = q.get("options") or []
            labels = [o.get("label") if isinstance(o, dict) else str(o) for o in opts]
            out.append(text + ("\n  options: " + " | ".join(l for l in labels if l) if labels else ""))
        return "\n".join(out).strip() or "(the question tool was called with no readable question text)"

    @staticmethod
    def classify(m, after_compaction=False):
        info = m.get("info", m)
        role = info.get("role")
        t = info.get("time", {}) or {}
        msg_id = info.get("id")
        completed = t.get("completed")
        finish = info.get("finish")
        err = info.get("error")
        text = " ".join(p.get("text", "") for p in m.get("parts", []) if p.get("type") == "text").strip()
        exc = clean_excerpt(text)
        if role == "user":
            # LOAD-BEARING, and it reads like a triviality. Every pattern below runs on message text,
            # and the text WE send is full of the markers they look for: the dispatch prompt says
            # "end with `REPORT READY`" in five places, names PLAN READY, QUESTION and STOP, and
            # quotes the handbook. Without this early return, every prompt we write would classify as
            # its own answer — a dispatch would register as the REPORT_READY it is asking for.
            # This is the second instance of the class (BOSS, 2026-09-07): our instruction text is
            # specific enough to match our own detectors. The first was a liveness grep counting
            # Codex processes whose PROMPT quoted `python -m pytest` as live pytest runs; the third
            # was working_item reading a rework that cited a sibling item. Anything here that
            # pattern-matches text we also author belongs behind a role check like this one.
            return "QUEUED", msg_id, t.get("created"), finish, exc
        qp = Dispatcher.question_part(m)
        if qp:
            # BEFORE the not-completed shortcut: a turn waiting on an answer is never completed, so
            # every question asked through the tool would otherwise be read as BUSY forever.
            return "QUESTION", msg_id, t.get("created"), finish, clean_excerpt(Dispatcher.render_question(qp))
        if not completed:
            return "BUSY", msg_id, t.get("created"), finish, exc
        if err:
            kind = "ERROR"
            exc = clean_excerpt(json.dumps(err)) if not exc else exc
        elif PLAN_RE.search(text) or REPORT_LINE_RE.search(text):
            pm, rm = PLAN_RE.search(text), REPORT_LINE_RE.search(text)
            # earliest LINE-START marker wins; a narrated mention of the other marker deeper in
            # the body (a plan's "then REPORT READY when done", a report quoting "PLAN READY" from
            # the handbook) never outranks the one the turn actually opened with.
            kind = "PLAN_READY" if (pm and (not rm or pm.start() < rm.start())) else "REPORT_READY"
        elif ACK_RE.search(text) and not REPORT_RE.search(text):
            kind = "ACK"
        elif (not after_compaction and PLAN_ANY_RE.search(text) and REPORT_RE.search(text)
              and PLAN_ANY_RE.search(text).start() < REPORT_RE.search(text).start()):
            # NEITHER marker is at a line start, and PLAN READY comes first. Measured 2026-09-07
            # 03:41:02: EXEC-D opened "Posting the PLAN block. PLAN READY: B.010.circleci-three-reds
            # -triage" — mid-line, so PLAN_RE missed it — and closed with the handbook's own "end
            # REPORT READY or QUESTION" at offset 1765. REPORT_RE matched that, and a PLAN was
            # filed as a report: no plan review, and had the plan named a report path that happened
            # to exist, a gate on an unbuilt item.
            #
            # The rule that fixes it without breaking the anchored one above: a line-start marker
            # still outranks everything (that branch runs first). Only when neither is anchored does
            # position decide, and then the marker the turn OPENED with wins — which is the same
            # principle, applied to a turn that did not start its line cleanly.
            #
            # `not after_compaction` is carried over from the REPORT branch below, and it was NOT
            # there in my first version: a post-compaction summary narrating both markers came out
            # PLAN_READY, which is the exact failure that guard exists to prevent — a description of
            # past work read as a claim about the present. Caught by the control in the test, not by
            # inspection.
            kind = "PLAN_READY"
        elif REPORT_RE.search(text) and not after_compaction:
            # REPORT_RE matches anywhere in the body, which is what makes it useful for a report
            # that does not open with the marker — and what made EXEC-F's post-compaction SUMMARY
            # register as REPORT_READY at 01:12: the summary NARRATED the marker while recounting
            # the session. (Measured: the "## Objective" heading alone does not match anything; the
            # quoted marker does, so requiring a literal token would have changed nothing.)
            # A summary is a description of past work, never a claim about the present.
            kind = "REPORT_READY"
        elif QUESTION_RE.search(text):
            kind = "QUESTION"
        elif WAITING_RE.search(text):
            kind = "WAITING"
        else:
            kind = "PROGRESS_STOP"
        return kind, msg_id, completed, finish, exc

    def poll_lockwatch(self):
        """One box-lock poll per tick, deferring to a running standalone `lockwatch.py --watch`.

        Never raises into the tick: a watchdog that can take the daemon down with it is worse than
        no watchdog. A dry run does everything except emit (lockwatch's emitter writes events.log
        directly, so dry runs are given a no-op emitter rather than being skipped — a code path a
        dry run returns before reaching is a path no dry run can test, per this file's own README).
        """
        try:
            import lockwatch
            if lockwatch.standalone_alive():
                if not getattr(self, "_lw_deferred", False):
                    self._lw_deferred = True
                    self.log("lockwatch: standalone --watch is running; daemon hook stands down "
                             "(kill it to hand over; they are mutually exclusive by pidfile)")
                return
            if getattr(self, "_lw_deferred", False):
                self._lw_deferred = False
                self.log("lockwatch: standalone gone, daemon hook taking over")
            if not getattr(self, "_lw", None):
                emit = (lambda *a: None) if self.dry else \
                    (lambda kind, sub, token, detail: self.emit(kind, "LOCK", sub, token or "-", detail))
                # A dry run gets NO state file: lockwatch.json is shared with the standalone and
                # with the real daemon, and a rehearsal that mutates the thing it is rehearsing
                # against can mark an episode fired so the live watcher never reports it.
                self._lw = lockwatch.LockWatch(emit, state_path=None if self.dry else lockwatch.STATE)
            self._lw.poll()
        except Exception as e:  # noqa: BLE001
            self.log(f"lockwatch poll error (ignored): {type(e).__name__}: {e}")

    # -- retryable provider errors (503/504/429), added 2026-09-05 after three overloaded turns
    @staticmethod
    def retryable_error(m):
        """-> (is_retryable, statusCode, short_reason) for the message's own error payload.

        MEASURED, not guessed (all seven error messages on the box at 18:5x): the provider's
        payload already carries `data.isRetryable`, and it is correct — 503 service_overloaded and
        504 server_error came back True, EXEC-F's dead-session 400 invalid_request_error came back
        False. So the primary test is the provider's own flag, not a status allowlist or a message
        regex; a regex over "overloaded" would have missed the 504 that actually happened tonight.
        The status check is a SECOND condition, not the first: a 4xx is never resumed even if the
        payload claims retryable, because retrying a malformed request reproduces it forever — and
        that is precisely the shape that killed EXEC-F's session.
        """
        info = m.get("info", m)
        err = info.get("error")
        if not isinstance(err, dict):
            return False, None, ""
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        status = data.get("statusCode")
        retry = data.get("isRetryable") is True
        msg = str(data.get("message", ""))[:120]
        if not retry:
            return False, status, f"provider says not retryable (status={status})"
        if not (isinstance(status, int) and (status >= 500 or status == 429)):
            return False, status, f"retryable flag but status={status} is not 5xx/429 — refusing"
        return True, status, msg

    def note_provider_error(self, status):
        """Rolling record of retryable provider errors, so BOSS can see the provider's state in
        pending.json without grepping the log."""
        hist = self.state.setdefault("provider_errors", [])
        hist.append({"t": time.time(), "status": status})
        cutoff = time.time() - 24 * 3600
        self.state["provider_errors"] = [h for h in hist if h.get("t", 0) >= cutoff]

    def provider_error_summary(self):
        hist = self.state.get("provider_errors", [])
        now_t = time.time()
        hour = [h for h in hist if h.get("t", 0) >= now_t - 3600]
        by = {}
        for h in hour:
            by[str(h.get("status"))] = by.get(str(h.get("status")), 0) + 1
        return {"last_hour": len(hour), "last_24h": len(hist), "by_status_last_hour": by,
                "last_at": ms_local(hist[-1]["t"] * 1000) if hist else None}

    def check_retryable_error(self, name, sid, m, when_ms, cur):
        """-> "grace" | "resumed" | None. None means: fall through to the normal ERROR path.

        Mirrors the stall episode rule deliberately: ONE resume, and a second retryable error on
        the same session inside the window escalates instead. A provider that is still overloaded
        will hand back the same 503 to the resume, and hammering it neither helps the provider nor
        tells BOSS anything.
        """
        ok, status, why = self.retryable_error(m)
        if not ok:
            return None
        item = cur["id"] if cur else "-"
        msg_id = m.get("info", m).get("id")

        # First sighting of THIS message. Two things depend on it, and both were wrong in the first
        # draft of this function (caught in calibration, not in review):
        #  1. the hourly counter — the grace path returns every tick, so counting on each pass
        #     turned one 503 into ~12 and would have told BOSS the provider was 12x worse than it is;
        #  2. the age — a message with no timestamp made `age` recompute to ~0 forever, so it sat in
        #     grace permanently: never resumed, never marked handled, never escalated. A silent hang
        #     is the worst outcome available here, worse than resuming early.
        seen = self.state.setdefault("err_seen", {})
        rec = seen.get(sid)
        if not rec or rec.get("msg") != msg_id:
            rec = {"msg": msg_id, "first_t": time.time()}
            seen[sid] = rec
            self.note_provider_error(status)

        # GRACE: the provider needs the gap. Deliberately do NOT mark this message handled while
        # waiting, so the next tick re-evaluates it; a 5-s poll means the resume lands within ~5 s
        # of the grace expiring.
        age = (time.time() - when_ms / 1000.0) if when_ms else (time.time() - rec["first_t"])
        grace = float(self.c("error_resume_grace_seconds", 60))
        if age < grace:
            return "grace"
        prev = self.state.setdefault("err_resumed", {}).get(sid)
        window = float(self.c("error_reresume_block_minutes", 10)) * 60
        if prev and time.time() - float(prev) < window:
            self.escalate(f"RETRYABLE PROVIDER ERROR again for {name} ({sid}) item={item} status={status} "
                          f"{int((time.time() - float(prev)) / 60)} min after an auto-resume — NOT resumed again. "
                          f"The provider is still failing; check its status before prompting. Detail: {why}")
            self.emit("ERROR_ESCALATED", name, sid, m.get("info", m).get("id"), f"item={item}",
                      f"second retryable {status} inside the re-resume window — BOSS decides")
            return None
        self.state["err_resumed"][sid] = time.time()
        if self.post_prompt(sid, ERROR_RESUME_PROMPT.format(hhmm=datetime.now().strftime("%H:%M"))):
            self.emit("ERROR_RESUMED", name, sid, m.get("info", m).get("id"), f"item={item}",
                      f"retryable provider error {status} after {int(age)}s grace — one-shot RESUME posted")
            return "resumed"
        self.escalate(f"RETRYABLE PROVIDER ERROR for {name} ({sid}) item={item} status={status}: "
                      f"the RESUME post FAILED. This session is stopped and was NOT resumed.")
        return None

    # -- stalled turns (provider-cut), added 2026-09-05 after the 16:35-16:54 outage
    @staticmethod
    def stall_shape(m):
        """-> (looks_stalled, fingerprint). A turn cut by a provider error, as measured 16:35.

        THE SHAPE ALONE IS NOT ENOUGH, and this is the whole difficulty. `assistant / not
        completed / finish=None / error=None / empty text` was measured on the eight cut sessions —
        but at 17:17, with every executor healthy and building, FIVE of seven live sessions matched
        that description exactly. Resuming on it would have injected a prompt into five running
        builds. Two further signals do the actual work:

        1. A working turn holds a tool part in `state.status == 'running'` (measured: four
           executors sat byte-identical for 45 s each because a `bash` pytest was running inside
           them). A cut turn holds none — nothing is in flight, and nothing ever will be.
        2. Even then the fingerprint must not move for a dwell period, because a turn between
           `reasoning` and its next tool call also has no running tool for a second or two.

        The fingerprint covers the message id, its part count and its serialized size, so a turn
        that is quietly growing reasoning tokens is not mistaken for a dead one.
        """
        info = m.get("info", m)
        if info.get("role") != "assistant":
            return False, ""
        t = info.get("time", {}) or {}
        if t.get("completed") or info.get("error"):
            return False, ""            # completed / errored turns are already classified elsewhere
        parts = m.get("parts", []) or []
        if " ".join(p.get("text", "") for p in parts if p.get("type") == "text").strip():
            return False, ""            # it produced text: the turn is alive
        for p in parts:
            st = p.get("state")
            if isinstance(st, dict) and st.get("status") in ("running", "pending"):
                return False, ""        # a tool is in flight — a long pytest, not a dead turn
        return True, f"{info.get('id')}:{len(parts)}:{len(json.dumps(m, default=str, sort_keys=True))}"

    def compaction_active(self, sid):
        """-> the id of the session's newest USER message if that message IS a compaction.

        2026-09-06 21:03:59, EXEC-D: STALL_ESCALATED fired on a turn that was COMPACTING, not cut.
        An auto-compaction ends the assistant turn, posts a user message carrying a `compaction`
        part, and starts a fresh assistant turn that has no parts and is not completed for a while —
        which is byte-for-byte the shape this detector calls a provider cut. Posting a RESUME into a
        compaction is the failure to avoid: it burns the session's first post-compaction turn on a
        prompt it did not need, at the exact moment its context was just rewritten.

        Deliberately consulted only when a stall is about to be DECLARED, not on every BUSY tick:
        this costs one extra API call per stall decision instead of one per poll per executor.
        """
        for msg in reversed(self.last_messages(sid, 3)):
            info = msg.get("info", msg)
            if info.get("role") != "user":
                continue
            parts = msg.get("parts", [])
            if any(p.get("type") == "compaction" for p in parts):
                return info.get("id")
            # the synthetic nudge opencode posts right after a compaction
            if any(p.get("type") == "text" and (p.get("metadata") or {}).get("compaction_continue")
                   for p in parts):
                return info.get("id")
            return None      # the newest user message is an ordinary prompt: not compacting
        return None

    def check_stall(self, name, sid, m, cur):
        """Detect a provider-cut turn; resume it ONCE. -> a status label, or None.

        Called only from the BUSY branch, which is exactly where a cut turn currently disappears:
        the daemon reads `not completed` as `busy`, pending.json says "building ... busy", and
        auto-continue never fires because that keys on a completed progress-line stop.
        """
        st = self.state.setdefault("stall", {})
        rec = st.get(sid)
        shaped, fp = self.stall_shape(m)
        if not shaped:
            if rec:
                del st[sid]
            # a turn with text or a running tool ends the compaction episode too: normal detection
            # resumes on the first turn after it that looks alive
            self.state.setdefault("compacting", {}).pop(sid, None)
            return None
        nowt = time.time()
        if not rec or rec.get("fp") != fp:
            st[sid] = {"fp": fp, "since": nowt, "polls": 1}
            return None
        rec["polls"] = int(rec.get("polls", 1)) + 1
        held = nowt - float(rec.get("since", nowt))
        if rec.get("fired"):
            return f"STALLED {int(held)}s (resumed, watching)"
        if rec["polls"] < int(self.c("stall_min_polls", 2)) or held < float(self.c("stall_after_seconds", 120)):
            return None
        item = cur["id"] if cur else "-"
        cid = self.compaction_active(sid)
        if cid:
            # Not a stall. Say so once, leave the window unfired so detection resumes cleanly, and
            # post NOTHING: the session is mid-compaction and a RESUME would land in it.
            comp = self.state.setdefault("compacting", {})
            if comp.get(sid) != cid:
                comp[sid] = cid
                self.emit("COMPACTING", name, sid, cid, f"item={item}",
                          "the newest user message is a compaction — this inert turn is the "
                          "post-compaction turn starting, NOT a provider cut. STALLED_TURN, RESUME "
                          "and escalation are all suppressed until it produces a live turn.")
            st.pop(sid, None)
            return "COMPACTING (stall detection suspended)"
        rec["fired"] = True
        # A partially-landed marker: the turn was cut, but text had already arrived carrying
        # PLAN READY / REPORT READY. Log it, because the BUSY branch never classifies text and the
        # event would otherwise not exist anywhere. (This alone would NOT have caught 2026-09-06
        # 20:45 — that message had NO text at stall time and only completed 5 min later; see
        # recover_missed_marker.)
        cut_text = text_of(m)
        if PLAN_RE.search(cut_text) or REPORT_LINE_RE.search(cut_text):
            self.emit("CUT_TURN_MARKER", name, sid, m.get("info", m).get("id"), f"item={item}",
                      "the CUT turn already carried a PLAN READY / REPORT READY block — "
                      + clean_excerpt(cut_text))
        self.emit("STALLED_TURN", name, sid, m.get("info", m).get("id"), f"item={item}",
                  f"assistant turn inert {int(held)}s over {rec['polls']} polls: no text, no running tool, "
                  f"not completed, no error — provider-cut shape")
        # A second stall soon after a resume is not a second accident. 16:35's cause was
        # `AI_APICallError: 5-hour usage limit reached. Resets in 34min` — resuming into a limit
        # burns the session's next turn for nothing and hides the real cause from BOSS.
        prev = self.state.setdefault("stall_resumed", {}).get(sid)
        window = float(self.c("stall_reresume_block_minutes", 10)) * 60
        if prev and nowt - float(prev) < window:
            err = (m.get("info", m).get("error") or {})
            self.escalate(f"STALLED_TURN again for {name} ({sid}) item={item} "
                          f"{int((nowt - float(prev)) / 60)} min after an auto-resume — NOT resumed again. "
                          f"Provider error: {json.dumps(err)[:300] if err else 'not exposed by the API'}. "
                          f"Check the provider limit/outage before prompting this session.")
            self.emit("STALL_ESCALATED", name, sid, m.get("info", m).get("id"), f"item={item}",
                      "second stall inside the re-resume window — BOSS decides, no auto-resume")
            return f"STALLED again — escalated (no resume)"
        self.state["stall_resumed"][sid] = nowt
        if self.post_prompt(sid, STALL_RESUME_PROMPT.format(hhmm=datetime.now().strftime("%H:%M"))):
            self.emit("STALL_RESUMED", name, sid, m.get("info", m).get("id"), f"item={item}",
                      "one-shot RESUME posted after a provider-cut turn")
            return f"STALL RESUMED ({item})"
        self.escalate(f"STALLED_TURN for {name} ({sid}) item={item}: the RESUME post FAILED "
                      f"(server unreachable?). This session is cut and was NOT resumed.")
        return "STALLED — resume POST failed"

    # ---------------------------------------------------------------- auto-gate / auto-rework
    # BOSS + ★ 2026-09-07. The decision rules live in autogate.py and are pure; this half launches,
    # watches and posts. The split is so the rules can be tested against recorded gate files without
    # a daemon, a server or a box.
    def standing_rules(self):
        try:
            return open(os.path.join(STATE_DIR, "EXECUTOR_STANDING_RULES.md"), errors="ignore").read().strip()
        except OSError:
            return ""

    def gates_running(self):
        try:
            out = subprocess.run(["ps", "-eo", "args="], capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return 1        # cannot tell -> assume busy; never launch a second gate on a guess
        return sum(1 for l in out.splitlines()
                   if "dispatcher/mergegate.py" in l and "grep" not in l and "zsh" not in l)

    def sha_at_lane_head(self, item):
        """Is the reported sha the lane head? Returns (ok, sha). Measured with git, never assumed."""
        wt, lane = item.get("worktree"), item.get("lane")
        if not wt or not os.path.isdir(wt):
            return False, ""
        try:
            r = subprocess.run(["git", "-C", wt, "rev-parse", lane or "HEAD"],
                               capture_output=True, text=True, timeout=30)
            head = (r.stdout or "").strip()
        except Exception:
            return False, ""
        return bool(head), head

    def launch_gate(self, item, no_box=False, source="report"):
        """Start mergegate for one item, detached. -> the sha it was launched at, or None.

        `source` only labels the event. `no_box` is BOSS's --no-box on the manual verb: it can drop
        the box, never take one gate_launchable would have refused — the serialisation check below
        still runs on the item's own proof files, so --no-box cannot be used to jump the box queue.
        """
        # BEFORE the import and before reading the lane head: a held actuator should do none of the
        # work, not merely stop at the last step.
        if getattr(self, "observing", False):
            return None, ("OBSERVE-ONLY hold is in force — no gate launched. The candidate is "
                          "unchanged and this is not a verdict about it.")
        import autogate
        ok, head = self.sha_at_lane_head(item)
        can, why = autogate.gate_launchable(item, self.gates_running(), ok)
        if not can:
            return None, why
        argv = ["/usr/bin/python3", os.path.join(HERE, "mergegate.py"), item["id"]]
        if no_box or not autogate.needs_box(item.get("proof_files")):
            argv.append("--no-box")
            why = why if not no_box else why + " (--no-box)"
        if self.dry:
            self.log(f"[dry] would launch {' '.join(argv)}")
            return head, "dry"
        try:
            subprocess.Popen(argv, cwd=CN, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except Exception as e:
            return None, f"launch failed: {e}"
        self.state.setdefault("autogate", {})[item["id"]] = {
            "sha": head, "started": time.time(), "mode": why, "source": source}
        item["status"] = "gated"
        self.emit("AUTO_GATE" if source == "report" else "MANUAL_GATE",
                  item.get("dispatched_to", "-"), "-", "-",
                  f"item={item['id']} sha={head[:10]} mode={why} source={source}",
                  "gate launched by the daemon on REPORT READY; BOSS still decides the merge"
                  if source == "report" else
                  "gate launched from dispatcherctl.sh gate — same path as the automatic one, "
                  "and its result is collected and reworked the same way; BOSS still decides the merge")
        return head, why

    def manual_gates_live(self):
        """Is a hand-named gate in flight? Then its result must be collected even with auto_gate off.

        The interlock that made this necessary: BOSS's remedy for a bad auto-gate is
        `auto_gate=false`, and collect_gates sits behind that same flag. Without this, a manual gate
        would launch, run for an hour, write its file, and be read by nobody — the exact failure the
        request-file protocol exists to prevent, reintroduced by the off switch.
        """
        return any((r or {}).get("source") == "manual"
                   for r in self.state.get("autogate", {}).values())

    def reload_cfg(self):
        """Re-read roster.json when it changes on disk. Called every tick; an mtime stat, no parse.

        BOSS, 2026-09-07: roster.json was loaded ONCE at startup and nothing re-read it, so a flag
        flipped in the file did nothing until a restart — and the daemon was described, by me, as
        reading it per tick. Between 02:42 and 02:50 it ran with the old auto_gate and the old stall
        threshold while the file said otherwise. Nothing was lost, but "live" was not true.

        A short or truncated file is REFUSED, not applied: this is read while BOSS is editing it, and
        blanking the config would silently drop every threshold to its default.
        """
        # `cfg_path` so the tests can point this at a temp file: roster.json is BOSS's, and a test
        # that had to write the live one to prove a reload would be a test nobody dares run.
        path = getattr(self, "cfg_path", None) or ROSTER_PATH
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return
        if mt == getattr(self, "_cfg_mtime", None):
            return
        cfg = load_json(path, None)
        if not isinstance(cfg, dict) or not cfg:
            self.emit("ROSTER_RELOAD_REFUSED", "-", "-", "-", "unreadable or empty roster.json",
                      "kept the config already in memory — a half-written file must never blank it")
            return
        self._cfg_mtime = mt
        changed = {k: (self.cfg.get(k), v) for k, v in cfg.items()
                   if k != "executors" and self.cfg.get(k) != v}
        dropped = [k for k in self.cfg if k not in cfg and k != "executors"]
        self.cfg = cfg
        if changed or dropped:
            self.emit("ROSTER_RELOADED", "-", "-", "-",
                      f"changed={len(changed)} dropped={len(dropped)}",
                      "; ".join(f"{k}: {old!r} -> {new!r}" for k, (old, new) in sorted(changed.items()))
                      + ("; dropped " + ", ".join(sorted(dropped)) if dropped else ""))

    def recover_missed_reports(self, q):
        """Once per process: gate any REPORT_READY that was handled by a daemon that then died.

        The crack, measured by BOSS 2026-09-07: EXEC-G reported at 02:55:26 while the daemon was
        restarting. The old process wrote `handled[sid] = msg_id` and exited; the new one read that
        map, saw the message as already dealt with, and launched nothing. No event says so — the
        board shows an ordinary item. Every restart drops whatever reported during it.

        Deliberately narrow. It runs on the FIRST tick only, it re-checks the same report-path
        evidence the live trigger demands, and it refuses anything with a gate file newer than the
        report or a gate already in flight — so it can neither resurrect an idle-ack nor re-gate work
        that was already gated. `recovered` records the message id, so a daemon restarted twice does
        not launch a second gate for the same report.
        """
        if getattr(self, "_recovered", False):
            return
        self._recovered = True
        import autogate
        rec = self.state.setdefault("recovered", {})
        for name, sid in sorted(self.roster.items()):
            recent = self.last_messages(sid, 3)
            m = recent[-1] if recent else None
            if not m:
                continue
            info = m.get("info", m)
            msg_id = info.get("id")
            if self.state["handled"].get(sid) != msg_id:
                continue                 # not handled yet — this tick will process it normally
            kind = self.classify(m)[0]
            if kind != "REPORT_READY":
                continue
            it = self.current_item(q, name) or next(
                (i for i in q["items"] if i.get("report_msg_id") == msg_id), None)
            if not it:
                continue
            if rec.get(it["id"]) == msg_id:
                continue                 # already recovered by an earlier restart
            if it["id"] in self.state.get("autogate", {}):
                continue                 # a gate for it is in flight
            checker, cwhy = self.path_checker(it)
            if checker is None:
                # NOT the silent path below. That silence is for "this is an ordinary old report" —
                # a judgement made by looking. This is "we could not look", and it has to say so or
                # a recoverable report is dropped a second time by a pass built to catch the first.
                told = self.state.setdefault("recovery_uncheckable", {})
                if told.get(it["id"]) != msg_id:
                    told[it["id"]] = msg_id
                    self.emit("RECOVERY_CANNOT_CHECK", name, sid, msg_id, f"item={it['id']}", cwhy)
                continue
            ok, why = autogate.recovery_ok(it, text_of(m), checker)
            if not ok:
                continue                 # silent by design: the common case is an ordinary old report
            when = float((info.get("time", {}) or {}).get("created") or 0) / 1000.0
            gate_md = os.path.join(GATES_DIR, f"{it['id']}.md")
            if os.path.exists(gate_md) and os.path.getmtime(gate_md) >= when:
                continue                 # a gate for this report already ran — never twice
            rec[it["id"]] = msg_id
            sha, gwhy = self.launch_gate(it, source="recovery")
            if sha:
                self.emit("REPORT_RECOVERED", name, sid, msg_id, f"item={it['id']} sha={sha[:10]}",
                          f"{why} — this report was handled by a daemon that then died and was "
                          f"gated by nobody; launched on startup")
            else:
                self.emit("REPORT_RECOVERY_DEFERRED", name, sid, msg_id, f"item={it['id']}", gwhy)
                self.escalate(f"RESTART RECOVERY {it['id']}: a dropped REPORT READY was found but the "
                              f"gate could not launch ({gwhy}). It will NOT be retried automatically.")

    def path_checker(self, item):
        """A `rel -> bool` that resolves INSIDE this item's checkout. -> (checker, "") or (None, why).

        The defect this replaces, in two places (the recovery pass and feed()):

            os.path.isfile(os.path.join(item.get("worktree") or "", rel))

        With no worktree that is `os.path.join("", rel)`, which is `rel`, which os.path resolves
        against THE DAEMON'S OWN CWD. It does not fail to check — it checks a directory nobody
        named, and then reports "none exist at the lane head", a sentence about a lane head it never
        looked at. False in the usual case; and had the daemon been started from a checkout, TRUE for
        somebody else's file.

        No worktree means CANNOT CHECK. That is a refusal, never a path.
        """
        wt = str(item.get("worktree") or "")
        if not wt or wt.startswith("("):
            return None, (f"the row for {item.get('id')} records no worktree" +
                          (f" (it says {wt}, which defers to the item prompt)" if wt else "") +
                          f", so a report path CANNOT BE CHECKED — an unchecked path is not a "
                          f"missing one. Set `worktree` on the row and the report is gated.")
        if not os.path.isdir(wt):
            return None, (f"the row for {item.get('id')} names worktree {wt}, which does not exist "
                          f"on disk, so a report path CANNOT BE CHECKED there — this is not "
                          f"evidence that the report is missing.")
        return (lambda rel: os.path.isfile(os.path.join(wt, rel))), ""

    def gated_item_for(self, q, name):
        """The item of this executor's that a gate is currently measuring, or None.

        Not current_item(): BOSS ruled `gated` stays OUT of CURRENT_STATUS because the gate in
        flight owns the sha. This answers a different question — "is there anything this executor
        could be doing?" — and the answer while its work is under review is no.

        The running mergegate list is consulted as well as the status, for the same reason
        lane_occupied does it: a gate BOSS launches by hand can be measuring an item the queue still
        calls `reported`.
        """
        live = self.running_gate_items()
        for it in q["items"]:
            if it.get("dispatched_to") != name:
                continue
            if str(it.get("status") or "").lower() == "gated":
                return it
            if live and it["id"] in live:
                return it
        return None

    def skip_gate(self, name, sid, msg_id, label, why):
        """Emit AUTO_GATE_SKIPPED at most once per message and reason.

        BOSS, 2026-09-07: EXEC-D and EXEC-E emitted a skip EVERY TICK on the same stale message ids.
        The tick re-feeds an already-handled idle executor on every pass (dispatcher.py, the
        `already-handled idle executor` branch), so a skip that fires from feed() fires forever —
        every 5 seconds, into the log BOSS reads. A reason nobody can scroll past is not a reason.

        Keyed on the reason too, so a skip whose CAUSE changes (deferred for the box, then the sha
        moved) still speaks once for the new cause.
        """
        seen = self.state.setdefault("gate_skipped", {})
        key = f"{msg_id}|{str(why)[:40]}"
        if seen.get(sid) == key:
            return False
        seen[sid] = key
        self.emit("AUTO_GATE_SKIPPED", name, sid, msg_id, label, why)
        return True

    def withdraw_idle_row(self, pending, name, sid, kind, msg_id):
        """Drop the pending row for a REPORT_READY the trigger judged NOT a report. -> bool.

        BOSS, 2026-09-07: "an idle ack should not raise a pending row for me at all once the trigger
        has judged it not a report" — two were cleared by hand within four minutes. The row is
        written before feed() runs because feed() is what judges, so the withdrawal happens after.

        ONLY on that judgement. A real report that merely could not launch keeps its row: BOSS still
        has to see it, and a row withdrawn on "could not gate" would hide exactly the case that needs
        a human.
        """
        v = getattr(self, "_report_verdict", None)
        if kind != "REPORT_READY" or not v or v[0] != msg_id:
            return False
        if v[1] is not False and v[1] != GATE_RUNNING:
            return False
        if sid not in pending:
            return False
        pending.pop(sid, None)
        self.state.get("stale_seen", {}).pop(sid, None)
        if v[1] == GATE_RUNNING:
            # Withdrawn, but NOT as an idle ack: a report did arrive and a verdict is already on its
            # way from the gate that owns the sha. The row would only ask BOSS to wait for something
            # that is already happening — but calling it IDLE_ACK would file a real report under
            # "there was no report", which is the confusion this whole three-state split is about.
            self.emit("REPORT_WHILE_GATED", name, sid, msg_id, "no pending row raised", v[2])
        else:
            self.emit("IDLE_ACK", name, sid, msg_id, "no pending row raised", v[2])
        return True

    def apply_gate_requests(self, q):
        """Apply `dispatcherctl.sh gate <item-id> [--no-box]` requests. One gate each, or none.

        A request file, not an edit: state lives in the daemon and is rewritten every tick, and the
        gate must be launched by the process that also collects it — a gate started from ctl would
        finish with nobody watching for its gate file, so its findings would never reach an executor.

        Re-validated here against the live queue rather than trusted: ctl read queue.json seconds
        ago and the item may have moved since.
        """
        import autogate
        try:
            reqs = sorted(f for f in os.listdir(GATE_REQ_DIR) if f.endswith(".json"))
        except OSError:
            return
        for fn in reqs:
            path = os.path.join(GATE_REQ_DIR, fn)
            req = load_json(path, None)
            self._rm(path)
            if not isinstance(req, dict) or not req.get("item"):
                self.emit("MANUAL_GATE_REFUSED", "-", "-", fn, "unreadable request",
                          "the request file is not a JSON object naming an item — nothing launched")
                continue
            item_id = str(req["item"])
            it = next((i for i in q["items"] if i["id"] == item_id), None)
            in_flight = item_id in self.state.get("autogate", {})
            ok, why = autogate.manual_gate_ok(it, in_flight)
            if not ok:
                self.emit("MANUAL_GATE_REFUSED", (it or {}).get("dispatched_to", "-"), "-", item_id,
                          "refused", why)
                self.escalate(f"MANUAL GATE {item_id} REFUSED: {why}")
                continue
            head, why2 = self.launch_gate(it, no_box=bool(req.get("no_box")), source="manual")
            if not head:
                self.emit("MANUAL_GATE_REFUSED", it.get("dispatched_to", "-"), "-", item_id,
                          "not launched", why2)
                self.escalate(f"MANUAL GATE {item_id} NOT LAUNCHED: {why2}. Nothing was changed; "
                              f"re-run dispatcherctl.sh gate {item_id} once that clears.")

    def apply_answer_requests(self, q):
        """Apply `dispatcherctl.sh answer <item> "<text>"`. Post the reply AND un-park, or neither.

        BOSS, 2026-09-07, on his own fourth silent failure of the night: "I act on the system through
        a side channel and leave its record untouched." A QUESTION parks the executor's item by
        design, so the question waits for him; he answered four of them by direct prompt_async and
        never cleared the park, and `parked` is not gateable. EXEC-J finished an artifact and its
        REPORT READY was skipped at 04:57 with "holds no dispatched, rework or reported item".

        ORDER IS THE WHOLE DESIGN. The post happens FIRST and the row moves only if it succeeded: a
        row un-parked before a failed post says the executor is building when nobody has told it
        anything, which is the same drift in the other direction. A failure leaves the row parked and
        says so.

        Every refusal names what the item IS, because "answer refused" without the current status
        sends BOSS back to the queue file to find out — and that round trip is what a side channel
        exists to avoid.
        """
        try:
            reqs = sorted(f for f in os.listdir(ANSWER_REQ_DIR) if f.endswith(".json"))
        except OSError:
            return
        for fn in reqs:
            path = os.path.join(ANSWER_REQ_DIR, fn)
            req = load_json(path, None)
            self._rm(path)
            if not isinstance(req, dict) or not req.get("item") or not str(req.get("text") or "").strip():
                self.emit("ANSWER_REFUSED", "-", "-", fn, "unreadable request",
                          "the request file is not a JSON object with an item and a non-empty text "
                          "— nothing was posted and no row was changed")
                continue
            item_id, text = str(req["item"]), str(req["text"])
            it = next((i for i in q["items"] if i["id"] == item_id), None)
            if not it:
                self.emit("ANSWER_REFUSED", "-", "-", item_id, "no such item",
                          "no queue row with that id — nothing posted, nothing changed")
                self.escalate(f"ANSWER {item_id} REFUSED: no queue row with that id.")
                continue
            st = str(it.get("status") or "unknown").lower()
            who = str(it.get("dispatched_to") or "")
            if st != "parked":
                why = (f"{item_id} is {st}, not parked — nothing was posted and no row was changed. "
                       f"`answer` exists to close a QUESTION's park; use the ordinary channel to "
                       f"talk to an executor that is not waiting on one.")
                self.emit("ANSWER_REFUSED", who or "-", "-", item_id, f"status={st}", why)
                self.escalate(f"ANSWER {item_id} REFUSED: {why}")
                continue
            if not who:
                why = (f"{item_id} is parked but records no executor, so there is nobody to post to. "
                       f"Un-park it with queuectl if the park is stale.")
                self.emit("ANSWER_REFUSED", "-", "-", item_id, "no executor", why)
                self.escalate(f"ANSWER {item_id} REFUSED: {why}")
                continue
            import autogate
            sid = self.roster.get(who)
            relayed = autogate.is_relayed(who) or not sid
            if relayed and not req.get("relayed_by_hand"):
                why = (f"{who} has no opencode session — this daemon cannot post to it, and "
                       f"answering means relaying the text yourself. Re-run with --relayed once you "
                       f"have, and the row will be un-parked on your word rather than on a post that "
                       f"never happened.")
                self.emit("ANSWER_REFUSED", who, sid or "-", item_id, "relay lane", why)
                self.escalate(f"ANSWER {item_id} REFUSED: {why}")
                continue
            posted = "relayed by hand (no post attempted — relay lane)"
            if not relayed:
                # post_prompt returns False and RAISES NOTHING — every failure is caught inside it.
                # The old `try/except Exception` here could therefore never fire, so an UNDELIVERED
                # answer un-parked the row, withdrew the QUESTION and emitted ANSWERED: the record
                # said an executor had been told something nobody told it. The except stays for a
                # stub or a future raise, but the RESULT is what decides, and both roads end in the
                # same refusal.
                delivered, err = False, ""
                try:
                    delivered = self.post_prompt(sid, text)
                    if not delivered:
                        err = getattr(self, "_last_post_error", "") or "post_prompt returned False"
                except Exception as e:  # noqa: BLE001 — a failed post must not move the row
                    err = f"{type(e).__name__}: {e}"
                if not delivered:
                    why = (f"the post to {who} ({sid}) FAILED: {err}. The row is STILL PARKED, its "
                           f"QUESTION is still pending, and the executor has not been told anything. "
                           f"Nothing was changed.")
                    self.emit("ANSWER_FAILED", who, sid, item_id, "post failed", why)
                    self.escalate(f"ANSWER {item_id} FAILED: {why}")
                    continue
                posted = f"posted to {sid}"
            # Only now does the record move.
            back = str(it.get("parked_from") or "").lower()
            if back not in ("dispatched", "rework", "reported"):
                # Rows parked before parked_from existed (and the four BOSS un-parked by hand on
                # 2026-09-07) carry no prior status. `dispatched` is the honest default — the
                # executor was working on it — and the event says the value was assumed, not read.
                back, source = "dispatched", "assumed (no parked_from on the row)"
            else:
                source = "restored from parked_from"
            # A QUESTION frees the executor for as long as it is parked, and the dispatcher fills
            # that gap — correctly. So by the time the answer arrives, the executor may already hold
            # something else, and restoring to `dispatched` double-books it (BOSS, 2026-09-07:
            # EXEC-M, 10:05:19 dispatched -> 10:06:03 parked -> 10:06:23 given B.013 -> 10:08:12
            # answered back to dispatched, holding two). The answer still gets posted — it is the
            # reply to a question that was really asked — but the ROW goes back to the pool PINNED
            # to this executor, so it is re-dispatched when the executor frees and no second row is
            # claimed in the meantime. `executor` is the pin `eligible_item` already honours, and
            # `answered_at` on a queued row already drives the undelivered narration and the
            # feed-hold clear, so this reuses machinery rather than adding a state.
            occupied = [o["id"] for o in self.current_items(q, who) if o["id"] != item_id]
            double = ""
            if occupied:
                double = (f"; NOT restored to {back}: {who} has since been given "
                          f"{', '.join(occupied)}, so this row is QUEUED and PINNED to {who} and "
                          f"will be re-dispatched when it frees — restoring it now would leave "
                          f"{who} holding {len(occupied) + 1} rows at once")
                back = "queued"
                it["executor"] = who
                it["dispatched_to"] = ""
            dropped = [k for k, r in list(self.state.get("pending", {}).items())
                       if str(r.get("kind")) == "QUESTION"
                       and (r.get("item") == item_id or r.get("executor") == who)]
            for k in dropped:
                self.state["pending"].pop(k, None)
            # BOSS, 2026-09-07: `answer` deliberately does NOT clear a feed hold — a question is
            # closed by an answer, a pause is closed by a judgement that the executor is fit to
            # continue, and a verb that silently resumes feeding is a bigger surprise than one that
            # does not. But an un-parked row whose executor is still held reads as "back to work",
            # so the event has to say the pause is still there or it is forgotten silently.
            hold = os.path.join(HOLD_DIR, f"{who}.feed")
            held_note = ("" if not os.path.exists(hold) else
                         f"; **FEED HOLD STILL IN PLACE for {who}** (hold/{who}.feed) — the question "
                         f"is closed but the executor is not being fed. `rm` that file when you "
                         f"judge it fit to continue; answering does not, and should not, do it.")
            it.update({"status": back, "answered_at": now_local(),
                       "answered_question_msg_id": it.get("question_msg_id")})
            it.pop("parked_from", None)
            self.emit("ANSWERED", who, sid or "-", item_id,
                      f"item={item_id} parked -> {back} ({source}); {posted}; "
                      f"{len(dropped)} QUESTION pending row(s) withdrawn{double}{held_note}",
                      clean_excerpt(text[:1200]))

    def collect_gates(self, q):
        """One pass over gates the daemon launched: act on the ones that have finished."""
        import autogate
        live = self.state.setdefault("autogate", {})
        for item_id, rec in list(live.items()):
            md = os.path.join(GATES_DIR, f"{item_id}.md")
            crashed = os.path.join(GATES_DIR, f"{item_id}.CRASHED.md")
            path = crashed if (os.path.exists(crashed) and os.path.getmtime(crashed) > rec["started"]) \
                else (md if (os.path.exists(md) and os.path.getmtime(md) > rec["started"]) else None)
            if not path:
                if time.time() - rec["started"] > 3 * 3600:
                    del live[item_id]
                    self.escalate(f"AUTO-GATE for {item_id} produced no gate file in 3h — check for a "
                                  f"gate that died without writing one.")
                continue
            # A rework is never sent twice for the same gate FILE: the file's own mtime is the key,
            # so a re-read of the same result can never re-prompt an executor.
            key = f"{item_id}@{int(os.path.getmtime(path))}"
            done = self.state.setdefault("autogate_done", {})
            if done.get(item_id) == key:
                continue
            done[item_id] = key
            del live[item_id]
            gate = autogate.parse_gate_md(open(path, errors="ignore").read())
            codex_text = ""
            cx = os.path.join(GATES_DIR, f"{item_id}.codex.txt")
            if os.path.exists(cx):
                codex_text = open(cx, errors="ignore").read()
            hist = self.state.setdefault("autogate_hist", {}).get(item_id, {})
            action, reason = autogate.decide(gate, codex_text, hist)
            it = next((i for i in q["items"] if i["id"] == item_id), None)
            if action != "rework" or not it:
                if it:
                    it["status"] = "reported"
                self.emit("AUTO_GATE_ESCALATED", (it or {}).get("dispatched_to", "-"), "-", "-",
                          f"item={item_id} verdict={gate.get('verdict')}", reason)
                self.escalate(f"AUTO-GATE {item_id}: {gate.get('verdict')} — {reason}. Gate file: {path}")
                continue
            self.send_rework(it, gate, codex_text, reason, path)

    def send_rework(self, it, gate, codex_text, reason, gate_path):
        """Item file FIRST, then the prompt. One event."""
        import autogate
        item_id = it["id"]
        hist = self.state.setdefault("autogate_hist", {}).setdefault(item_id, {"fails": 0, "classes": []})
        n = int(hist.get("fails", 0)) + 1
        sha = gate.get("sha") or self.state.get("autogate", {}).get(item_id, {}).get("sha", "")
        fname = it.get("prompt_file") or f"{item_id}.md"
        path = os.path.join(ITEMS_DIR, fname)
        block = autogate.rework_block(n, now_local(), sha, codex_text)
        if not self.dry:
            try:
                # The item file is written BEFORE the prompt: the prompt tells the executor the block
                # is already in its file, and a prompt that says so before it is true is a lie the
                # executor will act on.
                with open(path, "a") as f:
                    f.write(block)
            except OSError as e:
                self.escalate(f"AUTO-REWORK {item_id}: could NOT write the item file ({e}) — not prompted.")
                return
        who = it.get("dispatched_to", "")
        sid = self.roster.get(who)
        prompt = autogate.rework_prompt(item_id, n, sha, codex_text, self.standing_rules())
        if autogate.is_relayed(who) or (not sid and who):
            # A lane BOSS relays to by message. The item file already carries the findings; the
            # pending row is how BOSS learns there is something to relay. Claiming "posted to the
            # executor" here would leave the findings sitting in a file with nobody told.
            hist["fails"] = n
            hist["classes"] = sorted(set(hist.get("classes", [])) | set(autogate.finding_classes(codex_text)))
            it.update({"status": "rework", "reworked_at": now_local()})
            self.state["pending"][f"relay:{item_id}"] = {
                "executor": who or "-", "session": "-", "kind": "REWORK_TO_RELAY",
                "msg_id": os.path.basename(gate_path), "item": item_id,
                "artifact": it.get("artifact"), "since_ms": time.time() * 1000,
                "since_local": now_local(), "escalated": 0, "esc_count": 0, "last_esc_min": 0,
                "esc_base_ms": time.time() * 1000,
                "excerpt": (f"AUTO-REWORK {n} for {item_id}: {reason}. Findings are appended to "
                            f"{fname} as '## REWORK {n}'. {who} has NO opencode session — relay "
                            f"this by message; the daemon cannot prompt it."),
            }
            self.emit("AUTO_REWORK_RELAY", who or "-", "-", "-",
                      f"item={item_id} rework={n} sha={(sha or '')[:10]}",
                      f"{reason}. Findings appended verbatim to {fname}; {who} is not on opencode, "
                      f"so this is a pending row for BOSS to relay, not a post.")
            return
        if not sid or not self.post_prompt(sid, prompt):
            self.escalate(f"AUTO-REWORK {item_id}: the item file was updated but the prompt could NOT "
                          f"be posted (executor {it.get('dispatched_to')}). Re-dispatch by hand.")
            return
        hist["fails"] = n
        hist["classes"] = sorted(set(hist.get("classes", [])) | set(autogate.finding_classes(codex_text)))
        it.update({"status": "rework", "reworked_at": now_local()})
        self.emit("AUTO_REWORK", it.get("dispatched_to", "-"), sid, "-",
                  f"item={item_id} rework={n} sha={(sha or '')[:10]}",
                  f"{reason}. Findings appended verbatim to {fname} and posted to the executor; "
                  f"status=rework. BOSS was not asked.")

    def check_dead(self, name, sid, recent, cur):
        """An executor holding a dispatched item while producing nothing at all. -> label or None.

        BOSS, 2026-09-07: EXEC-H sat silent for ~5 HOURS on an empty resume turn and was found by
        hand. check_stall did not cover it, and the reason matters: once check_stall has fired and
        posted its one RESUME it returns "STALLED (resumed, watching)" on every later tick and never
        escalates again. Watching is not a state anyone is told about. A session that stays dead
        after its one resume therefore sits forever with a reassuring label on the board.

        This is the LAST resort, deliberately separate from check_stall: check_stall decides whether
        to resume (a fast, careful judgement about a turn shape), this decides whether to WAKE BOSS
        (a slow one about an executor). It fires on the fact that survives every explanation — an
        item was dispatched, and for N minutes the session has produced no text, no running tool and
        no completed turn.

        Fires ONCE per silent stretch: the escalation exists to be read, and one repeated every 5s
        is one nobody reads.
        """
        if not cur:
            return None                      # no dispatched item: an idle executor is not stuck
        mins = float(self.c("dead_after_minutes", 30))
        newest = recent[-1] if recent else None
        asst = next((m for m in reversed(recent or [])
                     if (m.get("info", m) or {}).get("role") == "assistant"), None)
        if asst is not None:
            info = asst.get("info", asst)
            t = info.get("time", {}) or {}
            if t.get("completed") or info.get("error"):
                return None                  # it finished or failed: other branches own that
            shaped, _fp = self.stall_shape(asst)
            if not shaped:
                return None                  # text arrived, or a tool is RUNNING — it is working
            since_ms = t.get("created")
        else:
            # No assistant message at all: the session was prompted and never answered. Age from the
            # newest message we do have, which is our own prompt.
            if not newest:
                return None
            since_ms = ((newest.get("info", newest) or {}).get("time") or {}).get("created")
        if not since_ms:
            return None
        silent_s = time.time() - float(since_ms) / 1000.0
        if silent_s < mins * 60:
            return None
        # The dedupe check comes BEFORE compaction_active(), which costs an API call: once a session
        # is known dead we would otherwise pay that call every 5s tick to re-learn the same thing.
        key = (asst or newest).get("info", (asst or newest)).get("id")
        seen = self.state.setdefault("dead", {})
        if seen.get(sid) == key:
            return f"DEAD {int(silent_s / 60)} min (escalated)"
        if self.compaction_active(sid):
            return None                      # a compaction is not death; COMPACTING owns that case
        seen[sid] = key
        what = "no assistant turn at all" if asst is None else \
               "an assistant turn with no text, no running tool and no completion"
        self.emit("STUCK", name, sid, key, f"item={cur['id']} silent={int(silent_s / 60)}min",
                  f"{name} has held {cur['id']} for {int(silent_s / 60)} minutes with {what}. "
                  f"It is not building. BOSS must decide: re-dispatch, reassign the item, or "
                  f"replace the session.")
        self.escalate(f"STUCK: {name} ({sid}) has held item {cur['id']} for "
                      f"{int(silent_s / 60)} min with {what}. No auto-action taken.")
        return f"DEAD {int(silent_s / 60)} min — escalated"

    def check_session_idle(self, name, sid, recent, cur):
        """A session that has not begun a turn AT ALL since we prompted it. -> label or None.

        BOSS, 2026-09-07: EXEC-M held a dispatched item and the board said `building` for 11
        minutes while the session was dead. The answer was posted at 10:08:12, `time.updated` never
        moved off 10:08:12, and NO ASSISTANT TURN EVER BEGAN. Every detector we have reads messages
        and branches on their kind — check_stall wants an inert assistant turn to measure, and there
        was none to be inert. This is the PROGRESS_STOP hole one level deeper: that was a kind we
        did not handle, this is the absence of a kind at all.

        So this one is keyed on the SESSION, not on its messages. Our own post bumps `time.updated`,
        which is what makes the signal sharp: if it has not moved since, then nothing has happened
        since we prompted — not a slow turn, not a long tool call, nothing.

        DELIBERATELY NARROW. It fires only when there is no assistant turn newer than the newest
        user message. A session mid-turn is check_stall's and check_dead's business, and a long
        `bash` inside a live turn must never read as death — that is the false positive that would
        make this detector worth ignoring. The cost of the narrowness is that it catches one shape;
        the shape it catches was invisible to everything else for 11 minutes and would have stayed
        invisible for 30.
        """
        if not cur:
            return None                       # no dispatched item: an idle executor is not stuck
        mins = float(self.c("session_idle_stuck_minutes", 10))
        newest = recent[-1] if recent else None
        if not newest:
            return None
        def created(m):
            return float(((m.get("info", m) or {}).get("time") or {}).get("created") or 0)
        asst = next((m for m in reversed(recent or [])
                     if (m.get("info", m) or {}).get("role") == "assistant"), None)
        if asst is not None and created(asst) >= created(newest):
            return None                       # a turn began after our prompt: not this shape
        seen = getattr(self, "session_seen", {}).get(sid)
        if not seen:
            # An absence is not a measurement. Say so ONCE per session per process rather than
            # returning None as if the session had been checked and found healthy.
            told = self.state.setdefault("liveness_unmeasured", {})
            if told.get(sid) != "said":
                told[sid] = "said"
                self.emit("LIVENESS_NOT_MEASURED", name, sid, "-", f"item={cur['id']}",
                          f"the session listing carried no time.updated for {sid}, so session-level "
                          f"liveness is NOT being checked for it — this is unmeasured, not healthy")
            return None
        updated_ms, read_at = seen
        idle_s = time.time() - updated_ms / 1000.0
        if idle_s < mins * 60:
            return None
        key = (newest.get("info", newest) or {}).get("id")
        dead = self.state.setdefault("dead", {})
        if dead.get(sid) == key:
            return f"NO TURN STARTED {int(idle_s / 60)} min (escalated)"
        if self.compaction_active(sid):
            return None                       # a compaction is not death; COMPACTING owns that case
        # Shared with check_dead ON PURPOSE: the same silent stretch must not be escalated twice
        # under two names once this one has already woken BOSS.
        dead[sid] = key
        stale = int(time.time() - read_at)
        self.emit("STUCK", name, sid, key, f"item={cur['id']} no-turn={int(idle_s / 60)}min",
                  f"{name} holds {cur['id']} and its session has not updated for "
                  f"{int(idle_s / 60)} min — NO ASSISTANT TURN HAS BEGUN since it was last "
                  f"prompted. It cannot be fed either: a session with no message has no kind, so "
                  f"the tick has nothing to branch on. Re-post, or unassign the item so another "
                  f"executor can take it. (session clock read {stale}s ago)")
        self.escalate(f"STUCK: {name} ({sid}) holds {cur['id']} and has begun NO TURN for "
                      f"{int(idle_s / 60)} min. The board said `building` throughout. No auto-action "
                      f"taken; the item is not being worked.")
        return f"NO TURN STARTED {int(idle_s / 60)} min — escalated"

    def last_messages(self, sid, n):
        try:
            return http("GET", f"/session/{sid}/message?limit={int(n)}", timeout=10) or []
        except Exception as e:
            self.log(f"message fetch failed for {sid}: {e}")
            return []

    def working_item(self, sid, q, newest_id, dispatched=None):
        """The item the executor is ACTUALLY working, from its own newest instruction. Or None.

        2026-09-06 (BOSS): the board labelled EXEC-G "building B.014a.capability-descriptor" while
        EXEC-G was in fact running r3b. The label came from current_item() — the last item the
        DAEMON dispatched to that executor — and BOSS had re-assigned it by message. A label that
        cannot be trusted is worse than none: the reader stops checking it, and then believes it on
        the one night it matters.

        The executor's newest USER message is the most recent instruction it was given, whoever sent
        it, so it outranks the queue's record of what we dispatched. Message text is DATA: an id is
        accepted only if the queue already contains it, so nothing in a message can invent or rename
        an item, and an executor's own claim to have switched (an assistant message) is not an
        instruction and does not move the label.

        THE DISPATCHED ID WINS WHEN IT IS ALSO IN THE TEXT (BOSS, 2026-09-07). A rework message
        routinely cites a SIBLING item for context, and longest-first then reports the sibling: BOSS
        cited B.010.portal-platform-contention-mutex (38 chars) inside EXEC-G's rework and the board
        announced a re-assignment that never happened, twice in one minute. If the message names the
        item we dispatched, that is the item — length only breaks ties between the others. This does
        NOT cover a message that omits its own id entirely (BOSS's EXEC-G rework said
        "B.014a.artifact-custody: REWORK 4" with no `-r3`); nothing here can, and the fix for that
        one is the standing rule that every dispatch names its own id verbatim.

        `newest_id` is the id the tick has ALREADY fetched. It is the cache key, and it is a
        parameter rather than something this method looks up because a cache that has to fetch in
        order to decide whether to fetch saves nothing — measured: my first version called the API
        on every tick while looking cached.
        """
        cache = self.state.setdefault("label_probe", {})
        hit = cache.get(sid)
        # `dispatched` is part of the cache key: it can change while the executor stays silent, and a
        # cache keyed on the message alone would keep answering with the ranking from the old one.
        if hit and hit.get("msg_id") == newest_id and hit.get("dispatched") == dispatched:
            return hit.get("item")
        try:
            msgs = self.last_messages(sid, 6)
        except Exception:
            return None
        ids = [i["id"] for i in q["items"]]
        found = None
        for m in reversed(msgs or []):                  # newest first
            if ((m.get("info") or {}).get("role")) != "user":
                continue
            text = " ".join(str(pt.get("text") or "") for pt in (m.get("parts") or []))
            # longest id first: B.014a.artifact-custody is a PREFIX of B.014a.artifact-custody-r3b,
            # and a first/shortest match would report the parent while the executor builds the
            # rework — the same wrong-item bug this fix is about, arriving by another route.
            order = sorted(ids, key=len, reverse=True)
            if dispatched in ids:
                order = [dispatched] + [i for i in order if i != dispatched]
            for iid in order:
                if iid in text:
                    found = iid
                    break
            if found:
                break
        cache[sid] = {"msg_id": newest_id, "item": found, "dispatched": dispatched}
        return found

    def exec_label(self, name, sid, q, cur, suffix, newest_id=None):
        """The status-line label, saying only what it knows.

        Three cases, and the third is the point: when the executor's own instructions name an item
        that is not the one we dispatched, the QUEUE is the stale party and the label says so
        instead of quietly printing either id alone.
        """
        held = self.current_items(q, name)
        if len(held) > 1:
            # FAIL CLOSED. working_item() breaks ties by preferring the id we DISPATCHED, and with
            # two dispatched rows there is no single such id — so the tiebreak falls back to message
            # text. On 2026-09-07 that text was BOSS's answer, which cited a THIRD item as an
            # analogy, and the board printed a confident "building <that item> (RE-ASSIGNED)" for an
            # executor which had never been given it. The double-booking is the defect; this label
            # refuses to paper over it with a guess.
            return (f"holds {len(held)} rows at once ({', '.join(i['id'] for i in held)}) {suffix} "
                    f"— NOT LABELLED: with two dispatched rows there is no dispatched id to prefer, "
                    f"and the only tiebreak left is message text")
        act = self.working_item(sid, q, newest_id, dispatched=(cur or {}).get("id"))
        if cur and act and act != cur["id"]:
            return f"building {act} {suffix} (RE-ASSIGNED — queue still says {cur['id']})"
        if act:
            return f"building {act} {suffix}"
        if cur:
            return f"building {cur['id']} {suffix} (dispatched, unconfirmed)"
        return suffix

    def recover_missed_marker(self, name, sid, q):
        """Find a PLAN READY / REPORT READY that completed BEHIND a resume we posted.

        2026-09-06 20:45, EXEC-D. The stall detector called msg_077483e5e cut after 123 s of silence
        and posted a RESUME. The message was not dead: it completed at 20:50:55 carrying a full
        PLAN READY. But the daemon reads `?limit=1`, and by then the last message was our own
        resume, so the completed PLAN READY was never classified, no PLAN_READY event was logged,
        no plan review ran, and the executor parked 6 min later on a QUESTION about the review that
        never came. Scanning the cut message's parts does not reach this: at stall time it had no
        text at all. What loses the event is reading only the last message, so the fix has to look
        past it — but only after a resume, and only at assistant messages this daemon never handled.
        """
        last = self.state.setdefault("stall_resumed", {}).get(sid)
        if not last:
            return None
        seen = self.state.setdefault("recovered", {})
        for m in reversed(self.last_messages(sid, 5)):
            info = m.get("info", m)
            if info.get("role") != "assistant" or not (info.get("time", {}) or {}).get("completed"):
                continue
            mid = info.get("id")
            if mid in (self.state["handled"].get(sid), seen.get(sid)) or info.get("error"):
                continue
            txt = text_of(m)
            pm, rm = PLAN_RE.search(txt), REPORT_LINE_RE.search(txt)
            if not (pm or rm):
                continue
            kind = "PLAN_READY" if (pm and (not rm or pm.start() < rm.start())) else "REPORT_READY"
            seen[sid] = mid
            cur = self.current_item(q, name)
            self.emit(kind, name, sid, mid, f"item={cur['id'] if cur else '-'} RECOVERED",
                      "completed behind a dispatcher RESUME and was never the last message — "
                      "found by looking past it; " + clean_excerpt(txt))
            if kind == "PLAN_READY" and cur and os.path.exists(PLANREVIEW_FLAG):
                self.start_plan_review(name, sid, cur["id"], cur.get("artifact", ""), txt)
                return f"plan review {cur['id']} (recovered)"
            # A REPORT READY recovered this way is NOT queued for BOSS as if it had just arrived:
            # it may be the report the executor is about to re-post under the resume, and two rows
            # for one report is its own defect. The event is the signal; BOSS reads events.log.
            return f"{kind} recovered ({mid[-8:]})"
        return None

    # -- queue feeding
    def ledger_status(self):
        now = time.time()
        if now - getattr(self, "_ledger_at", 0) > 60:
            st, cur = {}, None
            try:
                for ln in open(LEDGER, errors="ignore"):
                    m = re.match(r"^ARTIFACT:\s+(\S+)", ln)
                    if m:
                        cur = m.group(1); continue
                    m = re.match(r"^STATUS:\s+(.*)", ln)
                    if m and cur:
                        st[cur] = m.group(1).strip(); cur = None
            except Exception as e:
                self.log(f"ledger unreadable: {e}")
            self._ledger, self._ledger_at = st, now
        return self._ledger

    def queue_lock(self):
        """Two writers on queue.json (daemon tick + BOSS's editor) lost a `reported` update at 13:13:25
        on 2026-09-05. The daemon takes this flock for the whole tick; queuectl.py takes it per edit."""
        import fcntl
        f = open(QUEUE_LOCK, "w")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    def load_queue(self):
        return load_json(QUEUE, {"items": []})

    def save_queue(self, q):
        if not self.dry:
            q["updated"] = now_local()
            save_json_atomic(QUEUE, q)

    def dep_done(self, status):
        return any(str(status).upper().startswith(p) for p in self.c("dep_ok_prefixes", ["LANDED", "MERGED"]))

    def dep_ok(self, dep, q=None):
        """Is a dependency satisfied?  Returns (ok, why).

        2026-09-06 (BOSS): this resolved deps against the LEDGER by ARTIFACT name only. Two items
        carry deps written as ITEM ids (`B.011.port-row-decoding`, `B.010.trunk-ruff-clean`), which
        never appear as an ARTIFACT line, so `ledger_status().get()` returned "" for a dependency
        that was in fact finished — a permanent block that the board rendered as an ordinary wait.
        Resolution order now: ledger artifact -> queue item by id -> queue item by artifact (and the
        ledger status of THAT item's artifact). A dep matching none of them is UNRESOLVABLE: it still
        blocks (dispatching on an unknown dependency is worse), but it is named as unresolvable on
        the board and emitted once as DEP_UNRESOLVABLE, so it can never again look like waiting.
        """
        led = self.ledger_status()
        if dep in led:
            return self.dep_done(led[dep]), f"ledger:{led[dep]}"
        items = (q or {}).get("items", [])
        for it in items:
            if it.get("id") == dep or it.get("artifact") == dep:
                art = it.get("artifact")
                if art and art in led and self.dep_done(led[art]):
                    return True, f"queue:{it['id']} ledger:{led[art]}"
                return self.dep_done(it.get("status", "")), f"queue:{it['id']} status={it.get('status')}"
        seen = self.state.setdefault("dep_unresolvable", {})
        if time.time() - float(seen.get(dep) or 0) > 3600:
            seen[dep] = time.time()
            self.emit("DEP_UNRESOLVABLE", "-", "-", "-", f"dep={dep}",
                      "matches no ledger ARTIFACT and no queue item id/artifact — blocking, and "
                      "BOSS must correct the dep or the ledger; it is NOT an ordinary wait")
        return False, "UNRESOLVABLE"

    def busy_worktrees(self, q):
        """Worktrees a dispatched item or a live Codex run is working in. A requeued item whose worktree is
        still occupied must wait (BOSS 2026-09-05 13:35: two Codex runs wrote the same worktree at once)."""
        busy = {it.get("worktree") for it in q["items"] if it.get("status") == "dispatched" and it.get("worktree")}
        for run in self.state.get("codex", {}).values():
            for it in q["items"]:
                if it["id"] == run.get("item") and it.get("worktree"):
                    busy.add(it["worktree"])
        return busy

    # An item is finished with its lane only when BOSS says so. Every state in which SOMEONE may be
    # reading or writing that lane head holds it: the executor still has the item (dispatched,
    # rework), or the head is under review (reported, gating, gated).
    #
    # BOSS, 2026-09-07: `gating` was NOT in this list, and it is the status BOSS's own hand-launched
    # gates write — four items carry it right now. So the lane freed the moment
    # B.014a.artifact-custody-r3 went to `gating`, and the daemon dispatched EXEC-G onto
    # lane/014a-assets while gate 46103 was running platform proofs in that exact worktree. An edit
    # mid-gate corrupts the proof run and the head row silently: the gate measures a tree that is
    # changing under it and reports a verdict about a sha that no longer describes what it tested.
    # `rework` was missing for the same reason — the executor is actively writing the lane.
    #
    # `merged`, `held` and `done` are BOSS's word that the item is off the lane. `landed` frees too;
    # queued/parked/broken never occupied it in the first place.
    LANE_HOLDING = ("dispatched", "rework", "reported", "gating", "gated")

    def running_gate_items(self):
        """Item ids with a LIVE mergegate process. The status is a record; this is the fact.

        A status can be stale in both directions — a gate that crashed leaves `gating` behind, and a
        gate BOSS launched by hand before the status was written is running against a lane the queue
        still calls `rework`. The process list cannot be stale, so it is checked as well as, not
        instead of, the status.
        """
        try:
            out = subprocess.run(["ps", "-eo", "args="], capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return None            # cannot tell -> caller treats every lane as held; never guess free
        ids = set()
        for ln in out.splitlines():
            if "dispatcher/mergegate.py" not in ln or "grep" in ln or "zsh" in ln:
                continue
            parts = ln.split()
            for i, tok in enumerate(parts):
                if tok.endswith("mergegate.py") and i + 1 < len(parts):
                    nxt = parts[i + 1]
                    if not nxt.startswith("-"):
                        ids.add(nxt)
                    break
        return ids

    def lane_occupied(self, q, item):
        """The item already under review on this item's lane or worktree, or None.

        2026-09-06 22:45:45: EXEC-G posted REPORT READY for B.014a.artifact-custody-r3 and IN THE
        SAME SECOND the daemon dispatched B.014a.capability-descriptor to the same executor, the
        same lane/014a-assets and the same worktree, while r3 was `reported` and ungated. The
        second build then writes the lane head that r3's report names. A merge of r3 would carry a
        half-built second item, and the gate's "sha on lane head" row would FAIL for a reason that
        is our scheduling and not the lane's — a finding that reads like the candidate's fault.
        busy_worktrees() did not cover this: it tracks `dispatched` only, and r3 had already moved
        past that. The window is exactly the review, which is the longest part of an item's life.
        """
        keys = {}
        for k in ("lane", "worktree"):
            v = item.get(k)
            if v and not str(v).startswith("("):      # "(as in Part 1.3)" is a placeholder, not a path
                keys[k] = v
        if not keys:
            return None
        gating_now = self.running_gate_items()
        for other in q["items"]:
            if other is item or other["id"] == item["id"]:
                continue
            st = str(other.get("status") or "").lower()
            live = gating_now is None or other["id"] in gating_now
            if st not in self.LANE_HOLDING and not live:
                continue
            for k, v in keys.items():
                if other.get(k) == v:
                    return (other, k)
        return None

    # `reported` is here for a reason BOSS measured at 03:26:43: feed() flips rework -> reported on
    # the REPORT READY message BEFORE the auto-gate check runs, so every report auto-flipped ITSELF
    # out of the match set. The next tick found no current item and logged "EXEC-F holds no
    # dispatched or rework item" about the very item that had just reported. The gate could only
    # fire on the first pass, and never again if that pass refused — which is exactly when a second,
    # corrected report arrives.
    #
    # `rework` is here, not just `dispatched`. Found 2026-09-07 while building the restart recovery:
    # autogate.GATEABLE_STATUS has always listed rework, but feed() reaches the auto-gate branch only
    # when current_item() returns something, and this returned None for every reworked item — so a
    # second REPORT READY after a rework could never launch a gate. The rule the name implies is the
    # right one: an item sent back for rework is still the executor's current work.
    CURRENT_STATUS = ("dispatched", "rework", "reported")

    def current_items(self, q, name):
        """EVERY row this executor holds right now — normally one, occasionally two.

        BOSS, 2026-09-07: a QUESTION parks a row, which FREES the executor, so the dispatcher does
        its job and fills the gap; `answer` then restored the parked row straight back to
        `dispatched` and EXEC-M held two. A caller that wants "the item" can still take the first.
        A caller that must not GUESS which of two is real asks for the list — see exec_label.
        """
        return [it for it in q["items"]
                if it.get("status") in self.CURRENT_STATUS and it.get("dispatched_to") == name]

    def current_item(self, q, name):
        held = self.current_items(q, name)
        return held[0] if held else None

    UNFINISHED = ("dispatched", "rework", "reported", "gating", "gated", "parked")

    def worktree_claim(self, q, it):
        """Is this row's worktree claim WRONG? -> (kind, reason) or None.

        BOSS, 2026-09-07, owning both cases: "I keep writing queue rows whose checkout claims are
        wrong." Twice in two hours — once a worktree that does not exist on disk, so the item
        dispatched into a session that then asked where its checkout was; once a row pointing at
        another item's checkout, on a lane name that is not a branch.

        These are not waits, they are errors in the row, and the difference matters: a wait clears
        itself and an error does not. So this is separate from lane_occupied (a real wait, on a real
        shared tree) and it escalates rather than sitting quietly at the back of the queue.
        """
        wt = str(it.get("worktree") or "")
        if wt.startswith("("):
            return None                     # deliberate opt-out: the item prompt names the checkout
        if not wt:
            # BOSS, 2026-09-07: refuse to dispatch a row that neither names a worktree nor says it
            # is deferring to the prompt. Everything downstream of dispatch — the report-path check,
            # the gate, the lane guard — reads `worktree`, and with it absent each one silently
            # answers about the wrong directory.
            #
            # It is keyed on the FIELD, not on the prompt text. A prose check was the obvious idea
            # and it is unbuildable: the standing "Rules that bite" boilerplate contains the word
            # `worktree` and sits in 40 of the item files, so any grep for it passes every row —
            # a guard that cannot fail, of the exact kind we keep finding. `(named in the prompt)`
            # in the field makes the same intention explicit and checkable.
            return "UNSET", (f"the row records no worktree at all. Everything after dispatch reads "
                             f"that field — the report-path check, the gate, the lane guard — and "
                             f"with it empty each of them answers about the daemon's own directory "
                             f"instead. Set `worktree` to the checkout, or to `(named in the item "
                             f"prompt)` if that is deliberate; the second is honoured and skipped.")
        if not os.path.isdir(wt):
            return "MISSING", (f"its worktree {wt} does not exist on disk — dispatching would send an "
                               f"executor to a checkout that is not there")
        for other in q["items"]:
            if other is it or other["id"] == it["id"]:
                continue
            if other.get("worktree") != wt:
                continue
            if str(other.get("status") or "").lower() in self.UNFINISHED:
                return "SHARED", (f"{other['id']} ({other.get('status')}) already claims the same "
                                  f"worktree {wt} — two unfinished items in one checkout overwrite "
                                  f"each other's lane head")
        return None

    def skip_queued(self, name, it, reason, escalate=False):
        """Say why a queued item was passed over. Once per item and reason. -> bool (spoke).

        Until 2026-09-07 eligible_item() skipped on three paths and only ONE of them said anything:
        missing deps and `worktree busy` both set blocked_on and continued in silence. B.010.perf-
        regression-relative-check then sat queued for six minutes with the board showing an idle
        executor, and afterwards the cause could not be established at all — the hand-dispatch had
        cleared blocked_on and nothing had been written down. A stall nobody can read is worse than
        a stall: the next one costs the same six minutes plus an investigation that ends in
        "likeliest".
        """
        seen = self.state.setdefault("queued_skip", {})
        key = f"{it['id']}|{reason[:60]}"
        if seen.get(it["id"]) == key:
            return False
        seen[it["id"]] = key
        self.emit("QUEUED_SKIPPED", name, "-", "-", f"item={it['id']}", reason)
        if escalate:
            self.escalate(f"QUEUE ROW {it['id']}: {reason}. It will NOT be dispatched until the row "
                          f"is corrected — this is an error in the row, not a wait.")
        return True

    def eligible_item(self, q, name):
        for it in q["items"]:
            if it.get("status") != "queued":
                continue
            if it.get("executor") and it["executor"] != name:
                continue
            missing = []
            for d in it.get("deps", []):
                ok, why = self.dep_ok(d, q)
                if not ok:
                    missing.append(d + (" [DEP_UNRESOLVABLE]" if why == "UNRESOLVABLE" else ""))
            if missing:
                it["blocked_on"] = missing
                self.skip_queued(name, it, "waiting on deps: " + ", ".join(missing))
                continue
            it.pop("blocked_on", None)
            claim = self.worktree_claim(q, it)
            if claim:
                kind, why = claim
                it["blocked_on"] = [f"worktree {kind.lower()}"]
                self.skip_queued(name, it, f"{kind}: {why}", escalate=True)
                continue
            if it.get("worktree") in self.busy_worktrees(q) and not str(it.get("worktree", "")).startswith("("):
                it["blocked_on"] = ["worktree busy"]
                self.skip_queued(name, it, f"its worktree {it.get('worktree')} is busy with a "
                                           f"dispatched item")
                continue
            held = self.lane_occupied(q, it)
            if held:
                other, key = held
                it["blocked_on"] = [f"{key} occupied by {other['id']} ({other.get('status')})"]
                # One event per pair, not one per tick: the daemon polls every 5s and a review runs
                # for tens of minutes, so an un-deduped event would bury the board it is meant to
                # inform. The item stays QUEUED — it is not broken, it is waiting on BOSS.
                seen = self.state.setdefault("lane_occupied", {})
                pair = f"{it['id']}<-{other['id']}"
                if seen.get(pair) != other.get("status"):
                    seen[pair] = other.get("status")
                    self.emit("LANE_OCCUPIED", name, "-", "-",
                              f"item={it['id']} waits on {other['id']} ({other.get('status')})",
                              f"{key}={it.get(key)} — {other['id']} is {other.get('status')} and "
                              f"ungated, so its report names a sha that {it['id']} would overwrite. "
                              f"{it['id']} stays QUEUED until BOSS moves {other['id']} to "
                              f"merged/held/rework.")
                continue
            return it
        return None

    def start_plan_review(self, name, sid, item_id, artifact, plan_text):
        """Review a PLAN block off the tick thread. The review is advisory and fail-open, so the
        worst case is an executor told to proceed with no findings (see planreview.__doc__)."""
        def run():
            try:
                import planreview
                verdict = planreview.review(item_id, plan_text, artifact)
                msg = planreview.format_feedback(item_id, verdict)
            except Exception as e:
                verdict = {"error": str(e), "gaps": []}
                msg = (f"DISPATCHER PLAN REVIEW {item_id} — NOT RUN ({e}). Proceed with the build; "
                       "the merge gate still runs your proofs in full.")
            n, err = len(verdict.get("gaps") or []), verdict.get("error")
            self.emit("PLAN_REVIEWED", name, sid, "-", f"item={item_id} gaps={n} {'error=' + str(err) if err else ''}",
                      clean_excerpt(msg))
            # PLAN_REVIEWED says the review RAN, which is true whatever the post does. But the
            # executor is stopped waiting for exactly this message, so a lost post leaves it idle
            # for as long as nobody looks, under a log line that reads as if it had been told.
            if not self.post_prompt(sid, msg):
                why = getattr(self, "_last_post_error", "") or "post_prompt returned False"
                self.emit("PLAN_REVIEW_UNDELIVERED", name, sid, "-", f"item={item_id}",
                          f"the review ran and its feedback did NOT reach {name}: {why}. The "
                          f"executor is stopped waiting for it and will not resume on its own.")
                self.escalate(f"PLAN REVIEW {item_id}: the feedback could not be delivered to {name} "
                              f"({why}). The executor is waiting on a message it never got.")
        if self.dry:
            self.log(f"[dry] would plan-review {item_id}")
            return
        threading.Thread(target=run, name=f"planreview-{item_id}", daemon=True).start()

    def dispatch(self, q, name, sid, item, parked=None):
        try:
            body = open(os.path.join(ITEMS_DIR, item.get("prompt_file") or f"{item['id']}.md")).read()
        except Exception as e:
            self.log(f"item {item['id']} has no prompt file: {e}")
            item["status"] = "broken"; item["error"] = str(e)
            return False
        text = ITEM_PROMPT.format(
            id=item["id"], worktree=item.get("worktree", "(as in Part 1.3)"), lane=item.get("lane", "-"),
            scope=", ".join(item.get("scope", [])) or "(none declared — the gate records NOT DECLARED, which blocks the merge and is BOSS's row to fill, not a finding about your work)",
            proof=", ".join(item.get("proof_files", [])) or "(declare in your PLAN block)", body=body,
            box=BOX_RECIPE,
            planfirst=(PLAN_FIRST if os.path.exists(PLANREVIEW_FLAG) and item.get("kind") == "build" else ""),
            parked=(f"Your previous item {parked} is PARKED on its QUESTION — do not touch it until BOSS answers. " if parked else ""),
        )
        if not self.post_prompt(sid, text):
            return False
        item.update({"status": "dispatched", "dispatched_to": name, "session": sid, "dispatched_at": now_local()})
        self.emit("DISPATCHED", name, sid, "-", f"item={item['id']}", item.get("title", ""))
        return True

    def feed(self, q, name, sid, kind, msg_id, text=""):
        """Called when an executor has just gone idle on a terminal message. Returns the label to show."""
        if not self.c("feed_queue", True) or os.path.exists(QUEUE_HOLD) or os.path.exists(os.path.join(HOLD_DIR, f"{name}.feed")):
            return None
        cur = self.current_item(q, name)
        parked = None
        if not cur and kind == "REPORT_READY" and self.c("auto_gate", True):
            # No dispatched/rework/reported item for this executor. Until 2026-09-07 this fell
            # straight through to the feed and said nothing at all, which is how EXEC-F's 02:58:53
            # report produced neither an AUTO_GATE nor an AUTO_GATE_SKIPPED for 90 seconds.
            #
            # BOSS's ruling, 2026-09-07: `gated` stays OUT of CURRENT_STATUS — the gate in flight
            # owns the sha. But "holds no dispatched or rework item" is a FALSE statement about that
            # case, and an executor told it will resend the report it just sent. Name the gate that
            # is running, and say wait.
            mid_gate = next((i for i in q["items"]
                             if i.get("dispatched_to") == name
                             and str(i.get("status") or "").lower() == "gated"), None)
            if mid_gate:
                why = (f"a merge gate for {mid_gate['id']} is already running and owns that sha — "
                       f"the report is not gated again; the verdict is coming")
                self._report_verdict = (msg_id, GATE_RUNNING, why)
                if self.skip_gate(name, sid, msg_id, f"item={mid_gate['id']}", why):
                    # BEST-EFFORT BY DECISION (BOSS's rule, 2026-09-08): honour the result only where
                    # a lost post changes what a person or the daemon later believes. This one moves
                    # no state and writes no record — AUTO_GATE_SKIPPED is already emitted above and
                    # is true whether or not the courtesy note lands. The worst case is an executor
                    # that resends a report the gate then ignores. Decided, not missed.
                    self.post_prompt(sid, f"Your report for {mid_gate['id']} arrived while its merge "
                                          f"gate is still running. Do NOT resend it and do NOT reply "
                                          f"to this note — wait; if the gate fails, a rework reaches "
                                          f"you with the findings.")
                return None
            # A PARKED row is not "no item" — it is a finished report with nothing pointing at it,
            # and it is a BOSS error every single time it happens: a QUESTION parked the row, the
            # answer went out through a side channel, and the park was never cleared. Three times on
            # 2026-09-07 (EXEC-J 04:57, EXEC-H 05:10, and one earlier), each time swallowed as an
            # IDLE_ACK. BOSS's own written checklist has now failed three times, which is evidence
            # about the instrument, not about his attention — so the daemon RAISES it.
            parked_row = next((i for i in q["items"]
                               if i.get("dispatched_to") == name
                               and str(i.get("status") or "").lower() == "parked"), None)
            if parked_row:
                why = (f"{name} reported {parked_row['id']}, whose row is PARKED — the report is "
                       f"finished work with nothing pointing at it, and `parked` cannot be gated. "
                       f"A park is only cleared by answering the QUESTION that caused it: "
                       f"`./dispatcherctl.sh answer {parked_row['id']} \"<your reply>\"` posts and "
                       f"un-parks in one step. Nothing was gated and nothing was changed.")
                self._report_verdict = (msg_id, UNGATEABLE, why)
                self.emit("REPORT_ON_PARKED_ROW", name, sid, msg_id, f"item={parked_row['id']}", why)
                self.escalate(f"REPORT ON A PARKED ROW: {why}")
                # BEST-EFFORT BY DECISION: the event and the escalation above carry the whole fact to
                # BOSS, who is the one who must act. This note only spares the executor a resend.
                self.post_prompt(sid, f"Your report for {parked_row['id']} arrived. Its queue row is "
                                      f"parked pending an answer to your QUESTION, so nothing has "
                                      f"gated it yet — that is on us, not you. Do NOT resend and do "
                                      f"NOT reply to this note; the gate follows once the park is "
                                      f"cleared.")
                return None
            why = (f"{name} holds no dispatched, rework or reported item — nothing to gate this "
                   f"report against")
            self._report_verdict = (msg_id, UNGATEABLE, why)
            self.skip_gate(name, sid, msg_id, "item=-", why)
        if cur:
            if kind == "REPORT_READY":
                # Judge the status the item had BEFORE this message. `cur` is about to become
                # "reported", and a check run after that update could never fail — it would be
                # asking "is this item reported?" one line after setting it.
                prev_status_item = dict(cur)
                cur.update({"status": "reported", "report_msg_id": msg_id, "reported_at": now_local()})
                if self.c("auto_gate", True):
                    import autogate
                    # The marker alone is NOT the trigger. BOSS, 2026-09-07: an "are you idle?" note
                    # was answered "REPORT READY: idle — lane clean (merged ...)" and would have
                    # launched a gate on a merged item.
                    checker, cwhy = self.path_checker(cur)
                    # ORDER MATTERS, and my first version had it wrong: refusing on a missing
                    # checkout BEFORE reading the text turns every idle ack on a worktree-less row
                    # into a pending row for BOSS. "The message names no report path" is a judgement
                    # that needs no checkout — the checkout is only load-bearing once a path has to
                    # be resolved. My own control in test_reportheld caught this.
                    if checker is None and autogate.REPORT_PATH_RE.search(text or ""):
                        # A real report we cannot judge. UNGATEABLE, not False: the row stays with
                        # BOSS, because "we could not check" is the one verdict a human has to see.
                        self._report_verdict = (msg_id, UNGATEABLE, cwhy)
                        self.skip_gate(name, sid, msg_id, f"item={cur['id']}", cwhy)
                        return None
                    # Unreachable when a path is named (guarded above), so this stands in only for
                    # the text-only verdicts the trigger reaches first.
                    ok, why = autogate.report_trigger_ok(prev_status_item, text,
                                                         checker or (lambda rel: False))
                    # The verdict is parked for the caller: an idle ack that the trigger has just
                    # judged NOT a report must not also raise a pending row for BOSS to read and
                    # clear by hand (BOSS cleared two within four minutes on 2026-09-07).
                    if ok:
                        # Owner directive 2026-09-07: a report without exactly one INTERRUPT-TEST
                        # line is not gated. Checked AFTER the report-path evidence, so an idle ack
                        # is still an idle ack and not "missing an artefact"; and kept OUT of
                        # report_trigger_ok so the recovery pass and the manual verb are unaffected —
                        # a gate BOSS names by hand is BOSS's judgement, not the executor's paperwork.
                        iok, iwhy = autogate.interrupt_test_ok(text)
                        if not iok:
                            spoke = self.skip_gate(name, sid, msg_id, f"item={cur['id']}", iwhy)
                            # One line back to the executor: it can fix this without redoing work,
                            # and an unexplained non-gate is how a report sits for hours. Posted only
                            # when the skip is NEW — the tick re-feeds an idle executor every 5s, and
                            # the same correction repeated forever is noise the executor cannot act on.
                            if spoke:
                                # BEST-EFFORT BY DECISION: the row and the skip event already say the
                                # report was not gated and why. This is the courtesy copy to the
                                # executor so it can fix the paperwork without redoing work.
                                self.post_prompt(sid, f"Your REPORT READY for {cur['id']} was NOT gated: "
                                                      f"{iwhy}. {autogate.INTERRUPT_FIX}")
                            # The row STAYS: this is a real report BOSS should see waiting, unlike an
                            # idle ack. Only the "not a report at all" verdict withdraws a row.
                            self._report_verdict = (msg_id, True, iwhy)
                            return None
                        self.log(f"interrupt test ok for {cur['id']}: {iwhy}")
                    self._report_verdict = (msg_id, ok, why)
                    if not ok:
                        self.skip_gate(name, sid, msg_id, f"item={cur['id']}", why)
                        self.log(f"auto-gate NOT launched for {cur['id']}: {why}")
                    else:
                        sha, gwhy = self.launch_gate(cur)
                        if not sha:
                            # BOSS, 2026-09-07: a deferral was a log line only, so a report that was
                            # never gated looked exactly like one nobody had reported. Silence is the
                            # defect: EVERY non-launch says why, in the events log, where the board
                            # reads it.
                            self.skip_gate(name, sid, msg_id, f"item={cur['id']}",
                                           f"deferred, NOT launched: {gwhy}")
                            self.log(f"auto-gate deferred for {cur['id']}: {gwhy}")
            elif kind == "QUESTION":
                # parked_from is what `answer` restores. Without it an un-park has to guess, and a
                # guess that lands on the wrong status is the same class of silent drift the park
                # itself caused.
                cur.update({"status": "parked", "question_msg_id": msg_id, "parked_at": now_local(),
                            "parked_from": cur.get("status")})
                parked = cur["id"]
                if not self.c("feed_over_question", True):
                    return None
                now_ms = time.time() * 1000
                recent = [t for t in self.state["parks"].get(name, []) if now_ms - t < 3600000] + [now_ms]
                self.state["parks"][name] = recent
                if len(recent) >= int(self.c("max_parks_per_hour", 2)):
                    # runaway: EXEC-A parked five items in four minutes on 2026-09-05 — each new item answered
                    # with a QUESTION and the daemon kept feeding. Stop feeding this executor until BOSS clears it.
                    self.emit("FEED_PAUSED", name, sid, msg_id, f"{len(recent)} parks in the last hour — not fed until hold/{name}.feed is removed")
                    self.notify_owner("executor parking repeatedly", f"{name}: {len(recent)} QUESTIONs in an hour; feeding paused")
                    if not self.dry:
                        open(os.path.join(HOLD_DIR, f"{name}.feed"), "w").write("paused by circuit breaker; BOSS: answer its QUESTIONs then delete this file\n")
                    return None
            else:
                return None
        nxt = self.eligible_item(q, name)
        if nxt is None:
            return None
        if self.dispatch(q, name, sid, nxt, parked=parked):
            return f"dispatched {nxt['id']}"
        return None

    # -- Codex executors (owner order 2026-09-05: Plan 010 residuals and other sensitive items go to Codex, gpt-6-astra medium)
    def _queue_items(self):
        """The queue's items, for counting how many conversations sit on each account. Best effort:
        a load count that cannot be read is a worse-placed session, never a failed dispatch."""
        try:
            return json.load(open(os.path.join(STATE_DIR, "queue.json"))).get("items", [])
        except Exception:  # noqa: BLE001
            return []

    def codex_slots(self):
        return [f"CODEX-{i + 1}" for i in range(int(self.c("codex_slots", 0)))]

    def refuse_spawn(self, slot, why):
        """Record WHY a spawn did not happen, and return False. -> False.

        The board showed "idle: no eligible CODEX item" for a refusal, a deferral and a queue that
        was never read. Every path out of codex_spawn that does not start a worker comes through
        here, so the reason exists at the moment the slot's status line is written rather than being
        reconstructed from a log afterwards.
        """
        self._spawn_refusal[slot] = why
        return False

    def codex_spawn(self, slot, item):
        self._spawn_refusal.pop(slot, None)
        if getattr(self, "observing", False):
            # An actuator, and the most expensive one: it creates a worktree and starts a paid run.
            self.log(f"OBSERVE-ONLY: would spawn codex {slot} for {item['id']}")
            return self.refuse_spawn(slot, "OBSERVE-ONLY: actuators held")
        # STAGGER. Every spawn does a `git worktree add` AND a `uv sync --frozen` before the model
        # is ever called, so ten simultaneous spawns make SETUP the bottleneck and all ten pay for
        # each other. This defers rather than refuses: the item stays exactly where it was and the
        # next tick takes it. Zero disables it.
        gap = float(self.c("codex_spawn_stagger_seconds", 20))
        mstate = self.state.setdefault("muse", {})
        since = time.time() - float(mstate.get("last_spawn_at", 0))
        if gap > 0 and since < gap:
            self.log(f"{slot}: deferring {item['id']} for {gap - since:.0f}s — a spawn started "
                     f"{since:.0f}s ago and setup (worktree add + uv sync) is the bottleneck")
            return self.refuse_spawn(slot, f"staggered: waiting {gap - since:.0f}s behind the last spawn")
        wt = item.get("worktree", "")
        trunk = os.path.join(CN, "voicepod-plan010-rebuild")
        if not os.path.isdir(wt) and not self.dry:
            lane = item.get("lane") or f"lane/{item['id'].lower()}"
            r = subprocess.run(["git", "-C", trunk, "worktree", "add", "-b", lane, wt, "plan010/rebuild"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0 and "already exists" in (r.stderr or ""):
                # the branch survived an earlier attempt (2026-09-05): attach it instead of refusing
                r = subprocess.run(["git", "-C", trunk, "worktree", "add", wt, lane], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                self.log(f"{slot}: worktree add failed for {item['id']}: {r.stderr.strip()[:200]}")
                item["status"] = "broken"; item["error"] = r.stderr.strip()[:200]
                return self.refuse_spawn(slot, f"worktree add failed: {r.stderr.strip()[:120]}")
        venv_py = os.path.join(wt, "platform", ".venv", "bin", "python")
        venv_ok = os.path.exists(venv_py) and subprocess.run([venv_py, "-c", "import pytest, pgserver"], capture_output=True).returncode == 0
        if not venv_ok:  # a venv Codex half-built offline exists but cannot import pytest (2026-09-05)
            # Codex's sandbox has no network, so it cannot `uv sync` itself (2026-09-05, both first reports):
            # provision the frozen venv here, outside the sandbox, before the run starts.
            r = subprocess.run([self.c("uv_bin", "/Users/dhairyabajaria/.local/bin/uv"), "sync", "--frozen"],
                               cwd=os.path.join(wt, "platform"), capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                self.log(f"{slot}: uv sync failed for {item['id']}: {r.stderr.strip()[-200:]}")
                item["status"] = "broken"; item["error"] = "uv sync failed: " + r.stderr.strip()[-200:]
                return self.refuse_spawn(slot, "uv sync --frozen failed")
        try:
            body = open(os.path.join(ITEMS_DIR, item.get("prompt_file") or f"{item['id']}.md")).read()
        except Exception as e:
            item["status"] = "broken"; item["error"] = str(e)
            return self.refuse_spawn(slot, f"prompt file unreadable: {e}")
        resume = ""
        if item.get("codex_continue"):
            resume = (f"RESUME (continue {item['codex_continue']} of {self.c('max_auto_continue', 3)}): your previous run on this item "
                      "ended without REPORT READY or a QUESTION. Do NOT start over: run `git status --short` and `git log --oneline -5` "
                      "first, keep every uncommitted change, and continue from where the disk says you are.\n\n")
        # planfirst is always empty for Codex: a Codex run is a one-shot subprocess with no live
        # session to post review feedback back into, so a PLAN READY stop would strand it.
        prompt = CODEX_PROMPT.format(slot=slot, trunk=trunk, worktree=wt) + resume + ITEM_PROMPT.format(
            planfirst="", box="",  # Codex is box-FREE (CODEX_PROMPT forbids box.lock.d)
            id=item["id"], worktree=wt, lane=item.get("lane", "-"), parked="",
            scope=", ".join(item.get("scope", [])) or "(none declared)",
            proof=", ".join(item.get("proof_files", [])) or "(declare in your PLAN block)", body=body)
        log = os.path.join(CODEX_DIR, f"{datetime.now():%Y%m%d-%H%M}-{slot}-{item['id']}.log")
        roots = json.dumps([self.c("codex_git_dir", os.path.join(CN, "voice-pod", ".git")), os.path.join(CN, "test-logs")])
        # ROUTING (2026-09-08, wiring D). One builder for both routes, in museadapter, so this path
        # and run_attempt cannot drift apart. A profile route passes `-p <profile>` and NOTHING else:
        # the profile carries its own model, provider AND `model_reasoning_effort = xhigh`, and the
        # global `codex_effort` default is Astra's `medium` — passing it here would downgrade every
        # Muse worker with no error and a dispatch that looks correct. Nothing is re-routed by
        # default: with no `codex_routes` and no item `profile`, a slot keeps the historic Astra pair.
        # SELECTION, not just routing. A static per-slot map uses two keys, rotates none, and lets a
        # dead key take its slot down with it. select_profile distributes across the healthy Go
        # profiles, steps over a held one, and falls back to zen only when every Go key is held.
        # `last` lives in self.state so the rotation survives a tick: kept in a local, it would
        # restart at the head every time and hand every dispatch to muse-go-1.
        if not self.dry:
            try:
                persisted = museadapter.musesession.existing(CODEX_DIR, item["id"])
                if persisted and not item.get("profile"):
                    item["muse_profile"] = persisted["route"]["profile"]
            except (ValueError, OSError) as e:
                return self.refuse_spawn(slot, f"Muse conversation identity refused: {e}")
        profile, pnote = museadapter.select_profile(slot, item, self.cfg, mstate,
                                                    items=self._queue_items())
        if profile == museadapter.WAIT:
            # NOT a failure and NOT a re-placement. This conversation is warm on a key that is
            # behind a rolling wall, and its cached prefix is worth more than the wait. The item is
            # left exactly as it was for a later tick.
            self.log(f"{slot}: {item['id']} waits — {pnote}")
            return self.refuse_spawn(slot, f"waiting for its own key — {pnote}")
        if profile is None:
            self.log(f"{slot}: no usable profile for {item['id']}: {pnote}")
            item["status"] = "broken"; item["error"] = f"no usable profile: {pnote}"[:200]
            self.emit("ERROR", slot, "-", "-", f"item={item['id']}", f"no usable profile: {pnote}")
            return self.refuse_spawn(slot, f"no usable profile: {pnote}")
        if profile != (self.cfg.get("codex_routes") or {}).get(slot):
            # A substitution nobody can see is the failure this layer exists to prevent.
            self.log(f"{slot}: profile {profile} — {pnote}")
        rflags, renv, rspec, rwhy = museadapter.route_flags(
            profile, env=dict(os.environ),
            legacy_model=self.c("codex_model", "gpt-6-astra"),
            legacy_effort=self.c("codex_effort", "medium"))
        if rflags is None:
            # A credential this launcher cannot find is not a transient: hold the profile so the
            # NEXT dispatch steps over it instead of repeating the same refusal on every tick.
            if profile != museadapter.LEGACY:
                museadapter.mark_unhealthy(mstate, profile, museadapter.AUTH, why=rwhy)
            # Refused BEFORE the paid work. A missing key or an unreadable profile discovered after
            # the worktree add and the uv sync costs both of them and shows DISPATCHED on the board.
            self.log(f"{slot}: route refused for {item['id']} ({profile}): {rwhy}")
            item["status"] = "broken"; item["error"] = f"route {profile}: {rwhy}"[:200]
            self.emit("ERROR", slot, "-", "-", f"item={item['id']}", f"route {profile} refused: {rwhy}")
            return self.refuse_spawn(slot, f"route {profile} refused: {rwhy}")
        conversation = None
        if profile != museadapter.LEGACY and not self.dry:
            try:
                # Durable before Popen, including retries after a failed spawn/restart.
                conversation = museadapter.musesession.prepare(
                    CODEX_DIR, item["id"], rspec, worktree=wt, replace_profile=True)
                rflags += museadapter.musesession.flags(conversation)
            except (ValueError, OSError) as e:
                return self.refuse_spawn(slot, f"Muse conversation identity refused: {e}")
        cmd = [self.c("codex_bin", "codex"), "exec", "-s", "workspace-write", "-c", f"sandbox_workspace_write.writable_roots={roots}",
               *rflags, "--skip-git-repo-check", prompt]
        if self.dry:
            self.log(f"DRY-RUN would spawn codex for {item['id']} in {wt} (log {os.path.basename(log)})")
            return True
        try:
            with open(log, "w") as f:
                # The header is written from the RESOLVED per-slot values, not the globals. With a
                # global header a Muse run is logged as Astra, and every later attribution taken
                # from these headers is wrong — a run that succeeds under a false name.
                f.write(f"# {slot} {item['id']} start={now_local()} cwd={wt} "
                        f"profile={rspec['profile']} model={rspec['model']}/"
                        f"{rspec.get('effort') or 'profile-supplied'}\n")
                env = dict(renv)
                env["PATH"] = os.path.dirname(self.c("codex_bin", "codex")) + ":" + env.get("PATH", "/usr/bin:/bin")  # launchd PATH has no node
                proc = subprocess.Popen(cmd, cwd=wt, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        except Exception as e:  # a spawn failure (2026-09-05: launchd PATH had no `codex`) must not abort the tick
            self.log(f"{slot}: spawn failed for {item['id']}: {e}")
            item["status"] = "broken"; item["error"] = f"codex spawn failed: {e}"
            self.emit("ERROR", slot, "-", "-", f"item={item['id']}", f"codex spawn failed: {e}")
            return self.refuse_spawn(slot, f"launch failed: {type(e).__name__}: {e}")
        # AFFINITY IS WRITTEN ON THE ITEM, which is what makes it survive. Until now the chosen
        # profile lived only in self.state["codex"][slot] — slot-keyed RUN state — so a resume or a
        # re-spawn of the same item re-entered selection with nothing, was placed again, and landed
        # somewhere else with its warm prefix discarded and re-sent at full price. The state map is
        # only a load index; the item's own field is what select_profile reads.
        if profile not in (museadapter.LEGACY,) and not item.get("profile"):
            item["muse_profile"] = profile
            mstate.setdefault("affinity", {})[item["id"]] = profile
        mstate["last"] = profile      # tie-break only: placement is least-loaded, not a cursor
        mstate["last_spawn_at"] = time.time()   # the stagger clock starts when a spawn SUCCEEDS
        self.state["codex"][slot] = {"item": item["id"], "pid": proc.pid, "log": log, "started_ms": int(time.time() * 1000),
                                     "provider_conversation": conversation["header"] if conversation else None,
                                     "provider_conversation_record": conversation,
                                     "profile_note": pnote, "cwd": wt,   # the fallback route resolver's only discriminator
                                     "profile": rspec["profile"], "model_intended": rspec["model"],
                                     "provider_intended": rspec.get("provider"), "effort_intended": rspec.get("effort")}
        item.update({"status": "dispatched", "dispatched_to": slot, "session": f"codex:{proc.pid}", "dispatched_at": now_local()})
        self.emit("DISPATCHED", slot, f"codex:{proc.pid}", "-", f"item={item['id']}", item.get("title", ""))
        return True

    def codex_tick(self, q, pending, exec_status, fed_this_tick, max_feed):
        max_ms = int(self.c("codex_max_minutes", 180)) * 60000
        for slot in self.codex_slots():
            run = self.state["codex"].get(slot)
            if run:
                # Liveness: waitpid reaps our own child (and sees its exit); after a daemon restart the run is
                # NOT our child, waitpid raises ChildProcessError, and only kill(pid, 0) can answer — treating
                # that as "dead" marked six live runs broken and respawned them on 2026-09-05.
                try:
                    done_pid, _status = os.waitpid(run["pid"], os.WNOHANG)
                    alive = done_pid == 0
                except ChildProcessError:
                    try:
                        os.kill(run["pid"], 0)
                        alive = True
                    except ProcessLookupError:
                        alive = False
                except OSError:
                    alive = True
                if alive and time.time() * 1000 - run["started_ms"] > max_ms:
                    try:
                        os.killpg(run["pid"], 15)
                    except Exception:
                        pass
                    alive = False
                    run["timed_out"] = True
                if alive:
                    exec_status[slot] = f"building {run['item']} busy {int((time.time()*1000 - run['started_ms'])/60000)} min"
                    continue
                # finished: classify ONLY the final assistant message. `codex exec` repeats it after the
                # `tokens used\n<n>` trailer; grepping the whole tail matched BOSS's answer tokens echoed in
                # commits and comments (BOSS, 2026-09-05 13:00). A clean exit with neither token is a turn
                # end, not an error: the model stops after a tool step exactly like Muse's progress-line stop.
                try:
                    whole = open(run["log"], errors="ignore").read()
                except Exception:
                    whole = ""
                m_final = re.search(r"\ntokens used\n[\d,]+\n(.*)\Z", whole, re.S)
                text = m_final.group(1) if m_final else whole.rsplit("\ncodex\n", 1)[-1][-4000:]
                if run.get("timed_out"):
                    kind = "STUCK"
                elif REPORT_RE.search(text):
                    kind = "REPORT_READY"
                elif re.search(r"(^|\n)\s*\**QUESTION\b", text):
                    kind = "QUESTION"
                elif not whole.strip() or "ERROR" in whole[-800:] or "error" in whole[-300:].lower() and len(whole) < 2000:
                    kind = "ERROR"
                else:
                    kind = "TURN_ENDED"
                # THE ROUTE IS READ BACK, not assumed from the flags we passed. The flags are what we
                # asked for; session metadata is what ran. A worker that did the work on a route
                # nobody asked for is not a smaller problem than one that crashed, and a mismatch
                # here is the difference between passing the right flags and knowing they took.
                # UNMEASURED is not a pass and is not a failure either: it is reported and the run's
                # own verdict stands, because a rollout we cannot read says nothing about the work.
                # A key that refused or ran out is HELD here, where the evidence is, so the next
                # dispatch steps over it. AUTH waits for a human, QUOTA waits for a clock, and
                # TRANSPORT is not held at all — mark_unhealthy keeps that distinction.
                failed = museadapter.classify_failure(whole)
                if failed in (museadapter.AUTH, museadapter.QUOTA):
                    # A REFUSAL IS NOT A FAILURE, and the board must not read it as one. The provider
                    # declined to run: nothing about the item, the worktree or the work is wrong, and
                    # a rework or an auto-continue would just be refused again. Until now a quota
                    # refusal was invisible here and showed only in the provider's own log — BOSS,
                    # 2026-09-08. The distinct kind is what makes "the key is spent" legible next to
                    # "the code is broken".
                    kind = "REFUSED"
                if failed in (museadapter.AUTH, museadapter.QUOTA) and run.get("profile"):
                    # The SCOPE is parsed from the log itself, not from the summary line below it:
                    # a rolling wall and a weekly wall call for opposite actions (wait vs move) and
                    # only the provider's own words distinguish them.
                    scope = museadapter.quota_scope(whole) if failed == museadapter.QUOTA else None
                    museadapter.mark_unhealthy(
                        self.state.setdefault("muse", {}), run["profile"], failed,
                        minutes=int(self.c("quota_hold_minutes", 60)), scope=scope,
                        why=f"{run['item']} on {slot}, log {os.path.basename(run['log'])}")
                    self.log(f"{slot}: holding {run['profile']} — {failed}"
                             + (f" ({scope} wall)" if scope else "")
                             + f" reported by {run['item']}")
                # REFUSED AND NOT MEASURED ARE DIFFERENT FACTS ABOUT DIFFERENT SUBJECTS. REFUSED is
                # the provider declining to run. NOT MEASURED is US failing to observe — a statement
                # about our instrument, not about the work. BOSS, 2026-09-08: the first real Muse
                # sweep succeeded (26 KB report, two commits, positive control 5/5, 40 candidate
                # rows, a REJECTED section, and it refuted his seed) and was recorded broken because
                # the CLI printed no session id. Conflating the two discards exactly the output we
                # most want to keep. So: an unresolvable route blocks a MERGE — that ruling stands,
                # and it is written onto the item where the gate reads it — but it never sets `kind`
                # and never marks finished work broken.
                route_note, route_state, route_detail = "", None, ""
                if run.get("provider_intended"):
                    spec = {"profile": run.get("profile"), "model": run.get("model_intended"),
                            "provider": run.get("provider_intended"), "effort": run.get("effort_intended")}
                    sid = museadapter.session_id_from_jsonl(whole)
                    # ITEM 5. THE ROOT COMES FROM THE PROFILE THAT RAN. Both resolvers below used to
                    # be called with no root, so they fell back to the module constant
                    # `SESSIONS_ROOT` (~/.codex/sessions) — while `3b38f14` gives each profile an
                    # isolated CODEX_HOME and the spawn writes its rollout under THAT. The detector
                    # was enumerating a directory the spawns no longer write to, and three runs whose
                    # rollouts were sitting on disk were reported "route NOT MEASURED — WE could not
                    # observe what ran", blocking their merges. A false negative from a literal path.
                    sroot, srwhy = museadapter.sessions_root_for(run.get("profile"))
                    roll, rwhy, how = None, srwhy, ""
                    if sroot:
                        roll, rwhy = museadapter.rollout_for_session(sid, root=sroot)
                    if not roll and sroot:
                        # The id is the CHEAP route to the rollout, not the only one. When the CLI
                        # does not print one, exactly one session written in this run's window with
                        # this run's cwd identifies it — which is how BOSS resolved it by hand.
                        # SAME ROOT as above, never a union: searching both would let a rollout from
                        # the shared legacy root satisfy a spawn that ran with an isolated home,
                        # which is precisely the claim this must never make.
                        roll, how = museadapter.rollout_by_window(
                            run.get("cwd"), run.get("started_ms"), int(time.time() * 1000),
                            root=sroot)
                        if not roll:
                            rwhy = f"{rwhy}; and the time-and-cwd fallback found nothing: {how}"
                            how = ""
                    verdict, note = museadapter.route_matches(
                        spec, museadapter.resolved_route(roll) if roll else {})
                    route_state = verdict
                    if verdict is False:
                        kind = "ROUTE_MISMATCH"
                        route_note = note
                    elif verdict is None:
                        # BOTH reasons, never `note or rwhy`. The matcher's note says "there was
                        # no observed route to compare"; the RESOLVER'S why says WHICH DIRECTORY
                        # came up empty — and `or` threw the second one away, because the matcher
                        # always has something to say. That is how item 5 stayed invisible across
                        # three runs: the line named the failure and never named the place, so
                        # nobody could see it was looking in the wrong directory.
                        route_note = ("route NOT MEASURED — WE could not observe what ran. This says "
                                      "nothing about the work and does not mark it broken; it does "
                                      "block a merge, because an attempt whose route is unknown "
                                      "cannot be cited as evidence for the route it intended. "
                                      + " ".join(x for x in (note, rwhy) if x))
                    elif how:
                        route_note = f"route VERIFIED, resolved without a session id: {how}"
                    if sid and run.get("provider_conversation_record"):
                        try:
                            museadapter.musesession.bind(CODEX_DIR, sid, run["provider_conversation_record"])
                        except (ValueError, OSError) as e:
                            route_state = None
                            route_note = f"Muse exact-session identity binding refused: {e}"
                    route_detail = route_note
                    if route_note:
                        self.log(f"{slot}: {route_note}")
                exc = clean_excerpt(((route_note + "\n\n") if route_note else "") + text[-1500:])
                key = f"codex:{slot}"
                self.emit(kind, slot, key, "-", f"item={run['item']} log={os.path.basename(run['log'])}", exc)
                pending[key] = {"executor": slot, "session": key, "kind": kind, "msg_id": os.path.basename(run["log"]),
                                "item": run.get("item"),
                                "since_ms": int(time.time() * 1000), "since_local": now_local()[11:], "excerpt": exc,
                                "escalated": 0, "esc_count": 0, "last_esc_min": 0, "esc_base_ms": int(time.time() * 1000)}
                for it in q["items"]:
                    if it.get("status") == "dispatched" and it.get("dispatched_to") == slot:
                        if kind == "TURN_ENDED":
                            n = int(it.get("codex_continue", 0)) + 1
                            if n <= int(self.c("max_auto_continue", 3)):
                                it.update({"status": "queued", "executor": slot, "codex_continue": n, "dispatched_to": ""})
                                pending.pop(key, None)
                                self.emit("AUTO-CONTINUE", slot, key, "-", f"{n}/{self.c('max_auto_continue', 3)} item={it['id']} — turn ended without a token; re-dispatching to resume", exc)
                            else:
                                it.update({"status": "parked", "reported_at": now_local()})
                                pending[key]["kind"] = "STUCK"
                        else:
                            it.update({"status": "reported" if kind == "REPORT_READY" else ("parked" if kind == "QUESTION" else "broken"),
                                       "reported_at": now_local(),
                                       **({"parked_from": it.get("status")} if kind == "QUESTION" else {})})
                        if run.get("provider_intended"):
                            # The gate cannot re-derive this: the rollout window has passed and the
                            # run state is gone. Written on the ITEM because that is what survives.
                            # THREE STATES, THREE FIELDS. A MISMATCH IS MEASURED — it is a failure
                            # of the candidate and reads as one. UNMEASURED is a failure of the
                            # instrument and reads as "no result", which blocks a merge without
                            # calling the work bad. Collapsing them into one boolean is the same
                            # mistake one level up.
                            it["route_verified"] = route_state is True
                            it.pop("route_unverified", None)
                            it.pop("route_mismatch", None)
                            if route_state is False:
                                it["route_mismatch"] = route_detail[:400]
                            elif route_state is None:
                                it["route_unverified"] = (route_detail or "route not established")[:400]
                del self.state["codex"][slot]
                run = None
            if not run and fed_this_tick < max_feed and self.c("feed_queue", True) and not os.path.exists(QUEUE_HOLD) \
                    and not os.path.exists(os.path.join(HOLD_DIR, f"{slot}.feed")):  # BOSS parks a slot with hold/CODEX-n.feed
                nxt = None
                for it in q["items"]:
                    if it.get("status") != "queued" or it.get("executor") not in ("CODEX", slot):
                        continue
                    missing = []
                    for d in it.get("deps", []):
                        ok, why = self.dep_ok(d, q)
                        if not ok:
                            missing.append(d + (" [DEP_UNRESOLVABLE]" if why == "UNRESOLVABLE" else ""))
                    if missing:
                        it["blocked_on"] = missing; continue
                    if it.get("worktree") in self.busy_worktrees(q):
                        it["blocked_on"] = ["worktree busy"]; continue
                    nxt = it; break
                if nxt and self.codex_spawn(slot, nxt):
                    fed_this_tick += 1
                    if pending.pop(f"codex:{slot}", None):
                        self.emit("CLEARED", slot, f"codex:{slot}", "-", "new item spawned")
                    exec_status[slot] = f"dispatched {nxt['id']}"
                elif not run:
                    # THREE STATES, NOT ONE. "idle: no eligible CODEX item" was covering three
                    # different situations that need three different responses: nothing is queued
                    # (fine), something was queued and the spawn refused or deferred it (act on the
                    # reason), and the tick never reached this code at all (the daemon is degraded).
                    # A single string for all three is how sixteen hours of never-evaluated read as
                    # an empty queue.
                    if nxt:
                        exec_status[slot] = (f"REFUSED {nxt['id']}: "
                                             + (self._spawn_refusal.get(slot) or "no reason recorded"))
                    else:
                        exec_status[slot] = "idle: no CODEX item queued"
        return fed_this_tick

    def write_heartbeat(self, degraded=""):
        """The heartbeat, carrying the reason when this tick did nothing. Both readers print it."""
        if self.dry:
            return
        try:
            with open(HEARTBEAT, "w") as f:
                f.write(now_local() + (f" — DEGRADED: {degraded}" if degraded else "") + "\n")
        except OSError:
            pass

    def codex_only_pass(self):
        """Dispatch to the CODEX slots on a tick that is returning early for opencode's sake.

        CODEX WORKERS DO NOT USE THE OPENCODE SERVER. They are `codex exec` subprocesses on another
        provider entirely, and they were blocked for roughly sixteen hours by a health check for a
        service they never touch (BOSS, 2026-09-08 — no Muse worker has ever run under the daemon,
        and none of the wiring, routing or config we fixed was the reason).

        This is deliberately the minimum: load the queue, run the codex half, write the queue back if
        it changed. It shares no state with the opencode half, so there is nothing here to keep in
        step with it.
        """
        try:
            qlock = self.queue_lock()
            q = self.load_queue()
            before = json.dumps(q, sort_keys=True)
            status = {}
            # ITEM 6 (BOSS, 2026-09-10 21:48, reopening item 3). `apply_answer_requests` has exactly
            # ONE caller on the healthy path, and it sits AFTER the degraded early return — so with
            # opencode unreachable the daemon never reads `answerreq/` at all. BOSS filed a valid
            # answer for a parked CODEX row at 21:46 and three ticks passed with the file untouched
            # and no ANSWERED or ANSWER_REFUSED event. A CODEX question is answered by BOSS, not by
            # the opencode server, so holding it behind that server's health is the same mistake as
            # holding codex dispatch behind it — the one this whole degraded path exists to undo.
            #
            # HIS CORRECTION OF HIS OWN CLOSURE IS THE LESSON: test_serverdown.py proved the codex
            # pass, the aging, the escalation, the board and the heartbeat. It never proved answers.
            # A green licenses only the paths it walks, and item 3 was closed on a proof set read
            # wider than it was.
            #
            # HERE, not before the return, because this is where the queue lock and the queue are
            # already held — the healthy path applies answers under the same lock, and taking a
            # second one would be a deadlock rather than a guard. BEFORE codex_tick, so a row this
            # un-parks is dispatchable in the SAME tick, which is the healthy path's order too.
            # ITEM 7 (BOSS, 2026-09-10 22:12). Same shape as item 6, and the shape is the finding:
            # THE EARLY RETURN IS DEFINED BY WHAT IT PROTECTS (opencode) AND SCOPED BY POSITION
            # (everything after it). Those are not the same set, and the difference is where things
            # get dropped in silence. Enumerating tick() either side of the return and checking each
            # method for `http(`, the roster or a session id: `apply_gate_requests` and
            # `collect_gates` touch NONE of the three — a merge gate is a subprocess — so they were
            # stranded by position alone. The order below is the healthy path's order, deliberately.
            #
            # STILL STRANDED, AND CORRECTLY: check_dead, check_stall, check_session_idle, feed,
            # feed_when_idle, post_prompt, roster_refresh, recover_missed_reports, prune_offroster,
            # apply_clear_requests — every one reads the roster, a session or the server itself, so
            # a degraded tick genuinely cannot do them. That list is stated rather than left as the
            # residue of what nobody moved.
            try:
                self.apply_gate_requests(q)     # BOSS's hand-named gates: a subprocess, not a post
            except Exception as e:  # noqa: BLE001
                self.log(f"gate requests failed on the degraded pass: {type(e).__name__}: {e}")
            try:
                self.apply_answer_requests(q)
            except Exception as e:  # noqa: BLE001
                # ISOLATED FROM THE CODEX PASS. Sharing the outer try would let one malformed
                # request file cancel codex dispatch for every tick it survived — restoring the
                # sixteen-hour outage through a different door. One try PER FUNCTION for the same
                # reason: three functions behind one guard is one function's worth of protection.
                self.log(f"answer requests failed on the degraded pass: {type(e).__name__}: {e}")
            if self.c("auto_gate", True) or self.manual_gates_live():
                try:
                    self.collect_gates(q)
                except Exception as e:  # noqa: BLE001
                    self.log(f"collect_gates failed on the degraded pass: {type(e).__name__}: {e}")
            self.codex_tick(q, self.state["pending"], status, 0,
                            int(self.c("max_dispatch_per_tick", 2)))
            if json.dumps(q, sort_keys=True) != before:
                self.save_queue(q)
            return status
        except Exception as e:  # noqa: BLE001 — the degraded path must not take the daemon with it
            self.log(f"codex-only pass failed: {type(e).__name__}: {e}")
            return {}
        finally:
            try:
                qlock.close()
            except Exception:  # noqa: BLE001
                pass

    # -- one tick
    def tick(self):
        self.reload_cfg()   # roster.json is authoritative every tick, not just at startup
        # Written at the top and REWRITTEN by any path that returns early, because the heartbeat is
        # the signal everything else trusts. A fresh heartbeat plus a fresh state.json written on the
        # way OUT of a do-nothing tick is how a daemon that can do no work reads as a healthy idle
        # one — sixteen hours of it, measured 2026-09-07/08. A liveness signal written before the
        # work must not be allowed to stand in for the work.
        self.write_heartbeat()
        # THE HEARTBEAT MEASURES THE DAEMON, NOT THE PROGRAMME, and on 2026-09-07/08 that difference
        # cost six hours: every Claude session finished a turn and stopped, the Codex queue was
        # empty, and this file was written fresh every five seconds throughout. Nothing on any
        # instrument said the programme had halted.
        #
        # Placed BEFORE the STOP return on purpose. A paused daemon is exactly when a human most
        # needs to know the sessions have gone quiet, and the census reads a directory listing, a
        # pid and a file mtime — it actuates nothing, so the kill switch has nothing to hold back.
        # It holds no credential and opens no socket; see sessionwatch's docstring for why that is
        # the ruling and not merely the implementation.
        self.poll_sessions()

        if os.path.exists(STOP) and not (self.dry and "--ignore-stop" in sys.argv):
            if not self.paused_logged:
                self.log("PAUSED: STOP file present — heartbeat only")
                self.paused_logged = True
                self.paused_since = time.time()
            # `write_pending({})` used to blank the executor map, and an EMPTY map renders exactly
            # like a board with nothing happening on it. BOSS, 2026-09-07: he held the board for the
            # 09:18 apply, sized the pause as free because the view showed everyone busy, and the
            # view is fed by the mechanism he paused. Two Codex audits finished at 08:35 carrying
            # three [high]s and raised nothing; EXEC-M sat idle 57 min and EXEC-N 68, because nothing
            # was polling or auto-continuing them. The board could not have shown any of it.
            #
            # This does NOT resume observation — that is a bigger change and a decision, not a
            # detail. It replaces an absence that reads as a fact with a row that says the state is
            # UNKNOWN and for how long, which is the honest rendering of a paused sensor.
            held = int(time.time() - (getattr(self, "paused_since", None) or time.time()))
            self.write_pending({"(not observed)":
                                f"DISPATCHER PAUSED {held // 60}m — executor state is NOT being "
                                f"read, nothing is being auto-continued, and no escalation can "
                                f"fire. A quiet board here means UNMEASURED, not idle. Read the "
                                f"executors' own logs, not this view."})
            return
        self.paused_logged = False
        self.paused_since = None
        # Read ONCE per tick, so a file appearing mid-tick cannot half-apply: some executors acted
        # on and some not is a state neither switch is supposed to be able to produce.
        self.observing = os.path.exists(OBSERVE)
        if self.observing and not self.observe_logged:
            self.log("OBSERVE-ONLY: sensors run, actuators held — no posts, no dispatch, no gates")
            self.observe_logged = True
        elif not self.observing:
            self.observe_logged = False

        # Box-lock watchdog (detection only; never releases or writes the lock). Placed AFTER the
        # STOP check so the kill switch still means heartbeat-only, and BEFORE the server-health
        # block, which returns early when opencode is down — the box lock has nothing to do with
        # the opencode server, and 16:35 proved those two failures arrive together.
        self.poll_lockwatch()

        # server health
        try:
            http("GET", "/global/health", timeout=5)
            if self.server_down_since is not None:
                self.emit("SERVER_UP", "-", "-", "-", f"down {int(time.time() - self.server_down_since)}s")
                self.server_down_since = None
                self.state["server_down_emitted"] = False
        except Exception:
            if self.server_down_since is None:
                self.server_down_since = time.time()
            down_for = time.time() - self.server_down_since
            if down_for >= SERVER_DOWN_AFTER_S and not self.state["server_down_emitted"]:
                self.emit("SERVER_DOWN", "-", "-", "-", f"opencode server unreachable for {int(down_for)}s at {BASE}")
                self.notify_owner("opencode server down", f"{BASE} unreachable for {int(down_for)}s; auto_restart_server={self.c('auto_restart_server', False)}")
                self.state["server_down_emitted"] = True
                if self.c("auto_restart_server", False) and not self.dry:
                    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.voicepod.opencode-serve"], timeout=10, capture_output=True)
            # RENDERED EVERY TICK, not once. SERVER_DOWN is one-shot by design (an event should not
            # repeat), but a one-shot event is a statement about a MOMENT, and this is a CONDITION.
            # Sixteen hours of outage were announced once, yesterday, and silent since. The state
            # field and the heartbeat suffix are re-asserted on every degraded tick so the board
            # shows what is true now rather than what happened when it started.
            self.state["degraded"] = {
                "reason": f"opencode server unreachable at {BASE}",
                "since": self.server_down_since, "for_s": int(down_for),
                "effect": "opencode executors are NOT observed and NOT fed; CODEX slots continue",
                "as_of": now_local()}
            # CODEX HAS NO DEPENDENCY ON THAT SERVER and must not be held by its health.
            codex_status = self.codex_only_pass()
            self.state["degraded"]["codex"] = codex_status
            # AGE AND ESCALATE ANYWAY. Neither needs the opencode server: escalate() appends to a
            # file. Skipping it here is why two REPORT_READY rows waited seventeen hours with nobody
            # told — the rows were already known, and answering them was never blocked on polling.
            self.apply_hold_clears()
            self.age_and_escalate(self.state["pending"], degraded=True)
            # AND THE BOARD MUST BE WRITTEN, or it freezes at its last value and renders as current.
            # Measured 2026-09-08: pending.json was last written 2026-09-07 12:49:14 — 16.6
            # hours — because this path returns before write_pending(). `dispatcherctl status` was
            # therefore printing fourteen EXEC rows of yesterday's fiction ("building ... busy") and
            # `CODEX-1 idle: no eligible CODEX item` over a Muse worker that had been running for 91
            # seconds. Nothing was guessing: the file was simply old, and nothing said so.
            #
            # The opencode rows are replaced rather than left standing. A stale row is worse than a
            # missing one — it is an assertion — and the honest rendering of a sensor that is not
            # reading is a row that says so, the same shape as the PAUSED board above.
            self.write_pending(dict(
                codex_status,
                **{"(not observed)": f"opencode server unreachable {int(down_for) // 60}m — "
                                     f"executor state is NOT being read, nothing is auto-continued, "
                                     f"and no escalation can fire. These rows are UNMEASURED, not "
                                     f"idle. CODEX slots are unaffected and are shown above."}))
            self.write_heartbeat(f"opencode unreachable {int(down_for)}s — CODEX dispatch only")
            self.save_state()
            return
        self.state.pop("degraded", None)

        if not self.roster or time.time() - self.last_resync > self.c("resync_seconds", 60):
            self.roster_refresh()

        pending = self.state["pending"]
        # 2026-09-06 (BOSS §5.2): pending is keyed by session id and the tick only ever visits the
        # sessions IN the roster, so a row for a retired/replaced session (EXEC-A, retired 04:36) is
        # unreachable by every clear path and escalates forever. Prune it here, where the roster is
        # known. codex:* keys are owned by codex_tick and are left alone.
        self.apply_hold_clears()
        self.apply_clear_requests(pending)
        self.prune_offroster(pending)
        max_auto = int(self.c("max_auto_continue", 3))
        exec_status = {}
        qlock = self.queue_lock()
        q = self.load_queue()
        q_before = json.dumps(q, sort_keys=True)
        fed_this_tick = 0
        max_feed = int(self.c("max_dispatch_per_tick", 2))
        # Act on any gate the daemon launched that has since finished. Before dispatching, so an
        # item moving to `rework` is seen as occupied by the lane guard in the SAME tick.
        if self.c("auto_gate", True):
            try:
                self.recover_missed_reports(q)   # once per process, before anything else touches q
            except Exception as e:
                self.log(f"recover_missed_reports failed: {type(e).__name__}: {e}")
        self.apply_gate_requests(q)     # BOSS's hand-named gates run whether or not auto_gate is on
        self.apply_answer_requests(q)   # ...and answered QUESTIONs un-park in the same step as the post
        if self.c("auto_gate", True) or self.manual_gates_live():
            try:
                self.collect_gates(q)
            except Exception as e:      # a bug here must never take the poll down with it
                self.log(f"collect_gates failed: {type(e).__name__}: {e}")

        for name, sid in sorted(self.roster.items()):
            recent = self.last_messages(sid, 3)
            m = recent[-1] if recent else self.last_message(sid)
            # Was the message immediately before this one an auto-compaction? Same single fetch —
            # the daemon used to read ?limit=1 and had no way to see what preceded the turn it was
            # judging, which is the shape behind both the 20:45 cut-turn miss and 01:12.
            after_compaction = bool(
                len(recent) >= 2
                and (recent[-2].get("info", recent[-2]) or {}).get("role") == "user"
                and any((pt or {}).get("type") == "compaction" for pt in (recent[-2].get("parts") or [])))
            if m is None or m == "ERR":
                exec_status[name] = "unreadable"
                continue
            kind, msg_id, when_ms, finish, exc = self.classify(m, after_compaction)
            pend = pending.get(sid)
            if self.prune_superseded_error(pending, name, sid, kind, msg_id):
                pend = None

            if kind in ("QUEUED", "BUSY"):
                cur = self.current_item(q, name)
                exec_status[name] = self.exec_label(name, sid, q, cur,
                                                    "busy" if kind == "BUSY" else "queued", msg_id)
                if kind == "QUEUED":
                    lab = self.recover_missed_marker(name, sid, q)
                    if lab:
                        exec_status[name] = lab
                if kind == "BUSY":
                    # A provider-cut turn reads as BUSY forever. Detect and resume it here, before
                    # the `continue` below sends this session back into "building ... busy".
                    lab = self.check_stall(name, sid, m, cur)
                    if lab:
                        exec_status[name] = lab
                # Last-resort watchdog, run for BUSY *and* QUEUED: check_stall stops escalating
                # once it has posted its one resume, and a session that stays dead after that used
                # to sit forever under a reassuring label (EXEC-H, ~5 h).
                # Session-level first: it fires on a shape check_dead can only reach 20 minutes
                # later, and when it fires check_dead has nothing to add about the same silence.
                notick = self.check_session_idle(name, sid, recent, cur)
                if notick:
                    exec_status[name] = notick
                else:
                    dead = self.check_dead(name, sid, recent, cur)
                    if dead:
                        exec_status[name] = dead
                if pend:
                    self.emit("CLEARED", name, sid, pend.get("msg_id"), f"{pend['kind']} answered — session active again")
                    del pending[sid]
                    self.release_hold(name)
                    self.state.setdefault("last_clear", {})[name] = time.time() * 1000
                continue

            # A retryable provider error is handled BEFORE the handled-marking below, because the
            # 60-s grace has to leave the message un-handled so a later tick can revisit it.
            if kind == "ERROR" and self.state["handled"].get(sid) != msg_id:
                outcome = self.check_retryable_error(name, sid, m, when_ms, self.current_item(q, name))
                if outcome == "grace":
                    exec_status[name] = f"provider error — waiting out the {int(self.c('error_resume_grace_seconds', 60))}s grace"
                    continue                      # NOT marked handled: revisit next tick
                if outcome == "resumed":
                    self.state["handled"][sid] = msg_id
                    exec_status[name] = f"provider error — RESUMED"
                    continue

            if self.state["handled"].get(sid) == msg_id:
                cur = self.current_item(q, name)
                if kind == "ACK" and cur and self.state.setdefault("nudged", {}).get(sid) != msg_id:
                    # an ACK handled by an older daemon build (2026-09-05, EXEC-E) left the executor idle mid-item
                    # The marker is set only on a DELIVERED nudge: it suppresses every later attempt
                    # for this message, so setting it on a failed post retires the nudge for good and
                    # leaves the executor idle under a label that says it is building.
                    if self.post_prompt(sid, f"REOPEN acknowledged. Continue item {cur['id']} now from where the disk says you are; end at REPORT READY or a QUESTION block."):
                        self.state["nudged"][sid] = msg_id
                        exec_status[name] = f"building {cur['id']} (nudged after ACK)"
                    else:
                        exec_status[name] = (f"{cur['id']} — the ACK nudge did NOT reach it: "
                                             f"{getattr(self, '_last_post_error', '') or 'post refused'}")
                    continue
                # already-handled idle executor (e.g. stood down last night): feed it if the queue has work
                if self.feed_when_idle(q, name, kind, cur) and fed_this_tick < max_feed:
                    lab = self.feed(q, name, sid, kind, msg_id, text_of(m))
                    if lab:
                        fed_this_tick += 1
                        exec_status[name] = lab
                        continue
                exec_status[name] = self._idle_label(pend, kind, when_ms)
                continue
            self.state["handled"][sid] = msg_id

            if kind == "PLAN_READY":
                cur = self.current_item(q, name)
                if not os.path.exists(PLANREVIEW_FLAG) or not cur:
                    # feature off (or no item to review): the executor is mid-build and stopped for
                    # nothing — push it on rather than leaving it idle waiting on a review that
                    # will never come.
                    if self.post_prompt(sid, f"Plan review is not active{' for this item' if cur else ''}. "
                                             f"Continue the build now and end at REPORT READY or a QUESTION block."):
                        exec_status[name] = (f"building {cur['id']} " if cur else "") + "(plan review off — continued)"
                    else:
                        exec_status[name] = ((f"{cur['id']} " if cur else "")
                                             + f"— plan review is off and the CONTINUE did NOT reach "
                                               f"it: {getattr(self, '_last_post_error', '') or 'post refused'}")
                    continue
                self.emit("PLAN_READY", name, sid, msg_id, f"item={cur['id']} — reviewing before build", exc)
                exec_status[name] = f"plan review {cur['id']}"
                self.start_plan_review(name, sid, cur["id"], cur.get("artifact", ""), text_of(m))
                continue

            if kind == "PROGRESS_STOP":
                # An executor whose only item is UNDER GATE has nothing to continue. Measured
                # 2026-09-07 03:45-03:49: EXEC-F reported while its gate ran, was told to wait (my
                # note), answered "waiting", and that one-word turn was read as a stall — AUTO-
                # CONTINUE 1/3, 2/3, 3/3, then STUCK, and BOSS cleared the row by hand. Three
                # prompts and an escalation, all for an executor doing exactly what it was told.
                # The continue machinery is for a build that stopped mid-step; a gated item is not
                # one, whoever launched that gate.
                # Nothing to continue, and nobody feeding it. BOSS, 2026-09-07: an item sat
                # `queued` for six minutes while its executor showed "idle:PROGRESS_STOP" — and
                # FEED_KINDS is ("REPORT_READY", "QUESTION", "ACK"), so an executor at PROGRESS_STOP
                # or STUCK is fed on NO tick, ever: this branch `continue`s before the feed, and the
                # already-handled branch gates on FEED_KINDS too. Prompting "continue" at an
                # executor that holds no item is also wrong on its own terms — there is nothing to
                # continue. Treat it as the idle executor it is.
                if not self.current_item(q, name) and not self.gated_item_for(q, name):
                    self.state["auto"][sid] = 0
                    if fed_this_tick < max_feed:
                        lab = self.feed(q, name, sid, "ACK", msg_id, text_of(m))
                        if lab:
                            fed_this_tick += 1
                            exec_status[name] = lab + " (was idle at PROGRESS_STOP)"
                            continue
                    exec_status[name] = "idle at PROGRESS_STOP, holds no item, nothing eligible"
                    continue
                gated = self.gated_item_for(q, name)
                if gated:
                    exec_status[name] = f"waiting on the gate for {gated['id']}"
                    self.state["auto"][sid] = 0
                    continue
                n = int(self.state["auto"].get(sid, 0))
                if n < max_auto:
                    # A1b (BOSS, 2026-09-08). This used to emit AUTO-CONTINUE and increment the
                    # counter BEFORE posting, and post_prompt reports failure by returning False.
                    # So three lost posts walked the counter to max and escalated STUCK for an
                    # executor that was never prompted, with three log lines saying it had been.
                    # That is the board reporting what it SENT rather than what happened — the same
                    # shape that hid two dead executors for 7.3 and 5.7 hours.
                    # The post happens first and the record follows it. A failed post does NOT
                    # consume an attempt: the next tick tries again, and a session that stays
                    # unreachable is caught by check_session_idle and check_dead, which measure the
                    # SESSION rather than our own send count.
                    if self.post_prompt(sid, CONTINUE_PROMPT.format(n=n + 1, max=max_auto)):
                        self.state["auto"][sid] = n + 1
                        self.emit("AUTO-CONTINUE", name, sid, msg_id, f"{n + 1}/{max_auto} since={ms_local(when_ms)} finish={finish}", exc)
                        exec_status[name] = f"auto-continued {n + 1}/{max_auto}"
                    else:
                        err = getattr(self, "_last_post_error", "") or "post_prompt returned False"
                        # Once per message: the tick revisits an idle executor every 5s and a
                        # failure repeated forever is one nobody reads.
                        seen = self.state.setdefault("auto_failed", {})
                        if seen.get(sid) != msg_id:
                            seen[sid] = msg_id
                            self.emit("AUTO_CONTINUE_FAILED", name, sid, msg_id,
                                      f"attempt {n + 1}/{max_auto} NOT sent", err)
                        exec_status[name] = (f"auto-continue {n + 1}/{max_auto} NOT DELIVERED — {err}; "
                                             f"the attempt was not counted")
                    continue
                kind = "STUCK"
            else:
                self.state["auto"][sid] = 0

            self.emit(kind, name, sid, msg_id, f"since={ms_local(when_ms)} finish={finish}", exc)
            if kind == "ACK":
                cur = self.current_item(q, name)
                if cur:  # it acknowledged mid-item and ended its turn: nudge it back onto the item, do not re-feed
                    if self.post_prompt(sid, f"REOPEN acknowledged. Continue item {cur['id']} now from where the disk says you are; end at REPORT READY or a QUESTION block."):
                        exec_status[name] = f"building {cur['id']} (nudged after ACK)"
                    else:
                        exec_status[name] = (f"{cur['id']} — the ACK nudge did NOT reach it: "
                                             f"{getattr(self, '_last_post_error', '') or 'post refused'}")
                elif fed_this_tick < max_feed:
                    lab = self.feed(q, name, sid, kind, msg_id, text_of(m))
                    if lab:
                        fed_this_tick += 1
                    exec_status[name] = lab or "idle:ACK no eligible item"
                continue
            _cur = self.current_item(q, name)
            pending[sid] = {
                "executor": name, "session": sid, "kind": kind, "msg_id": msg_id,
                "item": _cur["id"] if _cur else None,
                "artifact": _cur.get("artifact") if _cur else None,
                "since_ms": when_ms, "since_local": ms_local(when_ms), "excerpt": exc,
                "escalated": 0, "esc_count": 0, "last_esc_min": 0,
                "esc_base_ms": max(when_ms or 0, self.started_ms),
            }
            exec_status[name] = self._idle_label(pending[sid], kind, when_ms)
            if kind in FEED_KINDS and fed_this_tick < max_feed:
                self._report_verdict = None
                lab = self.feed(q, name, sid, kind, msg_id, text_of(m))
                if lab:
                    fed_this_tick += 1
                    exec_status[name] = lab + f" (its {kind} is with BOSS)"
                v = getattr(self, "_report_verdict", None)
                if self.withdraw_idle_row(pending, name, sid, kind, msg_id):
                    exec_status[name] = (lab or "idle") + (
                        " (report held: its merge gate is still running)"
                        if v and v[1] == GATE_RUNNING else " (idle ack, not a report)")
                elif v and v[0] == msg_id and v[1] == UNGATEABLE and sid in pending:
                    # The row STAYS and now carries the reason. Without this the escalation reads
                    # "EXEC-X REPORT_READY waiting 20 min" and BOSS has to go and find out why
                    # nothing gated it — for a report whose reason we computed and then dropped.
                    pending[sid]["not_gated"] = v[2]
                    exec_status[name] = (lab or "idle") + " (REPORT NOT GATED — with BOSS)"

        self.clear_feed_holds_for_answered(q)
        fed_this_tick = self.codex_tick(q, pending, exec_status, fed_this_tick, max_feed)
        self.tag_stale(pending, q, exec_status)
        if json.dumps(q, sort_keys=True) != q_before:
            self.save_queue(q)
        qlock.close()  # releases the flock
        self.age_and_escalate(pending, degraded=False)
        self.write_pending(exec_status)
        self.save_state()

    def feed_when_idle(self, q, name, kind, cur):
        """May an ALREADY-HANDLED idle executor be fed on this tick? -> bool

        THE SECOND HALF OF A BUG I HALF-FIXED. My comment at the PROGRESS_STOP branch says it
        outright — "FEED_KINDS is ("REPORT_READY", "QUESTION", "ACK"), so an executor at
        PROGRESS_STOP or STUCK is fed on NO tick, ever" — and I fixed the branch I was looking at.
        The already-handled guard runs EARLIER in the same loop and had the identical hole, so it
        shadowed the fix completely: the branch I fixed is unreachable from the second tick onward,
        because by then the message is handled.

        The consequence was not a delay, it was permanent. Measured 2026-09-07: EXEC-M sat at
        PROGRESS_STOP for 88 minutes while two items were assigned to it by name, re-queued, and two
        more added. None of it could ever have reached it, and the board rendered the whole thing as
        an idle executor.

        `gated` is checked for the two new kinds and NOT for the three old ones, deliberately: an
        executor waiting on its own gate verdict holds no `current_item` but is not free, and the
        PROGRESS_STOP branch already treats those as separate questions. Widening the old kinds'
        condition here would be a behaviour change nobody asked for, in the same edit as a fix.
        """
        if cur:
            return False
        if kind in FEED_KINDS:
            return True
        if kind in ("PROGRESS_STOP", "STUCK"):
            return not self.gated_item_for(q, name)
        return False

    @staticmethod
    def _idle_label(pend, kind, when_ms):
        age = int((time.time() * 1000 - (when_ms or 0)) / 60000)
        if pend and pend.get("dead"):
            return f"DEAD:{pend['kind']} non-retryable 400, {age} min — session unusable, replace it"
        if pend:
            return f"idle:{pend['kind']} waiting for BOSS {age} min"
        return f"idle:{kind} {age} min"

    def undelivered_answer(self, executor):
        """Ground truth beats inference. The classifier below derives "BOSS never replied" from
        trunk commits, but whether an answer exists is RECORDED in the queue — and on 2026-09-05
        it reported DROPPED ("BOSS never replied") for an item BOSS had answered 19 minutes
        earlier, because a feed hold stopped the delivery. Deriving a fact that is written down
        is the same mistake as asking a component how it is doing."""
        try:
            q = load_json(QUEUE, {"items": []})
        except Exception:
            return None
        for it in q.get("items", []):
            if it.get("executor") != executor or not it.get("answered_at"):
                continue
            if it.get("status") == "queued":
                hold = os.path.exists(os.path.join(HOLD_DIR, f"{executor}.feed"))
                return (f"item {it['id']} was ANSWERED at {it['answered_at']} and is queued but undelivered"
                        + (" — a feed hold is blocking it" if hold else " — not yet picked up"))
        return None

    def boss_activity_since(self, since_ms):
        """Was BOSS demonstrably working while this item sat? Distinguishes the two stalls that
        look identical from outside: BOSS never saw it, vs BOSS read it and forgot to reply
        (both of 2026-09-05's level-2 escalations were the latter, per BOSS's own diagnosis)."""
        commits = 0
        try:
            iso = datetime.fromtimestamp(since_ms / 1000).strftime("%Y-%m-%dT%H:%M:%S")
            out = subprocess.run(
                ["git", "-C", os.path.join(CN, "voicepod-plan010-rebuild"),
                 "log", f"--since={iso}", "--format=%h"],
                capture_output=True, text=True, timeout=10,
            )
            commits = len([l for l in out.stdout.split("\n") if l.strip()])
        except Exception:
            commits = -1  # unknown, not zero
        others = [n for n, ms in self.state.get("last_clear", {}).items() if ms > since_ms]
        if commits > 0 or others:
            why = []
            if commits > 0:
                why.append(f"{commits} trunk commit{'s' if commits != 1 else ''}")
            if others:
                why.append("answered " + "/".join(sorted(others)))
            return "DROPPED", "BOSS active since it landed (" + ", ".join(why) + ") but never replied to this one"
        if commits == 0:
            return "UNSEEN", "no trunk commit and no other executor answered since it landed — BOSS may be idle or inside a long turn"
        return "UNKNOWN", "could not read trunk history"

    def apply_hold_clears(self):
        """Apply `dispatcherctl.sh clear-hold <profile>` requests: return a held key to service.

        A hold on an account is durable BY DESIGN — an AUTH hold waits for a human, because retrying
        a refused key just refuses again. That makes a WRONG hold durable too, and on 2026-09-08 one
        was: a worker doing its job wrote a finding containing "until logout/401", the failure
        classifier matched that `401`, and muse-go-1 was taken out of service until a human cleared
        it. The classifier is fixed; this is how the human clears it.

        A REQUEST FILE rather than an edit, for the same reason the row clears use one: the daemon
        rewrites state.json every tick, so hand-editing it under a live daemon is a lost update
        waiting to happen. Going through here also makes the release an EVENT — a key silently
        returning to service is how nobody would ever learn the hold had been wrong.
        """
        try:
            reqs = sorted(f for f in os.listdir(HOLDCLEAR_DIR) if f.endswith(".json"))
        except OSError:
            return
        for fn in reqs:
            path = os.path.join(HOLDCLEAR_DIR, fn)
            req = load_json(path, None)
            self._rm(path)
            if not isinstance(req, dict) or not req.get("profile"):
                self.log(f"hold-clear request {fn} is malformed; ignored")
                continue
            prof = req["profile"]
            held = (self.state.get("muse") or {}).get("unhealthy", {})
            if prof not in held:
                self.log(f"hold-clear {prof}: not held — nothing to clear")
                continue
            was = held.pop(prof)
            self.emit("HOLD_CLEARED", "-", "-", "-", f"profile={prof}",
                      f"was {was.get('class')} since {ms_local(int((was.get('since') or 0) * 1000))}"
                      f" — {req.get('why') or 'no reason given'}")
            self.log(f"hold-clear {prof}: released (was {was.get('class')})")

    def apply_clear_requests(self, pending):
        """Apply `dispatcherctl.sh clear` requests. Exactly one row each, or none.

        BOSS has no sanctioned way to clear a row that is real but finished with (a stale
        REPORT_READY), and hand-editing the daemon's state is not one — it would be overwritten by
        the next tick. ctl validates against the board and writes a request; this applies it, so the
        removal is recorded as an event rather than happening silently in a file.

        Re-validated here, not trusted: the board ctl read may be seconds old, and a row that has
        since changed must not be removed on the strength of a stale match.
        """
        try:
            reqs = sorted(f for f in os.listdir(CLEAR_DIR) if f.endswith(".json"))
        except OSError:
            return
        for fn in reqs:
            path = os.path.join(CLEAR_DIR, fn)
            req = load_json(path, None)
            if not isinstance(req, dict):
                self.emit("PENDING_CLEAR_REFUSED", "-", "-", fn, "unreadable request",
                          "the request file is not JSON — removed without touching any row")
                self._rm(path)
                continue
            sid, mid = req.get("session"), req.get("msg_id")
            hits = [k for k, p in pending.items()
                    if k == sid and (not mid or p.get("msg_id") == mid)]
            if len(hits) != 1:
                self.emit("PENDING_CLEAR_REFUSED", req.get("executor", "-"), str(sid), str(mid),
                          f"matched {len(hits)} rows",
                          "the board moved between the request and this tick — nothing removed; "
                          "re-run dispatcherctl.sh clear against the current board")
                self._rm(path)
                continue
            p = pending.pop(hits[0])
            self.state.setdefault("stale_seen", {}).pop(hits[0], None)
            self.emit("PENDING_CLEARED", p.get("executor", "-"), hits[0], p.get("msg_id"),
                      f"kind={p.get('kind')} age_min={p.get('age_min')}",
                      "cleared by BOSS via dispatcherctl: " + str(req.get("reason") or "no reason given"))
            self._rm(path)

    def _rm(self, path):
        if self.dry:
            return
        try:
            os.remove(path)
        except OSError:
            pass

    def prune_offroster(self, pending):
        """Drop pending rows for sessions the roster no longer contains.

        Two guards, both learned from a staged dry-run on 2026-09-06 that came up with a 2-name
        roster because roster.json was not beside the module: an EMPTY explicit roster means the
        config did not load, and pruning against a roster that failed to load would delete rows for
        live executors; and a session must be off the roster for the whole grace window, so one bad
        discovery tick costs a delay instead of a row. Seeding happens here, so the first tick after
        an upgrade records and the next one prunes."""
        if not self.c("executors", {}):
            return
        live = set(self.roster.values())
        seen = self.state.setdefault("roster_seen", {})
        now = time.time()
        for sid in live:
            seen[sid] = now
        grace = 600
        for sid in [k for k in list(pending) if not str(k).startswith("codex:") and k not in live]:
            last = seen.get(sid)
            if last is None:
                seen[sid] = now      # first sighting of an off-roster row: prune after the grace
                continue
            if now - float(last) < grace:
                continue
            p = pending.pop(sid)
            seen.pop(sid, None)
            self.emit("PENDING_PRUNED", p.get("executor", "-"), sid, p.get("msg_id"),
                      f"kind={p.get('kind')} age_min={p.get('age_min')}",
                      f"session absent from the roster for over {grace // 60} min (retired or replaced) "
                      "— nothing can clear this row and nobody can act on it")

    def prune_superseded_error(self, pending, name, sid, kind, msg_id):
        """2026-09-06 (BOSS §5.2): an ERROR row whose message is no longer the session's last one is
        history. Only a QUEUED/BUSY turn cleared a pending row, so an ERROR followed by any other
        clean turn sat on the board as "idle:ERROR waiting for BOSS" with nothing to rule on — all
        three of tonight's ERROR rows were of that kind. Returns True if a row was pruned."""
        pend = pending.get(sid)
        if not (pend and pend.get("kind") == "ERROR" and kind != "ERROR" and pend.get("msg_id") != msg_id):
            return False
        self.emit("CLEARED", name, sid, pend.get("msg_id"),
                  f"ERROR superseded by a later {kind} message ({msg_id})",
                  "stale ERROR row pruned — it was never a question for BOSS")
        del pending[sid]
        return True

    def hold_reason(self, name):
        try:
            with open(os.path.join(HOLD_DIR, name)) as f:
                return f.read().strip() or "held by BOSS"
        except Exception:
            return None

    def clear_feed_holds_for_answered(self, q):
        """BOSS's answer is the signal the circuit breaker was waiting for.

        The breaker writes hold/<EXEC>.feed after repeated parks to stop unanswered churn. But it
        also blocks delivery of an item BOSS has just ANSWERED, which is the opposite of its
        purpose: on 2026-09-05 the hold landed at 14:21:58, BOSS answered B.JSS.1 at 14:23 and
        requeued at 14:25, and EXEC-A then sat idle with an answered item because the requeue
        could not be delivered. An `answered_at` on a queued item is proof the churn ended.
        """
        for it in q["items"]:
            if it.get("status") != "queued" or not it.get("answered_at"):
                continue
            name = it.get("executor")
            path = os.path.join(HOLD_DIR, f"{name}.feed") if name else None
            if path and os.path.exists(path) and not self.dry:
                try:
                    os.remove(path)
                    self.state["parks"][name] = []  # the breaker's window starts over, or it re-trips at once
                    self.emit("FEED_RESUMED", name, "-", "-",
                              f"item={it['id']} answered at {it['answered_at']} — feed hold cleared automatically", "")
                except OSError:
                    pass

    def release_hold(self, name):
        """Release BOTH holds when an executor comes back to life.

        2026-09-06 (BOSS): the circuit breaker's `hold/<EXEC>.feed` says "BOSS: answer its QUESTIONs
        then delete this file" and nothing ever did. clear_feed_holds_for_answered() only removes it
        when a queued item carries `answered_at`; an executor that simply resumed kept the hold and
        was never fed again — two of them sat unfed for 17 h. A CLEARED event IS the churn ending.
        """
        for path, why in ((os.path.join(HOLD_DIR, name), "BOSS hold"),
                          (os.path.join(HOLD_DIR, f"{name}.feed"), "circuit-breaker feed hold")):
            if os.path.exists(path) and not self.dry:
                try:
                    os.remove(path)
                    if path.endswith(".feed"):
                        self.state.setdefault("parks", {})[name] = []  # window restarts, or it re-trips at once
                        self.emit("HOLD_RELEASED", name, "-", "-", "hold/{}.feed removed".format(name),
                                  "session active again (CLEARED) — the breaker's hold outlived the churn")
                    self.log(f"hold released for {name} ({why}, item cleared)")
                except Exception:
                    pass

    @staticmethod
    def _nonretryable_400(p):
        """Does this row's provider payload say the session itself is unusable? (statusCode 400 with
        isRetryable false — measured on EXEC-A 2026-09-06 04:32, which then answered nothing for 5 h.)"""
        exc = p.get("excerpt") or ""
        return '"statusCode": 400' in exc and '"isRetryable": false' in exc

    def item_of_pending(self, p, q):
        """Which queue item is this pending row about? Rows do not all carry one.

        Order: the recorded item (rows created from 2026-09-06 21:2x carry it), then the item id
        embedded in the msg_id — a Codex row's msg_id IS its log filename,
        `20260906-0229-CODEX-1-B.010.ci-required-green.log` — longest id first so
        `B.010.ci-required-green` never loses to a prefix of itself, then whatever that executor is
        currently dispatched.
        """
        by_id = {it["id"]: it for it in q.get("items", [])}
        if p.get("item") and p["item"] in by_id:
            return by_id[p["item"]]
        mid = str(p.get("msg_id") or "")
        # ONLY a Codex log filename, and only ids of real length. A bare substring test over an
        # opencode message id matched the two-character item `Q2` inside
        # `msg_072017ec1001VBvMlAe6TX60Q2` and tagged an unrelated ERROR row as finished work
        # (caught in calibration, 2026-09-06). A cross-check that mis-identifies its subject is
        # worse than no cross-check: it retires the wrong row.
        if mid.endswith(".log"):
            hits = [it for i_, it in by_id.items() if i_ and len(i_) >= 6 and i_ in mid]
            if hits:
                return max(hits, key=lambda it: len(it["id"]))
        return self.current_item(q, p.get("executor"))

    def stale_reason(self, p, q):
        """Is this pending row about work that is already FINISHED? -> tag text, or None.

        2026-09-06 21:13 (BOSS): CODEX-1's 02:40 REPORT_READY sat on the board for eleven hours
        after its artifact had LANDED (capped) on trunk, and a worker was dispatched from it. A
        pending row records that an executor said something; nothing ever re-read it against the
        world. NOT a prune — the row may still carry a question only BOSS can close, so this tags
        and says so, and BOSS clears it.
        """
        it = self.item_of_pending(p, q)
        if it:
            # THE ITEM'S OWN STATUS IS THE SOLE AUTHORITY once the item resolves (BOSS, 2026-09-06,
            # after this tagged EXEC-A2's LIVE report). Artifacts are SHARED BY DESIGN: every
            # A.<x> audit shares its artifact with the B.<x> build it audits, and every rework
            # (-r2/-r3/-renumber) shares its parent's — 13 artifacts were shared across non-final
            # items when this was measured. `A.012.sequence-ended-event` sitting `reported` under a
            # MERGED `012.sequence-ended-event` is the CORRECT state, not a stale row. Reading the
            # artifact here retires live work under a finished sibling.
            st = str(it.get("status") or "").lower()
            if st in ("landed", "merged", "done"):
                sha = it.get("merge_sha") or it.get("gate_sha") or ""
                when = it.get("merged_at") or it.get("gated_at") or ""
                return (f"STALE — {it['id']} is {st.upper()}"
                        + (f" at {str(sha)[:10]}" if sha else "")
                        + (f" ({when})" if when else "") + " — this row is about finished work")
            return None
        # Only when the item does NOT resolve — the row's item was renamed or removed from the
        # queue (BOSS renamed ten of them tonight) — does the artifact recorded on the row become
        # the best available evidence. A row with no artifact recorded is left alone.
        art = p.get("artifact")
        led = self.ledger_status().get(art, "") if art else ""
        if led and led.upper().startswith(("LANDED", "MERGED")):
            return (f"STALE — this row's item is no longer in the queue and its artifact {art} is "
                    f"{led.split()[0].rstrip(':')} per the ledger: {' '.join(led.split())[:70]} "
                    f"— this row is about finished work")
        return None

    def tag_stale(self, pending, q, exec_status):
        """Tag every pending row whose work is already finished. One event per row per reason."""
        seen = self.state.setdefault("stale_seen", {})
        for sid, p in pending.items():
            why = self.stale_reason(p, q)
            p["stale"] = why
            if not why:
                seen.pop(sid, None)
                continue
            name = p.get("executor", "-")
            if exec_status.get(name):
                # the whole reason, minus the trailing explanation — BOSS reads this line first
                exec_status[name] += " [" + why.rsplit(" — ", 1)[0] + "]"
            if seen.get(sid) != why:
                seen[sid] = why
                self.emit("PENDING_STALE", name, sid, p.get("msg_id"),
                          f"kind={p.get('kind')} age_min={p.get('age_min')}",
                          why + " — NOT pruned: it may still carry a question, BOSS clears it")

    def age_and_escalate(self, pending, degraded=False):
        """Age every pending row and escalate the ones that have waited too long.

        `degraded` means the opencode sensor is down. It is passed rather than inferred because the
        one branch that must change is the OFF_ROSTER skip: the roster is refreshed on the healthy
        path, so while degraded it may be empty or stale, and an empty roster would silently classify
        every row as OFF_ROSTER and escalate none of them. A silent no-op is exactly the failure this
        whole area keeps producing.

        Escalation itself needs no server — escalate() appends a line to a file — so a row waiting on
        BOSS must keep escalating whether or not the executors can be polled. Two REPORT_READY rows
        sat for seventeen hours with esc=2 and esc=1 because this pass never ran.
        """
        levels = self.c("escalate_after_minutes", [10, 20])
        repeat = self.c("escalation_repeat_minutes", 20)
        now_ms = time.time() * 1000
        live = set(self.roster.values())
        roster_unknown = degraded and not live
        for sid, p in pending.items():
            p["age_min"] = int((now_ms - (p.get("since_ms") or now_ms)) / 60000)
            # A row for a session the roster no longer carries is on its way out (prune_offroster
            # removes it once the grace window closes). Escalating it in the meantime would ask BOSS
            # to rule on an executor that no longer exists — measured on the first staged dry-run,
            # which fired three DEAD escalations for exactly those rows.
            if (self.c("executors", {}) and not str(sid).startswith("codex:") and sid not in live
                    and not roster_unknown):
                p["stall_class"] = "OFF_ROSTER"
                p["held"] = "session is not in the roster — row is being pruned, no escalation"
                continue
            hold = self.hold_reason(p["executor"])
            if not hold and p["kind"] == "WAITING":
                hold = "WAITING: executor stopped on a BOSS-ordered gate; no escalation"
            p["held"] = hold
            if hold:
                if not p.get("hold_logged"):
                    self.log(f"HELD {p['executor']} {p['kind']}: {hold} — escalation suppressed")
                    p["hold_logged"] = True
                continue
            p["hold_logged"] = False
            # 2026-09-06 (BOSS §5.2): a non-retryable 400 that has already survived both escalation
            # levels is not a stall BOSS can answer — the session is dead. Label it DEAD and stop
            # escalating; three repeats of "waiting for BOSS" gave BOSS nothing to rule on.
            if p["kind"] == "ERROR" and self._nonretryable_400(p) and p.get("escalated", 0) >= 2:
                if not p.get("dead"):
                    p["dead"] = True
                    p["stall_class"] = "DEAD"
                    self.emit("EXECUTOR_DEAD", p["executor"], sid, p.get("msg_id"),
                              f"age_min={p['age_min']}",
                              "non-retryable 400 persisted past both escalations — the SESSION is "
                              "unusable (classify per §5.2: context-window 400 -> fresh session; "
                              "malformed payload -> fix the caller). No further escalation.")
                    self.escalate(f"{p['executor']} DEAD ({sid}): non-retryable 400 for {p['age_min']} min — "
                                  "retire or replace the session; this is not a question BOSS can answer")
                continue
            esc_age = (now_ms - p.get("esc_base_ms", now_ms)) / 60000
            head = f"{p['executor']} {p['kind']} waiting {p['age_min']} min (since {p['since_local']}, {sid})"
            if p.get("not_gated"):
                head += f" — NOT GATED: {p['not_gated']}"
            if p["escalated"] < 1 and esc_age >= levels[0]:
                self.notify_owner("executor waiting on BOSS", head)
                p["escalated"] = 1
            if len(levels) > 1 and esc_age >= levels[1] and p["esc_count"] < 3 and (
                p["escalated"] < 2 or esc_age - p["last_esc_min"] >= repeat
            ):
                undelivered = self.undelivered_answer(p["executor"])
                if undelivered:
                    # not a BOSS stall at all: the answer exists and never reached the executor
                    cls, why = "UNDELIVERED", undelivered
                else:
                    cls, why = self.boss_activity_since(p.get("since_ms") or now_ms)
                p["stall_class"] = cls
                self.escalate(f"{head} — {cls}: {why}; backstop should message BOSS")
                p["escalated"] = 2
                p["esc_count"] += 1
                p["last_esc_min"] = esc_age

    def poll_sessions(self):
        """Census the Claude sessions and SAY when they have gone quiet. Never pokes one.

        Rate-limited by KEY, not by time alone: the finding changes wording when the queue changes,
        and a reader who sees the same line twice an hour learns nothing new — but a reader who sees
        it change learns that the shape changed. Re-emitted every `session_quiet_repeat_minutes`
        so a standing halt does not scroll away, and immediately when the key changes.
        """
        # THE GUARD COVERS THE WHOLE BODY, not just the probes. The first cut wrapped only the
        # census and the queue read, and `self.c(...)` one line below it raised AttributeError
        # through an existing test's stub — aborting a tick that this method only observes. A
        # fail-safe sensor whose fail-safe covers part of itself is not fail-safe; test_pausedview
        # caught it, which is what a suite is for.
        try:
            rows = sessionwatch.census()
            shape = sessionwatch.queue_shape(self.load_queue().get("items", []))
            quiet_after = int(self.c("session_quiet_minutes", 30)) * 60
            # THE ROUTE SET IS THE ONE THIS DAEMON WOULD ACTUALLY DISPATCH TO, taken from the
            # roster's own `codex_routes` plus any affinity a conversation already holds — not a
            # constant, and not every profile the adapter can name. If the roster configures
            # nothing, the daemon is on the legacy pair and this census has nothing to say, so it
            # reports NOT MEASURED rather than inventing a set it can then declare all-held.
            mstate = self.state.setdefault("muse", {})
            configured = sorted({p for p in (self.cfg.get("codex_routes") or {}).values() if p}
                                | {p for p in (mstate.get("affinity") or {}).values() if p})
            routes = (sessionwatch.route_health(mstate, profiles=configured)
                      if configured else None)
            self.state["sessions"] = {
                "rows": rows,
                "summary": sessionwatch.summarise(rows, quiet_after_s=quiet_after),
                "routes": routes,
                "board": sessionwatch.board_lines(rows, shape, quiet_after_s=quiet_after,
                                                  routes=routes),
                "at": now_local(),
            }
            # THE THIRD LOCK. `box.lock.d` and `portal.lock.d` do not know pgserver's global
            # postgres mutex exists, so a run can hold the box and be stalled behind a process
            # holding no project lock at all — board-reports-what-it-sent, third lock edition
            # (★, docs/ops/2026-09-09/PGSERVER-GLOBAL-LOCK.md: one run waited 102 minutes).
            # READ-ONLY: F_GETLK tests for the lock and never takes it. Never acquire here — this
            # daemon must not become the thing that serialises a test run.
            lock = pglock.probe()
            prev_cpu = self.state.get("pgserver_cpu") or {}
            cpu_now, runs = {}, []
            # ITEM 4. THE POPULATION COMES FROM THE BOX, NOT FROM THIS DAEMON'S OWN SPAWNS. The
            # loop below used to run over `state["codex"]`, which meant a run this daemon did not
            # start could never be STALLED — and the 102-minute wait this whole probe was built for
            # was WORKER-1's run, not ours. The instrument could not see its own founding incident.
            box_runs, pop_notes, pop_state = pglock.population()
            # G6: the CPU baseline is keyed on (pid, start_time), never on pid alone. A pid that is
            # recycled between two ticks would otherwise inherit the dead run's CPU reading and
            # come out LIVE on a delta that spans two different processes.
            def _key(pid, started):
                return f"{int(pid)}:{int(started)}" if started else f"{int(pid)}:?"
            seen = set()
            for r in box_runs:
                pid = r["pid"]
                k = _key(pid, r.get("started_at"))
                c = pglock.cpu_seconds(pid)
                cpu_now[k] = c
                state_, why_ = pglock.verdict(pid, lock, c, prev_cpu.get(k))
                runs.append({"slot": r.get("owner") or r.get("session"), "pid": pid,
                             "item": r.get("suite_root"), "state": state_, "why": why_,
                             "cpu_s": c, "source": "box", "session": r.get("session"),
                             "postmaster": r.get("postmaster")})
                seen.add(pid)
            # The daemon's own codex runs stay in the set — they are box runs too, they just do not
            # write pgserver markers. Deduped by pid so a codex run that DOES appear as a marker is
            # one row, not two.
            for slot, run in (self.state.get("codex") or {}).items():
                pid = run.get("pid")
                if not pid or pid in seen:
                    continue
                k = _key(pid, pglock.start_time(pid))
                c = pglock.cpu_seconds(pid)
                cpu_now[k] = c
                state_, why_ = pglock.verdict(pid, lock, c, prev_cpu.get(k))
                runs.append({"slot": slot, "pid": pid, "item": run.get("item"),
                             "state": state_, "why": why_, "cpu_s": c, "source": "codex"})
            self.state["pgserver_cpu"] = cpu_now
            self.state["sessions"]["pgserver_lock"] = lock
            self.state["sessions"]["runs"] = runs
            self.state["sessions"]["pgserver_population"] = {
                "state": pop_state, "notes": pop_notes, "root": pglock.marker_root(),
                "box_owner": pglock.box_owner()}
            self.state["sessions"]["board"] = (
                self.state["sessions"]["board"]
                + pglock_board_lines(lock, runs, pop_state, pop_notes))
            found = sessionwatch.finding(rows, shape, quiet_after_s=quiet_after, routes=routes)
            if not found:
                stalled = [r for r in runs if r["state"] == pglock.STALLED]
                if stalled:
                    found = ("RUN_STALLED",
                             "; ".join(f"{r['slot']} pid {r['pid']} ({r['item']}) {r['why']}"
                                       for r in stalled))
        except Exception as e:  # a sensor must never abort the tick it is only observing
            try:
                self.log(f"session census failed: {type(e).__name__}: {e}")
                self.state["sessions"] = {"error": f"{type(e).__name__}: {e}"}
            except Exception:
                pass
            return
        if not found:
            self.state.pop("sessions_last_emit", None)
            return
        key, text = found
        last = self.state.get("sessions_last_emit") or {}
        repeat_s = int(self.c("session_quiet_repeat_minutes", 30)) * 60
        if last.get("key") == key and (time.time() - float(last.get("at") or 0)) < repeat_s:
            return
        self.state["sessions_last_emit"] = {"key": key, "at": time.time()}
        self.emit(key, "SESSIONS", "-", "-", text)
        self.log(f"{key}: {text}")

    def write_pending(self, exec_status):
        items = sorted(self.state["pending"].values(), key=lambda p: p.get("since_ms") or 0)
        # AGE IS DERIVED HERE, NOT DISPLAYED FROM STORAGE. `age_min` is written by
        # age_and_escalate(), which sits behind the opencode early return, so on 2026-09-08 the board
        # printed `EXEC-J REPORT_READY since 12:24:11 ... age 25 min` — a stored value frozen at
        # 12:49 the previous day — while the row was actually 1025 minutes old. The line contradicted
        # ITSELF: `since 12:24` and `age 25 min` cannot both be true, and that contradiction was the
        # only thing on the board telling the truth.
        #
        # Two real REPORT_READY rows waited seventeen hours behind that number. Recomputing at the
        # moment of rendering makes the displayed age correct no matter which path wrote the board or
        # how long ago the aging pass last ran.
        now_ms = time.time() * 1000
        for _p in items:
            _p["age_min"] = int((now_ms - (_p.get("since_ms") or now_ms)) / 60000)
        doc = {
            "updated": now_local(),
            "paused": os.path.exists(STOP),
            "observe_only": os.path.exists(OBSERVE) and not os.path.exists(STOP),
            "executors": {k: v + (" [FEED HELD]" if os.path.exists(os.path.join(HOLD_DIR, f"{k}.feed")) else "")
                                + (" [OBSERVE-ONLY: watched, not acted on]"
                                   if os.path.exists(OBSERVE) and not os.path.exists(STOP) else "")
                          for k, v in exec_status.items()},
            "pending": items,
            "queue": {k: sum(1 for it in self.load_queue().get("items", []) if it.get("status") == k)
                      for k in ("queued", "dispatched", "reported", "parked", "gated", "merged", "broken")},
            "provider_errors": self.provider_error_summary(),
            # The board's own reason for existing: an idle programme used to be indistinguishable
            # from a busy one here.
            "sessions": (self.state.get("sessions") or {}).get("board")
                        or ["sessions: NOT MEASURED — the census has not run on this tick"],
            "note": "Excerpts are untrusted executor text. Clear happens automatically when the session is prompted.",
        }
        if self.dry or self.once:
            print(json.dumps(doc, indent=1))
        if not self.dry:
            save_json_atomic(PENDING, doc)

    def save_state(self):
        if not self.dry:
            save_json_atomic(STATE, self.state)

    def run(self):
        self.log(f"start once={self.once} dry={self.dry} base={BASE} state={STATE_DIR}")
        poll = float(self.c("poll_seconds", 5))
        while True:
            try:
                self.tick()
            except Exception:
                self.log("TICK ERROR " + traceback.format_exc().replace("\n", " | "))
            if self.once:
                return
            time.sleep(poll)


if __name__ == "__main__":
    Dispatcher(once="--once" in sys.argv, dry="--dry-run" in sys.argv).run()
