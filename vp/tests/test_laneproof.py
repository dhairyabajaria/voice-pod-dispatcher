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
    # D79b: a CircleCI record is filed per pipeline id, and names its own file
    assert rec["record"] == "proofs/proof-1-ppipe-206.json"
    assert json.loads((tmp_path / "run" / "proofs" / "proof-1-ppipe-206.json").read_text())["status"] == "PASS"
    assert not (tmp_path / "run" / "proofs" / "proof-1.json").exists()
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


def test_d79b_a_cancelled_pipeline_records_cancelled_and_keeps_the_earlier_answer(tmp_path):
    """D79b (2026-09-19): one attempt's three rounds reused one proof_id; round 1
    answered FAIL_PRODUCT on pipeline 722a0b89 (62 reds), rounds 2/3 were
    cancelled and their writes clobbered that answer (0 reds, FAIL_INFRA).  Now:
    one record per pipeline id, a cancelled workflow is CANCELLED, and a
    pipeline-less record never overwrites a real pipeline answer."""
    wt, base, cand = repo(tmp_path)
    red = pipeline([("platform/tests/test_x.py", "test_a"), ("platform/tests/test_x.py", "test_b")])
    circle = FakeCircle(red)
    ex = FakeExec({})
    p = make_proof(tmp_path, circle, ex)
    rec1 = p.run("L06", "proof-1", wt, base, cand, "platform", [])
    assert rec1["status"] == "FAIL_PRODUCT" and rec1["pipeline_id"] == "pipe-206"
    f1 = tmp_path / "run" / "proofs" / "proof-1-ppipe-206.json"
    assert f1.exists() and rec1["record"] == "proofs/proof-1-ppipe-206.json"
    assert len(json.loads(f1.read_text())["failed_nodes"]) == 2
    # round 2: a different pipeline, cancelled while its first shard was already red
    cut = pipeline([("platform/tests/test_x.py", "test_a")])
    cut["jobs"].append({"id": "j-cut", "name": "shard-2", "status": "canceled", "job_number": 342,
                        "workflow_id": "w1"})
    cut["workflows"] = [{"id": "w1", "name": "test", "status": "canceled"}]
    circle.res = cut
    circle.trigger = lambda branch, params, runner, account, targets=None, rotate=True: {
        "pipeline_id": "pipe-207", "account": account or "3"}
    logs = []
    p.log = logs.append
    rec2 = p.run("L06", "proof-1", wt, base, cand, "platform", [])
    assert rec2["status"] == "CANCELLED" and rec2["pipeline_id"] == "pipe-207"
    assert "cancelled" in rec2["reason"] and "D79b" in rec2["reason"]
    assert rec2["record"] == "proofs/proof-1-ppipe-207.json"
    assert any("was cancelled" in m and "CANCELLED" in m for m in logs)
    # round 1's answer is untouched, byte for byte
    kept = json.loads(f1.read_text())
    assert kept["status"] == "FAIL_PRODUCT" and kept["pipeline_id"] == "pipe-206" and len(kept["failed_nodes"]) == 2
    # a pipeline-less record (a gate refusal, a box run) never clobbers a pipeline answer either
    (tmp_path / "run" / "proofs" / "proof-9.json").write_text(json.dumps(
        {"status": "PASS", "pipeline_id": "pipe-1", "route": "circleci"}))
    out = p._write("proof-9", {"status": "BLOCKED_GATE", "route": "circleci", "pipeline_id": None})
    assert out.name != "proof-9.json" and out.name.startswith("proof-9-") and out.exists()
    assert json.loads((tmp_path / "run" / "proofs" / "proof-9.json").read_text())["status"] == "PASS"
    assert any("keeps its pipeline answer" in m for m in logs)
    # ...but a record without any pipeline answer is simply replaced
    p._write("proof-8", {"status": "BLOCKED_GATE", "pipeline_id": None})
    out = p._write("proof-8", {"status": "BLOCKED_CAP", "pipeline_id": None})
    assert out.name == "proof-8.json" and json.loads(out.read_text())["status"] == "BLOCKED_CAP"


