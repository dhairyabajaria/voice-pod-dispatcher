"""The primary gate reviewer's route, pinned in the INVOCATION and recorded from metadata.

Plan 003 §3/§5/§6.3, and the owner's condition of approval: the gate review must bypass the plugin
wrapper. The reason is the wrapper's own option surface, not a suspicion —

    codex-companion.mjs handleReviewCommand -> valueOptions ["base","scope","model","cwd"]
        `model` is accepted, `effort` IS NOT
    its adversarial branch: runAppServerTurn({prompt, model, sandbox, outputSchema, onProgress})
        no effort, while the sibling task path passes `effort: request.effort`
    lib/codex.mjs: turn/start with `model: options.model ?? null, effort: options.effort ?? null`

— so through the wrapper the model can be pinned and the effort cannot, and recording "Astra route"
while the effort still came from a config file nobody read is a guard excluded from its own proof
set.

Measured, not inferred (2026-09-08): of five sessions under ~/.codex/sessions carrying our
"Merge gate for" prompt, FOUR ran gpt-6-astra/medium and ONE ran gpt-5.6-luna/xhigh. The route was
UNPINNED AND DRIFTING, which is a different claim from "it was always the config default". And only
5 of ~146 gate runs left a rollout at all, because the wrapper's review path is ephemeral: the other
~141 have no session record and their identity is unrecoverable.

Hermetic: pure functions and fixture rollouts. No CLI, no network, no model call.
"""
import importlib.util, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL
spec = importlib.util.spec_from_file_location("mgr", os.path.join(HERE, os.pardir, "mergegate.py"))
M = importlib.util.module_from_spec(spec); sys.modules["mgr"] = M; spec.loader.exec_module(M)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

# ---------------------------------------------------------------- the invocation carries both pins
argv = M.codex_review_argv("plan010/rebuild", "cand0sha1", "focus text", "/tmp/schema.json",
                           "/tmp/last.txt")
check("MUST-BITE  the model is pinned IN THE INVOCATION",
      "-m" in argv and argv[argv.index("-m") + 1] == "gpt-6-astra", argv)
check("MUST-BITE  the EFFORT is pinned in the invocation too — this is the half the plugin wrapper "
      "cannot express, and pinning only the model would record an Astra route with an inherited "
      "effort",
      "-c" in argv and argv[argv.index("-c") + 1] == "model_reasoning_effort=medium", argv)
check("MUST-BITE  it does NOT go through the companion wrapper",
      not any("codex-companion" in str(a) for a in argv), argv)
check("MUST-BITE  --strict-config is passed, so a config key this CLI stops recognising is an "
      "ERROR rather than a silently ignored flag leaving the effort inherited",
      "--strict-config" in argv, argv)
check("MUST-BITE  the base is named in the PROMPT, because the CLI refuses `--base` together with "
      "a prompt (measured: \"the argument '--base <BRANCH>' cannot be used with '[PROMPT]'\") and "
      "its review subcommand ignores --output-schema, returning prose with no Verdict line at all",
      "plan010/rebuild" in argv[-1] and "--base" not in argv, (argv[-1][:120], "--base" in argv))
check("  the adversarial focus survives into that prompt", "focus text" in argv[-1], argv[-1][:160])
check("  the schema and the last-message file are passed, so the answer is structured and lands "
      "somewhere we can read",
      "--output-schema" in argv and "-o" in argv, argv)
check("  and the sandbox is read-only: a reviewer must not be able to change what it reviews",
      "-s" in argv and argv[argv.index("-s") + 1] == "read-only", argv)
check("  and --json, which is where the session id comes from", "--json" in argv, argv)
check("MUST-BITE  --ephemeral is NOT passed: the wrapper's ephemeral review is why ~141 of ~146 "
      "gate reviews have no recoverable identity at all",
      "--ephemeral" not in argv, argv)
check("CONTROL  effort medium is the DECIDED baseline and the constant says so, so a later raise "
      "is a visible edit rather than a drift",
      (M.ASTRA_MODEL, M.ASTRA_EFFORT) == ("gpt-6-astra", "medium"), (M.ASTRA_MODEL, M.ASTRA_EFFORT))

