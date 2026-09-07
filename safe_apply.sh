#!/bin/zsh
# safe_apply.sh <staged-file> <target-file>
#
# Copy a staged edit over a gate-stamped module ONLY if no merge gate is running, with the check
# and the write in the SAME shell invocation.
#
# 2026-09-05 18:16: I ran the "is a gate running?" check, then made several edits, and the write
# landed minutes later. Gates 47599 (18:15:43) and 47907 (18:16:21) launched inside that gap; both
# executed the 17:40:30 image and both stamped hashes for code they never ran. The check was not
# wrong — it was stale by the time it mattered. Check-then-act with a human-sized gap in between is
# not a check.
#
# This narrows the window from minutes to milliseconds. It does NOT close it: a gate can still
# launch between the `ps` and the `cp`. The real fix is in mergegate.py, which now records each
# module's md5 AT LOAD time, so a mid-run edit can no longer rewrite a running gate's provenance.
# Use both: this stops the edit from racing a gate, and load-time hashing makes the race harmless.
#
# NOTE the process filter: `pgrep -f mergegate.py` MATCHES ITS OWN CALLER when the calling shell's
# command line contains that string (measured 2026-09-05 — a wait loop matched itself and could
# never exit). Filter argv and exclude shells instead.
set -u
if [[ $# -ne 2 ]]; then
  print -u2 "usage: safe_apply.sh <staged-file> <target-file>"
  exit 2
fi
staged=$1; target=$2
[[ -f $staged ]] || { print -u2 "staged file not found: $staged"; exit 2 }
[[ -f $target ]] || { print -u2 "target file not found: $target"; exit 2 }

# The running-gate check applies ONLY to the three modules a gate loads and stamps. Everything else
# — dispatcher.py, README.md, roster.json — cannot affect a gate in flight, and a blanket refusal
# just kept A7 off the daemon while back-to-back gates ran (BOSS, 2026-09-05 19:0x).
# The list is FIXED HERE, not judged at apply time: the blanket rule's real virtue was that nobody
# has to reason correctly under pressure, and that survives as long as the decision is in the script.
# Add a module to this list the moment mergegate.py starts importing it.
STAMPED=(mergegate.py gatereview2.py citesweep.py)
stamped=0
for m in $STAMPED; do [[ ${target:t} == $m ]] && stamped=1; done

running=""
if (( stamped )); then
  running=$(ps -eo pid=,args= | grep "dispatcher/mergegate\.py" | grep -vE "zsh|grep" || true)
fi
if [[ -n $running ]]; then
  print -u2 "ABORTED — ${target:t} is gate-stamped and a merge gate is running; its stamp would name code it never executed:"
  print -u2 "$running"
  exit 1
fi
cp "$staged" "$target" || exit 1
if (( stamped )); then
  print "applied $(basename $target) at $(date '+%H:%M:%S') — no gate was running at the instant of the write"
else
  print "applied $(basename $target) at $(date '+%H:%M:%S') — not a gate-stamped module, so the gate check does not apply"
fi
print "mtime: $(stat -f '%Sm' -t '%F %T' "$target")"
for m in mergegate gatereview2 citesweep; do
  f="$(dirname "$target")/$m.py"
  [[ -f $f ]] && print "  $m $(md5 -q "$f" | cut -c1-8)"
done
