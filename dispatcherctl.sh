#!/bin/zsh
# dispatcherctl — install | start | stop | restart | status | pause | resume | tail | once | dry | run
# The Dispatcher itself is dispatcher.py (see README.md). This is the owner's switchboard.
set -u
# CN is overridable so this script can be exercised against a temp tree by the tests. Nothing else
# about its behaviour changes; the default is the real checkout.
CN="${CN:-/Users/dhairyabajaria/Claude Code/Calling New}"
LABEL="com.voicepod.dispatcher"
PLIST_SRC="$CN/dispatcher/$LABEL.plist"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE="$CN/test-logs/driver"
UID_=$(id -u)


# --- daemon pid, live children, and staleness -------------------------------------------------
# All three exist because of one 2026-09-07 finding: the running daemon (pid 36697, started 06:18:02)
# was executing code older than dispatcher.py's mtime (06:24:18), so a PROGRESS_STOP fix that was
# written, tested and applied was NOT running — and the only symptom was EXEC-M sitting idle for 27
# minutes holding nothing while an eligible item was queued to it. Staleness is self-concealing: a
# daemon on old code behaves plausibly.
daemon_pid() {
  local p
  p=$(launchctl print "gui/$UID_/$LABEL" 2>/dev/null | awk '/pid = /{print $3}')
  [[ -n $p ]] && { print -- "$p"; return 0 }
  # The nohup fallback has no plist to read. Resolve by argv, then confirm the pid's OWN comm is a
  # python binary — an argv grep alone matches any process whose command line merely QUOTES this
  # path, and our own prompts and reports do (BOSS, 2026-09-07: the same shape of grep counted 6
  # pytests when 1 was running, because the handoff text quotes the command).
  for p in ${(f)"$(ps -eo pid=,args= | grep 'dispatcher/dispatcher\.py' | grep -v grep | awk '{print $1}')"}; do
    local c=$(ps -p $p -o comm= 2>/dev/null)
    case "${c:t}" in python*) print -- "$p"; return 0 ;; esac
  done
  return 1
}

live_children() {
  # Gates and Codex runs. Both are spawned with start_new_session=True and MEASURABLY carry their
  # own pgid (live board: daemon pgid 36697, gate 41487, codex 47713/47865), so a process-group kill
  # does not reach them — but `launchctl bootout` attributes by JOB, and I will not test that by
  # destroying the jobs in question. So: report them and refuse; the operator decides.
  # FAIL-CLOSED (BOSS, 2026-09-07). This guard exists for one purpose — stop a restart taking live
  # children with it — and it reported "no live children" while a Codex sweep and a merge gate were
  # running. "I found nothing" and "there is nothing" were the same output. Now anything this cannot
  # positively classify is REPORTED, which refuses the restart, and the operator decides.
  #
  # THE MEASURED BUG: `${c:t}` on Apple's python is `Python`, capital P, because comm is
  # /Library/.../Python.app/Contents/MacOS/Python — and `case` in zsh is CASE-SENSITIVE, so
  # `python*` never matched and EVERY GATE was invisible to this check. Verified against a live
  # gate (pid 56839, B.010.circleci-three-reds-triage) which this function reported as nothing at
  # all. The old test passed because its fake ps answered `/usr/bin/python3`, lowercase — a fixture
  # using a name the real system does not produce.
  local pid c base
  local PSCMD=${DISPATCHERCTL_PS:-ps}
  local listing
  if ! listing=$($PSCMD -eo pid=,args= 2>/dev/null); then
    print -- "UNKNOWN  --  ps itself failed; this check cannot establish that there are no children"
    return 0
  fi
  for pid in ${(f)"$(print -r -- $listing | grep -E 'dispatcher/mergegate\.py|codex exec' | grep -v grep | awk '{print $1}')"}; do
    # argv can span lines (an executor prompt carries newlines), so a "pid" here may be a word from
    # the prompt body. Digits only.
    [[ $pid == <-> ]] || continue
    c=$($PSCMD -p $pid -o comm= 2>/dev/null)
    if [[ -z $c ]]; then
      # gone between the two stages is fine; still alive with no comm is not something to assume about
      if kill -0 $pid 2>/dev/null; then
        print -- "$pid  ??:??  UNCLASSIFIED — alive, but ps returned no comm for it"
      fi
      continue
    fi
    base=${${c:t}:l}
    case "$base" in
      python*|node|codex)
        print -- "$pid  $($PSCMD -p $pid -o etime= | tr -d ' ')  $($PSCMD -p $pid -o args= | cut -c1-80)" ;;
      zsh|bash|sh|dash|grep|awk|ps|login|sed|tail|head)
        ;;   # a shell or text tool that merely QUOTES the pattern — our own prompts match our own greps
      *)
        print -- "$pid  $($PSCMD -p $pid -o etime= | tr -d ' ')  UNCLASSIFIED comm=$base — not a known child and not a known bystander" ;;
    esac
  done
}

