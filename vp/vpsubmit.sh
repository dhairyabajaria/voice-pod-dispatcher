#!/bin/zsh
# vpsubmit.sh <ITEM> [<ITEM>...] — lint, copy into the run root, submit, mark READY.
# Architect helper (2026-09-13). Reads $VP_RUN_ROOT; packets from test-logs/audit/packets-v12/<ITEM>/.
set -u
CN='/Users/dhairyabajaria/Claude Code/Calling New'
R="${VP_RUN_ROOT:-$CN/test-logs/audit/run-v12-20260913}"
SRC="$CN/test-logs/audit/packets-v12"
TRUNK="$CN/voicepod-plan010-rebuild"
CTL="$CN/dispatcher/vp/vpctl.py"
LINT="$CN/dispatcher/vp/vplint.py"
rc=0
for ITEM in "$@"; do
  P="$SRC/$ITEM/PACKET.md"; B="$SRC/$ITEM/BENCHMARK.md"
  if [ ! -f "$P" ] || [ ! -f "$B" ]; then echo "$ITEM: missing packet or benchmark"; rc=1; continue; fi
  out=$(python3 "$LINT" packet --trunk "$TRUNK" --packets-dir "$SRC" "$P" "$B" 2>&1)
  if echo "$out" | grep -q '^ERROR'; then echo "$ITEM: LINT ERROR"; echo "$out" | grep '^ERROR'; rc=1; continue; fi
  base=$(grep -m1 '^base_sha:' "$P" | awk '{print $2}')
  crit=$(grep -m1 '^critical:' "$P" | awk '{print $2}')
  mkdir -p "$R/packets/$ITEM"
  cp "$P" "$B" "$R/packets/$ITEM/"
  args=(--run-root "$R" packet submit "$ITEM" --packet "$R/packets/$ITEM/PACKET.md" --benchmark "$R/packets/$ITEM/BENCHMARK.md" --base "$base" --submitted-by architect)
  [ "$crit" = "true" ] && args+=(--critical)
  pid=$(python3 "$CTL" "${args[@]}" 2>/dev/null | tail -n 1 | grep -o "packet-[0-9]*")
  if [ -z "$pid" ]; then echo "$ITEM: SUBMIT FAILED"; python3 "$CTL" "${args[@]}" 2>&1 | tail -n 2; rc=1; continue; fi
  python3 "$CTL" --run-root "$R" packet ready "$pid" >/dev/null 2>&1 && echo "$ITEM: READY ($pid, base ${base:0:8}, critical=$crit)" || { echo "$ITEM: READY FAILED for $pid"; rc=1; }
done
exit $rc
