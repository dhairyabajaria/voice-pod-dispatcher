#!/usr/bin/env python3
"""tests/test_vpschema.py -- the D10 RESULT.json rule, by category:

  binding fields  (item, attempt, commit, base, blocked): strict meaning,
                  lenient representation
  informational   (diff_stat, checks, disputes, notes): recognisable container
                  shape, any scalar leaf, number-like coercion, defaults
  checks          not executed  <=>  exit is not an exit code; then a reason
                  is mandatory; every check must be identifiable

Category tests generate arbitrary representations rather than the literal
shapes seen in one run."""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import vpschema  # noqa: E402

SHA = "a" * 40


def good(**over):
    r = {"item": "L00", "attempt": 1, "commit": SHA, "base": "b" * 40,
         "diff_stat": {"files": 1, "insertions": 1, "deletions": 0},
         "checks": [{"name": "ruff", "command": "ruff check x.py", "exit": 0, "log": "All checks passed!"},
                    {"name": "collect", "command": "uv run pytest --collect-only -q t.py", "exit": 0, "log": "ok"}],
         "disputes": [], "blocked": None, "notes": "n"}
    r.update(over)
    return r


def ok(r):
    valid, errs = vpschema.validate_result_obj(r)
    assert valid, errs
    canon, _, warnings = vpschema.normalize_result(r)
    return canon, warnings


def bad(r, where):
    valid, errs = vpschema.validate_result_obj(r)
    assert not valid and any(e.startswith(where) for e in errs), (where, errs)
    return errs


# -- binding -------------------------------------------------------------------------------

def test_binding_keys_are_required_and_strict_in_meaning():
    for key in ("item", "attempt", "commit", "base"):
        r = good()
        del r[key]
        bad(r, key)
    bad(good(item=""), "item")
    bad(good(commit="not-a-sha"), "commit")
    bad(good(base="xyz"), "base")
    bad(good(attempt="one"), "attempt")
    bad(good(attempt=True), "attempt")


def test_binding_representation_is_lenient():
    canon, _ = ok(good(attempt="7"))
    assert canon["attempt"] == 7
    ok(good(commit="ABCDEF1"))                              # 7-40 hex, any case
    for v in (None, False, ""):
        canon, _ = ok(good(blocked=v))
        assert canon["blocked"] is None
    for v in ("needs DB", 1, True):
        canon, _ = ok(good(blocked=v))
        assert canon["blocked"] == str(v)
    bad(good(blocked={"why": "x"}), "blocked")
    r = good()
    del r["blocked"]
    canon, warnings = ok(r)
    assert canon["blocked"] is None and any(w.startswith("blocked") for w in warnings)


# -- informational: numbers --------------------------------------------------------------

NUMBER_LIKE = [(3, 3), (3.0, 3), ("3", 3), (" 3 ", 3), ("+3", 3), ("1,024", 1024),
               (["a.py", "b.py", "c.py"], 3), ((), 0)]
NOT_NUMBER_LIKE = [True, "three", "3.5", 2.5, {"n": 3}, None, ""]


@pytest.mark.parametrize("val,expect", NUMBER_LIKE)
def test_int_like_accepts_every_number_like_representation(val, expect):
    assert vpschema.int_like(val) == expect


@pytest.mark.parametrize("val", NOT_NUMBER_LIKE)
def test_int_like_rejects_everything_else(val):
    assert vpschema.int_like(val) is None


@pytest.mark.parametrize("key", ["files", "insertions", "deletions"])
@pytest.mark.parametrize("val,expect", NUMBER_LIKE)
def test_diff_stat_leaves_coerce_from_any_number_like(key, val, expect):
    r = good()
    r["diff_stat"][key] = val
    canon, _ = ok(r)
    assert canon["diff_stat"][key] == expect
    if key == "files":
        assert vpschema.diff_stat_files(r) == expect


@pytest.mark.parametrize("key", ["files", "insertions", "deletions"])
@pytest.mark.parametrize("val", NOT_NUMBER_LIKE + [-4])
def test_diff_stat_odd_leaves_are_warnings_not_errors(key, val):
    r = good()
    r["diff_stat"][key] = val
    canon, warnings = ok(r)
    assert canon["diff_stat"][key] == 0 and any(w.startswith("diff_stat.%s" % key) for w in warnings)


def test_diff_stat_keeps_the_paths_when_files_was_a_list():
    r = good()
    r["diff_stat"]["files"] = ["TECHNICAL.md"]
    canon, _ = ok(r)
    assert canon["diff_stat"]["files"] == 1 and canon["diff_stat"]["paths"] == ["TECHNICAL.md"]


def test_diff_stat_container_shape_is_the_only_hard_rule():
    r = good()
    del r["diff_stat"]
    canon, warnings = ok(r)
    assert canon["diff_stat"] == {"files": 0, "insertions": 0, "deletions": 0}
    bad(good(diff_stat=[1, 1, 0]), "diff_stat")
    bad(good(diff_stat="1 file changed"), "diff_stat")


# -- informational: checks ----------------------------------------------------------------

EXIT_CODES = [0, 1, -1, 130, "0", " 2 ", "-1", 0.0]
NOT_EXECUTED = [None, "blocked", "not run", "skipped", "denied", "N/A", "", True, False, "n/a (sandbox)"]


@pytest.mark.parametrize("val", EXIT_CODES)
def test_any_int_like_exit_is_an_executed_check(val):
    r = good()
    r["checks"][1]["exit"] = val
    r["checks"][1]["log"] = ""                        # no reason needed when it ran
    canon, _ = ok(r)
    c = canon["checks"][1]
    assert c["executed"] and c["exit"] == int(float(str(val).strip()))


