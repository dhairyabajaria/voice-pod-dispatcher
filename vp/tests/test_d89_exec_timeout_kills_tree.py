"""D89: an exec timeout kills the whole process tree, and the box proof's
outer deadline is wider than vpproof's own (vpproof owns the stop).

03:32Z 2026-09-20: laneproof timed the vpproof wrapper out at
targeted_timeout_min (40) on a FULL run; subprocess.run killed the wrapper
alone and pytest (its own session) + its Postgres clusters ran on orphaned."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import laneproof  # noqa: E402
import vprunners  # noqa: E402

CHILD = textwrap.dedent("""
    import os, subprocess, sys, time
    # the grandchild detaches into its own session, exactly like vpproof's pytest
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    open(sys.argv[1], "w").write(str(g.pid))
    time.sleep(60)
""")


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_exec_timeout_kills_the_detached_grandchild_too(tmp_path):
    marker = tmp_path / "grandchild.pid"
    ex = vprunners.Exec()
    t0 = time.monotonic()
    rc, out, err = ex.run([sys.executable, "-c", CHILD, str(marker)], timeout_s=1.5)
    assert rc == 124 and "killed tree" in err
    gpid = int(marker.read_text())
    for _ in range(50):                       # the kill is asynchronous to our read
        if not _alive(gpid):
            break
        time.sleep(0.1)
    assert not _alive(gpid), "the grandchild in its own session must be dead"
    assert time.monotonic() - t0 < 20, "graces are short"
    # a normal completion is unchanged
    assert ex.run([sys.executable, "-c", "print('ok')"], timeout_s=5)[:2] == (0, "ok\n")


def test_descendants_walks_the_tree_and_kill_tree_reports_what_it_signalled(tmp_path):
    marker = tmp_path / "g.pid"
    proc = subprocess.Popen([sys.executable, "-c", CHILD, str(marker)], start_new_session=True)
    for _ in range(50):
        if marker.exists() and marker.read_text():
            break
        time.sleep(0.1)
    gpid = int(marker.read_text())
    assert gpid in vprunners.descendants(proc.pid)
    signalled = vprunners.kill_tree(proc.pid, grace_int_s=1.0, grace_term_s=1.0)
    assert set(signalled) >= {proc.pid, gpid}
    proc.wait(timeout=5)
    assert not _alive(gpid)


def test_box_deadlines_give_vpproof_the_stop_and_pass_timeout_min(tmp_path):
    class _Ex:
        calls = []

        def run(self, argv, cwd=None, timeout_s=120, env=None):
            self.calls.append((list(argv), timeout_s))
            return 0, '{"status": "PASS", "counts": {}}', ""
    p = laneproof.Proof.__new__(laneproof.Proof)
    p.cfg = {"targeted_timeout_min": 40, "full_box_timeout_min": 90, "proof_wait_max_min": 90,
             "kill_grace_int_s": 60, "kill_grace_term_s": 30}
    inner, outer = p.box_deadlines([])
    assert inner == 90 and outer == 90 * 60 + 90 * 60 + 90 + laneproof.Proof.BOX_STOP_SLACK_S
    inner_t, outer_t = p.box_deadlines(["platform/tests/test_a.py"])
    assert inner_t == 40 and outer_t == 40 * 60 + 90 * 60 + 90 + laneproof.Proof.BOX_STOP_SLACK_S
    assert outer > inner * 60 and outer_t > inner_t * 60, "the wrapper is never killed before its own deadline"
