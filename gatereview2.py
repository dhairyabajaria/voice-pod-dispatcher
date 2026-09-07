#!/usr/bin/env python3
"""Second-opinion merge-gate review: agy over the same candidate diff Codex reviews.

  gatereview2.py <item-id> --diff FILE [--artifact ID] [--out FILE]
  gatereview2.py --selftest                 known-answer check on a planted diff

BOSS decision 2026-09-05 16:15. Two reviewers disagreeing is signal; agy's measured error mode
(a confident false `critical` in the 2026-09-05 bake-off on the aab41585 erasure diff) places it
BESIDE a decision, never as the decision.

ADVISORY ONLY, AND STRUCTURALLY SO. Nothing in this module returns a pass/fail. It returns a
verdict line, a finding list and an error string; `mergegate.rec()` is never called with its
result, so no code path exists by which agy can move the gate's PASS/FAIL. That is a property of
the wiring, not of this docstring — if a later edit gives this a `rec()` call, the property is
gone and the 16:15 ruling is broken.

FAIL-OPEN, like planreview.py and unlike the gate around it. The gate fails CLOSED on Codex
because a missed defect there reaches trunk. A second opinion that cannot be obtained is not a
finding about the code, so an agy that is down, slow, or rate-limited must never turn a clean
candidate red — it reports `agy: unavailable — <reason>` and the gate proceeds on Codex alone.

DISAGREEMENT is the output BOSS actually reads. See `disagreement()`.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

CN = os.environ.get("CN", "/Users/dhairyabajaria/Claude Code/Calling New")
D = os.path.join(CN, "test-logs", "driver")
OFF_SWITCH = os.path.join(D, "GATEREVIEW2")
AGY = os.path.expanduser("~/.local/bin/agy")
MODEL = "gemini-3.8-flash-high"
TIMEOUT_S = 900
MAX_DIFF = 180000

SEVERE = ("high", "critical")

# Diff packing priority. `git diff` emits files in PATH order, so `audit/...` sorts first and a
# naive `diff[:MAX_DIFF]` spends the whole budget on pytest logs and reports. Measured 2026-09-05
# on B.010.member-egress-fence: 238 KB diff, and the 180 KB the reviewer received contained 13 log
# files, 3 reports and the ledger — and NOT ONE of the 7 platform/ files. agy approved it. BOSS saw
# three such approvals against real Codex [high]s before the cause was found. A second opinion that
# reviewed the logs and not the code is not a weak review, it is a review of a different artifact
# wearing an approval. Rank 0 is the code; rank 3 is dropped first.
# 2026-09-06 (BOSS): `plans/` joins audit/ and test-logs/ at the bottom. Measured over 27 agy gate
# reviews, 15 had product files omitted and 4 saw none at all, so those approves were input
# failures rather than model verdicts — the ledger and the plan files are the largest non-code
# things in a candidate diff and they were outranking nothing but the logs.
# 2026-09-07 (BOSS + ★): every NO REVIEW row tonight was a CI item whose diff agy never saw as
# code. PRODUCT_DIRS listed only the three service trees, so deploy/, .circleci/, .github/ and
# scripts/ ranked 1 — above the logs, but below nothing that mattered, and in a diff dominated by
# audit/ they were simply not product, so `0 product files` was literally true and useless.
# Measured over the last 300 trunk commits before adding these, rather than taken from a list:
# audit/ 437, plans/ 72, platform/ 33, docs/ 30, deploy/ 8, portal/ 4, .circleci/ 2, .github/ 2,
# scripts/ 1. The CI code an item ships lives in deploy/, .circleci/, .github/ and scripts/.
# NOTE the correction: BOSS asked for "ci/", which does not exist in this repo — the directory is
# ".circleci/". Adding "ci/" would have matched nothing and the NO REVIEW rows would have persisted
# while looking fixed.
PRODUCT_DIRS = ("platform/", "portal/", "agent/",
                "deploy/", ".circleci/", ".github/", "scripts/", "dispatcher/")
LOWEST_DIRS = ("audit/", "test-logs/", "plans/")


def rank(path):
    if path.startswith(PRODUCT_DIRS):
        return 0
    if path.startswith(LOWEST_DIRS) or path.endswith(".log"):
        return 3
    if path.startswith("docs/"):
        return 2
    return 1


def split_diff(diff_text):
    """-> [(path, chunk)] in git's own order. One chunk per `diff --git` stanza."""
    parts = re.split(r"(?m)^(?=diff --git )", diff_text or "")
    out = []
    for p in parts:
        if not p.strip():
            continue
        m = re.match(r"diff --git a/(\S+) b/(\S+)", p)
        out.append(((m.group(2) if m else "?"), p))
    return out


