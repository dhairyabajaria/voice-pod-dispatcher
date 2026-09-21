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
# when the candidate's ci.yml has the job, no raise when it does not.  D100
# (§98 item 3): the order job is ~80 min and bounds every canary, so it is
# rendered only for a FINAL canary (`order=True`: the Architect arms which) or
# when `only=` names it; intermediate canaries run without it (~40 min).
OPTIONAL_JOBS = ("platform-order",)
RUNS_ON = ["self-hosted", "voicepod"]
# D112 (Architect 2026-09-20, two boxes): a FULL proof is pinned to one host label
# (voicepod-a / voicepod-b, `host=`) so two canaries never share a host; only=
# and preflight runs keep the shared label.
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
# D105 (CircleCI Manager 2026-09-20): the pgserver lock's file sink defaulted to
# $TMPDIR (the shard's tmpfs pgdata dir) and never left the runner -- zero
# [pgserver-lock] lines in any uploaded log.  Pointed at the junit dir, it rides
# the shard's artifact (junit-platform-shards-N) as pgserver-lock-<shard>.log.
LOCK_LOG_ENV = "VOICEPOD_PGSERVER_LOCK_LOG"
LOCK_LOG_PATH = "%s/pgserver-lock-${{ matrix.shard }}.log" % JUNIT_DIR
SHARD_ENV = {"XDG_RUNTIME_DIR": "${{ runner.temp }}/xdg", "TMPDIR": SHARD_DIR, LOCK_LOG_ENV: LOCK_LOG_PATH}
# R-TEST-PG-STAGGER (Architect 2026-09-20): worker gwN sleeps N x this before the
# pgserver lock acquire; unset = no sleep (local untouched).  The overlay sets it
# on the shard job only, from roster proof.circleci.shard_stagger_s (no product
# default); roster proof.circleci.shard_workers lifts -n 3 -> 6 once it is live.
STAGGER_ENV = "VOICEPOD_PG_START_STAGGER_SECONDS"
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

# D99 (Architect 2026-09-20): `uv run python -m tests.shuffled_runner` is
# pytest.main(argv) with a seed plugin (platform/tests/shuffled_runner.py), so
# it takes --junitxml unchanged; before this it was the one pytest leg without
# junit (the order-dependence step of vp/platform, soon vp/platform-order)
PYTEST_RE = re.compile(r"^(?P<indent>\s*)(?P<cmd>uv run (?:pytest|python -m tests\.shuffled_runner)\b[^\n]*?)"
                       r"(?P<cont>\s*\\)?$", re.M)
# only the `freshness` subcommand takes --branch (L04-FLOOR-PROOF-BRANCH adds it
# under `elif mode == "freshness":`); `floor` / `control` refuse it with
# "unrecognized arguments" -- canary run 35483670689's vp/platform job (D83d)
FLOOR_RE = re.compile(r"ci_collection_floor\.py freshness\b[^\n]*")
VITEST_RE = re.compile(r"npm run test -- --reporter=json --outputFile=(?P<json>\S+)")
# D108 (-R10 portal, 2026-09-20): the coverage-floor vitest run had no junit leg,
# so its one red (Settings.test.tsx timeout under instrumentation) read "failed
# with zero failed tests" = FAIL_INFRA.  Any other `npm run test -- ...` line
# (not test:e2e) gets a junit reporter beside its own flags.
VITEST_OTHER_RE = re.compile(r"^(?P<indent>\s*)npm run test -- (?P<flags>(?!--reporter=json --outputFile=)[^\n]*?)"
                             r"(?P<cont>\s*\\)?$", re.M)


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


