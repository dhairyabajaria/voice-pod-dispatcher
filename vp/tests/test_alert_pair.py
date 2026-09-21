#!/usr/bin/env python3
"""tests/test_alert_pair.py -- OWNER-ALERTS.md and alerts.jsonl must agree.

D156. The defect this guards was found by accident: appending a retraction to
OWNER-ALERTS.md by hand, I checked my own append had landed and noticed the two
files had different line counts. 1171 alert lines in the .md against 1163
records in alerts.jsonl, the 8 orphans all BOX_LOCK_REAPED, going back three
days. Nothing was lost -- they are in events.jsonl -- but anything treating
alerts.jsonl as the index of OWNER-ALERTS.md was silently short, and would have
stayed short for every future store-originated alert.

The cause is that there is no single alert writer. Four files touch
OWNER-ALERTS.md and only one of them used to write the jsonl twin. So the
valuable half of this guard is not "vpstore.alert writes two files" -- it is
PINNING THE POPULATION, so a fifth writer cannot be added without someone
deciding what its machine-readable record looks like.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

VP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VP))

import vpstore  # noqa: E402

# Every dispatcher/vp file that may append to OWNER-ALERTS.md, and what each one
# writes. `alert_lines` = it emits "- <ts> **KIND** ..." rows, which MUST have an
# alerts.jsonl twin. `header_only` = it writes the file's "# OWNER-ALERTS <id>"
# banner, which is not an alert and needs no record.
WRITERS = {
    "lanedriver.py": "alert_lines",
    "vpstore.py": "alert_lines",
    "vpdriver.py": "alert_lines",
    "vpctl.py": "header_only",
}


def _writers_on_disk():
    return {p.name for p in VP.glob("*.py")
            if "OWNER-ALERTS.md" in p.read_text(encoding="utf-8")}


def test_the_set_of_owner_alert_writers_is_pinned():
    """THE load-bearing control. A new writer is exactly how the 8 orphans
    happened, and a behavioural test of the writers we know about cannot see
    one. If this fails, do not just add the name -- decide whether the new
    writer emits alert lines, and if it does, give it an alerts.jsonl record."""
    found = _writers_on_disk()
    known = set(WRITERS)
    assert found == known, (
        "the set of files touching OWNER-ALERTS.md changed: new=%s gone=%s. Every "
        "file that appends an alert LINE must also append its alerts.jsonl twin, "
        "or that alert kind becomes invisible to anything reading alerts.jsonl."
        % (sorted(found - known), sorted(known - found)))


def test_every_alert_line_writer_also_names_alerts_jsonl():
    """Each alert-line writer must mention the jsonl path in its own source.
    Coarse, deliberately: it cannot prove the write happens on every path, but
    it fails loudly the moment a writer emits alert rows and never mentions the
    twin -- which is the exact shape of the bug."""
    checked = 0
    for name, role in sorted(WRITERS.items()):
        if role != "alert_lines":
            continue
        src = (VP / name).read_text(encoding="utf-8")
        # Strip comments and docstrings first. My own D156 comment in vpdriver.py
        # contains the string "alerts.jsonl" twice, so a plain grep passed even
        # when the write itself was mutated to a different file -- the comment
        # answered the grep. Only CODE counts.
        code = re.sub(r"#.*", "", src)
        code = re.sub(r'"""(?:.|\n)*?"""', "", code)
        assert re.search(r'["\']alerts\.jsonl["\']|alerts_path|alerts_jsonl', code), (
            "%s appends alert lines to OWNER-ALERTS.md but its CODE never names "
            "alerts.jsonl -- its alerts would have no machine-readable twin" % name)
        checked += 1
    assert checked == 3, (
        "this loop is driven by WRITERS; if a role is retyped to header_only the "
        "check silently stops covering that file (checked=%d)" % checked)


def test_the_store_down_fallback_still_writes_both(tmp_path):
    """The degraded path, tested behaviourally rather than by grep.

    `alert_store_down` runs exactly when the store could not record the alert,
    so it is the one alert that cannot rely on vpstore writing the twin. It
    appends both files itself. A source grep could not see this correctly --
    see the comment above.
    """
    import vpdriver

    class _DeadStore(object):
        def alert(self, *a, **kw):
            return None                      # the store refused it

    drv = vpdriver.Driver.__new__(vpdriver.Driver)
    drv.run_root = tmp_path
    drv.store = _DeadStore()
    drv._alerted = set()
    drv.log = lambda *a, **kw: None
    drv.alert_store_down("k1", "postgres is not answering")

    md = (tmp_path / "OWNER-ALERTS.md").read_text(encoding="utf-8")
    js = [json.loads(l) for l in (tmp_path / "alerts.jsonl").read_text(encoding="utf-8").splitlines()
          if l.strip()]
    assert "STORE_UNAVAILABLE" in md and "postgres is not answering" in md
    assert len(js) == 1 and js[0]["kind"] == "STORE_UNAVAILABLE"
    assert js[0]["source"] == "vpdriver-fallback"
    ts = re.match(r"- (\S+)", md.strip().splitlines()[-1]).group(1)
    assert js[0]["ts"] == ts, "the pair must share a timestamp"


def test_vpstore_alert_writes_both_files_with_the_same_timestamp(tmp_path):
    """The behavioural half, for the writer that actually had the bug."""
    st = vpstore.open_store(str(tmp_path))
    try:
        ts = st.alert("BOX_LOCK_REAPED", "box.lock.d reaped for proof-X: owner gone", item=None)
    finally:
        st.close()

    md = (tmp_path / "OWNER-ALERTS.md").read_text(encoding="utf-8")
    assert "BOX_LOCK_REAPED" in md and ts in md

    lines = [l for l in (tmp_path / "alerts.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1, "exactly one record per alert: %r" % lines
    rec = json.loads(lines[0])
    assert rec["ts"] == ts, "the pair must share a timestamp or they cannot be joined"
    assert rec["kind"] == "BOX_LOCK_REAPED"
    assert rec["source"] == "vpstore"
    assert rec["severity"] == "unknown", (
        "vpstore does not run vpalerts.severity; guessing here would make the two "
        "writers disagree about the field that decides whether the owner is paged")


def test_the_two_files_stay_in_step_over_many_alerts(tmp_path):
    """Counts must match, not just 'both files exist'. A writer that appends the
    .md twice and the record once would pass a presence check forever."""
    st = vpstore.open_store(str(tmp_path))
    try:
        for i in range(7):
            st.alert("BOX_LOCK_REAPED", "reap %d" % i, item="T%d" % i)
    finally:
        st.close()
    md = [l for l in (tmp_path / "OWNER-ALERTS.md").read_text(encoding="utf-8").splitlines()
          if l.startswith("- ")]
    js = [json.loads(l) for l in (tmp_path / "alerts.jsonl").read_text(encoding="utf-8").splitlines()
          if l.strip()]
    assert len(md) == len(js) == 7, (len(md), len(js))
    assert [re.match(r"- (\S+)", l).group(1) for l in md] == [r["ts"] for r in js], \
        "same alerts, same order, same timestamps"
    assert [r["task"] for r in js] == ["T%d" % i for i in range(7)]


def test_a_failed_transaction_leaves_neither_line(tmp_path):
    """The inverted bug. Appending the jsonl eagerly would leave a record with no
    .md line when the surrounding transaction rolls back -- the same divergence,
    pointing the other way. Both lines are queued on _pending_lines and flush
    together."""
    st = vpstore.open_store(str(tmp_path))
    try:
        try:
            with st.tx():
                st.alert("BOX_LOCK_REAPED", "this transaction is doomed")
                raise RuntimeError("boom")
        except RuntimeError:
            pass
    finally:
        st.close()
    for f in ("OWNER-ALERTS.md", "alerts.jsonl"):
        p = tmp_path / f
        body = p.read_text(encoding="utf-8") if p.exists() else ""
        assert "doomed" not in body, "%s kept a line from a rolled-back transaction" % f
