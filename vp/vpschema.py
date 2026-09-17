#!/usr/bin/env python3
"""vpschema.py -- hand-written validators for the two model-written payloads.

SPEC.md Sec.Schemas:
  RESULT.json   {"item","attempt","commit","base","diff_stat":{"files","insertions",
                 "deletions"},"checks":[{"name","command","exit","log"}],
                 "disputes":[{"line","reason"}],"blocked":null|"reason","notes"}
  FINDINGS.json {"item","attempt","commit",
                 "lines":[{"id","kind","verdict","evidence","note"}],"all_pass":bool}

No third-party dependency (no jsonschema).  Public API:

    ok, errors = validate_result(path)
    ok, errors = validate_findings(path)

`errors` is a list of human-readable strings, each naming the offending JSON
pointer-ish path (e.g. "lines[3].verdict").  `ok` is True iff errors == [].

Both functions also accept an already-parsed object via validate_result_obj /
validate_findings_obj so the driver can validate without a second read.
"""

from __future__ import annotations

import json
import os
import re

__all__ = [
    "validate_result",
    "normalize_result",
    "int_like",
    "check_name",
    "check_executed",
    "check_reason",
    "diff_stat_files",
    "validate_findings",
    "validate_result_obj",
    "validate_findings_obj",
    "RESULT_SCHEMA_DOC",
    "FINDINGS_SCHEMA_DOC",
    "FINDING_KINDS",
    "FINDING_VERDICTS",
    "REVIEW_SCHEMA_DOC",
    "REVIEW_VERDICTS",
    "validate_review",
    "validate_review_obj",
]

FINDING_KINDS = ("invariant", "test", "negative", "forbidden", "evidence")
FINDING_VERDICTS = ("PASS", "FAIL", "UNKNOWN")

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# Written into <worktree>/.vp/ when vp/schemas/*.json is not on disk.
RESULT_SCHEMA_DOC = {
    "$comment": "Voice Pod v9 Builder result, validated by vpschema.validate_result. "
                "item/attempt/commit/base/blocked are BINDING (exact meaning; attempt "
                "may be a digit-string). diff_stat/checks/disputes/notes are your own "
                "honest report: keep the shapes below, never fabricate a number or an "
                "exit code; a check you could not run keeps exit null and says why in "
                "log.",
    "type": "object",
    "required": ["item", "attempt", "commit", "base", "diff_stat", "checks",
                 "disputes", "blocked", "notes"],
    "properties": {
        "item": {"type": "string"},
        "attempt": {"type": "integer"},
        "commit": {"type": "string", "pattern": "^[0-9a-f]{7,40}$"},
        "base": {"type": "string", "pattern": "^[0-9a-f]{7,40}$"},
        "diff_stat": {"type": "object",
                      "required": ["files", "insertions", "deletions"],
                      "$comment": "the three numbers of `git diff --shortstat <base>..<commit>`",
                      "properties": {
                          "files": {"type": "integer",
                                    "$comment": "COUNT of changed files (an array of their "
                                                "paths is tolerated and counted)"},
                          "insertions": {"type": "integer"},
                          "deletions": {"type": "integer"}}},
        "checks": {"type": "array", "items": {
            "type": "object",
            "required": ["command", "exit", "log"],
            "properties": {
                "name": {"type": "string", "$comment": "short label; defaults to the command"},
                "command": {"type": "string"},
                "exit": {"type": ["integer", "null", "string"],
                         "$comment": "the integer exit code, or null when NOT EXECUTED here "
                                     "(e.g. the sandbox denies the command); a word such as "
                                     "\"blocked\"/\"not run\" is read as null. Either way "
                                     "`log` must say why. Never fabricate an exit code."},
                "log": {"type": "string"}}}},
        "disputes": {"type": "array", "items": {
            "type": "object", "required": ["line", "reason"],
            "properties": {"line": {"type": ["string", "integer"]}, "reason": {"type": "string"}}}},
        "blocked": {"type": ["null", "string"],
                    "$comment": "null (or false / \"\") = not blocked; otherwise the reason"},
        "notes": {"type": "string"},
    },
}

