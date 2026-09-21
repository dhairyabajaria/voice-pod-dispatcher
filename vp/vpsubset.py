#!/usr/bin/env python
"""D176: derive each sealed proof record's `subset_verdict` into a sidecar.

WHY A SIDECAR AND NOT A FIELD.  The proofs dir is sealed -- the hourly manifest
pins ~1013 `proofs/proof-*` entries by path and sha256, and verify_all exits 1 on
any edit.  Stamping the field into the records would break every seal in the run.

WHY IT MUST BE DERIVED, NOT WRITTEN BY HAND (Architect 2, 2026-09-21).  Sealing
proves INTEGRITY -- unchanged since sealed -- not VALIDITY -- right when sealed.
A hand-authored sidecar, sealed, would be strictly WORSE than an unsealed one:
it would carry the sealed chain's authority while still being somebody's
unverifiable judgement call.  So this file is a pure function of the sealed
records, it records what it read, and a reader can re-derive it instead of
trusting it.

THE SIDECAR IS A DERIVED ARTIFACT OF THE SEALED RECORDS, NEVER A SECOND SOURCE
OF TRUTH.  If the two ever disagree, the RECORDS WIN and the sidecar is
regenerated.  Nothing may edit the sidecar to change a verdict; change the rule
here and re-derive, so the change is reviewable as code.

DETERMINISM.  Same sealed inputs -> byte-identical output.  There is deliberately
no timestamp in the document: a `generated_at` would make every run differ and
destroy the one property that lets a reader check the mapping.  `--verify`
re-derives and compares bytes.

THE SECOND INPUT, STATED OUT LOUD.  The verdict for a scoped record depends on
the pytest command line that ACTUALLY ran, which lives in the rendered
`.github/workflows/vp-proof.yml` at the overlay commit -- in git, not in the
sealed record.  That is a real second input and it is not hidden: every commit
read is recorded with the sha256 of the bytes read, so a reader can check that
too.  It is reproducible because a git commit is content-addressed, but it is not
"the sealed records alone", and pretending otherwise would be the same
false-assurance this whole field exists to remove.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = 1
CITABLE = ("full", "covered")


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(p):
    return sha256_bytes(Path(p).read_bytes())


class Deriver:
    """Pure function: (sealed records, trunk) -> sidecar document."""

    TWIN_STEP_RE = re.compile(
        r"Twin:.*?\n\s*working-directory: platform\n\s*run: (.*?)\n      env:", re.S)

    def __init__(self, run_root, trunk):
        self.run_root = Path(run_root)
        self.trunk = str(trunk)
        self.workflows = {}          # commit -> {"sha256":..., "cmd":...}

    # -- inputs -------------------------------------------------------------

    def records(self):
        """[(relpath, record, sha256)] sorted by path -- the sealed inputs."""
        out = []
        for f in sorted((self.run_root / "proofs").glob("proof-*.json")):
            raw = f.read_bytes()
            try:
                rec = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue                       # unreadable: not an input, reported by count
            out.append((str(f.relative_to(self.run_root)), rec, sha256_bytes(raw)))
        return out

    def twin_cmd(self, commit):
        """the pytest command the rendered workflow ran at `commit`, or None.
        Every read is memoised AND recorded with the bytes' sha256."""
        if not commit:
            return None
        if commit in self.workflows:
            return self.workflows[commit]["cmd"]
        r = subprocess.run(["git", "-C", self.trunk, "show",
                            "%s:.github/workflows/vp-proof.yml" % commit],
                           capture_output=True, text=True)
        cmd = None
        digest = None
        if r.returncode == 0:
            digest = sha256_bytes(r.stdout.encode("utf-8"))
            m = self.TWIN_STEP_RE.search(r.stdout)
            if m:
                cmd = re.sub(r"\s+", " ", m.group(1))
        self.workflows[commit] = {"sha256": digest, "cmd": cmd}
        return cmd

    # -- the rule -----------------------------------------------------------

    @staticmethod
    def files_of(only):
        s = str(only or "")
        body = s.split(":", 2)[2] if s.count(":") >= 2 else ""
        return [p.split("/")[-1] for p in body.split(",") if p.strip()]

    def overlay_commits(self, records):
        """id(record) -> overlay commit, for EVERY record on a twin job whose
        overlay can be identified -- the renderer included.

        The renderer is NOT exempt.  An earlier rule trusted "it rendered,
        therefore it ran its own ask", which is an assumption rather than a
        check, and it silently made 34 records citable.
        """
        by_job = {}
        for _p, rec, _h in records:
            for j in (rec.get("jobs") or []):
                if j.get("job_number") and str(j.get("name") or "").startswith("vp/platform-twin"):
                    by_job.setdefault(j["job_number"], []).append(rec)
        out = {}
        for _job, rs in by_job.items():
            owners = [r for r in rs
                      if r.get("measured_commit") and r.get("measured_commit") != r.get("sha")]
            if len(owners) != 1:
                continue                       # 0 or several: cannot say what ran
            for r in rs:
                out[id(r)] = owners[0]["measured_commit"]
        return out

    def classify(self, rec, overlay):
        """-> (verdict, why).  Never guesses a record alive: anything this cannot
        positively establish is `unresolvable`, which REFUSES."""
        if "only" not in rec:
            return "unresolvable", "no `only` key: predates D170, says nothing about what ran"
        only = rec.get("only")
        if only is None:
            return ("unresolvable",
                    "`only` is null, but nothing distinguishes a full run from a borrowed "
                    "pipeline pre-D173 (no repolled_from, tests_collected null, reason empty)")
        s = str(only)
        if s.startswith("preflight:"):
            return "preflight", "preflight ask"
        if overlay is None:
            return ("unresolvable",
                    "scoped ask, but no twin job with an identifiable overlay -- `only` is "
                    "only the ASK, and nothing here says what ran")
        cmd = self.twin_cmd(overlay)
        if cmd is None:
            return "unresolvable", "the rendered twin step at %s is unreadable" % str(overlay)[:12]
        want = self.files_of(s)
        if not want:
            return "unresolvable", "its own ask names no files"
        absent = [w for w in want if w not in cmd]
        if absent:
            return ("not_covered",
                    "%d of %d of its own file(s) never ran in the command that actually "
                    "executed at %s: %s" % (len(absent), len(want), str(overlay)[:12],
                                            ", ".join(absent)))
        return "covered", "every one of its own files is in the command executed at %s" % str(overlay)[:12]

    # -- output -------------------------------------------------------------

    def build(self):
        recs = self.records()
        overlay = self.overlay_commits(recs)
        entries = {}
        for path, rec, digest in recs:
            if "subset_verdict" in rec:
                verdict, why = rec["subset_verdict"], "written by laneproof at proof time"
            else:
                verdict, why = self.classify(rec, overlay.get(id(rec)))
            entries[path] = {"subset_verdict": verdict, "basis": why,
                             "record_sha256": digest, "proof_id": rec.get("proof_id")}
        doc = {
            "schema": SCHEMA,
            "derived_from": "the sealed proof records under proofs/, and the rendered "
                            "vp-proof.yml at each overlay commit",
            "authority": "DERIVED ARTIFACT, NOT A SOURCE OF TRUTH. If this disagrees with "
                         "the sealed records, the RECORDS WIN and this file is regenerated. "
                         "Never hand-edit a verdict here; change the rule in vpsubset.py and "
                         "re-derive, so the change is reviewable as code.",
            "generator": {"name": "vp/vpsubset.py", "sha256": sha256_file(__file__)},
            "workflow_inputs": {c: v["sha256"] for c, v in sorted(self.workflows.items())},
            "entries": entries,
        }
        return doc

    @staticmethod
    def dumps(doc):
        return json.dumps(doc, indent=2, sort_keys=True) + "\n"


