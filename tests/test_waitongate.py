"""An executor waiting on its own gate is not stalled.

Measured on the live board 2026-09-07 03:45–03:49. EXEC-F reported while a gate for its item was
running; the daemon told it to wait (correct, BOSS's ruling); EXEC-F answered "waiting"; that
one-word turn was classified PROGRESS_STOP and the continue machinery fired AUTO-CONTINUE 1/3, 2/3,
3/3 and then STUCK — three prompts and an escalation BOSS cleared by hand, for an executor doing
exactly what it had been told to do.

The continue machinery exists for a build that stopped mid-step. An item under gate is not one:
there is nothing the executor could be doing until the verdict, whoever launched that gate.

Hermetic: temp CN, no server, no gate, no box — the process probe is stubbed."""
import importlib.util, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); import fixturelog as FL
import fixtures                      # item 9: the shared state redirect

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

root = tempfile.mkdtemp(prefix="waitgate-")
os.makedirs(os.path.join(root, "test-logs", "driver", "gates"))
os.environ["CN"] = root
spec = importlib.util.spec_from_file_location(
    "dwg" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); spec.loader.exec_module(D)
fixtures.redirect_state(D)   # item 9: never the LIVE state dir

def dispatcher(live=frozenset()):
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {"auto": {}}
    d.running_gate_items = lambda: set(live)
    return d

def q(status, who="EXEC-F"):
    return {"items": [{"id": "B.010.ci-parallel-sharding", "status": status, "dispatched_to": who}]}

check("MUST-BITE  an item in `gated` means the executor is waiting on a gate",
      (dispatcher().gated_item_for(q("gated"), "EXEC-F") or {}).get("id") == "B.010.ci-parallel-sharding",
      dispatcher().gated_item_for(q("gated"), "EXEC-F"))
check("MUST-BITE  so does a LIVE mergegate on an item the queue still calls `reported` — a gate BOSS "
      "launched by hand",
      dispatcher({"B.010.ci-parallel-sharding"}).gated_item_for(q("reported"), "EXEC-F") is not None)
check("CONTROL  a `dispatched` item with no gate is NOT waiting — that executor should be continued",
      dispatcher().gated_item_for(q("dispatched"), "EXEC-F") is None)
check("  and another executor's gated item is not this one's",
      dispatcher().gated_item_for(q("gated", who="EXEC-B"), "EXEC-F") is None)
check("  a merged item with no live gate is not a wait", dispatcher().gated_item_for(q("merged"), "EXEC-F") is None)

# the branch itself: PROGRESS_STOP under a gate must not auto-continue
src = open(os.path.join(HERE, os.pardir, "dispatcher.py"), errors="ignore").read()
i = src.find('if kind == "PROGRESS_STOP":')
j = src.find('n = int(self.state["auto"].get(sid, 0))', i)
check("MUST-BITE  the gate check runs BEFORE the counter is read — a check after it would count the "
      "turn it is meant to excuse", i != -1 and j != -1 and "gated_item_for" in src[i:j],
      src[i:j][:120])
check("  and it resets the counter rather than leaving it primed for the next real stall",
      'self.state["auto"][sid] = 0' in src[i:j + 400])

# the wait note must not invite the very reply that caused the cascade
k = src.find("Do NOT resend it")
check("MUST-BITE  the wait note tells the executor not to reply either",
      k != -1 and "do NOT reply" in src[k:k + 200], src[k:k + 160] if k != -1 else k)

shutil.rmtree(root, ignore_errors=True)
print(("FAILED: " + ", ".join(fails)) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
