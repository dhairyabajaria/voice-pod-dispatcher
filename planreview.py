#!/usr/bin/env python3
"""Pre-build PLAN review: run agy over an executor's PLAN block BEFORE it writes code.

  planreview.py <item-id> <plan-file>     review one plan, print the JSON verdict
  planreview.py --selftest                run the built-in known-answer check

Owner-approved 2026-09-05 (change 1 of three). The merge gate already catches these defects —
but it catches them after the code exists, which is why they become reworks. Measured on
2026-09-04/05: roughly one in three items came back with substantive rework, and the findings
were the same handful of questions every time (see FIVE_QUESTIONS in the handbook draft).

WHY agy AND NOT CODEX HERE — match the model to the COST OF ITS ERROR MODE.
Measured 2026-09-05 on the aab41585 erasure diff: agy (gemini-3.8-flash-high) found the real
defect Codex found and rated it harder, but ALSO produced a confident `critical` that was
verified false (it claimed a RETURNS TABLE / DECLARE collision that is legal shadowing, and the
body returns the locals explicitly). At a MERGE gate that false critical costs a rework cycle,
so Codex stays there. On a ten-line PLAN block a false positive costs the executor one
clarifying sentence before a line is written — and the input is ~2 KB instead of 25 KB, and the
latency sits on the executor's critical path where Codex's slower turn hurts. Cheap, fast, and
occasionally wrong is the right trade HERE and the wrong one at the gate.

FAIL-OPEN, DELIBERATELY — the opposite of mergegate.py, and the asymmetry is the point.
mergegate fails CLOSED because a missed defect there reaches trunk. This is an advisory
pre-check standing in front of a gate that still runs in full, so a review that cannot be
obtained must not stall the build: agy being down, slow, or rate-limited would otherwise idle
every executor at once. An unavailable reviewer returns proceed=True with `error` set and the
executor is told the review did not run. A defect this misses is still caught at the gate.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

CN = os.environ.get("CN", "/Users/dhairyabajaria/Claude Code/Calling New")
D = os.path.join(CN, "test-logs", "driver")
LOGS = os.path.join(D, "planreview")
AGY = os.path.expanduser("~/.local/bin/agy")
MODEL = "gemini-3.8-flash-high"
TIMEOUT_S = 240

SCHEMA = {
    "type": "object",
    "required": ["proceed", "gaps"],
    "properties": {
        "proceed": {"type": "boolean"},
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question", "severity", "gap"],
                "properties": {
                    # which of the five the plan failed to answer; `other` for anything else
                    "question": {"type": "string",
                                 "enum": ["unreached-rows", "failure-mode", "called-from-production",
                                          "crash-between-steps", "overclaimed-proof", "other"]},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "gap": {"type": "string"},
                },
            },
        },
    },
}

PROMPT = """You are reviewing an implementation PLAN before any code is written. You are NOT reviewing code.

Your only job is to find what this plan has not thought about, while it is still cheap to fix. Judge it against exactly these five questions, which are the ones that have actually produced rework on this program:

1. unreached-rows — what PRE-EXISTING rows does this not reach? A migration that changes a constraint or adds a column almost always leaves historical rows in the old shape, and the plan must say what happens to them. This is the single most common defect on this program.
2. failure-mode — for each write in this path, what happens when it FAILS: does the system fail open (proceeds as if it succeeded) or closed (refuses)? Fail-open on a guard is a defect.
3. called-from-production — is every new function/trigger/view actually CALLED from production code, and by what? A function nothing calls is dead weight that tests can still make green.
4. crash-between-steps — if the process dies between two steps here, what state is left behind, and does anything reconcile it?
5. overclaimed-proof — does the plan claim its proof demonstrates something the named tests would not actually demonstrate? A test that passes for a reason other than the guard working is worse than no test.

Rules for your answer:
- Report a gap ONLY when the plan is genuinely silent or wrong on that question. A plan that answers a question adequately is not a gap, even if you would have answered differently.
- If a question does not apply to this plan (e.g. no migration, so no pre-existing rows), that is NOT a gap.
- `proceed` is true when there are no high-severity gaps. Medium and low gaps are worth raising but do not block.
- Be concrete: name the table, function, or step. "Consider error handling" is useless; "the UPDATE at step 3 has no stated behaviour if the trigger raises" is useful.
- You are reviewing a PLAN, so absence of implementation detail is expected and is not itself a gap.

ITEM: {item}
ARTIFACT: {artifact}