report_staleness() {
  local pid=$1 src="$CN/dispatcher/dispatcher.py"
  [[ -z $pid ]] && return 0
  local started mtime
  started=$(ps -p "$pid" -o lstart= 2>/dev/null) || return 0
  started=$(date -j -f "%a %b %d %T %Y" "$started" +%s 2>/dev/null) || return 0
  mtime=$(stat -f %m "$src")
  if (( mtime > started )); then
    print "STALE: dispatcher.py modified $(date -r $mtime '+%H:%M:%S'), daemon started $(date -r $started '+%H:%M:%S')"
    print "  -> the running daemon is NOT executing the code on disk. Restart when its children are"
    print "     done (dispatcherctl.sh restart refuses while they are live)."
  else
    print "code: daemon started $(date -r $started '+%H:%M:%S'), dispatcher.py mtime $(date -r $mtime '+%H:%M:%S') — current"
  fi
}

case "${1:-status}" in
  install)
    # Owner-run: puts the launchd agent in place so the daemon survives logout/reboot and restarts itself.
    mkdir -p "$HOME/Library/LaunchAgents" && cp "$PLIST_SRC" "$PLIST" && echo "installed $PLIST"
    pkill -f "dispatcher/dispatcher.py" 2>/dev/null && echo "stopped the nohup copy"
    launchctl bootstrap "gui/$UID_" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
    sleep 2; "$0" status ;;
  start)
    if [ -f "$PLIST" ]; then
      launchctl bootstrap "gui/$UID_" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
    else
      echo "launchd agent not installed yet — run: $0 install   (falling back to a nohup copy for now)"
      "$0" run
    fi
    sleep 2; "$0" status ;;
  run)
    # Detached, non-launchd copy (dies at logout; use 'install' for the permanent one).
    if pgrep -f "dispatcher/dispatcher.py" >/dev/null; then echo "already running (pid $(pgrep -f dispatcher/dispatcher.py | head -1))"; exit 0; fi
    nohup /usr/bin/python3 "$CN/dispatcher/dispatcher.py" >> "$STATE/dispatcher.out.log" 2>> "$STATE/dispatcher.err.log" < /dev/null &
    disown 2>/dev/null; sleep 1; echo "running detached, pid $(pgrep -f dispatcher/dispatcher.py | head -1)" ;;
  stop)
    # NEVER `pkill -f "dispatcher/dispatcher.py"`. That is a pattern kill on a shared box — the one
    # thing every executor is told never to do — and it matches ANY process whose command line
    # contains that path, including a shell of BOSS's that merely mentions it. Kill the pid launchd
    # reports (or the argv-resolved, comm-confirmed one for the nohup fallback), and nothing else.
    launchctl bootout "gui/$UID_/$LABEL" 2>/dev/null || launchctl unload -w "$PLIST" 2>/dev/null
    p=$(daemon_pid) || p=""
    if [[ -n $p ]]; then
      kill "$p" 2>/dev/null && echo "stopped pid $p" || echo "pid $p did not accept SIGTERM"
    else
      echo "stopped (no daemon pid found after bootout)"
    fi ;;
  restart)
    # A restart was the same command as "destroy every in-flight gate and audit", which made the
    # safe moment to deploy a fix a matter of luck. BOSS, 2026-09-07: three live children — a merge
    # gate 20 minutes into a box wait, and two Codex audits — were riding on the daemon he needed to
    # restart to activate a fix for an idle executor. He deferred, and spent ten minutes on ps
    # archaeology to decide that. This does the archaeology and refuses by default.
    kids=$(live_children)
    if [[ "${2:-}" == "--dry-run" ]]; then
      # Says what it would do and stops there. The guard's NO-children branch cannot otherwise be
      # exercised without actually stopping the daemon, and a guard whose safe path is untested is
      # half a guard.
      if [[ -n $kids ]]; then
        print "would REFUSE — live children:"; print "$kids" | sed 's/^/  /'; exit 1
      fi
      print "would restart — no live children"; exit 0
    fi
    if [[ -n $kids && "${2:-}" != "--force" ]]; then
      print -u2 "REFUSED: live children are running; a restart may take them with it."
      print -u2 "$kids" | sed 's/^/  /'
      print -u2 "  They are spawned detached (own session and pgid, measured), so a process-group"
      print -u2 "  kill does not reach them — but launchd attributes by JOB and that is untested,"
      print -u2 "  because testing it means destroying the jobs in question."
      print -u2 "  Wait for them, or: $0 restart --force"
      exit 1
    fi
    "$0" stop; sleep 1; "$0" start ;;
  pause)
    touch "$STATE/STOP"
    echo "PAUSED: STOP present — EVERYTHING off, observation included. No polling, no"
    echo "  classification, no auto-continue, no escalations. The board's executor view goes to a"
    echo "  '(not observed)' row: a quiet board while STOP is present means UNMEASURED, not idle."
    echo "  If you want 'no new gates' rather than 'everything off', use 'observe' instead.";;
  resume)
    rm -f "$STATE/STOP"; rm -f "$STATE/OBSERVE"; echo "resumed: both STOP and OBSERVE cleared";;
  observe)
    # The weaker hold. Deliberately a SEPARATE verb rather than a flag on pause: a kill switch that
    # takes arguments is a kill switch somebody gets wrong at hour ten (BOSS, 2026-09-07).
    touch "$STATE/OBSERVE"
    # A SWITCH THE RUNNING DAEMON DOES NOT KNOW ABOUT IS WORSE THAN NO SWITCH: it reads as held
    # while every actuator keeps firing. OBSERVE arrived on 2026-09-07, so a daemon started before
    # dispatcher.py's mtime ignores this file entirely. Say so loudly rather than let the touch
    # imply it worked.
    obs_pid=$(daemon_pid)
    if [[ -n $obs_pid ]]; then
      obs_started=$(ps -p "$obs_pid" -o lstart= 2>/dev/null)
      obs_started=$(date -j -f "%a %b %d %T %Y" "$obs_started" +%s 2>/dev/null || echo 0)
      obs_mtime=$(stat -f %m "$CN/dispatcher/dispatcher.py")
      if (( obs_started > 0 && obs_mtime > obs_started )); then
        print -u2 "WARNING: the RUNNING daemon (pid $obs_pid) started before dispatcher.py was last"
        print -u2 "  modified, so it may predate the OBSERVE switch and IGNORE this file. The hold is"
        print -u2 "  NOT in force until the daemon is restarted onto the code on disk. Check with"
        print -u2 "  'dispatcherctl.sh status' and restart when its children are done."
      fi
    fi
    if [[ -f "$STATE/STOP" ]]; then
      echo "OBSERVE set, but STOP IS ALSO PRESENT AND STOP WINS — the daemon is fully paused."
      echo "  Run 'dispatcherctl resume' then 'dispatcherctl observe' if you meant observe-only."
    else
      echo "OBSERVE-ONLY: sensors run, actuators held. The daemon keeps polling, classifying and"
      echo "  emitting REPORT_READY/QUESTION events and escalations — and posts nothing, dispatches"
      echo "  nothing, auto-continues nothing, launches no gates. Clear with 'unobserve' or 'resume'."
    fi;;
  unobserve)
    rm -f "$STATE/OBSERVE"; echo "observe-only cleared (STOP, if present, is unchanged)";;
  status)
    # WHICH switch is in force, always, and never by omission: an unprinted switch reads exactly
    # like an absent one.
    if [[ -f "$STATE/STOP" ]]; then
      echo "switch: STOP — everything off, observation included (a quiet board here is UNMEASURED)"
    elif [[ -f "$STATE/OBSERVE" ]]; then
      echo "switch: OBSERVE-ONLY — polling and events continue; no posts, dispatch, or gates"
    else
      echo "switch: none — running normally"
    fi
    if launchctl print "gui/$UID_/$LABEL" >/dev/null 2>&1; then
      pid=$(launchctl print "gui/$UID_/$LABEL" | awk '/pid = /{print $3}')
      echo "launchd: loaded, pid=${pid:-?}"
      report_staleness "${pid:-}"
    else
      p=$(pgrep -f "dispatcher/dispatcher.py" | head -1)
      [ -n "$p" ] && echo "process: running detached (nohup), pid=$p — not yet under launchd; run '$0 install'" || echo "process: NOT running"
      [ -n "$p" ] && report_staleness "$p"
    fi
    [ -f "$STATE/STOP" ] && echo "PAUSED (STOP file present)"
    # The STOP file silences the daemon's own poll_lockwatch(); a STANDALONE lockwatch.py --watch
    # keeps running through a pause, and that difference is the whole point of restart Step 1b.
    # Report it from the pidfile so `status` cannot imply lock monitoring that is not there.
    if [ -f "$STATE/lockwatch.pid" ]; then
      lwp=$(cat "$STATE/lockwatch.pid")
      if ps -p "$lwp" -o args= 2>/dev/null | grep -q "lockwatch.py"; then
        echo "lockwatch: standalone ALIVE, pid=$lwp"
      else
        echo "lockwatch: pidfile says $lwp but NO such lockwatch process — box lock is UNWATCHED"
      fi
    else
      echo "lockwatch: no pidfile — no standalone watcher recorded (daemon poll only, and STOP silences that)"
    fi
    if [ -f "$STATE/heartbeat" ]; then
      age=$(( $(date +%s) - $(stat -f %m "$STATE/heartbeat") ))
      echo "heartbeat: $(cat "$STATE/heartbeat") (${age}s ago)"
    else echo "heartbeat: none yet"; fi
    if [ -f "$STATE/pending.json" ]; then
      /usr/bin/python3 - "$STATE/pending.json" <<'EOF'
