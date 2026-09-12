#!/usr/bin/env python3
"""vpctl — command line over vpstore (SPEC.md §vpctl).

Exit codes: 0 ok, 2 usage, 3 refused, 4 conflict.
`--json` is accepted on the report/audit commands.
"""

from __future__ import annotations

import argparse
import json
import sys

import vpstore
from vpstore import Conflict, Refused, Store, Usage


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

def out(args, payload, text=None):
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(text if text is not None else payload)


def csv(value):
    return [v.strip() for v in value.split(",") if v.strip()]


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vpctl", description="Voice Pod v9 control layer")
    p.add_argument("--run-root", default=None, help="override VP_RUN_ROOT")
    sub = p.add_subparsers(dest="cmd", required=True)

    # run ------------------------------------------------------------------
    run = sub.add_parser("run").add_subparsers(dest="sub", required=True)
    r_init = run.add_parser("init")
    r_init.add_argument("run_id")
    r_init.add_argument("--trunk-head", default=None)
    run.add_parser("status").add_argument("--json", action="store_true")
    r_stop = run.add_parser("stop")
    r_stop.add_argument("--note", default=None)
    inhibit = run.add_parser("inhibit").add_subparsers(dest="sub2", required=True)
    i_set = inhibit.add_parser("set")
    i_set.add_argument("--reason", default=None)
    i_clear = inhibit.add_parser("clear")
    i_clear.add_argument("--reason", default=None)
    r_ck = run.add_parser("checkpoint")
    r_ck.add_argument("role")
    r_ck.add_argument("note")
    r_start = run.add_parser("start")
    r_start.add_argument("--id", required=True, help="run id, e.g. run-v12-20260913")
    r_start.add_argument("--trunk-sha", required=True)
    r_start.add_argument("--roster", required=True, help="roster template to copy")
    r_start.add_argument("--audit-root", default=None)
    r_res = run.add_parser("resume")
    r_res.add_argument("--note", default=None)
    r_fin = run.add_parser("finish")
    r_fin.add_argument("--reason", default=None)
    r_rec = run.add_parser("reconcile")
    r_rec.add_argument("--live-pids", default=None,
                       help="comma-separated pids that are known alive (the caller's own)")
    r_rec.add_argument("--json", action="store_true")

    # item verbs (K-07) ----------------------------------------------------
    itm = sub.add_parser("item").add_subparsers(dest="sub", required=True)
    for name in ("pause", "resume", "unblock", "block", "unassign", "pin"):
        ip = itm.add_parser(name)
        ip.add_argument("item")
        if name in ("pause", "unblock"):
            ip.add_argument("--note", default=None)
        if name == "unblock":
            ip.add_argument("--to", default="READY", choices=["READY", "GRADING"])
        if name == "block":
            ip.add_argument("--reason", required=True)
        if name == "pin":
            ip.add_argument("--server", required=True)

    # alerts (K-16) --------------------------------------------------------
    al = sub.add_parser("alert")
    al.add_argument("--kind", required=True)
    al.add_argument("--text", required=True)
    al.add_argument("--item", default=None)

    # union (K-21) ---------------------------------------------------------
    un = sub.add_parser("union").add_subparsers(dest="sub", required=True)
    ur = un.add_parser("record")
    ur.add_argument("--items", required=True)
    ur.add_argument("--union", required=True)
    ur.add_argument("--base", required=True)
    ur.add_argument("--worktree", default=None)
    ur.add_argument("--branch", default=None)
    ur.add_argument("--note", default=None)
    us = un.add_parser("status")
    us.add_argument("union_id")
    us.add_argument("--status", required=True)
    us.add_argument("--note", default=None)
    ul = un.add_parser("list")
    ul.add_argument("--status", default=None)
    ul.add_argument("--json", action="store_true")

    # role -----------------------------------------------------------------
    role = sub.add_parser("role").add_subparsers(dest="sub", required=True)
    ra = role.add_parser("add")
    ra.add_argument("role_id")
    ra.add_argument("--kind", required=True)
    ra.add_argument("--session-ref", default=None)
    ra.add_argument("--model", default=None)
    ra.add_argument("--server", default=None)
    ra.add_argument("--group", type=int, default=None)

    # packet ---------------------------------------------------------------
    packet = sub.add_parser("packet").add_subparsers(dest="sub", required=True)
    ps = packet.add_parser("submit")
    ps.add_argument("item")
    ps.add_argument("--benchmark", required=True)
    ps.add_argument("--packet", required=True)
    ps.add_argument("--base", required=True)
    ps.add_argument("--submitted-by", default=None)
    ps.add_argument("--critical", action="store_true")
    ps.add_argument("--allowed-files", default=None)
    packet.add_parser("ready").add_argument("packet_id")
    pb = packet.add_parser("block")
    pb.add_argument("packet_id")
    pb.add_argument("reason")
    psh = packet.add_parser("show")
    psh.add_argument("item")
    psh.add_argument("--json", action="store_true")

    # item flow ------------------------------------------------------------
    asg = sub.add_parser("assign")
    asg.add_argument("item")
    asg.add_argument("--group", type=int, required=True)
    asg.add_argument("--wip-check", action="store_true", default=True)
    asg.add_argument("--no-wip-check", dest="wip_check", action="store_false")

    cl = sub.add_parser("claim")
    cl.add_argument("item")
    cl.add_argument("--role", required=True)
    cl.add_argument("--worktree", required=True)
    cl.add_argument("--expected-rev", type=int, required=True)

    turn = sub.add_parser("turn").add_subparsers(dest="sub", required=True)
    ts = turn.add_parser("start")
    ts.add_argument("item")
    ts.add_argument("--kind", required=True, choices=list(vpstore.TURN_KINDS))
    ts.add_argument("--session", default=None)
    ts.add_argument("--server", default=None)
    ts.add_argument("--agent", default=None)
    ts.add_argument("--model", default=None)
    ts.add_argument("--variant", default=None)
    ts.add_argument("--pid", type=int, default=None)
    ts.add_argument("--driver-pid", type=int, default=None)
    ts.add_argument("--runner", default=None)
    te = turn.add_parser("end")
    te.add_argument("attempt_id")
    te.add_argument("--status", required=True)
    te.add_argument("--result", default=None)
    te.add_argument("--tokens-in", type=int, default=None)
    te.add_argument("--tokens-out", type=int, default=None)
    te.add_argument("--tokens-reason", type=int, default=None)
    te.add_argument("--cache-read", type=int, default=None)
    te.add_argument("--cache-write", type=int, default=None)
    te.add_argument("--cost", type=float, default=None)
    te.add_argument("--detail", default=None)
    te.add_argument("--session", default=None)

    sr = sub.add_parser("submit-result")
    sr.add_argument("item")
    sr.add_argument("--commit", required=True)
    sr.add_argument("--result", required=True)

    fd = sub.add_parser("findings")
    fd.add_argument("item")
    fd.add_argument("--path", required=True)

    sub.add_parser("mark-available").add_argument("role_id")

    # deliveries -----------------------------------------------------------
    rd = sub.add_parser("reserve-delivery")
    rd.add_argument("--from", dest="from_role", required=True)
    rd.add_argument("--to", dest="to_role", required=True)
    rd.add_argument("--type", dest="type", required=True)
    rd.add_argument("--ref", required=True)

    dl = sub.add_parser("delivery").add_subparsers(dest="sub", required=True)
    for st in ("sent", "ack", "uncertain", "fail"):
        d = dl.add_parser(st)
        d.add_argument("delivery_id")
        d.add_argument("--digest", default=None)

    # review ---------------------------------------------------------------
    review = sub.add_parser("review").add_subparsers(dest="sub", required=True)
    rr = review.add_parser("record")
    rr.add_argument("item")
    rr.add_argument("--subject", required=True)
    rr.add_argument("--base", required=True)
    rr.add_argument("--candidate", required=True)
    rr.add_argument("--reviewer", required=True)
    rr.add_argument("--reviewer-ref", required=True)
    rr.add_argument("--verdict", required=True)
    rr.add_argument("--findings", default=None)

    # proof ----------------------------------------------------------------
    proof = sub.add_parser("proof").add_subparsers(dest="sub", required=True)
    pq = proof.add_parser("request")
    pq.add_argument("candidate_sha")
    pq.add_argument("--base", required=True)
    pq.add_argument("--kind", required=True, choices=["targeted", "full"])
    pq.add_argument("--paths", default=None)
    pr = proof.add_parser("record")
    pr.add_argument("proof_id")
    pr.add_argument("--status", required=True)
    pr.add_argument("--counts", default=None)
    pr.add_argument("--artifacts", default=None)
    pr.add_argument("--pipeline-id", default=None)
    pr.add_argument("--workflow-id", default=None)

    # promotion ------------------------------------------------------------
    promote = sub.add_parser("promote").add_subparsers(dest="sub", required=True)
    pp = promote.add_parser("prepare")
    pp.add_argument("--items", required=True)
    pp.add_argument("--union", required=True)
    pp.add_argument("--base", required=True)
    pp.add_argument("--review", required=True)
    pp.add_argument("--proof", required=True)
    pc = promote.add_parser("commit")
    pc.add_argument("promo_id")
    pc.add_argument("--expected-ref", required=True)
    pc.add_argument("--actual-ref", default=None,
                    help="ref observed after the Integrator's compare-and-swap")

    # violation / msg ------------------------------------------------------
    vi = sub.add_parser("violation").add_subparsers(dest="sub", required=True)
    vr = vi.add_parser("record")
    vr.add_argument("--kind", required=True)
    vr.add_argument("--from", dest="from_role", required=True)
    vr.add_argument("--to", dest="to_role", required=True)
    vr.add_argument("--detail", required=True)

    msg = sub.add_parser("msg").add_subparsers(dest="sub", required=True)
    ml = msg.add_parser("log")
    ml.add_argument("--direction", required=True, choices=["send", "recv"])
    ml.add_argument("--from", dest="from_role", required=True)
    ml.add_argument("--to", dest="to_role", required=True)
    ml.add_argument("--type", dest="type", required=True)
    ml.add_argument("--ref", default=None)
    ml.add_argument("--body-file", default=None)
    ml.add_argument("--session-ref", default=None)
    ml.add_argument("--result", default=None)

    esc = sub.add_parser("escalate")
    esc.add_argument("--kind", required=True,
                     choices=sorted(vpstore.Store.ESCALATE_KINDS))
    esc.add_argument("--item", required=True)
    esc.add_argument("--attempt", default=None)
    esc.add_argument("--detail", required=True)

    # reports --------------------------------------------------------------
    rep = sub.add_parser("report")
    rep.add_argument("what", choices=["pending", "liveness", "costs", "violations",
                                      "latency", "idle", "items", "summary"])
    rep.add_argument("--json", action="store_true")

    au = sub.add_parser("audit")
    au.add_argument("item")
    au.add_argument("--json", action="store_true")

    sub.add_parser("render").add_argument("--json", action="store_true")
    sub.add_parser("seal").add_argument("--json", action="store_true")
    return p


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def dispatch(args, st: Store):
    c, s = args.cmd, getattr(args, "sub", None)

    if c == "run":
        if s == "init":
            return out(args, st.run_init(args.run_id, args.trunk_head),
                       f"run {args.run_id} RUNNING")
        if s == "status":
            r = st.run_status()
            return out(args, r, f"{r['run_id']} {r['status']} head={r['trunk_head']}")
        if s == "stop":
            return out(args, {"status": st.run_stop(args.note)}, "STOPPING")
        if s == "inhibit":
            if args.sub2 == "set":
                return out(args, {"status": st.run_inhibit_set(args.reason)}, "INHIBITED")
            return out(args, {"status": st.run_inhibit_clear(args.reason)}, "RUNNING")
        if s == "checkpoint":
            return out(args, {"seq": st.run_checkpoint(args.role, args.note)},
                       "checkpoint recorded")
        if s == "finish":
            return out(args, {"status": st.run_finish(args.reason)}, "STOPPED")
        if s == "resume":
            return out(args, {"status": st.run_resume(args.note)}, "RUNNING")
        if s == "start":
            return out(args, _run_start(args), f"run {args.id} started")
        if s == "reconcile":
            pids = csv(args.live_pids) if args.live_pids else []
            res = st.reconcile(pids)
            return out(args, res, f"orphaned: {len(res['orphaned'])}")

    if c == "item":
        fn = {"pause": lambda: st.item_pause(args.item, args.note),
              "resume": lambda: st.item_resume(args.item),
              "unblock": lambda: st.item_unblock(args.item, args.note, args.to),
              "block": lambda: st.item_block(args.item, args.reason),
              "unassign": lambda: st.item_unassign(args.item),
              "pin": lambda: st.item_pin(args.item, args.server)}[s]
        res = fn()
        return out(args, {"item": args.item, "status": res}, f"{args.item} {res}")

    if c == "alert":
        ts = st.alert(args.kind, args.text, args.item)
        return out(args, {"ts": ts, "kind": args.kind}, f"alert {args.kind} {ts}")

    if c == "union":
        if s == "record":
            uid = st.union_record(csv(args.items), args.union, args.base,
                                  args.worktree, args.branch, args.note)
            return out(args, {"union_id": uid}, uid)
        if s == "status":
            return out(args, {"union_id": args.union_id,
                              "status": st.union_status(args.union_id, args.status,
                                                        args.note)},
                       f"{args.union_id} {args.status}")
        if s == "list":
            rows = st.unions(args.status)
            return out(args, {"unions": rows}, "\n".join(
                f"{r['union_id']} {r['status']} {r['union_sha']} {','.join(r['items'])}"
                for r in rows) or "none")

    if c == "role" and s == "add":
        return out(args, st.role_add(args.role_id, args.kind, args.session_ref,
                                     args.model, args.server, args.group),
                   f"role {args.role_id} added")

    if c == "packet":
        if s == "submit":
            pid = st.packet_submit(args.item, args.benchmark, args.packet, args.base,
                                   args.submitted_by, args.critical,
                                   csv(args.allowed_files) if args.allowed_files else None)
            return out(args, {"packet_id": pid}, pid)
        if s == "ready":
            return out(args, {"packet_id": st.packet_ready(args.packet_id)},
                       f"{args.packet_id} READY")
        if s == "block":
            return out(args, {"packet_id": st.packet_block(args.packet_id, args.reason)},
                       f"{args.packet_id} BLOCKED")
        if s == "show":
            row = st.packet_show(args.item)
            return out(args, row, _packet_text(row))

    if c == "assign":
        return out(args, {"item": args.item, "status": st.assign(
            args.item, args.group, args.wip_check)}, f"{args.item} ASSIGNED")

    if c == "claim":
        return out(args, {"item": args.item, "status": st.claim(
            args.item, args.role, args.worktree, args.expected_rev)},
            f"{args.item} BUILDING")

    if c == "turn":
        if s == "start":
            aid = st.turn_start(args.item, args.kind, args.session, args.server,
                                args.agent, args.model, args.variant, args.pid,
                                args.driver_pid, args.runner)
            return out(args, {"attempt_id": aid}, aid)
        if s == "end":
            st.turn_end(args.attempt_id, args.status, args.result, args.tokens_in,
                        args.tokens_out, args.tokens_reason, args.cache_read, args.cost,
                        args.cache_write, args.detail, args.session)
            return out(args, {"attempt_id": args.attempt_id, "status": args.status},
                       f"{args.attempt_id} {args.status}")

    if c == "submit-result":
        return out(args, {"item": args.item,
                          "status": st.submit_result(args.item, args.commit, args.result)},
                   f"{args.item} GRADING")

    if c == "findings":
        res = st.findings(args.item, args.path)
        return out(args, res, f"{args.item} {res['status']} all_pass={res['all_pass']}")

    if c == "mark-available":
        return out(args, {"role_id": st.mark_available(args.role_id)},
                   f"{args.role_id} available")

    if c == "reserve-delivery":
        did = st.reserve_delivery(args.from_role, args.to_role, args.type, args.ref)
        return out(args, {"delivery_id": did}, did)

    if c == "delivery":
        stt = st.delivery_status(args.delivery_id, s, args.digest)
        return out(args, {"delivery_id": args.delivery_id, "status": stt},
                   f"{args.delivery_id} {stt}")

    if c == "review" and s == "record":
        rid = st.review_record(args.item, args.subject, args.base, args.candidate,
                               args.reviewer, args.reviewer_ref, args.verdict,
                               args.findings)
        return out(args, {"review_id": rid}, rid)

    if c == "proof":
        if s == "request":
            pid = st.proof_request(args.candidate_sha, args.base, args.kind,
                                   csv(args.paths) if args.paths else None)
            return out(args, {"proof_id": pid}, pid)
        if s == "record":
            counts = None
            if args.counts:
                with open(args.counts, "r", encoding="utf-8") as fh:
                    counts = json.load(fh)
            st.proof_record(args.proof_id, args.status, counts, args.artifacts,
                            args.pipeline_id, args.workflow_id)
            return out(args, {"proof_id": args.proof_id, "status": args.status},
                       f"{args.proof_id} {args.status}")

    if c == "promote":
        if s == "prepare":
            pid = st.promote_prepare(csv(args.items), args.union, args.base,
                                     args.review, args.proof)
            return out(args, {"promo_id": pid}, pid)
        if s == "commit":
            stt = st.promote_commit(args.promo_id, args.expected_ref, args.actual_ref)
            return out(args, {"promo_id": args.promo_id, "status": stt},
                       f"{args.promo_id} {stt}")

    if c == "violation" and s == "record":
        vid = st.violation_record(args.kind, args.from_role, args.to_role, args.detail)
        return out(args, {"violation_id": vid}, str(vid))

    if c == "msg" and s == "log":
        res = st.msg_log(args.direction, args.from_role, args.to_role, args.type,
                         args.ref, args.body_file, args.session_ref, args.result)
        return out(args, res, res["envelope"])

    if c == "escalate":
        seq = st.escalate(args.kind, args.item, args.attempt, args.detail)
        return out(args, {"seq": seq, "kind": args.kind, "item": args.item},
                   f"{args.kind} {args.item} seq={seq}")

    if c == "report":
        data = st.report(args.what)
        return out(args, data, _report_text(args.what, data))

    if c == "audit":
        a = st.audit(args.item)
        return out(args, a, _audit_text(a))

    if c == "render":
        files = st.render()
        return out(args, {"files": files}, "\n".join(files))

    if c == "seal":
        path = st.seal()
        return out(args, {"manifest": path}, path)

    raise Usage(f"unhandled command {c} {s}")


