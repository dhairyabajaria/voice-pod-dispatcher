#!/usr/bin/env python3
"""vpjourney.py -- the run's ledger derived from its event stream, not from notes.

Sources (all already written by the scheduler and the driver; nothing here
writes anything but its own report):

  orchestration-state/events/<seq>-<id>.json   the scheduler's own events,
        one file per state change, machine timestamps, gapless sequence --
        the authoritative record of what happened to every task
  orchestration-state/run-state.json           the scheduler's current view
  <run_root>/packets/<TASK>.json               driver dispatch records: which
        packet a row came from and, for a retry row, `retry_of`
  <packets_dir>/*/PACKET.md (+ synthesized twins)  the Architect's work items
  <catalog>                                    the plan's fixed catalog rows
  <run_root>/costs.jsonl                       one line per runner turn:
        runner / server / model / role / round / status / cost
  <run_root>/turns/<task>/<attempt>/*-record.json   per-turn effort/variant
  <run_root>/proofs/*.json                     proof outcomes (route, status)
  <run_root>/control.jsonl                     driver->scheduler calls with
        seq_before/seq_after (huge: read incrementally, never whole)
  <run_root>/LEDGER.md                         the driver's rendered table

Model:
  ROOT WORK ITEM  a catalog row, or a packet followed up its retry_of chain
                  and, for a hosted twin, up to the packet it proves; review
                  and repair rows attach to the root of their parent contract.
  ATTEMPT         one scheduler row (task id). Retries and review rounds are
                  attempts of the same root, never new work items.
  JOURNEY         a root's attempts in order, each with its scheduler events
                  and runner turns interleaved by time.

`verify` compares the derived picture with run-state.json, LEDGER.md and the
control.jsonl call spans and prints every divergence with the event that
proves it. Stdlib only.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import vppack  # noqa: E402
except Exception:  # pragma: no cover - vppack is a sibling module; tests run with it
    vppack = None

TERMINAL_EVENT_STATE = {
    "TASK_RETIRED": "CANCELLED",
    "TASK_PROMOTED_TO_INTEGRATED": "INTEGRATED",
    "TASK_BLOCKED": "BLOCKED",
}
RUNNING_EVENTS = {"TASK_CLAIMED", "TASK_STARTED"}
STEP_EVENTS = (
    "TEMPLATE_TASK_INSTANTIATED", "TASK_CLAIMED", "TASK_STARTED", "TASK_COMPLETED",
    "TASK_RETIRED", "TASK_PROMOTED_TO_INTEGRATED", "TASK_BLOCKED", "TASK_UNBLOCKED",
    "TASK_INVALIDATED", "TASK_DEPENDENCIES_REWIRED", "PRESERVED_RESULT_ADOPTED",
)
DONE_STATES = {"VERIFIED", "INTEGRATED", "ACCEPTED", "VERIFIED_LOCAL"}
OWED_STATES = {"REPAIR_REQUIRED", "INVALID_EVIDENCE", "BLOCKED"}
CLOSED_STATES = DONE_STATES | {"CANCELLED"}
# when a root has several rows still open, the one that needs a human first wins
OPEN_PRIORITY = ["RUNNING", "CLAIMED", "REPAIR_REQUIRED", "INVALID_EVIDENCE", "BLOCKED",
                 "READY", "WAITING_DEPENDENCY", "PENDING", "HELD"]


def utc_now():
    return datetime.now(timezone.utc)


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ts.microsecond // 1000)


def parse_ts(s):
    if not s:
        return None
    s = str(s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def short(sha):
    return (sha or "")[:10]


def proof_scope_label(p):
    """D149: what a proof RAN, for display beside its verdict.

    `only` empty/absent means the whole suite; anything else is that path list and
    nothing outside it.  Only a PASS gets a scope at all -- a FAIL or a HELD record
    ran something, but it is not evidence FOR anything, and labelling it `full`
    would read as a full-suite guarantee (the D146 default-to-strongest mistake).

    Deliberately a plain function over one record: the driver's own
    `LaneDriver.member_scope` picks the newest PASS across records and consults
    hosted twins, which needs the run root.  The board shows a record; the ledger
    shows a row.  Same vocabulary, different questions -- do not merge them.
    """
    if (p or {}).get("status") != "PASS":
        return "-"
    only = (p or {}).get("only")
    if not only:
        return "full suite"
    only = str(only).replace("|", "/")
    return "scoped: %s" % (only[:60] + ("..." if len(only) > 60 else ""))


# --------------------------------------------------------------------------
# incremental readers
# --------------------------------------------------------------------------

class TailReader(object):
    """Read a JSONL file another process is appending to: resume from the
    last byte consumed, keep a torn last line until its newline arrives,
    start over if the file shrank (rotated/truncated). Bad lines are
    counted, not raised."""

    def __init__(self, path, keep=None):
        self.path = Path(path)
        self.offset = 0
        self.buf = b""
        self.bad = 0
        self.keep = keep          # optional f(dict) -> dict to shrink what is kept

    CHUNK = 8 << 20

    def read_new(self, max_bytes=None):
        """Consume what was appended since the last call, in bounded chunks so
        a 250 MB control.jsonl never sits in memory. `max_bytes` caps one
        call (the rest is picked up next time)."""
        out = []
        try:
            size = self.path.stat().st_size
        except OSError:
            return out
        if size < self.offset:                 # truncated or rotated: start over
            self.offset, self.buf = 0, b""
        if size == self.offset:
            return out
        stop = size if max_bytes is None else min(size, self.offset + max_bytes)
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            while self.offset < stop:
                chunk = fh.read(min(self.CHUNK, stop - self.offset))
                if not chunk:
                    break
                self.offset += len(chunk)
                data = self.buf + chunk
                lines = data.split(b"\n")
                self.buf = lines.pop()         # b"" when the chunk ended in a newline
                for raw in lines:
                    obj = self._parse(raw)
                    if obj is not None:
                        out.append(obj)
        return out

    def _parse(self, raw):
        raw = raw.strip()
        if not raw:
            return None
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            self.bad += 1
            return None
        if not isinstance(obj, dict):
            self.bad += 1
            return None
        return self.keep(obj) if self.keep else obj


class EventStore(object):
    """orchestration-state/events/*.json, loaded once per file."""

    def __init__(self, events_dir):
        self.dir = Path(events_dir)
        self.by_seq = {}
        self.seen = set()
        self.bad = []

    def refresh(self):
        try:
            names = os.listdir(self.dir)
        except OSError:
            return 0
        n = 0
        for name in names:
            if not name.endswith(".json") or name in self.seen:
                continue
            self.seen.add(name)
            try:
                with open(self.dir / name, encoding="utf-8") as fh:
                    ev = json.load(fh)
                seq = int(ev.get("sequence"))
            except (OSError, ValueError, TypeError):
                self.bad.append(name)
                continue
            ev["_file"] = name
            self.by_seq[seq] = ev
            n += 1
        return n

    def ordered(self):
        return [self.by_seq[k] for k in sorted(self.by_seq)]

    def gaps(self):
        seqs = sorted(self.by_seq)
        if not seqs:
            return []
        return [s for s in range(seqs[0], seqs[-1] + 1) if s not in self.by_seq]


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

class Journey(object):
    def __init__(self, run_root):
        self.run_root = Path(run_root)
        self.roster_path = self.run_root / "roster.json"
        self.roster = self._load_json(self.roster_path) or {}
        run = self.roster.get("run") or {}
        self.state_path = Path(run.get("run_state") or (self.run_root / "run-state.json"))
        self.events = EventStore(self.state_path.parent / "events")
        self.pack_dir = Path(run.get("pack_dir") or run.get("packets_dir") or "")
        self.catalog_path = Path(run.get("catalog") or "")
        self.costs = TailReader(self.run_root / "costs.jsonl")
        self.control = TailReader(self.run_root / "control.jsonl", keep=self._control_keep)
        self.pipelines = TailReader(self.run_root / "proofs" / "circleci-pipelines.jsonl")
        self.alerts = TailReader(self.run_root / "alerts.jsonl")
        self.turns = []            # costs.jsonl lines
        self.calls = []            # slimmed control.jsonl lines
        self.pipeline_rows = []
        self.alert_rows = []
        self.state = {}
        self._state_mtime = None
        self.records = {}          # task -> packets/<task>.json
        self._records_mtime = None
        self.pack = {}             # packet id -> packet (vppack)
        self._pack_loaded = 0.0
        self.pack_lint = []
        self.proofs = {}           # proof_id -> proof json
        self.pass_by_task = {}     # D150: task -> newest PASSing proof (rebuilt each refresh)
        self._proof_seen = set()
        self.catalog_ids = []
        self.errors = []

    # -- loading -----------------------------------------------------------------

    @staticmethod
    def _load_json(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _control_keep(obj):
        argv = obj.get("argv") or []
        task = None
        for i, a in enumerate(argv):
            if a == "--task" and i + 1 < len(argv):
                task = argv[i + 1]
                break
        return {"ts": obj.get("ts"), "verb": obj.get("verb"), "rc": obj.get("rc"),
                "seq_before": obj.get("seq_before"), "seq_after": obj.get("seq_after"),
                "task": task, "ms": obj.get("ms")}

    def refresh(self, control=True):
        """Pull everything new. `control=False` skips the 250 MB call log
        (the dashboard reads it on a slower cadence than the rest)."""
        self.events.refresh()
        try:
            m = self.state_path.stat().st_mtime
        except OSError:
            m = None
        if m != self._state_mtime:
            st = self._load_json(self.state_path)
            if st is not None:
                self.state, self._state_mtime = st, m
        self._refresh_records()
        self._refresh_pack()
        self._refresh_catalog()
        self._refresh_proofs()
        self.turns.extend(self.costs.read_new())
        self.pipeline_rows.extend(self.pipelines.read_new())
        self.alert_rows.extend(self.alerts.read_new())
        if control:
            self.calls.extend(self.control.read_new())

    def _refresh_records(self):
        d = self.run_root / "packets"
        try:
            m = d.stat().st_mtime
        except OSError:
            return
        if m == self._records_mtime and self.records:
            return
        self._records_mtime = m
        for f in glob.glob(str(d / "*.json")):
            if f.endswith(".params.json"):
                continue
            rec = self._load_json(f)
            if isinstance(rec, dict) and rec.get("task"):
                self.records[rec["task"]] = rec

    def _refresh_pack(self, max_age_s=120):
        now = utc_now().timestamp()
        if self.pack and now - self._pack_loaded < max_age_s:
            return
        self._pack_loaded = now
        if vppack is None or not self.pack_dir or not self.pack_dir.is_dir():
            return
        try:
            packets, lint = vppack.load_pack(str(self.pack_dir))
        except Exception as exc:  # a malformed PACKET.md must not take the model down
            self.errors.append("load_pack: %s" % str(exc)[:200])
            return
        self.pack_lint = list(lint or [])
        self.pack = {pid: p for pid, p in packets.items() if isinstance(p, dict)}

    def _refresh_catalog(self):
        if self.catalog_ids or not self.catalog_path or not self.catalog_path.is_file():
            return
        cat = self._load_json(self.catalog_path)
        ids = []
        if isinstance(cat, dict):
            rows = cat.get("tasks") or cat.get("contracts") or cat.get("lanes") or []
            if isinstance(rows, dict):
                ids = list(rows.keys())
            else:
                ids = [r.get("task_id") or r.get("id") for r in rows if isinstance(r, dict)]
        elif isinstance(cat, list):
            ids = [r.get("task_id") or r.get("id") for r in cat if isinstance(r, dict)]
        self.catalog_ids = [i for i in ids if i]

    def _refresh_proofs(self):
        d = self.run_root / "proofs"
        try:
            names = os.listdir(d)
        except OSError:
            return
        for name in names:
            if not name.endswith(".json") or name in self._proof_seen:
                continue
            self._proof_seen.add(name)
            p = self._load_json(d / name)
            if isinstance(p, dict) and p.get("proof_id"):
                # D149: `only` is what the proof actually RAN.  Without it the board
                # can show PASS but not whether that PASS covers one file or the whole
                # suite -- the union-104 shape (see LEDGER.md's scope column, D148).
                self.proofs[p["proof_id"]] = {k: p.get(k) for k in
                                              ("proof_id", "status", "route", "sha", "ts", "kind", "counts", "rc", "account", "pipeline_id", "branch", "only")}
        self._index_passing_proofs()

    def _index_passing_proofs(self):
        """D150: newest PASSing proof per task, built ONCE per refresh.

        The roots table needs a scope per root, and a root owns several rows.
        Scanning every proof for every root is ~600 x 760 per refresh; this is
        one pass over the proofs instead, and the roots then do a dict lookup.
        """
        best = {}
        for pid, p in self.proofs.items():
            if p.get("status") != "PASS":
                continue
            body = pid[len("proof-"):] if pid.startswith("proof-") else pid
            task = body.rsplit("-", 1)[0]
            prev = best.get(task)
            if prev is None or (p.get("ts") or "") > (prev.get("ts") or ""):
                best[task] = p
        self.pass_by_task = best

    def rows_scope(self, rows):
        """D150: what the newest PASS across these rows actually ran.

        A root's evidence may sit on its hosted twin rather than the primary row,
        so this asks the whole row set rather than one row -- the same reason
        LaneDriver.member_scope consults twins.  `-` when no row has a PASS:
        that is "nothing to report here", never a full-suite claim.
        """
        best = None
        for t in rows or ():
            p = getattr(self, "pass_by_task", {}).get(t)
            if p and (best is None or (p.get("ts") or "") > (best.get("ts") or "")):
                best = p
        return proof_scope_label(best) if best else "-"

    # -- identity ------------------------------------------------------------------

    @property
    def tasks(self):
        return (self.state or {}).get("tasks") or {}

    def packet_of(self, task):
        row = self.tasks.get(task) or {}
        pid = (row.get("parameters") or {}).get("packet_id")
        if not pid:
            pid = (self.records.get(task) or {}).get("packet")
        return pid

    def root_packet(self, pid):
        """A hosted twin (<ID>-HOSTED[-GATE]) proves its parent packet: follow
        twin_of to the packet that is the actual work item. The driver's
        retry records already carry the parent packet id, so a -Rn row needs
        no chain walk here."""
        seen = set()
        while pid and pid not in seen:
            seen.add(pid)
            p = self.pack.get(pid)
            if p and p.get("twin_of"):
                pid = p["twin_of"]
                continue
            if pid not in self.pack:
                m = re.match(r"^(.*?)-HOSTED(?:-[A-Z0-9]+)?$", pid)   # twin whose parent dir is gone
                if m:
                    pid = m.group(1)
                    continue
            break
        return pid

    def root_of(self, task, _depth=0):
        """(root_id, kind, how) -- kind in catalog|packet|contract|orphan."""
        row = self.tasks.get(task)
        if row is None:
            return task, "orphan", "unknown row"
        if not row.get("dynamic"):
            return task, "catalog", "catalog row"
        pid = self.packet_of(task)
        if pid:
            root = self.root_packet(pid)
            if root in self.tasks and not self.tasks[root].get("dynamic"):
                # a regrade packet named after a catalog row IS that row's work
                return root, "catalog", "packet %s regrades catalog row %s" % (pid, root)
            return root, "packet", "packet %s" % pid if root == pid else "twin %s of packet %s" % (pid, root)
        parent = row.get("parent_contract_id")
        if parent and parent != task and parent in self.tasks and _depth < 10:
            r, k, how = self.root_of(parent, _depth + 1)
            return r, k, "%s of %s" % (row.get("template_id") or "row", parent)
        fam = re.sub(r"(?:-[RB]?\d+)+$", "", task)
        if fam != task and fam in self.tasks:
            return self.root_of(fam, _depth + 1)
        return task, "orphan", "no packet, no parent"

    # -- roots and journeys -----------------------------------------------------------

    def superseded_rows(self):
        """rows that a later -Rn row retries (driver packets/<task>.json retry_of);
        such a row is history even when the scheduler never retired it"""
        return {rec["retry_of"] for rec in self.records.values() if rec.get("retry_of")}

    def events_by_task(self):
        out = defaultdict(list)
        for ev in self.events.ordered():
            t = ev.get("task_id")
            if t:
                out[t].append(ev)
        return out

    def roots(self):
        """OrderedDict root_id -> {kind, rows[], state, latest_ts, done, owed}"""
        ev_by = self.events_by_task()
        roots = OrderedDict()
        for task, row in self.tasks.items():
            rid, kind, how = self.root_of(task)
            r = roots.setdefault(rid, {"id": rid, "kind": kind, "rows": [], "how": {}})
            r["rows"].append(task)
            r["how"][task] = how
            if kind == "catalog" and r["kind"] != "catalog":
                r["kind"] = "catalog"
        for rid, r in roots.items():
            def last_ts(t):
                # never run-state updated_at: the scheduler touches it every tick
                evs = ev_by.get(t) or []
                return (evs[-1].get("created_at") if evs else None) or ""
            rows = sorted(r["rows"], key=lambda t: (int(ev_by[t][0].get("sequence") or 0) if ev_by.get(t) else 0, t))
            r["rows"] = rows
            st = {t: self.tasks[t].get("state") for t in rows}
            r["states"] = st
            r["latest_row"] = max(rows, key=lambda t: last_ts(t) or "")
            r["latest_ts"] = last_ts(r["latest_row"])
            # the row that IS the work item: the catalog row, else the packet's own
            # dynamic row (L06 -> L06-V13), else the latest non-cancelled row
            primary = rid if (rid in st and not self.tasks[rid].get("dynamic")) else None
            if primary is None:
                did = vppack.dynamic_id(self.pack[rid]) if (vppack is not None and rid in self.pack) else rid
                cands = [t for t in rows if t == did or re.match(r"^%s-R\d+$" % re.escape(did), t)]
                # a retired row and its successor share the retirement timestamp: live wins
                primary = max(cands, key=lambda t: (st[t] != "CANCELLED", last_ts(t) or "", t)) if cands else None
            if primary is None:
                live = [t for t in rows if st[t] != "CANCELLED"]
                primary = max(live, key=lambda t: last_ts(t) or "") if live else r["latest_row"]
            r["primary_row"] = primary
            r["primary_state"] = st[primary]
            superseded = self.superseded_rows()
            open_rows = [t for t in rows if st[t] not in CLOSED_STATES and t not in superseded]
            r["open_rows"] = open_rows
            r["superseded_open"] = [t for t in rows if st[t] not in CLOSED_STATES and t in superseded]
            if open_rows:
                r["state"] = min((st[t] for t in open_rows),
                                 key=lambda s: OPEN_PRIORITY.index(s) if s in OPEN_PRIORITY else len(OPEN_PRIORITY))
            else:
                r["state"] = st[primary]
            twins = [t for t in rows if (self.pack.get(self.packet_of(t) or "") or {}).get("twin_of") == rid
                     or re.search(r"-HOSTED(-[A-Z0-9]+)?(-R\d+)?$", t)]
            r["hosted_state"] = st[max(twins, key=lambda t: (st[t] != "CANCELLED", last_ts(t) or "", t))] if twins else None
            # D150: scope beside the verdict on the roots table, not only inside the
            # journey detail.  The badge is what people read; a VERIFIED root whose
            # newest PASS is `scoped:` is not evidence outside that path list.
            r["proof_scope"] = self.rows_scope(rows)
            r["done"] = st[primary] in DONE_STATES
            r["settled"] = r["done"] and not open_rows
            r["owed"] = r["state"] in OWED_STATES
            r["attempts"] = len(rows)
            p = self.pack.get(rid) or {}
            r["title"] = p.get("title") or ""
        return roots

    def scope(self):
        """The fixed denominator and where each number comes from: catalog
        rows plus packet dirs, a packet named after a catalog row counted
        once (it regrades that row), twins and -Rn packets not counted."""
        catalog_rows = [t for t, r in self.tasks.items() if not r.get("dynamic")]
        cat = set(catalog_rows)
        pack_all = sorted(pid for pid, p in self.pack.items()
                          if not p.get("twin_of") and not re.search(r"-R\d+$", pid))
        pack_on_catalog = [pid for pid in pack_all if pid in cat]
        pack_new = [pid for pid in pack_all if pid not in cat]
        roots = self.roots()
        derived_catalog = [r for r in roots.values() if r["kind"] == "catalog"]
        derived_packet = [r for r in roots.values() if r["kind"] == "packet"]
        orphans = [r for r in roots.values() if r["kind"] == "orphan"]
        return {
            "catalog_rows": len(catalog_rows),
            "catalog_source": str(self.catalog_path),
            "catalog_file_rows": len(self.catalog_ids) if self.catalog_ids else None,
            "packet_dirs": len(pack_all),
            "packets_on_catalog_rows": len(pack_on_catalog),
            "packets_new_work": len(pack_new),
            "packets_source": str(self.pack_dir),
            "work_items_fixed": len(catalog_rows) + len(pack_new),
            "work_items_derived": len(derived_catalog) + len(derived_packet),
            "packet_roots_with_rows": len(derived_packet),
            "packet_roots_without_rows": sorted(set(pack_new) - set(r["id"] for r in derived_packet)),
            "packet_roots_not_in_pack": sorted(set(r["id"] for r in derived_packet) - set(pack_new)),
            "orphan_roots": [r["id"] for r in orphans],
            "attempt_rows": len(self.tasks),
            "retry_rows": sum(1 for t in self.tasks if re.search(r"-R\d+$", t)),
        }

    def summary(self):
        roots = self.roots()
        by_state = Counter(r["state"] for r in roots.values())
        by_kind = Counter(r["kind"] for r in roots.values())
        done = sum(1 for r in roots.values() if r["settled"])
        local_done = sum(1 for r in roots.values() if r["done"])
        owed = sum(1 for r in roots.values() if r["owed"])
        active = sum(1 for r in roots.values() if r["state"] in ("RUNNING", "CLAIMED", "READY"))
        hosted_open = sum(1 for r in roots.values() if r["hosted_state"] and r["hosted_state"] not in CLOSED_STATES)
        return {
            "generated_at": iso(utc_now()),
            "scope": self.scope(),
            "roots": len(roots),
            "roots_by_kind": dict(by_kind),
            "roots_by_state": dict(by_state),
            "settled": done, "local_done": local_done, "owed": owed, "active": active,
            "hosted_open": hosted_open,
            "not_settled": len(roots) - done,
            "rows": len(self.tasks),
            "rows_by_state": dict(Counter(r.get("state") for r in self.tasks.values())),
            "events": len(self.events.by_seq),
            "event_gaps": self.events.gaps()[:20],
            "turns": len(self.turns),
            "sequence": (self.state or {}).get("sequence"),
            "state_updated_at": (self.state or {}).get("updated_at"),
        }

    def _turn_effort(self, task, attempt, n, rnd, role):
        p = self.run_root / "turns" / str(task) / str(attempt) / ("%s-r%s-%s-record.json" % (n, rnd, role))
        rec = self._load_json(p)
        if not isinstance(rec, dict):
            return {}
        return {k: rec.get(k) for k in ("effort", "variant", "model_seen", "server", "session_id") if rec.get(k) not in (None, "None")}

    def journey(self, root_id):
        roots = self.roots()
        r = roots.get(root_id)
        if not r:
            # accept a row id and resolve to its root
            rid, _k, _h = self.root_of(root_id)
            r = roots.get(rid)
            if not r:
                return None
        ev_by = self.events_by_task()
        turns_by = defaultdict(list)
        for t in self.turns:
            if t.get("task") in r["rows"]:
                turns_by[t["task"]].append(t)
        proofs_by = defaultdict(list)
        for pid, p in self.proofs.items():
            body = pid[len("proof-"):] if pid.startswith("proof-") else pid
            task = body.rsplit("-", 1)[0]
            if task in r["rows"]:
                proofs_by[task].append(p)
        attempts = []
        for task in r["rows"]:
            row = self.tasks[task]
            steps = []
            for ev in ev_by.get(task) or []:
                if ev.get("type") not in STEP_EVENTS:
                    continue
                steps.append(self._event_step(ev))
            for t in turns_by.get(task) or []:
                extra = self._turn_effort(task, t.get("attempt"), t.get("n"), t.get("round"), t.get("role"))
                model = t.get("model") or "?"
                where = "@%s" % (extra.get("server") or t.get("server")) if (extra.get("server") or t.get("server")) else ""
                eff = extra.get("effort") or extra.get("variant")
                steps.append({
                    "ts": t.get("ts"), "kind": "turn", "seq": None,
                    "text": "r%s %s: %s %s%s%s -> %s" % (
                        t.get("round"), t.get("role"), t.get("runner"), model, where,
                        (" (%s)" % eff) if eff else "", t.get("status")),
                    "runner": t.get("runner"), "model": model, "effort": eff, "role": t.get("role"),
                    "round": t.get("round"), "status": t.get("status"),
                    "cost": t.get("cost"), "duration_s": t.get("duration_s"),
                    "attempt": t.get("attempt"), "model_seen": extra.get("model_seen"),
                    "source": "costs.jsonl + turns/%s/%s/%s-r%s-%s-record.json" % (
                        task, t.get("attempt"), t.get("n"), t.get("round"), t.get("role")),
                })
            for p in proofs_by.get(task) or []:
                counts = p.get("counts") or {}
                steps.append({
                    "ts": p.get("ts"), "kind": "proof", "seq": None,
                    "text": "proof %s on %s [%s] -> %s%s" % (
                        short(p.get("sha")), p.get("route"), proof_scope_label(p), p.get("status"),
                        (" (%s)" % ", ".join("%s=%s" % (k, v) for k, v in sorted(counts.items())
                                            if k in ("passed", "failed", "error", "skipped") and v)) if counts else ""),
                    "status": p.get("status"), "route": p.get("route"), "proof_id": p.get("proof_id"),
                    "scope": proof_scope_label(p),
                    "source": "proofs/%s.json" % p.get("proof_id"),
                })
            steps.sort(key=lambda s: (s.get("ts") or "", s.get("seq") or 0))
            attempts.append({
                "task": task, "state": row.get("state"), "kind": row.get("kind"),
                "template": row.get("template_id"), "how": r["how"].get(task),
                "base_sha": row.get("base_sha"), "output_sha": row.get("output_sha"),
                "depends_on": row.get("depends_on") or [], "stacked_on": row.get("stacked_on") or [],
                "blocker": row.get("blocker"), "packet": self.packet_of(task),
                "retry_of": (self.records.get(task) or {}).get("retry_of"),
                "retry_reason": (self.records.get(task) or {}).get("retry_reason"),
                "steps": steps,
            })
        return {"root": r["id"], "kind": r["kind"], "state": r["state"], "title": r.get("title"),
                "primary_row": r["primary_row"], "primary_state": r["primary_state"],
                "hosted_state": r["hosted_state"], "open_rows": r["open_rows"], "settled": r["settled"],
                "superseded_open": r["superseded_open"],
                "latest_row": r["latest_row"], "attempts": attempts}

    @staticmethod
    def _event_step(ev):
        typ = ev.get("type")
        if typ == "TEMPLATE_TASK_INSTANTIATED":
            text = "instantiated from %s (parent %s)" % (ev.get("template_id"), ev.get("parent_contract_id"))
        elif typ == "TASK_CLAIMED":
            text = "claimed by chief %s on %s%s" % (
                ev.get("chief"), short(ev.get("base_sha") or ev.get("stacked_base")),
                (" stacked on %s" % ",".join(ev.get("stacked_on") or [])) if ev.get("stacked_on") else "")
        elif typ == "TASK_STARTED":
            text = "started: route %s (%s)" % (ev.get("resolved_model"), ev.get("resolved_effort"))
        elif typ == "TASK_COMPLETED":
            text = "completed -> %s%s" % (ev.get("outcome"), (": %s" % str(ev.get("reason"))[:160]) if ev.get("reason") else "")
        elif typ == "TASK_RETIRED":
            text = "retired -> CANCELLED (closers %s)%s" % (",".join(ev.get("closers") or []),
                                                          (": %s" % str(ev.get("reason"))[:120]) if ev.get("reason") else "")
        elif typ == "TASK_PROMOTED_TO_INTEGRATED":
            text = "promoted -> INTEGRATED at %s" % short(ev.get("output_sha"))
        elif typ == "TASK_BLOCKED":
            text = "blocked [%s]%s" % (ev.get("class") or (ev.get("blocker") or {}).get("class") if isinstance(ev.get("blocker"), dict) else ev.get("class"),
                                      (": %s" % str(ev.get("reason"))[:160]) if ev.get("reason") else "")
        elif typ == "TASK_UNBLOCKED":
            text = "unblocked%s" % ((": %s" % str(ev.get("reason"))[:120]) if ev.get("reason") else "")
        elif typ == "TASK_INVALIDATED":
            text = "invalidated from %s%s" % (ev.get("from_state") or ev.get("previous_state"),
                                              (": %s" % str(ev.get("reason"))[:160]) if ev.get("reason") else "")
        elif typ == "TASK_DEPENDENCIES_REWIRED":
            text = "dependencies rewired %s -> %s by %s" % (ev.get("from"), ev.get("to"), ev.get("actor"))
        elif typ == "PRESERVED_RESULT_ADOPTED":
            text = "preserved result adopted -> %s" % ev.get("outcome")
        else:
            text = typ
        return {"ts": ev.get("created_at"), "kind": "event", "seq": int(ev.get("sequence") or 0),
                "type": typ, "text": text, "source": "events/%s" % ev.get("_file")}

    # -- verify ---------------------------------------------------------------------

    def derived_states(self):
        """task -> (expected_state or None, proving event)"""
        out = {}
        for task, evs in self.events_by_task().items():
            last = evs[-1]
            typ = last.get("type")
            if typ == "TASK_COMPLETED" or typ == "PRESERVED_RESULT_ADOPTED":
                exp = last.get("outcome")
            elif typ in TERMINAL_EVENT_STATE:
                exp = TERMINAL_EVENT_STATE[typ]
            elif typ in RUNNING_EVENTS:
                exp = "RUNNING"
            else:
                exp = None      # instantiate/rewire/unblock/invalidate: state is derived by the scheduler
            out[task] = (exp, last)
        return out

    def ledger_states(self):
        """LEDGER.md table: task -> state (None if unreadable)."""
        p = self.run_root / "LEDGER.md"
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return None
        out = {}
        for line in text.splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2 or cells[0] in ("task", "---", "") or set(cells[0]) <= {"-"}:
                continue
            task = cells[0].strip("`")
            if task in self.tasks:
                out[task] = cells[1].strip("`*")
        return out

    def verify(self):
        findings = []
        # 1. sequence continuity
        gaps = self.events.gaps()
        if gaps:
            findings.append({"check": "event_sequence", "severity": "error",
                             "detail": "missing sequences %s" % gaps[:20]})
        if self.events.bad:
            findings.append({"check": "event_files", "severity": "error",
                             "detail": "unreadable event files %s" % self.events.bad[:10]})
        # 2. derived state vs run-state.json
        ds = self.derived_states()
        for task, (exp, ev) in sorted(ds.items()):
            row = self.tasks.get(task)
            if row is None:
                findings.append({"check": "row_missing", "severity": "error", "task": task,
                                 "detail": "events exist (last %s seq %s) but run-state has no row"
                                 % (ev.get("type"), ev.get("sequence"))})
                continue
            actual = row.get("state")
            if exp is None:
                continue
            if exp == "RUNNING" and actual in ("RUNNING", "CLAIMED"):
                continue
            if exp != actual:
                findings.append({"check": "state_vs_events", "severity": "error", "task": task,
                                 "detail": "run-state says %s; last event %s (seq %s, %s) implies %s"
                                 % (actual, ev.get("type"), ev.get("sequence"), ev.get("created_at"), exp),
                                 "event": ev.get("_file")})
        for task in self.tasks:
            if task not in ds and self.tasks[task].get("dynamic"):
                findings.append({"check": "row_without_events", "severity": "warn", "task": task,
                                 "detail": "dynamic row has no scheduler event"})
        # 2b. a retried row that is still open: the successor exists, the original was never retired
        sup = self.superseded_rows()
        for task in sorted(sup):
            row = self.tasks.get(task)
            if row and row.get("state") not in CLOSED_STATES:
                succ = sorted(t for t, rec in self.records.items() if rec.get("retry_of") == task)
                findings.append({"check": "superseded_still_open", "severity": "warn", "task": task,
                                 "detail": "still %s although %s retries it; totals must not count it as open"
                                 % (row.get("state"), ",".join(succ))})
        # 3. LEDGER.md vs run-state
        ls = self.ledger_states()
        if ls is None:
            findings.append({"check": "ledger", "severity": "warn", "detail": "LEDGER.md unreadable"})
        else:
            stale = [(t, s, self.tasks[t].get("state")) for t, s in ls.items() if s != self.tasks[t].get("state")]
            for t, s, a in stale[:50]:
                findings.append({"check": "ledger_vs_state", "severity": "warn", "task": t,
                                 "detail": "LEDGER.md shows %s, run-state has %s (render is stale or wrong)" % (s, a)})
            missing = [t for t in self.tasks if t not in ls]
            if missing:
                findings.append({"check": "ledger_coverage", "severity": "warn",
                                 "detail": "%d rows absent from LEDGER.md (e.g. %s)" % (len(missing), missing[:5])})
        # 4. control.jsonl spans cover every event (only when the call log was read)
        if self.calls:
            covered = set()
            for c in self.calls:
                try:
                    a, b = int(c.get("seq_before")), int(c.get("seq_after"))
                except (TypeError, ValueError):
                    continue
                covered.update(range(a + 1, b + 1))
            uncovered = [s for s in sorted(self.events.by_seq) if s not in covered]
            if uncovered:
                findings.append({"check": "control_spans", "severity": "info",
                                 "detail": "%d event sequences not inside any driver call span (e.g. %s) -- written by another actor (scheduler bootstrap, Architect/Operator verbs) or a lost control line"
                                 % (len(uncovered), uncovered[:15])})
            if self.control.bad:
                findings.append({"check": "control_jsonl", "severity": "warn",
                                 "detail": "%d unparseable control.jsonl lines" % self.control.bad})
        # 5. identity coverage
        sc = self.scope()
        if sc["orphan_roots"]:
            findings.append({"check": "root_identity", "severity": "warn",
                             "detail": "%d rows could not be attributed to a catalog row or packet: %s"
                             % (len(sc["orphan_roots"]), sc["orphan_roots"][:10])})
        if sc["work_items_fixed"] + len(sc["packet_roots_not_in_pack"]) != sc["work_items_derived"] + len(sc["packet_roots_without_rows"]):
            findings.append({"check": "scope_arithmetic", "severity": "warn",
                             "detail": "fixed %d + roots not in pack %d != derived %d + packets without rows %d"
                             % (sc["work_items_fixed"], len(sc["packet_roots_not_in_pack"]),
                                sc["work_items_derived"], len(sc["packet_roots_without_rows"]))})
        if sc["packet_roots_not_in_pack"]:
            findings.append({"check": "root_identity", "severity": "warn",
                             "detail": "rows name packets that no PACKET.md defines (retired/renamed dirs?): %s"
                             % sc["packet_roots_not_in_pack"][:10]})
        return {"generated_at": iso(utc_now()), "events": len(self.events.by_seq),
                "rows": len(self.tasks), "calls_read": len(self.calls),
                "errors": sum(1 for f in findings if f["severity"] == "error"),
                "warnings": sum(1 for f in findings if f["severity"] == "warn"),
                "findings": findings}

    # -- text renders -----------------------------------------------------------------

    def render_ledger(self):
        """A LEDGER derived from events only: one line per root, its attempts and
        their proving events. Never reads the driver's in-memory state."""
        roots = self.roots()
        s = self.summary()
        out = ["# LEDGER (derived from orchestration-state/events, %s)" % s["generated_at"], "",
               "work items: %d fixed (%d catalog rows + %d new-work packets; %d packets regrade catalog rows) | settled %d | owed %d | attempts (rows) %d"
               % (s["scope"]["work_items_fixed"], s["scope"]["catalog_rows"], s["scope"]["packets_new_work"],
                  s["scope"]["packets_on_catalog_rows"], s["settled"], s["owed"], s["rows"]), "",
               "| root | kind | state | primary | hosted | open rows | attempts | last event |", "|---|---|---|---|---|---|---|---|"]
        for rid, r in sorted(roots.items(), key=lambda kv: (kv[1]["kind"], kv[0])):
            out.append("| %s | %s | %s | %s | %s | %s | %d | %s |" % (
                rid, r["kind"], r["state"], r["primary_state"], r["hosted_state"] or "-",
                ",".join(r["open_rows"]) or "-", r["attempts"], r["latest_ts"] or ""))
        return "\n".join(out) + "\n"

    def render_journey(self, root_id):
        j = self.journey(root_id)
        if not j:
            return "no such root or row: %s\n" % root_id
        out = ["%s [%s] %s (primary %s, hosted %s)%s" % (j["root"], j["kind"], j["state"], j["primary_state"],
                                                          j.get("hosted_state") or "-", (" -- %s" % j["title"]) if j.get("title") else "")]
        for a in j["attempts"]:
            out.append("  %s (%s, %s)%s" % (a["task"], a["template"] or a["kind"], a["state"],
                                             (" retry of %s: %s" % (a["retry_of"], (a["retry_reason"] or "")[:100])) if a["retry_of"] else ""))
            for st in a["steps"]:
                out.append("    %s  %s" % ((st.get("ts") or "")[:19], st["text"]))
        return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary")
    sub.add_parser("roots")
    sub.add_parser("scope")
    p = sub.add_parser("journey")
    p.add_argument("root")
    v = sub.add_parser("verify")
    v.add_argument("--no-control", action="store_true", help="skip the 250 MB control.jsonl span check")
    sub.add_parser("ledger")
    args = ap.parse_args(argv)
    j = Journey(args.run_root)
    j.refresh(control=(args.cmd == "verify" and not args.no_control))
    if args.cmd == "summary":
        print(json.dumps(j.summary(), indent=1))
    elif args.cmd == "scope":
        print(json.dumps(j.scope(), indent=1))
    elif args.cmd == "roots":
        roots = j.roots()
        if args.json:
            print(json.dumps(roots, indent=1))
        else:
            for rid, r in roots.items():
                print("%-32s %-8s %-18s primary=%-16s hosted=%-16s open=%d attempts=%d" % (
                    rid, r["kind"], r["state"], r["primary_state"], r["hosted_state"] or "-", len(r["open_rows"]), r["attempts"]))
    elif args.cmd == "journey":
        print(json.dumps(j.journey(args.root), indent=1) if args.json else j.render_journey(args.root), end="")
    elif args.cmd == "ledger":
        print(j.render_ledger(), end="")
    elif args.cmd == "verify":
        rep = j.verify()
        if args.json:
            print(json.dumps(rep, indent=1))
        else:
            print("events=%d rows=%d calls_read=%d errors=%d warnings=%d" % (
                rep["events"], rep["rows"], rep["calls_read"], rep["errors"], rep["warnings"]))
            for f in rep["findings"]:
                print("  [%s] %s%s: %s" % (f["severity"], f["check"], (" " + f["task"]) if f.get("task") else "", f["detail"]))
        return 1 if rep["errors"] else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
