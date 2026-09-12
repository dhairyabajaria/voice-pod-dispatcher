# vp — authority store and CLI

`vpstore.py` is the authority store (SQLite, WAL, `busy_timeout=10000`).
`vpctl.py` is the command line over it.  Standard library only, Python 3.12+.

- RUN_ROOT comes from `$VP_RUN_ROOT`, default
  `/Users/dhairyabajaria/Claude Code/Calling New/test-logs/audit/run-2026-09-12-night`
  (or `vpctl --run-root <dir>`).
- Every mutation writes one `event` row, mirrored to `RUN_ROOT/events.jsonl`, and
  to `escalations.jsonl` for the escalation kinds
  (`STUCK STALLED QUOTA AUTH BLOCKED VIOLATION INHIBIT STOP DELIVERY_INCIDENT ROUND_CAP`).
  The DB row is written first, the file line second, both inside the same
  transaction boundary: a file failure raises and the row is rolled back.
- Timestamps are UTC ISO-8601 with milliseconds and a trailing `Z`; every event
  also carries `mono` from `time.monotonic()`.
- Exit codes: `0` ok, `2` usage, `3` refused, `4` conflict.  `--json` on the
  report/audit/render/seal/run-status commands.

Run the tests: `python3 tests/test_vpstore.py`

## Commands

| command | example |
| --- | --- |
| `run init` | `vpctl run init run-2026-09-12-night --trunk-head a7d7915` |
| `run status` | `vpctl run status --json` |
| `run stop` | `vpctl run stop --note "owner full stop"` |
| `run inhibit set` | `vpctl run inhibit set --reason "box memory gate"` |
| `run inhibit clear` | `vpctl run inhibit clear --reason "gate passed"` |
| `run checkpoint` | `vpctl run checkpoint cos-1 "wave 3 assigned"` |
| `role add` | `vpctl role add bld-1 --kind builder --session-ref local_b6e9 --model opencode-go/muse-spark-1.3-contributor --server go1 --group 1` |
| `packet submit` | `vpctl packet submit T7 --benchmark .vp/BENCHMARK.md --packet .vp/PACKET.md --base a7d7915 --critical --allowed-files platform/db.py,platform/tests` |
| `packet ready` | `vpctl packet ready packet-00001` |
| `packet show` | `vpctl packet show T7 --json` (the newest READY packet for the item; refuses 3 when there is none) |
| `packet block` | `vpctl packet block packet-00001 "benchmark has no negative control"` |
| `assign` | `vpctl assign T7 --group 1 --wip-check` |
| `claim` | `vpctl claim T7 --role bld-1 --worktree /…/vp-worktrees/T7 --expected-rev 2` |
| `turn start` | `vpctl turn start T7 --kind build --session ses_01 --server go1 --agent vp-builder --model opencode-go/muse-spark-1.3-contributor --variant xhigh` |
| `turn end` | `vpctl turn end attempt-00007 --status DONE --result turns/T7/attempt-00007/1-builder.json --tokens-in 18000 --tokens-out 2400 --cost 0.31` |
| `submit-result` | `vpctl submit-result T7 --commit 4f1c2ab --result RESULT.json` |
| `findings` | `vpctl findings T7 --path /…/worktrees/T7/FINDINGS.json` |
| `mark-available` | `vpctl mark-available jr-1` |
| `reserve-delivery` | `vpctl reserve-delivery --from sr-1 --to jr-1 --type PACKET --ref T7` |
| `delivery sent\|ack\|uncertain\|fail` | `vpctl delivery ack delivery-00003 --digest 9f2c…` |
| `review record` | `vpctl review record T7 --subject code --base a7d7915 --candidate 4f1c2ab --reviewer sr-2 --reviewer-ref local_11aac7e2 --verdict APPROVED --findings reviews/T7/r2.json` |
| `proof request` | `vpctl proof request 4f1c2ab --base a7d7915 --kind full` |
| `proof record` | `vpctl proof record proof-00002 --status PASS --counts counts.json --artifacts proofs/4f1c2ab` |
| `promote prepare` | `vpctl promote prepare --items T7,T8 --union 9ab33f1 --base a7d7915 --review review-00004 --proof proof-00002` |
| `promote commit` | `vpctl promote commit promo-00001 --expected-ref 9ab33f1 --actual-ref 9ab33f1` |
| `violation record` | `vpctl violation record --kind CHANNEL --from bld-1 --to sr-1 --detail "builder messaged senior directly"` |
| `msg log` | `vpctl msg log --direction send --from cos-1 --to sr-1 --type TASK --ref T7 --body-file /tmp/body.md --session-ref local_d77dfa2f` |
| `escalate` | `vpctl escalate --kind QUOTA --item T7 --attempt attempt-00007 --detail "go1 429 until 12:30 IST"` |
| `report` | `vpctl report pending --json` (also `liveness costs violations latency idle items`; `items` rows carry `packet_path benchmark_path packet_rev critical allowed_files` from the newest READY packet, null when there is none) |
| `audit` | `vpctl audit T7 --json` |
| `render` | `vpctl render` → `LEDGER.md`, `TIMELINE-<item>.md` |
| `seal` | `vpctl seal` → `MANIFEST-<ts>.json` |

## Guards that bite

