"""D83 (§46): the GitHub Actions hosted-proof provider and its overlay workflow."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP))

import vpcircle  # noqa: E402
import vpgha  # noqa: E402
import vpgha_overlay  # noqa: E402

CI_YML = Path(__file__).resolve().parents[3] / "voice-pod" / "chief9-recovery" / ".github" / "workflows" / "ci.yml"

MINI_CI = """
name: ci
on:
  push:
    branches: [main]
  workflow_dispatch:
concurrency:
  group: x
permissions:
  contents: read
jobs:
  kb-real-provider-eval:
    runs-on: ubuntu-latest
    if: github.event_name == 'workflow_dispatch'
    steps:
      - run: echo skip
  platform:
    name: required / platform
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@abc
      - name: Enforce platform collected-test floor
        working-directory: platform
        run: uv run python ../scripts/ci_collection_floor.py floor --suite platform --baseline-file tests/collection_baseline.json
      - name: Collection baseline freshness
        run: python3 scripts/ci_collection_floor.py freshness --baseline-file platform/tests/collection_baseline.json
      - name: Run platform tests with coverage
        working-directory: platform
        run: |
          uv run pytest -q --cov=core \\
            --cov-report=json:/tmp/platform-coverage.json --cov-fail-under=80
          uv run python ../deploy/check_module_coverage.py /tmp/platform-coverage.json
      - name: Rotating order
        working-directory: platform
        run: uv run python -m tests.shuffled_runner -q
  platform-shards:
    name: required / platform (shard ${{ matrix.shard }}/8)
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        shard: [0, 1]
    steps:
      - uses: actions/checkout@abc
      - name: Run this shard with coverage
        working-directory: platform
        env:
          COVERAGE_FILE: .coverage.shard-${{ matrix.shard }}
        run: |
          set -euo pipefail
          files="$(SHARD='${{ matrix.shard }}' python3 - <<'EOF'
          print('tests/test_a.py')
          EOF
          )"
          uv run pytest -q -n auto --dist loadfile $files \\
            --cov=core --cov-report=
      - name: Upload shard coverage data
        uses: actions/upload-artifact@deadbeef
        with:
          name: platform-coverage-shard-${{ matrix.shard }}
          path: platform/.coverage.shard-${{ matrix.shard }}
  platform-coverage:
    name: required / platform-coverage
    runs-on: ubuntu-latest
    needs: [platform-shards]
    steps:
      - run: echo combine
  agent:
    name: required / agent
    runs-on: ubuntu-latest
    steps:
      - name: trap battery
        working-directory: agent
        run: uv run pytest ../evals/test_trap_battery.py -q
      - name: agent tests
        working-directory: agent
        run: uv run pytest -q tests --cov=. --cov-fail-under=80
      - name: floor
        working-directory: agent
        run: uv run python ../scripts/ci_collection_floor.py control --suite agent --baseline-file ../platform/tests/collection_baseline.json
  deploy-contracts:
    name: required / deploy-contracts
    runs-on: ubuntu-latest
    steps:
      - working-directory: platform
        run: uv run pytest ../deploy/tests -q
  portal:
    name: required / portal
    runs-on: ubuntu-latest
    steps:
      - name: Typecheck, unit tests and build
        working-directory: portal
        run: |
          set -euo pipefail
          npm ci
          npm run test -- --reporter=json --outputFile=/tmp/portal-test-results.json
          npm run build
      - name: floor
        working-directory: portal
        run: python3 ../scripts/ci_collection_floor.py floor --suite portal --baseline-file ../platform/tests/collection_baseline.json
      - name: Enforce portal line-coverage floor
        working-directory: portal
        run: npm run test -- --coverage.enabled --coverage.reporter=json-summary
      - name: e2e
        working-directory: portal
        run: npm run test:e2e
  supply-chain:
    name: required / supply-chain / ${{ matrix.component }}
    runs-on: ubuntu-latest
    strategy:
      matrix:
        include:
          - component: platform
            path: platform
          - component: portal
            path: portal
    steps:
      - run: echo audit
  deploy:
    name: required / deploy
    runs-on: ubuntu-latest
    steps:
      - run: docker build .
  required-checks:
    name: required / integration
    runs-on: ubuntu-latest
    if: always()
    needs: [platform, platform-shards, platform-coverage, agent, portal, deploy-contracts, supply-chain, deploy]
    steps:
      - run: echo gate
