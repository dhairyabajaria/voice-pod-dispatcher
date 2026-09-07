"""While Codex is walled, a gate should read the wall the PREVIOUS gate recorded, not re-buy it.

BOSS 2026-09-06 23:1x: the wall runs to Sep 9th, so every gate until then pays a reviewer call (two,
since the one-retry rule) to learn something already written down in the last gate's .codex.txt.

The dangerous direction is the other one, so both asymmetries err toward MAKING the call:
an unparseable or missing reset date does not skip, and a date that has passed does not skip. A
needless call costs seconds; a wrongly skipped review produces a gate that reports a reviewer outage
while the reviewer was in fact available — and unlike the wall itself, that claim leaves no evidence
anyone can check afterwards.

Fixture is the real wall text from gate 83676. Hermetic: temp GATES dir, no reviewer call."""
import importlib.util, os, shutil, sys, tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("mgw2", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mgw2"] = MG; spec.loader.exec_module(MG)
WALLED = open(os.path.join(HERE, "fixtures", "codex_walled.txt")).read()

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def gates(files):
    d = tempfile.mkdtemp(prefix="wallskip-")
    for i, (name, txt) in enumerate(files):
        p = os.path.join(d, name)
        open(p, "w").write(txt)
        os.utime(p, (1e9 - i, 1e9 - i))      # first entry is newest
    MG.GATES = d
    return d

NOW = datetime(2026, 9, 7, 12, 0)            # before the Sep 9 reset in the fixture
LATER = datetime(2026, 9, 10, 12, 0)         # after it

# 1. the real fixture: a wall recorded by an earlier gate, still in force
d = gates([("B.010.previous.codex.txt", WALLED)])
w = MG.recorded_wall(NOW)
check("MUST-BITE  a still-current recorded wall is found", w is not None, w)
check("  and it names the gate that recorded it", w and w[1] == "B.010.previous", w and w[1])
check("  with the reviewer's own reset time", w and w[0] == datetime(2026, 9, 9, 22, 40), w and str(w[0]))

# 2. THE ASYMMETRIES — each must make the call rather than skip
check("MUST-BITE  a wall whose date has PASSED does not skip", MG.recorded_wall(LATER) is None,
      MG.recorded_wall(LATER))
gates([("B.010.p.codex.txt", "You've hit your usage limit. Visit https://x for credits.")])
check("MUST-BITE  a wall with NO parseable date does not skip", MG.recorded_wall(NOW) is None)
gates([("B.010.p.codex.txt", "You've hit your usage limit … try again at Someday Next Week.")])
check("MUST-BITE  an unreadable date format does not skip", MG.recorded_wall(NOW) is None)
gates([])
check("no recorded reviews at all does not skip", MG.recorded_wall(NOW) is None)
MG.GATES = os.path.join(tempfile.mkdtemp(prefix="wallskip-gone-"), "nope")
check("a missing gates directory does not skip, and does not raise", MG.recorded_wall(NOW) is None)

# 3. RECENCY: the NEWEST record decides. A wall from an old file must not outvote a fresh
#    successful review, or the gate would stay 'walled' for days after the wall lifted.
gates([("B.010.fresh.codex.txt", '# Codex Adversarial Review\n\n"verdict": "approve"\n'),
       ("B.010.old.codex.txt", WALLED)])
check("MUST-BITE  a fresh SUCCESSFUL review outvotes an older wall record",
      MG.recorded_wall(NOW) is None, MG.recorded_wall(NOW))
gates([("B.010.old.codex.txt", WALLED),
       ("B.010.older.codex.txt", '"verdict": "approve"\n')])
check("CONTROL  and the reverse order still finds the wall", MG.recorded_wall(NOW) is not None)

# 4. the date parser, on the vendor's actual shape and the ordinals it uses
check("the reviewer's date shape parses", MG.parse_wall_date("Sep 9th, 2026 10:40 PM") ==
      datetime(2026, 9, 9, 22, 40), MG.parse_wall_date("Sep 9th, 2026 10:40 PM"))
check("  and a full month name too", MG.parse_wall_date("September 1st, 2026 9:05 AM") ==
      datetime(2026, 9, 1, 9, 5))
check("  garbage returns None rather than a guess", MG.parse_wall_date("soonish") is None)

shutil.rmtree(d, ignore_errors=True)
print("\nWALL SKIP " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
