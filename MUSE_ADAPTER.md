# museadapter.py — the managed launch path for Codex CLI Muse workers

Workstream D of `advisor-plans/003-final-execution-plan-after-muse-pilot-2026-09-08.md` (§5).
Owner: WORKER-2. Box-free. Nothing here runs a worker by itself; it is the layer the dispatcher
calls to start one and to decide what a finished process actually proved.

## What the pilot did and did not establish

Two accounts, one session each, a toy task, a disposable repository. That is the whole evidence
base. Many sessions on one account, failover, production worktrees, unattended long runs and quota
handling are all UNPROVEN, so this adapter reports on each of them rather than assuming any.

## The chain of claims

A launch is a chain, and each link is checked at the last point where refusing is still free.

| claim | checked by | if it fails |
|---|---|---|
| the profile declares a route | `profile_spec` | `CLI_CONFIG` — no launch |
| the credential exists | `child_env`, by NAME | `NO_ATTEMPT` — nothing launched, nothing spent |
| the flags were accepted | the CLI's exit status and its own error text | `CLI_CONFIG` |
| the worker did not fail | `classify_failure`, read BEFORE the exit code | `AUTH` / `QUOTA` / `TRANSPORT` / `CANCELLED` |
| the intended route ran | `resolved_route` + `route_matches`, from session metadata | `ROUTE_MISMATCH` |
| the work finished | `read_result`, correlated to item + attempt + session | `INCOMPLETE` |

Exit 0 is a statement about the process, never about the work. It appears nowhere in this table as
evidence of success.

## Routing

Go first, round-robin across `muse-go-1/2/3` so no single account carries every session and
discovers its limit alone. **Zen is permitted** as a configured fallback (owner via BOSS,
2026-09-08 — the earlier "never zen" is withdrawn), reached only when the Go accounts are exhausted
or dead. A zen result never satisfies a requirement that named Go: every attempt record carries
`tier`, `profile`, `provider_intended` and `provider_actual`, so the reviewer sees which ran without
having to ask.

A key seen refusing is SKIPPED and reported, not retried. `QUOTA` waits for a clock and `AUTH` waits
for a human; only `TRANSPORT` is retried.

The route is read back from the session's own metadata rather than from the worker's report, because
the pilot's worker reported a model it was not running, and because ★'s census on 2026-09-08 found
five sessions on `muse-spark-1.3-contributor-free` via `muse-zen-1`, from a scratch workspace that
is not ours, between 02:24 and 02:33. Something else on this machine reaches for that profile.
Passing the right flags is not evidence that they took.

## Credentials, and the difference between a secret and a handle

Read by NAME, never by value. The adapter consults `bool(value)` and nothing else; no value is
logged, compared, returned or stored in an attempt record. The child process gets exactly one
account's variable, and every other account's credential is REMOVED from its environment even when
this process inherited it.

### Handles are not secrets, and a pattern list only removes what somebody named

The child environment strips two different things for two different reasons.

**Secrets** — anything matching KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL, or the CLAUDE/ANTHROPIC/
SENTRY/AWS_/GH_ prefixes. These are *information*: holding one lets someone act as us later.

**Handles** — `SSH_AUTH_SOCK`, `SSH_AGENT_PID`, `GPG_AGENT_INFO`, `DBUS_SESSION_BUS_ADDRESS`,
`CLAUDE_CODE_MESSAGING_SOCKET`, `DOCKER_HOST`, `KUBECONFIG`. These are *authority*: a live connection
to something that will act on the owner's behalf when asked.

`SSH_AUTH_SOCK` is the one that makes the distinction matter. A process holding it can ask the
owner's running agent to **sign**. It never sees the private key, it is not limited to reading, and
the agent does not ask who is calling — that is push access to everything the agent can
authenticate, for as long as the socket is reachable.

