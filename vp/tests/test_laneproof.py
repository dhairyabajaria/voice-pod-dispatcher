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
        if str(argv[0]).endswith("/ruff"):
            return self.script.get("ruff", (0, "[]", ""))
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

    def prepare_measured(self, wt, cand, runner=None, only=None, order=False, shard=None, host=None):
        self.calls.append(("prepare", cand) + ((only,) if only else ()) + (("order",) if order else ())
                          + ((shard,) if shard else ()) + (("host", host) if host else ()))
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
    assert p.circle_cfg()["max_in_flight"] == 2, "D102: min(roster max_in_flight 2, gha MAX_IN_FLIGHT 2)"
    p.cfg["circleci"]["max_in_flight"] = 12
    assert p.circle_cfg()["max_in_flight"] == 2, \
        ("D102 corrected 2026-09-20: the gha constant is 2, not 3. D112 pins a FULL proof to one of two "
         "hosts, so by pigeonhole any cap above 2 puts two full pipelines on one 12-runner box -- the "
         "configuration measured ~20% worse than serial")
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


def test_d112_full_proofs_pin_the_least_loaded_host_and_only_runs_keep_the_shared_label(tmp_path):
    wt, base, cand = repo(tmp_path)
    green = {"jobs": [{"id": "j1", "name": "vp/agent", "status": "success", "job_number": 7}],
             "failed_tests": {}, "workflows": [{"id": "1", "status": "success"}]}
    gha = FakeGha(green)
    seen = []
    real_poll = gha.poll
    def poll(*a, **k):
        seen.append(dict(p.host_active))
        return real_poll(*a, **k)
    gha.poll = poll
    cfg = {"hosted": {"provider": "gha"},
           "circleci": {"enabled": True, "mode": "all", "kinds": ["platform"], "account": "A1",
                        "delete_branch_after": True, "hosts": ["voicepod-a", "voicepod-b"]}}
    p = make_proof(tmp_path, gha, FakeExec({}), cfg=cfg)
    rec = p.run("L06", "proof-L06-1", wt, base, cand, "platform", [])
    assert rec["host"] == "voicepod-a" and ("prepare", cand, "host", "voicepod-a") in gha.calls
    assert seen[-1] == {"voicepod-a": 1} and p.host_active == {"voicepod-a": 0}
    p.host_active = {"voicepod-a": 1}                     # one full proof already on -a
    rec = p.run("L06", "proof-L06-2", wt, base, cand, "platform", [])
    assert rec["host"] == "voicepod-b", "least loaded wins"
    rec = p.run("R-X", "proof-R-X-1", wt, base, cand, "platform", [], only="portal")
    assert rec["host"] is None and not any(c[:1] == ("prepare",) and "host" in c for c in gha.calls[-1:])
    del p.cfg["circleci"]["hosts"]
    rec = p.run("L06", "proof-L06-3", wt, base, cand, "platform", [])
    assert rec["host"] is None, "no roster hosts -> the shared label"


