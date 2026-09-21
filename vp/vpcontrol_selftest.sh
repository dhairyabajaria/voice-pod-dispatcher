#!/bin/bash
# vpcontrol_selftest.sh — prove vp/vpcontrol.sh can REFUSE, not merely pass (§355 condition 2).
#
# This is the test that found the original gate bug (grep -c '::' counting pytest's ERROR
# line as a collected test — see the header of vpcontrol.sh). The success arm passed on the
# first try and reproduced a known-good result exactly, which is why the success arm alone
# is not evidence. An instrument that cannot fail cannot certify anything.
#
# Three arms, and ALL THREE must hold:
#   1. a selector matching nothing        -> REFUSE (exit 3)
#   2. a file absent at that sha          -> REFUSE (exit 3)
#   3. a real, known-good case            -> run    (exit 0, collected > 0)
#
# Skips cleanly when the fixture repo/sha is not on this box, so it stays runnable anywhere.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CTL="$HERE/vpcontrol.sh"
REPO="${VPCONTROL_SELFTEST_REPO:-/Users/dhairyabajaria/Claude Code/Calling New/voice-pod/chief9-recovery}"
SHA="${VPCONTROL_SELFTEST_SHA:-6985a213e76b0755418982f16d4782dc3a145f83}"
REAL="tests/test_reply_path.py::test_ambiguous_failure_requires_reconciliation_before_retry"

if ! git -C "$REPO" cat-file -e "$SHA^{commit}" 2>/dev/null; then
  echo "SKIP: fixture repo/sha not on this box ($REPO @ ${SHA:0:8})"; exit 0
fi
# self-tests must not litter the audit dir with files named like real evidence
OUTDIR="$(mktemp -d "${TMPDIR:-/tmp}/vpctl-selftest-XXXXXX")"
export VPCONTROL_OUT="$OUTDIR"
fail=0

echo "-- arm 1: selector matches nothing (expect REFUSE / rc 3)"
"$CTL" "$REPO" "$SHA" platform SELFTEST-NOMATCH "tests/test_reply_path.py::test_does_not_exist" >/dev/null 2>&1
rc=$?; [ "$rc" -eq 3 ] && echo "   ok (rc=3)" || { echo "   FAIL: rc=$rc, expected 3"; fail=1; }

echo "-- arm 2: file absent at this sha (expect REFUSE / rc 3)"
"$CTL" "$REPO" "$SHA" platform SELFTEST-NOFILE "tests/test_inbound_reply_demotion.py" >/dev/null 2>&1
rc=$?; [ "$rc" -eq 3 ] && echo "   ok (rc=3)" || { echo "   FAIL: rc=$rc, expected 3"; fail=1; }

echo "-- arm 3: real known-good case (expect run / rc 0, collected > 0)"
out="$("$CTL" "$REPO" "$SHA" platform SELFTEST-REAL "$REAL" 2>&1)"
rc=$?
ncol=$(printf '%s\n' "$out" | sed -nE 's/^collected *: *([0-9]+).*/\1/p')
if [ "$rc" -eq 0 ] && [ "${ncol:-0}" -gt 0 ]; then echo "   ok (rc=0, collected=$ncol)"
else echo "   FAIL: rc=$rc collected=${ncol:-none}"; printf '%s\n' "$out" | tail -5; fail=1; fi

rm -rf "$OUTDIR"
[ "$fail" -eq 0 ] && { echo "SELFTEST PASS: refusal arms fire, success arm runs"; exit 0; }
echo "SELFTEST FAIL"; exit 1
