#!/usr/bin/env python3
"""Deterministic dependency scheduler for the Voice Pod launch catalog.

This module is intentionally standard-library only.  It is the authoritative
mechanism for turning a predecessor completion into newly READY work.  LLM
parents dispatch the returned work; they do not infer dependency state.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import review_gate


ACTIVE_STATES = {"CLAIMED", "DISPATCHED", "RUNNING", "RESULT_RECEIVED"}
FINISHED_STATES = {
    "VERIFIED", "INTEGRATED", "REPAIR_REQUIRED", "BLOCKED",
    "INVALID_EVIDENCE", "CANCELLED",
}
CLAIMABLE_STATES = {"READY"}
VALID_OUTCOMES = {
    "VERIFIED", "INTEGRATED", "REPAIR_REQUIRED", "BLOCKED", "INVALID_EVIDENCE",
}
REQUIRED_ACTIVATION_GATES = {
    "PRESERVATION_COMPLETE",
    "REQUIREMENTS_REBOUND",
    "RECOVERY_ADOPTED",
    "DRY_RUN_PASSED",
}
ROUTE_BINDINGS = {
    "BIND_APPROVED_GO_MUSE_ROUTE": (
        "go2/muse-spark-1.3-contributor",
        "xhigh",
        "router_go2_muse_spark_1_3_contributor",
    ),
    "BIND_APPROVED_GO_DEEPSEEK_ROUTE": (
        "go2/deepseek-v4.1-flash",
        "high",
        "router_go2_deepseek_v4_1_flash",
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text())


def atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def locked_state(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = read_json(path)
        flush_publications(path.parent, state)
        yield state
        atomic_json(path, state)
        flush_publications(path.parent, state)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def catalog_tasks(catalog: dict) -> dict:
    return {item["id"]: item for item in catalog["contracts"]}


def task_contract(state: dict, catalog: dict, task_id: str) -> dict:
    fixed = catalog_tasks(catalog)
    if task_id in fixed:
        row = state["tasks"].get(task_id) or {}
        if row.get("rewired"):
            # `rewire` recorded a v13 dependency set for this catalog task; the
            # catalog text stays authoritative for everything else
            return dict(fixed[task_id], depends_on=list(row["depends_on"]))
        return fixed[task_id]
    row = state["tasks"][task_id]
    return {
        "id": task_id,
        "chief": row["chief"],
        "kind": row["kind"],
        "role": row["role"],
        "depends_on": row["depends_on"],
    }


def normalize_path(raw: str) -> str:
    if not raw or raw.startswith("/"):
        raise ValueError(f"claim paths must be non-empty repository-relative paths: {raw!r}")
    if any(marker in raw for marker in ("*", "?", "[", "]")):
        raise ValueError(f"claim paths must be concrete, not globs: {raw!r}")
    value = str(PurePosixPath(raw))
    if value == "." or ".." in PurePosixPath(raw).parts:
        raise ValueError(f"claim path escapes repository: {raw!r}")
    return value.rstrip("/")


def paths_conflict(left: str, right: str) -> bool:
    left, right = normalize_path(left), normalize_path(right)
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def flush_publications(control_dir: Path, state: dict) -> None:
    """Replay committed publications; never expose an event before its state commit.

    The durable outbox remains in state, so a crash after commit but before file
    publication is repaired by the next mutating command (or reconcile).
    Files are notifications, not an app/runtime wake-up mechanism.
    """
    for relative, payload in state.get("publications", {}).items():
        path = control_dir / normalize_path(relative)
        if path.exists():
            if read_json(path) != payload:
                raise RuntimeError(f"immutable publication differs: {path}")
        else:
            atomic_json(path, payload)


def write_event(control_dir: Path, state: dict, event_type: str, payload: dict) -> dict:
    state["sequence"] += 1
    event = {
        "schema_version": "1",
        "event_id": f"{state['sequence']:08d}-{uuid.uuid4().hex[:12]}",
        "sequence": state["sequence"],
        "type": event_type,
        "run_id": state["run_id"],
        "created_at": utc_now(),
        **payload,
    }
    state.setdefault("publications", {})[f"events/{event['event_id']}.json"] = event
    return event


def notify(state: dict, chief: str, event: dict, task_ids: list[str]) -> None:
    message = {
        "schema_version": "1",
        "message_id": f"ready-{event['event_id']}-{chief}",
        "type": "TASKS_BECAME_READY",
        "to_chief": chief,
        "source_event_id": event["event_id"],
        "task_ids": sorted(task_ids),
        "required_action": "Run `ready --chief %s`, claim safe tasks, and dispatch before waiting." % chief,
        "created_at": event["created_at"],
    }
    state.setdefault("publications", {})[
        f"inbox/chief-{chief.lower()}/{message['message_id']}.json"
    ] = message


def dependency_status(state: dict, task: dict) -> tuple[bool, list[str]]:
    waiting = []
    for dependency in task["depends_on"]:
        predecessor = state["tasks"][dependency]
        if not predecessor.get("unlocks_dependents", False):
            waiting.append(dependency)
    if task["id"] in {"L35", "L42", "L44"}:
        # Physical integration is a build input, never an acceptance verdict.
        for task_id, row in state["tasks"].items():
            if row["state"] == "INTEGRATED" and task_id not in {"L35", "L42", "L44"} and row.get("kind") not in review_gate.ROLES:
                if not current_review(state, row, "junior"):
                    waiting.append(f"REVIEW:{task_id}:junior")
    return not waiting, waiting


def current_review(state, row, role):
    record = row.get("reviews", {}).get(role, {})
    if record.get("candidate_sha") != state["candidate"].get("sha") or not state["candidate"].get("sha"):
        return False
    try:
        packet_path = review_gate.hashed(record["packet"])
        packet = read_json(packet_path)
        review_gate.hashed(packet["runtime_log"])
        for artifact in packet["artifacts"]:
            review_gate.hashed(artifact)
    except (KeyError, ValueError, OSError):
        return False
    return record.get("status") == "PASS"


def enforce_review_transition(state, catalog, row, args):
    """Used by every result ingress, including recovery and manual promotion."""
    outcome = getattr(args, "outcome", "INTEGRATED")
    if outcome not in {"VERIFIED", "INTEGRATED"}:
        return
    kind = row.get("kind") or task_contract(state, catalog, row["task_id"])["kind"]
    role = kind if kind in review_gate.ROLES else None
    if row["task_id"] == "L35":
        role = "junior"
    elif row["task_id"] in {"L42", "L44"}:
        role = "final_review"
    if role:
        packet = getattr(args, "verdict", None)
        if not packet:
            raise RuntimeError(f"{row['task_id']} requires --verdict with validated {role} evidence")
        record = review_gate.validate(packet, state, catalog, row["task_id"], role)
        if row["task_id"] in {"L42", "L44"} and not current_review(state, state["tasks"]["L35"], "junior"):
            raise RuntimeError("final acceptance requires current-candidate consolidated Junior PASS")
        if row["task_id"] in {"L35", "L42", "L44"}:
            _, missing = dependency_status(state, task_contract(state, catalog, row["task_id"]))
            if missing:
                raise RuntimeError(f"acceptance dependencies unmet: {missing}")
        row.setdefault("reviews", {})[role] = record
    elif outcome == "INTEGRATED":
        row.setdefault("review_status", "PENDING_RECONCILIATION")


def recompute_readiness(state: dict, catalog: dict, control_dir: Path, source_event: dict | None) -> list[str]:
    became_ready: list[str] = []
    for task_id, row in state["tasks"].items():
        contract = task_contract(state, catalog, task_id)
        if row["state"] not in {"PLANNED", "WAITING_DEPENDENCY", "READY"}:
            continue
        satisfied, waiting = dependency_status(state, contract)
        new_state = "READY" if satisfied else "WAITING_DEPENDENCY"
        if row["state"] != "READY" and new_state == "READY":
            became_ready.append(task_id)
        row["state"] = new_state
        row["waiting_for"] = waiting
        row["updated_at"] = utc_now()

    if source_event and became_ready:
        by_chief: dict[str, list[str]] = {}
        for task_id in became_ready:
            by_chief.setdefault(state["tasks"][task_id]["chief"], []).append(task_id)
        for chief, task_ids in by_chief.items():
            notify(state, chief, source_event, task_ids)
    return became_ready


def ensure_catalog(state: dict, catalog_path: Path) -> dict:
    actual = sha256_file(catalog_path)
    expected = state["catalog"]["sha256"]
    if actual != expected:
        raise RuntimeError(f"catalog hash mismatch: state={expected}, actual={actual}")
    return read_json(catalog_path)


def cmd_init(args) -> None:
    catalog_path = Path(args.catalog).resolve()
    state_path = Path(args.state).resolve()
    control_dir = state_path.parent
    if state_path.exists() and not args.force:
        raise RuntimeError(f"state already exists: {state_path}; use --force only for a new run")
    catalog = read_json(catalog_path)
    now = utc_now()
    state = {
        "schema_version": "1",
        "run_id": args.run_id,
        "phase": "PREPARED",
        "catalog": {"path": str(catalog_path), "sha256": sha256_file(catalog_path)},
        "candidate": {"sha": None, "tree": None, "remote_ref": None},
        "chiefs": {
            "A": {"thread_id": None, "status": "UNBOUND"},
            "B": {"thread_id": None, "status": "UNBOUND"},
        },
        "sequence": 0,
        "activation_gates": {
            name: {"status": "OPEN", "evidence": None, "satisfied_at": None}
            for name in sorted(REQUIRED_ACTIVATION_GATES)
        },
        "created_at": now,
        "updated_at": now,
        "claims": {},
        "tasks": {
            item["id"]: {
                "task_id": item["id"],
                "chief": item["chief"],
                "kind": item["kind"],
                "role": item["role"],
                "dynamic": False,
                "state": "PLANNED",
                "depends_on": item["depends_on"],
                "waiting_for": list(item["depends_on"]),
                "unlocks_dependents": False,
                "attempt_id": None,
                "child_id": None,
                "claim_id": None,
                "exact_owned_paths": [],
                "base_sha": None,
                "output_sha": None,
                "tree_sha": None,
                "evidence": [],
                "blocker": None,
                "updated_at": now,
            }
            for item in catalog["contracts"]
        },
    }
    atomic_json(state_path, state)
    with locked_state(state_path) as locked:
        event = write_event(control_dir, locked, "RUN_INITIALIZED", {"catalog_sha256": locked["catalog"]["sha256"]})
        recompute_readiness(locked, catalog, control_dir, event)
        locked["updated_at"] = utc_now()
    print(json.dumps({"status": "INITIALIZED", "state": str(state_path)}, indent=2))


def cmd_bind(args) -> None:
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    with locked_state(state_path) as state:
        ensure_catalog(state, catalog_path)
        state["chiefs"][args.chief] = {"thread_id": args.thread_id, "status": "BOUND"}
        state["updated_at"] = utc_now()
        event = write_event(state_path.parent, state, "CHIEF_BOUND", {"chief": args.chief, "thread_id": args.thread_id})
    print(json.dumps(event, indent=2))


def cmd_gate(args) -> None:
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    evidence_path = Path(args.evidence).resolve()
    with locked_state(state_path) as state:
        ensure_catalog(state, catalog_path)
        if args.name not in REQUIRED_ACTIVATION_GATES:
            raise RuntimeError(f"unknown activation gate: {args.name}")
        evidence = {"path": str(evidence_path), "sha256": sha256_file(evidence_path)}
        state["activation_gates"][args.name] = {
            "status": "SATISFIED", "evidence": evidence, "satisfied_at": utc_now(),
        }
        state["updated_at"] = utc_now()
        event = write_event(state_path.parent, state, "ACTIVATION_GATE_SATISFIED", {
            "gate": args.name, "evidence": evidence,
        })
    print(json.dumps(event, indent=2))


def cmd_activate(args) -> None:
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    with locked_state(state_path) as state:
        ensure_catalog(state, catalog_path)
        if any(value["status"] != "BOUND" for value in state["chiefs"].values()):
            raise RuntimeError("both Chiefs must be bound before activation")
        open_gates = [name for name, row in state["activation_gates"].items() if row["status"] != "SATISFIED"]
        if open_gates:
            raise RuntimeError(f"activation gates remain open: {open_gates}")
        state["phase"] = "ACTIVE"
        state["updated_at"] = utc_now()
        event = write_event(state_path.parent, state, "RUN_ACTIVATED", {})
    print(json.dumps(event, indent=2))


def cmd_ready(args) -> None:
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    with locked_state(state_path) as state:
        catalog = ensure_catalog(state, catalog_path)
        recompute_readiness(state, catalog, state_path.parent, None)
        rows = [
            row for row in state["tasks"].values()
            if row["state"] == "READY" and (not args.chief or row["chief"] == args.chief)
        ]
        blocked = [
            {"task_id": row["task_id"], "chief": row["chief"], "state": row["state"],
             "waiting_for": row["waiting_for"], "blocker": row["blocker"]}
            for row in state["tasks"].values()
            if row["state"] in {"WAITING_DEPENDENCY", "BLOCKED", "REPAIR_REQUIRED"}
            and (not args.chief or row["chief"] == args.chief)
        ]
    print(json.dumps({"phase": state["phase"], "ready": rows, "not_ready": blocked}, indent=2))


def cmd_instantiate(args) -> None:
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    parameters_path = Path(args.parameters_json).resolve()
    parameters = read_json(parameters_path)
    with locked_state(state_path) as state:
        catalog = ensure_catalog(state, catalog_path)
        if args.task in state["tasks"]:
            raise RuntimeError(f"task already exists: {args.task}")
        templates = {item["id"]: item for item in catalog["templates"]}
        if args.template not in templates:
            raise RuntimeError(f"unknown template: {args.template}")
        template = templates[args.template]
        missing = [key for key in template["required_parameters"] if key not in parameters]
        if missing:
            raise RuntimeError(f"missing template parameters: {missing}")
        review_key = None
        if template["kind"] in {"junior", "security"}:
            candidate = state["candidate"]
            if not candidate.get("sha") or parameters.get("candidate_sha") != candidate["sha"] or parameters.get("tree_sha") != candidate["tree"]:
                raise RuntimeError("review must bind the registered candidate SHA and tree")
            scope = parameters.get("diff_or_scope", parameters.get("adversarial_scope"))
            review_key = hashlib.sha256(json.dumps(
                [args.template, candidate["sha"], candidate["tree"], scope], sort_keys=True
            ).encode()).hexdigest()
            for existing in state["tasks"].values():
                if existing.get("review_key") == review_key and existing["state"] not in {"INVALID_EVIDENCE", "CANCELLED"}:
                    raise RuntimeError(f"duplicate candidate/role/scope review: {existing['task_id']}")
        dependencies = args.depends_on or []
        unknown = sorted(set(dependencies) - set(state["tasks"]))
        if unknown:
            raise RuntimeError(f"unknown dependencies: {unknown}")
        if args.parent_contract not in state["tasks"]:
            raise RuntimeError(f"unknown parent contract: {args.parent_contract}")
        now = utc_now()
        state["tasks"][args.task] = {
            "task_id": args.task,
            "chief": args.chief,
            "kind": template["kind"],
            "role": catalog["roles"][template["kind"]],
            "dynamic": True,
            "template_id": args.template,
            "parent_contract_id": args.parent_contract,
            "parameters": parameters,
            "review_key": review_key,
            "acceptance": template["acceptance"],
            "state": "PLANNED",
            "depends_on": dependencies,
            "waiting_for": dependencies,
            "unlocks_dependents": False,
            "attempt_id": None,
            "child_id": None,
            "claim_id": None,
            "exact_owned_paths": [],
            "base_sha": None,
            "output_sha": None,
            "tree_sha": None,
            "evidence": [],
            "blocker": None,
            "updated_at": now,
        }
        event = write_event(state_path.parent, state, "TEMPLATE_TASK_INSTANTIATED", {
            "task_id": args.task, "template_id": args.template,
            "parent_contract_id": args.parent_contract, "chief": args.chief,
            "depends_on": dependencies, "parameters_sha256": sha256_file(parameters_path),
        })
        newly_ready = recompute_readiness(state, catalog, state_path.parent, event)
        state["updated_at"] = utc_now()
    print(json.dumps({"event": event, "newly_ready": newly_ready}, indent=2))


def cmd_claim(args) -> None:
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    paths = [normalize_path(item) for item in args.path]
    if not paths:
        raise RuntimeError("at least one exact --path is required")
    with locked_state(state_path) as state:
        ensure_catalog(state, catalog_path)
        if state["phase"] != "ACTIVE":
            raise RuntimeError(f"run is not ACTIVE: {state['phase']}")
        row = state["tasks"][args.task]
        if row["state"] not in CLAIMABLE_STATES:
            raise RuntimeError(f"{args.task} is {row['state']}, not READY")
        if row.get("review_key"):
            candidate = state["candidate"]
            if row["parameters"]["candidate_sha"] != candidate.get("sha") or args.base_sha != candidate.get("sha"):
                raise RuntimeError("stale review candidate; instantiate review for the registered candidate")
        if row["chief"] != args.chief and not args.transferred:
            raise RuntimeError(f"{args.task} belongs to Chief {row['chief']}; durable transfer required")
        for claim in state["claims"].values():
            if claim["state"] != "ACTIVE":
                continue
            for left in paths:
                for right in claim["paths"]:
                    if paths_conflict(left, right):
                        raise RuntimeError(f"path conflict with {claim['claim_id']}: {left} vs {right}")
        claim_id = args.claim_id or f"claim-{args.task.lower()}-{uuid.uuid4().hex[:12]}"
        state["claims"][claim_id] = {
            "claim_id": claim_id, "task_id": args.task, "chief": args.chief,
            "attempt_id": args.attempt_id, "paths": paths, "state": "ACTIVE",
            "acquired_at": utc_now(), "released_at": None,
        }
        stacked_on = [t for t in str(args.stacked_on or "").split(",") if t]
        if args.stacked_base and not re.fullmatch(r"[0-9a-f]{40}", args.stacked_base):
            raise RuntimeError("--stacked-base requires a full lowercase commit SHA")
        if args.stacked_base and args.stacked_base != args.base_sha:
            raise RuntimeError("--stacked-base must be the claim's --base-sha (the build stands on it)")
        if bool(args.stacked_base) != bool(stacked_on):
            raise RuntimeError("--stacked-base and --stacked-on go together")
        for dep in stacked_on:
            drow = state["tasks"].get(dep)
            if not drow or drow.get("output_sha") is None:
                raise RuntimeError(f"--stacked-on {dep}: not a row with an output_sha")
        row.update({
            "state": "CLAIMED", "attempt_id": args.attempt_id, "claim_id": claim_id,
            "exact_owned_paths": paths, "base_sha": args.base_sha, "updated_at": utc_now(),
            # D28 (BULK-RULING-2026-09-18 §10): a dependent build stands on its
            # dependency's VERIFIED output, and the ledger says so
            "stacked_base": args.stacked_base or None, "stacked_on": stacked_on,
        })
        event = write_event(state_path.parent, state, "TASK_CLAIMED", {
            "task_id": args.task, "chief": args.chief, "attempt_id": args.attempt_id,
            "claim_id": claim_id, "paths": paths,
            "stacked_base": args.stacked_base or None, "stacked_on": stacked_on,
        })
    print(json.dumps(event, indent=2))


def cmd_start(args) -> None:
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    with locked_state(state_path) as state:
        catalog = ensure_catalog(state, catalog_path)
        if state["phase"] != "ACTIVE":
            raise RuntimeError(f"run is not ACTIVE: {state['phase']}")
        row = state["tasks"][args.task]
        if row["state"] != "CLAIMED" or row["attempt_id"] != args.attempt_id:
            raise RuntimeError("task must be CLAIMED by the same attempt before start")
        contract = task_contract(state, catalog, args.task)
        expected = contract["role"]
        expected_model = expected["model"]
        if expected_model in ROUTE_BINDINGS:
            required_model, required_effort, required_agent_type = ROUTE_BINDINGS[expected_model]
        else:
            required_model, required_effort = expected_model, expected["effort"]
            required_agent_type = None
        if args.resolved_model != required_model or args.resolved_effort != required_effort:
            raise RuntimeError(
                f"route mismatch for {args.task}: expected {required_model}/{required_effort}, "
                f"got {args.resolved_model}/{args.resolved_effort}"
            )
        if required_agent_type and args.agent_type != required_agent_type:
            raise RuntimeError(
                f"agent type mismatch for {args.task}: expected {required_agent_type}, got {args.agent_type}"
            )
        row.update({
            "state": "RUNNING", "child_id": args.child_id, "updated_at": utc_now(),
            "route": {"model": args.resolved_model, "effort": args.resolved_effort,
                      "agent_type": args.agent_type},
        })
        event = write_event(state_path.parent, state, "TASK_STARTED", {
            "task_id": args.task, "attempt_id": args.attempt_id, "child_id": args.child_id,
            "resolved_model": args.resolved_model, "resolved_effort": args.resolved_effort,
            "agent_type": args.agent_type,
        })
    print(json.dumps(event, indent=2))


def cmd_adopt(args) -> None:
    """Adopt a preserved result without pretending a new child produced it."""
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    evidence = [{"path": str(Path(item).resolve()), "sha256": sha256_file(Path(item).resolve())} for item in args.evidence]
    if not evidence:
        raise RuntimeError("adoption requires hashed evidence")
    with locked_state(state_path) as state:
        catalog = ensure_catalog(state, catalog_path)
        if state["phase"] != "PREPARED":
            raise RuntimeError("preserved results may be adopted only before activation")
        row = state["tasks"][args.task]
        if row["state"] not in {"PLANNED", "WAITING_DEPENDENCY", "READY"}:
            raise RuntimeError(f"cannot adopt over existing state {row['state']}")
        if args.unlock_dependents and args.outcome not in {"VERIFIED", "INTEGRATED"}:
            raise RuntimeError("only VERIFIED or INTEGRATED adoption may unlock dependants")
        enforce_review_transition(state, catalog, row, args)
        row.update({
            "state": args.outcome,
            "unlocks_dependents": bool(args.unlock_dependents),
            "output_sha": args.output_sha,
            "tree_sha": args.tree_sha,
            "evidence": evidence,
            "blocker": args.reason if args.outcome in {"BLOCKED", "REPAIR_REQUIRED", "INVALID_EVIDENCE"} else None,
            "updated_at": utc_now(),
        })
        event = write_event(state_path.parent, state, "PRESERVED_RESULT_ADOPTED", {
            "task_id": args.task, "outcome": args.outcome,
            "unlocks_dependents": bool(args.unlock_dependents), "output_sha": args.output_sha,
            "tree_sha": args.tree_sha, "evidence": evidence, "reason": args.reason,
        })
        newly_ready = recompute_readiness(state, catalog, state_path.parent, event)
        state["updated_at"] = utc_now()
    print(json.dumps({"adoption_event": event, "newly_ready": newly_ready}, indent=2))


def cmd_block(args) -> None:
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    evidence_path = Path(args.evidence).resolve()
    with locked_state(state_path) as state:
        ensure_catalog(state, catalog_path)
        row = state["tasks"][args.task]
        if row["state"] in ACTIVE_STATES:
            raise RuntimeError("do not block a running attempt; harvest it with complete")
        if row["state"] in FINISHED_STATES and row["state"] != "BLOCKED":
            raise RuntimeError(f"cannot replace terminal state {row['state']} with a blocker")
        blocker = {
            "class": args.blocker_class,
            "reason": args.reason,
            "unblock_action": args.unblock_action,
            "evidence": {"path": str(evidence_path), "sha256": sha256_file(evidence_path)},
            "recorded_at": utc_now(),
        }
        row.update({"state": "BLOCKED", "blocker": blocker, "updated_at": utc_now()})
        event = write_event(state_path.parent, state, "TASK_BLOCKED", {
            "task_id": args.task, "blocker": blocker,
        })
    print(json.dumps(event, indent=2))


def cmd_unblock(args) -> None:
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    evidence_path = Path(args.evidence).resolve()
    with locked_state(state_path) as state:
        catalog = ensure_catalog(state, catalog_path)
        row = state["tasks"][args.task]
        if row["state"] != "BLOCKED":
            raise RuntimeError(f"{args.task} is not BLOCKED")
        previous = row["blocker"]
        row.update({"state": "PLANNED", "blocker": None, "updated_at": utc_now()})
        event = write_event(state_path.parent, state, "TASK_UNBLOCKED", {
            "task_id": args.task, "previous_blocker": previous,
            "evidence": {"path": str(evidence_path), "sha256": sha256_file(evidence_path)},
        })
        newly_ready = recompute_readiness(state, catalog, state_path.parent, event)
    print(json.dumps({"event": event, "newly_ready": newly_ready}, indent=2))


def commit_subject(repo: str, sha: str) -> str | None:
    try:
        return subprocess.check_output(["git", "-C", repo, "log", "-1", "--format=%s", sha],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def refuse_autofix_output(output_sha, repo, task_id) -> None:
    """D20 belt and braces: an integrated/union member whose output commit is a
    driver autofix (reformat) commit is contamination, never consumable.  Only
    D20's own safe-rules lint commit passes.  Needs --repo to read the subject;
    without it the check is skipped (the driver always passes its trunk)."""
    if not repo or not output_sha:
        return
    subject = commit_subject(repo, output_sha)
    if subject is None:
        raise RuntimeError(f"{task_id}: output_sha {output_sha[:12]} is not a commit in {repo}")
    if subject.startswith(AUTOFIX_SUBJECT) and not AUTOFIX_SAFE_SUBJECT.match(subject):
        raise RuntimeError(f"{task_id}: output_sha {output_sha[:12]} is an autofix commit ({subject!r}); "
                           "contaminated output is never promoted -- invalidate it as CONTAMINATED: and retry")


def cmd_promote(args) -> None:
    """Promote a verified leaf or repaired parent to consumable integrated output."""
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    evidence = [{"path": str(Path(item).resolve()), "sha256": sha256_file(Path(item).resolve())} for item in args.evidence]
    if not evidence:
        raise RuntimeError("promotion requires hashed integration/review evidence")
    with locked_state(state_path) as state:
        catalog = ensure_catalog(state, catalog_path)
        row = state["tasks"][args.task]
        if row["state"] not in {"VERIFIED", "REPAIR_REQUIRED"}:
            raise RuntimeError(f"promotion requires VERIFIED or REPAIR_REQUIRED, got {row['state']}")
        supporting = args.supporting_task or []
        if row["state"] == "REPAIR_REQUIRED" and not supporting:
            raise RuntimeError("repair-required parent needs at least one --supporting-task")
        for task_id in supporting:
            if task_id not in state["tasks"]:
                raise RuntimeError(f"unknown supporting task: {task_id}")
            support = state["tasks"][task_id]
            if support["state"] not in {"VERIFIED", "INTEGRATED"} or not support["unlocks_dependents"]:
                raise RuntimeError(f"supporting task is not consumable: {task_id}")
        previous = row["state"]
        refuse_autofix_output(args.output_sha, getattr(args, "repo", None), args.task)
        enforce_review_transition(state, catalog, row, args)
        row.update({
            "state": "INTEGRATED",
            "unlocks_dependents": True,
            "output_sha": args.output_sha,
            "tree_sha": args.tree_sha,
            "evidence": evidence,
            "blocker": None,
            "updated_at": utc_now(),
        })
        event = write_event(state_path.parent, state, "TASK_PROMOTED_TO_INTEGRATED", {
            "task_id": args.task, "previous_state": previous, "supporting_tasks": supporting,
            "output_sha": args.output_sha, "tree_sha": args.tree_sha, "evidence": evidence,
        })
        newly_ready = recompute_readiness(state, catalog, state_path.parent, event)
    print(json.dumps({"event": event, "newly_ready": newly_ready}, indent=2))


def cmd_complete(args) -> None:
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    evidence = [{"path": str(Path(item).resolve()), "sha256": sha256_file(Path(item).resolve())} for item in args.evidence]
    with locked_state(state_path) as state:
        catalog = ensure_catalog(state, catalog_path)
        row = state["tasks"][args.task]
        if row["state"] not in ACTIVE_STATES or row["attempt_id"] != args.attempt_id:
            raise RuntimeError("completion must match the active attempt")
        if args.unlock_dependents and args.outcome not in {"VERIFIED", "INTEGRATED"}:
            raise RuntimeError("only VERIFIED or INTEGRATED output may unlock dependants")
        if args.unlock_dependents and not evidence:
            raise RuntimeError("unlocking dependants requires at least one hashed evidence file")
        enforce_review_transition(state, catalog, row, args)
        row.update({
            "state": args.outcome,
            "unlocks_dependents": bool(args.unlock_dependents),
            "output_sha": args.output_sha,
            "tree_sha": args.tree_sha,
            "evidence": evidence,
            "blocker": args.reason if args.outcome in {"BLOCKED", "REPAIR_REQUIRED", "INVALID_EVIDENCE"} else None,
            "updated_at": utc_now(),
        })
        claim_id = row.get("claim_id")
        if claim_id:
            state["claims"][claim_id]["state"] = "RELEASED"
            state["claims"][claim_id]["released_at"] = utc_now()
        event = write_event(state_path.parent, state, "TASK_COMPLETED", {
            "task_id": args.task, "attempt_id": args.attempt_id, "outcome": args.outcome,
            "unlocks_dependents": bool(args.unlock_dependents), "output_sha": args.output_sha,
            "tree_sha": args.tree_sha, "evidence": evidence, "reason": args.reason,
        })
        newly_ready = recompute_readiness(state, catalog, state_path.parent, event)
        state["updated_at"] = utc_now()
    print(json.dumps({"completion_event": event, "newly_ready": newly_ready}, indent=2))


def cmd_drain(args) -> None:
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    with locked_state(state_path) as state:
        ensure_catalog(state, catalog_path)
        state["phase"] = "DRAINING"
        state["updated_at"] = utc_now()
        event = write_event(state_path.parent, state, "RUN_DRAINING", {"reason": args.reason})
    print(json.dumps(event, indent=2))


def cmd_rewire(args) -> None:
    """v13: re-point a catalog task's depends_on at the pack's dependency set
    (PACKET-FORMAT / L42 ruling: run-state's stale L42.depends_on is rewired by
    the driver's reconcile).  Only tasks that have not started; every named
    dependency must exist; the change is an event with the reason."""
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    with locked_state(state_path) as state:
        catalog = ensure_catalog(state, catalog_path)
        if args.task not in state["tasks"]:
            raise RuntimeError(f"unknown task: {args.task}")
        row = state["tasks"][args.task]
        if row["state"] not in {"PLANNED", "WAITING_DEPENDENCY", "READY"}:
            raise RuntimeError(f"{args.task} is {row['state']}; only an unstarted task can be rewired")
        deps = list(dict.fromkeys(args.depends_on or []))
        unknown = sorted(set(deps) - set(state["tasks"]))
        if unknown:
            raise RuntimeError(f"unknown dependencies: {unknown}")
        if args.task in deps:
            raise RuntimeError("a task cannot depend on itself")
        before = list(row["depends_on"])
        row["depends_on"] = deps
        row["rewired"] = {"from": before, "reason": args.reason, "at": utc_now(), "by": args.actor}
        row["updated_at"] = utc_now()
        event = write_event(state_path.parent, state, "TASK_DEPENDENCIES_REWIRED", {
            "task_id": args.task, "from": before, "to": deps, "reason": args.reason, "actor": args.actor})
        newly_ready = recompute_readiness(state, catalog, state_path.parent, event)
        state["updated_at"] = utc_now()
    print(json.dumps({"event": event, "newly_ready": newly_ready, "waiting_for":
                      state["tasks"][args.task]["waiting_for"]}, indent=2))


RETIRABLE_STATES = {"PLANNED", "WAITING_DEPENDENCY", "READY", "REPAIR_REQUIRED", "BLOCKED", "INVALID_EVIDENCE"}


def cmd_retire(args) -> None:
    """v13 closure (PACKET-FORMAT §6b): a review/repair task named in a packet's
    `closes` is retired -- CANCELLED with the closer recorded -- once the LAST
    closer is VERIFIED.  Never from an active or an accepted state; the closer
    must itself be VERIFIED/INTEGRATED."""
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    with locked_state(state_path) as state:
        catalog = ensure_catalog(state, catalog_path)
        if args.task not in state["tasks"]:
            raise RuntimeError(f"unknown task: {args.task}")
        row = state["tasks"][args.task]
        if row["state"] not in RETIRABLE_STATES:
            raise RuntimeError(f"{args.task} is {row['state']}; only {sorted(RETIRABLE_STATES)} can be retired")
        closers = list(dict.fromkeys(args.closer or []))
        for closer in closers:
            crow = state["tasks"].get(closer)
            if not crow:
                raise RuntimeError(f"unknown closer: {closer}")
            if crow["state"] not in {"VERIFIED", "INTEGRATED"}:
                raise RuntimeError(f"closer {closer} is {crow['state']}, not VERIFIED/INTEGRATED")
        if not closers:
            raise RuntimeError("at least one --closer is required")
        evidence = [str(Path(p).resolve()) for p in (args.evidence or [])]
        for p in evidence:
            if not Path(p).exists():
                raise RuntimeError(f"evidence missing: {p}")
        before = row["state"]
        row["state"] = "CANCELLED"
        row["blocker"] = {"class": "RETIRED", "reason": args.reason, "closers": closers, "from_state": before,
                          "evidence": evidence, "at": utc_now()}
        row["updated_at"] = utc_now()
        for claim in state["claims"].values():
            if claim["task_id"] == args.task and claim["state"] == "ACTIVE":
                claim["state"] = "RELEASED"
                claim["released_at"] = utc_now()
        event = write_event(state_path.parent, state, "TASK_RETIRED", {
            "task_id": args.task, "from_state": before, "closers": closers, "reason": args.reason,
            "evidence": evidence})
        state["updated_at"] = utc_now()
    print(json.dumps({"event": event, "task": args.task, "state": "CANCELLED"}, indent=2))


INVALIDATABLE_STATES = {"REPAIR_REQUIRED", "BLOCKED"}
CONTAMINATED_PREFIX = "CONTAMINATED:"
CONSUMED_STATES = {"WAITING_DEPENDENCY", "READY"} | ACTIVE_STATES | FINISHED_STATES
AUTOFIX_SUBJECT = "autofix:"
# D20's own lint commit (ruff check --fix, I001/F401/W291/W293 on the builder's
# files) is the one autofix output that may be promoted; every other autofix:
# subject (ruff format / prettier reformat, pre-D20) is contamination.
AUTOFIX_SAFE_SUBJECT = re.compile(r"^autofix: ruff I001/F401/W291/W293 \(")


def contaminated_verified_check(state, task_id, row, args, evidence_path) -> None:
    """A VERIFIED row may be invalidated ONLY as contamination (D20 sweep):
    (a) unconsumed -- no dependent row is waiting/ready/active/finished on the
        strength of it and no union lists it as a member,
    (b) --reason starts with CONTAMINATED: and the evidence file names the
        recorded output_sha, the contaminating commit and the clean commit,
    (c) not accepted and no candidate registered on top of it.
    Refuses naming the failed condition."""
    consumers = sorted(t for t, r in state["tasks"].items()
                       if task_id in (r.get("depends_on") or []) and r["state"] in CONSUMED_STATES)
    if consumers:
        raise RuntimeError(f"(a) {task_id} is consumed: dependents {consumers}")
    for members in sorted(Path(args.run_root).glob("unions/*/members.json")) if getattr(args, "run_root", None) else []:
        try:
            doc = read_json(members)
        except (OSError, ValueError):
            continue
        items = doc.get("items") or doc.get("members") or []
        names = [m.get("task") or m.get("id") if isinstance(m, dict) else m for m in items]
        if task_id in names:
            raise RuntimeError(f"(a) {task_id} is a union member: {members}")
    if not str(args.reason).startswith(CONTAMINATED_PREFIX):
        raise RuntimeError(f"(b) a VERIFIED row is invalidated only as {CONTAMINATED_PREFIX} <why>, got {args.reason!r}")
    try:
        ev = read_json(evidence_path)
    except ValueError as exc:
        raise RuntimeError(f"(b) evidence must be JSON naming output_sha/contaminating_commit/clean_commit: {exc}")
    for key in ("output_sha", "contaminating_commit", "clean_commit"):
        if not re.fullmatch(r"[0-9a-f]{7,40}", str(ev.get(key) or "")):
            raise RuntimeError(f"(b) evidence lacks {key}")
    recorded = str(row.get("output_sha") or "")
    if not recorded or not recorded.startswith(ev["output_sha"]) and not ev["output_sha"].startswith(recorded):
        raise RuntimeError(f"(b) evidence output_sha {ev['output_sha']} is not the recorded {recorded or 'none'}")
    if not (recorded.startswith(ev["contaminating_commit"]) or ev["contaminating_commit"].startswith(recorded)):
        raise RuntimeError("(b) contaminating_commit must be the recorded output_sha (the reformat commit on top)")
    if ev["clean_commit"] == ev["contaminating_commit"]:
        raise RuntimeError("(b) clean_commit must differ from the contaminating commit")
    if row["state"] != "VERIFIED" or row.get("accepted"):
        raise RuntimeError(f"(c) {task_id} is {row['state']}; accepted rows are never invalidated")
    cand = state.get("candidate") or {}
    if cand.get("sha") and str(cand.get("registered_at") or "") > str(row.get("updated_at") or ""):
        raise RuntimeError(f"(c) candidate {cand['sha'][:12]} was registered on top of {task_id}")


def cmd_invalidate(args) -> None:
    """v13: a finished row whose verdict was never a verdict (a wrong-mode
    dispatch, an environment failure the driver misfiled as REPAIR_REQUIRED)
    becomes INVALID_EVIDENCE -- the state the duplicate-review rule and
    retry-packet already treat as "not a result".  REPAIR_REQUIRED|BLOCKED
    only; never an active or an accepted row; the reason and a hashed
    evidence file are recorded on the row and in a TASK_INVALIDATED event.
    A hand edit of run-state.json would skip the event/sequence authority,
    which is why this verb exists."""
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    evidence_path = Path(args.evidence).resolve()
    if not evidence_path.is_file():
        raise RuntimeError(f"evidence missing: {evidence_path}")
    with locked_state(state_path) as state:
        ensure_catalog(state, catalog_path)
        if args.task not in state["tasks"]:
            raise RuntimeError(f"unknown task: {args.task}")
        row = state["tasks"][args.task]
        if row["state"] in ACTIVE_STATES:
            raise RuntimeError("do not invalidate a running attempt; harvest it with complete")
        klass = "INVALIDATED"
        if row["state"] == "VERIFIED":
            contaminated_verified_check(state, args.task, row, args, evidence_path)
            klass = "CONTAMINATED"
        elif row["state"] not in INVALIDATABLE_STATES:
            raise RuntimeError(f"{args.task} is {row['state']}; only {sorted(INVALIDATABLE_STATES)} "
                               f"(or an unconsumed VERIFIED row as {CONTAMINATED_PREFIX}) can be invalidated")
        before = row["state"]
        record = {"class": klass, "reason": args.reason, "from_state": before,
                  "previous_blocker": row.get("blocker"),
                  "evidence": {"path": str(evidence_path), "sha256": sha256_file(evidence_path)},
                  "at": utc_now()}
        if klass == "CONTAMINATED":
            record["output_sha"] = row.get("output_sha")
        row.update({"state": "INVALID_EVIDENCE", "blocker": record, "unlocks_dependents": False,
                    "updated_at": utc_now()})
        for claim in state["claims"].values():
            if claim["task_id"] == args.task and claim["state"] == "ACTIVE":
                claim["state"] = "RELEASED"
                claim["released_at"] = utc_now()
        payload = {"task_id": args.task, "from_state": before, "reason": args.reason,
                   "class": klass, "evidence": record["evidence"]}
        if klass == "CONTAMINATED":
            payload["output_sha"] = record["output_sha"]
        event = write_event(state_path.parent, state, "TASK_INVALIDATED", payload)
        state["updated_at"] = utc_now()
    print(json.dumps({"event": event, "task": args.task, "state": "INVALID_EVIDENCE"}, indent=2))


MIGRATION_FLOOR = 266  # highest file at the v13 base; 267 never existed (F13/B9)
MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def migration_ceiling(state: dict, base_dir: str | None) -> tuple[int, str]:
    """The highest number that is taken: the roster floor, the highest file at
    base (when a migrations directory is given) and every earlier allocation."""
    ceiling, source = MIGRATION_FLOOR, "floor"
    if base_dir:
        numbers = [int(m.group(1)) for m in
                   (MIGRATION_NAME.match(p.name) for p in Path(base_dir).iterdir()) if m]
        if numbers and max(numbers) > ceiling:
            ceiling, source = max(numbers), "base"
    taken = [int(n) for n in state.get("migrations", {})]
    if taken and max(taken) > ceiling:
        ceiling, source = max(taken), "allocated"
    return ceiling, source


def cmd_migration(args) -> None:
    """F13: the single migration-number allocator.  A packet never picks a
    number; it calls `migration allocate --task <ID>` and writes the file with
    the number returned.  Idempotent per task unless --another is given."""
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    with locked_state(state_path) as state:
        ensure_catalog(state, catalog_path)
        migrations = state.setdefault("migrations", {})
        if args.migration_cmd == "list":
            print(json.dumps({"ceiling": migration_ceiling(state, args.base_dir)[0],
                              "allocations": migrations}, indent=2))
            return
        if args.task not in state["tasks"]:
            raise SystemExit(f"unknown task: {args.task}")
        row = state["tasks"][args.task]
        if row["state"] in FINISHED_STATES:
            raise SystemExit(f"{args.task} is {row['state']}; a finished task allocates nothing")
        mine = sorted(int(n) for n, rec in migrations.items() if rec["task"] == args.task)
        if mine and not args.another:
            number = mine[-1]
            print(json.dumps({"number": number, "name": f"{number:03d}", "task": args.task,
                              "reused": True, "slug": migrations[str(number)].get("slug")}, indent=2))
            return
        ceiling, source = migration_ceiling(state, args.base_dir)
        number = ceiling + 1
        migrations[str(number)] = {"task": args.task, "attempt_id": row.get("attempt_id"),
                                   "slug": args.slug, "allocated_at": utc_now(),
                                   "ceiling": ceiling, "ceiling_source": source}
        state["updated_at"] = utc_now()
        event = write_event(state_path.parent, state, "MIGRATION_ALLOCATED",
                            {"task": args.task, "number": number, "ceiling": ceiling,
                             "ceiling_source": source, "slug": args.slug})
    print(json.dumps({"number": number, "name": f"{number:03d}", "task": args.task, "reused": False,
                      "ceiling": ceiling, "ceiling_source": source, "slug": args.slug,
                      "event_id": event["event_id"]}, indent=2))


def cmd_register_candidate(args) -> None:
    """Pin an integrated commit for review, without claiming release acceptance."""
    state_path = Path(args.state).resolve()
    packet_path = Path(args.packet).resolve()
    packet = read_json(packet_path)
    sha, tree = packet.get("candidate_sha", ""), packet.get("tree_sha", "")
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (sha, tree)):
        raise RuntimeError("candidate requires full lowercase commit and tree SHAs")
    repo = Path(args.repo).resolve()
    def git(expression):
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "--verify", expression], text=True
        ).strip()
    if git(sha + "^{commit}") != sha or git(sha + "^{tree}") != tree:
        raise RuntimeError("candidate SHA/tree does not match repository")
    artifacts = []
    for item in packet.get("artifact_manifest", {}).values():
        artifact = Path(args.artifact_root).resolve() / item["path"]
        if sha256_file(artifact) != item["sha256"]:
            raise RuntimeError(f"candidate artifact hash mismatch: {artifact}")
        artifacts.append({"path": str(artifact), "sha256": item["sha256"]})
    if not artifacts:
        raise RuntimeError("candidate packet requires hashed integration artifacts")
    with locked_state(state_path) as state:
        ensure_catalog(state, Path(args.catalog).resolve())
        if packet.get("contract_revision") != state["catalog"]["sha256"]:
            raise RuntimeError("candidate contract revision differs from active catalog")
        previous = state["candidate"]
        if previous.get("sha") == sha and previous.get("tree") == tree:
            print(json.dumps({"status": "ALREADY_REGISTERED", "candidate": previous}, indent=2))
            return
        if previous.get("sha") and args.expected_previous != previous["sha"]:
            raise RuntimeError("candidate replacement requires matching --expected-previous")
        if any(row["state"] in ACTIVE_STATES for row in state["tasks"].values()):
            raise RuntimeError("drain active attempts before changing the review candidate")
        candidate = {
            "sha": sha, "tree": tree, "remote_ref": None,
            "status": "PINNED_FOR_REVIEW_NOT_ACCEPTED", "repository": str(repo),
            "packet": {"path": str(packet_path), "sha256": sha256_file(packet_path)},
            "artifacts": artifacts, "registered_at": utc_now(),
            "registered_by": args.actor,
            "legacy_source_thread": packet.get("from_chief"),
        }
        if previous.get("sha"):
            state.setdefault("candidate_history", []).append(previous)
        state["candidate"] = candidate
        event = write_event(state_path.parent, state, "CANDIDATE_REGISTERED", {
            "candidate": candidate, "previous_sha": previous.get("sha"),
        })
        for chief in state["chiefs"]:
            message = {
                "message_id": f"candidate-{event['event_id']}-{chief}",
                "type": "CANDIDATE_REGISTERED", "to_chief": chief,
                "candidate_sha": sha, "tree_sha": tree,
                "source_event_id": event["event_id"], "created_at": event["created_at"],
                "required_action": "Reconcile review/CI tasks against this candidate; registration is not acceptance.",
            }
            state.setdefault("publications", {})[
                f"inbox/chief-{chief.lower()}/{message['message_id']}.json"
            ] = message
        state["updated_at"] = utc_now()
    print(json.dumps({"status": "REGISTERED", "candidate": candidate}, indent=2))


def cmd_bind_controller(args) -> None:
    """One parent may schedule both responsibility queues after a clean drain."""
    state_path = Path(args.state).resolve()
    with locked_state(state_path) as state:
        ensure_catalog(state, Path(args.catalog).resolve())
        if state["phase"] != "DRAINING" or any(
            row["state"] in ACTIVE_STATES for row in state["tasks"].values()
        ) or any(claim["state"] == "ACTIVE" for claim in state["claims"].values()):
            raise RuntimeError("single-controller binding requires DRAINING with no active attempts or claims")
        previous = state["chiefs"]
        state["chiefs"] = {chief: {"thread_id": args.thread_id, "status": "BOUND"} for chief in previous}
        state["controller"] = {"thread_id": args.thread_id, "mode": "SINGLE_PARENT_TWO_QUEUES",
                               "bound_at": utc_now(), "previous_chiefs": previous}
        event = write_event(state_path.parent, state, "CONTROLLER_BOUND", state["controller"])
    print(json.dumps(event, indent=2))


def cmd_reconcile(args) -> None:
    state_path = Path(args.state).resolve()
    with locked_state(state_path) as state:
        catalog = ensure_catalog(state, Path(args.catalog).resolve())
        errors = verify_state(state, catalog)
        if errors:
            raise RuntimeError(f"state verification failed: {errors}")
        for row in state["tasks"].values():
            if row["state"] == "INTEGRATED":
                row["review_status"] = "PASS" if current_review(state, row, "junior") else "PENDING_RECONCILIATION"
        event = write_event(state_path.parent, state, "STATE_RECONCILED", {})
        ready = recompute_readiness(state, catalog, state_path.parent, event)
    print(json.dumps({"status": "RECONCILED", "newly_ready": ready}, indent=2))


def cmd_record_review(args):
    state_path = Path(args.state).resolve()
    with locked_state(state_path) as state:
        catalog = ensure_catalog(state, Path(args.catalog).resolve())
        row = state["tasks"][args.task]
        record = review_gate.validate(args.verdict, state, catalog, args.task, args.role)
        row.setdefault("reviews", {})[args.role] = record
        if args.role == "junior":
            row["review_status"] = "PASS"
        event = write_event(state_path.parent, state, "REVIEW_RECORDED", {"task_id": args.task, "review": record})
        newly_ready = recompute_readiness(state, catalog, state_path.parent, event)
    print(json.dumps({"event": event, "newly_ready": newly_ready}, indent=2))


def cmd_frontier(args) -> None:
    """Read-only liveness report. Empty READY is never a completion verdict."""
    state = read_json(Path(args.state))
    catalog = ensure_catalog(state, Path(args.catalog).resolve())
    actions = []
    for task_id, row in state["tasks"].items():
        if row["state"] in {"READY", "REPAIR_REQUIRED", "BLOCKED", "WAITING_DEPENDENCY"}:
            actions.append({"task": task_id, "state": row["state"],
                            "waiting_for": row.get("waiting_for", []),
                            "blocker": row.get("blocker")})
    candidate = state["candidate"]
    reviews = [key for key, row in state["tasks"].items()
               if row.get("parameters", {}).get("candidate_sha") == candidate.get("sha")
               and candidate.get("sha") and row.get("template_id") in {"JUNIOR_REVIEW", "SECURITY_REVIEW"}
               and row["state"] not in {"CANCELLED", "INVALID_EVIDENCE"}]
    counts = {status: sum(row["state"] == status for row in state["tasks"].values())
              for status in sorted({row["state"] for row in state["tasks"].values()})}
    warnings = []
    if not candidate.get("sha"):
        warnings.append("NO_REGISTERED_CANDIDATE: inspect integration packets; do not wait for another Chief")
    elif not reviews:
        warnings.append("NO_CANDIDATE_REVIEW_TASKS: instantiate approved review contracts")
    if not candidate.get("remote_ref"):
        warnings.append("REMOTE_REACHABILITY_UNPROVEN: resolve CI publication/trigger authority")
    if counts.get("VERIFIED", 0):
        warnings.append("VERIFIED_OUTPUTS_REQUIRE_DISPOSITION: compare integration manifest before any repeat work")
    print(json.dumps({"sequence": state["sequence"], "phase": state["phase"],
                      "candidate": candidate, "counts": counts, "actions": actions,
                      "warnings": warnings, "integrity_errors": verify_state(state, catalog),
                      "completion": "NOT_ASSESSED_REQUIREMENT_AND_RELEASE_GATES_REQUIRED",
                      "runtime_wakeup": "NOT_PROVIDED_BY_FILE_SCHEDULER"}, indent=2))


def verify_state(state: dict, catalog: dict) -> list[str]:
    errors: list[str] = []
    known = set(catalog_tasks(catalog))
    if not known <= set(state["tasks"]):
        errors.append("state is missing fixed catalog task IDs")
    for task_id, row in state["tasks"].items():
        if task_id not in known and not row.get("dynamic"):
            errors.append(f"unknown non-dynamic task {task_id}")
    active_claims = [claim for claim in state["claims"].values() if claim["state"] == "ACTIVE"]
    for index, left in enumerate(active_claims):
        for right in active_claims[index + 1:]:
            if any(paths_conflict(a, b) for a in left["paths"] for b in right["paths"]):
                errors.append(f"active claims conflict: {left['claim_id']} and {right['claim_id']}")
    for task_id, row in state["tasks"].items():
        if row["state"] == "READY":
            satisfied, waiting = dependency_status(state, task_contract(state, catalog, task_id))
            if not satisfied:
                errors.append(f"{task_id} READY while waiting for {waiting}")
        if row["unlocks_dependents"] and row["state"] not in {"VERIFIED", "INTEGRATED"}:
            errors.append(f"{task_id} unlocks dependants from invalid state {row['state']}")
    return errors


def cmd_verify(args) -> None:
    state_path = Path(args.state).resolve()
    catalog_path = Path(args.catalog).resolve()
    state = read_json(state_path)
    catalog = ensure_catalog(state, catalog_path)
    errors = verify_state(state, catalog)
    print(json.dumps({"status": "PASS" if not errors else "FAIL", "errors": errors}, indent=2))
    if errors:
        raise SystemExit(1)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--state", default="orchestration-state/run-state.json")
    result.add_argument("--catalog", default="lane-contracts.json")
    commands = result.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--run-id", required=True)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    bind = commands.add_parser("bind-chief")
    bind.add_argument("--chief", choices=["A", "B"], required=True)
    bind.add_argument("--thread-id", required=True)
    bind.set_defaults(func=cmd_bind)

    gate = commands.add_parser("satisfy-gate")
    gate.add_argument("--name", choices=sorted(REQUIRED_ACTIVATION_GATES), required=True)
    gate.add_argument("--evidence", required=True)
    gate.set_defaults(func=cmd_gate)

    activate = commands.add_parser("activate")
    activate.set_defaults(func=cmd_activate)

    ready = commands.add_parser("ready")
    ready.add_argument("--chief", choices=["A", "B"])
    ready.set_defaults(func=cmd_ready)

    instantiate = commands.add_parser("instantiate")
    instantiate.add_argument("--template", required=True)
    instantiate.add_argument("--task", required=True)
    instantiate.add_argument("--parent-contract", required=True)
    instantiate.add_argument("--chief", choices=["A", "B"], required=True)
    instantiate.add_argument("--depends-on", action="append", default=[])
    instantiate.add_argument("--parameters-json", required=True)
    instantiate.set_defaults(func=cmd_instantiate)

    claim = commands.add_parser("claim")
    claim.add_argument("--task", required=True)
    claim.add_argument("--chief", choices=["A", "B"], required=True)
    claim.add_argument("--attempt-id", required=True)
    claim.add_argument("--claim-id")
    claim.add_argument("--base-sha", required=True)
    claim.add_argument("--path", action="append", default=[])
    claim.add_argument("--transferred", action="store_true")
    claim.add_argument("--stacked-base", help="D28: the dependency output (== --base-sha) the build stands on")
    claim.add_argument("--stacked-on", help="D28: comma-separated task ids whose outputs --stacked-base carries")
    claim.set_defaults(func=cmd_claim)

    start = commands.add_parser("start")
    start.add_argument("--task", required=True)
    start.add_argument("--attempt-id", required=True)
    start.add_argument("--child-id", required=True)
    start.add_argument("--resolved-model", required=True)
    start.add_argument("--resolved-effort", required=True)
    start.add_argument("--agent-type")
    start.set_defaults(func=cmd_start)

    adopt = commands.add_parser("adopt")
    adopt.add_argument("--task", required=True)
    adopt.add_argument("--outcome", choices=sorted(VALID_OUTCOMES), required=True)
    adopt.add_argument("--unlock-dependents", action="store_true")
    adopt.add_argument("--output-sha")
    adopt.add_argument("--tree-sha")
    adopt.add_argument("--evidence", action="append", default=[])
    adopt.add_argument("--reason")
    adopt.add_argument("--verdict")
    adopt.set_defaults(func=cmd_adopt)

    block = commands.add_parser("block")
    block.add_argument("--task", required=True)
    block.add_argument("--blocker-class", choices=["AUTHORITY", "EXTERNAL", "ROUTE", "CONFLICT", "NEW_SCOPE"], required=True)
    block.add_argument("--reason", required=True)
    block.add_argument("--unblock-action", required=True)
    block.add_argument("--evidence", required=True)
    block.set_defaults(func=cmd_block)

    unblock = commands.add_parser("unblock")
    unblock.add_argument("--task", required=True)
    unblock.add_argument("--evidence", required=True)
    unblock.set_defaults(func=cmd_unblock)

    promote = commands.add_parser("promote")
    promote.add_argument("--task", required=True)
    promote.add_argument("--supporting-task", action="append", default=[])
    promote.add_argument("--output-sha", required=True)
    promote.add_argument("--tree-sha", required=True)
    promote.add_argument("--evidence", action="append", default=[])
    promote.add_argument("--verdict")
    promote.add_argument("--repo", help="D20: refuse an autofix: output commit (subject read here)")
    promote.set_defaults(func=cmd_promote)

    complete = commands.add_parser("complete")
    complete.add_argument("--task", required=True)
    complete.add_argument("--attempt-id", required=True)
    complete.add_argument("--outcome", choices=sorted(VALID_OUTCOMES), required=True)
    complete.add_argument("--unlock-dependents", action="store_true")
    complete.add_argument("--output-sha")
    complete.add_argument("--tree-sha")
    complete.add_argument("--evidence", action="append", default=[])
    complete.add_argument("--reason")
    complete.add_argument("--verdict")
    complete.set_defaults(func=cmd_complete)

    drain = commands.add_parser("drain")
    drain.add_argument("--reason", required=True)
    drain.set_defaults(func=cmd_drain)

    verify = commands.add_parser("verify")
    verify.set_defaults(func=cmd_verify)

    register = commands.add_parser("register-candidate")
    register.add_argument("--repo", required=True)
    register.add_argument("--packet", required=True)
    register.add_argument("--artifact-root", required=True)
    register.add_argument("--actor", required=True)
    register.add_argument("--expected-previous")
    register.set_defaults(func=cmd_register_candidate)

    controller = commands.add_parser("bind-controller")
    controller.add_argument("--thread-id", required=True)
    controller.set_defaults(func=cmd_bind_controller)

    reconcile = commands.add_parser("reconcile")
    reconcile.set_defaults(func=cmd_reconcile)
    record = commands.add_parser("record-review")
    record.add_argument("--task", required=True)
    record.add_argument("--role", choices=sorted(review_gate.ROLES), required=True)
    record.add_argument("--verdict", required=True)
    record.set_defaults(func=cmd_record_review)
    frontier = commands.add_parser("frontier")
    frontier.set_defaults(func=cmd_frontier)
    rewire = commands.add_parser("rewire", help="v13: re-point a catalog task's depends_on at the pack's set")
    rewire.add_argument("--task", required=True)
    rewire.add_argument("--depends-on", nargs="*", default=[])
    rewire.add_argument("--reason", required=True)
    rewire.add_argument("--actor", default="lanedriver")
    rewire.set_defaults(func=cmd_rewire)
    retire = commands.add_parser("retire", help="v13 §6b: retire a review/repair task once its last closer is VERIFIED")
    retire.add_argument("--task", required=True)
    retire.add_argument("--closer", action="append", default=[], help="VERIFIED/INTEGRATED task(s) that close it")
    retire.add_argument("--reason", required=True)
    retire.add_argument("--evidence", action="append", default=[])
    retire.set_defaults(func=cmd_retire)
    invalidate = commands.add_parser("invalidate", help="v13: REPAIR_REQUIRED|BLOCKED -> INVALID_EVIDENCE when the "
                                                       "recorded verdict was never a verdict (hashed evidence required)")
    invalidate.add_argument("--task", required=True)
    invalidate.add_argument("--reason", required=True)
    invalidate.add_argument("--evidence", required=True)
    invalidate.add_argument("--run-root", help="D20: unions/*/members.json are scanned for a VERIFIED row")
    invalidate.set_defaults(func=cmd_invalidate)
    migration = commands.add_parser("migration", help="F13: single migration-number allocator")
    msub = migration.add_subparsers(dest="migration_cmd", required=True)
    allocate = msub.add_parser("allocate")
    allocate.add_argument("--task", required=True, help="owner task; idempotent per task")
    allocate.add_argument("--slug", help="snake slug the file will carry (recorded only)")
    allocate.add_argument("--base-dir", help="platform/db/migrations at base; raises the ceiling")
    allocate.add_argument("--another", action="store_true",
                          help="allocate a second number for a task that already holds one")
    allocate.set_defaults(func=cmd_migration)
    listing = msub.add_parser("list")
    listing.add_argument("--base-dir")
    listing.set_defaults(func=cmd_migration)
    return result


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