def test_d111_the_box_proof_lints_the_lanes_changed_platform_files_first(tmp_path):
    """D111: `ruff check` on the diff's platform .py files before the suite; a
    red is FAIL_PRODUCT with lint:: node ids and the suite is not run; clean
    -> the suite runs as before; no ruff in the worktree -> skipped, never red."""
    wt, base, cand = repo(tmp_path)
    rows = [{"filename": str(wt / "platform" / "hello.py"), "code": "F401",
             "message": "`os` imported but unused", "location": {"row": 1, "column": 8}}]
    ex = FakeExec({"ruff": (1, json.dumps(rows), "")})
    logs = []
    p = make_proof(tmp_path, FakeCircle({}), ex, cfg={"circleci": {"enabled": False}}, logs=logs)
    rec = p.run("R-X", "proof-R-X-1", wt, base, cand, "platform", ["platform/tests/test_mine.py"])
    assert rec["status"] == "PASS"                      # no ruff binary in this worktree: lint skipped, suite ran
    assert not any(str(c[0]).endswith("/ruff") for c in ex.calls)
    assert any("no platform/.venv/bin/ruff" in m for m in logs)
    ruff = wt / "platform" / ".venv" / "bin" / "ruff"
    ruff.parent.mkdir(parents=True)
    ruff.write_text("#!/bin/sh\n")
    rec = p.run("R-X", "proof-R-X-2", wt, base, cand, "platform", ["platform/tests/test_mine.py"])
    assert rec["status"] == "FAIL_PRODUCT" and rec["route"] == "box"
    assert rec["failed_nodes"] == ["lint::platform/hello.py:1 F401 `os` imported but unused"]
    call = next(c for c in ex.calls if str(c[0]).endswith("/ruff"))
    assert call[1:4] == ["check", "--output-format", "json"] and sorted(call[5:]) == ["hello.py", "tests/test_mine.py"]
    assert not any("vpproof.py" in str(a) for c in ex.calls[-1:] for a in c), "the suite did not run"
    assert rec["counts"]["lint_files"] == ["platform/hello.py", "platform/tests/test_mine.py"]
    # clean -> the suite runs
    ex.script["ruff"] = (0, "[]", "")
    rec = p.run("R-X", "proof-R-X-3", wt, base, cand, "platform", ["platform/tests/test_mine.py"])
    assert rec["status"] == "PASS" and any("vpproof.py" in str(a) for a in ex.calls[-1])
    assert any("2 changed platform file(s) clean (D111)" in m for m in logs)
    # roster off-switch
    p.cfg["lint_changed"] = False
    ex.script["ruff"] = (1, json.dumps(rows), "")
    rec = p.run("R-X", "proof-R-X-4", wt, base, cand, "platform", ["platform/tests/test_mine.py"])
    assert rec["status"] == "PASS"


def test_d114_a_targeted_platform_proof_overflows_to_one_hosted_platform_targeted_job(tmp_path):
    """D114 (§124): with the roster listing "targeted" and the box slot busy, a
    targeted platform proof runs hosted as ONE platform-targeted job over its
    paths (only=targeted:<n>:<paths>, counted against the scoped cap, any host);
    the D111 lint gate runs on the Mac first; when the scoped cap is full too it
    queues on the box as before; box free -> box as before."""
    wt, base, cand = repo(tmp_path)
    green = {"jobs": [{"id": "j1", "name": "vp/platform-targeted", "status": "success", "job_number": 9}],
             "failed_tests": {}, "workflows": [{"id": "1", "status": "success"}]}
    gha = FakeGha(green)
    ex = FakeExec({})
    logs = []
    cfg = {"hosted": {"provider": "gha"}, "targeted_workers": 4,
           "circleci": {"enabled": True, "mode": "overflow", "kinds": ["full", "targeted"], "account": "A1",
                        "max_only_in_flight": 2, "delete_branch_after": True}}
    p = make_proof(tmp_path, gha, ex, cfg=cfg, logs=logs)
    paths = ["platform/tests/test_a.py", "platform/tests/test_b.py::test_x"]
    # box free -> box
    rec = p.run("R-X", "proof-R-X-1", wt, base, cand, "platform", paths)
    assert rec["route"] == "box" and not [c for c in gha.calls if c[0] == "trigger"]
    # box busy -> hosted targeted job
    p.box_active = 1
    rec = p.run("R-X", "proof-R-X-2", wt, base, cand, "platform", paths)
    only = "targeted:4:platform/tests/test_a.py,platform/tests/test_b.py::test_x"
    assert rec["status"] == "PASS" and rec["route"] == "gha" and rec["only"] == only and rec["paths"] == paths
    assert rec["host"] is None, "a scoped job keeps the shared label"
    assert ("prepare", cand, only) in gha.calls
    assert any("D114 overflow: box busy (1/1) -> hosted platform-targeted job: 2 path(s), -n 4" in m for m in logs)
    assert p.circle_active == 0 and p.only_active == 0, "counted as a scoped job, released"
    # scoped cap full -> box (queue), never a hold
    p.only_active = 2
    n = len([c for c in gha.calls if c[0] == "trigger"])
    rec = p.run("R-X", "proof-R-X-3", wt, base, cand, "platform", paths)
    assert rec["route"] == "box" and len([c for c in gha.calls if c[0] == "trigger"]) == n
    assert any("D114 overflow: 2 scoped job(s) >= max_only_in_flight 2 -> box" in m for m in logs)
    p.only_active = 0
    # the D111 lint gate is a Mac pre-step: a red never reaches the fleet
    rows = [{"filename": str(wt / "platform" / "hello.py"), "code": "F401", "message": "unused",
             "location": {"row": 1, "column": 8}}]
    ruff = wt / "platform" / ".venv" / "bin" / "ruff"
    ruff.parent.mkdir(parents=True)
    ruff.write_text("#!/bin/sh\n")
    ex.script["ruff"] = (1, json.dumps(rows), "")
    rec = p.run("R-X", "proof-R-X-4", wt, base, cand, "platform", paths)
    assert rec["status"] == "FAIL_PRODUCT" and rec["failed_nodes"] == ["lint::platform/hello.py:1 F401 unused"]
    assert len([c for c in gha.calls if c[0] == "trigger"]) == n and "D114" in rec["reason"]
    # a full platform suite is untouched by D114 (still the full pipeline)
    ex.script["ruff"] = (0, "[]", "")
    rec = p.run("R-X", "proof-R-X-5", wt, base, cand, "platform", [])
    assert rec["route"] == "gha" and rec.get("only") is None
    # §125: mode_by_kind {platform: all} makes hosted primary for platform even with the box free;
    # agent stays overflow
    p.cfg["circleci"]["mode_by_kind"] = {"platform": "all"}
    p.cfg["circleci"]["kinds"] = ["full", "platform"]
    p.box_active = 0
    assert p.route("platform", "targeted") == ("gha", "mode all (mode_by_kind.platform)")
    assert p.route("agent", "targeted")[0] == "box"
    rec = p.run("R-X", "proof-R-X-6", wt, base, cand, "platform", paths)
    assert rec["route"] == "gha" and rec["only"] == only
    # §125: a hosted FAIL_INFRA on the targeted job -> the box answers, no strike
    gha.res = {"jobs": [{"id": "j1", "name": "vp/platform-targeted", "status": "failed", "job_number": 10}],
                  "failed_tests": {}, "workflows": [{"id": "1", "status": "failed"}]}
    rec = p.run("R-X", "proof-R-X-7", wt, base, cand, "platform", paths)
    assert rec["route"] == "box" and rec["status"] == "PASS"
    assert any("hosted targeted job FAIL_INFRA" in m and "the box answers (§125)" in m for m in logs)