"""

# What MINI_CI's gate requires, in MINI_CI's own job DECLARATION order.  Spelled
# out here on purpose: asserting rendered output against vpgha_overlay's derived
# set would compare the derivation with itself and pass however wrong it was.
# Note the order differs from the gate's `needs` above (which lists portal before
# deploy-contracts, as trunk's does) -- deriving the order from `needs` instead of
# from the declaration order fails this list, which is the point of the mismatch.
EXPECTED_MINI_JOBS = ["platform", "platform-shards", "platform-coverage", "agent",
                      "deploy-contracts", "portal", "supply-chain"]
# The same, for the real trunk ci.yml (its declaration order, independently read).
EXPECTED_TRUNK_JOBS = ["platform", "platform-shards", "platform-coverage", "agent",
                       "deploy-contracts", "portal", "supply-chain"]

# The overlay's own vocabulary for a test leg (vpgha_overlay.PYTEST_RE): `uv run
# pytest`, plus `uv run python -m tests.shuffled_runner`, which is pytest.main()
# with a seed plugin (D99).  Written out here rather than imported -- a test that
# reuses the module's regex cannot notice the module's regex being wrong.
# ANCHORED ON THE COMMAND on purpose: `/dev/shm/pytest-platform-shards-${{...}}`
# is a PATH, and a bare "pytest" count reads FOUR legs in platform-shards where
# there is one.
TEST_LEG_RE = re.compile(r"^\s*uv run (?:pytest|python -m tests\.shuffled_runner)\b")
VITEST_LEG_RE = re.compile(r"^\s*npm run test -- ")

# Rendered jobs that run no test leg of their own, and why.  This is the junit-level
# twin of vpgha_overlay.EXCLUDED_JOBS: vpgha classifies a non-success job with no
# junit artifact as FAIL_INFRA and never PASS, so a rendered job that emits nothing
# is either named here with a reason or is a defect.  Keeping it a table rather than
# a count is the whole point -- a literal ("== 6") went red when
# R-CI-PLATFORM-ORDER-JOB moved legs BETWEEN jobs, which is a ci.yml change the
# overlay handled correctly.
TRUNK_JOBS_WITHOUT_A_TEST_LEG = {
    "platform": "lint + mypy + collection floors only -- R-CI-PLATFORM-ORDER-JOB moved its "
                "coverage run into platform-shards and its order run into platform-order",
    "platform-coverage": "combines the shards' coverage data; it runs no tests",
    "supply-chain": "per-component dependency audit; it runs no tests",
}


def _logical_lines(run):
    """Rendered shell lines, with `\\`-continuations joined -- a leg's flags can
    sit on the line that started it or on the one after."""
    out, buf = [], ""
    for line in str(run or "").split("\n"):
        buf = (buf + " " + line.strip()) if buf else line
        if buf.rstrip().endswith("\\"):
            buf = buf.rstrip()[:-1]
            continue
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _junit_legs(job):
    """(test legs, legs carrying a junit reporter) for one rendered job."""
    total = witnessed = 0
    for st in job.get("steps", []):
        for line in _logical_lines(st.get("run")):
            if TEST_LEG_RE.match(line):
                total += 1
                witnessed += "--junitxml=" in line
            elif VITEST_LEG_RE.match(line):
                total += 1
                witnessed += "--outputFile.junit=" in line
    return total, witnessed


def test_overlay_renders_the_required_jobs_on_the_fleet_with_junit_per_leg():
    text = vpgha_overlay.render(MINI_CI, floor_supports_branch=True)
    doc = yaml.safe_load(text)
    assert doc["name"] == "vp-proof" and doc["run-name"] == "${{ inputs.proof_id || github.ref_name }}"
    on = doc[True]                                       # PyYAML reads `on:` as True
    assert list(on) == ["workflow_dispatch"]
    assert on["workflow_dispatch"]["inputs"]["proof_id"] == {"type": "string", "default": ""}
    assert on["workflow_dispatch"]["inputs"]["run_full_suite"] == {"type": "boolean", "default": True}
    assert doc["concurrency"]["cancel-in-progress"] is False and doc["permissions"] == {"contents": "read"}
    jobs = doc["jobs"]
    assert list(jobs) == EXPECTED_MINI_JOBS, "the gated jobs, in ci.yml declaration order; deploy/gate/eval dropped"
    for jid, job in jobs.items():
        assert job["runs-on"] == ["self-hosted", "voicepod"], jid
        assert job["timeout-minutes"] == vpgha_overlay.JOB_TIMEOUT_MIN == 90, "a hung run ends as timed_out (D83f)"
        assert "if" not in job
        assert job["steps"][0]["run"] == 'mkdir -p "$RUNNER_TEMP/junit"'
        last = job["steps"][-1]
        assert last["if"] == "always()" and last["uses"] == "actions/upload-artifact@deadbeef", "ci.yml's own pin"
        want = "${{ runner.temp }}/junit/*.xml" + ("\n${{ runner.temp }}/junit/*.log" if jid == "platform-shards" else "")
        assert last["with"]["path"] == want and last["with"]["if-no-files-found"] == "ignore"
    assert jobs["platform"]["name"] == "vp/platform" and jobs["platform"]["steps"][-1]["with"]["name"] == "junit-platform"
    assert jobs["platform-shards"]["name"] == "vp/platform-shards-${{ matrix.shard }}"
    assert jobs["platform-shards"]["steps"][-1]["with"]["name"] == "junit-platform-shards-${{ matrix.shard }}"
    assert jobs["supply-chain"]["name"] == "vp/supply-chain-${{ matrix.component }}"
    # pytest legs: junit per leg, xunit1, continuation lines intact; shard leg -n SHARD_WORKERS
    # every pytest-running job asserts linger right after the junit prep (CircleCI Manager, logind RemoveIPC)
    for jid in ("platform", "platform-shards", "agent", "deploy-contracts"):
        st = jobs[jid]["steps"][2 if jid == "deploy-contracts" else 1]     # docker assert sits first there
        assert st["name"].startswith("Assert the runner user has linger"), jid
        assert 'loginctl show-user "$(id -un)" -p Linger --value' in st["run"] and "enable-linger" not in st["run"].split("::error::")[0], "assert only"
    assert not any("linger" in str(st.get("name", "")) for st in jobs["portal"]["steps"]), "no pytest, no linger step"
    # §53: deploy-contracts asserts docker compose first (Oracle runners lack it; ubuntu-latest had it)
    dc = jobs["deploy-contracts"]["steps"]
    assert dc[1]["name"].startswith("Assert docker compose") and "docker compose version" in dc[1]["run"]
    assert "install" not in dc[1]["run"].split("::error::")[0], "assert only"
    assert not any("docker compose" in str(st.get("name", "")) for st in jobs["platform"]["steps"])
    plat = jobs["platform"]["steps"][5]["run"]
    assert ("uv run pytest -q --cov=core --junitxml=${{ runner.temp }}/junit/platform-1.xml -o junit_family=xunit1 \\\n"
            "  --cov-report=json:/tmp/platform-coverage.json --cov-fail-under=80\n") in plat
    assert "check_module_coverage.py" in plat
    # D99: the shuffled (order-dependence) run is pytest.main(argv) -> its own junit leg, no -n rewrite
    assert jobs["platform"]["steps"][6]["run"] == ("uv run python -m tests.shuffled_runner -q "
                                                   "--junitxml=${{ runner.temp }}/junit/platform-2.xml -o junit_family=xunit1")
    # the shard job gets its own pgserver lockfile + tmpfs pgdata (Advisor / 894cdeec)
    # junit prep, linger assert, shm size assert, pgserver prep, checkout, run
    assert jobs["platform-shards"]["steps"][2]["name"].startswith("Assert /dev/shm")
    prep = jobs["platform-shards"]["steps"][3]
    assert prep["run"] == ('rm -rf "/dev/shm/pytest-platform-shards-${{ matrix.shard }}"\n'
                           'mkdir -p "$RUNNER_TEMP/xdg" "/dev/shm/pytest-platform-shards-${{ matrix.shard }}"'), "rm FIRST: a killed run leaks nothing"
    assert jobs["platform-shards"]["steps"][-2] == {"name": "Remove this shard's pgdata dir (vp-proof)", "if": "always()",
                                                    "run": 'rm -rf "/dev/shm/pytest-platform-shards-${{ matrix.shard }}"'}
    assert vpgha_overlay.SHARD_TMP_ON_SHM is True, "WAL cap landed (R-TEST-PG-WAL-CAP-R2 VERIFIED): shards on tmpfs"
    shard_step = jobs["platform-shards"]["steps"][5]
    assert shard_step["env"]["XDG_RUNTIME_DIR"] == "${{ runner.temp }}/xdg"
    assert shard_step["env"]["TMPDIR"] == "/dev/shm/pytest-platform-shards-${{ matrix.shard }}", "tmpfs: WAL cap landed (R-TEST-PG-WAL-CAP-R2)"
    assert shard_step["env"]["COVERAGE_FILE"] == ".coverage.shard-${{ matrix.shard }}", "the candidate's own env kept"
    assert all("env" not in s or "XDG_RUNTIME_DIR" not in s["env"] for s in jobs["platform"]["steps"]), "only the shard job"
    shard = shard_step["run"]
    assert "uv run pytest -q -n 3 --dist loadfile $files --junitxml=${{ runner.temp }}/junit/platform-shards-${{ matrix.shard }}-1.xml -o junit_family=xunit1 \\\n  --cov=core" in shard
    assert "<<'EOF'" in shard and "-n auto" not in shard
    agent = [s["run"] for s in jobs["agent"]["steps"] if s.get("run")]
    assert agent[2].endswith("--junitxml=${{ runner.temp }}/junit/agent-1.xml -o junit_family=xunit1")
    assert "--junitxml=${{ runner.temp }}/junit/agent-2.xml" in agent[3]
    # rule 5: --branch on the `freshness` call only -- `floor`/`control` refuse it
    # ("unrecognized arguments: --branch", canary run 35483670689 vp/platform, D83d)
    assert jobs["platform"]["steps"][3]["run"].endswith('floor --suite platform --baseline-file tests/collection_baseline.json')
    assert jobs["platform"]["steps"][4]["run"].endswith('freshness --baseline-file platform/tests/collection_baseline.json --branch "$GITHUB_REF_NAME"')
    assert agent[4].endswith('control --suite agent --baseline-file ../platform/tests/collection_baseline.json')
    assert jobs["portal"]["steps"][2]["run"].endswith('floor --suite portal --baseline-file ../platform/tests/collection_baseline.json')
    assert text.count('--branch "$GITHUB_REF_NAME"') == 1
    # vitest: junit reporter beside the json one
    assert ("npm run test -- --reporter=json --reporter=junit --outputFile.json=/tmp/portal-test-results.json "
            "--outputFile.junit=${{ runner.temp }}/junit/portal-1.xml") in jobs["portal"]["steps"][1]["run"]
    # D108: the coverage-floor vitest run gets its own junit leg; test:e2e is untouched
    assert jobs["portal"]["steps"][3]["run"] == ("npm run test -- --coverage.enabled --coverage.reporter=json-summary "
                                                 "--reporter=default --reporter=junit "
                                                 "--outputFile.junit=${{ runner.temp }}/junit/portal-2.xml")
    assert jobs["portal"]["steps"][4]["run"] == "npm run test:e2e"
    assert text.count("--outputFile.junit=") == 2
    # matrix and needs survive
    assert jobs["platform-shards"]["strategy"]["matrix"]["shard"] == [0, 1]
    assert jobs["platform-coverage"]["needs"] == ["platform-shards"]
    # without --branch support nothing is appended
    text2 = vpgha_overlay.render(MINI_CI, floor_supports_branch=False)
    assert "--branch" not in text2
    assert vpgha_overlay.floor_supports_branch('p.add_argument("--branch", default=None)') is True
    assert vpgha_overlay.floor_supports_branch("no such flag") is False


def test_overlay_refuses_a_ci_yml_missing_a_required_job():
    """The set is derived from the gate's `needs`, so "ci.yml lacks a required
    job" can no longer fire -- ci.yml cannot lack a member of its own `needs`.
    What replaced it still catches this exact ci.yml: a `needs` entry naming a
    job that is not declared (GitHub rejects such a workflow outright)."""
    with pytest.raises(ValueError, match=r"needs` names undeclared job\(s\): deploy-contracts"):
        vpgha_overlay.render(MINI_CI.replace("  deploy-contracts:\n", "  deploy-contracts-x:\n"))


def test_overlay_classifies_every_ci_yml_job_and_refuses_a_stray():
    """The failure this exists to stop: a job ADDED to ci.yml is silently absent
    from every proof.  A hand-copied set can only notice a REMOVAL; nothing
    noticed `platform-order` appearing (§95 item 1)."""
    rendered, optional, excluded = vpgha_overlay.classify_jobs(yaml.safe_load(MINI_CI))
    assert list(rendered) == EXPECTED_MINI_JOBS
    assert list(optional) == []                      # MINI_CI declares no platform-order
    assert list(excluded) == ["kb-real-provider-eval", "deploy", "required-checks"]
    assert len(rendered) + len(optional) + len(excluded) == len(yaml.safe_load(MINI_CI)["jobs"]), \
        "every job placed exactly once"
    # a new job nobody has ruled on stops the render and names itself...
    stray = MINI_CI.replace("  deploy:\n", "  brand-new-suite:\n    runs-on: ubuntu-latest\n"
                                            "    steps:\n      - run: echo hi\n  deploy:\n")
    with pytest.raises(ValueError, match="classified nowhere: brand-new-suite"):
        vpgha_overlay.render(stray)
    # ...and the SAME ci.yml goes green once the gate requires it, rendering it
    # in declaration order rather than dropping it (the positive control: without
    # this half, the test above passes for any reason at all)
    gated = stray.replace("needs: [platform, platform-shards",
                          "needs: [brand-new-suite, platform, platform-shards")
    doc = yaml.safe_load(vpgha_overlay.render(gated))
    assert list(doc["jobs"]) == EXPECTED_MINI_JOBS + ["brand-new-suite"], \
        "declared after supply-chain, so rendered last"


def test_overlay_refuses_a_ci_yml_that_states_no_gate():
    with pytest.raises(ValueError, match="declares no 'required-checks' job"):
        vpgha_overlay.render(MINI_CI.replace("  required-checks:\n", "  renamed-gate:\n"))
    with pytest.raises(ValueError, match="empty `needs`"):
        vpgha_overlay.render(MINI_CI.replace(
            "    needs: [platform, platform-shards, platform-coverage, agent, portal, "
            "deploy-contracts, supply-chain, deploy]\n", "    needs: []\n"))


ORDER_JOB = """
  platform-order:
    runs-on: ubuntu-latest
    needs: [platform, deploy]
    if: github.event_name == 'push'
    steps:
      - uses: actions/checkout@v4
      - name: Order-dependence run
        run: |
          cd platform
          uv run python -m tests.shuffled_runner -q
