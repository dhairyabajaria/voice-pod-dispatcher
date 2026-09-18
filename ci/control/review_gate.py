"""Candidate-bound review validation; file existence is never a positive verdict."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

ROLES = {"junior": ("gpt-5.6-luna", "xhigh"),
         "security": ("gpt-5.6-sol", "high"),
         "final_review": ("gpt-5.6-sol", "high")}
# Accepted model strings per role beyond ROLES[role][0].  final_review may run
# on the 1M-context variant of Sol (06-ROUTING.md whole-candidate review); the
# effort stays "high" and a session must not switch between the two mid-review.
MODEL_ALIASES = {"final_review": ("gpt-5.6-sol", "gpt-5.6-sol-1m")}


def accepted_models(role):
    return tuple(dict.fromkeys((ROLES[role][0],) + MODEL_ALIASES.get(role, ())))
RUNTIME_ROOTS = (Path.home() / ".codex/sessions", Path.home() / ".codex/archived_sessions")


def load(path):
    return json.loads(Path(path).read_text())


def hashed(reference):
    path = Path(reference["path"])
    expected = reference.get("sha256")
    if not expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f"missing/mismatched artifact hash: {path}")
    return path


def _git(repo, args):
    import subprocess
    done = subprocess.run(["git", "-C", str(repo)] + list(args), text=True, capture_output=True)
    return done.returncode, done.stdout.strip()


def _union_subject(packet, candidate, role):
    """D30 (BULK-RULING §12 item 3): a junior/security review of a UNION TIP
    is keyed by that tip -- packet.union_sha == candidate_sha, tree_sha its
    tree -- and the tip must descend from the registered candidate in the
    candidate's repository.  final_review stays bound to the registered
    candidate itself.  Returns True when the packet is such a review."""
    tip = packet.get("union_sha")
    if role == "final_review" or not tip:
        return False
    if packet.get("candidate_sha") != tip:
        raise ValueError("union review must be keyed by its union tip")
    repo = candidate.get("repository")
    if not repo or not Path(repo).exists():
        raise ValueError("union review needs the candidate repository to verify the tip")
    rc, _ = _git(repo, ["merge-base", "--is-ancestor", candidate["sha"], tip])
    if rc != 0:
        raise ValueError("union tip does not descend from the registered candidate")
    rc, tree = _git(repo, ["rev-parse", tip + "^{tree}"])
    if rc != 0 or tree != packet.get("tree_sha"):
        raise ValueError("union tip tree mismatch")
    return True


def validate(packet_path, state, catalog, target, role):
    packet = load(packet_path)
    candidate = state["candidate"]
    if role not in ROLES or packet.get("role") != role:
        raise ValueError("wrong review role")
    if not candidate.get("sha"):
        raise ValueError("stale or missing review candidate")
    union = _union_subject(packet, candidate, role)
    if not union and (packet.get("candidate_sha"), packet.get("tree_sha")) != (candidate["sha"], candidate["tree"]):
        raise ValueError("stale or missing review candidate")
    if packet.get("verdict") not in {"PASS", "ACCEPT"} or packet.get("unresolved_blocking_findings") != []:
        raise ValueError("review is not a clean PASS/ACCEPT")
    if packet.get("catalog_sha256") != state["catalog"]["sha256"]:
        raise ValueError("review catalog mismatch")
    reviewer = packet.get("reviewer_session_id")
    authors = packet.get("author_session_ids")
    if not reviewer or not isinstance(authors, list) or not authors or reviewer in authors:
        raise ValueError("independent author/reviewer identities required")
    fixed = {c["id"]: c for c in catalog["contracts"]}
    def kind(task_id, row):
        return row.get("kind") or fixed.get(task_id, {}).get("kind")
    known_authors = {row.get("child_id") for task_id, row in state["tasks"].items()
                     if kind(task_id, row) not in ROLES and row.get("child_id")}
    known_authors.update(row.get("thread_id") for row in state.get("chiefs", {}).values())
    if reviewer in known_authors:
        raise ValueError("reviewer is a recorded builder/controller")
    expected_model, expected_effort = ROLES[role]
    runtime = hashed(packet["runtime_log"])
    if not any(runtime.resolve().is_relative_to(root.resolve()) for root in RUNTIME_ROOTS):
        raise ValueError("runtime proof must be an original native session log, not a child-authored model assertion")
    records = [json.loads(line) for line in runtime.read_text().splitlines() if line.strip()]
    sessions = {r["payload"].get("id") for r in records if r.get("type") == "session_meta"}
    contexts = [r["payload"] for r in records if r.get("type") == "turn_context"]
    if not _sessions_match_reviewer_chain(records, sessions, reviewer) or not contexts:
        raise ValueError("runtime session identity/turn evidence missing")
    post_fork = _post_fork_contexts(records)
    models = {r.get("model") for r in post_fork}
    if not post_fork or len(models) != 1 or next(iter(models)) not in accepted_models(role) \
            or any(r.get("effort") != expected_effort for r in post_fork):
        raise ValueError("runtime model/effort mismatch")
    # Only post-fork turns must use this route; pre-fork inherited turns are
    # the parent's history, not the reviewer's own work (see _post_fork_contexts).
    if packet.get("review_task_id") not in state["tasks"]:
        raise ValueError("review dispatch task missing")
    review_task = state["tasks"][packet["review_task_id"]]
    if review_task.get("child_id") != reviewer or kind(packet["review_task_id"], review_task) != role:
        raise ValueError("review dispatch/session linkage mismatch")
    contract = next((c for c in catalog["contracts"] if c["id"] == target), None)
    required = list(dict.fromkeys(contract.get("verification", []) + contract.get("acceptance", []))) if contract else state["tasks"][target].get("acceptance", [])
    if not required:
        raise ValueError("target has no machine-readable criteria")
    coverage = packet.get("coverage", {}).get(target, {})
    if any(coverage.get(criterion) != "PASS" for criterion in required):
        raise ValueError("missing/non-PASS target criterion coverage")
    artifacts = packet.get("artifacts")
    if not artifacts:
        raise ValueError("review supporting artifacts missing")
    for artifact in artifacts:
        hashed(artifact)
    return {"status": "PASS", "role": role, "candidate_sha": packet["candidate_sha"],
            "tree_sha": packet["tree_sha"], "reviewer_session_id": reviewer,
            "union": packet.get("union") if union else None, "registered_candidate": candidate["sha"],
            "packet": {"path": str(Path(packet_path).resolve()),
                       "sha256": hashlib.sha256(Path(packet_path).read_bytes()).hexdigest()}}
def _record_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _reviewer_chain(records, reviewer):
    """Reviewer session plus ancestors via parent_thread_id/forked_from_id.

    Forked native logs always carry the reviewer meta alongside the parent
    metas it was forked from, so the sessions check accepts the reviewer
    alone or the reviewer plus reachable ancestors; anything else fails.
    """
    metas = {}
    for row in records:
        if not isinstance(row, dict) or row.get("type") != "session_meta":
            continue
        payload = row.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("id"), str):
            metas[payload["id"]] = payload
    chain = {reviewer}
    stack = [reviewer]
    while stack:
        payload = metas.get(stack.pop(), {})
        for key in ("parent_thread_id", "forked_from_id"):
            parent = payload.get(key)
            if isinstance(parent, str) and parent not in chain:
                chain.add(parent)
                stack.append(parent)
    return chain


def _sessions_match_reviewer_chain(records, sessions, reviewer):
    if reviewer not in sessions:
        return False
    chain = _reviewer_chain(records, reviewer)
    if not set(sessions) <= chain:
        return False
    for row in records:
        if not isinstance(row, dict) or row.get("type") != "turn_context":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        owner = payload.get("session_id")
        if isinstance(owner, str) and owner not in chain:
            return False
    return True


def _as_ordinal(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _fork_cutoff(records):
    """Max fork-boundary ordinal across session_meta rows, or None.

    Both forked_from_ordinal_exclusive and subagent_history_start_ordinal mark
    the inherited-history boundary. Keying on the first session_meta record
    ordinal is wrong: in live pilot JR-REVIEW-11 that ordinal is 0 while the
    true boundary (subagent_history_start_ordinal) is 15, which admitted
    inherited go3 turns at ordinals 6/11 as post-fork. None means neither
    field exists anywhere, and callers keep legacy all-rows behavior.
    """
    cuts = []
    for row in records:
        if not isinstance(row, dict) or row.get("type") != "session_meta":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        for key in ("forked_from_ordinal_exclusive", "subagent_history_start_ordinal"):
            value = _as_ordinal(payload.get(key))
            if value is not None:
                cuts.append(value)
    return max(cuts) if cuts else None


def _post_fork_contexts(records):
    """Return the turn_context payloads attributable to the review session itself.

    Post-fork rule: a forked native session log carries copies of the parent's
    pre-fork turn_context rows, so only turns recorded after the fork prove the
    reviewer's own model/effort route. A turn_context row counts as post-fork
    iff its record ordinal is strictly greater than the cutoff, where the
    cutoff is the max over session_meta rows of forked_from_ordinal_exclusive
    and subagent_history_start_ordinal. Rows without a record ordinal cannot
    be ordered, so they stay in the checked set (fail-closed). When neither
    field exists anywhere, every turn_context counts, preserving the legacy
    behavior for old logs.
    Only the model/effort homogeneity check uses this filtered list;
    session-identity, linkage, candidate, coverage, and hash checks all keep
    using the unfiltered records.
    """
    turns = [r for r in records if isinstance(r, dict) and r.get("type") == "turn_context"]
    cutoff = _fork_cutoff(records)
    if cutoff is None:
        return [r["payload"] for r in turns]
    checked = []
    for row in turns:
        ordinal = row.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, (int, float)):
            checked.append(row["payload"])
        elif ordinal > cutoff:
            checked.append(row["payload"])
    return checked
