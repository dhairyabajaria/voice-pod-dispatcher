"""vpalerts.py -- alert severity computed from PATTERN, not from event type.

The 2026-09-17/18 phantom-repair loop re-spawned every 20-40 min for nine
hours while alerts.jsonl logged each cycle exactly like a routine ping: the
file has no priority field, and the loop's early alerts (PROOF_UNKNOWN) were
a kind that looks routine on its own. A static kind->severity map would have
missed it too, so severity here is derived from three signals over the
alert stream:

  repeat   how many times this signature fired inside the window
           (exact signature = (subject, kind); family signature strips the
           -R<n>/-<n> suffixes so a loop that re-instantiates B5-1, B5-2, ...
           still counts as one repeating failure);
  age      how long the same signature has been firing without a gap
           longer than `streak_gap_s` (an open, unresolved condition);
  backlog  growth of an externally supplied backlog count (e.g. rows owed a
           ruling, or READY rows nobody claims) across the window.

A small `floor` set names kinds that are urgent on FIRST sight because the
driver itself cannot proceed (budget, disk, scheduler down, authority
mismatch). The floor only raises; pattern can raise anything.

Pure functions, stdlib only: the driver calls `assess` at write time with
its recent in-memory alerts; the dashboard calls `annotate` over the whole
alerts.jsonl to render the same tiers after the fact.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

ROUTINE, ATTENTION, URGENT = "routine", "attention", "urgent"
TIERS = (ROUTINE, ATTENTION, URGENT)

DEFAULTS = {
    "window_s": 6 * 3600,        # repeats are counted inside this window
    "streak_gap_s": 2 * 3600,    # a gap longer than this closes a streak (age restarts)
    "age_horizon_s": 48 * 3600,  # how far back a streak is followed at most
    "repeat_attention": 2,
    "repeat_urgent": 3,
    "age_attention_s": 3600,
    "age_urgent_s": 3 * 3600,
    "backlog_attention": 2,      # backlog grew by at least this many inside the window
    "backlog_urgent": 5,
    # kinds that mean "the driver cannot do its job" -- urgent on first sight
    "floor_urgent": ["BUDGET", "DISK", "CONTROL_DOWN", "AUTHORITY_MISMATCH",
                     "ROSTER_REJECTED", "RELOAD_FAILED", "RESTART_STUCK",
                     "PROOF_BLOCKED_SHM", "TOKEN_CAP"],
    # kinds that are informational by themselves; only pattern can raise them
    # OWNER_GATE is a pending owner decision: its age is shown by the
    # decisions panel, so its own repeats (one per restart) must not page
    "floor_routine": ["IDLE", "RELOAD", "PROMOTED", "PACKET_RETRIED", "DRAIN",
                      "RESTART", "PROFILE_CHANGED", "OWNER_GATE"],
}

_SUFFIX_RE = re.compile(r"(?:-[RB]?\d+)+$")


def config(overrides=None):
    cfg = dict(DEFAULTS)
    for k, v in (overrides or {}).items():
        if k in cfg and v is not None:
            cfg[k] = v
    cfg["floor_urgent"] = set(cfg["floor_urgent"])
    cfg["floor_routine"] = set(cfg["floor_routine"])
    return cfg


def parse_ts(ts):
    """ISO-8601 with trailing Z -> epoch seconds; None when unparseable."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def subject_of(alert):
    """The row/packet an alert is about: its `task`, else the first word of
    its text (packet-level alerts are written with task=None but name the
    packet first: 'AUTH-ORACLE parent ... derived by ...')."""
    task = alert.get("task")
    if task:
        return str(task)
    text = str(alert.get("text") or "").strip()
    first = text.split(None, 1)[0] if text else ""
    return first.rstrip(":;,")


def family_of(subject):
    """L17-REPLY-WIRING-R1-B5-3 -> L17-REPLY-WIRING; a re-instantiation loop
    keeps one family while its task ids keep changing."""
    return _SUFFIX_RE.sub("", subject or "")


def signature(alert):
    return (subject_of(alert), str(alert.get("kind") or ""))


def family_signature(alert):
    return (family_of(subject_of(alert)), str(alert.get("kind") or ""))


def _streak_start(times, now, gap_s):
    """Earliest time of the streak that reaches `now`: walk back while
    consecutive firings are closer than gap_s."""
    start = now
    for t in sorted(times, reverse=True):
        if start - t > gap_s:
            break
        start = t
    return start


