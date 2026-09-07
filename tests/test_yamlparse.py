"""mergegate.run_yaml_parse_row — every YAML file in a candidate must actually parse.

Fixture fixtures/ci_yml_unparseable.yml is the REAL .github/workflows/ci.yml from
B.010.ci-collection-floor r5 @ 2d1630bf: a rebase left a commit subject inside the file, so it does
not parse at line 184 — and the gate ran the collection detector against it, got `[]`, and no row
noticed. Hermetic: builds its own git repo in a temp dir; advisory, so every assertion is on warn()."""
import importlib.util, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("mg5", os.path.join(HERE, os.pardir, "mergegate.py"))
MG = importlib.util.module_from_spec(spec); sys.modules["mg5"] = MG; spec.loader.exec_module(MG)
BAD = open(os.path.join(HERE, "fixtures", "ci_yml_unparseable.yml")).read()

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

def repo(files):
    root = tempfile.mkdtemp(prefix="yamlrow-")
    subprocess.run(["git", "init", "-q", root], check=True, capture_output=True)
    for k in ("user.email=t@t", "user.name=t"):
        subprocess.run(["git", "-C", root, "config", *k.split("=", 1)], check=True, capture_output=True)
    for rel, txt in files.items():
        p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(txt)
    subprocess.run(["git", "-C", root, "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", root, "commit", "-qm", "x"], check=True, capture_output=True)
    sha = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return root, sha

def run(files, changed):
    root, sha = repo(files)
    rows = []
    ok = MG.run_yaml_parse_row(root, sha, changed, rows.append)
    shutil.rmtree(root, ignore_errors=True)
    return ok, (rows[0] if rows else "")

# the fixture must still be unparseable, or this test proves nothing
import yaml
try:
    yaml.safe_load(BAD); parses = True
except yaml.YAMLError:
    parses = False
check("fixture: the real ci.yml still does NOT parse", parses is False)

# 1. the real case
ok, row = run({".github/workflows/ci.yml": BAD}, [".github/workflows/ci.yml"])
check("an unparseable workflow FAILS the row", ok is False and "YAML parse FAILED" in row, row[:150])
check("  naming the file and the line", ".github/workflows/ci.yml:184" in row, row[:150])
check("  and saying what it means for anything that read it",
      "enforces NOTHING" in row and "ADVISORY" in row)

# 2. MUST-PASS: valid YAML gets a row that says how many parsed, at the sha
ok, row = run({".github/workflows/ci.yml": "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"},
              [".github/workflows/ci.yml"])
check("MUST-PASS  a valid workflow parses clean", ok is True and "parse clean" in row, row)
check("  and the row still says the check RAN, with a count", "1 candidate YAML file(s)" in row, row)

# 3. a candidate with no YAML gets no row at all — no noise on every gate
ok, row = run({"platform/core/x.py": "x = 1\n"}, ["platform/core/x.py"])
check("no YAML in the diff -> no row", ok is None and row == "")

# 4. multi-document YAML is valid and must not be reported as broken
ok, row = run({"deploy/k8s.yaml": "a: 1\n---\nb: 2\n"}, ["deploy/k8s.yaml"])
check("a multi-document YAML file parses", ok is True, row)

# 5. a file deleted at this sha is not a parse failure
root, sha = repo({"a.yml": "a: 1\n"})
rows = []
ok = MG.run_yaml_parse_row(root, sha, ["a.yml", "gone.yml"], rows.append)
shutil.rmtree(root, ignore_errors=True)
check("a YAML file not present at the sha is counted, not failed",
      ok is True and "not present at this sha" in rows[0], rows[0])

# 6. it reads the SHA, not the working tree
root, sha = repo({"a.yml": "a: 1\n"})
open(os.path.join(root, "a.yml"), "w").write(": : broken now\n")   # dirty the tree after the commit
rows = []
ok = MG.run_yaml_parse_row(root, sha, ["a.yml"], rows.append)
shutil.rmtree(root, ignore_errors=True)
check("a dirtied working tree does not change the verdict — it parses the sha", ok is True, rows[0])

print("\nYAML PARSE " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
