#!/usr/bin/env python3
"""vpgha_overlay.py -- the driver's own GitHub Actions proof workflow (D83, §46).

`render(ci_yml_text, floor_supports_branch)` derives `.github/workflows/
vp-proof.yml` from the CANDIDATE's own ci.yml, so the proof measures exactly
the jobs the candidate ships (rule 3: every required ci.yml job), with:

  * `on: workflow_dispatch` only, inputs `proof_id` (the run-name, which is
    how the driver finds its run id) and `run_full_suite`;
  * every required job on `runs-on: [self-hosted, voicepod]` (the Oracle
    fleet), display name `vp/<job>[-<matrix>]` so an artifact maps to its
    job by name alone;
  * every pytest leg with `--junitxml=$RUNNER_TEMP/junit/<job>-<leg>.xml
    -o junit_family=xunit1` (xunit1 carries `file`, which
    vpdriver.circle_failed_nodes turns into node ids); the portal's vitest
    run adds a junit reporter beside its json one;
  * one `junit-<job>[-<matrix>]` artifact per job, uploaded `if: always()`
    (a non-success job without one classifies FAIL_INFRA, never PASS);
  * the shard job gets its own pgserver lockfile and tmpfs pgdata
    (Advisor, 2026-09-20: pgserver's lockfile is per Unix user, so 8 shard
    jobs on one box queued behind ONE lock and blew its 10 s start timeout):
    `XDG_RUNTIME_DIR=$RUNNER_TEMP/xdg`, `TMPDIR=$RUNNER_TEMP/pgdata-<shard>`
    (both mkdir'd first) and `-n SHARD_WORKERS` on the shard leg.  894cdeec on
    ci/self-hosted-runner-trial-2 put TMPDIR on /dev/shm and run 35478392897
    filled it (psycopg DiskFull / ENOSPC on every shard, 00:20Z), so the
    overlay keeps pgdata on the runner's disk;
  * `--branch "$GITHUB_REF_NAME"` on the ci_collection_floor.py `freshness`
    call when the candidate's script accepts it (rule 5; $CIRCLE_BRANCH is
    absent on GHA); `floor` / `control` never take it.

Non-required jobs (kb-real-provider-eval, scheduled scans, security-triage,
deploy, required-checks) are dropped: `deploy` needs GitHub-hosted image
builds and was never retargeted in the trial either.

The YAML head is written by hand (PyYAML reads `on:` as the boolean True);
the jobs are dumped with block scalars for multi-line scripts.
"""
from __future__ import annotations

import re

import yaml

REQUIRED_JOBS = ("platform", "platform-shards", "platform-coverage", "agent",
                 "deploy-contracts", "portal", "supply-chain")
# Required-when-present (§95 item 1, packet R-CI-PLATFORM-ORDER-JOB): rendered
# when the candidate's ci.yml has the job, no raise when it does not.  Without
# this a union carrying the packet would silently lose the serial order-
# dependence run on the canary (it moved out of `platform`).
OPTIONAL_JOBS = ("platform-order",)
RUNS_ON = ["self-hosted", "voicepod"]
# GitHub's default job timeout is 360 min; canary run 35483670689 hung disk-bound
# with no junit for 10+ min and nothing would have ended it.  A job past this
# is timed_out -> FAIL_INFRA (rule 4), never PASS.  CircleCI's whole pipeline
# took ~31-49 min (2026-09-14), so 90 covers a slow fleet with room.
JOB_TIMEOUT_MIN = 90
WORKFLOW_PATH = ".github/workflows/vp-proof.yml"
WORKFLOW_NAME = "vp-proof"
JUNIT_DIR = "${{ runner.temp }}/junit"
DEFAULT_UPLOAD_ACTION = "actions/upload-artifact@v4"
SHARD_JOB = "platform-shards"
SHARD_WORKERS = 3
# Where each shard's pgdata (TMPDIR) lives.  On tmpfs only once the product's
# test support caps WAL (Advisor 2026-09-20: pg_wal grows to max_wal_size=1GB
# per cluster, 24 clusters = 24GB worst case, so /dev/shm overflowed in run
# 35478392897 regardless of its size).  The cap landed: R-TEST-PG-WAL-CAP-R2
# VERIFIED 04:27:02Z (platform/testsupport/postgres.py `temporary_postgres`
# applies `ALTER SYSTEM SET max_wal_size='64MB' ...` to every disposable
# cluster), so from union-43 on pgdata is on /dev/shm behind the run-time
# size assert (SHARD_SHM_ASSERT); the disk path stays selectable here.
SHARD_TMP_ON_SHM = True
SHARD_SHM_MIN_GB = 6                                   # ~24 clusters x 150MB + headroom
_SHARD_DIR_DISK = "${{ runner.temp }}/pgdata-${{ matrix.shard }}"
_SHARD_DIR_SHM = "/dev/shm/pytest-platform-shards-${{ matrix.shard }}"
SHARD_DIR = _SHARD_DIR_SHM if SHARD_TMP_ON_SHM else _SHARD_DIR_DISK
SHARD_ENV = {"XDG_RUNTIME_DIR": "${{ runner.temp }}/xdg", "TMPDIR": SHARD_DIR}
# rm -rf FIRST (not only as cleanup): a cancelled / OOM-killed run must not
# leak its pgdata (RAM, on tmpfs) until the box reboots
SHARD_PREP = {"name": "Prepare per-job pgserver lock dir and pgdata dir (vp-proof)",
              "run": 'rm -rf "%s"\nmkdir -p "$RUNNER_TEMP/xdg" "%s"' % (SHARD_DIR, SHARD_DIR)}
