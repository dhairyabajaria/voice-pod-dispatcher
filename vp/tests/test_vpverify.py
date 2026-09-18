# -*- coding: utf-8 -*-
"""tests/test_vpverify.py -- AUDIT-3 `lanedriver.py verify` over a real run
tree: seals (chain + prefixes), state replay vs run-state/LEDGER.md, harvest
shas vs the trunk.  Each check is shown to PASS on an honest tree and to
FAIL on the one tamper it is for."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VP = HERE.parent
sys.path.insert(0, str(VP))
sys.path.insert(0, str(HERE))

import lanedriver  # noqa: E402
import vpverify  # noqa: E402
from test_lanedriver import Env, FakeRunner, routed_pass, settle  # noqa: E402


def _run(tmp_path):
    env = Env(tmp_path)
    env.activate()
    drv = env.driver({"opencode": FakeRunner(default=routed_pass), "codex": FakeRunner(), "claude": FakeRunner()})
    settle(drv, 3)
    drv.render()
    drv.seal(reason="test")
    return env, drv


def _verify(env, drv):
    return vpverify.verify_all(env.run_root, env.state.parent, env.state, drv.trunk)


def test_verify_passes_on_an_honest_tree_and_replays_the_state_exactly(tmp_path):
    env, drv = _run(tmp_path)
    rep = _verify(env, drv)
    assert rep["ok"], json.dumps(rep, indent=1)[:2000]
    assert rep["seals"]["seals"] >= 2 and rep["seals"]["chain_ok"] and rep["seals"]["files_checked"] > 10
    st = rep["state"]
    assert st["events"] > 5 and st["replay_diffs"] == [] and st["problems"] == [] and st["ledger_lag"] == []
    assert st["ledger_rows"] == st["tasks"]
    assert st["events_outside_control_jsonl"] == [], "every state change went through the driver"
    assert st["events_before_control_jsonl"] >= 1
    assert rep["shas"]["harvests"] >= 2 and rep["shas"]["shas_checked"] >= 2 and rep["shas"]["problems"] == []
    # renders.jsonl carries what LEDGER.md took from driver memory
    rows = [json.loads(l) for l in (env.run_root / "renders.jsonl").read_text().splitlines()]
    assert rows[-1]["sequence"] == json.loads(env.state.read_text())["sequence"]
    assert set(rows[-1]) >= {"spend_usd", "tokens", "live", "parked", "counts", "code_version", "activation_id"}
    # the CLI form writes verify/verify-<ts>.json and exits 0
    rc = lanedriver.main(["--roster", str(env.run_root / "roster.json"), "verify", "--summary"])
    assert rc == 0 and list((env.run_root / "verify").glob("verify-*.json"))


def test_verify_catches_a_rewritten_prefix_a_broken_chain_and_an_orphan_sha(tmp_path):
    env, drv = _run(tmp_path)
    assert _verify(env, drv)["ok"]
    # 1. an append-only file edited inside its sealed prefix
    log = env.run_root / "driver.log"
    data = log.read_bytes()
    log.write_bytes(data[:10] + b"X" + data[11:])
    rep = _verify(env, drv)
    assert any("driver.log first" in p and "changed since the seal" in p for p in rep["seals"]["problems"])
    log.write_bytes(data)
    assert _verify(env, drv)["ok"]
    # 2. a truncated append-only file
    log.write_bytes(data[:100])
    rep = _verify(env, drv)
    assert any("driver.log shrank" in p for p in rep["seals"]["problems"])
    log.write_bytes(data)
    # 3. a seal line removed from the middle: the chain breaks
    seals = env.run_root / "seals.jsonl"
    drv.seal(reason="third")
    lines = seals.read_text().splitlines()
    assert len(lines) >= 3
    seals.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    rep = _verify(env, drv)
    assert not rep["seals"]["chain_ok"] and any("chain broken" in p for p in rep["seals"]["problems"])
    seals.write_text("\n".join(lines) + "\n")
    assert _verify(env, drv)["seals"]["chain_ok"]
    # 4. a manifest edited after sealing
    rec = json.loads(lines[-1])
    man = env.run_root / rec["manifest"]
    body = man.read_text()
    man.write_text(body.replace('"reason": "third"', '"reason": "forged"'))
    rep = _verify(env, drv)
    assert any("sha256" in p and "!= recorded" in p for p in rep["seals"]["problems"])
    man.write_text(body)
    # 5. a rewritten mutable file is DRIFT, not a broken seal; a new seal clears it
    (env.run_root / "DECISIONS.md").write_text("D1: something\n")
    drv.seal(reason="after decisions")
    (env.run_root / "DECISIONS.md").write_text("D1: something\nD2: more\n")
    rep = _verify(env, drv)
    assert rep["seals"]["problems"] == [] and "DECISIONS.md" in rep["seals"]["drift_since_newest_seal"]
    drv.seal(reason="cover")
    assert "DECISIONS.md" not in _verify(env, drv)["seals"]["drift_since_newest_seal"]
    # 6. a harvest whose output_sha is not in the trunk (the TRUNK-04 class)
    h = next(env.run_root.glob("turns/L02/*/harvest.json"))
    doc = json.loads(h.read_text())
    good = doc["output_sha"]
    doc["output_sha"] = "0" * 40
    h.write_text(json.dumps(doc))
    rep = _verify(env, drv)
    assert any("output_sha 000000000000 is not a commit" in p for p in rep["shas"]["problems"])
    doc["output_sha"] = good
    h.write_text(json.dumps(doc))
    # 7. run-state edited by hand: the replay disagrees
    st = json.loads(env.state.read_text())
    st["tasks"]["L02"]["state"] = "INTEGRATED"
    env.state.write_text(json.dumps(st, indent=2))
    rep = _verify(env, drv)
    assert any(p.startswith("L02: run-state INTEGRATED vs events VERIFIED") for p in rep["state"]["problems"])
    assert "L02" in rep["state"]["ledger_lag"]


def test_verify_lists_state_changes_made_outside_the_driver(tmp_path):
    """an invalidate/block run straight against the scheduler (Architect or
    Fixer CLI) has no control.jsonl line: verify reports it by sequence"""
    env, drv = _run(tmp_path)
    assert _verify(env, drv)["state"]["events_outside_control_jsonl"] == []
    # every catalog row of the tiny fixture is VERIFIED after 3 ticks: a
    # register-candidate by hand is the out-of-driver state change under test
    from test_lanedriver import register_candidate
    sha, _tree = register_candidate(env)
    rep = _verify(env, drv)
    outside = rep["state"]["events_outside_control_jsonl"]
    assert [o["type"] for o in outside] == ["CANDIDATE_REGISTERED"]
    assert outside[0]["sequence"] > rep["state"]["control_first_sequence"]
    assert rep["state"]["problems"] == [], "the replay still agrees: the event log is complete"

