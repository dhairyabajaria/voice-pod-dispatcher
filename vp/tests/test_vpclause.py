"""D164/D165: the typed `— check:` clause classifier."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vpclause as vc  # noqa: E402

EM = "—"


def row(check, gate="CIRCLECI", prose="the shard is green"):
    return "- B7 [invariant] [hosted] %s %s check: %s (gate: %s)." % (prose, EM, check, gate)


def test_the_four_kinds_are_the_whole_closed_set():
    """The set is closed by construction and the test says so out loud, because
    a fifth kind added without a ruling is how a closed set stops being one."""
    assert vc.KINDS == ("nodes", "job-status", "artifact", "external")
    assert vc.CI_ONLY == ("nodes", "job-status", "artifact")
    assert vc.EXTERNAL not in vc.CI_ONLY


def test_a_recognised_kind_parses_its_detail_and_gate():
    c = vc.classify(row("nodes: platform/tests/test_ops.py"))
    assert (c.kind, c.detail, c.gate, c.status) == (
        "nodes", "platform/tests/test_ops.py", "CIRCLECI", vc.OK)
    assert vc.twin_answerable(c) and vc.lint_severity(c) == ""


def test_only_nodes_is_twin_answerable():
    """The whole point of the grammar: one rendered pytest job can show named
    test files green and can answer nothing else."""
    assert vc.twin_answerable(vc.classify(row("nodes: platform/tests/test_ops.py")))
    for kind, detail in (("job-status", "platform-shard, lint-and-typecheck"),
                         ("artifact", "jobs.json")):
        c = vc.classify(row("%s: %s" % (kind, detail)))
        assert c.status == vc.OK and not vc.twin_answerable(c), kind
    c = vc.classify(row("external: deploy/preflight.py on voicepod-vps", gate="DELIVERY-2A"))
    assert c.status == vc.OK and not vc.twin_answerable(c)


def test_absent_and_unrecognised_are_two_vocabularies():
    """Architect 2's named trap, and the reason this module exists rather than a
    regex in each linter.

    Decision 2 says an un-migrated row falls back to prose matching. Decision 3
    says an unknown kind ERRORs. Those are only compatible if the code can tell
    them apart -- otherwise `job-statuss:` reads as "no recognised kind", lands
    in the fallback, and the typo becomes a silent prose match, which is exactly
    what the closed set exists to prevent.

    The discriminator is whether a leading `word:` is present at all, never
    whether it is known."""
    absent = vc.classify(row("job links and status"))
    assert absent.status == vc.ABSENT and absent.kind == ""
    assert vc.lint_severity(absent) == "WARN"

    typo = vc.classify(row("job-statuss: platform-shard"))
    assert typo.status == vc.UNRECOGNISED, (
        "a leading word:-colon that is not in the closed set must NOT fall back")
    assert typo.kind == "", "an unrecognised lead word is never handed back as a kind"
    assert vc.lint_severity(typo) == "ERROR"
    assert "job-statuss" in typo.reason and "closed set" in typo.reason

    assert not vc.twin_answerable(typo), "fail closed: a typo must never be answerable"


def test_a_row_with_no_check_clause_at_all_is_absent_not_an_error():
    c = vc.classify("- B7 [invariant] [hosted] the shard is green (gate: CIRCLECI).")
    assert c.status == vc.ABSENT and vc.lint_severity(c) == "WARN"


def test_the_kind_times_gate_cross_check_is_a_product_of_two_closed_sets():
    """Architect 2's addition. It needs no prose: `nodes`/`job-status`/`artifact`
    are about a CI run and admit only CIRCLECI; `external` admits anything else.

    It would have caught L17-REGISTRY-REPLY-PINS B9 -- "among the required gates
    not green ... (gate: DELIVERY-2A)" -- by DECLARATION rather than by the
    CI-context conjunct D164 currently uses as a heuristic."""
    bad = vc.classify(row("nodes: platform/tests/test_ops.py", gate="DELIVERY-2A"))
    assert bad.status == vc.GATE_MISMATCH and vc.lint_severity(bad) == "ERROR"
    assert not vc.twin_answerable(bad), "fail closed on a contradiction"
    assert "DELIVERY-2A" in bad.reason and "CIRCLECI" in bad.reason

    bad2 = vc.classify(row("external: a deploy transcript", gate="CIRCLECI"))
    assert bad2.status == vc.GATE_MISMATCH

    assert vc.classify(row("external: a deploy transcript", gate="O3")).status == vc.OK
    assert vc.classify(row("job-status: platform-shard")).status == vc.OK


def test_the_cross_check_stays_quiet_when_the_row_declares_no_gate():
    """A missing `(gate: X)` is another linter's finding. Reporting it here too
    would give one defect two owners and two different messages."""
    c = vc.classify("- B7 [invariant] [hosted] prose %s check: nodes: platform/tests/a.py" % EM)
    assert c.status == vc.OK and c.gate == ""


def test_both_separators_in_the_live_corpus_are_accepted():
    """Measured over the 168 hosted rows: an em dash in 152, `--` in 16. A
    parser that took only the em dash would read 16 real rows as ABSENT and
    quietly leave them on the prose matcher forever."""
    assert vc.classify(row("nodes: a.py")).status == vc.OK
    dashed = "- B7 [invariant] [hosted] prose -- check: nodes: a.py (gate: CIRCLECI)."
    assert vc.classify(dashed).status == vc.OK


def test_the_l28_split_classifies_as_the_ruling_says_it_should():
    """The row that started this, after Architect 2's split: B12 keeps its id and
    narrows to the clause a twin CAN answer, and the job-status clause becomes
    the next free number at the end. Both halves classify cleanly and only the
    first is twin-answerable."""
    b12 = ("- B12 [test] [hosted] `platform/tests/test_ops.py` passes on the exact sha "
           "%s check: nodes: platform/tests/test_ops.py (gate: CIRCLECI)." % EM)
    b13 = ("- B13 [invariant] [hosted] the shard, lint and required-gate jobs are green "
           "%s check: job-status: platform-shard, lint-and-typecheck, required-gate "
           "(gate: CIRCLECI)." % EM)
    assert vc.twin_answerable(vc.classify(b12))
    c13 = vc.classify(b13)
    assert c13.status == vc.OK and not vc.twin_answerable(c13)


# The live pack is edited by other sessions while this suite runs, so this number
# moves for two unrelated reasons: rows being MIGRATED (the census actually
# changing, which `absent == hosted` catches) and the pack simply GROWING (a new
# packet, which means nothing). Kept as an explicit named constant so bumping it
# is an obvious one-line edit rather than a hunt through an assertion message.
# 2026-09-21: 169 -> 181 when Architect 2 placed 7 packets + the L28 B12
# amendment (12 new hosted rows). Verified independently with this test's own
# regex before bumping: strict 181, loose 182, the one divergence being the
# L17-REGISTRY-REPLY-PINS B10 `[box]` row whose TEXT contains '[hosted]'.
# 2026-09-21: 181 -> 184 as the pack kept growing (201 packets now). Every row is
# still ABSENT, so this is growth, not migration -- the census has not started
# moving yet. Verified independently with this test's own regex before bumping:
# strict 184, loose 185, the one divergence still being that same
# L17-REGISTRY-REPLY-PINS B10 `[box]` row. Confirmed pre-existing by running
# this test at dispatcher e9fbfe2 (before D194): fails there identically.
# 2026-09-22 (~23:20Z): 184 -> 185, 206 packets. Verified independently with this
# test's own regex first: strict 185, loose 186, sole divergence still that same
# L17-REGISTRY-REPLY-PINS `[box]` row whose TEXT contains '[hosted]'.
# DRIFT RATE, worth knowing before anyone treats a bump as noise: 169 -> 181 -> 184
# -> 185 across one night, i.e. it moves roughly hourly while the pack is being
# written. Every bump so far has been GROWTH (every hosted row still ABSENT), which
# is the benign case this test distinguishes. The day it fails with rows no longer
# ABSENT is the day the census actually started moving -- that one is not a bump.
EXPECTED_HOSTED = 185


def test_every_live_hosted_row_is_absent_today_so_the_census_starts_at_the_whole_corpus():
    """The migration's starting number, asserted rather than assumed: nothing in
    the pack carries a kind yet, so every hosted row is ABSENT/WARN and the prose
    matcher still decides all of them. When this test starts failing, rows have
    been migrated and the census is moving -- update the expectation to the new
    count rather than deleting the test.

    Skipped when the pack is not on this box, so the suite stays runnable
    anywhere."""
    import glob
    import re
    pack = ("/Users/dhairyabajaria/Claude Code/Calling New/voice-pod/advisor-plans/"
            "outbound-launch/v13-pack/03-PACKETS")
    files = sorted(glob.glob(pack + "/*/BENCHMARK.md"))
    if not files:
        import pytest
        pytest.skip("the v13 pack is not on this box")
    row_re = re.compile(r"^\s*-\s+(B\d+)\s+\[(\w+)\]\s+\[(box|hosted)\]\s+(.+)$")
    hosted, absent, errors = 0, 0, []
    for f in files:
        for ln in Path(f).read_text(encoding="utf-8", errors="replace").splitlines():
            m = row_re.match(ln)
            if not m or m.group(3) != "hosted":
                continue
            hosted += 1
            c = vc.classify(ln)
            if c.status == vc.ABSENT:
                absent += 1
            elif vc.lint_severity(c) == "ERROR":
                errors.append((f.split("/")[-2], m.group(1), c.reason))
    # The CENSUS PROPERTY first: it is the one that means something changed
    # substantively. The count tripwire below fires for a second, much duller
    # reason -- the pack simply growing -- and asserting the count first made a
    # new packet look like a migration. (2026-09-21: hosted went 168 -> 169 when
    # R-DB-CREDENTIAL-FILE-CUSTODY was added; every row was still ABSENT, so
    # nothing about the census had moved at all.)
    assert absent == hosted, "%d of %d rows already carry a kind" % (hosted - absent, hosted)
    assert errors == [], errors
    # The expected count is a deliberate tripwire and its remedy is to UPDATE it,
    # not to delete it. The message now says which of the two causes it is, and
    # names the recently-touched packets, so the next person does not have to
    # re-derive that the pack grew.
    if hosted != EXPECTED_HOSTED:
        import os
        recent = sorted(((os.path.getmtime(f), f.split("/")[-2]) for f in files),
                        reverse=True)[:5]
        raise AssertionError(
            "the hosted population is %d, not %d. Every row is still ABSENT, so this is "
            "the pack GROWING, not rows being migrated -- bump EXPECTED_HOSTED. "
            "(A `[box]` row in L17-REGISTRY-REPLY-PINS also contains the string "
            "'[hosted]', which is why a loose grep reads one higher than this test.) "
            "Most recently touched packets: %s"
            % (hosted, EXPECTED_HOSTED, ", ".join(n for _t, n in recent)))
