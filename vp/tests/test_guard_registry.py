"""§181 Part A: the guard-mutation registry's five clauses, enforced.

GUARD-MUTATION-REGISTRY-SPEC: "A guard, gate or stub can pass without ever having
been shown to refuse anything. The pass condition must therefore be RECORDED
MUTATION EVIDENCE: a single edit that bypasses the guard, the node it reddened,
and the log that shows the red -- not the existence of a test, and not a sentence
saying a test would fail."

This file is the mechanical half. It never runs a mutation itself (that is
`guard_controls.py run`, which needs minutes per control); it asserts that what
the registry CLAIMS still corresponds to the tree, so a guard cannot be renamed,
rewritten or deleted while its old red keeps vouching for it.

What it deliberately does NOT assert: that the registry covers every guard. A
population discovered by any literal -- a name pattern, a `raise` spelling, a
decorator -- is the same vacuous-guard defect in a new costume. The registry is
authoritative for what it contains and silent about what it omits; completeness
is a review obligation.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import guard_controls as gc  # noqa: E402

REGISTRY = json.loads(gc.REGISTRY.read_text(encoding="utf-8"))
GUARDS = REGISTRY["guards"]
IDS = [g["guard_id"] for g in GUARDS]


def test_the_registry_is_not_empty_and_its_ids_are_unique():
    """An empty registry would satisfy every per-entry clause below vacuously --
    the exact shape this whole spec exists to refuse."""
    assert GUARDS, "the registry is empty: every clause below would pass vacuously"
    assert len(IDS) == len(set(IDS)), "duplicate guard_id: %s" % IDS


@pytest.mark.parametrize("guard", GUARDS, ids=IDS)
def test_clause_1_path_and_symbol_still_resolve_by_ast(guard):
    """SPEC clause 1. A renamed or deleted guard fails LOUDLY rather than
    silently dropping out of the population. AST, never grep: grep cannot tell
    `def open_answers` from the string "open_answers" in a docstring, and a name
    that moved to another class still matches."""
    assert (gc.DISPATCHER / guard["path"]).exists(), "%s: path is gone" % guard["guard_id"]
    seg = gc.resolve_symbol(guard["path"], guard["symbol"])
    assert seg is not None, (
        "%s: %s no longer resolves in %s -- if the guard was renamed, update the registry; "
        "if it was deleted, say so and remove the entry deliberately"
        % (guard["guard_id"], guard["symbol"], guard["path"]))


@pytest.mark.parametrize("guard", GUARDS, ids=IDS)
def test_clause_2_the_guard_source_still_hashes_to_its_recorded_value(guard):
    """SPEC clause 2: a REWRITTEN guard cannot coast on an old red. The hash
    covers the guard's own source segment, not the whole file -- a file hash
    would redden on every unrelated edit and be switched off within a day."""
    now = gc.source_sha256(guard["path"], guard["symbol"])
    assert now is not None, "%s: does not resolve (clause 1 covers why)" % guard["guard_id"]
    assert now == guard["source_sha256"], (
        "%s: %s was rewritten since its controls ran (recorded %s, now %s). Re-run\n"
        "    guard_controls.py run %s\n"
        "and record the new reds; do NOT just update the hash."
        % (guard["guard_id"], guard["symbol"], guard["source_sha256"][:12], now[:12],
           guard["guard_id"]))


@pytest.mark.parametrize("guard", GUARDS, ids=IDS)
def test_clause_3_every_control_node_exists_in_the_collected_suite(guard):
    """SPEC clause 3. A node id that no longer collects proves nothing, and the
    failure is invisible: pytest exits 4 on an unknown node, which a harness
    reading only "did it go red" scores as a successful mutation."""
    for ctl in guard["controls"]:
        node = ctl["node"]
        proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                               "-p", "no:cacheprovider", node],
                              cwd=str(gc.VP), capture_output=True, text=True)
        assert proc.returncode == 0, (
            "%s: node does not collect: %s\n%s" % (guard["guard_id"], node,
                                                   (proc.stdout + proc.stderr)[-700:]))


@pytest.mark.parametrize("guard", GUARDS, ids=IDS)
def test_clause_4_every_control_log_exists_and_is_non_empty_where_the_row_names_it(guard):
    """SPEC clause 4, and it has two halves. WA-03 is why: its six controls were
    genuinely executed -- real pytest summaries, and observed values no
    description could invent (`assert 33792 != 33792`, a backend pid compared
    with itself) -- but the logs sat in `_staging/<packet>/probe/`, which
    placement does not carry. A log in a sibling directory the packaging drops
    satisfies "a log was produced" and fails the only question that matters at
    review: can the reviewer reach it. So the path must resolve INSIDE the
    artifact that ships, which here is `dispatcher/vp/`."""
    for ctl in guard["controls"]:
        log = gc.VP / ctl["log"]
        assert log.exists(), (
            "%s: %s does not exist. If the control ran somewhere else, the evidence does not "
            "ship and cannot be reviewed." % (guard["guard_id"], ctl["log"]))
        assert gc.VP in log.resolve().parents, (
            "%s: %s resolves outside dispatcher/vp -- it would not be carried"
            % (guard["guard_id"], ctl["log"]))
        text = log.read_text(encoding="utf-8", errors="replace")
        assert text.strip(), "%s: %s is empty" % (guard["guard_id"], ctl["log"])
        assert " failed" in text, (
            "%s: %s records no pytest failure line -- a control that did not redden is not "
            "evidence that the guard bites" % (guard["guard_id"], ctl["log"]))
        # "exists on my disk" is NOT "ships". dispatcher/.gitignore carries a
        # blanket `*.log` which silently swallowed every one of these on the
        # first commit attempt -- the WA-03 defect reproduced here, in the very
        # file written to prevent it. An ignored log satisfies every check
        # above and still cannot be reached from a clean clone.
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", str(log)],
                                 cwd=str(gc.DISPATCHER), capture_output=True, text=True)
        assert tracked.returncode == 0, (
            "%s: %s is not tracked by git -- it exists locally and would NOT ship. Check "
            "`git check-ignore -v` on it." % (guard["guard_id"], ctl["log"]))


@pytest.mark.parametrize("guard", [g for g in GUARDS if len(g["controls"]) > 1],
                         ids=[g["guard_id"] for g in GUARDS if len(g["controls"]) > 1])
def test_clause_5_two_controls_on_one_guard_must_fail_differently(guard):
    """SPEC clause 5: where a guard has two or more controls, their `observed`
    messages must DIFFER. A control table where every edit produces the same
    failure has discriminated nothing -- it shows one thing is checked, not that
    each edit is caught. (WA-03's B6 states this rule correctly; only its
    evidence was missing.)"""
    seen = [c["observed"] for c in guard["controls"]]
    assert all(s.strip() for s in seen), "%s: an empty `observed`" % guard["guard_id"]
    assert len(set(seen)) == len(seen), (
        "%s: %d controls but only %d distinct failures -- these edits are not discriminated:\n  %s"
        % (guard["guard_id"], len(seen), len(set(seen)), "\n  ".join(seen)))


@pytest.mark.parametrize("guard", GUARDS, ids=IDS)
def test_each_control_carries_the_fields_the_nightly_re_execution_will_need(guard):
    """Post-launch, the durable check reapplies each edit at current HEAD and
    asserts the same node still reds. That needs the mechanical old/new pair, not
    only the prose `edit` -- a description cannot be reapplied."""
    for ctl in guard["controls"]:
        for field in ("edit", "_edit_old", "_edit_new", "node", "observed", "log", "ts"):
            assert ctl.get(field), "%s: control is missing %r" % (guard["guard_id"], field)
        assert ctl["_edit_old"] != ctl["_edit_new"], "%s: the edit changes nothing" % guard["guard_id"]
        src = (gc.DISPATCHER / guard["path"]).read_text(encoding="utf-8")
        assert src.count(ctl["_edit_old"]) == 1, (
            "%s: `_edit_old` matches %d times in %s -- a mutation must be unambiguous"
            % (guard["guard_id"], src.count(ctl["_edit_old"]), guard["path"]))
