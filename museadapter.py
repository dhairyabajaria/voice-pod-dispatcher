#!/usr/bin/env python3
"""The managed launch path for Codex CLI Muse workers (Plan 003 §5, workstream D).

WHAT THIS IS FOR. The pilot proved a Muse worker can be driven through the Codex CLI. It proved it
with two accounts, one session each, on a toy task in a disposable repository. It did NOT prove
many sessions on one account, failover, production-worktree recovery, unattended long runs, or
quota handling — so this adapter treats every one of those as unproven and reports rather than
assumes. The six pilot findings in §5 are REQUIREMENTS here, not repairs somebody already made.

THE SHAPE OF EVERY DECISION IN THIS FILE. A launch is a chain of claims, and each one is checked
where it can still be refused cheaply:

  the credential exists          -> checked BEFORE launch, by name, never by value
  the flags were accepted        -> the CLI's own exit status
  the route we asked for is the  -> read from SESSION METADATA after the fact, never from the
    route that ran                  worker's own report; a report claiming a model is not evidence
  the work finished              -> a terminal structured result correlated to THIS item, attempt
                                    and session, on a path no earlier run could have written

Each link exists because the pilot broke it. Exit 0 with no work done was one of the six findings:
a process that exits cleanly is a statement about the process, not about the work.

WHAT IT WILL NOT DO. It will not read or log a credential value, load credentials for an account it
did not select, edit shared configuration to bridge a name, disable trust or sandbox protections, or
let an attempt that ran on one route satisfy a requirement that named another.
"""
import json
import os
import re
import time

D = os.path.dirname(os.path.abspath(__file__))
CODEX_HOME = os.path.expanduser("~/.codex")
SESSIONS_ROOT = os.path.join(CODEX_HOME, "sessions")

# Preference order (owner via BOSS, 2026-09-08): Go first, distributed across all three keys. Zen is
# PERMITTED and not preferred, and is reached only when the Go accounts are exhausted or dead.
#
# THE AMBIGUITY IS RECORDED RATHER THAN RESOLVED SILENTLY. The instruction was "Zen is permitted,
# with fallback to Go when the free tier stalls. Preference remains Go, distributed across all three
# keys." Read literally, that sentence has zen as the default with Go as its fallback, and its second
# half has Go as the preference. Those cannot both be the default. BOSS's own gloss — "Go is the
# default and the distribution target; zen is available rather than forbidden" — is the reading
# implemented here. Changing it is one list, not a rewrite.
GO_PROFILES = ("muse-go-1", "muse-go-2", "muse-go-3")
ZEN_PROFILES = ("muse-zen-1", "muse-zen-2", "muse-zen-3")

# Outcome classes. Structured events only make a quota failure observable IF THE ADAPTER HANDLES IT
# (§5), so each of these is a distinct thing a human does next, not a severity.
OK = "OK"                       # a terminal result, correlated, schema-valid
BLOCKED = "BLOCKED"             # the worker stopped and said why: a question, a missing prerequisite
AUTH = "AUTH"                   # the credential was refused — a key problem, not a work problem
QUOTA = "QUOTA"                 # rate limited or out of allowance — retry later, do not re-dispatch
TRANSPORT = "TRANSPORT"         # network/provider reachability — bounded retry is reasonable
CLI_CONFIG = "CLI_CONFIG"       # our own argv, profile or schema is wrong — retrying cannot help
CANCELLED = "CANCELLED"         # killed, timed out, or interrupted
INCOMPLETE = "INCOMPLETE"       # ran, produced no terminal result we can correlate: exit 0 is not one
ROUTE_MISMATCH = "ROUTE_MISMATCH"   # it ran, on a route this attempt did not intend
NO_ATTEMPT = "NO_ATTEMPT"       # refused before launch; nothing was started and nothing was spent

RETRYABLE = (TRANSPORT,)        # QUOTA waits for a clock, not a retry; AUTH and CLI_CONFIG need a human.


