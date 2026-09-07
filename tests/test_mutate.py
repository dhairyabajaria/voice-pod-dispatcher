"""The mutation runner must be impossible to use wrongly at 3am. These are its refusals.

Three harnesses produced misleading numbers on 2026-09-07 — a SyntaxError scoring as zero reds, an
`except Exception` scoring every crash as a catch, and a guard-removal that threw instead of failing.
mutate.py exists so those cannot be re-introduced by forgetting. This file exists so mutate.py's own
refusals are proved rather than assumed, because a guard nobody tested is a guard nobody has.

Every case runs the REAL script as a subprocess against a real temp module, and the load-bearing
assertions are about what the script REFUSES to print: a crash must not produce a count, and a
missing marker must not produce a count. An absent number is the whole point, so each of those is
paired with a control showing the same command DOES produce a number when the refusal is not due.

Hermetic: a temp dir, no board, no network, no git required.
"""
import importlib.util, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

MUT = os.path.join(HERE, os.pardir, "mutate.py")
spec = importlib.util.spec_from_file_location("mutmod", MUT)
MM = importlib.util.module_from_spec(spec); sys.modules["mutmod"] = MM; spec.loader.exec_module(MM)

ROOT = tempfile.mkdtemp(prefix="mutrunner-")
MARKER = os.path.join(ROOT, "pwned")
SUBJ = os.path.join(ROOT, "subject.py")
open(SUBJ, "w").write(
    "LIMIT = 10\n"
    "def allow(n):\n"
    "    if n > LIMIT:\n"
    "        return False\n"
    "    return True\n"
    "def shape(d):\n"
    "    return d['kind']\n"
    "def unused_path():\n"
    "    return 'never called by any test here'\n")
ORIG = MM.md5(SUBJ)

def t(name, body):
    p = os.path.join(ROOT, name)
    open(p, "w").write("import importlib.util,sys,os\n"
                       "s=importlib.util.spec_from_file_location('subject',%r)\n"
                       "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n" % SUBJ + body)
    return p

# a normal check-and-exit test, the shape every dispatcher test file uses
T_GUARD = t("t_guard.py", "ok = m.allow(11) is False\n"
                          "print(('PASS' if ok else 'FAIL') + '  over-limit is refused')\n"
                          "sys.exit(0 if ok else 1)\n")
# a bare-assert test: it raises AssertionError and PRINTS A TRACEBACK, and it is still a catch
T_ASSERT = t("t_assert.py", "assert m.allow(11) is False\nprint('PASS  asserted')\n")
# a test that trips over a shape it did not expect: KeyError, which is NOT a catch
T_SHAPE = t("t_shape.py", "print('kind is', m.shape({'kind': 'x'}))\n")

