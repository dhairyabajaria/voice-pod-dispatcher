#!/bin/zsh
# watch.sh — live board of every executor session, one screen, no attaching.
#   watch.sh          refresh every 5s
#   watch.sh 10       refresh every 10s
#   watch.sh once     print one frame and exit
set -u
CN="/Users/dhairyabajaria/Claude Code/Calling New"
BASE="http://127.0.0.1:4096"
D="$CN/test-logs/driver"
ARG="${1:-5}"
[ "$ARG" = "once" ] && EVERY=0 || EVERY="$ARG"

frame() {
/usr/bin/python3 - "$BASE" "$D" "$CN" <<'PY'
import json,sys,time,urllib.request,os,datetime,re
BASE,D,CN=sys.argv[1],sys.argv[2],sys.argv[3]
def get(p,t=8):
    try:
        with urllib.request.urlopen(BASE+p,timeout=t) as r: return json.loads(r.read().decode() or "null")
    except Exception: return None
B="\033[1m"; DIM="\033[2m"; R="\033[0m"
GRN="\033[32m"; YEL="\033[33m"; BLU="\033[34m"; RED="\033[31m"; CYN="\033[36m"
roster={}
try: roster=json.load(open(os.path.join(CN,"dispatcher","roster.json"))).get("executors",{})
except Exception: pass
for s in (get("/session?limit=200") or []):
    t=s.get("title") or ""
    if t.startswith("EXEC-") and s["id"] not in roster.values(): roster[t.split()[0]]=s["id"]
print(f"\n{B}EXECUTOR BOARD{R}  {DIM}{datetime.datetime.now():%H:%M:%S}   server {BASE}{R}")
print(DIM+"─"*100+R)
print(f"{DIM}{'WHO':7} {'STATE':9} {'LAST':9} {'KIND':13} WHAT{R}")
for name,sid in sorted(roster.items()):
    msgs=get(f"/session/{sid}/message?limit=1")
    if not msgs: print(f"{name:7} {DIM}unreachable{R}"); continue
    m=msgs[-1]; i=m.get("info",m); t=i.get("time",{}) or {}
    txt=" ".join(p.get("text","") for p in m.get("parts",[]) if p.get("type")=="text").strip()
    tools=[p.get("tool") for p in m.get("parts",[]) if p.get("type")=="tool"]
    when=t.get("completed") or t.get("created") or 0
    ago=int((time.time()*1000-when)/60000)
    clock=datetime.datetime.fromtimestamp(when/1000).strftime("%H:%M:%S") if when else "  --"
    if i.get("role")=="user":      state,col,kind=("queued",CYN,"prompt waiting")
    elif not t.get("completed"):   state,col,kind=("working",GRN,(tools[-1] if tools else "thinking"))
    else:
        state,col=("idle",YEL)
        u=txt.upper()
        kind=("REPORT READY" if "REPORT READY" in u else "QUESTION" if "QUESTION" in u
              else "WAITING" if ("WAITING ON GATE" in u or "REWORK READY" in u) else "stopped")
        if kind in ("QUESTION","WAITING"): col=BLU
        if kind=="stopped": col=RED
    body=re.sub(r"\s+"," ",txt)[:52] if txt else (f"{len(tools)} tool call(s)" if tools else "")
    age=f"{ago}m ago" if state=="idle" else ""
    print(f"{B}{name:7}{R} {col}{state:9}{R} {clock:9} {kind:13} {body} {DIM}{age}{R}")
print(DIM+"─"*100+R)
try:
    p=json.load(open(os.path.join(D,"pending.json")))
    items=p.get("pending",[])
    if items:
        print(f"{B}WAITING ON BOSS{R}")
        for e in items:
            held=f"  {DIM}[held: {(e.get('held') or '')[:44]}]{R}" if e.get("held") else ""
            print(f"  {e['executor']:7} {e['kind']:13} {e.get('age_min','?')}m  {e['excerpt'][:46]}{held}")
    else: print(f"{GRN}nothing waiting on BOSS{R}")
    hb=open(os.path.join(D,"heartbeat")).read().strip()
    hb_age=int(time.time()-os.path.getmtime(os.path.join(D,"heartbeat")))
    warn=f"  {RED}STALE{R}" if hb_age>60 else ""
    stop=f"  {YEL}PAUSED (STOP file){R}" if os.path.exists(os.path.join(D,"STOP")) else ""
    print(f"{DIM}dispatcher heartbeat {hb} ({hb_age}s ago){R}{warn}{stop}")
except Exception as e:
    print(f"{DIM}dispatcher state unreadable: {e}{R}")
PY
}

if [ "$EVERY" = "0" ]; then frame; exit 0; fi
printf '\033[?25l'; trap 'printf "\033[?25h\n"; exit 0' INT TERM
while true; do out=$(frame); clear; print -r -- "$out"; print -r -- "\n\033[2m  ctrl-c to stop · refresh ${EVERY}s · live events: dispatcherctl.sh tail\033[0m"; sleep "$EVERY"; done