# --------------------------------------------------------------------------- profiles, by name only
def profile_spec(profile, home=CODEX_HOME):
    """What a profile ROUTES TO, read from its own file. -> (spec, "") or (None, why).

    Returns the model id, the provider and the NAME of the environment variable holding its
    credential. The value is never read here and is never read anywhere in this module: an adapter
    that never holds a secret cannot leak one, and the only question it needs answered is whether
    the variable is set and non-empty.
    """
    path = os.path.join(home, f"{profile}.config.toml")
    if not os.path.isfile(path):
        return None, (f"no profile file at {path} — the profile name is wrong, or the profile was "
                      f"never installed on this machine")
    try:
        text = open(path).read()
    except OSError as e:
        return None, f"cannot read {path}: {type(e).__name__}: {e}"
    def one(key):
        m = re.search(rf'^\s*{key}\s*=\s*"([^"]+)"', text, re.M)
        return m.group(1) if m else None
    env_key = None
    m = re.search(r'env_key\s*=\s*"([A-Z0-9_]+)"', text)
    if m:
        env_key = m.group(1)
    spec = {"profile": profile, "model": one("model"), "provider": one("model_provider"),
            "effort": one("model_reasoning_effort"), "env_key": env_key,
            "tier": "go" if profile in GO_PROFILES else "zen" if profile in ZEN_PROFILES else "other"}
    missing = [k for k in ("model", "provider", "env_key") if not spec[k]]
    if missing:
        return None, (f"{path} declares no {', '.join(missing)} — this adapter will not guess a "
                      f"route, because a guessed route is the silent substitution Plan 003 forbids")
    return spec, ""


def preference_order(allow_zen=False, dead=()):
    """The profiles to try, in order. Go first, always; zen only when allowed AND Go is exhausted.

    `dead` is the set a caller has already seen refuse — a key that has stopped working is SKIPPED
    and reported, never retried into the ground (BOSS, 2026-09-08).
    """
    order = [p for p in GO_PROFILES if p not in dead]
    if allow_zen:
        order += [p for p in ZEN_PROFILES if p not in dead]
    return order


def next_profile(order, last=None):
    """Round-robin: the profile AFTER `last` in the order, wrapping. -> profile or None.

    The owner's instruction is to distribute work across all three keys, so the selector rotates
    rather than preferring the first that works — a scheduler that always picks the head of a list
    concentrates every session on one account and then discovers its limit alone.
    """
    if not order:
        return None
    if last in order:
        return order[(order.index(last) + 1) % len(order)]
    return order[0]


# --------------------------------------------------------------- credentials, in the child env only
KEY_ALIASES = {
    # Plan 003 §5, finding 1. The key file defines OPENCODE_GO_KEY1/2/3 and the profiles require
    # OPENCODE_GO_KEY_1/2/3 — one underscore apart, and no worker launches until something bridges
    # them. ★ has since appended the underscored spelling to the key file, so both exist today.
    # THIS MAP STAYS ANYWAY: a file that happens to be correct today is not a guard, and §5 asks for
    # the mapping and the check in the child environment regardless of what is on disk.
    "OPENCODE_GO_KEY_1": "OPENCODE_GO_KEY1",
    "OPENCODE_GO_KEY_2": "OPENCODE_GO_KEY2",
    "OPENCODE_GO_KEY_3": "OPENCODE_GO_KEY3",
}


