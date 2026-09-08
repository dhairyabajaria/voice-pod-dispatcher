"""REFUSED is the provider declining. NOT MEASURED is US failing to observe. They are not the
same fact and they are not about the same subject.

BOSS, 2026-09-08: the first real Muse sweep SUCCEEDED — a 26 KB report, two commits, a positive
control that fired 5/5, 40 candidate rows, a REJECTED section, and it refuted his own seed — and the
reap recorded the item `broken`, because the CLI printed no session id on stdout and `route_matches`
therefore returned NOT MEASURED. Conflating "they refused" with "we could not see" discards exactly
the output we most want to keep. He resolved that route by hand at 05:20:33 straight from the
rollouts directory: exactly one session in the run's window, cwd matching the worktree the daemon
created. That is available to the reap and is the fallback proved here.

Four properties, each of which was absent or wrong before this file:
  * `rollout_by_window` identifies a session with NO id, by time and cwd, and refuses on 0 and on
    >1 — the second-best match is not a match.
  * the cwd is REQUIRED. A bare time window matches every session that ran alongside ours, which on
    this machine includes zen sessions from a scratch workspace that are not ours.
  * the REAP reaches that fallback. Proving the resolver directly proves the resolver, which was
    never the thing at risk — so every reap property below is driven through `codex_tick`, and the
    call-site mutations are scored at the bottom of this file.
  * an unresolvable route blocks a MERGE and does NOT mark the work broken. Those are two separate
    assertions because the defect was that one of them was doing the other's job.

Hermetic: a temporary sessions root, a temporary log, no codex binary, no network, no real
~/.codex.
"""
import importlib, importlib.util, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# NOT the dispatcher directory: dispatcher.py must put its own directory on sys.path.
_spec = importlib.util.spec_from_file_location(
    "museadapter_rw", os.path.join(HERE, os.pardir, "museadapter.py"))
M = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(M)

P, FAILED = 0, []
def ok(cond, what):
    global P
    if cond: P += 1; print(f"PASS {what}")
    else: FAILED.append(what); print(f"FAIL {what}")

TMP = tempfile.mkdtemp(prefix="routewindow-")
ROOT = os.path.join(TMP, "sessions")
WT = os.path.join(TMP, "wt"); os.makedirs(WT)
OTHER = os.path.join(TMP, "other-wt"); os.makedirs(OTHER)
MODEL, PROV, EFFORT = "muse-spark-1.3-contributor", "muse-go-1", "xhigh"


def rollout(name, cwd, when, sid=None, model=MODEL, provider=PROV, effort=EFFORT):
    """A rollout file shaped like a real one: session_meta carries provider/cwd, turn_context the
    model and effort. The directory nesting is the real y/m/d, because the glob depends on it."""
    d = os.path.join(ROOT, "2026", "09", "08"); os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name + ".jsonl")
    sid = sid or name
    with open(path, "w") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {
            "session_id": sid, "model_provider": provider, "cli_version": "0.153.2", "cwd": cwd}}) + "\n")
        f.write(json.dumps({"type": "turn_context",
                            "payload": {"model": model, "effort": effort}}) + "\n")
    os.utime(path, (when, when))
    return path

NOW = time.time()
START_MS, END_MS = int((NOW - 300) * 1000), int(NOW * 1000)

# ---------------------------------------------------------------- the resolver, on its own -------
mine = rollout("rollout-mine", WT, NOW - 30)
rollout("rollout-elsewhere", OTHER, NOW - 30)                 # same window, someone else's cwd
rollout("rollout-old", WT, NOW - 7200)                        # our cwd, hours outside the window

got, why = M.rollout_by_window(WT, START_MS, END_MS, root=ROOT)
ok(got == mine, "MUST BITE: with no session id anywhere, exactly one rollout in the window carries "
                "our cwd and that is the one returned")
ok("time and cwd" in why and str(START_MS) in why,
   "  and the note says HOW it was resolved, because a route resolved this way must carry that "
   "wherever it is cited")

