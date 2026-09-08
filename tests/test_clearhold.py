"""`dispatcherctl.sh clear-hold` with NO argument died on a SyntaxError, and nothing noticed.

BOSS found it by RUNNING it, 2026-09-08:

    SyntaxError: f-string: invalid syntax   ->   (v.get(class))

The listing block sits inside a double-quoted `python3 -c "…"`. An inner `"` is not a quote to the
shell there — it CLOSES the string — so `v.get("class")` reached Python as `v.get(class)`, a
keyword. The RELEASE branch one line below was already argv-driven and worked; BOSS proved it live.
The listing branch is reached only with no argument, and the suite never exercised it.

Third unexercised branch of a guard to ship broken in one night — the others were `restart`'s
self-call behind `--dry-run` and the render-time aging behind a stubbed `write_pending`. So this
file does two things:

  * drives the real script, in a subprocess, through EVERY clear-hold branch — held, nothing held,
    unreadable state, and the release request. No re-implementation: the defect lived in the shell
    quoting, which only the shell can reproduce.
  * and a CLASS check over the whole script: every `python3 -c "…"` body must survive the shell's
    own quote-stripping and still compile. That is what makes the NEXT one of these impossible to
    ship, rather than this one impossible to repeat.

Deliberately NOT done: running every ctl verb to see which ones crash. `restart`, `pause` and `stop`
act on the LIVE daemon, and a test that proves a branch by triggering it is not available here. The
class check is static for exactly that reason, and it is weaker for it — it catches a body that
cannot parse, not a body that parses and does the wrong thing.

Hermetic: a temporary DISPATCHER_STATE, no daemon, no live state.json.
"""
import json, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(HERE, os.pardir, "dispatcherctl.sh")

P, FAILED = 0, []
def ok(cond, what):
    global P
    if cond: P += 1; print(f"PASS {what}")
    else: FAILED.append(what); print(f"FAIL {what}")

TMP = tempfile.mkdtemp(prefix="clearhold-")
ST = os.path.join(TMP, "state"); os.makedirs(ST)

def ctl(*args, state=ST):
    r = subprocess.run(["/bin/zsh", CTL, *args], capture_output=True, text=True,
                       env=dict(os.environ, DISPATCHER_STATE=state))
    return r.returncode, r.stdout + r.stderr

def write_state(obj):
    json.dump(obj, open(os.path.join(ST, "state.json"), "w"))

HELD = {"muse": {"unhealthy": {
    "muse-go-1": {"class": "AUTH", "why": "a finding that quoted a 401", "since": 1, "until": 0},
    "muse-go-2": {"class": "QUOTA", "why": "weekly wall", "since": 1, "until": 2}}}}

# --- the branch that died ------------------------------------------------------------------------
write_state(HELD)
rc, out = ctl("clear-hold")
ok("SyntaxError" not in out and "Traceback" not in out,
   "MUST BITE: `clear-hold` with no argument does not die in Python — the exact failure BOSS hit")
ok("muse-go-1" in out and "AUTH" in out and "401" in out,
   "MUST BITE: and it actually LISTS what is held, with the class and the reason. Exiting 2 with a "
   "usage line was already happening while the listing crashed — the exit code proved nothing")
ok("muse-go-2" in out and "QUOTA" in out,
   "  every held profile, not just the first — the loop runs")
ok(rc == 2, "  and it still exits 2, so a caller that keys on the status is unaffected")

# --- the branches either side of it, because 'nothing' has two very different causes ---------------
write_state({"muse": {"unhealthy": {}}})
rc, out = ctl("clear-hold")
ok("(none" in out and "nothing is held" in out,
   "MUST BITE: nothing held SAYS nothing is held, rather than printing an empty space under a "
   "heading and leaving the reader to guess")

rc, out = ctl("clear-hold", state=os.path.join(TMP, "no-such-dir"))
ok("CANNOT SAY" in out and "unreadable" in out,
   "MUST BITE: an unreadable state file is NOT reported as an empty hold list. Those rendered "
   "identically, and the reader would have concluded the account was in service")
ok("SyntaxError" not in out and "Traceback" not in out,
   "  and it says so in one line, not by leaking a stack trace")

