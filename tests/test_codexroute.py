"""codex_spawn's routing — the wiring that makes museadapter's guards the ones that actually run.

BOSS, 2026-09-08, after checking rather than assuming: `grep -c "museadapter\\|run_attempt"
dispatcher.py` returned 0. The adapter was proved and the daemon could not reach it — "a component
that exists and is proved, on a path that does not reach it, is the same shape as the board
reporting what it sent". This file exists so that cannot silently become true again.

Four properties, each of which failed silently before the wiring:
  * with nothing configured, a slot keeps the OLD Astra route. Wiring must not re-route every
    existing dispatch as a side effect.
  * a profile route passes `-p` and no effort flag. The profile carries xhigh; the daemon's global
    default is medium; passing both downgrades every Muse worker with no error at all.
  * the log header carries the RESOLVED pair. A global header logs a Muse run as Astra, and every
    later attribution taken from those headers is wrong — a run that succeeds under a false name.
  * a missing credential refuses BEFORE the worktree add and the uv sync, not after paying for both.

Hermetic: no codex binary, no worktree, no network. Popen and every git/uv call are stubbed.
"""
import importlib.util, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# NOTE what is deliberately NOT here: the dispatcher directory. dispatcher.py must put its own
# directory on sys.path, because every test loads it by path and nothing else will. The first cut of
# this file added that path itself and passed while eleven other test files died on
# ModuleNotFoundError — a fixture that repairs the very condition it exists to observe.
import importlib
M = importlib.import_module("museadapter") if "museadapter" in sys.modules else None
if M is None:
    _spec = importlib.util.spec_from_file_location(
        "museadapter", os.path.join(HERE, os.pardir, "museadapter.py"))
    M = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(M)

P, FAILED = 0, []
def ok(cond, what):
    global P
    if cond: P += 1; print(f"PASS {what}")
    else: FAILED.append(what); print(f"FAIL {what}")

TMP = tempfile.mkdtemp(prefix="codexroute-")
HOME = os.path.join(TMP, "codex"); os.makedirs(HOME)
open(os.path.join(HOME, "muse-go-1.config.toml"), "w").write(
    'model = "muse-spark-1.3-contributor"\nmodel_provider = "muse-go-1"\n'
    'model_reasoning_effort = "xhigh"\n[model_providers.muse-go-1]\nenv_key = "OPENCODE_GO_KEY_1"\n')
M.CODEX_HOME = HOME          # route_flags' default home, so the daemon resolves our fixture profiles

WT = os.path.join(TMP, "wt"); os.makedirs(os.path.join(WT, "platform", ".venv", "bin"))
LOGS = os.path.join(TMP, "logs"); os.makedirs(LOGS)
ITEMS = os.path.join(TMP, "items"); os.makedirs(ITEMS)
for _id in ("C1", "C2", "C3"):
    open(os.path.join(ITEMS, _id + ".md"), "w").write("the brief")


class FakeProc:
    pid = 9911


