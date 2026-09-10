"""A session that never begins a turn is invisible to every message-based detector.

BOSS, 2026-09-07: EXEC-M held a dispatched item and the board said `building` for 11 minutes while
the session was dead.

    answer posted           10:08:12
    session time.updated    10:08:12  (unchanged since)
    assistant turns started none

check_stall measures an INERT ASSISTANT TURN over ~93s. There was no turn to be inert, so nothing
fired. check_dead would have reached it — at 30 minutes. And the executor could not be fed either:
the tick reads each session's latest message and branches on its KIND, and a session that has
emitted nothing has no kind. That is the PROGRESS_STOP hole one level deeper — that was a kind we
did not handle; this is the absence of a kind at all.

So liveness here is keyed on the SESSION. Our own post bumps `time.updated`, which is what makes the
signal sharp: if it has not moved since, nothing has happened since we prompted.

THE FALSE POSITIVE THAT WOULD MAKE THIS WORTHLESS: an executor running a 40-minute pytest inside a
live turn. The check is therefore narrow by construction — it fires ONLY when no assistant turn is
newer than the newest user message — and that narrowness is asserted below, not just described.

Hermetic: a Dispatcher built with __new__, stub clocks, no HTTP."""
import importlib.util, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixtures                      # item 9: the shared state redirect
import fixturelog as FL
spec = importlib.util.spec_from_file_location("dnt", os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); sys.modules["dnt"] = D; spec.loader.exec_module(D)
fixtures.redirect_state(D)   # item 9: never the LIVE state dir

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

ITEM = {"id": "B.010.erasure-knowledge-learned-content"}
NOW = time.time()

def msg(role, mid, ago_s, **kw):
    m = {"info": {"id": mid, "role": role, "time": {"created": (NOW - ago_s) * 1000}}, "parts": []}
    m["info"]["time"].update(kw.pop("time", {}))
    m["parts"] = kw.pop("parts", [])
    return m

def dp(updated_ago_s=None, read_ago_s=0, cfg=None, compacting=False):
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {}
    d.events, d.escalations = [], []
    d.emit = lambda *a: d.events.append(a)
    d.escalate = lambda t: d.escalations.append(t)
    d.log = lambda *a, **k: None
    d.c = lambda k, default=None: (cfg or {}).get(k, default)
    d.compaction_active = lambda sid: "cmp" if compacting else None
    d.stall_shape = D.Dispatcher.stall_shape
    if updated_ago_s is not None:
        d.session_seen = {"ses_m": ((NOW - updated_ago_s) * 1000, NOW - read_ago_s)}
    return d

# BOSS's case, aged past the threshold: our post is the newest message and no turn ever began.
POSTED = [msg("user", "m-answer", 11 * 60)]
d = dp(updated_ago_s=11 * 60)
lab = d.check_session_idle("EXEC-M", "ses_m", POSTED, ITEM)
check("MUST-BITE  a session that has begun NO TURN since our post is caught — the board said "
      "`building` for 11 minutes and no message-based detector could see it",
      lab and "NO TURN STARTED" in lab, lab)
check("MUST-BITE  it escalates: this wakes BOSS, because a slot silently gone is worse than a slot "
      "visibly stuck",
      d.escalations and "STUCK" in d.escalations[0] and ITEM["id"] in d.escalations[0],
      d.escalations[:1])
check("MUST-BITE  the escalation says the executor cannot be FED either — a session with no message "
      "has no kind, so the tick has nothing to branch on",
      any("no kind" in str(a) for a in d.events), d.events[:1])
check("  and the STUCK event carries the age of the session clock it measured, because that value "
      "is up to resync_seconds stale and a silence figure that hides its own age is overconfident",
      any("session clock read" in str(a) for a in d.events), d.events[:1])
check("  it fires ONCE per silent stretch — an escalation repeated every 5s is one nobody reads",
      d.check_session_idle("EXEC-M", "ses_m", POSTED, ITEM).endswith("(escalated)")
      and len(d.escalations) == 1, (len(d.escalations),))

# --- the false positive that would make it worthless -------------------------------------------
d = dp(updated_ago_s=45 * 60)
LIVE = [msg("user", "m-post", 46 * 60), msg("assistant", "m-turn", 45 * 60)]
check("MUST-BITE  CONTROL: a turn that BEGAN after our post is never this shape, however long the "
      "session clock has sat — a 40-minute pytest inside a live turn must not read as death",
      d.check_session_idle("EXEC-M", "ses_m", LIVE, ITEM) is None and not d.escalations,
      (d.check_session_idle("EXEC-M", "ses_m", LIVE, ITEM), d.escalations))

