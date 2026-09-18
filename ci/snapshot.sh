#!/bin/sh
# Refresh ci/control/ from the live scheduler checkout and record the shas.
# The driver suite imports the scheduler from ../voice-pod/advisor-plans/outbound-launch
# relative to this repo; in CI that directory is rebuilt from this snapshot.
# Run after every Architect re-pin, then commit the result.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
src=${1:-"$here/../../voice-pod/advisor-plans/outbound-launch"}
dst="$here/control"
mkdir -p "$dst/v13-pack"
for f in orchestration_control.py review_gate.py CATALOG-AUTHORITY.json; do
  cp "$src/$f" "$dst/$f"
done
cp "$src/v13-pack/roster-v13.json" "$dst/v13-pack/roster-v13.json"
python3 "$here/verify_snapshot.py" --record