def test_d117_an_open_pipeline_is_repolled_only_for_the_same_only_set(tmp_path):
    """D117 (§136): D81/D93 re-poll matched by sha alone, so under D113 a twin
    adopted a sibling twin's open pipeline that never ran its files (5 VERIFIED
    on borrowed answers).  Now: equal `only` only (full for full, one scoped set
    for the identical set); a twin ask may adopt an open FULL run; a scoped set
    never answers another scoped set or a full ask.  Ledger rows carry `only`."""
    wt, base, cand = repo(tmp_path)
    circle = FakeCircle({"jobs": [{"id": "j2", "name": "lint", "status": "success", "job_number": 1}],
                         "failed_tests": {}, "workflows": [{"id": "w1", "status": "success"}]})
    p = make_proof(tmp_path, circle, FakeExec({}))
    ledger = tmp_path / "run" / "proofs" / "circleci-pipelines.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    a = "twin:3:platform/tests/test_a.py"
    b = "twin:3:platform/tests/test_b.py"
    rows = [{"ts": "2026-09-20T17:11:00.000Z", "proof_id": "proof-A-HOSTED-1", "pipeline_id": "1001", "account": "gha",
             "sha": cand, "status": "triggered", "only": a},
            {"ts": "2026-09-20T17:12:00.000Z", "proof_id": "proof-FULL-1", "pipeline_id": "1002", "account": "gha",
             "sha": cand, "status": "triggered", "only": None}]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert p.triggered_pipeline(cand, only=a)["pipeline_id"] == "1001"
    got = p.triggered_pipeline(cand, only=b)
    assert got["pipeline_id"] == "1002", "a sibling's scoped run is never adopted; the open FULL run is"
    assert p.triggered_pipeline(cand)["pipeline_id"] == "1002", "a full ask takes the full run only"
    assert p.triggered_pipeline(cand, only="targeted:3:platform/tests/test_b.py") is None, "targeted: adopts nothing else"
    # without the full row, a different scoped set finds nothing -> a fresh trigger
    ledger.write_text(json.dumps(rows[0]) + "\n")
    assert p.triggered_pipeline(cand, only=b) is None
    # the same rule on an OPEN proof record (status UNKNOWN)
    d = tmp_path / "run" / "proofs"
    (d / "proof-A-HOSTED-1.json").write_text(json.dumps({"proof_id": "proof-A-HOSTED-1", "sha": cand, "route": "gha",
                                                         "pipeline_id": "1001", "status": "UNKNOWN", "only": a,
                                                         "ts": "2026-09-20T17:13:00.000Z"}))
    assert p.triggered_pipeline(cand, only=a)["pipeline_id"] == "1001"
    assert p.triggered_pipeline(cand, only=b) is None and p.triggered_pipeline(cand) is None
    # a pre-D117 ledger row (no `only` key) reads as a full run
    ledger.write_text(json.dumps({"ts": "2026-09-20T17:14:00.000Z", "proof_id": "proof-OLD", "pipeline_id": "1003",
                                  "account": "gha", "sha": cand, "status": "triggered"}) + "\n")
    assert p.triggered_pipeline(cand)["pipeline_id"] == "1003" and p.triggered_pipeline(cand, only=b)["pipeline_id"] == "1003"
    assert p.triggered_pipeline(cand, only="portal") is None
    # a live trigger writes `only` into the ledger row
    gha = FakeGha({"jobs": [{"id": "j1", "name": "vp/platform-twin", "status": "success", "job_number": 7}],
                   "failed_tests": {}, "workflows": [{"id": "1", "status": "success"}]})
    p2 = make_proof(tmp_path / "two", gha, FakeExec({}), cfg={"hosted": {"provider": "gha"},
                                                            "circleci": {"enabled": True, "kinds": ["full"], "account": "A1"}})
    p2.run("A-HOSTED", "proof-A-HOSTED-2", wt, base, cand, "platform", [], only=a)
    row = [json.loads(l) for l in (tmp_path / "two" / "run" / "proofs" / "circleci-pipelines.jsonl").read_text().splitlines()][-1]
    assert row["status"] == "triggered" and row["only"] == a


