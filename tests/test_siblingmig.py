"""Migration 250 was taken by THREE lanes at once and no gate on any of them could see it.

2026-09-07: egress holds 250_connection_authority_clock.sql, erasure holds
250_erase_retention_task_subject_arrays.sql, journey holds
250_campaign_journey_sole_scheduler_slice1.sql, and trunk holds no 25x at all. Every lane correctly
took the next free number, every lane is individually contiguous, and every preflight passed
honestly — because the preflight compares a lane against TRUNK and never against another lane.

So an ADVISORY row, and advisory is the whole design. Taking next-free is correct behaviour; three
lanes doing it is not a defect in any of them. BOSS assigns numbers at the merge turn. This only
makes the queue of pending assignments visible at gate time instead of at the moment two of them
meet. If it ever fails a gate, it is wrong.

Hermetic: three real temp git repos and a temp queue.json.
"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixturelog as FL

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

spec = importlib.util.spec_from_file_location("mgs", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mgs"] = MG; spec.loader.exec_module(MG)

ROOT = tempfile.mkdtemp(prefix="siblingmig-")
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
def git(*a, cwd): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env=ENV)

def lane(name, migs):
    d = os.path.join(ROOT, name); os.makedirs(os.path.join(d, MG.MIG_DIR))
    git("init", "-q", "-b", "l/" + name, cwd=d)
    for m in migs:
        open(os.path.join(d, MG.MIG_DIR, m), "w").write("-- sql\n")
    git("add", "-A", cwd=d); git("commit", "-qm", name, cwd=d)
    return d

egress  = lane("egress",  ["249_x.sql", "250_connection_authority_clock.sql"])
erasure = lane("erasure", ["249_x.sql", "250_erase_retention_task_subject_arrays.sql"])
journey = lane("journey", ["249_x.sql", "250_campaign_journey_sole_scheduler_slice1.sql"])
polite  = lane("polite",  ["249_x.sql", "251_took_the_next_one.sql"])

QP = os.path.join(ROOT, "queue.json")
json.dump({"items": [{"id": "B.egress", "worktree": egress},
                     {"id": "B.erasure", "worktree": erasure},
                     {"id": "B.journey", "worktree": journey},
                     {"id": "B.polite", "worktree": polite},
                     {"id": "B.done", "worktree": erasure, "status": "merged"}]}, open(QP, "w"))

ours = MG._migrations(["git", "-C", egress, "ls-tree", "-r", "--name-only", "HEAD", MG.MIG_DIR], None)
sib = MG.sibling_migration_clashes("B.egress", ours, queue_path=QP)
names = sorted({o for _n, _m, o, _t in sib})

check("MUST-BITE  the OTHER two lanes holding 250 are named — this is the three-way collision no "
      "gate could see, because each lane only ever compares itself with trunk",
      names == ["B.erasure", "B.journey"], (names, sib))
check("MUST-BITE  and the row carries the FILENAMES, so a reader can tell which 250 is which "
      "without opening three worktrees",
      any("250_erase_retention_task_subject_arrays.sql" in t for _n, _m, _o, t in sib)
      and any("250_campaign_journey_sole_scheduler_slice1.sql" in t for _n, _m, _o, t in sib), sib)
check("MUST-BITE  CONTROL: 249, which every lane holds under the SAME filename, is NOT reported — "
      "lanes share their history by construction and a bare intersection reports everything",
      all(n != "249" for n, _m, _o, _t in sib), sorted({n for n, _m, _o, _t in sib}))
check("MUST-BITE  CONTROL: the lane that took 251 is not reported against anyone — correct "
      "behaviour must never appear in an advisory about collisions",
      "B.polite" not in names and MG.sibling_migration_clashes("B.polite", MG._migrations(
          ["git", "-C", polite, "ls-tree", "-r", "--name-only", "HEAD", MG.MIG_DIR], None),
          queue_path=QP) == [], names)
check("MUST-BITE  CONTROL: an item already MERGED is not a sibling — its number is trunk's problem "
      "now, and the preflight row above already covers trunk",
      "B.done" not in names, names)
# NOT a must-bite, and labelled honestly: mutating the `id == item_id` skip away scored MISSED, and
# it should have. A lane compared with itself holds every number under the SAME filename, so the
# same-filename filter already excludes it. The skip is cheaper, not load-bearing, and a test that
# pretends otherwise would be a check that cannot fail.
check("  the lane does not report itself (belt-and-braces: the same-filename filter already ensures "
      "this, which is why mutating the skip away changes nothing)",
      "B.egress" not in names, names)

# The ROW an executor reads, not just the tuples behind it. BOSS's condition for wanting this at all:
# the failure mode is a helpful executor who sees `250: this lane vs B.erasure`, renumbers to 251
# unprompted, and leaves two lanes disagreeing about which is stale — worse than the blindness.
row = MG.sibling_clash_row(sib)
check("MUST-BITE  the row tells the reader NOT to act on it, in words they cannot skim past",
      "DO NOT RENUMBER" in row and "ASK BOSS" in row, row[-170:])
check("MUST-BITE  and it says WHY, because an instruction without a reason is the first thing "
      "dropped under time pressure",
      "disagreeing about which one is stale" in row, row[-170:])
check("  and it still says the preflight above cannot see any of this, so the two rows are not read "
      "as one measurement",
      "compares this lane against TRUNK only" in row, row[-90:])
check("MUST-BITE  the row names every clashing lane, not just the first",
      "B.erasure" in row and "B.journey" in row, row[:200])

# an advisory must never be the thing that breaks a gate
bad = os.path.join(ROOT, "not-a-repo"); os.makedirs(bad)
json.dump({"items": [{"id": "B.gone", "worktree": os.path.join(ROOT, "vanished")},
                     {"id": "B.notrepo", "worktree": bad}]}, open(QP + ".2", "w"))
check("MUST-BITE  a missing worktree and a directory that is not a git repo are skipped silently — "
      "a sibling mid-rebase must never redden a gate that is about a different lane",
      MG.sibling_migration_clashes("B.egress", ours, queue_path=QP + ".2") == [], "")
check("  and an unreadable queue yields no advisory rather than an exception",
      MG.sibling_migration_clashes("B.egress", ours, queue_path=os.path.join(ROOT, "nope.json")) == [], "")

# WHY the rc check is load-bearing, since the end-to-end mutation of it scored MISSED: sh() MERGES
# STDERR INTO ITS OUTPUT, and git's error messages name paths. A line ending in a migration filename
# parses as a migration, so without the rc guard a failed sibling read would contribute PHANTOM
# NUMBERS to a collision advisory. The temp-dir cases above cannot show this because their git errors
# happen not to end in a filename; this one measures the parser directly.
# and the same guard on the HARD-FAIL path: _migrations() itself must not parse a failed listing.
# Measured with a real git error, not a synthetic string.
import subprocess as _sp
_bad = os.path.join(ROOT, "not-a-repo-2"); os.makedirs(_bad, exist_ok=True)
check("MUST-BITE  _migrations() returns {} when the git command FAILS — its numbers feed the "
      "MIGRATION COLLISION and DUPLICATE rows, which are hard FAILs, and a phantom there blocks a "
      "merge over a migration nobody holds",
      MG._migrations(["git", "-C", egress, "cat-file", "-p",
                      "platform/migrations/250_ghost.sql"], None) == {},
      "")
check("MUST-BITE  CONTROL: that exact git error DOES parse as migration 250 when handed straight to "
      "the parser — so the check above is the rc guard working, not the error being harmless",
      MG._migrations_from_text(_sp.run(["git", "-C", egress, "cat-file", "-p",
                                        "platform/migrations/250_ghost.sql"],
                                       capture_output=True, text=True).stderr) == {"250": {"250_ghost.sql"}},
      "")

ERRISH = "error: unable to read platform/migrations/250_ghost.sql"
check("MUST-BITE  CONTROL: an error message naming a migration path DOES parse as a migration — "
      "which is why a non-zero rc must be skipped rather than parsed",
      "250" in MG._migrations_from_text(ERRISH), MG._migrations_from_text(ERRISH))

shutil.rmtree(ROOT, ignore_errors=True)
print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