# ---------------------------------------------------------------- session id, never a newest guess
EV = "\n".join([
    "not json at all",
    json.dumps({"type": "thread.started", "payload": {"thread_id": "01a07ad5-6c1a-75b1-8287-306122042633"}}),
    json.dumps({"type": "item.completed", "payload": {"id": "msg_1"}}),
])
check("MUST-BITE  the session id is read from the CLI's own stdout events",
      M.session_id_from_jsonl(EV) == "01a07ad5-6c1a-75b1-8287-306122042633", M.session_id_from_jsonl(EV))
check("  a session_id key is accepted too — the event names belong to the CLI, not to us",
      M.session_id_from_jsonl(json.dumps({"type": "session.created", "session_id": "abcdefgh-1"})) == "abcdefgh-1")
check("MUST-BITE  output with NO id yields None — never a guess. Plan 003 §5 forbids a loose "
      "latest-file search, and an id belonging to someone else's session is worse than none",
      M.session_id_from_jsonl("no events here\n{}\n") is None,
      M.session_id_from_jsonl("no events here\n{}\n"))

# ---------------------------------------------------------------- rollout resolution refuses to guess
root = tempfile.mkdtemp(prefix="rollouts-")
day = os.path.join(root, "2026", "09", "08")
os.makedirs(day)
SID = "01a07ad5-6c1a-75b1-8287-306122042633"
def write_rollout(path, model="gpt-6-astra", effort="medium", provider="openai", sid=SID):
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {
            "session_id": sid, "model_provider": provider, "cli_version": "0.153.4",
            "cwd": "/wt"}}) + "\n")
        fh.write(json.dumps({"type": "response_item", "payload": {"type": "message"}}) + "\n")
        fh.write(json.dumps({"type": "turn_context", "payload": {
            "model": model, "effort": effort, "turn_id": "t1"}}) + "\n")
    return path

p1 = write_rollout(os.path.join(day, f"rollout-2026-09-08T03-00-00-{SID}.jsonl"))
got, why = M.rollout_for_session(SID, root)
check("MUST-BITE  the rollout is found BY SESSION ID", got == p1, (got, why))
missing, why2 = M.rollout_for_session("no-such-session-id", root)
check("MUST-BITE  an id with no rollout is a refusal that NAMES the likely cause, not a fallback "
      "to whatever file is newest",
      missing is None and "unrecoverable" in why2, why2)
write_rollout(os.path.join(day, f"rollout-2026-09-08T04-00-00-{SID}-copy.jsonl"))
dupe, why3 = M.rollout_for_session(SID, root)
check("MUST-BITE  TWO files carrying the id is also a refusal — picking one is how a record "
      "acquires somebody else's identity",
      dupe is None and "refusing to choose" in why3, why3)
none, why4 = M.rollout_for_session(None, root)
check("  and no id at all refuses with its own reason",
      none is None and "NO SESSION ID" in why4, why4)

# ---------------------------------------------------------------- provenance comes from two events
prov = M.provenance_from_rollout(p1)
check("MUST-BITE  model and effort are read from turn_context, provider and session from "
      "session_meta — measured field locations, because session_meta does NOT carry the model",
      prov.get("model") == "gpt-6-astra" and prov.get("effort") == "medium"
      and prov.get("provider") == "openai" and prov.get("session_id") == SID, prov)
partial = os.path.join(day, "rollout-partial.jsonl")
open(partial, "w").write(json.dumps({"type": "session_meta", "payload": {
    "session_id": "x", "model_provider": "openai"}}) + "\n")
pp = M.provenance_from_rollout(partial)
check("MUST-BITE  a missing field stays ABSENT rather than being defaulted — a provenance record "
      "that supplies the value it failed to find is worse than none",
      "model" not in pp and "effort" not in pp, pp)

# ---------------------------------------------------------------- the row: three outcomes
ok, txt = M.route_row(prov, "")
check("MUST-BITE  a matching route is a PASS row naming model, provider, effort and session",
      ok is True and "gpt-6-astra" in txt and "medium" in txt and SID in txt, txt)
