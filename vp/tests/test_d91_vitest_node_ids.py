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


# ── D195: root a junit node by the job that produced it ────────────────────────

def test_d195_job_root_maps_only_the_package_test_jobs():
    from vpdriver import job_root
    assert job_root("vp/agent") == "agent/"
    assert job_root("agent") == "agent/"
    assert job_root("vp/platform") == "platform/"
    assert job_root("vp/platform-shards-3") == "platform/"
    assert job_root("vp/platform-coverage") == "platform/"
    assert job_root("platform-shard") == "platform/"
    assert job_root("vp/portal") == "portal/"
    # jobs that do not run ONE package's suite must stay unrooted
    for other in ("vp/deploy-contracts", "lint-and-typecheck", "required-gate",
                  "combine-coverage", "collected-test-floor", "supply-chain-tests",
                  "vp/supply-chain-agent", "supply-chain-audit-agent",
                  "supply-chain-audit-platform", "", None):
        assert job_root(other) is None, other


def test_d195_the_same_rootless_path_from_two_jobs_gets_two_different_roots():
    """The defect this exists for. Measured on
    proof-R-VOICE-CONTEXT-BUDGET-HOSTED-60921T215122091: vp/agent and
    vp/platform-shards-* both upload `file='tests/test_x.py'` because each job
    runs pytest inside its own package. Exactly 3 basenames live under both
    roots, so before D195 an agent red could be charged to a platform row."""
    jobs = [{"job_number": 1, "name": "vp/agent"},
            {"job_number": 2, "name": "vp/platform-shards-3"}]
    failed = {
        1: [{"file": "tests/test_recording_opt_out.py", "classname": "tests.test_recording_opt_out",
             "name": "test_a_real_user_turn", "result": "failure", "message": "boom"}],
        2: [{"file": "tests/test_recording_opt_out.py", "classname": "tests.test_recording_opt_out",
             "name": "test_a_real_user_turn", "result": "failure", "message": "boom"}],
    }
    nodes, _ = circle_failed_nodes(failed, jobs=jobs)
    assert nodes == ["agent/tests/test_recording_opt_out.py::test_a_real_user_turn",
                     "platform/tests/test_recording_opt_out.py::test_a_real_user_turn"]
    # and the two must not collapse into one node
    assert len(nodes) == 2


def test_d195_rooted_nodes_no_longer_match_the_wrong_root_in_scope():
    """The consequence, asserted at the place that actually mis-charged the row:
    node_in_scope matches path suffixes in BOTH directions, so a rootless node
    matched either root. A rooted node matches only its own."""
    import laneproof
    agent_node = "agent/tests/test_recording_opt_out.py::test_x"
    plat_paths = ["platform/tests/test_recording_opt_out.py"]
    agent_paths = ["agent/tests/test_recording_opt_out.py"]
    assert laneproof.Proof.node_in_scope(agent_node, agent_paths) is True
    assert laneproof.Proof.node_in_scope(agent_node, plat_paths) is False
    # the pre-D195 rootless spelling is the ambiguity itself: it matches BOTH
    rootless = "tests/test_recording_opt_out.py::test_x"
    assert laneproof.Proof.node_in_scope(rootless, agent_paths) is True
    assert laneproof.Proof.node_in_scope(rootless, plat_paths) is True


def test_d195_is_inert_without_jobs_and_never_double_roots():
    """Backward compatibility: every pre-D195 caller passes no `jobs` and must get
    byte-identical output. And a junit that already carried a rooted path is
    left alone rather than prefixed twice."""
    failed = {1: [{"file": "tests/test_reply_path.py", "classname": "", "name": "test_x",
                   "result": "failure", "message": ""}]}
    assert circle_failed_nodes(failed)[0] == ["tests/test_reply_path.py::test_x"]
    already = {1: [{"file": "platform/tests/test_reply_path.py", "classname": "", "name": "test_x",
                    "result": "failure", "message": ""}]}
    jobs = [{"job_number": 1, "name": "vp/platform-shards-1"}]
    assert circle_failed_nodes(already, jobs=jobs)[0] == ["platform/tests/test_reply_path.py::test_x"]
    # an unmapped job (required-gate) leaves the node exactly as it was
    assert circle_failed_nodes(failed, jobs=[{"job_number": 1, "name": "vp/required-gate"}])[0] \
        == ["tests/test_reply_path.py::test_x"]
