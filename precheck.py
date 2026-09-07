#!/usr/bin/env python3
"""precheck.py <item-id> — the adversarial reviewer's reading of a DRAFT diff, before REPORT READY.

Owner directive 2026-09-07 03:2x: build INTO auto-gate, not beside it. An executor that only meets
Codex at the gate learns what it got wrong after the gate has been paid for — the box, the proofs and
a full review — and then reworks. This runs the SAME reviewer, the SAME prompt, on the lane diff
against trunk, with no box and no proofs, so the executor can fix findings before reporting.

THE GATE'S CODEX CALL STAYS COLD. That is the whole reason this is a separate process and not an
option on the gate:
  - a fresh companion invocation, so no thread, no context and no conversation is shared;
  - the same prompt text, so the gate's reviewer is not primed by a "you already said X";
  - findings are written to the ITEM FILE and this directory, never into the lane worktree, so
    nothing this writes can reach the gate through the candidate diff.
The one leak this cannot close is an executor pasting pre-check text into a file it then commits —
that is in the standing rules, not in code.

WALL PRECEDENCE: if Codex is walled, the pre-check is dropped FIRST and the gate keeps Codex. So a
recorded wall refuses before any call is made, and a wall discovered during the call is NOT retried
(mergegate retries once for the gate; spending the second call here would be spending the gate's).
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mergegate as MG          # noqa: E402 — codex_env/sh/codex_failure_kind live there, one copy

D = MG.D
ITEMS = os.path.join(D, "items")
PRECHECK = os.path.join(D, "precheck")
TRUNK = "plan010/rebuild"


def rounds(body):
    """Which rework round the item is on: pre-check is allowed once per round."""
    return body.count("## REWORK ")


def already_run(item_id, rnd):
    p = os.path.join(PRECHECK, f"{item_id}.round")
    try:
        return int(open(p).read().strip()) == rnd
    except Exception:
        return False


def mark_run(item_id, rnd):
    os.makedirs(PRECHECK, exist_ok=True)
    with open(os.path.join(PRECHECK, f"{item_id}.round"), "w") as f:
        f.write(str(rnd))


def gate_running_for(item_id):
    try:
        import subprocess
        out = subprocess.run(["ps", "-eo", "args="], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return True                      # cannot tell -> do not run beside a gate we cannot see
    for ln in out.splitlines():
        if "dispatcher/mergegate.py" in ln and item_id in ln and "grep" not in ln and "zsh" not in ln:
            return True
    return False


def block(rnd, when, out):
    return (f"\n\n## PRE-CHECK {rnd} ({when}) — adversarial review of the DRAFT diff\n\n"
            f"Not a gate: no proofs ran and no box was taken. The gate reviews this candidate COLD, "
            f"in a fresh thread that has not seen this text — a finding you disagree with here will "
            f"be raised there again unless you answer it in the code or in your report.\n\n"
            f"```\n{out.strip()}\n```\n")


def main(argv):
    if len(argv) != 1:
        print("usage: precheck.py <item-id>", file=sys.stderr)
        return 2
    item_id = argv[0]
    try:
        q = json.load(open(MG.QUEUE))
    except Exception as e:
        print(f"cannot read the queue: {e}", file=sys.stderr)
        return 2
    it = next((i for i in q.get("items", []) if i.get("id") == item_id), None)
    if not it:
        print(f"REFUSED: no queue item with id {item_id!r}", file=sys.stderr)
        return 1
    wt = it.get("worktree")
    if not wt or not os.path.isdir(wt):
        print(f"REFUSED: {item_id} has no worktree on disk ({wt})", file=sys.stderr)
        return 1
    if gate_running_for(item_id):
        print(f"REFUSED: a merge gate for {item_id} is running — its Codex call is the cold one and "
              f"must not race this. Wait for the gate file.", file=sys.stderr)
        return 1

    fname = it.get("prompt_file") or f"{item_id}.md"
    ipath = os.path.join(ITEMS, fname)
    try:
        body = open(ipath, errors="ignore").read()
    except OSError as e:
        print(f"REFUSED: cannot read the item file {ipath}: {e}", file=sys.stderr)
        return 1
    rnd = rounds(body)
    if already_run(item_id, rnd):
        print(f"REFUSED: pre-check already ran for {item_id} on rework round {rnd}. One per round — "
              f"a second reading of the same diff is the same reading.", file=sys.stderr)
        return 1

    walled = MG.recorded_wall()
    if walled:
        until, who = walled
        print(f"DROPPED: Codex is walled until {until} (recorded by {who}). The pre-check yields to "
              f"the gate, which keeps Codex. Report when ready; the gate will review.", file=sys.stderr)
        return 3

    pre = MG.codex_precheck()
    if pre:
        print(f"DROPPED: {pre[1]}", file=sys.stderr)
        return 3

    argv_codex = [MG.NODE, MG.CODEX, "adversarial-review", "--wait", "--base", TRUNK, "--scope", "branch",
                  f"Merge gate for {item_id} ({it.get('artifact', '')}): find authority gaps, tenancy "
                  f"leaks, money errors, fail-open paths."]
    t0 = time.time()
    rc, out = MG.sh(argv_codex, cwd=wt, timeout=1500, env=MG.codex_env())
    kind = MG.codex_failure_kind(rc, out)[0]
    if kind == "WALLED":
        # NOT retried. mergegate retries once for the gate; a retry here spends the gate's.
        print("DROPPED: Codex answered with its usage wall. The pre-check is dropped first by rule — "
              "the gate keeps Codex. Nothing was written to your item file.", file=sys.stderr)
        MG.emit("PRECHECK_DROPPED", item_id, "codex walled — pre-check yields, gate keeps Codex")
        return 3
    if rc != 0 or kind:
        print(f"FAILED: the reviewer did not answer ({kind or 'rc=%s' % rc}). Nothing was written.",
              file=sys.stderr)
        MG.emit("PRECHECK_FAILED", item_id, f"{kind or 'rc=%s' % rc} — nothing written")
        return 1

    os.makedirs(PRECHECK, exist_ok=True)
    open(os.path.join(PRECHECK, f"{item_id}.txt"), "w").write(out)
    with open(ipath, "a") as f:
        f.write(block(rnd, MG.now(), out))
    mark_run(item_id, rnd)
    MG.emit("PRECHECK", item_id, f"round={rnd} rc=0 {int(time.time() - t0)}s — findings appended to "
                                 f"{fname}; the gate's review stays cold")
    print(f"PRE-CHECK {rnd} written to items/{fname} ({int(time.time() - t0)}s). Fix what it finds, "
          f"then REPORT READY. The gate reviews cold — it has not seen this.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
