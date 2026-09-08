#!/usr/bin/env python3
"""Real tiny subprocesses: no model calls, databases, or shared task state."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import museadapter as m


class Runner(unittest.TestCase):
    def test_cwd_stdin_and_live_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "events"
            values = []
            code = ('import os,sys,time,json; '
                    'print(json.dumps({"cwd":os.getcwd(),"input":sys.stdin.read()}),flush=True); '
                    'time.sleep(0.8); print("DONE",flush=True)')
            def run():
                values.append(m.subprocess_runner([sys.executable, "-c", code], dict(os.environ),
                              "finite brief", 5, cwd=root, event_path=log))
            t = threading.Thread(target=run); t.start()
            until = time.monotonic() + 3
            while (not log.exists() or log.stat().st_size == 0) and time.monotonic() < until:
                time.sleep(0.01)
            try:
                self.assertTrue(t.is_alive(), "evidence must be readable before process completion")
                first = json.loads(log.read_text().splitlines()[0])
                self.assertEqual(Path(first["cwd"]).resolve(), Path(root).resolve())
                self.assertEqual(first["input"], "finite brief")
            finally: t.join(timeout=6)
            self.assertEqual(values[0][0], 0)

    def test_timeout_retains_output_and_is_cancelled(self):
        code = 'import time; print("partial evidence",flush=True); time.sleep(20)'
        rc, out, err, pid = m.subprocess_runner([sys.executable, "-c", code], dict(os.environ), "", 0.2)
        self.assertNotEqual(rc, 0)
        self.assertIn("partial evidence", out)
        self.assertEqual(m.classify_failure(err), m.CANCELLED)
        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_run_attempt_passes_owned_worktree_to_real_runner(self):
        with tempfile.TemporaryDirectory() as root:
            profile = Path(root) / "muse-go-1.config.toml"
            profile.write_text('model="muse-spark-1.3-contributor"\nmodel_provider="muse-go-1"\n'
                               'model_reasoning_effort="xhigh"\n[model_providers.muse-go-1]\n'
                               'env_key="OPENCODE_GO_KEY_1"\n')
            isolated = Path(root) / "muse-homes" / "muse-go-1"
            isolated.mkdir(parents=True)
            (isolated / "config.toml").write_text(profile.read_text())
            with patch.object(m, "subprocess_runner", return_value=(1, "", "error: 429 Too Many Requests", 123)) as run:
                m.run_attempt("test", "1", "brief", root, "muse-go-1", worktree=root,
                              home=root, env={"OPENCODE_GO_KEY_1": "test-key"})
                self.assertEqual(run.call_args.kwargs["cwd"], root)
                self.assertIn("event_path", run.call_args.kwargs)

    def test_closed_output_pipes_do_not_disable_deadline(self):
        code = 'import os,time; os.close(1); os.close(2); time.sleep(20)'
        started = time.monotonic()
        rc, out, err, pid = m.subprocess_runner([sys.executable, "-c", code], dict(os.environ), "", 0.1)
        self.assertLess(time.monotonic() - started, 4)
        self.assertNotEqual(rc, 0)
        self.assertEqual(m.classify_failure(err), m.CANCELLED)

    def test_invalid_log_path_cannot_launch_worker(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(__import__('subprocess'), "Popen") as spawn:
                with self.assertRaises(OSError):
                    m.subprocess_runner(["unused"], {}, "", 1, event_path=Path(root) / "absent/log")
                spawn.assert_not_called()


if __name__ == "__main__": unittest.main()