def test_d81_a_retry_repolls_the_pipeline_whose_poll_died_instead_of_triggering_again(tmp_path):
    """D81 (2026-09-19 20:46Z): pipeline eb3f3dc9 was triggered, the poll died
    ("identity request failed") -> UNKNOWN -> the retry would have triggered a
    second pipeline for the same sha (~2,300 credits) while the first still ran.
    A record that is UNKNOWN with a real pipeline_id is re-polled, never re-triggered."""
    wt, base, cand = repo(tmp_path)
    circle = FakeCircle({"jobs": [{"id": "j2", "name": "lint", "status": "success", "job_number": 342}],
                         "failed_tests": {}, "workflows": [{"id": "w1", "status": "success"}]})
    polls = {"n": 0}

    def poll(pipeline_id, interval, deadline_s, runner, account, abort, targets=None):
        polls["n"] += 1
        circle.calls.append(("poll", pipeline_id, account))
        if polls["n"] == 1:
            raise RuntimeError("Account 3: identity request failed; command was not run")
        return circle.res
    circle.poll = poll
    logs = []
    p = make_proof(tmp_path, circle, FakeExec({}), logs=logs)
    rec1 = p.run("L09", "proof-1", wt, base, cand, "platform", [])
    assert rec1["status"] == "UNKNOWN" and rec1["pipeline_id"] == "pipe-206"
    assert p.triggered_pipeline(cand)["pipeline_id"] == "pipe-206"
    triggers = [c for c in circle.calls if c[0] == "trigger"]
    assert len(triggers) == 1
    # the retry (same or another pid): no push, no trigger, the same pipeline polled again
    rec2 = p.run("L09", "proof-1", wt, base, cand, "platform", [])
    assert rec2["status"] == "PASS" and rec2["pipeline_id"] == "pipe-206" and rec2["account"] == "3"
    assert len([c for c in circle.calls if c[0] == "trigger"]) == 1, "no second trigger"
    assert len([c for c in circle.calls if c[0] == "push"]) == 1, "no second push"
    assert [c for c in circle.calls if c[0] == "poll"] == [("poll", "pipe-206", "3"), ("poll", "pipe-206", "3")]
    assert any("re-polls circleci pipeline pipe-206" in m and "D81" in m for m in logs)
    rows = [json.loads(l) for l in (tmp_path / "run" / "proofs" / "circleci-pipelines.jsonl").read_text().splitlines()]
    assert [r["status"] for r in rows] == ["triggered", "repolled"], "the re-poll is not a trigger (daily cap)"
    # once answered, nothing is open for that sha any more (D79 reuse takes over)
    assert p.triggered_pipeline(cand) is None
    # a CANCELLED or BLOCKED record with a pipeline id is closed, not open
    (tmp_path / "run" / "proofs" / "proof-x-pdead.json").write_text(json.dumps(
        {"sha": "deadbeef", "route": "circleci", "pipeline_id": "pipe-9", "status": "CANCELLED", "ts": "2026-09-19T20:00:00.000Z"}))
    assert p.triggered_pipeline("deadbeef") is None


class FakeGha(FakeCircle):
    """the gha provider surface: prepare_measured + OverlayDirty on top of FakeCircle"""
    ACCOUNTS = ("gha",)
    DEFAULT_ROTATION = ("gha",)
    MAX_IN_FLIGHT = 1

    class OverlayDirty(RuntimeError):
        pass

    def __init__(self, res, dirty=False):
        FakeCircle.__init__(self, res)
        self.dirty = dirty

    def prepare_measured(self, wt, cand, runner=None, only=None, order=False, shard=None):
        self.calls.append(("prepare", cand) + ((only,) if only else ()) + (("order",) if order else ())
                          + ((shard,) if shard else ()))
        if self.dirty:
            raise self.OverlayDirty("measured differs by ['platform/a.py']")
        # a real child commit of cand (same tree): the branch must point at something
        rc, out, err = sh(["git", "-C", str(wt), "commit-tree", "%s^{tree}" % cand, "-p", cand, "-m", "overlay"])
        assert rc == 0, err
        self.measured = out.strip()
        return self.measured

    def trigger(self, branch, params, runner, account, targets=None, rotate=True):
        self.calls.append(("trigger", branch, params, account))
        return {"pipeline_id": "35470000001", "account": "gha"}