def mutate(*args):
    p = subprocess.run([sys.executable, MUT, "--module", SUBJ, "--cwd", ROOT, *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr

GUARD = "    if n > LIMIT:\n        return False\n"

# ---------------------------------------------------------------------------------------------
# RULE 1 — a crash gets a WORD, not a number
rc, out = mutate("--anchor", "LIMIT = 10", "--replacement", 'LIMIT = "', "--test", f"{sys.executable} t_guard.py")
check("MUST-BITE  a mutation that does not COMPILE reports MUTATION-DID-NOT-LOAD",
      "MUTATION-DID-NOT-LOAD" in out and rc == 3, out.strip().splitlines()[-3:])
check("MUST-BITE  and emits NO COUNT AT ALL — this is the exact run that scored 'zero reds' by hand, "
      "and a zero beside a crash is indistinguishable from an assertion nobody wrote",
      not re.search(r"caught=\d", out), out.strip().splitlines()[-3:])

rc, out = mutate("--anchor", "LIMIT = 10", "--replacement", "import nosuchmodule_xyz\nLIMIT = 10",
                 "--test", f"{sys.executable} t_guard.py")
check("MUST-BITE  a mutation that compiles but does not IMPORT is also DID-NOT-LOAD, not a zero",
      "MUTATION-DID-NOT-LOAD" in out and rc == 3 and not re.search(r"caught=\d", out), out.strip().splitlines()[-2:])

# CONTROL for both of the above: the same shape of command DOES print a count when the module loads.
rc, out = mutate("--anchor", GUARD, "--replacement", "    if n > 10**9:\n        return False\n",
                 "--test", f"{sys.executable} t_guard.py")
check("MUST-BITE  CONTROL: a loadable mutation that the tests catch scores caught=1 — so the two "
      "absent counts above are the refusal firing, not the script failing to score anything ever",
      rc == 0 and "caught=1 missed=0 crashed=0" in out, out.strip().splitlines()[-2:])

# ---------------------------------------------------------------------------------------------
# RULE 2 — reds are classified, and a crash is not a catch
rc, out = mutate("--anchor", "return d['kind']", "--replacement", "return d['knid']",
                 "--test", f"{sys.executable} t_shape.py")
check("MUST-BITE  a test that THROWS a non-assertion exception scores crashed, NOT caught — this is "
      "BOSS's `except Exception` over-count, the direction that manufactures confidence",
      "crashed=1" in out and "caught=0" in out, out.strip().splitlines()[-3:])
check("  and the run says in words that a crash scored nothing, so the number is not read alone",
      "neither a catch nor a miss" in out, out.strip().splitlines()[-1:])
check("MUST-BITE  CONTROL: a bare `assert` test — which raises AssertionError and prints a full "
      "traceback — is a CATCH, so classification is by exception TYPE and not by the word Traceback",
      MM.classify(1, "Traceback (most recent call last):\n  ...\nAssertionError\n")[0] == "caught",
      MM.classify(1, "Traceback (most recent call last):\nAssertionError\n"))
rc, out = mutate("--anchor", GUARD, "--replacement", "    if n > 10**9:\n        return False\n",
                 "--test", f"{sys.executable} t_assert.py")
check("MUST-BITE  CONTROL, end to end: the bare-assert test scores caught=1 through the real script",
      "caught=1" in out and "crashed=0" in out, out.strip().splitlines()[-2:])

rc, out = mutate("--anchor", "def unused_path():", "--replacement", "def unused_path_renamed():",
                 "--test", f"{sys.executable} t_guard.py")
check("MUST-BITE  a mutation nothing catches scores missed=1, and missed is its own word — 'not "
      "caught' and 'crashed' must never share a bucket",
      "missed=1" in out and "caught=0" in out and "crashed=0" in out, out.strip().splitlines()[-2:])

# ---------------------------------------------------------------------------------------------
# RULE 3 — a safety claim needs an independent execution signal
rc, out = mutate("--safety", "--anchor", GUARD, "--replacement", "    if n > 10**9:\n        return False\n",
                 "--test", f"{sys.executable} t_guard.py")
check("MUST-BITE  --safety with no --marker REFUSES TO SCORE rather than scoring without one",
      rc == 2 and "REFUSED TO SCORE" in out and not re.search(r"caught=\d", out), out.strip().splitlines()[:2])
check("MUST-BITE  and it refuses BEFORE touching the module — a guard that fires after the edit has "
      "already let the risky thing happen",
      MM.md5(SUBJ) == ORIG, (MM.md5(SUBJ)[:8], ORIG[:8]))

# the marker is written on the path the test ACTUALLY takes. Put it inside the branch the mutation
# disables and it never runs, which is a correct MUTATION-DID-NOT-EXECUTE and a useless control.
wr = "    open(%r, 'w').write('pwned')\n    if n > 10**9:\n        return False\n" % MARKER
rc, out = mutate("--safety", "--marker", MARKER, "--anchor", GUARD, "--replacement", wr,
                 "--test", f"{sys.executable} t_guard.py")
check("MUST-BITE  CONTROL: with a marker the mutation actually writes, the run scores and SAYS the "
      "mutation demonstrably executed",
      rc == 0 and "caught=1" in out and "marker written" in out, out.strip().splitlines()[-2:])

# the mutation is real and compiles, but sits on a path no test reaches
unreached = "def unused_path():\n    open(%r, 'w').write('pwned')\n" % MARKER
if os.path.exists(MARKER): os.unlink(MARKER)
rc, out = mutate("--safety", "--marker", MARKER, "--anchor", "def unused_path():\n",
                 "--replacement", unreached, "--test", f"{sys.executable} t_guard.py")
check("MUST-BITE  a mutation on a path no test REACHES reports MUTATION-DID-NOT-EXECUTE and emits no "
      "count — a failure count cannot tell 'the tests defend this' from 'the line never ran'",
      rc == 3 and "MUTATION-DID-NOT-EXECUTE" in out and not re.search(r"caught=\d", out),
      out.strip().splitlines()[-3:])

# A SAME-LENGTH mutation inside the same second: CPython validates a cached .pyc by source SIZE and
# MTIME-TO-THE-SECOND, so without bytecode invalidation the test subprocess runs the OTHER version of
# the file and the run scores `missed`. That is a mutation that never executed reading as a hole in
# the tests, and it voided four cases of this very file on its first run.
check("MUST-BITE  CONTROL: `LIMIT` -> `10**9` is byte-for-byte the same LENGTH as what it replaces, "
      "and the caught=1 case above used exactly that pair — so a stale .pyc cannot make a mutation "
      "vanish and score as a hole in the tests",
      len("    if n > 10**9:\n        return False\n") == len(GUARD), (len(GUARD),))
# and the invalidation is checked where the cache ACTUALLY lives: Apple's /usr/bin/python3 sets
# sys.pycache_prefix to ~/Library/Caches/com.apple.python, so a scan of __pycache__ beside the module
# finds nothing, removes nothing, and reports success.
subprocess.run([sys.executable, "-c", "import importlib.util as u;"
                f"s=u.spec_from_file_location('subject',{SUBJ!r});"
                "m=u.module_from_spec(s);s.loader.exec_module(m)"], cwd=ROOT, capture_output=True)
cache = importlib.util.cache_from_source(SUBJ)
check("MUST-BITE  CONTROL: importing the subject really does leave a cache file, so the next check "
      "is not asserting the absence of something that never existed",
      os.path.exists(cache), cache)
check("MUST-BITE  invalidate_bytecode removes it — and it is found via cache_from_source, which "
      "honours sys.pycache_prefix, not by scanning the module's own directory",
      MM.invalidate_bytecode(SUBJ) and not os.path.exists(cache), cache)

# ---------------------------------------------------------------------------------------------
# RULE 4 — restore, and verify the restore
check("MUST-BITE  after every case above the module is byte-identical to what we started with",
      MM.md5(SUBJ) == ORIG, (MM.md5(SUBJ)[:8], ORIG[:8]))
check("MUST-BITE  CONTROL: verify_restore can FAIL — handed a hash the file does not have it returns "
      "False, so the green above is a measurement and not a function that always says yes",
      MM.verify_restore(SUBJ, "0" * 32)[0] is False, MM.verify_restore(SUBJ, "0" * 32)[1])
check("  and it NAMES the git leg as unavailable rather than skipping it silently — a leg that "
      "prints nothing when it does not run reads exactly like a leg that ran and passed",
      "NOT AVAILABLE" in MM.verify_restore(SUBJ, ORIG)[1], MM.verify_restore(SUBJ, ORIG)[1])

rc, out = mutate("--anchor", "this string is not in the module", "--replacement", "x",
                 "--test", f"{sys.executable} t_guard.py")
check("MUST-BITE  an anchor that does not match REFUSES — an unmatched anchor runs the UNMUTATED "
      "code, which scores as 'nothing caught it' and reads exactly like a hole in the tests",
      rc == 2 and "anchor not present" in out and not re.search(r"caught=\d", out), out.strip().splitlines()[:2])

# the pre-image must not outlive its run: a stale baseline restored later overwrites newer edits and
# reports success (2026-09-06, mutation-baseline-stale-restore-trap)
leftover = [f for f in os.listdir(tempfile.gettempdir()) if f.startswith("mutate-preimage-")]
check("MUST-BITE  no pre-image file is left behind after the runs above",
      not leftover, leftover[:5])

shutil.rmtree(ROOT, ignore_errors=True)
print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
