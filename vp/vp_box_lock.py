#!/usr/bin/env python3
"""vp_box_lock.py -- the shared box lock (D76b, BULK-RULING §21.2 amendment 4).

One CPU, at most TWO Postgres-backed proofs on the box at once.  The locks are
mkdir directories under CN with an owner.json {pid, token, ts, proof_id, tier}:

  box.lock.d          whole-box exclusive: full suites, targeted runs with more
                      than `targeted_max_paths` (8) paths, every external audit
                      / WORKER-1 run
  box.slot-0.lock.d   one Postgres-backed proof each; a small targeted proof
  box.slot-1.lock.d   (<= 8 paths) takes exactly ONE of them, never box.lock.d
  portal.lock.d       portal-vs-portal only (unchanged): an exclusive holder
                      takes it as before, a small proof only for a portal kind

Fixed acquisition order prevents deadlock: an exclusive holder takes box.lock.d
FIRST, then slot-0, slot-1, (portal), and releases in reverse; two exclusives
serialise on box.lock.d, an exclusive waits for both slots to drain, small
proofs only ever contend on the slots.  Starvation-freedom (D76b-1, Expert
Coder 2026-09-19): an exclusive waiter KEEPS box.lock.d while it waits for the
slots to drain, and a small proof yields (does not try a slot) whenever
box.lock.d exists -- so newcomers cannot win a freed slot ahead of the
exclusive.  Slot holders never wait on anything while holding a slot, so a
pending exclusive cannot deadlock them.  A lock whose owner pid is dead is
reaped (logged + alerted), never waited on forever.

CLI (the same helper for scripts):

  vp_box_lock.py exclusive|slot [--cn CN] [--wait-max-min N] [--proof-id ID] -- <command ...>
  vp_box_lock.py status [--cn CN]

runs <command> holding the lock(s), exit status = the command's; 75 (EX_TEMPFAIL)
when the lock could not be taken within --wait-max-min.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

EXCLUSIVE, SLOT = "exclusive", "slot"
BOX = "box.lock.d"
SLOTS = ("box.slot-0.lock.d", "box.slot-1.lock.d")
PORTAL = "portal.lock.d"
LOCK_NAMES = (BOX,) + SLOTS + (PORTAL,)
STALE_S = 5 * 60
TARGETED_MAX_PATHS = 8
EX_TEMPFAIL = 75
DEFAULT_CN = os.environ.get("VP_CN") or str(Path(__file__).resolve().parents[2])


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


def tier_for(kind, paths, proof_cfg=None):
    """exclusive for a full suite or a targeted run over more than
    proof.targeted_max_paths (8) paths; slot otherwise"""
    if kind == "full":
        return EXCLUSIVE
    v = (proof_cfg or {}).get("targeted_max_paths")
    try:
        cap = TARGETED_MAX_PATHS if v is None else int(v)     # 0 = every targeted run is exclusive
    except (TypeError, ValueError):
        cap = TARGETED_MAX_PATHS
    return EXCLUSIVE if len(paths or []) > cap else SLOT


def names_for(tier, proof_kind="platform"):
    """the locks a holder needs, in acquisition order (release is the reverse);
    a slot holder's list carries BOTH slots: it takes whichever is free"""
    if tier == EXCLUSIVE:
        return [BOX] + list(SLOTS) + [PORTAL]
    return list(SLOTS) + ([PORTAL] if proof_kind == "portal" else [])