# WHAT AN EXEC RUN RECORDS, measured end to end 2026-09-08 on cli 0.153.2: a `turn_context` event
# carrying model and effort, and a `session_meta` event carrying session_id, model_provider,
# cli_version and cwd. Both pins are demonstrably honoured — a probe with `-m gpt-5.6-luna -c
# model_reasoning_effort=medium` recorded luna/MEDIUM while the config default is low.
# `model_context_window` is not a substitute discriminator: astra and luna both report 258400.
# The branch below is the FALLBACK for a CLI that records neither, and it must state the limitation
# rather than print a confident label. (`codex exec review`, the subcommand, records no turn_context
# — which is what made my first reading of this wrong.)
EXEC_PROV = {"session_id": "01a07df4-a826-71f1-840b-e9701dfc031b", "provider": "openai",
             "cli_version": "0.153.2"}
ok2, txt2 = M.route_row(EXEC_PROV, "")
check("MUST-BITE  when the metadata carries NO model or effort, the row says the pins came from the "
      "invocation and were NOT confirmed — claiming metadata confirmation we do not have is the "
      "self-report problem Plan 003 exists to remove",
      ok2 is True and "PINNED IN THE INVOCATION" in txt2 and "NOT confirmed from metadata" in txt2,
      txt2)
check("  ...and it still names the session, provider and cli version it DID measure",
      "01a07df4" in txt2 and "openai" in txt2 and "0.153.2" in txt2, txt2)
drift, dtxt = M.route_row({"model": "gpt-5.6-luna", "effort": "xhigh", "provider": "openai",
                           "session_id": "z"}, "")
check("MUST-BITE  the OTHER pair the rollouts actually show — luna/xhigh — is ROUTE DRIFT and FAILS "
      "loudly; this is the exact review identity Plan 003 exists to eliminate",
      drift is False and "ROUTE DRIFT" in dtxt and "gpt-5.6-luna" in dtxt, dtxt)
nm, ntxt = M.route_row({}, "no rollout file carries session id abc")
check("MUST-BITE  an unreadable identity is NOT MEASURED (INCOMPLETE), never a pass — §6.3 accepts "
      "on identity, so a hoped-for label must never stand in for a measured one",
      nm is None and "NOT MEASURED" in ntxt and "not an Astra review" in ntxt, ntxt)
check("MUST-BITE  ...and it is SELF-DIAGNOSING: it names the three places to look, in order, and "
      "says the block is deliberate. Loud is only useful if the next person gets a thread to pull "
      "rather than a blocked merge with no explanation (BOSS's addition, 2026-09-08)",
      "thread.started" in ntxt and "rollout" in ntxt and "turn_context" in ntxt
      and "ON PURPOSE" in ntxt, ntxt)
noid_txt = M.rollout_for_session(None)[1]
check("MUST-BITE  the no-session-id refusal names the exact stdout event it looked for, so a CLI "
      "rename is a one-line diagnosis instead of an archaeology session",
      "thread.started" in noid_txt and "thread_id" in noid_txt, noid_txt)
# The session id is REQUIRED for any verdict but NOT MEASURED, so every drift fixture carries one.
WRONG_EFFORT = {"model": "gpt-6-astra", "effort": "low", "provider": "openai", "session_id": "s1"}
check("CONTROL  the right model with the WRONG effort is still drift — the effort is the half the "
      "wrapper could not pin, so a check that ignored it would pass the very case this replaces",
      M.route_row(WRONG_EFFORT)[0] is False, M.route_row(WRONG_EFFORT)[1])

# ---------------------------------------------------------------- rendering keeps ONE parser contract
PAYLOAD = json.dumps({
    "verdict": "needs-attention",
    "summary": "Do not merge: admission reclamation can invalidate a live owner.",
    "findings": [{"severity": "high", "title": "Reclamation can delete a newly claimed operation",
                  "body": "Two replays can both observe the same expired admission.",
                  "file": "platform/core/gaps.py", "line_start": 350, "line_end": 352,
                  "confidence": 0.8, "recommendation": "Compare created_at on delete."}],
    "next_steps": ["Add a generation column."]})
