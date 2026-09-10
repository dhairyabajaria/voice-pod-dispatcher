"""ITEM 5: the route detector enumerated the directory the spawns stopped writing to.

BOSS, 2026-09-10 21:46. Three CODEX-1 spawns ran (44738, 58305, 63949). The daemon logged
"route NOT MEASURED — WE could not observe what ran" for each and PARKED a QUESTION on
B.B2.renumber-015a — while all three rollouts sat on disk, unambiguous, under
`~/.codex/muse-homes/muse-go-1/sessions/2026/09/10/`.

`3b38f14` gave each Muse profile an isolated CODEX_HOME (museadapter.py:96), applied to the spawn at
:188. The resolvers kept reading the module constant `SESSIONS_ROOT` = `~/.codex/sessions`
(museadapter.py:37-38, :674, :711), and the reap called BOTH of them with no root at all
(dispatcher.py:2490, :2497). Measured 2026-09-10 21:50 on the live run — same cwd, same window, same
rule, changing only the directory:

    LEGACY  ~/.codex/sessions                       -> None
    PROFILE ~/.codex/muse-homes/muse-go-1/sessions  -> rollout-…T21-40-05-…jsonl
                                                      muse-go-1 / muse-spark-1.3-contributor / xhigh

The RULE was never wrong. Its POPULATION was — discovery by literal path, one layer above where the
same shape was found in the pgserver census an hour earlier. A false negative that blocks merges.

WHY THIS FILE EXISTS SEPARATELY FROM test_routewindow.py. That file's fixture points its ONE seam at
whatever root the product reads, so it passes on both sides of this fix and cannot discriminate. The
discriminator is a DECOY: a rollout under the legacy root with the same cwd and the same window,
which the reap must refuse to claim. Every check drives `codex_tick`; reverting `root=sroot` at
either call site must go red here.

Hermetic: temp CODEX_HOME, temp legacy root, no real ~/.codex, no process, no network.
"""
import importlib.util, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, os.pardir))
import museadapter as M                                        # noqa: E402

P, F = 0, []
def ok(c, w):
    global P
    if c: P += 1; print(f"PASS {w}")
    else: F.append(w); print(f"FAIL {w}")

TMP = tempfile.mkdtemp(prefix="profileroot-")
HOME = os.path.join(TMP, "codex")                     # stands in for ~/.codex
LEGACY_ROOT = os.path.join(HOME, "sessions")          # the shared root the spawns no longer write to
PROFILE = "muse-go-1"
PROFILE_ROOT = os.path.join(HOME, "muse-homes", PROFILE, "sessions")
WT = os.path.join(TMP, "wt"); os.makedirs(WT)
MODEL, PROV, EFFORT = "muse-spark-1.3-contributor", "muse-go-1", "xhigh"
LOGS = os.path.join(TMP, "logs"); os.makedirs(LOGS)


def rollout(root, name, cwd, when, sid=None, model=MODEL, provider=PROV, effort=EFFORT):
    d = os.path.join(root, "2026", "09", "10"); os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name + ".jsonl")
    sid = sid or name
    with open(path, "w") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {
            "session_id": sid, "model_provider": provider,
            "cli_version": "0.153.2", "cwd": cwd}}) + "\n")
        f.write(json.dumps({"type": "turn_context",
                            "payload": {"model": model, "effort": effort}}) + "\n")
    os.utime(path, (when, when))
    return path


