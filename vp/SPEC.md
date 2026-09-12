# vp — Voice Pod control layer (v9). Contract for vpstore / vpctl / vpdriver / vpcircle

All times: UTC ISO-8601 with milliseconds (`2026-09-12T00:00:00.000Z`) plus `mono` (time.monotonic()) where an interval is measured.
Root: `$CN/test-logs/audit/<run_id>/` (RUN_ROOT). `run_id` from `RUN_ROOT/activation-record.json`.
Nothing under RUN_ROOT is ever deleted or hand-edited. LEDGER.md and TIMELINE-*.md are generated.

## Files
- `state.db` — SQLite, WAL, the only authority. Written only through vpstore (vpctl/vpdriver import it).
- `events.jsonl` — append-only mirror of every store event: `{"ts","mono","seq","run","item","attempt","role","kind","from","to","ref","detail"}`. CoS/Recovery Monitor this.
- `escalations.jsonl` — subset: kinds STUCK, STALLED, QUOTA, AUTH, BLOCKED, VIOLATION, INHIBIT, STOP, DELIVERY_INCIDENT.
- `messages.jsonl` — envelope log (see Envelope).
- `turns/<item>/<attempt>/<n>-<role>.json` and `.export.json` — per model turn (driver writes).
- `reviews/<item>/<review_id>.json`, `proofs/<sha>/…`, `git/…`, `violations.jsonl`, `costs.jsonl`.
- `worktrees/<item>/` under `$CN/vp-worktrees/` (outside RUN_ROOT; git worktrees of the trunk checkout).
- `STOP` file at `$CN/test-logs/driver/STOP` → driver stops spawning, aborts running turns, store refuses claim/promote.

## Store tables (vpstore.py; sqlite3 stdlib; every write inside one transaction; `expected_rev` optimistic check where noted)
- run(run_id PK, started_ts, status: RUNNING|STOPPING|STOPPED|INHIBITED, trunk_head)
- role(role_id PK, kind: architect|boss|cos|recovery|senior|integrator|quickie|builder|junior|infra|daemon, session_ref, model, server, group_no)
- packet(packet_id PK, item, rev, submitted_by, benchmark_path, packet_path, base_sha, critical INT, allowed_files JSON, status: DRAFT|READY|BLOCKED, ts)
- item(item PK, rev, status, group_no, builder_role, junior_role, senior_plan, senior_final, worktree, base_sha, candidate_sha, round INT, wip_slot, ts)
  status machine: READY → ASSIGNED → BUILDING → GRADING → (BUILDING loop) → JUNIOR_SATISFIED → PREPARING → PREPARED → PROOF_PENDING → FINAL_REVIEW → (GRADING on findings) → APPROVED → PROMOTED | PAUSED | BLOCKED | STOPPED
- attempt(attempt_id PK, item, n, kind: build|grade|infra, session_id, server, agent, model, variant, status: RUNNING|DONE|STUCK|STALLED|QUOTA|AUTH|TRANSPORT|INCOMPLETE|ABORTED, started_ts, ended_ts, result_path, tokens_in, tokens_out, tokens_reason, cache_read, cost)
- delivery(delivery_id PK, from_role, to_role, type, ref, status: OUTBOX|RESERVED|SENT|ACK|CONSUMED|UNCERTAIN|FAILED, reserved_ts, sent_ts, ack_ts, digest)
- review(review_id PK, item, subject: plan|code, base_sha, candidate_sha, reviewer_role, reviewer_ref, independent INT, verdict: APPROVED|FINDINGS|BLOCKED, findings JSON, ts)
- proof(proof_id PK, candidate_sha, base_sha, kind: targeted|full, pipeline_id, workflow_id, status: REQUESTED|RUNNING|PASS|FAIL_PRODUCT|FAIL_INFRA|UNKNOWN|CANCELLED, counts JSON, artifacts_path, ts_req, ts_done)
- promotion(promo_id PK, items JSON, union_sha, base_sha, review_id, proof_id, status: INTENT|REFUSED|COMMITTED|RECONCILE, expected_ref, actual_ref, reason, ts)
- violation(id PK, ts, kind, from_role, to_role, payload_sha, detail)
- job(job_id PK, item, kind, ext_id, status, started_ts, last_seen_ts, deadline_ts)
- event(seq PK autoinc, ts, mono, run, item, attempt, role, kind, from_role, to_role, ref, detail JSON)  ← every mutation writes one; mirrored to events.jsonl
- message(id PK, ts, direction: send|recv, from_role, to_role, type, ref, body_sha, session_ref, result)

