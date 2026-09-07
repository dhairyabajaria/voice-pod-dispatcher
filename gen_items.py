#!/usr/bin/env python3
"""Generate queue items for every open Plan 011–017 artifact from the ledger.

  gen_items.py            append missing A.<artifact> (audit) / B.<artifact> (build) items to queue.json
  gen_items.py --print    only print what would be added

Source of truth is plans/EXECUTION_LEDGER.md (status + PROOF files). Dependencies are the
ARTIFACT_MAP.md §2 graph, hand-encoded below (parent -> child). One lane per executor so two
executors never share a worktree; audits go to an executor that does not own that lane (Rule 3).
"""
import json
import os
import re
import sys
from collections import Counter

CN = os.environ.get("CN", "/Users/dhairyabajaria/Claude Code/Calling New")
D = os.path.join(CN, "test-logs", "driver")
TR = os.path.join(CN, "voicepod-plan010-rebuild")
LEDGER = os.path.join(TR, "plans", "EXECUTION_LEDGER.md")

DEPS = {
    "011.stage-authority-fields": ["011.state-machine-schema"],
    "013.agent-config-schema-registry": ["011.stage-authority-fields"],
    "012.ruleset-versions": ["011.stage-authority-fields"],
    "014a.asset-delivery-intent": ["011.stage-authority-fields"],
    "015a.campaign-launch-snapshot": ["011.stage-authority-fields", "014a.release-v2", "015a.channel-template-inventory"],
    "012.trigger-action-map": ["011.transition-command", "011.business-events"],
    "011.workspace-column-adapter": ["011.resolution-state-model"],
    "016a.query-and-drilldown": ["011.workspace-column-adapter"],
    "015a.source-subscription-schema": ["011.business-events", "015a.source-identity-contract"],
    "016a.metric-catalog": ["011.business-events", "012.sequence-ended-event",
                            "015a.source-subscription-schema", "015a.operational-raw-views"],
    "012.sequence-schema": ["011.approval-queue"],
    "014a.asset-model": ["011.approval-queue"],
    "012.sequence-commands": ["011.agent-tool-registry", "013.release-resolver-binding"],
    "013.release-resolver-binding": ["013.agent-release-aggregate"],
    "014a.release-v2": ["013.agent-release-aggregate", "014a.capability-descriptor"],
    "013.agent-release-aggregate": ["013.knowledge-access-version", "013.knowledge-content-pack"],
    "015a.channel-template-inventory": ["012.whatsapp-step-reference"],
    "016a.report-definitions-and-csv": ["014a.artifact-custody"],
    "017a.backfills": ["015a.source-subscription-schema"],
    "017a.coexistence-state-machine": ["015a.journey-sole-scheduler"],
    "017a.release-gate-rows": ["016a.metric-catalog", "016a.use-case-vocabulary"],
    "017a.shadow-comparison": ["016a.hour-fact-store"],
}
CHILDREN = {}
for child, parents in DEPS.items():
    for parent in parents:
        CHILDREN.setdefault(parent, []).append(child)

# lane prefix -> (branch, worktree, builder executor)
LANE = {
    "011": ("lane/011-pipelines", "voicepod-lane-011", "EXEC-C"),
    "012": ("lane/012-sequences", "voicepod-lane-012", "EXEC-D"),
    "013": ("lane/013-agents", "voicepod-lane-013", "EXEC-F"),
    "014a": ("lane/014a-assets", "voicepod-lane-014a", "EXEC-G"),
    "015a": ("lane/015a-sources", "voicepod-lane-015a", "EXEC-A"),
    "016a": ("lane/016-analytics", "voicepod-lane-016", "EXEC-E"),
    "017a": ("lane/017-rollout", "voicepod-lane-017", "EXEC-H"),
    # Plan 010 residuals: sensitive and complex — owner order 2026-09-05: every one goes to Codex
    # (gpt-6-astra, medium). The daemon creates the worktree from trunk at dispatch time.
    "010": ("lane/010-residuals-codex", "voicepod-codex-010", "CODEX"),
}
AUDIT_WT = "(detached worktree at the row's COMMIT sha — create it: `git -C $TRUNK worktree add --detach $CN/voicepod-audit-<artifact> <sha>` then `cd platform && uv sync --frozen`; never audit inside the builder's lane worktree)"
AUDITOR = {"011": "EXEC-E", "012": "EXEC-F", "013": "EXEC-C", "014a": "EXEC-E",
           "015a": "EXEC-F", "016a": "EXEC-C", "017a": "EXEC-E",
           # audits must run + break-test on the box, which Codex's sandbox cannot (BOSS ruling 2026-09-05 12:45)
           "010": "EXEC-B"}

