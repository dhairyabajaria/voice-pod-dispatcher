#!/usr/bin/env python3
"""tests/test_vpdriver.py -- plain-assert tests for vpdriver.py + vpschema.py.

    python3 tests/test_vpdriver.py

No pytest, no network, no real server, no real opencode, no real git, and
nothing outside a per-test temp directory.  Every external effect is routed
through the fake scripts next to this file.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP.parent))
sys.path.insert(0, str(VP))
sys.path.insert(0, str(HERE))

import vpdriver            # noqa: E402
import vpschema            # noqa: E402
import fake_vpctl          # noqa: E402

FAKE_OC = HERE / "fake_opencode.sh"
FAKE_GIT = HERE / "fake_git.sh"
FAKE_OSA = HERE / "fake_osascript.sh"
FAKE_VPCTL = HERE / "fake_vpctl.py"

SHA = "abcdef1234567890abcdef1234567890abcdef12"


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

class RecordingRunner(vpdriver.Runner):
    """Real subprocess behaviour (the fakes are real scripts); the abort POST
    is captured instead of being sent to a socket."""

    def __init__(self):
        self.posts = []

    def post(self, url, timeout=10.0):
        self.posts.append(url)
        return 200, '{"ok":true}'


class Bed(object):
    """One temp RUN_ROOT + trunk + worktree root + a configured Driver."""

    def __init__(self, mode="ok", max_concurrent=2, items=None,
                 zen_fallback=False):
        self.tmp = Path(tempfile.mkdtemp(prefix="vpdrv-"))
        self.run_root = self.tmp / "RUN_ROOT"
        self.trunk = self.tmp / "trunk"
        self.wts = self.tmp / "vp-worktrees"
        for d in (self.run_root, self.trunk, self.wts):
            d.mkdir(parents=True, exist_ok=True)
        (self.trunk / "agent").mkdir()
        (self.trunk / "agent" / ".venv").mkdir()

        self.packet = self.tmp / "PACKET.md"
        self.packet.write_text("packet body\n", encoding="utf-8")
        self.benchmark = self.tmp / "BENCHMARK.md"
        self.benchmark.write_text("B1 invariant ...\n", encoding="utf-8")

        servers = {"go1": {"port": 4101,
                           "data": str(self.tmp / "data-go1"),
                           "max_concurrent": max_concurrent}}
        if zen_fallback:
            servers["zen"] = {"port": 4100,
                              "data": str(self.tmp / "data-zen"),
                              "max_concurrent": 1}
        roster = {
            "servers": servers,
            "groups": {"1": "go1", "2": "go1", "infra": "go1"},
            "models": {
                "builder": {"model": "opencode-go/muse-spark-1.3-contributor",
                            "variant": "xhigh", "agent": "vp-builder"},
                "junior": {"model": "opencode-go/deepseek-v4.1-flash",
                           "variant": "max", "agent": "vp-junior"},
            },
            "zen_fallback": zen_fallback,
            "round_cap": 8, "wip_per_group": 2,
            "trunk": str(self.trunk), "worktrees": str(self.wts),
        }
        self.roster_path = self.run_root / "roster.json"
        self.roster_path.write_text(json.dumps(roster, indent=2),
                                    encoding="utf-8")

        self.items_path = self.tmp / "items.json"
        self.set_items(items if items is not None else [self.item("B1")])
        self.running_path = self.tmp / "running.json"
        self.set_running([])
        self.packet_path_file = self.tmp / "packet.json"
        self.set_packet({})

        self.stop_file = self.tmp / "STOP"
        self.vpctl_log = self.tmp / "vpctl.jsonl"
        self.oc_log = self.tmp / "opencode.log"
        self.git_log = self.tmp / "git.log"
        self.osa_log = self.tmp / "osascript.log"
        self.stdin_log = self.tmp / "stdin.log"
        self.live_dir = self.tmp / "live"

        os.environ["VP_FAKE_MODE"] = mode
        os.environ["VP_FAKE_VPCTL_LOG"] = str(self.vpctl_log)
        os.environ["VP_FAKE_ITEMS"] = str(self.items_path)
        os.environ["VP_FAKE_RUNNING"] = str(self.running_path)
        os.environ["VP_FAKE_PACKET"] = str(self.packet_path_file)
        os.environ["VP_FAKE_OC_LOG"] = str(self.oc_log)
        os.environ["VP_FAKE_GIT_LOG"] = str(self.git_log)
        os.environ["VP_FAKE_OSA_LOG"] = str(self.osa_log)
        os.environ["VP_FAKE_STDIN_LOG"] = str(self.stdin_log)
        os.environ["VP_FAKE_LIVE"] = str(self.live_dir)
        os.environ["VP_FAKE_ATTEMPT"] = "a1"
        os.environ.pop("VP_FAKE_VPCTL_RC", None)
        os.environ.pop("VP_FAKE_FAIL_VERB", None)
        os.environ.pop("VP_FAKE_FAIL_RC", None)
        os.environ.pop("VP_FAKE_NO_ESCALATE", None)

        self.runner = RecordingRunner()
        self.driver = vpdriver.Driver(
            self.roster_path, runner=self.runner,
            vpctl_cmd=[sys.executable, str(FAKE_VPCTL)],
            stop_file=self.stop_file, interval=0.05,
            git_bin=str(FAKE_GIT), opencode_bin=str(FAKE_OC),
            osascript_bin=str(FAKE_OSA))

    # -- fixtures ------------------------------------------------------
    def item(self, name, status="BUILDING", group=1, rnd=0):
        return {"item": name, "status": status, "group_no": group,
                "round": rnd, "rev": 1,
                "worktree": str(self.wts / name),
                "base_sha": SHA,
                "packet_path": str(self.packet),
                "benchmark_path": str(self.benchmark)}

    def set_items(self, items):
        self.items_path.write_text(json.dumps(items), encoding="utf-8")

    def set_running(self, rows):
        self.running_path.write_text(json.dumps(rows), encoding="utf-8")

    def set_packet(self, row):
        self.packet_path_file.write_text(json.dumps(row), encoding="utf-8")

    # -- reads ---------------------------------------------------------
    def vpctl_calls(self):
        return fake_vpctl.calls(str(self.vpctl_log))

    def vpctl_verb(self, *verb):
        want = list(verb)
        out = []
        for call in self.vpctl_calls():
            head = [a for a in call if not a.startswith("--")][:len(want)]
            if head == want:
                out.append(call)
        return out

    def oc_invocations(self):
        if not self.oc_log.exists():
            return []
        return [l for l in self.oc_log.read_text().splitlines()
                if l.strip() and " export " not in (" %s " % l)]

    def oc_runs(self):
        return [l for l in self.oc_invocations() if l.startswith("run ")]

    def turn_records(self, item="B1", attempt="a1"):
        d = self.run_root / "turns" / item / attempt
        if not d.exists():
            return []
        return sorted(p for p in d.iterdir()
                      if p.name.endswith(".json")
                      and not p.name.endswith(".export.json"))

    def stdin_kinds(self):
        if not self.stdin_log.exists():
            return []
        return [l.strip() for l in self.stdin_log.read_text().splitlines()
                if l.strip()]

    def max_live(self):
        counts = Path(str(self.live_dir) + ".counts")
        if not counts.exists():
            return 0
        vals = [int(x) for x in counts.read_text().split() if x.strip()]
        return max(vals) if vals else 0

    def close(self):
        try:
            self.driver.join(timeout=15)
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)


def flag(call, name):
    """Value of --name in a recorded argv list, or None."""
    if name in call:
        i = call.index(name)
        if i + 1 < len(call):
            return call[i + 1]
    return None


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_schema_result_accepts_good_and_names_bad():
    tmp = Path(tempfile.mkdtemp(prefix="vpsch-"))
    try:
        good = {"item": "B1", "attempt": 1, "commit": SHA, "base": SHA,
                "diff_stat": {"files": 1, "insertions": 2, "deletions": 0},
                "checks": [{"name": "u", "command": "c", "exit": 0, "log": "l"}],
                "disputes": [], "blocked": None, "notes": "ok"}
        p = tmp / "RESULT.json"
        p.write_text(json.dumps(good))
        ok, errs = vpschema.validate_result(p)
        assert ok, "good RESULT rejected: %s" % errs

        bad = dict(good)
        del bad["base"]
        bad["diff_stat"] = {"files": "two", "insertions": 2, "deletions": 0}
        bad["checks"] = [{"name": "u", "command": "c", "log": "l"}]
        p.write_text(json.dumps(bad))
        ok, errs = vpschema.validate_result(p)
        assert not ok, "bad RESULT accepted"
        joined = " | ".join(errs)
        assert "base" in joined, joined
        assert "diff_stat.files" in joined, joined
        assert "checks[0].exit" in joined, joined

        ok, errs = vpschema.validate_result(tmp / "nope.json")
        assert not ok and "missing" in errs[0], errs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_schema_findings_all_pass_consistency():
    tmp = Path(tempfile.mkdtemp(prefix="vpsch-"))
    try:
        obj = {"item": "B1", "attempt": 1, "commit": SHA,
               "lines": [{"id": "1", "kind": "invariant", "verdict": "FAIL",
                          "evidence": "a.py:3", "note": ""}],
               "all_pass": True}
        p = tmp / "FINDINGS.json"
        p.write_text(json.dumps(obj))
        ok, errs = vpschema.validate_findings(p)
        assert not ok, "all_pass=true with a FAIL line was accepted"
        assert "all_pass" in " ".join(errs), errs

        obj["all_pass"] = False
        p.write_text(json.dumps(obj))
        ok, errs = vpschema.validate_findings(p)
        assert ok, errs

        obj["lines"][0]["kind"] = "vibes"
        p.write_text(json.dumps(obj))
        ok, errs = vpschema.validate_findings(p)
        assert not ok and "lines[0].kind" in " ".join(errs), errs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_builder_done_submits_result():
    bed = Bed(mode="ok")
    try:
        bed.driver.run_once()
        recs = bed.turn_records()
        assert len(recs) == 1, "expected 1 turn record, got %s" % recs
        rec = json.loads(recs[0].read_text())
        assert rec["classification"] == "DONE", rec["classification_detail"]
        assert rec["role"] == "builder"
        assert rec["server"] == "go1" and rec["port"] == 4101
        assert rec["session_id"] == "ses_fake_zzzz", rec["session_id"]
        assert rec["model"].endswith("muse-spark-1.3-contributor"), rec["model"]
        assert rec["variant"] == "xhigh"
        assert rec["prompt_sha256"] == vpdriver.sha256_text(
            vpdriver.BUILDER_PROMPT), "builder prompt is not the fixed header"
        assert rec["tokens_in"] == 1200 and rec["tokens_out"] == 333, rec
        assert rec["cache_read"] == 900 and rec["cost"] == 0.0031, rec
        assert rec["raw_head"] and len(rec["raw_head"]) <= 20
        assert rec["paths"]["export"], "export path not recorded"
        assert Path(rec["paths"]["export"]).exists(), "export file not written"
        assert "--format" in rec["argv"] and "json" in rec["argv"]

        ends = bed.vpctl_verb("turn", "end")
        assert len(ends) == 1, ends
        assert flag(ends[0], "--status") == "DONE", ends[0]
        assert flag(ends[0], "--tokens-in") == "1200", ends[0]

        subs = bed.vpctl_verb("submit-result")
        assert len(subs) == 1, "submit-result not called: %s" % bed.vpctl_calls()
        assert flag(subs[0], "--commit") == SHA, subs[0]

        # worktree template
        wt = bed.wts / "B1"
        for name in ("PACKET.md", "BENCHMARK.md", "RESULT_SCHEMA.json",
                     "FINDINGS_SCHEMA.json"):
            assert (wt / ".vp" / name).exists(), "missing .vp/%s" % name
        assert (wt / "agent" / ".venv").is_symlink(), "agent/.venv symlink"
        assert "worktree add" in bed.git_log.read_text()
    finally:
        bed.close()


def test_junior_findings_handed_back():
    bed = Bed(mode="junior", items=None)
    try:
        bed.set_items([bed.item("B1", status="GRADING")])
        bed.driver.run_once()
        recs = bed.turn_records()
        rec = json.loads(recs[0].read_text())
        assert rec["classification"] == "DONE", rec["classification_detail"]
        assert rec["role"] == "junior" and rec["kind"] == "grade"
        assert rec["prompt_sha256"] == vpdriver.sha256_text(
            vpdriver.JUNIOR_PROMPT)
        fs = bed.vpctl_verb("findings")
        assert len(fs) == 1, "findings not called: %s" % bed.vpctl_calls()
        assert flag(fs[0], "--path").endswith("FINDINGS.json"), fs[0]
        assert not bed.vpctl_verb("submit-result")
    finally:
        bed.close()


def test_progress_stop_resumes_three_times_then_stuck():
    bed = Bed(mode="nofile")
    try:
        bed.driver.run_once()
        runs = bed.oc_runs()
        assert len(runs) == 4, \
            "expected 1 turn + 3 resumes = 4 opencode runs, got %d: %s" \
            % (len(runs), runs)
        assert "--session" not in runs[0], "first run must not resume"
        for r in runs[1:]:
            assert "--session ses_fake_zzzz" in r, "resume lost the session: %s" % r
            assert r.rstrip().endswith("continue"), \
                "resume prompt must be the single word continue: %s" % r

        recs = bed.turn_records()
        assert len(recs) == 4, recs
        last = json.loads(recs[-1].read_text())
        assert last["classification"] == "STUCK", last["classification"]

        ends = bed.vpctl_verb("turn", "end")
        assert len(ends) == 1 and flag(ends[0], "--status") == "STUCK", ends
        esc = bed.vpctl_verb("escalate")
        assert esc and flag(esc[0], "--kind") == "STUCK", esc
        assert "STUCK" in bed.osa_log.read_text(), "no macOS notification"
        assert not bed.vpctl_verb("submit-result")
    finally:
        bed.close()


def test_quota_marks_server_skipped():
    bed = Bed(mode="quota")
    try:
        bed.driver.run_once()
        rec = json.loads(bed.turn_records()[0].read_text())
        assert rec["classification"] == "QUOTA", rec["classification_detail"]
        assert len(bed.oc_runs()) == 1, "QUOTA must not be resumed"
        ends = bed.vpctl_verb("turn", "end")
        assert flag(ends[0], "--status") == "QUOTA", ends
        esc = bed.vpctl_verb("escalate")
        assert esc and flag(esc[0], "--kind") == "QUOTA", esc
        assert "QUOTA" in bed.osa_log.read_text()
        assert bed.driver.servers["go1"]["skipped_until"] > time.time(), \
            "server go1 was not marked skipped"
        assert bed.driver._is_skipped("go1")
        # and with the server skipped and no zen fallback, nothing is routed
        srv, _tier = bed.driver.pick_server(1)
        assert srv is None, "routed to a skipped server: %s" % srv
    finally:
        bed.close()


def test_auth_classified_and_notified():
    bed = Bed(mode="auth")
    try:
        bed.driver.run_once()
        rec = json.loads(bed.turn_records()[0].read_text())
        assert rec["classification"] == "AUTH", rec["classification_detail"]
        esc = bed.vpctl_verb("escalate")
        assert esc and flag(esc[0], "--kind") == "AUTH", esc
        assert "AUTH" in bed.osa_log.read_text()
    finally:
        bed.close()


def test_invalid_schema_is_incomplete():
    bed = Bed(mode="bad")
    try:
        bed.driver.run_once()
        rec = json.loads(bed.turn_records()[0].read_text())
        assert rec["classification"] == "INCOMPLETE", rec["classification"]
        assert "missing" in rec["classification_detail"], \
            rec["classification_detail"]
        ends = bed.vpctl_verb("turn", "end")
        assert flag(ends[0], "--status") == "INCOMPLETE", ends
        esc = bed.vpctl_verb("escalate")
        assert esc and flag(esc[0], "--kind") == "INCOMPLETE", esc
        assert not bed.vpctl_verb("submit-result"), \
            "a schema-invalid RESULT.json must never be submitted"
        assert "INCOMPLETE" not in (bed.osa_log.read_text()
                                    if bed.osa_log.exists() else "")
    finally:
        bed.close()


def test_stop_file_aborts_and_blocks_new_spawns():
    bed = Bed(mode="hang", max_concurrent=2)
    try:
        os.environ["VP_FAKE_HANG"] = "2.0"
        bed.driver.tick()                      # spawns B1
        deadline = time.time() + 5
        while time.time() < deadline and not bed.oc_runs():
            time.sleep(0.05)
        time.sleep(0.4)                        # let the session id land
        assert len(bed.oc_runs()) == 1, bed.oc_runs()

        bed.set_items([bed.item("B1"), bed.item("B2")])
        bed.stop_file.write_text("stop\n", encoding="utf-8")

        spawned = bed.driver.tick()
        assert spawned == 0, "spawned a turn while STOP was in force"
        assert bed.runner.posts, "no abort POST was sent"
        url = bed.runner.posts[0]
        assert url == "http://127.0.0.1:4101/session/ses_fake_zzzz/abort", url
        assert len(bed.oc_runs()) == 1, \
            "a new opencode process started under STOP: %s" % bed.oc_runs()

        hb = json.loads((bed.run_root / "driver.heartbeat").read_text())
        assert hb["stopping"] is True, hb
        bed.driver.join(timeout=10)
        rec = json.loads(bed.turn_records()[-1].read_text())
        assert rec["classification"] == "ABORTED", rec["classification"]
        ends = bed.vpctl_verb("turn", "end")
        assert flag(ends[0], "--status") == "ABORTED", ends
        assert not bed.vpctl_verb("submit-result")
    finally:
        os.environ.pop("VP_FAKE_HANG", None)
        bed.close()


def test_per_server_concurrency_never_exceeded():
    bed = Bed(mode="slow", max_concurrent=2)
    try:
        bed.set_items([bed.item("I%d" % i, group=(1 if i % 2 else 2))
                       for i in range(1, 7)])
        bed.driver.loop(max_ticks=14)
        bed.driver.join(timeout=20)
        assert len(bed.oc_runs()) >= 4, \
            "fake opencode barely ran (%d)" % len(bed.oc_runs())
        peak = bed.max_live()
        assert peak >= 2, "test never reached the limit (peak=%d)" % peak
        assert peak <= 2, \
            "per-server concurrency exceeded: peak %d > max_concurrent 2" % peak
        assert bed.driver._active.get("go1", 0) == 0, bed.driver._active
    finally:
        bed.close()


def test_heartbeat_written_every_tick():
    bed = Bed(mode="ok")
    try:
        bed.set_items([])
        bed.driver.tick()
        hb_path = bed.run_root / "driver.heartbeat"
        assert hb_path.exists(), "no driver.heartbeat"
        hb1 = json.loads(hb_path.read_text())
        assert hb1["tick"] == 1 and hb1["stopping"] is False, hb1
        assert hb1["pid"] == os.getpid()
        assert "go1" in hb1["servers"], hb1
        assert hb1["ts"].endswith("Z") and "T" in hb1["ts"], hb1["ts"]
        bed.driver.tick()
        hb2 = json.loads(hb_path.read_text())
        assert hb2["tick"] == 2, hb2
        assert hb2["mono"] >= hb1["mono"]
    finally:
        bed.close()


def test_assigned_item_only_gets_a_worktree():
    bed = Bed(mode="ok")
    try:
        bed.set_items([bed.item("A9", status="ASSIGNED")])
        bed.driver.run_once()
        assert (bed.wts / "A9" / ".vp" / "PACKET.md").exists(), \
            "worktree template not created for an ASSIGNED item"
        assert not bed.oc_runs(), "spawned a turn for an ASSIGNED item"
        assert not bed.vpctl_verb("turn", "start")
        claims = bed.vpctl_verb("claim")
        assert len(claims) == 1, \
            "ASSIGNED item was never claimed, it would sit forever: %s" \
            % bed.vpctl_calls()
        assert claims[0][1] == "A9", claims[0]
        assert flag(claims[0], "--role") == "builder1", claims[0]
        assert flag(claims[0], "--worktree") == str(bed.wts / "A9"), claims[0]
        assert flag(claims[0], "--expected-rev") == "1", claims[0]
    finally:
        bed.close()


def test_escalation_falls_back_to_jsonl_when_vpctl_lacks_the_verb():
    bed = Bed(mode="quota")
    try:
        os.environ["VP_FAKE_NO_ESCALATE"] = "1"
        bed.driver.run_once()
        path = bed.run_root / "escalations.jsonl"
        assert path.exists(), "no escalations.jsonl fallback"
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        assert rows and rows[0]["kind"] == "QUOTA", rows
        assert rows[0]["item"] == "B1" and rows[0]["role"] == "daemon", rows
    finally:
        os.environ.pop("VP_FAKE_NO_ESCALATE", None)
        bed.close()


def test_zen_fallback_routes_a_quota_skipped_group():
    bed = Bed(mode="ok", zen_fallback=True)
    try:
        assert bed.driver.pick_server(1) == ("go1", "primary")
        bed.driver.mark_server_skipped("go1", "429")
        assert bed.driver.pick_server(1) == ("zen", "zen_fallback")
        bed.driver.mark_server_skipped("zen", "429")
        assert bed.driver.pick_server(1) == (None, None)
    finally:
        bed.close()


def test_session_id_parser_is_tolerant():
    for obj, want in (
            ({"sessionID": "s1"}, "s1"),
            ({"session_id": "s2"}, "s2"),
            ({"info": {"id": "s3"}}, "s3"),
            ({"a": {"b": [{"sessionId": "s4"}]}}, "s4"),
            ({"session": {"id": "s5"}}, "s5"),
            ({"nothing": 1}, None)):
        got = vpdriver.find_session_id(obj)
        assert got == want, "find_session_id(%r) -> %r, want %r" % (obj, got, want)


def test_exit_zero_is_not_success():
    tmp = Path(tempfile.mkdtemp(prefix="vpcls-"))
    try:
        missing = tmp / "RESULT.json"
        status, _d = vpdriver.classify("all good, finished\n", "finished",
                                       missing, "builder")
        assert status == vpdriver.PROGRESS_STOP, status
        status, _d = vpdriver.classify("provider error: upstream\n", "",
                                       missing, "builder")
        assert status == "STALLED", status
        status, _d = vpdriver.classify("HTTP 429 rate limit\n", "x",
                                       missing, "builder")
        assert status == "QUOTA", status
        status, _d = vpdriver.classify("401 unauthorized\n", "x",
                                       missing, "builder")
        assert status == "AUTH", status
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_item_with_a_running_attempt_is_not_spawned_again():
    bed = Bed(mode="ok")
    try:
        bed.set_running([{"attempt_id": "a0", "item": "B1",
                          "status": "RUNNING"}])
        spawned = bed.driver.run_once()
        assert not bed.oc_runs(), \
            "opened a second attempt on an item that already has a RUNNING one"
        assert not bed.vpctl_verb("turn", "start")
    finally:
        bed.close()


def test_missing_benchmark_refuses_instead_of_writing_an_empty_one():
    bed = Bed(mode="ok")
    try:
        rec = bed.item("B1")
        rec["benchmark_path"] = str(bed.tmp / "gone.md")
        bed.set_items([rec])
        bed.driver.run_once()
        wt = bed.wts / "B1"
        assert not (wt / ".vp" / "BENCHMARK.md").exists(), \
            "wrote an empty BENCHMARK.md instead of refusing"
        assert not bed.oc_runs(), "spawned a turn with no benchmark"
        esc = bed.vpctl_verb("escalate")
        assert esc, "no escalation for a missing benchmark: %s" % bed.vpctl_calls()
        detail = flag(esc[0], "--detail") or ""
        assert "BENCHMARK.md" in detail, detail
    finally:
        bed.close()


def test_every_child_process_gets_devnull_stdin():
    """`opencode run` blocks forever on an open stdin pipe -- the root cause of
    the first real run's hang.  Every turn, resume and export must be started
    with stdin=DEVNULL."""
    bed = Bed(mode="ok")
    try:
        bed.driver.run_once()
        kinds = bed.stdin_kinds()
        assert kinds, "the fake opencode never recorded its stdin"
        # one run + one export
        assert len(kinds) >= 2, kinds
        assert set(kinds) == {"devnull"}, \
            "opencode was started with stdin=%s (a pipe hangs the real binary)" \
            % sorted(set(kinds))
        rec = json.loads(bed.turn_records()[0].read_text())
        assert rec["classification"] == "DONE", rec["classification_detail"]
    finally:
        bed.close()


def test_usage_is_summed_over_every_step_finish():
    bed = Bed(mode="multistep")
    try:
        bed.driver.run_once()
        rec = json.loads(bed.turn_records()[0].read_text())
        assert rec["classification"] == "DONE", rec["classification_detail"]
        assert rec["steps"] == 2, rec["steps"]
        assert rec["last_step_reason"] == "stop", rec["last_step_reason"]
        assert rec["tokens_in"] == 1200, "tokens_in not summed: %s" % rec["tokens_in"]
        assert rec["tokens_out"] == 333, rec["tokens_out"]
        assert rec["tokens_reason"] == 12, rec["tokens_reason"]
        assert rec["cache_read"] == 900, rec["cache_read"]
        assert abs(rec["cost"] - 0.0031) < 1e-9, rec["cost"]
        ends = bed.vpctl_verb("turn", "end")
        assert flag(ends[0], "--tokens-in") == "1200", ends[0]
        assert flag(ends[0], "--cost") == "0.0031", ends[0]
    finally:
        bed.close()


def test_step_finish_stop_with_no_file_is_a_progress_stop():
    bed = Bed(mode="nofile")
    try:
        bed.driver.run_once()
        first = json.loads(bed.turn_records()[0].read_text())
        assert first["last_step_reason"] == "stop", first["last_step_reason"]
        assert "reason=stop" in first["classification_detail"], \
            first["classification_detail"]
        last = json.loads(bed.turn_records()[-1].read_text())
        assert last["classification"] == "STUCK", last["classification"]
    finally:
        bed.close()


def test_denied_tool_is_recorded_not_mistaken_for_a_stall():
    bed = Bed(mode="denied")
    try:
        bed.driver.run_once()
        rec = json.loads(bed.turn_records()[0].read_text())
        assert rec["denied_tools"], "a denied tool was not recorded"
        assert "rule which prevents" in rec["denied_tools"][0]["error"], rec
        assert rec["denied_tools"][0]["tool"] == "bash", rec["denied_tools"]
        # a denial is not a provider stall: reason=stop wins, so it resumes
        assert len(bed.oc_runs()) == 4, bed.oc_runs()
        last = json.loads(bed.turn_records()[-1].read_text())
        assert last["classification"] == "STUCK", last["classification"]
    finally:
        bed.close()


def test_session_id_comes_from_the_top_level_sessionID_field():
    acc = vpdriver.EventAccumulator()
    acc.feed({"type": "step_start", "timestamp": 1, "sessionID": "ses_top",
              "part": {"step": 1}})
    acc.feed({"type": "text", "sessionID": "ses_other",
              "part": {"type": "text", "text": "hello"}})
    acc.feed({"type": "step_finish", "sessionID": "ses_top",
              "part": {"reason": "stop", "cost": 0.5,
                       "tokens": {"total": 3, "input": 1, "output": 2,
                                  "reasoning": 0,
                                  "cache": {"write": 1, "read": 7}}}})
    assert acc.session_id == "ses_top", acc.session_id
    assert acc.text() == "hello", acc.text()
    assert acc.last_reason == "stop"
    u = acc.usage()
    assert u == {"tokens_in": 1, "tokens_out": 2, "tokens_reason": 0,
                 "cache_read": 7, "cost": 0.5}, u
    # no step_finish at all -> no invented zeros
    empty = vpdriver.EventAccumulator()
    empty.feed({"type": "step_start", "sessionID": "s"})
    assert empty.usage()["tokens_in"] is None, empty.usage()


def test_packet_paths_come_from_packet_show_when_the_row_lacks_them():
    bed = Bed(mode="ok")
    try:
        rec = bed.item("B1")
        rec.pop("packet_path")
        rec.pop("benchmark_path")
        bed.set_items([rec])
        bed.set_packet({"packet_id": "p1", "item": "B1",
                        "packet_path": str(bed.packet),
                        "benchmark_path": str(bed.benchmark),
                        "status": "READY"})
        bed.driver.run_once()
        assert bed.vpctl_verb("packet", "show"), \
            "packet show was never called: %s" % bed.vpctl_calls()
        wt = bed.wts / "B1"
        assert (wt / ".vp" / "BENCHMARK.md").read_text().startswith("B1 invariant")
        r = json.loads(bed.turn_records()[0].read_text())
        assert r["classification"] == "DONE", r["classification_detail"]
    finally:
        bed.close()


def test_escalate_uses_only_the_flags_vpctl_accepts():
    bed = Bed(mode="quota")
    try:
        bed.driver.run_once()
        esc = bed.vpctl_verb("escalate")
        assert esc, bed.vpctl_calls()
        call = esc[0]
        assert "--ref" not in call, \
            "vpctl escalate has no --ref; the call would exit 2: %s" % call
        assert flag(call, "--kind") == "QUOTA"
        assert flag(call, "--item") == "B1"
        assert flag(call, "--attempt") == "a1", call
        detail = flag(call, "--detail") or ""
        assert "record=" in detail, "turn record pointer lost: %s" % detail
        # nothing fell through to the jsonl fallback
        assert not (bed.run_root / "escalations.jsonl").exists()
    finally:
        bed.close()


def test_driver_side_failure_escalates_once_across_five_ticks():
    """The live-run defect: `turn start` exited 2, the driver escalated
    STALLED, released the item and retried on the very next tick -- 8
    identical escalations in 2 minutes."""
    bed = Bed(mode="ok")
    try:
        os.environ["VP_FAKE_FAIL_VERB"] = "turn start"
        for _ in range(5):
            bed.driver.tick()
            bed.driver.join(timeout=10)
        esc = bed.vpctl_verb("escalate")
        assert len(esc) == 1, \
            "expected exactly 1 escalation across 5 ticks, got %d: %s" \
            % (len(esc), [flag(e, "--detail") for e in esc])
        assert flag(esc[0], "--kind") == "STALLED", esc[0]
        detail = flag(esc[0], "--detail") or ""
        assert "1/3" in detail and "retry in 60s" in detail, detail
        starts = bed.vpctl_verb("turn", "start")
        assert len(starts) == 1, \
            "retried the failing item %d times instead of backing off" \
            % len(starts)
        assert bed.driver.item_is_backed_off("B1", 1)
    finally:
        os.environ.pop("VP_FAKE_FAIL_VERB", None)
        bed.close()


def test_three_driver_side_failures_mark_stuck_until_the_rev_changes():
    bed = Bed(mode="ok")
    try:
        drv = bed.driver
        n1 = drv.note_item_failure("B1", 1, "boom")
        n2 = drv.note_item_failure("B1", 1, "boom")
        assert (n1, n2) == (1, 2), (n1, n2)
        esc = bed.vpctl_verb("escalate")
        assert [flag(e, "--kind") for e in esc] == ["STALLED", "STALLED"], esc

        n3 = drv.note_item_failure("B1", 1, "boom")
        assert n3 == 3
        esc = bed.vpctl_verb("escalate")
        assert flag(esc[-1], "--kind") == "STUCK", esc[-1]
        assert "3 consecutive" in (flag(esc[-1], "--detail") or ""), esc[-1]

        # stuck: no retry at all, even once the backoff window would be over
        drv._fail["B1"]["next_try"] = 0.0
        assert drv.item_is_backed_off("B1", 1), "retried a STUCK item"
        # a new rev is a new situation
        assert not drv.item_is_backed_off("B1", 2), \
            "a changed rev must clear the stuck flag"
        assert "B1" not in drv._fail

        # and a good turn resets the counter
        drv.note_item_failure("B1", 3, "boom")
        drv.clear_item_failures("B1")
        assert not drv.item_is_backed_off("B1", 3)
    finally:
        bed.close()


def test_a_failing_claim_is_backed_off_too():
    bed = Bed(mode="ok")
    try:
        bed.set_items([bed.item("A9", status="ASSIGNED")])
        os.environ["VP_FAKE_FAIL_VERB"] = "claim"
        os.environ["VP_FAKE_FAIL_RC"] = "4"       # conflict: stale rev
        for _ in range(4):
            bed.driver.tick()
        assert len(bed.vpctl_verb("claim")) == 1, bed.vpctl_verb("claim")
        esc = bed.vpctl_verb("escalate")
        assert len(esc) == 1, esc
        assert "claim" in (flag(esc[0], "--detail") or ""), esc[0]
    finally:
        os.environ.pop("VP_FAKE_FAIL_VERB", None)
        os.environ.pop("VP_FAKE_FAIL_RC", None)
        bed.close()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    passed = failed = 0
    failures = []
    for fn in TESTS:
        try:
            fn()
            passed += 1
            print("PASS %s" % fn.__name__)
        except Exception:
            failed += 1
            tb = traceback.format_exc().strip().splitlines()
            failures.append("%s: %s" % (fn.__name__, tb[-1]))
            print("FAIL %s" % fn.__name__)
            print("\n".join("    " + l for l in tb[-6:]))
    total = passed + failed
    print("\nvpdriver: %d/%d" % (passed, total))
    for f in failures:
        print("  ! " + f)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