def assess(alert, history, *, now=None, backlog=None, cfg=None):
    """Severity of one alert given the alerts before it.

    alert    dict with kind, task, text, ts
    history  iterable of earlier alert dicts (any order; only those inside
             the window matter)
    backlog  optional list of (ts, count) samples of a backlog the alert's
             consumer cares about; growth inside the window is the signal
    Returns {severity, repeat, repeat_family, age_s, backlog_delta, reasons}.
    """
    cfg = config(cfg)
    now = parse_ts(alert.get("ts")) if now is None else float(now)
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    kind = str(alert.get("kind") or "")
    sig = signature(alert)
    fam = family_signature(alert)
    win_lo = now - cfg["window_s"]

    exact_times, family_times, streak_times = [now], [now], [now]
    age_lo = now - cfg["age_horizon_s"]
    for h in history:
        t = parse_ts(h.get("ts"))
        if t is None or t > now or t < age_lo:
            continue
        if family_signature(h) != fam:
            continue
        streak_times.append(t)
        if t >= win_lo:
            family_times.append(t)
            if signature(h) == sig:
                exact_times.append(t)
    repeat = len(exact_times)
    repeat_family = len(family_times)
    age_s = now - _streak_start(streak_times, now, cfg["streak_gap_s"])

    backlog_delta = 0
    if backlog:
        pts = sorted((parse_ts(t), int(c)) for t, c in backlog if parse_ts(t) is not None)
        inside = [(t, c) for t, c in pts if win_lo <= t <= now]
        if len(inside) >= 2:
            backlog_delta = inside[-1][1] - inside[0][1]

    reasons = []
    tier = ROUTINE
    strongest = max(repeat, repeat_family)
    # informational kinds (IDLE, RELOAD, ...) repeat by design; for them only
    # backlog growth is a signal, never their own cadence
    patterned = kind not in cfg["floor_routine"]
    if patterned and strongest >= cfg["repeat_urgent"]:
        tier = URGENT
        reasons.append("repeat=%d in %dh" % (strongest, cfg["window_s"] // 3600))
    elif patterned and strongest >= cfg["repeat_attention"]:
        tier = ATTENTION
        reasons.append("repeat=%d in %dh" % (strongest, cfg["window_s"] // 3600))
    if patterned and age_s >= cfg["age_urgent_s"]:
        tier = URGENT
        reasons.append("open %dm" % (age_s // 60))
    elif patterned and age_s >= cfg["age_attention_s"]:
        tier = max(tier, ATTENTION, key=TIERS.index)
        reasons.append("open %dm" % (age_s // 60))
    if backlog_delta >= cfg["backlog_urgent"]:
        tier = URGENT
        reasons.append("backlog +%d" % backlog_delta)
    elif backlog_delta >= cfg["backlog_attention"]:
        tier = max(tier, ATTENTION, key=TIERS.index)
        reasons.append("backlog +%d" % backlog_delta)
    if kind in cfg["floor_urgent"]:
        tier = URGENT
        reasons.append("kind %s stops the driver" % kind)
    elif kind not in cfg["floor_routine"] and tier == ROUTINE and repeat == 1:
        # a first, unrepeated failure-class alert is worth a look but not a page
        if kind not in ("PACKET_RETRIED",) and _looks_like_failure(kind):
            tier = ATTENTION
            reasons.append("first %s" % kind)
    return {
        "severity": tier,
        "repeat": repeat,
        "repeat_family": repeat_family,
        "age_s": int(age_s),
        "backlog_delta": backlog_delta,
        "reasons": reasons,
        "signature": "%s/%s" % sig,
        "family": "%s/%s" % fam,
    }


_FAILURE_WORDS = ("FAIL", "STUCK", "REFUSED", "MISSING", "CONFLICT", "TIMEOUT",
                  "CRASH", "UNKNOWN", "CAPPED", "EXPIRED", "DOWN", "OWED")


def _looks_like_failure(kind):
    k = kind.upper()
    return any(w in k for w in _FAILURE_WORDS)


def annotate(alerts, *, backlog=None, cfg=None):
    """Assess a whole stream in order; yields (alert, assessment). Alerts are
    processed in file order; each sees only the alerts before it, exactly
    as the driver would have at write time."""
    cfg = config(cfg)
    seen = []
    for a in alerts:
        res = assess(a, seen, backlog=backlog, cfg=cfg)
        seen.append(a)
        # keep the history bounded: nothing older than the window matters
        t = parse_ts(a.get("ts"))
        if t is not None and len(seen) > 2000:
            lo = t - cfg["age_horizon_s"]
            seen = [h for h in seen if (parse_ts(h.get("ts")) or t) >= lo]
        yield a, res


def should_notify(assessment):
    return assessment.get("severity") == URGENT
