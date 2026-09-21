#!/usr/bin/env python3
"""vpgha.py -- the GitHub Actions hosted-proof provider (D83, BULK-RULING §46).

The same surface laneproof uses from vpcircle, over the `gh` CLI (the
owner's choice: gh's own keyring-held authentication, never a token in a
file or the roster) against the Oracle self-hosted runner fleet:

  prepare_measured(wt, cand, runner)  candidate + ONE overlay commit adding
                                      .github/workflows/vp-proof.yml (rendered
                                      from the candidate's ci.yml by
                                      vpgha_overlay); asserts the diff is
                                      exactly that path (OverlayDirty otherwise)
  push_branch / delete_branch         git over the worktree's GitHub origin
  trigger(branch, params, ...)        `gh workflow run vp-proof.yml --ref <branch>
                                      -f proof_id=<branch>`; the run id is found
                                      by run-name == branch (`gh run list`)
  poll(run_id, ...)                   `gh run view --json` until completed, then
                                      the junit artifacts -> failed tests per job
  classify / record                   vpcircle's, over jobs mapped to its shape
  cancel_pipeline                     `gh run cancel`

One pseudo account "gha" (no rotation, no credits). Statuses map so that
D79b's rule holds: a cancelled run/job is CANCELLED; a non-success job with
no junit is FAIL_INFRA; any junit failure is FAIL_PRODUCT; all junit present
and green is PASS.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import vpcircle  # noqa: E402
import vpgha_overlay  # noqa: E402

REPO = "dhairyabajaria/voice-pod-NEW"
WORKFLOW = "vp-proof.yml"
ACCOUNT = "gha"
ACCOUNTS = (ACCOUNT,)
DEFAULT_ROTATION = (ACCOUNT,)
TARGETS = {ACCOUNT: {"repo": REPO, "push_remote": None, "org": "GitHub Actions (Oracle fleet)"}}
FIND_RUN_TRIES, FIND_RUN_WAIT_S = 18, 5          # a dispatched run appears within seconds; allow 90 s
# D102 (owner order via the Architect, 2026-09-20 14:3xZ), CORRECTED 2026-09-20 23:0xZ
# with the CircleCI Manager's live runner counts: the original reasoned fleet-wide
# ("the fleet is 12 runners") for what D112 later made a PER-BOX decision, so its
# 3 was too high the moment full proofs were pinned.
#
# The fleet is 3 boxes / 36 runners, but a FULL pipeline pins to one of only two
# (proof.circleci.hosts = voicepod-a, voicepod-c) at 12 runners each; voicepod-b
# takes scoped/twin work only.  pick_host balances over those two, so by pigeonhole
# ANY cap above 2 puts two full pipelines on one box -- the configuration the
# -n3/-n6/-n9 runs measured at ~20% WORSE than serial.  2 is the only value that
# guarantees one full pipeline per box, which is the ~15:12 (~1.25x) oversubscription
# D102 accepted as no-harm; a doubled-up box is ~2.5x.
#
# Rule 6's "one in flight" is superseded; roster proof.circleci.max_full_in_flight
# overrides without a code change (laneproof.circle_cfg), the only= cap stays separate.
MAX_IN_FLIGHT = 2

Cancelled = vpcircle.Cancelled
classify = vpcircle.classify
record = vpcircle.record
_looks_like_credit_error = lambda text: False    # noqa: E731 -- no credits on a self-hosted fleet

# GitHub conclusion -> the CircleCI job status vocabulary vpcircle.classify reads
CONCLUSION_MAP = {
    "success": "success", "failure": "failed", "cancelled": "canceled", "timed_out": "timedout",
    "skipped": "infrastructure_fail", "startup_failure": "infrastructure_fail", "action_required": "infrastructure_fail",
    "neutral": "infrastructure_fail", "stale": "infrastructure_fail",
}


class OverlayDirty(RuntimeError):
    """rule 2: the measured commit differs from the candidate by more than the workflow file"""


class Runner(object):
    """`gh` and `git` over subprocess; injectable for tests"""

    def __init__(self, run=subprocess.run, binary=None, timeout_s=120):
        self._run = run
        self._binary = binary or shutil.which("gh") or "gh"
        self.timeout_s = timeout_s

    def gh(self, args, timeout_s=None, input_text=None):
        argv = [self._binary] + list(args)
        env = dict(os.environ)
        env["GH_PROMPT_DISABLED"] = "1"
        env["GH_NO_UPDATE_NOTIFIER"] = "1"
        return self._run(argv, capture_output=True, text=True, env=env,
                         timeout=timeout_s or self.timeout_s, input=input_text)

    def git(self, args, cwd=None, env=None):
        return self._run(["git"] + list(args), cwd=cwd, capture_output=True, text=True, timeout=self.timeout_s,
                         env=env)


def _check(result, what):
    if result.returncode != 0:
        raise RuntimeError("%s failed (rc %s): %s" % (what, result.returncode,
                                                      ((result.stderr or "") + (result.stdout or "")).strip()[-400:]))
    return result


def target(account, targets=None):
    return dict(TARGETS[ACCOUNT])


def github_remote(worktree, runner=None, override=None):
    return vpcircle.github_remote(worktree, runner=None, override=override)


def project_visible(runner=None, account=None, targets=None):
    """the authenticated gh identity can read the repo and its workflows"""
    runner = runner or Runner()
    res = runner.gh(["api", "repos/%s/actions/workflows/%s" % (REPO, WORKFLOW), "--jq", ".state"])
    text = ((res.stdout or "") + (res.stderr or "")).strip()
    if res.returncode != 0:
        return False, "gh cannot read %s on %s: %s" % (WORKFLOW, REPO, text[:200])
    if "active" not in text:
        return False, "%s on %s is %s, not active (the dispatch-only stub must be on main)" % (WORKFLOW, REPO, text[:60])
    return True, ACCOUNT


def push_branch(worktree, branch, runner=None, remote=None):
    return vpcircle.push_branch(worktree, branch, runner=None, remote=remote)


def delete_branch(worktree, branch, runner=None, remote=None):
    return vpcircle.delete_branch(worktree, branch, runner=None, remote=remote)


# -- rule 2: the measured commit ---------------------------------------------------------

def _git(runner, args, cwd, what, env=None):
    res = runner.git(args, cwd=cwd, env=env)
    if res.returncode != 0:
        raise RuntimeError("%s: git %s failed: %s" % (what, " ".join(args[:2]),
                                                      ((res.stderr or "") + (res.stdout or "")).strip()[-300:]))
    return (res.stdout or "").strip()


def render_overlay(wt, cand, runner, only=None, order=False, shard=None, host=None, exists=None):
    """vp-proof.yml text for this candidate, from ITS ci.yml and floor script;
    `only` (§95 item 2) narrows the workflow to one job / matrix leg; `order`
    (D100) adds the platform-order job for a final canary"""
    ci_text = _git(runner, ["show", "%s:.github/workflows/ci.yml" % cand], wt, "overlay")
    try:
        floor = _git(runner, ["show", "%s:scripts/ci_collection_floor.py" % cand], wt, "overlay")
    except RuntimeError:
        floor = ""
    shard = shard or {}
    return vpgha_overlay.render(ci_text, vpgha_overlay.floor_supports_branch(floor), only=only, order=order,
                                shard_workers=shard.get("workers"), shard_stagger_s=shard.get("stagger_s"), host=host,
                                exists=exists)


def _tree_exists(runner, wt, sha):
    """D162: `exists(<repo-relative path>) -> bool` against the CANDIDATE's tree.

    `git cat-file -e <sha>:<path>` answers from the object store, so nothing is
    checked out and the shared worktree is never touched.  Anything other than a
    clean success reads as absent -- the check is only ever used to refuse, so
    failing closed here costs a refusal and never a false clear."""
    def exists(path):
        try:
            _git(runner, ["cat-file", "-e", "%s:%s" % (sha, path)], wt, "overlay")
            return True
        except Exception:                                  # noqa: BLE001
            return False
    return exists


def prepare_measured(wt, cand, runner=None, text=None, only=None, order=False, shard=None, host=None):
    """-> measured commit sha: `cand` + one commit that adds/replaces
    .github/workflows/vp-proof.yml, built through a temporary index so the
    worktree's HEAD, index and files are untouched.  Asserts (rule 2) that
    `git diff --name-only cand measured` is exactly that path."""
    runner = runner or Runner()
    wt = str(wt)
    # D162: the existence check runs HERE, at render time, because this is where
    # the candidate's own tree is in hand.  A `text` supplied by the caller is
    # already rendered, so there is nothing left to check -- that path is tests.
    text = text if text is not None else render_overlay(wt, cand, runner, only=only, order=order, shard=shard,
                                                        host=host, exists=_tree_exists(runner, wt, cand))
    with tempfile.TemporaryDirectory(prefix="vp-overlay-") as tmp:
        blob_path = Path(tmp) / "vp-proof.yml"
        blob_path.write_text(text, encoding="utf-8")
        blob = _git(runner, ["hash-object", "-w", str(blob_path)], wt, "overlay")
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
        _git(runner, ["read-tree", cand], wt, "overlay", env=env)
        _git(runner, ["update-index", "--add", "--cacheinfo", "100644,%s,%s" % (blob, vpgha_overlay.WORKFLOW_PATH)],
             wt, "overlay", env=env)
        tree = _git(runner, ["write-tree"], wt, "overlay", env=env)
        measured = _git(runner, ["-c", "user.name=vp-lanedriver", "-c", "user.email=lanedriver@voicepod.local",
                                 "commit-tree", tree, "-p", cand, "-m",
                                 "vp-proof overlay (D83 §46): %s on %s%s%s"
                                 % (vpgha_overlay.WORKFLOW_PATH, cand[:12], " only=%s" % only if only else "",
                                    " order=final" if order else "")],
                        wt, "overlay")
    changed = [l for l in _git(runner, ["diff", "--name-only", cand, measured], wt, "overlay").splitlines()
               if l.strip()]
    if changed != [vpgha_overlay.WORKFLOW_PATH]:
        raise OverlayDirty("measured commit %s differs from %s by %s, not exactly %s"
                           % (measured[:12], cand[:12], changed, vpgha_overlay.WORKFLOW_PATH))
    return measured


# -- trigger / poll ----------------------------------------------------------------------

def _json(res, what):
    _check(res, what)
    try:
        return json.loads(res.stdout or "null")
    except ValueError as exc:
        raise RuntimeError("%s: invalid JSON: %s" % (what, exc)) from None


def find_run(branch, runner, tries=FIND_RUN_TRIES, wait_s=FIND_RUN_WAIT_S, sleep=time.sleep, known=()):
    """the newest vp-proof run whose run-name is `branch` (our proof_id input).
    D103 (2026-09-20 14:32Z): a retried proof re-uses its branch name, so the
    list also holds the EARLIER run of the same proof; the driver polled the
    cancelled 35513112276 while its real re-trigger 35516791190 ran unobserved.
    `known` (run ids listed before the trigger) and completed runs are skipped."""
    known = set(str(k) for k in known)
    for i in range(tries):
        rows = _json(runner.gh(["run", "list", "-R", REPO, "--workflow", WORKFLOW, "--branch", branch,
                                "--event", "workflow_dispatch", "--limit", "20",
                                "--json", "databaseId,displayTitle,createdAt,status"]), "gh run list")
        mine = [r for r in rows or [] if str(r.get("displayTitle")) == branch
                and str(r.get("databaseId")) not in known and str(r.get("status")) != "completed"]
        if mine:
            mine.sort(key=lambda r: str(r.get("createdAt") or ""))
            return str(mine[-1]["databaseId"])
        if i + 1 < tries:
            sleep(wait_s)
    raise RuntimeError("gh workflow run: no vp-proof run named %s appeared within %ds" % (branch, tries * wait_s))


def _runs_named(branch, runner):
    try:
        rows = _json(runner.gh(["run", "list", "-R", REPO, "--workflow", WORKFLOW, "--branch", branch,
                                "--event", "workflow_dispatch", "--limit", "20", "--json", "databaseId,displayTitle"]),
                     "gh run list")
    except RuntimeError:
        return []
    return [str(r.get("databaseId")) for r in rows or [] if str(r.get("displayTitle")) == branch]


def trigger(branch, parameters, runner=None, account=None, targets=None, rotate=False, sleep=time.sleep):
    """-> {"pipeline_id": <run id>, "account": "gha"}"""
    runner = runner or Runner()
    known = _runs_named(branch, runner)              # D103: runs of an earlier attempt on this branch name
    args = ["workflow", "run", WORKFLOW, "-R", REPO, "--ref", branch, "-f", "proof_id=%s" % branch]
    for k, v in (parameters or {}).items():
        args += ["-f", "%s=%s" % (k, "true" if v is True else "false" if v is False else v)]
    _check(runner.gh(args), "gh workflow run")
    return {"pipeline_id": find_run(branch, runner, sleep=sleep, known=known), "account": ACCOUNT}


def runners(runner=None):
    """Fleet-3: [{name, status, busy}] for the repo's self-hosted runners (read-only)"""
    runner = runner or Runner()
    data = _json(runner.gh(["api", "--paginate", "repos/%s/actions/runners" % REPO,
                            "--jq", "[.runners[] | {name, status, busy}]"]), "gh api runners")
    rows = []
    for chunk in (data if isinstance(data, list) else [data]):
        rows.extend(chunk if isinstance(chunk, list) else [chunk])
    return [{"name": str(r.get("name")), "status": str(r.get("status")), "busy": bool(r.get("busy"))}
            for r in rows if isinstance(r, dict) and r.get("name")]


