#!/usr/bin/env python3
"""tests/test_laneproof.py -- item 5 (F6): a CircleCI red in a file the
candidate does not touch is re-run on the box before the candidate is failed;
box-green = PASS + TRUNK finding.  Fake pipeline JSON, fake vpproof, real git."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP))

import laneproof  # noqa: E402
import vpcircle   # noqa: E402


def sh(args, cwd=None):
    cp = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, universal_newlines=True)
    return cp.returncode, cp.stdout, cp.stderr


def git_fn(args, cwd=None, timeout_s=300, log=False):
    return sh(["git"] + list(args), cwd=cwd)


def repo(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    sh(["git", "-C", str(wt), "init", "-q"])
    sh(["git", "-C", str(wt), "config", "user.email", "t@t"])
    sh(["git", "-C", str(wt), "config", "user.name", "t"])
    (wt / "platform" / "tests").mkdir(parents=True)
    (wt / "platform" / "hello.py").write_text("x=1\n")
    (wt / "platform" / "tests" / "test_flaky.py").write_text("def test_race(): pass\n")
    (wt / "platform" / "tests" / "test_mine.py").write_text("def test_mine(): pass\n")
    (wt / "portal").mkdir()
    (wt / "portal" / "a.test.ts").write_text("")
    sh(["git", "-C", str(wt), "add", "-A"])
    sh(["git", "-C", str(wt), "commit", "-q", "-m", "base"])
    base = sh(["git", "-C", str(wt), "rev-parse", "HEAD"])[1].strip()
    (wt / "platform" / "hello.py").write_text("x=2\n")
    (wt / "platform" / "tests" / "test_mine.py").write_text("def test_mine(): assert True\n")
    sh(["git", "-C", str(wt), "add", "-A"])
    sh(["git", "-C", str(wt), "commit", "-q", "-m", "cand"])
    cand = sh(["git", "-C", str(wt), "rev-parse", "HEAD"])[1].strip()
    return wt, base, cand


def pipeline(failed_nodes):
    """fake pipeline JSON: one failed platform shard with the given junit rows"""
    jobs = [{"id": "j1", "name": "platform-shard", "status": "failed", "job_number": 341},
            {"id": "j2", "name": "lint", "status": "success", "job_number": 342}]
    tests = [{"file": f, "classname": f.replace("/", ".")[:-3], "name": n, "result": "failure",
              "message": "AssertionError: race between sweep and select"} for f, n in failed_nodes]
    return {"jobs": jobs, "failed_tests": {341: tests}, "workflows": [{"id": "w1", "status": "failed"}]}


class FakeCircle(object):
    Cancelled = vpcircle.Cancelled
    classify = staticmethod(vpcircle.classify)
    ACCOUNTS = vpcircle.ACCOUNTS
    DEFAULT_ROTATION = vpcircle.DEFAULT_ROTATION
    target = staticmethod(vpcircle.target)
    _looks_like_credit_error = staticmethod(vpcircle._looks_like_credit_error)

    def __init__(self, res):
        self.res = res
        self.calls = []

    class Runner(object):
        def git(self, args, cwd=None):
            return ""

    def github_remote(self, wt, runner=None, override=None):
        return "git@github.com:dhairyabajaria/voice-pod-NEW.git"

    def project_visible(self, runner, account, targets=None):
        self.calls.append(("preflight", account))
        return True, ""

    def push_branch(self, wt, branch, runner, remote=None):
        self.calls.append(("push", branch, remote))

    def delete_branch(self, wt, branch, runner, remote=None):
        self.calls.append(("delete", branch, remote))

    def trigger(self, branch, params, runner, account, targets=None, rotate=True):
        self.calls.append(("trigger", branch, params, account))
        return {"pipeline_id": "pipe-206", "account": account or "3"}

    def poll(self, pipeline_id, interval, deadline_s, runner, account, abort, targets=None):
        self.calls.append(("poll", pipeline_id))
        return self.res

    def record(self, run_root, sha, pipeline, jobs, failed, cls):
        d = Path(run_root) / "proofs" / sha
        d.mkdir(parents=True, exist_ok=True)
        (d / "classified.json").write_text(json.dumps(cls))
        return d

    def cancel_pipeline(self, pid, runner, account):
        return []


class FakeExec(object):
    """vpproof.py invocations answer from `script` (per proof-id substring)."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def run(self, argv, cwd=None, timeout_s=120, env=None):
        self.calls.append(list(argv))
        if any(str(a).endswith("vpproof.py") for a in argv):
            pid = argv[argv.index("--proof-id") + 1]
            for key, res in self.script.items():
                if key in pid:
                    return 0, json.dumps(res) + "\n", ""
            return 0, json.dumps({"status": "PASS", "counts": {}}) + "\n", ""
        return sh(argv, cwd=cwd)