def _rewrite_run(script, job_id, suffix, floor_supports_branch, legs, shard_workers=None):
    """pytest legs get junit; shard leg -n SHARD_WORKERS; floor calls --branch; vitest junit"""
    def pytest_sub(m):
        legs[0] += 1
        cmd = m.group("cmd")
        if job_id == SHARD_JOB:
            cmd = re.sub(r"-n\s+\S+", "-n %d" % int(shard_workers or SHARD_WORKERS), cmd, count=1)
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

    def vitest_other_sub(m):
        if "--outputFile.junit=" in m.group("flags"):
            return m.group(0)
        legs[0] += 1
        return "%snpm run test -- %s --reporter=default --reporter=junit --outputFile.junit=%s/%s%s-%d.xml%s" % (
            m.group("indent"), m.group("flags").rstrip(), JUNIT_DIR, job_id, suffix, legs[0], m.group("cont") or "")
    out = VITEST_OTHER_RE.sub(vitest_other_sub, out)
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


PREFLIGHT_JOB = "platform-preflight"
PREFLIGHT_PREFIX = "preflight:"
PREFLIGHT_DROP_RE = re.compile(r"ci_collection_floor\.py|check_module_coverage\.py|tests\.shuffled_runner|uv run pytest\b")


class UnrunnablePaths(ValueError):
    """D162: `only` declares test paths the rendered step cannot find at the
    candidate.  A ValueError so an older caller that only catches ValueError
    still sees it, and its own class so the proof path can name the cause."""


def wd_relative(wd, paths):
    """repo-relative test paths -> the spelling the rendered step passes to pytest.

    D106: `test_paths` are repo-relative (`platform/tests/x.py`) while the step
    runs with `working-directory: platform` (run 35518327671: "file or directory
    not found"), so the prefix comes off.  A path that does NOT start with
    `<wd>/` is passed through unchanged, and that is deliberate, not an oversight:
    a wd-relative spelling (`tests/x.py`) is a legitimate form and
    test_vpgha.py:545 pins a mixed-form `only` end to end.

    D162: factored out of preflight_job so the existence check in
    `unrunnable_paths` reads the SAME transformation the step will actually get.
    A second copy of this three-line expression is how the check and the thing it
    checks drift apart."""
    return [x[len(wd) + 1:] if x.startswith(wd + "/") else x for x in paths]


def unrunnable_paths(paths, wd, exists):
    """D162: which of `paths` will the rendered step fail to find, given the
    repository contents `exists(<repo-relative path>) -> bool` at the candidate?

    The rendered step runs `uv run pytest -q <rel>` with `working-directory: wd`,
    so the file pytest opens is `<wd>/<rel>` -- that one spelling is what this
    checks, never the declared path.

    The D106 gap this exists for: the strip above only fires for paths that start
    with `<wd>/`.  Anything else passes through, so a repo-relative path from
    another package (`agent/tests/x.py`) is handed to pytest inside `platform/`,
    resolves to `platform/agent/tests/x.py`, matches nothing, and pytest exits 5
    ("no tests ran") -- which the classifier reads as infrastructure_fail rather
    than as the packet defect it is.

    Pure, with `exists` injected: the caller holds the worktree and answers from
    `git cat-file -e <sha>:<path>`, so nothing here needs a checkout.  Returns the
    DECLARED spellings, because that is what a packet author would have to fix."""
    rel = wd_relative(wd, paths)
    return [p for p, r in zip(paths, rel) if not exists("%s/%s" % (wd, r))]