def _map_job(j, run_id):
    conclusion = j.get("conclusion")
    status = CONCLUSION_MAP.get(str(conclusion or "").lower(), "unknown" if conclusion else "running")
    return {"id": str(j.get("databaseId") or j.get("id")), "name": j.get("name"), "status": status,
            "conclusion": conclusion, "job_number": j.get("databaseId") or j.get("id"),
            "workflow_id": str(run_id), "workflow_name": vpgha_overlay.WORKFLOW_NAME, "url": j.get("url")}


def parse_junit(text):
    """pytest xunit1 / vitest junit -> [{file, classname, name, result, message}] for the reds"""
    out = []
    root = ET.fromstring(text)
    for case in root.iter("testcase"):
        red = None
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is not None:
                red = tag
                msg = (node.get("message") or (node.text or "")).strip()
                break
        if red is None:
            continue
        out.append({"file": case.get("file") or "", "classname": case.get("classname") or "",
                    "name": case.get("name") or "", "result": red, "message": msg[:600]})
    return out


def _artifacts(run_id, runner):
    data = _json(runner.gh(["api", "--paginate", "repos/%s/actions/runs/%s/artifacts" % (REPO, run_id),
                            "--jq", "[.artifacts[] | {name, id, expired}]"]), "gh api artifacts")
    rows = []
    for chunk in (data if isinstance(data, list) else [data]):
        rows.extend(chunk if isinstance(chunk, list) else [chunk])
    return {r["name"]: r for r in rows if isinstance(r, dict) and r.get("name")}