def _run_start(args):
    """K-07: create the run root, copy the roster + Claude role settings,
    init the store, write CURRENT-RUN.  Refuses a second live run."""
    import os
    import shutil
    here = os.path.dirname(os.path.abspath(__file__))
    roster = json.load(open(args.roster, "r", encoding="utf-8"))
    audit_root = args.audit_root or roster.get("run", {}).get("audit_root") or \
        os.path.join(vpstore.DEFAULT_CN, "test-logs", "audit")
    run_root = os.path.join(audit_root, args.id)
    current = os.path.join(audit_root, "CURRENT-RUN")
    if os.path.exists(current):
        prev = open(current, "r", encoding="utf-8").read().strip()
        hb = os.path.join(prev, "driver.heartbeat")
        if prev != run_root and os.path.exists(hb):
            import time
            age = time.time() - os.path.getmtime(hb)
            if age < 120:
                raise Refused(f"another run is live: {prev} (heartbeat {age:.0f}s old)")
    if os.path.exists(os.path.join(run_root, "state.db")):
        raise Conflict(f"run root {run_root} already has a state.db")
    os.makedirs(run_root, exist_ok=True)
    for sub in ("packets", "turns", "reviews", "proofs", "unions", "claude-settings", "probes"):
        os.makedirs(os.path.join(run_root, sub), exist_ok=True)
    shutil.copy(args.roster, os.path.join(run_root, "roster.json"))
    src = os.path.join(here, "claude-settings")
    if os.path.isdir(src):
        for n in os.listdir(src):
            if n.endswith(".json"):
                shutil.copy(os.path.join(src, n), os.path.join(run_root, "claude-settings", n))
    st = vpstore.open_store(run_root)
    st.run_init(args.id, args.trunk_sha)
    st.close()
    with open(os.path.join(run_root, "OWNER-ALERTS.md"), "a", encoding="utf-8") as fh:
        fh.write(f"# OWNER-ALERTS {args.id}\n\n")
    with open(current, "w", encoding="utf-8") as fh:
        fh.write(run_root + "\n")
    return {"run_id": args.id, "run_root": run_root, "current": current,
            "trunk_sha": args.trunk_sha}


