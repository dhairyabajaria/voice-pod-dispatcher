#!/usr/bin/env python3
"""Self-contained test runner for vpstore / vpctl.

    python3 tests/test_vpstore.py

Plain asserts, a temp RUN_ROOT per test, no external packages, no network,
no servers, nothing outside the temp dirs.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
VP = os.path.dirname(HERE)
sys.path.insert(0, VP)

import vpstore  # noqa: E402
from vpstore import Conflict, Refused, Store, Usage  # noqa: E402


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

TESTS = []
FAILURES = []


def test(fn):
    TESTS.append(fn)
    return fn


def new_root(tmp, name, roster=None):
    root = os.path.join(tmp, name)
    os.makedirs(root, exist_ok=True)
    if roster is not None:
        with open(os.path.join(root, "roster.json"), "w", encoding="utf-8") as fh:
            json.dump(roster, fh)
    os.environ["VP_RUN_ROOT"] = root
    # keep the real driver STOP file out of every test
    os.environ["VP_STOP_FILE"] = os.path.join(root, "STOP")
    return root


def refuses(fn, *a, **kw):
    """Assert the call refuses; return the Refused exception."""
    try:
        fn(*a, **kw)
    except Refused as e:
        return e
    raise AssertionError(f"expected Refused from {fn.__name__}, got success")


def conflicts(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except Conflict as e:
        return e
    raise AssertionError(f"expected Conflict from {fn.__name__}, got success")


def write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


def findings_doc(item, attempt, commit, verdicts):
    return {"item": item, "attempt": attempt, "commit": commit,
            "lines": [{"id": f"L{i}", "kind": "invariant", "verdict": v,
                       "evidence": "file:1", "note": ""}
                      for i, v in enumerate(verdicts)],
            "all_pass": all(v == "PASS" for v in verdicts)}


def bootstrap(root, item="I-1", base="base000", group=1):
    """run + roles + packet ready -> item in READY."""
    st = Store(root)
    st.run_init("run-test", trunk_head=base)
    st.role_add("bld-1", "builder", session_ref="sess-b1")
    st.role_add("bld-2", "builder", session_ref="sess-b2")
    st.role_add("jr-1", "junior")
    st.role_add("sr-1", "senior", session_ref="sess-s1")
    st.role_add("sr-2", "senior", session_ref="sess-s2")
    st.role_add("cos-1", "cos")
    st.role_add("int-1", "integrator")
    pid = st.packet_submit(item, "B.md", "P.md", base, submitted_by="sr-1")
    st.packet_ready(pid)
    return st


def to_approved(st, tmp, item="I-1", base="base000", cand="cand111",
                plan_reviewer="ref-plan", code_reviewer="ref-code"):
    """Drive one item READY -> APPROVED and return (review_id, proof_id)."""
    st.assign(item, 1)
    rev = st.get_item(item)["rev"]
    st.claim(item, "bld-1", os.path.join(tmp, "wt", item), rev)
    a = st.turn_start(item, "build", session="s1", server="go1", model="m")
    st.turn_end(a, "DONE", result="RESULT.json", cost=0.1)
    st.submit_result(item, cand, "RESULT.json")
    g = st.turn_start(item, "grade", session="s2", server="go1", model="m")
    st.turn_end(g, "DONE", result="FINDINGS.json", cost=0.05)
    fp = write_json(os.path.join(tmp, f"f-{item}-ok.json"),
                    findings_doc(item, g, cand, ["PASS", "PASS"]))
    st.findings(item, fp)
    assert st.get_item(item)["status"] == "JUNIOR_SATISFIED"
    st.review_record(item, "plan", base, cand, "sr-1", plan_reviewer, "APPROVED")
    assert st.get_item(item)["status"] == "PREPARING"
    proof = st.proof_request(cand, base, "full")
    assert st.get_item(item)["status"] == "PREPARED"
    st.proof_record(proof, "RUNNING")
    assert st.get_item(item)["status"] == "PROOF_PENDING"
    st.proof_record(proof, "PASS", counts={"tests": 10, "failed": 0},
                    artifacts=os.path.join(tmp, "proofs", cand))
    assert st.get_item(item)["status"] == "FINAL_REVIEW"
    rid = st.review_record(item, "code", base, cand, "sr-2", code_reviewer, "APPROVED")
    assert st.get_item(item)["status"] == "APPROVED"
    return rid, proof


# --------------------------------------------------------------------------
# 1. happy path
# --------------------------------------------------------------------------

@test
def test_happy_path_ready_to_promoted():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "happy")
        st = bootstrap(root)
        assert st.get_item("I-1")["status"] == "READY"
        rid, pid = to_approved(st, tmp)
        promo = st.promote_prepare(["I-1"], "cand111", "base000", rid, pid)
        assert st.q1("SELECT status FROM promotion WHERE promo_id=?",
                     (promo,))["status"] == "INTENT"
        assert st.promote_commit(promo, "cand111", "cand111") == "COMMITTED"
        assert st.get_item("I-1")["status"] == "PROMOTED"

        # events.jsonl mirrors every event row, in the same count
        rows = st.q1("SELECT COUNT(*) c FROM event")["c"]
        with open(st.events_path, encoding="utf-8") as fh:
            lines = [json.loads(x) for x in fh if x.strip()]
        assert len(lines) == rows, f"{len(lines)} jsonl lines vs {rows} event rows"
        for rec in lines:
            assert rec["ts"].endswith("Z") and rec["ts"][-5] == "."
            assert isinstance(rec["mono"], float)
        # render + seal + audit
        files = st.render()
        assert any(f.endswith("LEDGER.md") for f in files)
        assert any(f.endswith("TIMELINE-I-1.md") for f in files)
        manifest = st.seal()
        man = json.load(open(manifest, encoding="utf-8"))
        assert "state.db-wal" not in man["files"] and "state.db-shm" not in man["files"]
        assert "state.db" in man["files"] and "events.jsonl" in man["files"]
        a = st.audit("I-1")
        ts_list = [e["ts"] for e in a["trail"]]
        assert ts_list == sorted(ts_list), "audit trail is not chronological"
        assert len(a["trail"]) > 10
        st.close()


# --------------------------------------------------------------------------
# 2. guards
# --------------------------------------------------------------------------

@test
def test_assign_refuses_on_stop_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "stopfile")
        st = bootstrap(root)
        open(os.environ["VP_STOP_FILE"], "w").close()
        refuses(st.assign, "I-1", 1)
        assert st.get_item("I-1")["status"] == "READY"
        # positive control: same call succeeds once the STOP file is gone
        os.remove(os.environ["VP_STOP_FILE"])
        assert st.assign("I-1", 1) == "ASSIGNED"
        st.close()


@test
def test_assign_refuses_when_inhibited_or_stopping():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "inhibit")
        st = bootstrap(root)
        st.run_inhibit_set("owner pause")
        refuses(st.assign, "I-1", 1)
        st.run_inhibit_clear("resume")
        assert st.assign("I-1", 1) == "ASSIGNED"   # positive control
        # STOPPING refuses too
        pid = st.packet_submit("I-2", "B.md", "P.md", "base000")
        st.packet_ready(pid)
        st.run_stop("owner stop")
        refuses(st.assign, "I-2", 2)
        st.close()


@test
def test_assign_refuses_over_wip_per_group():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "wip", roster={"wip_per_group": 2, "round_cap": 8})
        st = bootstrap(root)
        for extra in ("I-2", "I-3"):
            p = st.packet_submit(extra, "B.md", "P.md", "base000")
            st.packet_ready(p)
        st.assign("I-1", 1)
        st.assign("I-2", 1)
        e = refuses(st.assign, "I-3", 1)
        assert "cap 2" in str(e), str(e)
        assert st.get_item("I-3")["status"] == "READY"
        # v12: claiming does NOT free the slot (BUILDING is still in flight);
        # the slot frees when the item leaves the builder side (pause here).
        st.claim("I-1", "bld-1", os.path.join(tmp, "wt"), st.get_item("I-1")["rev"])
        e = refuses(st.assign, "I-3", 1)
        assert "cap 2" in str(e), str(e)
        st.item_pause("I-1", "test")
        assert st.assign("I-3", 1) == "ASSIGNED"
        assert st.item_resume("I-1") == "BUILDING"
        st.close()


@test
def test_claim_refuses_second_writer():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "claim")
        st = bootstrap(root)
        st.assign("I-1", 1)
        rev = st.get_item("I-1")["rev"]
        st.claim("I-1", "bld-1", os.path.join(tmp, "wt"), rev)
        e = refuses(st.claim, "I-1", "bld-2", os.path.join(tmp, "wt2"),
                    st.get_item("I-1")["rev"])
        assert "unique writer" in str(e)
        assert st.get_item("I-1")["builder_role"] == "bld-1"
        # a stale expected_rev is a conflict, not a refusal
        p = st.packet_submit("I-2", "B.md", "P.md", "base000")
        st.packet_ready(p)
        st.assign("I-2", 2)
        conflicts(st.claim, "I-2", "bld-2", os.path.join(tmp, "wt2"), 999)
        # positive control: the right rev claims
        assert st.claim("I-2", "bld-2", os.path.join(tmp, "wt2"),
                        st.get_item("I-2")["rev"]) == "BUILDING"
        st.close()


@test
def test_review_refuses_wrong_subject_kind():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "subject")
        st = bootstrap(root)
        st.assign("I-1", 1)
        st.claim("I-1", "bld-1", tmp, st.get_item("I-1")["rev"])
        refuses(st.review_record, "I-1", "design", "base000", "cand111",
                "sr-1", "ref-plan", "APPROVED")
        # positive control: the same call with a legal subject records
        rid = st.review_record("I-1", "plan", "base000", "cand111",
                               "sr-1", "ref-plan", "FINDINGS")
        assert rid.startswith("review-")
        st.close()


@test
def test_review_refuses_stale_candidate():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "stale")
        st = bootstrap(root)
        to_approved(st, tmp)   # item ends at APPROVED, candidate cand111
        e = refuses(st.review_record, "I-1", "code", "base000", "OLDSHA",
                    "sr-2", "ref-other", "FINDINGS")
        assert "stale candidate" in str(e)
        # positive control: the live candidate is accepted
        rid = st.review_record("I-1", "code", "base000", "cand111",
                               "sr-2", "ref-other", "FINDINGS")
        assert rid and st.get_item("I-1")["status"] == "GRADING"
        st.close()


@test
def test_review_refuses_non_independent_final():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "independence")
        st = bootstrap(root)
        st.assign("I-1", 1)
        st.claim("I-1", "bld-1", tmp, st.get_item("I-1")["rev"])
        a = st.turn_start("I-1", "build")
        st.turn_end(a, "DONE")
        st.submit_result("I-1", "cand111", "RESULT.json")
        fp = write_json(os.path.join(tmp, "f.json"),
                        findings_doc("I-1", a, "cand111", ["PASS"]))
        st.findings("I-1", fp)
        st.review_record("I-1", "plan", "base000", "cand111", "sr-1",
                         "ref-same", "APPROVED")
        p = st.proof_request("cand111", "base000", "full")
        st.proof_record(p, "PASS")
        assert st.get_item("I-1")["status"] == "FINAL_REVIEW"
        e = refuses(st.review_record, "I-1", "code", "base000", "cand111",
                    "sr-1", "ref-same", "APPROVED")
        assert "independence" in str(e)
        assert st.get_item("I-1")["status"] == "FINAL_REVIEW"
        # positive control: a different reviewer_ref approves
        rid = st.review_record("I-1", "code", "base000", "cand111", "sr-2",
                               "ref-other", "APPROVED")
        assert st.q1("SELECT independent FROM review WHERE review_id=?",
                     (rid,))["independent"] == 1
        assert st.get_item("I-1")["status"] == "APPROVED"
        st.close()


@test
def test_promote_prepare_refuses_and_records_refused_row():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "promote")
        st = bootstrap(root)
        rid, pid = to_approved(st, tmp)

        # (a) proof not PASS
        bad_proof = st.proof_request("cand111", "base000", "targeted")
        st.proof_record(bad_proof, "FAIL_PRODUCT", counts={"failed": 3})
        e = refuses(st.promote_prepare, ["I-1"], "cand111", "base000", rid, bad_proof)
        assert "proof status is FAIL_PRODUCT" in str(e)

        # (b) review candidate != union
        e = refuses(st.promote_prepare, ["I-1"], "OTHERSHA", "base000", rid, pid)
        assert "!= union" in str(e)

        # (c) run inhibited
        st.run_inhibit_set("pause")
        refuses(st.promote_prepare, ["I-1"], "cand111", "base000", rid, pid)
        st.run_inhibit_clear()

        # (d) an item that is not APPROVED
        p2 = st.packet_submit("I-9", "B.md", "P.md", "base000")
        st.packet_ready(p2)
        e = refuses(st.promote_prepare, ["I-1", "I-9"], "cand111", "base000", rid, pid)
        assert "not APPROVED" in str(e)

        refused = st.q("SELECT * FROM promotion WHERE status='REFUSED' ORDER BY ts")
        assert len(refused) == 4, f"expected 4 REFUSED rows, got {len(refused)}"
        assert all(r["reason"] for r in refused), "a REFUSED row has no reason"

        # positive control: every condition satisfied -> INTENT
        promo = st.promote_prepare(["I-1"], "cand111", "base000", rid, pid)
        assert st.q1("SELECT status FROM promotion WHERE promo_id=?",
                     (promo,))["status"] == "INTENT"
        st.close()


@test
def test_matrix_refuses_pairs_and_records_violation():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "matrix")
        st = bootstrap(root)
        # builder -> senior is not in the matrix
        refuses(st.reserve_delivery, "bld-1", "sr-1", "RESULT", "I-1")
        assert st.q1("SELECT COUNT(*) c FROM violation")["c"] == 1
        body = os.path.join(tmp, "body.txt")
        with open(body, "w", encoding="utf-8") as fh:
            fh.write("hello")
        refuses(st.msg_log, "send", "bld-1", "sr-1", "NOTE", "I-1", body)
        assert st.q1("SELECT COUNT(*) c FROM violation")["c"] == 2
        with open(st.violations_path, encoding="utf-8") as fh:
            assert len([x for x in fh if x.strip()]) == 2
        # positive control: senior -> junior and builder -> daemon are allowed
        did = st.reserve_delivery("sr-1", "jr-1", "PACKET", "I-1")
        assert st.delivery_status(did, "sent") == "SENT"
        assert st.delivery_status(did, "ack") == "ACK"
        st.role_add("dmn-1", "daemon")
        res = st.msg_log("send", "bld-1", "dmn-1", "RESULT", "I-1", body,
                         session_ref="sess-b1")
        assert res["envelope"].startswith("VP-ENVELOPE run=run-test")
        assert st.q1("SELECT COUNT(*) c FROM violation")["c"] == 2
        # a sender id that is not that role's session is a violation too
        refuses(st.msg_log, "send", "bld-1", "dmn-1", "RESULT", "I-1", body,
                session_ref="sess-IMPOSTOR")
        assert st.q1("SELECT COUNT(*) c FROM violation")["c"] == 3
        # STOP from owner is accepted on any channel
        st.msg_log("send", "owner", "cos-1", "STOP", "-", body)
        st.close()


@test
def test_findings_all_pass_and_round_cap():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "roundcap", roster={"round_cap": 2, "wip_per_group": 2})
        st = bootstrap(root)
        st.assign("I-1", 1)
        st.claim("I-1", "bld-1", tmp, st.get_item("I-1")["rev"])
        for n in range(2):
            a = st.turn_start("I-1", "build")
            st.turn_end(a, "DONE")
            st.submit_result("I-1", f"cand{n}", "RESULT.json")
            fp = write_json(os.path.join(tmp, f"f{n}.json"),
                            # all_pass is computed from the lines, not trusted:
                            {**findings_doc("I-1", a, f"cand{n}", ["PASS", "FAIL"]),
                             "all_pass": True})
            res = st.findings("I-1", fp)
            assert res["all_pass"] is False, "all_pass must come from the lines"
        assert st.get_item("I-1")["status"] == "BLOCKED"
        caps = st.q("SELECT * FROM event WHERE kind='ROUND_CAP'")
        assert len(caps) == 1, f"expected one ROUND_CAP event, got {len(caps)}"
        with open(st.escalations_path, encoding="utf-8") as fh:
            kinds = [json.loads(x)["kind"] for x in fh if x.strip()]
        assert "ROUND_CAP" in kinds, kinds
        # positive control: an all-PASS findings file satisfies the junior
        root2 = new_root(tmp, "roundcap-ok", roster={"round_cap": 2})
        st2 = bootstrap(root2)
        st2.assign("I-1", 1)
        st2.claim("I-1", "bld-1", tmp, st2.get_item("I-1")["rev"])
        a = st2.turn_start("I-1", "build")
        st2.turn_end(a, "DONE")
        st2.submit_result("I-1", "candX", "RESULT.json")
        fp = write_json(os.path.join(tmp, "fok.json"),
                        findings_doc("I-1", a, "candX", ["PASS", "PASS"]))
        assert st2.findings("I-1", fp)["all_pass"] is True
        assert st2.get_item("I-1")["status"] == "JUNIOR_SATISFIED"
        st.close()
        st2.close()


@test
def test_illegal_state_transition_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "machine")
        st = bootstrap(root)
        # READY -> GRADING is not a transition in the machine
        refuses(st.submit_result, "I-1", "cand111", "RESULT.json")
        st.assign("I-1", 1)
        st.claim("I-1", "bld-1", tmp, st.get_item("I-1")["rev"])
        assert st.submit_result("I-1", "cand111", "RESULT.json") == "GRADING"
        st.close()


# --------------------------------------------------------------------------
# 3. idempotency
# --------------------------------------------------------------------------

@test
def test_duplicate_event_application_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "idem")
        st = bootstrap(root)
        st.assign("I-1", 1)
        st.claim("I-1", "bld-1", tmp, st.get_item("I-1")["rev"])
        a = st.turn_start("I-1", "build")
        st.turn_end(a, "DONE")
        st.submit_result("I-1", "cand111", "RESULT.json")
        fp = write_json(os.path.join(tmp, "f.json"),
                        findings_doc("I-1", a, "cand111", ["PASS"]))
        before_rows = st.q1("SELECT COUNT(*) c FROM event WHERE kind='FINDINGS'")["c"]
        st.findings("I-1", fp)
        after_rows = st.q1("SELECT COUNT(*) c FROM event WHERE kind='FINDINGS'")["c"]
        assert after_rows == before_rows + 1
        with open(st.events_path, encoding="utf-8") as fh:
            lines1 = len([x for x in fh if x.strip()])
        # re-applying the same (item, attempt, kind, ref) writes nothing new
        with st.tx():
            seq_a, created_a = st.event("FINDINGS", item="I-1", attempt=a,
                                        ref="cand111", detail={"x": 1},
                                        idempotent=True)
        assert created_a is False
        with st.tx():
            seq_b, created_b = st.event("FINDINGS", item="I-1", attempt=a,
                                        ref="cand111", detail={"x": 2},
                                        idempotent=True)
        assert created_b is False and seq_a == seq_b
        assert st.q1("SELECT COUNT(*) c FROM event WHERE kind='FINDINGS'")["c"] == after_rows
        with open(st.events_path, encoding="utf-8") as fh:
            assert len([x for x in fh if x.strip()]) == lines1
        # positive control: a different ref is a new event
        with st.tx():
            _, created_c = st.event("FINDINGS", item="I-1", attempt=a,
                                    ref="cand222", idempotent=True)
        assert created_c is True
        assert st.q1("SELECT COUNT(*) c FROM event WHERE kind='FINDINGS'")["c"] == \
            after_rows + 1
        st.close()


# --------------------------------------------------------------------------
# 4. concurrent writers (two+ processes)
# --------------------------------------------------------------------------

def _writer(root, tag, n):
    """Child process: append n events through its own Store connection."""
    st = vpstore.Store(root)
    try:
        for i in range(n):
            with st.tx():
                st.event("CHECKPOINT", role=tag, ref=f"{tag}-{i}",
                         detail={"i": i})
    finally:
        st.close()


@test
def test_concurrent_writers_lose_no_event():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "concurrent")
        st = bootstrap(root)
        base = st.q1("SELECT COUNT(*) c FROM event")["c"]
        st.close()
        per, nproc = 40, 3
        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=_writer, args=(root, f"w{k}", per))
                 for k in range(nproc)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(120)
        assert all(p.exitcode == 0 for p in procs), \
            f"child exit codes {[p.exitcode for p in procs]}"
        st = vpstore.Store(root)
        rows = st.q1("SELECT COUNT(*) c FROM event WHERE kind='CHECKPOINT'")["c"]
        assert rows == per * nproc, f"{rows} event rows vs {per * nproc} attempted"
        total = st.q1("SELECT COUNT(*) c FROM event")["c"]
        assert total == base + per * nproc
        with open(st.events_path, encoding="utf-8") as fh:
            recs = [json.loads(x) for x in fh if x.strip()]
        assert len(recs) == total, f"{len(recs)} jsonl lines vs {total} rows"
        seqs = [r["seq"] for r in recs]
        assert len(set(seqs)) == len(seqs), "duplicate seq in events.jsonl"
        refs = {r["ref"] for r in recs if r["kind"] == "CHECKPOINT"}
        assert len(refs) == per * nproc, "a concurrent event was lost"
        st.close()


# --------------------------------------------------------------------------
# 5. crash between promotion INTENT and COMMITTED
# --------------------------------------------------------------------------

def _crash_after_intent(root, tmp, rid, pid):
    """Child: record the INTENT, then die before the commit step."""
    st = vpstore.Store(root)
    st.promote_prepare(["I-1"], "cand111", "base000", rid, pid)
    os._exit(9)          # hard crash: no COMMITTED, no cleanup


@test
def test_crash_between_intent_and_committed_is_reconcilable():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "crash")
        st = bootstrap(root)
        rid, pid = to_approved(st, tmp)
        st.close()
        ctx = mp.get_context("spawn")
        p = ctx.Process(target=_crash_after_intent, args=(root, tmp, rid, pid))
        p.start()
        p.join(120)
        assert p.exitcode == 9, f"child exitcode {p.exitcode}"

        st = vpstore.Store(root)
        rows = st.q("SELECT * FROM promotion WHERE status='INTENT'")
        assert len(rows) == 1, f"expected one surviving INTENT row, got {len(rows)}"
        promo = rows[0]["promo_id"]
        assert promo in [r["promo_id"] for r in st.reconcilable_promotions()]
        assert st.get_item("I-1")["status"] == "APPROVED", "item must not be PROMOTED"
        # the recorded intent survived in the event mirror
        with open(st.events_path, encoding="utf-8") as fh:
            kinds = [json.loads(x)["kind"] for x in fh if x.strip()]
        assert "PROMOTE_INTENT" in kinds and "PROMOTE_COMMITTED" not in kinds
        # reconciling: the ref the Integrator actually left differs -> RECONCILE
        assert st.promote_commit(promo, "cand111", "SOMETHINGELSE") == "RECONCILE"
        row = st.q1("SELECT * FROM promotion WHERE promo_id=?", (promo,))
        assert row["status"] == "RECONCILE" and row["actual_ref"] == "SOMETHINGELSE"
        assert st.get_item("I-1")["status"] == "APPROVED"
        # positive control: a matching ref commits
        promo2 = st.promote_prepare(["I-1"], "cand111", "base000", rid, pid)
        assert st.promote_commit(promo2, "cand111", "cand111") == "COMMITTED"
        assert st.get_item("I-1")["status"] == "PROMOTED"
        st.close()


# --------------------------------------------------------------------------
# 6. vpctl exit codes
# --------------------------------------------------------------------------

@test
def test_vpctl_exit_codes_and_json():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "cli")
        env = dict(os.environ, VP_RUN_ROOT=root,
                   VP_STOP_FILE=os.path.join(root, "STOP"))

        def run(*a):
            return subprocess.run([sys.executable, os.path.join(VP, "vpctl.py"), *a],
                                  capture_output=True, text=True, env=env, cwd=VP)

        assert run("run", "init", "run-cli").returncode == 0
        assert run("run", "init", "run-cli").returncode == 4      # conflict
        assert run("role", "add", "bld-1", "--kind", "builder").returncode == 0
        assert run("role", "add", "bld-1", "--kind", "nope").returncode == 2  # usage
        r = run("packet", "submit", "I-1", "--benchmark", "B.md",
                "--packet", "P.md", "--base", "base000")
        assert r.returncode == 0
        packet_id = r.stdout.strip()
        assert run("packet", "ready", packet_id).returncode == 0
        open(env["VP_STOP_FILE"], "w").close()
        assert run("assign", "I-1", "--group", "1").returncode == 3   # refused
        os.remove(env["VP_STOP_FILE"])
        assert run("assign", "I-1", "--group", "1").returncode == 0
        assert run("claim", "I-1", "--role", "bld-1", "--worktree", tmp,
                   "--expected-rev", "999").returncode == 4          # conflict
        assert run("bogus-command").returncode == 2
        r = run("report", "items", "--json")
        assert r.returncode == 0 and json.loads(r.stdout)["items"][0]["item"] == "I-1"
        r = run("audit", "I-1", "--json")
        assert r.returncode == 0 and json.loads(r.stdout)["item"] == "I-1"
        assert run("render").returncode == 0
        assert run("seal").returncode == 0
        assert os.path.exists(os.path.join(root, "LEDGER.md"))


@test
def test_event_file_failure_rolls_back_the_db_row():
    """The jsonl mirror is inside the transaction boundary: if the file write
    fails the DB row must not survive."""
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "mirror")
        st = bootstrap(root)
        before = st.q1("SELECT COUNT(*) c FROM event")["c"]
        os.remove(st.events_path)
        os.mkdir(st.events_path)          # append will now fail
        try:
            st.run_checkpoint("cos-1", "this must not survive")
        except OSError:
            pass
        else:
            raise AssertionError("expected the file failure to raise")
        assert st.q1("SELECT COUNT(*) c FROM event")["c"] == before, \
            "event row survived a failed mirror write"
        os.rmdir(st.events_path)
        # positive control: the same call succeeds once the file is writable
        st.run_checkpoint("cos-1", "this one lands")
        assert st.q1("SELECT COUNT(*) c FROM event")["c"] == before + 1
        st.close()


# --------------------------------------------------------------------------
# 7. packet join on report items, packet show, escalate
# --------------------------------------------------------------------------

@test
def test_report_items_joins_newest_ready_packet():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "packetjoin")
        st = bootstrap(root)          # I-1 has one READY packet
        # a second item whose packet is only DRAFT, and a third with no packet
        draft = st.packet_submit("I-2", "B2.md", "P2.md", "base222")
        for it, base in (("I-2", "base222"), ("I-3", "base333")):
            st.ex("INSERT INTO item(item,rev,status,round,base_sha,ts) "
                  "VALUES(?,1,'READY',0,?,?)", (it, base, vpstore.now_ts()))
        # a newer READY packet for I-1 must win over the older one
        p2 = st.packet_submit("I-1", "B-v2.md", "P-v2.md", "base000",
                              critical=True, allowed_files=["a.py", "b/c.py"])
        st.packet_ready(p2)
        rows = {r["item"]: r for r in st.report("items")["items"]}
        one = rows["I-1"]
        assert one["packet_path"] == "P-v2.md", one["packet_path"]
        assert one["benchmark_path"] == "B-v2.md"
        assert one["packet_rev"] == 1
        assert one["critical"] is True
        assert one["allowed_files"] == ["a.py", "b/c.py"]
        # DRAFT and missing packets join as null
        for item in ("I-2", "I-3"):
            r = rows[item]
            assert r["packet_path"] is None and r["benchmark_path"] is None
            assert r["packet_rev"] is None and r["critical"] is None
            assert r["allowed_files"] is None, r["allowed_files"]
        # positive control: readying the draft fills the join in
        st.packet_ready(draft)
        r = {x["item"]: x for x in st.report("items")["items"]}["I-2"]
        assert r["packet_path"] == "P2.md" and r["critical"] is False
        assert r["allowed_files"] == []
        st.close()


@test
def test_packet_show_returns_newest_ready_packet():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "packetshow")
        st = bootstrap(root)
        row = st.packet_show("I-1")
        assert row["item"] == "I-1" and row["status"] == "READY"
        assert row["packet_path"] == "P.md" and row["benchmark_path"] == "B.md"
        assert row["allowed_files"] == [] and row["critical"] is False
        # an item with only a DRAFT packet is refused
        st.packet_submit("I-2", "B2.md", "P2.md", "base222")
        st.ex("INSERT INTO item(item,rev,status,round,base_sha,ts) "
              "VALUES('I-2',1,'READY',0,'base222',?)", (vpstore.now_ts(),))
        refuses(st.packet_show, "I-2")
        refuses(st.packet_show, "I-nope")
        # positive control
        pid = st.q1("SELECT packet_id FROM packet WHERE item='I-2'")["packet_id"]
        st.packet_ready(pid)
        assert st.packet_show("I-2")["packet_path"] == "P2.md"
        st.close()


@test
def test_escalate_writes_event_and_mirrors_to_escalations():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "escalate")
        st = bootstrap(root)
        st.assign("I-1", 1)
        st.claim("I-1", "bld-1", tmp, st.get_item("I-1")["rev"])
        a = st.turn_start("I-1", "build")
        with open(st.escalations_path, "a", encoding="utf-8"):
            pass
        with open(st.escalations_path, encoding="utf-8") as fh:
            before = len([x for x in fh if x.strip()])
        kinds = ["STUCK", "STALLED", "QUOTA", "AUTH", "INCOMPLETE", "BLOCKED",
                 "ROUND_CAP", "DELIVERY_INCIDENT", "INHIBIT"]
        for k in kinds:
            seq = st.escalate(k, "I-1", attempt=a, detail=f"{k} from the driver")
            assert isinstance(seq, int)
        with open(st.escalations_path, encoding="utf-8") as fh:
            recs = [json.loads(x) for x in fh if x.strip()]
        got = [r["kind"] for r in recs[before:]]
        assert got == kinds, got
        for r in recs[before:]:
            assert r["item"] == "I-1" and r["attempt"] == a
            assert r["detail"]["detail"].endswith("from the driver")
            assert r["ts"].endswith("Z") and isinstance(r["mono"], float)
        # every escalation is also a plain event row and an events.jsonl line
        for k in kinds:
            assert st.q1("SELECT COUNT(*) c FROM event WHERE kind=? AND item='I-1'",
                         (k,))["c"] >= 1
        with open(st.events_path, encoding="utf-8") as fh:
            ev_kinds = [json.loads(x)["kind"] for x in fh if x.strip()]
        assert all(k in ev_kinds for k in kinds)
        # refusals: a kind outside the list, an unknown item, an unknown attempt
        try:
            st.escalate("PANIC", "I-1", detail="nope")
        except Usage:
            pass
        else:
            raise AssertionError("expected Usage for an unknown escalation kind")
        refuses(st.escalate, "STUCK", "I-nope", None, "nope")
        refuses(st.escalate, "STUCK", "I-1", "attempt-99999", "nope")
        # positive control: attempt omitted is fine
        assert isinstance(st.escalate("STUCK", "I-1", None, "no attempt"), int)
        st.close()


@test
def test_vpctl_packet_show_and_escalate():
    with tempfile.TemporaryDirectory() as tmp:
        root = new_root(tmp, "cli2")
        env = dict(os.environ, VP_RUN_ROOT=root,
                   VP_STOP_FILE=os.path.join(root, "STOP"))

        def run(*a):
            return subprocess.run([sys.executable, os.path.join(VP, "vpctl.py"), *a],
                                  capture_output=True, text=True, env=env, cwd=VP)

        assert run("run", "init", "run-cli2").returncode == 0
        r = run("packet", "submit", "I-1", "--benchmark", "B.md", "--packet", "P.md",
                "--base", "base000", "--critical", "--allowed-files", "a.py,b.py")
        pid = r.stdout.strip()
        assert run("packet", "show", "I-1", "--json").returncode == 3   # DRAFT only
        assert run("packet", "ready", pid).returncode == 0
        r = run("packet", "show", "I-1", "--json")
        assert r.returncode == 0
        row = json.loads(r.stdout)
        assert row["allowed_files"] == ["a.py", "b.py"] and row["critical"] is True
        r = run("report", "items", "--json")
        item = json.loads(r.stdout)["items"][0]
        for key in ("packet_path", "benchmark_path", "packet_rev", "critical",
                    "allowed_files"):
            assert key in item, key
        assert item["packet_path"] == "P.md"
        r = run("escalate", "--kind", "QUOTA", "--item", "I-1",
                "--detail", "go1 429 until 12:30 IST")
        assert r.returncode == 0, r.stderr
        assert run("escalate", "--kind", "NOPE", "--item", "I-1",
                   "--detail", "x").returncode == 2
        assert run("escalate", "--kind", "STUCK", "--item", "I-nope",
                   "--detail", "x").returncode == 3
        with open(os.path.join(root, "escalations.jsonl"), encoding="utf-8") as fh:
            recs = [json.loads(x) for x in fh if x.strip()]
        assert [r["kind"] for r in recs] == ["QUOTA"], [r["kind"] for r in recs]


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    passed = 0
    for fn in TESTS:
        try:
            fn()
            passed += 1
            print(f"PASS {fn.__name__}")
        except Exception:
            FAILURES.append((fn.__name__, traceback.format_exc()))
            print(f"FAIL {fn.__name__}")
    print(f"\nvpstore: {passed}/{len(TESTS)} passed")
    for name, tb in FAILURES:
        print(f"\n--- {name} ---\n{tb}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
