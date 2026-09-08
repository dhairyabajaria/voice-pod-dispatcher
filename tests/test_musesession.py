#!/usr/bin/env python3
"""Conversation identity persistence and actual managed-launch wiring; no providers."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import museadapter as m
import musesession as s

SPEC = {"profile": "muse-go-1", "provider": "muse-go-1", "model": "muse-spark-1.3-contributor",
        "effort": "xhigh", "env_key": "OPENCODE_GO_KEY_1"}
SID = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"


class Conversation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_parallel_conversations_unique_and_retries_stable(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda i: s.prepare(self.root, f"item-{i}", SPEC), range(20)))
            retries = list(pool.map(lambda _: s.prepare(self.root, "item-0", SPEC), range(20)))
        self.assertEqual(len({r["header"] for r in records}), 20)
        self.assertEqual({r["header"] for r in retries}, {records[0]["header"]})

    def test_exact_resume_and_affinity_fail_closed(self):
        record = s.prepare(self.root, "item", SPEC)
        s.bind(self.root, SID, record)
        self.assertEqual(s.prepare(self.root, "item", SPEC, session_id=SID)["header"], record["header"])
        with self.assertRaisesRegex(ValueError, "different item"):
            s.prepare(self.root, "other-item", SPEC, session_id=SID)
        with self.assertRaisesRegex(ValueError, "different item"):
            s.prepare(self.root, "item", SPEC, session_id=SID, worktree="/different")
        for key in s.ROUTE_FIELDS:
            with self.assertRaisesRegex(ValueError, "route changed"):
                s.prepare(self.root, "item", dict(SPEC, **{key: "changed"}))
        with self.assertRaisesRegex(ValueError, "no recorded"):
            s.prepare(self.root, "item", SPEC, session_id="missing")
        with self.assertRaisesRegex(ValueError, "different Muse"):
            s.bind(self.root, SID, s.prepare(self.root, "other-item", SPEC))

    def test_explicit_new_profile_placement_keeps_old_resume_binding(self):
        first = s.prepare(self.root, "item", SPEC)
        s.bind(self.root, SID, first)
        other = dict(SPEC, profile="muse-go-2", provider="muse-go-2", env_key="OPENCODE_GO_KEY_2")
        replacement = s.prepare(self.root, "item", other, replace_profile=True)
        self.assertNotEqual(first["header"], replacement["header"])
        self.assertEqual(s.prepare(self.root, "item", other)["header"], replacement["header"])
        self.assertEqual(s.prepare(self.root, "item", SPEC, session_id=SID)["header"], first["header"])
        with self.assertRaisesRegex(ValueError, "route changed"):
            s.prepare(self.root, "item", other, session_id=SID, replace_profile=True)

    def test_override_only_targets_header_leaf(self):
        record = s.prepare(self.root, "item", SPEC)
        flags = s.flags(record)
        key, value = flags[1].split("=", 1)
        self.assertEqual(key, "model_providers.muse-go-1.http_headers.x-opencode-session")
        self.assertEqual(json.loads(value), record["header"])
        self.assertNotIn("model_reasoning_effort", flags[1])
        self.assertNotIn("OPENCODE_GO_KEY", flags[1])

    def test_managed_launch_persists_before_runner_and_resume_reuses(self):
        profile = Path(self.root) / "muse-go-1.config.toml"
        content = ('model="muse-spark-1.3-contributor"\nmodel_provider="muse-go-1"\n'
                   'model_reasoning_effort="xhigh"\n[model_providers.muse-go-1]\n'
                   'env_key="OPENCODE_GO_KEY_1"\n[model_providers.muse-go-1.http_headers]\n'
                   'x-opencode-session="old-static"\nx-custom="keep-me"\n')
        profile.write_text(content)
        seen = []
        def runner(argv, env, brief, timeout):
            override = next(x for x in argv if x.startswith("model_providers."))
            persisted = list((Path(self.root) / "muse-conversations").glob("conversation-*.json"))
            self.assertTrue(persisted, "identity must precede spawn")
            header = json.loads(override.split("=", 1)[1])
            self.assertIn(header, [json.loads(p.read_text())["header"] for p in persisted])
            self.assertEqual(env["OPENCODE_GO_KEY_1"], "fake")
            self.assertFalse(any("model_reasoning_effort" in a for a in argv))
            seen.append((header, argv))
            return 1, json.dumps({"type": "thread.started", "thread_id": SID}), "error: 429 Too Many Requests", 1
        args = dict(home=self.root, env={"OPENCODE_GO_KEY_1": "fake"}, runner=runner)
        first = m.run_attempt("item", 1, "brief", self.root, "muse-go-1", **args)
        second = m.run_attempt("item", 2, "brief", self.root, "muse-go-1", session_id=SID, **args)
        self.assertEqual(first["provider_conversation"], second["provider_conversation"])
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertIn("resume", seen[1][1])
        retry = m.run_attempt("item", 3, "brief", self.root, "muse-go-1", **args)
        self.assertEqual(retry["provider_conversation"], first["provider_conversation"])
        self.assertEqual(profile.read_text(), content)
        refused = m.run_attempt("item", 3, "brief", self.root, "muse-go-1", session_id="missing", **args)
        self.assertEqual(refused["outcome"], m.CLI_CONFIG)
        cross_item = m.run_attempt("other-item", 4, "brief", self.root, "muse-go-1", session_id=SID, **args)
        self.assertEqual(cross_item["outcome"], m.CLI_CONFIG)
        self.assertEqual(len(seen), 3)


if __name__ == "__main__":
    unittest.main()