FULL_HDR = "===== FULL FILE (not a diff): {path} — {n} bytes at the candidate sha =====\n"
FULL_FTR = "===== end of {path} =====\n"


def full_block(path, body):
    """A whole file, labelled as one.

    BOSS's condition, 2026-09-07, and it is not cosmetic: a reviewer handed a full file inside a
    section it believes to be a diff reports UNCHANGED lines as findings, and BOSS then holds a lane
    on a defect that does not exist. The header says what this is, in the reviewer's own reading
    order, and the footer closes it so the next stanza cannot be read as part of this file.
    """
    return FULL_HDR.format(path=path, n=len(body)) + body.rstrip("\n") + "\n" + FULL_FTR.format(path=path)


def upgrade_to_whole(packed, kept, used, budget, whole_file, truncated=()):
    """Swap hunk stanzas for WHOLE files while the total still fits. -> (packed, used, upgraded).

    Measured on 9 real candidates before this was built (133 product files, their live worktrees):
    packing every product file whole costs a median 6.6x its hunks — median 811k against a 180k
    budget, 8 of 9 candidates over. Packing whole files only where they FIT shows 52% of product
    files in full and keeps every candidate inside budget (median 168,752, max 179,735), with the
    rest keeping exactly the hunks they have today. That is the version BOSS approved; the naive one
    was rejected on those numbers.

    Cheapest upgrade first: sorted by the EXTRA bytes each costs, so the budget buys as many whole
    files as it can rather than being spent on the first large one that happens to fit.
    """
    if not whole_file:
        return packed, used, []
    cands = []
    for i, (path, _n) in kept:
        if rank(path) != 0 or path in truncated:
            continue                     # non-product, or already head-truncated at the budget
        try:
            body = whole_file(path)
        except Exception:
            body = None
        if not body:
            continue                     # deleted, binary, unreadable: keep the hunk, say nothing
        block = full_block(path, body)
        cands.append((len(block) - len(packed[i]), i, path, block))
    upgraded = []
    for extra, i, path, block in sorted(cands):
        if used + extra <= budget:
            used += extra
            packed[i] = block
            upgraded.append((path, len(block)))
    return packed, used, upgraded


def pack_diff(diff_text, budget=None, whole_file=None):
    """Fit the diff into `budget` chars, CODE FIRST. -> (packed, kept, dropped, note).

    kept/dropped are [(path, bytes)]. A rank-0 file too big for the remaining budget is included
    HEAD-TRUNCATED rather than dropped, because a partial view of the code beats a complete view
    of the logs. Everything omitted is named in `note`, which goes into the prompt AND the gate
    file — a reviewer that cannot see a file must be told the file exists, or it will report the
    absence as a finding, and a reader of the row must be able to see what was not looked at.
    """
    budget = budget or MAX_DIFF
    chunks = split_diff(diff_text)
    if not chunks:
        return diff_text[:budget], [], [], "", []
    order = sorted(range(len(chunks)), key=lambda i: (rank(chunks[i][0]), i))
    kept, dropped, used, packed = [], [], 0, {}
    for i in order:
        path, chunk = chunks[i]
        if used + len(chunk) <= budget:
            packed[i] = chunk
            kept.append((path, len(chunk)))
            used += len(chunk)
        elif rank(path) == 0 and budget - used > 2000:
            room = budget - used - 200
            packed[i] = chunk[:room] + f"\n[... {len(chunk) - room} bytes of {path} truncated ...]\n"
            kept.append((path, room))
            used = budget
            dropped.append((path + " (TAIL ONLY)", len(chunk) - room))
        else:
            dropped.append((path, len(chunk)))
    # UPGRADE PASS. Everything above is unchanged: this only swaps stanzas that are already IN the
    # pack, so a file that could not be fitted as a hunk cannot appear here, and nothing that fits
    # today is lost.
    truncated = {p.replace(" (TAIL ONLY)", "") for p, _ in dropped if p.endswith(" (TAIL ONLY)")}
    # `kept` is in packing order and `packed` is keyed by ORIGINAL chunk index, so they are paired
    # by path — pairing by position is the kind of implicit alignment that breaks silently the day
    # either list gains an entry.
    by_path = {}
    for i in packed:
        by_path.setdefault(chunks[i][0], i)
    pairs = [(by_path[p], (p, n)) for p, n in kept if p in by_path]
    packed, used, upgraded = upgrade_to_whole(packed, pairs, used, budget, whole_file, truncated)

    out = "".join(packed[i] for i in sorted(packed))
    note = ""
    if dropped:
        note = ("\n\nNOT INCLUDED (the gate dropped these to fit the code in; they are part of the\n"
                "candidate but are NOT shown, so do not report anything about them as missing):\n"
                + "\n".join(f"  {p} ({n} bytes)" for p, n in dropped))
    return out, kept, dropped, note, upgraded