def preflight_job(src, paths, exists=None):
    """Fleet-2 (§98): the `platform` job with its suite steps replaced by ONE
    serial `uv run pytest -q <paths>` (the held lane's own test files); setup,
    lint and typecheck steps are kept.  A signal about the lane, never a verdict.

    D162: `exists(<repo-relative path>) -> bool` is optional and OFF by default,
    so every existing caller and test keeps today's behaviour.  When it is given
    -- `prepare_measured` supplies it from the candidate's own tree -- a path the
    rendered step could not find raises `UnrunnablePaths` here, at render time,
    before a pipeline is spent.  The check lives in this function because this is
    the one place that holds BOTH `wd` and `paths`; deriving `wd` again anywhere
    else would be a second copy of the rule."""
    job = dict(src["platform"])
    steps, done = [], False
    for st in job.get("steps") or []:
        run = str(st.get("run") or "")
        if run and PREFLIGHT_DROP_RE.search(run):
            if not done:
                done = True
                wd = st.get("working-directory") or "platform"
                rel = wd_relative(wd, paths)      # D106, D162: one copy of the rule
                if exists is not None:
                    bad = unrunnable_paths(paths, wd, exists)
                    if bad:
                        raise UnrunnablePaths(
                            "the rendered step runs in %r, so these declared path(s) resolve to "
                            "nothing and pytest would exit 5 (\"no tests ran\"), which reads as "
                            "infrastructure_fail rather than the packet defect it is: %s"
                            % (wd, ", ".join(bad)))
                steps.append({"name": "Preflight: the lane's own test files (vp-proof, Fleet-2)",
                              "working-directory": wd, "run": "uv run pytest -q %s" % " ".join(rel)})
            continue
        steps.append(st)
    if not done:
        raise ValueError("preflight: the platform job has no pytest step to replace")
    job["steps"] = steps
    job.pop("strategy", None)
    return job


TWIN_JOB = "platform-twin"
TWIN_PREFIX = "twin:"
TWIN_WORKERS = 3
TARGETED_JOB = "platform-targeted"
TARGETED_PREFIX = "targeted:"       # D114: a box-shaped targeted platform proof offloaded to the fleet
SCOPED_JOBS = (TWIN_JOB, TARGETED_JOB)


def scoped_spec(only):
    """"twin:<n>:<p,...>" / "targeted:<n>:<p,...>" -> (job_id, workers, paths) or None"""
    only = str(only or "")
    for prefix, job in ((TWIN_PREFIX, TWIN_JOB), (TARGETED_PREFIX, TARGETED_JOB)):
        if not only.startswith(prefix):
            continue
        spec = only[len(prefix):]
        workers, _, rest = spec.partition(":")
        paths = [x for x in rest.split(",") if x.strip()]
        if not workers.isdigit() or not paths:
            raise ValueError("only=%s: expected %s<workers>:<path,...>" % (only, prefix))
        return job, int(workers), paths
    return None


def twin_job(src, paths, workers=None, exists=None):
    """D113 (§114): a released lane's SCOPED twin -- the `platform` job with its
    suite steps replaced by ONE `uv run pytest -q -n <workers> <paths>` over the
    parent's test files plus its contract's; junit as every pytest leg, NO --cov
    (class I, the cross-host coverage combine, cannot touch it), per-worker
    Postgres like a shard leg.  Its record answers the twin's own ask only."""
    job = preflight_job(src, paths, exists=exists)
    n = int(workers or TWIN_WORKERS)
    for st in job.get("steps") or []:
        run = str(st.get("run") or "")
        if run.startswith("uv run pytest -q "):
            st["name"] = "Twin: the lane's and its contract's test files (vp-proof, D113)"
            st["run"] = "uv run pytest -q -n %d %s" % (n, run[len("uv run pytest -q "):])
    return job


def _subst_steps(steps, old, new):
    def sub(v):
        if isinstance(v, _Literal):
            return _Literal(str(v).replace(old, new))
        if isinstance(v, str):
            return v.replace(old, new)
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v
    return [sub(st) for st in steps]


