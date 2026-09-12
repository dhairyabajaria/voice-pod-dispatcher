#!/usr/bin/env python3
"""tests/fake_vpctl.py -- stand-in for the sibling agent's vpctl.py.

The driver only ever reaches the store through `python3 vpctl.py ... --json`,
so this stub is a complete substitute for testing: it records every call as a
JSON line in $VP_FAKE_VPCTL_LOG and answers with the shapes the driver parses.

Env:
  VP_FAKE_VPCTL_LOG  path to the call log (jsonl, one argv list per line)
  VP_FAKE_ITEMS      path to a JSON file holding the `report items` list
  VP_FAKE_RUNNING    path to a JSON file holding `report liveness`
                     attempts_running (default: empty)
  VP_FAKE_PACKET     path to a JSON file holding the `packet show` row
  VP_FAKE_ATTEMPT    attempt_id handed back by `turn start` (default a1)
  VP_FAKE_VPCTL_RC   force this exit code for every call (failure injection)
  VP_FAKE_FAIL_VERB  fail just this verb ("turn start"), rc VP_FAKE_FAIL_RC (2)
  VP_FAKE_NO_ESCALATE  if set, `escalate` exits 2 (verb not implemented)
"""

import json
import os
import sys


def record(args):
    log = os.environ.get("VP_FAKE_VPCTL_LOG")
    if not log:
        return
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(args) + "\n")


def calls(log_path):
    """Helper for tests: -> list of argv lists."""
    out = []
    if not os.path.exists(log_path):
        return out
    with open(log_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    record(args)

    # fail one specific verb, e.g. VP_FAKE_FAIL_VERB="turn start" (rc from
    # VP_FAKE_FAIL_RC, default 2 = usage) -- the live-run defect.
    fail_verb = os.environ.get("VP_FAKE_FAIL_VERB")
    if fail_verb:
        words = fail_verb.split()
        if [a for a in args if not a.startswith("--")][:len(words)] == words:
            sys.stderr.write("unrecognized arguments for %s\n" % fail_verb)
            return int(os.environ.get("VP_FAKE_FAIL_RC", "2"))

    forced = os.environ.get("VP_FAKE_VPCTL_RC")
    if forced:
        sys.stdout.write(json.dumps({"ok": False, "forced": True}) + "\n")
        return int(forced)

    head = [a for a in args if not a.startswith("--")][:2]

    if head[:2] == ["report", "items"]:
        path = os.environ.get("VP_FAKE_ITEMS")
        items = []
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                items = json.load(fh)
        sys.stdout.write(json.dumps({"items": items}) + "\n")
        return 0

    if head[:2] == ["packet", "show"]:
        path = os.environ.get("VP_FAKE_PACKET")
        row = {}
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                row = json.load(fh)
        sys.stdout.write(json.dumps(row) + "\n")
        return 0 if row else 3

    if head[:2] == ["report", "liveness"]:
        path = os.environ.get("VP_FAKE_RUNNING")
        rows = []
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
        sys.stdout.write(json.dumps({"attempts_running": rows}) + "\n")
        return 0

    if head[:2] == ["turn", "start"]:
        sys.stdout.write(json.dumps(
            {"attempt_id": os.environ.get("VP_FAKE_ATTEMPT", "a1")}) + "\n")
        return 0

    if head[:1] == ["escalate"] and os.environ.get("VP_FAKE_NO_ESCALATE"):
        sys.stderr.write("unknown command 'escalate'\n")
        return 2

    sys.stdout.write(json.dumps({"ok": True, "cmd": head}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
