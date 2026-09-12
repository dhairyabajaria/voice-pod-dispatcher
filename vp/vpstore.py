#!/usr/bin/env python3
"""vpstore — authority store for the Voice Pod v9 control layer.

Python 3.12, standard library only (sqlite3, json, time, datetime, hashlib, os).

The SQLite database at RUN_ROOT/state.db is the only authority.  Every mutation
writes exactly one event row, which is mirrored to RUN_ROOT/events.jsonl (and to
escalations.jsonl for the escalation kinds) inside the same transaction boundary:
the database row is written first, the file line second, and a file failure
raises so the transaction rolls back.

See SPEC.md for the contract this file implements.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import sqlite3
import time

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_RUN_ROOT = (
    "/Users/dhairyabajaria/Claude Code/Calling New/"
    "test-logs/audit/run-2026-09-12-night"
)
DEFAULT_CN = "/Users/dhairyabajaria/Claude Code/Calling New"

ESCALATION_KINDS = {
    "STUCK",
    "STALLED",
    "QUOTA",
    "AUTH",
    "BLOCKED",
    "VIOLATION",
    "INHIBIT",
    "STOP",
    "DELIVERY_INCIDENT",
    "ROUND_CAP",
    "RUNNER_DENIED", "RUNNER_TIMEOUT", "RUNNER_CRASH", "RUNNER_REFUSED",
    "QUOTA_WEEKLY", "QUOTA_ROLLING", "RATE", "DEGRADED", "MODEL_MISMATCH",
    "SESSION_MISMATCH", "ORPHANED", "RECORD_INVALID", "BUDGET", "DISK",
    "PARKED", "OWNER_ACTION", "TRUNK_MOVED", "PROOF_FAIL", "UNION_CONFLICT",
}

ITEM_STATUSES = [
    "READY",
    "ASSIGNED",
    "BUILDING",
    "GRADING",
    "JUNIOR_SATISFIED",
    "PREPARING",
    "PREPARED",
    "PROOF_PENDING",
    "FINAL_REVIEW",
    "APPROVED",
    "PROMOTED",
    "PAUSED",
    "BLOCKED",
    "STOPPED",
]

# status machine: allowed forward transitions (terminal/halt states handled apart)
ITEM_TRANSITIONS = {
    "READY": {"ASSIGNED", "PAUSED", "BLOCKED", "STOPPED"},
    "ASSIGNED": {"BUILDING", "READY", "PAUSED", "BLOCKED", "STOPPED"},
    "BUILDING": {"GRADING", "PAUSED", "BLOCKED", "STOPPED"},
    "GRADING": {"BUILDING", "JUNIOR_SATISFIED", "PAUSED", "BLOCKED", "STOPPED"},
    "JUNIOR_SATISFIED": {"PREPARING", "GRADING", "PAUSED", "BLOCKED", "STOPPED"},
    "PREPARING": {"PREPARED", "GRADING", "PAUSED", "BLOCKED", "STOPPED"},
    "PREPARED": {"PROOF_PENDING", "FINAL_REVIEW", "GRADING", "PAUSED", "BLOCKED", "STOPPED"},
    "PROOF_PENDING": {"FINAL_REVIEW", "GRADING", "PREPARED", "PAUSED", "BLOCKED", "STOPPED"},
    "FINAL_REVIEW": {"APPROVED", "GRADING", "PAUSED", "BLOCKED", "STOPPED"},
    "APPROVED": {"PROMOTED", "GRADING", "PAUSED", "BLOCKED", "STOPPED"},
    "PROMOTED": set(),
    "PAUSED": {"READY", "ASSIGNED", "BUILDING", "GRADING", "JUNIOR_SATISFIED",
               "PREPARING", "PREPARED", "PROOF_PENDING", "FINAL_REVIEW",
               "APPROVED", "STOPPED", "BLOCKED"},
    "BLOCKED": {"READY", "GRADING", "STOPPED"},
    "STOPPED": set(),
}

# Communication matrix (from -> set(to)).  Anything else refuses + violation.
MATRIX: dict[str, set[str]] = {
    "owner": {"architect"},
    "architect": {"owner", "boss"},
    "boss": {"architect", "cos"},
    "cos": {"boss", "senior", "integrator", "quickie", "daemon"},
    "senior": {"cos", "junior"},
    "integrator": {"cos"},
    "quickie": {"cos", "integrator"},
    "junior": {"integrator", "daemon"},
    "daemon": {"builder", "junior", "infra", "cos", "recovery"},
    "builder": {"daemon"},
    "infra": {"daemon"},
    "recovery": {"cos", "owner"},
}

ATTEMPT_STATUSES = {
    "RUNNING", "DONE", "STUCK", "STALLED", "QUOTA", "AUTH",
    "TRANSPORT", "INCOMPLETE", "ABORTED",
    # v12 runner statuses (vprunners.py / 07-FAILURE-CATALOG)
    "RUNNER_EMPTY", "RUNNER_DENIED", "RUNNER_TIMEOUT", "RUNNER_CRASH",
    "RUNNER_REFUSED", "QUOTA_WEEKLY", "QUOTA_ROLLING", "RATE", "DEGRADED",
    "MODEL_MISMATCH", "SESSION_MISMATCH", "ORPHANED", "STOPPED",
    "RECORD_INVALID", "PROGRESS_STOP",
}
RUNNER_ESCALATIONS = {
    "RUNNER_DENIED", "RUNNER_TIMEOUT", "RUNNER_CRASH", "RUNNER_REFUSED",
    "QUOTA_WEEKLY", "QUOTA_ROLLING", "RATE", "DEGRADED", "MODEL_MISMATCH",
    "SESSION_MISMATCH", "ORPHANED", "RECORD_INVALID", "BUDGET", "DISK",
    "PARKED", "OWNER_ACTION", "TRUNK_MOVED", "PROOF_FAIL", "UNION_CONFLICT",
}
TURN_KINDS = ("build", "grade", "infra", "review", "final", "integrate",
              "proof", "boss")
DELIVERY_STATUSES = {
    "OUTBOX", "RESERVED", "SENT", "ACK", "CONSUMED", "UNCERTAIN", "FAILED",
}
PROOF_STATUSES = {
    "REQUESTED", "RUNNING", "PASS", "FAIL_PRODUCT", "FAIL_INFRA", "UNKNOWN", "CANCELLED",
}
RUN_STATUSES = {"RUNNING", "STOPPING", "STOPPED", "INHIBITED"}


# --------------------------------------------------------------------------
# Errors -> exit codes
# --------------------------------------------------------------------------

class VpError(Exception):
    code = 1


class Usage(VpError):
    code = 2


class Refused(VpError):
    code = 3


class Conflict(VpError):
    code = 4


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def now_ts() -> str:
    """UTC ISO-8601 with milliseconds and a trailing Z."""
    dt = datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def mono() -> float:
    return time.monotonic()


def run_root() -> str:
    return os.environ.get("VP_RUN_ROOT", DEFAULT_RUN_ROOT)


def stop_file_path(root=None) -> str:
    """v12: `<run root>/STOP` (00-START-HERE: STOP is a file in the run root).
    VP_STOP_FILE overrides (tests); the v9 `$CN/test-logs/driver/STOP` is the
    fallback only when no run root is known."""
    override = os.environ.get("VP_STOP_FILE")
    if override:
        return override
    if root:
        return os.path.join(root, "STOP")
    cn = os.environ.get("VP_CN", DEFAULT_CN)
    return os.path.join(cn, "test-logs", "driver", "STOP")


def stop_file_exists(root=None) -> bool:
    return os.path.exists(stop_file_path(root))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def append_line(path: str, line: str) -> None:
    """Atomic-enough single-write append; raises on failure."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _jload(text, default=None):
    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