def make_proof(tmp_path, circle, exec_, cfg=None, alerts=None, logs=None):
    alerts = alerts if alerts is not None else []
    logs = logs if logs is not None else []
    cfg = cfg or {"circleci": {"enabled": True, "mode": "all", "kinds": ["platform"], "account": "3",
                               "delete_branch_after": True}}
    return laneproof.Proof(tmp_path / "run", VP, tmp_path, git_fn, exec_,
                           lambda m: logs.append(m), lambda k, t, task=None: alerts.append((k, t)),
                           cfg, circle=circle, circle_runner=circle.Runner())


def test_untouched_red_reruns_on_the_box_green_is_pass_with_trunk_finding(tmp_path):
    wt, base, cand = repo(tmp_path)
    circle = FakeCircle(pipeline([("platform/tests/test_flaky.py", "test_race")]))
    ex = FakeExec({"rerun": {"status": "PASS", "counts": {"failed_nodes": []}}})
    alerts, logs = [], []
    p = make_proof(tmp_path, circle, ex, alerts=alerts, logs=logs)
    rec = p.run("L35", "proof-1", wt, base, cand, "platform", ["platform/tests"])
    assert rec["status"] == "PASS" and rec["route"] == "circleci"
    assert rec["flake_suspect"] == {"nodes": ["platform/tests/test_flaky.py::test_race"],
                                    "rerun": "PASS", "still_red": []}
    assert rec["failed_nodes"] == []
    rerun = next(a for a in ex.calls if "--proof-id" in a and "rerun" in a[a.index("--proof-id") + 1])
    assert "--no-record" in rerun and rerun[rerun.index("--workers") + 1] == "1"
    assert rerun[rerun.index("--paths") + 1] == "platform/tests/test_flaky.py::test_race"
    finding = json.loads((tmp_path / "run" / "trunk-findings.jsonl").read_text())
    assert finding["kind"] == "TRUNK_FLAKE_SUSPECT" and finding["pipeline_id"] == "pipe-206"
    assert finding["nodes"] == ["platform/tests/test_flaky.py::test_race"]
    assert [a[0] for a in alerts] == ["TRUNK_FLAKE_SUSPECT"]
    assert json.loads((tmp_path / "run" / "proofs" / "proof-1.json").read_text())["status"] == "PASS"
    assert ("trigger", "vp/proof/proof-1-%s" % cand[:12], {"run_full_suite": True}, "3") in circle.calls
    assert any("box re-run" in m for m in logs)
    # branch cleaned up locally
    assert "vp/proof/proof-1" not in sh(["git", "-C", str(wt), "branch"])[1]


def test_untouched_red_that_stays_red_on_the_box_is_fail_product(tmp_path):
    wt, base, cand = repo(tmp_path)
    circle = FakeCircle(pipeline([("platform/tests/test_flaky.py", "test_race")]))
    ex = FakeExec({"rerun": {"status": "FAIL_PRODUCT",
                             "counts": {"failed_nodes": ["platform/tests/test_flaky.py::test_race"]}}})
    alerts = []
    p = make_proof(tmp_path, circle, ex, alerts=alerts)
    rec = p.run("L35", "proof-2", wt, base, cand, "platform", [])
    assert rec["status"] == "FAIL_PRODUCT"
    assert rec["failed_nodes"] == ["platform/tests/test_flaky.py::test_race"]
    assert rec["flake_suspect"]["rerun"] == "FAIL_PRODUCT"
    assert "race between sweep" in rec["errors"]["platform/tests/test_flaky.py::test_race"]
    assert alerts == [] and not (tmp_path / "run" / "trunk-findings.jsonl").exists()


def test_red_in_a_touched_file_is_never_rerun(tmp_path):
    wt, base, cand = repo(tmp_path)
    circle = FakeCircle(pipeline([("platform/tests/test_mine.py", "test_mine"),
                                  ("platform/tests/test_flaky.py", "test_race")]))
    ex = FakeExec({})
    p = make_proof(tmp_path, circle, ex)
    rec = p.run("L35", "proof-3", wt, base, cand, "platform", [])
    assert rec["status"] == "FAIL_PRODUCT" and rec["flake_suspect"] is None
    assert not any("vpproof.py" in " ".join(a) for a in ex.calls), "no box re-run"
    assert len(rec["failed_nodes"]) == 2


