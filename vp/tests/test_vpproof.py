#!/usr/bin/env python3
"""tests/test_vpproof.py -- D69: orphaned SysV shm preflight and the infra
classification of a leg whose Postgres could not start.  No box, no ipcrm.

    python3 tests/test_vpproof.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import vpproof  # noqa: E402

IPCS = """IPC status from <running system> as of Mon Sep 14 19:06:22 IST 2026
T     ID     KEY        MODE       OWNER    GROUP  CREATOR   CGROUP NATTCH  SEGSZ  CPID  LPID   ATIME    DTIME    CTIME
Shared Memory:
m 4915200 0x63468830 --rw------- dhairyabajaria    staff dhairyabajaria    staff      0     56  53802  53802 11:10:44 12:22:18 11:10:44
m 2949121 0x62647963 --rw------- dhairyabajaria    staff dhairyabajaria    staff      0     56  48604  48604  2:11:45  3:15:46  2:11:45
m 655362 0x617b5a55 --rw------- dhairyabajaria    staff dhairyabajaria    staff      1     56  14782  14782 23:54:44  0:59:54 23:54:44
m 655363 0x617b5a32 --rw------- dhairyabajaria    staff dhairyabajaria    staff      0     56  14688  14688 23:54:42  0:59:21 23:54:42
"""

SHM_LOG = """# vpproof ...
platform/tests/test_x.py::test_a ERROR                                   [ 50%]
selecting dynamic shared memory implementation ... posix
2026-09-14 13:15:06.361 UTC [11664] FATAL:  could not create shared memory segment: No space left on device
E               subprocess.CalledProcessError: Command '['/x/.venv/lib/python3.12/site-packages/pgserver/pginstall/bin/initdb', '-D', '/tmp/pg']' returned non-zero exit status 1.
ERROR platform/tests/test_x.py::test_a - subprocess.CalledProcessError
ERROR platform/tests/test_x.py::test_b - subprocess.CalledProcessError
=========== 554 errors in 12.00s ===========
"""

PRODUCT_LOG = """collected 3 items
platform/tests/test_x.py::test_a FAILED                                  [100%]
FAILED platform/tests/test_x.py::test_a - AssertionError
=========== 1 failed, 2 passed in 1.00s ===========
"""


def test_a_leg_whose_postgres_could_not_start_is_infra_not_product():
    status, counts = vpproof._classify(1, SHM_LOG, False)
    assert status == "FAIL_INFRA", (status, counts)
    assert counts["reason"].startswith("postgres could not start"), counts
    # the nodes are still recorded for the reader, they just are not findings
    assert counts["failed_nodes"], counts
    status, _ = vpproof._classify(1, PRODUCT_LOG, False)
    assert status == "FAIL_PRODUCT"


def test_zero_collected_is_infra_whatever_the_rc():
    for rc in (0, 1, 5):
        status, counts = vpproof._classify(rc, "collected 0 items\nno tests ran\n", False)
        assert status == "FAIL_INFRA", (rc, status)
        assert counts["reason"] in ("no tests collected", "no tests ran"), counts


def test_ipcs_rows_parse_the_right_columns():
    segs = vpproof._ipcs_segments(IPCS)
    assert [(s["id"], s["nattch"], s["cpid"]) for s in segs] == \
        [(4915200, 0, 53802), (2949121, 0, 48604), (655362, 1, 14782), (655363, 0, 14688)]


def test_orphans_are_unattached_with_a_dead_creator():
    alive = lambda pid: pid == 48604          # one creator still running
    orphans, total = vpproof.orphaned_shm(IPCS, alive)
    assert total == 4
    assert [o["id"] for o in orphans] == [4915200, 655363], orphans


def test_preflight_never_runs_ipcrm_unless_reap_is_on():
    calls, notes = [], []

    class CP(object):
        returncode = 0
        stdout = ""

    def run(argv, **kw):
        calls.append(argv)
        return CP()

    res = vpproof.shm_preflight(notes.append, reap=False, run=run, ipcs_text=IPCS,
                                alive=lambda pid: pid == 48604)
    assert calls == [] and res["orphans"] == 2 and res["removed"] == 0, (calls, res)
    assert "reap off" in notes[-1]
    res = vpproof.shm_preflight(notes.append, reap=True, run=run, ipcs_text=IPCS,
                                alive=lambda pid: pid == 48604)
    assert calls == [["ipcrm", "-m", "4915200"], ["ipcrm", "-m", "655363"]], calls
    assert res["removed"] == 2 and res["errors"] == [], res


# -- D18: a timed-out proof unwinds its fixtures and leaks no postmaster ------------------------

PLATFORM_PY = Path("/Users/dhairyabajaria/Claude Code/Calling New/voice-pod/chief9-recovery/platform/.venv/bin/python")

CONFTEST = """import os, time, pytest