def test_d118b_a_scoped_pass_that_collected_zero_tests_is_fail_infra(tmp_path):
    """D118b (§137): the twin job over portal .test.tsx files ran `uv run pytest`
    on nothing and came back green -- a scoped PASS must carry a junit case
    count; zero collected = FAIL_INFRA (never reused, never VERIFIED)."""
    wt, base, cand = repo(tmp_path)
    green = {"jobs": [{"id": "j1", "name": "vp/platform-twin", "status": "success", "job_number": 7}],
             "failed_tests": {}, "workflows": [{"id": "1", "status": "success"}]}
    gha = FakeGha(green)
    gha.totals = {7: 0}
    gha.junit_totals = lambda run_id, jobs, runner: {j["job_number"]: gha.totals.get(j["job_number"], 0) for j in jobs}
    cfg = {"hosted": {"provider": "gha"}, "circleci": {"enabled": True, "kinds": ["full"], "account": "A1"}}
    logs = []
    p = make_proof(tmp_path, gha, FakeExec({}), cfg=cfg, logs=logs)
    only = "twin:3:portal/src/App.test.tsx"
    rec = p.run("L20-HOSTED", "proof-L20-1", wt, base, cand, "portal", [], only=only)
    assert rec["status"] == "FAIL_INFRA" and rec["tests_collected"] == 0 and "D118b" in rec["reason"]
    assert any("scoped job collected 0 tests" in m for m in logs)
    gha.totals = {7: 12}
    rec = p.run("L20-HOSTED", "proof-L20-2", wt, base, cand, "platform", [], only="twin:3:platform/tests/test_a.py")
    assert rec["status"] == "PASS" and rec["tests_collected"] == 12
    # a full run is not counted (no junit download on green jobs)
    rec = p.run("L20-HOSTED", "proof-L20-3", wt, base, cand, "platform", [])
    assert rec["status"] == "PASS" and rec["tests_collected"] is None