def _packet_text(row):
    return "\n".join(f"{k}: {v}" for k, v in row.items())


def _report_text(what, data):
    lines = [f"# report {what}"]
    for key, rows in data.items():
        if isinstance(rows, list):
            lines.append(f"{key}: {len(rows)}")
            for r in rows:
                lines.append("  " + json.dumps(r, sort_keys=True, default=str))
        else:
            lines.append(f"{key}: {rows}")
    return "\n".join(lines)


def _audit_text(a):
    lines = [f"# audit {a['item']}",
             f"status: {a['state']['status'] if a['state'] else '-'}", ""]
    for e in a["trail"]:
        lines.append(f"{e['ts']}  {e['source']:<9} {e['kind']:<28} {e['ref'] or '-'}")
    lines += ["", "files:"]
    for k, v in a["files"].items():
        lines.append(f"  {k}: {v or '-'}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    # `--json` is accepted anywhere on every verb (the driver appends it to
    # each call); verbs whose parser does not declare it get it stripped here.
    want_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # argparse already printed
        return 2 if e.code else 0
    if want_json:
        args.json = True
    st = None
    try:
        st = vpstore.open_store(args.run_root)
        dispatch(args, st)
        return 0
    except Usage as e:
        print(f"usage: {e}", file=sys.stderr)
        return 2
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 3
    except Conflict as e:
        print(f"conflict: {e}", file=sys.stderr)
        return 4
    except FileNotFoundError as e:
        print(f"usage: {e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"usage: bad json ({e})", file=sys.stderr)
        return 2
    finally:
        if st:
            st.close()


if __name__ == "__main__":
    sys.exit(main())
