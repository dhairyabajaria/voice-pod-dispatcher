"""Durable, non-secret OpenCode conversation identities for managed Muse launches.

Records precede launch. Missing historical session bindings refuse exact resume;
profile-wide headers are not evidence of what an old conversation actually used.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

ROUTE_FIELDS = ("profile", "provider", "model", "effort", "env_key")


def _path(root, kind, key):
    digest = hashlib.sha256(str(key).encode()).hexdigest()
    return Path(root) / "muse-conversations" / f"{kind}-{digest}.json"


def _write(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".identity-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.isfile(name):
            os.unlink(name)


def _checked(record, spec):
    if (not isinstance(record, dict) or record.get("version") != 1
            or not isinstance(record.get("header"), str)
            or not record["header"].startswith("voicepod-muse-")):
        raise ValueError("Invalid persisted Muse conversation identity; refusing launch")
    try:
        uuid.UUID(record["header"].removeprefix("voicepod-muse-"))
    except ValueError:
        raise ValueError("Invalid persisted Muse conversation header") from None
    if record.get("route") != {k: spec.get(k) for k in ROUTE_FIELDS}:
        raise ValueError("Muse conversation route changed; preserve its profile, account, model and effort")
    return record


def existing(root, conversation):
    """Recover pre-launch account affinity after a supervisor crash."""
    try:
        with _path(root, "conversation", conversation).open() as stream:
            record = json.load(stream)
    except FileNotFoundError:
        return None
    if not isinstance(record, dict) or not isinstance(record.get("route"), dict):
        raise ValueError("Invalid persisted Muse conversation route")
    return _checked(record, record["route"])


def prepare(root, conversation, spec, *, session_id=None, owner=None, worktree=None,
            replace_profile=False):
    """Persist before launch; only explicit new-profile placement rotates identity."""
    owner = str(conversation if owner is None else owner)
    worktree = os.path.realpath(worktree) if worktree is not None else None
    path = _path(root, "session" if session_id else "conversation", session_id or conversation)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with path.open() as stream:
                record = json.load(stream)
        except FileNotFoundError:
            if session_id:
                raise ValueError("Exact Muse session has no recorded provider identity; refusing resume. "
                                 "Recover its original managed identity record, or start a new conversation explicitly") from None
            record = None
        if record is not None:
            if not isinstance(record, dict) or not isinstance(record.get("route"), dict):
                raise ValueError("Invalid persisted Muse conversation route")
            _checked(record, record["route"])
            if record.get("owner") != owner or record.get("worktree") != worktree:
                raise ValueError("Muse conversation belongs to a different item or worktree; refusing launch")
            if not session_id and replace_profile and record["route"].get("profile") != spec.get("profile"):
                # The scheduler deliberately re-placed this item. Prior exact-session
                # bindings remain immutable; this is a NEW conversation on the new key.
                record = None
        if record is None:
            record = {"version": 1, "header": "voicepod-muse-" + str(uuid.uuid4()),
                      "route": {k: spec.get(k) for k in ROUTE_FIELDS},
                      "conversation": str(conversation), "owner": owner, "worktree": worktree}
            _write(path, record)
        _checked(record, spec)
        if session_id and record.get("session_id") != str(session_id):
            raise ValueError("Persisted Muse identity names a different session")
        return record


def bind(root, session_id, record):
    """Associate only an explicitly observed CLI session with the launch identity."""
    path = _path(root, "session", session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = dict(record, session_id=str(session_id))
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with path.open() as stream:
                old = json.load(stream)
            if old != value:
                raise ValueError("CLI session already belongs to a different Muse conversation")
        except FileNotFoundError:
            _write(path, value)


def flags(record):
    # Override the single leaf: other http_headers and env_http_headers survive.
    provider = record["route"]["provider"]
    if not isinstance(provider, str) or not provider or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in provider):
        raise ValueError("Muse provider name cannot be represented safely in a config override")
    return ["-c", f'model_providers.{provider}.http_headers.x-opencode-session=' + json.dumps(record["header"])]