got2, why2 = M.rollout_by_window(OTHER, START_MS, END_MS, root=ROOT)
ok(got2 == os.path.join(ROOT, "2026", "09", "08", "rollout-elsewhere.jsonl"),
   "  CONTROL: the cwd is what discriminates — a different cwd resolves to a different session, so "
   "the match above was not just 'the only file there'")

second = rollout("rollout-twin", WT, NOW - 20)                # a second session, our cwd, in window
got3, why3 = M.rollout_by_window(WT, START_MS, END_MS, root=ROOT)
ok(got3 is None and "refusing to choose" in why3,
   "MUST BITE: two sessions in the window with our cwd is NOT an identification — it refuses "
   "instead of taking the newest, which is the guess that would have been wrong")
os.remove(second)

ok(M.rollout_by_window("", START_MS, END_MS, root=ROOT)[0] is None,
   "MUST BITE: with no cwd it REFUSES — a bare time window matches every session that ran "
   "alongside ours, including the zen sessions from a workspace that is not ours")
ok("refusing to guess" in M.rollout_by_window("", START_MS, END_MS, root=ROOT)[1],
   "  and it says why, rather than reading as 'no session found'")
ok(M.rollout_by_window(WT, 0, END_MS, root=ROOT)[0] is None
   and M.rollout_by_window(WT, END_MS, START_MS, root=ROOT)[0] is None,
   "MUST BITE: an absent or inverted window refuses too — an unusable window must not silently "
   "become an unbounded one")
ok(M.rollout_by_window(os.path.join(TMP, "nobody"), START_MS, END_MS, root=ROOT)[0] is None,
   "  CONTROL: a cwd nothing ran in finds nothing, so the match is not matching everything")
ok("0 rollout" not in M.rollout_by_window(os.path.join(TMP, "nobody"), START_MS, END_MS, root=ROOT)[1]
   and "fell in the window" in M.rollout_by_window(os.path.join(TMP, "nobody"), START_MS, END_MS,
                                                   root=ROOT)[1],
   "  and the miss distinguishes its own causes: it reports how many rollouts were in the window "
   "at all, so 'wrong cwd' does not read the same as 'wrong window'")

# a symlinked cwd is the same directory. The daemon records the path it launched with; the CLI
# records the path it resolved to, and on this machine /tmp is a symlink to /private/tmp.
LINK = os.path.join(TMP, "wt-link"); os.symlink(WT, LINK)
ok(M.rollout_by_window(LINK, START_MS, END_MS, root=ROOT)[0] == mine,
   "  the cwd comparison is by real path — /tmp is a symlink on this machine and a string compare "
   "would have missed every match")

# The module global must be honoured AT CALL TIME. It was a default argument bound at import, so
# setting museadapter.SESSIONS_ROOT was accepted and silently ignored — a config that looks applied
# and is not. Found by this file's first red, not by reading the source.
_saved = M.SESSIONS_ROOT
M.SESSIONS_ROOT = ROOT
try:
    ok(M.rollout_by_window(WT, START_MS, END_MS)[0] == mine
       and M.rollout_for_session("rollout-mine")[0] == mine,
       "MUST BITE: both resolvers read SESSIONS_ROOT at call time, so an override of the module "
       "global actually takes")
finally:
    M.SESSIONS_ROOT = _saved

# ---------------------------------------------------------------- through the REAP ---------------
LOGS = os.path.join(TMP, "logs"); os.makedirs(LOGS)
ITEMS = os.path.join(TMP, "items"); os.makedirs(ITEMS)
open(os.path.join(ITEMS, "C1.md"), "w").write("the brief")


