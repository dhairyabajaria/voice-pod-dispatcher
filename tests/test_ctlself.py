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
import os, subprocess, sys, tempfile

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

print(f"\n{P} passed, {len(F)} failed")
for x in F: print("  FAILED:", x)
sys.exit(1 if F else 0)