SCHEMA = {
    "type": "object",
    "required": ["verdict", "verdict_line", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "needs-attention", "reject"]},
        # One line, quoted VERBATIM into the gate row. The row is BOSS's first read of this
        # reviewer, so the reviewer writes it rather than the gate paraphrasing it.
        "verdict_line": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "title", "detail"],
                "properties": {
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "file": {"type": "string"},
                    "symbol": {"type": "string"},
                },
            },
        },
    },
}

PROMPT = """You are the SECOND of two independent adversarial reviewers on a merge candidate for a
multi-tenant customer-operations platform. The other reviewer has seen the same diff. You do not
see its verdict and must not try to guess it — an independent read is the entire value you add.

Review the diff below for defects that would matter once this reaches production:
- authority gaps: a privilege, role, or ACL widened; a guard that the production path never calls.
- tenancy leaks: a query, policy, or index missing its org predicate; a cross-org edge made reachable.
- money errors: a wrong amount, sign, rounding, currency, or a charge that can bill twice or zero.
- fail-open paths: a write or guard whose FAILURE lets the caller proceed as if it succeeded.
- unreached rows: a migration or constraint change that leaves pre-existing rows in the old shape.
- overclaimed proof: a test that would pass for a reason other than the guard working.

Rules for your answer:
- Report a finding only for a defect you can point at IN THIS DIFF. Name the file, and the function,
  table, or line. A finding you cannot locate is not a finding.
- Severity is about consequence in production, not about how sure you are. `critical` and `high` are
  for a defect that loses, leaks, or miscounts real data or money. Style, naming, and taste are `low`.
- Do NOT invent a defect to seem useful, and do NOT approve to seem agreeable. An empty findings list
  with `verdict: approve` is a perfectly good answer for a clean diff.
- You are reading a diff, not the whole repository, so context outside the diff is unavailable rather
  than absent. Do not report something as missing when the diff simply does not show it.
- `verdict_line` is ONE line beginning `Verdict:` that a human will read on its own, out of context.

ITEM: {item}
ARTIFACT: {artifact}

--- DIFF ({nbytes} bytes{trunc}) ---
{diff}
--- END DIFF ---
"""


def review(item_id, diff_text, artifact="", whole_file=None):
    """Run agy over a candidate diff. Never raises: an unobtainable review returns error=..."""
    if os.path.exists(OFF_SWITCH):
        return {"skipped": True, "error": f"off-switch {os.path.relpath(OFF_SWITCH, CN)} present"}
    if not os.path.exists(AGY):
        return {"error": f"agy not found at {AGY}"}
    if not (diff_text or "").strip():
        # Reviewing an empty diff would return a confident `approve` about nothing at all.
        return {"error": "candidate diff was empty — nothing to review"}
    full = len(diff_text)
    packed, kept, dropped, note, upgraded = pack_diff(diff_text, whole_file=whole_file)
    had_code = [p for p, _ in split_diff(diff_text) if rank(p) == 0]
    got_code = [p for p, _ in kept if rank(p) == 0]
    if had_code and not got_code:
        # The negative control for this module's worst failure: reviewing only the logs and
        # reporting `approve`. Refuse instead — fail-open means the gate proceeds on Codex, which
        # is strictly better than a fabricated second approval.
        return {"error": f"packing kept no product file out of {len(had_code)} "
                         f"({', '.join(had_code[:3])}...) — refusing to review docs and call it a second opinion"}
    trunc = "" if not dropped else f", {len(dropped)} of {len(kept) + len(dropped)} files omitted"
    prompt = PROMPT.format(item=item_id, artifact=artifact or "-", diff=packed + note,
                           nbytes=len(packed), trunc=trunc)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as sf:
        json.dump(SCHEMA, sf)
        schema_path = sf.name
    try:
        # Headless agy AUTO-DENIES every tool call and then returns an empty verdict (measured
        # 2026-09-05, planreview.py), so the diff must be IN the message — never a path to read.
        proc = subprocess.run(
            [AGY, "-p", "Do NOT use any tools. Everything you need is in this message.\n\n" + prompt,
             "--model", MODEL, "--output-format", "json", "--json-schema", schema_path,
             "--print-timeout", "12m"],
            capture_output=True, text=True, timeout=TIMEOUT_S, cwd=CN,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"agy timed out after {TIMEOUT_S}s"}
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        return {"error": f"agy failed to run: {e}"}
    finally:
        try:
            os.unlink(schema_path)
        except OSError:
            pass
    v = parse(proc.stdout, proc.stderr)
    v["truncated"] = bool(dropped)
    v["kept"] = kept
    v["dropped"] = dropped
    v["full_bytes"] = full
    v["upgraded"] = upgraded
    v["packed_set"] = packed_set(kept, dropped, upgraded)
    return v


