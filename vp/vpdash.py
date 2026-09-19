#!/usr/bin/env python3
"""vpdash.py -- live dashboard for a v13 run, rendered from the sources only.

  python3 vp/vpdash.py --run-root <run_root> [--port 4180] [--bind 127.0.0.1]

One stdlib HTTP server, one HTML page with no external assets, a JSON API
behind it. A background thread refreshes a `vpjourney.Journey` (events,
run-state, packets, costs, proofs, alerts, CircleCI ledger) every few
seconds and the 250 MB control.jsonl on a slower cadence and by byte
offset; handlers only ever read the last finished snapshot.

Nothing here reads LEDGER.md for numbers: totals come from the event
stream grouped by ROOT work item (vpjourney), alert tiers from
vpalerts.annotate over alerts.jsonl, gate ages from the roster.N.json
snapshots, CircleCI usage from proofs/circleci-pipelines.jsonl and the
proof records. Every number on the page names the file it came from.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vpalerts   # noqa: E402
import vpjourney  # noqa: E402
try:
    import vpcircle  # noqa: E402
except Exception:  # pragma: no cover
    vpcircle = None

OPEN_RULING_RE = re.compile(r"PENDING|BLOCKED|needs", re.I)


def utc_now():
    return datetime.now(timezone.utc)


def age_s(ts, now=None):
    t = vpalerts.parse_ts(ts)
    if t is None:
        return None
    return int(((now or utc_now()).timestamp()) - t)


def fmt_age(s):
    if s is None:
        return "?"
    s = int(s)
    if s < 90:
        return "%ds" % s
    if s < 5400:
        return "%dm" % (s // 60)
    if s < 172800:
        return "%dh%02dm" % (s // 3600, (s % 3600) // 60)
    return "%dd%dh" % (s // 86400, (s % 86400) // 3600)


class Dash(object):
    """Holds the Journey and the derived snapshot; one refresh at a time."""

    def __init__(self, run_root, control_every_s=60, refresh_s=10):
        self.run_root = Path(run_root)
        self.j = vpjourney.Journey(run_root)
        self.lock = threading.Lock()
        self.snapshot = {}
        self.refresh_s = refresh_s
        self.control_every_s = control_every_s
        self._last_control = 0.0
        self._verify = None
        self._stop = threading.Event()
        self.errors = []

    # -- refresh -----------------------------------------------------------------------

    def refresh(self, force_control=False):
        t0 = time.time()
        with_control = force_control or (t0 - self._last_control >= self.control_every_s)
        try:
            self.j.refresh(control=False)
            if with_control:
                # startup reads the whole backlog; later ticks are bounded and the
                # rest is picked up next time
                cap = None if self._last_control == 0.0 else 64 << 20
                self.j.calls.extend(self.j.control.read_new(max_bytes=cap))
                self._last_control = t0
            snap = self.build()
            if with_control or self._verify is None:
                self._verify = self.j.verify()
            snap["verify"] = self._verify
            snap["refresh_ms"] = int((time.time() - t0) * 1000)
            with self.lock:
                self.snapshot = snap
        except Exception as exc:   # the page must keep serving the last good snapshot
            self.errors.append("%s %s" % (vpjourney.iso(utc_now()), str(exc)[:300]))
            del self.errors[:-20]

    def loop(self):
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.refresh_s)

    def start(self):
        self.refresh(force_control=True)
        th = threading.Thread(target=self.loop, name="vpdash-refresh", daemon=True)
        th.start()
        return th

    def stop(self):
        self._stop.set()

    # -- derived views ----------------------------------------------------------------------

    def build(self):
        j = self.j
        now = utc_now()
        roots = j.roots()
        summary = j.summary()
        backlog = self.backlog_series()
        alerts = self.alerts_view(backlog, now)
        return {
            "generated_at": vpjourney.iso(now),
            "run_root": str(self.run_root),
            "summary": summary,
            "freshness": self.freshness(now),
            "roots": [self.root_row(r) for r in roots.values()],
            "circleci": self.circleci_view(now),
            "alerts": alerts,
            "decisions": self.decisions_view(roots, alerts, now),
            "errors": list(self.errors),
            "sources": {
                "events": str(j.events.dir), "run_state": str(j.state_path),
                "packets": str(j.pack_dir), "catalog": str(j.catalog_path),
                "records": str(self.run_root / "packets"), "costs": str(j.costs.path),
                "control": str(j.control.path), "proofs": str(self.run_root / "proofs"),
                "pipelines": str(j.pipelines.path), "alerts": str(j.alerts.path),
                "roster_snapshots": str(self.run_root / "roster.*.json"),
            },
        }

    @staticmethod
    def root_row(r):
        return {k: r.get(k) for k in ("id", "kind", "title", "state", "primary_row", "primary_state",
                                       "hosted_state", "open_rows", "superseded_open", "attempts",
                                       "latest_row", "latest_ts", "settled", "done", "owed", "rows", "states")}

    def freshness(self, now):
        hb = vpjourney.Journey._load_json(self.run_root / "driver.heartbeat") or {}
        last_ev = None
        if self.j.events.by_seq:
            last_ev = self.j.events.by_seq[max(self.j.events.by_seq)].get("created_at")
        return {
            "heartbeat_ts": hb.get("ts"), "heartbeat_age_s": age_s(hb.get("ts"), now),
            "driver_pid": hb.get("pid"), "driver_tick": hb.get("tick"), "driver_active": hb.get("active"),
            "driver_idle_since": hb.get("idle_since"), "driver_sequence": hb.get("sequence"),
            "budget": hb.get("budget"), "runners": hb.get("runners"), "servers": hb.get("servers"),
            "code_version": hb.get("code_version"), "reloads": hb.get("reloads"),
            "state_sequence": (self.j.state or {}).get("sequence"),
            "state_updated_at": (self.j.state or {}).get("updated_at"),
            "events_sequence": max(self.j.events.by_seq) if self.j.events.by_seq else None,
            "last_event_ts": last_ev, "last_event_age_s": age_s(last_ev, now),
            "control_lines_read": len(self.j.calls), "control_offset": self.j.control.offset,
            "control_bad_lines": self.j.control.bad, "costs_bad_lines": self.j.costs.bad,
            "alerts_bad_lines": self.j.alerts.bad,
        }

    def backlog_series(self, max_points=400):
        """(ts, rows owed a ruling) replayed from the events: the signal
        vpalerts uses to raise otherwise-routine alerts."""
        state = {}
        pts = []
        for ev in self.j.events.ordered():
            t = ev.get("task_id")
            typ = ev.get("type")
            if not t:
                continue
            if typ in ("TASK_COMPLETED", "PRESERVED_RESULT_ADOPTED"):
                state[t] = ev.get("outcome")
            elif typ in vpjourney.TERMINAL_EVENT_STATE:
                state[t] = vpjourney.TERMINAL_EVENT_STATE[typ]
            elif typ in vpjourney.RUNNING_EVENTS:
                state[t] = "RUNNING"
            elif typ in ("TASK_UNBLOCKED", "TASK_INVALIDATED", "TEMPLATE_TASK_INSTANTIATED"):
                state[t] = "OPEN"
            else:
                continue
            owed = sum(1 for s in state.values() if s in vpjourney.OWED_STATES)
            pts.append((ev.get("created_at"), owed))
        if len(pts) > max_points:
            step = len(pts) / float(max_points)
            pts = [pts[int(i * step)] for i in range(max_points)] + [pts[-1]]
        return pts

    def alerts_view(self, backlog, now, tail=300):
        cfg = ((self.j.roster.get("alerts") or {}).get("severity")) if isinstance(self.j.roster.get("alerts"), dict) else None
        rows = list(self.j.alert_rows)
        annotated = []
        for a, res in vpalerts.annotate(rows, backlog=backlog, cfg=cfg):
            annotated.append((a, res))
        by_sev = Counter(res["severity"] for _a, res in annotated)
        by_kind = Counter(a.get("kind") for a in rows)
        # open conditions: the latest alert of each family, if still inside a streak gap
        gap = vpalerts.config(cfg)["streak_gap_s"]
        latest = OrderedDict()
        for a, res in annotated:
            latest[res["family"]] = (a, res)
        open_conditions = []
        for fam, (a, res) in latest.items():
            ag = age_s(a.get("ts"), now)
            if ag is not None and ag <= gap and res["severity"] != vpalerts.ROUTINE:
                open_conditions.append({"family": fam, "severity": res["severity"], "last_ts": a.get("ts"),
                                        "age_s": ag, "repeat_family": res["repeat_family"],
                                        "streak_s": res["age_s"], "reasons": res["reasons"],
                                        "task": a.get("task"), "text": (a.get("text") or "")[:300]})
        open_conditions.sort(key=lambda c: (vpalerts.TIERS.index(c["severity"]), -c["repeat_family"]), reverse=True)
        recent = []
        for a, res in annotated[-tail:]:
            recent.append({"ts": a.get("ts"), "kind": a.get("kind"), "task": a.get("task"),
                           "text": (a.get("text") or "")[:400], "severity": res["severity"],
                           "repeat": res["repeat"], "repeat_family": res["repeat_family"],
                           "streak_s": res["age_s"], "reasons": res["reasons"],
                           # what the driver itself wrote, if the D60 patch is live
                           "recorded_severity": a.get("severity")})
        recent.reverse()
        return {"total": len(rows), "by_severity": dict(by_sev), "by_kind": dict(by_kind.most_common()),
                "open_conditions": open_conditions, "recent": recent,
                "backlog_now": backlog[-1][1] if backlog else None, "backlog_points": len(backlog),
                "recorded_severity_present": any(a.get("severity") for a in rows[-50:])}

    def circleci_view(self, now):
        j = self.j
        cfg = ((j.roster.get("proof") or {}).get("circleci")) or {}
        accounts = list(vpcircle.TARGETS.keys()) if vpcircle else sorted({p.get("account") for p in j.pipeline_rows if p.get("account")})
        today = now.strftime("%Y-%m-%d")
        per = OrderedDict()
        for acc in accounts:
            tgt = (vpcircle.TARGETS.get(acc) if vpcircle else None) or {}
            per[acc] = {"account": acc, "org": tgt.get("org"), "repo": tgt.get("repo"),
                        "triggered_total": 0, "triggered_today": 0, "last_trigger_ts": None,
                        "refused_gate": 0, "refused_cap": 0, "credits_blocked": 0, "trigger_failed": 0,
                        "proofs": Counter(), "in_flight": [], "last_proof_ts": None, "last_proof_status": None}
        finished = {}   # proof_id -> proof record
        for pid, p in j.proofs.items():
            if p.get("route") == "circleci":
                finished[pid] = p
        for row in j.pipeline_rows:
            acc = row.get("account")
            if acc not in per:
                per[acc] = dict(per[accounts[0]], account=acc, org=None, repo=None, proofs=Counter(), in_flight=[]) if accounts else {}
                per[acc].update({"triggered_total": 0, "triggered_today": 0, "last_trigger_ts": None})
            st = row.get("status") or "triggered"
            a = per[acc]
            if st == "triggered":
                a["triggered_total"] += 1
                if (row.get("ts") or "")[:10] == today:
                    a["triggered_today"] += 1
                a["last_trigger_ts"] = max(a["last_trigger_ts"] or "", row.get("ts") or "")
                pr = finished.get(row.get("proof_id"))
                if pr is None:
                    a["in_flight"].append({"proof_id": row.get("proof_id"), "pipeline_id": row.get("pipeline_id"),
                                           "ts": row.get("ts"), "age_s": age_s(row.get("ts"), now), "sha": vpjourney.short(row.get("sha"))})
            elif st in ("refused_gate", "refused_cap", "credits_blocked", "trigger_failed"):
                a[st] += 1
        for pid, p in finished.items():
            acc = p.get("account")
            if acc in per:
                per[acc]["proofs"][p.get("status") or "?"] += 1
                if (p.get("ts") or "") > (per[acc]["last_proof_ts"] or ""):
                    per[acc]["last_proof_ts"], per[acc]["last_proof_status"] = p.get("ts"), p.get("status")
        unattributed = Counter(p.get("status") for p in finished.values() if p.get("account") not in per)
        for a in per.values():
            a["proofs"] = dict(a["proofs"])
            # a pipeline older than the roster deadline with no proof record is not in flight, it is lost
            dl = int(cfg.get("deadline_min") or 90) * 60 + 1800
            a["lost"] = [x for x in a["in_flight"] if (x["age_s"] or 0) > dl]
            a["in_flight"] = [x for x in a["in_flight"] if (x["age_s"] or 0) <= dl]
        gate_open = bool((j.roster.get("owner_gates") or {}).get("DELIVERY-1")) if isinstance(j.roster.get("owner_gates"), dict) else False
        total_today = sum(a["triggered_today"] for a in per.values())
        return {
            "enabled": cfg.get("enabled"), "mode": cfg.get("mode"), "gate": "DELIVERY-1", "gate_open": gate_open,
            "rotation": cfg.get("rotation"), "spread": cfg.get("spread"), "primary_account": cfg.get("account"),
            "max_pipelines_per_day": cfg.get("max_pipelines_per_day"), "max_pipelines_in_flight": cfg.get("max_pipelines_in_flight"),
            "triggered_today_total": total_today, "triggered_total": sum(a["triggered_total"] for a in per.values()),
            "in_flight_total": sum(len(a["in_flight"]) for a in per.values()),
            "accounts": list(per.values()),
            "proofs_unattributed": dict(unattributed),
            "ledger_has_status_field": any("status" in r for r in j.pipeline_rows),
            "note": ("pipelines ledger records successful triggers only (no status field yet): refused/failed "
                     "triggers are invisible until the laneproof patch lands") if not any("status" in r for r in j.pipeline_rows) else None,
        }

    def gate_history(self):
        """gate -> {value, since_ts, since_is_bound, snapshots} from roster.N.json mtimes"""
        snaps = []
        for f in glob.glob(str(self.run_root / "roster.*.json")):
            name = os.path.basename(f)
            if not re.match(r"^roster\.\d+\.json$", name):
                continue
            r = vpjourney.Journey._load_json(f)
            if isinstance(r, dict):
                snaps.append((os.path.getmtime(f), name, r.get("owner_gates") if isinstance(r.get("owner_gates"), dict) else {}))
        live = self.j.roster.get("owner_gates") if isinstance(self.j.roster.get("owner_gates"), dict) else {}
        try:
            live_m = os.path.getmtime(self.run_root / "roster.json")
        except OSError:
            live_m = time.time()
        snaps.sort()
        snaps.append((live_m, "roster.json", live))
        gates = sorted(k for k in live if not k.startswith("_"))
        out = OrderedDict()
        for g in gates:
            cur = bool(live.get(g))
            since, bound = None, False
            for m, name, og in reversed(snaps):
                v = bool(og.get(g)) if g in og else None
                if v == cur:
                    since = m
                    continue
                bound = True          # the previous snapshot had another value: `since` is exact
                break
            out[g] = {"gate": g, "open": cur, "since_ts": vpjourney.iso(datetime.fromtimestamp(since, timezone.utc)) if since else None,
                      "since_exact": bound, "age_s": int(time.time() - since) if since else None}
        return out, len(snaps) - 1

    def decisions_view(self, roots, alerts, now):
        j = self.j
        gates, n_snaps = self.gate_history()
        # rows held by a gate: an open row whose packet is gated by a CLOSED gate, or
        # whose blocker names the gate. An open gate holds nothing -- a BLOCKED twin
        # behind an open gate is held by its blocker reason (listed separately).
        held = defaultdict(list)
        blocked_by = Counter()
        blocked_rows = defaultdict(list)
        sup = j.superseded_rows()
        for task, row in j.tasks.items():
            if row.get("state") in vpjourney.CLOSED_STATES or task in sup:
                continue
            pid = j.packet_of(task) or ""
            p = j.pack.get(pid) or {}
            gate = p.get("twin_gate") or (p.get("owner_gate") if p.get("owner_gate") not in (None, "none") else None)
            if gate == "CIRCLECI":
                gate = "DELIVERY-1"
            blocker = row.get("blocker")
            btxt = json.dumps(blocker) if blocker else ""
            m = re.search(r"(DELIVERY-[1-5][AB]?|O[1-9])", btxt)
            if m:
                held[m.group(1)].append(task)
            elif gate and gate in gates and not gates[gate]["open"]:
                held[gate].append(task)
            elif gate and gate not in gates:
                held[gate].append(task)
            if row.get("state") == "BLOCKED":
                reason = ""
                if isinstance(blocker, dict):
                    reason = str(blocker.get("reason") or "").split(":", 1)[0].strip()
                    key = "%s: %s" % (blocker.get("class") or "?", reason or "(no reason)")
                else:
                    key = str(blocker or "(no blocker)")[:80]
                blocked_by[key] += 1
                blocked_rows[key].append(task)
        for g, info in gates.items():
            info["held_rows"] = sorted(held.get(g, []))
            info["held_roots"] = sorted({j.root_of(t)[0] for t in held.get(g, [])})
            if info["open"]:
                gated = [t for t, r in j.tasks.items() if r.get("state") not in vpjourney.CLOSED_STATES and t not in sup
                         and ((j.pack.get(j.packet_of(t) or "") or {}).get("twin_gate") or "").replace("CIRCLECI", "DELIVERY-1") == g]
                info["gated_rows_still_open"] = len(gated)
        for g in sorted(set(held) - set(gates)):
            gates[g] = {"gate": g, "open": None, "since_ts": None, "since_exact": False, "age_s": None,
                        "held_rows": sorted(held[g]), "held_roots": sorted({j.root_of(t)[0] for t in held[g]}),
                        "note": "gate named by rows but absent from roster.owner_gates (reads closed)"}
        blocked = [{"reason": k, "rows": n, "roots": sorted({j.root_of(t)[0] for t in blocked_rows[k]})}
                   for k, n in blocked_by.most_common()]
        # rulings still open in RULINGS-TRACKING.md
        rulings = []
        p = self.run_root / "RULINGS-TRACKING.md"
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.startswith("| R"):
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) >= 5 and OPEN_RULING_RE.search(cells[4]) and "EXECUTED" not in cells[4].split("(")[0]:
                    rulings.append({"id": cells[0], "by": cells[1], "ruled_at": cells[2], "action": cells[3][:200], "status": cells[4]})
        except OSError:
            pass
        # owner questions in comms.jsonl (kind question*, to owner), still unanswered = no later answer row
        questions = []
        try:
            tr = vpjourney.TailReader(self.run_root / "comms.jsonl")
            for c in tr.read_new():
                kind = str(c.get("kind") or "")
                if c.get("to") == "owner" and kind.startswith("question"):
                    questions.append({"ts": c.get("ts"), "from": c.get("from"), "task": c.get("task"),
                                      "text": str(c.get("text") or "")[:300], "age_s": age_s(c.get("ts"), now)})
        except Exception:
            pass
        open_gate_alerts = [c for c in alerts["open_conditions"] if c["family"].endswith("/OWNER_GATE")]
        closed = [g for g in gates.values() if g["open"] is False and g["held_rows"]]
        return {"gates": list(gates.values()), "roster_snapshots": n_snaps, "blocked_by_reason": blocked,
                "blocked_rows_total": sum(blocked_by.values()),
                "closed_gates_holding_work": len(closed),
                "rows_held_by_closed_gates": sum(len(g["held_rows"]) for g in closed),
                "open_rulings": rulings, "owner_questions": questions[-20:],
                "owner_gate_alerts_open": open_gate_alerts}

    # -- per-request views -------------------------------------------------------------------------

    def journey(self, root):
        return self.j.journey(root)

    def row(self, task):
        j = self.j
        row = j.tasks.get(task)
        if row is None:
            return None
        evs = [j._event_step(e) for e in j.events_by_task().get(task) or []]
        return {"task": task, "row": row, "record": j.records.get(task), "root": j.root_of(task),
                "packet": j.packet_of(task), "events": evs,
                "turns": [t for t in j.turns if t.get("task") == task],
                "proofs": [p for pid, p in j.proofs.items() if pid.startswith("proof-%s-" % task)]}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def make_handler(dash):
    class H(BaseHTTPRequestHandler):
        server_version = "vpdash/1"

        def log_message(self, fmt, *args):   # quiet: the driver's log is the log
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, default=str).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            with dash.lock:
                snap = dash.snapshot
            if u.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/snapshot":
                return self._send(200, snap)
            if u.path == "/api/summary":
                return self._send(200, {k: snap.get(k) for k in ("generated_at", "summary", "freshness", "refresh_ms", "errors")})
            if u.path == "/api/roots":
                return self._send(200, snap.get("roots") or [])
            if u.path == "/api/circleci":
                return self._send(200, snap.get("circleci") or {})
            if u.path == "/api/alerts":
                return self._send(200, snap.get("alerts") or {})
            if u.path == "/api/decisions":
                return self._send(200, snap.get("decisions") or {})
            if u.path == "/api/verify":
                return self._send(200, snap.get("verify") or {})
            if u.path == "/api/journey":
                root = (q.get("root") or [""])[0]
                jn = dash.journey(root)
                return self._send(200 if jn else 404, jn or {"error": "no such root or row", "root": root})
            if u.path == "/api/row":
                task = (q.get("task") or [""])[0]
                r = dash.row(task)
                return self._send(200 if r else 404, r or {"error": "no such row", "task": task})
            if u.path == "/api/ledger.md":
                return self._send(200, dash.j.render_ledger(), "text/plain; charset=utf-8")
            if u.path == "/healthz":
                return self._send(200, {"ok": bool(snap), "generated_at": snap.get("generated_at")})
            return self._send(404, {"error": "not found"})
    return H


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>v13 run</title>
<style>
:root{--bg:#0f1115;--fg:#d7dae0;--mut:#8a919e;--card:#171a21;--line:#262b35;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--info:#58a6ff;--pur:#bc8cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.4 -apple-system,Segoe UI,Helvetica,Arial,sans-serif}
header{display:flex;gap:18px;align-items:baseline;padding:10px 16px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:2;flex-wrap:wrap}
h1{font-size:15px;margin:0}.mut{color:var(--mut)}.small{font-size:11px}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:12px;padding:12px 16px}
section{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px 12px;min-width:0}
section.wide{grid-column:1/-1}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 8px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:3px 6px;border-bottom:1px solid var(--line);vertical-align:top;font-variant-numeric:tabular-nums}
th{color:var(--mut);font-weight:500;font-size:11px}tr.click{cursor:pointer}tr.click:hover{background:#1e2330}
.b{display:inline-block;padding:0 6px;border-radius:9px;font-size:11px;line-height:17px;border:1px solid transparent}
.s-VERIFIED,.s-INTEGRATED,.s-ACCEPTED,.s-PASS,.s-VERIFIED_LOCAL{color:var(--ok);border-color:var(--ok)}
.s-BLOCKED,.s-WAITING_DEPENDENCY,.s-HELD,.s-UNKNOWN{color:var(--warn);border-color:var(--warn)}
.s-REPAIR_REQUIRED,.s-INVALID_EVIDENCE,.s-FAIL_PRODUCT,.s-FAIL_INFRA{color:var(--bad);border-color:var(--bad)}
.s-CANCELLED{color:var(--mut);border-color:var(--mut)}.s-RUNNING,.s-CLAIMED,.s-READY{color:var(--info);border-color:var(--info)}
.sev-urgent{color:var(--bad);font-weight:600}.sev-attention{color:var(--warn)}.sev-routine{color:var(--mut)}
.big{font-size:22px;font-weight:600}.kpi{display:flex;gap:22px;flex-wrap:wrap}.kpi div{min-width:80px}.kpi .l{color:var(--mut);font-size:11px}
#drawer{position:fixed;top:0;right:0;width:min(760px,95vw);height:100vh;background:var(--card);border-left:1px solid var(--line);overflow:auto;padding:14px 16px;display:none;z-index:5}
#drawer.open{display:block}#drawer pre{white-space:pre-wrap;font:12px/1.35 ui-monospace,Menlo,monospace;color:var(--fg)}
.x{float:right;cursor:pointer;color:var(--mut)}code{font:12px ui-monospace,Menlo,monospace;color:#c9d1d9}
.src{color:var(--mut);font-size:10px}.filters{display:flex;gap:8px;margin-bottom:6px;flex-wrap:wrap}select,input{background:#0f1115;color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:2px 6px;font-size:12px}
.step{display:grid;grid-template-columns:150px 1fr;gap:8px;padding:2px 0;border-bottom:1px dotted var(--line)}
.step .t{color:var(--mut);font:11px ui-monospace,Menlo,monospace}.k-turn{color:var(--pur)}.k-proof{color:var(--info)}
.dot{display:inline-block;width:8px;height:8px;border-radius:4px;margin-right:4px}
</style></head><body>
<header><h1>v13 run</h1><span id="fresh" class="mut small"></span><span id="err" class="small" style="color:var(--bad)"></span></header>
<main>
<section class="wide"><h2>Work items <span class="src" id="scopesrc"></span></h2><div class="kpi" id="kpi"></div></section>
<section><h2>Open owner decisions</h2><div id="decisions"></div></section>
<section><h2>Alerts by pattern <span class="src">alerts.jsonl · vpalerts.annotate</span></h2><div id="alerts"></div></section>
<section class="wide"><h2>CircleCI <span class="src">proofs/circleci-pipelines.jsonl · proofs/*.json · roster.proof.circleci</span></h2><div id="circle"></div></section>
<section class="wide"><h2>Roots <span class="src">events/*.json grouped by root work item; click a row for its journey</span></h2>
<div class="filters"><select id="fkind"><option value="">all kinds</option><option>catalog</option><option>packet</option><option>orphan</option></select>
<select id="fstate"><option value="">all states</option></select><input id="fq" placeholder="filter id/title"><label><input type="checkbox" id="fopen"> only not settled</label><span class="mut small" id="rootcount"></span></div>
<table id="roots"><thead><tr><th>root</th><th>kind</th><th>state</th><th>primary</th><th>hosted</th><th>open rows</th><th>attempts</th><th>last event</th><th>title</th></tr></thead><tbody></tbody></table></section>
<section class="wide"><h2>Recent alerts</h2><table id="recent"><thead><tr><th>ts</th><th>severity</th><th>kind</th><th>task</th><th>repeat/fam</th><th>streak</th><th>why</th><th>text</th></tr></thead><tbody></tbody></table></section>
<section class="wide"><h2>Verify <span class="src">vpjourney.verify: events vs run-state vs LEDGER.md vs control.jsonl spans</span></h2><div id="verify"></div></section>
</main>
<div id="drawer"><span class="x" onclick="closeDrawer()">✕ close</span><div id="dbody"></div></div>
<script>
const $=s=>document.querySelector(s);const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const st=s=>`<span class="b s-${esc(s)}">${esc(s||'-')}</span>`;
function age(s){if(s==null)return '?';s=Math.floor(s);if(s<90)return s+'s';if(s<5400)return Math.floor(s/60)+'m';if(s<172800)return Math.floor(s/3600)+'h'+String(Math.floor(s%3600/60)).padStart(2,'0')+'m';return Math.floor(s/86400)+'d'+Math.floor(s%86400/3600)+'h'}
let SNAP=null;
async function load(){try{const r=await fetch('/api/snapshot');SNAP=await r.json();render()}catch(e){$('#err').textContent='fetch failed: '+e}}
function render(){const s=SNAP;if(!s||!s.summary)return;const f=s.freshness||{};
 const hbAge=f.heartbeat_age_s;const alive=hbAge!=null&&hbAge<180;
 $('#fresh').innerHTML=`<span class="dot" style="background:${alive?'var(--ok)':'var(--bad)'}"></span>driver ${alive?'alive':'STALE'} (heartbeat ${age(hbAge)} ago, pid ${esc(f.driver_pid)}, tick ${esc(f.driver_tick)}, ${esc(f.driver_active)} live) · events seq ${esc(f.events_sequence)} / run-state seq ${esc(f.state_sequence)} / driver seq ${esc(f.driver_sequence)} · last event ${age(f.last_event_age_s)} ago · budget $${f.budget?f.budget.spent_usd:'?'} of $${f.budget?f.budget.max_usd:'?'} · control.jsonl ${(f.control_offset/1048576).toFixed(0)} MB read (${esc(f.control_lines_read)} calls${f.control_bad_lines?', '+f.control_bad_lines+' torn':''}) · snapshot ${esc(s.generated_at)} (${esc(s.refresh_ms)} ms)`;
 $('#err').textContent=(s.errors&&s.errors.length)?('refresh errors: '+s.errors[s.errors.length-1]):'';
 const su=s.summary,sc=su.scope;
 $('#scopesrc').textContent=`${sc.catalog_rows} catalog rows (${sc.catalog_source.split('/').pop()}) + ${sc.packets_new_work} new-work packets of ${sc.packet_dirs} PACKET.md dirs (${sc.packets_on_catalog_rows} regrade catalog rows and count once) = ${sc.work_items_fixed} fixed; derived ${sc.work_items_derived}${sc.orphan_roots.length?'; ORPHANS '+sc.orphan_roots.join(','):''}`;
 const rb=su.roots_by_state||{};
 $('#kpi').innerHTML=[['work items',su.roots],['settled',su.settled],['done locally',su.local_done],['hosted proof open',su.hosted_open],['owed a ruling',su.owed],['active',su.active],['not settled',su.not_settled],['rows (attempts)',su.rows],['retry rows',sc.retry_rows],['events',su.events],['turns',su.turns]].map(([l,v])=>`<div><div class="big">${esc(v)}</div><div class="l">${l}</div></div>`).join('')+
  `<div style="flex:1"><div class="l">root state</div>${Object.entries(rb).sort((a,b)=>b[1]-a[1]).map(([k,v])=>st(k)+' '+v).join(' &nbsp; ')}</div>`;
 // re-render a panel only when its data changed: a refresh must not pull the table
 // out from under a reader (or a click); alerts/decisions carry server-side ages so they do move
 changed('roots',s.roots,renderRoots);changed('alerts',s.alerts,renderAlerts);changed('circle',s.circleci,renderCircle);changed('dec',s.decisions,renderDecisions);changed('verify',s.verify,renderVerify);}
const LAST={};function changed(k,data,fn){const j=JSON.stringify(data);if(LAST[k]===j)return;LAST[k]=j;fn();}
function renderRoots(){const roots=SNAP.roots||[];const sel=$('#fstate');if(sel.options.length<=1){[...new Set(roots.map(r=>r.state))].sort().forEach(s=>{const o=document.createElement('option');o.textContent=s;sel.appendChild(o)})}
 const k=$('#fkind').value,s=$('#fstate').value,q=$('#fq').value.toLowerCase(),oo=$('#fopen').checked;
 const rows=roots.filter(r=>(!k||r.kind==k)&&(!s||r.state==s)&&(!q||(r.id+' '+(r.title||'')).toLowerCase().includes(q))&&(!oo||!r.settled));
 const order={RUNNING:0,CLAIMED:0,REPAIR_REQUIRED:1,INVALID_EVIDENCE:1,BLOCKED:2,WAITING_DEPENDENCY:3,READY:3,VERIFIED:4,INTEGRATED:5,CANCELLED:6};
 rows.sort((a,b)=>(order[a.state]??3)-(order[b.state]??3)||(b.latest_ts||'').localeCompare(a.latest_ts||''));
 $('#rootcount').textContent=rows.length+' of '+roots.length;
 $('#roots tbody').innerHTML=rows.map(r=>`<tr class="click" onclick="openJourney('${esc(r.id)}')"><td><code>${esc(r.id)}</code></td><td>${esc(r.kind)}</td><td>${st(r.state)}</td><td>${st(r.primary_state)}</td><td>${r.hosted_state?st(r.hosted_state):'<span class=mut>-</span>'}</td><td>${(r.open_rows||[]).map(t=>'<code>'+esc(t)+'</code>').join(' ')}${r.superseded_open&&r.superseded_open.length?' <span class="mut small">+'+r.superseded_open.length+' zombie</span>':''}</td><td>${r.attempts}</td><td class="small mut">${esc((r.latest_ts||'').slice(0,19))}</td><td class="small">${esc((r.title||'').slice(0,90))}</td></tr>`).join('');}
function renderAlerts(){const a=SNAP.alerts||{};const bs=a.by_severity||{};
 let h=`<div class="kpi"><div><div class="big sev-urgent">${bs.urgent||0}</div><div class="l">urgent</div></div><div><div class="big sev-attention">${bs.attention||0}</div><div class="l">attention</div></div><div><div class="big sev-routine">${bs.routine||0}</div><div class="l">routine</div></div><div><div class="big">${a.backlog_now??'?'}</div><div class="l">rows owed now (events)</div></div></div>`;
 h+=`<div class="mut small">${a.recorded_severity_present?'severity recorded by the driver (D60 live)':'severity computed here; driver not yet writing it (D60 patch pending)'}</div>`;
 h+='<h2 style="margin-top:8px">open conditions (family still inside its streak gap)</h2>';
 const oc=a.open_conditions||[];h+=oc.length?'<table>'+oc.map(c=>`<tr><td class="sev-${c.severity}">${esc(c.severity)}</td><td><code>${esc(c.family)}</code></td><td>×${c.repeat_family}</td><td>${c.repeat_family>1?'open '+age(c.streak_s):'<span class=mut>-</span>'}</td><td class="mut small">last ${age(c.age_s)} ago</td><td class="small">${esc(c.reasons.join('; '))}</td></tr>`).join('')+'</table>':'<div class="mut">none</div>';
 $('#alerts').innerHTML=h;
 $('#recent tbody').innerHTML=(a.recent||[]).slice(0,120).map(r=>`<tr${r.task?` class="click" onclick="openRow('${esc(r.task)}')"`:''}><td class="small mut">${esc((r.ts||'').slice(5,19))}</td><td class="sev-${r.severity}">${esc(r.severity)}${r.recorded_severity&&r.recorded_severity!=r.severity?' <span class=mut>(driver: '+esc(r.recorded_severity)+')</span>':''}</td><td>${esc(r.kind)}</td><td><code>${esc(r.task||'')}</code></td><td>${r.repeat}/${r.repeat_family}</td><td>${age(r.streak_s)}</td><td class="small">${esc(r.reasons.join('; '))}</td><td class="small">${esc(r.text.slice(0,160))}</td></tr>`).join('');}
function renderCircle(){const c=SNAP.circleci||{};
 let h=`<div class="kpi"><div><div class="big">${c.triggered_today_total}</div><div class="l">pipelines today (UTC)</div></div><div><div class="big">${c.max_pipelines_per_day??'-'}</div><div class="l">max_pipelines_per_day (roster)</div></div><div><div class="big">${c.in_flight_total}</div><div class="l">in flight (no proof record yet)</div></div><div><div class="big">${c.max_pipelines_in_flight??'-'}</div><div class="l">max in flight (roster)</div></div><div><div class="big">${c.triggered_total}</div><div class="l">triggered, run total</div></div><div><div class="big ${c.gate_open?'s-VERIFIED':'s-BLOCKED'}">${c.gate_open?'OPEN':'CLOSED'}</div><div class="l">${esc(c.gate)} gate</div></div><div><div class="big">${esc(c.mode)}</div><div class="l">mode · primary ${esc(c.primary_account)} · spread ${(c.spread||[]).join(',')}</div></div></div>`;
 if(c.note)h+=`<div class="small" style="color:var(--warn)">${esc(c.note)}</div>`;
 h+='<table><tr><th>account</th><th>org / repo</th><th>today</th><th>total</th><th>in flight</th><th>lost</th><th>refused gate</th><th>refused cap</th><th>credits</th><th>failed</th><th>proof outcomes</th><th>last trigger</th><th>last proof</th></tr>'+(c.accounts||[]).map(a=>`<tr><td><b>${esc(a.account)}</b></td><td class="small">${esc(a.org||'')}<br><span class=mut>${esc(a.repo||'')}</span></td><td>${a.triggered_today}</td><td>${a.triggered_total}</td><td>${a.in_flight.length?a.in_flight.map(x=>`<code title="${esc(x.pipeline_id)}">${esc(x.proof_id.replace('proof-',''))}</code> ${age(x.age_s)}`).join('<br>'):'-'}</td><td>${a.lost.length||'-'}</td><td>${a.refused_gate||'-'}</td><td>${a.refused_cap||'-'}</td><td>${a.credits_blocked||'-'}</td><td>${a.trigger_failed||'-'}</td><td>${Object.entries(a.proofs).map(([k,v])=>st(k)+' '+v).join(' ')||'-'}</td><td class="small mut">${esc((a.last_trigger_ts||'-').slice(0,16))}</td><td class="small">${a.last_proof_ts?st(a.last_proof_status)+' <span class=mut>'+esc(a.last_proof_ts.slice(0,16))+'</span>':'-'}</td></tr>`).join('')+'</table>';
 if(c.proofs_unattributed&&Object.keys(c.proofs_unattributed).length)h+=`<div class="mut small">${Object.values(c.proofs_unattributed).reduce((x,y)=>x+y,0)} circleci proof records carry no account field (pre-D44): ${esc(JSON.stringify(c.proofs_unattributed))}</div>`;
 $('#circle').innerHTML=h;}
function renderDecisions(){const d=SNAP.decisions||{};let h=`<div class="kpi"><div><div class="big">${d.closed_gates_holding_work}</div><div class="l">closed gates holding work</div></div><div><div class="big">${d.rows_held_by_closed_gates}</div><div class="l">rows held</div></div><div><div class="big">${(d.open_rulings||[]).length}</div><div class="l">rulings not executed</div></div></div>`;
 h+='<table><tr><th>gate</th><th>state</th><th>for</th><th>holds</th></tr>'+(d.gates||[]).map(g=>`<tr><td><b>${esc(g.gate)}</b></td><td>${g.open==null?'<span class=mut>not in roster</span>':g.open?'<span class="s-VERIFIED b">open</span>':'<span class="s-BLOCKED b">closed</span>'}</td><td>${g.age_s!=null?(g.since_exact?'':'≥ ')+age(g.age_s):'?'}</td><td class="small">${g.held_roots.length?g.held_roots.length+' items: '+g.held_roots.slice(0,12).map(r=>`<code class="click" onclick="openJourney('${esc(r)}')">${esc(r)}</code>`).join(' ')+(g.held_roots.length>12?' …':''):(g.open&&g.gated_rows_still_open?'<span class=mut>open; '+g.gated_rows_still_open+' gated rows still open for other reasons</span>':'<span class=mut>-</span>')}${g.note?'<br><span style="color:var(--warn)">'+esc(g.note)+'</span>':''}</td></tr>`).join('')+'</table>';
 if((d.blocked_by_reason||[]).length)h+=`<h2 style="margin-top:8px">BLOCKED rows by blocker (${d.blocked_rows_total}) <span class="src">run-state blocker.class: reason</span></h2><table>`+d.blocked_by_reason.map(b=>`<tr><td><b>${b.rows}</b></td><td class="small"><code>${esc(b.reason)}</code><br><span class=mut>${b.roots.slice(0,10).map(r=>`<code class="click" onclick="openJourney('${esc(r)}')">${esc(r)}</code>`).join(' ')}${b.roots.length>10?' … '+b.roots.length+' items':''}</span></td></tr>`).join('')+'</table>';
 h+=`<div class="mut small">since = mtime of the earliest roster.N.json snapshot (${d.roster_snapshots}) with the current value; "≥" when no snapshot shows another value</div>`;
 if((d.open_rulings||[]).length)h+='<h2 style="margin-top:8px">rulings not executed (RULINGS-TRACKING.md)</h2><table>'+d.open_rulings.map(r=>`<tr><td>${esc(r.id)}</td><td class="small">${esc(r.action)}</td><td class="small" style="color:var(--warn)">${esc(r.status)}</td></tr>`).join('')+'</table>';
 if((d.owner_questions||[]).length)h+='<h2 style="margin-top:8px">questions to owner (comms.jsonl)</h2><table>'+d.owner_questions.map(q=>`<tr><td class="small mut">${age(q.age_s)}</td><td class="small">${esc(q.text)}</td></tr>`).join('')+'</table>';
 $('#decisions').innerHTML=h;}
function renderVerify(){const v=SNAP.verify||{};const f=v.findings||[];const by={};f.forEach(x=>{(by[x.check]=by[x.check]||[]).push(x)});
 let h=`<div class="kpi"><div><div class="big ${v.errors?'sev-urgent':''}">${v.errors??'?'}</div><div class="l">errors</div></div><div><div class="big">${v.warnings??'?'}</div><div class="l">warnings</div></div><div><div class="big">${esc(v.calls_read)}</div><div class="l">control.jsonl calls checked</div></div></div>`;
 h+=Object.entries(by).map(([k,xs])=>`<details><summary><b>${esc(k)}</b> <span class="mut">${xs.length} · ${esc(xs[0].severity)}</span></summary><table>${xs.slice(0,80).map(x=>`<tr><td>${x.task?`<code class="click" onclick="openRow('${esc(x.task)}')">${esc(x.task)}</code>`:''}</td><td class="small">${esc(x.detail)}</td></tr>`).join('')}</table></details>`).join('')||'<div class="mut">no findings</div>';
 $('#verify').innerHTML=h;}
async function openJourney(root){const r=await fetch('/api/journey?root='+encodeURIComponent(root));const j=await r.json();if(j.error){alert(j.error);return}
 let h=`<h1><code>${esc(j.root)}</code> ${st(j.state)} <span class="mut small">${esc(j.kind)} · primary ${esc(j.primary_row)} ${st(j.primary_state)} · hosted ${j.hosted_state?st(j.hosted_state):'-'} · ${j.settled?'settled':'not settled'}</span></h1><div class="small">${esc(j.title||'')}</div>`;
 for(const a of j.attempts){h+=`<h2 style="margin-top:14px"><code class="click" onclick="openRow('${esc(a.task)}')">${esc(a.task)}</code> ${st(a.state)} <span class="mut">${esc(a.template||a.kind||'')} · ${esc(a.how||'')}${a.retry_of?' · retry of '+esc(a.retry_of)+(a.retry_reason?': '+esc(a.retry_reason.slice(0,140)):''):''}</span></h2>`;
  h+=`<div class="small mut">base ${esc((a.base_sha||'').slice(0,10))} → output ${esc((a.output_sha||'').slice(0,10))}${a.depends_on.length?' · depends_on '+a.depends_on.map(esc).join(','):''}${a.stacked_on.length?' · stacked_on '+a.stacked_on.map(esc).join(','):''}${a.blocker?' · blocker '+esc(JSON.stringify(a.blocker)).slice(0,200):''}</div>`;
  h+=a.steps.map(s=>`<div class="step"><div class="t" title="${esc(s.source)}">${esc((s.ts||'').slice(5,19))} ${s.kind=='event'?'#'+s.seq:''}</div><div class="k-${esc(s.kind)}">${esc(s.text)}${s.kind=='turn'&&s.cost!=null?` <span class="mut small">$${(+s.cost).toFixed(3)} ${s.duration_s?Math.round(s.duration_s)+'s':''}</span>`:''}</div></div>`).join('');}
 $('#dbody').innerHTML=h;$('#drawer').classList.add('open');}
async function openRow(task){const r=await fetch('/api/row?task='+encodeURIComponent(task));const j=await r.json();if(j.error){alert(j.error);return}
 let h=`<h1><code>${esc(task)}</code> ${st(j.row.state)} <span class="mut small">root <code class="click" onclick="openJourney('${esc(j.root[0])}')">${esc(j.root[0])}</code> (${esc(j.root[2])})</span></h1>`;
 h+='<h2>run-state row</h2><pre>'+esc(JSON.stringify(j.row,null,1))+'</pre>';if(j.record)h+='<h2>driver packet record</h2><pre>'+esc(JSON.stringify(j.record,null,1))+'</pre>';
 h+='<h2>events</h2>'+j.events.map(s=>`<div class="step"><div class="t">${esc((s.ts||'').slice(5,19))} #${s.seq}</div><div>${esc(s.text)} <span class="src">${esc(s.source)}</span></div></div>`).join('');
 if(j.turns.length)h+='<h2>turns (costs.jsonl)</h2><pre>'+esc(JSON.stringify(j.turns,null,1))+'</pre>';if(j.proofs.length)h+='<h2>proofs</h2><pre>'+esc(JSON.stringify(j.proofs,null,1))+'</pre>';
 $('#dbody').innerHTML=h;$('#drawer').classList.add('open');}
function closeDrawer(){$('#drawer').classList.remove('open')}
document.addEventListener('keydown',e=>{if(e.key=='Escape')closeDrawer()});
['#fkind','#fstate','#fq','#fopen'].forEach(s=>$(s).addEventListener('input',renderRoots));
load();setInterval(load,15000);
</script></body></html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--port", type=int, default=4180)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--refresh-s", type=int, default=10)
    ap.add_argument("--control-every-s", type=int, default=60)
    ap.add_argument("--once", action="store_true", help="build one snapshot, print its summary and exit")
    args = ap.parse_args(argv)
    dash = Dash(args.run_root, control_every_s=args.control_every_s, refresh_s=args.refresh_s)
    if args.once:
        dash.refresh(force_control=True)
        snap = dash.snapshot
        out = {k: snap.get(k) for k in ("generated_at", "refresh_ms", "errors")}
        out["summary"] = {k: v for k, v in (snap.get("summary") or {}).items() if k != "scope"}
        out["scope"] = (snap.get("summary") or {}).get("scope")
        out["circleci"] = {k: v for k, v in (snap.get("circleci") or {}).items() if k != "accounts"}
        out["circleci_accounts"] = {a["account"]: {"today": a["triggered_today"], "total": a["triggered_total"],
                                                   "in_flight": len(a["in_flight"]), "proofs": a["proofs"]}
                                    for a in (snap.get("circleci") or {}).get("accounts", [])}
        out["alerts"] = {k: v for k, v in (snap.get("alerts") or {}).items() if k not in ("recent",)}
        out["decisions"] = snap.get("decisions")
        out["verify"] = {k: v for k, v in (snap.get("verify") or {}).items() if k != "findings"}
        print(json.dumps(out, indent=1, default=str))
        return 0
    dash.start()
    srv = ThreadingHTTPServer((args.bind, args.port), make_handler(dash))
    srv.daemon_threads = True
    print("vpdash on http://%s:%d/  run_root=%s" % (args.bind, args.port, args.run_root), flush=True)
    try:
        srv.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        dash.stop()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