md = M.render_review(PAYLOAD)
ok_v, verdict, blockers = M.codex_verdict(md)
check("MUST-BITE  the rendered review is read by the EXISTING verdict parser",
      verdict == "needs-attention" and ok_v is False, (verdict, ok_v))
check("MUST-BITE  ...and its finding counts as a blocker. Feeding the raw JSON instead would keep "
      "the verdict working and silently score every finding ZERO, because `\"severity\": \"high\"` "
      "is not the bracketed marker severity_hits() looks for",
      len(blockers) == 1, (blockers, len(M.codex_verdict(PAYLOAD)[2])))
check("MUST-BITE  CONTROL: the raw JSON really does score zero — otherwise the renderer is "
      "protecting against nothing and this whole step is decoration",
      len(M.codex_verdict(PAYLOAD)[2]) == 0, M.codex_verdict(PAYLOAD)[2])
check("  the rendered shape matches the stored .codex.txt files: a Verdict line and `- [high] …`",
      "Verdict: needs-attention" in md and "- [high]" in md and "gaps.py:350-352" in md, md[:200])
appr = M.render_review(json.dumps({"verdict": "approve", "summary": "ok", "findings": [],
                                   "next_steps": []}))
check("CONTROL  an approve with no findings still parses as a PASS — a renderer that made every "
      "review look bad would pass every check above",
      M.codex_verdict(appr)[0] is True, M.codex_verdict(appr))
check("MUST-BITE  an unparseable answer is passed through VERBATIM, so the fail-closed verdict "
      "check sees what actually came back instead of an empty string that hides it",
      M.render_review("You've hit your usage limit. try again at Sep 9th, 2026 10:40 PM.")
      .startswith("You've hit"), M.render_review("You've hit your usage limit."))
check("MUST-BITE  ...which keeps the WALL detector working on the passed-through text — the wall "
      "arrives as prose, not as schema JSON, and losing it would turn an outage into a FAIL",
      M.codex_failure_kind(1, M.render_review(
          "You've hit your usage limit. Visit x to try again at Sep 9th, 2026 10:40 PM."))[0] == "WALLED",
      M.codex_failure_kind(1, M.render_review("You've hit your usage limit. Visit x."))[0])

# ---------------------------------------------------------------- LIVE ACCEPTANCE, recorded here
# Plan 003 §6.3 accepts on the reviewer recording explicit Astra identity, so the acceptance run is
# named in the proof set rather than living only in a report. One pinned review of a two-commit
# scratch repo (a doubled refund), 2026-09-08 03:54 IST, cli 0.153.2:
#
#   argv    codex exec --strict-config -m gpt-6-astra -c model_reasoning_effort=medium
#           -s read-only --output-schema <wrapper schema> -o <file> --json <prompt naming the base>
#   rc      0 in 21s
#   answer  {"verdict":"needs-attention","findings":[{"severity":"high","title":"Refund amount is
#           doubled",...}]}   -> rendered -> parsed as verdict=needs-attention, 1 blocker
#   route   session=01a07df8-9da0-7ef2-b5b6-55d536c53731 model=gpt-6-astra effort=medium
#           provider=openai cli=0.153.2, all from that session's own metadata
#
# The fixture below is that run's real answer, so the parse chain is pinned to something that
# actually came back from the reviewer rather than to prose I wrote to match my own renderer.
LIVE = ('{"verdict":"needs-attention","summary":"The branch introduces a refund amount error. The '
        'unused tenant_id predates this branch and is excluded from findings.","findings":'
        '[{"severity":"high","title":"Refund amount is doubled","body":"The changed return value '
        'reports twice the requested refund.","file":"pay.py","line_start":3,"line_end":3,'
        '"confidence":0.9,"recommendation":"Return the requested amount."}],'
        '"next_steps":["Restore the original amount."]}')