def packed_set(kept, dropped, upgraded):
    """What the reviewer ACTUALLY saw, one line per file. BOSS, 2026-09-07: this is what lets a
    verdict be read against its input — "Codex missed it" and "Codex never saw it" are different
    findings and today they are indistinguishable."""
    whole = {p for p, _ in upgraded}
    rows = []
    for p, n in kept:
        rows.append(f"  {'FULL' if p in whole else 'hunk'} {n:>8}  {p}")
    for p, n in dropped:
        rows.append(f"  {'TAIL' if p.endswith('(TAIL ONLY)') else 'NONE'} {n:>8}  {p}")
    return "\n".join(rows)


def parse(stdout, stderr=""):
    """The verdict is in `structured_output` (mirrored in `response`) — NOT `result`, which does
    not exist. Reading the wrong key looks exactly like a model that returned nothing."""
    try:
        d = json.loads(stdout)
    except Exception:
        return {"error": f"agy output was not JSON: {(stderr or stdout)[:300]}"}
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
    if not isinstance(raw, dict) or "verdict" not in raw:
        return {"error": f"agy returned no verdict (status={d.get('status')})"}
    findings = [f for f in raw.get("findings", []) if isinstance(f, dict) and f.get("title")]
    line = (raw.get("verdict_line") or "").strip().split("\n")[0]
    return {"verdict": str(raw.get("verdict", "")).lower(),
            "verdict_line": line or f"Verdict: {raw.get('verdict')}",
            "findings": findings,
            "usage": (d.get("usage") or {}).get("total_tokens")}


def tokens(text):
    """File basenames and identifiers, for deciding whether two findings are about the same thing."""
    out = set()
    for m in re.finditer(r"[\w./-]*\w\.(?:py|sql|ts|tsx)\b", text or ""):
        out.add(os.path.basename(m.group(0)).lower())
    for m in re.finditer(r"\b[a-z_][a-z0-9_]{7,}\b", (text or "").lower()):
        out.add(m.group(0))
    return out


def codex_severe(codex_out):
    """The [high]/[critical] lines from the Codex review, as comparable token sets.

    Codex's output is prose, not structured, so this reads the LINES that carry a severe marker
    rather than pretending to parse findings out of it. A line-level read can over- or
    under-count; that is acceptable because the only thing built on it is an advisory DISAGREE
    label, and it is why `disagreement()` also compares the two verdicts directly.

    2026-09-05: this used the same bare-word scan mergegate did, so Codex's "No substantive merge
    blocker found" produced two phantom [high]s here and a DISAGREE of "0 agy / 2 codex" against a
    candidate BOTH reviewers had approved. The severity vocabulary now lives in ONE place
    (mergegate.severity_hits) so a fix to it cannot land in one scanner and not the other.
    """
    try:
        from mergegate import severity_hits
    except Exception:  # noqa: BLE001 — standalone use without the sibling on the path
        return [ln.strip() for ln in (codex_out or "").split("\n")
                if re.search(r"\[(?:high|critical|blocker|p0|p1)\]", ln, re.I)]
    hits = []
    for ln in (codex_out or "").split("\n"):
        if severity_hits(ln):
            hits.append(ln.strip())
    return hits


def no_review(agy_v):
    """Did this "review" actually read any product code? -> "" if it did, else the reason.

    BOSS + ★, 2026-09-06 23:1x: three of six recent gates printed `Verdict: approve … [reviewed 0
    product file(s)]`. An approve that read no product file is not a weak second opinion, it is a
    verdict about a different artifact wearing an approval — and it was rendered identically to an
    earned one, right next to the finding count, which is where a reader stops.

    review() already REFUSES when the diff HAD product files and the packer kept none. This covers
    the other route to the same row: a candidate whose diff contains no product file at all. Those
    two are NOT the same fact and are not reported as one — "audit/ starved the diff" would be a
    lie about a CI-config-only candidate, and a reader who acts on it would go looking for a packer
    bug that is not there.
    """
    if agy_v.get("error") or agy_v.get("skipped"):
        return ""                       # already reported as unavailable; not a second cause
    kept = agy_v.get("kept") or []
    if any(rank(p) == 0 for p, _ in kept):
        return ""
    dropped = [p for p, _ in (agy_v.get("dropped") or [])
               if rank(p.replace(" (TAIL ONLY)", "")) == 0]
    if dropped:
        return (f"0 product files packed — the diff was starved by lower-ranked files "
                f"({len(dropped)} product file(s) dropped: {', '.join(dropped[:3])})")
    return "0 product files in the candidate diff at all — there was no product code to review"