@pytest.fixture(scope="session")
def cluster():
    # stands for temporary_postgres(): the try/finally is the only teardown
    mark = os.path.join(os.environ["MARK_DIR"], "up-" + os.environ.get("PYTEST_XDIST_WORKER", "main"))
    open(mark, "w").write("up")
    try:
        yield mark
    finally:
        open(mark.replace("up-", "down-"), "w").write("torn down")
"""

TESTS = """import time

def test_hangs_a(cluster):
    time.sleep(600)

def test_hangs_b(cluster):
    time.sleep(600)
"""


def _project(tmp):
    d = tmp / "proj"
    d.mkdir()
    (d / "conftest.py").write_text(CONFTEST)
    (d / "test_hang.py").write_text(TESTS)
    return d


def test_sigint_first_lets_every_xdist_worker_run_its_finally(tmp_path):
    """the real mechanism: killpg(SIGINT) on pytest -n 2 -> KeyboardInterrupt in
    the controller AND both workers -> session fixtures unwind (pg_ctl stop
    would run here); SIGTERM/SIGKILL never get a turn."""
    import os, subprocess, time
    if not PLATFORM_PY.exists():
        return
    proj = _project(tmp_path)
    marks = tmp_path / "marks"
    marks.mkdir()
    env = dict(os.environ, MARK_DIR=str(marks), PYTHONDONTWRITEBYTECODE="1")
    log = tmp_path / "run.log"
    with open(log, "w") as fh:
        proc = subprocess.Popen([str(PLATFORM_PY), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                 "-n", "2", "--dist", "loadfile", "test_hang.py", "test_hang.py"],
                                cwd=str(proj), env=env, stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + 60
        while time.time() < deadline and len(list(marks.glob("up-*"))) < 1:
            time.sleep(0.2)
        assert list(marks.glob("up-*")), "the workers never started: %s" % log.read_text()[-800:]
        time.sleep(1.0)
        notes = []
        rc, steps = vpproof.stop_group(proc, notes.append, grace_int=45.0, grace_term=10.0)
    ups = sorted(p.name for p in marks.glob("up-*"))
    downs = sorted(p.name for p in marks.glob("down-*"))
    assert steps == ["SIGINT"], (steps, notes, log.read_text()[-800:])
    assert [d.replace("down-", "") for d in downs] == [u.replace("up-", "") for u in ups], \
        "every worker that brought a cluster up tore it down: ups=%s downs=%s\n%s" % (ups, downs, log.read_text()[-800:])
    assert proc.poll() is not None


def test_escalates_to_sigkill_when_the_group_ignores_int_and_term(tmp_path):
    import subprocess, sys
    proc = subprocess.Popen([sys.executable, "-c",
                             "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(600)"],
                            stdin=subprocess.DEVNULL, start_new_session=True)
    import time
    time.sleep(1.5)                                   # let it install SIG_IGN first
    notes = []
    rc, steps = vpproof.stop_group(proc, notes.append, grace_int=1.0, grace_term=1.0)
    assert steps == ["SIGINT", "SIGTERM", "SIGKILL"], (steps, notes)
    assert rc != 0 and any("SIGINT did not stop" in n for n in notes)


def test_reap_stops_only_clusters_under_the_proofs_own_temp_root(tmp_path, monkeypatch):
    root = tmp_path / "vpproof-x"
    mine = root / "voicepod-test-pg-session-gw0-abc" / "pgdata"
    mine.mkdir(parents=True)
    (mine / "postmaster.pid").write_text("4242\n/x\n")
    other = tmp_path / "voicepod-test-pg-someone-else" / "pgdata"      # NOT under root
    other.mkdir(parents=True)
    (other / "postmaster.pid").write_text("7\n")
    (root / "voicepod-test-pg-clean-exit").mkdir()                       # no pgdata: nothing to stop
    monkeypatch.setattr(vpproof, "pg_ctl_path", lambda py: "/fake/pg_ctl")
    calls, notes = [], []

    class CP(object):
        returncode = 0
        stdout = "server stopped"

    def run(argv, **kw):
        calls.append(argv)
        return CP()
    out = vpproof.reap_run_postmasters(root, "python", notes.append, run=run)
    assert calls == [["/fake/pg_ctl", "-D", str(mine), "-m", "immediate", "-w", "-t", "20", "stop"]], calls
    assert out == [{"pgdata": str(mine), "pid": 4242, "stopped": True, "detail": "server stopped"}]
    assert "stopped" in notes[0] and str(other) not in "".join(notes)
    assert vpproof.reap_run_postmasters(tmp_path / "nothing-here", "python", notes.append, run=run) == []


def test_shm_gate_threshold_and_live_count():
    assert vpproof.shm_block_threshold(32, {}) == 24
    assert vpproof.shm_block_threshold(32, {"shm_block_at": 20}) == 20
    assert vpproof.shm_block_threshold(8, {}) == 4
    n = vpproof.shm_segment_count()
    assert n is None or n >= 0


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name)
            except Exception:
                fails += 1
                print("FAIL %s" % name)
                traceback.print_exc()
    print("%d failed" % fails)
    raise SystemExit(1 if fails else 0)