def child_env(spec, base=None):
    """The environment for ONE worker process. -> (env, "") or (None, why).

    Three rules, all from §5:
      * the mapping happens HERE, in the child's environment — never by editing the shared key file
        or any config, and never in this process's own os.environ;
      * only the SELECTED account's variable is carried; the other accounts' credentials are removed
        from the child even if this process inherited them;
      * a missing or empty credential is an explicit refusal BEFORE launch, not a launch that fails
        somewhere less legible.
    No value is read, compared, logged or returned — only `bool(value)` is ever consulted.
    """
    base = os.environ if base is None else base
    want = spec["env_key"]
    value = base.get(want) or base.get(KEY_ALIASES.get(want, ""), "")
    if not str(value).strip():
        alias = KEY_ALIASES.get(want)
        return None, (f"the credential for {spec['profile']} is missing or empty: neither {want}"
                      + (f" nor its unsuffixed spelling {alias}" if alias else "")
                      + " is set and non-empty in this environment. Nothing was launched, so nothing "
                        "was spent. (No value was read; only whether one exists.)")
    env = dict(base)
    # every credential this adapter knows about, dropped, then exactly one put back
    for k in list(env):
        if k.startswith("OPENCODE_GO_KEY") or k.startswith("OPENCODE_ZEN_KEY"):
            env.pop(k, None)
    env[want] = value
    env["CODEX_HOME"] = env.get("CODEX_HOME", CODEX_HOME)
    return env, ""


# ------------------------------------------------------------------------------ argv, launch/resume
LEGACY = "legacy-astra"      # the route the daemon has always taken: -m <model> plus a global effort


def slot_route(slot, item=None, cfg=None):
    """Which profile a slot should run. -> profile name, or LEGACY for the historic Astra pair.

    Three sources, most specific first: the item's own `profile`, then a `codex_routes` map in
    roster.json, then LEGACY.

    THE DEFAULT IS DELIBERATELY THE OLD BEHAVIOUR. Every slot runs Astra today via `-m gpt-6-astra`
    with no profile at all. Defaulting the slots to muse profiles here would re-route every existing
    dispatch as a side effect of wiring, which is a change nobody asked for arriving inside a change
    somebody did. Muse is opted INTO, per item or per slot, and the opt-in is one line of config.

    I do not write roster.json (it is not mine to edit), so `codex_routes` is read if someone adds it
    and absent harmlessly if nobody has.
    """
    if item and str(item.get("profile") or "").strip():
        return item["profile"].strip()
    routes = (cfg or {}).get("codex_routes") or {}
    return routes.get(slot) or LEGACY


def route_flags(profile, home=CODEX_HOME, env=None, legacy_model=None, legacy_effort=None):
    """The routing half of an argv, plus the child env. -> (flags, env, spec, "") or (…, why).

    ONE BUILDER FOR BOTH ROUTES, so the daemon cannot drift from the adapter. A profile route passes
    `-p <profile>` and NOTHING ELSE: no `-m`, and above all no `-c model_reasoning_effort=`, because
    the profile already carries `xhigh` and the daemon's global default is Astra's `medium` — passing
    it would downgrade every Muse worker silently. The legacy route keeps the historic pair exactly
    as it was.
    """
    if profile == LEGACY:
        return (["-m", legacy_model or "gpt-6-astra",
                 "-c", f"model_reasoning_effort={legacy_effort or 'medium'}"],
                dict(env or os.environ),
                {"profile": LEGACY, "model": legacy_model or "gpt-6-astra", "provider": None,
                 "effort": legacy_effort or "medium", "env_key": None, "tier": "legacy"}, "")
    spec, why = profile_spec(profile, home=home)
    if not spec:
        return None, None, None, why
    cenv, why = child_env(spec, base=env)
    if not cenv:
        return None, None, spec, why
    return ["-p", profile], cenv, spec, ""


