"""Hermetic calibration for mergegate.run_merge_preflight: real git repos in a temp dir, no box,
no network, no model. Must-pass (a clean lane) AND must-bite (collision, conflict) — a check that
only bites passes on everything, and one that never bites is the gate hole it replaces."""
import importlib.util, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("mg", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mg"] = MG; spec.loader.exec_module(MG)

def git(*a, cwd): subprocess.run(["git"] + list(a), cwd=cwd, check=True, capture_output=True)
def write(root, rel, txt):
    p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(txt)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def scenario(build_lane):
    """-> (ok, detail) from one run_merge_preflight against a fresh trunk+lane pair."""
    root = tempfile.mkdtemp(prefix="preflight-")
    trunk = os.path.join(root, "trunk")
    os.makedirs(trunk); git("init", "-q", "-b", "plan010/rebuild", cwd=trunk)
    git("config", "user.email", "t@t", cwd=trunk); git("config", "user.name", "t", cwd=trunk)
    write(trunk, "platform/db/migrations/246_base.sql", "-- 246\n")
    write(trunk, "platform/core/db.py", "SHARED = 1\n")
    git("add", "-A", cwd=trunk); git("commit", "-qm", "base", cwd=trunk)
    lane = os.path.join(root, "lane")
    git("worktree", "add", "-q", "--detach", lane, "HEAD", cwd=trunk)
    build_lane(lane, trunk)
    git("add", "-A", cwd=lane); git("commit", "-qm", "lane work", cwd=lane)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=lane, capture_output=True, text=True).stdout.strip()
    MG.TRUNK, MG.CN = trunk, root
    got = {}
    MG.run_merge_preflight(lane, head, "TESTITEM", lambda n, ok, d: got.update(ok=ok, detail=d))
    left = os.path.exists(os.path.join(root, "voicepod-integ-TESTITEM"))
    st = subprocess.run(["git", "status", "--short"], cwd=trunk, capture_output=True, text=True).stdout.strip()
    shutil.rmtree(root, ignore_errors=True)
    return got.get("ok"), got.get("detail", ""), left, st

# 1. MUST-PASS: a lane that takes the next free number and edits nothing trunk touched.
def clean(lane, trunk):
    write(lane, "platform/db/migrations/247_lane.sql", "-- 247 lane\n")
    write(lane, "platform/core/lane_only.py", "x = 1\n")
ok, detail, left, st = scenario(clean)
check("MUST-PASS  clean lane merges", ok is True, detail)
check("  integration worktree removed", not left)
check("  trunk left untouched (no merge state, clean status)", st == "", repr(st))

# 2. MUST-BITE: trunk and lane both took 247, different files. Invisible to every lane-only check.
def collide(lane, trunk):
    write(trunk, "platform/db/migrations/247_trunk_took_it.sql", "-- 247 trunk\n")
    git("add", "-A", cwd=trunk); git("commit", "-qm", "trunk takes 247", cwd=trunk)
    write(lane, "platform/db/migrations/247_lane_also.sql", "-- 247 lane\n")
ok, detail, left, _ = scenario(collide)
check("MUST-BITE  same number, different filename FAILS", ok is False, detail[:200])
check("  names BOTH files", "247_lane_also.sql" in detail and "247_trunk_took_it.sql" in detail)
check("  reports the duplicate in the merged tree too", "DUPLICATE" in detail)
check("  integration worktree removed", not left)

# 3. MUST-BITE: a real merge conflict.
def conflict(lane, trunk):
    write(trunk, "platform/core/db.py", "SHARED = 2  # trunk\n")
    git("add", "-A", cwd=trunk); git("commit", "-qm", "trunk edits db.py", cwd=trunk)
    write(lane, "platform/core/db.py", "SHARED = 3  # lane\n")
ok, detail, left, st = scenario(conflict)
check("MUST-BITE  merge conflict FAILS", ok is False, detail[:200])
check("  lists the conflicted file", "platform/core/db.py" in detail)
check("  integration worktree removed after an aborted merge", not left)
check("  trunk left untouched", st == "", repr(st))

# 4. MUST-PASS: the shared 246 is NOT a collision (same number, same file, in both trees).
def shared_only(lane, trunk):
    write(lane, "platform/core/lane_only.py", "x = 1\n")
ok, detail, _, _ = scenario(shared_only)
check("MUST-PASS  a number shared by both trees is not a collision", ok is True, detail)

print("\nPREFLIGHT " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
