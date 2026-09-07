# Plan 003 §6.2 — the managed launch path, validated on real workers

2026-09-08, WORKER-2. Disposable git repositories only: no trunk, no box, no platform code, no
worktree added to any real repository.

## What §6.2 asks for, and what was run

> "Validate the managed launch path in disposable worktrees: one bounded edit/test/report, exact
> resume and a parked question/answer. Run two managed workers simultaneously and verify distinct
> files, logs, session IDs and selected profiles."

Six real Codex CLI workers across two rounds, on all three Go accounts.

## Round 1 — routing and isolation hold; three defects surfaced

Three workers concurrently, one per account, each with a bounded edit/test/report brief.

| item | profile | provider (from rollout) | model | effort | cwd | session id |
|---|---|---|---|---|---|---|
| E2E-A | muse-go-1 | muse-go-1 | muse-spark-1.3-contributor | xhigh | wtA | 01a07e23-4272… |
| E2E-B | muse-go-2 | muse-go-2 | muse-spark-1.3-contributor | xhigh | wtB | 01a07e23-42a8… |
| E2E-Q | muse-go-3 | muse-go-3 | muse-spark-1.3-contributor | xhigh | wtC | 01a07e23-81d3… |

Every provider read back from `session_meta`, every model and effort from `turn_context` — never
from an exit status and never from the worker's own report. **Distinct sessions, distinct providers,
distinct working directories, distinct logs. Isolation holds and per-slot routing holds.**

Then three defects that no fixture I would have written could have produced:

**1. All three runs were classified as authentication failures while their work had succeeded.**
A worker printed its own environment. That environment contained `Claude%401.46388.4` — a
URL-encoded `@` followed by a version number — and the bare `401` pattern matched inside it. The
classifier was reading the worker's own output as though it were the CLI reporting on itself.
Fixed: classification now reads only CLI-level text (non-JSON lines and error-typed events), and the
numeric codes carry word boundaries. A worker may legitimately quote "401" or "rate limit" while
doing exactly what it was asked.

**2. The same dump showed what we had handed it.** This session's Claude Code identifiers, socket
paths and a sentry key had been inherited straight into a third-party paid worker. The worker did
nothing wrong — it had them because the launcher passed its whole environment. Fixed: the child
environment now drops anything matching KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL and the
CLAUDE/ANTHROPIC/SENTRY/AWS_/GH_ prefixes, keeping the one selected credential and what the child
needs to run.

**3. Two workers edited, tested, and could not commit.** `.git` is not writable under
`-s workspace-write` unless it is named a writable root, so the managed path could edit and test but
not deliver. Fixed: `launch_argv` and `resume_argv` take `writable_roots`.

And one observation worth keeping even though nothing depends on it yet: **both blocked workers
reported the UNCHANGED BASE sha as their `candidate_sha`.** A worker's report of what it produced is
a claim, not evidence.

## Round 2 — the same path, after the fixes

Two workers concurrently, on accounts 1 and 2, same brief.

| item | profile | provider | effort | outcome | reported sha | repo HEAD |
|---|---|---|---|---|---|---|
| E2E-D | muse-go-1 | muse-go-1 | xhigh | **OK** | 814204f4 | `814204f add mul` |
| E2E-E | muse-go-2 | muse-go-2 | xhigh | **OK** | aa3ea71c | `aa3ea71 add mul` |

Both edited, both ran their tests, both committed, both wrote a schema-valid terminal result
correlated to their own item and session. The reported sha is the repository's actual HEAD in each
case, and the two shas differ — the workers did not touch each other's repository.

35s and 30s wall clock, concurrent.

## The parked question, and the exact resume

E2E-Q was briefed on a task whose specification was deliberately incomplete. It did not guess: it
returned `outcome: "question"` with a single question — *"What should div(a, b) do when b is
zero?"* — and `candidate_sha: "none"`. That is the parked-question half.

The answer was then delivered by resuming **that exact session id**, with the argv confirmed live:

    codex exec --json --strict-config -p muse-go-3 -s workspace-write -o <fresh result path> \
      -c sandbox_workspace_write.writable_roots=[…] --output-schema … resume 01a07e23-81d3-… -

Parent options before `resume`, the exact stored id after it, and no `--last` anywhere.

The resumed run reported back **the same session id it was given**, `01a07e23-81d3-7c62-8a28-7f0dbce4783c`,
and its rollout confirms `muse-go-3` / `muse-spark-1.3-contributor` / `xhigh` in `wtC`. It is the
same session continuing, not a new one that happened to be handed the same brief.

It then implemented what the answer specified, tested both branches, and committed:

    def div(a, b):
        if b == 0:
            raise ValueError("division by zero")
        return a / b

`6772f9a add div`, matching the `candidate_sha` it reported. 92s.

## What this establishes, and what it does not

**Established, on real workers:** per-profile routing takes; three accounts stay isolated in
sessions, providers and working directories; two managed workers run simultaneously without
touching each other's repository; a bounded edit/test/commit/report completes and is verifiable
against the repository rather than the worker's word; a worker with an incomplete specification asks
instead of guessing; and an exact-id resume continues that same session and finishes the work.

**Not established.** Three concurrent is a demonstrated floor, not a ceiling. These are trivial
tasks in tiny repositories — they say nothing about a worker on the platform repo with a real test
suite. Wall-clock times here (30–92s) are not a basis for any timeout. And the 27× token spread ★
measured on identical prompts remains unexplained, so worker cost is still not predictable and no
slot count follows from any of this.

**One caveat that must travel with every row above:** effort is what the CLI *sent*. Whether the
provider honoured it is not observable from here.
