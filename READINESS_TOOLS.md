# Readiness tools — 2026-09-08

## CircleCI account selection

Use `python3 circleaccount.py 1` (or 2/3) to verify identity. Run commands as:

```sh
python3 circleaccount.py 3 -- org list --json
python3 circleaccount.py 3 -- run get RUN_UUID --json
```

The launcher reads the dedicated `voicepod-circleci-account-N` macOS keychain
entry, passes it only through the child environment, and verifies `auth me` against
the expected account UUID before executing a command. It does not rotate or reset
credentials. It refuses shared login/config/server overrides. A config filename
alone does not select one of these dedicated keychain entries.

Identity was verified live for all three accounts. Accounts 1 and 2 returned empty
organization lists; authentication is not additional test capacity. Account 3 has
the existing project. Never infer available credits from the number of accounts.

## Desktop task observation fallback

Use the native task API first. When it omits progress or final text, reconcile the
exact task and turn against its durable session log:

```sh
python3 sessionstatus.py SESSION_UUID --turn TURN_UUID
python3 sessionstatus.py SESSION_UUID --turn TURN_UUID --include-text
```

This is read-only and does not expose tool outputs or reasoning. It verifies the
session identity, refuses ambiguous logs or missing requested turns, and returns
the final response only with `--include-text`. A newer turn never inherits an older
turn's completion. Partial/corrupt evidence is explicitly marked. `completed`
means that model turn ended, not that tests passed or the product was accepted.
Absence of evidence must not cause an automatic duplicate launch or process kill.

Live verification recovered the readiness worker's 70 tool events and final report
for turn `01a080f6-6dc0-7f73-bace-e68a2f00600f`, which the desktop wait API omitted.

## Managed Muse runner

`museadapter.run_attempt` now passes the declared worktree to the real subprocess,
streams events to its attempt log before completion, and preserves partial output
on timeout. Cancellation stops its own process group, including children retaining
output pipes. It cannot launch a worker if its event file cannot be opened.

The daemon has a separate asynchronous launch/reap path; this patch does not
replace that scheduler or enable a new fleet. The standalone managed entry point
is fixed, but provider success, exact continuation and sustained concurrency must
still be demonstrated before a long Muse run.

## Verification

New tests cover conflicting CircleCI credentials, identity mismatch, command
override refusal, exact-turn result recovery, partial/ambiguous logs, worktree/EOF
handling, live output, and timeout with closed output pipes. The existing dispatcher
suite passed with process-read permission and an isolated Python cache:

```sh
PYTHONPYCACHEPREFIX=/private/tmp/voicepod-readiness-pycache zsh tests/run_all.sh
```

The existing suite includes a legacy Claude-session live census; it is not entirely
hermetic and may fail when that environment is absent. New tests make no provider
calls. The bounded code changes also received independent Astra review.

No production deployment, model substitution, new organization, fleet restart or
long autonomous run is implied by installing these tools.