lok, lverdict, lblockers = M.codex_verdict(M.render_review(LIVE))
check("MUST-BITE  the LIVE acceptance answer parses end to end: verdict read, finding counted, "
      "fail-closed holds",
      lverdict == "needs-attention" and lok is False and len(lblockers) == 1,
      (lverdict, lok, lblockers))

# ---------------------------------------------------------------- the FROZEN candidate half of A2
# Plan 003 §4 A2: "Python, portal and reviewers inspect the pinned candidate/base, not a moving
# worktree." Measured before changing anything: the PYTHON proofs already built a detached checkout
# at the sha (and the comment on that code says why — the daemon dispatches the lane's next item
# into the lane worktree, so it is dirty for the whole of the next build). The REVIEWER and the
# PORTAL proofs were both handed that same moving tree.
check("MUST-BITE  the review prompt names the CANDIDATE SHA, not HEAD — HEAD moves while the gate "
      "runs, and a review of a later commit filed under this sha is evidence about the wrong tree",
      "cand0sha1" in argv[-1] and "HEAD" not in argv[-1], argv[-1][:220])
SRC = open(os.path.join(HERE, os.pardir, "mergegate.py"), errors="ignore").read()
check("MUST-BITE  the reviewer RUNS in a frozen checkout, not the lane worktree — the prompt naming "
      "the sha is not enough on its own, because the tool reads the tree it is standing in",
      "rwt, rwhy = frozen_checkout(wt, sha," in SRC and "sh(argv, cwd=rwt," in SRC
      and "sh(argv, cwd=wt," not in SRC)
check("MUST-BITE  the portal proofs run in one too — this is the row that could measure a later "
      "commit and report it under this candidate's sha",
      "pwt, pwhy = frozen_checkout(wt, sha," in SRC
      and "run_portal_proofs(portal_proofs, pwt," in SRC
      and "run_portal_proofs(portal_proofs, wt," not in SRC)
check("MUST-BITE  neither falls back to the moving tree when the checkout cannot be built: a result "
      "filed under a sha it never ran against looks like evidence and is not",
      "The lane worktree was NOT used as a substitute" in SRC
      and 'codex_box["error"] = f"frozen checkout for the review' in SRC)
check("  and both trees are removed again — a gate that leaks a worktree per run fills the disk "
      "and leaves `git worktree list` unreadable",
      SRC.count("drop_checkout(") >= 3, SRC.count("drop_checkout("))

# CONTROL on the helper itself, against a real repo: it must produce the sha's content, not HEAD's.
import subprocess
repo = tempfile.mkdtemp(prefix="frozen-")
def git(*a, cwd=repo):
    return subprocess.run(("git",) + a, cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
git("init", "-q", "-b", "main")
open(os.path.join(repo, "f"), "w").write("candidate\n")
git("add", "f"); git("commit", "-qm", "candidate")
CAND = git("rev-parse", "HEAD").stdout.strip()
open(os.path.join(repo, "f"), "w").write("LATER WORK, not this candidate\n")
git("add", "f"); git("commit", "-qm", "later")
open(os.path.join(repo, "dirty"), "w").write("uncommitted\n")
M.CN = tempfile.mkdtemp(prefix="frozen-cn-")
path, why = M.frozen_checkout(repo, CAND, "test")
check("MUST-BITE  the frozen checkout holds the CANDIDATE's content, not the lane worktree's later "
      "commit — this is the whole property, measured against a real repo rather than described",
      path and open(os.path.join(path, "f")).read().strip() == "candidate", (path, why))
check("  ...and none of the lane worktree's uncommitted work",
      path and not os.path.exists(os.path.join(path, "dirty")), path)
M.drop_checkout(repo, path)
check("  the checkout is really gone after drop_checkout", not os.path.exists(os.path.join(path, "f")))
bad, badwhy = M.frozen_checkout(repo, "0" * 40, "test")
check("MUST-BITE  an unbuildable checkout returns a REASON and no path — the caller must be forced "
      "to decide, not handed a tree that silently means something else",
      bad is None and "could not build" in badwhy, badwhy)

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