def launch_argv(profile, result_path, schema=None, sandbox="workspace-write"):
    """A NEW worker. The brief arrives on stdin and the caller closes it (see brief_stdin).

    §5, finding 3: choose ONE explicit input mode. The brief goes through a closed pipe rather than
    argv — a multi-page brief in a command line is visible in every `ps` on the machine, and an
    inherited open stdin is what left the pilot's worker waiting for input nobody would type.
    `-` tells the CLI to read the prompt from stdin explicitly, so nothing depends on it guessing.
    """
    # NO EFFORT FLAG, deliberately. ★'s probe (2026-09-08) confirmed --profile resolves to
    # ~/.codex/<name>.config.toml even though that name appears nowhere in config.toml, and that the
    # profile's own `model_reasoning_effort = xhigh` reaches turn_context. A `-c
    # model_reasoning_effort=...` here would silently override it — the dispatcher's global default
    # is Astra's `medium` — so the profile is the single source of truth and route_matches checks
    # that it arrived. Same for `-m`: the profile names the model.
    argv = [codex_bin(), "exec", "--json", "--strict-config", "-p", profile,
            "-s", sandbox, "-o", result_path]
    if schema:
        argv += ["--output-schema", schema]
    return argv + ["-"]


def resume_argv(profile, session_id, result_path, schema=None, sandbox="workspace-write"):
    """RESUME an exact session. -> argv, or raises ValueError on an empty id.

    §5, finding 4, and both halves matter:
      * parent options come BEFORE the `resume` subcommand — `codex exec [options] resume <id>` —
        because the CLI's own help declares them on `exec`, not on `resume`;
      * the exact stored session id, NEVER `--last`. `--last` resolves to whatever ran most recently
        on this machine, which under concurrency is somebody else's worker. It is the one flag that
        turns a resume into a silent cross-item write.
    """
    if not str(session_id or "").strip():
        raise ValueError("resume needs the exact stored session id; --last is never acceptable here")
    argv = [codex_bin(), "exec", "--json", "--strict-config", "-p", profile,
            "-s", sandbox, "-o", result_path]
    if schema:
        argv += ["--output-schema", schema]
    return argv + ["resume", str(session_id), "-"]


def codex_bin():
    """The codex binary, from roster.json's `codex_bin`, else the name on PATH.

    Same source as the gate uses (mergegate.codex_bin): launchd's PATH has no node bin directory, so
    a bare `codex` resolves for BOSS's shell and not for the daemon — measured 2026-09-07, and it
    cost every auto-gate its Codex row.
    """
    try:
        v = json.load(open(os.path.join(D, "roster.json"))).get("codex_bin")
        if v and os.path.exists(os.path.expanduser(v)):
            return os.path.expanduser(v)
    except Exception:  # noqa: BLE001 — a missing roster is not a reason to fail the launch here
        pass
    return "codex"


