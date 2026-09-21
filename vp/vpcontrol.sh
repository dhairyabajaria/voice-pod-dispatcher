#!/bin/bash
# vpcontrol.sh — run a control at an exact sha and emit a self-validating transcript.
# Standing default for any fix claiming to resolve a measured regression (§355).
#
# Usage:
#   vpcontrol.sh <repo> <sha> <subdir> <label> <test-selector...>
#     repo    absolute path to the git repo        (e.g. .../voice-pod/chief9-recovery)
#     sha     the commit to measure AT             (a packet's base_sha, or a fix's OUTPUT sha)
#     subdir  working dir inside the worktree      (proof_kind decides this: platform | agent)
#     label   row/packet name, used in the filename
#
# Prints: transcript path, sha256, bytes, collected count, pytest exit code.
# Exit: 0 ran, 2 setup failed, 3 REFUSED by the collection gate.
#
# CITE THE TRANSCRIPT BY ABSOLUTE PATH, NEVER RELATIVE (§355 condition 3).
# `dispatcher` is a repo that no product worktree contains, and controls run with their cwd
# inside a product worktree. A relative spelling resolves from nowhere and degrades a
# citation to an assertion — the reader cannot get to the file it names.
#
# WHY EACH STEP EXISTS. Every one of these has cost a real retry at least once:
#   - detached worktree, never the live tree: proves the code came from <sha>, not trunk
#   - collection gate FIRST: a zero-collection run exits 0 and reads as green
#   - header records HEAD + dirty count: a grader checks the sha without trusting the runner
#   - pytest's own `rootdir:` line is left in the transcript: independent proof of where it ran
#   - hash computed AFTER the file is final: a pin must follow its file, never lead it
#   - no pipe on the pytest call: a pipeline reports the PIPE's exit code, not pytest's
#
# ── THE BUG THIS GATE ALREADY HAD, recorded here on purpose (§355 condition 1) ──
# The first version counted collected tests with `grep -c '::'`. That is wrong, and it is
# wrong in the direction that matters. Given a MISSPELLED selector, pytest emits:
#     ERROR: not found: /path/tests/test_reply_path.py::test_this_name_does_not_exist
# That line contains '::'. So the gate counted the ERROR MESSAGE as a collected test and
# reported `collected: 1` for a test that does not exist — the gate certified a run of
# nothing. A validity condition that an error message can satisfy is not a gate.
# It now reads pytest's OWN "N tests collected" summary and refuses on a non-zero collect rc.
# Do not "simplify" this back to a grep for '::' or for a count of lines.
# Found by running the new script against a case whose answer was already known by hand;
# every previous hand-execution had hidden the miscount. Keep vpcontrol_selftest.sh green.
set -uo pipefail

if [ "$#" -lt 5 ]; then
  sed -n '2,12p' "$0"; exit 2
fi
REPO="$1"; SHA="$2"; SUBDIR="$3"; LABEL="$4"; shift 4
OUT="${VPCONTROL_OUT:-/Users/dhairyabajaria/Claude Code/Calling New/test-logs/audit/run-v13-20260917}"
WT="$(mktemp -d "${TMPDIR:-/tmp}/vpctl-XXXXXX")/wt"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$OUT/CONTROL-$LABEL-${SHA:0:8}-$TS.log"
PY="$REPO/$SUBDIR/.venv/bin/python"
COLLECT="$(mktemp "${TMPDIR:-/tmp}/vpctl-collect-XXXXXX")"

[ -x "$PY" ] || { echo "FATAL: no interpreter at $PY"; exit 2; }
git -C "$REPO" worktree add -q --detach "$WT" "$SHA" || { echo "FATAL: worktree add failed for $SHA"; exit 2; }
cd "$WT/$SUBDIR" 2>/dev/null || { echo "FATAL: subdir '$SUBDIR' missing at $SHA"; \
  git -C "$REPO" worktree remove --force "$WT" 2>/dev/null; exit 2; }

"$PY" -m pytest --collect-only -q -p no:cacheprovider "$@" > "$COLLECT" 2>&1
CRC=$?
NCOL=$(sed -nE 's/^([0-9]+) tests? collected.*/\1/p' "$COLLECT" | tail -1)
[ -z "$NCOL" ] && NCOL=0
if [ "$CRC" -ne 0 ] || [ "$NCOL" -eq 0 ]; then
  echo "REFUSING: collect rc=$CRC, tests collected=$NCOL at $SHA"
  echo "  (0 collected, or a selector matching nothing, would read as green)"
  tail -5 "$COLLECT"; rm -f "$COLLECT"
  git -C "$REPO" worktree remove --force "$WT" 2>/dev/null
  exit 3
fi

{ echo "# CONTROL RUN — $LABEL"
  echo "# run_by: $(whoami)@$(hostname -s) via vp/vpcontrol.sh"
  echo "# run_at_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# repo: $REPO"
  echo "# HEAD: $(git -C "$WT" rev-parse HEAD)"
  echo "# requested_sha: $SHA"
  echo "# worktree: detached, modified files: $(git -C "$WT" status --porcelain | wc -l | tr -d ' ')"
  echo "# collected: $NCOL (gate passed; 0 aborts the run before any transcript exists)"
  echo "# cwd: $SUBDIR"
  echo; } > "$LOG"

"$PY" -m pytest -v -p no:cacheprovider "$@" >> "$LOG" 2>&1
RC=$?
{ echo; echo "# pytest exit code: $RC"; } >> "$LOG"

HASH=$(shasum -a 256 "$LOG" | cut -d' ' -f1)   # AFTER the file is final. Never before.
echo "transcript : $LOG"
echo "sha256     : $HASH"
echo "bytes      : $(wc -c < "$LOG" | tr -d ' ')"
echo "collected  : $NCOL"
echo "pytest_rc  : $RC"
rm -f "$COLLECT"
git -C "$REPO" worktree remove --force "$WT" 2>/dev/null
exit 0