def test_d83_the_gha_provider_is_chosen_by_the_roster_and_records_the_measured_commit(tmp_path):
    """§46: proof.hosted.provider gha -> vpgha; one identity; in-flight 1; the
    proof branch points at cand + the overlay; records carry route gha,
    provider, measured_commit; D81's open-pipeline check reads the gha route."""
    import vpgha
    wt, base, cand = repo(tmp_path)
    cfg = {"hosted": {"provider": "gha"},
           "circleci": {"enabled": True, "mode": "all", "kinds": ["platform"], "account": "A1",
                        "rotation": ["A1", "A2"], "max_in_flight": 2, "delete_branch_after": True}}
    p = laneproof.Proof(tmp_path / "run", VP, tmp_path, git_fn, FakeExec({}), lambda m: None,
                        lambda k, t, task=None: None, cfg)
    assert p.provider() == "gha" and p.circle is vpgha and p.hosted_route() == "gha"
    assert p.circle_cfg()["max_in_flight"] == 2, "D102: min(roster max_in_flight 2, gha MAX_IN_FLIGHT 3)"
    p.cfg["circleci"]["max_in_flight"] = 12
    assert p.circle_cfg()["max_in_flight"] == 3, "D102: the gha constant is 3 full pipelines"
    p.cfg["circleci"]["max_full_in_flight"] = 5
    assert p.circle_cfg()["max_in_flight"] == 5, "D102: roster max_full_in_flight overrides the constant"
    del p.cfg["circleci"]["max_full_in_flight"], p.cfg["circleci"]["max_in_flight"]
    assert p._accounts(p.circle_cfg()) == (["gha"], []), "no rotation, no credits"
    assert p.route("platform") == ("gha", "mode all")
    p.cfg["hosted"] = {"provider": "circleci"}
    assert p.circle is vpcircle and p.hosted_route() == "circleci" and p.circle_cfg()["max_in_flight"] == 2
    # end to end with a fake provider that has the gha surface
    green = {"jobs": [{"id": "j2", "name": "vp/agent", "status": "success", "job_number": 342}],
             "failed_tests": {}, "workflows": [{"id": "1", "status": "success"}]}
    gha = FakeGha(green)
    logs, alerts = [], []
    p = make_proof(tmp_path, gha, FakeExec({}), cfg={"hosted": {"provider": "gha"},
                                                     "circleci": {"enabled": True, "mode": "all", "kinds": ["platform"],
                                                                  "account": "A1", "delete_branch_after": True}},
                   alerts=alerts, logs=logs)
    rec = p.run("L06", "proof-g1", wt, base, cand, "platform", [])
    assert rec["status"] == "PASS" and rec["route"] == "gha" and rec["provider"] == "gha"
    assert rec["pipeline_id"] == "35470000001" and rec["account"] == "gha"
    assert rec["sha"] == cand and rec["measured_commit"] == gha.measured and gha.measured != cand
    assert ("prepare", cand) in gha.calls
    assert any("measured commit %s = %s + vp-proof overlay (D83)" % (gha.measured[:12], cand[:12]) in m for m in logs)
    assert rec["record"] == "proofs/proof-g1-p35470000.json"
    assert ("trigger", "vp/proof/proof-g1-%s" % cand[:12], {"run_full_suite": True}, "gha") in gha.calls
    # rule 2: a dirty overlay is refused before any push or trigger, with an alert
    dirty = FakeGha(green, dirty=True)
    p = make_proof(tmp_path, dirty, FakeExec({}), cfg={"hosted": {"provider": "gha"},
                                                       "circleci": {"enabled": True, "mode": "all", "kinds": ["platform"]}},
                   alerts=alerts, logs=logs)
    rec = p.run("L06", "proof-g2", wt, base, cand, "platform", [])
    assert rec["status"] == "OVERLAY_DIRTY" and rec["pipeline_id"] is None and "platform/a.py" in rec["reason"]
    assert not [c for c in dirty.calls if c[0] in ("push", "trigger")]
    assert [a[0] for a in alerts] == ["PROOF_OVERLAY_DIRTY"]
    # D81 sees an open gha pipeline too
    (tmp_path / "run" / "proofs" / "proof-g3-p1.json").write_text(json.dumps(
        {"sha": "abc", "route": "gha", "pipeline_id": "35470000009", "status": "UNKNOWN", "ts": "2026-09-20T00:00:00.000Z"}))
    assert p.triggered_pipeline("abc")["pipeline_id"] == "35470000009"