FINDINGS_SCHEMA_DOC = {
    "$comment": "Voice Pod v9 Junior findings. All keys required. "
                "Validated by vpschema.validate_findings.",
    "type": "object",
    "required": ["item", "attempt", "commit", "lines", "all_pass"],
    "properties": {
        "item": {"type": "string"},
        "attempt": {"type": "integer"},
        "commit": {"type": "string", "pattern": "^[0-9a-f]{7,40}$"},
        "lines": {"type": "array", "minItems": 1, "items": {
            "type": "object",
            "required": ["id", "kind", "verdict", "evidence", "note"],
            "properties": {
                "kind": {"enum": list(FINDING_KINDS)},
                "verdict": {"enum": list(FINDING_VERDICTS)},
            }}},
        "all_pass": {"type": "boolean"},
    },
}


# --------------------------------------------------------------------------
# small primitives
# --------------------------------------------------------------------------

def _err(errors, where, msg):
    errors.append("%s: %s" % (where, msg))


def _req(obj, key, where, errors):
    """Return (present, value).  Records a 'missing key' error when absent."""
    if not isinstance(obj, dict) or key not in obj:
        _err(errors, "%s.%s" % (where, key) if where else key, "missing")
        return False, None
    return True, obj[key]


def _want_str(val, where, errors, allow_empty=False):
    if not isinstance(val, str):
        _err(errors, where, "expected string, got %s" % type(val).__name__)
        return False
    if not allow_empty and val.strip() == "":
        _err(errors, where, "must not be empty")
        return False
    return True


def _want_int(val, where, errors, minimum=None):
    # bool is a subclass of int; reject it explicitly.
    if isinstance(val, bool) or not isinstance(val, int):
        _err(errors, where, "expected integer, got %s" % type(val).__name__)
        return False
    if minimum is not None and val < minimum:
        _err(errors, where, "must be >= %d (got %d)" % (minimum, val))
        return False
    return True


# --------------------------------------------------------------------------
# RESULT.json rule (D10)
#
# RESULT.json has two classes of field:
#   BINDING       item, attempt, commit, base, blocked -- the driver acts on
#                 them (HEAD check, attempt identity, blocked routing).  Their
#                 *meaning* is strict, their *representation* is lenient: an
#                 attempt may be an int or a digit-string, a sha any 7-40 hex,
#                 blocked null/false/"" (not blocked) or any non-empty scalar
#                 (its text is the reason).
#   INFORMATIONAL diff_stat, checks, disputes, notes -- the builder's own
#                 report.  The proof harness and the grader are the gates, so a
#                 builder that reports honestly but spells a leaf differently
#                 must not lose the turn.  Rule: the CONTAINER shape must be
#                 recognisable (object / array of objects; a lone object where
#                 an array is expected is wrapped), a missing container
#                 defaults to empty, and every LEAF accepts any JSON scalar --
#                 numbers coerce from number-like values (int, integral float,
#                 digit-string, or a list whose length is the count) and
#                 otherwise stay as written.  The one hard rule inside checks:
#                 a check that was NOT EXECUTED (exit is null / a word / bool)
#                 must carry a non-empty log or reason saying why, and every
#                 check must be identifiable (command or name).
# normalize_result() returns the canonical form + warnings; validate_result()
# returns errors only for binding and structural failures.
# --------------------------------------------------------------------------

BINDING_KEYS = ("item", "attempt", "commit", "base", "blocked")
INFO_DEFAULTS = {"diff_stat": {}, "checks": [], "disputes": [], "notes": ""}
NOT_EXECUTED_REASON_KEYS = ("log", "reason", "note", "notes", "why")
CHECK_SYNONYMS = {"command": ("cmd", "argv", "run"),
                  "exit": ("exit_code", "exitcode", "rc", "returncode", "return_code", "status", "code"),
                  "log": ("output", "stdout", "result", "detail"),
                  "name": ("label", "id", "check")}