SCHEMA = """
CREATE TABLE IF NOT EXISTS run(
  run_id TEXT PRIMARY KEY, started_ts TEXT, status TEXT, trunk_head TEXT);
CREATE TABLE IF NOT EXISTS role(
  role_id TEXT PRIMARY KEY, kind TEXT, session_ref TEXT, model TEXT,
  server TEXT, group_no INTEGER, available INTEGER DEFAULT 0, ts TEXT);
CREATE TABLE IF NOT EXISTS packet(
  packet_id TEXT PRIMARY KEY, item TEXT, rev INTEGER, submitted_by TEXT,
  benchmark_path TEXT, packet_path TEXT, base_sha TEXT, critical INTEGER,
  allowed_files TEXT, status TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS item(
  item TEXT PRIMARY KEY, rev INTEGER, status TEXT, group_no INTEGER,
  builder_role TEXT, junior_role TEXT, senior_plan TEXT, senior_final TEXT,
  worktree TEXT, base_sha TEXT, candidate_sha TEXT, round INTEGER,
  wip_slot INTEGER, ts TEXT);
CREATE TABLE IF NOT EXISTS attempt(
  attempt_id TEXT PRIMARY KEY, item TEXT, n INTEGER, kind TEXT, session_id TEXT,
  server TEXT, agent TEXT, model TEXT, variant TEXT, status TEXT,
  started_ts TEXT, ended_ts TEXT, result_path TEXT, tokens_in INTEGER,
  tokens_out INTEGER, tokens_reason INTEGER, cache_read INTEGER, cost REAL);
CREATE TABLE IF NOT EXISTS delivery(
  delivery_id TEXT PRIMARY KEY, from_role TEXT, to_role TEXT, type TEXT,
  ref TEXT, status TEXT, reserved_ts TEXT, sent_ts TEXT, ack_ts TEXT, digest TEXT);
CREATE TABLE IF NOT EXISTS review(
  review_id TEXT PRIMARY KEY, item TEXT, subject TEXT, base_sha TEXT,
  candidate_sha TEXT, reviewer_role TEXT, reviewer_ref TEXT, independent INTEGER,
  verdict TEXT, findings TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS proof(
  proof_id TEXT PRIMARY KEY, candidate_sha TEXT, base_sha TEXT, kind TEXT,
  pipeline_id TEXT, workflow_id TEXT, status TEXT, counts TEXT,
  artifacts_path TEXT, ts_req TEXT, ts_done TEXT);
CREATE TABLE IF NOT EXISTS promotion(
  promo_id TEXT PRIMARY KEY, items TEXT, union_sha TEXT, base_sha TEXT,
  review_id TEXT, proof_id TEXT, status TEXT, expected_ref TEXT,
  actual_ref TEXT, reason TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS violation(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, from_role TEXT,
  to_role TEXT, payload_sha TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS job(
  job_id TEXT PRIMARY KEY, item TEXT, kind TEXT, ext_id TEXT, status TEXT,
  started_ts TEXT, last_seen_ts TEXT, deadline_ts TEXT);
CREATE TABLE IF NOT EXISTS event(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, mono REAL, run TEXT,
  item TEXT, attempt TEXT, role TEXT, kind TEXT, from_role TEXT, to_role TEXT,
  ref TEXT, detail TEXT, idem TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS message(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, direction TEXT, from_role TEXT,
  to_role TEXT, type TEXT, ref TEXT, body_sha TEXT, session_ref TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS counter(name TEXT PRIMARY KEY, value INTEGER);
CREATE TABLE IF NOT EXISTS union_(
  union_id TEXT PRIMARY KEY, no INTEGER, items TEXT, union_sha TEXT,
  base_sha TEXT, worktree TEXT, branch TEXT, status TEXT, note TEXT, ts TEXT);
CREATE INDEX IF NOT EXISTS event_item ON event(item);
CREATE INDEX IF NOT EXISTS attempt_item ON attempt(item);
CREATE INDEX IF NOT EXISTS review_item ON review(item);
"""


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