def result_path_for(state_dir, item, attempt):
    """A result path NO EARLIER RUN COULD HAVE WRITTEN (§5, finding 2).

    "Use unique result paths so a previous success cannot satisfy a later run." The attempt number
    and a monotonic stamp are both in the name, so a re-dispatch of the same item cannot be
    completed by the file its previous attempt left behind.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(item))
    return os.path.join(state_dir, f"{safe}.attempt{int(attempt)}.{time.time_ns()}.json")


# ------------------------------------------------------------------- what actually happened, if any
FAIL_PATTERNS = (
    # (class, pattern). Ordered: the first match wins, so the specific sits above the general.
    (AUTH, r"401|unauthor|invalid api key|authentication fail|forbidden"),
    (QUOTA, r"rate.?limit|quota|usage limit|too many requests|429|insufficient credit"),
    (CLI_CONFIG, r"unknown option|unrecognized|no such profile|unexpected argument|invalid config|"
                 r"strict-config|no such file or directory.*schema"),
    (TRANSPORT, r"connection refused|connection reset|timed out|temporary failure|dns|"
                r"network is unreachable|502|503|504"),
    (CANCELLED, r"cancell?ed|interrupted|sigterm|sigkill"),
)


def classify_failure(text):
    """Name the failure class in a worker's output, or None. Text only — never an exit code.

    Exit codes are the thing §5 says not to trust: "parse failure events even if the exit code is
    zero". So this reads what the process SAID, and the caller combines it with everything else.
    """
    low = (text or "").lower()
    for cls, pat in FAIL_PATTERNS:
        if re.search(pat, low):
            return cls
    return None


def session_id_from_jsonl(text):
    """The session id the CLI reported, or None. Never a newest-file search.

    Measured 2026-09-08, cli 0.153.2: the first stdout line of `codex exec --json` is
    `{"type": "thread.started", "thread_id": "..."}`. Other spellings are accepted because the event
    names belong to the CLI and a rename must degrade to "not measured", never to a wrong id.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        pay = d.get("payload") if isinstance(d.get("payload"), dict) else d
        for key in ("thread_id", "session_id", "conversation_id"):
            v = pay.get(key) or d.get(key)
            # any non-empty string under one of those exact names. An earlier draft here required
            # >= 8 characters "because ids are UUIDs"; a short id then read as NO ID AT ALL, and the
            # attempt was reported INCOMPLETE for the wrong reason. A length floor on a value whose
            # format belongs to somebody else buys nothing and hides the id it rejects.
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def rollout_for_session(session_id, root=SESSIONS_ROOT):
    """The rollout file for THIS session id. -> (path, "") or (None, why). Refuses on 0 and on >1."""
    import glob
    if not session_id:
        return None, ("the CLI reported NO SESSION ID on stdout — looked for a JSONL line carrying "
                      "`thread_id`, `session_id` or `conversation_id`, of which cli 0.153.2 prints "
                      "`{\"type\": \"thread.started\", \"thread_id\": ...}` first. A rename is a "
                      "one-line fix here; no line at all means the run never started")
    hits = sorted(glob.glob(os.path.join(root, "*", "*", "*", f"*{session_id}*.jsonl")))
    if not hits:
        return None, (f"no rollout under {root} carries session id {session_id} — an ephemeral run, "
                      f"or a run that died before its first turn")
    if len(hits) > 1:
        return None, (f"{len(hits)} rollouts carry session id {session_id}; refusing to choose "
                      f"between them")
    return hits[0], ""


def resolved_route(rollout_path):
    """What ACTUALLY ran, from the session's own metadata. -> dict.

    §5, finding 5, "wrong model self-report": the pilot's worker reported a model it was not running.
    So the route is read from `session_meta` (provider, cli version, cwd) and `turn_context` (model,
    effort) — measured field locations — and never from the worker's prose. Anything absent stays
    absent: a provenance record that supplies the value it failed to find is worse than none.
    """
    out = {}
    try:
        with open(rollout_path) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                pay = d.get("payload") or {}
                if d.get("type") == "session_meta":
                    for src, dst in (("session_id", "session_id"), ("model_provider", "provider"),
                                     ("cli_version", "cli_version"), ("cwd", "cwd")):
                        if pay.get(src) and dst not in out:
                            out[dst] = pay[src]
                elif d.get("type") == "turn_context":
                    for src, dst in (("model", "model"), ("effort", "effort")):
                        if pay.get(src) and dst not in out:
                            out[dst] = pay[src]
                if {"model", "provider", "session_id"} <= set(out):
                    break
    except OSError as e:
        out["read_error"] = f"{type(e).__name__}: {e}"
    return out