def test_portal_and_over_cap_reds_are_not_rerun(tmp_path):
    wt, base, cand = repo(tmp_path)
    p = make_proof(tmp_path, FakeCircle({}), FakeExec({}))
    cc = p.circle_cfg()
    assert p.untouched_red_nodes(wt, base, cand, ["portal/a.test.ts::renders"], cc) is None
    assert p.untouched_red_nodes(wt, base, cand, ["tests/test_flaky.py::test_race"], cc) == \
        ["platform/tests/test_flaky.py::test_race"], "junit paths lacking platform/ are normalised"
    many = ["platform/tests/test_flaky.py::t%d" % i for i in range(11)]
    assert p.untouched_red_nodes(wt, base, cand, many, cc) is None
    assert p.untouched_red_nodes(wt, base, cand, ["platform/tests/missing.py::x"], cc) is None


def test_routing_off_all_overflow_cap_and_in_flight(tmp_path):
    ex = FakeExec({})
    p = make_proof(tmp_path, FakeCircle({}), ex, cfg={"circleci": {"enabled": False}})
    assert p.route("platform")[0] == "box"
    p.cfg = {"circleci": {"enabled": True, "mode": "all", "kinds": ["platform"]}}
    assert p.route("platform")[0] == "circleci" and p.route("portal")[0] == "box"
    p.cfg = {"circleci": {"enabled": True, "mode": "overflow", "kinds": ["platform"]}, "box_slots": 1}
    assert p.route("platform") == ("box", "overflow: box free (0/1)")
    p.box_active = 1
    assert p.route("platform")[0] == "circleci"
    p.circle_active = 2
    assert p.route("platform")[0] == "box", "max_in_flight 2 reached"
    p.circle_active = 0
    p.cfg["circleci"]["max_pipelines_per_day"] = 1
    (tmp_path / "run" / "proofs").mkdir(parents=True)
    (tmp_path / "run" / "proofs" / "circleci-pipelines.jsonl").write_text(
        json.dumps({"ts": laneproof.utc_ms(), "pipeline_id": "x"}) + "\n")
    alerts = []
    p.alert = lambda k, t, task=None: alerts.append(k)
    assert p.route("platform")[0] == "box" and alerts == ["PIPELINE_CAP"]
    p.cfg["circleci"]["max_pipelines_per_day"] = None
    assert p.route("platform")[0] == "circleci", "O5: no daily cap when null"


def test_circleci_failure_is_unknown_never_a_crash(tmp_path):
    wt, base, cand = repo(tmp_path)

    class Broken(FakeCircle):
        def trigger(self, *a, **k):
            raise RuntimeError("credits exhausted")

    p = make_proof(tmp_path, Broken({}), FakeExec({}))
    rec = p.run("L35", "proof-4", wt, base, cand, "platform", [])
    # D44: a credit-shaped trigger error rotates through every account; all refusing
    # is BLOCKED_CREDITS (held, no strike), and the reason names each account
    assert rec["status"] == "BLOCKED_CREDITS" and rec["reason"].count("credits exhausted") == 6, rec
    assert sorted(p._credit_blocked) == ["1", "2", "3", "A1", "A2", "A3"]

    class Broken2(FakeCircle):
        def trigger(self, *a, **k):
            raise RuntimeError("boom: 500 from circleci")
    p = make_proof(tmp_path, Broken2({}), FakeExec({}))
    rec = p.run("L35", "proof-4b", wt, base, cand, "platform", [])
    assert rec["status"] == "UNKNOWN" and "boom" in rec["reason"], "a non-credit failure is UNKNOWN as before"


# -- item 9: vpcircle configuration -------------------------------------------------

