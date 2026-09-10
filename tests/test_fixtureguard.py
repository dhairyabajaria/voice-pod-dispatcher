"""The 36th test file must not be able to skip the state redirect.

BOSS item 9, 2026-09-10 22:32. Item 8's sweep found 12 of 35 test files leaving at least one
DELETABLE live control path unredirected while driving the daemon — `hold/`, `answerreq/`,
`gatereq/`, `clear/`, `holdclear/`, all directories `dispatcher.py` removes files from. The trigger
was concrete: BOSS created the real `hold/CODEX-1.feed` at 21:52:58 and five checks in
`test_serverdown.py` went red, because that file read `HOLD_DIR` live. **A test's verdict depended
on what the operator happened to be doing**, and a different operator action would have had the
suite DELETE his file instead of merely reading it.

Twelve individual fixes is how the thirteenth gets missed — the same "one file instead of its
population" shape as the pgserver census and the route resolver, found the same evening. So the fix
was one shared `fixtures.redirect_state`, and THIS FILE is what makes it a floor rather than a
convention: a new test that loads `dispatcher.py` as a module and forgets the helper fails here.

THE CRITERION IS "LOADS THE MODULE", NOT "MENTIONS THE FILE". `test_restartguard.py` drives
`dispatcherctl.sh` as a subprocess and names dispatcher.py only in prose and argv; it holds no module
object and has nothing to redirect. A guard that fired on the word would have to be suppressed there,
and a guard with an exception list is one edit away from having two.
"""
import ast, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixtures                                                        # noqa: E402

P, F = 0, []
def ok(c, w):
    global P
    if c: P += 1; print(f"PASS {w}")
    else: F.append(w); print(f"FAIL {w}")


def loads_dispatcher(src):
    """Does this file load dispatcher.py AS A MODULE? -> bool.

    Both halves are required: `spec_from_file_location` names the loader, and the literal
    `dispatcher.py` names what is being loaded. Reading the source as text (`open(...).read()` for a
    source-order assertion) is neither.
    """
    return "spec_from_file_location" in src and "dispatcher.py" in src


missing, covered = [], []
for fn in sorted(os.listdir(HERE)):
    if not (fn.startswith("test_") and fn.endswith(".py")):
        continue
    src = open(os.path.join(HERE, fn)).read()
    if not loads_dispatcher(src):
        continue
    (covered if "redirect_state" in src else missing).append(fn)

ok(not missing,
   "MUST BITE: every test file that loads dispatcher.py as a module calls "
   "fixtures.redirect_state(). Without it the file reads — and can DELETE — the operator's live "
   "hold/, answerreq/, gatereq/, clear/ and holdclear/. Missing: " + (", ".join(missing) or "none"))
ok(len(covered) >= 34,
   f"CONTROL: the guard found {len(covered)} covered file(s); a criterion that matched nothing "
   f"would pass the check above for the wrong reason — an empty population is not a clean one")

# ---------------------------------------------------------------- the helper itself must work
import importlib.util, tempfile                                        # noqa: E402
spec = importlib.util.spec_from_file_location(
    "dspguard", os.path.join(HERE, os.pardir, "dispatcher.py"))
DP = importlib.util.module_from_spec(spec); spec.loader.exec_module(DP)
live_before = fixtures.live_paths(DP)
ok(len(live_before) >= 20,
   f"CONTROL: a freshly loaded dispatcher.py really does point at live paths "
   f"({len(live_before)} of them) — if this were 0 the guard below would prove nothing")
root = fixtures.redirect_state(DP)
ok(fixtures.live_paths(DP) == [],
   "MUST BITE: after redirect_state, NO constant points under $HOME any more — asserted over the "
   "whole set, not over the five somebody remembered")
ok(all(getattr(DP, n).startswith(root) for n in ("HOLD_DIR", "ANSWER_REQ_DIR", "GATE_REQ_DIR",
                                                 "CLEAR_DIR", "HOLDCLEAR_DIR", "QUEUE", "STOP")),
   "  including every directory the daemon DELETES files from, and the queue and kill switch")
ok(all(os.path.isdir(getattr(DP, n)) for n in ("HOLD_DIR", "ANSWER_REQ_DIR", "GATE_REQ_DIR")),
   "  and the directories EXIST: a listdir of a missing dir is an OSError that several call sites "
   "swallow, and a swallowed error looks exactly like an empty directory")

# idempotent, because several files load the module more than once
again = fixtures.redirect_state(DP, root)
ok(again == root and fixtures.live_paths(DP) == [],
   "  calling it twice is harmless — three files load the daemon more than once")

# a per-test override AFTER the helper still wins: the helper is a floor, not a ceiling
tmp2 = tempfile.mkdtemp(prefix="override-")
DP.QUEUE = os.path.join(tmp2, "queue.json")
ok(DP.QUEUE.startswith(tmp2),
   "CONTROL: a test's own assignment after the helper still wins — the helper is a floor, which is "
   "why it is safe to insert it immediately after exec_module in every file")

print(f"\n{P} passed, {len(F)} failed")
for x in F: print("  FAILED:", x)
sys.exit(1 if F else 0)
