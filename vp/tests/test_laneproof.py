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

    def __init__(self, res):
        self.res = res
        self.calls = []

    class Runner(object):
        def git(self, args, cwd=None):
            return ""

    def project_visible(self, runner, account):
        self.calls.append(("preflight", account))
        return True, ""

    def push_branch(self, wt, branch, runner):
        self.calls.append(("push", branch))

    def trigger(self, branch, params, runner, account):
        self.calls.append(("trigger", branch, params, account))
        return {"pipeline_id": "pipe-206", "account": account or "3"}

    def poll(self, pipeline_id, interval, deadline_s, runner, account, abort):
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
    assert rec["status"] == "UNKNOWN" and "credits exhausted" in rec["reason"]