def test_item9_defaults_account_3_overflow_no_daily_cap_two_in_flight_delete_branch(tmp_path):
    p = make_proof(tmp_path, FakeCircle({}), FakeExec({}))
    p.cfg = {}
    cc = p.circle_cfg()
    assert (cc["account"], cc["mode"], cc["max_pipelines_per_day"], cc["max_in_flight"],
            cc["delete_branch_after"], cc["flip_off_watcher"]) == ("3", "overflow", None, 2, True, False)
    # roster-v13 spellings: max_pipelines_in_flight alias, 0 daily cap = no cap
    p.cfg = {"circleci": {"enabled": True, "kinds": ["full"], "max_pipelines_in_flight": 1,
                          "max_pipelines_per_day": 0}}
    assert p.circle_cfg()["max_in_flight"] == 1
    p.box_active = 1
    assert p.route("full")[0] == "circleci"
    p.circle_active = 1
    assert p.route("full") == ("box", "circleci in flight 1/1")


def test_item9_flip_off_watcher_is_off_by_default_and_routes_to_box_when_armed(tmp_path):
    p = make_proof(tmp_path, FakeCircle({}), FakeExec({}),
                   cfg={"circleci": {"enabled": True, "mode": "all", "kinds": ["full"]}})
    (tmp_path / "run").mkdir(exist_ok=True)
    (tmp_path / "run" / "CIRCLECI-OFF").write_text("owner said stop\n")
    assert p.route("full")[0] == "circleci", "watcher off: the OFF file is ignored"
    p.cfg["circleci"]["flip_off_watcher"] = True
    assert p.route("full") == ("box", "circleci flipped off (CIRCLECI-OFF)")


def test_item9_flip_off_mid_poll_cancels_the_pipeline(tmp_path):
    wt, base, cand = repo(tmp_path)

    class Flip(FakeCircle):
        def poll(self, pipeline_id, interval, deadline_s, runner, account, abort, targets=None):
            (tmp_path / "run" / "CIRCLECI-OFF").write_text("stop\n")
            if abort():
                raise vpcircle.Cancelled("flipped off")
            return self.res

        def cancel_pipeline(self, pid, runner, account):
            self.calls.append(("cancel", pid))
            return ["w1"]

    circle = Flip(pipeline([]))
    p = make_proof(tmp_path, circle, FakeExec({}),
                   cfg={"circleci": {"enabled": True, "mode": "all", "kinds": ["platform"],
                                     "flip_off_watcher": True}})
    rec = p.run("L35", "proof-9", wt, base, cand, "platform", [])
    assert rec["status"] == "CANCELLED" and rec["cancelled_workflows"] == ["w1"]
    assert ("cancel", "pipe-206") in circle.calls


def test_docs_only_proof_passes_without_a_run(tmp_path):
    logs = []
    p = make_proof(tmp_path, FakeCircle(pipeline([])), None, logs=logs)   # no exec: a run would blow up
    rec = p.run("R-DOCS", "proof-docs-1", tmp_path, "b" * 40, "c" * 40, "docs", [])
    assert rec["status"] == "PASS" and rec["route"] == "none" and rec["failed_nodes"] == []
    assert (tmp_path / "run" / "proofs" / "proof-docs-1.json").exists()
    assert any("docs-only" in m for m in logs)


def test_box_proof_skips_the_store_row_unless_the_roster_asks(tmp_path):
    ex = FakeExec({})
    cfg = {"circleci": {"enabled": False}}
    p = make_proof(tmp_path, FakeCircle(pipeline([])), ex, cfg=cfg)
    rec = p.run("L02", "proof-L02-1", tmp_path, "b" * 40, "c" * 40, "platform", ["platform/tests/test_a.py"])
    assert rec["status"] == "PASS" and rec["route"] == "box"
    argv = ex.calls[-1]
    assert "--no-record" in argv, "v13 records proofs in RUN_ROOT/proofs, not the vpstore row"
    assert (tmp_path / "run" / "proofs" / "proof-L02-1.json").exists()
    p2 = make_proof(tmp_path, FakeCircle(pipeline([])), ex, cfg=dict(cfg, store_record=True))
    p2.run("L02", "proof-L02-2", tmp_path, "b" * 40, "c" * 40, "platform", ["platform/tests/test_a.py"])
    assert "--no-record" not in ex.calls[-1]


def test_full_suite_routes_to_circleci_when_kinds_names_full(tmp_path):
    """06-ROUTING §5: circleci.kinds ["full"] means every FULL suite goes
    off-box; a targeted proof of the same proof_kind stays on the box."""
    ex = FakeExec({})
    cfg = {"circleci": {"enabled": True, "mode": "overflow", "kinds": ["full"], "account": "3"}}
    p = make_proof(tmp_path, FakeCircle(pipeline([])), ex, cfg=cfg)
    assert p.route("platform", "full")[0] == "circleci"
    assert p.route("platform", "targeted")[0] == "box"
    assert p.route("platform")[0] == "box", "no suite given: the proof_kind alone is not listed"