def test_d87_a_hosted_full_suite_refused_by_the_off_switch_or_cap_is_held_not_run_on_the_box(tmp_path):
    """02:52Z 2026-09-20: the CIRCLECI-OFF kill switch cancelled the canary and the
    retry's route() fell through to the BOX for the whole platform suite
    (06-ROUTING §5 forbids it).  Now: held as BLOCKED_OFF / BLOCKED_CAP (no
    strike, re-asked each tick); a targeted suite still uses the box."""
    wt, base, cand = repo(tmp_path)
    ex = FakeExec({})
    logs = []
    p = make_proof(tmp_path, FakeCircle({}), ex, logs=logs,
                   cfg={"circleci": {"enabled": True, "mode": "overflow", "kinds": ["full"],
                                     "flip_off_watcher": True}, "box_slots": 1, "memory_hold_below_pct": 0})
    boxed = []
    p.run_box = lambda *a, **k: boxed.append(a) or {"status": "PASS", "route": "box"}
    (tmp_path / "run").mkdir(exist_ok=True)
    (tmp_path / "run" / "CIRCLECI-OFF").write_text("stop\n")
    rec = p.run("L06-HOSTED", "proof-off", wt, base, cand, "platform", [])
    assert rec["status"] == "BLOCKED_OFF" and rec["route"] == "circleci" and rec["pipeline_id"] is None
    assert "never run on the box" in rec["reason"] and boxed == [], "the full suite did not touch the box"
    assert any("full suite held off the box" in m for m in logs)
    # a targeted suite is still the box's while the switch is off
    rec = p.run("L06", "proof-off-t", wt, base, cand, "platform", ["platform/tests/test_a.py"])
    assert rec["route"] == "box" and len(boxed) == 1
    # the in-flight cap holds a full suite the same way (BLOCKED_CAP)
    (tmp_path / "run" / "CIRCLECI-OFF").unlink()
    p.circle_active = 2
    rec = p.run("L06-HOSTED", "proof-cap", wt, base, cand, "platform", [])
    assert rec["status"] == "BLOCKED_CAP" and len(boxed) == 1
    # a roster that never routes "full" hosted keeps its box behaviour
    p.cfg = {"circleci": {"enabled": True, "mode": "overflow", "kinds": ["targeted"]}, "box_slots": 1,
             "memory_hold_below_pct": 0}
    p.circle_active = 0
    rec = p.run("L06-HOSTED", "proof-box", wt, base, cand, "platform", [])
    assert rec["route"] == "box" and len(boxed) == 2