class Store:
    """The authority store.  All mutations go through one transaction each."""

    def __init__(self, root: str | None = None):
        self.root = root or run_root()
        os.makedirs(self.root, exist_ok=True)
        self.db_path = os.path.join(self.root, "state.db")
        self.events_path = os.path.join(self.root, "events.jsonl")
        self.escalations_path = os.path.join(self.root, "escalations.jsonl")
        self.messages_path = os.path.join(self.root, "messages.jsonl")
        self.violations_path = os.path.join(self.root, "violations.jsonl")
        self.db = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)  # DDL is autocommitted; never inside a tx
        self._migrate()
        self._pending_lines: list[tuple[str, str]] = []
        self._depth = 0

    # v12 columns added to v9 tables (idempotent ALTERs)
    _MIGRATIONS = (
        ("attempt", "pid", "INTEGER"), ("attempt", "driver_pid", "INTEGER"),
        ("attempt", "runner", "TEXT"), ("attempt", "cache_write", "INTEGER"),
        ("attempt", "detail", "TEXT"),
        ("item", "paused_from", "TEXT"), ("item", "note", "TEXT"),
        ("item", "pinned_server", "TEXT"), ("item", "union_id", "TEXT"),
        ("item", "critical", "INTEGER"),
    )

    def _migrate(self):
        for table, col, typ in self._MIGRATIONS:
            cols = {r["name"] for r in self.q(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    # -- low level ---------------------------------------------------------

    @contextlib.contextmanager
    def _raw_tx(self):
        deadline = time.monotonic() + 15.0
        while True:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.02)
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    @contextlib.contextmanager
    def tx(self):
        """One transaction boundary; queued jsonl lines are flushed before COMMIT."""
        if self._depth:
            yield
            return
        self._depth = 1
        self._pending_lines = []
        try:
            with self._raw_tx():
                yield
                # DB rows are already written; now the file lines.  A file
                # failure raises and the surrounding transaction rolls back.
                for path, line in self._pending_lines:
                    append_line(path, line)
        finally:
            self._pending_lines = []
            self._depth = 0

    def q(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def ex(self, sql, args=()):
        return self.db.execute(sql, args)

    def next_id(self, name: str) -> str:
        self.ex(
            "INSERT INTO counter(name,value) VALUES(?,1) "
            "ON CONFLICT(name) DO UPDATE SET value=value+1",
            (name,),
        )
        n = self.q1("SELECT value FROM counter WHERE name=?", (name,))["value"]
        return f"{name}-{n:05d}"

    # -- run helpers -------------------------------------------------------

    def current_run(self):
        return self.q1("SELECT * FROM run ORDER BY started_ts DESC LIMIT 1")

    def run_id(self) -> str:
        r = self.current_run()
        return r["run_id"] if r else "-"

    def roster(self) -> dict:
        path = os.path.join(self.root, "roster.json")
        data = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        data.setdefault("round_cap", 8)
        data.setdefault("wip_per_group", 2)
        return data

    # -- event writer ------------------------------------------------------

    def event(self, kind, *, item=None, attempt=None, role=None, from_role=None,
              to_role=None, ref=None, detail=None, idempotent=False,
              escalate=False):
        """Append one event row (+ jsonl mirror).  Returns (seq, created).

        Idempotency key is (item, attempt, kind, ref) and is applied only when
        `idempotent` is set and all four parts are present; re-applying the same
        event then returns the existing seq and writes nothing.
        """
        idem = None
        if idempotent and item and attempt and kind and ref:
            idem = f"{item}|{attempt}|{kind}|{ref}"
            row = self.q1("SELECT seq FROM event WHERE idem=?", (idem,))
            if row:
                return row["seq"], False
        ts = now_ts()
        m = mono()
        detail_json = json.dumps(detail, sort_keys=True) if detail is not None else None
        try:
            cur = self.ex(
                "INSERT INTO event(ts,mono,run,item,attempt,role,kind,from_role,"
                "to_role,ref,detail,idem) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, m, self.run_id(), item, attempt, role, kind, from_role,
                 to_role, ref, detail_json, idem),
            )
        except sqlite3.IntegrityError:
            row = self.q1("SELECT seq FROM event WHERE idem=?", (idem,))
            if row:
                return row["seq"], False
            raise
        seq = cur.lastrowid
        rec = {
            "ts": ts, "mono": m, "seq": seq, "run": self.run_id(), "item": item,
            "attempt": attempt, "role": role, "kind": kind, "from": from_role,
            "to": to_role, "ref": ref, "detail": detail,
        }
        line = json.dumps(rec, sort_keys=True) + "\n"
        self._pending_lines.append((self.events_path, line))
        if kind in ESCALATION_KINDS or escalate:
            self._pending_lines.append((self.escalations_path, line))
        return seq, True

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def run_init(self, run_id, trunk_head=None):
        with self.tx():
            if self.q1("SELECT 1 FROM run WHERE run_id=?", (run_id,)):
                raise Conflict(f"run {run_id} already initialised")
            self.ex("INSERT INTO run(run_id,started_ts,status,trunk_head) VALUES(?,?,?,?)",
                    (run_id, now_ts(), "RUNNING", trunk_head))
            self.event("RUN_INIT", ref=run_id, detail={"trunk_head": trunk_head})
        return run_id

    def _require_run(self):
        r = self.current_run()
        if not r:
            raise Refused("no run initialised (vpctl run init)")
        return r

    def run_status(self):
        r = self._require_run()
        return dict(r)

    def run_stop(self, note=None):
        with self.tx():
            r = self._require_run()
            self.ex("UPDATE run SET status=? WHERE run_id=?", ("STOPPING", r["run_id"]))
            self.event("STOP", ref=r["run_id"], detail={"note": note})
        return "STOPPING"

    def run_inhibit_set(self, reason=None):
        with self.tx():
            r = self._require_run()
            self.ex("UPDATE run SET status=? WHERE run_id=?", ("INHIBITED", r["run_id"]))
            self.event("INHIBIT", ref=r["run_id"], detail={"reason": reason, "state": "set"})
        return "INHIBITED"

    def run_inhibit_clear(self, reason=None):
        with self.tx():
            r = self._require_run()
            if r["status"] == "STOPPING":
                raise Refused("run is STOPPING; inhibit cannot be cleared")
            self.ex("UPDATE run SET status=? WHERE run_id=?", ("RUNNING", r["run_id"]))
            self.event("INHIBIT", ref=r["run_id"], detail={"reason": reason, "state": "clear"})
        return "RUNNING"

    def run_checkpoint(self, role, note):
        with self.tx():
            self._require_run()
            seq, _ = self.event("CHECKPOINT", role=role, detail={"note": note})
        return seq

    # ------------------------------------------------------------------
    # role
    # ------------------------------------------------------------------

    ROLE_KINDS = {"architect", "boss", "cos", "recovery", "senior", "integrator",
                  "quickie", "builder", "junior", "infra", "daemon"}

    def role_add(self, role_id, kind, session_ref=None, model=None, server=None,
                 group_no=None):
        if kind not in self.ROLE_KINDS:
            raise Usage(f"unknown role kind {kind}")
        with self.tx():
            if self.q1("SELECT 1 FROM role WHERE role_id=?", (role_id,)):
                raise Conflict(f"role {role_id} exists")
            self.ex("INSERT INTO role(role_id,kind,session_ref,model,server,group_no,"
                    "available,ts) VALUES(?,?,?,?,?,?,0,?)",
                    (role_id, kind, session_ref, model, server, group_no, now_ts()))
            self.event("ROLE_ADD", role=role_id, ref=role_id, detail={"kind": kind})
        return role_id

    def mark_available(self, role_id):
        with self.tx():
            if not self.q1("SELECT 1 FROM role WHERE role_id=?", (role_id,)):
                raise Refused(f"unknown role {role_id}")
            self.ex("UPDATE role SET available=1 WHERE role_id=?", (role_id,))
            self.event("AVAILABLE", role=role_id, ref=role_id)
        return role_id

    def role_kind(self, role_id):
        row = self.q1("SELECT kind FROM role WHERE role_id=?", (role_id,))
        return row["kind"] if row else None

    # ------------------------------------------------------------------
    # packet
    # ------------------------------------------------------------------

    def packet_submit(self, item, benchmark, packet, base_sha, submitted_by=None,
                      critical=False, allowed_files=None):
        with self.tx():
            self._require_run()
            pid = self.next_id("packet")
            self.ex("INSERT INTO packet(packet_id,item,rev,submitted_by,benchmark_path,"
                    "packet_path,base_sha,critical,allowed_files,status,ts) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, item, 1, submitted_by, benchmark, packet, base_sha,
                     1 if critical else 0,
                     json.dumps(allowed_files or []), "DRAFT", now_ts()))
            self.event("PACKET_SUBMIT", item=item, ref=pid,
                       detail={"base": base_sha, "critical": bool(critical)})
        return pid

    def packet_ready(self, packet_id):
        with self.tx():
            p = self.q1("SELECT * FROM packet WHERE packet_id=?", (packet_id,))
            if not p:
                raise Refused(f"unknown packet {packet_id}")
            if p["status"] == "BLOCKED":
                raise Refused(f"packet {packet_id} is BLOCKED")
            self.ex("UPDATE packet SET status='READY' WHERE packet_id=?", (packet_id,))
            it = self.q1("SELECT * FROM item WHERE item=?", (p["item"],))
            if not it:
                self.ex("INSERT INTO item(item,rev,status,round,base_sha,ts) "
                        "VALUES(?,?,?,?,?,?)",
                        (p["item"], 1, "READY", 0, p["base_sha"], now_ts()))
            self.event("PACKET_READY", item=p["item"], ref=packet_id)
        return packet_id

    def packet_block(self, packet_id, reason):
        with self.tx():
            p = self.q1("SELECT * FROM packet WHERE packet_id=?", (packet_id,))
            if not p:
                raise Refused(f"unknown packet {packet_id}")
            self.ex("UPDATE packet SET status='BLOCKED' WHERE packet_id=?", (packet_id,))
            self.event("BLOCKED", item=p["item"], ref=packet_id, detail={"reason": reason})
        return packet_id

    def newest_ready_packet(self, item):
        """The newest READY packet row for an item, or None."""
        return self.q1(
            "SELECT * FROM packet WHERE item=? AND status='READY' "
            "ORDER BY ts DESC, packet_id DESC LIMIT 1", (item,))

    def packet_fields(self, item) -> dict:
        """The packet columns joined onto an item row (null when no READY packet)."""
        p = self.newest_ready_packet(item)
        if not p:
            return {"packet_id": None, "packet_path": None, "benchmark_path": None,
                    "packet_rev": None, "critical": None, "allowed_files": None}
        return {"packet_id": p["packet_id"], "packet_path": p["packet_path"],
                "benchmark_path": p["benchmark_path"], "packet_rev": p["rev"],
                "critical": bool(p["critical"]),
                "allowed_files": _jload(p["allowed_files"], [])}

    def packet_show(self, item) -> dict:
        """The newest READY packet for an item (refuses when there is none)."""
        p = self.newest_ready_packet(item)
        if not p:
            raise Refused(f"item {item} has no READY packet")
        row = dict(p)
        row["allowed_files"] = _jload(p["allowed_files"], [])
        row["critical"] = bool(p["critical"])
        return row

    # ------------------------------------------------------------------
    # escalations
    # ------------------------------------------------------------------

    ESCALATE_KINDS = {"STUCK", "STALLED", "QUOTA", "AUTH", "INCOMPLETE", "BLOCKED",
                      "ROUND_CAP", "DELIVERY_INCIDENT", "INHIBIT"} | RUNNER_ESCALATIONS

    def escalate(self, kind, item, attempt=None, detail=None):
        """Write one escalation event (the driver calls this)."""
        if kind not in self.ESCALATE_KINDS:
            raise Usage(f"kind must be one of {sorted(self.ESCALATE_KINDS)}")
        with self.tx():
            self.get_item(item)
            if attempt and not self.q1(
                    "SELECT 1 FROM attempt WHERE attempt_id=?", (attempt,)):
                raise Refused(f"unknown attempt {attempt}")
            seq, _ = self.event(kind, item=item, attempt=attempt,
                                ref=attempt or item, detail={"detail": detail},
                                escalate=True)
        return seq

    # ------------------------------------------------------------------
    # item state machine
    # ------------------------------------------------------------------

    def get_item(self, item):
        row = self.q1("SELECT * FROM item WHERE item=?", (item,))
        if not row:
            raise Refused(f"unknown item {item}")
        return row

    def _set_item(self, item, new_status, expected_rev=None, **fields):
        """Optimistic item update; bumps rev.  Must be called inside a tx."""
        row = self.get_item(item)
        if expected_rev is not None and int(expected_rev) != int(row["rev"]):
            raise Conflict(
                f"item {item} rev is {row['rev']}, expected {expected_rev}")
        if new_status is not None and new_status != row["status"]:
            allowed = ITEM_TRANSITIONS.get(row["status"], set())
            if new_status not in allowed:
                raise Refused(
                    f"item {item}: illegal transition {row['status']} -> {new_status}")
        sets, args = ["rev=rev+1", "ts=?"], [now_ts()]
        if new_status is not None:
            sets.append("status=?")
            args.append(new_status)
            # leaving the union path (rework / block / re-queue) drops the
            # union membership; the union row keeps its history.
            if new_status in ("GRADING", "BUILDING", "BLOCKED", "READY") and \
                    "union_id" not in fields and row["union_id"]:
                sets.append("union_id=NULL")
                self.ex("UPDATE union_ SET status='FAILED',note=COALESCE(note,'')||? "
                        "WHERE union_id=? AND status NOT IN ('APPROVED','PROMOTED')",
                        (f" item {item} -> {new_status};", row["union_id"]))
        for k, v in fields.items():
            sets.append(f"{k}=?")
            args.append(v)
        args.append(item)
        self.ex(f"UPDATE item SET {', '.join(sets)} WHERE item=?", args)
        return row["status"]

    # -- assign ------------------------------------------------------------

    def assign(self, item, group_no, wip_check=True):
        with self.tx():
            r = self._require_run()
            if stop_file_exists(self.root):
                self.event("STOP", item=item, detail={"where": "assign",
                                                      "file": stop_file_path(self.root)})
                raise Refused("STOP file present; assign refused")
            if r["status"] in ("INHIBITED", "STOPPING", "STOPPED"):
                self.event("INHIBIT", item=item,
                           detail={"where": "assign", "run_status": r["status"]})
                raise Refused(f"run is {r['status']}; assign refused")
            cap = int(self.roster().get("wip_per_group", 2))
            if wip_check:
                n = self.q1(
                    "SELECT COUNT(*) c FROM item WHERE group_no=? AND status IN "
                    "('ASSIGNED','BUILDING','GRADING','JUNIOR_SATISFIED')",
                    (group_no,))["c"]
                if n >= cap:
                    raise Refused(
                        f"group {group_no} has {n} in-flight items (cap {cap})")
            self._set_item(item, "ASSIGNED", group_no=group_no)
            self.event("ASSIGN", item=item, ref=str(group_no),
                       detail={"group": group_no})
        return self.get_item(item)["status"]

    # -- claim -------------------------------------------------------------

    def claim(self, item, role, worktree, expected_rev):
        with self.tx():
            if stop_file_exists(self.root):
                self.event("STOP", item=item, detail={"where": "claim"})
                raise Refused("STOP file present; claim refused")
            r = self._require_run()
            if r["status"] in ("STOPPING", "STOPPED", "INHIBITED"):
                raise Refused(f"run is {r['status']}; claim refused")
            row = self.get_item(item)
            if row["builder_role"] and row["builder_role"] != role:
                raise Refused(
                    f"item {item} already claimed by {row['builder_role']}; "
                    f"{role} refused (unique writer per item)")
            self._set_item(item, "BUILDING", expected_rev=expected_rev,
                           builder_role=role, worktree=worktree)
            self.event("CLAIM", item=item, role=role, ref=role,
                       detail={"worktree": worktree})
        return self.get_item(item)["status"]

    # -- turns -------------------------------------------------------------

    def turn_start(self, item, kind, session=None, server=None, agent=None,
                   model=None, variant=None, pid=None, driver_pid=None,
                   runner=None):
        if kind not in TURN_KINDS:
            raise Usage(f"bad turn kind {kind}")
        with self.tx():
            self.get_item(item)
            running = self.q1("SELECT attempt_id FROM attempt WHERE item=? AND "
                              "status='RUNNING'", (item,))
            if running:
                raise Conflict(f"item {item} already has RUNNING attempt "
                               f"{running['attempt_id']}")
            n = self.q1("SELECT COALESCE(MAX(n),0)+1 n FROM attempt WHERE item=?",
                        (item,))["n"]
            aid = self.next_id("attempt")
            self.ex("INSERT INTO attempt(attempt_id,item,n,kind,session_id,server,"
                    "agent,model,variant,status,started_ts,pid,driver_pid,runner) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, item, n, kind, session, server, agent, model, variant,
                     "RUNNING", now_ts(), pid, driver_pid, runner))
            self.event("TURN_START", item=item, attempt=aid, ref=aid,
                       detail={"kind": kind, "n": n, "server": server,
                               "model": model, "runner": runner})
        return aid

    def turn_end(self, attempt_id, status, result=None, tokens_in=None,
                 tokens_out=None, tokens_reason=None, cache_read=None, cost=None,
                 cache_write=None, detail=None, session=None):
        if status not in ATTEMPT_STATUSES:
            raise Usage(f"bad attempt status {status}")
        with self.tx():
            a = self.q1("SELECT * FROM attempt WHERE attempt_id=?", (attempt_id,))
            if not a:
                raise Refused(f"unknown attempt {attempt_id}")
            if a["status"] != "RUNNING":
                raise Conflict(f"attempt {attempt_id} already ended ({a['status']})")
            self.ex("UPDATE attempt SET status=?,ended_ts=?,result_path=?,tokens_in=?,"
                    "tokens_out=?,tokens_reason=?,cache_read=?,cost=?,cache_write=?,"
                    "detail=?,session_id=COALESCE(?,session_id) WHERE attempt_id=?",
                    (status, now_ts(), result, tokens_in, tokens_out, tokens_reason,
                     cache_read, cost, cache_write, detail, session, attempt_id))
            self.event("TURN_END", item=a["item"], attempt=attempt_id, ref=attempt_id,
                       detail={"status": status, "cost": cost, "kind": a["kind"],
                               "detail": (detail or "")[:300]})
            if status in ESCALATION_KINDS:
                self.event(status, item=a["item"], attempt=attempt_id,
                           ref=attempt_id, detail={"from": "turn end",
                                                   "detail": (detail or "")[:300]})
        return attempt_id

    # -- v12 item verbs (K-07) ---------------------------------------------

    def item_pause(self, item, note=None):
        with self.tx():
            row = self.get_item(item)
            if row["status"] in ("PAUSED", "PROMOTED", "STOPPED"):
                raise Refused(f"item {item} is {row['status']}; pause refused")
            self._set_item(item, "PAUSED", paused_from=row["status"], note=note)
            self.event("PAUSE", item=item, detail={"from": row["status"], "note": note})
        return "PAUSED"

    def item_resume(self, item):
        with self.tx():
            row = self.get_item(item)
            if row["status"] != "PAUSED":
                raise Refused(f"item {item} is {row['status']}, not PAUSED")
            back = row["paused_from"] or "READY"
            self._set_item(item, back, paused_from=None)
            self.event("RESUME", item=item, detail={"to": back})
        return back

    def item_unblock(self, item, note=None, to="READY"):
        if to not in ("READY", "GRADING"):
            raise Usage("unblock target must be READY|GRADING")
        with self.tx():
            row = self.get_item(item)
            if row["status"] != "BLOCKED":
                raise Refused(f"item {item} is {row['status']}, not BLOCKED")
            self._set_item(item, to, note=note)
            self.event("UNBLOCK", item=item, detail={"to": to, "note": note})
        return to

    def item_block(self, item, reason):
        with self.tx():
            row = self.get_item(item)
            if row["status"] in ("BLOCKED", "PROMOTED", "STOPPED"):
                raise Refused(f"item {item} is {row['status']}; block refused")
            self._set_item(item, "BLOCKED", note=reason)
            self.event("BLOCKED", item=item, ref=item, detail={"reason": reason},
                       escalate=True)
        return "BLOCKED"

    def item_unassign(self, item):
        with self.tx():
            row = self.get_item(item)
            if row["status"] != "ASSIGNED":
                raise Refused(f"item {item} is {row['status']}, not ASSIGNED")
            self._set_item(item, "READY", group_no=None, builder_role=None)
            self.event("UNASSIGN", item=item)
        return "READY"

    def item_pin(self, item, server):
        with self.tx():
            self.get_item(item)
            self.ex("UPDATE item SET pinned_server=? WHERE item=?", (server, item))
            self.event("PIN", item=item, ref=server)
        return server

    def run_resume(self, note=None):
        """Owner's explicit word: STOPPED/STOPPING -> RUNNING (never automatic)."""
        with self.tx():
            r = self._require_run()
            if r["status"] == "RUNNING":
                raise Conflict("run is already RUNNING")
            if stop_file_exists(self.root):
                raise Refused(f"STOP file present ({stop_file_path(self.root)}); remove it first")
            self.ex("UPDATE run SET status='RUNNING' WHERE run_id=?", (r["run_id"],))
            self.event("RUN_RESUME", ref=r["run_id"], detail={"note": note,
                                                             "from": r["status"]})
        return "RUNNING"

    def run_finish(self, reason=None):
        with self.tx():
            r = self._require_run()
            self.ex("UPDATE run SET status=? WHERE run_id=?", ("STOPPED", r["run_id"]))
            self.event("RUN_FINISH", ref=r["run_id"], detail={"reason": reason})
        return "STOPPED"

    def alert(self, kind, text, item=None):
        """K-16: one line in OWNER-ALERTS.md, clock-stamped by the store, plus an
        event.  Never carries a secret value: callers pass account NAMES."""
        ts = now_ts()
        line = f"- {ts} **{kind}**" + (f" `{item}`" if item else "") + f" — {text}\n"
        with self.tx():
            self.event("ALERT", item=item, ref=kind, detail={"text": text[:500]})
            self._pending_lines.append((os.path.join(self.root, "OWNER-ALERTS.md"),
                                        line))
        return ts

    def reconcile(self, live_pids=None):
        """K-06: every RUNNING attempt whose pid is dead (or whose driver is
        dead) becomes ORPHANED; returns what changed.  Never touches an
        attempt whose pid is in `live_pids` (the calling driver's own)."""
        live_pids = set(int(p) for p in (live_pids or ()))
        changed = []
        rows = self.q("SELECT * FROM attempt WHERE status='RUNNING'")
        for a in rows:
            pid = a["pid"]
            dpid = a["driver_pid"]
            alive = False
            if pid and int(pid) in live_pids:
                alive = True
            elif pid and _pid_alive(int(pid)):
                alive = True
            if alive and dpid and not _pid_alive(int(dpid)) and int(dpid) != os.getpid():
                alive = False   # a child of a dead driver is nobody's
            if alive:
                continue
            with self.tx():
                self.ex("UPDATE attempt SET status='ORPHANED',ended_ts=?,detail=? "
                        "WHERE attempt_id=? AND status='RUNNING'",
                        (now_ts(), f"reconcile: pid={pid} driver={dpid} dead",
                         a["attempt_id"]))
                self.event("ORPHANED", item=a["item"], attempt=a["attempt_id"],
                           ref=a["attempt_id"], detail={"pid": pid, "driver_pid": dpid})
            changed.append({"attempt": a["attempt_id"], "item": a["item"],
                            "kind": a["kind"], "pid": pid})
        return {"orphaned": changed}

    # -- union (K-21) --------------------------------------------------------

    def union_record(self, items, union_sha, base_sha, worktree=None, branch=None,
                     note=None):
        """The integrator built union N = base + items.  Each item must be
        PREPARING (senior approved); its candidate becomes the union sha so
        proof and final review address the exact tree that ships."""
        with self.tx():
            self._require_run()
            for it in items:
                row = self.get_item(it)
                if row["status"] != "PREPARING":
                    raise Refused(f"item {it} is {row['status']}, not PREPARING")
            no = (self.q1("SELECT COALESCE(MAX(no),0)+1 n FROM union_")["n"])
            uid = f"union-{no}"
            self.ex("INSERT INTO union_(union_id,no,items,union_sha,base_sha,worktree,"
                    "branch,status,note,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (uid, no, json.dumps(list(items)), union_sha, base_sha, worktree,
                     branch, "BUILT", note, now_ts()))
            for it in items:
                self.ex("UPDATE item SET candidate_sha=?,union_id=?,rev=rev+1,ts=? "
                        "WHERE item=?", (union_sha, uid, now_ts(), it))
            self.event("UNION", ref=uid, detail={"items": list(items),
                                                 "union": union_sha, "base": base_sha})
        return uid

    def union_status(self, union_id, status, note=None):
        with self.tx():
            u = self.q1("SELECT * FROM union_ WHERE union_id=?", (union_id,))
            if not u:
                raise Refused(f"unknown union {union_id}")
            self.ex("UPDATE union_ SET status=?,note=COALESCE(?,note) WHERE union_id=?",
                    (status, note, union_id))
            self.event("UNION_STATUS", ref=union_id, detail={"status": status,
                                                             "note": note})
        return status

    def unions(self, status=None):
        if status:
            rows = self.q("SELECT * FROM union_ WHERE status=? ORDER BY no", (status,))
        else:
            rows = self.q("SELECT * FROM union_ ORDER BY no")
        out = []
        for r in rows:
            d = dict(r)
            d["items"] = _jload(r["items"], [])
            out.append(d)
        return out

    # -- results / findings ------------------------------------------------

    def submit_result(self, item, commit, result_path):
        with self.tx():
            self._set_item(item, "GRADING", candidate_sha=commit)
            self.event("SUBMIT_RESULT", item=item, ref=commit,
                       detail={"result": result_path})
        return self.get_item(item)["status"]

    @staticmethod
    def validate_findings(doc) -> tuple[bool, str]:
        """Hand-written checker for FINDINGS.json (no jsonschema dependency)."""
        if not isinstance(doc, dict):
            return False, "findings must be an object"
        for k in ("item", "lines"):
            if k not in doc:
                return False, f"findings missing {k}"
        if not isinstance(doc["lines"], list) or not doc["lines"]:
            return False, "findings.lines must be a non-empty list"
        kinds = {"invariant", "test", "negative", "forbidden", "evidence"}
        verdicts = {"PASS", "FAIL", "UNKNOWN"}
        for ln in doc["lines"]:
            if not isinstance(ln, dict):
                return False, "findings line must be an object"
            if ln.get("kind") not in kinds:
                return False, f"bad findings line kind {ln.get('kind')!r}"
            if ln.get("verdict") not in verdicts:
                return False, f"bad findings line verdict {ln.get('verdict')!r}"
            if "id" not in ln:
                return False, "findings line missing id"
        return True, ""

    @staticmethod
    def validate_result(doc) -> tuple[bool, str]:
        if not isinstance(doc, dict):
            return False, "result must be an object"
        for k in ("item", "commit", "base", "diff_stat", "checks"):
            if k not in doc:
                return False, f"result missing {k}"
        if not isinstance(doc["diff_stat"], dict):
            return False, "result.diff_stat must be an object"
        if not isinstance(doc["checks"], list):
            return False, "result.checks must be a list"
        return True, ""

    def findings(self, item, path):
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        ok, why = self.validate_findings(doc)
        if not ok:
            raise Usage(why)
        # all_pass is computed from the lines, never trusted from the file
        all_pass = all(ln["verdict"] == "PASS" for ln in doc["lines"])
        with self.tx():
            row = self.get_item(item)
            if row["status"] not in ("GRADING", "FINAL_REVIEW"):
                raise Refused(f"item {item} is {row['status']}; findings expects GRADING")
            cap = int(self.roster().get("round_cap", 8))
            rnd = int(row["round"] or 0)
            attempt = doc.get("attempt")
            self.event("FINDINGS", item=item, attempt=attempt,
                       ref=doc.get("commit") or row["candidate_sha"],
                       detail={"all_pass": all_pass, "round": rnd,
                               "lines": len(doc["lines"])},
                       idempotent=True)
            if all_pass:
                self._set_item(item, "JUNIOR_SATISFIED")
                new_status = "JUNIOR_SATISFIED"
            else:
                rnd += 1
                if rnd >= cap:
                    self.event("ROUND_CAP", item=item, ref=str(rnd),
                               detail={"round": rnd, "cap": cap})
                    self._set_item(item, "BLOCKED", round=rnd)
                    new_status = "BLOCKED"
                else:
                    self._set_item(item, "BUILDING", round=rnd)
                    new_status = "BUILDING"
        return {"all_pass": all_pass, "status": new_status,
                "round": self.get_item(item)["round"]}

    # ------------------------------------------------------------------
    # deliveries and messages (communication matrix)
    # ------------------------------------------------------------------

    def _matrix_ok(self, from_kind, to_kind, type_=None) -> bool:
        if type_ and str(type_).upper() == "STOP" and from_kind == "owner":
            return True  # STOP from owner accepted from any channel
        return to_kind in MATRIX.get(from_kind, set())

    def _kind_of(self, ref):
        """A role_id resolves to its kind; a bare kind name stays itself."""
        k = self.role_kind(ref)
        if k:
            return k
        if ref in MATRIX or ref in self.ROLE_KINDS or ref == "owner":
            return ref
        return None

    def record_violation(self, kind, from_role, to_role, detail, payload_sha=None):
        """Must be called inside a tx."""
        ts = now_ts()
        self.ex("INSERT INTO violation(ts,kind,from_role,to_role,payload_sha,detail) "
                "VALUES(?,?,?,?,?,?)", (ts, kind, from_role, to_role, payload_sha, detail))
        vid = self.q1("SELECT last_insert_rowid() r")["r"]
        rec = {"ts": ts, "id": vid, "kind": kind, "from": from_role,
               "to": to_role, "payload_sha": payload_sha, "detail": detail}
        self._pending_lines.append((self.violations_path,
                                    json.dumps(rec, sort_keys=True) + "\n"))
        self.event("VIOLATION", from_role=from_role, to_role=to_role,
                   ref=str(vid), detail={"kind": kind, "detail": detail})
        return vid

    def violation_record(self, kind, from_role, to_role, detail):
        with self.tx():
            return self.record_violation(kind, from_role, to_role, detail)

    def reserve_delivery(self, from_role, to_role, type_, ref):
        refusal = None
        with self.tx():
            fk, tk = self._kind_of(from_role), self._kind_of(to_role)
            if not fk or not tk or not self._matrix_ok(fk, tk, type_):
                # the violation row must survive the refusal, so it is committed
                # first and the Refused is raised after the transaction closes
                self.record_violation("MATRIX", from_role, to_role,
                                      f"pair {fk}->{tk} not in communication matrix")
                refusal = Refused(
                    f"pair {from_role}->{to_role} not in communication matrix")
                did = None
            else:
                did = self.next_id("delivery")
                self.ex("INSERT INTO delivery(delivery_id,from_role,to_role,type,"
                        "ref,status,reserved_ts) VALUES(?,?,?,?,?,?,?)",
                        (did, from_role, to_role, type_, ref, "RESERVED", now_ts()))
                self.event("DELIVERY_RESERVE", from_role=from_role, to_role=to_role,
                           ref=did, detail={"type": type_, "ref": ref})
        if refusal:
            raise refusal
        return did

    def delivery_status(self, delivery_id, status, digest=None):
        mapping = {"sent": "SENT", "ack": "ACK", "uncertain": "UNCERTAIN",
                   "fail": "FAILED", "consumed": "CONSUMED"}
        st = mapping.get(status, status)
        if st not in DELIVERY_STATUSES:
            raise Usage(f"bad delivery status {status}")
        with self.tx():
            d = self.q1("SELECT * FROM delivery WHERE delivery_id=?", (delivery_id,))
            if not d:
                raise Refused(f"unknown delivery {delivery_id}")
            col = {"SENT": "sent_ts", "ACK": "ack_ts"}.get(st)
            if col:
                self.ex(f"UPDATE delivery SET status=?,{col}=?,digest=COALESCE(?,digest) "
                        "WHERE delivery_id=?", (st, now_ts(), digest, delivery_id))
            else:
                self.ex("UPDATE delivery SET status=?,digest=COALESCE(?,digest) "
                        "WHERE delivery_id=?", (st, digest, delivery_id))
            self.event("DELIVERY_" + st, from_role=d["from_role"], to_role=d["to_role"],
                       ref=delivery_id)
            if st in ("UNCERTAIN", "FAILED"):
                self.event("DELIVERY_INCIDENT", from_role=d["from_role"],
                           to_role=d["to_role"], ref=delivery_id, detail={"status": st})
        return st

    def msg_log(self, direction, from_role, to_role, type_, ref, body_file=None,
                session_ref=None, result=None):
        if direction not in ("send", "recv"):
            raise Usage("direction must be send|recv")
        body_sha = None
        if body_file:
            body_sha = sha256_file(body_file)
        refusal, mid, env = None, None, None
        with self.tx():
            fk, tk = self._kind_of(from_role), self._kind_of(to_role)
            if not fk or not tk or not self._matrix_ok(fk, tk, type_):
                self.record_violation("MATRIX", from_role, to_role,
                                      f"pair {fk}->{tk} not in communication matrix",
                                      payload_sha=body_sha)
                refusal = Refused(
                    f"pair {from_role}->{to_role} not in communication matrix")
            else:
                # the sender id must match the role table when a session is named
                r = self.q1("SELECT session_ref FROM role WHERE role_id=?",
                            (from_role,)) if session_ref else None
                if r and r["session_ref"] and r["session_ref"] != session_ref:
                    self.record_violation("SENDER_ID", from_role, to_role,
                                          f"session {session_ref} is not {from_role}",
                                          payload_sha=body_sha)
                    refusal = Refused(f"session {session_ref} is not role {from_role}")
                else:
                    ts = now_ts()
                    self.ex("INSERT INTO message(ts,direction,from_role,to_role,type,"
                            "ref,body_sha,session_ref,result) VALUES(?,?,?,?,?,?,?,?,?)",
                            (ts, direction, from_role, to_role, type_, ref, body_sha,
                             session_ref, result))
                    mid = self.q1("SELECT last_insert_rowid() r")["r"]
                    env = (f"VP-ENVELOPE run={self.run_id()} from={from_role}:"
                           f"{session_ref or '-'} to={to_role} type={type_} "
                           f"ref={ref or '-'} ts={ts}")
                    rec = {"ts": ts, "id": mid, "direction": direction,
                           "from": from_role, "to": to_role, "type": type_,
                           "ref": ref, "body_sha": body_sha,
                           "session_ref": session_ref, "envelope": env}
                    self._pending_lines.append(
                        (self.messages_path, json.dumps(rec, sort_keys=True) + "\n"))
                    self.event("MSG_" + direction.upper(), from_role=from_role,
                               to_role=to_role, ref=str(mid), detail={"type": type_})
        if refusal:
            raise refusal
        return {"id": mid, "envelope": env}

    # ------------------------------------------------------------------
    # review
    # ------------------------------------------------------------------

    def review_record(self, item, subject, base, candidate, reviewer_role,
                      reviewer_ref, verdict, findings_path=None):
        if subject not in ("plan", "code"):
            raise Refused(f"wrong subject kind {subject!r} (plan|code)")
        if verdict not in ("APPROVED", "FINDINGS", "BLOCKED"):
            raise Usage(f"bad verdict {verdict}")
        findings = None
        if findings_path:
            with open(findings_path, "r", encoding="utf-8") as fh:
                findings = json.load(fh)
        with self.tx():
            row = self.get_item(item)
            if subject == "code":
                if not row["candidate_sha"]:
                    raise Refused(f"item {item} has no candidate; code review refused")
                if candidate != row["candidate_sha"]:
                    raise Refused(
                        f"stale candidate {candidate}: item {item} candidate is "
                        f"{row['candidate_sha']}")
                prior_plan = self.q1(
                    "SELECT reviewer_ref FROM review WHERE item=? AND subject='plan' "
                    "ORDER BY ts DESC LIMIT 1", (item,))
                if prior_plan and prior_plan["reviewer_ref"] == reviewer_ref:
                    raise Refused(
                        f"independence: {reviewer_ref} recorded the plan review of "
                        f"{item} and cannot record the final code review")
                independent = 1
            else:
                if row["status"] not in ("JUNIOR_SATISFIED", "PREPARING", "READY",
                                         "ASSIGNED", "BUILDING", "GRADING"):
                    raise Refused(f"item {item} is {row['status']}; plan review refused")
                independent = 0
            rid = self.next_id("review")
            self.ex("INSERT INTO review(review_id,item,subject,base_sha,candidate_sha,"
                    "reviewer_role,reviewer_ref,independent,verdict,findings,ts) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, item, subject, base, candidate, reviewer_role, reviewer_ref,
                     independent, verdict,
                     json.dumps(findings) if findings is not None else None, now_ts()))
            self.event("REVIEW", item=item, role=reviewer_role, ref=rid,
                       detail={"subject": subject, "verdict": verdict,
                               "independent": bool(independent)})
            # state machine
            if subject == "plan":
                self.ex("UPDATE item SET senior_plan=? WHERE item=?", (reviewer_role, item))
                if verdict == "APPROVED" and row["status"] == "JUNIOR_SATISFIED":
                    self._set_item(item, "PREPARING")
                elif verdict == "FINDINGS" and row["status"] == "JUNIOR_SATISFIED":
                    self._set_item(item, "GRADING")
                elif verdict == "BLOCKED":
                    self._set_item(item, "BLOCKED")
                    self.event("BLOCKED", item=item, ref=rid)
            else:
                self.ex("UPDATE item SET senior_final=? WHERE item=?", (reviewer_role, item))
                if verdict == "APPROVED":
                    if row["status"] != "FINAL_REVIEW":
                        raise Refused(
                            f"item {item} is {row['status']}; final approval needs "
                            "FINAL_REVIEW")
                    self._set_item(item, "APPROVED")
                elif verdict == "FINDINGS":
                    self._set_item(item, "GRADING")
                else:
                    self._set_item(item, "BLOCKED")
                    self.event("BLOCKED", item=item, ref=rid)
        return rid

    # ------------------------------------------------------------------
    # proof
    # ------------------------------------------------------------------

    def proof_request(self, candidate_sha, base, kind, paths=None):
        if kind not in ("targeted", "full"):
            raise Usage("proof kind must be targeted|full")
        with self.tx():
            pid = self.next_id("proof")
            self.ex("INSERT INTO proof(proof_id,candidate_sha,base_sha,kind,status,"
                    "counts,ts_req) VALUES(?,?,?,?,?,?,?)",
                    (pid, candidate_sha, base, kind, "REQUESTED",
                     json.dumps({"paths": paths or []}), now_ts()))
            for row in self.q("SELECT * FROM item WHERE candidate_sha=?", (candidate_sha,)):
                if row["status"] == "PREPARING":
                    self._set_item(row["item"], "PREPARED")
            self.event("PROOF_REQUEST", ref=pid,
                       detail={"candidate": candidate_sha, "kind": kind})
        return pid

    def proof_record(self, proof_id, status, counts=None, artifacts=None,
                     pipeline_id=None, workflow_id=None):
        if status not in PROOF_STATUSES:
            raise Usage(f"bad proof status {status}")
        with self.tx():
            p = self.q1("SELECT * FROM proof WHERE proof_id=?", (proof_id,))
            if not p:
                raise Refused(f"unknown proof {proof_id}")
            done = None if status in ("REQUESTED", "RUNNING") else now_ts()
            self.ex("UPDATE proof SET status=?,counts=COALESCE(?,counts),"
                    "artifacts_path=COALESCE(?,artifacts_path),"
                    "pipeline_id=COALESCE(?,pipeline_id),"
                    "workflow_id=COALESCE(?,workflow_id),ts_done=? WHERE proof_id=?",
                    (status, json.dumps(counts) if counts is not None else None,
                     artifacts, pipeline_id, workflow_id, done, proof_id))
            for row in self.q("SELECT * FROM item WHERE candidate_sha=?",
                              (p["candidate_sha"],)):
                st = row["status"]
                if status == "RUNNING" and st == "PREPARED":
                    self._set_item(row["item"], "PROOF_PENDING")
                elif status == "PASS" and st in ("PREPARED", "PROOF_PENDING"):
                    self._set_item(row["item"], "FINAL_REVIEW")
                elif status == "FAIL_PRODUCT" and st in ("PREPARED", "PROOF_PENDING"):
                    self._set_item(row["item"], "GRADING")
                    self.event("PROOF_FAIL", item=row["item"], ref=proof_id,
                               detail={"status": status}, escalate=True)
                elif status in ("FAIL_INFRA", "UNKNOWN", "CANCELLED") and st == "PROOF_PENDING":
                    self._set_item(row["item"], "PREPARED")
            self.event("PROOF_RECORD", ref=proof_id,
                       detail={"status": status, "counts": counts})
        return proof_id

    # ------------------------------------------------------------------
    # promotion
    # ------------------------------------------------------------------

    def _promotion_refuse(self, items, union_sha, base, review_id, proof_id, reason):
        """Record a REFUSED promotion row.  Inside a tx."""
        pid = self.next_id("promo")
        self.ex("INSERT INTO promotion(promo_id,items,union_sha,base_sha,review_id,"
                "proof_id,status,reason,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                (pid, json.dumps(items), union_sha, base, review_id, proof_id,
                 "REFUSED", reason, now_ts()))
        self.event("PROMOTE_REFUSED", ref=pid, detail={"reason": reason,
                                                       "items": items})
        return pid

    def promote_prepare(self, items, union_sha, base, review_id, proof_id):
        with self.tx():
            r = self._require_run()
            reason = None
            rv = self.q1("SELECT * FROM review WHERE review_id=?", (review_id,))
            pf = self.q1("SELECT * FROM proof WHERE proof_id=?", (proof_id,))
            if stop_file_exists(self.root):
                reason = "STOP file present"
            elif r["status"] in ("STOPPING", "INHIBITED", "STOPPED"):
                reason = f"run is {r['status']}"
            elif rv is None:
                reason = f"unknown review {review_id}"
            elif rv["subject"] != "code":
                reason = f"review {review_id} subject is {rv['subject']}, not code"
            elif rv["candidate_sha"] != union_sha:
                reason = (f"review candidate {rv['candidate_sha']} != union {union_sha}")
            elif rv["verdict"] != "APPROVED":
                reason = f"review verdict is {rv['verdict']}"
            elif not rv["independent"]:
                reason = "review is not independent"
            elif pf is None:
                reason = f"unknown proof {proof_id}"
            elif pf["status"] != "PASS":
                reason = f"proof status is {pf['status']}"
            elif pf["candidate_sha"] != union_sha:
                reason = f"proof candidate {pf['candidate_sha']} != union {union_sha}"
            else:
                for it in items:
                    row = self.q1("SELECT * FROM item WHERE item=?", (it,))
                    if row is None:
                        reason = f"unknown item {it}"
                        break
                    if row["status"] != "APPROVED":
                        reason = f"item {it} is {row['status']}, not APPROVED"
                        break
            if reason:
                # the REFUSED row is committed; the Refused is raised after
                pid = self._promotion_refuse(items, union_sha, base, review_id,
                                             proof_id, reason)
                refusal = Refused(f"promotion refused ({pid}): {reason}")
            else:
                refusal = None
                pid = self.next_id("promo")
                self.ex("INSERT INTO promotion(promo_id,items,union_sha,base_sha,"
                        "review_id,proof_id,status,ts) VALUES(?,?,?,?,?,?,?,?)",
                        (pid, json.dumps(items), union_sha, base, review_id, proof_id,
                         "INTENT", now_ts()))
                self.event("PROMOTE_INTENT", ref=pid,
                           detail={"items": items, "union": union_sha})
        if refusal:
            raise refusal
        return pid

    def promote_commit(self, promo_id, expected_ref, actual_ref=None):
        with self.tx():
            p = self.q1("SELECT * FROM promotion WHERE promo_id=?", (promo_id,))
            if not p:
                raise Refused(f"unknown promotion {promo_id}")
            if p["status"] == "COMMITTED":
                raise Conflict(f"promotion {promo_id} already COMMITTED")
            if p["status"] != "INTENT":
                raise Refused(f"promotion {promo_id} is {p['status']}, not INTENT")
            items = json.loads(p["items"])
            if actual_ref is None or actual_ref == expected_ref:
                self.ex("UPDATE promotion SET status='COMMITTED',expected_ref=?,"
                        "actual_ref=? WHERE promo_id=?",
                        (expected_ref, actual_ref or expected_ref, promo_id))
                for it in items:
                    self._set_item(it, "PROMOTED")
                self.event("PROMOTE_COMMITTED", ref=promo_id,
                           detail={"items": items, "ref": expected_ref})
                return "COMMITTED"
            self.ex("UPDATE promotion SET status='RECONCILE',expected_ref=?,"
                    "actual_ref=?,reason=? WHERE promo_id=?",
                    (expected_ref, actual_ref,
                     f"expected {expected_ref} but ref is {actual_ref}", promo_id))
            self.event("PROMOTE_RECONCILE", ref=promo_id,
                       detail={"expected": expected_ref, "actual": actual_ref})
            return "RECONCILE"

    def reconcilable_promotions(self):
        """INTENT rows are the crash window: recorded intent, no committed ref."""
        return [dict(r) for r in self.q(
            "SELECT * FROM promotion WHERE status IN ('INTENT','RECONCILE') "
            "ORDER BY ts")]

    # ------------------------------------------------------------------
    # reports / audit / render / seal
    # ------------------------------------------------------------------

    def report(self, what):
        if what == "pending":
            return {
                "items": [dict(r) for r in self.q(
                    "SELECT item,status,group_no,round,rev FROM item "
                    "WHERE status NOT IN ('PROMOTED','STOPPED') ORDER BY item")],
                "deliveries": [dict(r) for r in self.q(
                    "SELECT * FROM delivery WHERE status IN "
                    "('OUTBOX','RESERVED','SENT','UNCERTAIN') ORDER BY reserved_ts")],
                "promotions": self.reconcilable_promotions(),
                "proofs": [dict(r) for r in self.q(
                    "SELECT * FROM proof WHERE status IN ('REQUESTED','RUNNING')")],
            }
        if what == "liveness":
            return {"attempts_running": [dict(r) for r in self.q(
                "SELECT * FROM attempt WHERE status='RUNNING' ORDER BY started_ts")],
                "last_event": dict(self.q1(
                    "SELECT * FROM event ORDER BY seq DESC LIMIT 1") or {}),
                "run": dict(self.current_run() or {})}
        if what == "costs":
            rows = self.q(
                "SELECT item,COUNT(*) attempts,SUM(COALESCE(cost,0)) cost,"
                "SUM(COALESCE(tokens_in,0)) tokens_in,"
                "SUM(COALESCE(tokens_out,0)) tokens_out FROM attempt GROUP BY item")
            total = self.q1("SELECT SUM(COALESCE(cost,0)) c FROM attempt")["c"] or 0
            return {"by_item": [dict(r) for r in rows], "total_cost": total}
        if what == "violations":
            return {"violations": [dict(r) for r in self.q(
                "SELECT * FROM violation ORDER BY id")]}
        if what == "latency":
            rows = self.q("SELECT attempt_id,item,kind,started_ts,ended_ts,status "
                          "FROM attempt WHERE ended_ts IS NOT NULL ORDER BY started_ts")
            out = []
            for r in rows:
                d = dict(r)
                d["seconds"] = _iso_delta(r["started_ts"], r["ended_ts"])
                out.append(d)
            return {"attempts": out}
        if what == "idle":
            return {"roles": [dict(r) for r in self.q(
                "SELECT role_id,kind,available,group_no FROM role ORDER BY role_id")],
                "items_unassigned": [dict(r) for r in self.q(
                    "SELECT item,status FROM item WHERE status='READY'")]}
        if what == "items":
            rows = []
            running = {r["item"]: r["attempt_id"] for r in self.q(
                "SELECT item,attempt_id FROM attempt WHERE status='RUNNING'")}
            open_proofs = {}
            for r in self.q("SELECT candidate_sha,proof_id,status FROM proof WHERE "
                            "status IN ('REQUESTED','RUNNING')"):
                open_proofs[r["candidate_sha"]] = r["proof_id"]
            for r in self.q("SELECT * FROM item ORDER BY item"):
                d = dict(r)
                d.update(self.packet_fields(r["item"]))
                d["running_attempt"] = running.get(r["item"])
                d["open_proof"] = open_proofs.get(r["candidate_sha"])
                rows.append(d)
            return {"items": rows}
        if what == "summary":
            by = {r["status"]: r["c"] for r in self.q(
                "SELECT status,COUNT(*) c FROM item GROUP BY status")}
            runners = [dict(r) for r in self.q(
                "SELECT COALESCE(runner,agent,'?') runner,COUNT(*) attempts,"
                "SUM(COALESCE(cost,0)) cost,SUM(COALESCE(tokens_in,0)) tokens_in,"
                "SUM(COALESCE(tokens_out,0)) tokens_out,"
                "SUM(COALESCE(cache_read,0)) cache_read FROM attempt GROUP BY 1")]
            statuses = [dict(r) for r in self.q(
                "SELECT status,COUNT(*) c FROM attempt GROUP BY status ORDER BY c DESC")]
            alerts = self.q1("SELECT COUNT(*) c FROM event WHERE kind='ALERT'")["c"]
            return {"items_by_status": by, "attempts_by_runner": runners,
                    "attempts_by_status": statuses, "unions": self.unions(),
                    "alerts": alerts, "run": dict(self.current_run() or {})}
        raise Usage(f"unknown report {what}")

    def audit(self, item):
        """Chronological merge of every row that names the item, + file pointers."""
        trail = []
        for r in self.q("SELECT * FROM event WHERE item=? ORDER BY seq", (item,)):
            trail.append({"ts": r["ts"], "source": "event", "kind": r["kind"],
                          "ref": r["ref"], "detail": _jload(r["detail"]),
                          "seq": r["seq"]})
        for r in self.q("SELECT * FROM attempt WHERE item=? ORDER BY started_ts", (item,)):
            trail.append({"ts": r["started_ts"], "source": "attempt",
                          "kind": f"attempt {r['kind']} start", "ref": r["attempt_id"],
                          "detail": {"server": r["server"], "model": r["model"]}})
            if r["ended_ts"]:
                trail.append({"ts": r["ended_ts"], "source": "attempt",
                              "kind": f"attempt {r['kind']} end", "ref": r["attempt_id"],
                              "detail": {"status": r["status"],
                                         "result": r["result_path"]}})
        for r in self.q("SELECT * FROM review WHERE item=? ORDER BY ts", (item,)):
            trail.append({"ts": r["ts"], "source": "review",
                          "kind": f"review {r['subject']} {r['verdict']}",
                          "ref": r["review_id"],
                          "detail": {"reviewer": r["reviewer_ref"],
                                     "independent": r["independent"]}})
        row = self.q1("SELECT * FROM item WHERE item=?", (item,))
        cand = row["candidate_sha"] if row else None
        if cand:
            for r in self.q("SELECT * FROM proof WHERE candidate_sha=? ORDER BY ts_req",
                            (cand,)):
                trail.append({"ts": r["ts_req"], "source": "proof",
                              "kind": f"proof {r['kind']} {r['status']}",
                              "ref": r["proof_id"],
                              "detail": {"artifacts": r["artifacts_path"]}})
        for r in self.q("SELECT * FROM delivery WHERE ref=? ORDER BY reserved_ts", (item,)):
            trail.append({"ts": r["reserved_ts"], "source": "delivery",
                          "kind": f"delivery {r['status']}", "ref": r["delivery_id"],
                          "detail": {"from": r["from_role"], "to": r["to_role"]}})
        for r in self.q("SELECT * FROM message WHERE ref=? ORDER BY ts", (item,)):
            trail.append({"ts": r["ts"], "source": "message",
                          "kind": f"message {r['direction']} {r['type']}",
                          "ref": str(r["id"]),
                          "detail": {"from": r["from_role"], "to": r["to_role"]}})
        for r in self.q("SELECT * FROM promotion ORDER BY ts"):
            if item in _jload(r["items"], []):
                trail.append({"ts": r["ts"], "source": "promotion",
                              "kind": f"promotion {r['status']}", "ref": r["promo_id"],
                              "detail": {"reason": r["reason"]}})
        trail.sort(key=lambda e: (e["ts"], e.get("seq", 0)))
        pointers = {
            "turns": os.path.join(self.root, "turns", item),
            "reviews": os.path.join(self.root, "reviews", item),
            "proofs": os.path.join(self.root, "proofs", cand or "-"),
            "worktree": row["worktree"] if row else None,
        }
        return {"item": item, "state": dict(row) if row else None,
                "trail": trail, "files": pointers}

    def render(self):
        """Write LEDGER.md and TIMELINE-<item>.md into RUN_ROOT."""
        written = []
        run = self.current_run()
        lines = ["# LEDGER", "",
                 f"run: {run['run_id'] if run else '-'}  "
                 f"status: {run['status'] if run else '-'}",
                 f"generated: {now_ts()}", "",
                 "| item | status | group | round | base | candidate | builder |",
                 "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in self.q("SELECT * FROM item ORDER BY item"):
            lines.append(
                f"| {r['item']} | {r['status']} | {r['group_no'] or '-'} | "
                f"{r['round'] or 0} | {(r['base_sha'] or '-')[:12]} | "
                f"{(r['candidate_sha'] or '-')[:12]} | {r['builder_role'] or '-'} |")
        lines += ["", "## Promotions", "",
                  "| promo | status | items | union | reason |",
                  "| --- | --- | --- | --- | --- |"]
        for r in self.q("SELECT * FROM promotion ORDER BY ts"):
            lines.append(f"| {r['promo_id']} | {r['status']} | "
                         f"{','.join(_jload(r['items'], []))} | "
                         f"{(r['union_sha'] or '-')[:12]} | {r['reason'] or '-'} |")
        lines += ["", "## Violations", ""]
        vs = self.q("SELECT * FROM violation ORDER BY id")
        if not vs:
            lines.append("none")
        for r in vs:
            lines.append(f"- {r['ts']} {r['kind']} {r['from_role']}->{r['to_role']}: "
                         f"{r['detail']}")
        path = os.path.join(self.root, "LEDGER.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        written.append(path)
        for r in self.q("SELECT item FROM item ORDER BY item"):
            item = r["item"]
            a = self.audit(item)
            tl = [f"# TIMELINE {item}", "", f"generated: {now_ts()}", "",
                  f"status: {a['state']['status']}", "",
                  "| ts | source | kind | ref |", "| --- | --- | --- | --- |"]
            for e in a["trail"]:
                tl.append(f"| {e['ts']} | {e['source']} | {e['kind']} | "
                          f"{e['ref'] or '-'} |")
            tl += ["", "## Files", ""]
            for k, v in a["files"].items():
                tl.append(f"- {k}: {v or '-'}")
            p = os.path.join(self.root, f"TIMELINE-{item}.md")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("\n".join(tl) + "\n")
            written.append(p)
        with self.tx():
            self.event("RENDER", detail={"files": [os.path.basename(p) for p in written]})
        return written

    def seal(self):
        """sha256 manifest of every file under RUN_ROOT except the WAL sidecars."""
        skip = {"state.db-wal", "state.db-shm"}
        files = {}
        for dirpath, _dirs, names in os.walk(self.root):
            for n in sorted(names):
                if n in skip:
                    continue
                full = os.path.join(dirpath, n)
                rel = os.path.relpath(full, self.root)
                if rel.startswith("MANIFEST-") and rel.endswith(".json"):
                    continue
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                files[rel] = {"sha256": sha256_file(full),
                              "bytes": os.path.getsize(full)}
        ts = now_ts()
        run = self.current_run()
        manifest = {"run": run["run_id"] if run else "-", "ts": ts,
                    "root": self.root, "count": len(files), "files": files}
        name = f"MANIFEST-{ts.replace(':', '').replace('.', '-')}.json"
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
        with self.tx():
            self.event("SEAL", ref=name, detail={"count": len(files)})
        return path

    def close(self):
        with contextlib.suppress(Exception):
            self.db.close()


def _iso_delta(a: str, b: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    ta = datetime.datetime.strptime(a, fmt)
    tb = datetime.datetime.strptime(b, fmt)
    return round((tb - ta).total_seconds(), 3)


def open_store(root: str | None = None) -> Store:
    return Store(root)


if __name__ == "__main__":  # pragma: no cover - smoke only
    s = open_store()
    print(json.dumps({"root": s.root, "run": dict(s.current_run() or {})}, indent=2))
