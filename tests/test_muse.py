#!/usr/bin/env python3
"""Proof set for museadapter.py — workstream D, Plan 003 §5.

Hermetic: no network, no codex binary, no box, no ~/.codex. Every profile, rollout, result file and
worker process is built under a temp directory the test owns.

WHAT THIS SET IS BUILT TO CATCH. Every check that matters drives `run_attempt`, the real entry point,
not the helper it exercises. A helper-only proof answers "does this guard work when reached", and the
guard was never the thing at risk — the open question is whether the path reaches it. The standing
rule from 2026-09-07 is that the mutation which must BITE is the one reverting the CALL SITE, so the
CALL-SITE section at the bottom names each line whose removal must redden this file, and the
mutation harness scores exactly those.
"""
import json, os, re, shutil, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import museadapter as M

P, F = 0, []
def ok(cond, what):
    global P
    if cond:
        P += 1; print(f"PASS {what}")
    else:
        F.append(what); print(f"FAIL {what}")

TMP = tempfile.mkdtemp(prefix="museadapter-test-")
HOME = os.path.join(TMP, "codex"); os.makedirs(HOME)
SESS = os.path.join(TMP, "sessions")
STATE = os.path.join(TMP, "state"); os.makedirs(STATE)

GO1 = """model = "muse-spark-1.3-contributor"
model_provider = "muse-go-1"
model_reasoning_effort = "xhigh"
[model_providers.muse-go-1]
env_key = "OPENCODE_GO_KEY_1"
"""
ZEN1 = """model = "muse-spark-1.3-contributor-free"
model_provider = "muse-zen-1"
[model_providers.muse-zen-1]
env_key = "OPENCODE_ZEN_KEY_1"
"""
open(os.path.join(HOME, "muse-go-1.config.toml"), "w").write(GO1)
open(os.path.join(HOME, "muse-go-2.config.toml"), "w").write(GO1.replace("go-1", "go-2").replace("KEY_1", "KEY_2"))
open(os.path.join(HOME, "muse-zen-1.config.toml"), "w").write(ZEN1)
open(os.path.join(HOME, "muse-broken.config.toml"), "w").write('model = "x"\n')

def rollout(sid, model="muse-spark-1.3-contributor", provider="muse-go-1", effort="xhigh",
            meta=True, turn=True):
    d = os.path.join(SESS, "2026", "09", "08"); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"rollout-2026-09-08T04-00-00-{sid}.jsonl")
    lines = []
    if meta:
        lines.append({"type": "session_meta", "payload": {"id": sid, "session_id": sid,
                      "model_provider": provider, "cli_version": "0.153.2", "cwd": "/w"}})
    if turn:
        pay = {"model": model}
        if effort:
            pay["effort"] = effort
        lines.append({"type": "turn_context", "payload": pay})
    open(p, "w").write("\n".join(json.dumps(x) for x in lines) + "\n")
    return p

def fake_runner(rc=0, out="", err="", write=None, capture=None):
    """A worker. `write` is (path_key, content) written where the -o flag points."""
    def run(argv, env, stdin_text, timeout):
        if capture is not None:
            capture.update(argv=argv, env=env, stdin=stdin_text, timeout=timeout)
        if write is not None:
            o = argv[argv.index("-o") + 1]
            open(o, "w").write(write if isinstance(write, str) else json.dumps(write))
        return rc, out, err, 4242
    return run

SID = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
STARTED = json.dumps({"type": "thread.started", "thread_id": SID})
GOOD = {"item": "C1", "session_id": SID, "outcome": "done", "candidate_sha": "abc1234",
        "summary": "table added, three readers repointed"}
ENV_OK = {"OPENCODE_GO_KEY_1": "sk-live-value", "PATH": "/usr/bin"}

def attempt(**kw):
    kw.setdefault("profile", "muse-go-1"); kw.setdefault("env", dict(ENV_OK))
    kw.setdefault("home", HOME); kw.setdefault("sessions_root", SESS)
    kw.setdefault("runner", fake_runner(0, STARTED, write=GOOD))
    return M.run_attempt(kw.pop("item", "C1"), kw.pop("attempt", 1), kw.pop("brief", "do the work"),
                         STATE, kw.pop("profile"), **kw)