def test_d93_a_triggered_ledger_row_without_a_proof_record_is_an_open_pipeline(tmp_path):
    """D93: the driver died mid-poll (reboot) before writing any proof record;
    the trigger is on the pipelines ledger.  D81 must re-poll that run, never
    trigger a second one for the same sha.  Once any proof record names the
    pipeline, the row is answered and closed."""
    wt, base, cand = repo(tmp_path)
    circle = FakeCircle({"jobs": [{"id": "j2", "name": "lint", "status": "success", "job_number": 1}],
                         "failed_tests": {}, "workflows": [{"id": "w1", "status": "success"}]})
    logs = []
    p = make_proof(tmp_path, circle, FakeExec({}), logs=logs)
    ledger = tmp_path / "run" / "proofs" / "circleci-pipelines.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"ts": "2026-09-20T09:38:41.652Z", "proof_id": "proof-L06-HOSTED-R7-x",
                                  "pipeline_id": "35502871574", "account": "gha", "sha": cand,
                                  "status": "triggered"}) + "\n")
    assert p.triggered_pipeline(cand)["pipeline_id"] == "35502871574"
    assert p.triggered_pipeline("other-sha") is None
    rec = p.run("L06", "proof-L06-HOSTED-R7-x", wt, base, cand, "platform", [])
    assert rec["status"] == "PASS" and rec["pipeline_id"] == "35502871574"
    assert not [c for c in circle.calls if c[0] == "trigger"], "re-polled, never re-triggered"
    assert [c[:2] for c in circle.calls if c[0] == "poll"] == [("poll", "35502871574")]
    assert any("re-polls circleci pipeline 35502871574" in m and "D81" in m for m in logs)
    # answered now: the proof record names the pipeline, so the ledger row is closed
    assert p.triggered_pipeline(cand) is None


def test_d98_only_dispatches_one_hosted_job_as_a_targeted_record(tmp_path):
    """§95 item 2 (D98): `only=<job|shard>` renders and triggers ONE workflow
    job / matrix leg on the hosted route; the record carries `only`, so it is
    a targeted proof of that packet's own claim and never a full-suite PASS."""
    wt, base, cand = repo(tmp_path)
    green = {"jobs": [{"id": "j1", "name": "vp/platform-shards-3", "status": "success", "job_number": 7}],
             "failed_tests": {}, "workflows": [{"id": "1", "status": "success"}]}
    gha = FakeGha(green)
    logs = []
    cfg = {"hosted": {"provider": "gha"},
           "circleci": {"enabled": True, "mode": "overflow", "kinds": ["full"], "account": "A1",
                        "delete_branch_after": True}}
    p = make_proof(tmp_path, gha, FakeExec({}), cfg=cfg, logs=logs)
    # a targeted (paths given) platform proof would route to the box under this
    # roster; only= forces the hosted route regardless
    assert p.route("platform", "targeted")[0] == "box"
    rec = p.run("R-X", "proof-R-X-1", wt, base, cand, "platform", ["platform/tests/test_a.py"],
                only="platform-shards-3")
    assert rec["status"] == "PASS" and rec["route"] == "gha" and rec["only"] == "platform-shards-3"
    assert rec["paths"] == ["platform/tests/test_a.py"]
    assert ("prepare", cand, "platform-shards-3") in gha.calls, "the overlay was rendered for that leg alone"
    assert [c for c in gha.calls if c[0] == "trigger"], "triggered on the hosted route"
    assert any("only=platform-shards-3" in m and "never a full-suite answer" in m for m in logs)


def test_d98_only_never_runs_on_the_box_and_needs_a_rendering_provider(tmp_path):
    wt, base, cand = repo(tmp_path)
    ex = FakeExec({})
    # hosted disabled: held (BLOCKED_OFF), not dropped on the box
    p = make_proof(tmp_path, FakeGha({}), ex, cfg={"hosted": {"provider": "gha"},
                                                   "circleci": {"enabled": False}})
    rec = p.run("R-X", "proof-R-X-2", wt, base, cand, "platform", [], only="portal")
    assert rec["status"] == "BLOCKED_OFF" and "only=portal needs the hosted route" in rec["reason"]
    assert not ex.calls, "no box run"
    # the circleci provider cannot render a per-job workflow: UNKNOWN, no trigger, no box run
    circle = FakeCircle({"jobs": [], "failed_tests": {}, "workflows": []})
    p = make_proof(tmp_path, circle, ex)
    rec = p.run("R-X", "proof-R-X-3", wt, base, cand, "platform", [], only="portal")
    assert rec["status"] == "UNKNOWN" and "only=portal needs a provider that renders" in rec["reason"]
    assert not [c for c in circle.calls if c[0] == "trigger"] and not ex.calls


