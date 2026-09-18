#!/usr/bin/env python3
"""ci/verify_snapshot.py -- the scheduler snapshot the CI suite imports.

`--record`: (re)write SNAPSHOT.json from the files under ci/control/ (run by
ci/snapshot.sh after copying them from the live checkout).

`<out.json>`: CI guard -- every snapshot file must hash to what SNAPSHOT.json
recorded (a stale or hand-edited copy fails loudly; the suite must never pass
on the wrong scheduler), then a pin file for the suite (VP_TEST_AUTHORITY) is
derived from the snapshot's CATALOG-AUTHORITY.json with the scheduler /
review_gate shas replaced by the snapshot's own, so the driver's authority
check is exercised against exactly the code under test.  Whether the LIVE
pin matched the scheduler when the snapshot was taken is printed, not
enforced: that is a run-time property of the box, not of the code.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CTL = HERE / "control"
FILES = ["orchestration_control.py", "review_gate.py", "CATALOG-AUTHORITY.json", "v13-pack/roster-v13.json"]


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "missing"


def record():
    shas = {f: sha(CTL / f) for f in FILES}
    pins = json.loads((CTL / "CATALOG-AUTHORITY.json").read_text())
    ts = subprocess.check_output(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], text=True).strip()
    doc = {"taken_at": ts, "files": shas,
           "pin_matches_scheduler": pins.get("scheduler_sha256") == shas["orchestration_control.py"],
           "pin_matches_review_gate": pins.get("review_validator_sha256") == shas["review_gate.py"]}
    (CTL / "SNAPSHOT.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(json.dumps(doc, indent=2))


def verify(out):
    doc = json.loads((CTL / "SNAPSHOT.json").read_text())
    bad = ["%s: %s != recorded %s" % (rel, sha(CTL / rel)[:12], want[:12])
           for rel, want in doc["files"].items() if sha(CTL / rel) != want]
    if bad:
        print("SNAPSHOT MISMATCH:\n  " + "\n  ".join(bad))
        return 2
    pins = json.loads((CTL / "CATALOG-AUTHORITY.json").read_text())
    pins["scheduler_sha256"] = doc["files"]["orchestration_control.py"]
    pins["review_validator_sha256"] = doc["files"]["review_gate.py"]
    Path(out).write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n")
    print("snapshot ok (taken %s; live pin matched the scheduler then: %s); suite pin -> %s"
          % (doc["taken_at"], doc["pin_matches_scheduler"], out))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--record"]:
        record()
    elif len(sys.argv) == 2:
        sys.exit(verify(sys.argv[1]))
    else:
        print(__doc__)
        sys.exit(1)