def disagreement(agy_v, codex_verdict, codex_out, codex_ran=True):
    """-> (bool, reason). Three ways two reviewers disagree, per BOSS 16:15.

    (a) exactly one of them says `approve`;
    (b) one raised a [high]+ finding and the other raised none at all;
    (c) both raised [high]+ findings and they share no file or identifier — two reviewers each
        certain about a different defect is not agreement, it is two unreviewed halves.

    Deliberately NOT a disagreement: differing severity on the same defect, or differing counts of
    low/medium findings. Those are the normal spread between two readers and labelling them
    DISAGREE would make the label mean nothing within a day.
    """
    if agy_v.get("error") or agy_v.get("skipped") or no_review(agy_v):
        # A verdict that read no product code cannot agree or disagree with one that did. Comparing
        # them would let an unearned `approve` cancel a real Codex finding — the disagreement label
        # would go quiet at exactly the moment it is most needed.
        return False, ""
    if not codex_ran or not (codex_out or codex_verdict):
        # A Codex review that did not run is not a Codex review that disagreed. Measured
        # 2026-09-05 on a --no-codex dry run, which printed `DISAGREE: verdicts differ:
        # agy=approve codex=NONE` — a label that fires when there is nothing to compare against
        # is a label BOSS learns to skip past, which costs the real disagreements their signal.
        return False, ""
    a_sev = [f for f in agy_v.get("findings", []) if f.get("severity") in SEVERE]
    c_sev = codex_severe(codex_out)
    a_ok = agy_v.get("verdict") == "approve"
    c_ok = (codex_verdict or "").lower() == "approve"
    if a_ok != c_ok:
        return True, f"verdicts differ: agy={agy_v.get('verdict')} codex={codex_verdict or 'NONE'}"
    if bool(a_sev) != bool(c_sev):
        who = "agy" if a_sev else "codex"
        return True, f"only {who} raised a [high]+ finding ({len(a_sev)} agy / {len(c_sev)} codex)"
    if a_sev and c_sev:
        at = set().union(*(tokens(f"{f.get('file','')} {f.get('symbol','')} {f.get('title','')} "
                                 f"{f.get('detail','')}") for f in a_sev))
        ct = set().union(*(tokens(ln) for ln in c_sev))
        if not (at & ct):
            return True, (f"[high]+ findings do not overlap: agy on {sorted(at)[:4]}, "
                          f"codex on {sorted(ct)[:4]}")
    return False, ""


NEAR_FULL = 0.90        # BOSS's threshold, from the measurement below


def occupancy(agy_v):
    """(sent_bytes, fraction of MAX_DIFF). Counts only what was SENT — never the dropped rows.

    BOSS measured six packs on 2026-09-07 and his FIRST pass summed every manifest row, NONE entries
    included, and produced "1718% occupancy, 95% non-product" — he was about to report the packer as
    catastrophically over budget. The manifest's own header says NONE means dropped; the sizes were
    read without the kinds. So this function exists as much to be the ONE place that arithmetic
    lives as to compute it: a number that cannot distinguish its own causes is the house failure,
    and it caught him on his own data after he had caught it in everyone else's all night.

    The corrected figures: median occupancy 37%, non-product 24% of sent bytes, 7,022,137 bytes
    correctly dropped (backfills-r2 alone carried a 3.3 MB committed log). The packer is behaving —
    with one watch item, which is what NEAR_FULL is for.
    """
    sent = sum(n for _p, n in (agy_v.get("kept") or []))
    return sent, (sent / MAX_DIFF if MAX_DIFF else 0.0)


def pct(occ):
    """Floor, never round. `:.0f` turned 99.7% into "100% of budget", which reads as AT the limit —
    and "at the limit" is the one reading a reviewer would act on differently from "nearly"."""
    return int(occ * 100)