def test_circleci_credit_refusal_after_the_trigger_is_blocked_credits_not_a_fail(tmp_path):
    """pipeline ad4709dd (2026-09-18): every job 'failed' in 75 s with CircleCI's
    'no credits are available on your plan' message -- the account's condition,
    reported as BLOCKED_CREDITS (the driver holds without a strike), never as
    FAIL_INFRA/FAIL_PRODUCT"""
    wt, base, cand = repo(tmp_path)
    res = {"jobs": [{"id": "j1", "name": "lint-and-typecheck", "status": "failed", "job_number": 11}],
           "failed_tests": {}, "workflows": [{"id": "w1", "status": "failed"}]}

    class Broke(FakeCircle):
        def credit_block(self, jobs, runner, account, targets=None):
            self.calls.append(("credit_block", [j["name"] for j in jobs]))
            return "This job has been blocked because no credits are available on your plan."
    fake = Broke(res)
    p = make_proof(tmp_path, fake, FakeExec({}))
    rec = p.run("L22-HOSTED", "proof-5", wt, base, cand, "platform", [])
    # D44: every account was tried on its own repo and refused; the reason names each
    assert rec["status"] == "BLOCKED_CREDITS" and rec["reason"].count("no credits") >= 3 and rec["pipeline_id"] is None
    assert sorted(p._credit_blocked) == sorted(FakeCircle.DEFAULT_ROTATION), "every account was refused"
    assert ("credit_block", ["lint-and-typecheck"]) in fake.calls
    assert [c[3] for c in fake.calls if c[0] == "trigger"] == ["3", "1", "2", "A1", "A2", "A3"]
    assert any(c[:2] == ("delete", "vp/proof/proof-5-%s" % cand[:12]) for c in fake.calls), "branch cleaned up"
    # the real detector reads the failed job's messages through the runner
    import vpcircle

    class R(object):
        def circleci_api(self, account, path, method="GET", data=None):
            import subprocess
            body = {"messages": [{"type": "error", "reason": "free-plan-no-credits-available",
                                  "message": "This job has been blocked because no credits are available on your plan."}]}
            return subprocess.CompletedProcess([path], 0, json.dumps(body), "")
    assert "no credits" in vpcircle.credit_block([{"status": "failed", "job_number": 11}], R(), "3")
    assert vpcircle.credit_block([{"status": "success", "job_number": 11}], R(), "3") is None


def test_circleci_spread_starts_successive_proofs_on_successive_accounts(tmp_path):
    """D49: roster circleci.spread = [A1, A2, A3] -> proof 1 starts on A1, proof 2 on
    A2, proof 3 on A3, proof 4 on A1 again; the rest of the rotation follows each
    as fallback.  Without `spread` the D44 order (account, then rotation) is unchanged."""
    cfg = {"circleci": {"enabled": True, "mode": "all", "kinds": ["platform"], "account": "A1",
                        "rotation": ["A1", "A2", "A3", "2", "3", "1"], "spread": ["A1", "A2", "A3"]}}
    p = make_proof(tmp_path, FakeCircle({}), FakeExec({}), cfg=cfg)
    cc = p.circle_cfg()
    starts = [p._accounts(cc)[0] for _ in range(4)]
    assert starts == [["A1", "A2", "A3", "2", "3", "1"], ["A2", "A3", "A1", "2", "3", "1"],
                      ["A3", "A1", "A2", "2", "3", "1"], ["A1", "A2", "A3", "2", "3", "1"]], starts
    # a credits-blocked spread member is skipped, the round-robin still advances
    p._block_account("A2", "no credits")
    assert p._accounts(cc)[0] == ["A3", "A1", "2", "3", "1"]
    # no spread: D44 order, stable across proofs
    cfg2 = {"circleci": {"enabled": True, "mode": "all", "kinds": ["platform"], "account": "3"}}
    p2 = make_proof(tmp_path, FakeCircle({}), FakeExec({}), cfg=cfg2)
    cc2 = p2.circle_cfg()
    assert p2._accounts(cc2)[0] == p2._accounts(cc2)[0] == ["3", "1", "2", "A1", "A2", "A3"]