# --- the threshold is real ----------------------------------------------------------------------
d = dp(updated_ago_s=9 * 60)
check("CONTROL  9 minutes is under the 10-minute default and is not escalated — without this, a "
      "check that fired on every dispatched row would look identical",
      d.check_session_idle("EXEC-M", "ses_m", POSTED, ITEM) is None, d.escalations)
d = dp(updated_ago_s=9 * 60, cfg={"session_idle_stuck_minutes": 5})
check("  and the threshold is configurable, read from roster.json like every other one",
      d.check_session_idle("EXEC-M", "ses_m", POSTED, ITEM) is not None)

# --- an executor holding nothing is not stuck ---------------------------------------------------
d = dp(updated_ago_s=99 * 60)
check("CONTROL  no dispatched item, no finding: an idle executor is idle, not stuck",
      d.check_session_idle("EXEC-M", "ses_m", POSTED, None) is None, d.escalations)

# --- a compaction is not death ------------------------------------------------------------------
d = dp(updated_ago_s=11 * 60, compacting=True)
check("CONTROL  a session in COMPACTION is not escalated — it owns that case, and waking BOSS for "
      "one is the cry-wolf that gets the detector ignored",
      d.check_session_idle("EXEC-M", "ses_m", POSTED, ITEM) is None, d.escalations)

# --- an UNMEASURED session must not read as a healthy one ---------------------------------------
d = dp(updated_ago_s=None)
lab = d.check_session_idle("EXEC-M", "ses_m", POSTED, ITEM)
kinds = [a[0] for a in d.events]
check("MUST-BITE  when the listing carried no time.updated, the session is announced as NOT "
      "MEASURED rather than passing silently — an absence of evidence is not evidence of life",
      lab is None and "LIVENESS_NOT_MEASURED" in kinds, (lab, kinds))
lab2 = d.check_session_idle("EXEC-M", "ses_m", POSTED, ITEM)
check("  ...said once per session per process, not once per 5-second tick",
      [a[0] for a in d.events].count("LIVENESS_NOT_MEASURED") == 1, [a[0] for a in d.events])

# --- it shares check_dead's dedupe, so one silence is not escalated twice under two names --------
# The post itself is 40 min old here, so check_dead's OWN 30-minute threshold is crossed too —
# otherwise it returns None for a reason that has nothing to do with the shared dedupe and the check
# below would pass without testing anything.
OLD_POST = [msg("user", "m-answer", 40 * 60)]
d = dp(updated_ago_s=40 * 60, cfg={"dead_after_minutes": 30})
d.check_session_idle("EXEC-M", "ses_m", OLD_POST, ITEM)
before = len(d.escalations)
check("  CONTROL on the fixture: check_dead's own threshold really is crossed, so its silence below "
      "is the dedupe and not a short age",
      (time.time() - float(OLD_POST[0]["info"]["time"]["created"]) / 1000.0) > 30 * 60)
again = d.check_dead("EXEC-M", "ses_m", OLD_POST, ITEM)
check("MUST-BITE  check_dead does not escalate the SAME silent stretch a second time under its own "
      "name — the two detectors share one dedupe key on purpose",
      len(d.escalations) == before and "escalated" in (again or ""), (before, len(d.escalations), again))

# --- the roster listing is what fills the clock, at no extra API cost ---------------------------
d = D.Dispatcher.__new__(D.Dispatcher)
d.state = {}; d.dirs = {}; d.roster = {}; d.log = lambda *a, **k: None
d.c = lambda k, default=None: {"executors": {}, "auto_discover_prefix": "EXEC-"}.get(k, default)
calls = []
real_http = D.http
D.http = lambda method, path, body=None, timeout=10: calls.append(path) or [
    {"id": "ses_m", "title": "EXEC-M worker", "directory": "/tmp/x",
     "time": {"updated": (NOW - 60) * 1000}}]
d.roster_refresh()
D.http = real_http
check("MUST-BITE  the session clock comes from the roster listing the daemon ALREADY fetches — one "
      "call, not a new per-executor poll every 5 seconds",
      d.session_seen.get("ses_m") and len(calls) == 1, (list(d.session_seen), calls))

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
