#!/usr/bin/env python3
"""tests/test_reload.py -- D12 hot reload: RUN_ROOT/RELOAD (owner-created)
makes the running driver quiesce, reload its code in place at active == 0,
rebind every live object, log RELOAD with before/after hashes and resume.
One in-process test over the real modules plus an extra module the test
edits; one failure test; one real subprocess driver over a private copy of
vp/ whose vpschema.py the test edits mid-run."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP))
sys.path.insert(0, str(HERE))

import lanedriver  # noqa: E402
from test_lanedriver import Env, FakeRunner, result_ok, settle, slow_result  # noqa: E402


def _extra_module(tmp_path, body):
    f = tmp_path / "vpextra_t.py"
    f.write_text(body)
    name = "vpextra_t"
    sys.modules.pop(name, None)
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))         # importlib.reload needs a findable spec
    importlib.invalidate_caches()
    importlib.import_module(name)
    return f


def _reloads(env):
    p = env.run_root / "reloads.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_reload_waits_for_active_zero_then_rebinds_and_resumes(tmp_path):
    env = Env(tmp_path)
    env.activate()
    extra = _extra_module(tmp_path, "def answer():\n    return 1\n")
    oc = FakeRunner(default=slow_result(1.5))
    drv = env.driver({"opencode": oc, "codex": FakeRunner(), "claude": FakeRunner()}, interval=0.2)
    drv.reload_extra = ["vpextra_t"]
    drv.reload_modules = ()                          # in-process: leave the shared helper modules alone
    drv._code_hashes = drv.code_hashes()
    v0 = drv.code_version()
    old_cls = type(drv)
    th = threading.Thread(target=drv.loop, daemon=True)
    th.start()
    time.sleep(0.5)                                   # L00 is live
    assert env.rows()["L00"]["state"] == "RUNNING"
    extra.write_text("def answer():\n    return 2\n")
    (env.run_root / "RELOAD").write_text(json.dumps({"reason": "test change"}))
    time.sleep(0.5)
    hb = json.loads((env.run_root / "driver.heartbeat").read_text())
    assert hb["reload_pending"] is True and hb["reloads"] == 0, "quiescing, not reloaded mid-turn"
    assert type(drv) is old_cls
    deadline = time.time() + 15
    while time.time() < deadline and not _reloads(env):
        time.sleep(0.2)
    recs = _reloads(env)
    assert recs and recs[0]["ok"] is True and recs[0]["changed"] == ["vpextra_t"], recs
    assert env.rows()["L00"]["state"] == "VERIFIED", "the live turn finished before the reload"
    assert not (env.run_root / "RELOAD").exists()
    # the process now runs the new code: extra module, driver class, held objects
    assert sys.modules["vpextra_t"].answer() == 2
    assert type(drv) is not old_cls and type(drv).__module__ == "lanedriver_r1"
    assert type(drv.control).__module__ == "lanedriver_r1"
    assert type(drv.proof).__module__ == "laneproof"
    assert drv.code_version() != v0 and drv._reload_count == 1
    log = (env.run_root / "driver.log").read_text()
    assert "RELOAD requested (test change)" in log and "RELOAD ok #1" in log
    assert "RELOAD" in (env.run_root / "OWNER-ALERTS.md").read_text()
    assert "reload ok" in (env.run_root / "comms.jsonl").read_text()
    # ... and keeps dispatching: the remaining rows complete under the new class
    deadline = time.time() + 20
    while time.time() < deadline and any(env.rows()[t]["state"] != "VERIFIED" for t in ("L01", "L02", "L03", "L04")):
        time.sleep(0.3)
    rows = env.rows()
    assert all(rows[t]["state"] == "VERIFIED" for t in ("L01", "L02", "L03", "L04")), {t: rows[t]["state"] for t in rows}
    hb = json.loads((env.run_root / "driver.heartbeat").read_text())
    assert hb["reloads"] == 1 and hb["code_version"] == drv.code_version() and hb["reload_pending"] is False
    (env.run_root / "STOP").write_text("")
    th.join(timeout=10)
    assert not th.is_alive()
    # a second reload numbers r2 and reports "no file changed"
    sys.modules.pop("vpextra_t", None)


def test_reload_failure_keeps_old_code_and_stops_new_claims(tmp_path):
    env = Env(tmp_path)
    env.activate()
    extra = _extra_module(tmp_path, "def answer():\n    return 1\n")
    drv = env.driver({"opencode": FakeRunner(default=result_ok), "codex": FakeRunner(), "claude": FakeRunner()})
    drv.reload_extra = ["vpextra_t"]
    drv.reload_modules = ()                          # in-process: leave the shared helper modules alone
    drv._code_hashes = drv.code_hashes()
    old_cls = type(drv)
    extra.write_text("def answer(:\n")                # syntax error: nothing may change
    (env.run_root / "RELOAD").write_text("")
    drv.tick()
    recs = _reloads(env)
    assert recs and recs[0]["ok"] is False and "SyntaxError" in recs[0]["error"]
    assert type(drv) is old_cls and sys.modules["vpextra_t"].answer() == 1
    assert drv._reload_failed and not drv._guards(), "no new claims after a failed reload"
    assert "RELOAD_FAILED" in (env.run_root / "OWNER-ALERTS.md").read_text()
    assert not (env.run_root / "RELOAD").exists()
    before = env.rows()
    drv.tick()
    assert env.rows() == before or all(r["state"] in ("READY", "PLANNED", "WAITING_DEPENDENCY")
                                       for r in env.rows().values())
    # the owner fixes the file and asks again: the reload succeeds and claims resume
    extra.write_text("def answer():\n    return 3\n")
    (env.run_root / "RELOAD").write_text("fixed")
    drv.tick()
    assert drv._reload_failed is None and _reloads(env)[-1]["ok"] is True
    assert sys.modules["vpextra_t"].answer() == 3 and type(drv).__module__ == "lanedriver_r1"
    settle(drv, 3)
    assert env.rows()["L00"]["state"] == "VERIFIED"
    sys.modules.pop("vpextra_t", None)


def test_reload_cli_writes_the_marker(tmp_path):
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({})
    rc = lanedriver.main(["--roster", str(env.run_root / "roster.json"), "reload", "--reason", "hotfix x"])
    assert rc == 0
    doc = json.loads((env.run_root / "RELOAD").read_text())
    assert doc["reason"] == "hotfix x" and doc["by"] == "cli"
    assert drv.reload_requested()


def test_real_driver_process_reloads_an_edited_module_without_restarting(tmp_path):
    """A subprocess driver (fake runners) over a private copy of vp/: the test
    edits the copy's vpschema.py and touches RELOAD; the same pid logs RELOAD
    ok naming vpschema, its heartbeat code_version changes, and it finishes
    its ticks on the new code."""
    copy = tmp_path / "dispatcher" / "vp"                 # vpcircle imports ../circleaccount.py
    shutil.copytree(VP, copy, ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))
    shutil.copy(VP.parent / "circleaccount.py", copy.parent / "circleaccount.py")
    env = Env(tmp_path)
    env.activate()
    log_path = env.run_root / "driver.log"
    proc = subprocess.Popen([sys.executable, str(copy / "lanedriver.py"), "--roster", str(env.run_root / "roster.json"),
                             "run", "--loop", "--max-ticks", "60", "--interval", "0.3", "--fake-runners"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=str(copy))
    try:
        def hb():
            try:
                return json.loads((env.run_root / "driver.heartbeat").read_text())
            except (OSError, ValueError):
                return None
        deadline = time.time() + 20
        while time.time() < deadline and not (hb() and hb()["tick"] >= 2):
            time.sleep(0.2)
        h0 = hb()
        assert h0 and h0["pid"] == proc.pid, h0
        v0 = h0["code_version"]
        vs = copy / "vpschema.py"
        vs.write_text(vs.read_text() + "\n# hot-reload test edit\nRELOAD_TEST_MARK = 1\n")
        (env.run_root / "RELOAD").write_text(json.dumps({"reason": "edit vpschema"}))
        deadline = time.time() + 25
        while time.time() < deadline and "RELOAD ok" not in (log_path.read_text() if log_path.exists() else "") \
                and "RELOAD FAILED" not in (log_path.read_text() if log_path.exists() else ""):
            time.sleep(0.3)
        log = log_path.read_text()
        assert "RELOAD ok #1" in log, log[-2000:]
        assert "changed=['vpschema']" in log
        rec = json.loads((env.run_root / "reloads.jsonl").read_text().splitlines()[-1])
        assert rec["ok"] and rec["pid"] == proc.pid and rec["changed"] == ["vpschema"]
        assert rec["before"]["vpschema"] != rec["after"]["vpschema"]
        deadline = time.time() + 10
        while time.time() < deadline and not (hb() and hb()["reloads"] == 1):
            time.sleep(0.2)
        h1 = hb()
        assert h1["pid"] == proc.pid and h1["reloads"] == 1 and h1["code_version"] != v0, h1
        assert "RELOAD" in (env.run_root / "OWNER-ALERTS.md").read_text()
    finally:
        (env.run_root / "STOP").write_text("")
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=10)
    err = proc.stderr.read().decode(errors="replace")
    assert "Traceback" not in err, err[-2000:]
