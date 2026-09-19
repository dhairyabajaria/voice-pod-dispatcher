# vpdash — handoff for the next agent

The live dashboard for a v13 orchestration run. It belongs to the **driver-orchestration repo**
(`dispatcher/`, GitHub `dhairyabajaria/voice-pod-dispatcher`), **not** to the application repo
(`voice-pod/chief9-recovery`). It only READS the run's files; it never writes into the application
repo, the scheduler state, or the driver's files. Its own output is one log file per start in the run root.

Written 2026-09-19 (dispatcher commits D60–D62b). Everything below is measured from the live run
`run-v13-20260917` at 15:10Z; counts grow while the run is active.

---

## 1. Code — all files, absolute paths

Root of the repo: `/Users/dhairyabajaria/Claude Code/Calling New/dispatcher/`

| File | Lines | What it is |
|---|---|---|
| `vp/vpdash.py` | 669 | **The dashboard.** stdlib `ThreadingHTTPServer`; `class Dash` builds a snapshot every 10 s from a `vpjourney.Journey`; `make_handler()` serves the JSON API; `PAGE` (a Python string near the bottom, ~line 330–460) is the entire HTML/CSS/JS page — no external assets, no build step. |
| `vp/vpjourney.py` | 856 | **The data model.** `TailReader` (byte-offset, torn-line-safe JSONL reader), `EventStore` (events/*.json), `class Journey` (roots, journeys, `scope()`, `summary()`, `verify()`, `render_ledger()`), and a CLI. vpdash imports this. |
| `vp/vpalerts.py` | 248 | **Alert severity from pattern** (repeat / family repeat / streak age / backlog). vpdash calls `vpalerts.annotate()` over alerts.jsonl; the live driver also calls `vpalerts.assess()` when it writes an alert. Shared with the driver — changing thresholds here changes the driver's paging after its next reload. |
| `vp/tests/test_vpdash.py` | 168 | 6 tests: snapshot totals, alerts panel, CircleCI panel (all 6 accounts + refusal rows), decisions/gate age, HTTP API, `--once` CLI. Builds its fixture with `test_vpjourney.build_run`. |
| `vp/tests/test_vpjourney.py` | 288 | 9 tests incl. the synthetic run-root builder `build_run(tmp_path)` — the fastest way to see every input file's shape in miniature. |
| `vp/tests/test_vpalerts.py` | 106 | 10 tests for the severity rules. |

Modules vpdash/vpjourney import from the same dir (read-only, do not edit for dashboard work):
`vp/vppack.py` (PACKET.md loader — `load_pack`, `dynamic_id`, `twin_of`, `gate_open`), `vp/vpcircle.py`
(`TARGETS`: the six CircleCI accounts `3, 1, 2, A1, A2, A3` with org/repo names).

Related but NOT dashboard code (driver patches I wrote, already landed by the Fixer as D54–D57):
`ci/expert-alert-severity.patch`, `ci/expert-base-invariant.patch`, `ci/expert-gates-jsonl.patch`,
`ci/expert-circleci-trigger-gate.patch`. Leave them; they are history.

### Run / test

```bash
# interpreter: the platform venv (system python3 has no pytest)
PY="/Users/dhairyabajaria/Claude Code/Calling New/voice-pod/chief9-recovery/platform/.venv/bin/python"
cd "/Users/dhairyabajaria/Claude Code/Calling New/dispatcher"

# tests (fast, no box, no locks needed)
"$PY" -m pytest vp/tests/test_vpdash.py vp/tests/test_vpjourney.py vp/tests/test_vpalerts.py -q -p no:cacheprovider

# one snapshot to stdout, no server
"$PY" vp/vpdash.py --run-root "/Users/dhairyabajaria/Claude Code/Calling New/test-logs/audit/run-v13-20260917" --once

# the server (what is running now)
"$PY" vp/vpdash.py --run-root "/Users/dhairyabajaria/Claude Code/Calling New/test-logs/audit/run-v13-20260917" --port 4180
#   flags: --bind 127.0.0.1  --refresh-s 10  --control-every-s 60

# the model's own CLI
"$PY" vp/vpjourney.py --run-root <run_root> summary | scope | roots | journey <ROOT-or-ROW> | verify [--no-control] | ledger   [--json]
```

**Running instance:** pid in `/private/tmp/claude-501/-Users-dhairyabajaria-Claude-Code-Calling-New/8c63237f-e833-4ea8-9048-1d42cc30d44c/scratchpad/vpdash.pid`
(session-scoped; if that dir is gone, `lsof -nP -iTCP:4180 -sTCP:LISTEN` finds it). Stop it with `kill <that pid>` only —
**never `pkill -f python`/pattern kills on this box** (the driver and Postgres share it). Its log: `<run_root>/vpdash-<UTC ts>.log`
(one per start; today's are `vpdash-20260919T13*.log`, `vpdash-20260919T140736Z.log`). It is a hand-started process, not a
service — after a Mac restart someone re-runs the server command.

### HTTP API (`vp/vpdash.py` lines ~484–512)

| Path | Returns |
|---|---|
| `/` | the page (`PAGE` string) |
| `/api/snapshot` | everything below in one object (what the page fetches every 15 s) |
| `/api/summary` | `generated_at, summary (roots/scope/counts), freshness (heartbeat, sequences, control.jsonl offset), refresh_ms, errors` |
| `/api/roots` | list of root work items: `id, kind, title, state, primary_row/state, hosted_state, open_rows, superseded_open, attempts, latest_ts, settled, done, owed, rows, states` |
| `/api/journey?root=X` | full journey of a root (X may be a row id; it resolves to its root): attempts → steps (events + runner turns + proofs), each step with its `source` file |
| `/api/row?task=X` | one scheduler row: run-state row, driver packet record, events, turns, proofs |
| `/api/circleci` | per-account usage for all six accounts + cap/gate/mode from the roster |
| `/api/alerts` | by_severity, by_kind, open_conditions, recent (last 300), backlog_now |
| `/api/decisions` | gates (open/closed, age, source, held rows), blocked_by_reason, open_rulings, owner_questions |
| `/api/verify` | the `vpjourney.verify()` report (findings with check/severity/task/detail) |
| `/api/ledger.md` | a LEDGER derived from events only (text) |
| `/healthz` | `{ok, generated_at}` |

---

## 2. Data — every file the dashboard reads

The run root: **`/Users/dhairyabajaria/Claude Code/Calling New/test-logs/audit/run-v13-20260917/`** (not a git repo).
`vpdash --run-root` takes this path; everything else is found from `<run_root>/roster.json` → `run.*` keys
(`run_state`, `packets_dir` (tests spell it `pack_dir`), `catalog`, `run_root`, `trunk`).

### 2a. Scheduler state (outside the run root — the authoritative stream)

| Path | Shape | Used for |
|---|---|---|
| `/Users/dhairyabajaria/Claude Code/Calling New/voice-pod/advisor-plans/outbound-launch/orchestration-state/events/*.json` (1,621 files, one per event) | `{sequence, type, task_id, created_at, event_id, …}`; per type: `resolved_model/resolved_effort` (TASK_STARTED), `outcome`/`reason` (TASK_COMPLETED), `base_sha/stacked_base/stacked_on/chief` (TASK_CLAIMED), `output_sha` (TASK_PROMOTED_TO_INTEGRATED), `closers` (TASK_RETIRED), `class/blocker` (TASK_BLOCKED), `template_id/parent_contract_id` (TEMPLATE_TASK_INSTANTIATED), `from/to/actor` (TASK_DEPENDENCIES_REWIRED) | **The source of truth.** Journey steps, root ordering, derived state, sequence-gap check, backlog series. Loaded once per file (`EventStore`). |
| `…/orchestration-state/run-state.json` | `{sequence, updated_at, tasks: {task_id: row}}`; row keys: `state, dynamic, kind, template_id, parent_contract_id, parameters.packet_id, depends_on, stacked_on, base_sha, output_sha, blocker, review_key, attempt_id, chief, updated_at` | Current state per row, identity (packet_id, parent), blockers. **Never use `updated_at` for timing — the scheduler touches it every tick.** |
| `/Users/dhairyabajaria/Claude Code/Calling New/voice-pod/advisor-plans/outbound-launch/lane-contracts.json` | catalog of the 49 fixed lanes | `catalog_file_rows` cross-check only |
| `/Users/dhairyabajaria/Claude Code/Calling New/voice-pod/advisor-plans/outbound-launch/v13-pack/03-PACKETS/<ID>/PACKET.md` (+ `BENCHMARK.md`) — 92 dirs | YAML front matter: `item, title, scheduler_task, template, v13_kind, depends_on, owner_gate, …`; hosted twins (`<ID>-HOSTED[-GATE]`) are synthesised by `vppack.load_pack` from `[hosted]` rows in BENCHMARK.md and carry `twin_of` | Titles, root identity (twin → parent; a packet named after a catalog row regrades that row), twin gates. Reloaded every 120 s. |
| `/Users/dhairyabajaria/Claude Code/Calling New/voice-pod/advisor-plans/outbound-launch/orchestration_control.py` | the scheduler | **not read** — listed because `roster.run.scheduler` names it; never edit it (AUTHORITY_MISMATCH re-pin trap) |

### 2b. Driver files inside the run root

| Path | Shape | Used for |
|---|---|---|
| `roster.json` (11 KB, hot-reloaded by the driver) | `run.*` paths; `owner_gates {DELIVERY-1..5, 2A, 2B: bool}`; `proof.circleci {enabled, mode, account, rotation, spread, max_pipelines_per_day, max_pipelines_in_flight, deadline_min}`; optional `alerts.severity` overrides for vpalerts | paths, gate values, CircleCI caps/rotation |
| `roster.N.json` (17 snapshots, N=1..) | the roster as it was after each reload; **file mtime = when that reload happened** | gate age fallback: earliest snapshot in the trailing run with the current value |
| `gates.jsonl` (new, driver D64/D57) | `{ts, gate, from, to, why: startup\|reload}` | exact gate flip time — preferred over snapshot mtimes when a `reload` row exists |
| `packets/<TASK>.json` (402 records; skip `*.params.json`) | `{packet, task, retry_of, retry_reason, parent_contract_id, template, v13_kind, …}` | which packet a row came from; `retry_of` = the row this -Rn row replaces (a row with a successor is "superseded" and never counted as open) |
| `costs.jsonl` (233 KB) | one line per runner turn: `{ts, task, attempt, n, round, role, runner, server, model, status, cost, duration_s, tokens_*}` (no effort) | journey "turn" steps, turn counts |
| `turns/<task>/<attempt>/<n>-r<round>-<role>-record.json` | `{effort, variant, model_seen, server, session_id, …}` | effort/model_seen on a turn step **only when this file exists** — nothing is invented |
| `proofs/proof-<task>-<stamp>.json` (112) | `{proof_id, status, route (box\|circleci\|none), sha, ts, kind, counts, account, pipeline_id, branch, reason}` | proof steps; CircleCI outcomes per account; `BLOCKED_GATE/CAP/OFF` = refused at trigger time |
| `proofs/circleci-pipelines.jsonl` | `{ts, proof_id, pipeline_id, account, sha, status?, reason?}`; `status` ∈ `triggered\|refused_gate\|refused_cap\|refused_off\|credits_blocked\|trigger_failed` (rows without status are pre-D65 = triggered) | pipelines today/total per account, in-flight (triggered with no proof record yet), lost (in-flight older than deadline+30 min), refusal counts |
| `alerts.jsonl` (225 KB, ~600 lines) | `{ts, kind, task, text}` + since D54: `severity, repeat, repeat_family, age_s, reasons` | alerts panel via `vpalerts.annotate` (recomputed, and compared to the driver's recorded severity) |
| `control.jsonl` (**250 MB**) | driver→scheduler calls `{ts, verb, argv, rc, seq_before, seq_after, ms, stdout…}` | `verify` only: every event sequence should fall inside some call's `(seq_before, seq_after]`. Read by byte offset: whole file once at startup, then ≤64 MB per minute; a slimmed dict per line is kept (`ts, verb, rc, seq_before, seq_after, task, ms`) |
| `driver.heartbeat` (JSON, rewritten every tick) | `{ts, pid, tick, active, sequence, idle_since, budget{spent_usd,max_usd,tokens}, runners, servers, reloads, code_version, …}` | the header line (driver alive?, seq agreement, budget) |
| `RULINGS-TRACKING.md` | table `\| R# \| by \| ruled at \| action \| status \| …` | "rulings not executed" = rows whose status matches `PENDING\|BLOCKED\|needs` and is not EXECUTED |
| `comms.jsonl` (330 KB) | `{ts, from, to, kind, task, text}` | owner questions = `to == "owner"` and `kind` starts with `question` |
| `LEDGER.md` | the driver's rendered table | **read only by `verify`** to flag drift vs run-state; **never used for any number on the page** |
| `DECISIONS.md` | the run's decision log — every issue/fix/proof, incl. rows D60–D65 and the backfill for this work | not read by the dashboard; where you log what you change |

Not read (exist in the run root, may be useful): `driver.log`, `driver.out`, `renders.jsonl`, `reloads.jsonl`, `git.jsonl`,
`unions/*/members.json`, `OWNER-ALERTS.md`, `INCIDENT-*.md`, `probes/`, `hosted/`, `seals/`.

---

## 3. The model, in five rules (so the numbers make sense)

1. **Work item = root.** A catalog row (49) or a new-work packet (55) = **104 fixed**. A packet dir named after a catalog row
   (`L06`…`L44`, 34 of them) *regrades* that row and counts once. `-Rn` rows are attempts of the same root; hosted twins
   (`X-HOSTED`) attach to X; review/repair rows attach through `parent_contract_id`. `scope()` shows the arithmetic and
   `verify` warns if fixed ≠ derived.
2. **Root state comes from the LIVE rows.** primary row = the catalog row, else the newest non-CANCELLED `-Rn` of the packet's own
   row. A retired row and its successor share the retirement timestamp — live wins. Rows with a `retry_of` successor are
   history ("superseded"), not open; 47 hosted twins sit `INVALID_EVIDENCE` with an `-R1` — that is why the driver's LEDGER
   count is inflated. `state` = worst open row (RUNNING > REPAIR_REQUIRED/INVALID_EVIDENCE > BLOCKED > WAITING) else the primary's.
3. **Held by a gate** only when the gate is CLOSED (or absent from `owner_gates` — reads closed, e.g. `O3` named by L35).
   The 48 BLOCKED rows are `EXTERNAL: HOSTED-TWIN-BASE-LACKS-SEED-FIX` (waiting on L09-SEED-FIX), not DELIVERY-1.
4. **Alert severity** is computed from pattern (`vpalerts.assess`): repeat of the same (subject, kind) in 6 h, family repeat
   (suffixes `-R1/-B5-3` stripped), streak age, and — for IDLE/DRAIN/OWED_RULINGS only — growth of the owed backlog replayed
   from events. Floors: BUDGET/DISK/CONTROL_DOWN/AUTHORITY_MISMATCH… urgent at first sight; IDLE/RELOAD/OWNER_GATE… never
   escalate on their own cadence.
5. **The page re-renders a panel only when its data changed** (`changed()` in the JS) so a refresh never pulls a table out
   from under the reader; alerts/decisions carry server-side ages so they do move.

---

## 4. Known limits / natural next improvements

- Panels are `minmax(420px)` cards; below ~900 px the CircleCI table scrolls horizontally.
- No auth, binds 127.0.0.1 only — keep it that way (the run root has org names and repo paths).
- Gate "since" is a lower bound (shown `≥`) when no snapshot or gates.jsonl row shows the flip.
- `verify` runs only when control.jsonl is read (startup + every 60 s), so its numbers lag the rest by up to a minute.
- `--once` JSON omits `recent` alerts and `findings` to stay short; use `/api/snapshot` for everything.
- The 507 event sequences outside any driver call span are the scheduler's bootstrap + Architect/Operator verbs; `verify` reports them as `info`.

## 5. Rules for whoever edits this

- Log every issue → fix → proof as a row in `<run_root>/DECISIONS.md` **at the moment you find it** (owner's standing rule; a commit message is not the log).
- Test locally with the three test files above; they need no box, no Postgres, no locks.
- `vpalerts.py` is shared with the live driver (it is in the driver's `RELOAD_ORDER`): a threshold change there changes paging after the driver's next reload. Dashboard-only changes belong in `vpdash.py`/`vpjourney.py`.
- Do not write into the run root except your own log/handoff files; do not touch `roster.json`, `orchestration_control.py`, `lanedriver.py`, `laneproof.py`, `vpcircle.py` (Fixer's files — hand patches to the Fixer session).
- Restart the server by killing **its** pid, then re-run the server command; write a DECISIONS row naming the commit you restarted onto.
