#!/usr/bin/env python3
"""tests/test_vpschema.py -- RESULT.json checks[].exit: an honest NOT EXECUTED
(null + log) is valid; digit-strings coerce; anything else is malformed."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import vpschema  # noqa: E402


def _result(**checks_over):
    chk = {"name": "pytest-collect-only", "command": "uv run pytest --collect-only -q t.py",
           "exit": 0, "log": "ok"}
    chk.update(checks_over)
    return {"item": "L00", "attempt": 1, "commit": "a" * 40, "base": "b" * 40,
            "diff_stat": {"files": 1, "insertions": 1, "deletions": 0},
            "checks": [{"name": "ruff", "command": "ruff check x.py", "exit": 0, "log": "All checks passed!"}, chk],
            "disputes": [], "blocked": None, "notes": "n"}


def test_check_exit_int_is_fine():
    ok, errs = vpschema.validate_result_obj(_result())
    assert ok, errs


def test_check_exit_null_with_a_reason_is_an_honest_not_executed():
    ok, errs = vpschema.validate_result_obj(_result(exit=None, log="NOT EXECUTED: sandbox denies *pytest*"))
    assert ok, errs


def test_check_exit_null_without_a_reason_is_malformed():
    ok, errs = vpschema.validate_result_obj(_result(exit=None, log=""))
    assert not ok and any("checks[1].exit" in e and "non-empty log" in e for e in errs), errs


def test_check_exit_digit_string_coerces_but_words_and_bools_do_not():
    assert vpschema.validate_result_obj(_result(exit="0"))[0]
    assert vpschema.validate_result_obj(_result(exit="-1"))[0]
    ok, errs = vpschema.validate_result_obj(_result(exit="not run"))
    assert not ok and any("checks[1].exit" in e and "got str" in e for e in errs), errs
    ok, errs = vpschema.validate_result_obj(_result(exit=True))
    assert not ok and any("got bool" in e for e in errs), errs


def test_schema_doc_tells_the_model_null_is_allowed():
    ex = vpschema.RESULT_SCHEMA_DOC["properties"]["checks"]["items"]["properties"]["exit"]
    assert ex["type"] == ["integer", "null"] and "NOT EXECUTED" in ex["$comment"]