# assert, never mutate: the tmpfs size is the runner image's (/etc/fstab) job;
# a too-small mount fails here with a clear line instead of a DiskFull wall
SHARD_SHM_ASSERT = {"name": "Assert /dev/shm is large enough for this shard (vp-proof)",
                    "run": 'size_kb=$(df -Pk /dev/shm | awk \'NR==2{print $2}\')\n'
                           'echo "/dev/shm size: ${size_kb} kB"\n'
                           'test "$size_kb" -ge %d || { echo "::error::/dev/shm is smaller than %dG; '
                           'resize it in the runner image (/etc/fstab), not here"; exit 1; }'
                           % (SHARD_SHM_MIN_GB * 1024 * 1024, SHARD_SHM_MIN_GB)}
SHARD_CLEANUP = {"name": "Remove this shard's pgdata dir (vp-proof)", "if": "always()",
                 "run": 'rm -rf "%s"' % SHARD_DIR}
# CircleCI Manager 2026-09-20 (host fix applied out of band on the Oracle box):
# systemd-logind's RemoveIPC wipes a user's /dev/shm + POSIX shm when their
# last login session ends, so an SSH logout mid-run killed every shard's
# live Postgres ("Failed to remove POSIX shared memory directory ...").
# linger + RemoveIPC=no are the fix; this step only ASSERTS linger so a
# regression (image rebuild, someone disabling it) is one loud red job.
# It goes on every job that runs pytest (each one starts pgserver clusters).
# Architect §53 (canary run 35483670689): deploy-contracts' three reds go through
# the docker compose renderer, which ubuntu-latest preinstalls and the Oracle
# runners lack; an absent renderer must name itself, not surface as BLOCKED.
DOCKER_ASSERT = {"name": "Assert docker compose is available (vp-proof)",
                 "run": 'set -euo pipefail\ndocker compose version || { echo "::error::docker compose is missing '
                        'on this runner: the deploy-contracts renderer needs it; install it in the runner image, '
                        'not here"; exit 1; }'}
DOCKER_ASSERT_JOBS = ("deploy-contracts",)
LINGER_ASSERT = {"name": "Assert the runner user has linger enabled (vp-proof)",
                 "run": 'set -euo pipefail\n'
                        'linger="$(loginctl show-user "$(id -un)" -p Linger --value)"\n'
                        'echo "Linger=$linger"\n'
                        'test "$linger" = "yes" || { echo "::error::linger is off for $(id -un): '
                        'logind RemoveIPC will wipe /dev/shm mid-run; fix on the host '
                        '(loginctl enable-linger + RemoveIPC=no), not here"; exit 1; }'}

PYTEST_RE = re.compile(r"^(?P<indent>\s*)(?P<cmd>uv run pytest\b[^\n]*?)(?P<cont>\s*\\)?$", re.M)
# only the `freshness` subcommand takes --branch (L04-FLOOR-PROOF-BRANCH adds it
# under `elif mode == "freshness":`); `floor` / `control` refuse it with
# "unrecognized arguments" -- canary run 35483670689's vp/platform job (D83d)
FLOOR_RE = re.compile(r"ci_collection_floor\.py freshness\b[^\n]*")
VITEST_RE = re.compile(r"npm run test -- --reporter=json --outputFile=(?P<json>\S+)")


