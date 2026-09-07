"""Auto-gate and auto-rework: what the daemon may decide by itself, and what it must not.

BOSS + ★, 2026-09-07 throughput diagnosis. Every REPORT_READY waited for BOSS to launch a gate, and
every FAIL waited for BOSS to read Codex and re-prompt. Both are mechanical when the answer is
unambiguous; both cost the item a human round trip.

THE ASYMMETRY IS THE DESIGN AND IS ASSERTED HERE: the daemon may hand a FAILING candidate its
reviewer's own findings, and may never act on a PASS. An unnecessary rework costs one executor turn;
an unnecessary merge costs trunk. Every escalate branch below is a case where the machine cannot
tell a candidate problem from a system problem, and handing findings back in any of them would be
asking an executor to fix something it did not do.

Fixtures are real gate .md shapes. Hermetic: no daemon, no server, no gate, no box."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("ag", os.path.join(HERE, os.pardir, "autogate.py"))
A = importlib.util.module_from_spec(spec); sys.modules["ag"] = A; spec.loader.exec_module(A)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def gate_md(verdict="FAIL", codex="rc=0 verdict=needs-attention blockers=2['[high]'] (3709 chars)",
            extra=(), disagree=False):
    rows = [("sha on lane head", "PASS", "report sha 26b38cfe1f head 26b38cfe1f"),
            ("proofs", "PASS", "77 passed"),
            ("codex adversarial review", "PASS" if verdict == "PASS" else "FAIL", codex)]
    rows += list(extra)
    body = f"# GATE B.010.x — {verdict} — 2026-09-07 02:00\n\n"
    for n, ok, d in rows:
        body += f"- **{n}**: {ok} — {d}\n"
    if disagree:
        body += "- agy second opinion: Verdict: approve (0 findings) — DISAGREE: verdicts differ\n"
    return body

CODEX = "## Findings\n\n[high] platform/core/pay.py: the guard is never reached\n[medium] docs\n"

# ---------------------------------------------------------------- the automated case
g = A.parse_gate_md(gate_md())
act, why = A.decide(g, CODEX, {})
check("MUST-BITE  a FAIL with a real Codex verdict is auto-reworked", act == "rework", (act, why))
check("  and the reason names the verdict", "needs-attention" in why, why)

# ---------------------------------------------------------------- BOSS's named controls
g = A.parse_gate_md(gate_md("PASS", codex="rc=0 verdict=approve blockers=0"))
act, why = A.decide(g, CODEX, {})
check("MUST-BITE  a PASS is NEVER auto-reworked — merges stay BOSS's", act == "escalate", (act, why))
check("  and the reason says so", "BOSS decides merges" in why, why)

for detail, label in (
        ("rc=1 verdict=NONE FOUND (fail-closed) blockers=0 (0 chars)", "NONE"),
        ("CODEX WALLED until Sep 9, 2026 10:40 PM (not retried)", "a WALL"),
        ("not run (--no-codex)", "a skipped reviewer"),
        ("error RuntimeError: boom", "a reviewer error"),
        ("still running after 1800s — joined and failed closed", "a reviewer timeout")):
    g = A.parse_gate_md(gate_md(codex=detail))
    act, why = A.decide(g, CODEX, {})
    check(f"MUST-BITE  {label} is NEVER auto-reworked", act == "escalate", (act, why))

g = A.parse_gate_md(gate_md("NOT COMPLETED"))
check("MUST-BITE  a CRASHED gate is escalated, not reworked", A.decide(g, CODEX, {})[0] == "escalate")
g = A.parse_gate_md(gate_md(disagree=True))
act, why = A.decide(g, CODEX, {})
check("MUST-BITE  a DISAGREE is escalated — a rework would pick a side the gate did not",
      act == "escalate", (act, why))

# ---------------------------------------------------------------- the two convergence limits
g = A.parse_gate_md(gate_md())
check("a 2nd consecutive fail is still auto-reworked", A.decide(g, CODEX, {"fails": 1})[0] == "rework")
act, why = A.decide(g, CODEX, {"fails": 2})
check("MUST-BITE  the 3rd consecutive fail goes to BOSS", act == "escalate", (act, why))
check("  and says the item is not converging", "not converging" in why, why)
act, why = A.decide(g, CODEX, {"fails": 0, "classes": ["high:platform/core/pay.py"]})
check("MUST-BITE  a REPEATED finding class goes to BOSS", act == "escalate", (act, why))
check("  naming the class that repeated", "platform/core/pay.py" in why, why)
act, _ = A.decide(g, CODEX, {"fails": 0, "classes": ["high:platform/core/other.py"]})
check("CONTROL  a DIFFERENT finding class still auto-reworks", act == "rework")

# ---------------------------------------------------------------- verbatim, and the box decision
blk = A.rework_block(2, "2026-09-07 02:00", "26b38cfe1f", CODEX)
check("MUST-BITE  the findings are appended VERBATIM, unsummarised", CODEX.strip() in blk, blk[:120])
check("  under a header naming the rework number and the sha",
      "## REWORK 2 (auto, 2026-09-07 02:00) — gate at 26b38cfe1f" in blk, blk[:90])
pr = A.rework_prompt("B.010.x", 2, "26b38cfe1f", CODEX, "STANDING RULES BODY")
check("MUST-BITE  the prompt carries the standing rules FIRST", pr.startswith("STANDING RULES BODY"), pr[:60])
check("  and the findings verbatim", CODEX.strip() in pr)
check("  and tells the executor a non-defect must be answered, not skipped",
      "do not silently skip" in pr, pr[-200:])

check("a .py proof file needs the box", A.needs_box(["tests/test_x.py"]) is True)
check("CONTROL  a portal-only item does not", A.needs_box(["tests/x.tsx"]) is False)
check("CONTROL  an item with no proof files does not", A.needs_box([]) is False)

item = {"id": "B.010.x", "worktree": "/tmp/wt", "lane": "lane/x", "proof_files": ["tests/test_x.py"]}
ok, why = A.gate_launchable(item, gates_running=0, sha_ok=True)
check("a box item launches when the box is free", ok is True, why)
ok, why = A.gate_launchable(item, gates_running=1, sha_ok=True)
check("MUST-BITE  a box item is NOT launched while another gate runs", ok is False, why)
ok, why = A.gate_launchable(dict(item, proof_files=["x.tsx"]), gates_running=1, sha_ok=True)
check("CONTROL  a box-FREE item may launch alongside a running gate", ok is True, why)
ok, why = A.gate_launchable(item, gates_running=0, sha_ok=False)
check("MUST-BITE  nothing is gated when the report's sha is not the lane head", ok is False, why)

# ---------------------------------------------------------------- the trigger itself
# BOSS, measured live 2026-09-07: EXEC-B answered an "are you idle?" note with
#   "REPORT READY: idle — lane ... clean (merged ...), no lock held ... Awaiting next dispatch."
# and the board raised REPORT_READY. Under auto-gate that launches a Codex run and takes the box
# for an item that was already merged. The marker is a token an executor can write in a sentence
# about anything; it is not evidence that a report exists.
REAL = "REPORT READY — B.010.x\n\nReport: audit/plan-execution-2026-09-04/reports/B.010.x-art-r1.md\n"
IDLE = ("REPORT READY: idle — lane lane/x clean (merged 26b38cfe), no lock held. "
        "Awaiting next dispatch.\n")
have = lambda rel: rel.endswith("B.010.x-art-r1.md")
DISPATCHED = {"id": "B.010.x", "status": "dispatched"}

ok, why = A.report_trigger_ok(DISPATCHED, REAL, have)
check("a real report on a dispatched item fires the gate", ok is True, why)
ok, why = A.report_trigger_ok(DISPATCHED, IDLE, have)
check("MUST-BITE  the idle-ack names no report path and does NOT fire", ok is False, why)
check("  and the reason says a token is not evidence", "is a token" in why, why)
# `reported` LEFT this list on 2026-09-07 03:2x and the premise it was written on was wrong. feed()
# sets `reported` on the REPORT READY message BEFORE this check runs, so every report arrived here
# already flipped out of the match set: measured live by BOSS at 03:26:43, the gate could fire on
# the first pass and never on a corrected second report. What this list is really for is finished
# items, and those are excluded twice over — here, and by current_item() upstream.
for st in ("merged", "held", "done", "parked", "queued", "broken"):
    ok, why = A.report_trigger_ok({"id": "B.010.x", "status": st}, REAL, have)
    check(f"MUST-BITE  a message on a {st} item does NOT fire the gate", ok is False, (st, why))
ok, why = A.report_trigger_ok({"id": "B.010.x", "status": "reported"}, REAL, have)
check("MUST-BITE  a `reported` item with a real report DOES fire — feed() set that status a line ago",
      ok is True, why)
ok, why = A.report_trigger_ok({"id": "B.010.x", "status": "reported"}, IDLE, have)
check("  and the idle-ack protection did not move with it: the PATH evidence still refuses",
      ok is False, why)
ok, why = A.report_trigger_ok({"id": "B.010.x", "status": "rework"}, REAL, have)
check("CONTROL  a rework item DOES fire — it is still in flight", ok is True, why)
ok, why = A.report_trigger_ok(DISPATCHED, REAL, lambda rel: False)
check("MUST-BITE  a named report that does NOT exist at the lane head does not fire", ok is False, why)
check("  and the reason names the path it looked for", "B.010.x-art-r1.md" in why, why)
ok, why = A.report_trigger_ok(DISPATCHED, "ACK REOPEN — idle and fed.", have)
check("CONTROL  a bare ACK does not fire", ok is False, why)
check("the plain reports/ form is recognised too",
      A.report_trigger_ok(DISPATCHED, "done, see reports/B.010.x-art-r1.md", have)[0] is True)

# ---------------------------------------------------------------- an APPROVE on a failing gate
# Measured 2026-09-07 04:10:03, the FIRST daemon-launched gate: Codex approved, the gate failed on
# proofs, and the escalation read "the Codex row carries no reviewer opinion (verdict=approve) —
# nothing was measured about this candidate by the reviewer". Every word of that was false. The
# ACTION was right (an approve carries no findings to hand back); the REASON misdescribed the
# evidence, which is worse than no reason — BOSS reads the reason instead of the gate file, and
# would go looking for a reviewer outage that never happened.
APPROVE_FAIL = ("# GATE B.010.x — FAIL — 2026-09-07 04:10\n\n"
                "- **sha on lane head**: PASS — report sha b5521332cb\n"
                "- **proofs**: FAIL — 2 failed\n"
                "- **codex adversarial review**: PASS — rc=0 verdict=approve blockers=0[] (2739 chars)\n")
g_af = A.parse_gate_md(APPROVE_FAIL)
check("MUST-BITE  failed_rows names the row that actually failed", A.failed_rows(g_af) == ["proofs"],
      A.failed_rows(g_af))
act, why = A.decide(g_af, "", {})
check("MUST-BITE  an approve on a failing gate still ESCALATES — no findings to hand back",
      act == "escalate", (act, why))
check("MUST-BITE  and the reason says Codex APPROVED, never that it gave no opinion",
      "APPROVED" in why and "no reviewer opinion" not in why, why)
check("  and it names what the gate actually failed on", "proofs" in why, why)

NO_VERDICT = ("# GATE B.010.x — FAIL — 2026-09-07 04:10\n\n"
              "- **proofs**: FAIL — 2 failed\n"
              "- **codex adversarial review**: FAIL — rc=1 NONE (0 chars)\n")
act2, why2 = A.decide(A.parse_gate_md(NO_VERDICT), "", {})
check("CONTROL  a row with NO verdict still says exactly that — the two cases stay distinct",
      act2 == "escalate" and "no reviewer opinion" in why2 and "APPROVED" not in why2, why2)

print("\nAUTOGATE " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