def int_like(val):
    """-> int, or None when `val` is not a number-like representation.
    Number-like: int (not bool), integral float, digit-string (+/-, spaces),
    or a list/tuple (its length -- 'files': [paths] is a count by another name)."""
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float) and val.is_integer():
        return int(val)
    if isinstance(val, str):
        t = val.strip().replace(",", "")
        if t.lstrip("+-").isdigit():
            return int(t)
        return None
    if isinstance(val, (list, tuple)):
        return len(val)
    return None


def _text(val):
    """any scalar as text; None/False/"" -> "" """
    if val is None or val is False:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, (int, float, bool)):
        return str(val)
    try:
        return json.dumps(val, sort_keys=True)
    except (TypeError, ValueError):
        return str(val)


def check_executed(chk):
    """True iff checks[].exit is an exit code (int-like); null, a word,
    a bool or a missing key all mean NOT EXECUTED."""
    val = chk.get("exit") if isinstance(chk, dict) else None
    return int_like(val) is not None and not isinstance(val, (list, tuple))


def check_name(chk):
    """checks[].name, or the command when the builder left the label out"""
    if isinstance(chk, dict):
        for k in ("name", "command"):
            v = chk.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return "?"


def check_reason(chk):
    """why a check was not executed: the first non-empty of log/reason/note/why"""
    if isinstance(chk, dict):
        for k in NOT_EXECUTED_REASON_KEYS:
            t = _text(chk.get(k)).strip()
            if t:
                return t
    return ""


def _want_blocked(val, where, errors):
    """null/false/"" -> not blocked; any other scalar -> a reason"""
    if val is None or val is False or (isinstance(val, str) and not val.strip()):
        return True
    if isinstance(val, (str, int, float, bool)):
        return True
    _err(errors, where, "expected null or a reason string, got %s" % type(val).__name__)
    return False


def _as_list(val, where, warnings):
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        warnings.append("%s: object where an array was expected; wrapped" % where)
        return [val]
    warnings.append("%s: %s where an array was expected; dropped" % (where, type(val).__name__))
    return []


def _normalize_diff_stat(ds, warnings, errors):
    if ds is None:
        warnings.append("diff_stat: missing; defaulted to zeros")
        ds = {}
    if not isinstance(ds, dict):
        _err(errors, "diff_stat", "expected object, got %s" % type(ds).__name__)
        return {"files": 0, "insertions": 0, "deletions": 0}
    out = {}
    for key in ("files", "insertions", "deletions"):
        val = ds.get(key)
        n = int_like(val)
        if key not in ds:
            warnings.append("diff_stat.%s: missing; 0" % key)
            n = 0
        elif n is None:
            warnings.append("diff_stat.%s: not number-like (%r); 0" % (key, val))
            n = 0
        elif n < 0:
            warnings.append("diff_stat.%s: negative (%d); 0" % (key, n))
            n = 0
        out[key] = n
    if isinstance(ds.get("files"), (list, tuple)):
        out["paths"] = [str(x) for x in ds["files"]]
    return out


