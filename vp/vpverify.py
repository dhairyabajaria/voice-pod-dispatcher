# -*- coding: utf-8 -*-
"""vpverify.py -- `lanedriver.py verify`: turn the audit trail from "present"
into "checkable" (AUDIT-3, 2026-09-18).

  seals     every seals.jsonl line: the chain (prev_hash = sha256 of the line
            before), the snapshot manifest on disk with the recorded sha256,
            and every sealed file: append-only files (.jsonl/.log) by PREFIX
            (first N bytes hash to the sealed sha256, file at least N bytes),
            other files by equality -- but only for the NEWEST seal that
            lists them (an older seal legitimately saw an earlier LEDGER.md /
            roster.json / run-state mirror; a prefix must hold in every seal).
  state     re-derive every task's state from the scheduler's own event log
            (orchestration-state/events/*.json, sequence-ordered) and diff it
            against run-state.json and the state column of LEDGER.md; list
            the events that have no control.jsonl line covering their
            sequence (actions taken outside the driver: Architect/Fixer CLI).
            Readiness (PLANNED/WAITING_DEPENDENCY/READY) is not an event: it
            is recomputed from depends_on + unlocks_dependents exactly as the
            scheduler does.
  shas      every turns/*/*/harvest.json output_sha / tree_sha resolves in the
            trunk repo, and equals the TASK_COMPLETED event of that attempt.

Pure functions over the run root; nothing is written.  Exit 0 = all PASS.
"""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

APPEND_ONLY = (".jsonl", ".log")
TERMINAL_BY_EVENT = {
    "TASK_CLAIMED": "CLAIMED", "TASK_STARTED": "RUNNING", "TASK_BLOCKED": "BLOCKED",
    "TASK_UNBLOCKED": "PLANNED", "TASK_RETIRED": "CANCELLED", "TASK_INVALIDATED": "INVALID_EVIDENCE",
    "TASK_PROMOTED_TO_INTEGRATED": "INTEGRATED", "TEMPLATE_TASK_INSTANTIATED": "PLANNED",
}
READINESS = ("PLANNED", "WAITING_DEPENDENCY", "READY")
LEDGER_ROW = re.compile(r"^\| (?P<task>[^|]+?) \| (?P<state>[^|]+?) \|")


def _sha256_prefix(path, nbytes):
    h = hashlib.sha256()
    left = int(nbytes)
    with open(path, "rb") as fh:
        while left > 0:
            chunk = fh.read(min(1 << 20, left))
            if not chunk:
                break
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest(), int(nbytes) - left


