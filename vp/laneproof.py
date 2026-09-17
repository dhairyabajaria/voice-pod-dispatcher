#!/usr/bin/env python3
"""laneproof.py -- the v13 proof step for lanedriver (box via vpproof,
hosted via vpcircle) with the F6 rule:

  a CircleCI red confined to files the candidate diff (base..cand) does not
  touch is a CI-runner flake suspect -> exactly those nodes are re-run on the
  box, serially; all green = the proof is PASS and a TRUNK finding is filed
  (trunk-findings.jsonl + TRUNK_FLAKE_SUSPECT alert); any red = FAIL_PRODUCT
  as before.  The candidate is never failed on a red it could not have caused
  before the box has had its say.

Routing (roster `proof`): circleci.enabled false -> box; mode `all` -> every
listed kind off-box; mode `overflow` (default) -> box while a box slot is
free, else CircleCI; `max_pipelines_per_day` null = no daily cap (O5);
`max_in_flight` (2) bounds concurrent pipelines.  stdlib only.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vpcircle   # noqa: E402
from vpdriver import circle_failed_nodes  # noqa: E402

RERUN_KINDS = {"platform": "platform/", "deploy": "deploy/", "agent": "agent/"}
DEFAULT_CIRCLE = {"enabled": False, "mode": "overflow", "kinds": ["full"], "param": "run_full_suite",
                  "branch_prefix": "vp/proof/", "account": "3", "poll_interval_s": 60,
                  "deadline_min": 90, "delete_branch_after": True, "flake_rerun_max": 10,
                  "max_pipelines_per_day": None, "max_in_flight": 2,
                  # item 9: the owner granted standing CircleCI use, so the flip-off
                  # watcher (RUN_ROOT/CIRCLECI-OFF routes to the box and cancels the
                  # pipelines in flight) is optional and off by default
                  "flip_off_watcher": False}
OFF_FILE = "CIRCLECI-OFF"


def utc_ms():
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


class Proof(object):

    def __init__(self, run_root, here, cn, git, exec_, log, alert, proof_cfg, circle=None,
                 circle_runner=None, python=None):
        self.run_root = Path(run_root)
        self.here = Path(here)
        self.cn = Path(cn)
        self.git = git                      # callable(args, cwd=None) -> (rc, out, err)
        self.exec = exec_
        self.log = log
        self.alert = alert
        self.cfg = dict(proof_cfg or {})
        self.circle = circle or vpcircle
        self.circle_runner = circle_runner
        self.python = python or sys.executable
        self._lock = threading.Lock()
        self.box_active = 0
        self.circle_active = 0

    # -- config ----------------------------------------------------------------------------

    def circle_cfg(self):
        cc = dict(DEFAULT_CIRCLE)
        raw = dict(self.cfg.get("circleci") or {})
        # roster-v13 spells the in-flight bound `max_pipelines_in_flight`
        if "max_pipelines_in_flight" in raw and "max_in_flight" not in raw:
            raw["max_in_flight"] = raw.pop("max_pipelines_in_flight")
        cc.update(raw)
        return cc

    def circle_off(self):
        """flip-off watcher: only when enabled, and only by the OFF file"""
        if not self.circle_cfg().get("flip_off_watcher"):
            return False
        return (self.run_root / OFF_FILE).exists()

    def route(self, kind, suite=None):
        """-> ("box"|"circleci", why).  `circleci.kinds` may name the suite
        ("full" / "targeted", 06-ROUTING §5: every full suite off-box) or a
        proof_kind ("platform", ...); either match makes CircleCI eligible."""
        cc = self.circle_cfg()
        listed = set(cc.get("kinds") or [])
        if not cc.get("enabled") or not ({kind, suite} & listed):
            return "box", "circleci disabled or kind %s/%s not listed" % (kind, suite)
        if self.circle_off():
            return "box", "circleci flipped off (%s)" % OFF_FILE
        cap = cc.get("max_pipelines_per_day")
        if cap is not None and int(cap) > 0 and self.pipelines_today() >= int(cap):
            self.alert("PIPELINE_CAP", "%d CircleCI pipelines today >= cap %d; proofs fall back "
                       "to the box" % (self.pipelines_today(), int(cap)))
            return "box", "daily cap"
        with self._lock:
            if self.circle_active >= int(cc.get("max_in_flight", 2)):
                return "box", "circleci in flight %d/%d" % (self.circle_active, cc.get("max_in_flight", 2))
            mode = str(cc.get("mode", "overflow"))
            if mode in ("all", "swap"):
                return "circleci", "mode all"
            if suite == "full" and "full" in listed:
                return "circleci", "full suite is never run on the box (06-ROUTING §5)"
            slots = int(self.cfg.get("box_slots", 1))
            if self.box_active < slots:
                return "box", "overflow: box free (%d/%d)" % (self.box_active, slots)
            return "circleci", "overflow: box busy (%d/%d)" % (self.box_active, slots)

    def pipelines_today(self):
        ledger = self.run_root / "proofs" / "circleci-pipelines.jsonl"
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        n = 0
        try:
            for line in ledger.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("ts", "")[:10] == today:
                        n += 1
                except ValueError:
                    pass
        except OSError:
            pass
        return n

    def _note_pipeline(self, pid, pipeline_id, account, cand):
        ledger = self.run_root / "proofs" / "circleci-pipelines.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": utc_ms(), "proof_id": pid, "pipeline_id": pipeline_id,
                                 "account": account, "sha": cand}) + "\n")

    # -- entry -----------------------------------------------------------------------------

    NO_RUN_KINDS = ("docs",)

    def run(self, task, pid, wt, base, cand, kind, paths, abort=None):
        if kind in self.NO_RUN_KINDS:
            # 06-ROUTING §5: docs-only proofs pass without a run
            rec = {"status": "PASS", "route": "none", "proof_id": pid, "sha": cand, "kind": kind,
                   "paths": paths, "failed_nodes": [], "errors": {}, "reason": "%s-only: no suite to run" % kind,
                   "ts": utc_ms()}
            self._write(pid, rec)
            self.log("PROOF %s %s -> PASS (%s-only, no run)" % (task, pid, kind))
            return rec
        suite = "targeted" if paths else "full"
        route, why = self.route(kind, suite)
        self.log("PROOF %s %s route=%s (%s, %s %s)" % (task, pid, route, why, suite, kind))
        if route == "circleci":
            return self.run_circleci(task, pid, wt, base, cand, kind, paths, abort=abort)
        return self.run_box(task, pid, wt, cand, kind, paths)

    # -- box -------------------------------------------------------------------------------

    def run_box(self, task, pid, wt, cand, kind, paths, no_record=True, workers=None):
        # v13 keeps its proof records in RUN_ROOT/proofs/<pid>.json (self._write);
        # vpproof's own store row needs a prior proof_request the lane driver
        # never makes ("unknown proof <pid>" -> UNKNOWN on the very first live
        # proof), so the store row is off unless the roster asks for it.
        no_record = no_record and not bool(self.cfg.get("store_record"))
        argv = [self.python, str(self.here / "vpproof.py"), "run", "--run-root", str(self.run_root),
                "--worktree", str(wt), "--sha", cand, "--proof-id", pid,
                "--kind", "targeted" if paths else "full",
                "--proof-kind", kind if kind in ("platform", "portal", "agent", "deploy") else "platform",
                "--cn", str(self.cn)]
        if paths:
            argv += ["--paths", ",".join(paths)]
        if workers:
            argv += ["--workers", str(workers)]
        if no_record:
            argv.append("--no-record")
        with self._lock:
            self.box_active += 1
        try:
            rc, out, err = self.exec.run(argv, cwd=str(self.here),
                                         timeout_s=float(self.cfg.get("targeted_timeout_min", 40)) * 60)
        finally:
            with self._lock:
                self.box_active = max(0, self.box_active - 1)
        try:
            res = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except ValueError:
            res = {}
        status = res.get("status") or "UNKNOWN"
        if rc == 124:
            status = "UNKNOWN"
        counts = res.get("counts") or {}
        rec = {"status": status, "route": "box", "proof_id": pid, "sha": cand, "kind": kind,
               "paths": paths, "failed_nodes": counts.get("failed_nodes") or [],
               "errors": counts.get("errors") or {}, "rc": rc, "stderr": (err or "")[-500:],
               "counts": counts, "log": res.get("log"), "ts": utc_ms()}
        self._write(pid, rec)
        self.log("PROOF %s %s -> %s (box, %d reds)" % (task, pid, status, len(rec["failed_nodes"])))
        return rec

    # -- circleci --------------------------------------------------------------------------

    def run_circleci(self, task, pid, wt, base, cand, kind, paths, abort=None):
        cc = self.circle_cfg()
        runner = self.circle_runner or self.circle.Runner()
        branch = "%s%s-%s" % (cc.get("branch_prefix", "vp/proof/"), pid, cand[:12])
        param = cc.get("param") or "run_full_suite"
        pipeline_id, account = None, None
        with self._lock:
            self.circle_active += 1
        try:
            try:
                ok, why = self.circle.project_visible(runner, cc.get("account"))
                if not ok:
                    raise RuntimeError("preflight: %s" % why)
                rc, out, err = self.git(["-C", str(wt), "branch", "-f", branch, cand])
                if rc != 0:
                    raise RuntimeError("git branch -f %s failed: %s" % (branch, (err or out)[:200]))
                self.circle.push_branch(wt, branch, runner)
                trig = self.circle.trigger(branch, {param: True}, runner, cc.get("account"))
                pipeline_id, account = trig["pipeline_id"], trig["account"]
                self._note_pipeline(pid, pipeline_id, account, cand)
                self.log("PROOF %s %s circleci pipeline %s (account %s)" % (task, pid, pipeline_id, account))
                res = self.circle.poll(pipeline_id, interval=int(cc.get("poll_interval_s", 60)),
                                       deadline_s=int(cc.get("deadline_min", 90)) * 60,
                                       runner=runner, account=account,
                                       abort=lambda: bool((abort and abort()) or self.circle_off()))
            except self.circle.Cancelled:
                done = self.circle.cancel_pipeline(pipeline_id, runner, account)
                rec = {"status": "CANCELLED", "route": "circleci", "proof_id": pid, "sha": cand,
                       "pipeline_id": pipeline_id, "cancelled_workflows": done, "ts": utc_ms()}
                self._write(pid, rec)
                return rec
            except Exception as exc:
                rec = {"status": "UNKNOWN", "route": "circleci", "proof_id": pid, "sha": cand,
                       "reason": "circleci: %s: %s" % (type(exc).__name__, str(exc)[:300]),
                       "pipeline_id": pipeline_id, "account": account, "branch": branch,
                       "failed_nodes": [], "ts": utc_ms()}
                self._write(pid, rec)
                self.log("PROOF %s %s -> UNKNOWN (circleci: %s)" % (task, pid, str(exc)[:200]))
                return rec
            cls = self.circle.classify(res["jobs"], res["failed_tests"])
            status = cls["status"]
            failed, errors = circle_failed_nodes(res["failed_tests"])
            flake = None
            if status == "FAIL_PRODUCT" and failed:
                nodes = self.untouched_red_nodes(wt, base, cand, failed, cc)
                if nodes:
                    self.log("PROOF %s %s circleci %d reds in untouched files -> box re-run"
                             % (task, pid, len(nodes)))
                    st2, still = self.box_rerun(pid, cand, wt, nodes)
                    flake = {"nodes": nodes, "rerun": st2, "still_red": still}
                    if st2 == "PASS":
                        status, failed, errors = "PASS", [], {}
                        self.file_trunk_finding(task, pid, cand, pipeline_id, nodes)
                    else:
                        self.log("PROOF %s %s box re-run %s: %s" % (task, pid, st2, ", ".join(still)[:200]))
            try:
                out_dir = self.circle.record(self.run_root, cand,
                                             {"pipeline_id": pipeline_id, "account": account,
                                              "branch": branch, "proof_id": pid,
                                              "workflows": res.get("workflows")},
                                             res["jobs"], res["failed_tests"], cls)
            except Exception as exc:
                out_dir = "record failed: %s" % exc
            rec = {"status": status, "route": "circleci", "proof_id": pid, "sha": cand, "kind": kind,
                   "paths": paths, "reds": cls["reds"], "failed_nodes": failed, "errors": errors,
                   "flake_suspect": flake, "pipeline_id": pipeline_id, "account": account,
                   "branch": branch, "record_dir": str(out_dir), "ts": utc_ms(),
                   "jobs": [{"name": j.get("name"), "status": j.get("status"),
                             "job_number": j.get("job_number")} for j in res["jobs"]]}
            self._write(pid, rec)
            self.log("PROOF %s %s -> %s (circleci pipeline %s, %d reds)"
                     % (task, pid, status, pipeline_id, len(cls["reds"])))
            return rec
        finally:
            with self._lock:
                self.circle_active = max(0, self.circle_active - 1)
            if cc.get("delete_branch_after", True):
                try:
                    runner.git(["push", "origin", "--delete", branch], cwd=wt)
                except Exception as exc:
                    self.log("circleci branch cleanup %s: %s" % (branch, exc))
                self.git(["-C", str(wt), "branch", "-D", branch])

    # -- F6 ---------------------------------------------------------------------------------

    def untouched_red_nodes(self, wt, base, cand, failed, cc):
        """The failed node ids, repo-relative, when EVERY one lives in a file the
        diff base..cand does not touch, is a pytest kind, and there are at most
        flake_rerun_max of them; else None."""
        cap = int(cc.get("flake_rerun_max", 10))
        if not failed or len(failed) > cap:
            return None
        rc, out, _ = self.git(["-C", str(wt), "diff", "--name-only", "%s..%s" % (base, cand)])
        if rc != 0:
            return None
        touched = set(l.strip() for l in out.splitlines() if l.strip())
        nodes = []
        for node in failed:
            f = node.split("::", 1)[0]
            rest = node[len(f):]
            full = next((c for c in (f, "platform/" + f) if (Path(wt) / c).exists()), None)
            if full is None or full in touched:
                return None
            if not any(full.startswith(p) for p in RERUN_KINDS.values()):
                return None
            nodes.append(full + rest)
        return sorted(set(nodes))

    def box_rerun(self, pid, cand, wt, nodes):
        """Serial box re-run of exactly `nodes`, per pytest kind, no record.
        -> (status, still_red_nodes)"""
        by_kind = {}
        for n in nodes:
            k = next(k for k, p in RERUN_KINDS.items() if n.startswith(p))
            by_kind.setdefault(k, []).append(n)
        status, still = "PASS", []
        for k, ns in by_kind.items():
            rec = self.run_box("rerun", "%s-rerun-%s" % (pid, k), wt, cand, k, ns, no_record=True,
                               workers=1)
            st = rec.get("status") or "UNKNOWN"
            if st != "PASS":
                status = st if st in ("FAIL_PRODUCT", "FAIL_INFRA") else "UNKNOWN"
                still += rec.get("failed_nodes") or ns
        return status, still

    def file_trunk_finding(self, task, pid, cand, pipeline_id, nodes):
        finding = {"ts": utc_ms(), "kind": "TRUNK_FLAKE_SUSPECT", "task": task, "proof_id": pid,
                   "sha": cand, "pipeline_id": pipeline_id, "nodes": nodes,
                   "disposition": "box-green: proof PASS; nodes owed a TRUNK packet"}
        path = self.run_root / "trunk-findings.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(finding, sort_keys=True) + "\n")
        self.alert("TRUNK_FLAKE_SUSPECT", "%s: circleci pipeline %s red on %d node(s) in files the "
                   "candidate does not touch; all green on a serial box re-run -> PASS. TRUNK "
                   "finding filed. Nodes: %s" % (pid, pipeline_id, len(nodes), ", ".join(nodes)), task)
        return finding

    def _write(self, pid, rec):
        d = self.run_root / "proofs"
        d.mkdir(parents=True, exist_ok=True)
        (d / ("%s.json" % pid)).write_text(json.dumps(rec, indent=2, sort_keys=True, default=str),
                                          encoding="utf-8")
        return d / ("%s.json" % pid)