"""


def test_overlay_renders_platform_order_only_when_present():
    # §95 item 1 (R-CI-PLATFORM-ORDER-JOB): required-when-present -- the plain
    # MINI_CI renders without it and without raising...
    base = yaml.safe_load(vpgha_overlay.render(MINI_CI))
    assert "platform-order" not in base["jobs"]
    assert list(base["jobs"]) == EXPECTED_MINI_JOBS
    # ...and a ci.yml carrying the job renders it after the required set, on the
    # fleet, with its junit leg, and with `needs` pruned to rendered jobs only
    # (a needs on the dropped `deploy` would make GitHub reject the workflow).
    # D100: even when present it is rendered only for a FINAL canary (order=True) or only=
    inter = yaml.safe_load(vpgha_overlay.render(MINI_CI + ORDER_JOB))
    assert "platform-order" not in inter["jobs"], "an intermediate canary omits the ~80-min order job"
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI + ORDER_JOB, order=True))
    assert list(doc["jobs"]) == EXPECTED_MINI_JOBS + ["platform-order"]
    job = doc["jobs"]["platform-order"]
    assert job["name"] == "vp/platform-order" and job["runs-on"] == ["self-hosted", "voicepod"]
    assert job["needs"] == ["platform"] and "if" not in job
    assert job["timeout-minutes"] == vpgha_overlay.JOB_TIMEOUT_MIN
    runs = [str(st.get("run", "")) for st in job["steps"]]
    assert any("--junitxml=${{ runner.temp }}/junit/platform-order-1.xml" in r for r in runs)
    assert job["steps"][-1]["with"]["name"] == "junit-platform-order"


def test_overlay_drops_a_needs_that_only_named_dropped_jobs():
    txt = MINI_CI + ORDER_JOB.replace("needs: [platform, deploy]", "needs: [deploy]")
    doc = yaml.safe_load(vpgha_overlay.render(txt, order=True))
    assert "needs" not in doc["jobs"]["platform-order"]


def test_overlay_only_narrows_to_one_shard_or_job():
    # §95 item 2: only=platform-shards-1 renders that job alone with matrix [1]
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI, only="platform-shards-1"))
    assert list(doc["jobs"]) == ["platform-shards"]
    job = doc["jobs"]["platform-shards"]
    assert job["strategy"]["matrix"]["shard"] == [1]
    assert job["name"] == "vp/platform-shards-${{ matrix.shard }}", "the job/artifact names keep their shape"
    assert job["steps"][-1]["with"]["name"] == "junit-platform-shards-${{ matrix.shard }}"
    # only=<job> renders that job alone; a needs on an unrendered job is pruned
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI + ORDER_JOB, only="platform-order"))
    assert list(doc["jobs"]) == ["platform-order"] and "needs" not in doc["jobs"]["platform-order"]
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI, only="portal"))
    assert list(doc["jobs"]) == ["portal"]


def test_overlay_only_refuses_an_unknown_job_or_leg():
    with pytest.raises(ValueError, match="no matrix leg 7"):
        vpgha_overlay.render(MINI_CI, only="platform-shards-7")
    with pytest.raises(ValueError, match="names no rendered job"):
        vpgha_overlay.render(MINI_CI, only="deploy")
    with pytest.raises(ValueError, match="names no rendered job"):
        vpgha_overlay.render(MINI_CI, only="platform-order")     # absent from this ci.yml


def test_overlay_on_tmpfs_asserts_the_shm_size_and_never_remounts(monkeypatch):
    """Advisor 2026-09-20: the tmpfs size is the runner image's (/etc/fstab);
    the job only ASSERTS it (clear failure), never remounts; pgdata under
    /dev/shm/pytest-platform-shards-<N> is rm -rf'd first and last."""
    monkeypatch.setattr(vpgha_overlay, "SHARD_TMP_ON_SHM", True)
    monkeypatch.setattr(vpgha_overlay, "SHARD_DIR", vpgha_overlay._SHARD_DIR_SHM)
    monkeypatch.setattr(vpgha_overlay, "SHARD_ENV", {"XDG_RUNTIME_DIR": "${{ runner.temp }}/xdg", "TMPDIR": vpgha_overlay._SHARD_DIR_SHM})
    monkeypatch.setattr(vpgha_overlay, "SHARD_PREP", {"name": "prep", "run": 'rm -rf "%s"\nmkdir -p "$RUNNER_TEMP/xdg" "%s"' % ((vpgha_overlay._SHARD_DIR_SHM,) * 2)})
    monkeypatch.setattr(vpgha_overlay, "SHARD_CLEANUP", {"name": "clean", "if": "always()", "run": 'rm -rf "%s"' % vpgha_overlay._SHARD_DIR_SHM})
    jobs = yaml.safe_load(vpgha_overlay.render(MINI_CI))["jobs"]
    steps = jobs["platform-shards"]["steps"]
    assert steps[1]["name"].startswith("Assert the runner user has linger")
    assert steps[2]["name"].startswith("Assert /dev/shm")
    assert "df -Pk /dev/shm" in steps[2]["run"] and "-ge %d" % (vpgha_overlay.SHARD_SHM_MIN_GB * 1024 * 1024) in steps[2]["run"]
    assert "mount" not in steps[2]["run"], "assert, never mutate"
    assert steps[3]["run"].startswith('rm -rf "/dev/shm/pytest-platform-shards-${{ matrix.shard }}"')
    assert steps[5]["env"]["TMPDIR"] == "/dev/shm/pytest-platform-shards-${{ matrix.shard }}"
    assert steps[-2]["if"] == "always()" and steps[-2]["run"] == 'rm -rf "/dev/shm/pytest-platform-shards-${{ matrix.shard }}"'
    assert not any("Assert /dev/shm" in str(st.get("name")) for st in jobs["platform"]["steps"])


