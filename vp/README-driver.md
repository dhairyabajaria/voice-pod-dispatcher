# vpdriver — the cheap-model turn daemon (SPEC.md §Driver)

`vpdriver.py` is the only process that launches OpenCode turns. It owns no state:
the store (`vpstore.py`) is reached exclusively through the `vpctl.py` CLI with
`--json`, so the driver and the store can be developed and deployed apart.

Stdlib only. Python 3.9+.

## Run

```sh
cd "$CN/dispatcher/vp"
python3 vpdriver.py --roster "$RUN_ROOT/roster.json" --once   # one tick, then drain
python3 vpdriver.py --roster "$RUN_ROOT/roster.json" --loop   # tick until STOP
```

Options:

| flag | default | meaning |
|---|---|---|
| `--roster` | required | `RUN_ROOT/roster.json`; **RUN_ROOT is its parent directory** |
| `--once` / `--loop` | required (one of) | one tick and drain, or tick forever |
| `--interval` | `5.0` | seconds between ticks |
| `--stop-file` | `$CN/test-logs/driver/STOP` | `$CN` is derived as the parent of `roster.trunk` |
| `--vpctl` | `<python> vpctl.py` | shell-split command used for every store call |
| `--max-ticks` | none | bounded `--loop`, for rehearsals |

Validate a payload by hand:

```sh
python3 vpschema.py result   <worktree>/.vp/RESULT.json
python3 vpschema.py findings <worktree>/.vp/FINDINGS.json
```

## What one tick does

1. Write `RUN_ROOT/driver.heartbeat` (atomic rename) — **always**, including
   while stopping.
2. If the STOP file exists: `POST http://127.0.0.1:<port>/session/<sid>/abort`
   for every in-flight turn (once per turn), spawn nothing, return. Running
   turns end as `ABORTED` and are never submitted.
3. `vpctl report items --json` for the queue and `vpctl report liveness
   --json` for items that already carry a RUNNING attempt.
4. `ASSIGNED` → create the worktree from the template, then
   `vpctl claim <item> --role builder<group_no> --worktree <wt> --expected-rev
   <rev>` so the item actually reaches `BUILDING`. `BUILDING` → builder turn.
   `GRADING` → junior turn.
5. Items inside a driver-side failure backoff are skipped (see below).
6. Route the item's `group_no` to a server via `roster.groups`; a quota-skipped
   server falls through to `zen` when `roster.zen_fallback` is true (the turn
   record carries `"tier": "primary" | "zen_fallback"`).
7. Spawn at most `max_concurrent` turns per server. One thread per turn.

## Driver-side failure backoff

A *driver-side* failure is the driver's own machinery failing — a refused or
broken `vpctl` call, a worktree that cannot be built — as opposed to a model
turn that ran and was classified. Without a backoff a permanently broken item
is retried every tick: the first live run produced **8 identical STALLED
escalations in 2 minutes** because `turn start` exited 2.

Per item the driver keeps `(count, next_try, rev, stuck)`:

| consecutive failures | escalation | next retry |
|---|---|---|
| 1 | `STALLED` (detail says `1/3`) | +60 s |
| 2 | `STALLED` (`2/3`) | +120 s |
| 3 | `STUCK`, once | never, until the item row's `rev` changes |

Exactly one escalation per failure event, so a stuck item costs one line, not
one per tick. A changed `rev` clears the entry (somebody changed the thing that
was broken); a turn that completes without a driver-side exception resets the
counter.

## One turn

```
opencode run --attach http://127.0.0.1:<port> --dir <worktree> \
  --agent <vp-builder|vp-junior> --model opencode-go/<model> --variant <v> \
  --format json --title "<item> <kind> r<round>" [--session <sid>] "<prompt>"
```

* **`stdin` is `/dev/null` for every child** — `opencode run` blocks forever
  when fd 0 is an open pipe. This was the root cause of the first real run's
  hang; it applies to the turn, every resume, and `export`.
* env: `XDG_DATA_HOME` = that server's `data` dir from roster.json; `PATH` has
  `~/.local/node-v22/bin` first when it exists; `XDG_CONFIG_HOME` untouched.
