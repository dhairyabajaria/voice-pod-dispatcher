"""Detect and REPORT that Claude sessions have gone quiet with work outstanding. Never poke them.

2026-09-07/08: every Claude session — BOSS, both workers, Temp — finished a turn and stopped, the
Codex queue was empty, and the dispatcher stayed alive and healthy with nothing to do. Six hours
produced nothing and NOTHING ON ANY INSTRUMENT SAID SO. The heartbeat was fresh the whole time,
because a heartbeat measures the daemon, not the programme.

BOSS's ruling, 2026-09-08: build the DETECTOR, not the poker. Waking a session is technically
available to a daemon — measured: the per-session socket in /tmp/cc-socks accepts a connection from
a process with no Claude environment at all, and the CLI binary documents the NDJSON protocol — but
it requires handing a long-running daemon each session's socket path AND its live messaging token.
`CLAUDE_CODE_MESSAGING_SOCKET` is in the `HANDLES` list `museadapter.child_env` deliberately strips
from every spawned worker, so a poker reverses that decision. **That is the owner's call, and this
module is what makes it on evidence instead of on argument.**

SO THIS MODULE HOLDS NO CREDENTIAL AND OPENS NO SOCKET. It reads a directory listing, asks the OS
whether a pid is alive, and stats a transcript file. Nothing here can send anything to anyone.

THE THREE STATES ARE KEPT APART ON PURPOSE (BOSS: "a board that cannot tell 'session ended' from
'session ignoring me' is board-reports-what-it-sent again"):

    LIVE      the socket's pid is alive and is a claude process
    ENDED     the socket file is there and its pid is not — a leftover, not a session
    UNKNOWN   we could not tell, and say so rather than guessing

and, for a LIVE session, `quiet_s` is None when the transcript could not be found. **None is NOT
MEASURED. It is never rendered as 0 and never counted as quiet**, because "this session has been
silent for hours" and "we cannot see this session" are different claims and only one of them is
about the session.
"""
from __future__ import annotations

import os
import re
import subprocess
import time

#: Where the CLI puts one unix socket per session process, named for that pid. Measured 2026-09-08;
#: the CLI binary also accepts /run/user/<uid>/cc-socks and a termux path, neither of which exists on
#: this machine. Both /tmp and /private/tmp are listed because /tmp is a symlink here and a caller
#: may hand us either.
SOCK_DIRS: tuple[str, ...] = ("/tmp/cc-socks", "/private/tmp/cc-socks")

LIVE, ENDED, UNKNOWN = "LIVE", "ENDED", "UNKNOWN"
_SOCK_RE = re.compile(r"^(\d+)\.sock$")


def _sock_dir(dirs=SOCK_DIRS):
    for d in dirs:
        if os.path.isdir(d):
            return d
    return None


def pid_is_claude_session(pid, *, runner=None):
    """Is this pid alive AND a claude process? -> True | False | None (could not tell).

    THE COMMAND CHECK IS NOT DECORATION. Socket files outlive their sessions — six sockets were on
    disk against far fewer live sessions — and pids are reused. A bare liveness test would report a
    recycled pid as a live Claude session, which is the confident-wrong-answer shape this whole
    module exists to avoid.

    `ps -eo comm` TRUNCATES AT 16 CHARACTERS and would miss the real name; the two-stage
    `ps -p <pid> -o comm=` does not. That trap is recorded from an earlier argv census that counted
    six pytest runs when one was live.
    """
    run = runner or _ps_comm
    try:
        comm = run(pid)
    except Exception:
        return None
    if comm is None:
        return None
    if not comm.strip():
        return False        # ps returned cleanly with no row: the pid is not running
    return "claude" in comm.lower()


def _ps_comm(pid):
    p = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="],
                       capture_output=True, text=True, timeout=5)
    return p.stdout if p.returncode == 0 else ""


def session_uuid(pid, *, runner=None):
    """The session's own transcript id, from its argv. -> str | None (NOT MEASURED).

    MEASURED 2026-09-08, and the first design was wrong: a session does NOT hold its transcript
    open. `lsof -p <pid>` on a live session lists its socket, its binary and its sockets and no
    `.jsonl` at all — the CLI appends and closes. That first cut passed 26 green checks against
    injected probes and reported "quiet NOT MEASURED" for all six real sessions: a detector that
    could never fire, shipped green. The same class as the import-time default argument and the
    unrecorded cwd, twice in one night.

    What a session DOES expose is its argv: `--resume=<uuid>`. That is an exact pid-to-transcript
    mapping needing no credential and no lsof. A session started fresh rather than resumed may carry
    no such flag, and then this returns None — NOT MEASURED, which is the honest answer and not a
    guess at the newest file in the directory.
    """
    run = runner or _ps_args
    try:
        args = run(pid)
    except Exception:
        return None
    if not args:
        return None
    m = re.search(r"--resume[= ]([0-9a-fA-F-]{36})", args)
    return m.group(1) if m else None


def _ps_args(pid):
    p = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "args="],
                       capture_output=True, text=True, timeout=5)
    return p.stdout if p.returncode == 0 else ""


def transcript_mtime(pid, *, uuid_of=None, projects_root=None):
    """When did this session last write its transcript? -> float | None (NOT MEASURED)."""
    import glob
    uid = (uuid_of or session_uuid)(pid)
    if not uid:
        return None
    root = projects_root or os.path.expanduser("~/.claude/projects")
    hits = glob.glob(os.path.join(root, "*", f"{uid}.jsonl"))
    if len(hits) != 1:
        # 0 is a transcript we cannot find; >1 is two projects claiming one id. Neither is a time.
        return None
    try:
        return os.path.getmtime(hits[0])
    except OSError:
        return None


