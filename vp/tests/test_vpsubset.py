"""D176 sidecar generator: it must be DERIVED, reproducible, and fail closed.

Architect 2's ruling (2026-09-21): sealing proves INTEGRITY (unchanged since
sealed), not VALIDITY (right when sealed). A hand-authored sidecar, sealed, would
be WORSE than an unsealed one -- it would carry the sealed chain's authority
while still being somebody's unverifiable judgement call.
"""
import json
from pathlib import Path

from vp import vpsubset


def _rec(tmp, name, **kw):
    d = dict(kw)
    d.setdefault("proof_id", name)
    p = tmp / "proofs" / ("%s.json" % name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2, sort_keys=True), encoding="utf-8")
    return p


def _deriver(tmp, cmds=None):
    d = vpsubset.Deriver(tmp, "/nonexistent-trunk")
    if cmds is not None:
        d.workflows = {c: {"sha256": "x" * 64, "cmd": v} for c, v in cmds.items()}
    return d


def test_the_sidecar_is_byte_identical_across_derivations(tmp_path):
    """No timestamp, by design. A `generated_at` would make every run differ and
    destroy the one property that lets a reader CHECK the mapping instead of
    trusting it -- which is the whole reason the artifact is derived."""
    _rec(tmp_path, "proof-A", only=None, sha="a" * 40, status="PASS")
    _rec(tmp_path, "proof-B", sha="b" * 40, status="PASS")

    one = vpsubset.Deriver.dumps(_deriver(tmp_path).build())
    two = vpsubset.Deriver.dumps(_deriver(tmp_path).build())
    assert one == two
    assert "generated_at" not in one, "a timestamp makes the output unverifiable"


def test_it_records_what_it_read_so_a_reader_can_verify_rather_than_trust(tmp_path):
    """The generator's own sha and every input record's sha travel with the
    verdicts. Without them the sidecar asserts a mapping nobody can check."""
    _rec(tmp_path, "proof-A", only=None, sha="a" * 40)
    doc = _deriver(tmp_path).build()

    assert doc["generator"]["name"] == "vp/vpsubset.py"
    assert len(doc["generator"]["sha256"]) == 64
    for e in doc["entries"].values():
        assert len(e["record_sha256"]) == 64
    assert "RECORDS WIN" in doc["authority"], (
        "the artifact must state that it is derived, not a second source of truth")


def test_an_unresolvable_record_is_never_guessed_alive(tmp_path):
    """The third state exists so the generator is never under pressure to guess
    a verdict to keep a record citable. Both refusal reasons stay distinct."""
    _rec(tmp_path, "proof-nokey", sha="a" * 40)                    # no `only` key at all
    _rec(tmp_path, "proof-null", only=None, sha="b" * 40)          # `only` present but null
    ent = _deriver(tmp_path).build()["entries"]

    v = {Path(k).stem: e for k, e in ent.items()}
    assert v["proof-nokey"]["subset_verdict"] == "unresolvable"
    assert v["proof-null"]["subset_verdict"] == "unresolvable"
    # the two refusals are distinguishable forever, which is why the reasons differ
    assert v["proof-nokey"]["basis"] != v["proof-null"]["basis"]
    for e in ent.values():
        assert e["subset_verdict"] not in vpsubset.CITABLE


def test_the_renderer_is_not_exempt_from_the_executed_command_check(tmp_path):
    """The record that RENDERED the overlay is checked like every other record on
    the job. An earlier rule trusted "it rendered, therefore it ran its own ask",
    which is an assumption rather than a check and silently made 34 records
    citable."""
    job = [{"job_number": 1, "name": "vp/platform-twin-1"}]
    _rec(tmp_path, "proof-renderer", only="twin:3:platform/tests/test_x.py",
         sha="a" * 40, measured_commit="c" * 40, jobs=job)
    _rec(tmp_path, "proof-adopter", only="twin:3:platform/tests/test_y.py",
         sha="a" * 40, measured_commit="a" * 40, jobs=job)

    # the overlay ran ONLY test_x.py -- so the renderer is covered and the
    # adopter is not; the renderer's own ask is still checked, not assumed
    ent = _deriver(tmp_path, {"c" * 40: "pytest -q -n 3 tests/test_x.py"}).build()["entries"]
    v = {Path(k).stem: e["subset_verdict"] for k, e in ent.items()}
    assert v["proof-renderer"] == "covered", v
    assert v["proof-adopter"] == "not_covered", v

    # and when the overlay did NOT run the renderer's own file, the renderer
    # must be refused too -- this is the case the exemption used to hide
    ent2 = _deriver(tmp_path, {"c" * 40: "pytest -q -n 3 tests/test_z.py"}).build()["entries"]
    v2 = {Path(k).stem: e["subset_verdict"] for k, e in ent2.items()}
    assert v2["proof-renderer"] == "not_covered", v2