**The name-matching missed it entirely**, because `SSH_AUTH_SOCK` contains none of those words, and
this adapter was reported as clean while carrying it. That is the general lesson worth more than the
list: a deny-list by pattern removes exactly what somebody thought to name, and the thing you did
not think to name looks identical to the thing that is not there.

### Rotation warning — two spellings, one secret, one file

The profiles require `OPENCODE_GO_KEY_1/2/3`. The key file historically defined
`OPENCODE_GO_KEY1/2/3` — one underscore apart, which is why no Codex worker could launch at all
(§5, finding 1). ★ has since appended the underscored spelling to the same file, so **both spellings
of the same three secrets now live in one file, and both must be rotated together.**

If only one spelling is updated, the OpenCode server and the Codex CLI silently diverge onto
different paid accounts. Nothing errors. The bill is the only place it shows.

`KEY_ALIASES` in the adapter bridges the two spellings in the CHILD environment. It stays even
though the file is currently correct: a file that happens to be right today is not a guard, and the
adapter must never edit shared configuration to make a launch work.

## Input, launch and resume

The brief goes down a closed stdin pipe with an explicit `-` argument (§5, finding 3) — never argv,
where a multi-page brief is visible in every `ps` on a shared machine, and never an inherited open
stdin, which is what left the pilot's worker waiting for input nobody would type.

Resume passes parent options before the `resume` subcommand and then the exact stored session id.
**`--last` is never used and never appears as a string literal in the module**; a test asserts that.
Under concurrency `--last` resolves to whatever ran most recently on this machine — somebody else's
worker — which turns a resume into a silent cross-item write.

## Effort: the profile speaks, and nothing else may

Each `muse-go-N` profile carries `model_reasoning_effort = xhigh`, and ★'s probe on 2026-09-08 (six
concurrent, two per key) saw it reach `turn_context` on all six runs. The dispatcher's existing spawn
argv passes a GLOBAL `-c model_reasoning_effort=<codex_effort>` whose default is Astra's `medium`.

**Passing that flag alongside `--profile` would downgrade every Muse worker from xhigh to medium
with no error, no warning, and a dispatch that looks correct.** So this adapter passes no effort
flag and no `-m` at all: the profile is the single source of truth per slot, and `route_matches`
checks the profile's value actually arrived rather than trusting that it did. An effort that differs
from the profile's is a refusal; an effort the session does not record at all is NOT FULLY MEASURED,
which is not a pass.

One caveat that must not be dropped when this is quoted: `turn_context` records what the CLI **sent**.
It is not proof the provider **honoured** it. The verified-route note says so in its own text.

## `--profile` resolves by filename, and that was measured

`--profile muse-go-N` resolves to `~/.codex/muse-go-N.config.toml` even though that name appears
nowhere in `~/.codex/config.toml` — v2 profiles are file-resolved, not section-registered. This was
genuinely unclear from the documentation and was settled by reading `session_meta.model_provider`
from six real rollouts, not by the commands' exit statuses, which can succeed while routing
elsewhere.

Because each profile carries its own `model_provider` AND its own `env_key`, **one profile per slot
is the entirety of the three-key distribution.** No key logic belongs in the dispatcher, no secret
belongs in `roster.json`. The adapter still checks the selected key is present and non-empty before
launch; it never handles the value.

## Session affinity: place once, then leave it alone

**Prompt cache is per account, and it is 92% of what Muse consumes** — measured on the Go plan,
8,314,573,726 cache-read tokens against 4,988,483 output. Moving a live conversation to a different
key throws that prefix away and re-sends it as fresh input at full price.

So distribution and cache pull in opposite directions, and the resolution is: **spread NEW
conversations widely, then never move them.**

* A conversation's key is chosen once, on its first dispatch, by **least-loaded placement** across
  the healthy Go profiles — the thing that matters is how many warm conversations a key already
  carries, and least-loaded self-corrects after a re-placement where a rotating cursor would keep
  feeding the busiest key.
