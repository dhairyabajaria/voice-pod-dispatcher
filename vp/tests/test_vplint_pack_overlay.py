"""D85: lint_packet layers the pack's own check-v13.py when packets_dir carries one.

The Architect placed R-TEST-PG-WAL-CAP (§47) after adding a standing
Prohibitions line that check-v13.py requires and vplint.lint_packet alone did
not -- two linters, one packet, two answers.  Now the pack's extra rules run
inside lint_packet; the pack's file stays the only copy of them."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import vplint  # noqa: E402

PACKET = """---
item: X-ONE
title: t
group: 1
base_sha: 5deac821fbb129aabc149af95f729a1f8049638a
depends_on: []
releases: []
critical: false
owned_files:
  - a.py
forbidden_files: []
test_paths:
  - tests/test_a.py
proof_kind: platform
max_rounds: 2
---
## Goal
g
## Not in scope
n
## Witnesses
- `a.py:1` -- x
## Steps
1. s
## Prohibitions
- nothing else
"""
BENCH = """# X-ONE benchmark

- B1 [invariant] [box] a. check: `grep -n x a.py` prints one line.
- B2 [test] [box] `tests/test_a.py` green. check: `pytest tests/test_a.py -q` passes.
- B3 [negative] [box] r. check: reverted, RED.
- B4 [forbidden] [box] f. check: `git diff --stat base..HEAD -- b` empty.
- B5 [evidence] [box] e. check: notes carry it.
"""
FAKE_CHECK = '''
def check_dir(d):
    txt = (d / "PACKET.md").read_text()
    out = []
    if "STANDING-LINE" not in txt:
        out.append("ERROR body: Prohibitions must contain the standing line 'STANDING-LINE'")
    out.append("note: informational, dropped")
    return out
'''


def _packet(tmp_path, text=PACKET):
    d = tmp_path / "X-ONE"
    d.mkdir(parents=True)
    (d / "PACKET.md").write_text(text)
    (d / "BENCHMARK.md").write_text(BENCH)
    return d


def test_without_a_pack_checker_lint_packet_is_unchanged(tmp_path):
    d = _packet(tmp_path)
    packs = tmp_path / "packs"
    packs.mkdir()
    assert vplint.lint_packet(d / "PACKET.md", d / "BENCHMARK.md", packets_dir=str(packs)) == []
    assert vplint.lint_packet(d / "PACKET.md", d / "BENCHMARK.md") == []


def test_the_pack_checker_runs_inside_lint_packet_and_only_its_findings_join(tmp_path):
    d = _packet(tmp_path)
    packs = tmp_path / "packs"
    packs.mkdir()
    (packs / vplint.PACK_CHECK).write_text(FAKE_CHECK)
    out = vplint.lint_packet(d / "PACKET.md", d / "BENCHMARK.md", packets_dir=str(packs))
    assert out == ["ERROR body: Prohibitions must contain the standing line 'STANDING-LINE'"]
    fixed = _packet(tmp_path / "ok", PACKET.replace("- nothing else", "- STANDING-LINE\n- nothing else"))
    assert vplint.lint_packet(fixed / "PACKET.md", fixed / "BENCHMARK.md", packets_dir=str(packs)) == []


def test_a_broken_pack_checker_is_a_warning_not_silence(tmp_path):
    d = _packet(tmp_path)
    packs = tmp_path / "packs"
    packs.mkdir()
    (packs / vplint.PACK_CHECK).write_text("def check_dir(d):\n    raise RuntimeError('boom')\n")
    out = vplint.lint_packet(d / "PACKET.md", d / "BENCHMARK.md", packets_dir=str(packs))
    assert out == ["WARN pack: check-v13.py could not run: boom"]


def test_the_live_pack_checker_requires_the_standing_line():
    """the real check-v13.py, on a packet lacking the line it demands"""
    packs = Path("/Users/dhairyabajaria/Claude Code/Calling New/voice-pod/advisor-plans/outbound-launch/v13-pack/03-PACKETS")
    if not (packs / vplint.PACK_CHECK).is_file():
        import pytest
        pytest.skip("live pack not checked out")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        d = _packet(Path(tmp), PACKET.replace("max_rounds: 2\n", "max_rounds: 2\nv13_kind: repair\nscheduler_task: NEW:REPAIR\n"
                                              "closes: []\nrunner_role: builder\nhosted_owed: false\nowner_gate: none\n"))
        out = vplint.lint_packet(d / "PACKET.md", d / "BENCHMARK.md", packets_dir=str(packs))
        assert any("One shell command per bash call" in o for o in out), out


def test_d139_an_unknown_header_key_is_rejected_instead_of_silently_ignored(tmp_path):
    """D139: the packet header had no closed vocabulary, so a misspelled key
    parsed cleanly, was read by nobody, and left the packet on the default.

    That is a fail-open on a field whose only purpose is to OVERRIDE a default.
    `twin_scope: full` is load-bearing -- it forces an unscoped hosted twin, and
    R-SEC-CALLERS-AND-SEED-ROUTE needed it to answer a [hosted] row at all. A
    typo in it does not fail loudly; it silently restores the scoped twin the
    header was written to refuse, and the packet burns its round budget on a row
    it cannot answer.

    The baseline assertion is the control: the fixture must lint clean, or a
    vocabulary that rejects everything would pass this test for the wrong
    reason."""
    good = _packet(tmp_path / "good")
    assert not [ln for ln in vplint.lint_packet(str(good / "PACKET.md"), str(good / "BENCHMARK.md"))
                if "unknown key" in ln], "the unmutated fixture must be clean"

    for spelling in ("twin_scopes", "twn_scope", "proof_scope"):
        d = _packet(tmp_path / spelling, PACKET.replace("proof_kind: platform",
                                                        "%s: full\nproof_kind: platform" % spelling))
        out = vplint.lint_packet(str(d / "PACKET.md"), str(d / "BENCHMARK.md"))
        assert any("unknown key" in ln and spelling in ln for ln in out), (
            "%s is read by no code and must be rejected, not ignored: %r" % (spelling, out))


def test_d139_twin_scope_must_carry_a_value_lanedriver_actually_acts_on(tmp_path):
    """D139: `lanedriver._twin_scope_only` (:1542) tests
    `str(hdr.get("twin_scope") or "").strip().lower() == "full"` and falls
    through to the SCOPED twin on anything else. So every spelling but "full" is
    a silent no-op, and a wrong value is indistinguishable from omitting the key.

    "full" itself must stay accepted -- a check that rejected the one working
    value would be worse than no check at all."""
    ok = _packet(tmp_path / "full", PACKET.replace("proof_kind: platform",
                                                   "twin_scope: full\nproof_kind: platform"))
    assert not [ln for ln in vplint.lint_packet(str(ok / "PACKET.md"), str(ok / "BENCHMARK.md"))
                if "twin_scope" in ln], "twin_scope: full is the working value and must pass"

    for bad in ("ful", "FULL-SUITE", "true", "yes"):
        d = _packet(tmp_path / ("bad-" + bad), PACKET.replace(
            "proof_kind: platform", "twin_scope: %s\nproof_kind: platform" % bad))
        out = vplint.lint_packet(str(d / "PACKET.md"), str(d / "BENCHMARK.md"))
        assert any("twin_scope" in ln and "is not one of" in ln for ln in out), (
            "twin_scope: %s does nothing in lanedriver and must be rejected: %r" % (bad, out))
