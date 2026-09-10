"""A restart must not be the same command as "destroy every in-flight gate and audit".

BOSS, 2026-09-07: the running daemon (pid 36697, started 06:18:02) was executing code older than
dispatcher.py's mtime (06:24:18), so a PROGRESS_STOP fix that was written, applied and green was NOT
running. The only symptom was EXEC-M sitting idle for 27 minutes holding nothing while an eligible
item was queued to it. Staleness is self-concealing: a daemon on old code behaves plausibly.

The obvious fix — restart — was unsafe: three live children were riding on that daemon (a merge gate
20 minutes into a box wait, two Codex audits), and `stop` was a `launchctl bootout` followed by a
PATTERN KILL. He deferred the restart and spent ten minutes on ps archaeology to decide that.

Three properties here, and the third is the one that would have saved the ten minutes:
  * `stop` kills a RESOLVED PID, never a pattern. `pkill -f "dispatcher/dispatcher.py"` matches any
    process whose command line merely contains that path — including a shell that mentions it.
  * `restart` REFUSES while children are live, names them with their ages, and needs --force.
  * `status` compares the daemon's start time against dispatcher.py's mtime and says STALE.

Hermetic: a fake `ps` on PATH, a temp CN, and --dry-run so the no-children branch never stops a real
daemon. A guard whose safe path is untested is half a guard."""
import os, re, shutil, stat, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

CTL = os.path.join(HERE, os.pardir, "dispatcherctl.sh")
fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

ROOT = tempfile.mkdtemp(prefix="restartguard-")
os.makedirs(os.path.join(ROOT, "test-logs", "driver"))
os.makedirs(os.path.join(ROOT, "dispatcher"))
open(os.path.join(ROOT, "dispatcher", "dispatcher.py"), "w").write("# fake\n")

BUSY = """#!/bin/sh
case "$*" in
  *"-eo pid=,args="*) echo "  41487 /usr/bin/python3 /x/dispatcher/mergegate.py B.010.erasure --no-box"
                      echo "  47713 node /x/bin/codex exec -s workspace-write audit"
                      echo "  99999 /bin/zsh -c echo dispatcher/mergegate.py in a prompt" ;;
  *"-p 41487 -o comm="*) echo "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python" ;;
  *"-p 47713 -o comm="*) echo "node" ;;
  *"-p 99999 -o comm="*) echo "/bin/zsh" ;;
  *"-p 41487 -o etime="*) echo "   20:11" ;;
  *"-p 47713 -o etime="*) echo "   05:02" ;;
  *"-o args="*) echo "  (args)" ;;
esac
"""
# A FIXED daemon start time, so staleness is decided by the FIXTURE and never by how long the real
# daemon on this machine happens to have been up. Before this, `report_staleness` read the live
# process table through a bare `ps` that bypassed the stub, and the CONTROL check ("an up-to-date
# daemon is NOT called stale") went red on 2026-09-10 simply because the running daemon was a day
# older than `now - 86400`. The test was reporting the machine's uptime as a code defect.
T0 = 1788000000                      # a fixed instant; both mtimes below are set relative to IT
T0_LSTART = time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(T0))

IDLE = """#!/bin/sh
case "$*" in
  *"-eo pid=,args="*) echo "  99999 /bin/zsh -c echo dispatcher/mergegate.py in a prompt" ;;
  *"-p 99999 -o comm="*) echo "/bin/zsh" ;;
  *"-o lstart="*) echo "%s" ;;
esac
""" % T0_LSTART

