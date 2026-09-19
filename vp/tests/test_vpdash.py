#!/usr/bin/env python3
"""tests/test_vpdash.py -- the dashboard renders from the sources only."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import vpdash  # noqa: E402
from test_vpjourney import build_run, ts  # noqa: E402


def with_extras(tmp_path):
    run_root = build_run(tmp_path)
    (run_root / "alerts.jsonl").write_text("".join(json.dumps(a) + "\n" for a in [
        {"ts": ts(11, 31), "kind": "PROOF_UNKNOWN", "task": "P-FIX", "text": "P-FIX B1 unknown"},
        {"ts": ts(12, 1), "kind": "PROOF_UNKNOWN", "task": "P-FIX-R1", "text": "P-FIX-R1 B1 unknown"},
        {"ts": ts(12, 31), "kind": "PROOF_UNKNOWN", "task": "P-FIX-R1", "text": "P-FIX-R1 B1 unknown"},
        {"ts": ts(13), "kind": "IDLE", "task": None, "text": "nothing to dispatch"},
    ]) + '{"torn": tru')
    (run_root / "proofs" / "circleci-pipelines.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"ts": ts(12, 5), "proof_id": "proof-P-FIX-R1-1", "pipeline_id": "pipe-1", "account": "A1", "sha": "d" * 40},
        {"ts": ts(12, 6), "proof_id": "proof-P-FIX-HOSTED-R1-9", "pipeline_id": "pipe-2", "account": "3", "sha": "e" * 40},
        {"ts": ts(12, 7), "proof_id": "proof-X-1", "pipeline_id": None, "account": "A2", "sha": "e" * 40,
         "status": "refused_gate", "reason": "DELIVERY-1 closed at trigger time"},
    ]))
    p = json.loads((run_root / "proofs" / "proof-P-FIX-R1-1.json").read_text())
    p.update({"route": "circleci", "account": "A1", "pipeline_id": "pipe-1"})
    (run_root / "proofs" / "proof-P-FIX-R1-1.json").write_text(json.dumps(p))
    roster = json.loads((run_root / "roster.json").read_text())
    roster["owner_gates"] = {"DELIVERY-1": False, "DELIVERY-2": True, "_comment": "x"}
    roster["proof"] = {"circleci": {"enabled": True, "mode": "overflow", "max_pipelines_per_day": 40,
                                    "max_pipelines_in_flight": 12, "rotation": ["A1"], "spread": ["A1"], "account": "A1",
                                    "deadline_min": 90}}
    (run_root / "roster.json").write_text(json.dumps(roster))
    snap1 = dict(roster, owner_gates={"DELIVERY-1": True, "DELIVERY-2": True})
    (run_root / "roster.1.json").write_text(json.dumps(snap1))
    os.utime(run_root / "roster.1.json", (time.time() - 7200, time.time() - 7200))
    snap2 = dict(roster)
    (run_root / "roster.2.json").write_text(json.dumps(snap2))
    os.utime(run_root / "roster.2.json", (time.time() - 3600, time.time() - 3600))
    (run_root / "driver.heartbeat").write_text(json.dumps({"ts": vpdash.vpjourney.iso(vpdash.utc_now()), "pid": 1, "tick": 5,
                                                           "active": 0, "sequence": 19, "budget": {"spent_usd": 1, "max_usd": 4}}))
    (run_root / "RULINGS-TRACKING.md").write_text(
        "| # | Ruled by | Ruled at (UTC) | Action | Status | Executed at | Evidence |\n|---|---|---|---|---|---|---|\n"
        "| R1 | Architect | 10:00Z | do a | EXECUTED | 10:05Z | ok |\n"
        "| R2 | Architect | 10:10Z | do b | PENDING | — | — |\n"
        "| R3 | Architect | 10:20Z | do c | EXECUTED (via retry) | 10:30Z | ok |\n")
    return run_root


def test_snapshot_totals_come_from_roots_and_name_their_sources(tmp_path):
    d = vpdash.Dash(with_extras(tmp_path))
    d.refresh(force_control=True)
    s = d.snapshot
    assert not d.errors, d.errors
    assert s["summary"]["roots"] == 3 and s["summary"]["scope"]["work_items_fixed"] == 2
    assert s["freshness"]["heartbeat_age_s"] < 60 and s["freshness"]["events_sequence"] == 19
    assert s["freshness"]["alerts_bad_lines"] == 0            # the torn tail is held, not counted as bad
    assert s["sources"]["events"].endswith("orchestration-state/events")
    assert {r["id"] for r in s["roots"]} == {"L00", "P-FIX", "ORPHAN-9"}
    pf = [r for r in s["roots"] if r["id"] == "P-FIX"][0]
    assert pf["primary_state"] == "VERIFIED" and pf["hosted_state"] == "BLOCKED" and pf["superseded_open"] == ["P-FIX-HOSTED"]
    assert s["verify"]["errors"] == 0 and s["verify"]["calls_read"] == 2


def test_alerts_panel_uses_pattern_severity_and_backlog_from_events(tmp_path):
    d = vpdash.Dash(with_extras(tmp_path))
    d.refresh(force_control=True)
    a = d.snapshot["alerts"]
    assert a["total"] == 4 and a["backlog_points"] > 0
    by_kind = {r["kind"]: r for r in a["recent"]}
    third = [r for r in a["recent"] if r["ts"] == ts(12, 31)][0]
    assert third["severity"] == "urgent" and third["repeat_family"] == 3      # P-FIX / P-FIX-R1 are one family
    assert by_kind["IDLE"]["severity"] == "routine"
    assert a["recorded_severity_present"] is False                             # driver did not write severity


def test_circleci_panel_covers_every_account_and_the_failed_trigger_rows(tmp_path):
    d = vpdash.Dash(with_extras(tmp_path))
    d.refresh(force_control=True)
    c = d.snapshot["circleci"]
    accounts = {a["account"]: a for a in c["accounts"]}
    assert set(accounts) >= {"3", "1", "2", "A1", "A2", "A3"}
    assert c["max_pipelines_per_day"] == 40 and c["gate_open"] is False and c["triggered_total"] == 2
    assert accounts["A1"]["triggered_total"] == 1 and accounts["A1"]["proofs"] == {"PASS": 1} and accounts["A1"]["in_flight"] == []
    assert accounts["3"]["triggered_total"] == 1 and len(accounts["3"]["lost"]) == 1   # triggered, no proof record, far past the deadline
    assert accounts["A2"]["refused_gate"] == 1 and accounts["A2"]["triggered_total"] == 0
    assert c["ledger_has_status_field"] is True and c["note"] is None
    # a D65 refusal record (no account, no pipeline) is a refusal, not an unattributed proof
    (d.run_root / "proofs" / "proof-P-FIX-HOSTED-R1-2.json").write_text(json.dumps(
        {"proof_id": "proof-P-FIX-HOSTED-R1-2", "status": "BLOCKED_GATE", "route": "circleci", "account": None,
         "pipeline_id": None, "ts": ts(12, 9), "reason": "circleci: owner gate DELIVERY-1 not open at trigger time"}))
    d.refresh()
    c = d.snapshot["circleci"]
    assert c["proofs_refused"] == {"BLOCKED_GATE": 1} and c["proofs_unattributed"] == {}


def test_decisions_panel_gate_age_from_snapshots_and_held_rows(tmp_path):
    d = vpdash.Dash(with_extras(tmp_path))
    d.refresh(force_control=True)
    dec = d.snapshot["decisions"]
    gates = {g["gate"]: g for g in dec["gates"]}
    d1 = gates["DELIVERY-1"]
    assert d1["open"] is False and d1["since_exact"] is True and 3000 < d1["age_s"] < 4200   # flipped between roster.1 and roster.2
    assert d1["held_rows"] == ["P-FIX-HOSTED-R1"] and d1["held_roots"] == ["P-FIX"]
    d2 = gates["DELIVERY-2"]
    assert d2["open"] is True and d2["since_exact"] is False                                   # true in every snapshot: lower bound only
    assert dec["closed_gates_holding_work"] == 1 and dec["rows_held_by_closed_gates"] == 1
    assert d1["source"] == "roster.N.json mtimes"
    # D64: once the driver writes gates.jsonl the flip time is exact and wins
    (d.run_root / "gates.jsonl").write_text(json.dumps({"ts": ts(9), "gate": "DELIVERY-1", "from": None, "to": True, "why": "startup"}) + "\n"
                                            + json.dumps({"ts": ts(9, 30), "gate": "DELIVERY-1", "from": True, "to": False, "why": "reload"}) + "\n")
    d.refresh()
    g = {g["gate"]: g for g in d.snapshot["decisions"]["gates"]}["DELIVERY-1"]
    assert g["source"] == "gates.jsonl" and g["since_ts"] == ts(9, 30) and g["since_exact"] is True
    assert [r["id"] for r in dec["open_rulings"]] == ["R2"]
    assert dec["blocked_by_reason"] == [{"reason": "OWNER_GATE: (no reason)", "rows": 1, "roots": ["P-FIX"]}]


def test_http_api_serves_the_snapshot_and_drilldowns(tmp_path):
    d = vpdash.Dash(with_extras(tmp_path))
    d.refresh(force_control=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), vpdash.make_handler(d))
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        def get(path):
            try:
                with urllib.request.urlopen(base + path, timeout=5) as r:
                    return r.status, r.headers.get("Content-Type", ""), r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, "", e.read().decode()
        st, ct, body = get("/")
        assert st == 200 and "text/html" in ct and "<script" in body and "http://" not in body.split("<body>")[1]
        st, _ct, body = get("/api/summary")
        assert st == 200 and json.loads(body)["summary"]["roots"] == 3
        st, _ct, body = get("/api/journey?root=P-FIX-R1")
        j = json.loads(body)
        assert st == 200 and j["root"] == "P-FIX" and len(j["attempts"]) == 4
        st, _ct, body = get("/api/row?task=P-FIX-HOSTED")
        assert st == 200 and json.loads(body)["root"][0] == "P-FIX"
        st, _ct, _body = get("/api/journey?root=NOPE")
        assert st == 404
        st, ct, body = get("/api/ledger.md")
        assert st == 200 and body.startswith("# LEDGER (derived from")
        st, _ct, body = get("/healthz")
        assert st == 200 and json.loads(body)["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_once_cli_prints_a_summary(tmp_path, capsys):
    assert vpdash.main(["--run-root", str(with_extras(tmp_path)), "--once"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["roots"] == 3 and out["circleci_accounts"]["A1"]["total"] == 1