@pytest.mark.skipif(not CI_YML.exists(), reason="trunk ci.yml not checked out")
def test_overlay_renders_from_the_real_trunk_ci_yml():
    text = vpgha_overlay.render(CI_YML.read_text(encoding="utf-8"), floor_supports_branch=False)
    doc = yaml.safe_load(text)
    assert list(doc["jobs"]) == EXPECTED_TRUNK_JOBS, "the real gate needs, read independently"
    # No hardcoded leg count.  "== 6" was the same stale-copy defect as the old
    # REQUIRED_JOBS: it went red because R-CI-PLATFORM-ORDER-JOB moved legs between
    # jobs, a ci.yml change the overlay handled correctly.  What must hold is the
    # MECHANISM -- a rendered test leg with no junit reporter turns a red suite into
    # a FAIL_INFRA verdict instead of a failure anyone reads.
    per_job = {jid: _junit_legs(job) for jid, job in doc["jobs"].items()}
    assert sum(total for total, _ in per_job.values()) >= 3, \
        "TEST_LEG_RE matched almost nothing -- every assertion below would pass vacuously"
    naked = {jid: total - seen for jid, (total, seen) in per_job.items() if total != seen}
    assert not naked, "rendered test leg(s) with no junit reporter: %s" % naked
    # every rendered job either emits junit or is named, with a reason, as running
    # no tests -- the honest form of "the contributing set is the rendered set",
    # which is FALSE here: three trunk jobs run no tests at all
    silent = {jid for jid, (total, _) in per_job.items() if not total}
    assert silent == set(TRUNK_JOBS_WITHOUT_A_TEST_LEG), (
        "a rendered job started or stopped running tests: %s. A non-success job with no "
        "junit artifact classifies FAIL_INFRA, never PASS -- give it a junit leg, or add "
        "it to TRUNK_JOBS_WITHOUT_A_TEST_LEG with a reason."
        % sorted(silent ^ set(TRUNK_JOBS_WITHOUT_A_TEST_LEG)))
    assert text.count("-n 3 --dist loadfile") == 1 and "-n auto" not in text
    # the FINAL-canary shape (order=True), derived the same way: the order job is
    # `uv run python -m tests.shuffled_runner`, a pytest leg that is not spelled
    # "pytest" -- a leg detector keyed on that word alone would miss it entirely
    final = yaml.safe_load(vpgha_overlay.render(CI_YML.read_text(encoding="utf-8"), order=True))
    assert _junit_legs(final["jobs"]["platform-order"]) == (1, 1)
    naked_final = {jid: t - s for jid, (t, s) in
                   ((j, _junit_legs(b)) for j, b in final["jobs"].items()) if t != s}
    assert not naked_final, naked_final
    assert text.count("XDG_RUNTIME_DIR: ${{ runner.temp }}/xdg") == 1


def _repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    sh = lambda *a: subprocess.run(["git", "-C", str(r)] + list(a), check=True, capture_output=True, text=True)  # noqa: E731
    sh("init", "-q", "-b", "main")
    sh("config", "user.email", "t@t")
    sh("config", "user.name", "t")
    (r / ".github" / "workflows").mkdir(parents=True)
    (r / ".github" / "workflows" / "ci.yml").write_text(MINI_CI)
    (r / "scripts").mkdir()
    (r / "scripts" / "ci_collection_floor.py").write_text('p.add_argument("--branch")\n')
    (r / "platform").mkdir()
    (r / "platform" / "a.py").write_text("x = 1\n")
    sh("add", "-A")
    sh("commit", "-q", "-m", "cand")
    cand = sh("rev-parse", "HEAD").stdout.strip()
    (r / "platform" / "a.py").write_text("x = 2\n")            # dirty worktree file: must stay untouched
    return r, cand, sh