def reap(log_text, *, cwd=WT, profile=PROFILE):
    spec = importlib.util.spec_from_file_location(
        "dspr" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod.museadapter.CODEX_HOME = HOME              # the profile home the reap must derive from
    mod.museadapter.SESSIONS_ROOT = LEGACY_ROOT    # the legacy root it must NOT read for a profile
    d = mod.Dispatcher.__new__(mod.Dispatcher)
    d.state = {"codex": {}, "muse": {}}
    d.cfg = {"codex_slots": ["CODEX-1"], "quota_hold_minutes": 60, "max_auto_continue": 3,
             "feed_queue": False}
    d.dry = False
    d.c = lambda k, dflt=None: d.cfg.get(k, dflt)
    d.codex_slots = lambda: ["CODEX-1"]
    d._logs, d._evs = [], []
    d.log = lambda *a, **k: d._logs.append(a[0] if a else "")
    d.emit = lambda *a: d._evs.append(a)
    lg = os.path.join(LOGS, f"r-{time.time_ns()}.log"); open(lg, "w").write(log_text)
    d.state["codex"] = {"CODEX-1": {
        "item": "C1", "pid": 999999, "log": lg, "cwd": cwd,
        "started_ms": int((time.time() - 60) * 1000), "profile": profile,
        "model_intended": MODEL, "provider_intended": PROV, "effort_intended": EFFORT}}
    q = {"items": [{"id": "C1", "status": "dispatched", "dispatched_to": "CODEX-1"}]}
    pend = {}
    d.codex_tick(q, pend, {}, 0, 0)
    return d, q["items"][0], d._evs, d._logs


REPORT = "codex\nran the sweep\nREPORT READY: 40 candidate rows, the seed is REFUTED\n"
NOW = time.time()

# ---------------------------------------------------------- POSITIVE CONTROL: BOSS's three runs
# The rollout exists ONLY under the profile's own home, with no session id in the log — the exact
# shape of 21-21-30 / 21-33-26 / 21-40-05.
rollout(PROFILE_ROOT, "rollout-2026-09-10T21-40-05-01a08c15", WT, NOW - 30)
d, it, evs, logs = reap(REPORT)
ok(it.get("route_verified") is True and not it.get("route_unverified"),
   "PC MUST BITE: a rollout written under the PROFILE'S OWN home resolves and the route is "
   "VERIFIED. This is BOSS's 21:40:05 run, which the daemon reported as NOT MEASURED while the "
   "file sat on disk one directory away")
ok(it["status"] == "reported" and not any(e[0] == "REFUSED" for e in evs),
   "  and the finished work is REPORTED, not parked behind an instrument failure")

# ------------------------------------------------------- NEGATIVE CONTROL: the legacy decoy
# Same cwd, same window, same model and provider — but under the SHARED legacy root, which an
# isolated-home spawn cannot have written. Claiming it would be a route verified against a file
# belonging to somebody else's process.
# THE PROFILE ROOT MUST STILL EXIST AND BE EMPTY. First cut used rmtree, and the negative control
# then PASSED against a deliberately reverted call site — because a missing profile dir makes
# `sessions_root_for` return None and G4 skips the fallback entirely. The check was shielded by a
# different guard and proved nothing about the one in its name. Measured, not reasoned: with the
# mutation applied it read 12 passed / 1 failed, and the 1 was the positive control.
for _f in os.listdir(os.path.join(PROFILE_ROOT, "2026", "09", "10")):
    os.remove(os.path.join(PROFILE_ROOT, "2026", "09", "10", _f))
rollout(LEGACY_ROOT, "rollout-2026-09-10T21-40-05-decoy", WT, NOW - 30)
d, it, evs, logs = reap(REPORT)
ok(it.get("route_verified") is not True,
   "NC MUST BITE: a rollout under the LEGACY root with the same cwd and window is NOT claimed for a "
   "spawn that ran with an isolated home. Reverting `root=sroot` at either call site makes this go "
   "green against a file the run never wrote — a verified route sourced from another process")
ok(it["status"] == "reported",
   "  and it is still not marked broken: NOT MEASURED is about our instrument, never about the work")
ok(any("NOT MEASURED" in l for l in logs),
   "  the operator line says NOT MEASURED")
ok(any(PROFILE in l and "muse-homes" in l for l in logs),
   "G3 MUST BITE: and the refusal NAMES THE DIRECTORY IT SEARCHED. A bare 'not measured' is exactly "
   "how this bug stayed invisible for three runs — the next instance has to be readable from the "
   "line itself")

# ------------------------------------------------- G2: never a union, even when both roots have one
rollout(PROFILE_ROOT, "rollout-2026-09-10T21-40-05-01a08c15", WT, NOW - 30)
d, it, evs, logs = reap(REPORT)
ok(it.get("route_verified") is True and len(logs) >= 0,
   "G2: with a rollout in BOTH roots the profile one wins and there is no ambiguity — a union "
   "search would find two, and 'refuse on >1' would then turn a correct resolution into a refusal")

# ------------------------------------- G4: a profile with no sessions dir is UNMEASURED, not legacy
d, it, evs, logs = reap(REPORT, profile="muse-go-3")     # no muse-homes/muse-go-3 in this fixture
ok(it.get("route_verified") is not True,
   "G4 MUST BITE: a profile whose home has no sessions directory does NOT fall back to the shared "
   "legacy root. A silent fallback restores this exact defect and hands back a confident answer "
   "from a directory the profile never wrote to")
ok(any("muse-go-3" in l and ("never written" in l or "no sessions directory" in l) for l in logs),
   "  and it says which profile and which path, in words")

# --------------------------------------------------------------- G1/G5: the legacy profile is legacy
# LEGACY has no isolated home — it is the one route that legitimately reads the shared root.
lg_root, why = M.sessions_root_for(M.LEGACY, home=HOME)
ok(lg_root == M.SESSIONS_ROOT and not why,
   "G1: the LEGACY route keeps the shared root, because it is the one profile that never got an "
   "isolated home — named explicitly rather than falling out of a missing directory")
pr, why = M.sessions_root_for(PROFILE, home=HOME)
ok(pr == PROFILE_ROOT and not why, "  and a named profile resolves to its own home")

# ------------------------------------------------- the late-binding trap, not reintroduced
saved = M.CODEX_HOME
try:
    M.CODEX_HOME = HOME
    r, _ = M.sessions_root_for(PROFILE)
    ok(r == PROFILE_ROOT,
       "MUST BITE: `home` is read at CALL time, not bound as a default argument at import. This "
       "module already carries that scar at rollout_for_session, where an override that looked "
       "applied was silently ignored — writing it the same way again would be the second instance")
finally:
    M.CODEX_HOME = saved

# ------------------------------------------------------- G7: the resolver never reads credentials
os.makedirs(os.path.join(HOME, "muse-homes", PROFILE), exist_ok=True)
open(os.path.join(HOME, "muse-homes", PROFILE, "auth.json"), "w").write('{"OPENAI_API_KEY":"x"}')
d, it, evs, logs = reap(REPORT)
blob = json.dumps([it, evs, logs], default=str)
ok("OPENAI_API_KEY" not in blob and '"x"' not in blob,
   "G7: the profile home holds auth material and the resolver reads only sessions/**/*.jsonl under "
   "it. No credential name and no credential value reaches the item, the events or the log")

print(f"\n{P} passed, {len(F)} failed")
for x in F: print("  FAILED:", x)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if F else 0)