# ------------------------------------------------------------------ profiles read, never guessed
s, why = M.profile_spec("muse-go-1", home=HOME)
ok(s and s["model"] == "muse-spark-1.3-contributor" and s["provider"] == "muse-go-1"
   and s["env_key"] == "OPENCODE_GO_KEY_1" and s["tier"] == "go", "profile_spec reads go-1's route")
ok(M.profile_spec("muse-zen-1", home=HOME)[0]["tier"] == "zen", "zen-1 is classed as the zen tier")
s2, why2 = M.profile_spec("muse-broken", home=HOME)
ok(s2 is None and "provider" in why2 and "env_key" in why2,
   "a profile missing provider/env_key is refused, and the refusal names what is missing")
ok(M.profile_spec("muse-go-9", home=HOME)[0] is None, "an absent profile file is refused")

# ---------------------------------------------------------------------------- preference and rotation
ok(M.preference_order() == list(M.GO_PROFILES), "Go only, in order, when zen is not allowed")
ok(M.preference_order(allow_zen=True)[:3] == list(M.GO_PROFILES),
   "zen is permitted but never preferred: all three Go profiles come first")
ok(M.preference_order(allow_zen=True)[3:] == list(M.ZEN_PROFILES), "zen fills the tail")
ok("muse-go-2" not in M.preference_order(dead=("muse-go-2",)),
   "a key seen refusing is SKIPPED, not retried")
o = M.preference_order()
ok(M.next_profile(o, "muse-go-1") == "muse-go-2" and M.next_profile(o, "muse-go-3") == "muse-go-1",
   "round-robin distributes across all three keys and wraps")
ok(M.next_profile(o, "muse-zen-1") == "muse-go-1",
   "a `last` outside the order restarts at the head rather than failing")
ok(M.next_profile([], "x") is None, "an exhausted order yields no profile, not a default one")

# --------------------------------------------------------------------- credentials: names, not values
e, why = M.child_env(s, base={"OPENCODE_GO_KEY_1": "v", "OPENCODE_GO_KEY_2": "other"})
ok(e and e.get("OPENCODE_GO_KEY_1") == "v", "the selected account's credential is carried")
ok(e and "OPENCODE_GO_KEY_2" not in e,
   "another account's credential is REMOVED from the child even when inherited")
e2, why2 = M.child_env(s, base={"OPENCODE_GO_KEY1": "unsuffixed-only"})
ok(e2 and e2.get("OPENCODE_GO_KEY_1") == "unsuffixed-only",
   "the KEY1 -> KEY_1 spelling gap is bridged IN THE CHILD ENV (§5 finding 1)")
before = dict(os.environ)
M.child_env(s, base={"OPENCODE_GO_KEY1": "v"})
ok(dict(os.environ) == before, "bridging never mutates this process's own environment")
e3, why3 = M.child_env(s, base={"OPENCODE_GO_KEY_1": "   "})
SENTINEL = "zzq-secret-7f3a"
ok(e3 is None and "missing or empty" in why3 and "nothing was spent" in why3,
   "an empty credential is refused BEFORE launch, and the refusal says nothing was spent")
ok(SENTINEL not in (M.child_env(s, base={"OPENCODE_GO_KEY_1": SENTINEL + " ", "X": ""})[1] or "")
   and SENTINEL not in json.dumps(M.child_env(s, base={"OPENCODE_GO_KEY_1": ""})[1]),
   "a refusal never quotes a credential value")
ok(M.child_env(M.profile_spec("muse-zen-1", home=HOME)[0], base=dict(ENV_OK))[0] is None,
   "a zen attempt with no ZEN key is refused before launch, not failed at the provider")

# ------------------------------------------------------------------------------------------- argv
a = M.launch_argv("muse-go-1", "/r/x.json", schema="/s.json")
ok(a[-1] == "-" and "--json" in a and "--strict-config" in a,
   "launch reads the brief from stdin explicitly, emits JSONL, and refuses unknown config")
ok(a[a.index("-p") + 1] == "muse-go-1" and a[a.index("-o") + 1] == "/r/x.json",
   "the profile and the result path are passed as measured")
ok("--last" not in a, "no --last on launch")
ok(not any("effort" in x for x in a) and "-m" not in a,
   "MUST BITE: launch passes NO effort and NO -m flag — the profile is the single source of truth, "
   "and a global effort flag would silently downgrade every Muse worker from xhigh to medium")
