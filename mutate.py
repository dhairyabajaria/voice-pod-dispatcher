#!/usr/bin/env python3
"""Run ONE mutation against a module and score it, in a way that cannot be used wrongly in a hurry.

WHY THIS EXISTS. Three times on 2026-09-07 a mutation produced a number that meant something other
than what the number says:

  * WORKER-2, staged mergegate: a mutation put nested same-quote strings inside an f-string. The
    module raised SyntaxError, the test crashed before its first line, and `grep -c "^FAIL"` counted
    nothing. ZERO REDS — indistinguishable from an assertion nobody wrote. An UNDER-count.
  * BOSS, test-logs/review-scripts/codex-replacement-2/mut6b.py:25 — `except Exception:
    fails.append(...)`. Any exception scored as a catch, so a test merely tripping over the mutated
    shape counted as evidence that the tests defend the property. An OVER-count, and the dangerous
    direction: it manufactures confidence. Three reworks were decided with that harness in the room
    (each was corroborated by a marker or a named reproducing case, not by the number alone).
  * WORKER-2, dispatcherctl: a mutation removing the "owner names no pid" guard CRASHED on
    `m.group(1)` instead of failing, scoring zero about the very guard it targeted.

One root: A CRASH IS NEITHER A CATCH NOR A MISS, and every harness above had to be *remembered* into
correctness. BOSS ruled the class on 2026-09-07 (BOSS_DECISIONS), and then ruled that the rule itself
must not live in a habit — "a discipline decays the way a note does". So the four rules are
structural here, not documented here:

  1. The mutated module must LOAD. If it does not, this prints MUTATION-DID-NOT-LOAD and never emits
     a count at all. A crash gets a WORD, not a number.
  2. Reds are CLASSIFIED. Assertion failures are catches; anything else is a crash and is reported
     separately. There is no single total, because "5" is not a result.
  3. --safety demands an independent execution signal (--marker, the `pwned` pattern). Without one it
     REFUSES TO SCORE rather than scoring without it.
  4. The module is restored and THE RESTORE IS VERIFIED, and the suite is re-greened after. A restore
     that silently failed poisons every later measurement in the session; we have had that happen.

WHAT IT DOES NOT DO. It runs one mutation. It does not sweep, rank, or decide. Scoring a mutation is
not the same as knowing the property is defended: a caught mutation says only that the proof set
noticed THIS edit.
"""
import argparse, hashlib, importlib.util, os, py_compile, re, shlex, shutil, subprocess, sys, tempfile

# A test framework's own red is an AssertionError and nothing else. Every OTHER exception type is a
# crash, which is BOSS's rule 1 stated as code: "catch AssertionError specifically and let everything
# else propagate loudly". Matching on the type name rather than on the word "Traceback" is what lets a
# plain `assert x` test — which DOES print a traceback — score as the catch it is.
EXC = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\b", re.M)
HARD_CRASH = ("ERROR collecting", "INTERNALERROR", "Fatal Python error")