import json,sys
d=json.load(open(sys.argv[1])); p=d.get("pending",[])
print(f"pending for BOSS: {len(p)}  (updated {d.get('updated')})")
for e in p:
    print(f"  {e['executor']:7} {e['kind']:13} since {e['since_local']}  age {e.get('age_min','?'):>4} min  esc={e['escalated']}  {e['excerpt'][:90]}")
    if e.get("stale"): print(f"          !! {e['stale']}")
for k,v in d.get("executors",{}).items(): print(f"  {k:7} {v}")
EOF
    fi ;;
  clear)
    # clear <row-key> "<reason>" — ask the daemon to drop exactly ONE pending row.
    # It writes a REQUEST, it does not edit state: pending lives in the daemon's memory and is
    # rewritten every tick, so an edit from here would be silently overwritten. The daemon applies
    # it on its next tick and records PENDING_CLEARED with the reason.
    key="${2:-}"; reason="${3:-}"
    if [ -z "$key" ] || [ -z "$reason" ]; then
      echo "usage: $0 clear <row-key> \"<reason>\"   (row-key = the msg id / log filename on the board)"; exit 2
    fi
    mkdir -p "$STATE/clear"
    /usr/bin/python3 - "$STATE/pending.json" "$STATE/clear" "$key" "$reason" <<'EOF'
