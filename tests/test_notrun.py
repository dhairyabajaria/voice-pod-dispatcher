"""A check that COULD NOT RUN is not a check that FAILED.

BOSS, 2026-09-07: "`proofs=FAIL` on a --no-box gate is wrong reporting, and it has been miscolouring
every gate all night". It also made his own metric unmovable: the overall verdict is the AND of
every row, so a --no-box gate scored FAIL by construction and GATE_PASS:GATE_FAIL read 0:9 and 0:8
across the night while the reviewer signal underneath it went 0:9 -> 7:1.

His two rules are the two things this file exists to hold down:
  * a NOT RUN is never counted as a pass ANYWHERE — not in the header, not in the event kind, not in
    the exit code, and above all not in a merge;
  * the verdict never reads PASS to a human who would take it as "this was proven".

The gate is driven END TO END against a temp checkout, because the bug being fixed lives in the
assembly of the verdict and not in any one function. Hermetic: own git repo, no box, no reviewers,
no network."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def git(*a, cwd): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
def write(root, rel, txt):
    p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(txt)


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, os.pardir, "mergegate.py"))
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m)
    return m


def run_gate(tag, proof_files, argv_extra, scope=("**",), stub_proofs=None, codex_ok=False,
             codex_mode=None):
    """One real gate run. Returns (rc, gate .md text, events text, trunk log)."""
    MG = load("mg_" + tag)
    root = tempfile.mkdtemp(prefix="notrun-")
    wt = os.path.join(root, "lane"); os.makedirs(wt)
    git("init", "-q", "-b", "plan010/rebuild", cwd=wt)
    git("config", "user.email", "t@t", cwd=wt); git("config", "user.name", "t", cwd=wt)
    write(wt, "platform/core/x.py", "x = 1\n")
    git("add", "-A", cwd=wt); git("commit", "-qm", "base", cwd=wt)
    git("checkout", "-q", "-b", "lane/x", cwd=wt)
    write(wt, "platform/core/x.py", "x = 2\n")
    write(wt, "audit/plan-execution-2026-09-04/reports/ITEM-x-r1.md",
          "# REPORT ITEM — x — 2026-09-06 IST\n\n## 1. Branch\nhead.\n")
    git("add", "-A", cwd=wt); git("commit", "-qm", "candidate", cwd=wt)
    trunk = os.path.join(root, "trunk")
    subprocess.run(["git", "clone", "-q", wt, trunk], check=True, capture_output=True)
    git("checkout", "-q", "plan010/rebuild", cwd=trunk)
    git("config", "user.email", "t@t", cwd=trunk); git("config", "user.name", "t", cwd=trunk)
    state = os.path.join(root, "test-logs", "driver"); gates = os.path.join(state, "gates")
    os.makedirs(gates)
    queue = os.path.join(state, "queue.json")
    json.dump({"items": [{"id": "ITEM", "artifact": "x", "status": "reported", "worktree": wt,
                          "lane": "lane/x", "scope": list(scope), "proof_files": list(proof_files),
                          "dispatched_at": "2026-09-07 04:00:00"}]}, open(queue, "w"))
    MG.CN, MG.TRUNK, MG.QUEUE, MG.GATES = root, trunk, queue, gates
    MG.EVENTS = os.path.join(state, "events.log"); MG.D = state
    MG.run_merge_preflight = lambda *a, **k: None
    MG.run_yaml_parse_row = lambda *a, **k: None
    MG.run_citesweep_isolated = lambda *a, **k: None
    MG.run_citesweep = lambda *a, **k: None
    if stub_proofs is not None:
        MG.run_proofs = stub_proofs
    if codex_mode == "wall_recorded":
        # the wall the gate finds BEFORE calling: recorded_wall() is what the pre-check reads
        import datetime as _dt
        MG.recorded_wall = lambda now=None: (_dt.datetime(2026, 9, 9, 22, 40), "test-logs/wall.json")
    elif codex_mode == "wall_output":
        # the wall the gate discovers IN the answer, after paying for the call
        walled = open(os.path.join(HERE, "fixtures", "codex_walled.txt"), errors="ignore").read()
        real_sh0 = MG.sh
        MG.sh = lambda argv, **k: ((1, walled)
                                   if any("adversarial-review" in str(a) for a in argv)
                                   else real_sh0(argv, **k))
        MG.codex_precheck = lambda: None
    elif codex_mode == "timeout":
        # a reviewer that never comes back. join(1800) cannot be waited out in a test, so the thread
        # itself is the stub: it starts nothing and reports itself alive, which is exactly the state
        # the gate has to handle.
        class _Hung:
            def __init__(self, *a, **k): pass
            def start(self): pass
            def join(self, timeout=None): pass
            def is_alive(self): return True
        MG.threading = type("T", (), {"Thread": _Hung})()
        MG.codex_precheck = lambda: None
    if codex_ok:
        # the reviewer must really PASS for the merge control below, and a real call is out of the
        # question in a hermetic test: intercept at the process boundary, leaving the gate's own
        # verdict parsing (fail-CLOSED) in the path.
        real_sh = MG.sh
        MG.sh = lambda argv, **k: ((0, '{"verdict": "approve", "blockers": []}')
                                   if any("adversarial-review" in str(a) for a in argv)
                                   else real_sh(argv, **k))
        MG.codex_precheck = lambda: None
    sys.argv = ["mergegate.py", "ITEM", *argv_extra]
    with FL.prefixed(f"gate run [{tag}]"):
        rc = MG.main()
    md = os.path.join(gates, "ITEM.md")
    body = open(md).read() if os.path.isfile(md) else ""
    ev = open(MG.EVENTS).read() if os.path.isfile(MG.EVENTS) else ""
    log = subprocess.run(["git", "log", "--oneline", "plan010/rebuild"], cwd=trunk,
                         capture_output=True, text=True).stdout
    q = json.load(open(queue))
    shutil.rmtree(root, ignore_errors=True)
    return rc, body, ev, log, q


# ---------------------------------------------------------------- A. nothing failed, proofs unrun
rc, body, ev, _log, q = run_gate("a", ["tests/test_x.py"], ["--no-box", "--no-codex", "--no-agy"])
head = body.splitlines()[0] if body else ""
check("MUST-BITE  the proofs row says NOT RUN, not FAIL",
      "- **proofs**: NOT RUN" in body,
      [l for l in body.splitlines() if l.startswith("- **proofs**")][:1])
check("  and it names what went unmeasured, so the reason travels with the row",
      "tests/test_x.py" in body and "--no-box" in body,
      [l for l in body.splitlines() if l.startswith("- **proofs**")][:1])
check("MUST-BITE  the verdict is INCOMPLETE — a gate that proved nothing must not say PASS",
      head.startswith("# GATE ITEM — INCOMPLETE"), head)
check("MUST-BITE  ...and it is not FAIL either: nothing about this candidate failed",
      "FAIL" not in head, head)
check("  the header names what did not run", "proofs" in head and "not run" in head, head)
check("MUST-BITE  the EVENT is GATE_INCOMPLETE — the kind is what every counter reads",
      "GATE_INCOMPLETE" in ev and "GATE_PASS" not in ev, ev.strip()[-200:])
check("MUST-BITE  the emitted line still carries dispatched_at — the split in the checkpoint "
      "reporter is built on it, and test_precheck can only see that the code says so",
      "dispatched_at=2026-09-07 04:00:00" in ev, ev.strip()[:120])
check("MUST-BITE  the exit code is not 0: a shell `rc == 0` must not read unrun as success",
      rc == 3, rc)
check("  the queue row records WHICH checks did not run",
      q["items"][0].get("gate_not_run") == ["proofs", "codex adversarial review"],
      q["items"][0].get("gate_not_run"))
check("  --no-codex is the same kind of absence and is recorded the same way",
      "- **codex adversarial review**: NOT RUN" in body,
      [l for l in body.splitlines() if "codex adversarial" in l][:1])

# ---------------------------------------------------------------- B. a real failure, plus an unrun
rc_b, body_b, ev_b, _l, _q = run_gate("b", ["tests/test_x.py"],
                                      ["--no-box", "--no-codex", "--no-agy"], scope=["docs/**"])
head_b = body_b.splitlines()[0] if body_b else ""
check("MUST-BITE  a REAL failure still fails, and is not softened by an unrun check beside it",
      head_b.startswith("# GATE ITEM — FAIL"), head_b)
check("  the failure is the scope row, measured, not the unrun one",
      "- **scope**: FAIL" in body_b, [l for l in body_b.splitlines() if "scope" in l][:1])
check("  the header still carries the unrun reason alongside the failure",
      "not run" in head_b, head_b)
check("MUST-BITE  the event is GATE_FAIL, never GATE_INCOMPLETE, when something really failed",
      "GATE_FAIL" in ev_b and "GATE_INCOMPLETE" not in ev_b, ev_b.strip()[-160:])
check("  and rc is 1, the ordinary failure code", rc_b == 1, rc_b)

# ---------------------------------------------------------------- C. autopilot must never merge it
rc_c, body_c, ev_c, log_c, _q = run_gate("c", ["tests/test_x.py"],
                                         ["--no-box", "--no-codex", "--no-agy", "--autopilot"])
check("MUST-BITE  AUTOPILOT does not merge a gate whose proofs never ran",
      "merge(" not in log_c, log_c.strip()[:200])
check("MUST-BITE  ...and it SAYS why rather than merging silently or skipping silently",
      "MERGE_SKIPPED" in ev_c and "not run" in ev_c,
      [l for l in ev_c.splitlines() if "MERGE_SKIPPED" in l][:1])

def ran_proofs(it, wt, sha, head_, item_id, rec, *a, **k):
    rec("proofs", True, "12 passed in 4.10s")


rc_e, body_e, ev_e, log_e, _q = run_gate("e", ["tests/test_x.py"], ["--no-agy", "--autopilot"],
                                         stub_proofs=ran_proofs, codex_ok=True)
check("CONTROL  this harness CAN see a merge: proofs that RAN and passed do get merged under "
      "autopilot — without this, case C's 'did not merge' would pass on a broken harness",
      "merge(" in log_e, (body_e.splitlines()[:1], log_e.strip()[:120]))

# ---------------------------------------------------------------- D. not applicable is not unrun
seen = {}
def stub(it, wt, sha, head_, item_id, rec, *a, **k):
    seen["called"] = True
    rec("portal proofs", True, "vitest 12 passed")
rc_d, body_d, ev_d, _l, q_d = run_gate("d", ["portal/x.spec.ts"],
                                       ["--no-box", "--no-codex", "--no-agy"], stub_proofs=stub)
check("MUST-BITE  a box-free item is measured, not marked unrun: the daemon passes --no-box to EVERY "
      "one of them, so this branch decides whether they can ever gate anything but FAIL",
      seen.get("called") is True and "- **proofs**: NOT RUN" not in body_d,
      [l for l in body_d.splitlines() if l.startswith("- **")][:3])
check("  its own proof row is what decides the verdict",
      "- **portal proofs**: PASS" in body_d, body_d.splitlines()[:1])

# ---------------------------------------------------------------- F/G. an OUTAGE cannot merge
# BOSS's condition for converting these rows from FAIL to NOT RUN (2026-09-07): fail-closed no
# longer rests on the row's own value, it rests entirely on "INCOMPLETE never merges" — one line
# away from being lost by a well-meaning change. So the property is pinned HERE, independently,
# against a merge that really does not happen, with case E above as the control that really does.
for tag, mode, label in (("f", "wall_recorded", "a wall found BEFORE the call"),
                         ("g", "wall_output", "a wall found IN the answer"),
                         ("h", "timeout", "a reviewer that never returns")):
    rc_x, body_x, ev_x, log_x, _q = run_gate(tag, ["tests/test_x.py"],
                                             ["--no-agy", "--autopilot"],
                                             stub_proofs=ran_proofs, codex_mode=mode)
    row = [l for l in body_x.splitlines() if "codex adversarial" in l][:1]
    check(f"MUST-BITE  {label} is NOT RUN, not FAIL — an outage says nothing about the candidate",
          "- **codex adversarial review**: NOT RUN" in body_x, row)
    check(f"MUST-BITE  {label} CANNOT MERGE, even under autopilot with every other check green",
          "merge(" not in log_x, (body_x.splitlines()[:1], log_x.strip()[:120]))
    check(f"  ...and the gate says so out loud rather than skipping in silence",
          "MERGE_SKIPPED" in ev_x and "not run" in ev_x,
          [l for l in ev_x.splitlines() if "MERGE_SKIPPED" in l][:1])
    check(f"  the verdict is INCOMPLETE, and the proofs that DID run are not what is missing",
          body_x.splitlines()[0].startswith("# GATE ITEM — INCOMPLETE")
          and "- **proofs**: PASS" in body_x, body_x.splitlines()[:1])
    check(f"  the cause survives into the row, so BOSS can tell wait-it-out from go-and-look",
          ("WALLED" in body_x if "wall" in mode else "1800s" in body_x), row)

# an ERROR in our OWN runner is a different animal and stays a FAIL: that is a gate defect, and a
# defect must stay loud. This is the negative control for the conversion above.
src = open(os.path.join(HERE, os.pardir, "mergegate.py"), errors="ignore").read()
i = src.find('elif "error" in codex_box:')
check("CONTROL  an exception in our own runner is still a FAIL, not an outage",
      i != -1 and 'rec("codex adversarial review", False,' in src[i:i + 160], i)

# ---------------------------------------------------------------- autogate reads the third status
spec = importlib.util.spec_from_file_location("ag_nr", os.path.join(HERE, os.pardir, "autogate.py"))
AG = importlib.util.module_from_spec(spec); spec.loader.exec_module(AG)
g = AG.parse_gate_md(body)
check("MUST-BITE  autogate PARSES a NOT RUN row — an unparsed row is invisible to every reason it builds",
      "proofs" in g["rows"] and g["rows"]["proofs"][0] is None, g["rows"].get("proofs"))
check("MUST-BITE  failed_rows does NOT list it: `if not ok` would call an unrun check a failure",
      "proofs" not in AG.failed_rows(g), AG.failed_rows(g))
check("  not_run_rows does list it", "proofs" in AG.not_run_rows(g), AG.not_run_rows(g))
act, why = AG.decide(g, "", {})
check("MUST-BITE  an INCOMPLETE gate is ESCALATED, never auto-reworked — there is nothing to hand back",
      act == "escalate", (act, why))
check("  and the reason says unproven, not approved and not failed",
      "unproven" in why and "proofs" in why, why)
gp = AG.parse_gate_md("# GATE ITEM — PASS — x\n- **proofs**: PASS — 12 passed\n")
check("CONTROL  an ordinary PASS still escalates as a PASS", AG.decide(gp, "", {})[1].startswith("GATE PASS"))
gf = AG.parse_gate_md("# GATE ITEM — FAIL — x\n- **proofs**: FAIL — 2 failed\n")
check("CONTROL  a real FAIL row is still a failure", AG.failed_rows(gf) == ["proofs"], AG.failed_rows(gf))

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