def test_a_record_the_writer_already_stamped_is_never_overridden(tmp_path):
    """laneproof decides the field at the moment it knows what it ran. The
    generator only fills in records written before that existed; it must never
    overwrite a write-time verdict with a re-derived one."""
    _rec(tmp_path, "proof-live", only="twin:3:platform/tests/test_x.py",
         sha="a" * 40, subset_verdict="full")
    ent = _deriver(tmp_path).build()["entries"]
    e = list(ent.values())[0]
    assert e["subset_verdict"] == "full"
    assert "laneproof" in e["basis"]


def test_the_precondition_control_aborts_when_it_cannot_fail(tmp_path):
    """A control that finds ZERO known-bad records proves nothing -- "0 leaked"
    is exactly what an instrument incapable of the verdict reports. It must abort
    rather than report success."""
    _rec(tmp_path, "proof-A", only=None, sha="a" * 40)
    d = _deriver(tmp_path)
    recs = d.records()
    doc = d.build()
    n, leaked = vpsubset.control_known_bad(d, recs, d.overlay_commits(recs), doc["entries"])
    assert n == 0 and not leaked

    rc = vpsubset.main(["--run-root", str(tmp_path), "--trunk", "/nonexistent-trunk"])
    assert rc == 1, "a control that cannot fail must abort the run"


def test_the_header_lets_a_reader_regenerate_and_pin_the_corpus(tmp_path):
    """Architect 2's condition: the output must self-declare as derived -- name
    the generator, the regenerate command, and the input tip it came from, so a
    reader knows in one line it is a snapshot and not a source.

    The input tip matters more here than usual: the run root is NOT a git repo,
    so the input set has no commit of its own to cite. Without a digest there is
    no way to say WHICH corpus state a verdict came from."""
    _rec(tmp_path, "proof-A", only=None, sha="a" * 40)
    _rec(tmp_path, "proof-B", sha="b" * 40)
    doc = _deriver(tmp_path).build()

    assert "vpsubset.py" in doc["regenerate"] and "--run-root" in doc["regenerate"]
    assert "--verify" in doc["regenerate"], "a reader must be told how to CHECK, not only rewrite"
    assert doc["input_tip"]["records"] == 2
    assert len(doc["input_tip"]["digest"]) == 64


def test_the_input_tip_moves_when_the_corpus_moves_and_not_otherwise(tmp_path):
    """A tip that does not change when the inputs change pins nothing, and one
    that changes on re-derivation cannot be compared across runs. Both halves are
    asserted -- the second is the one a `generated_at` would have broken."""
    _rec(tmp_path, "proof-A", only=None, sha="a" * 40)
    first = _deriver(tmp_path).build()["input_tip"]
    assert _deriver(tmp_path).build()["input_tip"] == first, "unstable under re-derivation"

    _rec(tmp_path, "proof-B", sha="b" * 40)                       # a record ADDED
    after_add = _deriver(tmp_path).build()["input_tip"]
    assert after_add != first and after_add["records"] == 2

    _rec(tmp_path, "proof-B", sha="c" * 40)                       # a record ALTERED
    after_edit = _deriver(tmp_path).build()["input_tip"]
    assert after_edit != after_add, "an edited record must move the tip"
    assert after_edit["records"] == 2, "an edit is not an addition"