import json, os, re, sys, time
pending_path, clear_dir, key, reason = sys.argv[1:5]
try:
    rows = json.load(open(pending_path)).get("pending", [])
except Exception as e:
    print(f"cannot read {pending_path}: {e}"); sys.exit(2)
exact = [r for r in rows if r.get("msg_id") == key or r.get("session") == key]
hits = exact or [r for r in rows if key in str(r.get("msg_id", "")) or key in str(r.get("session", ""))]
if not hits:
    print(f"REFUSED: no pending row matches {key!r}. Board has {len(rows)} row(s):")
    for r in rows:
        print(f"  {r['executor']:8} {r['kind']:13} {r['msg_id']}")
    sys.exit(1)
if len(hits) > 1:
    print(f"REFUSED: {key!r} matches {len(hits)} rows — name one exactly:")
    for r in hits:
        print(f"  {r['executor']:8} {r['kind']:13} {r['msg_id']}")
    sys.exit(1)
r = hits[0]
safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(r.get("msg_id") or r["session"]))[:120]
req = {"session": r["session"], "msg_id": r.get("msg_id"), "executor": r.get("executor"),
       "reason": reason, "requested_at": time.strftime("%Y-%m-%d %H:%M:%S")}
path = os.path.join(clear_dir, safe + ".json")
tmp = path + ".tmp"
json.dump(req, open(tmp, "w"), indent=1)
os.replace(tmp, path)
print(f"requested: clear {r['executor']} {r['kind']} row {r.get('msg_id')}")
print(f"  reason:  {reason}")
print(f"  written: {path}")
print("  the daemon applies it on its next tick (<=5s) and logs PENDING_CLEARED; nothing else is touched.")
EOF
    ;;
  precheck)
    # precheck <item-id> — EXECUTOR-invoked. The adversarial reviewer's reading of the draft diff,
    # before REPORT READY. No box, no proofs. See dispatcher/precheck.py for why the gate's own
    # Codex call stays cold, and why a wall drops the pre-check rather than the gate.
    item="${2:-}"
    if [ -z "$item" ]; then
      echo "usage: $0 precheck <item-id>"; exit 2
    fi
    exec /usr/bin/python3 "$CN/dispatcher/precheck.py" "$item" ;;
  checkpoint)
    # checkpoint [since-sha] — the night's state as markdown, on demand. Read-only: it opens the
    # queue, events.log and the trunk git log, writes nothing and touches no board state, so it is
    # safe to run beside a live gate and safe to run twice.
    #
    # Redirect it if you want a file; it goes to stdout so a run never leaves a stale report behind
    # for someone to read as current.
    exec /usr/bin/python3 "$CN/dispatcher/checkpoint.py" "${2:-}" ;;
  answer)
    # answer <item-id> "<text>" [--relayed] — reply to an executor's QUESTION and un-park its item
    # in ONE step, so the two cannot drift apart.
    #
    # A QUESTION parks its item by design: the question waits for BOSS. On 2026-09-07 four questions
    # were answered by direct prompt_async and the parks were never cleared, so four executors built
    # against rows the daemon could not act on — EXEC-J's finished REPORT READY was skipped at 04:57
    # with "holds no dispatched, rework or reported item". The post and the un-park belong together.
    #
    # A REQUEST, like `clear` and `gate`: only the daemon may write a queue row, and only the daemon
    # holds the roster that maps an executor to its session.
    item="${2:-}"; text="${3:-}"; relayed=0
    [ "${4:-}" = "--relayed" ] && relayed=1
    if [ -z "$item" ] || [ -z "$text" ]; then
      echo "usage: $0 answer <item-id> \"<text>\" [--relayed]"
      echo "  --relayed: the executor has no opencode session and you have already told it by hand;"
      echo "             the row is un-parked on your word instead of on a post that never happened."
      exit 2
    fi
    mkdir -p "$STATE/answerreq"
    /usr/bin/python3 - "$STATE/queue.json" "$STATE/answerreq" "$item" "$text" "$relayed" <<'EOF'
