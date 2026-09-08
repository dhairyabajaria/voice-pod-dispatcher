#!/usr/bin/env python3
"""Read exact Codex session/turn evidence when desktop status omits messages.

Read-only. A finished model turn is not product acceptance. Never redispatch or
kill a worker based on a missing log/heartbeat; report the evidence gap instead.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid


def uuid_text(value):
    return str(uuid.UUID(value))


def locate(session, roots):
    session = uuid_text(session)
    paths = sorted({p.resolve() for root in roots for p in Path(root).rglob(f"*{session}.jsonl")})
    if len(paths) != 1:
        raise ValueError(f"Expected one exact session log; found {len(paths)}")
    return paths[0]


def inspect(path, session, turn=None, include_text=False):
    session = uuid_text(session)
    if turn:
        turn = uuid_text(turn)
    turns = {}
    current = None
    identity = None
    incomplete_tail = False
    malformed = 0
    with open(path) as stream:
        for line in stream:
            if not line.endswith("\n"):
                incomplete_tail = True
                continue  # writer may still be flushing the final event
            try:
                event = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            payload = event.get("payload") or {}
            kind = event.get("type")
            stamp = event.get("timestamp")
            if kind == "session_meta":
                found = payload.get("id") or payload.get("session_id")
                if identity is not None and identity != found:
                    raise ValueError("Conflicting session identities")
                identity = found
            if kind == "event_msg" and payload.get("type") == "task_started":
                current = payload.get("turn_id")
                turns[current] = {"turn_id": current, "status": "in_progress",
                                  "started_at": stamp, "last_observed_event_at": stamp,
                                  "tool_events": 0}
            if kind == "turn_context":
                current = payload.get("turn_id") or current
                row = turns.setdefault(current, {"turn_id": current, "status": "unknown", "tool_events": 0})
                row.update(model=payload.get("model"), effort=payload.get("effort"))
            if current not in turns:
                continue
            row = turns[current]
            if kind == "response_item":
                t = payload.get("type", "")
                if t in {"function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"}:
                    row["tool_events"] += 1
                    row["last_observed_event_at"] = stamp
                if t == "message" and payload.get("role") == "assistant":
                    row["last_observed_event_at"] = stamp
                    phase = payload.get("phase")
                    row["last_message_phase"] = phase
                    if phase in {"final", "final_answer"}:
                        row["final_message_present"] = True
                        if include_text:
                            row["final_text"] = "\n".join(x.get("text", "") for x in payload.get("content", [])
                                                          if x.get("type") in {"output_text", "text"})
            if kind == "event_msg" and payload.get("type") in {"task_complete", "turn_aborted"}:
                target = payload.get("turn_id")
                # Explicit identity required: never let a stale completion close a newer turn.
                if target in turns:
                    row = turns[target]
                    row["status"] = "completed" if payload["type"] == "task_complete" else "cancelled"
                    row["completed_at"] = stamp
                    if payload.get("last_agent_message"):
                        row["final_message_present"] = True
                        if include_text:
                            row["final_text"] = payload["last_agent_message"]
    if identity != session:
        raise ValueError("Session metadata does not match requested identity")
    chosen = turn or current
    if chosen not in turns:
        raise ValueError("Requested turn is absent; refusing to substitute an older turn")
    result = dict(turns[chosen], session_id=session, source=str(path),
                  evidence_complete=not (incomplete_tail or malformed),
                  incomplete_tail=incomplete_tail, malformed_records=malformed,
                  product_accepted=False)
    if malformed:
        result["status"] = "unverified"
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("session", type=uuid_text)
    p.add_argument("--turn", type=uuid_text)
    p.add_argument("--include-text", action="store_true")
    p.add_argument("--root", action="append")
    args = p.parse_args()
    roots = args.root or [Path.home() / ".codex/sessions", Path.home() / ".codex/archived_sessions"]
    try:
        print(json.dumps(inspect(locate(args.session, roots), args.session, args.turn, args.include_text), indent=2))
        return 0
    except (ValueError, OSError) as e:
        print(json.dumps({"status": "unverified", "session_id": args.session, "reason": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