def _normalize_check(chk, w, warnings, errors):
    if not isinstance(chk, dict):
        if isinstance(chk, str) and chk.strip():
            warnings.append("%s: bare string; read as a not-executed command" % w)
            chk = {"command": chk, "exit": None, "log": "(no log)"}
        else:
            _err(errors, w, "expected object, got %s" % type(chk).__name__)
            return None
    for key, alts in CHECK_SYNONYMS.items():
        if key not in chk:
            for alt in alts:
                if alt in chk:
                    chk = dict(chk, **{key: chk[alt]})
                    warnings.append("%s.%s: read from `%s`" % (w, key, alt))
                    break
    name = check_name(chk)
    if name == "?":
        _err(errors, w, "unidentifiable check: needs a command or a name")
        return None
    executed = check_executed(chk)
    reason = check_reason(chk)
    if not executed and not reason:
        _err(errors, "%s.exit" % w, "%r means NOT EXECUTED; a non-empty log/reason must say why"
             % (chk.get("exit"),))
        return None
    out = dict(chk)
    out["name"] = name
    out["command"] = _text(chk.get("command")) or name
    out["exit"] = int_like(chk.get("exit")) if executed else None
    out["executed"] = executed
    out["log"] = _text(chk.get("log"))
    if not executed:
        out["reason"] = reason
    for k in ("name", "command", "log"):
        if k in chk and not isinstance(chk[k], str):
            warnings.append("%s.%s: %s coerced to text" % (w, k, type(chk[k]).__name__))
    return out


def _normalize_dispute(dis, w, warnings, errors):
    if isinstance(dis, str):
        warnings.append("%s: bare string; read as the reason" % w)
        return {"line": "?", "reason": dis}
    if not isinstance(dis, dict):
        _err(errors, w, "expected object, got %s" % type(dis).__name__)
        return None
    out = dict(dis)
    out["line"] = _text(dis.get("line")) or "?"
    out["reason"] = _text(dis.get("reason") or dis.get("note") or dis.get("why"))
    if not out["reason"].strip():
        warnings.append("%s: no reason given" % w)
    return out


def normalize_result(obj):
    """-> (canonical, errors, warnings).  `canonical` is None when obj is not an
    object.  Errors are binding/structural failures only (see the D10 rule);
    warnings record every lenient reading so the record can carry them."""
    errors, warnings = [], []
    if not isinstance(obj, dict):
        return None, ["<root>: expected object, got %s" % type(obj).__name__], warnings
    out = dict(obj)
    # binding
    ok, v = _req(obj, "item", "", errors)
    if ok:
        _want_str(v, "item", errors)
    ok, v = _req(obj, "attempt", "", errors)
    if ok and _want_attempt(v, "attempt", errors):
        out["attempt"] = int(v)
    ok, v = _req(obj, "commit", "", errors)
    if ok:
        _want_sha(v, "commit", errors)
    ok, v = _req(obj, "base", "", errors)
    if ok:
        _want_sha(v, "base", errors)
    if "blocked" not in obj:
        warnings.append("blocked: missing; null")
        out["blocked"] = None
    elif _want_blocked(obj["blocked"], "blocked", errors):
        t = _text(obj["blocked"]).strip()
        out["blocked"] = t or None
    # informational
    for key, default in INFO_DEFAULTS.items():
        if key not in obj:
            warnings.append("%s: missing; %r" % (key, default))
    out["diff_stat"] = _normalize_diff_stat(obj.get("diff_stat"), warnings, errors)
    checks = []
    for i, chk in enumerate(_as_list(obj.get("checks"), "checks", warnings)):
        c = _normalize_check(chk, "checks[%d]" % i, warnings, errors)
        if c is not None:
            checks.append(c)
    out["checks"] = checks
    disputes = []
    for i, dis in enumerate(_as_list(obj.get("disputes"), "disputes", warnings)):
        d = _normalize_dispute(dis, "disputes[%d]" % i, warnings, errors)
        if d is not None:
            disputes.append(d)
    out["disputes"] = disputes
    out["notes"] = _text(obj.get("notes"))
    return out, errors, warnings


def _want_sha(val, where, errors):
    if not _want_str(val, where, errors):
        return False
    if not _SHA_RE.match(val):
        _err(errors, where, "not a git sha (7-40 hex chars): %r" % (val,))
        return False
    return True