import json, os, re, sys, time
queue_path, req_dir, item_id, text, relayed = sys.argv[1:6]
try:
    items = json.load(open(queue_path)).get("items", [])
except Exception as e:
    print(f"cannot read {queue_path}: {e}"); sys.exit(2)
hits = [i for i in items if i.get("id") == item_id]
if not hits:
    near = [i for i in items if item_id in str(i.get("id", ""))]
    print(f"REFUSED: no queue item with id {item_id!r}." +
          ("" if not near else " Did you mean: " + ", ".join(i["id"] for i in near[:5])))
    sys.exit(1)
it = hits[0]
# Read-only advice, re-checked by the daemon against the live queue before anything is posted: this
# file is a snapshot and the row can move in the seconds between.
st = str(it.get("status") or "unknown").lower()
if st != "parked":
    print(f"REFUSED: {item_id} is {st}, not parked. `answer` closes a QUESTION's park; an item that "
          f"is not waiting on one takes the ordinary channel.")
    sys.exit(1)
req = {"item": item_id, "text": text, "relayed_by_hand": bool(int(relayed)),
       "requested_at": time.strftime("%Y-%m-%d %H:%M:%S")}
path = os.path.join(req_dir, re.sub(r"[^A-Za-z0-9._-]", "_", item_id)[:120] + ".json")
tmp = path + ".tmp"
json.dump(req, open(tmp, "w"), indent=1)
os.replace(tmp, path)
print(f"requested: answer {item_id}  (parked at {it.get('parked_at', '?')}, executor "
      f"{it.get('dispatched_to') or '(none recorded)'})")