def row(agy_v, disagree_reason="", codex_ran=True):
    """The gate row. Never contributes to PASS/FAIL."""
    if agy_v.get("skipped"):
        return f"agy second opinion: SKIPPED — {agy_v['error']}"
    if agy_v.get("error"):
        return f"agy second opinion: agy: unavailable — {agy_v['error']}"
    nr = no_review(agy_v)
    if nr:
        # The verdict is NOT printed. A reader who sees `approve` has already formed a view before
        # reaching any caveat that follows it.
        return (f"agy second opinion: NO REVIEW — {nr}. The model's answer is discarded: it was "
                f"about the non-product part of the diff. Not compared with Codex. "
                f"— ADVISORY, does not affect PASS/FAIL")
    n = len(agy_v.get("findings", []))
    sev = [f for f in agy_v.get("findings", []) if f.get("severity") in SEVERE]
    r = f"agy second opinion: {agy_v['verdict_line']} ({n} findings"
    if sev:
        r += f", {len(sev)} [high]+"
    r += ")"
    code = [p for p, _ in agy_v.get("kept", []) if rank(p) == 0]
    dropped_code = [p for p, _ in (agy_v.get("dropped") or [])
                    if rank(p.replace(" (TAIL ONLY)", "")) == 0]
    total_code = len(code) + len(dropped_code)
    # "reviewed 3 product file(s)" out of how many? The denominator is the fact BOSS needs, and
    # without it the row reads the same whether coverage was 3/3 or 3/19.
    r += (f" [reviewed {len(code)} of {total_code} product file(s)" if total_code != len(code)
          else f" [reviewed all {len(code)} product file(s)")
    if agy_v.get("dropped"):
        r += f", {len(agy_v['dropped'])} file(s) omitted to fit"
        pm = [p for p, _ in agy_v["dropped"] if rank(p.replace(" (TAIL ONLY)", "")) == 0]
        if pm:   # a count alone reads as "logs dropped"; product omission is a different fact
            r += f" INCLUDING {len(pm)} PRODUCT FILE(S): " + ", ".join(pm[:3])
    sent, occ = occupancy(agy_v)
    r += f", pack {pct(occ)}% of budget"
    r += "]"
    if occ >= NEAR_FULL:
        # BOSS, 2026-09-07, on backfills-r2 at 99.7% — the lane carrying the [high] he ruled on:
        # "it is one commit away from the reviewer silently losing files". The coverage line above
        # is the only thing between a truncated pack and a confident approve on a partial read, and
        # a pack that is FULL but not yet OVER prints exactly the same coverage line as a roomy one.
        r += (f" — **PACK NEAR FULL: {sent:,} of {MAX_DIFF:,} chars ({pct(occ)}%)**. Nothing "
              f"was lost from THIS review, but the next commit on this lane will start dropping "
              f"files, and a dropped file reads as an approve of code nobody saw. Check the coverage "
              f"count above before trusting this verdict, and split the candidate if it grows.")
    if disagree_reason:
        r += f" — DISAGREE: {disagree_reason}"
    elif not codex_ran:
        r += " — no comparison: the Codex review did not run"
    return r + " — ADVISORY, does not affect PASS/FAIL"


def input_header(agy_v):
    """What the reviewer was ACTUALLY given, at the top of the file, above the verdict.

    2026-09-06 (BOSS): the omitted list existed but sat below the verdict, so a reader met the
    approval first and the input second. 15 of 27 agy reviews had product files omitted and 4 saw
    none; an approve from a review that could not see the code is an input failure and has to be
    legible as one BEFORE the verdict is read. A review that saw everything says so in one line.
    """
    kept, dropped = agy_v.get("kept", []), agy_v.get("dropped", [])
    if not kept and not dropped:
        return []
    prod_missing = [(p, n) for p, n in dropped if rank(p.replace(" (TAIL ONLY)", "")) == 0]
    nprod = len([p for p, _ in kept if rank(p) == 0])
    sent, occ = occupancy(agy_v)
    L = [f"input: {len(kept)} of {len(kept) + len(dropped)} file(s) shown "
         f"({nprod} product) out of {agy_v.get('full_bytes')} bytes of candidate diff. "
         f"Pack: {sent:,} of {MAX_DIFF:,} chars, {pct(occ)}% of budget.", ""]
    if occ >= NEAR_FULL and not prod_missing:
        # Above the verdict, like the omission warning beside it: a full pack is not yet a failed
        # one, and the reader has to meet that fact before forming a view rather than after.
        L += [f"**PACK NEAR FULL ({pct(occ)}%) — nothing was dropped from this review, but "
              f"this lane is one commit from silent omission.**", ""]
    if prod_missing:
        L += ["**PRODUCT FILES WERE OMITTED FROM THIS REVIEW — treat any verdict below as an input "
              "failure, not a model verdict:**"] + [f"- `{p}` ({n} bytes unseen)" for p, n in prod_missing] + [""]
    if dropped:
        L += ["Omitted to fit (named to the reviewer so it would not report them as missing): "
              + ", ".join(f"`{p}`" for p, _ in dropped), ""]
    else:
        L += ["Nothing was omitted: the reviewer saw the whole candidate diff.", ""]
    return L