- `assign` — refuses when the STOP file exists, when the run is `INHIBITED`/`STOPPING`/`STOPPED`,
  or when the group already holds `wip_per_group` unaccepted (`ASSIGNED`) items.
- `claim` — refuses a second writer (`builder_role` already set to another role);
  a stale `--expected-rev` is a conflict (4); STOP/INHIBIT also refuse.
- `review record` — refuses a subject that is not `plan|code`; refuses a candidate
  that is not the item's current `candidate_sha`; refuses a final code review by
  the same `reviewer_ref` that recorded the plan review for that item.
- `promote prepare` — refuses unless review.subject=code ∧ review.candidate=union ∧
  verdict=APPROVED ∧ independent ∧ proof.status=PASS ∧ proof.candidate=union ∧
  run not STOPPING/INHIBITED ∧ no STOP file ∧ every item APPROVED.  A refusal
  records a `promotion` row with `status=REFUSED` and the reason, then exits 3.
- `reserve-delivery` / `msg log` — refuse any pair outside the communication
  matrix and record a `violation` row + `violations.jsonl` line (the row is
  committed, then the refusal is raised).
- `findings` — computes `all_pass` from the lines (a file that claims
  `"all_pass": true` with a FAIL line is still a failure) and, at `round_cap`,
  writes a `ROUND_CAP` event to `escalations.jsonl` and blocks the item.

## Decisions (stricter reading where SPEC.md is ambiguous)

1. **`promote commit --actual-ref`.** The spec has the Integrator do the
   `git update-ref` compare-and-swap and vpctl verify afterwards.  vpctl never
   touches a git repo, so the observed ref is passed in with `--actual-ref`.
   Equal to `--expected-ref` → `COMMITTED`; different → `RECONCILE` with the
   reason recorded.  Omitting it is treated as "the Integrator reports the
   expected ref".
2. **Intermediate item states.** The spec lists no command that sets
   `PREPARING/PREPARED/PROOF_PENDING/FINAL_REVIEW` directly, and inventing one
   was ruled out, so they are driven by the existing commands:
   `review record --subject plan --verdict APPROVED` on a `JUNIOR_SATISFIED`
   item → `PREPARING`; `proof request` on the item's candidate → `PREPARED`;
   `proof record --status RUNNING` → `PROOF_PENDING`; `proof record --status PASS`
   → `FINAL_REVIEW`; `review record --subject code` APPROVED → `APPROVED`,
   FINDINGS → `GRADING`, BLOCKED → `BLOCKED`; `promote commit` → `PROMOTED`.
   A `FAIL_PRODUCT`/`FAIL_INFRA` proof moves no item: the rework must come
   through `findings`, which is the round-counting path.
3. **"unaccepted items" for the WIP cap** is read as items in the group with
   status exactly `ASSIGNED` (assigned but not yet claimed).  The cap is
   `roster.wip_per_group` (default 2), so `≥ 2` is the spec's literal number and
   a roster change moves it.
4. **`round_cap`.** `round ≥ round_cap` writes the `ROUND_CAP` event *and*
   blocks the item (the stricter reading: an escalation that leaves the loop
   running is not a cap).  `ROUND_CAP` is treated as an escalation kind and is
   mirrored to `escalations.jsonl`.
5. **Event idempotency** is keyed on `(item, attempt, kind, ref)` and applied
   only where the spec implies re-application — currently the `FINDINGS` event,
   which a driver may replay after a crash.  Re-applying returns the existing
   `seq` and writes no new DB row and no new jsonl line.  Every other event is
   an unconditional append.
6. **Refusal durability.** A refusal that the spec says must leave a record
   (`violation`, `REFUSED` promotion) commits that record first and raises after
   the transaction closes, otherwise the refusal would roll back its own
   evidence.
7. **Independence** is judged on `reviewer_ref` (the session id), not on
   `reviewer_role`, because the same role id can be re-used across sessions.
   A code review is stored with `independent=1` only after that check passes.
8. **Matrix role names.** `--from`/`--to` accept either a `role_id` from the
   `role` table (resolved to its kind) or a bare kind name.  An unknown name is
   refused, not silently allowed.  `STOP` from `owner` is accepted on any channel.
9. **`msg log` sender check.** When `--session-ref` is given and the role table
   holds a different `session_ref` for that role, the message is refused and a
   `SENDER_ID` violation is recorded.
10. **STOP file location** is `$CN/test-logs/driver/STOP` (`$VP_CN` overrides
    `$CN`); `$VP_STOP_FILE` overrides the whole path and is what the tests use so
    they never read or write the real driver file.
11. **`escalate` and `INCOMPLETE`.** The spec's `escalations.jsonl` subset does
    not list `INCOMPLETE`, but `vpctl escalate` accepts it, so `escalate` forces
    the mirror for the kind it is given instead of widening the global
    escalation-kind set — `INCOMPLETE` still does not become an escalation when
    it arrives as a `turn end` status.
12. **`packet show` / the `report items` join** use the *newest READY* packet for
    the item (`ts`, then `packet_id`).  A `DRAFT` or `BLOCKED` packet joins as
    null and makes `packet show` refuse (3), so a packet that was never readied
    can never be mistaken for the item's contract.
13. **`seal`** hashes every regular file under RUN_ROOT except `state.db-wal`,
    `state.db-shm`, symlinks, and earlier `MANIFEST-*.json` files.