def junit_case_count(text):
    """the number of <testcase> elements (collected and run: passed, failed, skipped)"""
    return sum(1 for _ in ET.fromstring(text).iter("testcase"))


def junit_totals(run_id, jobs, runner, download_dir=None):
    """D118b: {job_number: testcase count} from each job's junit artifact, green
    jobs included; a job without an artifact gets no key.  A SCOPED job that
    collected nothing (0 cases) is no answer -- laneproof classifies it FAIL_INFRA."""
    have = _artifacts(run_id, runner)
    out = {}
    tmp = download_dir or tempfile.mkdtemp(prefix="vp-junit-")
    for j in jobs:
        key = vpgha_overlay.job_key_from_name(j.get("name"))
        if not key:
            continue
        name = "junit-%s" % key
        if name not in have or have[name].get("expired"):
            continue
        dest = Path(tmp) / name
        res = runner.gh(["run", "download", str(run_id), "-R", REPO, "-n", name, "-D", str(dest)], timeout_s=300)
        if res.returncode != 0:
            continue
        n = 0
        for f in sorted(dest.rglob("*.xml")):
            try:
                n += junit_case_count(f.read_text(encoding="utf-8"))
            except (OSError, ET.ParseError):
                continue
        out[j["job_number"]] = n
    return out