--- PLAN ---
{plan}
--- END PLAN ---
"""


def review(item_id, plan_text, artifact=""):
    """Run agy over a plan. Never raises: an unobtainable review returns proceed=True + error."""
    if not os.path.exists(AGY):
        return {"proceed": True, "gaps": [], "error": f"agy not found at {AGY}"}
    os.makedirs(LOGS, exist_ok=True)
    prompt = PROMPT.format(item=item_id, artifact=artifact or "-", plan=plan_text.strip()[:60000])
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as sf:
        json.dump(SCHEMA, sf)
        schema_path = sf.name
    try:
        # `--print` with the prompt inline and an explicit no-tools instruction: headless agy
        # AUTO-DENIES any tool call and then returns an empty verdict (measured 2026-09-05), so a
        # plan review must never depend on the model reading the tree itself.
        proc = subprocess.run(
            [AGY, "-p", "Do NOT use any tools. Everything you need is in this message.\n\n" + prompt,
             "--model", MODEL, "--output-format", "json", "--json-schema", schema_path,
             "--print-timeout", "4m"],
            capture_output=True, text=True, timeout=TIMEOUT_S, cwd=CN,
        )
    except subprocess.TimeoutExpired:
        return {"proceed": True, "gaps": [], "error": f"agy timed out after {TIMEOUT_S}s"}
    except Exception as e:
        return {"proceed": True, "gaps": [], "error": f"agy failed to run: {e}"}
    finally:
        try:
            os.unlink(schema_path)
        except OSError:
            pass
    return parse(proc.stdout, proc.stderr)


def parse(stdout, stderr=""):
    """Verdict lives in `structured_output` (mirrored in `response`) — NOT `result`, which does
    not exist; reading the wrong key looks exactly like a model that returned nothing."""
    try:
        d = json.loads(stdout)
    except Exception:
        return {"proceed": True, "gaps": [], "error": f"agy output was not JSON: {(stderr or stdout)[:300]}"}
    raw = d.get("structured_output") or d.get("response") or ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, re.S)
            try:
                raw = json.loads(m.group(0)) if m else {}
            except Exception:
                raw = {}
    if not isinstance(raw, dict) or "proceed" not in raw:
        return {"proceed": True, "gaps": [], "error": f"agy returned no verdict (status={d.get('status')})"}
    gaps = [g for g in raw.get("gaps", []) if isinstance(g, dict) and g.get("gap")]
    # `proceed` is the model's own call, but a high-severity gap overrides it: a reviewer that
    # reports a blocker and then waves the plan through is the false-green shape all over again.
    proceed = bool(raw.get("proceed")) and not any(g.get("severity") == "high" for g in gaps)
    return {"proceed": proceed, "gaps": gaps, "usage": (d.get("usage") or {}).get("total_tokens")}


def format_feedback(item_id, verdict):
    """The message posted back to the executor. Advisory, and says so — this is not a gate."""
    if verdict.get("error"):
        return (f"DISPATCHER PLAN REVIEW {item_id} — NOT RUN ({verdict['error']}). Proceed with the build. "
                "The merge gate is unaffected and still runs your proofs in full.")
    gaps = verdict.get("gaps") or []
    if not gaps:
        return (f"DISPATCHER PLAN REVIEW {item_id} — no gaps found. Proceed with the build as planned. "
                "This was an advisory pre-check, not an approval: the merge gate still runs your proofs.")
    lines = [f"DISPATCHER PLAN REVIEW {item_id} — {len(gaps)} gap(s) found by an automated pre-build reviewer "
             f"(agy/{MODEL}). This is ADVISORY, not a verdict, and the reviewer is wrong often enough that you "
             "should judge each point rather than obey it.", ""]
    for i, g in enumerate(sorted(gaps, key=lambda x: {"high": 0, "medium": 1, "low": 2}.get(x.get("severity"), 3)), 1):
        lines.append(f"{i}. [{g.get('severity', '?')}] ({g.get('question', 'other')}) {g.get('gap', '').strip()}")
    lines += ["",
              "Do ONE of these, then continue in the same turn — do not stop and wait for BOSS:",
              "  - fold the point into your plan and build the corrected version, or",
              "  - reject it in one sentence in your report's §1 saying why it does not apply.",
              "A point you reject with a reason is a fine outcome; a point you ignore silently is not. "
              "Raise a QUESTION only if a gap reveals a genuine spec/schema ambiguity that is not yours to decide."]
    return "\n".join(lines)


SELFTEST_PLAN = """PLAN for 010.example-terminal-check
1. Add migration 250 relaxing the CHECK on recording_tasks so contact_id may be NULL when
   source_state is 'completed' or 'aborted'.
2. Add function platform_sever_recording_link(org_id, contact_id) that UPDATEs the rows to NULL.
3. Proof: tests/test_recording_sever.py asserts the function nulls the link for a fresh contact.
"""


def selftest():
    """Known-answer check: this plan has NO backfill for rows erased before the migration, and
    never says what calls the new function. A reviewer that misses both is not worth running."""
    v = review("SELFTEST", SELFTEST_PLAN, "010.example-terminal-check")
    print(json.dumps(v, indent=2))
    if v.get("error"):
        print(f"\nSELFTEST INCONCLUSIVE — review unobtainable: {v['error']}")
        return 2
    qs = {g.get("question") for g in v.get("gaps", [])}
    print(f"\nquestions raised: {sorted(qs)}")
    hit = qs & {"unreached-rows", "called-from-production"}
    print("SELFTEST PASS" if hit else "SELFTEST FAIL — missed both planted gaps")
    return 0 if hit else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    item_id, plan_file = sys.argv[1], sys.argv[2]
    verdict = review(item_id, open(plan_file, errors="ignore").read())
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