def test_d135_two_concurrent_full_proofs_never_reserve_the_same_host(tmp_path):
    """D135: `pick_host` read `host_active` under the lock, RELEASED it, and the
    `+= 1` happened in a later lock block -- so two proof threads arriving in that
    window both read the same counts and both chose the same host.

    2026-09-21 00:53Z it cost two whole pipelines: 35549017476 and 35549017480 each
    put all 15 jobs on voicepod-c, 30 shard jobs and their per-worker Postgres
    clusters on one box's 12G /dev/shm. The clusters died mid-run and 3732 nodes
    reddened across the two runs, not one of them a product defect.

    The cap was 2 and there were 2 hosts, so the per-box guarantee everyone
    reasoned from never held: the pigeonhole argument is about the CAP, this is
    about the CHOICE.

    This test must RACE. Called one after another, the old code also returns a, b
    -- the defect is invisible to a sequential test, which is why it survived.
    """
    import threading

    cc = {"hosts": ["voicepod-a", "voicepod-b", "voicepod-c"]}
    p = make_proof(tmp_path, FakeGha({}), FakeExec({}))
    p.host_active = {}

    # every thread must be inside pick/reserve at once, or nothing is being raced
    n = len(cc["hosts"])
    at_the_gate = threading.Barrier(n)
    got, lock = [], threading.Lock()

    def claim():
        at_the_gate.wait(timeout=10)
        h = p.reserve_host(cc)
        with lock:
            got.append(h)

    threads = [threading.Thread(target=claim) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len(got) == n, "a thread did not finish: %r" % (got,)
    assert sorted(got) == sorted(cc["hosts"]), (
        "two full proofs reserved the same host: %r -- this is the 2026-09-21 "
        "/dev/shm cluster death" % (got,))
    assert p.host_active == {h: 1 for h in cc["hosts"]}, p.host_active

    # a reservation is a SLOT, so the next caller goes round again rather than
    # piling onto whichever host happens to sort first
    assert p.reserve_host(cc) == "voicepod-a"
    assert p.host_active["voicepod-a"] == 2

    # pick_host stays a pure query: it reports, it never takes
    before = dict(p.host_active)
    assert p.pick_host(cc) in ("voicepod-b", "voicepod-c")
    assert p.host_active == before, "pick_host must not reserve"

    # no hosts listed = the shared label = nothing to reserve
    assert p.reserve_host({"hosts": []}) is None
    assert p.pick_host({}) is None


def test_d135_run_circleci_reserves_its_host_and_does_not_increment_again():
    """The defect was at the CALL SITE, not inside the function, so this is the
    assertion that actually bites.

    `reserve_host` is atomic by construction -- racing it can never fail, so the
    race test above cannot distinguish the fix from the bug. Measured: with the
    real window between the two lock acquisitions (`triggered_pipeline` globs and
    parses every proof record), the old two-phase sequence collided 40/40 and the
    atomic one 0/40. Reverting `run_circleci` to `pick_host` would restore that
    exact 40/40 while every other test here stayed green.

    Also guards the other half: `reserve_host` already took the slot, so a second
    `+= 1` in run_circleci would double-count it, and the single decrement on the
    release path would leave the host permanently 'busy' -- the host would then
    never be chosen again and the fleet would quietly shrink.
    """
    import inspect
    src = inspect.getsource(laneproof.Proof.run_circleci)
    head = src.split("try:", 1)[0]
    assert "self.reserve_host(cc)" in head, (
        "run_circleci must RESERVE its host, not merely pick one -- see the "
        "2026-09-21 /dev/shm cluster death (pipelines 35549017476 and 35549017480 "
        "both put 15 jobs on voicepod-c)")
    assert "self.pick_host(" not in src, (
        "run_circleci is using the query form again: pick_host reserves nothing, "
        "so two callers in the window both get the same host")
    # ban the PROPERTY, not the token: the release path legitimately writes
    # `host_active[host] = max(0, ... - 1)`, so a bare `host_active[host] =` ban
    # reddens on correct code. Only the INCREMENT is forbidden here.
    assert "self.host_active.get(host, 0) + 1" not in src, (
        "run_circleci increments host_active itself -- reserve_host already took "
        "the slot, so this double-counts and the single decrement on release "
        "leaves the host permanently 'busy', shrinking the fleet silently")
    assert "max(0, self.host_active.get(host, 0) - 1)" in src, (
        "the release path is gone: a reserved slot that is never freed is the "
        "same fleet-shrinking bug from the other direction")


def _open_full_record(run_root, cand, pipeline_id="pipe-206", proof_id="proof-full"):
    """an OPEN full-suite record for `cand`: what open_answers() lets a twin adopt"""
    d = run_root / "proofs"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("%s.json" % proof_id)).write_text(json.dumps({
        "status": "UNKNOWN", "route": "circleci", "proof_id": proof_id, "sha": cand,
        "pipeline_id": pipeline_id, "account": "3", "only": None, "ts": "2026-09-21T01:38:00Z"}))


