"""The LEGACY (Astra) branch of route_flags handed every worker the launcher's whole environment.

Authorised by the owner 2026-09-08, relayed verbatim by BOSS, scope fenced to the environment only:
NOT the model, NOT the effort, NOT the slot count.

The shape is the one this programme keeps paying for: `child_env` existed, was correct, and was
PROVED — and the branch four lines above it returned `dict(env or os.environ)`, so the guard covered
the path nobody was leaking on. `SSH_AUTH_SOCK`, the six provider keys and this session's
`CLAUDE_CODE_MESSAGING_*` all reached every Astra worker. Measured: a real worker printed its
environment into its own transcript and they were all there.

WHICH BRANCH THIS FILE DRIVES, stated because a test that exercises the already-stripped path proves
a guard that was never at risk: **every check below goes through `route_flags(M.LEGACY, ...)`** —
the branch that was leaking. The Muse branch is exercised only as a CONTROL, to show the strip did
not change it.

THE DANGEROUS DIRECTION HERE IS A STRIP THAT IS TOO AGGRESSIVE. A worker that no longer starts is a
worse outcome than the leak, so the last section asserts what a launched worker still needs. Astra
authenticates from ~/.codex/auth.json — a FILE, verified present before this landed — so it needs no
credential in the environment at all.

Hermetic: no spawn, no network, no real ~/.codex.
"""
import importlib.util, os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location(
    "museadapter_le", os.path.join(HERE, os.pardir, "museadapter.py"))
M = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(M)

P, FAILED = 0, []
def ok(cond, what):
    global P
    if cond: P += 1; print(f"PASS {what}")
    else: FAILED.append(what); print(f"FAIL {what}")

# Everything a launcher might be holding. Values are deliberately meaningless: this module never
# reads one, and neither does the test.
DIRTY = {
    "PATH": "/usr/bin", "HOME": "/home/x", "LANG": "en_US.UTF-8", "TMPDIR": "/tmp",
    "SSH_AUTH_SOCK": "/private/tmp/ssh.sock", "SSH_AGENT_PID": "42",
    "GPG_AGENT_INFO": "/g", "DBUS_SESSION_BUS_ADDRESS": "/d",
    "DOCKER_HOST": "unix:///d.sock", "KUBECONFIG": "/k",
    "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/1902.sock",
    "CLAUDE_CODE_MESSAGING_TOKEN": "irrelevant",
    "CLAUDE_CODE_SSE_PORT": "1", "ANTHROPIC_API_KEY": "irrelevant",
    "OPENCODE_GO_KEY_1": "irrelevant", "OPENCODE_GO_KEY_2": "irrelevant",
    "OPENCODE_GO_KEY_3": "irrelevant", "OPENCODE_ZEN_KEY": "irrelevant",
    "SENTRY_DSN": "irrelevant", "GITHUB_TOKEN": "irrelevant", "GH_TOKEN": "irrelevant",
    "AWS_SECRET_ACCESS_KEY": "irrelevant", "MY_PASSWORD": "irrelevant",
    "SOME_CREDENTIAL": "irrelevant", "X_PASSWD": "irrelevant",
}
MUST_GO = [k for k in DIRTY if k not in ("PATH", "HOME", "LANG", "TMPDIR")]

# ---- THE LEGACY BRANCH, which is the one that was leaking -------------------------------------
flags, env, spec, why = M.route_flags(M.LEGACY, legacy_model="gpt-6-astra",
                                      legacy_effort="medium", env=DIRTY)
ok(spec["tier"] == "legacy" and why == "",
   "CONTROL: these checks really are driving the LEGACY branch — a test aimed at the already-clean "
   "Muse path would prove a guard that was never at risk")
leaked = sorted(k for k in env if k in MUST_GO)
ok(not leaked,
   f"MUST BITE: the legacy worker's environment carries NONE of the credentials or machine handles. "
   f"Leaked: {leaked}")
ok("SSH_AUTH_SOCK" not in env,
   "MUST BITE: SSH_AUTH_SOCK specifically — a live agent socket is the launcher's identity, and a "
   "third-party worker holding it can sign as this machine")
ok(not any(k.startswith("CLAUDE") for k in env),
   "MUST BITE: no CLAUDE* variable survives — including the messaging socket and token, which are "
   "exactly what a session-poker would have needed. GOD refused that poker, so nothing in the "
   "accepted design ever wants this in a child again")
ok(not any(k.startswith("OPENCODE_") for k in env),
   "MUST BITE: no provider key reaches Astra, which is not the account paying for it")

# ---- THE FENCE: environment only. Model, effort and slots are the owner's stated out-of-scope ---
ok(flags == ["-m", "gpt-6-astra", "-c", "model_reasoning_effort=medium"],
   "MUST BITE: the legacy FLAGS are byte-identical to before — the owner's scope was the "
   "environment strip only, and a change to the model or effort here would exceed it")