ok(not any("effort" in x for x in M.resume_argv("muse-go-1", SID, "/r/x.json")),
   "resume passes no effort flag either")
r = M.resume_argv("muse-go-1", SID, "/r/x.json")
ok(r.index("resume") > r.index("-o") and r[r.index("resume") + 1] == SID,
   "parent options precede `resume`, and the exact stored session id follows it (§5 finding 4)")
ok("--last" not in r, "resume NEVER uses --last")
try:
    M.resume_argv("muse-go-1", "", "/r/x.json"); ok(False, "an empty session id must refuse")
except ValueError as ex:
    ok("--last" in str(ex), "an empty session id refuses and says why --last is not the answer")
p1 = M.result_path_for(STATE, "C1", 1); time.sleep(0.001); p2 = M.result_path_for(STATE, "C1", 1)
ok(p1 != p2 and "attempt1" in os.path.basename(p1),
   "two runs of the SAME item and attempt get different result paths (§5 finding 2)")
_p = M.result_path_for(STATE, "C/1 ../x", 1)
ok(os.path.dirname(_p) == STATE and os.sep not in os.path.basename(_p),
   "an item id carrying separators and .. cannot walk out of the state directory")

# ------------------------------------------------------------------ reading what actually happened
ok(M.session_id_from_jsonl(STARTED) == SID, "the session id is read from thread.started")
ok(M.session_id_from_jsonl('{"type":"x"}\nnot json\n' + STARTED) == SID,
   "non-JSON and unrelated lines are skipped")
ok(M.session_id_from_jsonl(json.dumps({"type": "thread.started", "thread_id": "s1"})) == "s1",
   "a SHORT session id is read, not silently dropped by a length floor")
ok(M.session_id_from_jsonl("no events at all") is None,
   "no id line yields None — never a newest-file guess")
rp = rollout(SID)
got, why = M.rollout_for_session(SID, root=SESS)
ok(got == rp, "the rollout is found BY SESSION ID")
ok(M.rollout_for_session(None, root=SESS)[0] is None, "no session id refuses")
ok(M.rollout_for_session("nope", root=SESS)[0] is None, "an unmatched id refuses")
dup = rollout(SID).replace(SID, SID)  # a second file carrying the same id
shutil.copy(rp, rp.replace("04-00-00", "04-00-01"))
ok(M.rollout_for_session(SID, root=SESS)[0] is None, "TWO rollouts for one id refuses to choose")
os.remove(rp.replace("04-00-00", "04-00-01"))
act = M.resolved_route(rp)
ok(act.get("provider") == "muse-go-1" and act.get("model") == "muse-spark-1.3-contributor"
   and act.get("effort") == "xhigh",
   "provider comes from session_meta and model/effort from turn_context, as measured")
ok(M.route_matches(s, act)[0] is True, "a matching route verifies")
ok("SENT" in M.route_matches(s, act)[1],
   "a verified route says effort is what the CLI SENT, not what the provider honoured")
_down = M.resolved_route(rollout("0199a1b2-down-grad-0006-000000000006", effort="medium"))
_v, _n = M.route_matches(s, _down)
ok(_v is False and "EFFORT MISMATCH" in _n,
   "MUST BITE: the profile declares xhigh and the session sent medium -> refused, not passed. "
   "This is the silent downgrade a global effort flag causes")
_noe = M.resolved_route(rollout("0199a1b2-noef-fort-0007-000000000007", effort=None))
ok(M.route_matches(s, _noe)[0] is None,
   "a profile that declares an effort and a session that records none is NOT FULLY MEASURED")
ok(M.route_matches(M.profile_spec("muse-zen-1", home=HOME)[0], M.resolved_route(rollout("0199a1b2-zenn-oeff-0008-000000000008",
   model="muse-spark-1.3-contributor-free", provider="muse-zen-1", effort=None)))[0] is True,
   "a profile declaring NO effort is not held to one — the check follows the profile, not a constant")
bad = M.resolved_route(rollout("0199a1b2-zzz0-0000-0004-000000000004", model="muse-spark-1.3-contributor-free", provider="muse-zen-1"))
v, note = M.route_matches(s, bad)
ok(v is False and "muse-zen-1" in note and "muse-go-1" in note,
   "a Go attempt that landed on zen is a MISMATCH, and the note names both routes")