def daemon(cfg, env, spawned):
    spec = importlib.util.spec_from_file_location(
        "dsp" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
    DP = importlib.util.module_from_spec(spec); spec.loader.exec_module(DP)
    DP.CODEX_DIR = LOGS
    DP.ITEMS_DIR = ITEMS
    DP.museadapter.CODEX_HOME = HOME
    # every expensive real thing, stubbed: the venv probe passes, uv and git never run, Popen records
    DP.os.path.exists = lambda p: True
    class R:  # a subprocess result that always succeeds
        returncode = 0; stdout = ""; stderr = ""
    DP.subprocess.run = lambda *a, **k: R()
    DP.subprocess.Popen = lambda cmd, **k: (spawned.update(cmd=cmd, kw=k), FakeProc())[1]
    dp = DP.Dispatcher.__new__(DP.Dispatcher)
    dp.dry = False; dp.observing = False
    dp.cfg = cfg
    dp.state = {"codex": {}}
    dp.log = lambda *a, **k: logs.append(a[0] if a else "")
    dp.emit = lambda *a: events.append(a)
    dp.escalate = lambda *a: None
    dp._env = env
    return DP, dp


BASE_CFG = {"codex_bin": "/x/bin/codex", "codex_model": "gpt-6-astra", "codex_effort": "medium"}

def spawn(cfg_extra=None, item_extra=None, env=None):
    global logs, events
    logs, events = [], []
    cfg = dict(BASE_CFG); cfg.update(cfg_extra or {})
    item = {"id": "C1", "worktree": WT, "lane": "lane/c1", "title": "t"}
    item.update(item_extra or {})
    spawned = {}
    DP, dp = daemon(cfg, env, spawned)
    real_environ = dict(DP.os.environ)
    if env is not None:
        DP.os.environ.clear(); DP.os.environ.update(env)
    try:
        rc = dp.codex_spawn("CODEX-1", item)
    finally:
        DP.os.environ.clear(); DP.os.environ.update(real_environ)
    return rc, spawned, item, dp, logs, events


# ------------------------------------------------------------------ the wiring exists at all
src = open(os.path.join(HERE, os.pardir, "dispatcher.py")).read()
ok("sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))" in src,
   "MUST BITE: dispatcher.py puts its OWN directory on sys.path, so `import museadapter` survives "
   "being loaded by path — which is how all 62 test files load it")
ok("import museadapter" in src and "museadapter.select_profile(" in src
   and "museadapter.route_flags(" in src and "museadapter.route_matches(" in src,
   "MUST BITE: dispatcher.py actually imports and calls museadapter — the check BOSS ran by hand "
   "(grep -c returned 0) is now a test that fails if the wiring is removed")

# ---------------------------------------------------------------- default: nothing is re-routed
rc, sp, item, dp, lg, ev = spawn(env={"PATH": "/usr/bin"})
cmd = sp["cmd"]
ok(rc is True and "-m" in cmd and cmd[cmd.index("-m") + 1] == "gpt-6-astra",
   "MUST BITE: with no codex_routes and no item profile, the slot keeps the historic Astra model")
ok("model_reasoning_effort=medium" in cmd,
   "the legacy route keeps its global effort exactly as before")
ok("-p" not in cmd, "the legacy route passes no profile")
hdr = open(dp.state["codex"]["CODEX-1"]["log"]).readline()
ok("profile=legacy-astra" in hdr and "gpt-6-astra" in hdr, "the legacy header names the legacy route")

# --------------------------------------------------------------------- a profile route, per slot
rc, sp, item, dp, lg, ev = spawn({"codex_routes": {"CODEX-1": "muse-go-1"}},
                                 env={"PATH": "/usr/bin", "OPENCODE_GO_KEY_1": "sk-x"})
cmd = sp["cmd"]
ok(rc is True and "-p" in cmd and cmd[cmd.index("-p") + 1] == "muse-go-1",
   "a codex_routes entry puts --profile on the argv")
ok("-m" not in cmd, "MUST BITE: a profile route passes NO -m — the profile names the model")
ok(not any("model_reasoning_effort" in str(x) for x in cmd),
   "MUST BITE: a profile route passes NO effort flag. The profile carries xhigh and the daemon's "
   "global default is medium: passing it would downgrade every Muse worker with no error at all")
ok(sp["kw"]["env"].get("OPENCODE_GO_KEY_1") == "sk-x",
   "the child gets the selected account's credential")
hdr = open(dp.state["codex"]["CODEX-1"]["log"]).readline()
ok("profile=muse-go-1" in hdr and "muse-spark-1.3-contributor" in hdr and "gpt-6-astra" not in hdr,
   "MUST BITE: the header carries the RESOLVED pair. A global header logs a Muse run as Astra and "
   "every attribution taken from it afterwards is wrong")
run = dp.state["codex"]["CODEX-1"]
ok(run["profile"] == "muse-go-1" and run["provider_intended"] == "muse-go-1"
   and run["effort_intended"] == "xhigh",
   "the run record carries what this dispatch INTENDED, so the reap can check what actually ran")

rc, sp, item, dp, lg, ev = spawn(item_extra={"profile": "muse-go-1"},
                                 env={"PATH": "/usr/bin", "OPENCODE_GO_KEY_1": "sk-x"})
ok("-p" in sp["cmd"], "an item's own profile selects the route without any slot config")

# ------------------------------------------------------------------- refusal before the paid work
rc, sp, item, dp, lg, ev = spawn({"codex_routes": {"CODEX-1": "muse-go-1"}}, env={"PATH": "/usr/bin"})
ok(rc is False and not sp and item["status"] == "broken",
   "MUST BITE: no credential -> nothing is spawned")
ok("route" in (item.get("error") or "") and any(k[0] == "ERROR" for k in ev),
   "the refusal names the route and reaches the board, rather than a silent DISPATCHED")
rc, sp, item, dp, lg, ev = spawn({"codex_routes": {"CODEX-1": "muse-go-404"}}, env={"PATH": "/usr/bin"})
ok(rc is False and "muse-go-404" in (item.get("error") or ""),
   "an unknown profile is refused by name, before a worktree and a uv sync are paid for")

# ---------------------------------------------------- what the wiring deliberately does NOT claim
ok("read_result" not in src,
   "the daemon does NOT use the structured-result half: its workers report by writing REPORT READY "
   "in a log, not by writing a schema-shaped file, so correlating a terminal result needs the "
   "worker protocol changed first (§6.2). Claiming it here would be a guard that cannot fire")

# ------------------------------------------------- the selection policy reaches the SPAWN PATH
ok("museadapter.select_profile(" in src and 'mstate["last"] = profile' in src
   and "museadapter.mark_unhealthy(" in src,
   "MUST BITE: codex_spawn SELECTS (rotates, steps over a held key) rather than only routing, and "
   "persists `last` in the daemon's state — kept in a local it would restart at the head each tick")

for w in ("muse-go-1.config.toml", "muse-go-2.config.toml", "muse-go-3.config.toml"):
    open(os.path.join(HOME, w), "w").write(
        'model = "muse-spark-1.3-contributor"\nmodel_provider = "%s"\n'
        'model_reasoning_effort = "xhigh"\n[model_providers.%s]\nenv_key = "OPENCODE_GO_KEY_%s"\n'
        % (w[:9], w[:9], w[7]))
ENV3 = {"PATH": "/usr/bin", "OPENCODE_GO_KEY_1": "k1", "OPENCODE_GO_KEY_2": "k2",
        "OPENCODE_GO_KEY_3": "k3"}
picked, state = [], {}
for _ in range(3):
    global logs, events
    logs, events = [], []
    cfg = dict(BASE_CFG, codex_routes={"CODEX-1": "rotate"})
    spawned = {}
    DP, dp = daemon(cfg, ENV3, spawned)
    dp.state["muse"] = state                      # the daemon's state, carried across ticks
    real = dict(DP.os.environ); DP.os.environ.clear(); DP.os.environ.update(ENV3)
    try:
        dp.codex_spawn("CODEX-1", {"id": "C1", "worktree": WT, "lane": "l", "title": "t"})
    finally:
        DP.os.environ.clear(); DP.os.environ.update(real)
    picked.append(spawned["cmd"][spawned["cmd"].index("-p") + 1])
    state = dp.state["muse"]
ok(picked == ["muse-go-1", "muse-go-2", "muse-go-3"],
   "MUST BITE: three consecutive dispatches through the REAL spawn path land on three different "
   "keys — the owner's distribution instruction, on the path, not in a helper nobody calls")

logs, events = [], []
spawned = {}
DP, dp = daemon(dict(BASE_CFG, codex_routes={"CODEX-1": "muse-go-1"}), ENV3, spawned)
dp.state["muse"] = M.mark_unhealthy({}, "muse-go-1", M.AUTH, why="refused earlier")
real = dict(DP.os.environ); DP.os.environ.clear(); DP.os.environ.update(ENV3)
try:
    dp.codex_spawn("CODEX-1", {"id": "C2", "worktree": WT, "lane": "l", "title": "t"})
finally:
    DP.os.environ.clear(); DP.os.environ.update(real)
got = spawned["cmd"][spawned["cmd"].index("-p") + 1]
ok(got != "muse-go-1" and any("stepped over" in str(x) for x in logs),
   "MUST BITE: a held key is stepped over BY THE DAEMON and the substitution is logged — a slot "
   "silently running on a key its config does not name is the failure this layer prevents")

logs, events = [], []
spawned = {}
DP, dp = daemon(dict(BASE_CFG, codex_routes={"CODEX-1": "muse-go-1"}), {"PATH": "/usr/bin"}, spawned)
dp.state["muse"] = {}
real = dict(DP.os.environ); DP.os.environ.clear(); DP.os.environ.update({"PATH": "/usr/bin"})
try:
    it = {"id": "C3", "worktree": WT, "lane": "l", "title": "t"}
    dp.codex_spawn("CODEX-1", it)
finally:
    DP.os.environ.clear(); DP.os.environ.update(real)
ok(it["status"] == "broken" and "muse-go-1" in dp.state["muse"].get("unhealthy", {}),
   "MUST BITE: a credential the launcher cannot find HOLDS the profile, so the next tick steps over "
   "it instead of repeating the same refusal forever")


print(f"\n{P} passed, {len(FAILED)} failed")
for f in FAILED: print("  FAILED:", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