def junit_by_job(run_id, jobs, runner, download_dir=None):
    """{job_number: [red test items]} from the junit-<job> artifacts; a job
    whose artifact is missing or empty gets NO key (classify -> FAIL_INFRA
    when the job failed; a green job without junit stays green)."""
    have = _artifacts(run_id, runner)
    out = {}
    tmp = download_dir or tempfile.mkdtemp(prefix="vp-junit-")
    for j in jobs:
        key = vpgha_overlay.job_key_from_name(j.get("name"))
        if not key:
            continue
        name = "junit-%s" % key
        if name not in have or have[name].get("expired"):
            continue
        dest = Path(tmp) / name
        res = runner.gh(["run", "download", str(run_id), "-R", REPO, "-n", name, "-D", str(dest)], timeout_s=300)
        if res.returncode != 0:
            continue
        reds = []
        for f in sorted(dest.rglob("*.xml")):
            try:
                reds.extend(parse_junit(f.read_text(encoding="utf-8")))
            except (OSError, ET.ParseError):
                continue
        out[j["job_number"]] = reds
    return out


def poll(run_id, interval=60, deadline_s=5400, runner=None, account=None, sleep=time.sleep,
         clock=time.monotonic, abort=None, targets=None):
    """{"pipeline_id", "workflows", "jobs", "failed_tests"} once the run completed"""
    runner = runner or Runner()
    started = clock()
    transient = 0
    while True:
        try:
            view = _json(runner.gh(["run", "view", str(run_id), "-R", REPO,
                                    "--json", "status,conclusion,jobs,displayTitle,headSha,url"]), "gh run view")
        except RuntimeError:
            transient += 1
            if transient > vpcircle.POLL_TRANSIENT_MAX or clock() - started >= deadline_s:
                raise
            if abort is not None and abort():
                raise Cancelled("poll: aborted by caller for run %s" % run_id)
            sleep(interval)
            continue
        transient = 0
        if str(view.get("status")) == "completed":
            jobs = [_map_job(j, run_id) for j in view.get("jobs") or []]
            conclusion = str(view.get("conclusion") or "").lower()
            workflows = [{"id": str(run_id), "name": vpgha_overlay.WORKFLOW_NAME,
                          "status": CONCLUSION_MAP.get(conclusion, conclusion or "unknown"), "url": view.get("url")}]
            failed_tests = junit_by_job(run_id, [j for j in jobs if j["status"] != "success"], runner)
            return {"pipeline_id": str(run_id), "workflows": workflows, "jobs": jobs, "failed_tests": failed_tests,
                    "head_sha": view.get("headSha")}
        if clock() - started >= deadline_s:
            raise TimeoutError("poll: deadline of %ds exceeded for run %s" % (deadline_s, run_id))
        if abort is not None and abort():
            raise Cancelled("poll: aborted by caller for run %s" % run_id)
        sleep(interval)


def cancel_pipeline(run_id, runner=None, account=None):
    runner = runner or Runner()
    res = runner.gh(["run", "cancel", str(run_id), "-R", REPO])
    return [str(run_id)] if res.returncode == 0 else []


def credit_block(jobs, runner=None, account=None, targets=None):
    return None