zs = M.profile_spec("muse-zen-1", home=HOME)[0]
ok(M.route_matches(zs, act)[0] is False,
   "a zen attempt that landed on Go is ALSO a mismatch — the check is the intended profile, not a tier")
v2, note2 = M.route_matches(s, M.resolved_route(rollout("0199a1b2-nome-ta00-0005-000000000005", meta=False, turn=False)))
ok(v2 is None and "NOT MEASURED" in note2,
   "unreadable metadata is UNMEASURED, never a pass")

# ------------------------------------------------------------------------------- terminal results
rpth = os.path.join(STATE, "r.json")
ok(M.read_result(os.path.join(STATE, "absent.json"), "C1", 1, SID)[0] is None, "a missing result is not a completion")
open(rpth, "w").write("")
ok(M.read_result(rpth, "C1", 1, SID)[0] is None, "an empty result file is not a completion")
open(rpth, "w").write("{nope")
ok(M.read_result(rpth, "C1", 1, SID)[0] is None, "invalid JSON is not a completion")
open(rpth, "w").write(json.dumps(dict(GOOD, item="C9")))
ok(M.read_result(rpth, "C1", 1, SID)[0] is None, "a result naming another ITEM cannot complete this one")
open(rpth, "w").write(json.dumps(dict(GOOD, session_id="old-session")))
ok(M.read_result(rpth, "C1", 1, SID)[0] is None, "a result from another SESSION is stale and refused")
open(rpth, "w").write(json.dumps(dict(GOOD, outcome="probably fine")))
ok(M.read_result(rpth, "C1", 1, SID)[0] is None, "an undeclared outcome is not a completed one")
open(rpth, "w").write(json.dumps(GOOD))
ok(M.read_result(rpth, "C1", 1, SID)[0] == GOOD, "a correlated, schema-shaped result is accepted")

# -------------------------------------------------------------------------- failure classification
for text, want in [("HTTP 401 Unauthorized", M.AUTH), ("rate limit exceeded", M.QUOTA),
                   ("429 Too Many Requests", M.QUOTA), ("connection refused", M.TRANSPORT),
                   ("unknown option '--nope'", M.CLI_CONFIG), ("stream cancelled", M.CANCELLED),
                   ("wrote the migration", None)]:
    ok(M.classify_failure(text) == want, f"classify_failure({text!r}) -> {want}")
ok(M.classify_failure("401 unauthorized: rate limit") == M.AUTH,
   "the specific class wins over the general when both words appear")
ok(M.QUOTA not in M.RETRYABLE and M.AUTH not in M.RETRYABLE and M.TRANSPORT in M.RETRYABLE,
   "quota waits for a clock and auth needs a human; only transport is retried")

# ============================================================ THE ENTRY POINT (call-site coverage)
rollout(SID)
r = attempt()
ok(r["outcome"] == M.OK and r["candidate_sha"] == "abc1234" and r["route_verified"] is True,
   "the happy path: correlated result on the intended route -> OK")
ok(r["model_actual"] == "muse-spark-1.3-contributor" and r["provider_actual"] == "muse-go-1"
   and r["session_id"] == SID and r["pid"] == 4242 and os.path.isfile(r["event_log"]),
   "the record stores the resolved route, session, pid and event log (§5's storage list)")
ok(not any("KEY" in str(k).upper() or "sk-live-value" == str(v) for k, v in r.items()),
   "no credential name or value appears anywhere in the durable record")

cap = {}
attempt(runner=fake_runner(0, STARTED, write=GOOD, capture=cap))
ok(cap["stdin"] == "do the work" and "-" == cap["argv"][-1],
   "run_attempt sends the brief down stdin, not argv")
ok(cap["env"].get("OPENCODE_GO_KEY_1") == "sk-live-value",
   "run_attempt CALLS child_env: the child holds the selected credential")
r = attempt(env={"OPENCODE_GO_KEY_2": "wrong-account"})
ok(r["outcome"] == M.NO_ATTEMPT and "nothing was launched" in r["detail"].lower(),
   "MUST BITE: no credential for the selected profile -> refused before launch, nothing spent")
r = attempt(profile="muse-broken")
ok(r["outcome"] == M.CLI_CONFIG, "MUST BITE: an unusable profile refuses before launch")