class _Literal(str):
    """a multi-line run: script dumped as a `|` block"""


def _represent_literal(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(_Literal, _represent_literal)


def matrix_key(job):
    """the job's one matrix key ("shard", "component"), else None"""
    strat = (job.get("strategy") or {}).get("matrix") or {}
    if "include" in strat:
        keys = [k for k in (strat["include"][0] if strat["include"] else {}) if k == "component"] or \
               list((strat["include"][0] if strat["include"] else {}).keys())[:1]
    else:
        keys = list(strat.keys())[:1]
    return keys[0] if keys else None


def matrix_suffix(job):
    """`${{ matrix.<key> }}` expression for the job's matrix (one key), else ''"""
    key = matrix_key(job)
    return ("-${{ matrix.%s }}" % key) if key else ""


def _rewrite_run(script, job_id, suffix, floor_supports_branch, legs):
    """pytest legs get junit; shard leg -n 1; floor calls --branch; vitest junit"""
    def pytest_sub(m):
        legs[0] += 1
        cmd = m.group("cmd")
        if job_id == SHARD_JOB:
            cmd = re.sub(r"-n\s+\S+", "-n %d" % SHARD_WORKERS, cmd, count=1)
        cmd += " --junitxml=%s/%s%s-%d.xml -o junit_family=xunit1" % (JUNIT_DIR, job_id, suffix, legs[0])
        return "%s%s%s" % (m.group("indent"), cmd, m.group("cont") or "")
    out = PYTEST_RE.sub(pytest_sub, script)
    if floor_supports_branch:
        out = FLOOR_RE.sub(lambda m: m.group(0) + ' --branch "$GITHUB_REF_NAME"', out)

    def vitest_sub(m):
        legs[0] += 1
        return ("npm run test -- --reporter=json --reporter=junit --outputFile.json=%s "
                "--outputFile.junit=%s/%s%s-%d.xml" % (m.group("json"), JUNIT_DIR, job_id, suffix, legs[0]))
    out = VITEST_RE.sub(vitest_sub, out)
    return out


def _upload_action(ci):
    for job in (ci.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            uses = str(step.get("uses") or "")
            if uses.startswith("actions/upload-artifact@"):
                return uses
    return DEFAULT_UPLOAD_ACTION


def select_only(src, selected, only):
    """§95 item 2 `only=<job|shard>` -> ([job_id], {job_id: narrowed job}).
    `only` names one rendered job ("portal", "platform-order") or one leg of a
    matrix job ("platform-shards-3" = matrix value 3 of platform-shards).  The
    overlay then carries THAT job alone (no skipped siblings: a skipped job
    classifies as infrastructure_fail); the record it produces is a targeted
    proof, never a full-suite answer (lanedriver refuses to adopt it)."""
    only = str(only or "").strip()
    if only in selected:
        return [only], {only: dict(src[only])}
    # longest job id first: "platform-shards-3" is a leg of platform-shards, not
    # a leg "shards-3" of platform (which has no matrix)
    for job_id in sorted(selected, key=len, reverse=True):
        if not only.startswith(job_id + "-") or not matrix_key(src[job_id]):
            continue
        leg = only[len(job_id) + 1:]
        job = dict(src[job_id])
        strat = dict(job.get("strategy") or {})
        matrix = dict(strat.get("matrix") or {})
        key = matrix_key(job)
        if "include" in matrix:
            rows = [r for r in matrix["include"] or [] if key and str(r.get(key)) == leg]
            if not rows:
                raise ValueError("only=%s: %s has no matrix leg %s" % (only, job_id, leg))
            matrix["include"] = rows
        else:
            vals = list(matrix[key]) if key and isinstance(matrix.get(key), list) else []
            hit = [v for v in vals if str(v) == leg]
            if not hit:
                raise ValueError("only=%s: %s has no matrix leg %s" % (only, job_id, leg))
            matrix[key] = hit
        strat["matrix"] = matrix
        job["strategy"] = strat
        return [job_id], {job_id: job}
    raise ValueError("only=%s names no rendered job or matrix leg (jobs: %s)" % (only, ", ".join(selected)))


def render_jobs(ci, floor_supports_branch=False, only=None):
    """{job_id: job} for the overlay, derived from a parsed ci.yml; `only`
    narrows it to one job / matrix leg (select_only)"""
    upload = _upload_action(ci)
    src = ci.get("jobs") or {}
    missing = [j for j in REQUIRED_JOBS if j not in src]
    if missing:
        raise ValueError("ci.yml lacks required job(s): %s" % ", ".join(missing))
    selected = list(REQUIRED_JOBS) + [j for j in OPTIONAL_JOBS if j in src]
    if only:
        selected, src = select_only(src, selected, only)
    out = {}
    for job_id in selected:
        job = dict(src[job_id])
        if job.get("needs"):
            # a dependency on a dropped job (deploy, required-checks...) would make
            # GitHub reject the whole workflow; keep only rendered jobs
            needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
            kept = [n for n in needs if n in selected]
            if kept:
                job["needs"] = kept
            else:
                job.pop("needs")
        suffix = matrix_suffix(job)
        job["name"] = "vp/%s%s" % (job_id, suffix)
        job["runs-on"] = list(RUNS_ON)
        job["timeout-minutes"] = JOB_TIMEOUT_MIN
        job.pop("if", None)
        legs = [0]
        steps = [{"name": "Prepare junit dir (vp-proof)", "run": 'mkdir -p "$RUNNER_TEMP/junit"'}]
        if job_id == SHARD_JOB:
            if SHARD_TMP_ON_SHM:
                steps.append(dict(SHARD_SHM_ASSERT))
            steps.append(dict(SHARD_PREP))
        for step in job.get("steps") or []:
            step = dict(step)
            if step.get("run"):
                new = _rewrite_run(str(step["run"]), job_id, suffix, floor_supports_branch, legs)
                step["run"] = _Literal(new) if "\n" in new else new
                if job_id == SHARD_JOB and "pytest" in new:
                    env = dict(step.get("env") or {})
                    env.update(SHARD_ENV)
                    step["env"] = env
            steps.append(step)
        if job_id == SHARD_JOB:
            steps.append(dict(SHARD_CLEANUP))
        if legs[0] and any("pytest" in str(st.get("run", "")) for st in steps):
            steps.insert(1, dict(LINGER_ASSERT))
        if job_id in DOCKER_ASSERT_JOBS:
            steps.insert(1, dict(DOCKER_ASSERT))
        steps.append({
            "name": "Retain junit results (vp-proof)",
            "if": "always()",
            "uses": upload,
            "with": {"name": "junit-%s%s" % (job_id, suffix), "path": "%s/*.xml" % JUNIT_DIR,
                     "if-no-files-found": "ignore", "retention-days": 7},
        })
        job["steps"] = steps
        out[job_id] = job
    return out


HEAD = """# Generated by dispatcher/vp/vpgha_overlay.py (D83, BULK-RULING §46) from the
# candidate's own .github/workflows/ci.yml -- the lane driver's hosted proof.
# Never edit by hand; never merge to main except as the dispatch-only stub.
name: %(name)s
run-name: ${{ inputs.proof_id || github.ref_name }}
on:
  workflow_dispatch:
    inputs:
      proof_id:
        type: string
        default: ''
      run_full_suite:
        type: boolean
        default: true
concurrency:
  group: vp-proof-${{ github.ref }}
  cancel-in-progress: false
permissions:
  contents: read
""" % {"name": WORKFLOW_NAME}


def render(ci_yml_text, floor_supports_branch=False, only=None):
    ci = yaml.safe_load(ci_yml_text)
    jobs = render_jobs(ci, floor_supports_branch, only=only)
    body = yaml.dump({"jobs": jobs}, Dumper=_Dumper, sort_keys=False, width=200, allow_unicode=True,
                     default_flow_style=False)
    return HEAD + body


def floor_supports_branch(script_text):
    """the candidate's scripts/ci_collection_floor.py accepts --branch (L04-FLOOR-PROOF-BRANCH)"""
    return '"--branch"' in (script_text or "") or "'--branch'" in (script_text or "")


def job_key_from_name(name):
    """'vp/platform-shards-3' -> 'platform-shards-3' (the artifact suffix); None for others"""
    name = str(name or "")
    return name[3:] if name.startswith("vp/") else None


if __name__ == "__main__":  # pragma: no cover -- `vpgha_overlay.py <ci.yml> [<floor.py>]`
    import sys
    ci_text = open(sys.argv[1], encoding="utf-8").read()
    floor = open(sys.argv[2], encoding="utf-8").read() if len(sys.argv) > 2 else ""
    sys.stdout.write(render(ci_text, floor_supports_branch(floor)))