def daemon():
    spec = importlib.util.spec_from_file_location(
        "dsprw" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod.museadapter.SESSIONS_ROOT = ROOT      # the ONE seam: never read the real ~/.codex/sessions
    d = mod.Dispatcher.__new__(mod.Dispatcher)
    d.state = {"codex": {}, "muse": {}}
    d.cfg = {"codex_slots": ["CODEX-1"], "quota_hold_minutes": 60, "max_auto_continue": 3,
             "feed_queue": False}
    d.dry = False
    d.c = lambda k, dflt=None: d.cfg.get(k, dflt)
    d.codex_slots = lambda: ["CODEX-1"]
    d.log = lambda *a, **k: d._logs.append(a[0] if a else "")
    d.emit = lambda *a: d._evs.append(a)
    d._logs, d._evs = [], []
    return mod, d


def reap(log_text, cwd=WT, sid=None, started_ms=None):
    mod, d = daemon()
    lg = os.path.join(LOGS, f"reap-{time.time_ns()}.log")
    open(lg, "w").write(log_text)
    d.state["codex"] = {"CODEX-1": {
        "item": "C1", "pid": 999999, "log": lg, "cwd": cwd,
        "started_ms": started_ms if started_ms is not None else int((time.time() - 60) * 1000),
        "profile": PROV, "model_intended": MODEL, "provider_intended": PROV,
        "effort_intended": EFFORT}}
    q = {"items": [{"id": "C1", "status": "dispatched", "dispatched_to": "CODEX-1"}]}
    pend = {}
    assert d.codex_slots(), "the reap harness must actually have a slot to reap"
    d.codex_tick(q, pend, {}, 0, 0)
    return d, q["items"][0], pend, d._evs, d._logs

REPORT = ("codex\nworked through the corpus\nREPORT READY: 40 candidate rows, positive control 5/5, "
          "the seed is REFUTED\n")

# The live case: a finished report, and NO session id anywhere in the log.
d, it, pend, evs, logs = reap(REPORT)
ok(it["status"] == "reported",
   "MUST BITE: a finished report with no session id in the log is REPORTED, not broken — this is "
   "the sweep that was thrown away")
ok(it.get("route_verified") is True and not it.get("route_unverified"),
   "MUST BITE: and its route is VERIFIED, resolved by time and cwd — the reap reaches the fallback, "
   "measured through codex_tick and not by calling the resolver")
ok(not any(e[0] == "REFUSED" for e in evs),
   "MUST BITE: nothing was refused, so nothing reaches the board as REFUSED")
ok(any("resolved without a session id" in l for l in logs),
   "  and the log says the route was established the long way, so a reader is not left wondering "
   "which route was checked")

# Same log, but nothing in the rollout directory can identify it: the instrument failed.
d, it, pend, evs, logs = reap(REPORT, cwd=os.path.join(TMP, "unknown-wt"))
ok(it["status"] == "reported",
   "MUST BITE: an unresolvable route STILL does not mark finished work broken — NOT MEASURED is a "
   "statement about our instrument, not about the work")
ok(it.get("route_unverified") and it.get("route_verified") is False,
   "MUST BITE: it is written onto the ITEM, which is what survives — the gate cannot re-derive it "
   "once the run state is gone and the window has passed")
ok(not any(e[0] in ("REFUSED", "ROUTE_MISMATCH") for e in evs),
   "  and it is neither a refusal nor a mismatch: both of those are claims about what happened, "
   "and we do not have one")
ok(any("NOT MEASURED" in l and "does not mark it broken" in l for l in logs),
   "  the operator-facing line says which of the two it is, in words")

# CONTROL: a route that IS measured and IS wrong must still fail, or the fix laundered mismatches.
rollout("rollout-zen", os.path.join(TMP, "zen-wt"), NOW - 30,
        model="other-model", provider="muse-zen-1")
d, it, pend, evs, logs = reap(REPORT, cwd=os.path.join(TMP, "zen-wt"))
ok(it["status"] == "broken" and it.get("route_mismatch") and not it.get("route_unverified"),
   "MUST BITE: CONTROL — a session that records a route nobody asked for is still a MISMATCH and "
   "still fails. The three states stay three; they were not collapsed into 'not broken'")
ok(any(e[0] == "ROUTE_MISMATCH" for e in evs),
   "  and it reaches the board as ROUTE_MISMATCH")

# CONTROL: when the CLI DOES print an id, the cheap path is used and the answer is the same.
# the sid is IN THE FILENAME, as it is on a real rollout — that is what rollout_for_session
# globs for, and a fixture without it exercises the fallback while claiming to test the id path.
SID = "01a07e26-ef35-7671-89a3-e795e0eaf007"
sid_roll = rollout("rollout-" + SID, WT, NOW - 25, sid=SID)
d, it, pend, evs, logs = reap(
    '{"type": "thread.started", "thread_id": "' + SID + '"}\n' + REPORT)
ok(it.get("route_verified") is True and not any("resolved without a session id" in l for l in logs),
   "  CONTROL: with an id on stdout the fallback is not used at all — it is a fallback, not a "
   "replacement for the measurement that works")

# ---------------------------------------------------------------- the gate row -------------------
MG = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location("mgrw", os.path.join(HERE, os.pardir, "mergegate.py")))
sys.modules["mgrw"] = MG
MG.__spec__.loader.exec_module(MG)

