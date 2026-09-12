#!/usr/bin/env python3
"""Tests for vpcircle.py -- fake Runner only, never the real circleci CLI or
the network, and never a real pipeline trigger."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import vpcircle as vc  # noqa: E402
import circleaccount as c  # noqa: E402


_ACCOUNT_RE = re.compile(r"circleci-account-(\d+)")


def _account_from_argv(argv):
    for arg in argv:
        m = _ACCOUNT_RE.search(str(arg))
        if m:
            return m.group(1)
    raise AssertionError(f"no account marker in argv: {argv}")


def _is_auth_me(argv):
    return "auth" in argv and "me" in argv


def _api_path(argv):
    if "api" not in argv:
        return None
    return argv[argv.index("api") + 1]


class FakeRun:
    """Fakes subprocess.run for both circleaccount's identity check calls
    (security / auth me) and the actual `circleci api ...` invocation.

    `api_handlers` maps an account id -> callable(path, argv) -> (rc,
    stdout, stderr), or a single callable(account, path, argv) -> that
    3-tuple used for every account. `identity_ok` (default True) controls
    whether `auth me` reports the expected id for the account in the
    config path, so verified_env's identity check passes.
    """

    def __init__(self, api_handler, identity_ok=True, secret="fake-secret"):
        self.calls = []
        self.api_handler = api_handler
        self.identity_ok = identity_ok
        self.secret = secret

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if argv[0] == "security":
            return subprocess.CompletedProcess(argv, 0, f"{self.secret}\n", "")
        if _is_auth_me(argv):
            account = _account_from_argv(argv)
            reported = c.EXPECTED[account] if self.identity_ok else "someone-else"
            return subprocess.CompletedProcess(argv, 0, json.dumps({"id": reported}), "")
        path = _api_path(argv)
        if path is not None:
            account = _account_from_argv(argv)
            rc, out, err = self.api_handler(account, path, argv)
            return subprocess.CompletedProcess(argv, rc, out, err)
        raise AssertionError(f"unexpected argv in FakeRun: {argv}")


class TriggerBodyTests(unittest.TestCase):
    def test_trigger_body_has_real_json_booleans(self):
        seen = {}

        def handler(account, path, argv):
            seen["path"] = path
            seen["payload"] = argv[argv.index("-d") + 1]
            return 0, json.dumps({"id": "pipeline-123"}), ""

        runner = vc.Runner(run=FakeRun(handler))
        result = vc.trigger(
            "vp/item1",
            {"run_full_suite": True, "dry_run": False, "test_paths": "tests/x"},
            runner=runner,
            account="1",
        )

        self.assertEqual(result, {"pipeline_id": "pipeline-123", "account": "1"})
        self.assertEqual(
            seen["path"],
            f"api/v2/project/{vc.SLUG}/pipeline/run",
        )
        body = json.loads(seen["payload"])
        self.assertEqual(body["definition_id"], vc.DEFINITION_ID)
        self.assertEqual(body["config"], {"branch": "vp/item1"})
        self.assertEqual(body["checkout"], {"branch": "vp/item1"})
        self.assertEqual(body["parameters"]["run_full_suite"], True)
        self.assertEqual(body["parameters"]["dry_run"], False)
        # Booleans must be real JSON booleans: never the literal 4/5-char
        # quoted strings "true"/"false" in the serialised payload.
        self.assertNotIn('"true"', seen["payload"])
        self.assertNotIn('"false"', seen["payload"])
        self.assertIn('"run_full_suite": true', seen["payload"])
        self.assertIn('"dry_run": false', seen["payload"])
        self.assertIsInstance(body["parameters"]["run_full_suite"], bool)
        self.assertIsInstance(body["parameters"]["dry_run"], bool)


class ClassifyTests(unittest.TestCase):
    def test_all_success_is_pass(self):
        jobs = [
            {"id": "a", "name": "lint", "status": "success", "job_number": 1},
            {"id": "b", "name": "portal", "status": "success", "job_number": 2},
        ]
        result = vc.classify(jobs, {})
        self.assertEqual(result, {"status": "PASS", "reds": []})

    def test_failed_job_with_failed_tests_is_fail_product(self):
        jobs = [
            {"id": "a", "name": "platform-shard-0", "status": "failed", "job_number": 7},
            {"id": "b", "name": "portal", "status": "success", "job_number": 8},
        ]
        failed_tests = {
            7: [{"name": "test_x", "classname": "TestFoo", "result": "failure"}],
        }
        result = vc.classify(jobs, failed_tests)
        self.assertEqual(result["status"], "FAIL_PRODUCT")
        self.assertEqual(len(result["reds"]), 1)
        self.assertEqual(result["reds"][0]["kind"], "product")
        self.assertEqual(result["reds"][0]["job_number"], 7)
        self.assertEqual(result["reds"][0]["tests"], failed_tests[7])

    def test_failed_job_with_no_failed_tests_is_fail_infra(self):
        jobs = [
            {"id": "a", "name": "platform-shard-1", "status": "failed", "job_number": 9},
        ]
        result = vc.classify(jobs, {9: []})
        self.assertEqual(result["status"], "FAIL_INFRA")
        self.assertEqual(result["reds"][0]["kind"], "infra")

    def test_infrastructure_fail_status_is_fail_infra(self):
        for status in ("infrastructure_fail", "timedout", "canceled"):
            jobs = [{"id": "a", "name": "agent", "status": status, "job_number": 3}]
            result = vc.classify(jobs, {})
            self.assertEqual(result["status"], "FAIL_INFRA", status)

    def test_not_run_after_failed_dependency_is_fail_infra(self):
        jobs = [
            {"id": "up", "name": "lint", "status": "failed", "job_number": 1},
            {"id": "down", "name": "required-gate", "status": "not_run",
             "job_number": None, "dependencies": ["up"]},
        ]
        result = vc.classify(jobs, {1: []})
        self.assertEqual(result["status"], "FAIL_INFRA")
        kinds = {r["job"]: r["kind"] for r in result["reds"]}
        self.assertEqual(kinds["required-gate"], "infra")

    def test_product_failure_dominates_over_infra(self):
        jobs = [
            {"id": "a", "name": "platform-shard-0", "status": "failed", "job_number": 1},
            {"id": "b", "name": "agent", "status": "timedout", "job_number": 2},
        ]
        result = vc.classify(jobs, {1: [{"name": "test_x", "result": "failure"}]})
        self.assertEqual(result["status"], "FAIL_PRODUCT")

    def test_unrecognised_status_is_unknown(self):
        jobs = [{"id": "a", "name": "mystery", "status": "on_hold", "job_number": None}]
        result = vc.classify(jobs, {})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reds"][0]["kind"], "unknown")


class RotationTests(unittest.TestCase):
    def test_credit_error_rotates_to_next_account_once(self):
        calls_by_account = []

        def handler(account, path, argv):
            calls_by_account.append(account)
            if account == "1":
                return 1, "", "Error: insufficient plan credits to trigger this pipeline"
            return 0, json.dumps({"id": "pipeline-after-rotation"}), ""

        runner = vc.Runner(run=FakeRun(handler))
        result = vc.trigger("vp/item2", {"run_full_suite": False}, runner=runner, account="1")

        self.assertEqual(result["pipeline_id"], "pipeline-after-rotation")
        self.assertEqual(result["account"], vc.ACCOUNTS[1])
        self.assertEqual(calls_by_account, ["1", vc.ACCOUNTS[1]])

    def test_credit_error_on_every_attempted_account_raises(self):
        def handler(account, path, argv):
            return 1, "", "plan limit reached, please add credits"

        runner = vc.Runner(run=FakeRun(handler))
        with self.assertRaises(RuntimeError):
            vc.trigger("vp/item3", {}, runner=runner, account="1")

    def test_non_credit_error_does_not_rotate(self):
        calls_by_account = []

        def handler(account, path, argv):
            calls_by_account.append(account)
            return 1, "", "500 internal server error"

        runner = vc.Runner(run=FakeRun(handler))
        with self.assertRaises(RuntimeError):
            vc.trigger("vp/item4", {}, runner=runner, account="1")
        self.assertEqual(calls_by_account, ["1"])


class PollTests(unittest.TestCase):
    def test_poll_ends_when_all_workflows_terminal(self):
        state = {"workflow_calls": 0}
        sleeps = []
        clock_values = iter([0.0, 1.0, 2.0])

        def handler(account, path, argv):
            if path == f"api/v2/pipeline/pipe-1/workflow":
                state["workflow_calls"] += 1
                if state["workflow_calls"] == 1:
                    return 0, json.dumps({"items": [{"id": "wf-1", "status": "running"}]}), ""
                return 0, json.dumps({"items": [{"id": "wf-1", "status": "success"}]}), ""
            if path == "api/v2/workflow/wf-1/job":
                return 0, json.dumps({"items": [
                    {"id": "job-a", "name": "platform-shard-0", "status": "failed",
                     "job_number": 42},
                    {"id": "job-b", "name": "lint-and-typecheck", "status": "success",
                     "job_number": 43},
                ]}), ""
            if path == f"api/v2/project/{vc.SLUG}/42/tests":
                return 0, json.dumps({"items": [
                    {"name": "test_x", "result": "failure"},
                    {"name": "test_y", "result": "success"},
                ]}), ""
            raise AssertionError(f"unexpected path {path}")

        runner = vc.Runner(run=FakeRun(handler))
        result = vc.poll(
            "pipe-1", interval=60, deadline_s=5400, runner=runner, account="1",
            sleep=lambda s: sleeps.append(s), clock=lambda: next(clock_values),
        )

        self.assertEqual(state["workflow_calls"], 2)
        self.assertEqual(sleeps, [60])
        self.assertEqual(result["pipeline_id"], "pipe-1")
        self.assertEqual(len(result["jobs"]), 2)
        self.assertIn(42, result["failed_tests"])
        self.assertEqual(len(result["failed_tests"][42]), 1)
        self.assertEqual(result["failed_tests"][42][0]["name"], "test_x")

    def test_poll_raises_timeout_error_past_deadline(self):
        def handler(account, path, argv):
            return 0, json.dumps({"items": [{"id": "wf-1", "status": "running"}]}), ""

        runner = vc.Runner(run=FakeRun(handler))
        clock_values = iter([0.0, 100.0])
        with self.assertRaises(TimeoutError):
            vc.poll(
                "pipe-2", interval=10, deadline_s=90, runner=runner, account="1",
                sleep=lambda s: None, clock=lambda: next(clock_values),
            )


class RecordTests(unittest.TestCase):
    def test_record_writes_four_files_with_ts(self, tmp_path=None):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = vc.record(
                tmp, "abc1234",
                pipeline={"pipeline_id": "pipe-1"},
                jobs=[{"name": "lint", "status": "success"}],
                failed_tests={},
                classified={"status": "PASS", "reds": []},
            )
            expect = out_dir
            self.assertEqual(expect, Path(tmp) / "proofs" / "abc1234")
            names = {"pipeline.json", "jobs.json", "tests-failed.json", "classified.json"}
            for name in names:
                path = out_dir / name
                self.assertTrue(path.exists(), name)
                doc = json.loads(path.read_text())
                self.assertIn("ts", doc)
                self.assertIsInstance(doc["ts"], int)
                self.assertGreater(doc["ts"], 1_700_000_000_000)  # sane UTC-ms


if __name__ == "__main__":
    unittest.main()
