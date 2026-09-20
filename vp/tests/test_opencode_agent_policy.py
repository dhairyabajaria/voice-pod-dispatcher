"""D88: the OpenCode builder/grader bash policy denies by EXECUTABLE, never by filename.

Replays the exact commands the R-TEST-PG-WAL-CAP-R1 records show as denied
(a read-only `git diff` of test_pgserver_lifecycle.py, a `git show | grep`,
`git grep "ALTER SYSTEM"`) and the shapes that must stay denied (a pytest run
in every spelling, starting a cluster) through the agent files' bash rules,
with OpenCode's semantics: glob over the whole command, the LAST matching
rule wins (the files say so beside the collect-only allows)."""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

AGENTS = Path.home() / ".config" / "opencode" / "agents"
FILES = ("vp-builder.md", "vp-infra.md")

DENIED_READ_ONLY = [   # from turns/R-TEST-PG-WAL-CAP-R1/.../1-r2-builder-export.json etc.
    "git diff c6b3e08e7a412043ab1afae875efaa145dd8dc05..HEAD -- platform/tests/test_pgserver_lifecycle.py | head -80",
    'git grep -n "ALTER SYSTEM" HEAD -- platform',
    'git show 6cc52414:platform/testsupport/postgres.py | grep -n "ALTER SYSTEM\\|pg_reload_conf\\|_TEST_CLUSTER"',
    'git show HEAD:platform/tests/test_pgserver_lifecycle.py',
    'grep -n "pytest.mark" platform/tests/test_pgserver_lifecycle.py',
    "sed -n 180,240p platform/testsupport/postgres.py",
    "cat platform/testsupport/pgserver_lock.py",
    "uv run ruff check testsupport/postgres.py tests/test_pgserver_lifecycle.py",
    "git ls-files | grep -i pgserver",
]
MUST_DENY = [
    "pytest",
    "pytest -q platform/tests/test_pgserver_lifecycle.py",
    "uv run pytest -q tests",
    "cd platform && uv run pytest tests/test_pgserver_lifecycle.py -q",
    "cd platform; uv run pytest -q",
    "python -m pytest tests",
    "python3 -m pytest -q",
    ".venv/bin/pytest -q",
    "/Users/x/platform/.venv/bin/pytest",
    "ls && pytest",
    "ls | pytest",
    "python -c 'import pgserver; pgserver.get_server(\"/tmp/pg\")'",
    "python3 -c \"import pgserver\"",
    "pgserver --help",
    "pg_ctl -D /tmp/pg start",
    "cd platform && pg_ctl start",
    ".venv/lib/python3.12/site-packages/pgserver/pginstall/bin/postgres -D x",
]
MUST_ALLOW_STILL = [
    "uv run pytest --collect-only -q",
    "cd platform && uv run pytest --collect-only -q",
]


def _bash_rules(text):
    block = text.split("  bash:", 1)[1]
    rules = []
    for line in block.splitlines():
        m = re.match(r'\s{4}"((?:[^"\\]|\\.)*)":\s*(allow|deny|ask)\s*$', line)
        if m:
            rules.append((m.group(1), m.group(2)))
        elif line.strip() and not line.startswith("    ") and not line.strip().startswith("#"):
            break
    assert rules, "no bash rules parsed"
    return rules


def decide(rules, cmd):
    verdict = "ask"
    for pat, action in rules:                    # last match wins
        if fnmatch.fnmatchcase(cmd, pat):
            verdict = action
    return verdict


@pytest.mark.skipif(not AGENTS.is_dir(), reason="no OpenCode agents dir on this host")
@pytest.mark.parametrize("name", FILES)
def test_d88_read_only_commands_on_tool_named_files_are_allowed_and_runs_stay_denied(name):
    path = AGENTS / name
    if not path.is_file():
        pytest.skip("%s absent" % name)
    rules = _bash_rules(path.read_text(encoding="utf-8"))
    assert not any(p in ("*pytest*", "*pgserver*") for p, _ in rules), "filename-substring denies are gone"
    for cmd in DENIED_READ_ONLY:
        assert decide(rules, cmd) == "allow", "%s: read-only command denied: %s" % (name, cmd)
    for cmd in MUST_DENY:
        assert decide(rules, cmd) == "deny", "%s: a run/cluster shape got through: %s" % (name, cmd)
    if name == "vp-builder.md":
        for cmd in MUST_ALLOW_STILL:
            assert decide(rules, cmd) == "allow", "%s: collection-only lost: %s" % (name, cmd)


GRADER_DENIED = [   # turns/R-TEST-PG-WAL-CAP-R2/.../1-r1-grader-export.json (04:27Z)
    "ls -la .vp",
    'git grep -n "ALTER SYSTEM" HEAD -- platform',
    'git grep -n "pg_reload_conf" HEAD -- platform',
    'grep -n "SHOW" platform/tests/test_pgserver_lifecycle.py',
    "sed -n 180,240p platform/testsupport/postgres.py",
    "cat platform/testsupport/postgres.py",
    "git diff --stat c6b3e08e..HEAD -- platform",
]
GRADER_MUST_DENY = [
    "uv run pytest -q tests",
    "python3 - <<'EOF'\nprint(1)\nEOF",
    "cat x.py && rm -rf platform",
    "ls | sh",
    "cat a.py > b.py",
    "pg_ctl start",
]


def test_d90_the_grader_can_run_read_only_check_lines_and_nothing_else():
    path = AGENTS / "vp-junior.md"
    if not path.is_file():
        pytest.skip("vp-junior.md absent")
    rules = _bash_rules(path.read_text(encoding="utf-8"))
    for cmd in GRADER_DENIED:
        assert decide(rules, cmd) == "allow", "grader read-only command denied: %s" % cmd
    for cmd in GRADER_MUST_DENY:
        assert decide(rules, cmd) == "deny", "grader run/write shape got through: %s" % cmd