print(f"  will restore: {it.get('parked_from') or 'dispatched (assumed — no parked_from on the row)'}")
print(f"  written:     {path}")
print("  the daemon posts it on the next tick (<=5s) and logs ANSWERED with the row transition, or")
print("  ANSWER_REFUSED / ANSWER_FAILED with the reason. A failed post leaves the row PARKED.")
EOF
    ;;
  gate)
    # gate <item-id> [--no-box] — hand-launch the merge gate for one item.
    #
    # A REQUEST, like `clear`: the daemon must be the one to launch it, because the daemon is also
    # the process that watches for the gate file, decides, and delivers the rework. A gate started
    # straight from here would run to completion with nobody reading its result.
    #
    # This is the same path the automatic trigger uses (gate_launchable -> mergegate -> decide ->
    # rework), so the lanes the daemon cannot see — WORKER-1/WORKER-2, whose reports reach BOSS as
    # messages, not as executor turns — get identical gate logic instead of a hand-run gate.
    item="${2:-}"; nobox=0
    [ "${3:-}" = "--no-box" ] && nobox=1
    if [ -z "$item" ]; then
      echo "usage: $0 gate <item-id> [--no-box]"; exit 2
    fi
    mkdir -p "$STATE/gatereq"
    # $STATE/queue.json, NOT $CN/dispatcher/queue.json. It read the latter from the day the verb was
    # written; no such file has ever existed, so every `dispatcherctl.sh gate` died at "cannot read
    # ...: [Errno 2]" and exited 2. Nobody noticed because the daemon's automatic path is unaffected
    # and the test built its fixture at the same wrong location — the test pinned the defect
    # (found 2026-09-07 while adding `answer`).
    /usr/bin/python3 - "$STATE/queue.json" "$STATE/gatereq" "$item" "$nobox" <<'EOF'
import json, os, re, sys, time
queue_path, req_dir, item_id, nobox = sys.argv[1:5]
try:
    items = json.load(open(queue_path)).get("items", [])
except Exception as e:
    print(f"cannot read {queue_path}: {e}"); sys.exit(2)
hits = [i for i in items if i.get("id") == item_id]
if not hits:
    near = [i for i in items if item_id in str(i.get("id", ""))]
    print(f"REFUSED: no queue item with id {item_id!r}." +
          ("" if not near else " Did you mean: " + ", ".join(i["id"] for i in near[:5])))
    sys.exit(1)
it = hits[0]
# Read-only advice. The daemon re-checks all of this against the live queue before it launches,
# because this file is a snapshot and the item can move in the seconds in between.
st = str(it.get("status") or "unknown").lower()
if st not in ("dispatched", "rework", "reported"):
    print(f"REFUSED: {item_id} is {st} — a merged or abandoned item has nothing to gate.")
    sys.exit(1)
req = {"item": item_id, "no_box": bool(int(nobox)),
       "requested_at": time.strftime("%Y-%m-%d %H:%M:%S")}