r = attempt(runner=fake_runner(0, STARTED, err="HTTP 401 Unauthorized", write=GOOD))
ok(r["outcome"] == M.AUTH,
   "MUST BITE: a failure event on EXIT 0 beats both the exit code and a written result (§5 finding 2)")
r = attempt(runner=fake_runner(0, STARTED))
ok(r["outcome"] == M.INCOMPLETE and "not about the work" in r["detail"],
   "MUST BITE: exit 0 with NO result is INCOMPLETE, never OK")
r = attempt(runner=fake_runner(0, STARTED, write=dict(GOOD, item="C9")))
ok(r["outcome"] == M.INCOMPLETE, "MUST BITE: a result for another item cannot complete this one")

rollout("0199a1b2-mism-atch-0001-000000000001", model="muse-spark-1.3-contributor-free", provider="muse-zen-1")
r = attempt(runner=fake_runner(0, json.dumps({"type": "thread.started", "thread_id": "0199a1b2-mism-atch-0001-000000000001"}),
                               write=dict(GOOD, session_id="0199a1b2-mism-atch-0001-000000000001")))
ok(r["outcome"] == M.ROUTE_MISMATCH and r["route_verified"] is False,
   "MUST BITE: a completed, correlated result on the WRONG route is a mismatch, not an OK")
ok(r["provider_actual"] == "muse-zen-1" and r["provider_intended"] == "muse-go-1",
   "the mismatch record carries both routes, so the reviewer sees which ran")
r = attempt(runner=fake_runner(0, json.dumps({"type": "thread.started", "thread_id": "0199a1b2-ghos-t000-0002-000000000002"}),
                               write=dict(GOOD, session_id="0199a1b2-ghos-t000-0002-000000000002")))
ok(r["outcome"] == M.INCOMPLETE and r["route_verified"] is None and "NOT MEASURED" in r["detail"],
   "MUST BITE: a result whose route cannot be verified is not evidence for that route")

zrol = rollout("0199a1b2-zenr-un00-0003-000000000003", model="muse-spark-1.3-contributor-free", provider="muse-zen-1")
r = M.run_attempt("C1", 1, "b", STATE, "muse-zen-1", home=HOME, sessions_root=SESS,
                  env={"OPENCODE_ZEN_KEY_1": "k"},
                  runner=fake_runner(0, json.dumps({"type": "thread.started", "thread_id": "0199a1b2-zenr-un00-0003-000000000003"}),
                                     write=dict(GOOD, session_id="0199a1b2-zenr-un00-0003-000000000003")))
ok(r["outcome"] == M.OK and r["tier"] == "zen" and r["provider_actual"] == "muse-zen-1",
   "a zen attempt that INTENDED zen passes, and its record says zen where the reviewer looks")

def boom(*a): raise OSError("no such file: codex")
ok(attempt(runner=boom)["outcome"] == M.TRANSPORT, "a launch that raises produces a row, not a crash")
def slow(*a): raise TimeoutError("no result within 60s")
ok(attempt(runner=slow)["outcome"] == M.CANCELLED, "a timeout is CANCELLED and keeps its row")

rollout(SID)
cap2 = {}
r = attempt(session_id=SID, runner=fake_runner(0, STARTED, write=GOOD, capture=cap2))
ok("resume" in cap2["argv"] and cap2["argv"][cap2["argv"].index("resume") + 1] == SID
   and "--last" not in cap2["argv"],
   "MUST BITE: run_attempt resumes by EXACT id — the --last cross-item write is unreachable from here")

# ------------------------------------------------------- source-order guards the behaviour cannot see
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "museadapter.py")).read()
body = src[src.index("def run_attempt("):src.index("def subprocess_runner(")]
ok(body.index("child_env(") < body.index("= runner(argv"),
   "the credential check happens BEFORE the launch, not after it")
ok(body.index("classify_failure(") < body.index("read_result("),
   "failure events are read BEFORE a result is believed")
ok(body.index("route_matches(") < body.index("outcome = OK"),
   "the route is verified before any attempt can be called OK")
ok(re.search(r"except Exception[^\n]*\n\s*return rec\(", body),
   "no worker failure escapes as an exception")
ok('"--last"' not in src and "'--last'" not in src,
   "--last never appears as a string literal: it can only reach an argv as one, and it never does")

print(f"\n{P} passed, {len(F)} failed")
for f in F:
    print("  FAILED:", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if F else 0)