@pytest.mark.parametrize("val", NOT_EXECUTED)
def test_any_non_exit_code_means_not_executed_and_needs_a_reason(val):
    r = good()
    r["checks"][1]["exit"] = val
    r["checks"][1]["log"] = "NOT EXECUTED: sandbox denies *pytest*"
    canon, _ = ok(r)
    c = canon["checks"][1]
    assert not c["executed"] and c["exit"] is None and "sandbox" in c["reason"]
    r["checks"][1]["log"] = ""
    bad(r, "checks[1].exit")


@pytest.mark.parametrize("reason_key", vpschema.NOT_EXECUTED_REASON_KEYS)
def test_the_reason_may_live_under_any_reason_key(reason_key):
    r = good()
    r["checks"][1] = {"command": "uv run pytest -q", "exit": None, "log": "", reason_key: "denied by profile"}
    canon, _ = ok(r)
    assert canon["checks"][1]["reason"] == "denied by profile"


def test_missing_exit_key_is_not_executed():
    r = good()
    del r["checks"][1]["exit"]
    r["checks"][1]["log"] = "never ran"
    canon, _ = ok(r)
    assert not canon["checks"][1]["executed"]


def test_checks_need_an_identity_but_name_and_command_are_interchangeable():
    r = good()
    del r["checks"][1]["name"]
    canon, _ = ok(r)
    assert canon["checks"][1]["name"] == "uv run pytest --collect-only -q t.py"
    r = good()
    del r["checks"][1]["command"]
    canon, _ = ok(r)
    assert canon["checks"][1]["command"] == "collect"
    r = good()
    r["checks"][1] = {"exit": 0, "log": "ok"}
    bad(r, "checks[1]")


def test_check_leaves_accept_any_scalar_with_a_warning():
    r = good()
    r["checks"][1].update({"name": 7, "log": ["a", "b"], "extra": {"k": 1}})
    canon, warnings = ok(r)
    c = canon["checks"][1]
    assert c["name"] == "uv run pytest --collect-only -q t.py" and c["log"] == '["a", "b"]'
    assert c["extra"] == {"k": 1} and any("checks[1].log" in w for w in warnings)


def test_checks_container_shape():
    r = good()
    del r["checks"]
    canon, warnings = ok(r)
    assert canon["checks"] == [] and any(w.startswith("checks") for w in warnings)
    r = good(checks=good()["checks"][0])                # a lone object is wrapped
    canon, _ = ok(r)
    assert len(canon["checks"]) == 1
    r = good(checks=["ruff check x.py"])               # a bare string is a not-executed command
    canon, _ = ok(r)
    assert canon["checks"][0]["command"] == "ruff check x.py" and not canon["checks"][0]["executed"]
    bad(good(checks=[42]), "checks[0]")
    bad(good(checks=[{"name": "x", "exit": 0, "log": 1, "command": None}, 5]), "checks[1]")


# -- informational: disputes / notes -------------------------------------------------------

def test_disputes_and_notes_are_lenient():
    canon, _ = ok(good(disputes=[{"line": 3, "reason": "r"}, {"line": "B2", "note": "n"}, "bare reason"]))
    assert [d["line"] for d in canon["disputes"]] == ["3", "B2", "?"]
    assert canon["disputes"][2]["reason"] == "bare reason"
    canon, warnings = ok(good(disputes={"line": "B1", "reason": "x"}))
    assert len(canon["disputes"]) == 1
    bad(good(disputes=[None]), "disputes[0]")
    for v in (None, "", 3, ["a"]):
        canon, _ = ok(good(notes=v))
        assert isinstance(canon["notes"], str)
    r = good()
    del r["notes"]
    del r["disputes"]
    canon, _ = ok(r)
    assert canon["notes"] == "" and canon["disputes"] == []


# -- every combination of lenient forms at once ------------------------------------------

def test_arbitrary_combinations_of_lenient_forms_validate():
    exits = [0, None, "blocked", "1"]
    files = [1, ["a"], "1"]
    names = ["n", None]
    for ex, fi, nm in itertools.product(exits, files, names):
        chk = {"command": "cmd", "exit": ex, "log": "why"}
        if nm:
            chk["name"] = nm
        r = good(checks=[chk], attempt="3", blocked=False)
        r["diff_stat"]["files"] = fi
        ok(r)


def test_schema_doc_states_the_rule_for_the_model():
    doc = vpschema.RESULT_SCHEMA_DOC
    assert "BINDING" in doc["$comment"] and "never fabricate" in doc["$comment"]
    ex = doc["properties"]["checks"]["items"]["properties"]["exit"]
    assert set(ex["type"]) == {"integer", "null", "string"} and "NOT EXECUTED" in ex["$comment"]
    assert "name" not in doc["properties"]["checks"]["items"]["required"]


@pytest.mark.parametrize("key,alt", [(k, a) for k, alts in vpschema.CHECK_SYNONYMS.items() for a in alts])
def test_check_key_synonyms_are_read(key, alt):
    chk = {"name": "t", "command": "cmd", "exit": 0, "log": "ok"}
    chk[alt] = chk.pop(key)
    canon, warnings = ok(good(checks=[chk]))
    c = canon["checks"][0]
    assert c["name"] == "t" and c["command"] == "cmd" and c["exit"] == 0 and c["executed"] and c["log"] == "ok"
    assert any(alt in w for w in warnings)