def test_d140_a_twin_adopting_a_full_run_is_not_charged_that_run_s_other_failures(tmp_path):
    """D140: `open_answers` deliberately lets a `twin:` ask adopt an open FULL
    run, and that adoption is sound -- a full run really does execute the twin's
    paths. What was not sound is keeping the full run's WHOLE failure set as the
    twin's answer.

    Measured 2026-09-21 on pipeline 35551631471: four twins adopted one full run
    and every one of them was charged the same six nodes. L26-HOSTED-R3 asked
    for six platform billing/export files and was handed a portal node from a
    `vp/portal` job. `twin_job()`'s own docstring says its record "answers the
    twin's own ask only"; nothing enforced it.

    The status downgrade is gated on the adopted run being FULL, because that is
    what makes "none of mine failed" mean "mine passed" -- absence of a failure
    is not evidence of execution."""
    wt, base, cand = repo(tmp_path)
    circle = FakeCircle(pipeline([
        ("portal/src/routes/Settings.test.tsx", "Settings members panel > saves business hours"),
        ("platform/tests/test_campaign_admission_db.py", "test_a_two_role_multi_action_launch"),
        ("platform/tests/test_l17_reply_fence_derivation_db.py", "test_withdrawing_the_consent_grant"),
    ]))
    ex = FakeExec({})
    logs = []
    p = make_proof(tmp_path, circle, ex, logs=logs)
    _open_full_record(tmp_path / "run", cand)

    only = "twin:3:platform/tests/test_billing_control.py,platform/tests/test_export_formats.py"
    rec = p.run("L26-HOSTED-R3", "proof-twin", wt, base, cand, "platform",
                ["platform/tests/test_billing_control.py"], only=only)

    assert rec["failed_nodes"] == [], (
        "a twin must not be charged nodes outside its own only= scope: %r" % rec["failed_nodes"])
    assert len(rec["out_of_scope_failed"]) == 3, rec["out_of_scope_failed"]
    assert any("Settings.test.tsx" in str(n) for n in rec["out_of_scope_failed"])
    assert rec["status"] == "PASS", (
        "the adopted run was FULL, so it ran this twin's paths and none of them failed: %s"
        % rec["status"])
    assert any("outside only=" in m for m in logs)


def test_d140_an_in_scope_red_still_fails_the_twin(tmp_path):
    """D140 control. The filter must not become a way for a twin to pass while
    its own files are red -- that would turn a real verdict into an unfailable
    one, which is worse than the bug. One node inside the scope, and the twin
    fails on exactly that node and no other."""
    wt, base, cand = repo(tmp_path)
    circle = FakeCircle(pipeline([
        ("portal/src/routes/Settings.test.tsx", "Settings members panel > saves business hours"),
        ("platform/tests/test_billing_control.py", "test_the_twins_own_file_is_red"),
    ]))
    p = make_proof(tmp_path, circle, FakeExec({}))
    _open_full_record(tmp_path / "run", cand)

    only = "twin:3:platform/tests/test_billing_control.py"
    rec = p.run("L26-HOSTED-R3", "proof-twin", wt, base, cand, "platform",
                ["platform/tests/test_billing_control.py"], only=only)

    assert rec["failed_nodes"] == [
        "platform/tests/test_billing_control.py::test_the_twins_own_file_is_red"], rec["failed_nodes"]
    assert rec["status"] == "FAIL_PRODUCT", rec["status"]
    assert [str(n) for n in rec["out_of_scope_failed"]] == [
        "portal/src/routes/Settings.test.tsx::Settings members panel > saves business hours"]