def route_matches(spec, actual):
    """Did the route this attempt INTENDED actually run? -> (True | False | None, text).

    BOSS, 2026-09-08: ★'s census found five sessions on `muse-spark-1.3-contributor-free` via
    `muse-zen-1` between 02:24 and 02:33, from a scratch workspace that is not ours. Something else
    on this machine reaches for the zen profile. So passing the right flags is not evidence that they
    took, and this reads the answer back.

    Since zen became permitted, the failure condition is no longer "anything but Go". It is "anything
    that does not match the profile THIS ATTEMPT intended" — which is the stronger check, because it
    catches a Go attempt that landed on zen AND a zen attempt that landed on Go.

    None means the metadata could not be read. That is not a pass: an attempt whose route is unknown
    cannot be offered as evidence for a requirement that named one.
    """
    if not actual or not actual.get("model") or not actual.get("provider"):
        return None, ("route NOT MEASURED — the session metadata carries no model/provider, so what "
                      "ran is unknown. This attempt cannot be cited as evidence for the route it "
                      "intended: " + json.dumps(actual or {}, sort_keys=True))
    got = (actual["model"], actual["provider"])
    want = (spec["model"], spec["provider"])
    ident = (f"model={got[0]} provider={got[1]} effort={actual.get('effort', 'unrecorded')} "
             f"session={actual.get('session_id', 'unrecorded')}")
    if got != want:
        return False, (f"ROUTE MISMATCH — this attempt intended {spec['profile']} "
                       f"({want[0]} via {want[1]}) and the session records {got[0]} via {got[1]}. "
                       f"{ident}")
    # EFFORT IS PART OF THE ROUTE, and it is the half that fails quietly. Each muse profile carries
    # `model_reasoning_effort = xhigh` and it reached turn_context in all six of ★'s probes
    # (2026-09-08, six concurrent, two per key). The existing spawn argv passes a GLOBAL
    # `-c model_reasoning_effort=<codex_effort>` whose default is Astra's `medium`, so passing that
    # flag alongside --profile would downgrade every Muse worker with no error, no warning and a
    # correct-looking dispatch. This adapter therefore passes NO effort flag at all (see
    # launch_argv) and CHECKS the profile's value came through instead: one source of truth per
    # slot rather than two that can disagree.
    #
    # A caveat that must not be lost: turn_context records what Codex SENT. It is not proof the
    # provider HONOURED it. This asserts the request, and says so.
    if spec.get("effort") and actual.get("effort") and actual["effort"] != spec["effort"]:
        return False, (f"EFFORT MISMATCH — {spec['profile']} declares "
                       f"model_reasoning_effort={spec['effort']} and the session sent "
                       f"{actual['effort']}. Something is overriding the profile — most likely a "
                       f"global effort flag on the argv. {ident} (This is what was SENT; whether "
                       f"the provider honoured it is not observable here.)")
    if spec.get("effort") and not actual.get("effort"):
        return None, (f"route NOT FULLY MEASURED — {spec['profile']} declares effort "
                      f"{spec['effort']} and the session records none, so the request cannot be "
                      f"confirmed. {ident}")
    return True, (f"{spec['profile']} confirmed from session metadata: {ident} "
                  f"(effort as SENT by the CLI; provider adherence is not observable here)")


def read_result(path, item, attempt, session_id):
    """The worker's terminal structured result, correlated. -> (result, "") or (None, why).

    §5, finding 2, and every clause here is one of its ways to fail: "Exit 0 with no result, stale
    result, invalid schema, wrong item or terminal error can never mark work complete."
    """
    if not os.path.isfile(path):
        return None, (f"no result at {path}: the process ended without writing a terminal result. "
                      f"An exit status is a statement about the process, not about the work")
    try:
        raw = open(path).read()
    except OSError as e:
        return None, f"cannot read the result at {path}: {type(e).__name__}: {e}"
    if not raw.strip():
        return None, f"the result at {path} is empty — the file was created and never written"
    try:
        d = json.loads(raw)
    except ValueError as e:
        return None, f"the result at {path} is not valid JSON ({e}) — nothing can be read from it"
    if not isinstance(d, dict):
        return None, f"the result at {path} is a {type(d).__name__}, not an object"
    got_item = str(d.get("item") or "")
    if got_item and got_item != str(item):
        return None, (f"the result at {path} names item {got_item}, not {item} — a result from "
                      f"another item can never complete this one")
    got_sid = str(d.get("session_id") or "")
    if got_sid and session_id and got_sid != str(session_id):
        return None, (f"the result at {path} names session {got_sid}, not {session_id} — this is a "
                      f"stale or foreign result")
    if str(d.get("outcome") or "") not in ("done", "blocked", "question"):
        return None, (f"the result at {path} declares outcome={d.get('outcome')!r}, which is not one "
                      f"of done/blocked/question — an undeclared outcome is not a completed one")
    return d, ""