## Communication matrix (enforced by vpstore.reserve_delivery and vpctl msg log)
allowed (from→to): owner→architect, architect→owner, architect→boss, boss→architect, boss→cos, cos→boss, cos→senior, cos→integrator, cos→quickie, senior→cos, integrator→cos, quickie→cos, senior→junior(store), junior→integrator(store), cos→daemon, daemon→builder, daemon→junior, daemon→infra, builder→daemon, junior→daemon, infra→daemon, daemon→cos, daemon→recovery, recovery→cos, recovery→owner, quickie→integrator.
Anything else → refuse + violation row. STOP from owner accepted from any channel.

## Envelope (first line of every cross-session message)
`VP-ENVELOPE run=<run_id> from=<role>:<session_id> to=<role> type=<TYPE> ref=<store id or -> ts=<UTC ms>`
`vpctl msg log --direction send|recv --from … --to … --type … --ref … --body-file …` validates the pair against the matrix and the sender id against role table; refusal writes a violation and exits 3.

## vpctl commands (argparse; JSON out with --json; exit 0 ok, 2 usage, 3 refused, 4 conflict)
run init|status|stop|inhibit set|inhibit clear|checkpoint <role> <note>
role add <role_id> --kind … [--session-ref … --model … --server … --group N]
packet submit <item> --benchmark <path> --packet <path> --base <sha> [--critical] [--allowed-files a,b] → packet_id (status DRAFT); packet ready <packet_id>; packet block <packet_id> <reason>
assign <item> --group N [--wip-check]  (READY→ASSIGNED; refuses if group has ≥2 unaccepted items or STOP/INHIBIT)
claim <item> --role <builder_role> --worktree <path> --expected-rev N (ASSIGNED→BUILDING; unique writer per item)
turn start <item> --kind build|grade|infra --session … --server … --agent … --model … --variant …  → attempt_id
turn end <attempt_id> --status … --result <path> [--tokens-in … --tokens-out … --tokens-reason … --cache-read … --cost …]
submit-result <item> --commit <sha> --result <path>   (BUILDING→GRADING)
findings <item> --path <FINDINGS.json>   (GRADING→BUILDING if any FAIL, round+1; → JUNIOR_SATISFIED if all PASS; round≥8 → escalation event ROUND_CAP)
mark-available <role_id>
reserve-delivery --from … --to … --type … --ref … → delivery_id; delivery sent|ack|uncertain|fail <delivery_id>
review record <item> --subject plan|code --base … --candidate … --reviewer <role> --reviewer-ref <id> --verdict … --findings <json path>   (checks independence: final code reviewer ≠ plan reviewer of same item; wrong-kind/stale candidate refused)
proof request <candidate_sha> --base … --kind targeted|full [--paths …] → proof_id ; proof record <proof_id> --status … --counts <json> --artifacts <dir>
promote prepare --items a,b --union <sha> --base <sha> --review <review_id> --proof <proof_id> → promo_id (refuse unless review.subject=code ∧ review.candidate=union ∧ verdict=APPROVED ∧ independent ∧ proof.status=PASS ∧ proof.candidate=union ∧ run not STOPPING/INHIBITED ∧ items all APPROVED)
promote commit <promo_id> --expected-ref <sha>  (records INTENT→COMMITTED with actual ref; the git op itself is done by the Integrator with `git update-ref` compare-and-swap; vpctl verifies afterwards)
violation record --kind … --from … --to … --detail …
msg log … (above)
report pending|liveness|costs|violations|latency|idle|items  [--json]
audit <item>   (chronological merge of event, attempt, review, proof, delivery, message rows + file pointers)
render  (LEDGER.md + TIMELINE-<item>.md into RUN_ROOT)
seal  (sha256 manifest of RUN_ROOT files → MANIFEST-<ts>.json)