def invalidate_bytecode(path):
    """Delete every cached .pyc for this module, wherever this interpreter keeps them. -> files removed.

    MEASURED 2026-09-07, and it silently voided four of this runner's own first-run cases. CPython
    validates a cached .pyc against the source's SIZE and MTIME-TO-THE-SECOND. A mutation like
    `n > LIMIT` -> `n > 10**9` is EXACTLY THE SAME LENGTH, and a mutate-run-restore cycle finishes
    inside one second, so the test subprocess re-used bytecode compiled from the other version of the
    file. Every affected case scored `missed` — a mutation that never executed, reading as "the tests
    do not defend this", which is the same false hole the marker rule exists to prevent.

    It cuts both ways, and the second direction is worse: a .pyc compiled from the MUTATED source can
    survive the restore, so the code under test stays mutated with correct bytes on disk. Hence this
    runs after the mutation AND after the restore.

    TWO FACTS WITH DIFFERENT SCOPES, and conflating them leaves half the fleet believing it is safe:

      * THE STALENESS IS UNIVERSAL. size + mtime-to-the-second is CPython's default validation, on
        every interpreter. BOSS reproduced it on platform/.venv/bin/python on 2026-09-07: a function
        returning 111, edited to 222 (same length) and re-imported in a FRESH process, still returned
        111. Our mutations look exactly like that — `>` to `<`, one digit, `10**9` to `10**8`.
      * THE CACHE LOCATION IS NOT UNIVERSAL. platform/.venv/bin/python has pycache_prefix = None, so
        its caches sit in `__pycache__` beside the module and deleting that directory genuinely
        works. Apple's /usr/bin/python3 — the interpreter that runs dispatcher/ — sets it to
        ~/Library/Caches/com.apple.python, so a scan beside the module finds nothing, removes
        nothing, and reports success. That is what it did to me here.

    Hence `cache_from_source`, which honours the prefix, and never a hand-built `__pycache__/x.pyc`.

    AND NOTE WHAT ACTUALLY CATCHES THIS: rule 3. A marker proves the mutated line ran without knowing
    anything about .pyc files at all. The four cases this voided would each have been caught by
    --safety on its own. This function is the specific fix; the marker is the general one.
    """
    removed = []
    cands = [importlib.util.cache_from_source(path)]
    for opt in ("1", "2"):
        try:
            cands.append(importlib.util.cache_from_source(path, optimization=opt))
        except Exception:  # noqa: BLE001 — an interpreter that refuses the variant simply has none
            pass
    d, base = os.path.dirname(path), os.path.splitext(os.path.basename(path))[0]
    pc = os.path.join(d, "__pycache__")
    if os.path.isdir(pc):
        cands += [os.path.join(pc, f) for f in os.listdir(pc) if f.split(".")[0] == base]
    cands.append(path + "c")
    for c in cands:
        if os.path.exists(c):
            os.unlink(c); removed.append(c)
    return removed


