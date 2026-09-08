#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sessionstatus as s

SID = "00000000-0000-4000-8000-000000000001"
T1 = "00000000-0000-4000-8000-000000000002"
T2 = "00000000-0000-4000-8000-000000000003"


class Status(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / f"rollout-{SID}.jsonl"
        self.event("session_meta", {"id": SID})

    def event(self, kind, payload):
        with self.path.open("a") as f:
            f.write(json.dumps({"type": kind, "payload": payload, "timestamp": "2026-09-08T12:00:00Z"}) + "\n")

    def start(self, turn):
        self.event("event_msg", {"type": "task_started", "turn_id": turn})

    def test_old_completion_does_not_finish_new_turn(self):
        self.start(T1)
        self.event("event_msg", {"type": "task_complete", "turn_id": T1, "last_agent_message": "old"})
        self.start(T2)
        self.event("event_msg", {"type": "task_complete", "turn_id": T1})
        result = s.inspect(self.path, SID, T2, True)
        self.assertEqual(result["status"], "in_progress")
        self.assertNotIn("final_text", result)

    def test_recovers_exact_final_without_tool_output(self):
        self.start(T1)
        self.event("response_item", {"type": "custom_tool_call_output", "output": "secret must not appear"})
        self.event("event_msg", {"type": "task_complete", "turn_id": T1, "last_agent_message": "report ready"})
        result = s.inspect(self.path, SID, T1, True)
        self.assertEqual(result["final_text"], "report ready")
        self.assertFalse(result["product_accepted"])
        self.assertNotIn("secret", json.dumps(result))

    def test_wrong_or_missing_identity_refused(self):
        self.start(T1)
        with self.assertRaises(ValueError): s.inspect(self.path, T2)
        with self.assertRaises(ValueError): s.inspect(self.path, SID, T2)

    def test_truncated_tail_not_success(self):
        self.start(T1)
        with self.path.open("a") as f: f.write('{"type":')
        result = s.inspect(self.path, SID, T1)
        self.assertFalse(result["evidence_complete"])
        self.assertEqual(result["status"], "in_progress")

    def test_ambiguous_log_refused(self):
        (Path(self.tmp.name) / f"copy-{SID}.jsonl").write_text(self.path.read_text())
        with self.assertRaises(ValueError): s.locate(SID, [self.tmp.name])


if __name__ == "__main__": unittest.main()
