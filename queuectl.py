#!/usr/bin/env python3
"""Locked edits to test-logs/driver/queue.json — the only safe way to edit it while the daemon runs.

  queuectl.py show [<id>]                       print items (or one) as id/status/executor/dispatched_to
  queuectl.py set <id> key=value [key=value…]   e.g. set B.010.x status=queued executor=CODEX dispatched_to=
  queuectl.py requeue <id> [executor]           status=queued, dispatched_to cleared, optional pin
  queuectl.py park <id> "<reason>"              status=parked with parked_reason

The daemon holds queue.json.lock for the whole of every tick; this tool takes the same flock for
every edit, so neither side can clobber the other's write (2026-09-05 13:13:25: a `reported`
update was lost to an unlocked editor and an item was dispatched twice).
"""
import fcntl
import json
import os
import sys
from datetime import datetime

CN = os.environ.get("CN", "/Users/dhairyabajaria/Claude Code/Calling New")
QUEUE = os.path.join(CN, "test-logs", "driver", "queue.json")
LOCK = QUEUE + ".lock"


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    with open(LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        q = json.load(open(QUEUE))
        items = {i["id"]: i for i in q["items"]}
        if cmd == "show":
            for i in q["items"]:
                if len(args) > 1 and i["id"] != args[1]:
                    continue
                print(f"{i['id']:<44} {i.get('status', ''):<10} exec={i.get('executor', ''):<7} to={i.get('dispatched_to', '')} "
                      f"{'blocked_on=' + str(i['blocked_on']) if i.get('blocked_on') else ''}")
            return 0
        if len(args) < 2 or args[1] not in items:
            print("no such item")
            return 2
        it = items[args[1]]
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if cmd == "set":
            for kv in args[2:]:
                k, _, v = kv.partition("=")
                # a list/object/bool/null value is stored as JSON, not as its text (BOSS, 2026-09-05:
                # proof_files=["a","b"] landed as a string and would have been splatted per character)
                # `v and` is load-bearing: "" [:1] is "", and "" in "[{" is True in Python, so an empty
                # value (the documented way to unpin: dispatched_to=) took the JSON path and was refused.
                if (v and v[0] in "[{") or v in ("true", "false", "null"):
                    try:
                        v = json.loads(v)
                    except json.JSONDecodeError:
                        print(f"refusing {k}: value looks like JSON but does not parse")
                        return 2
                it[k] = v
            it["edited_at"] = stamp
        elif cmd == "requeue":
            it.update({"status": "queued", "dispatched_to": "", "edited_at": stamp})
            if len(args) > 2:
                it["executor"] = args[2]
            it.pop("blocked_on", None)
        elif cmd == "park":
            it.update({"status": "parked", "parked_reason": args[2] if len(args) > 2 else "", "edited_at": stamp})
        else:
            print("unknown command")
            return 2
        tmp = QUEUE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(q, f, indent=1)
        os.replace(tmp, QUEUE)
        print(f"{it['id']}: status={it.get('status')} executor={it.get('executor')} dispatched_to={it.get('dispatched_to', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