def _want_attempt(val, where, errors):
    """attempt is an integer in the schema doc; a digit-string from the store
    CLI is accepted and not reported (see README-driver.md Decisions)."""
    if isinstance(val, bool):
        _err(errors, where, "expected integer, got bool")
        return False
    if isinstance(val, int):
        return True
    if isinstance(val, str) and val.isdigit():
        return True
    _err(errors, where, "expected integer attempt id, got %r" % (val,))
    return False


def _load(path):
    """-> (obj, errors).  obj is None when the file could not be read/parsed."""
    errors = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None, ["<file>: missing: %s" % path]
    except OSError as exc:
        return None, ["<file>: unreadable: %s: %s" % (path, exc)]
    if raw.strip() == "":
        return None, ["<file>: empty: %s" % path]
    try:
        return json.loads(raw), errors
    except ValueError as exc:
        return None, ["<file>: invalid JSON: %s: %s" % (path, exc)]


# --------------------------------------------------------------------------
# RESULT.json
# --------------------------------------------------------------------------

def validate_result_obj(obj):
    """(ok, errors) under the D10 rule; see normalize_result for the reading."""
    _, errors, _ = normalize_result(obj)
    return (not errors), errors


def diff_stat_files(obj):
    """diff_stat.files as an int whatever valid form the builder used"""
    n = int_like(((obj or {}).get("diff_stat") or {}).get("files"))
    return 0 if n is None else n


def validate_result(path):
    """(ok, errors) for a RESULT.json on disk."""
    obj, load_errors = _load(path)
    if obj is None:
        return False, load_errors
    ok, errors = validate_result_obj(obj)
    return ok, load_errors + errors


# --------------------------------------------------------------------------
# FINDINGS.json
# --------------------------------------------------------------------------

def validate_findings_obj(obj):
    errors = []
    if not isinstance(obj, dict):
        return False, ["<root>: expected object, got %s" % type(obj).__name__]

    ok, v = _req(obj, "item", "", errors)
    if ok:
        _want_str(v, "item", errors)
    ok, v = _req(obj, "attempt", "", errors)
    if ok:
        _want_attempt(v, "attempt", errors)
    ok, v = _req(obj, "commit", "", errors)
    if ok:
        _want_sha(v, "commit", errors)

    verdicts = []
    ok, lines = _req(obj, "lines", "", errors)
    if ok:
        if not isinstance(lines, list):
            _err(errors, "lines", "expected array, got %s" % type(lines).__name__)
        elif not lines:
            _err(errors, "lines", "must not be empty")
        else:
            seen_ids = set()
            for i, ln in enumerate(lines):
                w = "lines[%d]" % i
                if not isinstance(ln, dict):
                    _err(errors, w, "expected object, got %s" % type(ln).__name__)
                    continue
                present, lid = _req(ln, "id", w, errors)
                if present:
                    if isinstance(lid, bool) or not isinstance(lid, (str, int)):
                        _err(errors, "%s.id" % w, "expected string or integer")
                    else:
                        if lid in seen_ids:
                            _err(errors, "%s.id" % w, "duplicate id %r" % (lid,))
                        seen_ids.add(lid)
                present, kind = _req(ln, "kind", w, errors)
                if present and kind not in FINDING_KINDS:
                    _err(errors, "%s.kind" % w,
                         "expected one of %s, got %r" % (list(FINDING_KINDS), kind))
                present, verdict = _req(ln, "verdict", w, errors)
                if present:
                    if verdict not in FINDING_VERDICTS:
                        _err(errors, "%s.verdict" % w,
                             "expected one of %s, got %r"
                             % (list(FINDING_VERDICTS), verdict))
                    else:
                        verdicts.append(verdict)
                present, ev = _req(ln, "evidence", w, errors)
                if present:
                    _want_str(ev, "%s.evidence" % w, errors)
                present, note = _req(ln, "note", w, errors)
                if present and note is not None:
                    _want_str(note, "%s.note" % w, errors, allow_empty=True)

    ok, all_pass = _req(obj, "all_pass", "", errors)
    if ok:
        if not isinstance(all_pass, bool):
            _err(errors, "all_pass",
                 "expected boolean, got %s" % type(all_pass).__name__)
        elif verdicts and not errors:
            # Consistency: all_pass must agree with the per-line verdicts.
            computed = all(x == "PASS" for x in verdicts)
            if computed != all_pass:
                _err(errors, "all_pass",
                     "is %s but per-line verdicts compute %s"
                     % (all_pass, computed))

    return (not errors), errors


