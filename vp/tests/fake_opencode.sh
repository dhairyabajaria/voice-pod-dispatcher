#!/bin/sh
# tests/fake_opencode.sh -- stand-in for the real `opencode` binary (1.18.27).
# Never talks to a network, a model or a server. Emits the VERIFIED
# `--format json` event shape and writes the worktree payload the driver reads.
#
#   {"type":"step_start|tool_use|text|step_finish","timestamp":<ms>,
#    "sessionID":"ses_…","part":{…}}
#
# Env:
#   VP_FAKE_MODE      ok | junior | nofile | quota | auth | bad | slow | hang
#                     | denied | multistep
#   VP_FAKE_OC_LOG    append each invocation's argv here
#   VP_FAKE_STDIN_LOG append the kind of fd 0 this process was given
#   VP_FAKE_LIVE      dir used to count simultaneous fake processes
set -u

LOG="${VP_FAKE_OC_LOG:-/dev/null}"
echo "$*" >> "$LOG"

# --- stdin discipline -------------------------------------------------------
# The real `opencode run` blocks forever when fd 0 is an open pipe, so the
# driver must hand every child /dev/null. Record what we got and refuse a pipe
# the way the real binary effectively does: by never returning.
if [ -p /dev/stdin ]; then
  STDIN_KIND=pipe
elif [ -t 0 ]; then
  STDIN_KIND=tty
elif [ -c /dev/stdin ]; then
  STDIN_KIND=devnull
else
  STDIN_KIND=other
fi
echo "$STDIN_KIND" >> "${VP_FAKE_STDIN_LOG:-/dev/null}"
if [ "$STDIN_KIND" = "pipe" ]; then
  # Simulate the hang without actually hanging the test suite: exit non-zero
  # with nothing on stdout, which the driver can only read as a dead turn.
  echo '{"type":"fatal","error":"stdin is a pipe: the real opencode would hang here"}'
  exit 70
fi

if [ "${1:-}" = "export" ]; then
  printf '{"session":"%s","messages":[],"fake":true}\n' "${2:-}"
  exit 0
fi

MODE="${VP_FAKE_MODE:-ok}"
DIR=""
SESSION=""
prev=""
for a in "$@"; do
  case "$prev" in
    --dir) DIR="$a" ;;
    --session) SESSION="$a" ;;
  esac
  prev="$a"
done
SID="${SESSION:-ses_fake_zzzz}"
SHA="abcdef1234567890abcdef1234567890abcdef12"
BASE="fedcba0987654321fedcba0987654321fedcba09"
TS=1757640000000

LIVE="${VP_FAKE_LIVE:-}"
if [ -n "$LIVE" ]; then
  mkdir -p "$LIVE"
  : > "$LIVE/$$"
  ls "$LIVE" | wc -l | tr -d ' ' >> "${LIVE}.counts"
fi

ev() {  # ev <type> <part-json>
  printf '{"type":"%s","timestamp":%s,"sessionID":"%s","part":%s}\n' \
    "$1" "$TS" "$SID" "$2"
}

step_start() { ev step_start '{"step":1}'; }

# step_finish with tokens; args: reason input output reasoning cacheread cost
step_finish() {
  ev step_finish "{\"reason\":\"$1\",\"cost\":$6,\"tokens\":{\"total\":$(( $2 + $3 )),\"input\":$2,\"output\":$3,\"reasoning\":$4,\"cache\":{\"write\":10,\"read\":$5}}}"
}

say() { ev text "{\"type\":\"text\",\"text\":\"$1\"}"; }

write_result() {
  mkdir -p "$DIR/.vp"
  cat > "$DIR/.vp/RESULT.json" <<EOF
{
  "item": "ITEM",
  "attempt": 1,
  "commit": "$SHA",
  "base": "$BASE",
  "diff_stat": {"files": 2, "insertions": 30, "deletions": 4},
  "checks": [{"name": "unit", "command": "pytest -q", "exit": 0, "log": "logs/unit.txt"}],
  "disputes": [],
  "blocked": null,
  "notes": "fake builder turn"
}
EOF
}

write_findings() {
  mkdir -p "$DIR/.vp"
  cat > "$DIR/.vp/FINDINGS.json" <<EOF
{
  "item": "ITEM",
  "attempt": 1,
  "commit": "$SHA",
  "lines": [
    {"id": "B1", "kind": "invariant", "verdict": "PASS", "evidence": "src/a.py:12", "note": ""}
  ],
  "all_pass": true
}
EOF
}

case "$MODE" in
  ok)
    step_start; write_result
    say "wrote RESULT.json"
    step_finish stop 1200 333 12 900 0.0031 ;;
  junior)
    step_start; write_findings
    say "wrote FINDINGS.json"
    step_finish stop 1200 333 12 900 0.0031 ;;
  multistep)
    # usage must be SUMMED across every step_finish, not taken from the last
    step_start; say "step one"
    step_finish tool_use 1000 100 5 400 0.0010
    step_start; write_result; say "step two"
    step_finish stop 200 233 7 500 0.0021 ;;
  nofile)
    step_start
    say "step 1 of 9 ..."
    step_finish stop 1200 333 12 900 0.0031 ;;
  denied)
    step_start
    ev tool_use '{"tool":"bash","state":{"status":"error","error":"refused by a rule which prevents writes outside the worktree"}}'
    say "I could not run that"
    step_finish stop 1200 333 12 900 0.0031 ;;
  quota)
    step_start
    ev step_finish '{"reason":"error","error":"HTTP 429 rate limit exceeded for this key"}' ;;
  auth)
    step_start
    ev step_finish '{"reason":"error","error":"401 unauthorized: invalid api key"}' ;;
  bad)
    step_start
    mkdir -p "$DIR/.vp"
    printf '{"item":"ITEM","notes":"missing nearly everything"}\n' > "$DIR/.vp/RESULT.json"
    say "done"
    step_finish stop 10 10 0 0 0.0001 ;;
  slow)
    step_start
    sleep 0.4
    write_result
    step_finish stop 1200 333 12 900 0.0031 ;;
  hang)
    step_start
    sleep "${VP_FAKE_HANG:-1.5}" ;;
  *)
    step_start ;;
esac

[ -n "$LIVE" ] && rm -f "$LIVE/$$"
exit 0