* stdout is read line by line and folded by `EventAccumulator` (see below). The
  **first 20 raw lines are stored verbatim** in the turn record so the parser
  can be corrected after a format change without re-running the turn.
* `opencode export <sid>` runs with the **same** `XDG_DATA_HOME` and lands in
  `<n>-<role>.export.json`.
* `vpctl turn end` closes the attempt, then `submit-result` (builder) or
  `findings` (junior) — only on `DONE`.

### `--format json` event stream

One JSON object per line, verified against the real CLI:

```json
{"type":"step_start|tool_use|text|step_finish","timestamp":<ms>,
 "sessionID":"ses_…","part":{…}}
```

* **`sessionID` is top-level on every event**; the driver takes it from the
  first event (and keeps the tolerant recursive search as a fallback only).
* `text.part.text` is the assistant text.
* `step_finish.part` carries `reason` (`"stop"`), `cost`, and
  `tokens:{total,input,output,reasoning,cache:{write,read}}`.
  **Usage is summed over every `step_finish`** — one run is many steps, so
  reading only the last one undercounts.
* a denied tool is `tool_use` with `part.state.status == "error"` and an
  `error` containing `rule which prevents`. Denials are collected into
  `denied_tools` on the turn record; they are *not* treated as provider errors.

### Prompts (byte-identical across every round — prompt caching)

* builder: `Read .vp/PACKET.md and .vp/BENCHMARK.md. If .vp/FINDINGS.json exists repair only its FAIL lines. Follow the agent rules. Write .vp/RESULT.json per .vp/RESULT_SCHEMA.json and stop.`
* junior: `Grade the current HEAD commit of this worktree against .vp/BENCHMARK.md line by line. Write .vp/FINDINGS.json per .vp/FINDINGS_SCHEMA.json with file:line evidence and stop.`
* resume: `continue`

Nothing is interpolated into a prompt. Item-specific payload lives in the
worktree files; `--title` carries the item/round for the human. The turn record
stores `prompt_sha256`, so a cache miss is diagnosable after the fact.

## Classification table

Checked in this order against the whole captured stream. **Exit code 0 is never
success by itself and is never an input to classification** — it is only
recorded.

| # | Signal | Status | Driver action |
|---|---|---|---|
| 1 | `401`, `unauthorized`, `invalid api key`, `invalid_api_key` | `AUTH` | escalate + macOS notification |
| 2 | `429`, `rate limit`, `rate_limit`, `quota`, `too many requests` | `QUOTA` | mark server SKIPPED (15 min), escalate + notification |
| 3 | output file missing, **last `step_finish` reason is `stop`** | *progress-stop* | resume the same session with `continue`, ×3 max |
| 4 | output file missing **and** (`provider`+`error`, or a stream error with no assistant text) | `STALLED` | escalate (no notification) |
| 4b | output file missing, no error text, no `stop` | *progress-stop* | as row 3 |
| 5 | still missing after 3 resumes | `STUCK` | escalate + macOS notification |
| 6 | output file present but fails `vpschema` | `INCOMPLETE` | escalate (no notification), **never submitted** |
| 7 | output file present and valid | `DONE` | `submit-result` / `findings` |
| 8 | STOP file in force | `ABORTED` | abort POST; nothing submitted, no escalation |

Output file = `<worktree>/.vp/RESULT.json` (builder, infra) or
`<worktree>/.vp/FINDINGS.json` (junior).

Escalations are written through `vpctl escalate --kind … --item … --attempt …
--detail …` for kinds `STUCK STALLED QUOTA AUTH INCOMPLETE`. That verb takes no
`--ref`, so the turn-record path travels inside `--detail` as `[record=…]`.
Notifications go out via `osascript -e 'display notification …'` for `STUCK`,
`QUOTA`, `AUTH` only.

## Turn record — `RUN_ROOT/turns/<item>/<attempt>/<n>-<role>.json`