def validate_findings(path):
    """(ok, errors) for a FINDINGS.json on disk."""
    obj, load_errors = _load(path)
    if obj is None:
        return False, load_errors
    ok, errors = validate_findings_obj(obj)
    return ok, load_errors + errors


# --------------------------------------------------------------------------
# REVIEW.json  (K-10: senior per-item review and final per-union review)
# --------------------------------------------------------------------------

REVIEW_VERDICTS = ("APPROVE", "FINDINGS", "BLOCKED")
REVIEW_SEVERITIES = ("low", "medium", "high", "critical")

REVIEW_SCHEMA_DOC = {
    "$comment": "Voice Pod v12 review record (senior per item, final per union). "
                "Validated by vpschema.validate_review. verdict=APPROVE requires "
                ">=1 evidence line naming file:line AND every benchmark id in "
                "verdicts[]; a [medium]+ finding with a reproduce command blocks.",
    "type": "object",
    "additionalProperties": False,
    "required": ["item", "subject", "base", "candidate", "reviewer", "verdict",
                 "verdicts", "findings", "evidence", "summary"],
    "properties": {
        "item": {"type": "string"},
        "subject": {"enum": ["item", "union"]},
        "base": {"type": "string", "pattern": "^[0-9a-f]{7,40}$"},
        "candidate": {"type": "string", "pattern": "^[0-9a-f]{7,40}$"},
        "reviewer": {"type": "string"},
        "verdict": {"enum": list(REVIEW_VERDICTS)},
        "verdicts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "verdict", "evidence"],
            "properties": {
                "id": {"type": "string"},
                "verdict": {"enum": ["PASS", "FAIL", "UNKNOWN"]},
                "evidence": {"type": "string"}}}},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "severity", "title", "file", "line", "detail",
                         "reproduce", "benchmark_line"],
            "properties": {
                "id": {"type": "string"},
                "severity": {"enum": list(REVIEW_SEVERITIES)},
                "title": {"type": "string"},
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "detail": {"type": "string"},
                "reproduce": {"type": ["string", "null"]},
                "benchmark_line": {"type": "string"}}}},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}

_FILE_LINE_RE = re.compile(r"[\w./-]+\.[A-Za-z]{1,6}:\d+")