def test_prepare_measured_adds_exactly_the_workflow_and_leaves_the_worktree_alone(tmp_path):
    r, cand, sh = _repo(tmp_path)
    runner = vpgha.Runner()
    measured = vpgha.prepare_measured(r, cand, runner)
    assert measured != cand and len(measured) == 40
    assert sh("diff", "--name-only", cand, measured).stdout.split() == [".github/workflows/vp-proof.yml"]
    assert sh("rev-parse", "HEAD").stdout.strip() == cand, "HEAD untouched"
    assert (r / "platform" / "a.py").read_text() == "x = 2\n", "working file untouched"
    assert sh("status", "--porcelain").stdout.strip() == "M platform/a.py", "index untouched"
    text = sh("show", "%s:.github/workflows/vp-proof.yml" % measured).stdout
    assert "--branch \"$GITHUB_REF_NAME\"" in text, "rendered from the candidate's own script (accepts --branch)"
    assert yaml.safe_load(text)["jobs"]["platform"]["runs-on"] == ["self-hosted", "voicepod"]
    # idempotent per content: the same candidate yields the same tree
    again = vpgha.prepare_measured(r, cand, runner)
    assert sh("rev-parse", "%s^{tree}" % again).stdout == sh("rev-parse", "%s^{tree}" % measured).stdout
    # rule 2: a measured commit that differs by anything else is refused (no trigger)
    real = runner.git

    def lying_git(args, cwd=None, env=None):
        res = real(args, cwd=cwd, env=env)
        if args[:2] == ["diff", "--name-only"]:
            res.stdout = res.stdout + "platform/a.py\n"
        return res
    runner.git = lying_git
    with pytest.raises(vpgha.OverlayDirty, match="platform/a.py"):
        vpgha.prepare_measured(r, cand, runner)


