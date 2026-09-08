#!/usr/bin/env python3
"""Exercise the actual launcher with conflicting inherited/default credentials."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import circleaccount as c


class Accounts(unittest.TestCase):
    def fake(self, actual):
        self.calls = []
        def run(argv, **kw):
            self.calls.append((argv, kw))
            if argv[0] == "security":
                return subprocess.CompletedProcess(argv, 0, "test-selected-secret\n", "")
            if "auth" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"id": actual}), "")
            return subprocess.CompletedProcess(argv, 0)
        return run

    def test_accounts_use_selected_token_and_verify_before_command(self):
        for account, expected in c.EXPECTED.items():
            base = {"CIRCLE_TOKEN": "wrong-shared-token", "CIRCLE_HOST": "https://wrong.example"}
            self.assertEqual(c.execute(account, ["org", "list"], run=self.fake(expected),
                                       base=base, binary="circleci"), 0)
            self.assertEqual(len(self.calls), 3)
            self.assertIn(f"voicepod-circleci-account-{account}", self.calls[0][0])
            env = self.calls[2][1]["env"]
            self.assertEqual(env["CIRCLE_TOKEN"], "test-selected-secret")
            self.assertEqual(env["CIRCLE_HOST"], "https://circleci.com")
            self.assertEqual(base["CIRCLE_TOKEN"], "wrong-shared-token")
            self.assertNotIn("test-selected-secret", str(self.calls[2][0]))

    def test_wrong_account_cannot_dispatch(self):
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            c.execute("1", ["run", "trigger"], run=self.fake(c.EXPECTED["3"]), binary="circleci")
        self.assertEqual(len(self.calls), 2)

    def test_missing_key_fails_before_network(self):
        def run(argv, **kwargs):
            self.assertEqual(argv[0], "security")
            return subprocess.CompletedProcess(argv, 1, "", "secret-bearing-error")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            c.execute("1", ["org", "list"], run=run, binary="circleci")

    def test_no_shared_login_or_config_override(self):
        for command in (["auth", "login"], ["run", "list", "--config=elsewhere"], ["--debug"],
                        ["--debug=true", "auth", "login"], ["-celsewhere", "org", "list"],
                        ["org", "list", "--host=https://wrong.example"],
                        ["org", "list", "--token=other"], ["api", "https://wrong.example"]):
            with self.assertRaises(ValueError):
                c.execute("1", command)


if __name__ == "__main__":
    unittest.main()