def test_d140_scope_matching_compares_path_parts_not_substrings():
    """D140: the two sides spell the same file differently -- the spec says
    `platform/tests/test_x.py`, the junit node says `tests/test_x.py`, because
    the job runs with platform/ as its working directory. Matching has to work
    in both directions.

    The last case is the one that matters: a bare substring test would let
    `test_export.py` swallow `test_export_formats.py`, silently widening every
    scope it is applied to."""
    ok = laneproof.Proof.node_in_scope
    assert ok("tests/test_x.py::case", ["platform/tests/test_x.py"])
    assert ok("platform/tests/test_x.py::case", ["tests/test_x.py"])
    assert ok("platform/tests/test_x.py::case", ["platform/tests/test_x.py"])
    assert not ok("src/routes/Settings.test.tsx::case", ["platform/tests/test_x.py"])
    assert not ok("tests/test_export_formats.py::case", ["platform/tests/test_export.py"])
    assert not ok("tests/test_x.py::case", [])


def test_d140_adopting_a_scoped_run_filters_but_does_not_downgrade(tmp_path):
    """D140 gate control.

    Filtering the node list is always right -- those nodes are not this ask's to
    answer however the run was produced. Turning FAIL_PRODUCT into PASS is only
    right when something PROVED this twin's paths actually ran, and the only
    thing that proves it is an adopted FULL run.

    Here the twin adopts an open run with the IDENTICAL scope (which
    open_answers allows on an exact match). Everything red is outside that
    scope, so nothing is charged -- but `adopted_full` is false, so the verdict
    stands. "Not my failure" and "I passed" are different claims, and only one
    of them is supported. Deleting the `adopted_full` gate reds this and nothing
    else."""
    wt, base, cand = repo(tmp_path)
    only = "twin:3:platform/tests/test_billing_control.py"
    circle = FakeCircle(pipeline([
        ("portal/src/routes/Settings.test.tsx", "Settings members panel > saves business hours"),
    ]))
    p = make_proof(tmp_path, circle, FakeExec({}))
    d = (tmp_path / "run" / "proofs")
    d.mkdir(parents=True, exist_ok=True)
    (d / "proof-scoped.json").write_text(json.dumps({
        "status": "UNKNOWN", "route": "circleci", "proof_id": "proof-scoped", "sha": cand,
        "pipeline_id": "pipe-206", "account": "3", "only": only, "ts": "2026-09-21T01:38:00Z"}))

    rec = p.run("L26-HOSTED-R3", "proof-twin", wt, base, cand, "platform",
                ["platform/tests/test_billing_control.py"], only=only)

    assert rec["failed_nodes"] == [], rec["failed_nodes"]
    assert len(rec["out_of_scope_failed"]) == 1, rec["out_of_scope_failed"]
    assert rec["status"] != "PASS", (
        "the adopted run was scoped, so nothing established that these paths ran: %s" % rec["status"])


def test_d141_adopting_an_open_pipeline_does_not_take_an_in_flight_slot(tmp_path):
    """D141: `circle_active` gates `max_in_flight`, a cap whose purpose is to
    limit concurrent PIPELINES. But run_circleci increments it before the
    `if prior:` branch decides whether it will trigger anything, and a D81
    adoption triggers nothing -- the log says "no new trigger (D81)". So the cap
    was throttling waiting, which is free, rather than triggering, which costs
    runners. Four proofs re-polled pipeline 35551631471 on 2026-09-21, and while
    they waited the counter read full and real triggers were refused
    BLOCKED_CAP against an idle fleet.

    The observation has to happen DURING the run. The finally always returns the
    counter to 0, so a before/after assertion passes for the broken code too --
    an unfailable test. This captures the count from inside poll(), which is
    exactly when the adopted pipeline is being waited on."""
    wt, base, cand = repo(tmp_path)
    seen = {}

    class WatchingCircle(FakeCircle):
        def poll(self, pipeline_id, interval, deadline_s, runner, account, abort, targets=None):
            seen["circle_active"] = self.proof.circle_active
            return FakeCircle.poll(self, pipeline_id, interval, deadline_s, runner, account,
                                   abort, targets)

    circle = WatchingCircle(pipeline([]))
    p = make_proof(tmp_path, circle, FakeExec({}))
    circle.proof = p
    _open_full_record(tmp_path / "run", cand)

    assert p.circle_active == 0
    p.run("L26-HOSTED", "proof-adopt", wt, base, cand, "platform", ["platform/tests"])

    assert seen.get("circle_active") == 0, (
        "an adopted pipeline triggers nothing, so it must hold no in-flight slot while it "
        "waits -- saw %r during the poll" % seen.get("circle_active"))
    assert p.circle_active == 0, "and the count must still balance afterwards"