`n` counts **process invocations within one attempt**: `1` is the real prompt,
`2..4` are the `continue` resumes. Each gets its own record. Fields: `item,
attempt, n, role, kind, round, server, port, tier, session_id, agent, model,
variant, prompt_sha256, prompt_bytes, argv, start_ts, end_ts, duration_s,
exit_code, classification, classification_detail, tokens_in, tokens_out,
tokens_reason, cache_read, cost, event_lines, steps, last_step_reason,
denied_tools, raw_head, xdg_data_home, paths{
worktree, output, output_exists, packet, benchmark, export, record}`.

`RUN_ROOT/driver.heartbeat` (every tick): `ts, mono, pid, tick, stopping,
roster, run_root, active_by_server, live_items, servers{port, skipped_until,
skip_reason}`.

## Worktree template (SPEC §Worktree template)

```
git -C <trunk> worktree add <worktrees>/<item> -b vp/<item> <base_sha>
```
then the three read-only symlinks — `agent/.venv`, `portal/node_modules`,
`platform/.venv` → the trunk's — then `<worktree>/.vp/` gets `PACKET.md`,
`BENCHMARK.md` (copied from the packet row's paths) and `RESULT_SCHEMA.json`,
`FINDINGS_SCHEMA.json` (from `vp/schemas/` when present, else the embedded
copies in `vpschema.py`).

## Tests

```sh
python3 tests/test_vpdriver.py     # 28 plain-assert tests, no pytest
```

Fakes (all real files next to the test, all effect-free): `fake_opencode.sh`,
`fake_git.sh`, `fake_osascript.sh`, `fake_vpctl.py`. No network, no server, no
real `opencode`, no real repository, nothing written outside a temp dir.

Covered: builder `DONE` → `submit-result`; junior `DONE` → `findings`; missing
RESULT.json → exactly 3 resumes (same session, prompt `continue`) then `STUCK`;
a 429 body → `QUOTA` + server skipped + not re-routed; a 401 → `AUTH`;
schema-invalid → `INCOMPLETE` and never submitted; STOP → abort POST to the
exact abort URL and zero new spawns; per-server concurrency peak ≤ 2 with 6
queued items (the fake processes count themselves; the test also asserts the
peak *reached* 2, so it cannot pass vacuously); heartbeat contents on
consecutive ticks; ASSIGNED items get a worktree and no turn; the
`escalations.jsonl` fallback; zen fallback routing; an item with a RUNNING
attempt getting no second turn; an ASSIGNED item being claimed with the right
role/worktree/expected-rev; a `turn start` that exits 2 producing exactly one
escalation across five ticks (not five) and only one retry; three consecutive
driver-side failures marking the item STUCK and not retrying until its rev
changes; a refused claim backing off the same way; a missing benchmark refusing
instead of writing an empty one; session-id parser tolerance; and that exit 0 alone is
not success. From the first real run: every child process started with
`stdin=devnull` (the fake records the kind of fd 0 it was handed); usage summed
across two `step_finish` events rather than taken from the last; `sessionID`
read from the top level of the first event; a `stop` reason with no output file
classified as a progress-stop; a denied tool recorded as `denied_tools` instead
of a stall; `packet show` used when the item row lacks the paths; and
`escalate` called with only the flags vpctl accepts (no `--ref`).

Negative controls run against this tree (mutate → red → restore, file verified
byte-identical afterwards): removing the backoff gate reds the five-tick test
with the exact live symptom (`got 5` escalations); removing the `claim_item`
call reds the ASSIGNED test (`never claimed, it would sit forever`);
`stdin=DEVNULL` → `stdin=PIPE` reds the stdin test
(`['pipe']`); summing → last-wins reds the usage test (`tokens_in not summed:
200`); removing the `max_concurrent` check reds the concurrency test
(`peak 6 > 2`); removing the abort call reds the STOP test
(`no abort POST was sent`); making `classify` always return `DONE` reds the
schema test.

## Decisions

1. **Store access is CLI-only.** The driver never imports `vpstore`/`vpctl`; it
   shells `python3 vpctl.py … --json` through `Runner`. A sibling agent owns
   those files, and `tests/fake_vpctl.py` substitutes for them completely.
2. **`vpctl report items --json`** is the work queue:
   `{"items":[<item row>, …]}` (a bare JSON array is also accepted). The rows
   now carry `packet_path` / `benchmark_path`; when a row lacks them the driver
   falls back to `vpctl packet show <item> --json` and only then refuses
   (Decision 4). `session_id` and `running_attempt` are read from the same rows
   when present.
3. **Concurrent-attempt guard is two-layered.** In-process, an item with a live
   turn thread is skipped. Across processes, the driver calls
   `vpctl report liveness --json` once per tick and skips any item that already
   has a `RUNNING` attempt row. A *stale* RUNNING attempt (crashed driver) will
   therefore park the item until Recovery closes it — deliberately, because the
   alternative is two writers in one worktree.
4. **An absent packet or benchmark is a refusal, not an empty file.** If
   `rec["benchmark_path"]` / `rec["packet_path"]` is missing or does not exist,
   and `<worktree>/.vp/` does not already hold a non-empty copy from an earlier
   round, `ensure_worktree` raises, the item is escalated, and **no turn is
   spawned**. An empty BENCHMARK.md grades as vacuously satisfied and an empty
   PACKET.md burns a whole turn; both would look like progress.
5. **`vpctl escalate --kind --item --attempt --detail`** is the escalation
   path. It has **no `--ref`**, so the turn-record path is appended to
   `--detail` as `[record=<path>]`. The kinds the driver emits
   (`STUCK STALLED QUOTA AUTH INCOMPLETE`) are all inside
   `vpstore.Store.ESCALATE_KINDS`. If the verb ever exits 2 or 3 the driver
   still appends the row to `RUN_ROOT/escalations.jsonl` itself, so an
   escalation is never lost to a CLI-surface disagreement; on success it does
   not double-write.
6. **`attempt_id`** is read from `turn start`'s `attempt_id` (or `attempt`) key
   and used verbatim as the `turns/<item>/<attempt>/` directory name. If the
   store returns nothing usable the driver falls back to `a0` rather than
   dropping the record.
7. **`attempt` in the payload schemas** is an integer per `schemas/*.json`; the
   validators also accept a digit-string, because the store CLI may hand the
   model a string id. Nothing else is loosened.
8. **`all_pass` is cross-checked** against the per-line verdicts in
   FINDINGS.json. A disagreement is an error — so a junior that writes
   `all_pass: true` over a `FAIL` line is `INCOMPLETE`, not silently satisfied.
9. **Extra keys are ignored** by both validators; only the SPEC keys are
   required and typed. Commit/base must look like a git sha (7–40 hex).
10. **The output file is deleted before the first process of an attempt.** A
   round-2 builder would otherwise be graded on round 1's RESULT.json, and a
   progress-stop would read as `DONE`. Resumes do not delete it.
11. **Classification ignores the exit code** entirely (it is recorded only).
   Order is AUTH → QUOTA → STALLED/progress-stop → schema. Markers are matched
   case-insensitively anywhere in the captured stream, so a session id or path
   containing `429` would misclassify; the `raw_head` field makes that
   diagnosable, and no path or argv is ever fed into the classifier.
12. **QUOTA cooldown is 15 minutes** when the stream carries no reset hint.
    SPEC says "until reset hint" but the CLI's hint format is unverified; when
    the first real 429 body is in a turn record, parse it here.
13. **`$CN`** is derived as the parent of `roster.trunk`, so the STOP file
    default is `<parent of trunk>/test-logs/driver/STOP`. Override with
    `--stop-file`.
14. **RUN_ROOT** is the roster file's parent directory. No second flag, no env.
15. **One thread per turn, not a subprocess pool.** The per-server counter is
    acquired before the thread starts and released in the thread's `finally`,
    so a crashing turn cannot leak a slot.
16. **STOP aborts but does not kill.** The driver POSTs to the abort endpoint
    and marks the attempt `ABORTED`; it never `SIGKILL`s an OpenCode process,
    and it never `pkill`s anything. Aborted turns are not escalated (the stop
    was deliberate) and are never submitted.
17. **`--once` drains.** It ticks once and then joins the turns it started, so
    the records and `turn end` calls are complete when the process exits.
18. **Git push / CircleCI is out of scope** for this file — `vpcircle.py` owns
    it. The driver never pushes a branch.
19. **`stdin=DEVNULL` on every child process**, not just `opencode`.
    `opencode run` never returns when fd 0 is an open pipe — the hang on the
    first real run. `Runner.run` and `Runner.spawn` both set it, so the turn,
    each `continue` resume, `export`, `git` and `vpctl` all get `/dev/null`. A
    test asserts the fake opencode saw `devnull` and not `pipe` on fd 0; the
    fake refuses a pipe rather than hanging the suite.
20. **Usage is summed over every `step_finish`, not taken from the last one.**
    A run is many steps; the last step's `tokens` cover that step only. `cost`
    is summed the same way. When a run produced no `step_finish` at all the
    usage fields stay `None` rather than becoming a misleading zero, so nothing
    is reported to `turn end`.
21. **The session id is `sessionID` at the top level of every event**, taken
    from the first event. The recursive `find_session_id` search is kept only
    as a fallback for a future format change, and is covered by its own test.
22. **A denied tool is not a stall.** `tool_use` with
    `part.state.status=="error"` and `rule which prevents` in the error is
    recorded in `denied_tools` on the turn record and left out of the
    error signal, so an agent-rule denial resumes as a progress-stop instead of
    being escalated as `STALLED`. Any *other* `tool_use` error does count as a
    stream error.
23. **`last_step_reason == "stop"` with no output file is the progress-stop
    case** and is checked before the stall heuristics: the model ended its turn
    cleanly and simply did not write the file, which is exactly what `continue`
    is for.
24. **`vpctl claim` is the driver's job.** Right after `ensure_worktree` for
    an `ASSIGNED` item the driver claims it as `builder<group_no>` (or the
    row's own `builder_role` when set) with `--expected-rev` from the item row.
    Without this an `ASSIGNED` item never reaches `BUILDING` and sits forever.
    A refused claim (stale rev, exit 4) is a driver-side failure and goes into
    the backoff like any other.
25. **Driver-side failures back off per item** (table above) instead of
    retrying every tick. A model turn that ran and was classified is *not* a
    driver-side failure and is unaffected — only the driver's own machinery
    failing counts.
26. **`Store.call` appends `--json` to every verb.** vpctl now strips `--json`
    from argv anywhere and sets `args.json`, so this is safe for verbs that do
    not declare the flag; the driver keeps appending it unconditionally rather
    than maintaining a second list of which verbs support it.

## Cost telemetry (K-08) — runners that report tokens but no USD

Claude (`total_cost_usd`) and OpenCode (`cost`) report dollars; Codex reports
only tokens. `vpdriver.estimate_cost` prices such a turn from the roster:

```json
"pricing": {
  "codex": {
    "billing": "subscription",
    "models": {"gpt-5.6-sol": {"input_per_1m": 0, "cached_input_per_1m": 0, "output_per_1m": 0}}
  }
}
```

Every `costs.jsonl` line carries `cost` (what the budget counts), `est_cost_usd`
and `cost_basis`: `reported` (the runner said), `estimated` (`billing: "api"`,
the estimate is the spend), `subscription` (tokens priced, spend counted as 0 —
the K-08 rule), or `unpriced` (no table for the model: cost stays null and an
`UNPRICED` owner alert fires once per model). `attempt.cost` gets the same
number. Codex `output_tokens` already include reasoning; cached input is priced
once at the cached rate. Measured on run-v12-20260913: 139 Codex reviews,
mean 962k input (882k cached) / 8k output, every `cost` null before this.

## Store-surface items — resolved

Both earlier blockers are closed and the driver now uses the real verbs:

* `report items` rows carry `packet_path` / `benchmark_path`, with
  `packet show <item> --json` as the fallback read.
* `vpctl escalate` exists (no `--ref`; the record path rides in `--detail`).

The `escalations.jsonl` fallback stays as a belt-and-braces path and is still
tested.
