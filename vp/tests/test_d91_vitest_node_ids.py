"""D91: vitest junit rows carry the file path in `classname` (src/App.test.tsx);
the dotted-classname unfolder must not turn them into src/App/test/tsx.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vpdriver import circle_failed_nodes  # noqa: E402


def test_vitest_classname_is_kept_as_the_path():
    nodes, errors = circle_failed_nodes({"vp/portal": [
        {"file": "", "classname": "src/App.test.tsx", "result": "failure",
         "name": "Wave 2 runtime mounts > opens capture", "message": "Unable to find role"},
        {"file": "", "classname": "src/routes/Campaigns.test.tsx", "result": "failure",
         "name": "campaign master and detail > falls back", "message": "not found"},
    ]})
    assert nodes == ["src/App.test.tsx::Wave 2 runtime mounts > opens capture",
                     "src/routes/Campaigns.test.tsx::campaign master and detail > falls back"]
    assert errors["src/App.test.tsx::Wave 2 runtime mounts > opens capture"] == "Unable to find role"


def test_pytest_dotted_classname_still_unfolds():
    nodes, _ = circle_failed_nodes({"vp/platform": [
        {"file": "", "classname": "tests.test_notify", "result": "failure",
         "name": "test_idle_sweeps", "message": "assert 429 == 200"},
        {"file": "", "classname": "tests.test_x.TestFoo", "result": "error",
         "name": "test_bar", "message": ""},
    ]})
    assert nodes == ["tests/test_notify.py::test_idle_sweeps", "tests/test_x.py::TestFoo::test_bar"]