BUILD = """Build artifact `{a}` (Plan {plan}). Item {iid}.
Sources of truth, in this order: the frozen contract `plans/contracts/{plan3}.md` (this artifact's section; ledger §6.1 lists PROVISIONAL contracts — check before consuming), the plan `plans/{plan3}-*.md`, and this artifact's ledger row, pasted below.
Dependencies you build against (Rule 2: `git show` the sha and run the named PROOF yourself before consuming): {deps}.
Consumers that will build on you (do not change their interface without a QUESTION): {kids}.
Rules that bite: search the suite before writing a proof (Part 3.3); targeted proofs only, never a sweep (Part 2.1); migrations at the worktree's next free number, renumbered at merge (Part 3.4); edit ONLY this artifact's ledger row (write `BUILT, measured` or `MERGED (awaiting AUDIT)` — never LANDED); break-test every green (rule 7).
Current ledger STATUS: {status}
Report `{iid}` per Part 5.3."""

AUDIT = """AUDIT artifact `{a}` (Plan {plan}) — item {iid}. You are the NON-BUILDER auditor (Rule 3). If you built any part of this artifact, end at once with a QUESTION block saying so and the queue hands it elsewhere.
Told to DISPROVE: read the contract section in `plans/contracts/{plan3}.md`, then the ledger row (pasted below), `git show` its COMMIT sha, run its PROOF targeted on the box (Part 2.2 preflight pasted, head as log line 1), and break-test at least two of its guards yourself (do not trust the row's break-test claims). Check every MUST in the contract has a named test. Check the removal control the row describes actually reds.
Write `audit/plan-execution-2026-09-04/reports/{iid}-audit-{a}.md` with verdict PASSED / FAILED and evidence; a FAILED audit lists each finding with file:line. Do NOT edit the ledger row — BOSS writes `AUDIT: PASSED` and flips to LANDED from your report.
Current ledger STATUS: {status}
Report `{iid}`."""


def read_ledger():
    lines = open(LEDGER, errors="ignore").read().splitlines()
    rows, cur = {}, None
    for ln in lines:
        m = re.match(r"^ARTIFACT:\s+(\S+)", ln)
        if m:
            cur = m.group(1)
            rows[cur] = {"status": "", "proof": [], "block": [ln]}
            continue
        if cur:
            rows[cur]["block"].append(ln)
            m = re.match(r"^STATUS:\s+(.*)", ln)
            if m:
                rows[cur]["status"] = m.group(1).strip()
            for f in re.findall(r"platform/tests/test_\w+\.py", ln):
                if f not in rows[cur]["proof"]:
                    rows[cur]["proof"].append(f)
            if ln.startswith("```") and rows[cur]["status"]:
                cur = None
    return rows


