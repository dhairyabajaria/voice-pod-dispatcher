#!/usr/bin/env python3
"""laneladder.py -- v13 ladder L0-L7 + the five live-qualification points
(SINGLE-CHIEF-RECOVERY.md) on throwaway run roots with FAKE runners.

    python3 laneladder.py --root <ladder dir> --trunk <repo> --catalog <lane-contracts.json> \
        --control <orchestration_control.py> --pack-dir <03-PACKETS> --receipt <DRY-RUN-RECEIPT.json> \
        [--pytest-python <python>] [--l7-seconds 90]

No model call, no proof harness, no CircleCI, no write to the source trunk or
the live run-state: every rung runs on its own clone + run-state under --root.
Rungs that need a real model (L6) or the Operator's session (L2) are recorded
SKIPPED with the reason, never faked.  Every timestamp is taken from the clock
at the moment it is recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import lanedriver   # noqa: E402
import lanedryrun   # noqa: E402
from lanedriver import DryRunProof, DryRunRunner, LaneDriver, utc_ms  # noqa: E402


class Args(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


class RoleDelayRunner(DryRunRunner):
    """DryRunRunner whose delay depends on the role (reviews slow, builds fast)"""

    def __init__(self, delays, **kw):
        DryRunRunner.__init__(self, **kw)
        self.delays = dict(delays)
        self.delay_s = 0.0                       # the wait happens here, per call (threads share self)

    def run(self, spec, abort_flag=None):
        end = time.monotonic() + float(self.delays.get(spec.role, self.delays.get("default", 0.3)))
        while time.monotonic() < end:
            if abort_flag is not None and abort_flag.is_set():
                return lanedriver.vprunners.TurnOutcome(lanedriver.STATUS_ABORTED, "STOP", runner=self.name)
            time.sleep(0.05)
        return DryRunRunner.run(self, spec, abort_flag)


def register_candidate(drv, root, sha, actor):
    """register `sha` (reachable through the clone's shared object store) as the
    review candidate, exactly as the integrator does; needs no active attempts."""
    import hashlib
    tree = sh(["git", "-C", str(root.trunk), "rev-parse", "%s^{tree}" % sha])[1].strip()
    art = root.dir / ("integration-record-%s.json" % actor)
    art.write_text(json.dumps({"integration": True, "ladder": actor, "sha": sha}) + "\n")
    packet = root.dir / ("candidate-packet-%s.json" % actor)
    packet.write_text(json.dumps({
        "candidate_sha": sha, "tree_sha": tree,
        "contract_revision": hashlib.sha256((root.dir / "control" / "lane-contracts.json").read_bytes()).hexdigest(),
        "artifact_manifest": {"record": {"path": art.name,
                                         "sha256": hashlib.sha256(art.read_bytes()).hexdigest()}}}))
    rc, reg = drv.control.call("register-candidate", ["--repo", str(root.trunk), "--packet", str(packet),
                                                      "--artifact-root", str(root.dir), "--actor", actor])
    return tree, rc


def instantiate_review(drv, root, task, parent, sha, tree):
    params = root.dir / ("review-%s.json" % task)
    params.write_text(json.dumps({"parent_contract_id": parent, "candidate_sha": sha, "tree_sha": tree,
                                  "diff_or_scope": "whole", "criteria": "catalog", "evidence": []}))
    try:
        rc, inst = drv.control.call("instantiate", ["--template", "JUNIOR_REVIEW", "--task", task,
                                                    "--parent-contract", parent, "--chief", "B",
                                                    "--parameters-json", str(params)])
        return rc, None
    except lanedriver.ControlError as exc:
        return 1, str(exc)[:300]


def sh(argv, cwd=None, timeout=1800):
    cp = subprocess.run([str(a) for a in argv], cwd=cwd, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                        timeout=timeout)
    return cp.returncode, cp.stdout, cp.stderr


def http_get(url, timeout=3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(200).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, str(exc)[:120])


# -- roots ------------------------------------------------------------------------------

class Root(object):
    """one throwaway run root: trunk clone + control + run/roster.json"""

    def __init__(self, base, name, trunk, catalog, control, pack_dir, roster_patch=None, state_src=None):
        self.dir = Path(base) / name
        if self.dir.exists():
            shutil.rmtree(self.dir)
        out = _capture(lambda: lanedryrun.cmd_init(Args(root=str(self.dir), trunk=str(trunk),
                                                        catalog=str(catalog), control=str(control),
                                                        pack_dir=str(pack_dir) if pack_dir else None,
                                                        run_id="ladder-%s" % name, force_roster=True,
                                                        state_src=str(state_src) if state_src else None)))
        self.init_out = json.loads(out) if out.strip().startswith("{") else {"raw": out}
        self.run_root = self.dir / "run"
        self.roster_path = self.run_root / "roster.json"
        self.trunk = self.dir / "trunk"
        self.state = self.dir / "control" / "orchestration-state" / "run-state.json"
        roster = json.loads(self.roster_path.read_text())
        roster.setdefault("alerts", {}).update({"frontier_every_s": 0, "idle_every_min": 1})
        roster.setdefault("concurrency", {})["max_minutes_per_turn"] = {"default": 2}
        if roster_patch:
            for k, v in roster_patch.items():
                if isinstance(v, dict):
                    roster.setdefault(k, {}).update(v)
                else:
                    roster[k] = v
        self.roster_path.write_text(json.dumps(roster, indent=2) + "\n")

    def rows(self):
        return json.loads(self.state.read_text())["tasks"]

    def cleanup(self):
        """drop the clone + worktrees (~10 GB per rung on this catalog: a full
        checkout per task), keep run/ and control/ as the audit record"""
        freed = 0
        for sub in ("vp-worktrees", "trunk"):
            d = self.dir / sub
            if d.exists():
                freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                shutil.rmtree(d, ignore_errors=True)
        return round(freed / 1e9, 1)

    def counts(self):
        c = {}
        for r in self.rows().values():
            c[r["state"]] = c.get(r["state"], 0) + 1
        return dict(sorted(c.items()))

    def control_lines(self):
        p = self.run_root / "control.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def driver(self, verdicts=None, delay_s=0.3, proof_delay_s=0.5, proof_statuses=None, delays=None):
        dry = RoleDelayRunner(delays or {"default": delay_s}, verdicts=verdicts, delay_s=delay_s)
        proof = DryRunProof(self.run_root, statuses=proof_statuses, delay_s=proof_delay_s)
        drv = LaneDriver(self.roster_path, runners={"opencode": dry, "codex": dry, "claude": dry, "agy": dry},
                         proof=proof, interval=0.2)
        drv.fake_runners = True
        proof.log = drv.log
        drv.dry, drv.dryproof = dry, proof
        return drv


def _capture(fn):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


def pump(drv, until=None, max_ticks=200, join_s=0.5, trace=None):
    """tick until `until()` or max_ticks; returns (ticks, satisfied).  `trace`
    collects (tick, active, live) per tick from the heartbeat."""
    for i in range(max_ticks):
        drv.tick()
        drv.join(timeout=join_s)
        hb = drv.write_heartbeat()
        if trace is not None:
            trace.append((drv.tick_count, hb["active"], sorted(hb["live"])))
        if until and until():
            return i + 1, True
    return max_ticks, until is None


def settle(drv, max_ticks=400, join_s=0.5, trace=None):
    """tick until nothing is live and nothing new gets spawned for 3 ticks"""
    quiet = 0
    for i in range(max_ticks):
        spawned = drv.tick()
        drv.join(timeout=join_s)
        hb = drv.write_heartbeat()
        if trace is not None:
            trace.append((drv.tick_count, hb["active"], sorted(hb["live"])))
        quiet = quiet + 1 if (not spawned and hb["active"] == 0) else 0
        if quiet >= 3:
            return i + 1
    return max_ticks


# -- rungs ------------------------------------------------------------------------------

def rung_l0(a):
    rc, head, _ = sh(["git", "-C", str(a.trunk), "rev-parse", "HEAD"])
    rc2, branch, _ = sh(["git", "-C", str(a.trunk), "rev-parse", "--abbrev-ref", "HEAD"])
    du = shutil.disk_usage("/")
    checks = {"python": platform.python_version(), "git": sh(["git", "--version"])[1].strip(),
              "trunk_head": head.strip(), "trunk_branch": branch.strip(),
              "disk_free_gb": round(du.free / 1e9, 1)}
    for name, url in (("opencode_go2", "http://127.0.0.1:4102/health"),
                      ("codex_router", "http://127.0.0.1:4202/health")):
        st, body = http_get(url)
        checks[name] = {"url": url, "status": st, "body": body[:80]}
    return {"status": "RECORDED", "note": "read-only; server health informational (fake runners need none)",
            "checks": checks, "ts": utc_ms()}


def rung_l1(a):
    t0 = time.monotonic()
    rc, out, err = sh([a.pytest_python, "-m", "pytest", str(HERE / "tests"), "-q", "-p", "no:cacheprovider"],
                      cwd=str(HERE.parent.parent))
    tail = (out.strip().splitlines() or [""])[-1]
    rc2, out2, err2 = sh(["python3", "-m", "unittest", "test_orchestration_control", "test_review_gate"],
                         cwd=str(Path(a.control).parent))
    tail2 = [l for l in (err2 or out2).strip().splitlines() if l.startswith("Ran ") or l in ("OK", "FAILED")]
    return {"status": "PASS" if rc == 0 and rc2 == 0 else "FAIL", "dispatcher": tail,
            "scheduler": " ".join(tail2), "seconds": round(time.monotonic() - t0, 1), "ts": utc_ms()}


def rung_l2(a):
    return {"status": "SKIPPED_OPERATOR", "ts": utc_ms(),
            "note": "permission edges are proven in the Operator's session against 08-PERMISSIONS.md; "
                    "the Fixer's session has no claim on those rules"}


def rung_l3_l5(a, roots):
    """L3 thin cycle + L5 concurrency + Q1 on the full catalog, fake runners."""
    root = Root(roots, "l3", a.trunk, a.catalog, a.control, a.pack_dir)
    head0 = sh(["git", "-C", str(root.trunk), "rev-parse", "HEAD"])[1].strip()
    drv = root.driver(delay_s=0.6, proof_delay_s=0.5)
    trace = []
    t0 = utc_ms()
    ticks = settle(drv, trace=trace)
    drv._finish("ladder-l3")
    counts = root.counts()
    rows = root.rows()
    head1 = sh(["git", "-C", str(root.trunk), "rev-parse", "HEAD"])[1].strip()
    max_live = max([t[1] for t in trace] or [0])
    peak = next((t for t in trace if t[1] == max_live), None)
    # Q1: L00 VERIFIED -> L01..L04 READY -> claimed in the driver's next cycle, no human
    lines = root.control_lines()
    done = next((l for l in lines if l["verb"] == "complete" and "L00" in l["argv"] and l["rc"] == 0), None)
    claims = [l for l in lines if l["verb"] == "claim" and l["rc"] == 0
              and any(t in l["argv"] for t in ("L01", "L02", "L03", "L04"))]
    q1 = {"status": "FAIL", "ts": utc_ms()}
    if done and claims:
        first = min(claims, key=lambda l: l["ts"])
        q1 = {"status": "PASS", "l00_complete_ts": done["ts"], "l00_sequence_after": done["seq_after"],
              "first_dependent_claim": {"task": [x for x in first["argv"] if x.startswith("L0")][0],
                                        "ts": first["ts"], "sequence_before": first["seq_before"]},
              "dependents_claimed": sorted({[x for x in l["argv"] if x.startswith("L0")][0] for l in claims}),
              "human_messages": 0, "ts": utc_ms()}
    l3 = {"status": "PASS" if counts.get("VERIFIED", 0) >= 20 and head0 == head1 else "FAIL",
          "run_root": str(root.run_root), "ticks": ticks, "started": t0, "finished": utc_ms(),
          "states": counts, "trunk_head_before": head0, "trunk_head_after": head1,
          "model_calls": 0, "turns": len(drv.dry.calls), "proofs": len(drv.dryproof.calls),
          "waiting_on_dynamic": sorted(t for t, r in rows.items() if r["state"] == "WAITING_DEPENDENCY"
                                       and any(d.startswith("D") for d in r.get("waiting_for", []))),
          "gate_holds": sorted(p.parent.parent.name for p in root.run_root.glob("turns/*/*/gate-hold.json")),
          "gate_hold_note": "an attempt the scheduler holds for its review packet (requires --verdict) is "
                            "parked, not re-run; the review packets are the Architect's REVIEW-* tasks",
          "files": sorted(p.name for p in root.run_root.iterdir())}
    l3["freed_gb"] = root.cleanup()
    l5 = {"status": "PASS" if max_live >= 4 else "FAIL", "max_live": max_live, "peak_tick": peak,
          "server_max": drv.servers["go2"]["max_concurrent"],
          "proof_max_active": drv.dryproof.max_active, "ts": utc_ms()}
    return l3, l5, q1


def rung_l4(a):
    names = ["test_grader_fail_twice_is_repair_required_then_repair_is_instantiated_and_promotes",
             "test_quota_parks_the_role_and_never_fails_the_task",
             "test_three_runner_failures_complete_invalid_evidence_with_backoff",
             "test_stop_aborts_children_and_restart_adopts_the_attempt",
             "test_draining_phase_refuses_new_claims_but_harvests_running",
             "test_f9_restart_aborts_when_turns_do_not_finish",
             "test_f8_item_verbs_refuse_while_the_attempt_is_live",
             "test_f3_token_cap_parks_that_runner_only",
             "test_proof_pass_verifies_and_fail_product_is_repair_required",
             "test_proof_unknown_keeps_the_head_and_retries_proof_only",
             "test_untouched_red_reruns_on_the_box_green_is_pass_with_trunk_finding",
             "test_circleci_failure_is_unknown_never_a_crash",
             "test_item9_flip_off_mid_poll_cancels_the_pipeline",
             "test_item8_activation_record_hashes_five_profiles_and_flags_an_edit_on_restart"]
    rc, out, err = sh([a.pytest_python, "-m", "pytest", str(HERE / "tests"), "-q", "-p", "no:cacheprovider",
                       "-k", " or ".join(names)], cwd=str(HERE.parent.parent))
    tail = (out.strip().splitlines() or [""])[-1]
    return {"status": "PASS" if rc == 0 and "%d passed" % len(names) in tail else "FAIL",
            "injected": names, "result": tail, "ts": utc_ms()}


def rung_q2(a, roots):
    """independent review + CI preparation concurrent: the candidate is registered
    while nothing is active (the scheduler's rule), its JUNIOR_REVIEW is
    instantiated, and the next cycle dispatches the review (slow) next to the
    builders whose proofs (CI prep) run at the same time."""
    root = Root(roots, "q2", a.trunk, a.catalog, a.control, a.pack_dir,
                roster_patch={"proof": {"require_for_kinds": ["builder", "integration"]}})
    drv = root.driver(delays={"default": 0.3, "reviewer": 8.0}, proof_delay_s=5.0)
    pump(drv, until=lambda: root.rows()["L00"]["state"] == "VERIFIED" and drv.write_heartbeat()["active"] == 0,
         max_ticks=60)
    sha = root.rows()["L00"]["output_sha"]
    tree, rc = register_candidate(drv, root, sha, "q2")
    rc2, err = instantiate_review(drv, root, "J-L00", "L00", sha, tree)
    registered_ts = utc_ms()
    both, trace = None, []
    for _ in range(80):
        drv.tick()
        time.sleep(0.4)
        hb = drv.write_heartbeat()
        trace.append((drv.tick_count, hb["active"], sorted(hb["live"]), drv.dryproof.active))
        if "J-L00" in hb["live"] and drv.dryproof.active > 0:
            both = {"tick": drv.tick_count, "ts": hb["ts"], "live": sorted(hb["live"]),
                    "proofs_running": drv.dryproof.active,
                    "proof_tasks": [c["task"] for c in drv.dryproof.calls]}
            break
    settle(drv, max_ticks=120)
    drv._finish("ladder-q2")
    rows = root.rows()
    return {"status": "PASS" if both and rc == 0 and rc2 == 0 else "FAIL",
            "candidate_sha": sha, "registered_ts": registered_ts, "register_rc": rc, "instantiate_rc": rc2,
            "instantiate_error": err, "concurrent": both, "trace_head": trace[:6],
            "review_final": rows.get("J-L00", {}).get("state"),
            "review_note": "the fake reviewer's APPROVE is refused by review_gate (no native codex rollout "
                           "in a dry run) -> INVALID_EVIDENCE, honestly; the concurrency is the point measured",
            "run_root": str(root.run_root), "freed_gb": root.cleanup(), "ts": utc_ms()}


def rung_q3(a, roots):
    """HOLD -> one owned repair -> the repair's successor gets the review.
    (L02: a catalog task no packet owns -- pack-owned rows are repaired by
    their packets, never by the frontier.)"""
    root = Root(roots, "q3", a.trunk, a.catalog, a.control, a.pack_dir)
    drv = root.driver(verdicts={"L02": "FAIL"}, delay_s=0.3)
    pump(drv, until=lambda: root.rows()["L02"]["state"] == "REPAIR_REQUIRED", max_ticks=120)
    hold_ts = utc_ms()
    rows = root.rows()
    if rows["L02"]["state"] != "REPAIR_REQUIRED":
        return {"status": "FAIL", "reason": "L02 never held", "states": root.counts(), "ts": utc_ms()}
    # let the frontier instantiate exactly one repair and run it green
    drv.dry.verdicts.pop("L02", None)
    settle(drv, max_ticks=600)                   # the frontier runs once the catalog is idle
    pump(drv, until=lambda: any(t.startswith("R-L02") and r["state"] == "VERIFIED"
                                for t, r in root.rows().items()), max_ticks=200)
    repairs = sorted(t for t in root.rows() if t.startswith("R-L02"))
    settle(drv, max_ticks=100)                   # idle again: the frontier promotes the parent
    rows = root.rows()
    if not (len(repairs) == 1 and rows[repairs[0]]["state"] == "VERIFIED"):
        drv._finish("ladder-q3")
        return {"status": "FAIL", "repairs": repairs, "states": root.counts(), "freed_gb": root.cleanup(),
                "ts": utc_ms()}
    # the successor: the repair's candidate is registered (under a drain: the
    # scheduler refuses a candidate change with attempts active) and its
    # JUNIOR_REVIEW instantiated; the driver dispatches the review on undrain
    rep = rows[repairs[0]]
    sha = rep["output_sha"]
    drv.write_drain("ladder q3: register the repair candidate", 0)
    settle(drv, max_ticks=60)
    tree, rc0 = register_candidate(drv, root, sha, "q3")
    review_task = "J-%s" % repairs[0]
    rc, err = instantiate_review(drv, root, review_task, repairs[0], sha, tree)
    drv.clear_drain("ladder q3")
    review_dispatched = None
    if rc == 0:
        pump(drv, until=lambda: any(l["verb"] == "start" and review_task in l["argv"] and l["rc"] == 0
                                    for l in root.control_lines()), max_ticks=60)
        review_dispatched = next((l["ts"] for l in root.control_lines()
                                  if l["verb"] == "start" and review_task in l["argv"] and l["rc"] == 0), None)
        settle(drv, max_ticks=60)
    drv._finish("ladder-q3")
    rows = root.rows()
    return {"status": "PASS" if review_dispatched else "FAIL", "hold_ts": hold_ts,
            "repairs": repairs, "repair_state": rows[repairs[0]]["state"], "repair_sha": sha,
            "parent_state": rows["L02"]["state"],
            "review_task": review_task, "review_dispatched_ts": review_dispatched,
            "review_final": rows.get(review_task, {}).get("state"), "register_rc": rc0,
            "instantiate_rc": rc, "instantiate_error": err,
            "review_note": "fake reviewer verdict is refused by review_gate in a dry run (INVALID_EVIDENCE)",
            "run_root": str(root.run_root), "freed_gb": root.cleanup(), "ts": utc_ms()}


def rung_q4(a, roots):
    """a driver stops mid-turn; a fresh driver on the same root adopts the
    orphan and dispatch continues with no owner message."""
    root = Root(roots, "q4", a.trunk, a.catalog, a.control, a.pack_dir,
                roster_patch={"run": {"stop_grace_s": 1}})
    drv = root.driver(delay_s=6.0)
    pump(drv, until=lambda: root.rows()["L00"]["state"] == "VERIFIED", max_ticks=60, join_s=1.0)
    drv.tick()                                   # dispatch L01..L04 (6 s turns)
    time.sleep(0.5)
    live_before = sorted(drv.write_heartbeat()["live"])
    (root.run_root / "STOP").write_text("ladder q4\n")
    for _ in range(40):
        drv.tick()                               # grace 1 s, then the abort flag
        if not drv.write_heartbeat()["active"]:
            break
        time.sleep(0.5)
    drv.join(timeout=15)
    drv._finish("STOP")
    stop_ts = utc_ms()
    orphans = sorted(t for t, r in root.rows().items() if r["state"] in ("CLAIMED", "RUNNING"))
    (root.run_root / "STOP").unlink()
    drv2 = root.driver(delay_s=0.3)
    ticks = settle(drv2, max_ticks=200)
    drv2._finish("ladder-q4")
    adopted = [l.split()[2] for l in (root.run_root / "driver.log").read_text().splitlines()
               if " ADOPT " in l and l.index(" ADOPT ") < 40]
    after = root.counts()
    progressed = [t for t in orphans if root.rows()[t]["state"] == "VERIFIED"]
    return {"status": "PASS" if orphans and adopted and len(progressed) == len(orphans) else "FAIL",
            "live_at_stop": live_before, "stop_ts": stop_ts, "orphans_left_running": orphans,
            "adopted": sorted(set(adopted)),
            "restart_ticks": ticks, "orphans_verified_after_restart": progressed, "states_after": after,
            "human_messages": 0, "run_root": str(root.run_root), "freed_gb": root.cleanup(), "ts": utc_ms()}


def rung_q5(a, roots):
    """DRAIN: no new starts; the running turn finishes and its outputs stay."""
    root = Root(roots, "q5", a.trunk, a.catalog, a.control, a.pack_dir)
    drv = root.driver(delay_s=2.0)
    pump(drv, until=lambda: root.rows()["L00"]["state"] == "VERIFIED", max_ticks=60, join_s=1.0)
    drv.tick()                                   # L01..L04 start (2 s turns)
    time.sleep(0.3)
    live = sorted(drv.write_heartbeat()["live"])
    drv.write_drain("ladder q5", 0)
    drain_ts = utc_ms()
    claims_at_drain = len([l for l in root.control_lines() if l["verb"] == "claim"])
    settle(drv, max_ticks=60, join_s=1.0)
    claims_after = len([l for l in root.control_lines() if l["verb"] == "claim"])
    rows = root.rows()
    harvested = [t for t in live if any((root.run_root / "turns" / t).glob("*/harvest.json"))]
    ready_left = sorted(t for t, r in rows.items() if r["state"] == "READY")
    drv.clear_drain("ladder q5")
    drv._finish("ladder-q5")
    return {"status": "PASS" if live and claims_after == claims_at_drain and len(harvested) == len(live)
            and all(rows[t]["state"] == "VERIFIED" for t in live) else "FAIL",
            "live_at_drain": live, "drain_ts": drain_ts, "claims_before": claims_at_drain,
            "claims_after": claims_after, "finished_during_drain": [rows[t]["state"] for t in live],
            "harvests_on_disk": harvested, "ready_refused_while_drained": ready_left,
            "run_root": str(root.run_root), "freed_gb": root.cleanup(), "ts": utc_ms()}


def rung_p1(a, roots):
    """pack §4(e): the real 64 packets against a COPY of the live run-state
    (140 rows, seq 482, registered candidate s3), fake runners, no model."""
    if not a.state_src:
        return {"status": "NOT_RUN", "reason": "--state-src not given", "ts": utc_ms()}
    root = Root(roots, "p1", a.trunk, a.catalog, a.control, a.pack_dir, state_src=a.state_src,
                roster_patch={"alerts": {"pack_every_s": 0}})
    before = root.counts()
    head0 = sh(["git", "-C", str(root.trunk), "rev-parse", "HEAD"])[1].strip()
    drv = root.driver(delay_s=0.2, proof_delay_s=0.2)
    assert drv.pack, "no packets loaded from %s" % a.pack_dir
    trace = []
    t0 = utc_ms()
    ticks = settle(drv, max_ticks=600, trace=trace)
    drv._finish("ladder-p1")
    head1 = sh(["git", "-C", str(root.trunk), "rev-parse", "HEAD"])[1].strip()
    rows = root.rows()
    pdir = root.run_root / "packets"
    bindings = [json.loads(l) for l in (pdir / "bindings.jsonl").read_text().splitlines()] \
        if (pdir / "bindings.jsonl").exists() else []
    closures = [json.loads(l) for l in (pdir / "closures.jsonl").read_text().splitlines()] \
        if (pdir / "closures.jsonl").exists() else []
    alerts = []
    try:
        alerts = [json.loads(l) for l in (root.run_root / "alerts.jsonl").read_text().splitlines()]
    except OSError:
        pass
    kinds = {}
    for al in alerts:
        kinds[al["kind"]] = kinds.get(al["kind"], 0) + 1
    bound = {b["packet"]: b for b in bindings if "mode" in b}
    per_packet = {}
    for pid, p in sorted(drv.pack.items()):
        t = next((t for t, q in drv.pack_by_task.items() if q == pid), None)
        per_packet[pid] = {"task": t, "mode": bound.get(pid, {}).get("mode"),
                           "state": rows.get(t, {}).get("state") if t else None,
                           "parent": bound.get(pid, {}).get("parent_contract_id"),
                           "parent_how": bound.get(pid, {}).get("parent_how"),
                           "owner_gate": p["owner_gate"] if p["owner_gate"] != "none" else None}
    unbound = sorted(pid for pid, v in per_packet.items() if not v["task"])
    retired = sorted(set(sum((c.get("retired") or [] for c in closures), [])))
    pending = {c["packet"]: c["pending"] for c in closures if c.get("pending")}
    holds = sorted(p.parent.parent.name for p in root.run_root.glob("turns/*/*/gate-hold.json"))
    ok = (head0 == head1 and not any(k in kinds for k in ("STUCK", "CONTROL_DOWN", "CLOSURE_FAILED"))
          and len(bound) >= 40 and retired)
    return {"status": "PASS" if ok else "FAIL", "run_root": str(root.run_root), "ticks": ticks,
            "started": t0, "finished": utc_ms(), "states_before": before, "states_after": root.counts(),
            "packets": len(drv.pack), "pack_lint": drv.pack_lint, "bound": len(bound),
            "unbound": unbound, "per_packet": per_packet, "retired": retired, "closes_pending": pending,
            "gate_holds": holds, "alerts": kinds, "turns": len(drv.dry.calls), "proofs": len(drv.dryproof.calls),
            "trunk_head_before": head0, "trunk_head_after": head1, "model_calls": 0,
            "candidate_sha": (json.loads(root.state.read_text()).get("candidate") or {}).get("sha"),
            "l42": {"depends_on": rows.get("L42", {}).get("depends_on"), "state": rows.get("L42", {}).get("state"),
                    "waiting_for": rows.get("L42", {}).get("waiting_for")},
            "freed_gb": root.cleanup(), "ts": utc_ms()}


def rung_l6(a):
    return {"status": "SKIPPED_FAKE_RUNNERS", "ts": utc_ms(),
            "note": "a real item needs a model turn; the work order forbids live turns on product lanes "
                    "for this rung -- L6 is the Operator's first real packet on the live run"}


def rung_l7(a, roots):
    """unattended: a subprocess `run --loop --fake-runners` for --l7-seconds,
    then the STOP drill on the same pid path."""
    root = Root(roots, "l7", a.trunk, a.catalog, a.control, a.pack_dir)
    py = sys.executable
    argv = [py, str(HERE / "lanedriver.py"), "--roster", str(root.roster_path), "run", "--loop",
            "--interval", "1", "--fake-runners"]
    start_ts = utc_ms()
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            cwd=str(HERE))
    t_end = time.monotonic() + a.l7_seconds
    hb_ticks = []
    while time.monotonic() < t_end and proc.poll() is None:
        time.sleep(2)
        try:
            hb = json.loads((root.run_root / "driver.heartbeat").read_text())
            hb_ticks.append(hb["tick"])
        except (OSError, ValueError):
            pass
    alive = proc.poll() is None
    (root.run_root / "STOP").write_text("ladder l7\n")
    stop_ts = utc_ms()
    try:
        _out, err = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        err = b"timeout waiting for STOP exit"
    exit_ts = utc_ms()
    handoff = {}
    try:
        handoff = json.loads((root.run_root / "STOP-HANDOFF.json").read_text())
    except (OSError, ValueError):
        pass
    alerts = []
    try:
        alerts = [json.loads(l)["kind"] for l in (root.run_root / "alerts.jsonl").read_text().splitlines()]
    except (OSError, ValueError):
        pass
    files = sorted(p.name for p in root.run_root.iterdir())
    need = ["LEDGER.md", "activation-record.json", "comms.jsonl", "control.jsonl", "git.jsonl",
            "driver.heartbeat", "driver.log", "costs.jsonl", "probes", "turns"]
    manifests = [f for f in files if f.startswith("MANIFEST-")]
    bad = [k for k in alerts if k in ("STUCK", "CONTROL_DOWN", "RECONCILE_FAILED", "PROOF_UNKNOWN")]
    ok = alive and proc.returncode == 0 and handoff.get("reason") == "STOP" and hb_ticks and \
        max(hb_ticks) >= a.l7_seconds // 2 and all(n in files for n in need) and manifests and not bad
    return {"status": "PASS" if ok else "FAIL", "pid": proc.pid, "started": start_ts, "stop_written": stop_ts,
            "exited": exit_ts, "exit_code": proc.returncode, "alive_until_stop": alive,
            "heartbeat_ticks": max(hb_ticks) if hb_ticks else 0, "seconds": a.l7_seconds,
            "handoff_reason": handoff.get("reason"), "states": root.counts(), "alerts": sorted(set(alerts)),
            "unwanted_alerts": bad, "manifests": manifests, "files": files,
            "stderr_tail": (err or b"").decode("utf-8", "replace")[-400:], "run_root": str(root.run_root),
            "freed_gb": root.cleanup(), "ts": utc_ms()}


# -- main -------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--trunk", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--control", required=True)
    ap.add_argument("--pack-dir")
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--pytest-python", default=sys.executable)
    ap.add_argument("--l7-seconds", type=int, default=90)
    ap.add_argument("--only", help="comma list of rungs to run (default all)")
    ap.add_argument("--state-src", help="live orchestration-state dir for rung P1 (copied, never written)")
    a = ap.parse_args(argv)
    a.trunk, a.catalog, a.control = Path(a.trunk).resolve(), Path(a.catalog).resolve(), Path(a.control).resolve()
    a.pack_dir = Path(a.pack_dir).resolve() if a.pack_dir else None
    roots = Path(a.root).resolve()
    roots.mkdir(parents=True, exist_ok=True)
    only = set(a.only.split(",")) if a.only else None
    want = lambda k: only is None or k in only  # noqa: E731
    receipt = {"receipt": "DRY-RUN-RECEIPT-v13", "started": utc_ms(), "fake_runners": True, "model_calls": 0,
               "source_trunk": str(a.trunk), "source_trunk_head":
               sh(["git", "-C", str(a.trunk), "rev-parse", "HEAD"])[1].strip(),
               "catalog": str(a.catalog), "catalog_sha256": lanedriver.sha256_file(a.catalog),
               "scheduler_sha256": lanedriver.sha256_file(a.control), "dispatcher_commit":
               sh(["git", "-C", str(HERE.parent), "rev-parse", "HEAD"])[1].strip(),
               "ladder_root": str(roots), "rungs": {}, "qualification": {}}
    r, q = receipt["rungs"], receipt["qualification"]

    def run(key, fn, into):
        if not want(key):
            into[key] = {"status": "NOT_RUN"}
            return
        t0 = time.monotonic()
        try:
            into[key] = fn()
        except Exception as exc:  # noqa: BLE001 -- the receipt records the crash, never hides it
            import traceback
            into[key] = {"status": "CRASH", "error": "%s: %s" % (type(exc).__name__, exc),
                         "trace": traceback.format_exc()[-1500:], "ts": utc_ms()}
        into[key]["seconds"] = round(time.monotonic() - t0, 1)
        print("%s %s" % (key, into[key]["status"]), flush=True)

    run("L0", lambda: rung_l0(a), r)
    run("L1", lambda: rung_l1(a), r)
    run("L2", lambda: rung_l2(a), r)
    if want("L3"):
        try:
            l3, l5, q1 = rung_l3_l5(a, roots)
        except Exception as exc:  # noqa: BLE001
            import traceback
            l3 = l5 = q1 = {"status": "CRASH", "error": "%s: %s" % (type(exc).__name__, exc),
                            "trace": traceback.format_exc()[-1500:]}
        r["L3"], r["L5"], q["Q1"] = l3, l5, q1
        for k in ("L3", "L5"):
            print("%s %s" % (k, r[k]["status"]), flush=True)
        print("Q1 %s" % q1["status"], flush=True)
    run("L4", lambda: rung_l4(a), r)
    run("P1", lambda: rung_p1(a, roots), r)
    run("L6", lambda: rung_l6(a), r)
    run("Q2", lambda: rung_q2(a, roots), q)
    run("Q3", lambda: rung_q3(a, roots), q)
    run("Q4", lambda: rung_q4(a, roots), q)
    run("Q5", lambda: rung_q5(a, roots), q)
    run("L7", lambda: rung_l7(a, roots), r)
    statuses = [v["status"] for v in list(r.values()) + list(q.values())]
    receipt["finished"] = utc_ms()
    receipt["verdict"] = "PASS" if all(s in ("PASS", "RECORDED") or s.startswith("SKIPPED") or s == "NOT_RUN"
                                       for s in statuses) else "FAIL"
    receipt["skipped"] = {k: v["status"] for k, v in list(r.items()) + list(q.items())
                          if v["status"].startswith("SKIPPED")}
    out = Path(a.receipt)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print("verdict %s -> %s" % (receipt["verdict"], out))
    return 0 if receipt["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
