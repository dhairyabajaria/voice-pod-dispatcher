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

## Credentials

Read by NAME, never by value. The adapter consults `bool(value)` and nothing else; no value is
logged, compared, returned or stored in an attempt record. The child process gets exactly one
account's variable, and every other account's credential is REMOVED from its environment even when
this process inherited it.

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
