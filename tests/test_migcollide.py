"""A migration number is only unique in the tree it lands in, and a PASS about that EXPIRES.

BOSS ruled on 2026-09-07 that migration numbers get assigned at the merge turn, believing no gate on
either side could see the collision: trunk holds 249_auxiliary_not_sent_authority.sql and the egress
lane holds 249_connection_authority_clock.sql, different filenames, so git merges them clean and
db.py refuses the duplicate only at runtime.

MEASURED, and the belief is wrong in a useful direction: `run_merge_preflight` already compares the
lane's migration NUMBERS against trunk's and hard-FAILS on same-number-different-filename. Run
against the real trees today it reports `249: lane ['249_connection_authority_clock.sql'] vs trunk
['249_auxiliary_not_sent_authority.sql']` on BOTH lanes. The ruling still stands — assignment at the
merge turn is what prevents the collision — but the gate is not blind to it.

What IS true is subtler and is what this file pins: the check measures the UNION at one moment, so
its verdict expires when EITHER side moves, and an expired verdict reads exactly like a live one.
Those lanes' earlier gates passed honestly because trunk had not taken 249 yet. Then, within the
hour, the egress lane renumbered 249 -> 250 and the FAIL above went stale too (BOSS reproduced it:
lane ec91da2a COLLIDE=['249'], lane 81fbf545 COLLIDE=none) — that one expired because the LANE moved,
not trunk. A stale FAIL is as unreadable as a stale PASS, so the row must name BOTH shas.

Hermetic: two real temp git repos, no board."""
import importlib.util, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location("mgm", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mgm"] = MG; spec.loader.exec_module(MG)

ROOT = tempfile.mkdtemp(prefix="migcollide-")
def git(*a, cwd): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                                 env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                      "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
def mig(root, name):
    d = os.path.join(root, MG.MIG_DIR); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, name), "w").write("-- sql\n")

trunk = os.path.join(ROOT, "trunk"); os.makedirs(trunk)
git("init", "-q", "-b", "plan010/rebuild", cwd=trunk)
mig(trunk, "248_audit_residuals.sql"); mig(trunk, "249_auxiliary_not_sent_authority.sql")
git("add", "-A", cwd=trunk); git("commit", "-qm", "trunk", cwd=trunk)

lane = os.path.join(ROOT, "lane"); os.makedirs(lane)
git("init", "-q", "-b", "lane/x", cwd=lane)
mig(lane, "248_audit_residuals.sql"); mig(lane, "249_connection_authority_clock.sql")
git("add", "-A", cwd=lane); git("commit", "-qm", "lane", cwd=lane)

t = MG._migrations(["git", "-C", trunk, "ls-tree", "-r", "--name-only", "plan010/rebuild", MG.MIG_DIR], None)
l = MG._migrations(["git", "-C", lane, "ls-tree", "-r", "--name-only", "HEAD", MG.MIG_DIR], None)
collide = MG.migration_collisions(l, t)   # the module's own rule, never a copy of it
check("MUST-BITE  the REAL 249 shape — same number, different filename — is detected, so the gate is "
      "NOT blind to it as we believed",
      collide == ["249"], (collide, sorted(set(l) & set(t))))
check("MUST-BITE  CONTROL: the number both trees hold with the SAME filename is NOT a collision — "
      "lane and trunk share 001..N by construction, so a naive set intersection reports every "
      "number and means nothing",
      "248" not in collide and "248" in (set(l) & set(t)), sorted(set(l) & set(t)))

# a lane that took the NEXT free number is clean — the guard must not fire on correct behaviour
lane2 = os.path.join(ROOT, "lane2"); os.makedirs(lane2)
git("init", "-q", "-b", "lane/y", cwd=lane2)
mig(lane2, "248_audit_residuals.sql"); mig(lane2, "249_auxiliary_not_sent_authority.sql")
mig(lane2, "250_connection_authority_clock.sql")
git("add", "-A", cwd=lane2); git("commit", "-qm", "lane2", cwd=lane2)
l2 = MG._migrations(["git", "-C", lane2, "ls-tree", "-r", "--name-only", "HEAD", MG.MIG_DIR], None)
check("MUST-BITE  CONTROL: a lane that took 250 instead is CLEAN — BOSS's ruling in its resolved "
      "form must not be reported as a defect",
      not MG.migration_collisions(l2, t), sorted(set(l2) & set(t)))

# The provenance claim is checked on the ROW THE GATE WRITES, not on the source that writes it: a
# source-read passes while the value is empty, and "measured against trunk " with nothing after it
# is exactly the shape that reads as provenance and carries none.
MG.CN, MG.TRUNK = ROOT, trunk
rows = []
MG.run_merge_preflight(lane, subprocess.run(["git", "rev-parse", "HEAD"], cwd=lane,
                                            capture_output=True, text=True).stdout.strip(),
                       "B.010.test", lambda n, ok, d: rows.append((n, ok, d)))
row = rows[-1][2] if rows else ""
check("MUST-BITE  the collision is reported in the gate ROW, end to end — not merely computable",
      rows and rows[-1][1] is False and "MIGRATION COLLISION" in row and "249" in row, rows[-1:])
# BOTH shas, not trunk's alone. The row is about a PAIR, and the first real expiry we hit came from
# the lane side. Each sha is checked against the sha it is supposed to name, not merely for shape:
# a regex for ten hex digits passes on the same value printed twice.
lane_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=lane,
                           capture_output=True, text=True).stdout.strip()
trunk_head = subprocess.run(["git", "rev-parse", "plan010/rebuild"], cwd=trunk,
                            capture_output=True, text=True).stdout.strip()
check("MUST-BITE  the row names the LANE SHA it was measured on, and it is the real lane head — the "
      "egress FAIL expired because the LANE moved, so a row naming only trunk cannot be dated",
      re.search(r"measured on lane " + lane_head[:10] + r"\b", row) is not None, row[-160:])
check("MUST-BITE  and the row names the TRUNK SHA it was measured against, and it is the real trunk "
      "head — an expired verdict about the union reads exactly like a live one",
      re.search(r"against trunk " + trunk_head[:10] + r"\b", row) is not None, row[-160:])
check("  CONTROL: the two shas are different values, so a row that printed one of them twice cannot "
      "satisfy both checks above by accident",
      lane_head[:10] != trunk_head[:10], (lane_head[:10], trunk_head[:10]))
check("  it says so in those words, so the reader knows the verdict has a shelf life and why",
      "expires when either moves" in row, row[-90:])

shutil.rmtree(ROOT, ignore_errors=True)
print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