def attempt_record(**kw):
    """One attempt's durable row (§5's storage list). Credential VALUES are never among the fields.

    "A process-start event is not an acknowledgement and a worker's completed report is not
    acceptance of its code" — so `outcome` here describes the RUN, and acceptance stays with the
    gate and the reviewer.
    """
    row = {"role": "muse-implementer", "item": None, "owner": None, "worktree": None,
           "profile": None, "tier": None, "model_intended": None, "model_actual": None,
           "provider_intended": None, "provider_actual": None, "effort_actual": None,
           "base_sha": None, "candidate_sha": None, "pid": None, "session_id": None,
           "event_log": None, "result_path": None, "outcome": None, "detail": None,
           "attempt": None, "started_at": None, "ended_at": None, "route_verified": None}
    row.update(kw)
    for k in list(row):
        if "key" in k.lower() or "secret" in k.lower() or "token" in k.lower():
            row.pop(k)      # a field named like a credential never leaves this function
    return row


# ------------------------------------------------------------------------------------ the entry point
def run_attempt(item, attempt, brief, state_dir, profile, *, worktree=None, owner=None,
                base_sha=None, session_id=None, schema=None, timeout=None, home=CODEX_HOME,
                sessions_root=None, env=None, runner=None):
    """Run ONE attempt end to end and return its durable record. Never raises for a worker failure.

    THIS IS THE ONLY FUNCTION A CALLER SHOULD USE, and every check above is wired here rather than
    left for a caller to remember. A guard a caller has to call is a guard that is one forgotten line
    from absent — and the proof set for this module scores mutations that revert THESE call sites,
    not ones that break the helpers, because the helpers were never the thing at risk.

    Order matters and is not arrangement: profile -> credential -> launch -> session id -> route ->
    result. Each refusal happens at the last point where nothing has been spent yet.

    `runner(argv, env, stdin_text, timeout) -> (rc, stdout, stderr, pid)` is the single seam. Tests
    pass a fake; production passes `subprocess_runner`. Nothing else in this function is mockable,
    so a test cannot accidentally prove a path the product does not take.
    """
    sessions_root = sessions_root or SESSIONS_ROOT
    runner = runner or subprocess_runner
    rec = lambda **kw: attempt_record(item=item, attempt=attempt, owner=owner, worktree=worktree,
                                      base_sha=base_sha, profile=profile,
                                      started_at=started, ended_at=time.time(), **kw)
    started = time.time()

    spec, why = profile_spec(profile, home=home)
    if not spec:
        return rec(outcome=CLI_CONFIG, detail=why)
    rec = lambda _rec=rec, _s=spec, **kw: _rec(tier=_s["tier"], model_intended=_s["model"],
                                               provider_intended=_s["provider"], **kw)

    cenv, why = child_env(spec, base=env)
    if not cenv:
        # NO_ATTEMPT, not AUTH: the credential was never offered to anyone, so nothing was refused
        # and nothing was consumed. Reporting this as an auth failure would send a human to look at
        # an account that is probably fine.
        return rec(outcome=NO_ATTEMPT, detail=why)

    result_path = result_path_for(state_dir, item, attempt)
    try:
        argv = (resume_argv(profile, session_id, result_path, schema=schema) if session_id
                else launch_argv(profile, result_path, schema=schema))
    except ValueError as e:
        return rec(outcome=CLI_CONFIG, detail=str(e), result_path=result_path)

    try:
        rc, out, err, pid = runner(argv, cenv, brief, timeout)
    except TimeoutError as e:
        return rec(outcome=CANCELLED, detail=f"the worker was stopped: {e}", result_path=result_path)
    except Exception as e:  # noqa: BLE001 — a launch failure must produce a row, never an exception
        return rec(outcome=TRANSPORT, detail=f"could not launch: {type(e).__name__}: {e}",
                   result_path=result_path)

    sid = session_id_from_jsonl(out) or session_id
    log_path = os.path.join(state_dir, os.path.basename(result_path) + ".events")
    try:
        with open(log_path, "w") as fh:
            fh.write(out or "")
            if err:
                fh.write("\n----- stderr -----\n" + err)
    except OSError:
        log_path = None

    # ONE completion path, shared with the daemon. codex_spawn cannot call run_attempt (it launches
    # and reaps a tick later, where this blocks), so the checks live in verify_finish and BOTH
    # callers run exactly those. A second, weaker copy for the daemon is how a proved guard ends up
    # off the path it was built for.
    outcome, detail, fields = verify_finish(spec, (err or "") + "\n" + (out or ""), result_path,
                                            item, attempt, sessions_root=sessions_root)
    fields.setdefault("session_id", sid)
    if not fields.get("session_id"):
        fields["session_id"] = sid
    return rec(outcome=outcome, detail=detail if outcome == OK else f"exit {rc}: {detail}", pid=pid, result_path=result_path,
               event_log=log_path, **{k: v for k, v in fields.items() if k != "session_id"},
               session_id=fields.get("session_id") or sid)