def test_circleci_rotates_to_the_next_account_on_its_own_repo_when_one_is_out_of_credits(tmp_path):
    """D44: account 3 (Pareen Calling, dhairyabajaria/voice-pod-NEW) is refused for
    credits AFTER the trigger; the proof is re-pushed to account 1's own mirror
    repo (voicepod-ci-mirror-a) and triggered on ITS project; account 3 is then
    skipped for CREDIT_BLOCK_S; every pushed remote is cleaned up."""
    wt, base, cand = repo(tmp_path)
    res = {"jobs": [{"id": "j1", "name": "platform-shard", "status": "success", "job_number": 11}],
           "failed_tests": {}, "workflows": [{"id": "w1", "status": "success"}]}

    class Rot(FakeCircle):
        def credit_block(self, jobs, runner, account, targets=None):
            self.calls.append(("credit_block", account))
            return "no credits are available on your plan" if account == "3" else None

        def trigger(self, branch, params, runner, account, targets=None, rotate=True):
            assert rotate is False, "the caller owns the rotation (the branch must be on the next repo first)"
            self.calls.append(("trigger", branch, params, account, vpcircle.target(account, targets)["definition_id"]))
            return {"pipeline_id": "pipe-%s" % account, "account": account}
    fake = Rot(res)
    p = make_proof(tmp_path, fake, FakeExec({}))
    rec = p.run("L22-HOSTED", "proof-6", wt, base, cand, "platform", [])
    assert rec["status"] == "PASS" and rec["pipeline_id"] == "pipe-1" and rec["account"] == "1", rec
    br = "vp/proof/proof-6-%s" % cand[:12]
    pushes = [c for c in fake.calls if c[0] == "push"]
    assert pushes == [("push", br, "git@github.com:dhairyabajaria/voice-pod-NEW.git"),
                      ("push", br, "git@github.com:voicepod-ci-mirror-a/voice-pod-NEW.git")]
    trig = [c for c in fake.calls if c[0] == "trigger"]
    assert [(c[3], c[4]) for c in trig] == [("3", vpcircle.DEFINITION_ID), ("1", "cda494d8-6652-4487-8d96-98267be06312")]
    assert sorted(c[2] for c in fake.calls if c[0] == "delete") == sorted(r for _, _, r in pushes)
    assert p._credit_blocked.get("3", 0) > 0 and "1" not in p._credit_blocked
    # the next proof skips account 3 without pushing to it
    fake.calls.clear()
    rec = p.run("L23-HOSTED", "proof-7", wt, base, cand, "platform", [])
    assert rec["account"] == "1" and [c[3] for c in fake.calls if c[0] == "trigger"] == ["1"]
    assert [c[2] for c in fake.calls if c[0] == "push"] == ["git@github.com:voicepod-ci-mirror-a/voice-pod-NEW.git"]
    # every account refused -> BLOCKED_CREDITS (the driver holds, no strike)
    p._credit_blocked = {a: p._credit_blocked.get("3") for a in vpcircle.DEFAULT_ROTATION}
    rec = p.run("L24-HOSTED", "proof-8", wt, base, cand, "platform", [])
    assert rec["status"] == "BLOCKED_CREDITS" and "blocked for credits" in rec["reason"]
    # roster overrides reach the target table
    assert vpcircle.target("2", {"2": {"definition_id": "x"}})["definition_id"] == "x"
    assert vpcircle.slug_for("2").startswith("circleci/7BDzzoWMeVDmriGVMigj1S/")


# -- D65: the gate, the cap and the off switch are re-asked at TRIGGER time; every trigger
#    attempt leaves a ledger row; only triggered rows count against the day --------------------