def fake_ps(body, name="ps_fake"):
    p = os.path.join(ROOT, name)
    open(p, "w").write(body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p


def ctl(*args, ps=None):
    env = {**os.environ, "CN": ROOT}
    if ps:
        env["DISPATCHERCTL_PS"] = ps
    r = subprocess.run(["/bin/zsh", CTL, *args], capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr


# THE COMM STRING IS APPLE'S REAL ONE, capital P. The old fixture said `/usr/bin/python3`, and that
# lowercase invention is why this file was green on 2026-09-07 while `live_children` reported NOTHING
# against a live gate: `${c:t}` is `Python`, zsh `case` is case-sensitive, and `python*` never
# matched. Every gate was invisible to the guard, and the guard's whole job is to refuse a restart
# while one is running. A fixture that uses a name the real system does not produce tests nothing.
rc, out = ctl("restart", "--dry-run", ps=fake_ps(BUSY))
check("MUST-BITE  restart REFUSES while a gate and a Codex audit are live",
      rc == 1 and "REFUSE" in out, (rc, FL.flat(out)[:150]))
check("  it NAMES them with their ages — this is the ten minutes of ps archaeology BOSS did by hand",
      "41487" in out and "20:11" in out and "47713" in out, FL.flat(out)[:200])
check("MUST-BITE  a process that merely QUOTES the gate path in a prompt is NOT counted as a live "
      "child — our own instruction text matches our own monitoring greps",
      "99999" not in out, FL.flat(out)[:200])

# --- FAIL-CLOSED: what the check cannot classify, it must not wave through --------------------
UNKNOWN_COMM = """#!/bin/sh
case "$*" in
  *"-eo pid=,args="*) echo "  51515 /opt/weird/runtime /x/bin/codex exec -s workspace-write audit" ;;
  *"-p 51515 -o comm="*) echo "/opt/weird/runtime" ;;
  *"-p 51515 -o etime="*) echo "   03:00" ;;
  *"-o args="*) echo "  (args)" ;;
esac
"""
rc, out = ctl("restart", "--dry-run", ps=fake_ps(UNKNOWN_COMM, "ps_unknown"))
check("MUST-BITE  a candidate whose comm is NEITHER a known child NOR a known bystander is reported "
      "and the restart REFUSED — 'I found nothing' and 'there is nothing' must not be one output",
      rc == 1 and "UNCLASSIFIED" in out and "51515" in out, FL.flat(out)[:160])

PS_BROKEN = """#!/bin/sh
exit 1
"""
rc, out = ctl("restart", "--dry-run", ps=fake_ps(PS_BROKEN, "ps_broken"))
check("MUST-BITE  if ps ITSELF fails the restart is refused — a guard that cannot look must never "
      "report a clear board",
      rc == 1 and "cannot establish" in out, FL.flat(out)[:160])

rc, out = ctl("restart", "--dry-run", ps=fake_ps(IDLE, "ps_idle"))
check("MUST-BITE  CONTROL: with no live children it would proceed — a guard that refuses always is "
      "not a guard, it is an outage",
      rc == 0 and "would restart" in out, (rc, FL.flat(out)[:120]))

src = open(CTL, errors="ignore").read()
stop_block = src[src.index("  stop)"):src.index("  restart)")]
# Strip comments first. The block's own comment explains why pkill is banned, and a source-read
# that matches its own explanation gets "fixed" by deleting the explanation — which is how the
# reason for a rule disappears while the rule survives.
code_lines = [l for l in stop_block.splitlines() if not l.strip().startswith("#")]
check("MUST-BITE  `stop` contains NO pattern kill — pkill -f on a shared box is the one thing every "
      "executor is told never to do, and it matches any shell that mentions the path",
      not any("pkill" in l for l in code_lines), [l.strip() for l in code_lines if "kill" in l])
check("  and the ban is still EXPLAINED in the block, not just enforced",
      "pkill" in stop_block, "the comment survives the check that would delete it")
check("  it kills a RESOLVED pid instead", 'kill "$p"' in stop_block,
      [l.strip() for l in stop_block.splitlines() if "kill" in l])
check("  and daemon_pid confirms the pid's own comm, so an argv match alone cannot select a victim",
      "-o comm=" in src[src.index("daemon_pid()"):src.index("live_children()")])

# staleness: a daemon started BEFORE its source was modified must be called stale, and one started
# after must not — the second is what stops the warning from being wallpaper.
os.utime(os.path.join(ROOT, "dispatcher", "dispatcher.py"), (T0 + 3600,) * 2)
rc, out = ctl("status", ps=fake_ps(IDLE, "ps_idle2"))
check("MUST-BITE  status says STALE when dispatcher.py is newer than the running daemon — the only "
      "symptom otherwise is something quietly not happening",
      "STALE" in out, FL.flat(out)[:200])
os.utime(os.path.join(ROOT, "dispatcher", "dispatcher.py"), (T0 - 3600,) * 2)
rc, out = ctl("status", ps=fake_ps(IDLE, "ps_idle3"))
check("MUST-BITE  CONTROL: an up-to-date daemon is NOT called stale — a warning on every status is "
      "a warning on none",
      "STALE" not in out and "current" in out, FL.flat(out)[:200])

shutil.rmtree(ROOT, ignore_errors=True)
print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
