"""`dispatcherctl.sh restart` re-invokes a path that exists — from any working directory.

BOSS, 2026-09-08: `zsh dispatcherctl.sh restart` passed its live-children guard and then died with
`command not found`. Line 156 called `"$0"`, and `$0` is a bare relative name when the script is
invoked that way rather than executed by path.

THE REASON IT SURVIVED IS THE POINT. The `--dry-run` branch exits BEFORE the re-invocation, so the
only branch anybody could exercise without stopping the daemon was the one that never reaches the
bug. **The guard was tested and the action was not** — the class ruled on tonight: a proof set that
constructs its own environment must assert the environment, not build it, and here the proof set
simply never entered the room.

So the fix carries a seam: `selfpath` prints exactly the path `restart` would re-invoke, and these
checks run it the way BOSS ran it — by relative name, from an unrelated directory — and require the
answer to be a real file. Stopping the daemon to test the rest is not something a test may do.
"""
import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(HERE, os.pardir, "dispatcherctl.sh")
P, F = 0, []
def ok(c, w):
    global P
    if c: P += 1; print(f"PASS {w}")
    else: F.append(w); print(f"FAIL {w}")


def run(cwd, *args, script="dispatcherctl.sh"):
    return subprocess.run(["/bin/zsh", script, *args], cwd=cwd, capture_output=True, text=True)


d = os.path.abspath(os.path.join(HERE, os.pardir))
# invoked EXACTLY as BOSS invoked it: `zsh dispatcherctl.sh`, a bare relative name
r = run(d, "selfpath")
ok(r.returncode == 0 and os.path.isfile(r.stdout.strip()),
   "MUST BITE: invoked by bare relative name, the path restart re-invokes is a REAL FILE — a bare "
   "$0 here is 'dispatcherctl.sh', which exists only while the cwd happens to be right")
ok(os.path.samefile(r.stdout.strip(), CTL), "and it is THIS script, not something else on PATH")

# and from a directory that has no such file at all
tmp = tempfile.mkdtemp(prefix="ctlself-")
r2 = subprocess.run(["/bin/zsh", CTL, "selfpath"], cwd=tmp, capture_output=True, text=True)
ok(r2.returncode == 0 and os.path.isfile(r2.stdout.strip())
   and os.path.samefile(r2.stdout.strip(), CTL),
   "MUST BITE: from an unrelated cwd the same absolute path comes back — this is the case that "
   "actually broke, since restart runs wherever the operator happened to be standing")

src = open(CTL).read()
ok("SELF=${0:A}" in src,
   "the resolution is ${0:A}: it fixes a relative name AND a symlink, where a plain $PWD/$0 fixes "
   "only the first")
ok('"$SELF" stop; sleep 1; "$SELF" start' in src and '"$0" stop' not in src,
   "MUST BITE: the restart action itself uses the resolved path — the bug was in the ACTION, and a "
   "fix that only corrected the message would leave it exactly as broken")
ok("$0 restart --force" not in src,
   "the refusal message tells the operator a command that will work when they paste it")

# the guard still guards: --dry-run must still refuse when children are live, and it must still exit
# before doing anything. Asserted on the source because exercising it needs a live daemon.
i_dry = src.index('"${2:-}" == "--dry-run"')
ok(src.index('"$SELF" stop') > i_dry,
   "the dry-run branch still exits ahead of the re-invocation, so the guard's safe path stays safe")


# --------------------------------------------- `status` must date the board it prints
# 2026-09-08: pending.json was nineteen hours old and `status` printed every row as though it were
# current — fourteen executor rows of yesterday, and CODEX-1 idle over a live worker. The daemon
# side is fixed (it writes the board on the degraded path now), but a board can go stale for other
# reasons — a dead daemon, a full disk — and the reader must say how old the thing it is showing is.
import json as _json, subprocess as _sp, tempfile as _tf, time as _t, os as _os
_st = _tf.mkdtemp(prefix="ctlstatus-")
_json.dump({"updated": "2026-09-07 12:49:14", "pending": [],
            "executors": {"CODEX-1": "idle: no eligible CODEX item"}},
           open(_os.path.join(_st, "pending.json"), "w"))
_os.utime(_os.path.join(_st, "pending.json"), (_t.time() - 19 * 3600, _t.time() - 19 * 3600))
_r = _sp.run(["/bin/zsh", CTL, "status"], capture_output=True, text=True,
             env=dict(_os.environ, STATE=_st, VOICEPOD_STATE=_st))
_out = _r.stdout + _r.stderr
# NOT `"ago" in _out`: the heartbeat line prints "ago" on every run, so that check passed with the
# staleness warning mutated away — it could not distinguish its own cause. Caught by mutating the
# warning and watching this check stay green.
ok("MIN OLD" in _out and "pending for BOSS" in _out,
   "MUST BITE: `status` dates the board it prints, on the board's OWN line. A nineteen-hour-old "
   "snapshot rendered as the present is how fourteen fictional executor rows and an idle-over-live-"
   "work line survived a whole night of people reading them")
ok("snapshot from then, not now" in _out or "MIN OLD" in _out,
   "and it says plainly that the rows are from then rather than now, not merely a number the reader "
   "has to do arithmetic on")
shutil.rmtree(_st, ignore_errors=True)

print(f"\n{P} passed, {len(F)} failed")
for x in F: print("  FAILED:", x)
sys.exit(1 if F else 0)
