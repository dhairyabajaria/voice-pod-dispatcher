#!/usr/bin/env python3
"""vpproof.py -- box-locked proofs for the Voice Pod v12 control layer (K-09).

    python3 vpproof.py run --run-root R --worktree WT --sha SHA --proof-id P
                           [--kind targeted|full] [--paths a,b] [--timeout-min N]
                           [--wait-max-min N] [--proof-kind platform|portal|agent|deploy]
    python3 vpproof.py lock-status --cn CN

Rules (06.4, A4 box facts, memory):
  * ONE pytest on the box at a time.  Both `box.lock.d` and `portal.lock.d`
    are taken for any proof (one CPU, two locks) with an owner.json
    {pid, token, ts, sha, proof_id}.  A stale lock (owner pid dead for
    > 5 min) is reclaimed and logged; a live owner's lock is never removed.
  * The log file name carries the sha and the proof id (never a fixed name).
  * A red is `rc == 1` with `^(FAILED|ERROR)` lines in a FINISHED log; the
    `[100%]` marker must be present or the run is FAIL_INFRA, not a verdict.
  * Portal proofs print `node --version` into the log; if it is not v22 the
    proof is FAIL_INFRA.
  * Exit codes: pytest 0 PASS, 1 FAIL_PRODUCT (with failed node ids),
    2/3/4/5 FAIL_INFRA, timeout FAIL_INFRA, anything else UNKNOWN.

Every store write goes through vpstore.Store (same process) so timestamps are
the store's.  stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vpstore  # noqa: E402

LOCK_NAMES = ("box.lock.d", "portal.lock.d")
STALE_S = 5 * 60
NODE22_BIN = str(Path.home() / ".local" / "node-v22.11.0-darwin-arm64" / "bin")

_FAILED_RE = re.compile(r"^(FAILED|ERROR) (\S+)", re.M)
_MARKER_RE = re.compile(r"\[100%\]")
_SUMMARY_RE = re.compile(r"=+ (.*?) in [\d.]+s", re.M)
_COLLECTED_RE = re.compile(r"collected (\d+) items?|(\d+) tests? collected")


def utc_ms():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# locks
# --------------------------------------------------------------------------

class BoxLocks(object):
    def __init__(self, cn, sha, proof_id, log=None):
        self.cn = Path(cn)
        self.sha = sha
        self.proof_id = proof_id
        self.token = uuid.uuid4().hex
        self.held = []
        self.log = log or (lambda s: None)

    def _owner_path(self, name):
        return self.cn / name / "owner.json"

    def _try_one(self, name):
        d = self.cn / name
        try:
            os.mkdir(str(d))
        except FileExistsError:
            # stale?
            op = self._owner_path(name)
            try:
                owner = json.loads(op.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                owner = {}
            pid = owner.get("pid")
            ts = owner.get("mono_epoch") or 0
            if pid and _pid_alive(pid):
                return False, "held by pid %s (%s)" % (pid, owner.get("proof_id"))
            if not pid or (time.time() - float(ts or 0)) > STALE_S:
                self.log("reclaiming stale %s (owner=%s)" % (name, owner))
                try:
                    op.unlink()
                except OSError:
                    pass
                try:
                    os.rmdir(str(d))
                except OSError:
                    return False, "stale but not removable"
                try:
                    os.mkdir(str(d))
                except FileExistsError:
                    return False, "lost the race"
            else:
                return False, "recent owner without live pid; waiting"
        except OSError as exc:
            return False, "mkdir failed: %s" % exc
        self._owner_path(name).write_text(json.dumps({
            "pid": os.getpid(), "token": self.token, "ts": utc_ms(),
            "mono_epoch": time.time(), "sha": self.sha, "proof_id": self.proof_id,
            "owner": "vpproof"}, indent=2), encoding="utf-8")
        self.held.append(name)
        return True, ""

    def acquire(self, wait_max_s):
        deadline = time.monotonic() + wait_max_s
        reason = ""
        while True:
            for name in LOCK_NAMES:
                if name in self.held:
                    continue
                ok, reason = self._try_one(name)
                if not ok:
                    break
            if len(self.held) == len(LOCK_NAMES):
                return True, ""
            self.release()          # never hold one while waiting for the other
            if time.monotonic() > deadline:
                return False, reason
            time.sleep(5)

    def release(self):
        for name in list(self.held):
            op = self._owner_path(name)
            try:
                owner = json.loads(op.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                owner = {}
            if owner.get("token") == self.token:
                try:
                    op.unlink()
                except OSError:
                    pass
                try:
                    os.rmdir(str(self.cn / name))
                except OSError:
                    pass
            self.held.remove(name)


def lock_status(cn):
    out = {}
    for name in LOCK_NAMES:
        d = Path(cn) / name
        if not d.exists():
            out[name] = {"held": False}
            continue
        try:
            owner = json.loads((d / "owner.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            owner = {}
        out[name] = {"held": True, "owner": owner,
                     "alive": bool(owner.get("pid") and _pid_alive(owner["pid"]))}
    return out


# --------------------------------------------------------------------------
# the proof
# --------------------------------------------------------------------------

_VITEST_FILES_RE = re.compile(r"Test Files\s+(.*)\n")
_VITEST_TESTS_RE = re.compile(r"\bTests\s+(.*)\n")
_VITEST_FAIL_RE = re.compile(r"^\s*(?:FAIL|\u00d7|\u2716)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)(?:\s*>\s*(.+?))?\s*$", re.M)


# D69: a leg whose Postgres could not start is never a verdict on the product.
# proof-00073/00074 hit kern.sysv.shmmni=32 exhausted by orphaned SysV segments
# and were recorded FAIL_PRODUCT with 554 ERROR nodes.
_INFRA_RE = re.compile(
    r"could not create shared memory segment|No space left on device|"
    r"Failed postgres command|CalledProcessError: Command '\['[^']*/initdb'|"
    r"initdb: error:|pg_ctl: could not start server|could not bind IPv[46] address")


def _ipcs_segments(text):
    """Rows of `ipcs -m -a` (macOS): m ID KEY MODE OWNER GROUP CREATOR CGROUP
    NATTCH SEGSZ CPID LPID ... -> [{id, nattch, cpid}]."""
    out = []
    for ln in text.splitlines():
        f = ln.split()
        if len(f) < 11 or f[0] != "m":
            continue
        try:
            out.append({"id": int(f[1]), "nattch": int(f[8]), "cpid": int(f[10])})
        except ValueError:
            continue
    return out


def orphaned_shm(ipcs_text=None, alive=_pid_alive):
    """SysV shm segments nobody is attached to whose creator pid is dead:
    what a killed xdist worker / test-postgres cluster leaves behind."""
    if ipcs_text is None:
        try:
            ipcs_text = subprocess.run(["ipcs", "-m", "-a"], stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, universal_newlines=True,
                                       timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return [], 0
    segs = _ipcs_segments(ipcs_text)
    return [s for s in segs if s["nattch"] == 0 and not alive(s["cpid"])], len(segs)


def shm_limit():
    try:
        return int(subprocess.run(["sysctl", "-n", "kern.sysv.shmmni"], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, universal_newlines=True,
                                  timeout=10).stdout.strip() or 32)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 32


def shm_preflight(log, reap=False, run=subprocess.run, ipcs_text=None, alive=_pid_alive):
    """Before a platform leg: count (and, only when the roster says
    proof.shm_reap: true, remove with `ipcrm -m`) orphaned segments.  Returns
    {"total", "orphans", "ids", "removed", "errors", "limit"}."""
    orphans, total = orphaned_shm(ipcs_text, alive)
    res = {"total": total, "orphans": len(orphans), "ids": [o["id"] for o in orphans],
           "removed": 0, "errors": [], "limit": shm_limit()}
    if orphans and reap:
        for o in orphans:
            try:
                cp = run(["ipcrm", "-m", str(o["id"])], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)
                if cp.returncode == 0:
                    res["removed"] += 1
                else:
                    res["errors"].append("%s: %s" % (o["id"], (cp.stdout or "").strip()[:80]))
            except (OSError, subprocess.TimeoutExpired) as exc:
                res["errors"].append("%s: %s" % (o["id"], exc))
    log("shm preflight: %d segments, %d orphaned (dead creator, NATTCH 0), %d removed, "
        "limit %d%s" % (res["total"], res["orphans"], res["removed"], res["limit"],
                        "" if reap else " (reap off: roster proof.shm_reap)"))
    return res



# -- D18: a timed-out proof must not leak postmasters ------------------------------------
# pgserver starts postgres with `pg_ctl start`; the postmaster setsid()s into its
# own process group, so killpg on pytest's group never reaches it.  Only the
# try/finally in platform/testsupport/postgres.py (temporary_postgres) and
# pgserver's atexit hook stop it, and SIGTERM/SIGKILL skip both.  So: SIGINT
# first (KeyboardInterrupt: pytest and every xdist worker unwind their
# fixtures, pg_ctl stop runs, bounded at 30 s by pgserver_lock), a grace of at
# least 45 s, then SIGTERM, then SIGKILL; and whatever still runs under the
# proof's OWN temp root (TMPDIR is per proof) is stopped by exact pgdata path.
KILL_GRACE_INT_S = 60.0
KILL_GRACE_TERM_S = 30.0
PG_PREFIX = "voicepod-test-pg-"


def stop_group(proc, log, grace_int=KILL_GRACE_INT_S, grace_term=KILL_GRACE_TERM_S,
               killpg=os.killpg, getpgid=os.getpgid):
    """SIGINT -> grace -> SIGTERM -> grace -> SIGKILL on the proof's process
    group; returns (rc, escalation list)."""
    steps = []
    for sig, grace in ((signal.SIGINT, grace_int), (signal.SIGTERM, grace_term), (signal.SIGKILL, None)):
        try:
            killpg(getpgid(proc.pid), sig)
            steps.append(sig.name)
        except OSError as exc:
            steps.append("%s: %s" % (sig.name, exc))
        try:
            rc = proc.wait(timeout=grace) if grace is not None else proc.wait()
            log("timeout: group stopped after %s (rc=%s)" % (" -> ".join(steps), rc))
            return rc, steps
        except subprocess.TimeoutExpired:
            log("timeout: %s did not stop the group within %ss" % (sig.name, grace))
    return proc.wait(), steps


def pg_ctl_path(python):
    """pgserver's bundled pg_ctl for the venv `python` runs the proof with."""
    try:
        out = subprocess.run([python, "-c", "import pgserver._commands as c; print(c.POSTGRES_BIN_PATH)"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                             timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    cand = Path(out) / "pg_ctl" if out else None
    return str(cand) if cand and cand.exists() else None


def reap_run_postmasters(tmp_root, python, log, run=subprocess.run):
    """Stop every cluster the proof itself created: postmaster.pid files under
    THIS proof's temp root only (identity by exact path, never a pattern kill),
    `pg_ctl -D <pgdata> -m immediate stop` each.  Returns the list of
    {pgdata, pid, stopped}."""
    tmp_root = Path(tmp_root)
    found = sorted(tmp_root.glob("%s*/pgdata/postmaster.pid" % PG_PREFIX))
    if not found:
        return []
    pg_ctl = pg_ctl_path(python)
    out = []
    for pidfile in found:
        pgdata = pidfile.parent
        try:
            pid = int((pidfile.read_text(encoding="utf-8").splitlines() or ["0"])[0].strip() or 0)
        except (OSError, ValueError):
            pid = 0
        rec = {"pgdata": str(pgdata), "pid": pid, "stopped": False, "detail": ""}
        if pg_ctl is None:
            rec["detail"] = "pg_ctl not found"
        else:
            try:
                cp = run([pg_ctl, "-D", str(pgdata), "-m", "immediate", "-w", "-t", "20", "stop"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
                rec["stopped"] = cp.returncode == 0
                rec["detail"] = (cp.stdout or "").strip()[-160:]
            except (OSError, subprocess.TimeoutExpired) as exc:
                rec["detail"] = str(exc)[:160]
        if not rec["stopped"] and pid and not _pid_alive(pid):
            rec["stopped"] = True
            rec["detail"] = (rec["detail"] + " (postmaster already gone)").strip()
        log("reap %s pid=%s -> %s %s" % (pgdata, pid, "stopped" if rec["stopped"] else "STILL RUNNING", rec["detail"]))
        out.append(rec)
    return out


def shm_segment_count():
    """live SysV shm segments (`ipcs -m`), or None when ipcs is unavailable."""
    try:
        text = subprocess.run(["ipcs", "-m"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              universal_newlines=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return sum(1 for ln in text.splitlines() if ln.split()[:1] == ["m"])


def shm_block_threshold(limit, proof_cfg):
    """the roster's proof.shm_block_at, else limit - 8 (one 4-worker run's
    segments plus margin under kern.sysv.shmmni; 24 on the 32-slot Mac)."""
    try:
        v = int(proof_cfg.get("shm_block_at") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else max(4, int(limit) - 8)


def _classify_vitest(rc, log_text, timed_out):
    """vitest run: 'Test Files  N passed (N)' / 'Tests  N passed (N)' summary lines;
    a failing file prints ' FAIL  path > name' lines. rc 1 with
    'No test files found' is infra, not product."""
    files = _VITEST_FILES_RE.search(log_text)
    tests = _VITEST_TESTS_RE.search(log_text)
    def _node(m):
        f = m.group(1)
        f = f if f.startswith("portal/") else "portal/" + f   # cwd is portal/
        return ("%s::%s" % (f, m.group(2))) if m.group(2) else f
    failed = sorted(set(_node(m) for m in _VITEST_FAIL_RE.finditer(log_text)))
    marker = bool(files and tests)
    counts = {"rc": rc, "failed_nodes": failed, "marker": marker,
              "collected": None, "summary": (tests.group(1).strip() if tests else None),
              "timed_out": timed_out, "runner": "vitest"}
    if timed_out:
        return "FAIL_INFRA", counts
    if "No test files found" in log_text:
        return "FAIL_INFRA", dict(counts, reason="vitest: no test files matched")
    if "FAIL_INFRA: node is not v22" in log_text:
        return "FAIL_INFRA", dict(counts, reason="node is not v22")
    if rc == 0 and marker and "failed" not in (files.group(1) + tests.group(1)):
        return "PASS", counts
    if rc != 0 and marker and ("failed" in (files.group(1) + tests.group(1)) or failed):
        return "FAIL_PRODUCT", counts
    if rc != 0 and not marker:
        return "FAIL_INFRA", dict(counts, reason="vitest exited %s without a summary" % rc)
    return "UNKNOWN", counts


def _classify(rc, log_text, timed_out):
    failed = [m.group(2) for m in _FAILED_RE.finditer(log_text)]
    marker = bool(_MARKER_RE.search(log_text))
    m = _COLLECTED_RE.search(log_text)
    collected = int(m.group(1) or m.group(2)) if m else None
    summary = _SUMMARY_RE.findall(log_text)
    counts = {"rc": rc, "failed_nodes": sorted(set(failed)), "marker": marker,
              "collected": collected, "summary": summary[-1] if summary else None,
              "timed_out": timed_out}
    if timed_out:
        return "FAIL_INFRA", counts
    infra = _INFRA_RE.search(log_text)
    if infra:
        return "FAIL_INFRA", dict(counts, reason="postgres could not start: %s"
                                  % infra.group(0)[:80])
    if collected == 0:
        return "FAIL_INFRA", dict(counts, reason="no tests collected")
    if rc == 0 and marker:
        return "PASS", counts
    if rc == 0 and not marker and collected == 0:
        return "FAIL_INFRA", dict(counts, reason="no tests ran")
    if rc == 1 and failed and marker:
        return "FAIL_PRODUCT", counts
    if rc == 1 and not failed:
        return "FAIL_INFRA", dict(counts, reason="rc 1 without FAILED/ERROR lines")
    if rc in (2, 3, 4, 5):
        return "FAIL_INFRA", dict(counts, reason="pytest usage/interrupt/no-tests rc")
    if rc == 0 and not marker:
        return "FAIL_INFRA", dict(counts, reason="rc 0 without [100%] marker")
    return "UNKNOWN", counts


def _rec(args, st, *a, **k):
    if getattr(args, 'no_record', False):
        return None
    return st.proof_record(*a, **k)


def run_proof(args):
    st = vpstore.Store(args.run_root)
    roster = st.roster()
    cn = args.cn or roster.get("run", {}).get("cn") or vpstore.DEFAULT_CN
    trunk = roster.get("run", {}).get("trunk") or roster.get("trunk") or \
        os.path.join(cn, "voicepod-plan010-rebuild")
    proof_cfg = roster.get("proof", {})
    timeout_min = args.timeout_min or (proof_cfg.get("full_box_timeout_min", 90)
                                       if args.kind == "full"
                                       else proof_cfg.get("targeted_timeout_min", 40))
    wait_max = args.wait_max_min or roster.get("concurrency", {}).get(
        "proof_wait_max_min", 90)
    proofs_dir = Path(args.run_root) / "proofs" / args.sha
    proofs_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_ms().replace(":", "").replace(".", "-")
    log_path = proofs_dir / ("%s-%s-%s.log" % (args.kind, args.proof_id, stamp))
    meta_path = proofs_dir / ("%s-%s-%s.json" % (args.kind, args.proof_id, stamp))
    notes = []

    def log(s):
        notes.append("%s %s" % (utc_ms(), s))

    locks = BoxLocks(cn, args.sha, args.proof_id, log)
    ok, why = locks.acquire(wait_max * 60)
    if not ok:
        counts = {"reason": "box busy: %s" % why, "waited_min": wait_max}
        _rec(args, st, args.proof_id, "FAIL_INFRA", counts=counts,
                        artifacts=str(meta_path))
        meta_path.write_text(json.dumps({"ts": utc_ms(), "status": "FAIL_INFRA",
                                         "counts": counts, "notes": notes}, indent=2),
                             encoding="utf-8")
        print(json.dumps({"status": "FAIL_INFRA", "counts": counts}))
        return 0
    try:
        _rec(args, st, args.proof_id, "RUNNING")
        paths = [p for p in (args.paths or "").split(",") if p.strip()]
        wt = Path(args.worktree)
        env = dict(os.environ)
        env["PATH"] = NODE22_BIN + os.pathsep + env.get("PATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        py_for_reap = str(wt / "platform" / ".venv" / "bin" / "python")
        if not Path(py_for_reap).exists():
            py_for_reap = str(Path(trunk) / "platform" / ".venv" / "bin" / "python")
        if args.proof_kind == "portal":
            # vitest runs from portal/; packet paths are repo-relative
            rel = [p[len("portal/"):] if p.startswith("portal/") else p for p in paths]
            argv = ["npx", "vitest", "run"] + rel
            cwd = str(wt / "portal")
            pre = ["node", "--version"]
        elif args.proof_kind == "agent":
            # agent suite: its own venv (symlinked from trunk), serial, from agent/
            py = str(wt / "agent" / ".venv" / "bin" / "python")
            if not Path(py).exists():
                py = str(Path(trunk) / "agent" / ".venv" / "bin" / "python")
            rel = [p[len("agent/"):] if p.startswith("agent/") else p for p in paths]
            argv = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider"] + (rel or ["tests"])
            cwd = str(wt / "agent")
            pre = None
        elif args.proof_kind == "deploy":
            py = str(Path(trunk) / "platform" / ".venv" / "bin" / "python")
            argv = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider"] + \
                (paths or ["deploy/tests"])
            cwd = str(wt)
            pre = None
        else:
            py = str(wt / "platform" / ".venv" / "bin" / "python")
            if not Path(py).exists():
                py = str(Path(trunk) / "platform" / ".venv" / "bin" / "python")
            argv = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    "-n", str(args.workers), "--dist", "loadfile"]
            py_for_reap = py
            if args.kind == "targeted":
                argv += paths
            else:
                argv += ["platform/tests"]
            cwd = str(wt)
            pre = None
        shm = None
        if args.proof_kind == "platform":
            shm = shm_preflight(log, reap=bool(proof_cfg.get("shm_reap", False)))
            left = shm["orphans"] - shm["removed"]
            if left and left >= max(8, shm["limit"] // 2):
                st.alert("SHM_ORPHANS", "%d of %d SysV shm slots held by orphaned segments "
                         "(dead creator, NATTCH 0); postgres cannot start when full. Owner: "
                         "`ipcrm -m` them (ids %s), or set roster proof.shm_reap: true so "
                         "vpproof removes them before each platform leg"
                         % (left, shm["limit"], " ".join(str(i) for i in shm["ids"][:40])))
            # D18 (c): a box near the shm ceiling gets NO proof -- launching one
            # would red on "could not create shared memory segment", cost a
            # FAIL_CAP strike and leak more.  BLOCKED_SHM is a box condition,
            # not the candidate's: the driver holds the task, never counts it.
            live = shm_segment_count()
            live = shm["total"] if live is None else live
            block_at = shm_block_threshold(shm["limit"], proof_cfg)
            shm["live_after_preflight"] = live
            shm["block_at"] = block_at
            if live >= block_at:
                counts = {"reason": "PROOF_BLOCKED_SHM: %d live SysV shm segments >= %d (limit %d); "
                                    "postgres could not start -- owner: reboot or `ipcrm -m` the "
                                    "orphans" % (live, block_at, shm["limit"]),
                          "shm_preflight": shm, "paths": paths}
                log(counts["reason"])
                st.alert("PROOF_BLOCKED_SHM", counts["reason"])
                _rec(args, st, args.proof_id, "BLOCKED_SHM", counts=counts, artifacts=str(meta_path))
                meta_path.write_text(json.dumps({"ts": utc_ms(), "status": "BLOCKED_SHM", "counts": counts,
                                                 "notes": notes}, indent=2), encoding="utf-8")
                print(json.dumps({"status": "BLOCKED_SHM", "counts": counts}))
                return 0
        # D18 (b): every cluster this proof creates lives under its own temp
        # root (platform/testsupport/postgres.py honours TMPDIR), so a post-kill
        # reap can name each pgdata exactly and touch nothing else on the box
        tmp_root = Path(tempfile.mkdtemp(prefix="vpproof-%s-" % re.sub(r"[^A-Za-z0-9_.-]", "_", args.proof_id)[-40:]))
        env["TMPDIR"] = str(tmp_root)
        env["VPPROOF_TMP_ROOT"] = str(tmp_root)
        started = utc_ms()
        timed_out = False
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("# vpproof %s %s sha=%s kind=%s proof_kind=%s\n"
                     % (started, args.proof_id, args.sha, args.kind, args.proof_kind))
            fh.write("# cwd=%s\n# argv=%s\n" % (cwd, json.dumps(argv)))
            if shm is not None:
                fh.write("# shm preflight: %s\n" % json.dumps(shm))
            if pre:
                try:
                    v = subprocess.run(pre, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, universal_newlines=True,
                                       timeout=60).stdout.strip()
                except Exception as exc:
                    v = "error: %s" % exc
                fh.write("# node --version: %s\n" % v)
                if not v.startswith("v22"):
                    fh.write("# FAIL_INFRA: node is not v22\n")
                    fh.flush()
                    counts = {"reason": "node %s is not v22" % v}
                    _rec(args, st, args.proof_id, "FAIL_INFRA", counts=counts,
                                    artifacts=str(log_path))
                    print(json.dumps({"status": "FAIL_INFRA", "counts": counts}))
                    return 0
            fh.flush()
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            escalation = []
            try:
                rc = proc.wait(timeout=timeout_min * 60)
            except subprocess.TimeoutExpired:
                timed_out = True
                rc, escalation = stop_group(
                    proc, log,
                    grace_int=float(proof_cfg.get("kill_grace_int_s", KILL_GRACE_INT_S)),
                    grace_term=float(proof_cfg.get("kill_grace_term_s", KILL_GRACE_TERM_S)))
            reaped = reap_run_postmasters(tmp_root, env.get("VPPROOF_PYTHON") or py_for_reap, log)
            leftover = [r for r in reaped if not r["stopped"]]
            if not leftover:
                shutil.rmtree(str(tmp_root), ignore_errors=True)
            fh.write("\n# vpproof end %s rc=%s timed_out=%s escalation=%s reaped=%s\n"
                     % (utc_ms(), rc, timed_out, json.dumps(escalation), json.dumps(reaped)))
        text = log_path.read_text(encoding="utf-8", errors="replace")
        status, counts = (_classify_vitest if args.proof_kind == "portal" else _classify)(rc, text, timed_out)
        counts["started"] = started
        counts["ended"] = utc_ms()
        counts["paths"] = paths
        counts["escalation"] = escalation
        counts["reaped_postmasters"] = reaped
        counts["tmp_root"] = str(tmp_root)
        if shm is not None:
            counts["shm_preflight"] = shm
        meta = {"ts": utc_ms(), "proof_id": args.proof_id, "sha": args.sha,
                "kind": args.kind, "proof_kind": args.proof_kind, "status": status,
                "counts": counts, "log": str(log_path), "notes": notes,
                "argv": argv, "cwd": cwd}
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        _rec(args, st, args.proof_id, status, counts=counts, artifacts=str(meta_path))
        print(json.dumps({"status": status, "counts": counts, "log": str(log_path)}))
        return 0
    finally:
        locks.release()
        st.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vpproof.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--run-root", required=True)
    r.add_argument("--worktree", required=True)
    r.add_argument("--sha", required=True)
    r.add_argument("--proof-id", required=True)
    r.add_argument("--kind", default="targeted", choices=["targeted", "full"])
    r.add_argument("--proof-kind", default="platform",
                   choices=["platform", "portal", "agent", "deploy"])
    r.add_argument("--paths", default="")
    r.add_argument("--timeout-min", type=int, default=None)
    r.add_argument("--wait-max-min", type=int, default=None)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--cn", default=None)
    r.add_argument("--no-record", action="store_true",
                   help="do not write the proof row (the driver aggregates several kinds)")
    ls = sub.add_parser("lock-status")
    ls.add_argument("--cn", default=vpstore.DEFAULT_CN)
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run_proof(args)
    print(json.dumps(lock_status(args.cn), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