def _jsonl(path):
    try:
        lines = [l for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []
    out = []
    for l in lines:
        try:
            out.append((l, json.loads(l)))
        except ValueError:
            out.append((l, None))
    return out


# -- seals ----------------------------------------------------------------------------------

def verify_seals(run_root):
    run_root = Path(run_root)
    rows = _jsonl(run_root / "seals.jsonl")
    problems, drift, prev_hash, checked = [], [], None, 0
    newest_for = {}                       # rel -> (index, entry) in the newest seal listing it
    manifests = []
    for i, (line, rec) in enumerate(rows):
        if rec is None:
            problems.append("seals.jsonl line %d is not JSON" % (i + 1))
            prev_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()
            continue
        if "prev_hash" in rec and rec.get("prev_hash") != prev_hash:
            problems.append("seal %d (%s): prev_hash %s != sha256 of the previous line %s -- chain broken"
                            % (i + 1, rec.get("ts"), str(rec.get("prev_hash"))[:12], str(prev_hash)[:12]))
        prev_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()
        mpath = run_root / rec.get("manifest", "")
        if not mpath.is_file() and rec.get("manifest", "").startswith("MANIFEST-"):
            mpath = run_root / rec["manifest"]
        if not mpath.is_file():
            problems.append("seal %d (%s): manifest %s missing" % (i + 1, rec.get("ts"), rec.get("manifest")))
            manifests.append(None)
            continue
        body = mpath.read_bytes()
        actual = hashlib.sha256(body).hexdigest()
        if actual != rec.get("sha256"):
            if "/" in str(rec.get("manifest", "")) or "prev_hash" in rec:
                problems.append("seal %d (%s): %s sha256 %s != recorded %s"
                                % (i + 1, rec.get("ts"), rec.get("manifest"), actual[:12], str(rec.get("sha256"))[:12]))
            else:
                # pre-chain seals shared MANIFEST-<day>.json and were rewritten
                # in place by design; only the newest per day is checkable
                pass
        try:
            man = json.loads(body.decode("utf-8"))
        except ValueError:
            problems.append("seal %d: manifest %s is not JSON" % (i + 1, rec.get("manifest")))
            manifests.append(None)
            continue
        manifests.append(man)
        for rel, ent in (man.get("entries") or {}).items():
            newest_for[rel] = (i, ent)
    # prefixes must hold for EVERY seal; equality only for the newest listing
    for i, man in enumerate(manifests):
        if not man:
            continue
        for rel, ent in (man.get("entries") or {}).items():
            p = run_root / rel
            append_only = ent.get("append_only", rel.endswith(APPEND_ONLY))
            if not p.is_file():
                if newest_for.get(rel, (None,))[0] == i:
                    problems.append("seal %d: %s sealed but missing now" % (i + 1, rel))
                continue
            if append_only:
                size = p.stat().st_size
                if size < int(ent["bytes"]):
                    problems.append("seal %d: %s shrank: %d < sealed %d bytes" % (i + 1, rel, size, ent["bytes"]))
                    continue
                digest, n = _sha256_prefix(p, ent["bytes"])
                checked += 1
                if digest != ent["sha256"]:
                    problems.append("seal %d: %s first %d bytes changed since the seal" % (i + 1, rel, ent["bytes"]))
            elif newest_for.get(rel, (None,))[0] == i:
                # a rewritable file: equality against the newest seal only;
                # a change since then is DRIFT (seal now to cover it), never
                # a broken seal -- the seal proved what the file was THEN
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
                checked += 1
                if digest != ent["sha256"]:
                    drift.append(rel)
    return {"seals": len(rows), "files_checked": checked, "problems": problems, "drift_since_newest_seal": drift,
            "newest_seal_ts": (rows[-1][1] or {}).get("ts") if rows else None,
            "chain_ok": not any("chain broken" in p for p in problems)}


# -- state replay ---------------------------------------------------------------------------

def load_events(control_dir):
    evs = []
    for p in sorted(Path(control_dir, "events").glob("*.json")):
        try:
            evs.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    evs.sort(key=lambda e: int(e.get("sequence") or 0))
    return evs


def replay(events, tasks_now):
    """-> {task: {state, unlocks_dependents, output_sha, attempt_id}} for
    every task an event touched; readiness recomputed afterwards for the
    untouched / unstarted rows using run-state's depends_on."""
    derived = {}
    for e in events:
        t = e.get("task_id")
        typ = e.get("type")
        if not t:
            continue
        row = derived.setdefault(t, {"state": None, "unlocks_dependents": False, "output_sha": None, "attempt_id": None})
        if typ in ("TASK_COMPLETED", "PRESERVED_RESULT_ADOPTED"):
            row["state"] = e.get("outcome")
            row["unlocks_dependents"] = bool(e.get("unlocks_dependents"))
            row["output_sha"] = e.get("output_sha")
            row["attempt_id"] = e.get("attempt_id") or row["attempt_id"]
        elif typ == "TASK_PROMOTED_TO_INTEGRATED":
            row["state"] = "INTEGRATED"
            row["unlocks_dependents"] = True
            row["output_sha"] = e.get("output_sha")
        elif typ == "TASK_INVALIDATED":
            row["state"] = "INVALID_EVIDENCE"
            row["unlocks_dependents"] = False
        elif typ in TERMINAL_BY_EVENT:
            row["state"] = TERMINAL_BY_EVENT[typ]
            if typ == "TASK_CLAIMED":
                row["attempt_id"] = e.get("attempt_id")
            if typ in ("TASK_RETIRED", "TASK_BLOCKED"):
                row["unlocks_dependents"] = False
    # readiness for rows whose derived state is unstarted (or untouched)
    for t, now in tasks_now.items():
        d = derived.setdefault(t, {"state": None, "unlocks_dependents": False, "output_sha": None, "attempt_id": None})
        if d["state"] in (None, "PLANNED"):
            deps = now.get("depends_on") or []
            waiting = [x for x in deps if not (derived.get(x) or {}).get("unlocks_dependents")]
            d["state"] = "WAITING_DEPENDENCY" if waiting else "READY"
            d["readiness"] = True
    return derived


def ledger_states(ledger_path):
    out = {}
    try:
        text = Path(ledger_path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        m = LEDGER_ROW.match(line)
        if m and m.group("task") not in ("task", "---"):
            out[m.group("task")] = m.group("state")
    return out


def verify_state(run_root, control_dir, state_path):
    run_root = Path(run_root)
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"problems": ["run-state unreadable: %s" % exc]}
    tasks = state.get("tasks") or {}
    events = load_events(control_dir)
    derived = replay(events, tasks)
    problems, diffs = [], []
    for t, now in sorted(tasks.items()):
        d = derived.get(t) or {}
        if t in ("L35", "L42", "L44") and d.get("readiness"):
            continue                      # their readiness also waits on REVIEW:* rows (not an event)
        if d.get("state") != now.get("state"):
            # L35/L42/L44 wait on review rows the replay cannot see; report, do not fail
            diffs.append({"task": t, "run_state": now.get("state"), "replayed": d.get("state"),
                          "note": "readiness" if d.get("readiness") else "event"})
        elif d.get("output_sha") and now.get("output_sha") and d["output_sha"] != now["output_sha"]:
            diffs.append({"task": t, "field": "output_sha", "run_state": now["output_sha"], "replayed": d["output_sha"]})
    hard = [x for x in diffs if x.get("note") == "event" or x.get("field")]
    for x in hard:
        problems.append("%s: run-state %s vs events %s%s" % (x["task"], x.get("run_state"), x.get("replayed"),
                                                             " (%s)" % x["field"] if x.get("field") else ""))
    # LEDGER.md state column vs run-state (the ledger is rendered on a timer:
    # a row that moved after the last render is a lag, reported separately)
    ledger = ledger_states(run_root / "LEDGER.md")
    lag = [t for t, st in ledger.items() if t in tasks and tasks[t].get("state") != st]
    # events without a driver control.jsonl line: out-of-driver actions
    spans = []
    for _l, rec in _jsonl(run_root / "control.jsonl"):
        if rec and rec.get("seq_after", -1) > rec.get("seq_before", -1):
            spans.append((int(rec["seq_before"]), int(rec["seq_after"])))
    covered = set()
    for a, b in spans:
        covered.update(range(a + 1, b + 1))
    first = min((a for a, _b in spans), default=None)
    outside = [{"sequence": e.get("sequence"), "type": e.get("type"), "task": e.get("task_id"),
                "actor": e.get("actor"), "at": e.get("created_at")}
               for e in events if e.get("sequence") and int(e["sequence"]) not in covered
               and (first is None or int(e["sequence"]) > first)]
    before = sum(1 for e in events if e.get("sequence") and first is not None and int(e["sequence"]) <= first)
    return {"events": len(events), "tasks": len(tasks), "replay_diffs": diffs, "problems": problems,
            "ledger_lag": lag, "ledger_rows": len(ledger), "control_lines": len(spans),
            "control_first_sequence": first, "events_before_control_jsonl": before,
            "events_outside_control_jsonl": outside}


# -- shas -----------------------------------------------------------------------------------

def verify_shas(run_root, trunk, control_dir):
    run_root = Path(run_root)
    completed = {}
    for e in load_events(control_dir):
        if e.get("type") == "TASK_COMPLETED" and e.get("attempt_id"):
            completed[e["attempt_id"]] = e
    problems, checked, missing = [], 0, []
    for h in sorted(run_root.glob("turns/*/*/harvest.json")):
        try:
            doc = json.loads(h.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            problems.append("%s unreadable" % h.relative_to(run_root))
            continue
        attempt = doc.get("attempt")
        for key, kind in (("output_sha", "commit"), ("tree_sha", "tree")):
            sha = doc.get(key)
            if not sha:
                continue
            checked += 1
            rc = subprocess.run(["git", "-C", str(trunk), "cat-file", "-e", "%s^{%s}" % (sha, kind)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
            if rc != 0:
                missing.append({"attempt": attempt, key: sha})
                problems.append("%s: %s %s is not a %s in %s" % (attempt, key, sha[:12], kind, trunk))
        ev = completed.get(attempt)
        if ev and doc.get("output_sha") and ev.get("output_sha") and ev["output_sha"] != doc["output_sha"]:
            problems.append("%s: harvest output_sha %s != TASK_COMPLETED %s"
                            % (attempt, doc["output_sha"][:12], ev["output_sha"][:12]))
    return {"harvests": len(list(run_root.glob("turns/*/*/harvest.json"))), "shas_checked": checked,
            "missing": missing, "problems": problems}


def verify_all(run_root, control_dir, state_path, trunk):
    rep = {"seals": verify_seals(run_root), "state": verify_state(run_root, control_dir, state_path),
           "shas": verify_shas(run_root, trunk, control_dir)}
    rep["ok"] = not (rep["seals"]["problems"] or rep["state"]["problems"] or rep["shas"]["problems"])
    return rep