def render_jobs(ci, floor_supports_branch=False, only=None, order=False, shard_workers=None, shard_stagger_s=None,
                host=None, exists=None):
    """{job_id: job} for the overlay, derived from a parsed ci.yml; `only`
    narrows it to one job / matrix leg (select_only); `order` adds the
    optional order-dependence job when the ci.yml has it (D100);
    `shard_workers` / `shard_stagger_s` tune the shard job (R-TEST-PG-STAGGER)"""
    upload = _upload_action(ci)
    src = ci.get("jobs") or {}
    missing = [j for j in REQUIRED_JOBS if j not in src]
    if missing:
        raise ValueError("ci.yml lacks required job(s): %s" % ", ".join(missing))
    selected = list(REQUIRED_JOBS) + [j for j in OPTIONAL_JOBS if j in src and (order or only)]
    if only and str(only).startswith(PREFLIGHT_PREFIX):
        paths = [x for x in str(only)[len(PREFLIGHT_PREFIX):].split(",") if x.strip()]
        if not paths:
            raise ValueError("only=%s names no test paths" % only)
        selected, src = [PREFLIGHT_JOB], {PREFLIGHT_JOB: preflight_job(src, paths, exists=exists)}
    elif only and scoped_spec(only):
        # D113 "twin:<workers>:<p1,p2,...>" / D114 "targeted:<workers>:<paths>" --
        # one scoped job (the platform job's setup + one pytest -n N), any host
        job_id, workers, paths = scoped_spec(only)
        selected, src = [job_id], {job_id: twin_job(src, paths, workers, exists=exists)}
    elif only:
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
        job["runs-on"] = ["self-hosted", str(host)] if (host and not only) else list(RUNS_ON)
        job["timeout-minutes"] = JOB_TIMEOUT_MIN
        job.pop("if", None)
        legs = [0]
        steps = [{"name": "Prepare junit dir (vp-proof)", "run": 'mkdir -p "$RUNNER_TEMP/junit"'}]
        if job_id == SHARD_JOB or job_id in SCOPED_JOBS:
            if SHARD_TMP_ON_SHM:
                steps.append(dict(SHARD_SHM_ASSERT))
            steps.append(dict(SHARD_PREP))
        for step in job.get("steps") or []:
            step = dict(step)
            if step.get("run"):
                new = _rewrite_run(str(step["run"]), job_id, suffix, floor_supports_branch, legs,
                                   shard_workers=shard_workers)
                step["run"] = _Literal(new) if "\n" in new else new
                if (job_id == SHARD_JOB or job_id in SCOPED_JOBS) and "pytest" in new:
                    env = dict(step.get("env") or {})
                    env.update(SHARD_ENV)
                    if shard_stagger_s is not None and float(shard_stagger_s) > 0:
                        env[STAGGER_ENV] = "%.2f" % float(shard_stagger_s)
                    step["env"] = env
            steps.append(step)
        if job_id == SHARD_JOB or job_id in SCOPED_JOBS:
            steps.append(dict(SHARD_CLEANUP))
        if job_id in SCOPED_JOBS:
            # no matrix: the shard dir/lock-log names key on the run id instead, so
            # several twin jobs on one host never share (or remove) one /dev/shm dir
            steps = _subst_steps(steps, "${{ matrix.shard }}", "%s-${{ github.run_id }}" % job_id.split("-", 1)[1])
        if legs[0] and any("pytest" in str(st.get("run", "")) for st in steps):
            steps.insert(1, dict(LINGER_ASSERT))
        if job_id in DOCKER_ASSERT_JOBS:
            steps.insert(1, dict(DOCKER_ASSERT))
        steps.append({
            "name": "Retain junit results (vp-proof)",
            "if": "always()",
            "uses": upload,
            "with": {"name": "junit-%s%s" % (job_id, suffix),
                     "path": "%s/*.xml%s" % (JUNIT_DIR, "\n%s/*.log" % JUNIT_DIR if (job_id == SHARD_JOB or job_id in SCOPED_JOBS) else ""),
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


def render(ci_yml_text, floor_supports_branch=False, only=None, order=False, shard_workers=None,
           shard_stagger_s=None, host=None, exists=None):
    ci = yaml.safe_load(ci_yml_text)
    jobs = render_jobs(ci, floor_supports_branch, only=only, order=order, shard_workers=shard_workers,
                       shard_stagger_s=shard_stagger_s, host=host, exists=exists)
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