def _ledger(tmp_path):
    p = tmp_path / "run" / "proofs" / "circleci-pipelines.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_d65_gate_closed_at_trigger_time_refuses_without_a_push_and_leaves_a_row(tmp_path):
    """2026-09-18 21:25-21:32Z: 11 account-3 pipelines were triggered by rounds routed
    before DELIVERY-1 closed. The gate is a live callable read at the trigger."""
    wt, base, cand = repo(tmp_path)
    fake = FakeCircle({"jobs": [{"id": "j1", "name": "platform-shard", "status": "success", "job_number": 1}],
                       "failed_tests": {}, "workflows": []})
    gate = {"open": True}
    p = make_proof(tmp_path, fake, FakeExec({}))
    p.gate_open = lambda: gate["open"]
    rec = p.run("L22-HOSTED", "proof-g1", wt, base, cand, "platform", [])
    assert rec["status"] == "PASS"
    rows = _ledger(tmp_path)
    assert [r["status"] for r in rows] == ["triggered"] and rows[0]["pipeline_id"] == "pipe-206"
    gate["open"] = False                                  # the owner closes the gate between routing and trigger
    fake.calls.clear()
    rec = p.run("L23-HOSTED", "proof-g2", wt, base, cand, "platform", [])
    assert rec["status"] == "BLOCKED_GATE" and rec["route"] == "circleci" and rec["pipeline_id"] is None
    assert "not open at trigger time" in rec["reason"]
    assert not [c for c in fake.calls if c[0] in ("push", "trigger")], "nothing pushed, nothing triggered"
    rows = _ledger(tmp_path)
    assert rows[-1]["status"] == "refused_gate" and rows[-1]["proof_id"] == "proof-g2" and rows[-1]["pipeline_id"] is None
    assert p.pipelines_today() == 1, "a refused trigger spends nothing"
    # an unreadable gate is a closed gate
    p.gate_open = lambda: 1 / 0
    rec = p.run("L24-HOSTED", "proof-g3", wt, base, cand, "platform", [])
    assert rec["status"] == "BLOCKED_GATE" and "unreadable" in rec["reason"]
    # no gate callable (tests, dry runs) = no gate
    p.gate_open = None
    assert p.run("L25-HOSTED", "proof-g4", wt, base, cand, "platform", [])["status"] == "PASS"


def test_d65_daily_cap_is_enforced_per_trigger_targeted_falls_to_box_full_is_blocked(tmp_path):
    wt, base, cand = repo(tmp_path)
    fake = FakeCircle({"jobs": [{"id": "j1", "name": "platform-shard", "status": "success", "job_number": 1}],
                       "failed_tests": {}, "workflows": []})
    ex = FakeExec({})
    p = make_proof(tmp_path, fake, ex, cfg={"circleci": {"enabled": True, "mode": "all", "kinds": ["platform", "full"],
                                                         "account": "3", "max_pipelines_per_day": 2}})
    assert p.run("A", "proof-c1", wt, base, cand, "platform", ["platform/a.py"])["status"] == "PASS"
    assert p.run("B", "proof-c2", wt, base, cand, "platform", ["platform/a.py"])["status"] == "PASS"
    assert p.pipelines_today() == 2
    # route() would already say box now; but a proof routed a moment earlier reaches the trigger:
    p.route = lambda kind, suite=None: ("circleci", "routed before the cap")
    fake.calls.clear()
    rec = p.run("C", "proof-c3", wt, base, cand, "platform", ["platform/a.py"])
    assert rec["status"] == "PASS" and rec["route"] == "box", rec              # targeted: the box takes it
    assert not [c for c in fake.calls if c[0] == "trigger"]
    rec = p.run("D-HOSTED", "proof-c4", wt, base, cand, "platform", [])       # full suite: never on the box
    assert rec["status"] == "BLOCKED_CAP" and "max_pipelines_per_day 2" in rec["reason"]
    rows = _ledger(tmp_path)
    assert [r["status"] for r in rows] == ["triggered", "triggered", "refused_cap", "refused_cap"]
    assert p.pipelines_today() == 2, "refused rows never count"
    # the pre-D65 ledger (no status field) still counts as triggered
    with open(tmp_path / "run" / "proofs" / "circleci-pipelines.jsonl", "a") as fh:
        fh.write(json.dumps({"ts": laneproof.utc_ms(), "pipeline_id": "legacy", "account": "3"}) + "\n")
    assert p.pipelines_today() == 3


