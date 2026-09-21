#!/usr/bin/env python3
"""vpsweep.py -- packets on disk that never became run-state rows.

  python3 vp/vpsweep.py --roster <roster.json> [--json] [--quiet-when-clean]

THE CLASS THIS EXISTS FOR (Architect 2, 2026-09-21).  A packet with no
`parent_contract:` header and no lane id anywhere in its body passes BOTH
linters, sits on disk looking placed, and never enters run-state.  It produces
no red, no blocker and no failing row -- there is nothing to be red, because the
row was never created.  `_pack_instantiate` does `alert_once("pack-parent:<id>")`,
which by construction fires once, into a log nobody greps.  Two P0 packets sat
stranded this way and were found only because PACKET_NO_PARENT happened to match
an unrelated monitor filter.

WHY A SWEEP AND NOT A LINT ARM.  A lint arm at placement time catches only
packets placed after it ships -- every packet already on disk stays invisible --
and only this one cause.  A sweep compares what is on disk against what is in
run-state, so it catches the CLASS: any lint-clean packet with no live row, for
any reason, including reasons nobody has thought of yet.

IT REPORTS WHAT IT EXAMINED, NOT ONLY WHAT IT FOUND.  "0 stranded" from a real
scan of 178 packets and "0 stranded" because the directory was empty, misspelled
or unreadable are the same sentence and opposite facts.  Every result carries
`examined`, and a scan that finds no packets at all is an ERROR (exit 2), never
a clean bill of health.  Learned the hard way three times on this run.

IT CLASSIFIES WITH THE DRIVER'S OWN FUNCTIONS, IN THE DRIVER'S ORDER.  Every
"why" below comes from `vppack.bind` / `owner_gate_open` / `dependency_tasks` /
`parent_for`, checked in the same sequence `LaneDriver._pack_instantiate` uses.
A sweep that re-implements the rule reports a disagreement with itself, not with
the driver -- and then cries wolf until it is switched off.

EXIT CODES: 0 clean, 1 stranded packets found, 2 the sweep could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vppack  # noqa: E402

# A packet legitimately has no row while it waits for something nameable.  These
# are reported as `waiting`, not `stranded` -- the difference is whether anyone
# is expected to act.
WAITING = "waiting"
STRANDED = "stranded"
BOUND = "bound"
UNSEEN = "unseen"
BLOCKED_PARENT = "blocked_parent"

# A parent row in one of these will never produce an output, so a twin waiting
# on it is not waiting -- it is finished, silently. Not raised as an alarm: a
# CANCELLED row usually has a successor in blocker.closers and the packet may
# rebind to it. Reported separately so a human can tell the two apart, which
# "waiting" alone cannot.
TERMINAL_PARENT_STATES = ("CANCELLED", "INVALID_EVIDENCE")

# LaneDriver.UNION_MEMBER_STATES -- the states in which a parent's output counts.
UNION_MEMBER_STATES = ("VERIFIED", "INTEGRATED", "ACCEPTED")


class SweepUnusable(Exception):
    """The sweep could not run. Never downgrade this to a clean result."""


def _load_run_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SweepUnusable("run-state unreadable at %s: %s" % (path, exc))
    tasks = (data or {}).get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise SweepUnusable(
            "run-state at %s has no tasks; a sweep against an empty run-state "
            "would call every packet stranded" % path)
    return tasks


def bindings_from_disk(run_root, pack_ids, tasks):
    """{packet_id: task} from run_root/packets/<pid>.json -- the driver's durable
    binding memory (LaneDriver._pack_restore).

    Without this a packet whose row has FINISHED re-resolves through bind()
    alone, which offers a dynamic twin instead of the row it actually drove, so
    a completed packet can look like it never bound. vppack.bound_task takes
    `bindings` for exactly this reason; passing None is what its docstring warns
    against.
    """
    out = {}
    if not run_root:
        return out
    pdir = Path(run_root) / "packets"
    if not pdir.is_dir():
        return out
    for pid in pack_ids:
        f = pdir / ("%s.json" % pid)
        if not f.exists():
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        t = rec.get("task")
        if t in tasks:
            out[pid] = t
    return out


def classify(p, pack, tasks, roster, seen=None, bindings=None):
    """(status, reason) for one packet, in LaneDriver._pack_instantiate's order.

    Returns the driver's own verdict, not a second opinion about it.
    """
    task = vppack.bound_task(p, tasks, bindings)
    if task and task in tasks:
        return BOUND, "row %s is in run-state (%s)" % (task, tasks[task].get("state"))

    mode = vppack.bind(p, tasks)
    if mode[0] == "hold":
        return WAITING, "held: %s" % mode[1]

    # dynamic and not yet instantiated -- ask why, in the driver's sequence
    if not vppack.owner_gate_open(p, roster):
        return WAITING, "owner gate %s is not open" % p.get("owner_gate")

    gated = {q for q, qp in pack.items() if not vppack.owner_gate_open(qp, roster)}
    # bindings, not None: the driver passes its binding memory here, and a
    # dependency whose packet has FINISHED resolves only through it.
    if vppack.dependency_tasks(p, pack, tasks, bindings, gated=gated) is None:
        return WAITING, "a dependency packet of %s is not bound yet" % (p.get("depends_on") or [])

    if vppack.is_hosted_twin(p) and p.get("twin_of"):
        # Mirror LaneDriver._twin_parent_output exactly: resolve the PARENT PACKET
        # to its bound row, then require an accepted state AND an output_sha.
        #
        # My first cut did `t.startswith(twin_of + "-")`, which matches the TWIN
        # ITSELF -- `L17-REPLY-WIRING-FIX-1-HOSTED` starts with
        # `L17-REPLY-WIRING-FIX-1-` -- so a twin was its own parent and three
        # legitimately-waiting packets were reported stranded on the first run.
        # Prefix-matching ids is banned here for the same reason it is banned in
        # twin_base.requires and in proof-record lookup.
        parent_pack = pack.get(p["twin_of"])
        ptask = vppack.bound_task(parent_pack, tasks, bindings) if parent_pack else None
        prow = tasks.get(ptask) or {}
        if (prow.get("state") not in UNION_MEMBER_STATES) or not prow.get("output_sha"):
            pstate = prow.get("state") or "no row"
            status = BLOCKED_PARENT if pstate in TERMINAL_PARENT_STATES else WAITING
            tail = ("that row is terminal, so no output will ever arrive unless the "
                    "packet rebinds to a successor") if status == BLOCKED_PARENT \
                else "the parent has no accepted output yet"
            return status, "hosted twin of %s (row %s, %s): %s" % (
                p["twin_of"], ptask or "unbound", pstate, tail)

    if seen is not None and p["id"] not in seen:
        # Placed since the driver's last pack scan.  It has not failed to bind --
        # nothing has tried yet.  R-PORTAL-OPTION-RACE-AND-TIMEOUT-OVERRIDES was
        # created 4 minutes before this sweep first ran and the driver picked it
        # up 3 minutes later; calling that "stranded" would train people to
        # ignore the word on exactly the packets that are newest.
        return UNSEEN, "placed since the driver's last pack scan; not yet attempted"

    parent, how = vppack.parent_for(p, tasks, pack)
    if not parent:
        # THE silent class: lint-clean, placed, and unreachable by the scheduler.
        return STRANDED, ("no parent_contract derivable and no lane id in the body; "
                          "add `parent_contract:` to its header")
    return STRANDED, "bindable (parent %s via %s) but no run-state row exists" % (parent, how)


def _seen_packet_ids(driver_log, ids):
    """The packet ids the driver has actually mentioned. None when no log was
    given -- and None means "do not claim to know", not "none of them"."""
    if not driver_log:
        return None
    try:
        text = Path(driver_log).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return {pid for pid in ids if pid in text}


def sweep(pack_dir, run_state_path, roster=None, driver_log=None, run_root=None):
    """CLI entry: read run-state off disk, then sweep. Raises SweepUnusable
    rather than returning a clean result it cannot support."""
    return sweep_loaded(pack_dir, _load_run_state(run_state_path), roster=roster,
                        driver_log=driver_log, run_root=run_root)


def sweep_loaded(pack_dir, tasks, roster=None, driver_log=None, run_root=None,
                 pack=None, lint=None):
    """D153: the same sweep against tasks the CALLER already holds.

    The driver must not re-read run-state.json off disk: the control plane owns
    that file and may be mid-write, and the driver's own `state_view()` is the
    authoritative copy anyway. Re-reading would make the in-tick sweep race the
    writer and disagree with the driver about the very rows it is judging.

    `pack`/`lint` may likewise be passed in when the caller already loaded them
    (the driver reloads the pack on the same timer), but the emptiness guard
    below still applies to whatever it is given -- a caller handing in an empty
    pack gets the same refusal as a caller with an empty directory.
    """
    roster = roster or {}
    pack_dir = Path(pack_dir)
    if not pack_dir.is_dir():
        raise SweepUnusable("pack dir does not exist: %s" % pack_dir)
    if not isinstance(tasks, dict) or not tasks:
        raise SweepUnusable("run-state holds no tasks; refusing to report every "
                            "packet stranded from a state file that says nothing")

    if pack is None:
        pack, lint = vppack.load_pack(pack_dir)
    lint = lint or []
    dirs = [d for d in sorted(pack_dir.iterdir()) if d.is_dir()]
    without_packet = [d.name for d in dirs if not (d / "PACKET.md").exists()]
    if not pack:
        raise SweepUnusable(
            "%s holds %d directories but no loadable PACKET.md; refusing to report "
            "'0 stranded' from a scan that examined nothing" % (pack_dir, len(dirs)))

    seen = _seen_packet_ids(driver_log, list(pack))
    bindings = bindings_from_disk(run_root, list(pack), tasks)
    out = {BOUND: [], WAITING: [], STRANDED: [], UNSEEN: [], BLOCKED_PARENT: []}
    for pid in sorted(pack):
        status, reason = classify(pack[pid], pack, tasks, roster, seen, bindings)
        out[status].append({"packet": pid, "reason": reason})
    return {
        "pack_dir": str(pack_dir),
        "examined": len(pack),
        "dirs_seen": len(dirs),
        "dirs_without_packet_md": without_packet,
        "run_state_tasks": len(tasks),
        "lint": lint,
        "bound": out[BOUND],
        "waiting": out[WAITING],
        "unseen": out[UNSEEN],
        "blocked_parent": out[BLOCKED_PARENT],
        "stranded": out[STRANDED],
        "driver_log_read": bool(seen is not None),
        "bindings_restored": len(bindings),
    }


def render(res):
    lines = [
        "packet sweep: examined %d packet(s) in %s against %d run-state task(s)"
        % (res["examined"], res["pack_dir"], res["run_state_tasks"]),
        "  bound %d   waiting %d   unseen %d   parent-terminal %d   STRANDED %d"
        % (len(res["bound"]), len(res["waiting"]), len(res["unseen"]),
           len(res["blocked_parent"]), len(res["stranded"])),
    ]
    if not res["driver_log_read"]:
        lines.append("  (no driver.log read: a newly placed packet cannot be told "
                     "apart from a stranded one, so `unseen` is always 0 here)")
    if res["dirs_without_packet_md"]:
        lines.append("  %d director(ies) with no PACKET.md: %s"
                     % (len(res["dirs_without_packet_md"]),
                        ", ".join(res["dirs_without_packet_md"][:8])))
    if res["lint"]:
        lines.append("  %d lint line(s) from load_pack" % len(res["lint"]))
    if res["blocked_parent"]:
        lines.append("")
        lines.append("parent-terminal -- waiting on a row that will never produce an output:")
        for row in res["blocked_parent"]:
            lines.append("  %-44s %s" % (row["packet"][:44], row["reason"]))
    if res["stranded"]:
        lines.append("")
        lines.append("STRANDED -- on disk, lint-clean, and no run-state row will ever appear:")
        for row in res["stranded"]:
            lines.append("  %-44s %s" % (row["packet"][:44], row["reason"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roster", required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--driver-log", default=None,
                    help="driver.log; lets the sweep tell a newly placed packet "
                         "(unseen) from one the driver tried and could not bind (stranded)")
    ap.add_argument("--quiet-when-clean", action="store_true",
                    help="print nothing when no packet is stranded (for cron/timer use)")
    args = ap.parse_args(argv)
    try:
        roster_raw = json.loads(Path(args.roster).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("SWEEP UNUSABLE: roster unreadable: %s" % exc, file=sys.stderr)
        return 2
    roster = roster_raw
    run = roster.get("run") or {}
    pack_dir = run.get("pack_dir") or run.get("packets_dir")
    run_state = run.get("run_state") or (roster.get("control") or {}).get("state")
    if not pack_dir or not run_state:
        print("SWEEP UNUSABLE: roster names no pack dir (run.pack_dir/packets_dir) "
              "or no run-state (run.run_state/control.state)", file=sys.stderr)
        return 2
    try:
        root = run.get("run_root") or str(Path(args.roster).resolve().parent)
        log = args.driver_log
        if log is None:
            guess = Path(root) / "driver.log"
            log = str(guess) if guess.exists() else None
        res = sweep(pack_dir, run_state, roster, driver_log=log, run_root=root)
    except SweepUnusable as exc:
        print("SWEEP UNUSABLE: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True))
    elif not (args.quiet_when_clean and not res["stranded"]):
        print(render(res))
    return 1 if res["stranded"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