ok(spec["model"] == "gpt-6-astra" and spec["effort"] == "medium" and spec["provider"] is None,
   "  and the legacy spec is unchanged in every field")
# THE DEFAULTS, WHICH ARE WHERE THE FENCE IS WEAKEST. Every check above passes an explicit model
# and effort, so the `or "gpt-6-astra"` / `or "medium"` fallbacks never run — and a mutation that
# changed the default effort to xhigh scored MISSED against all of them. That is the owner's scope
# line ("do not change Astra's model, effort or slot count") being unguarded exactly where a change
# would be silent: a caller that passes nothing gets whatever the fallback says.
fd, _, sd, _ = M.route_flags(M.LEGACY, env=DIRTY)
ok(fd == ["-m", "gpt-6-astra", "-c", "model_reasoning_effort=medium"],
   f"MUST BITE: with NO model or effort supplied, the legacy DEFAULTS are still gpt-6-astra and "
   f"medium — the owner fenced those out of scope and a silent default change is how that fence "
   f"would be crossed without a diff anyone reads: {fd}")
ok(sd["model"] == "gpt-6-astra" and sd["effort"] == "medium",
   "  and the spec the daemon records carries the same defaults, so the log header cannot claim a "
   "route the worker did not run")

f2, _, s2, _ = M.route_flags(M.LEGACY, legacy_model="custom-model", legacy_effort="high", env=DIRTY)
ok(f2 == ["-m", "custom-model", "-c", "model_reasoning_effort=high"] and s2["effort"] == "high",
   "  and the caller's model/effort still pass through untouched — the strip touched neither")

# ---- THE OTHER DIRECTION: a worker that cannot start is worse than the leak ---------------------
ok(env.get("PATH") == "/usr/bin" and env.get("HOME") == "/home/x",
   "MUST BITE: PATH and HOME SURVIVE. The dangerous failure here is a strip so aggressive the "
   "worker never launches, which is worse than the leak it fixes")
ok(env.get("LANG") == "en_US.UTF-8" and env.get("TMPDIR") == "/tmp",
   "  and ordinary non-secret environment is left alone rather than allow-listed away")
ok(len(env) == 4, f"  exactly the four harmless names survive, no more: {sorted(env)}")

# ---- CONTROL: the Muse branch is unchanged by the refactor --------------------------------------
# A TEMP DIR, not tests/fixtures/: a test that writes into the repo leaves an untracked artifact
# that someone eventually commits as if it were a checked-in fixture.
import tempfile
HOME_ = tempfile.mkdtemp(prefix="legacyenv-home-")
open(os.path.join(HOME_, "muse-go-1.config.toml"), "w").write(
    'model = "muse-spark-1.3-contributor"\nmodel_provider = "muse-go-1"\n'
    'model_reasoning_effort = "xhigh"\n[model_providers.muse-go-1]\nenv_key = "OPENCODE_GO_KEY_1"\n')
os.makedirs(os.path.join(HOME_, "muse-homes", "muse-go-1"))
open(os.path.join(HOME_, "muse-homes", "muse-go-1", "config.toml"), "w").write(
    open(os.path.join(HOME_, "muse-go-1.config.toml")).read())
mf, menv, mspec, mwhy = M.route_flags("muse-go-1", env=DIRTY, home=HOME_)
ok(mf == ["-p", "muse-go-1"] and menv.get("OPENCODE_GO_KEY_1") == "irrelevant",
   "CONTROL: the Muse branch still carries EXACTLY ONE key — its own — so the shared strip did not "
   "break the path that was already correct")
ok(not any(k.startswith("OPENCODE_GO_KEY_2") or k.startswith("OPENCODE_GO_KEY_3")
           or k.startswith("OPENCODE_ZEN") for k in menv),
   "  and the other accounts' keys are still removed")
ok("SSH_AUTH_SOCK" not in menv and not any(k.startswith("CLAUDE") for k in menv),
   "  and it still drops the handles")

# ---- the strip itself, called directly, for the keep= contract ----------------------------------
kept = M.scrub_env(DIRTY, keep="OPENCODE_GO_KEY_2")
ok("OPENCODE_GO_KEY_2" in kept and "OPENCODE_GO_KEY_1" not in kept,
   "MUST BITE: `keep` exempts exactly one name and nothing else — that is how the Muse branch puts "
   "its own account back without re-admitting the others")
ok("OPENCODE_GO_KEY_2" not in M.scrub_env(DIRTY),
   "  and with keep=None — the legacy call — nothing is exempt at all")

shutil.rmtree(HOME_, ignore_errors=True)
print(f"\n{P} passed, {len(FAILED)} failed")
for f in FAILED: print("  FAILED:", f)
sys.exit(1 if FAILED else 0)