class BoxLocks(object):
    def __init__(self, cn, sha, proof_id, log=None, tier=EXCLUSIVE, proof_kind="platform",
                 alert=None, alive=_pid_alive):
        self.cn = Path(cn)
        self.sha = sha
        self.proof_id = proof_id
        self.tier = tier
        self.proof_kind = proof_kind
        self.token = uuid.uuid4().hex
        self.held = []
        self.log = log or (lambda s: None)
        self.alert = alert or (lambda kind, text: None)
        self.alive = alive
        self.names = names_for(tier, proof_kind)

    def _owner_path(self, name):
        return self.cn / name / "owner.json"

    def _try_one(self, name):
        d = self.cn / name
        try:
            os.mkdir(str(d))
        except FileExistsError:
            op = self._owner_path(name)
            try:
                owner = json.loads(op.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                owner = {}
            pid = owner.get("pid")
            ts = owner.get("mono_epoch") or 0
            if pid and self.alive(pid):
                return False, "held by pid %s (%s, %s)" % (pid, owner.get("proof_id"), owner.get("tier"))
            if not pid or (time.time() - float(ts or 0)) > STALE_S:
                # dead holder (or an ownerless dir older than STALE_S): reap it, say so
                why = "owner pid %s is dead" % pid if pid else "no live owner for > %ds" % STALE_S
                self.log("reaping %s: %s (owner=%s)" % (name, why, owner))
                self.alert("BOX_LOCK_REAPED", "%s reaped for %s: %s; owner was %s" % (name, self.proof_id, why,
                                                                                       json.dumps(owner)))
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
            "tier": self.tier, "owner": "vp_box_lock"}, indent=2), encoding="utf-8")
        self.held.append(name)
        return True, ""

    def _box_pending(self):
        """box.lock.d exists (an exclusive holds or waits): small proofs yield"""
        return (self.cn / BOX).exists()

    def _round(self):
        """one attempt in the fixed order; -> (ok, reason).  An exclusive keeps
        box.lock.d between rounds (its claim on the box); everything else is
        all-or-nothing -- a partial hold is released before waiting."""
        reason = ""
        if self.tier == EXCLUSIVE:
            for name in self.names:
                if name in self.held:
                    continue
                ok, reason = self._try_one(name)
                if not ok:
                    return False, reason
            return True, ""
        if self._box_pending():
            return False, "yielding to the exclusive holder/waiter of %s" % BOX
        got_slot = False
        for name in SLOTS:
            ok, reason = self._try_one(name)
            if ok:
                got_slot = True
                break
        if not got_slot:
            return False, "both slots busy: %s" % reason
        for name in self.names:
            if name in SLOTS:
                continue
            ok, reason = self._try_one(name)
            if not ok:
                return False, reason
        return True, ""

    def acquire(self, wait_max_s, sleep=time.sleep):
        deadline = time.monotonic() + wait_max_s
        while True:
            ok, reason = self._round()
            if ok:
                return True, ""
            # never hold a slot while waiting; an exclusive keeps only box.lock.d
            self.release(keep=(BOX,) if self.tier == EXCLUSIVE else ())
            if time.monotonic() > deadline:
                self.release()
                return False, reason
            sleep(5)

    def release(self, keep=()):
        for name in reversed(list(self.held)):
            if name in keep:
                continue
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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tier", choices=[EXCLUSIVE, SLOT, "status"])
    ap.add_argument("--cn", default=DEFAULT_CN)
    ap.add_argument("--wait-max-min", type=float, default=90.0)
    ap.add_argument("--proof-id", default=None)
    ap.add_argument("--proof-kind", default="platform")
    ap.epilog = "everything after `--` is the command to run under the lock"
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = []
    if "--" in argv:
        cmd = argv[argv.index("--") + 1:]
        argv = argv[:argv.index("--")]
    args = ap.parse_args(argv)
    if args.tier == "status":
        print(json.dumps(lock_status(args.cn), indent=2))
        return 0
    if not cmd:
        ap.error("a command is required after --")
    pid_label = args.proof_id or ("%s-%s-%d" % (args.tier, os.path.basename(cmd[0]), os.getpid()))
    locks = BoxLocks(args.cn, sha=None, proof_id=pid_label, log=lambda s: print("vp_box_lock: " + s, file=sys.stderr),
                     tier=args.tier, proof_kind=args.proof_kind,
                     alert=lambda k, t: print("vp_box_lock ALERT %s: %s" % (k, t), file=sys.stderr))
    ok, why = locks.acquire(args.wait_max_min * 60)
    if not ok:
        print("vp_box_lock: could not take %s within %.0f min: %s" % (args.tier, args.wait_max_min, why),
              file=sys.stderr)
        return EX_TEMPFAIL
    try:
        return subprocess.call(cmd)
    finally:
        locks.release()


if __name__ == "__main__":
    sys.exit(main())