* The chosen profile is written **onto the item** (`muse_profile`). Every resume, retry and re-spawn
  reads it and lands on the same key.
* **Affinity follows the conversation, never the slot.** A mapping keyed on `CODEX-N` silently moves
  a warm session onto a cold key the moment slots are reassigned.

Per-launch rotation is the most expensive scheduling policy available and this file does not
implement it.

### When a warm conversation may be moved

| the key is | action | why |
|---|---|---|
| behind a **rolling** wall | **WAIT** | the window refills on its own; the cache outlives it |
| behind a **weekly** wall | **MOVE** | waiting a week costs more than a cold start |
| **AUTH**-held | **MOVE** | waiting cannot help when a human has to act |

**An unrecognised wall counts as rolling.** Guessing rolling when it was weekly idles a slot until
someone looks; guessing weekly when it was rolling burns a warm prefix on every wall we cannot
parse. The asymmetry is not close.

## Selecting a profile: pin, or place

`codex_routes` in `roster.json` says what a slot does, and the two options mean different things:

| value | behaviour |
|---|---|
| a profile name (`muse-go-1`) | **pinned.** Always that key, unless it is held, in which case this dispatch is stepped over to a healthy one. |
| `rotate` (or `muse` / `go` / `pool`) | **placement.** A NEW conversation goes to the least-loaded healthy Go profile; an existing one keeps its own key. |
| absent | the historic Astra route, unchanged. |

**With every slot pinned to a name, no placement happens and a third key is never selected.** That is
a legitimate configuration, but it is not the owner's "distribute across all three keys" — that
needs `rotate`. An item's own `profile` field overrides both, and is honoured even for a held
profile, because someone asked for that one specifically.

`last` survives in the daemon's state only as a tie-break between equally-loaded keys. Placement is
by load, not by cursor.

Ten sticky sessions over three accounts is **3-4 warm conversations per key**, which is good for
cache and makes per-account quota the binding constraint — a weekly wall now takes 3-4 conversations
down together, so `account_load()` reports the per-key count before that happens rather than after.

## Health: three failures, three different answers

A key that stops working is skipped and reported, never retried into the ground — and *how long* it
is skipped depends on what went wrong:

* **AUTH**, or a credential this launcher cannot find — held **until a human clears it**. Retrying
  cannot help, and every retry is another refused call.
* **QUOTA** — held **until a clock** (`quota_hold_minutes`, default 60). The key is not broken, it is
  spent.
* **TRANSPORT** — not held at all. That is the one failure worth retrying.

Zen is reached only when **every** Go profile is held. The note on that dispatch says so, and says a
zen result must not be offered as evidence for anything that named Go.

## Concurrency is a setting, and nobody can size it yet

Six concurrent is a demonstrated floor, not a ceiling — nobody pushed past six, and a trivial prompt
is not a real workload. Six identical one-line prompts consumed 1,957 / 1,963 / 3,755 / 9,138 /
49,323 / 52,892 tokens: a **27x spread on the same input, cause unexplained.** Until someone
measures why, a worker's cost is not predictable and no slot count can be justified from it. That is
why `run_attempt` is per-attempt and holds no global state, and why no pool is built here.

## Result paths

`result_path_for` puts the attempt number and a monotonic stamp in the filename, so a re-dispatch of
an item can never be completed by the file its previous attempt left behind (§5, finding 2).

## Proof set

`tests/test_muse.py`, hermetic: no network, no codex binary, no box, no `~/.codex`. 83 checks.
Every guard is exercised through `run_attempt`, the real entry point, and the mutation set scores
reverted CALL SITES rather than broken helpers — the helper was never the thing at risk. Measured
2026-09-08: 11 call-site mutations, 11 CAUGHT, 0 MISSED, source restored and verified against its
pre-image md5 each time. Three of the eleven cover the effort trap: adding a global effort flag to
the argv, dropping the effort half of the route assertion, and treating a missing effort as verified.
