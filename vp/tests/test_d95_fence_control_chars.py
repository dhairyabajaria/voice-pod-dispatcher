"""D95: a text-fence record with a literal TAB (or newline) inside a string
value still parses.  L-FAKE-CONTROLS-SOURCES r3 (12:55Z): the grader quoted
`git diff --numstat` output ("11<TAB>3<TAB>platform/core/capability.py") in a
check log; strict JSON refused the whole record and the turn read as
"finished (stop) with no record" four times in 50 s -> STUCK 1/3."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vprunners import extract_json_fence  # noqa: E402


def test_a_literal_tab_inside_a_string_value_does_not_lose_the_record():
    text = ('preamble\n```json\n{\n  "item": "X", "checks": [{"command": "git diff --numstat a..b",'
            ' "log": "11\t3\tplatform/core/capability.py"}], "all_pass": false\n}\n```\n')
    obj = extract_json_fence(text)
    assert obj is not None and obj["item"] == "X"
    assert obj["checks"][0]["log"] == "11\t3\tplatform/core/capability.py"


def test_a_bare_object_with_a_raw_newline_in_a_string_parses_too():
    obj = extract_json_fence('{"item": "Y", "notes": "line one\nline two"}')
    assert obj == {"item": "Y", "notes": "line one\nline two"}


def test_real_garbage_is_still_none():
    assert extract_json_fence("```json\n{not json}\n```") is None
    assert extract_json_fence("") is None