def validate_review_obj(obj, benchmark_ids=None):
    """(ok, errors).  `benchmark_ids` (iterable of str) makes the check
    'every benchmark id has a verdict' active (K-11 semantic lint)."""
    errors = []
    if not isinstance(obj, dict):
        return False, ["<root>: expected object, got %s" % type(obj).__name__]
    for key in ("item", "reviewer", "summary"):
        ok, v = _req(obj, key, "", errors)
        if ok:
            _want_str(v, key, errors, allow_empty=(key == "summary"))
    ok, v = _req(obj, "subject", "", errors)
    if ok and v not in ("item", "union"):
        _err(errors, "subject", "expected item|union, got %r" % (v,))
    for key in ("base", "candidate"):
        ok, v = _req(obj, key, "", errors)
        if ok:
            _want_sha(v, key, errors)
    ok, verdict = _req(obj, "verdict", "", errors)
    if ok and verdict not in REVIEW_VERDICTS:
        _err(errors, "verdict", "expected one of %s, got %r"
             % (list(REVIEW_VERDICTS), verdict))
    ok, vs = _req(obj, "verdicts", "", errors)
    seen = set()
    fails = 0
    if ok:
        if not isinstance(vs, list):
            _err(errors, "verdicts", "expected array")
            vs = []
        for i, ln in enumerate(vs):
            w = "verdicts[%d]" % i
            if not isinstance(ln, dict):
                _err(errors, w, "expected object")
                continue
            p, lid = _req(ln, "id", w, errors)
            if p:
                if not isinstance(lid, str) or not lid:
                    _err(errors, w + ".id", "expected non-empty string")
                else:
                    seen.add(lid)
            p, vv = _req(ln, "verdict", w, errors)
            if p and vv not in ("PASS", "FAIL", "UNKNOWN"):
                _err(errors, w + ".verdict", "expected PASS|FAIL|UNKNOWN")
            elif p and vv == "FAIL":
                fails += 1
            p, ev = _req(ln, "evidence", w, errors)
            if p:
                _want_str(ev, w + ".evidence", errors)
    ok, fs = _req(obj, "findings", "", errors)
    blocking = 0
    if ok:
        if not isinstance(fs, list):
            _err(errors, "findings", "expected array")
            fs = []
        for i, f in enumerate(fs):
            w = "findings[%d]" % i
            if not isinstance(f, dict):
                _err(errors, w, "expected object")
                continue
            for key in ("id", "title", "file", "detail", "benchmark_line"):
                p, v = _req(f, key, w, errors)
                if p:
                    _want_str(v, "%s.%s" % (w, key), errors,
                              allow_empty=(key == "benchmark_line"))
            p, sev = _req(f, "severity", w, errors)
            if p and sev not in REVIEW_SEVERITIES:
                _err(errors, w + ".severity", "expected one of %s"
                     % list(REVIEW_SEVERITIES))
            p, line = _req(f, "line", w, errors)
            if p:
                _want_int(line, w + ".line", errors, minimum=0)
            p, rep = _req(f, "reproduce", w, errors)
            if p and rep is not None and not isinstance(rep, str):
                _err(errors, w + ".reproduce", "expected string or null")
            if sev in ("medium", "high", "critical") and isinstance(rep, str) and rep.strip():
                blocking += 1
    ok, evs = _req(obj, "evidence", "", errors)
    ev_ok = 0
    if ok:
        if not isinstance(evs, list):
            _err(errors, "evidence", "expected array")
        else:
            for i, e in enumerate(evs):
                if not isinstance(e, str):
                    _err(errors, "evidence[%d]" % i, "expected string")
                elif _FILE_LINE_RE.search(e):
                    ev_ok += 1
    # semantics
    if not errors and verdict == "APPROVE":
        if ev_ok < 1:
            _err(errors, "verdict", "APPROVE needs >=1 evidence line with file:line")
        if fails:
            _err(errors, "verdict", "APPROVE with %d FAIL verdicts" % fails)
        if blocking:
            _err(errors, "verdict", "APPROVE with %d blocking findings "
                 "([medium]+ with a reproduce command)" % blocking)
        if benchmark_ids is not None:
            missing = sorted(set(str(b) for b in benchmark_ids) - seen)
            if missing:
                _err(errors, "verdicts", "benchmark ids without a verdict: %s"
                     % ", ".join(missing[:10]))
    if not errors and verdict == "FINDINGS" and not fs and not fails:
        _err(errors, "verdict", "FINDINGS with no findings and no FAIL verdicts")
    return (not errors), errors


def validate_review(path, benchmark_ids=None):
    obj, load_errors = _load(path)
    if obj is None:
        return False, load_errors
    ok, errors = validate_review_obj(obj, benchmark_ids)
    return ok, load_errors + errors


# --------------------------------------------------------------------------

def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in ("result", "findings", "review"):
        sys.stderr.write("usage: vpschema.py result|findings|review <path>\n")
        return 2
    fn = {"result": validate_result, "findings": validate_findings,
          "review": validate_review}[argv[0]]
    ok, errors = fn(argv[1])
    print(json.dumps({"ok": ok, "errors": errors}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