def verify_finish(spec, log_text, result_path, item, attempt, sessions_root=None):
    """What a FINISHED process proved. -> (outcome, detail, fields).

    The completion half of run_attempt, split out so the daemon runs the SAME guards. codex_spawn is
    fire-and-forget — it starts a process, records a pid and reaps it a tick later — so it cannot
    call run_attempt, which blocks until the worker exits. Left unsplit, the daemon would have grown
    a second, weaker copy of these checks, and a guard that is not on the path is not a guard.
    """
    sessions_root = sessions_root or SESSIONS_ROOT
    fields = {}
    cls = classify_failure(log_text or "")
    sid = session_id_from_jsonl(log_text or "")
    fields["session_id"] = sid
    if cls:
        return cls, f"the worker reported a {cls} failure", fields
    if spec.get("profile") == LEGACY:
        # The legacy route has no profile to read back, so there is nothing to assert against. Say
        # that, rather than reporting an unchecked route as a verified one.
        fields["route_verified"] = None
        result, why = read_result(result_path, item, attempt, sid)
        if not result:
            return INCOMPLETE, f"{why} (legacy route: no profile to verify against)", fields
        fields["candidate_sha"] = result.get("candidate_sha")
        return (OK if result.get("outcome") == "done" else BLOCKED), "legacy route, unverified", fields
    rollout, why = rollout_for_session(sid, root=sessions_root)
    actual = resolved_route(rollout) if rollout else {}
    ok_, note = route_matches(spec, actual)
    fields.update(model_actual=actual.get("model"), provider_actual=actual.get("provider"),
                  effort_actual=actual.get("effort"), route_verified=ok_)
    if ok_ is False:
        return ROUTE_MISMATCH, note, fields
    result, why2 = read_result(result_path, item, attempt, sid)
    if not result:
        return INCOMPLETE, f"{why2}" + ("" if ok_ else f" (also: {note or why})"), fields
    if ok_ is None:
        return INCOMPLETE, f"the worker produced a terminal result, but {note}", fields
    fields["candidate_sha"] = result.get("candidate_sha")
    return (OK if result.get("outcome") == "done" else BLOCKED), note, fields


def subprocess_runner(argv, env, stdin_text, timeout):
    """The real seam. Writes the brief down a pipe and CLOSES it (§5, finding 3)."""
    import subprocess
    p = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(input=stdin_text or "", timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        raise TimeoutError(f"no result within {timeout}s; the worker (pid {p.pid}) was killed")
    return p.returncode, out, err, p.pid