def render(item_id, agy_v, codex_verdict="", codex_out="", disagree_reason=""):
    L = [f"# AGY SECOND OPINION — {item_id}", "",
         f"model: `{MODEL}`  ·  ADVISORY ONLY: this review cannot change the gate's PASS/FAIL.", ""]
    L += input_header(agy_v)
    if agy_v.get("skipped") or agy_v.get("error"):
        L += ["## NOT OBTAINED", "", f"`{agy_v['error']}`", "",
              "Fail-open by contract: the gate proceeds on the Codex review alone.", ""]
        return "\n".join(L + ["## GATE ROW", "", "```", row(agy_v, disagree_reason), "```", ""])
    L += ["## VERDICT", "", f"> {agy_v['verdict_line']}", "",
          f"- verdict: `{agy_v.get('verdict')}`",
          f"- codex verdict: `{codex_verdict or 'NONE'}`",
          f"- findings: {len(agy_v.get('findings', []))}", ""]
    kept, dropped = agy_v.get("kept", []), agy_v.get("dropped", [])
    L += [f"### What this review actually saw ({agy_v.get('full_bytes')} bytes of candidate diff)", "",
          "Product files reviewed (`platform/` `portal/` `agent/`):"]
    L += [f"- `{p}` ({n} bytes)" for p, n in kept if rank(p) == 0] or \
         ["- **NONE — this diff changed no product file.**"]
    if [p for p, _ in kept if rank(p)]:
        L += ["", "Also included: " + ", ".join(f"`{p}`" for p, _ in kept if rank(p))]
    if dropped:
        L += ["", "**Omitted to fit the code in** (named to the reviewer so it would not report "
              "them as missing):"] + [f"- `{p}` ({n} bytes)" for p, n in dropped]
    L += ["",
          f"- **DISAGREE**: {disagree_reason}" if disagree_reason else "- reviewers agree", ""]
    if agy_v.get("findings"):
        L += ["## FINDINGS", ""]
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        for i, f in enumerate(sorted(agy_v["findings"], key=lambda x: order.get(x.get("severity"), 4)), 1):
            L += [f"### {i}. [{f.get('severity')}] {f.get('title')}",
                  f"- where: `{f.get('file') or '?'}`" + (f" · `{f.get('symbol')}`" if f.get("symbol") else ""),
                  f"- {f.get('detail', '').strip()}", ""]
    if codex_out:
        sev = codex_severe(codex_out)
        L += ["## CODEX [high]+ LINES (for the overlap comparison)", ""]
        L += [f"- `{ln[:200]}`" for ln in sev[:12]] or ["- none"]
        L.append("")
    L += ["## GATE ROW", "", "```", row(agy_v, disagree_reason), "```", ""]
    return "\n".join(L)


# --- calibration -------------------------------------------------------------------------------
SELFTEST_DIFF = """diff --git a/platform/core/billing.py b/platform/core/billing.py
--- a/platform/core/billing.py
+++ b/platform/core/billing.py
@@ -40,7 +40,14 @@ def settle(conn, org_id, minutes):
-    rows = conn.execute("SELECT rate FROM org_rates WHERE org_id = %s", (org_id,)).fetchall()
+    # cache the rate table across orgs so settlement is not a per-org round trip
+    rows = conn.execute("SELECT rate FROM org_rates").fetchall()
     rate = rows[0]["rate"]
-    charge = round(minutes * rate, 2)
+    charge = round(minutes * rate)
     try:
         conn.execute("INSERT INTO charges (org_id, amount) VALUES (%s, %s)", (org_id, charge))
-    except Exception:
-        raise
+    except Exception:
+        pass  # settlement is best-effort; the reconciler will pick it up
     return charge
"""


