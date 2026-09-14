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