## Driver (vpdriver.py) responsibilities
Loop every 5 s: read STOP/INHIBIT; for each item needing a turn (ASSIGNED with worktree missing → create worktree from template; BUILDING with no RUNNING attempt → builder turn; GRADING → junior turn), spawn one OpenCode turn on the group's server:
 `opencode run --attach http://127.0.0.1:<port> --dir <worktree> --agent vp-builder|vp-junior --model opencode-go/<model> --variant <v> --format json --title "<item> <kind> r<round>" [--session <sid>] "<prompt>"`  (env: XDG_CONFIG_HOME default; PATH with node22 first)
 Per-server concurrency limit from roster (start 2). Prompt = fixed header + paths of PACKET.md/BENCHMARK.md/FINDINGS.json in the worktree (the files are the payload; the prompt is short and stable for cache).
 On process end: classify (parse JSON events; last assistant text; provider error → STALLED; 429/quota text → QUOTA; 401 → AUTH; no RESULT.json/FINDINGS.json or schema-invalid → INCOMPLETE); write turns/<item>/<attempt>/<n>-<role>.json; `opencode export <sid>` → .export.json (run against the right XDG_DATA_HOME); vpctl turn end; then submit-result or findings.
 Muse progress-line stop (no RESULT.json, no error): resume same session with "continue" ×3 max → STUCK.
 QUOTA on a server: mark server SKIPPED until reset hint; route the group to zen server only if roster.zen_fallback=true and record tier in the attempt.
 Escalations: write escalations.jsonl; macOS notification via osascript for STUCK/QUOTA/AUTH/INHIBIT.
 STOP: POST /session/<sid>/abort for RUNNING attempts; mark ABORTED; exit spawning loop; keep heartbeat.
 Heartbeat file RUN_ROOT/driver.heartbeat every tick.

## Schemas
RESULT.json: {"item","attempt","commit","base","diff_stat":{"files":N,"insertions":N,"deletions":N},"checks":[{"name","command","exit","log"}],"disputes":[{"line","reason"}],"blocked":null|"reason","notes"}
FINDINGS.json: {"item","attempt","commit","lines":[{"id","kind":"invariant|test|negative|forbidden|evidence","verdict":"PASS|FAIL|UNKNOWN","evidence":"file:line or command output ref","note"}],"all_pass":bool}
Both validated with a hand-written checker (no jsonschema dependency).

## Servers / roster (RUN_ROOT/roster.json)
{"servers":{"go1":{"port":4101,"data":"~/.local/share/vp-go1","max_concurrent":2},"go2":{...4102},"go3":{...4103},"zen":{"port":4100,"data":"~/.local/share/vp-zen","max_concurrent":1}},
 "groups":{"1":"go1","2":"go1","3":"go2","4":"go2","5":"go3","6":"go3","infra":"go3"},
 "models":{"builder":{"model":"opencode-go/muse-spark-1.3-contributor","variant":"xhigh","agent":"vp-builder"},"junior":{"model":"opencode-go/deepseek-v4.1-flash","variant":"max","agent":"vp-junior"},"infra":{"model":"opencode-go/deepseek-v4.1-flash","variant":"max","agent":"vp-builder"}},
 "zen_fallback":true,"round_cap":8,"wip_per_group":2,"trunk":"/Users/dhairyabajaria/Claude Code/Calling New/voicepod-plan010-rebuild","worktrees":"/Users/dhairyabajaria/Claude Code/Calling New/vp-worktrees",
 "sessions":{"boss":"local_b7d10efa-cba6-4f8b-b82f-810798f5c3fa","cos":"local_d77dfa2f-85ca-4aa3-9064-1887fe8c2d9b","recovery":"local_cb0036df-a661-461d-8c4e-4b9694c99637"}}

## Worktree template
`git -C <trunk> worktree add <worktrees>/<item> -b vp/<item> <base_sha>`; then symlinks: agent/.venv → <trunk>/agent/.venv, portal/node_modules → <trunk>/portal/node_modules, platform/.venv → <trunk>/platform/.venv (read-only use). Write PACKET.md, BENCHMARK.md, RESULT_SCHEMA.json, FINDINGS_SCHEMA.json into `<worktree>/.vp/`. Node 22 first on PATH for any process in the worktree.

## CircleCI bridge (vpcircle.py)
push: `git push origin vp/<item>:vp/<item>` from the worktree (driver/integrator process, never the model). trigger: `circleci api "api/v2/project/<slug>/pipeline/run" -X POST -d '{"definition_id":"5c8213d0-1d01-416c-bae6-983da31dd8d2","config":{"branch":B},"checkout":{"branch":B},"parameters":{...}}'` (booleans must be JSON). poll: pipeline → workflows → jobs every 60 s; record per-job status/duration; on finish fetch tests for failed jobs via `api/v2/project/<slug>/<job_number>/tests`; write proofs/<sha>/{pipeline.json,jobs.json,tests-failed.json,classified.json}; `vpctl proof record`. slug: circleci/ENeXGDVeXvRGEyPGm5rywd/csivA8jTLotdwZbp17ACv. Account rotation: read `dispatcher/circleaccount.py` for the existing multi-account launcher; if a trigger returns a credit/plan error, switch account and record it.