rows, warns = [], []
MG.run_route_provenance_row({"route_unverified": "no rollout carries this run"},
                            lambda n, p, det: rows.append((n, p, det)), warns.append)
ok(rows and rows[-1][1] is None,
   "MUST BITE: at the gate, NOT MEASURED records as NOT RUN — which blocks the merge exactly as an "
   "unrun check does, and never renders the word FAIL against work nobody measured")
rows.clear()
MG.run_route_provenance_row({"route_mismatch": "intended muse-go-1, session records muse-zen-1"},
                            lambda n, p, det: rows.append((n, p, det)), warns.append)
ok(rows and rows[-1][1] is False,
   "  CONTROL: a measured mismatch is a FAIL, because that one IS a claim about the candidate")
rows.clear()
MG.run_route_provenance_row({"route_verified": True},
                            lambda n, p, det: rows.append((n, p, det)), warns.append)
ok(rows and rows[-1][1] is True, "  CONTROL: a verified route passes")
rows.clear(); warns.clear()
MG.run_route_provenance_row({"dispatched_to": "CODEX-1"},
                            lambda n, p, det: rows.append((n, p, det)), warns.append)
ok(not rows and warns and "NO ROW" in warns[-1],
   "  a row dispatched before this existed is WARNED, not failed: turning every older codex row "
   "INCOMPLETE would change the verdict of work that predates the check")
rows.clear(); warns.clear()
MG.run_route_provenance_row({"executor": "EXEC-A"},
                            lambda n, p, det: rows.append((n, p, det)), warns.append)
ok(not rows and not warns,
   "  and a non-codex row records nothing at all — this gate is about the codex route")

# ---------------------------------------------------------------- the gate CALL SITE -------------
# A SOURCE-LEVEL check, and named as one: proving run_route_provenance_row works proves the
# function, which was never the thing at risk — the open question is whether the gate reaches it,
# and _main needs a repo, a box and a git history to drive. So the call is asserted in the AST of
# the function that must make it. Weaker than a behavioural check and stronger than nothing; it
# catches the mutation that deletes the call site, which is the way this fix would be lost.
import ast as _ast
_tree = _ast.parse(open(os.path.join(HERE, os.pardir, "mergegate.py")).read())
_main_fn = next(n for n in _ast.walk(_tree)
                if isinstance(n, _ast.FunctionDef) and n.name == "_main")
_calls = {n.func.id for n in _ast.walk(_main_fn)
          if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)}
ok("run_route_provenance_row" in _calls,
   "MUST BITE: the gate's own body calls the provenance row — a check nothing calls is "
   "indistinguishable from no check")

print(f"\n{P} passed, {len(FAILED)} failed")
for f in FAILED: print("  FAILED:", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
