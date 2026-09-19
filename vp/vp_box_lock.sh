#!/bin/zsh
# vp_box_lock.sh exclusive|slot|status [--cn CN] [--wait-max-min N] [--proof-id ID] -- <command ...>
# D76b shared box lock for shell scripts (audit runner, WORKER-1): runs <command> holding the lock(s).
# exit 75 = lock not taken within --wait-max-min. Python: dispatcher/vp/vp_box_lock.py (same semantics).
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${VP_PYTHON:-python3}"
exec "$PY" "$HERE/vp_box_lock.py" "$@"