def test_d140b_a_malformed_scope_spec_cannot_answer_a_scoped_ask(tmp_path):
    """D140b: `only` has THREE legitimate forms, not two -- twin:<n>:<paths>,
    targeted:<n>:<paths>, and a bare workflow job or shard name (§95 item 2,
    e.g. only="portal"). Only a ValueError is unambiguous: the prefix matched
    and the body did not. That is a driver bug, and the ask is then scoped to
    something we cannot derive, so it has not been answered.

    The claim fails closed while the run survives -- FAIL_INFRA, this file's
    idiom for "not an answer" (D79b cancelled, D118b collected-zero) -- so the
    driver retries instead of charging the packet a verdict nobody scoped. A
    record FIELD would not have done: an earlier draft wrote a `scope_filter`
    label that nothing consumed, so the verdict still landed and still answered
    the scoped ask. A guard is something that REFUSES
    ([[gate-on-the-property-not-the-artifact]])."""
    wt, base, cand = repo(tmp_path)
    circle = FakeCircle(pipeline([
        ("platform/tests/test_something.py", "test_not_this_ask_s_file"),
    ]))
    alerts = []
    p = make_proof(tmp_path, circle, FakeExec({}), alerts=alerts)
    _open_full_record(tmp_path / "run", cand)

    # `twin:` so open_answers permits adopting the open FULL run (no render, so
    # the malformed body reaches run_circleci); `x` is not a worker count.
    rec = p.run("L26-HOSTED", "proof-bad", wt, base, cand, "platform",
                ["platform/tests/test_billing_control.py"], only="twin:x:platform/tests/a.py")

    assert rec["status"] == "FAIL_INFRA", (
        "an ask whose scope cannot be derived has not been answered: %s" % rec["status"])
    assert rec.get("unscoped") and "does not parse" in rec["unscoped"], rec.get("unscoped")
    assert "not an answer" in (rec.get("reason") or ""), rec.get("reason")
    assert any(k == "PROOF_SCOPE_UNPARSED" for k, _ in alerts), alerts


def test_d140b_a_bare_job_name_only_is_legitimate_and_must_not_be_failed(tmp_path):
    """D140b control, and the one that caught my own over-reach.

    §95 item 2 allows `only=<job|shard>` -- a bare workflow job name with no
    prefix and no paths. `scoped_spec` returns None for it, correctly. My first
    draft read "returns None" as "typo'd prefix" and turned every job-level
    only= proof into FAIL_INFRA; the existing `test_fleet1_...` and `test_d98_...`
    caught it immediately.

    This pins the third vocabulary explicitly so the next person tightening this
    check sees it named rather than rediscovering it from a red suite.
    [[a-validity-check-needs-the-whole-vocabulary]]"""
    import vpgha_overlay

    assert vpgha_overlay.scoped_spec("portal") is None
    assert vpgha_overlay.scoped_spec("platform-shards-3") is None
    assert vpgha_overlay.scoped_spec("twin:3:a.py") is not None
    assert vpgha_overlay.scoped_spec("targeted:2:a.py,b.py") is not None

    wt, base, cand = repo(tmp_path)
    # a bare job-name only= needs a provider that renders the workflow, as
    # test_fleet1_only_runs_have_their_own_in_flight_cap does
    green = {"jobs": [{"id": "j1", "name": "vp/portal", "status": "success", "job_number": 7}],
             "failed_tests": {}, "workflows": [{"id": "1", "status": "success"}]}
    alerts = []
    cfg = {"hosted": {"provider": "gha"},
           "circleci": {"enabled": True, "mode": "all", "kinds": ["platform"], "account": "A1",
                        "delete_branch_after": True}}
    p = make_proof(tmp_path, FakeGha(green), FakeExec({}), cfg=cfg, alerts=alerts)
    rec = p.run("R-X", "proof-job", wt, base, cand, "platform", [], only="portal")

    assert rec["status"] == "PASS", (
        "a bare job name is a legitimate only= form and must not be treated as "
        "an underivable scope: %s / %s" % (rec["status"], rec.get("unscoped")))
    assert rec.get("unscoped") is None
    assert not [k for k, _ in alerts if k == "PROOF_SCOPE_UNPARSED"], alerts