def test_fleet1_only_runs_have_their_own_in_flight_cap(tmp_path):
    """Fleet-1 (§98): only= proofs count against max_only_in_flight (default 3),
    never against the full-pipeline cap; at the cap they hold (BLOCKED_CAP, no
    strike) instead of running."""
    wt, base, cand = repo(tmp_path)
    green = {"jobs": [{"id": "j1", "name": "vp/portal", "status": "success", "job_number": 7}],
             "failed_tests": {}, "workflows": [{"id": "1", "status": "success"}]}
    gha = FakeGha(green)
    seen = {}
    real_poll = gha.poll
    def poll(*a, **k):
        seen["only_active"], seen["circle_active"] = p.only_active, p.circle_active
        return real_poll(*a, **k)
    gha.poll = poll
    cfg = {"hosted": {"provider": "gha"},
           "circleci": {"enabled": True, "mode": "all", "kinds": ["platform"], "account": "A1",
                        "delete_branch_after": True}}
    p = make_proof(tmp_path, gha, FakeExec({}), cfg=cfg)
    assert p.only_cap() == 3
    rec = p.run("R-X", "proof-R-X-1", wt, base, cand, "platform", [], only="portal")
    assert rec["status"] == "PASS" and seen == {"only_active": 1, "circle_active": 0}
    assert p.only_active == 0 and p.circle_active == 0
    p.only_active = 3
    rec = p.run("R-X", "proof-R-X-2", wt, base, cand, "platform", [], only="portal")
    assert rec["status"] == "BLOCKED_CAP" and "3 only= proofs in flight >= max_only_in_flight 3" in rec["reason"]
    assert len([c for c in gha.calls if c[0] == "trigger"]) == 1, "the held one triggered nothing"
    p.cfg["circleci"]["max_only_in_flight"] = 4
    rec = p.run("R-X", "proof-R-X-3", wt, base, cand, "platform", [], only="portal")
    assert rec["status"] == "PASS"


def test_d100_a_final_canary_renders_the_order_job_and_records_it(tmp_path):
    wt, base, cand = repo(tmp_path)
    green = {"jobs": [{"id": "j1", "name": "vp/platform-order", "status": "success", "job_number": 9}],
             "failed_tests": {}, "workflows": [{"id": "1", "status": "success"}]}
    gha = FakeGha(green)
    logs = []
    cfg = {"hosted": {"provider": "gha"},
           "circleci": {"enabled": True, "mode": "all", "kinds": ["platform"], "account": "A1",
                        "delete_branch_after": True}}
    p = make_proof(tmp_path, gha, FakeExec({}), cfg=cfg, logs=logs)
    rec = p.run("L06", "proof-L06-1", wt, base, cand, "platform", [], order=True)
    assert rec["status"] == "PASS" and rec["order"] is True and rec["only"] is None
    assert ("prepare", cand, "order") in gha.calls
    assert any("order=final (platform-order rendered, D100)" in m for m in logs)
    rec = p.run("L06", "proof-L06-2", wt, base, cand, "platform", [])
    assert rec["order"] is False and ("prepare", cand) in gha.calls


def test_roster_shard_workers_and_stagger_reach_the_overlay(tmp_path):
    wt, base, cand = repo(tmp_path)
    green = {"jobs": [{"id": "j1", "name": "vp/agent", "status": "success", "job_number": 7}],
             "failed_tests": {}, "workflows": [{"id": "1", "status": "success"}]}
    gha = FakeGha(green)
    cfg = {"hosted": {"provider": "gha"},
           "circleci": {"enabled": True, "mode": "all", "kinds": ["platform"], "account": "A1",
                        "delete_branch_after": True, "shard_workers": 6, "shard_stagger_s": 1.25}}
    p = make_proof(tmp_path, gha, FakeExec({}), cfg=cfg)
    p.run("L06", "proof-L06-1", wt, base, cand, "platform", [])
    assert ("prepare", cand, {"workers": 6, "stagger_s": 1.25}) in gha.calls
    del p.cfg["circleci"]["shard_workers"], p.cfg["circleci"]["shard_stagger_s"]
    p.run("L06", "proof-L06-2", wt, base, cand, "platform", [])
    assert ("prepare", cand) in gha.calls, "no roster keys -> the plain call"
