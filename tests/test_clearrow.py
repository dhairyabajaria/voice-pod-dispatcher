"""`dispatcherctl.sh clear <key> "<reason>"` and the daemon side that applies it.

BOSS had no sanctioned way to clear a real-but-finished row. Hand-editing was not one: pending
lives in the daemon's memory and is rewritten every tick, so an outside edit is silently overwritten
— which is why ctl writes a REQUEST and the daemon does the removal and logs it. Hermetic: a temp
CN, the REAL dispatcherctl.sh invoked against it, no daemon, no live state."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
import fixtures                      # item 9: the shared state redirect
CTL = os.path.join(HERE, os.pardir, "dispatcherctl.sh")
spec = importlib.util.spec_from_file_location("dclear", os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); sys.modules["dclear"] = D; spec.loader.exec_module(D)
fixtures.redirect_state(D)   # item 9: never the LIVE state dir
PEND = json.load(open(os.path.join(HERE, "fixtures", "pending.json")))

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def sandbox():
    root = tempfile.mkdtemp(prefix="clearrow-")
    st = os.path.join(root, "test-logs", "driver"); os.makedirs(os.path.join(st, "clear"))
    json.dump(PEND, open(os.path.join(st, "pending.json"), "w"), indent=1)
    return root, st

def ctl(root, *args):
    env = dict(os.environ, CN=root)
    p = subprocess.run(["/bin/zsh", CTL, *args], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr

CODEX_KEY = [r for r in PEND["pending"] if r["executor"] == "CODEX-1"][0]["msg_id"]

# 1. the real use: clear the stale CODEX-1 row by the key shown on the board
root, st = sandbox()
rc, out = ctl(root, "clear", CODEX_KEY, "artifact landed 18dea63d; report already read")
reqs = os.listdir(os.path.join(st, "clear"))
check("clear writes exactly one request and reports what it will do", rc == 0 and len(reqs) == 1, out.strip()[:130])
req = json.load(open(os.path.join(st, "clear", reqs[0])))
check("  the request names the session, the msg_id and the reason",
      req["session"] == "codex:CODEX-1" and req["msg_id"] == CODEX_KEY and "18dea63d" in req["reason"])
check("  and pending.json is NOT edited by ctl",
      json.load(open(os.path.join(st, "pending.json"))) == PEND)

# 2. the daemon applies it: one row gone, every other row byte-identical, one event
d = D.Dispatcher.__new__(D.Dispatcher)
# dry=False on purpose: _rm() is a no-op in dry mode, so a dry run could not prove the request is
# consumed — and a request that survives would be re-applied on every tick. CLEAR_DIR points into
# the sandbox, so the only file this can delete is the one this test wrote.
d.dry, d.once = False, True
d.state = {"pending": {r["session"]: dict(r) for r in PEND["pending"]}, "stale_seen": {}}
d.events = []; d.emit = lambda *f: d.events.append(tuple(str(x) for x in f))
d.log = lambda *a: None
D.CLEAR_DIR = os.path.join(st, "clear")
before = {k: json.dumps(v, sort_keys=True) for k, v in d.state["pending"].items()}
d.apply_clear_requests(d.state["pending"])
after = {k: json.dumps(v, sort_keys=True) for k, v in d.state["pending"].items()}
check("the daemon removes exactly the requested row",
      "codex:CODEX-1" not in after and len(after) == len(before) - 1, str(list(after)))
check("  every other row is byte-identical",
      all(after[k] == before[k] for k in after), "a surviving row changed")
check("  one PENDING_CLEARED, carrying the reason",
      [e[0] for e in d.events].count("PENDING_CLEARED") == 1
      and "18dea63d" in d.events[0][5], str(d.events[:1]))
check("  the request file is consumed, so it cannot be applied twice",
      os.listdir(os.path.join(st, "clear")) == [], str(os.listdir(os.path.join(st, "clear"))))
shutil.rmtree(root, ignore_errors=True)

# 3. MUST-REFUSE: an ambiguous key
root, st = sandbox()
rc, out = ctl(root, "clear", "msg_07", "too vague")
check("MUST-REFUSE  an ambiguous key is refused and lists the candidates",
      rc == 1 and "matches 3 rows" in out and not os.listdir(os.path.join(st, "clear")), out.strip()[:120])

# 4. MUST-REFUSE: a key matching nothing
rc, out = ctl(root, "clear", "no-such-row", "x")
check("MUST-REFUSE  a key matching nothing is refused and prints the board",
      rc == 1 and "no pending row matches" in out and not os.listdir(os.path.join(st, "clear")), out.strip()[:100])

# 5. MUST-REFUSE: a missing reason (a clear with no reason is an unexplained deletion)
rc, out = ctl(root, "clear", CODEX_KEY)
check("MUST-REFUSE  a clear with no reason is refused", rc == 2 and "usage:" in out, out.strip()[:90])
shutil.rmtree(root, ignore_errors=True)

# 6. the daemon re-validates: a request whose row has since changed removes NOTHING
root, st = sandbox()
ctl(root, "clear", CODEX_KEY, "r")
d2 = D.Dispatcher.__new__(D.Dispatcher)
d2.dry, d2.once = True, True
d2.state = {"pending": {r["session"]: dict(r) for r in PEND["pending"]}, "stale_seen": {}}
d2.state["pending"]["codex:CODEX-1"]["msg_id"] = "a-newer-report.log"   # the board moved
d2.events = []; d2.emit = lambda *f: d2.events.append(tuple(str(x) for x in f))
d2.log = lambda *a: None
D.CLEAR_DIR = os.path.join(st, "clear")
n = len(d2.state["pending"])
d2.apply_clear_requests(d2.state["pending"])
check("a request whose row has since changed removes nothing and says so",
      len(d2.state["pending"]) == n and [e[0] for e in d2.events] == ["PENDING_CLEAR_REFUSED"],
      str([e[0] for e in d2.events]))
shutil.rmtree(root, ignore_errors=True)

print("\nCLEAR ROW " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
