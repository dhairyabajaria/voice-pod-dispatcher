#!/usr/bin/env python3
"""vpcircle.py -- CircleCI bridge for the Voice Pod v9 control layer.

SPEC.md Sec. "CircleCI bridge (vpcircle.py)":
  push_branch(worktree, branch)   git push origin <branch>:<branch>
  trigger(branch, parameters) -> pipeline_id
      circleci api "api/v2/project/<slug>/pipeline/run" -X POST -d '<json>'
      body: {"definition_id": DEFINITION_ID, "config": {"branch": B},
             "checkout": {"branch": B}, "parameters": P}
      Booleans in P must serialise as real JSON booleans (json.dumps does
      this for real Python bool values; never build the body by string
      interpolation).
  poll(pipeline_id, interval=60, deadline_s=5400) -> dict
      pipeline -> workflows (api/v2/pipeline/<id>/workflow) -> jobs
      (api/v2/workflow/<wid>/job); for each failed job with a job_number,
      fetch api/v2/project/<slug>/<job_number>/tests and keep only entries
      with result failure or error (never skipped).
  classify(jobs, failed_tests) -> {"status": ..., "reds": [...]}
      FAIL_PRODUCT: >=1 job failed with >=1 failed test.
      FAIL_INFRA:   a job failed with zero failed tests, or a job's status
                    is infrastructure_fail/timedout/canceled, or a job is
                    not_run because an upstream dependency failed.
      UNKNOWN:      anything else that is not a clean success.
      PASS:         every job succeeded.
  record(run_root, sha, pipeline, jobs, failed_tests, classified)
      writes proofs/<sha>/{pipeline.json,jobs.json,tests-failed.json,
      classified.json} under run_root, each stamped with a UTC-ms "ts".

Account rotation: dispatcher/circleaccount.py is the existing multi-account
launcher (identity-checked keychain credential per account, never the
CLI's shared default). vpcircle imports it and reuses verified_env() to
build the (prefix, env) pair for each `circleci` invocation instead of
reimplementing credential selection. If a trigger response mentions
credits/plan/payment, vpcircle retries the trigger exactly once on the
next account in rotation order and records which account actually ran.

stdlib only. Every subprocess call (circleci CLI, git) goes through the
Runner class so tests can fake them -- vpcircle never calls the real
circleci CLI or the network on its own, and never triggers a real
pipeline outside of the trigger()/poll() functions a caller explicitly
invokes with a live Runner.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import circleaccount as _circleaccount  # noqa: E402

__all__ = [
    "Runner",
    "project_visible",
    "SLUG",
    "DEFINITION_ID",
    "ACCOUNTS",
    "push_branch",
    "trigger",
    "poll",
    "classify",
    "record",
]

# api/v2/project/<slug>/... -- see SPEC.md.
SLUG = "circleci/ENeXGDVeXvRGEyPGm5rywd/csivA8jTLotdwZbp17ACv"
DEFINITION_ID = "5c8213d0-1d01-416c-bae6-983da31dd8d2"

# Rotation order: whatever order circleaccount.EXPECTED declares its
# accounts in ("1", "2", "3").
ACCOUNTS = tuple(_circleaccount.EXPECTED)

# D44: each keychain account triggers ITS OWN project (org / definition) and
# checks out ITS OWN GitHub repo, so the proof branch must be pushed to that
# repo before the trigger.  push_remote None = the worktree's origin chain
# (vpcircle.github_remote, D38).  Roster `proof.circleci.accounts` overrides
# any field per account; `proof.circleci.rotation` orders the fallbacks.
TARGETS = {
    "3": {"slug": SLUG, "definition_id": DEFINITION_ID, "push_remote": None,
          "org": "Pareen Calling", "repo": "dhairyabajaria/voice-pod-NEW"},
    "1": {"slug": "circleci/AUoERWov6KVkJZG4hCBuxU/2RSozHJZM8Qz5jxzjTR6tE",
          "definition_id": "cda494d8-6652-4487-8d96-98267be06312",
          "push_remote": "git@github.com:voicepod-ci-mirror-a/voice-pod-NEW.git",
          "org": "Voice Pod CI Secondary A", "repo": "voicepod-ci-mirror-a/voice-pod-NEW"},
    "2": {"slug": "circleci/7BDzzoWMeVDmriGVMigj1S/BhwRJt34cg1vNDorZmgA52",
          "definition_id": "a38ba2a6-da55-4603-a292-3b643b15925d",
          "push_remote": "git@github.com:voicepod-ci-mirror-b/voice-pod-NEW.git",
          "org": "Voice Pod CI Secondary B", "repo": "voicepod-ci-mirror-b/voice-pod-NEW"},
}
DEFAULT_ROTATION = ("3", "1", "2")


def target(account=None, overrides=None):
    """the project an account triggers: TARGETS[account] under roster overrides"""
    acct = account or DEFAULT_ROTATION[0]
    base = dict(TARGETS.get(acct) or {"slug": SLUG, "definition_id": DEFINITION_ID, "push_remote": None})
    base.update(dict((overrides or {}).get(acct) or {}))
    return base


def slug_for(account=None, overrides=None):
    return target(account, overrides)["slug"]

# A trigger response is treated as a credit/billing refusal (not a hard
# failure) when its body text mentions any of these, case-insensitively.
CREDIT_MARKERS = ("credit", "plan", "payment")

# CircleCI workflow statuses that mean "this workflow will not change again".
WORKFLOW_TERMINAL = {"success", "failed", "error", "canceled", "unauthorized"}

# Job statuses that, on their own (independent of failed-test counts), mean
# an infrastructure failure rather than a product one.
INFRA_JOB_STATUSES = {"infrastructure_fail", "timedout", "canceled"}

# Job statuses that mean the job is done and will not change again.
JOB_TERMINAL_STATUSES = {
    "success", "failed", "infrastructure_fail", "timedout", "canceled",
    "not_run", "unauthorized", "terminated-unknown",
}


class Runner:
    """Every subprocess call vpcircle makes goes through here.

    Tests construct a Runner with a fake `run` callable (the same
    signature subprocess.run has: `run(argv, **kwargs) ->
    subprocess.CompletedProcess`) so no real process is ever spawned.
    """

    def __init__(self, run=subprocess.run, binary=None):
        self._run = run
        self._binary = binary

    def git(self, args, cwd=None):
        argv = ["git", *args] if cwd is None else ["git", "-C", str(cwd), *args]
        return self._run(argv, capture_output=True, text=True)

    def circleci_api(self, account, path, method="GET", data=None):
        """Run `circleci api <path> [-X <method>] [-d <data>]` under the
        identity-checked credential for `account` (via
        circleaccount.verified_env). Returns the CompletedProcess; never
        raises on a non-zero exit so callers can inspect stdout/stderr to
        decide what happened (credit error vs hard failure)."""
        command = ["api", path]
        if method and method != "GET":
            command += ["-X", method]
        if data is not None:
            command += ["-d", data]
        prefix, env, _identity = _circleaccount.verified_env(
            account, run=self._run, binary=self._binary)
        return self._run(prefix + command, env=env, capture_output=True, text=True)


def project_visible(runner=None, account=None, targets=None):
    """GET api/v2/project/<slug> under `account`: (ok, detail).  A 404 here
    means the credential is not a member of the project's org (trial
    attempt 1: account 1 vs "Pareen Calling"), and no branch should be
    pushed for it."""
    runner = runner or Runner()
    acct = account or DEFAULT_ROTATION[0]
    slug = slug_for(acct, targets)
    result = runner.circleci_api(acct, f"api/v2/project/{slug}", method="GET")
    text = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        return False, f"account {acct} cannot read project {slug}: {text[:200]}"
    return True, acct


RED_RESULTS = ("failure", "error")


def _looks_like_credit_error(text):
    lowered = (text or "").lower()
    return any(marker in lowered for marker in CREDIT_MARKERS)


def _now_ms():
    return int(time.time() * 1000)


def _is_local_url(url):
    u = (url or "").strip()
    return u.startswith(("/", "file://", "./", "../")) or (u.endswith(".git") and "://" not in u and ":" not in u)


def github_remote(worktree, runner=None, override=None, hops=4):
    """D38: the remote CircleCI's GitHub App watches.  Every v13 worktree is
    a clone of voice-pod/chief9-recovery whose `origin` is the LOCAL repo
    voice-pod/.git, so `push origin` never reached GitHub and every trigger
    400'd ("Failed to fetch config reference").  Follow local origins
    (repo -> its origin -> ...) until a non-local URL; `override` (roster
    circleci.push_remote) wins."""
    if override:
        return override
    runner = runner or Runner()
    cwd = worktree
    for _ in range(hops):
        res = runner.git(["remote", "get-url", "origin"], cwd=cwd)
        url = (res.stdout or "").strip() if res.returncode == 0 else ""
        if not url:
            raise RuntimeError(f"github_remote: no origin url at {cwd}")
        if not _is_local_url(url):
            return url
        cwd = url[len("file://"):] if url.startswith("file://") else url
        if cwd.endswith("/.git"):
            cwd = cwd[:-5]
    raise RuntimeError(f"github_remote: origin chain from {worktree} never leaves this machine")


def branch_on_remote(worktree, remote, branch, runner=None):
    runner = runner or Runner()
    res = runner.git(["ls-remote", "--heads", remote, branch], cwd=worktree)
    return res.returncode == 0 and bool((res.stdout or "").strip())


def push_branch(worktree, branch, runner=None, remote=None):
    """git push <github remote> <branch>:<branch> from `worktree`, then prove
    the branch is there (ls-remote) -- the trigger must never be asked for a
    branch GitHub has not got.  Never the model: called only by the
    driver/integrator process."""
    runner = runner or Runner()
    remote = remote or github_remote(worktree, runner)
    refspec = f"{branch}:{branch}"
    result = runner.git(["push", remote, refspec], cwd=worktree)
    if result.returncode != 0:
        raise RuntimeError(
            f"push_branch: git push {remote} {refspec} failed: {(result.stderr or '').strip()}"
        )
    if not branch_on_remote(worktree, remote, branch, runner):
        raise RuntimeError(f"push_branch: {branch} is not on {remote} after the push")
    return result


def delete_branch(worktree, branch, runner=None, remote=None):
    runner = runner or Runner()
    remote = remote or github_remote(worktree, runner)
    return runner.git(["push", remote, "--delete", branch], cwd=worktree)


def _trigger_once(runner, account, branch, parameters, targets=None):
    tgt = target(account, targets)
    body = {
        "definition_id": tgt["definition_id"],
        "config": {"branch": branch},
        "checkout": {"branch": branch},
        "parameters": parameters,
    }
    # json.dumps renders a Python bool as a JSON boolean (true/false, not
    # "true"/"false") -- this is what keeps parameter booleans real.
    payload = json.dumps(body)
    path = f"api/v2/project/{tgt['slug']}/pipeline/run"
    return runner.circleci_api(account, path, method="POST", data=payload)


def trigger(branch, parameters, runner=None, account=None, targets=None, rotate=True):
    """circleci api .../pipeline/run -X POST -d '<json>' for `branch` with
    `parameters`. Rotates to the next account exactly once if the first
    response looks like a credit/plan/payment refusal (rotate=False: the
    caller owns the rotation -- D44: the branch must be pushed to the next
    account's repo first, laneproof.run_circleci does that).

    Returns {"pipeline_id": ..., "account": <account that ran>}.
    """
    runner = runner or Runner()
    start = account or DEFAULT_ROTATION[0]
    others = [a for a in ACCOUNTS if a != start] if rotate else []
    attempts = [start] + others[:1]  # primary + exactly one retry account

    last_text = None
    for i, acct in enumerate(attempts):
        result = _trigger_once(runner, acct, branch, parameters, targets)
        text = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0:
            try:
                resp = json.loads(result.stdout)
            except ValueError as exc:
                raise RuntimeError(
                    f"trigger: invalid JSON from circleci api (account {acct}): {exc}"
                ) from None
            pipeline_id = resp.get("id")
            if not pipeline_id:
                raise RuntimeError(
                    f"trigger: no pipeline id in response (account {acct}): {resp}"
                )
            return {"pipeline_id": pipeline_id, "account": acct}
        if _looks_like_credit_error(text) and i < len(attempts) - 1:
            last_text = text
            continue
        raise RuntimeError(
            f"trigger: circleci api failed (account {acct}): {text.strip()}"
        )
    raise RuntimeError(
        f"trigger: exhausted account rotation; last response: {last_text}"
    )


def _get_json(runner, account, path):
    result = runner.circleci_api(account, path, method="GET")
    if result.returncode != 0:
        raise RuntimeError(
            f"circleci api {path} failed (account {account}): "
            f"{(result.stderr or '').strip()}"
        )
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(
            f"circleci api {path}: invalid JSON (account {account}): {exc}"
        ) from None


class Cancelled(Exception):
    """poll() stopped because the caller's `abort()` returned true."""


def cancel_pipeline(pipeline_id, runner=None, account=None):
    """Best effort: POST cancel on every non-terminal workflow of the
    pipeline (a cancelled chain union should not keep burning credits).
    Returns the workflow ids cancelled; never raises."""
    runner = runner or Runner()
    acct = account or DEFAULT_ROTATION[0]
    done = []
    try:
        wfs = _get_json(runner, acct, f"api/v2/pipeline/{pipeline_id}/workflow").get("items", [])
        for w in wfs:
            if w.get("status") in WORKFLOW_TERMINAL or not w.get("id"):
                continue
            r = runner.circleci_api(acct, f"api/v2/workflow/{w['id']}/cancel", method="POST")
            if r.returncode == 0:
                done.append(w["id"])
    except Exception:
        pass
    return done


def poll(pipeline_id, interval=60, deadline_s=5400, runner=None, account=None,
         sleep=time.sleep, clock=time.monotonic, abort=None, targets=None):
    """Walk pipeline -> workflows -> jobs until every workflow is terminal,
    then fetch failed tests for every failed job with a job_number.

    Returns {"pipeline_id", "workflows", "jobs", "failed_tests"} where
    failed_tests maps job_number -> [test dict, ...] (result != success).

    `sleep`/`clock` are injectable so tests never actually sleep.
    `abort()` is checked before every sleep; true raises Cancelled.
    """
    runner = runner or Runner()
    acct = account or DEFAULT_ROTATION[0]
    started = clock()

    while True:
        workflows = _get_json(
            runner, acct, f"api/v2/pipeline/{pipeline_id}/workflow"
        ).get("items", [])
        all_terminal = bool(workflows) and all(
            w.get("status") in WORKFLOW_TERMINAL for w in workflows
        )
        if all_terminal:
            jobs = []
            for wf in workflows:
                wf_jobs = _get_json(
                    runner, acct, f"api/v2/workflow/{wf['id']}/job"
                ).get("items", [])
                for j in wf_jobs:
                    j = dict(j)
                    j["workflow_id"] = wf.get("id")
                    j["workflow_name"] = wf.get("name")
                    jobs.append(j)

            failed_tests = {}
            for j in jobs:
                if j.get("status") == "failed" and j.get("job_number") is not None:
                    tests_resp = _get_json(
                        runner, acct,
                        f"api/v2/project/{slug_for(acct, targets)}/{j['job_number']}/tests",
                    )
                    items = tests_resp.get("items", [])
                    # pytest junit results are success | failure | error |
                    # skipped (and CircleCI reports "skipped" for xfail).
                    # Pipeline 206 (2026-09-14): "!= success" turned 255
                    # skips into reds and handed the builder 269 nodes for
                    # 14 real failures.  Only failure/error are red.
                    failed_tests[j["job_number"]] = [
                        t for t in items if t.get("result") in RED_RESULTS
                    ]

            return {
                "pipeline_id": pipeline_id,
                "workflows": workflows,
                "jobs": jobs,
                "failed_tests": failed_tests,
            }

        if clock() - started >= deadline_s:
            raise TimeoutError(
                f"poll: deadline of {deadline_s}s exceeded for pipeline {pipeline_id}"
            )
        if abort is not None and abort():
            raise Cancelled(f"poll: aborted by caller for pipeline {pipeline_id}")
        sleep(interval)


def _dependency_failed(jobs_by_id, dep_ids):
    for dep_id in dep_ids or ():
        dep = jobs_by_id.get(dep_id)
        if dep is None:
            continue
        status = dep.get("status")
        if status == "failed" or status in INFRA_JOB_STATUSES:
            return True
    return False


NO_CREDIT_MARKERS = ("no-credits", "no credits", "credits are available", "upgrade to continue")


def credit_block(jobs, runner=None, account=None, targets=None):
    """The plan/credit refusal that only shows AFTER a successful trigger:
    every job of pipeline ad4709dd (2026-09-18 21:12Z) 'failed' in 75 s with
    the job message 'This job has been blocked because no credits are
    available on your plan' (reason free-plan-no-credits-available).  Reads
    the first failed job's detail; returns that message or None."""
    runner = runner or Runner()
    acct = account or DEFAULT_ROTATION[0]
    for j in jobs:
        if j.get("status") != "failed" or j.get("job_number") is None:
            continue
        try:
            det = _get_json(runner, acct, f"api/v2/project/{slug_for(acct, targets)}/job/{j['job_number']}")
        except RuntimeError:
            return None
        for m in det.get("messages") or []:
            text = "%s %s" % (m.get("reason") or "", m.get("message") or "")
            if any(k in text.lower() for k in NO_CREDIT_MARKERS):
                return (m.get("message") or m.get("reason") or "credits").strip()
        return None
    return None


def classify(jobs, failed_tests):
    """{"status": PASS|FAIL_PRODUCT|FAIL_INFRA|UNKNOWN, "reds": [...]}"""
    jobs_by_id = {j.get("id"): j for j in jobs if j.get("id") is not None}

    reds = []
    has_product = False
    has_infra = False
    has_unknown = False

    for job in jobs:
        status = job.get("status")
        name = job.get("name")
        job_number = job.get("job_number")

        if status == "success":
            continue

        if status == "failed":
            tests = failed_tests.get(job_number) or []
            if tests:
                has_product = True
                reds.append({
                    "job": name, "job_number": job_number, "status": status,
                    "kind": "product", "tests": tests,
                })
            else:
                has_infra = True
                reds.append({
                    "job": name, "job_number": job_number, "status": status,
                    "kind": "infra", "reason": "failed with zero failed tests",
                })
            continue

        if status in INFRA_JOB_STATUSES:
            has_infra = True
            reds.append({
                "job": name, "job_number": job_number, "status": status,
                "kind": "infra", "reason": status,
            })
            continue

        if status == "not_run":
            if _dependency_failed(jobs_by_id, job.get("dependencies")):
                has_infra = True
                reds.append({
                    "job": name, "job_number": job_number, "status": status,
                    "kind": "infra", "reason": "not_run-with-upstream-failure",
                })
            else:
                has_unknown = True
                reds.append({
                    "job": name, "job_number": job_number, "status": status,
                    "kind": "unknown", "reason": "not_run with no failed dependency",
                })
            continue

        has_unknown = True
        reds.append({
            "job": name, "job_number": job_number, "status": status,
            "kind": "unknown", "reason": f"unrecognised status {status!r}",
        })

    if has_product:
        overall = "FAIL_PRODUCT"
    elif has_infra:
        overall = "FAIL_INFRA"
    elif has_unknown:
        overall = "UNKNOWN"
    else:
        overall = "PASS"

    return {"status": overall, "reds": reds}


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record(run_root, sha, pipeline, jobs, failed_tests, classified):
    """Write proofs/<sha>/{pipeline.json,jobs.json,tests-failed.json,
    classified.json} under run_root. Each file is stamped with a UTC-ms
    "ts". Returns the proofs/<sha> directory path."""
    out_dir = Path(run_root) / "proofs" / str(sha)
    ts = _now_ms()
    _write_json(out_dir / "pipeline.json", {"ts": ts, "pipeline": pipeline})
    _write_json(out_dir / "jobs.json", {"ts": ts, "jobs": jobs})
    _write_json(out_dir / "tests-failed.json", {"ts": ts, "failed_tests": failed_tests})
    _write_json(out_dir / "classified.json", {"ts": ts, **classified})
    return out_dir


def main(argv=None):  # pragma: no cover -- thin CLI wrapper, not unit tested
    import argparse

    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    push_p = sub.add_parser("push")
    push_p.add_argument("worktree")
    push_p.add_argument("branch")

    trig_p = sub.add_parser("trigger")
    trig_p.add_argument("branch")
    trig_p.add_argument("--parameters", default="{}")
    trig_p.add_argument("--account")

    poll_p = sub.add_parser("poll")
    poll_p.add_argument("pipeline_id")
    poll_p.add_argument("--interval", type=int, default=60)
    poll_p.add_argument("--deadline-s", type=int, default=5400)
    poll_p.add_argument("--account")

    a = p.parse_args(argv)
    if a.cmd == "push":
        push_branch(a.worktree, a.branch)
        print(json.dumps({"ok": True}))
        return 0
    if a.cmd == "trigger":
        result = trigger(a.branch, json.loads(a.parameters), account=a.account)
        print(json.dumps(result))
        return 0
    if a.cmd == "poll":
        result = poll(a.pipeline_id, interval=a.interval, deadline_s=a.deadline_s,
                      account=a.account)
        print(json.dumps(result))
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