path = os.path.join(req_dir, re.sub(r"[^A-Za-z0-9._-]", "_", item_id)[:120] + ".json")
tmp = path + ".tmp"
json.dump(req, open(tmp, "w"), indent=1)
os.replace(tmp, path)
proofs = it.get("proof_files") or []
box = any(str(p).endswith(".py") for p in proofs)
print(f"requested: gate {item_id}  (status={st}, lane={it.get('lane')}, executor={it.get('dispatched_to')})")
print(f"  box:     {'--no-box (overridden)' if req['no_box'] else ('needed — queues behind any running gate' if box else 'not needed')}")
print(f"  written: {path}")
print("  the daemon launches it on its next tick (<=5s) and logs MANUAL_GATE, or MANUAL_GATE_REFUSED")
print("  with the reason. It collects the result and delivers the rework on the same path as an")
print("  automatic gate — a relayed executor (WORKER-1/WORKER-2/BOSS) gets a pending row, not a post.")
EOF
    ;;
  gates)
    # gates [N] — read-only listing of the newest gate files: item, verdict, age, failed rows, stamp.
    # Replaces `ls -t $STATE/gates`. Reads only; writes nothing; never touches a running gate.
    /usr/bin/python3 - "$STATE/gates" "${2:-15}" <<'PYGATES'
import os, re, sys, time
d = sys.argv[1]
try:
    limit = max(1, int(sys.argv[2]))
except ValueError:
    print("usage: dispatcherctl.sh gates [count]"); sys.exit(2)
try:
    files = [f for f in os.listdir(d) if f.endswith(".md") and not f.endswith(".cites.md")]
except OSError as e:
    print("no gate listing: %s" % e); sys.exit(0)
if not files:
    print("no gate files in %s" % d); sys.exit(0)
files.sort(key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True)
rows = []
for f in files[:limit]:
    p = os.path.join(d, f)
    try:
        lines = open(p, errors="replace").read(20000).splitlines()
    except OSError as e:
        rows.append((f[:-3], "UNREADABLE", os.path.getmtime(p), str(e)[:60], [], "CRASH"))
        continue
    head = lines[0] if lines else ""
    m = re.match(r"#\s*GATE\s+(\S+)\s+[-—]+\s*(.+?)\s*(?:[-—]+\s*(.*))?$", head)
    stamp = ""
    for ln in lines:
        if ln.startswith("gate code "):
            stamp = ln[len("gate code "):].strip()
    fails = [ln for ln in lines if re.match(r"\s*[-*]\s*\*\*.+\*\*\s*[:=].*FAIL", ln)]
    raw_fails = list(fails)
    # Show the row NAME plus a shouted cause when the row has one. "codex adversarial review" alone
    # cannot distinguish a three-day reviewer outage from a real finding, and those need opposite
    # responses (wait vs look) — which is the whole reason the codex row now shouts CODEX WALLED.
    # Only an ALL-CAPS lead is taken: it is the convention the gate uses for "this row did not
    # measure what its name suggests", so a lower-case detail is left out rather than guessed at.
    # ONE WORD saying WHICH KIND of failure, so a listing answers "must I open this?".
    # Measured over the 48 gate files on disk: codex 18, proofs 13, sha-on-lane-head 3, portal 1.
    # 18 of 26 failures are the reviewer row, and while Codex is walled that is near-universal and
    # says nothing about the candidate — so separating a REVIEW failure from a real red is the whole
    # value of the column. WALLED is split out of REVIEW for the same reason: it is not an opinion.
    # Precedence CRASH > PROOFS > SCOPE > REVIEW/WALLED, and a trailing "+" means other kinds failed
    # too — the failed row NAMES are already printed beside it, so the word classifies rather than
    # replaces them.
    def kind_of(fail_lines, verdict):
        if "NOT COMPLETED" in verdict or "UNPARSED" in verdict or "UNREADABLE" in verdict:
            return "CRASH"
        kinds = []
        for ln in fail_lines:
            nm = re.sub(r".*?\*\*(.+?)\*\*.*", r"\1", ln).lower()
            det = ln.split("FAIL", 1)[1].lstrip(" \u2014-:") if "FAIL" in ln else ""
            # A row that did not MEASURE anything is not a red. BOSS, 2026-09-07: guard-residuals r8
            # listed as PROOFS+ while its proofs row said "not run (--no-box)" — he ran the two
            # files by hand, 77 passed, and the real hold was Codex. A gate run box-free would
            # otherwise be listed as a proof failure every time.
            # TWO words, not one, because these are different facts and BOSS's wording would merge
            # them: SKIPPED is a deliberate flag (--no-box / --no-codex) and needs no action;
            # NOT-RUN is the gate TRYING and failing — the proof path raised, no venv, box busy,
            # census non-zero — which is a gate or environment defect somebody has to fix.
            if re.match(r"not run \(--", det):
                kinds.append("SKIPPED")
                continue
            if re.match(r"(NOT RUN|not measured|still running|box busy|census=|cannot create)", det):
                kinds.append("NOT-RUN")
                continue
            if "proof" in nm:
                kinds.append("PROOFS")
            elif "sha" in nm or "scope" in nm or "preflight" in nm or "hand-merge" in nm or "migration" in nm:
                kinds.append("SCOPE")
            elif "codex" in nm or "agy" in nm or "review" in nm:
                kinds.append("WALLED" if "CODEX WALLED" in ln else "REVIEW")
            else:
                kinds.append("OTHER")
        # CRASH > PROOFS > SCOPE > REVIEW/WALLED > NOT-RUN > SKIPPED. A real red outranks everything
        # below it; an unmeasured row never outranks a measured finding, which is BOSS's point.
        for k in ("PROOFS", "SCOPE", "REVIEW", "WALLED", "OTHER", "NOT-RUN", "SKIPPED"):
            if k in kinds:
                return k + ("+" if len(set(kinds)) > 1 else "")
        return ""

    named = []
    for ln in fails:
        nm = re.sub(r".*?\*\*(.+?)\*\*.*", r"\1", ln)
        det = ln.split("FAIL", 1)[1].lstrip(" —-:") if "FAIL" in ln else ""
        # An ALL-CAPS lead is the trigger; what gets SHOWN is the whole clause it opens, up to the
        # em dash the gate uses to separate the cause from its explanation. Matching only the
        # capitals printed "CODEX WALLED" and dropped "until Sep 9th" — the half BOSS needs.
        cause = ""
        if re.match(r"[A-Z][A-Z]+(?: [A-Z]+)*[ :]", det):
            clause = re.split(r" — |\. ", det)[0].strip()
            cause = clause[:56] + ("…" if len(clause) > 56 else "")
        named.append(nm + (" (" + cause + ")" if cause else ""))
    fails = named
    if m:
        rows.append((m.group(1), m.group(2)[:24], os.path.getmtime(p), stamp, fails,
                     kind_of(raw_fails, m.group(2))))
    else:
        # A file we cannot parse is NAMED, not skipped: a silent omission here would read
        # exactly like "no such gate".
        rows.append((f[:-3], "UNPARSED HEADER", os.path.getmtime(p), stamp, [], "CRASH"))