def census(*, dirs=SOCK_DIRS, is_claude=pid_is_claude_session, mtime=transcript_mtime,
           now=None):
    """One row per socket on disk. -> list[dict], sorted by pid.

    Every row says which of the three states it is in and, for a LIVE one, how long it has been
    quiet — or that we could not measure it.
    """
    now = time.time() if now is None else now
    d = _sock_dir(dirs)
    if d is None:
        return []
    rows = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    for name in names:
        m = _SOCK_RE.match(name)
        if not m:
            continue
        pid = int(m.group(1))
        alive = is_claude(pid)
        row = {"pid": pid, "sock": os.path.join(d, name), "quiet_s": None}
        if alive is None:
            row["state"] = UNKNOWN
        elif alive:
            row["state"] = LIVE
            t = mtime(pid)
            row["quiet_s"] = None if t is None else max(0, int(now - t))
        else:
            row["state"] = ENDED
        rows.append(row)
    return sorted(rows, key=lambda r: r["pid"])


def summarise(rows, *, quiet_after_s=1800):
    """-> dict of counts. `quiet` counts only sessions MEASURED to be quiet."""
    live = [r for r in rows if r["state"] == LIVE]
    measured = [r for r in live if r["quiet_s"] is not None]
    return {
        "sockets": len(rows),
        "live": len(live),
        "ended": len([r for r in rows if r["state"] == ENDED]),
        "unknown": len([r for r in rows if r["state"] == UNKNOWN]),
        "unmeasured": len(live) - len(measured),
        "quiet": len([r for r in measured if r["quiet_s"] >= quiet_after_s]),
        "quietest_s": max([r["quiet_s"] for r in measured], default=None),
    }


def queue_shape(items):
    """What the queue has for anyone to do. -> dict.

    `dispatchable` is the number a worker could pick up right now. The six lost hours had an EMPTY
    queue, so a poke would have woken a session that found nothing and stopped again — which is why
    this count travels with every quiet finding rather than being left for the reader to look up.
    """
    by = {}
    for it in items or []:
        by[str(it.get("status") or "?")] = by.get(str(it.get("status") or "?"), 0) + 1
    # THE TERMINAL SET IS THE PRODUCT'S OWN, not one I judged: dispatcher.py:3268 uses
    # ("landed", "merged", "done"). Inventing a second definition here would make this row disagree
    # with the daemon about what is finished.
    terminal = {"landed", "merged", "done"}
    return {
        "total": len(items or []),
        "dispatchable": by.get("queued", 0),
        "in_flight": by.get("dispatched", 0),
        "awaiting_boss": by.get("reported", 0) + by.get("parked", 0) + by.get("broken", 0),
        "outstanding": sum(n for s, n in by.items() if s not in terminal),
        "by_status": by,
    }


def finding(rows, shape, *, quiet_after_s=1800):
    """The one sentence worth emitting, or None when there is nothing to say. -> (key, text) | None

    Deliberately reports the SHAPE and never a remedy: this module does not know whether the answer
    is more queue, a restart, or nothing at all, and a detector that recommends an action is one
    step from taking it.
    """
    s = summarise(rows, quiet_after_s=quiet_after_s)
    if not s["live"]:
        return None
    mins = quiet_after_s // 60
    if s["quiet"] and not shape["dispatchable"] and shape["outstanding"]:
        return ("SESSIONS_QUIET_QUEUE_EMPTY",
                f"{s['quiet']} of {s['live']} live session(s) quiet >{mins}m, "
                f"NOTHING dispatchable, and {shape['outstanding']} item(s) outstanding "
                f"({shape['awaiting_boss']} awaiting BOSS). The programme is stopped and the "
                f"daemon is healthy — a fresh heartbeat measures the daemon, not the work.")
    if s["quiet"] and shape["dispatchable"]:
        return ("SESSIONS_QUIET_WORK_WAITING",
                f"{s['quiet']} of {s['live']} live session(s) quiet >{mins}m while "
                f"{shape['dispatchable']} item(s) are dispatchable.")
    if s["quiet"]:
        return ("SESSIONS_QUIET",
                f"{s['quiet']} of {s['live']} live session(s) quiet >{mins}m; "
                f"queue: {shape['by_status'] or 'empty'}.")
    return None


def board_lines(rows, shape, *, quiet_after_s=1800):
    """What the board prints. Every state is named; nothing absent is rendered as a number."""
    s = summarise(rows, quiet_after_s=quiet_after_s)
    if not rows:
        return ["sessions: NOT MEASURED — no socket directory found; this row is blind, not empty"]
    out = [f"sessions: {s['live']} live, {s['quiet']} quiet >{quiet_after_s // 60}m, "
           f"{s['ended']} ended socket(s) left on disk"
           + (f", {s['unknown']} UNKNOWN" if s["unknown"] else "")
           + (f", {s['unmeasured']} live but NOT MEASURED" if s["unmeasured"] else "")
           + f"  |  queue: {shape['dispatchable']} dispatchable, "
             f"{shape['outstanding']} outstanding"]
    for r in rows:
        if r["state"] != LIVE:
            continue
        q = "quiet NOT MEASURED" if r["quiet_s"] is None else f"quiet {r['quiet_s'] // 60}m"
        out.append(f"  pid {r['pid']:<7} LIVE   {q}")
    return out