def test_d65_credit_and_trigger_failures_leave_rows_that_do_not_count(tmp_path):
    wt, base, cand = repo(tmp_path)
    res = {"jobs": [{"id": "j1", "name": "platform-shard", "status": "success", "job_number": 1}],
           "failed_tests": {}, "workflows": []}

    class Flaky(FakeCircle):
        def trigger(self, branch, params, runner, account, targets=None, rotate=True):
            self.calls.append(("trigger", branch, params, account))
            if account == "3":
                raise RuntimeError("HTTP 402: no credits are available on your plan")
            if account == "1":
                raise RuntimeError("HTTP 500: upstream")
            return {"pipeline_id": "pipe-%s" % account, "account": account}
    fake = Flaky(res)
    p = make_proof(tmp_path, fake, FakeExec({}), cfg={"circleci": {"enabled": True, "mode": "all", "kinds": ["platform"],
                                                                   "account": "3", "rotation": ["3", "1", "2"]}})
    rec = p.run("X", "proof-f1", wt, base, cand, "platform", [])
    assert rec["status"] == "UNKNOWN" and "HTTP 500" in rec["reason"]
    rows = _ledger(tmp_path)
    assert [(r["account"], r["status"]) for r in rows] == [("3", "credits_blocked"), ("1", "trigger_failed")]
    assert "402" in rows[0]["reason"] and p.pipelines_today() == 0
    # a refusal that shows only AFTER the trigger is recorded against its pipeline id
    class Late(FakeCircle):
        def credit_block(self, jobs, runner, account, targets=None):
            return "no credits are available on your plan" if account == "2" else None

        def trigger(self, branch, params, runner, account, targets=None, rotate=True):
            return {"pipeline_id": "pipe-%s" % account, "account": account}
    p2 = make_proof(tmp_path / "two", Late(res), FakeExec({}), cfg={"circleci": {"enabled": True, "mode": "all",
                                                                                 "kinds": ["platform"], "account": "2",
                                                                                 "rotation": ["2", "A1"]}})
    rec = p2.run("Y", "proof-f2", wt, base, cand, "platform", [])
    assert rec["status"] == "PASS" and rec["account"] == "A1"
    rows = _ledger(tmp_path / "two")
    assert [(r["account"], r["status"], r["pipeline_id"]) for r in rows] == [
        ("2", "triggered", "pipe-2"), ("2", "credits_blocked", "pipe-2"), ("A1", "triggered", "pipe-A1")]
    assert p2.pipelines_today() == 2, "the blocked pipeline was still spent"


def test_d76_memory_free_pct_parses_memory_pressure():
    """D76: the gate reads memory_pressure's own percentage (the audit runner's
    launch gate), None when the command is missing or says something else."""
    class R(object):
        def __init__(self, out): self.stdout, self.stderr = out, ""
    out = "The system has 17179869184 (4194304 pages with a page size of 4096).\n\nStats: \n" \
          "Pages free: 12345 \n...\nSystem-wide memory free percentage: 41%\n"
    assert laneproof.memory_free_pct(run=lambda *a, **k: R(out)) == 41
    assert laneproof.memory_free_pct(run=lambda *a, **k: R("nonsense")) is None
    def missing(*a, **k):
        raise OSError("no memory_pressure")
    assert laneproof.memory_free_pct(run=missing) is None


def test_d76_box_proof_is_held_below_the_memory_floor_and_runs_above_it(tmp_path):
    """D76: a box-routed proof asks memory_pressure first; below
    proof.memory_hold_below_pct (default 30) it returns BLOCKED_MEMORY without
    launching vpproof (no Postgres spun up), above it the proof runs as before."""
    wt, base, cand = repo(tmp_path)
    circle = FakeCircle(pipeline([]))
    ex = FakeExec({})
    alerts, logs = [], []
    p = make_proof(tmp_path, circle, ex, cfg={"circleci": {"enabled": False}}, alerts=alerts, logs=logs)
    p.memory_pct = lambda: 22
    rec = p.run("L40", "proof-mem-1", wt, base, cand, "platform", ["platform/tests/test_x.py"])
    assert rec["status"] == "BLOCKED_MEMORY" and rec["route"] == "box"
    assert "22% < 30%" in rec["reason"]
    assert not [a for a in ex.calls if "vpproof.py" in " ".join(map(str, a))], "vpproof never launched"
    assert any("BLOCKED_MEMORY" in m for m in logs)
    p.memory_pct = lambda: 45
    rec = p.run("L40", "proof-mem-2", wt, base, cand, "platform", ["platform/tests/test_x.py"])
    assert rec["status"] == "PASS" and rec["route"] == "box"
    p.memory_pct = lambda: None                       # unreadable: never a hold
    assert p.run("L40", "proof-mem-3", wt, base, cand, "platform", ["platform/tests/test_x.py"])["status"] == "PASS"
    p.cfg["memory_hold_below_pct"] = 0                # roster can switch the gate off
    p.memory_pct = lambda: 1
    assert p.run("L40", "proof-mem-4", wt, base, cand, "platform", ["platform/tests/test_x.py"])["status"] == "PASS"
