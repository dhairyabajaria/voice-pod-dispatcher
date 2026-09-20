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
    `XDG_RUNTIME_DIR=$RUNNER_TEMP/xdg`, `TMPDIR=/dev/shm/pytest-<job>-<shard>`
    (both mkdir'd first) and `-n SHARD_WORKERS` on the shard leg, mirroring
    voice-pod 894cdeec on ci/self-hosted-runner-trial-2;
  * `--branch "$GITHUB_REF_NAME"` on every ci_collection_floor.py call
    when the candidate's script accepts it (rule 5; $CIRCLE_BRANCH is
    absent on GHA).

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
RUNS_ON = ["self-hosted", "voicepod"]
WORKFLOW_PATH = ".github/workflows/vp-proof.yml"
WORKFLOW_NAME = "vp-proof"
JUNIT_DIR = "${{ runner.temp }}/junit"
DEFAULT_UPLOAD_ACTION = "actions/upload-artifact@v4"
SHARD_JOB = "platform-shards"
SHARD_WORKERS = 3
SHARD_ENV = {"XDG_RUNTIME_DIR": "${{ runner.temp }}/xdg",
             "TMPDIR": "/dev/shm/pytest-${{ github.job }}-${{ matrix.shard }}"}
SHARD_PREP = {"name": "Prepare per-job pgserver lock dir and tmpfs pgdata dir (vp-proof)",
              "run": 'mkdir -p "$RUNNER_TEMP/xdg" "/dev/shm/pytest-${GITHUB_JOB}-${{ matrix.shard }}"'}

PYTEST_RE = re.compile(r"^(?P<indent>\s*)(?P<cmd>uv run pytest\b[^\n]*?)(?P<cont>\s*\\)?$", re.M)
FLOOR_RE = re.compile(r"ci_collection_floor\.py (floor|control|freshness)\b[^\n]*")
VITEST_RE = re.compile(r"npm run test -- --reporter=json --outputFile=(?P<json>\S+)")


class _Literal(str):
    """a multi-line run: script dumped as a `|` block"""


def _represent_literal(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(_Literal, _represent_literal)


def matrix_suffix(job):
    """`${{ matrix.<key> }}` expression for the job's matrix (one key), else ''"""
    strat = (job.get("strategy") or {}).get("matrix") or {}
    if "include" in strat:
        keys = [k for k in (strat["include"][0] if strat["include"] else {}) if k == "component"] or \
               list((strat["include"][0] if strat["include"] else {}).keys())[:1]
    else:
        keys = list(strat.keys())[:1]
    return ("-${{ matrix.%s }}" % keys[0]) if keys else ""


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


def render_jobs(ci, floor_supports_branch=False):
    """{job_id: job} for the overlay, derived from a parsed ci.yml"""
    upload = _upload_action(ci)
    src = ci.get("jobs") or {}
    missing = [j for j in REQUIRED_JOBS if j not in src]
    if missing:
        raise ValueError("ci.yml lacks required job(s): %s" % ", ".join(missing))
    out = {}
    for job_id in REQUIRED_JOBS:
        job = dict(src[job_id])
        suffix = matrix_suffix(job)
        job["name"] = "vp/%s%s" % (job_id, suffix)
        job["runs-on"] = list(RUNS_ON)
        job.pop("if", None)
        legs = [0]
        steps = [{"name": "Prepare junit dir (vp-proof)", "run": 'mkdir -p "$RUNNER_TEMP/junit"'}]
        if job_id == SHARD_JOB:
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


def render(ci_yml_text, floor_supports_branch=False):
    ci = yaml.safe_load(ci_yml_text)
    jobs = render_jobs(ci, floor_supports_branch)
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
