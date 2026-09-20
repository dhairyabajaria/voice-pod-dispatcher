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
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vpcircle
import vpgha   # noqa: E402
import vpgha_overlay   # noqa: E402
from vpdriver import circle_failed_nodes  # noqa: E402

RERUN_KINDS = {"platform": "platform/", "deploy": "deploy/", "agent": "agent/"}
HOSTED_ROUTES = ("circleci", "gha")      # D83: the `route` values a hosted record may carry

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


MEMORY_PRESSURE_RE = re.compile(r"free percentage:\s*(\d+)%")


def memory_free_pct(run=subprocess.run):
    """D76: the box's free memory as macOS `memory_pressure` reports it
    ("System-wide memory free percentage: 41%"), the same number the old audit
    runner gated launches on; None when the command is missing or unparsable."""
    try:
        res = run(["memory_pressure"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    m = MEMORY_PRESSURE_RE.search((res.stdout or "") + (res.stderr or ""))
    return int(m.group(1)) if m else None


class Proof(object):

    def __init__(self, run_root, here, cn, git, exec_, log, alert, proof_cfg, circle=None,
                 circle_runner=None, python=None, gate_open=None):
        self.run_root = Path(run_root)
        self.here = Path(here)
        self.cn = Path(cn)
        self.git = git                      # callable(args, cwd=None) -> (rc, out, err)
        self.exec = exec_
        self.log = log
        self.alert = alert
        self.cfg = dict(proof_cfg or {})
        self._circle_override = circle              # tests inject a fake provider
        self.circle_runner = circle_runner
        self.python = python or sys.executable
        # D65: callable() -> bool read at TRIGGER time (the driver passes a lambda over
        # its live roster); None = no owner gate on CircleCI triggers
        self.gate_open = gate_open
        self._lock = threading.Lock()
        self.box_active = 0
        self.circle_active = 0
        self.only_active = 0          # Fleet-1: only= runs in flight (own cap)

    # -- config ----------------------------------------------------------------------------

    def provider(self):
        """D83 (§46 rule 8): roster proof.hosted.provider = "circleci" (default) | "gha";
        the CircleCI block (proof.circleci.*) keeps the shared knobs (enabled,
        kinds, mode, branch_prefix, poll/deadline, caps) whichever provider runs."""
        return str((self.cfg.get("hosted") or {}).get("provider") or "circleci")

    @property
    def circle(self):
        if getattr(self, "_circle_override", None) is not None:
            return self._circle_override
        return vpgha if self.provider() == "gha" else vpcircle

    @circle.setter
    def circle(self, value):
        self._circle_override = value

    def hosted_route(self):
        """the `route` a hosted record carries: "gha" or "circleci" (HOSTED_ROUTES)"""
        return "gha" if self.provider() == "gha" else "circleci"

    def circle_cfg(self):
        cc = dict(DEFAULT_CIRCLE)
        raw = dict(self.cfg.get("circleci") or {})
        # roster-v13 spells the in-flight bound `max_pipelines_in_flight`
        if "max_pipelines_in_flight" in raw and "max_in_flight" not in raw:
            raw["max_in_flight"] = raw.pop("max_pipelines_in_flight")
        cc.update(raw)
        cap = getattr(self.circle, "MAX_IN_FLIGHT", None)  # a provider may bound the in-flight count (gha: 3, D102)
        if raw.get("max_full_in_flight") is not None:
            cap = int(raw["max_full_in_flight"])            # D102: the roster knob wins over the provider constant
        if cap is not None:
            cc["max_in_flight"] = min(int(cc.get("max_in_flight", 2) or 2), int(cap))
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
            return "box", "%s disabled or kind %s/%s not listed" % (self.hosted_route(), kind, suite)
        if self.circle_off():
            return "box", "%s flipped off (%s)" % (self.hosted_route(), OFF_FILE)
        cap = cc.get("max_pipelines_per_day")
        if cap is not None and int(cap) > 0 and self.pipelines_today() >= int(cap):
            self.alert("PIPELINE_CAP", "%d CircleCI pipelines today >= cap %d; proofs fall back "
                       "to the box" % (self.pipelines_today(), int(cap)))
            return "box", "daily cap"
        with self._lock:
            if self.circle_active >= int(cc.get("max_in_flight", 2)):
                return "box", "%s in flight %d/%d" % (self.hosted_route(), self.circle_active, cc.get("max_in_flight", 2))
            mode = str(cc.get("mode", "overflow"))
            # §125 (D114): `mode_by_kind: {platform: all}` makes hosted PRIMARY for one
            # proof_kind while the roster-wide mode stays overflow for the rest
            by_kind = cc.get("mode_by_kind") if isinstance(cc.get("mode_by_kind"), dict) else {}
            mode = str(by_kind.get(kind) or mode)
            if mode in ("all", "swap"):
                return self.hosted_route(), "mode all%s" % (" (mode_by_kind.%s)" % kind if by_kind.get(kind) else "")
            if suite == "full" and "full" in listed:
                return self.hosted_route(), "full suite is never run on the box (06-ROUTING §5)"
            slots = int(self.cfg.get("box_slots", 1))
            if self.box_active < slots:
                return "box", "overflow: box free (%d/%d)" % (self.box_active, slots)
            return self.hosted_route(), "overflow: box busy (%d/%d)" % (self.box_active, slots)

    TRIGGERED = "triggered"
    # D65: every trigger attempt leaves a ledger row; only TRIGGERED rows spend a pipeline
    REFUSED_GATE, REFUSED_CAP, REFUSED_OFF = "refused_gate", "refused_cap", "refused_off"
    REFUSED_OVERLAY = "refused_overlay"            # D83 rule 2: the measured commit is not cand + the workflow
    CREDITS_BLOCKED, TRIGGER_FAILED = "credits_blocked", "trigger_failed"
    REPOLLED = "repolled"                          # D81: an open pipeline polled again, not a trigger

    def pipelines_today(self):
        """pipelines actually triggered today (UTC): rows without a status are the
        pre-D65 ledger, which recorded successful triggers only"""
        ledger = self.run_root / "proofs" / "circleci-pipelines.jsonl"
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        n = 0
        try:
            for line in ledger.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("ts", "")[:10] == today and row.get("status", self.TRIGGERED) == self.TRIGGERED:
                    n += 1
        except OSError:
            pass
        return n

    def _note_pipeline(self, pid, pipeline_id, account, cand, status=TRIGGERED, reason=None, only=None):
        ledger = self.run_root / "proofs" / "circleci-pipelines.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": utc_ms(), "proof_id": pid, "pipeline_id": pipeline_id, "account": account, "sha": cand,
               "status": status, "only": only}
        if reason:
            row["reason"] = str(reason)[:300]
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    class Refused(RuntimeError):
        """a trigger our own gate/cap/off-switch refused at trigger time (not CircleCI)"""

        def __init__(self, status, why):
            RuntimeError.__init__(self, why)
            self.status = status

    def _trigger_refusal(self, cc):
        """-> (status, why) when a trigger must not happen NOW, else None. route()
        asked the same questions when the proof was routed; a pipeline is spent at
        the trigger, so they are asked again here (2026-09-18 21:25-21:32Z: 11
        account-3 pipelines from rounds routed before DELIVERY-1 closed)."""
        gate_open = getattr(self, "gate_open", None)   # a pre-D65 instance after a hot reload
        if gate_open is not None:
            try:
                open_ = bool(gate_open())
            except Exception as exc:  # noqa: BLE001 -- an unreadable gate is a closed gate
                return self.REFUSED_GATE, "owner gate unreadable at trigger time: %s" % str(exc)[:120]
            if not open_:
                return self.REFUSED_GATE, "owner gate DELIVERY-1 not open at trigger time"
        if self.circle_off():
            return self.REFUSED_OFF, "%s flipped off (%s) at trigger time" % (self.hosted_route(), OFF_FILE)
        cap = cc.get("max_pipelines_per_day")
        if cap is not None and int(cap) > 0:
            n = self.pipelines_today()
            if n >= int(cap):
                return self.REFUSED_CAP, "%d pipelines triggered today >= max_pipelines_per_day %d" % (n, int(cap))
        return None

    # -- entry -----------------------------------------------------------------------------

    NO_RUN_KINDS = ("docs",)

    ONLY_IN_FLIGHT_DEFAULT = 3

    def only_cap(self):
        """Fleet-1 (§98): only= proofs have their own in-flight cap (default 3),
        beside the full-pipeline cap (max_in_flight, 1 on gha)"""
        return int(self.circle_cfg().get("max_only_in_flight", self.ONLY_IN_FLIGHT_DEFAULT) or 0)

    def run(self, task, pid, wt, base, cand, kind, paths, abort=None, workers=None, only=None, order=False):
        if only:
            # §95 item 2: `only=<job|shard>` is a targeted HOSTED proof of one
            # workflow job / matrix leg (the overlay carries that job alone).  It
            # never routes to the box and its record carries `only`, so D79/D94
            # adoption and the canary release refuse it as a full-suite answer.
            cc = self.circle_cfg()
            if not cc.get("enabled") or self.circle_off():
                rec = {"status": "BLOCKED_OFF", "route": self.hosted_route(), "proof_id": pid, "sha": cand,
                       "kind": kind, "paths": paths, "only": only, "pipeline_id": None, "account": None,
                       "reason": "only=%s needs the hosted route (%s %s)"
                                 % (only, self.hosted_route(), "flipped off" if cc.get("enabled") else "disabled"),
                       "failed_nodes": [], "ts": utc_ms()}
                self._write(pid, rec)
                self.log("PROOF %s %s -> BLOCKED_OFF: only=%s needs the hosted route" % (task, pid, only))
                return rec
            with self._lock:
                only_active = getattr(self, "only_active", 0)
            if only_active >= self.only_cap():
                rec = {"status": "BLOCKED_CAP", "route": self.hosted_route(), "proof_id": pid, "sha": cand,
                       "kind": kind, "paths": paths, "only": only, "pipeline_id": None, "account": None,
                       "reason": "only=%s held: %d only= proofs in flight >= max_only_in_flight %d (Fleet-1)"
                                 % (only, only_active, self.only_cap()),
                       "failed_nodes": [], "ts": utc_ms()}
                self._write(pid, rec)
                self.log("PROOF %s %s -> BLOCKED_CAP: %s" % (task, pid, rec["reason"]))
                return rec
            self.log("PROOF %s %s route=%s (only=%s: one job/leg, targeted, %s)" % (task, pid, self.hosted_route(), only, kind))
            return self.run_circleci(task, pid, wt, base, cand, kind, paths, abort=abort, only=only)
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
        if route in HOSTED_ROUTES and suite == "targeted" and kind == "platform" and self.provider() == "gha":
            # D114 (§124): a TARGETED platform proof the router sends hosted (roster
            # kinds lists "targeted"/"platform"; overflow: the box slot is busy) runs
            # as ONE platform-targeted job (the twin shape: setup + pytest -n N over
            # exactly `paths`, junit, no --cov) instead of the full pipeline.  The
            # D111 lint gate stays a Mac pre-step; F6 re-runs untouched reds on the
            # Mac (run_circleci).  The record carries only=targeted:... -- an answer
            # for the same kind+paths ask only, never a twin's, never a full suite's.
            lint, linted = self.lint_changed(wt, base, cand)
            if lint:
                rec = {"status": "FAIL_PRODUCT", "route": self.hosted_route(), "proof_id": pid, "sha": cand,
                       "kind": kind, "paths": paths, "failed_nodes": lint, "errors": {}, "rc": 1, "stderr": "",
                       "counts": {"failed_nodes": lint, "lint_files": linted}, "pipeline_id": None,
                       "reason": "D111: ruff reds in the lane's changed platform files (Mac pre-step, D114)",
                       "ts": utc_ms()}
                self._write(pid, rec)
                self.log("PROOF %s %s -> FAIL_PRODUCT (%d lint red(s) in %d changed file(s), D111 pre-step; "
                         "hosted targeted job not run)" % (task, pid, len(lint), len(linted)))
                return rec
            if linted:
                self.log("LINT %s %s: %d changed platform file(s) clean (D111)" % (task, pid, len(linted)))
            with self._lock:
                only_active = getattr(self, "only_active", 0)
            if only_active >= self.only_cap():
                # the fleet's scoped slots are full too: queue on the box as before, no hold
                self.log("PROOF %s %s D114 overflow: %d scoped job(s) >= max_only_in_flight %d -> box"
                         % (task, pid, only_active, self.only_cap()))
            else:
                n = int(workers or self.cfg.get("targeted_workers") or vpgha_overlay.TWIN_WORKERS)
                only = "%s%d:%s" % (vpgha_overlay.TARGETED_PREFIX, n, ",".join(str(x) for x in paths))
                self.log("PROOF %s %s D114 %s -> hosted platform-targeted job: %d path(s), -n %d"
                         % (task, pid, why, len(paths), n))
                rec = self.run_circleci(task, pid, wt, base, cand, kind, paths, abort=abort, only=only)
                if rec.get("status") == "FAIL_INFRA" and not (abort and abort()):
                    # §125: a hosted infrastructure failure on a targeted job is the
                    # fleet's problem, not the candidate's -> the Mac answers instead
                    # of a strike (the record above stays as the fleet's evidence)
                    self.log("PROOF %s %s hosted targeted job FAIL_INFRA (pipeline %s) -> the box answers (§125)"
                             % (task, pid, rec.get("pipeline_id")))
                    return self.run_box(task, pid, wt, cand, kind, paths, workers=workers, base=base)
                return rec
        elif route in HOSTED_ROUTES:
            return self.run_circleci(task, pid, wt, base, cand, kind, paths, abort=abort, order=order)
        held_off = self.full_suite_held(suite, why)
        if held_off:
            # D87: a hosted-eligible FULL suite refused by the off switch / cap /
            # in-flight limit is never dropped onto the box (06-ROUTING §5) --
            # 02:52Z the CIRCLECI-OFF kill switch sent L06-HOSTED-R5's whole
            # platform suite to the box.  Held without a strike (BLOCKED_*), the
            # driver re-asks each tick, exactly like a refused trigger.
            status = "BLOCKED_OFF" if self.circle_off() else "BLOCKED_CAP"
            rec = {"status": status, "route": self.hosted_route(), "proof_id": pid, "sha": cand, "kind": kind,
                   "paths": paths, "pipeline_id": None, "account": None,
                   "reason": "%s: full suite held, never run on the box (06-ROUTING §5, D87): %s"
                             % (self.hosted_route(), why),
                   "failed_nodes": [], "ts": utc_ms()}
            self._write(pid, rec)
            self.log("PROOF %s %s -> %s: full suite held off the box (%s)" % (task, pid, status, why))
            return rec
        held = self.memory_hold()
        if held:
            # D76: the box is short of memory (a proof spins up 4 Postgres + 4 pytest
            # workers): the box's condition, never the candidate's -- the driver holds
            # the attempt without a strike (PROOF_BLOCKED_MEMORY) and re-asks each tick
            rec = {"status": "BLOCKED_MEMORY", "route": "box", "proof_id": pid, "sha": cand, "kind": kind,
                   "paths": paths, "reason": held, "failed_nodes": [], "ts": utc_ms()}
            self._write(pid, rec)
            self.log("PROOF %s %s -> BLOCKED_MEMORY: %s" % (task, pid, held))
            return rec
        return self.run_box(task, pid, wt, cand, kind, paths, workers=workers, base=base)

    def full_suite_held(self, suite, why):
        """D87: True when a full suite that the roster routes hosted (`kinds`
        lists "full") was refused by the hosted gate rather than by policy --
        it must wait, not run on the box.  A roster that does not list "full"
        keeps its box behaviour."""
        if suite != "full":
            return False
        cc = self.circle_cfg()
        if not cc.get("enabled") or "full" not in set(cc.get("kinds") or []):
            return False
        return True

    BOX_STOP_SLACK_S = 120.0

    def box_deadlines(self, paths):
        """D89 -> (vpproof's own --timeout-min, the exec's outer timeout_s).
        The outer bound leaves room for the box-lock wait (proof_wait_max_min,
        90 by default) and vpproof's SIGINT/SIGTERM grace, so the wrapper is
        never killed while it still owns a live pytest."""
        cfg = self.cfg
        inner = int(cfg.get("targeted_timeout_min", 40) if paths else cfg.get("full_box_timeout_min", 90))
        wait = float(cfg.get("proof_wait_max_min", 90))
        grace = float(cfg.get("kill_grace_int_s", 60)) + float(cfg.get("kill_grace_term_s", 30))
        return inner, inner * 60.0 + wait * 60.0 + grace + self.BOX_STOP_SLACK_S

    def memory_hold(self):
        """D76: -> reason when free memory is below proof.memory_hold_below_pct
        (default 30, the audit runner's launch gate), else None.  A pre-D76
        instance after a hot reload has no `memory_pct`: read it with getattr."""
        floor = int(self.cfg.get("memory_hold_below_pct", 30) or 0)
        if floor <= 0:
            return None
        pct = (getattr(self, "memory_pct", None) or memory_free_pct)()
        if pct is None or pct >= floor:
            return None
        return "box memory free %d%% < %d%% (memory_pressure)" % (pct, floor)

    # -- box -------------------------------------------------------------------------------

    LINT_RULES_NOTE = "ruff check (platform/pyproject rule set, the same as vp/platform's Lint step)"

    def lint_changed(self, wt, base, cand):
        """D111 (Architect 2026-09-20): `ruff check` on the lane's changed platform
        .py files before the box suite -- the hosted vp/platform job runs
        `uv run ruff check .` and the box proof never did, so D04 verified with
        lint reds and trunk 5deac821 itself reds on 6 files.  -> (reds, files):
        reds as "lint::<file>:<line> <code> <msg>" node ids, [] when clean or
        nothing to lint; None when ruff could not run (never a red)."""
        if not base or not self.cfg.get("lint_changed", True):
            return [], []
        rc, out, _ = self.git(["-C", str(wt), "diff", "--name-only", "%s..%s" % (base, cand), "--", "platform/*.py"])
        files = [l.strip() for l in (out or "").splitlines() if l.strip().endswith(".py")] if rc == 0 else []
        files = [f for f in files if (Path(wt) / f).exists()]
        if not files:
            return [], []
        ruff = Path(wt) / "platform" / ".venv" / "bin" / "ruff"
        if not ruff.exists():
            self.log("LINT %s: no platform/.venv/bin/ruff in the worktree; lint skipped" % wt)
            return None, files
        rel = [f[len("platform/"):] for f in files]
        rc, out, err = self.exec.run([str(ruff), "check", "--output-format", "json", "--no-cache"] + rel,
                                     cwd=str(Path(wt) / "platform"), timeout_s=120)
        try:
            rows = json.loads(out or "[]")
        except ValueError:
            self.log("LINT %s: ruff output unreadable (rc %s): %s" % (wt, rc, (err or out or "")[-200:]))
            return None, files
        reds = ["lint::platform/%s:%s %s %s" % (Path(r.get("filename", "")).relative_to(Path(wt) / "platform")
                                                if str(r.get("filename", "")).startswith(str(Path(wt) / "platform"))
                                                else r.get("filename", ""),
                                                (r.get("location") or {}).get("row"), r.get("code"),
                                                str(r.get("message") or "")[:120])
                for r in rows if isinstance(r, dict)]
        return reds, files

    def run_box(self, task, pid, wt, cand, kind, paths, no_record=True, workers=None, base=None):
        lint, linted = self.lint_changed(wt, base, cand)
        if lint:
            # a lint red is a product red on the hosted route (vp/platform's first step),
            # so it is one here too: FAIL_PRODUCT before any test runs
            rec = {"status": "FAIL_PRODUCT", "route": "box", "proof_id": pid, "sha": cand, "kind": kind,
                   "paths": paths, "failed_nodes": lint, "errors": {}, "rc": 1, "stderr": "",
                   "counts": {"failed_nodes": lint, "lint_files": linted},
                   "reason": "D111: ruff reds in the lane's changed platform files (the hosted Lint gate would fail)",
                   "ts": utc_ms()}
            self._write(pid, rec)
            self.log("PROOF %s %s -> FAIL_PRODUCT (box, %d lint red(s) in %d changed file(s), D111; suite not run)"
                     % (task, pid, len(lint), len(linted)))
            return rec
        if linted:
            self.log("LINT %s %s: %d changed platform file(s) clean (D111)" % (task, pid, len(linted)))
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
        # D89: vpproof owns the deadline.  Before, the exec here timed out at
        # targeted_timeout_min (40) even for a FULL run whose own limit is
        # full_box_timeout_min (90) -- and the box-lock wait counted inside it --
        # so subprocess.run killed the wrapper alone and pytest + its clusters
        # ran on orphaned (L06-HOSTED-R5, 03:32Z, pid 11825).  Now the inner
        # limit is passed explicitly and the outer one is inner + the lock wait
        # + vpproof's INT/TERM grace, so stop_group + reap always run first.
        inner_min, outer_s = self.box_deadlines(paths)
        argv += ["--timeout-min", str(inner_min)]
        with self._lock:
            self.box_active += 1
        try:
            rc, out, err = self.exec.run(argv, cwd=str(self.here), timeout_s=outer_s)
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

    class AllBlocked(RuntimeError):
        """every CircleCI account refused the proof for credits/plan"""

    CREDIT_BLOCK_S = 6 * 3600                 # an account CircleCI refused for credits is skipped this long

    _spread_lock = threading.Lock()   # D49 round-robin counter guard (class-level: one per process)

    def _accounts(self, cc):
        """D44: the account order for one proof: roster circleci.account first,
        then circleci.rotation (default vpcircle.DEFAULT_ROTATION), minus the
        accounts CircleCI refused for credits within CREDIT_BLOCK_S"""
        if self.provider() == "gha":
            return [vpgha.ACCOUNT], []            # one identity (gh's), no rotation, no credits
        first = str(cc.get("account") or self.circle.ACCOUNTS[0])
        order = [first] + [str(a) for a in (cc.get("rotation") or getattr(self.circle, "DEFAULT_ROTATION", ()))
                           if str(a) != first]
        spread = [str(a) for a in (cc.get("spread") or ())]
        if spread:
            # D49: proofs start on successive `spread` accounts (round-robin) so
            # parallel pipelines land on A1/A2/A3 evenly instead of queueing on
            # the first; the rest of the rotation follows as fallback
            with self._spread_lock:
                self._spread_n = getattr(self, "_spread_n", -1) + 1
                k = self._spread_n % len(spread)
            head = spread[k:] + spread[:k]
            order = head + [a for a in order if a not in head]
        blocked = getattr(self, "_credit_blocked", {})
        now = time.monotonic()
        return [a for a in order if blocked.get(a, 0) <= now], [a for a in order if blocked.get(a, 0) > now]

    def _block_account(self, acct, why):
        self._credit_blocked = getattr(self, "_credit_blocked", {})
        self._credit_blocked[acct] = time.monotonic() + self.CREDIT_BLOCK_S
        self.log("PROOF circleci account %s blocked for credits for %dh: %s"
                 % (acct, self.CREDIT_BLOCK_S // 3600, why[:160]))

    def pick_host(self, cc):
        """D112: the host label (roster proof.circleci.hosts) with the fewest FULL
        proofs in flight, ties by list order; None when no hosts are listed (the
        shared label).  only=/preflight runs never pin."""
        hosts = [str(h) for h in (cc.get("hosts") or []) if str(h).strip()]
        if not hosts:
            return None
        with self._lock:
            live = dict(getattr(self, "host_active", {}) or {})
        return min(hosts, key=lambda h: (int(live.get(h, 0)), hosts.index(h)))

    def run_circleci(self, task, pid, wt, base, cand, kind, paths, abort=None, only=None, order=False):
        cc = self.circle_cfg()
        host = None if only else self.pick_host(cc)
        runner = self.circle_runner or self.circle.Runner()
        branch = "%s%s-%s" % (cc.get("branch_prefix", "vp/proof/"), pid, cand[:12])
        param = cc.get("param") or "run_full_suite"
        targets = cc.get("accounts") or None
        pipeline_id, account = None, None
        measured = cand                               # D83: the commit the run measures (cand + overlay on gha)
        pushed = {}                                   # account -> remote the branch was pushed to
        prior = self.triggered_pipeline(cand, only=only)
        with self._lock:
            # Fleet-1: only= runs count against their own cap, never the full-pipeline one
            if only:
                self.only_active = getattr(self, "only_active", 0) + 1
            else:
                self.circle_active += 1
                if host:
                    self.host_active = dict(getattr(self, "host_active", {}) or {})
                    self.host_active[host] = self.host_active.get(host, 0) + 1
        try:
            try:
                if prior:
                    # D81 (20:46Z): the poll of a real, still-running pipeline died
                    # ("identity request failed") -> UNKNOWN -> the retry re-triggered
                    # a second pipeline for the same sha.  Re-poll the one we have.
                    pipeline_id, account = prior["pipeline_id"], prior.get("account")
                    self.log("PROOF %s %s re-polls %s pipeline %s (account %s) recorded by %s: "
                             "no new trigger (D81)" % (task, pid, self.hosted_route(), pipeline_id, account, prior.get("proof_id")))
                    self._note_pipeline(pid, pipeline_id, account, cand, status=self.REPOLLED, only=only,
                                        reason="D81: from %s" % prior.get("proof_id"))
                    res = self.circle.poll(pipeline_id, interval=int(cc.get("poll_interval_s", 60)),
                                           deadline_s=int(cc.get("deadline_min", 90)) * 60,
                                           runner=runner, account=account, targets=targets,
                                           abort=lambda: bool((abort and abort()) or self.circle_off()))
                    accounts = []
                else:
                    prepare = getattr(self.circle, "prepare_measured", None)
                    if only and prepare is None:
                        raise RuntimeError("only=%s needs a provider that renders the workflow (gha); %s cannot"
                                           % (only, self.provider()))
                    if prepare is not None:
                        # D83 §46 rule 2: the measured commit = candidate + exactly one
                        # overlay commit (.github/workflows/vp-proof.yml); anything else
                        # in the diff is OVERLAY_DIRTY and nothing is triggered
                        try:
                            kw = {}
                            if only:
                                kw["only"] = only
                            if order:
                                kw["order"] = True
                            shard = {k: cc[v] for k, v in (("workers", "shard_workers"), ("stagger_s", "shard_stagger_s"))
                                     if cc.get(v) is not None}
                            if shard:
                                kw["shard"] = shard          # roster proof.circleci.shard_workers / shard_stagger_s
                            if host:
                                kw["host"] = host            # D112: one host per full proof
                            measured = prepare(wt, cand, runner, **kw) if kw else prepare(wt, cand, runner)
                        except getattr(self.circle, "OverlayDirty", ()) as exc:
                            raise self.Refused(self.REFUSED_OVERLAY, str(exc)[:300])
                        self.log("PROOF %s %s measured commit %s = %s + vp-proof overlay (D83%s%s%s)"
                                 % (task, pid, measured[:12], cand[:12], ", only=%s" % only if only else "",
                                    ", order=final (platform-order rendered, D100)" if order else "",
                                    ", host=%s (D112)" % host if host else ""))
                    rc, out, err = self.git(["-C", str(wt), "branch", "-f", branch, measured])
                    if rc != 0:
                        raise RuntimeError("git branch -f %s failed: %s" % (branch, (err or out)[:200]))
                    accounts, skipped = self._accounts(cc)
                    if not accounts:
                        raise self.AllBlocked("every CircleCI account is blocked for credits: %s" % skipped)
                    res, refusals = None, []
                for acct in accounts:
                    refusal = self._trigger_refusal(cc)
                    if refusal:
                        self._note_pipeline(pid, None, acct, cand, status=refusal[0], reason=refusal[1], only=only)
                        raise self.Refused(*refusal)
                    # D44: each account triggers its own project on its own GitHub repo --
                    # push there first, and prove the branch is there (D38)
                    tgt = self.circle.target(acct, targets)
                    remote = tgt.get("push_remote") or cc.get("push_remote") or self.circle.github_remote(wt, runner)
                    ok, why = self.circle.project_visible(runner, acct, targets)
                    if not ok:
                        raise RuntimeError("preflight: %s" % why)
                    self.circle.push_branch(wt, branch, runner, remote)
                    pushed[acct] = remote
                    try:
                        trig = self.circle.trigger(branch, {param: True}, runner, acct, targets=targets, rotate=False)
                    except RuntimeError as exc:
                        if self.circle._looks_like_credit_error(str(exc)):
                            self._note_pipeline(pid, None, acct, cand, status=self.CREDITS_BLOCKED, reason=str(exc), only=only)
                            refusals.append((acct, str(exc)[:200]))
                            self._block_account(acct, str(exc))
                            continue
                        self._note_pipeline(pid, None, acct, cand, status=self.TRIGGER_FAILED, reason=str(exc), only=only)
                        raise
                    pipeline_id, account = trig["pipeline_id"], trig["account"]
                    self._note_pipeline(pid, pipeline_id, account, cand, only=only)
                    self.log("PROOF %s %s %s pipeline %s (account %s, %s)" % (task, pid, self.hosted_route(), pipeline_id, account,
                                                                                   tgt.get("repo") or remote))
                    res = self.circle.poll(pipeline_id, interval=int(cc.get("poll_interval_s", 60)),
                                           deadline_s=int(cc.get("deadline_min", 90)) * 60,
                                           runner=runner, account=account, targets=targets,
                                           abort=lambda: bool((abort and abort()) or self.circle_off()))
                    blocked = getattr(self.circle, "credit_block", lambda *_a, **_k: None)(res["jobs"], runner,
                                                                                            account, targets)
                    if not blocked:
                        break
                    # the refusal that only shows AFTER the trigger (account 3, 21:12Z): this
                    # account is out; the next one gets the same branch on its own repo
                    refusals.append((acct, blocked[:200]))
                    self._block_account(acct, blocked)
                    self._note_pipeline(pid, pipeline_id, account, cand, status=self.CREDITS_BLOCKED, reason=blocked, only=only)
                    self.log("PROOF %s %s %s pipeline %s (account %s) blocked for credits -> next account"
                             % (task, pid, self.hosted_route(), pipeline_id, account))
                    res, pipeline_id, account = None, None, None
                if res is None and not prior:
                    raise self.AllBlocked("; ".join("account %s: %s" % r for r in refusals)
                                          or "every CircleCI account is blocked for credits: %s" % skipped)
            except self.circle.Cancelled:
                done = self.circle.cancel_pipeline(pipeline_id, runner, account)
                rec = {"status": "CANCELLED", "route": self.hosted_route(), "proof_id": pid, "sha": cand,
                       "pipeline_id": pipeline_id, "cancelled_workflows": done, "ts": utc_ms()}
                self._write(pid, rec)
                return rec
            except self.Refused as exc:
                # our own gate/cap/off switch said no at trigger time: no pipeline was spent
                if exc.status == self.REFUSED_CAP and paths and not only:
                    # a targeted suite can still run on the box, exactly as route() would have sent it
                    # (never an only= job: it is one workflow job, not a node list)
                    self.log("PROOF %s %s circleci refused (%s) -> box" % (task, pid, exc))
                    return self.run_box(task, pid, wt, cand, kind, paths, base=base)
                status = {self.REFUSED_GATE: "BLOCKED_GATE", self.REFUSED_CAP: "BLOCKED_CAP",
                          self.REFUSED_OFF: "BLOCKED_OFF", self.REFUSED_OVERLAY: "OVERLAY_DIRTY"}[exc.status]
                rec = {"status": status, "route": self.hosted_route(), "proof_id": pid, "sha": cand, "kind": kind,
                       "pipeline_id": None, "account": None, "branch": branch,
                       "reason": "%s: %s" % (self.hosted_route(), str(exc)[:300]), "failed_nodes": [], "ts": utc_ms()}
                self._write(pid, rec)
                self.log("PROOF %s %s -> %s: %s" % (task, pid, status, str(exc)[:200]))
                if status == "OVERLAY_DIRTY":
                    self.alert("PROOF_OVERLAY_DIRTY", "%s %s: the measured commit is not the candidate + "
                               "vp-proof.yml alone (D83 rule 2); no run triggered: %s" % (task, pid, str(exc)[:300]),
                               task)
                return rec
            except self.AllBlocked as exc:
                # a plan/credit refusal is the account's condition, never the candidate's:
                # the driver holds the attempt without a strike (PROOF_BLOCKED_CREDITS)
                rec = {"status": "BLOCKED_CREDITS", "route": self.hosted_route(), "proof_id": pid, "sha": cand,
                       "pipeline_id": pipeline_id, "account": account, "branch": branch,
                       "reason": "%s: %s" % (self.hosted_route(), str(exc)[:300]), "failed_nodes": [], "ts": utc_ms()}
                self._write(pid, rec)
                self.log("PROOF %s %s -> BLOCKED_CREDITS: %s" % (task, pid, str(exc)[:200]))
                return rec
            except Exception as exc:
                rec = {"status": "UNKNOWN", "route": self.hosted_route(), "proof_id": pid, "sha": cand,
                       "reason": "%s: %s: %s" % (self.hosted_route(), type(exc).__name__, str(exc)[:300]),
                       "pipeline_id": pipeline_id, "account": account, "branch": branch,
                       "failed_nodes": [], "ts": utc_ms()}
                self._write(pid, rec)
                self.log("PROOF %s %s -> UNKNOWN (%s: %s)" % (task, pid, self.hosted_route(), str(exc)[:200]))
                return rec
            try:
                cls = self.circle.classify(res["jobs"], res["failed_tests"], workflows=res.get("workflows"))
            except TypeError:                     # an older classify without the D79b kwarg
                cls = self.circle.classify(res["jobs"], res["failed_tests"])
            status = cls["status"]
            failed, errors = circle_failed_nodes(res["failed_tests"])
            flake = None
            collected = None
            if only and vpgha_overlay.scoped_spec(only) and status == "PASS":
                # D118b (§137): a scoped job that collected nothing is no answer
                # (the twin job rendered `uv run pytest` over portal .test.tsx files:
                # "failed with zero failed tests" / a green empty run) -> FAIL_INFRA
                totals = getattr(self.circle, "junit_totals", None)
                if totals is not None:
                    try:
                        scoped_jobs = [j for j in res["jobs"] if str(j.get("name") or "").startswith("vp/platform-t")]
                        got = totals(pipeline_id, scoped_jobs, runner)
                        collected = sum(got.values()) if got else None
                    except Exception as exc:  # noqa: BLE001
                        self.log("PROOF %s %s junit totals unreadable: %s" % (task, pid, exc))
                    if collected == 0:
                        status = "FAIL_INFRA"
                        self.log("PROOF %s %s scoped job collected 0 tests (%s) -> FAIL_INFRA, no answer (D118b)"
                                 % (task, pid, only[:80]))
            if status == "CANCELLED":
                # D79b: a cancelled workflow/job is no answer about the candidate --
                # recorded as CANCELLED (the driver retries after the backoff, the
                # canary does not release, D79 never reuses it), never as a
                # PASS/FAIL_* that later rounds or the reuse check could trust
                self.log("PROOF %s %s %s pipeline %s was cancelled (%d job(s)/workflow(s)) -> CANCELLED"
                         % (task, pid, self.hosted_route(), pipeline_id, len(cls["reds"])))
            if status == "FAIL_PRODUCT" and failed:
                nodes = self.untouched_red_nodes(wt, base, cand, failed, cc)
                if nodes:
                    self.log("PROOF %s %s %s %d reds in untouched files -> box re-run"
                             % (task, pid, self.hosted_route(), len(nodes)))
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
            rec = {"status": status, "route": self.hosted_route(), "proof_id": pid, "sha": cand, "kind": kind,
                   "paths": paths, "only": only, "order": bool(order), "host": host,
                   "reds": cls["reds"], "failed_nodes": failed, "errors": errors,
                   "flake_suspect": flake, "tests_collected": collected,
                   "pipeline_id": pipeline_id, "account": account,
                   "branch": branch, "record_dir": str(out_dir), "ts": utc_ms(),
                   "provider": self.provider(), "measured_commit": measured,
                   "reason": ("circleci pipeline %s cancelled: not an answer (D79b)" % pipeline_id
                              if status == "CANCELLED" else
                              "scoped job collected 0 tests: not an answer (D118b)" if collected == 0 else None),
                   "jobs": [{"name": j.get("name"), "status": j.get("status"),
                             "job_number": j.get("job_number")} for j in res["jobs"]]}
            self._write(pid, rec)
            self.log("PROOF %s %s -> %s (%s pipeline %s, %d red job(s), %d red node(s)%s)"
                     % (task, pid, status, self.hosted_route(), pipeline_id, len(cls["reds"]), len(failed),
                        "; only=%s, targeted: never a full-suite answer" % only if only else ""))
            return rec
        finally:
            with self._lock:
                if only:
                    self.only_active = max(0, getattr(self, "only_active", 0) - 1)
                else:
                    self.circle_active = max(0, self.circle_active - 1)
                    if host:
                        self.host_active = dict(getattr(self, "host_active", {}) or {})
                        self.host_active[host] = max(0, self.host_active.get(host, 0) - 1)
            if cc.get("delete_branch_after", True):
                for acct, remote in (pushed or {"": cc.get("push_remote")}).items():
                    try:
                        self.circle.delete_branch(wt, branch, runner, remote)
                    except Exception as exc:
                        self.log("circleci branch cleanup %s on %s: %s" % (branch, remote or "origin chain", exc))
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

    ANSWERS = ("PASS", "FAIL_PRODUCT", "FAIL_INFRA")

    OPEN_STATUSES = ("UNKNOWN",)

    @staticmethod
    def open_answers(rec_only, only):
        """D117 (§136): an open pipeline may be re-polled for an ask only when it
        ran the SAME thing -- equal `only` (a full for a full, one scoped set for
        the identical set); a twin ask may also adopt an open FULL run (the D113
        twin_adopts_full shape).  Never one scoped set for another: under D113
        every twin on one union base shares the sha, and the first twin's
        pipeline (one twin step = ITS files) was adopted by 5 siblings today."""
        rec_only, only = rec_only or None, only or None
        if rec_only == only:
            return True
        return bool(only and str(only).startswith("twin:") and not rec_only)

    @staticmethod
    def _open_rank(rec, only):
        """an exact `only` match beats a twin's adoption of a full run; newest next"""
        return (1 if (rec.get("only") or None) == (only or None) else 0, str(rec.get("ts") or ""))

    def triggered_pipeline(self, cand, only=None):
        """D81: the newest CircleCI record for this sha whose pipeline was really
        triggered but whose answer never came back (status UNKNOWN: the poll
        died, the pipeline did not) -> {pipeline_id, account, proof_id}, else None.
        A PASS/FAIL_* is D79's business (reuse), CANCELLED/BLOCKED_* are closed.
        D117: only a record that ran the same `only` (open_answers)."""
        d = self.run_root / "proofs"
        best = None
        try:
            files = sorted(d.glob("proof-*.json"))
        except OSError:
            return None
        for f in files:
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (rec.get("sha") == cand and rec.get("route") in HOSTED_ROUTES and rec.get("pipeline_id")
                    and rec.get("status") in self.OPEN_STATUSES and self.open_answers(rec.get("only"), only)):
                if best is None or self._open_rank(rec, only) > self._open_rank(best, only):
                    best = rec
        if best is None:
            best = self.ledger_open_pipeline(cand, only=only)
        return best

    def ledger_open_pipeline(self, cand, only=None):
        """D93: the driver died mid-poll (the 10:53Z reboot) -> no proof-*.json
        was ever written for the trigger, so D81 saw nothing and the restart
        re-triggered a second run for the same sha (the 04:41Z precedent).
        The trigger itself IS on record: circleci-pipelines.jsonl gets a
        `triggered` row the moment the pipeline exists.  A triggered row for
        this sha with no proof record naming its pipeline_id is open."""
        ledger = self.run_root / "proofs" / "circleci-pipelines.jsonl"
        try:
            rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        except (OSError, ValueError):
            return None
        answered = set()
        try:
            for f in (self.run_root / "proofs").glob("proof-*.json"):
                try:
                    rec = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if rec.get("pipeline_id"):
                    answered.add(str(rec["pipeline_id"]))
        except OSError:
            pass
        # a later ledger row that names the pipeline with any status other than
        # triggered/repolled (credits_blocked after the trigger, 21:12Z) closes it too
        for row in rows:
            if row.get("pipeline_id") and row.get("status") not in (self.TRIGGERED, self.REPOLLED):
                answered.add(str(row["pipeline_id"]))
        best = None
        for row in rows:
            if (row.get("sha") == cand and row.get("pipeline_id") and row.get("status") == self.TRIGGERED
                    and str(row["pipeline_id"]) not in answered and self.open_answers(row.get("only"), only)):
                if best is None or self._open_rank(row, only) > self._open_rank(best, only):
                    best = row
        return best

    def _write(self, pid, rec):
        """RUN_ROOT/proofs/<pid>.json, or <pid>-p<pipeline8>.json when the record
        names a CircleCI pipeline: one record per pipeline id (D79b).  A record
        without a pipeline never overwrites one that holds a real pipeline
        answer -- it lands beside it under a timestamped name.  The file's own
        relative path is written into the record as `record`.

        Why: on 2026-09-19 three rounds of one attempt reused one proof_id and
        each round's write clobbered the previous pipeline's answer (722a0b89's
        62 reds became f2ae535d's cancelled 0)."""
        d = self.run_root / "proofs"
        d.mkdir(parents=True, exist_ok=True)
        pipe = rec.get("pipeline_id")
        name = "%s-p%s.json" % (pid, str(pipe)[:8]) if pipe else "%s.json" % pid
        if not pipe and (d / name).exists():
            try:
                old = json.loads((d / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old = {}
            if old.get("pipeline_id") and old.get("status") in self.ANSWERS:
                name = "%s-%s.json" % (pid, utc_ms().replace(":", "").replace("-", "").replace(".", ""))
                self.log("PROOF %s keeps its pipeline answer (%s %s); writing %s beside it"
                         % (pid, old.get("pipeline_id"), old.get("status"), name))
        rec["record"] = "proofs/%s" % name
        (d / name).write_text(json.dumps(rec, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return d / name
