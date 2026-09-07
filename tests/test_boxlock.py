"""The box lock's dead-owner break, and the four conditions that make it safe to automate.

BOSS, 2026-09-07: test-logs/box.lock.d/owner held `EXEC-G-RW5b-... pid=95921` from 05:13 IST and
`ps -p 95921` returned nothing. A gate queued behind it at 06:28 would have waited the full 3600 s
for a corpse — while EMITTING the proof every ten minutes, since GATE_WAITING_FOR_BOX prints the
owner string and that string carries `pid=`.

THE RISK IS THE WHOLE DESIGN. A dead token does not prove an idle box. At that same moment EXEC-G
had a LIVE pytest (pid 45246, permission matrix on lane-014a): the executor believed the box was
still its own from an earlier run and never re-acquired. Breaking on the dead pid alone would have
started a second Postgres cluster on top of a live one — which has happened in this program before
and corrupts both runs while looking green in neither.

So every refusal below is a case that MUST NOT break, and each one is asserted separately: a
conjunction tested only through its true branch is a conjunction with untested clauses.

Hermetic: temp CN, stubbed ps, no box, no Postgres."""
import importlib.util, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location("mgb", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mgb"] = MG; spec.loader.exec_module(MG)

ROOT = tempfile.mkdtemp(prefix="boxlock-")
os.makedirs(os.path.join(ROOT, "test-logs"))
MG.CN = ROOT
MG.LOCK = os.path.join(ROOT, "test-logs", "box.lock.d")
MG.time = type("T", (), {"sleep": staticmethod(lambda _s: None), "time": time.time})()

PS_IDLE = (0, "  501 /usr/bin/python3 dispatcher/mergegate.py X\n  502 /bin/zsh -l\n")
PS_PYTEST = (0, "  501 /Users/x/platform/.venv/bin/python -m pytest tests/test_permission_matrix.py\n")

# The REAL 2026-09-07 board: one live pytest, its wrapper shell, and four Codex processes whose
# PROMPT TEXT quotes `./.venv/bin/python -m pytest` because our own handoff instructions contain it.
# `ps -eo args= | grep -c "[-]m pytest"` returned 6 with ONE pytest running.
PS_MIXED = (0,
    "  45244 /bin/zsh -c CN=...; ./.venv/bin/python -m pytest -q tests/test_permission_matrix.py\n"
    "  45246 ./.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_permission_matrix.py\n"
    "  47713 node /Users/x/codex-companion.mjs adversarial-review Run ./.venv/bin/python -m pytest\n"
    "  47754 /Users/x/vendor/bin/codex exec ... instructions: ./.venv/bin/python -m pytest tests/\n"
    "  47865 node /Users/x/codex-companion.mjs adversarial-review ... -m pytest ...\n"
    "  47870 /Users/x/vendor/bin/codex exec ... -m pytest ...\n")
COMM = {"45244": "/bin/zsh", "45246": "./.venv/bin/python", "47713": "node",
        "47754": "/Users/x/vendor/bin/codex", "47865": "node", "47870": "/Users/x/vendor/bin/codex"}

def ps_router(comm_map=None, ps=PS_MIXED, missing=()):
    """Stub for MG.sh that answers both stages, and RECORDS the argv of stage two."""
    seen = []
    cmap = COMM if comm_map is None else comm_map
    def run(argv, **k):
        seen.append(list(argv))
        if argv[:3] == ["ps", "-eo", "pid=,args="] or argv[1] == "-eo":
            return ps
        pid = argv[argv.index("-p") + 1]
        if pid in missing:
            return 1, ""
        return 0, cmap.get(pid, "") + "\n"
    return run, seen

def hold(owner, age_s):
    if os.path.isdir(MG.LOCK):
        shutil.rmtree(MG.LOCK)
    os.mkdir(MG.LOCK)
    f = os.path.join(MG.LOCK, "owner")
    open(f, "w").write(owner)
    os.utime(f, (time.time() - age_s, time.time() - age_s))

DEAD = "EXEC-G-RW5b-1788738228 2026-09-06T23:43:48Z pid=999999"
ALIVE = f"EXEC-G-RW5b-1788738228 2026-09-06T23:43:48Z pid={os.getpid()}"

# ---------------------------------------------------------------- counting pytest, the two traps
# BOSS hit BOTH one-stage versions on 2026-09-07 and one of them is dangerous rather than useless.
run, seen = ps_router()
MG.sh = run
pids, ev, sure = MG.pytest_pids()
check("MUST-BITE  a Codex process whose PROMPT QUOTES `./.venv/bin/python -m pytest` is NOT counted "
      "as a pytest — our own instruction text is long enough to match our own monitoring greps, and "
      "an argv grep alone returned SIX with ONE pytest running",
      pids == ["45246"], (pids, ev))
check("  the evidence names both numbers, so a reader can see the filter did something",
      "6 argv candidate(s), 1 real" in ev, ev)
check("MUST-BITE  stage two asks for comm as the ONLY field (`ps -p <pid> -o comm=`) — in the "
      "MULTI-COLUMN form macOS truncates comm to 16 chars, `./.venv/bin/python` reads as "
      "`./.venv/bin/pyth`, and a python$ regex then returns 0 while a pytest is alive. That is the "
      "version that breaks a held lock and starts a second cluster",
      all(a[-1] == "comm=" and "-p" in a for a in seen[1:]) and all(len(a) == 5 for a in seen[1:]),
      seen[1:3])
check("MUST-BITE  CONTROL: the truncated form is what would break it — a comm of "
      "'./.venv/bin/pyth' must NOT be counted, so the code cannot be silently switched back",
      MG.pytest_pids.__doc__ and "truncates comm to 16" in MG.pytest_pids.__doc__
      and (lambda: (MG.__dict__.update(sh=ps_router({"45246": "./.venv/bin/pyth"})[0]),
                    MG.pytest_pids()[0] == [])[1])(),
      "a 16-char comm is not a python binary and is not counted")

run, seen = ps_router(missing=("45246",))
MG.sh = run
pids, ev, sure = MG.pytest_pids()
check("  a candidate that EXITED between the two stages is dropped, not counted",
      pids == [] and sure is True, (pids, ev))

run, seen = ps_router({"45246": ""})
MG.sh = run
busy, ev = MG.any_pytest_running()
check("MUST-BITE  a candidate whose comm cannot be resolved makes the answer BUSY — unknown is "
      "never idle when the cost of a wrong idle is two clusters on one data directory",
      busy is True and "BUSY" in ev, (busy, ev))

MG.sh = lambda argv, **k: PS_IDLE

# ---------------------------------------------------------------- the true branch
MG.sh = lambda argv, **k: PS_IDLE
hold(DEAD, 4000)
broke, why = MG.break_dead_box_lock()
check("MUST-BITE  a lock whose owner pid is DEAD, old, with NO pytest anywhere and an unchanged "
      "token, IS broken — otherwise a gate waits an hour for a corpse while printing the proof",
      broke is True and not os.path.isdir(MG.LOCK), (broke, why))
check("  and the reason names the owner, the age and every condition it checked — a silent break is "
      "indistinguishable from a lock that was never taken",
      "pid=999999 DEAD" in why and "0 real" in why and "unchanged" in why and "age=" in why, why)

# ---------------------------------------------------------------- every refusal, one at a time
MG.sh = ps_router({"501": "/Users/x/platform/.venv/bin/python"}, ps=PS_PYTEST)[0]
hold(DEAD, 4000)
broke, why = MG.break_dead_box_lock()
check("MUST-BITE  a DEAD token while a pytest runs is NOT broken — this is the exact 06:2x state "
      "(EXEC-G's token a corpse, its pytest alive) and breaking it starts a second cluster on a "
      "live one",
      broke is False and os.path.isdir(MG.LOCK), (broke, why))
check("  and the refusal logs the INPUTS, not just the verdict — owner, pid, alive/dead, and the "
      "real pytest count, so a wrong call can be read in either direction afterwards",
      "does not prove an idle box" in why and "pid=999999 DEAD" in why and "1 real" in why
      and "NOT BROKEN" in why, why[:220])

MG.sh = lambda argv, **k: (1, "")
hold(DEAD, 4000)
broke, why = MG.break_dead_box_lock()
check("MUST-BITE  an unreadable `ps` counts as BUSY — a false 'busy' costs a wait, a false 'idle' "
      "costs two clusters",
      broke is False and "BUSY" in why, (broke, why))

MG.sh = lambda argv, **k: PS_IDLE
hold(ALIVE, 4000)
broke, why = MG.break_dead_box_lock()
check("MUST-BITE  a LIVE owner is never broken, however old", broke is False and "ALIVE" in why, why)

hold(DEAD, 60)
broke, why = MG.break_dead_box_lock()
check("MUST-BITE  a YOUNG lock is not examined at all — somebody may be mid-setup",
      broke is False and "only 60s old" in why.replace("61s", "60s"), why)

hold("EXEC-G-no-pid-here", 4000)
broke, why = MG.break_dead_box_lock()
check("MUST-BITE  an owner with NO pid is not broken — unknown is not dead",
      broke is False and "unknown is not dead" in why, why)

# the recheck: a holder that re-acquires properly during the gap must be left alone
hold(DEAD, 4000)
# The re-acquire has to land DURING the recheck gap, which is where a real one would: injecting it
# at the wrong point (after the owner is re-read) passed the break and looked like a code bug when
# it was a fixture bug. Stubbing sleep is the faithful simulation of "somebody acted in the gap".
MG.time = type("T", (), {
    "sleep": staticmethod(lambda _s: open(os.path.join(MG.LOCK, "owner"), "w").write(
        "EXEC-G-RW6-NEW pid=%d" % os.getpid())),
    "time": time.time})()
broke, why = MG.break_dead_box_lock()
MG.time = type("T", (), {"sleep": staticmethod(lambda _s: None), "time": time.time})()
check("MUST-BITE  a token that CHANGES across the recheck is left alone — the clause that makes "
      "this safe to automate is that it stands down rather than racing",
      broke is False and "re-acquired" in why and os.path.isdir(MG.LOCK), (broke, why))

MG.sh = lambda argv, **k: PS_IDLE
if os.path.isdir(MG.LOCK):
    shutil.rmtree(MG.LOCK)
broke, why = MG.break_dead_box_lock()
check("  no lock at all is not an error, and nothing is 'broken'", broke is False, (broke, why))

# ---------------------------------------------------------------- the waiting event carries it
hold(DEAD, 4000)
MG.sh = ps_router({"501": "/Users/x/platform/.venv/bin/python"}, ps=PS_PYTEST)[0]
emits = []
MG.emit = lambda kind, item, detail: emits.append((kind, item, detail))
rows = []
it = {"id": "B.010.q", "proof_files": ["tests/test_x.py"]}
MG.BOX_STALE_S = 1800
MG._run_proofs(it, os.path.join(ROOT, "wt"), "abc", "abc", "B.010.q",
               lambda n, ok, d: rows.append((n, ok, d)))
waiting = [d for k, _i, d in emits if k == "GATE_WAITING_FOR_BOX"]
check("MUST-BITE  a gate that DECLINES to break says why in the same line it says it is waiting — "
      "the gate was already printing `pid=` every ten minutes and waiting for it anyway",
      waiting and "not breaking it because" in waiting[0] and "idle box" in waiting[0],
      waiting[:1])
check("CONTROL  and it did not break the lock it declined to break",
      not [k for k, _i, _d in emits if k == "GATE_BROKE_DEAD_BOX_LOCK"],
      [k for k, _i, _d in emits][:4])

# ---------------------------------------------------------------- and the BREAK is announced
hold(DEAD, 4000)
MG.sh = lambda argv, **k: PS_IDLE
emits2 = []
MG.emit = lambda kind, item, detail: emits2.append((kind, item, detail))
rows2 = []
MG._run_proofs({"id": "B.010.q2", "proof_files": ["tests/test_x.py"]},
               os.path.join(ROOT, "wt"), "abc", "abc", "B.010.q2",
               lambda n, ok, d: rows2.append((n, ok, d)))
broke_ev = [d for k, _i, d in emits2 if k == "GATE_BROKE_DEAD_BOX_LOCK"]
check("MUST-BITE  a break inside the wait loop EMITS — a silent lock break is indistinguishable "
      "from a lock that was never taken",
      bool(broke_ev), [k for k, _i, _d in emits2][:4])
check("  and the event carries the owner and the age it broke", broke_ev and "age=" in broke_ev[0],
      broke_ev[:1])

shutil.rmtree(ROOT, ignore_errors=True)
print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