class FakeGh(object):
    """gh CLI answers by subcommand; records every argv"""

    def __init__(self, run_rows=None, views=None, artifacts=None, downloads=None, dispatch_rc=0, rows_before=None):
        self.calls = []
        self.run_rows = run_rows or []
        # D103: what `gh run list` shows BEFORE the dispatch (an earlier attempt's runs)
        self.rows_before = rows_before
        self.dispatched = False
        self.views = list(views or [])
        self.artifacts = artifacts or []
        self.downloads = downloads or {}
        self.dispatch_rc = dispatch_rc

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        a = argv[1:]
        if a[:2] == ["workflow", "run"]:
            self.dispatched = not self.dispatch_rc
            return subprocess.CompletedProcess(argv, self.dispatch_rc, "", "" if not self.dispatch_rc else "HTTP 404")
        if a[:2] == ["run", "list"]:
            rows = self.run_rows if (self.dispatched or self.rows_before is None) else self.rows_before
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")
        if a[:2] == ["run", "view"]:
            v = self.views.pop(0) if len(self.views) > 1 else self.views[0]
            return subprocess.CompletedProcess(argv, 0, json.dumps(v), "")
        if a[0] == "api" and "artifacts" in a[2]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.artifacts), "")
        if a[:2] == ["run", "download"]:
            name = a[a.index("-n") + 1]
            dest = Path(a[a.index("-D") + 1])
            dest.mkdir(parents=True, exist_ok=True)
            for fn, text in (self.downloads.get(name) or {}).items():
                (dest / fn).write_text(text)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if a[:2] == ["run", "cancel"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if a[0] == "api" and "workflows" in a[1]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        raise AssertionError("unexpected gh %s" % a)


JUNIT_RED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3" failures="1" errors="1">
<testcase classname="tests.test_x" file="tests/test_x.py" line="3" name="test_a[p1]" time="0.1">
  <failure message="AssertionError: boom">trace</failure></testcase>
<testcase classname="tests.test_x" file="tests/test_x.py" line="9" name="test_b" time="0.1">
  <error message="fixture blew up">trace</error></testcase>
<testcase classname="tests.test_x" file="tests/test_x.py" line="12" name="test_c" time="0.1"/>
</testsuite></testsuites>"""


def test_trigger_finds_its_run_by_run_name_and_poll_reads_junit_into_reds(monkeypatch):
    gh = FakeGh(
        rows_before=[{"databaseId": 111, "displayTitle": "vp/proof/other", "createdAt": "2026-09-19T20:00:00Z"}],
        run_rows=[{"databaseId": 111, "displayTitle": "vp/proof/other", "createdAt": "2026-09-19T20:00:00Z"},
                  {"databaseId": 222, "displayTitle": "vp/proof/proof-1-abc", "createdAt": "2026-09-19T20:01:00Z"},
                  {"databaseId": 223, "displayTitle": "vp/proof/proof-1-abc", "createdAt": "2026-09-19T20:02:00Z"}],
        views=[{"status": "in_progress", "conclusion": None, "jobs": []},
               {"status": "completed", "conclusion": "failure", "headSha": "abc", "url": "u", "jobs": [
                   {"databaseId": 1, "name": "vp/platform", "status": "completed", "conclusion": "failure"},
                   {"databaseId": 2, "name": "vp/platform-shards-3", "status": "completed", "conclusion": "failure"},
                   {"databaseId": 3, "name": "vp/agent", "status": "completed", "conclusion": "success"},
                   {"databaseId": 4, "name": "vp/portal", "status": "completed", "conclusion": "skipped"}]}],
        artifacts=[{"name": "junit-platform", "id": 9, "expired": False},
                   {"name": "junit-platform-shards-3", "id": 10, "expired": False}],
        downloads={"junit-platform": {"platform-1.xml": JUNIT_RED},
                   "junit-platform-shards-3": {"platform-shards-3-1.xml": JUNIT_RED.replace("failures=\"1\" errors=\"1\"", "failures=\"0\" errors=\"0\"").replace("<failure", "<skipped").replace("</failure>", "</skipped>").replace("<error", "<skipped").replace("</error>", "</skipped>")}},
    )
    runner = vpgha.Runner(run=gh, binary="gh")
    sleeps = []
    trig = vpgha.trigger("vp/proof/proof-1-abc", {"run_full_suite": True}, runner, sleep=sleeps.append)
    assert trig == {"pipeline_id": "223", "account": "gha"}, "the newest run named after the branch"
    dispatch = next(c for c in gh.calls if c[1:3] == ["workflow", "run"])
    assert dispatch[3:] == ["vp-proof.yml", "-R", vpgha.REPO, "--ref", "vp/proof/proof-1-abc",
                            "-f", "proof_id=vp/proof/proof-1-abc", "-f", "run_full_suite=true"]
    assert sleeps == []
    res = vpgha.poll("223", interval=60, deadline_s=5400, runner=runner, sleep=sleeps.append, clock=lambda: 0.0)
    assert sleeps == [60]
    assert res["pipeline_id"] == "223" and res["workflows"][0]["status"] == "failed"
    by = {j["name"]: j for j in res["jobs"]}
    assert by["vp/platform"]["status"] == "failed" and by["vp/agent"]["status"] == "success"
    assert by["vp/portal"]["status"] == "infrastructure_fail", "GHA 'skipped' = a needs failed; never green"
    assert sorted(res["failed_tests"]) == [1, 2], "junit fetched for the non-success jobs only"
    reds = res["failed_tests"][1]
    assert [(t["name"], t["result"], t["file"]) for t in reds] == [("test_a[p1]", "failure", "tests/test_x.py"),
                                                                     ("test_b", "error", "tests/test_x.py")]
    assert res["failed_tests"][2] == [], "a failed job whose junit has no reds"
    downloads = [c for c in gh.calls if c[1:3] == ["run", "download"]]
    assert sorted(c[c.index("-n") + 1] for c in downloads) == ["junit-platform", "junit-platform-shards-3"]
    # the CircleCI classifier reads the mapped shape: reds -> FAIL_PRODUCT with nodes
    cls = vpcircle.classify(res["jobs"], res["failed_tests"], workflows=res["workflows"])
    assert cls["status"] == "FAIL_PRODUCT"
    kinds = {r["job"]: r["kind"] for r in cls["reds"]}
    assert kinds == {"vp/platform": "product", "vp/platform-shards-3": "infra", "vp/portal": "infra"}, \
        "failed with junit but zero reds = infra (rule 3), never PASS"


def test_a_missing_junit_artifact_makes_a_failed_job_infra_and_a_cancelled_run_is_cancelled():
    view_failed = {"status": "completed", "conclusion": "failure", "jobs": [
        {"databaseId": 1, "name": "vp/platform", "status": "completed", "conclusion": "failure"}]}
    gh = FakeGh(views=[view_failed], artifacts=[], downloads={})
    res = vpgha.poll("5", runner=vpgha.Runner(run=gh, binary="gh"), sleep=lambda s: None, clock=lambda: 0.0)
    assert res["failed_tests"] == {} and not [c for c in gh.calls if c[1:3] == ["run", "download"]]
    assert vpcircle.classify(res["jobs"], res["failed_tests"], workflows=res["workflows"])["status"] == "FAIL_INFRA"
    view_cancelled = {"status": "completed", "conclusion": "cancelled", "jobs": [
        {"databaseId": 1, "name": "vp/platform", "status": "completed", "conclusion": "cancelled"},
        {"databaseId": 2, "name": "vp/agent", "status": "completed", "conclusion": "success"}]}
    gh = FakeGh(views=[view_cancelled], artifacts=[], downloads={})
    res = vpgha.poll("6", runner=vpgha.Runner(run=gh, binary="gh"), sleep=lambda s: None, clock=lambda: 0.0)
    assert vpcircle.classify(res["jobs"], res["failed_tests"], workflows=res["workflows"])["status"] == "CANCELLED"
    view_green = {"status": "completed", "conclusion": "success", "jobs": [
        {"databaseId": 1, "name": "vp/platform", "status": "completed", "conclusion": "success"}]}
    gh = FakeGh(views=[view_green])
    res = vpgha.poll("7", runner=vpgha.Runner(run=gh, binary="gh"), sleep=lambda s: None, clock=lambda: 0.0)
    assert vpcircle.classify(res["jobs"], res["failed_tests"], workflows=res["workflows"])["status"] == "PASS"
    assert vpgha.cancel_pipeline("7", vpgha.Runner(run=gh, binary="gh")) == ["7"]


def test_trigger_failures_and_poll_transients_surface_or_retry():
    gh = FakeGh(dispatch_rc=1)
    with pytest.raises(RuntimeError, match="gh workflow run failed"):
        vpgha.trigger("vp/proof/x", {}, vpgha.Runner(run=gh, binary="gh"), sleep=lambda s: None)
    gh = FakeGh(run_rows=[])
    with pytest.raises(RuntimeError, match="no vp-proof run named vp/proof/x appeared"):
        vpgha.find_run("vp/proof/x", vpgha.Runner(run=gh, binary="gh"), tries=3, wait_s=1, sleep=lambda s: None)
    assert len([c for c in gh.calls if c[1:3] == ["run", "list"]]) == 3
    # a transient `gh run view` failure is retried (vpcircle.POLL_TRANSIENT_MAX), then answered
    n = {"i": 0}

    def flaky(argv, **kw):
        if argv[1:3] == ["run", "view"]:
            n["i"] += 1
            if n["i"] <= 2:
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 502")
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"status": "completed", "conclusion": "success", "jobs": []}), "")
        return subprocess.CompletedProcess(argv, 0, "[]", "")
    sleeps = []
    res = vpgha.poll("9", interval=30, runner=vpgha.Runner(run=flaky, binary="gh"), sleep=sleeps.append,
                     clock=lambda: 0.0)
    assert res["workflows"][0]["status"] == "success" and sleeps == [30, 30]
    assert vpgha.project_visible(vpgha.Runner(run=FakeGh(), binary="gh")) == (True, "gha")


def test_fleet2_preflight_renders_the_platform_job_with_one_pytest_of_the_lanes_files():
    """Fleet-2 (§98): only=preflight:<paths> renders `platform-preflight` alone:
    the platform job's setup kept, floor/coverage/shuffled/suite steps replaced
    by one serial `uv run pytest -q <paths>` with its junit leg."""
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI, only="preflight:platform/tests/test_a.py,tests/test_b.py"))
    assert list(doc["jobs"]) == ["platform-preflight"]
    job = doc["jobs"]["platform-preflight"]
    assert job["name"] == "vp/platform-preflight" and "strategy" not in job
    runs = [str(st.get("run", "")) for st in job["steps"]]
    assert sum("uv run pytest" in r for r in runs) == 1
    assert any(r.startswith("uv run pytest -q tests/test_a.py tests/test_b.py --junitxml=${{ runner.temp }}/junit/platform-preflight-1.xml")
               for r in runs), "D106: repo-relative platform/tests/... becomes tests/... in the platform working dir"
    step = next(st for st in job["steps"] if "uv run pytest" in str(st.get("run", "")))
    assert step["working-directory"] == "platform" and "platform/tests/" not in step["run"]
    assert not any("ci_collection_floor" in r or "shuffled_runner" in r or "check_module_coverage" in r for r in runs)
    assert job["steps"][-1]["with"]["name"] == "junit-platform-preflight"
    with pytest.raises(ValueError, match="names no test paths"):
        vpgha_overlay.render(MINI_CI, only="preflight:")


def test_shard_workers_and_pg_stagger_come_from_the_roster_and_touch_the_shard_job_only():
    """R-TEST-PG-STAGGER pre-stage: roster proof.circleci.shard_workers rewrites the
    shard leg's -n; shard_stagger_s sets VOICEPOD_PG_START_STAGGER_SECONDS on the
    shard pytest step only (unset by default: local and other jobs untouched)."""
    base = yaml.safe_load(vpgha_overlay.render(MINI_CI))
    shard_steps = base["jobs"]["platform-shards"]["steps"]
    run = next(st for st in shard_steps if "uv run pytest" in str(st.get("run", "")))
    assert "-n 3 --dist loadfile" in run["run"] and vpgha_overlay.STAGGER_ENV not in run["env"]
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI, shard_workers=6, shard_stagger_s=1.25))
    run = next(st for st in doc["jobs"]["platform-shards"]["steps"] if "uv run pytest" in str(st.get("run", "")))
    assert "-n 6 --dist loadfile" in run["run"] and run["env"][vpgha_overlay.STAGGER_ENV] == "1.25"
    text = vpgha_overlay.render(MINI_CI, shard_workers=6, shard_stagger_s=1.25)
    assert text.count(vpgha_overlay.STAGGER_ENV) == 1, "the shard job only"
    doc0 = yaml.safe_load(vpgha_overlay.render(MINI_CI, shard_stagger_s=0))
    run0 = next(st for st in doc0["jobs"]["platform-shards"]["steps"] if "uv run pytest" in str(st.get("run", "")))
    assert vpgha_overlay.STAGGER_ENV not in run0["env"], "0 = unset"


def test_d103_a_retriggered_proof_never_adopts_its_earlier_attempts_run():
    """D103 (14:32Z): L06-HOSTED-R9's retry re-used its branch name; `gh run list`
    still held the cancelled 35513112276 and find_run returned it (newest by
    createdAt at that instant) while the real re-trigger 35516791190 ran
    unobserved -> a second PROOF_CANCELLED strike on a run that was never
    polled.  Runs listed before the dispatch, and completed runs, are skipped;
    the new run is waited for."""
    old = {"databaseId": 35513112276, "displayTitle": "vp/proof/p-R9-4c72", "createdAt": "2026-09-20T13:18:29Z",
           "status": "completed"}
    new = {"databaseId": 35516791190, "displayTitle": "vp/proof/p-R9-4c72", "createdAt": "2026-09-20T14:32:20Z",
           "status": "queued"}
    gh = FakeGh(rows_before=[old], run_rows=[old, new])
    runner = vpgha.Runner(run=gh, binary="gh")
    trig = vpgha.trigger("vp/proof/p-R9-4c72", {"run_full_suite": True}, runner, sleep=lambda s: None)
    assert trig["pipeline_id"] == "35516791190"
    # the new run not yet visible: wait, never fall back to the old one
    class Late(FakeGh):
        n = 0
        def __call__(self, argv, **kw):
            if argv[1:3] == ["run", "list"] and self.dispatched:
                Late.n += 1
                if Late.n < 3:
                    return subprocess.CompletedProcess(argv, 0, json.dumps([old]), "")
            return FakeGh.__call__(self, argv, **kw)
    gh = Late(rows_before=[old], run_rows=[old, new])
    sleeps = []
    trig = vpgha.trigger("vp/proof/p-R9-4c72", {"run_full_suite": True}, vpgha.Runner(run=gh, binary="gh"),
                         sleep=sleeps.append)
    assert trig["pipeline_id"] == "35516791190" and sleeps == [5, 5]
    # a completed run of the same name is never the answer even when nothing else appears
    gh = FakeGh(rows_before=[], run_rows=[old])
    with pytest.raises(RuntimeError, match="no vp-proof run named"):
        vpgha.trigger("vp/proof/p-R9-4c72", {}, vpgha.Runner(run=gh, binary="gh"), sleep=lambda s: None)


def test_d105_the_pgserver_lock_log_rides_the_shard_artifact():
    """D105: VOICEPOD_PGSERVER_LOCK_LOG on the shard pytest step points into the
    junit dir (per shard) and the shard artifact uploads *.log beside *.xml;
    other jobs are untouched (junit_by_job reads *.xml only)."""
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI))
    run = next(st for st in doc["jobs"]["platform-shards"]["steps"] if "uv run pytest" in str(st.get("run", "")))
    assert run["env"]["VOICEPOD_PGSERVER_LOCK_LOG"] == "${{ runner.temp }}/junit/pgserver-lock-${{ matrix.shard }}.log"
    text = vpgha_overlay.render(MINI_CI)
    assert text.count("VOICEPOD_PGSERVER_LOCK_LOG") == 1
    assert doc["jobs"]["platform"]["steps"][-1]["with"]["path"] == "${{ runner.temp }}/junit/*.xml"


def test_d112_host_pins_every_job_of_a_full_overlay_but_never_an_only_run():
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI, host="voicepod-b"))
    assert all(j["runs-on"] == ["self-hosted", "voicepod-b"] for j in doc["jobs"].values())
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI, host="voicepod-b", only="portal"))
    assert doc["jobs"]["portal"]["runs-on"] == ["self-hosted", "voicepod"]
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI, host="voicepod-b", only="preflight:tests/test_a.py"))
    assert doc["jobs"]["platform-preflight"]["runs-on"] == ["self-hosted", "voicepod"]


def test_d113_a_scoped_twin_renders_one_platform_twin_job_with_xdist_junit_and_no_cov():
    """D113 (§114): only=twin:<n>:<paths> renders `platform-twin` alone: the
    platform job's setup kept, the suite steps replaced by one
    `uv run pytest -q -n <n> <paths>` with junit, no --cov (class I cannot touch
    it), a per-worker Postgres dir keyed on the run id (no matrix), the shared
    runner label (any host: only full pipelines are D112-pinned)."""
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI, only="twin:3:platform/tests/test_a.py,tests/test_b.py",
                                              host="voicepod-b"))
    assert list(doc["jobs"]) == ["platform-twin"]
    job = doc["jobs"]["platform-twin"]
    assert job["name"] == "vp/platform-twin" and "strategy" not in job
    assert job["runs-on"] == ["self-hosted", "voicepod"], "a scoped twin may land on any host"
    runs = [str(st.get("run", "")) for st in job["steps"]]
    assert sum("uv run pytest" in r for r in runs) == 1
    step = next(st for st in job["steps"] if "uv run pytest" in str(st.get("run", "")))
    assert step["run"].startswith("uv run pytest -q -n 3 tests/test_a.py tests/test_b.py "
                                  "--junitxml=${{ runner.temp }}/junit/platform-twin-1.xml")
    assert "--cov" not in step["run"] and step["working-directory"] == "platform"
    assert step["env"]["TMPDIR"] == "/dev/shm/pytest-platform-shards-twin-${{ github.run_id }}"
    assert step["env"][vpgha_overlay.LOCK_LOG_ENV].endswith("pgserver-lock-twin-${{ github.run_id }}.log")
    assert "matrix.shard" not in vpgha_overlay.render(MINI_CI, only="twin:3:tests/test_a.py")
    assert any("/dev/shm/pytest-platform-shards-twin-${{ github.run_id }}" in r and r.startswith("rm -rf") for r in runs)
    assert not any("ci_collection_floor" in r or "shuffled_runner" in r or "check_module_coverage" in r for r in runs)
    up = job["steps"][-1]["with"]
    assert up["name"] == "junit-platform-twin" and "*.log" in up["path"]
    with pytest.raises(ValueError, match="twin:<workers>"):
        vpgha_overlay.render(MINI_CI, only="twin:tests/test_a.py")
    with pytest.raises(ValueError, match="twin:<workers>"):
        vpgha_overlay.render(MINI_CI, only="twin:3:")


def test_d114_targeted_renders_the_twin_shaped_job_under_its_own_name():
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI, only="targeted:2:platform/tests/test_a.py::test_x"))
    assert list(doc["jobs"]) == ["platform-targeted"]
    job = doc["jobs"]["platform-targeted"]
    step = next(st for st in job["steps"] if "uv run pytest" in str(st.get("run", "")))
    assert step["run"].startswith("uv run pytest -q -n 2 tests/test_a.py::test_x --junitxml=")
    assert step["env"]["TMPDIR"] == "/dev/shm/pytest-platform-shards-targeted-${{ github.run_id }}"
    assert job["steps"][-1]["with"]["name"] == "junit-platform-targeted"
    assert vpgha_overlay.scoped_spec("portal") is None
    with pytest.raises(ValueError, match="targeted:<workers>"):
        vpgha_overlay.render(MINI_CI, only="targeted:x:tests/a.py")


def test_d118b_junit_case_count_counts_every_testcase():
    xml = ('<testsuites><testsuite tests="3"><testcase classname="a" name="t1" file="tests/a.py"/>'
           '<testcase classname="a" name="t2" file="tests/a.py"><failure message="x"/></testcase>'
           '<testcase classname="a" name="t3" file="tests/a.py"><skipped/></testcase></testsuite></testsuites>')
    assert vpgha.junit_case_count(xml) == 3
    assert vpgha.junit_case_count("<testsuite/>") == 0


# -- D162: the rendered step's own spelling is what decides whether a path runs --

def test_d162_wd_relative_is_the_one_copy_of_the_strip_rule():
    """D162. `preflight_job` used to inline this three-line expression, so the
    check in `unrunnable_paths` could only be written as a SECOND copy of it --
    and a check that re-implements the thing it checks agrees with itself
    forever. Both now read the same helper.

    The pass-through arm is the deliberate one: a wd-relative spelling is a
    legitimate form and test_overlay_only_preflight_renders_one_serial_pytest_job
    pins a mixed-form `only` end to end."""
    import vpgha_overlay as ov

    assert ov.wd_relative("platform", ["platform/tests/a.py"]) == ["tests/a.py"]
    assert ov.wd_relative("platform", ["tests/b.py"]) == ["tests/b.py"], (
        "a wd-relative path passes through unchanged -- deliberate, pinned elsewhere")
    assert ov.wd_relative("platform", ["agent/tests/x.py"]) == ["agent/tests/x.py"], (
        "D106's gap: another package's repo-relative path is NOT stripped")
    assert ov.wd_relative("platform", ["platformish/tests/c.py"]) == ["platformish/tests/c.py"], (
        "the prefix test is on `wd + '/'`, never a bare substring")


def test_d162_unrunnable_paths_names_the_declared_spelling_that_will_not_run():
    """D162. The rendered step runs `uv run pytest -q <rel>` with
    `working-directory: platform`, so the file pytest opens is `platform/<rel>`.
    A repo-relative path from another package resolves to
    `platform/agent/tests/x.py`, matches nothing, and pytest exits 5 -- which the
    classifier reads as infrastructure_fail rather than the packet defect it is
    (D106, run 35518327671).

    The returned spelling is the DECLARED one, because that is the string a
    packet author would have to change."""
    import vpgha_overlay as ov

    tree = {"platform/tests/a.py", "platform/tests/b.py", "agent/tests/x.py"}
    exists = lambda p: p in tree                                        # noqa: E731

    paths = ["platform/tests/a.py", "tests/b.py", "agent/tests/x.py"]
    got = ov.unrunnable_paths(paths, "platform", exists)

    assert got == ["agent/tests/x.py"], got
    assert "tests/b.py" not in got, (
        "the wd-relative form resolves to platform/tests/b.py and runs fine")
    assert "platform/tests/a.py" not in got


def test_d162_a_path_that_exists_in_the_repo_but_not_under_the_wd_is_still_unrunnable():
    """D162's load-bearing control. `agent/tests/x.py` IS in the tree -- the
    packet did not invent it -- and it still cannot run, because the step is
    chdir'd into `platform`. Checking "does this path exist in the repo" instead
    of "does <wd>/<rel> exist" would clear exactly the case D106 is about, and
    would be green against every packet that ever declared a real file."""
    import vpgha_overlay as ov

    tree = {"agent/tests/x.py"}
    assert ov.unrunnable_paths(["agent/tests/x.py"], "platform", lambda p: p in tree) \
        == ["agent/tests/x.py"]
    assert ov.unrunnable_paths(["agent/tests/x.py"], "agent", lambda p: p in tree) == [], (
        "under working-directory: agent the same path resolves and runs")


def test_d162_reports_nothing_when_every_path_resolves():
    """D162 control: the checker must be silent on the ordinary case, or it would
    refuse every proof in the fleet."""
    import vpgha_overlay as ov

    tree = {"platform/tests/a.py", "platform/tests/b.py"}
    assert ov.unrunnable_paths(["platform/tests/a.py", "tests/b.py"], "platform",
                               lambda p: p in tree) == []


def test_d162_prepare_measured_refuses_paths_the_rendered_step_cannot_find(tmp_path):
    """D162 end to end, at the place the ruling put it: render time, where the
    candidate's own tree is in hand and no pipeline has been spent.

    D106 stripped `platform/` and passed everything else through, so a
    repo-relative path from another package was handed to pytest inside
    `platform/`, matched nothing, and exited 5 -- which `classify` reads as
    infrastructure_fail rather than the packet defect it is (run 35518327671).

    `agent/tests/x.py` below EXISTS in the repo. That is the control: a check
    that asked "is this path in the tree" would clear exactly the case this is
    about, and would be green against every packet that ever declared a real
    file. What matters is whether `<working-directory>/<rel>` resolves."""
    r, _cand, sh = _repo(tmp_path)
    (r / "platform" / "tests").mkdir()
    (r / "platform" / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (r / "agent" / "tests").mkdir(parents=True)
    (r / "agent" / "tests" / "x.py").write_text("def test_x(): pass\n")
    sh("add", "-A")
    sh("commit", "-q", "-m", "tests")
    cand = sh("rev-parse", "HEAD").stdout.strip()
    runner = vpgha.Runner()

    # the ordinary case is silent, in both spellings the step accepts
    assert len(vpgha.prepare_measured(r, cand, runner, only="preflight:platform/tests/test_a.py")) == 40
    assert len(vpgha.prepare_measured(r, cand, runner, only="preflight:tests/test_a.py")) == 40
    # ... and a full render, which declares no paths at all, is untouched
    assert len(vpgha.prepare_measured(r, cand, runner)) == 40

    with pytest.raises(vpgha_overlay.UnrunnablePaths, match="agent/tests/x.py"):
        vpgha.prepare_measured(r, cand, runner, only="preflight:agent/tests/x.py")
    assert sh("cat-file", "-e", "%s:agent/tests/x.py" % cand).returncode == 0, (
        "the refused path is really in the tree: this is about where the step runs, "
        "not about whether the packet invented a file")

    with pytest.raises(vpgha_overlay.UnrunnablePaths, match="test_missing.py"):
        vpgha.prepare_measured(r, cand, runner, only="preflight:platform/tests/test_missing.py")

    # a twin: ask goes through the same builder and gets the same refusal
    with pytest.raises(vpgha_overlay.UnrunnablePaths, match="agent/tests/x.py"):
        vpgha.prepare_measured(r, cand, runner, only="twin:3:agent/tests/x.py")


def test_d162_the_check_is_off_unless_a_caller_supplies_exists(tmp_path):
    """D162 control: `exists` defaults to None everywhere it was threaded, so
    every existing caller, test and tool renders exactly what it rendered before.
    `render(MINI_CI, only="preflight:agent/tests/x.py")` has no tree to consult
    and must not start guessing."""
    doc = yaml.safe_load(vpgha_overlay.render(MINI_CI, only="preflight:agent/tests/x.py"))
    assert list(doc["jobs"]) == ["platform-preflight"]
    runs = [str(st.get("run", "")) for st in doc["jobs"]["platform-preflight"]["steps"]]
    assert any("uv run pytest -q agent/tests/x.py" in r for r in runs), (
        "unchanged without `exists`: D162 refuses, it never rewrites a path")