def selftest_pack():
    """Hermetic (no model call): the packer must keep the CODE when the budget cannot hold the diff.

    Regression control for the 2026-09-05 defect where `diff[:MAX_DIFF]` handed the reviewer 13
    pytest logs and 3 reports and zero product files, and it approved. Runs before the agy arm so
    it costs nothing and fails fast."""
    ok = True

    def stanza(path, n):
        return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n" + ("+x\n" * (n // 3))

    # git emits in PATH order, so audit/ sorts first and would eat a naive window whole.
    diff = (stanza("audit/plan-execution-2026-09-04/logs/a.log", 90000)
            + stanza("audit/plan-execution-2026-09-04/reports/r.md", 90000)
            + stanza("plans/EXECUTION_LEDGER.md", 5000)
            + stanza("platform/core/billing.py", 6000)
            + stanza("platform/tests/test_billing.py", 6000))
    naive = [p for p, _ in split_diff(diff[:MAX_DIFF])]
    packed, kept, dropped, note, _up = pack_diff(diff, budget=120000)
    code = [p for p, _ in kept if rank(p) == 0]
    print(f"MUST-BITE  [packing] naive diff[:MAX_DIFF] would review: {naive}")
    print(f"  packed keeps product files: {code}")
    print(f"  dropped: {[p for p, _ in dropped]}")
    ok &= sorted(code) == ["platform/core/billing.py", "platform/tests/test_billing.py"]
    ok &= all(rank(p) != 0 for p, _ in dropped)
    ok &= "NOT INCLUDED" in note and "audit/" in note
    print(f"  -> packing {'PASS' if ok else 'FAIL'} (all product kept, only non-product dropped, note names them)")

    # plans/ ranks with audit/, below docs/ (BOSS 2026-09-06).
    ranks = {p: rank(p) for p in ("platform/core/x.py", "docs/TECHNICAL.md",
                                  "plans/EXECUTION_LEDGER.md", "audit/x/logs/a.log", "README.md")}
    print(f"MUST-BITE  [rank] {ranks}")
    rk = ranks["plans/EXECUTION_LEDGER.md"] == ranks["audit/x/logs/a.log"] == 3 \
        and ranks["docs/TECHNICAL.md"] == 2 and ranks["platform/core/x.py"] == 0
    ok &= rk
    print(f"  -> plans/ ranks with audit/, below docs/: {'PASS' if rk else 'FAIL'}")

    # The header must announce an omitted PRODUCT file above the verdict, and say so plainly when
    # nothing was omitted. A header that is silent either way is the defect this fixes.
    big = stanza("platform/core/huge.py", 200000) + stanza("platform/core/small.py", 1000)
    _, k3, d3, _, _ = pack_diff(big, budget=60000)
    h_bad = "\n".join(input_header({"kept": k3, "dropped": d3, "full_bytes": len(big)}))
    h_ok = "\n".join(input_header({"kept": [("platform/core/small.py", 1000)], "dropped": [], "full_bytes": 1000}))
    hdr = ("PRODUCT FILES WERE OMITTED" in h_bad) and ("PRODUCT FILES WERE OMITTED" not in h_ok) \
        and ("Nothing was omitted" in h_ok)
    print(f"MUST-BITE  [header] product omission announced={('PRODUCT FILES WERE OMITTED' in h_bad)}, "
          f"clean review says so={('Nothing was omitted' in h_ok)}")
    ok &= hdr
    print(f"  -> input header {'PASS' if hdr else 'FAIL'}")
    r_bad = row({"verdict_line": "approve", "findings": [], "kept": k3, "dropped": d3})
    print(f"  gate row: {r_bad[:160]}")
    ok &= "PRODUCT FILE(S)" in r_bad

    # The refuse-to-review backstop: no product file could be fitted at all.
    tiny, k2, _, _, _ = pack_diff(diff, budget=500)
    got = [p for p, _ in k2 if rank(p) == 0]
    print(f"MUST-BITE  [refuse] budget=500 keeps product files {got} -> review() must refuse rather than approve docs")
    ok &= not got
    print(f"  -> refuse-guard input {'PASS' if not got else 'FAIL'}")
    return ok


def selftest():
    """Known-answer check. This diff drops an org predicate (tenancy), rounds money to whole units
    (money), and swallows the INSERT failure while still returning a charge (fail-open). A reviewer
    that approves it, or that returns no verdict at all, is not worth running at a merge gate."""
    pack_ok = selftest_pack()
    print()
    v = review("SELFTEST", SELFTEST_DIFF, "selftest.billing")
    print(json.dumps({k: v[k] for k in v if k != "findings"}, indent=2))
    for f in v.get("findings", []):
        print(f"  [{f.get('severity')}] {f.get('title')} — {f.get('file')}")
    if v.get("error"):
        print(f"\nSELFTEST INCONCLUSIVE — review unobtainable: {v['error']}")
        return 2
    blob = " ".join(f"{f.get('title')} {f.get('detail')} {f.get('file')}" for f in v.get("findings", []))
    hits = {k for k, pat in (("tenancy", r"org_id|tenan|cross-org"),
                             ("money", r"round|amount|charge|cent|decimal"),
                             ("fail-open", r"except|pass|swallow|fail.?open|silent"))
            if re.search(pat, blob, re.I)}
    print(f"\nplanted defects found: {sorted(hits)}")
    ok = v.get("verdict") != "approve" and len(hits) >= 2
    # A DISAGREE control: the same verdict read against a Codex that approved must flag.
    dis, why = disagreement(v, "approve", "no material blockers found")
    print(f"disagree-vs-approving-codex: {dis} ({why})")
    print("\nSELFTEST " + ("PASS" if ok and dis and pack_ok else "FAIL"))
    return 0 if (ok and dis and pack_ok) else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("item")
    ap.add_argument("--diff", required=True)
    ap.add_argument("--artifact", default="")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    v = review(a.item, open(a.diff, errors="ignore").read(), a.artifact)
    out = a.out or os.path.join(D, "gates", f"{a.item}.agy.txt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w").write(render(a.item, v))
    print(row(v))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