w = max([len(r[0]) for r in rows] + [4])
now = time.time()
print("%s  %-16s  %-7s  %8s  %s" % ("ITEM".ljust(w), "VERDICT", "KIND", "AGE", "FAILED ROWS"))
for item, verdict, mt, stamp, fails, kind in rows:
    mins = (now - mt) / 60.0
    age = ("%.0fm" % mins) if mins < 90 else ("%.1fh" % (mins / 60.0))
    print("%s  %-16s  %-7s  %8s  %s" % (item.ljust(w), verdict, kind or "-", age,
                                       ", ".join(fails) or "-"))
    if stamp:
        print("%s  %s" % (" " * w, stamp[:110]))
print("")
print("%d of %d gate file(s) shown, newest first — advisory listing, no verdict is derived here." % (len(rows), len(files)))
PYGATES
    ;;
  selftest)
    # Every hermetic check for dispatcher/** — no box, no Postgres, no model call, no live state.
    # Rerunnable by anyone, which is the point: a green nobody else can reproduce is a claim.
    exec /bin/zsh "$CN/dispatcher/tests/run_all.sh" ;;
  tail)
    tail -n 20 -F "$STATE/events.log" ;;
  once)
    /usr/bin/python3 "$CN/dispatcher/dispatcher.py" --once ;;
  dry)
    /usr/bin/python3 "$CN/dispatcher/dispatcher.py" --once --dry-run ;;
  *)
    echo "usage: dispatcherctl.sh install|start|stop|restart|status|pause|resume|clear|answer|precheck|gate|gates|checkpoint|selftest|restart [--force|--dry-run]|tail|once|dry|run"; exit 2 ;;
esac
