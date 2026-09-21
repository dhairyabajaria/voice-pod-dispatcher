"""D164/D165: the typed `— check:` clause of a BENCHMARK.md row.

One copy, deliberately. `check-v13.py` already does
`sys.path.insert(0, ".../dispatcher/vp"); import vplint`, so both linters and
the driver read this module rather than each carrying a regex. Two copies of a
classification rule drift, and this one decides whether a scoped twin may answer
a row -- a drift here is a green that proves nothing.

Grammar (Architect 2's ruling, 2026-09-21):

    - B<n> [<class>] [hosted] <prose> — check: <kind>: <detail> (gate: X)

`<kind>` is a CLOSED set. The ruling's load-bearing distinction is that an
ABSENT kind and an UNRECOGNISED kind are two different vocabularies:

  * absent       -- the clause has no `word:` lead at all. Falls back to the
                    prose matching in `lanedriver._twin_scope_only`, and the
                    linters WARN naming the row. This is the migration path for
                    the 168 rows written before the grammar existed.
  * unrecognised -- there IS a `word:` lead and it is not in the closed set.
                    ERROR, and fail closed at dispatch.

Collapsing those two is the whole trap: if `check: job-statuss:` reads as "no
recognised kind" it lands in the fallback and a typo becomes a silent prose
match, which is exactly what the closed set exists to prevent. The precedent is
measured -- v13 proof kinds silently become `platform` when unrecognised, so a
typo yields a valid-looking object of the wrong type. Here a silent fallback is
strictly worse, because it produces a green.

Authoring-time linting genuinely works for this, unlike the cases where a lint
cannot see an edit that has not happened: the row text IS the artifact.
"""
from __future__ import annotations

import re
from collections import namedtuple

#: Twin-answerable. Named test files or node ids the run must show green.
NODES = "nodes"
#: A named CI job's own conclusion. One rendered pytest job can never answer it.
JOB_STATUS = "job-status"
#: A file the run produces -- jobs.json, junit xml, a coverage report.
ARTIFACT = "artifact"
#: A non-CI gate's output -- a deploy transcript, a rehearsal host.
EXTERNAL = "external"

KINDS = (NODES, JOB_STATUS, ARTIFACT, EXTERNAL)

#: The CI gate. `nodes`/`job-status`/`artifact` are about a CI run and admit
#: only this; `external` is about anything but.
CI_GATE = "CIRCLECI"
CI_ONLY = (NODES, JOB_STATUS, ARTIFACT)

OK = "ok"
ABSENT = "absent"
UNRECOGNISED = "unrecognised"
GATE_MISMATCH = "gate-mismatch"

# Both separators occur in the live corpus: measured 2026-09-21 over the 168
# hosted rows, an em dash in 152 and `--` in 16. Every row has exactly one
# `check:` and none has two, so the match is unambiguous.
_CHECK_RE = re.compile(r"(?:—|--)\s*check:\s*(.+)$", re.I)
_KIND_RE = re.compile(r"^\s*([a-z][a-z-]*)\s*:")
_GATE_RE = re.compile(r"\(gate:\s*([A-Za-z0-9-]+)\s*\)")

Clause = namedtuple("Clause", "kind detail gate status reason")


def classify(row_text):
    """-> Clause(kind, detail, gate, status, reason) for one benchmark row.

    `kind` is "" unless `status` is OK or GATE_MISMATCH -- an unrecognised lead
    word is never handed back as if it were a kind.
    """
    text = str(row_text or "")
    gate_m = _GATE_RE.search(text)
    gate = gate_m.group(1) if gate_m else ""

    check = _CHECK_RE.search(text)
    if not check:
        return Clause("", "", gate, ABSENT,
                      "the row has no `-- check:` clause to read a kind from")

    # The gate is its own field, so it comes off the detail rather than being
    # carried twice in two spellings.
    body = _GATE_RE.sub("", check.group(1)).strip().rstrip(".").strip()
    lead = _KIND_RE.match(body)
    if not lead:
        return Clause("", body, gate, ABSENT,
                      "the check clause names no kind (no leading `word:`), so it is "
                      "read by prose matching until it is migrated")

    word = lead.group(1).lower()
    detail = body[lead.end():].strip()
    if word not in KINDS:
        return Clause("", detail, gate, UNRECOGNISED,
                      "unknown check kind %r; the closed set is %s"
                      % (word, ", ".join(KINDS)))

    # The cross-check is a product of two CLOSED sets, not an inference about
    # meaning, so it does not reintroduce the prose-heuristic problem. It does
    # NOT catch nodes-versus-job-status confusion within CI, which stays a
    # review question.
    if gate:
        if word in CI_ONLY and gate != CI_GATE:
            return Clause(word, detail, gate, GATE_MISMATCH,
                          "check kind %r is about a CI run and admits only (gate: %s), "
                          "but this row declares (gate: %s)" % (word, CI_GATE, gate))
        if word == EXTERNAL and gate == CI_GATE:
            return Clause(word, detail, gate, GATE_MISMATCH,
                          "check kind %r is about a non-CI gate, but this row declares "
                          "(gate: %s)" % (word, CI_GATE))
    return Clause(word, detail, gate, OK, "")


def twin_answerable(clause):
    """May a SCOPED TWIN answer this row?

    True only for a clean `nodes:` clause. Every other outcome is False, and
    that includes UNRECOGNISED and GATE_MISMATCH -- failing closed is the point,
    because the alternative is a typo producing a green.

    ABSENT is False here too, but the caller must not read that as a refusal: an
    un-migrated row is decided by the prose matcher, so callers test
    `status == ABSENT` FIRST and fall back. `lint_severity` says the same thing
    in the linters' vocabulary.
    """
    return bool(clause.status == OK and clause.kind == NODES)


def lint_severity(clause):
    """-> ("", "WARN", "ERROR") for a linter reporting this row."""
    if clause.status == OK:
        return ""
    if clause.status == ABSENT:
        return "WARN"
    return "ERROR"