def md5(path):
    with open(path, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def git_root_relative(path):
    """(repo_root, path-relative-to-root) or (None, None) when the file is not in a git repo.

    `git show HEAD:<p>` takes p relative to the REPOSITORY ROOT and `-C subdir` does NOT change that
    (2026-09-06). Building the leg on a cwd-relative path errors, and the error reads exactly like a
    failed restore.
    """
    d = os.path.dirname(os.path.abspath(path))
    r = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode != 0:
        return None, None
    root = r.stdout.strip()
    rel = os.path.relpath(os.path.abspath(path), root)
    tracked = subprocess.run(["git", "-C", root, "ls-files", "--error-unmatch", rel],
                             capture_output=True, text=True).returncode == 0
    return (root, rel) if tracked else (None, None)


def verify_restore(path, expected_md5):
    """Is `path` byte-identical to the pre-image we took before mutating? -> (ok, how_it_was_checked)

    Two legs, and the second is REPORTED AS UNAVAILABLE rather than skipped: a leg that prints
    nothing when it does not run reads exactly like a leg that ran and passed.
    """
    legs = []
    ok = md5(path) == expected_md5
    legs.append(f"pre-image md5 {'matches' if ok else 'MISMATCH'}")
    root, rel = git_root_relative(path)
    if root is None:
        legs.append("git leg NOT AVAILABLE (file is not tracked in a git repo)")
    else:
        show = subprocess.run(["git", "-C", root, "show", f"HEAD:{rel}"], capture_output=True)
        if show.returncode != 0:
            legs.append("git leg NOT AVAILABLE (no HEAD blob for this path)")
        else:
            same = hashlib.md5(show.stdout).hexdigest() == md5(path)
            legs.append(f"git HEAD:{rel} {'matches' if same else 'MISMATCH'}")
            ok = ok and same
    return ok, "; ".join(legs)


def classify(rc, out):
    """One test command's outcome -> ('caught' | 'crashed' | 'missed', why).

    A CRASH OUTRANKS A RED. A run that printed FAIL lines and then hit a KeyError has not
    demonstrated that an assertion caught the mutation; it has demonstrated that we cannot tell.

    AssertionError is the one exception type that is a catch, because it is the only one a test
    raises ON PURPOSE. Anything else — the KeyError of a test tripping over a shape it did not
    expect — is the over-count that scored three reworks' worth of confidence in BOSS's harness.

    Conservative on purpose: a test that merely PRINTS the word "KeyError" in a detail string is
    scored as crashed. Wrongly calling a catch a crash costs a re-read; the other direction costs a
    false green.
    """
    excs = set(EXC.findall(out))
    non_assert = sorted(excs - {"AssertionError"})
    if non_assert:
        return "crashed", f"exception type(s) other than AssertionError: {non_assert}"
    if any(m in out for m in HARD_CRASH):
        return "crashed", "the test runner failed before or outside the tests"
    if rc > 1 or rc < 0:
        return "crashed", f"exit {rc} is not a test verdict (0 pass / 1 fail)"
    return ("caught" if rc != 0 else "missed"), f"exit {rc}"


def run(cmd, cwd):
    # shlex, not .split(): this tree lives under "Calling New" and a naive split turns one path into
    # two arguments. That failure looks like a missing test file, which scores as a miss.
    p = subprocess.run(shlex.split(cmd) if isinstance(cmd, str) else cmd,
                       cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score ONE mutation. See module docstring for the rules.")
    ap.add_argument("--module", required=True)
    ap.add_argument("--anchor"); ap.add_argument("--anchor-file")
    ap.add_argument("--replacement", default=None); ap.add_argument("--replacement-file")
    ap.add_argument("--test", action="append", required=True,
                    help="a test command, repeatable; each is scored separately")
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--name", default="(unnamed mutation)")
    ap.add_argument("--safety", action="store_true",
                    help="the claim is a SAFETY property: an execution marker is then mandatory")
    ap.add_argument("--marker", default=None,
                    help="path the MUTATED code writes when it executes (the `pwned` pattern)")
    ap.add_argument("--no-import", action="store_true",
                    help="compile-check only; use when importing the module has side effects")
    a = ap.parse_args(argv)

    def say(*x): print(*x, flush=True)

    anchor = open(a.anchor_file).read() if a.anchor_file else a.anchor
    repl = open(a.replacement_file).read() if a.replacement_file else a.replacement
    if anchor is None or repl is None:
        say("REFUSED — need --anchor/--anchor-file and --replacement/--replacement-file"); return 2

    # RULE 3, and it is checked BEFORE the module is touched: a guard that fires after the edit has
    # already let the risky thing happen.
    if a.safety and not a.marker:
        say("REFUSED TO SCORE — --safety was given with no --marker.")
        say("  A safety claim needs an INDEPENDENT signal that the mutation executed. A failure count")
        say("  cannot supply it: tests can fail for reasons unrelated to the mutated line, and tests")
        say("  can pass because the mutated line never ran. Supply --marker, or drop --safety and")
        say("  accept that this run does not license a safety claim.")
        return 2

    path = os.path.abspath(a.module)
    cwd = a.cwd or os.path.dirname(path)
    src = open(path, encoding="utf-8").read()
    hits = src.count(anchor)
    if hits == 0:
        say(f"REFUSED — anchor not present in {os.path.basename(path)}; nothing was changed.")
        say("  An anchor that does not match produces a run of the UNMUTATED code, which scores as")
        say("  'nothing caught it' and looks exactly like a hole in the tests.")
        return 2
    say(f"mutation: {a.name}")
    say(f"  module {path}")
    say(f"  anchor occurs {hits}x (all replaced)")

    pre = tempfile.NamedTemporaryFile(prefix="mutate-preimage-", delete=False).name
    shutil.copyfile(path, pre)
    orig_md5 = md5(path)
    say(f"  pre-image {orig_md5[:8]} -> {pre}")
    if a.marker and os.path.exists(a.marker):
        os.unlink(a.marker)
        say(f"  marker {a.marker} existed before the run and was removed — a stale marker proves nothing")

    scored = None
    try:
        open(path, "w", encoding="utf-8").write(src.replace(anchor, repl))
        invalidate_bytecode(path)

        # RULE 1. compile() first: it separates SyntaxError from every other load failure WITHOUT
        # executing the module. Then a real import, unless the caller says that has side effects.
        try:
            # NOT cfile=os.devnull: py_compile REFUSES to write a non-regular file and raises
            # FileExistsError, which this very block would then report as "does not compile" — a
            # broken checker producing the exact refusal it exists to produce, on every input.
            cf = tempfile.NamedTemporaryFile(prefix="mutate-pyc-", suffix=".pyc", delete=False).name
            try:
                py_compile.compile(path, doraise=True, cfile=cf)
            finally:
                os.path.exists(cf) and os.unlink(cf)
        except py_compile.PyCompileError as e:
            say("MUTATION-DID-NOT-LOAD — the mutated module does not compile, so no test ever ran.")
            say("  This is NOT a score of zero. Nothing was measured. Fix the mutation and re-run.")
            say(f"  {str(e).strip().splitlines()[-1][:300]}")
            return 3
        if not a.no_import:
            rc, out = run([sys.executable, "-c",
                           "import importlib.util as u,sys;"
                           f"s=u.spec_from_file_location('m_under_test', {path!r});"
                           "m=u.module_from_spec(s);sys.modules['m_under_test']=m;s.loader.exec_module(m)"], cwd)
            if rc != 0:
                say("MUTATION-DID-NOT-LOAD — the mutated module compiles but does not import.")
                say("  This is NOT a score of zero. Nothing was measured.")
                say("  " + out.strip().splitlines()[-1][:300] if out.strip() else "")
                return 3

        results = []
        for cmd in a.test:
            rc, out = run(cmd, cwd)
            verdict, why = classify(rc, out)
            results.append((cmd, verdict, rc))
            say(f"  {verdict.upper():8s} {cmd}   ({why})")

        # RULE 3, second half. Checked BEFORE any count is printed: a count published beside a
        # missing marker is read as a score, and the caveat underneath it is not.
        if a.marker and not os.path.exists(a.marker):
            say("MUTATION-DID-NOT-EXECUTE — the marker was never written, so the mutated line did not run.")
            say("  No score is emitted. Every verdict above is about code paths the mutation never")
            say("  reached, and reporting them as a mutation score would be a fabrication.")
            return 3

        c = sum(1 for _, v, _ in results if v == "caught")
        m = sum(1 for _, v, _ in results if v == "missed")
        k = sum(1 for _, v, _ in results if v == "crashed")
        scored = f"caught={c} missed={m} crashed={k}"
        if a.marker:
            scored += "  [marker written: the mutation demonstrably executed]"
    finally:
        # RULE 4. Restore, verify the restore, then delete the pre-image — a pre-image that outlives
        # its run is the stale-baseline trap: a later restore writes old content over newer edits and
        # reports success.
        shutil.copyfile(pre, path)
        invalidate_bytecode(path)
        ok, how = verify_restore(path, orig_md5)
        os.unlink(pre)
        say(f"  restored: {how}")
        if not ok:
            say("RESTORE-FAILED — the module on disk is NOT the code you started with.")
            say("  Every measurement taken after this point in the session is suspect. Fix it now.")
            return 4

    for cmd in a.test:
        rc, out = run(cmd, cwd)
        if rc != 0 or classify(rc, out)[0] == "crashed":
            say(f"RESTORE-VERIFIED-BUT-SUITE-RED — `{cmd}` does not pass on the restored module (exit {rc}).")
            say("  The bytes are right, so the red is not the mutation. Something else is wrong and")
            say("  the score below would have been read against a suite that was already broken.")
            say(f"  would have been: {scored}")
            return 4

    say(scored)
    if "crashed=0" not in scored:
        say("  NOTE: a crashed test is neither a catch nor a miss. It scored nothing. Read the crash.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