# --- the release branch, which BOSS proved live; here so it stays proved ---------------------------
write_state(HELD)
rc, out = ctl("clear-hold", "muse-go-1", "the 401 was inside a finding")
req = os.path.join(ST, "holdclear", "muse-go-1.json")
ok(rc == 0 and os.path.isfile(req),
   "MUST BITE: naming a profile writes the release REQUEST file — the release path still works")
# guarded: if the request file is absent the assertion above has already failed, and a crash
# here would replace that FAIL with a traceback — a crash is neither a catch nor a miss.
body = json.load(open(req)) if os.path.isfile(req) else {}
ok(body.get("profile") == "muse-go-1" and body.get("why") == "the 401 was inside a finding",
   "  carrying the profile and the operator's own reason, which is what HOLD_CLEARED then quotes")
ok(json.load(open(os.path.join(ST, "state.json"))) == HELD,
   "  and state.json is UNTOUCHED: this is a request, not an edit — the daemon rewrites that file "
   "every tick and an edit here would be overwritten or would clobber a tick")

# --- THE CLASS -------------------------------------------------------------------------------------
# Every embedded Python body in the script, put through the shell's own quote-stripping and then
# compiled. This is the check that would have caught the defect above without anyone running the
# branch — and it catches the next one in a branch nobody has run either.
# THE GUARD IS DEFINED OVER THE POPULATION, NOT OVER THE FILE THAT HAPPENED TO HAVE THE BUG.
# The first cut of this scanned dispatcherctl.sh alone. That is green for exactly one path: the day
# someone adds a `python3 -c "…"` to watch.sh or safe_apply.sh, the defect this file exists to catch
# ships again with a fully green suite. Today dispatcherctl.sh is the only script carrying embedded
# Python (2 of 2) — which is why the widening costs nothing now and is worth exactly nothing later
# if it is not done now.
D = os.path.join(HERE, os.pardir)
SHELLS = sorted(
    [os.path.join(D, f) for f in os.listdir(D) if f.endswith(".sh")]
    + [os.path.join(HERE, f) for f in os.listdir(HERE) if f.endswith(".sh")])
bodies, bad_quote, per_file = [], [], {}
for path in SHELLS:
    src = open(path).read()
    rel = os.path.relpath(path, D)
    per_file[rel] = src.count("python3 -c")
    for m in re.finditer(r'python3 -c "', src):
        start = m.end()
        end = src.index('"', start)
        body = src[start:end]
        after = src[end + 1:end + 2]
        line = src[:start].count("\n") + 1
        # If the character after the closing quote is not whitespace or a redirect, the shell did
        # NOT end the argument there — the quote was one somebody meant as Python, and everything
        # after it reaches Python unquoted. That is precisely `v.get("class")` -> `v.get(class)`.
        if after not in (" ", "\t", "\n", ">", ""):
            bad_quote.append((rel, line, body[-60:] + '"' + src[end + 1:end + 20]))
        bodies.append((rel, line, body))
ok(len(SHELLS) >= 4 and any(f.endswith("dispatcherctl.sh") for f in SHELLS),
   f"  CONTROL: the scan enumerates every shell script in dispatcher/ and tests/, not one file — "
   f"{len(SHELLS)} found: {[os.path.basename(f) for f in SHELLS]}")
ok(bodies, "  CONTROL: it actually found embedded Python — an empty scan passes vacuously")
ok(len(bodies) == sum(per_file.values()),
   f"MUST BITE: the scan reaches EVERY embedded Python present, not just the double-quoted ones it "
   f"knows how to parse — {len(bodies)} scanned of {sum(per_file.values())} present {per_file}. If "
   f"this fails because someone wrote `python3 -c '…'` or a heredoc, extend the scan: a checker "
   f"that silently skips a form is the same absence-reads-as-a-pass shape it exists to catch")
ok(not bad_quote,
   "MUST BITE: no embedded Python body contains a bare \" — inside a double-quoted shell string "
   f"that closes the argument and the rest reaches Python unquoted: {bad_quote}")
uncompilable = []
for rel, line, body in bodies:
    try:
        compile(body, f"{rel}:{line}", "exec")
    except SyntaxError as e:
        uncompilable.append((rel, line, str(e)))
ok(not uncompilable,
   f"MUST BITE: every embedded Python body compiles, so a branch nobody has run cannot ship a "
   f"SyntaxError: {uncompilable}")

print(f"\n{P} passed, {len(FAILED)} failed")
for f in FAILED: print("  FAILED:", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