# -- controls ---------------------------------------------------------------

def control_known_bad(deriver, recs, overlay, entries):
    """PRECONDITION: every record whose own files are absent from the executed
    command must NOT come out `covered`.  Aborts when the count is ZERO -- a
    control that cannot fail proves nothing."""
    n, leaked = 0, []
    for path, rec, _h in recs:
        oc = overlay.get(id(rec))
        if oc is None:
            continue
        cmd = deriver.twin_cmd(oc)
        want = deriver.files_of(rec.get("only"))
        if cmd is not None and want and all(w not in cmd for w in want):
            n += 1
            if entries[path]["subset_verdict"] == "covered":
                leaked.append(rec.get("proof_id"))
    return n, leaked


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--trunk", required=True)
    ap.add_argument("--out", help="write the sidecar here (default: stdout summary only)")
    ap.add_argument("--verify", action="store_true",
                    help="re-derive and compare bytes against --out; exit 1 on any drift")
    a = ap.parse_args(argv)

    d = Deriver(a.run_root, a.trunk)
    recs = d.records()
    overlay = d.overlay_commits(recs)
    doc = d.build()
    entries = doc["entries"]

    tally = {}
    for e in entries.values():
        tally[e["subset_verdict"]] = tally.get(e["subset_verdict"], 0) + 1
    print("records: %d   workflow commits read: %d" % (len(recs), len(d.workflows)))
    for k in sorted(tally, key=lambda k: -tally[k]):
        print("  %4d  %s" % (tally[k], k))
    citable = sum(v for k, v in tally.items() if k in CITABLE)
    print("  citable: %d of %d" % (citable, len(entries)))

    n, leaked = control_known_bad(d, recs, overlay, entries)
    print("\ncontrol: known-bad records (own files absent from the executed command): %d" % n)
    if n == 0:
        return _die("ABORT: the precondition control found NO known-bad records, so it "
                    "cannot fail and proves nothing. Fix the control, not the corpus.")
    if leaked:
        for pid in leaked:
            print("   LEAKED as covered: %s" % pid)
        return _die("ABORT: %d known-bad record(s) came out `covered`. The rule is wrong. "
                    "Nothing was written." % len(leaked))
    print("  none came out `covered` -- OK")

    # determinism: the whole point of having no timestamp in the document
    if Deriver(a.run_root, a.trunk).dumps(Deriver(a.run_root, a.trunk).build()) != d.dumps(doc):
        return _die("ABORT: two derivations of the same inputs differ. The output is not "
                    "reproducible, so a reader cannot verify the mapping.")
    print("  a second derivation is byte-identical -- OK")

    if not a.out:
        return 0
    out = Path(a.out)
    body = d.dumps(doc)
    if a.verify:
        if not out.exists():
            return _die("ABORT: %s does not exist; nothing to verify." % out)
        if out.read_text(encoding="utf-8") != body:
            return _die("ABORT: %s does not match a fresh derivation. Either the sealed "
                        "records changed or the file was edited. The RECORDS win: "
                        "regenerate." % out)
        print("\nverified: %s matches a fresh derivation byte for byte." % out)
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print("\nwrote %s (%d entries); the sealed records were NOT opened for writing."
          % (out, len(entries)))
    return 0


def _die(msg):
    print(msg, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
