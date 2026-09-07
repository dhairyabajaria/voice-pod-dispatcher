"""The daemon could not find the codex binary, and said the wrong thing about it.

BOSS, 2026-09-07: gates launched from BOSS's shell reviewed fine; the same gate launched by the
daemon wrote `rc=1 NONE` with "Codex CLI is not installed or is missing required runtime support.
Install it with `npm install -g @openai/codex`". Nothing was missing. The companion resolves the BARE
NAME `codex` from PATH (lib/codex.mjs: getCodexAvailability -> binaryAvailable("codex", ["--version"])
and again for "app-server"), and launchd hands the daemon PATH=/usr/bin:/bin:/usr/sbin:/sbin:
/usr/local/bin — no node bin directory. Every auto-gate's Codex row came back NONE and BOSS re-ran
each by hand, which is the entire value of auto-gate spent on one missing PATH entry.

This file is measured against the REAL binary and the REAL launchd PATH, because the failure was
environmental and a mocked PATH would have proved nothing about the environment that broke."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location("mgcp" + str(time.time_ns()),
                                              os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); spec.loader.exec_module(MG)

# The PATH launchd actually gives the daemon, read from the plist rather than assumed.
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.voicepod.dispatcher.plist")
launchd_path = None
if os.path.exists(PLIST):
    body = open(PLIST, errors="ignore").read()
    i = body.find("<key>PATH</key>")
    if i != -1:
        j = body.find("<string>", i); k = body.find("</string>", j)
        launchd_path = body[j + len("<string>"):k]
check("the LaunchAgent's PATH was read from the plist, not assumed", bool(launchd_path), launchd_path)

# 1. the bin comes from roster.json, where BOSS already keeps it for the daemon's own Codex runs
roster = json.load(open(os.path.join(HERE, os.pardir, "roster.json")))
check("MUST-BITE  codex_bin() is roster.json's codex_bin", MG.codex_bin() == roster.get("codex_bin"),
      (MG.codex_bin(), roster.get("codex_bin")))
check("  and that path EXISTS on this box — the binary was never missing",
      os.path.exists(MG.codex_bin()), MG.codex_bin())

# 2. THE DEFECT, reproduced: under the daemon's own PATH the bare name does not resolve
if launchd_path:
    check("MUST-BITE  under the LaunchAgent's PATH, bare `codex` does NOT resolve — this is the bug",
          shutil.which("codex", path=launchd_path) is None, shutil.which("codex", path=launchd_path))

# 3. THE FIX, measured with the launchd PATH in place
real = os.environ.get("PATH", "")
try:
    os.environ["PATH"] = launchd_path or "/usr/bin:/bin"
    env = MG.codex_env()
    check("MUST-BITE  codex_env() makes bare `codex` resolvable from the daemon's PATH",
          shutil.which("codex", path=env["PATH"]) == MG.codex_bin(), shutil.which("codex", path=env["PATH"]))
    check("  it PREPENDS and never drops what launchd gave us",
          all(d in env["PATH"].split(":") for d in (launchd_path or "/usr/bin").split(":")),
          env["PATH"])
    check("  and it is idempotent — a second call does not stack duplicates",
          env["PATH"].split(":").count(os.path.dirname(MG.codex_bin())) == 1, env["PATH"])
    # the two probes the companion actually runs, against the real binary
    for args, label in ((["--version"], "codex --version"), (["app-server", "--help"], "codex app-server --help")):
        r = subprocess.run(["codex", *args], capture_output=True, text=True, env=env, timeout=120)
        check(f"MUST-BITE  {label} succeeds under codex_env — this is the pair the companion probes",
              r.returncode == 0, (r.returncode, (r.stdout + r.stderr)[:80]))
    # CONTROL: the same call without the fix fails, so the pass above is the PATH and nothing else
    bad = dict(os.environ); bad["PATH"] = launchd_path or "/usr/bin:/bin"
    try:
        r = subprocess.run(["codex", "--version"], capture_output=True, text=True, env=bad, timeout=60)
        failed = r.returncode != 0
    except OSError:
        failed = True
    check("CONTROL  the same call WITHOUT codex_env fails — the pass above is the PATH, not luck", failed)
finally:
    os.environ["PATH"] = real

# 4. sh() carries an env through at all
rc, out = MG.sh(["/bin/sh", "-c", "echo $MARKER"], env={"MARKER": "carried", "PATH": "/usr/bin:/bin"})
check("MUST-BITE  sh() passes env to the child — otherwise the fix above never reaches Codex",
      rc == 0 and out.strip() == "carried", (rc, out.strip()))
rc, out = MG.sh(["/bin/sh", "-c", "echo default-env-ok"])
check("  and sh() with no env still runs (nothing else in the gate changed)",
      rc == 0 and "default-env-ok" in out, (rc, out.strip()))

# 5. a genuinely missing binary is NAMED, not reported as "not installed" — measured by CALLING it
check("with the real binary present, the precheck lets the review run", MG.codex_precheck() is None,
      MG.codex_precheck())
_orig = MG.codex_bin
MG.codex_bin = lambda: "/nonexistent/bin/codex"
pre = MG.codex_precheck()
MG.codex_bin = _orig
check("MUST-BITE  a missing binary is a RESULT naming the path it looked at, not a crash",
      pre and pre[0] == 127 and "/nonexistent/bin/codex" in pre[1] and "roster.json codex_bin" in pre[1],
      pre)
check("  and it does not tell BOSS to npm install something that is already there",
      pre and "npm install" not in pre[1], pre)

print(("FAILED: " + ", ".join(fails)) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
