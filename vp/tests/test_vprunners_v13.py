#!/usr/bin/env python3
"""tests/test_vprunners_v13.py -- item 2: OpenCodeHttpRunner against a fake
`opencode serve` (stdlib http.server), the CLI runner's provider-error
watchdog, AgyRunner, CodexRunner.preopen.  No network beyond 127.0.0.1, no
real binaries."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP))

import vprunners  # noqa: E402
from vprunners import (OpenCodeHttpRunner, OpenCodeRunner, AgyRunner, CodexRunner,  # noqa: E402
                       TurnSpec, classify_provider_error)


# -- fake opencode serve ----------------------------------------------------------------

class FakeServe(object):
    """Scripted server.  `messages` is a list of GET /session/<id>/message
    bodies returned in order (the last one repeats)."""

    def __init__(self, messages, prompt_status=204, session_id="ses_fake1"):
        self.messages = list(messages)
        self.prompt_status = prompt_status
        self.session_id = session_id
        self.calls = []
        self.polls = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                path = self.path.split("?")[0]
                outer.calls.append(("POST", path, body))
                if path == "/session":
                    return self._send(200, {"id": outer.session_id, "title": body.get("title")})
                if path.endswith("/prompt_async"):
                    return self._send(outer.prompt_status, None if outer.prompt_status == 204
                                      else {"name": "ProviderAuthError", "data": {"message": "invalid api key"}})
                if path.endswith("/abort"):
                    return self._send(200, True)
                return self._send(404, {"error": "nope"})

            def do_GET(self):
                path = self.path.split("?")[0]
                outer.calls.append(("GET", path, None))
                if path.endswith("/message"):
                    i = min(outer.polls, len(outer.messages) - 1)
                    outer.polls += 1
                    return self._send(200, outer.messages[i])
                return self._send(404, {})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.th.start()

    def close(self):
        self.httpd.shutdown()


def assistant(text=None, completed=True, error=None, tokens=None, cost=0.01,
              model="muse-spark-1.3-contributor", provider="opencode-go", finish="stop", parts=None):
    info = {"id": "msg_a1", "sessionID": "ses_fake1", "role": "assistant",
            "time": {"created": 1}, "modelID": model, "providerID": provider,
            "tokens": tokens or {"input": 120, "output": 40, "reasoning": 5, "cache": {"read": 100, "write": 0}},
            "cost": cost, "finish": finish}
    if completed:
        info["time"]["completed"] = 2
    if error:
        info["error"] = error
    ps = list(parts or [])
    if text:
        ps.append({"id": "prt1", "type": "text", "text": text})
    return {"info": info, "parts": ps}


USER = {"info": {"id": "msg_u1", "sessionID": "ses_fake1", "role": "user", "time": {"created": 0}},
        "parts": [{"type": "text", "text": "prompt"}]}


def spec_for(tmp_path, url, role="builder", **kw):
    wt = tmp_path / "wt"
    (wt / ".vp").mkdir(parents=True, exist_ok=True)
    (wt / ".vp" / "BASE").write_text("a" * 40 + "\n")
    base = dict(server_url=url, variant="xhigh", agent="vp-builder", timeout_s=kw.pop("timeout_s", 30),
                log_dir=str(tmp_path / "logs"), tag="1", out_path=str(wt / ".vp" / "RESULT.json"))
    base.update(kw)
    return TurnSpec(role, "L02", wt, "Read .vp/PACKET.md and stop.",
                    "opencode-go/muse-spark-1.3-contributor", **base)


def test_http_runner_done_reads_record_tokens_and_cost(tmp_path):
    srv = FakeServe([[], [USER, assistant(text="working", completed=False)],
                     [USER, assistant(text="done")]])
    try:
        r = OpenCodeHttpRunner(poll_s=0.05)
        spec = spec_for(tmp_path, srv.url)
        rec = {"item": "L02", "attempt": 1, "commit": "b" * 40, "base": "a" * 40,
               "diff_stat": {"files": 1, "insertions": 1, "deletions": 0},
               "checks": [{"name": "x", "command": "true", "exit": 0, "log": ""}],
               "disputes": [], "blocked": None, "notes": ""}
        Path(spec.out_path).write_text(json.dumps(rec))
        spec.validator = lambda p: (True, [])
        out = r.run(spec)
        assert out.status == "DONE", (out.status, out.detail)
        assert out.session_id == "ses_fake1"
        assert out.usage == {"tokens_in": 120, "tokens_out": 40, "tokens_reason": 5,
                             "cache_read": 100, "cache_write": 0, "cost": 0.01}
        assert out.text == "done" and out.model_seen == "opencode-go/muse-spark-1.3-contributor"
        create = next(c for c in srv.calls if c[0] == "POST" and c[1] == "/session")
        assert create[2]["title"] == spec.title and create[2]["agent"] == "vp-builder"
        prompt = next(c for c in srv.calls if c[1].endswith("/prompt_async"))
        assert prompt[2]["model"] == {"providerID": "opencode-go", "modelID": "muse-spark-1.3-contributor"}
        assert prompt[2]["variant"] == "xhigh"
        assert prompt[2]["parts"] == [{"type": "text", "text": spec.prompt}]
        assert not any(c[1].endswith("/abort") for c in srv.calls)
        log = (tmp_path / "logs" / "1-opencode-http.jsonl").read_text()
        assert '"prompt_async"' in log and '"messages"' in log
        d = out.to_dict()
        assert d["usage"]["cost"] == 0.01 and d["model_seen"].endswith("contributor")
    finally:
        srv.close()


def test_http_runner_provider_error_is_classified_monthly_quota(tmp_path):
    err = {"name": "APIError", "data": {"message": "Monthly usage limit reached. Resets in 18 days. "
                                        "To continue using this model now, enable usage from your available balance",
                                        "isRetryable": False, "statusCode": 402}}
    srv = FakeServe([[], [USER, assistant(text=None, completed=False, error=err)]])
    try:
        out = OpenCodeHttpRunner(poll_s=0.05).run(spec_for(tmp_path, srv.url))
        assert out.status == "QUOTA_WEEKLY", (out.status, out.detail)
        assert "Monthly usage limit" in out.detail
        assert out.usage["reset_minutes"] == 18 * 24 * 60
        assert not Path(spec_for(tmp_path, srv.url).out_path).exists()
    finally:
        srv.close()


def test_http_runner_rate_and_degraded_and_auth(tmp_path):
    for msg, want in (("rate_limit_exceeded: too many requests", "RATE"),
                      ("upstream response was not valid json 502", "DEGRADED"),
                      ("ProviderAuthError invalid api key", "AUTH"),
                      ("5-hour usage limit reached. Resets in 45 minutes", "QUOTA_ROLLING")):
        srv = FakeServe([[], [USER, assistant(completed=False,
                                              error={"name": "UnknownError", "data": {"message": msg}})]])
        try:
            out = OpenCodeHttpRunner(poll_s=0.05).run(spec_for(tmp_path, srv.url))
            assert out.status == want, (msg, out.status)
        finally:
            srv.close()
    assert classify_provider_error("5-hour usage limit reached. Resets in 45 minutes")[1] == 45


def test_http_runner_timeout_posts_abort(tmp_path):
    srv = FakeServe([[USER, assistant(text="still", completed=False)]])
    try:
        out = OpenCodeHttpRunner(poll_s=0.05).run(spec_for(tmp_path, srv.url, timeout_s=0.4))
        assert out.status == "RUNNER_TIMEOUT"
        assert any(c[1] == "/session/ses_fake1/abort" for c in srv.calls)
    finally:
        srv.close()


def test_http_runner_stop_flag_aborts(tmp_path):
    srv = FakeServe([[USER, assistant(text="still", completed=False)]])
    try:
        flag = threading.Event()
        threading.Timer(0.3, flag.set).start()
        out = OpenCodeHttpRunner(poll_s=0.05).run(spec_for(tmp_path, srv.url), abort_flag=flag)
        assert out.status == "ABORTED"
        assert any(c[1].endswith("/abort") for c in srv.calls)
    finally:
        srv.close()


def test_http_runner_fence_record_for_grader_and_denied_tool(tmp_path):
    fence = "```json\n" + json.dumps({"item": "L02", "attempt": 1, "commit": "b" * 40,
                                       "lines": [{"id": "B1", "kind": "evidence", "verdict": "PASS",
                                                  "evidence": "a.py:1", "note": ""}],
                                       "all_pass": True}) + "\n```"
    denied = {"id": "prt9", "type": "tool", "callID": "c", "tool": "write",
              "state": {"status": "error", "error": "there is a rule which prevents this", "input": {},
                        "time": {"start": 1, "end": 2}}}
    srv = FakeServe([[], [USER, assistant(text=fence, parts=[denied])]])
    try:
        spec = spec_for(tmp_path, srv.url, role="junior", expect_fence=True)
        spec.out_path = str(Path(spec.cwd) / ".vp" / "FINDINGS.json")
        spec.validator = lambda p: (True, [])
        out = OpenCodeHttpRunner(poll_s=0.05).run(spec)
        assert out.status == "DONE" and "fence" in out.detail
        assert json.loads(Path(spec.out_path).read_text())["all_pass"] is True
        assert out.denied_tools[0]["tool"] == "write"
    finally:
        srv.close()


def test_http_runner_progress_stop_without_record_and_export(tmp_path):
    srv = FakeServe([[], [USER, assistant(text="I looked around.")]])
    try:
        r = OpenCodeHttpRunner(poll_s=0.05)
        spec = spec_for(tmp_path, srv.url)
        out = r.run(spec)
        assert out.status == "PROGRESS_STOP" and "no record" in out.detail
        exp = r.export(spec, "ses_fake1", str(tmp_path / "export.json"))
        assert exp["exit"] == 0 and json.loads((tmp_path / "export.json").read_text())[0]["info"]["role"] == "user"
    finally:
        srv.close()


def test_http_runner_model_mismatch_and_prompt_refused(tmp_path):
    srv = FakeServe([[], [USER, assistant(text="x", model="deepseek-v4.1-flash")]])
    try:
        out = OpenCodeHttpRunner(poll_s=0.05).run(spec_for(tmp_path, srv.url))
        assert out.status == "MODEL_MISMATCH"
    finally:
        srv.close()
    srv = FakeServe([[]], prompt_status=401)
    try:
        out = OpenCodeHttpRunner(poll_s=0.05).run(spec_for(tmp_path, srv.url))
        assert out.status == "AUTH", (out.status, out.detail)
    finally:
        srv.close()


def test_http_runner_server_down_is_degraded_not_crash(tmp_path):
    out = OpenCodeHttpRunner(poll_s=0.05).run(spec_for(tmp_path, "http://127.0.0.1:1"))
    assert out.status == "DEGRADED" and "unreachable" in out.detail


# -- CLI fallback: watchdog kills the hung child on info.error -------------------------------

def _script(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_cli_runner_watchdog_aborts_hung_child_on_provider_error(tmp_path, monkeypatch):
    err = {"name": "APIError", "data": {"message": "Monthly usage limit reached", "isRetryable": False}}
    srv = FakeServe([[USER, assistant(completed=False, error=err)]])
    fake = _script(tmp_path / "opencode", "#!/bin/sh\n"
                   'echo \'{"type":"step_start","sessionID":"ses_fake1","part":{}}\'\n'
                   "sleep 30\n")
    monkeypatch.setattr(OpenCodeRunner, "ERROR_POLL_S", 0.1)
    try:
        r = OpenCodeRunner(binary=fake)
        t0 = time.monotonic()
        out = r.run(spec_for(tmp_path, srv.url, timeout_s=25))
        assert out.status == "QUOTA_WEEKLY", (out.status, out.detail)
        assert "child aborted" in out.detail
        assert time.monotonic() - t0 < 10, "the hung child was killed by the watchdog, not the timeout"
    finally:
        srv.close()


# -- agy -----------------------------------------------------------------------------------------

def test_agy_runner_argv_and_structured_output(tmp_path):
    result = {"type": "result", "session_id": "agy-1", "result": "ok",
              "structured_output": {"item": "L02", "verdict": "APPROVE"},
              "usage": {"input_tokens": 10, "output_tokens": 3}, "total_cost_usd": 0}
    fake = _script(tmp_path / "agy", "#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n" % json.dumps(result))
    r = AgyRunner(binary=fake)
    wt = tmp_path / "wt"
    (wt / ".vp").mkdir(parents=True)
    spec = TurnSpec("advisor", "L02", wt, "pre-review this packet", "gemini-3.8-flash-high",
                    schema_text='{"type":"object"}', out_path=str(wt / ".vp" / "ADVICE.json"),
                    log_dir=str(tmp_path / "logs"), tag="1")
    assert r.argv(spec) == [fake, "-p", spec.prompt, "--output-format", "json",
                            "--model", "gemini-3.8-flash-high", "--json-schema", '{"type":"object"}']
    out = r.run(spec)
    assert out.status == "DONE" and out.structured == {"item": "L02", "verdict": "APPROVE"}
    assert out.session_id == "agy-1" and out.usage["tokens_in"] == 10 and out.runner == "agy"
    assert (tmp_path / "logs" / "1-agy.json").exists()


# -- codex pre-open --------------------------------------------------------------------------

def test_codex_preopen_returns_the_thread_id_before_start(tmp_path):
    fake = _script(tmp_path / "codex", "#!/bin/sh\n"
                   "# record fd0 kind and args\n"
                   'echo \'{"type":"thread.started","thread_id":"0199-thread-abc"}\'\n'
                   'echo \'{"type":"turn.started","model":"gpt-5.6-luna"}\'\n'
                   'echo \'{"type":"item.completed","item":{"type":"agent_message","text":"READY"}}\'\n'
                   'echo \'{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":1}}\'\n')
    r = CodexRunner(binary=fake)
    wt = tmp_path / "wt"
    wt.mkdir()
    spec = TurnSpec("junior", "JR-1", wt, "review it", "gpt-5.6-luna", effort="xhigh",
                    log_dir=str(tmp_path / "logs"), tag="1")
    tid, detail = r.preopen(spec)
    assert tid == "0199-thread-abc", detail
    assert (tmp_path / "logs" / "1-preopen-codex.jsonl").exists()
    # the review turn then resumes that thread
    argv = r.argv(TurnSpec("junior", "JR-1", wt, "review it", "gpt-5.6-luna", session_id=tid),
                  tmp_path / "last.txt")
    assert argv[1:3] == ["exec", "resume"] and tid in argv


def test_runner_for_defaults_to_http_transport():
    assert isinstance(vprunners.runner_for("opencode"), OpenCodeHttpRunner)
    assert isinstance(vprunners.runner_for("opencode", opencode_transport="cli"), OpenCodeRunner)
    assert isinstance(vprunners.runner_for("agy"), AgyRunner)