def main():
    print_only = "--print" in sys.argv
    rows = read_ledger()
    import fcntl
    lock = open(os.path.join(D, "queue.json.lock"), "w")
    fcntl.flock(lock, fcntl.LOCK_EX)  # same lock the daemon holds per tick and queuectl.py per edit
    q = json.load(open(os.path.join(D, "queue.json")))
    have = {i["id"] for i in q["items"]}
    new = []
    for pref in ["010", "011", "012", "013", "014a", "015a", "016a", "017a"]:
        for a, r in rows.items():
            if not a.startswith(pref + "."):
                continue
            st = r["status"]
            u = st.upper()
            if u.startswith("LANDED") or "BLOCKED" in u[:60]:
                continue
            lane, wt, ex = LANE[pref]
            if u.startswith("MERGED") or "AUDIT" in u[:60] or "awaiting integration" in st.lower():
                iid, kind, ex, tmpl = f"A.{a}", "audit", AUDITOR[pref], AUDIT
                wt = AUDIT_WT if ex != "CODEX" else wt
            else:
                iid, kind, tmpl = f"B.{a}", "build", BUILD
            if pref == "010":
                # each 010 item gets its own worktree; the daemon creates it from trunk at dispatch
                lane, wt = f"lane/010-{a.split('.', 1)[1]}", f"voicepod-codex-{a.split('.', 1)[1]}"
            if iid in have:
                continue
            deps = DEPS.get(a, [])
            if pref == "010":
                # Plan 010 has no contracts/010.md: its frozen surface is 000-plan010-surface.md
                tmpl = tmpl.replace("`plans/contracts/{plan3}.md`", "`plans/contracts/000-plan010-surface.md`")
                if kind == "audit":
                    # BOSS ruling 2026-09-05 13:25: Plan 010 builders are Codex and box-free, so their §11
                    # break-tests were designed but NEVER RUN. The auditor executes them before AUDIT PASSED.
                    tmpl = tmpl.replace(
                        "Check every MUST in the contract has a named test.",
                        "THIS ARTIFACT WAS BUILT BY CODEX, BOX-FREE (BOSS ruling 2026-09-05 13:25): its report §11 lists "
                        "break-tests the builder DESIGNED but marked NOT RUN, and the ledger row reads "
                        "`MERGED (awaiting AUDIT; builder break-tests NOT RUN — Codex box-free)`. You MUST EXECUTE every one "
                        "of those §11 break-tests yourself on the box — break the guard exactly as §11 describes, paste the red "
                        "summary line, restore, paste the green — before any PASSED verdict; a §11 entry you could not make go red "
                        "is a FAILED audit finding, not a note. Then check every MUST in the contract has a named test.")
            body = tmpl.format(a=a, plan=pref, plan3=pref[:3], iid=iid,
                               deps=", ".join(deps) or "none beyond Plan 010",
                               kids=", ".join(CHILDREN.get(a, [])) or "none", status=st)
            body += "\n\n--- ledger row ---\n" + "\n".join(r["block"][:80])
            item = {"id": iid, "kind": kind, "artifact": a, "title": f"{kind} {a}", "executor": ex,
                    "lane": lane, "worktree": wt if wt.startswith("(") else os.path.join(CN, wt), "deps": deps,
                    "scope": ["platform/**", "portal/**"],
                    "proof_files": [p.replace("platform/", "") for p in r["proof"]],
                    "prompt_file": f"{iid}.md", "status": "queued"}
            new.append((item, body))
    if print_only:
        for item, _ in new:
            print(f"{item['id']:<44} {item['executor']:<7} deps={item['deps']}")
    else:
        for item, body in new:
            with open(os.path.join(D, "items", item["prompt_file"]), "w") as f:
                f.write(body)
        q["items"] += [i for i, _ in new if i["kind"] == "audit"] + [i for i, _ in new if i["kind"] == "build"]
        with open(os.path.join(D, "queue.json.tmp"), "w") as f:
            json.dump(q, f, indent=1)
        os.replace(os.path.join(D, "queue.json.tmp"), os.path.join(D, "queue.json"))
    kinds = Counter(i["kind"] for i, _ in new)
    print(f"{'would add' if print_only else 'added'} {kinds.get('audit', 0)} audit + {kinds.get('build', 0)} build; "
          f"queue {'would be' if print_only else 'now'} {len(q['items']) + (len(new) if print_only else 0)}")
    print("per executor:", dict(Counter(i["executor"] or "any" for i in q["items"] + ([i for i, _ in new] if print_only else []))))
    unsat = sum(1 for i, _ in new if any(not rows.get(d, {}).get("status", "").upper().startswith(("LANDED", "MERGED")) for d in i["deps"]))
    print("items whose deps are not yet LANDED/MERGED (held by the daemon until they are):", unsat)


if __name__ == "__main__":
    main()
