#!/usr/bin/env python3
"""vplint.py -- lint for packets, benchmarks and role records (K-11).

    python3 vplint.py packet   <PACKET.md> <BENCHMARK.md> [--trunk DIR] [--json]
    python3 vplint.py review   <REVIEW.json> [--benchmark BENCHMARK.md]
    python3 vplint.py findings <FINDINGS.json> [--benchmark BENCHMARK.md] [--worktree WT]
    python3 vplint.py roster   <roster.json>

Exit 0 = clean, 1 = findings, 2 = usage.  Output: one line per finding,
`LEVEL code: message`.  stdlib only.

PACKET.md header is a fenced front-matter block:
    ---
    item: SAFE-11
    title: ...
    group: 3
    base_sha: <40 hex>
    depends_on: []           # or a YAML list
    releases: [OPS-06]
    critical: false
    owned_files:
      - platform/api/admin.py
    forbidden_files:
      - platform/db/migrations/**
    test_paths:
      - platform/tests/api/test_x.py
    proof_kind: platform
    max_rounds: 4
    max_minutes_build: 45
    reviewer_model: gpt-5.6-sol
    owner_needed: none
    ---
BENCHMARK.md lines:  `- B1 [invariant] text — check: how`
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vpschema  # noqa: E402

REQUIRED_KEYS = ("item", "title", "group", "base_sha", "owned_files", "test_paths",
                 "proof_kind", "max_rounds")
LIST_KEYS = ("depends_on", "releases", "owned_files", "forbidden_files", "test_paths")
KINDS = ("invariant", "test", "negative", "forbidden", "evidence")
_BENCH_RE = re.compile(r"^\s*-\s+(B\d+)\s+\[(\w+)\]\s+(.+)$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")


def parse_front_matter(text):
    """Tiny YAML subset: `key: scalar`, `key: [a, b]`, `key:` + `  - item`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, ["packet: no front-matter block (---)"]
    data, errs, key = {}, [], None
    i = 1
    while i < len(lines):
        ln = lines[i]
        i += 1
        if ln.strip() == "---":
            return data, errs
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s*-\s+(.*)$", ln)
        if m and key is not None and isinstance(data.get(key), list):
            data[key].append(m.group(1).strip().strip('"').strip("'"))
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", ln)
        if not m:
            errs.append("packet: unparsable header line %d: %r" % (i, ln))
            continue
        key, val = m.group(1), m.group(2).split(" #", 1)[0].strip()
        if val == "":
            data[key] = []
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            data[key] = [v.strip().strip('"').strip("'")
                         for v in inner.split(",") if v.strip()] if inner else []
        else:
            low = val.lower()
            if low in ("true", "false"):
                data[key] = (low == "true")
            elif re.match(r"^-?\d+$", val):
                data[key] = int(val)
            else:
                data[key] = val.strip('"').strip("'")
    return data, errs + ["packet: front-matter never closed"]


def parse_benchmark(text):
    rows, errs, seen = [], [], set()
    for n, ln in enumerate(text.splitlines(), 1):
        if not ln.strip().startswith("-"):
            continue
        m = _BENCH_RE.match(ln)
        if not m:
            if ln.strip().startswith("- B"):
                errs.append("benchmark: line %d does not match "
                            "'- B<n> [kind] text — check: how'" % n)
            continue
        bid, kind, body = m.groups()
        if bid in seen:
            errs.append("benchmark: duplicate id %s (line %d)" % (bid, n))
        seen.add(bid)
        if kind not in KINDS:
            errs.append("benchmark: %s has unknown kind [%s]" % (bid, kind))
        if "check:" not in body:
            errs.append("benchmark: %s has no 'check:' clause" % bid)
        rows.append({"id": bid, "kind": kind, "text": body, "line": n})
    return rows, errs


def benchmark_ids(path):
    rows, _ = parse_benchmark(Path(path).read_text(encoding="utf-8"))
    return [r["id"] for r in rows]


def _git(trunk, args):
    try:
        cp = subprocess.run(["git", "-C", str(trunk)] + list(args), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=60)
        return cp.returncode, cp.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def lint_packet(packet_path, benchmark_path, trunk=None, packets_dir=None):
    out = []
    ptxt = Path(packet_path).read_text(encoding="utf-8")
    btxt = Path(benchmark_path).read_text(encoding="utf-8")
    hdr, errs = parse_front_matter(ptxt)
    out += ["ERROR header: " + e for e in errs]
    if hdr is None:
        return out
    for k in REQUIRED_KEYS:
        if k not in hdr:
            out.append("ERROR header: missing %s" % k)
    for k in LIST_KEYS:
        if k in hdr and not isinstance(hdr[k], list):
            out.append("ERROR header: %s must be a list" % k)
            hdr[k] = []
    base = str(hdr.get("base_sha", ""))
    if not _SHA40.match(base):
        out.append("ERROR header: base_sha must be 40 lowercase hex, got %r" % base)
    elif trunk:
        rc, _ = _git(trunk, ["cat-file", "-e", base + "^{commit}"])
        if rc != 0:
            out.append("ERROR header: base_sha %s is not a commit in %s" % (base[:12], trunk))
    owned = hdr.get("owned_files") or []
    forb = hdr.get("forbidden_files") or []
    if not owned:
        out.append("ERROR header: owned_files is empty")
    for o in owned:
        for f in forb:
            if fnmatch.fnmatch(o, f) or o == f:
                out.append("ERROR header: owned file %s matches forbidden %s" % (o, f))
    tests = hdr.get("test_paths") or []
    if hdr.get("proof_kind") != "docs" and not tests:
        out.append("ERROR header: test_paths is empty for proof_kind %s"
                   % hdr.get("proof_kind"))
    if trunk and _SHA40.match(base):
        for t in tests:
            rc, _ = _git(trunk, ["cat-file", "-e", "%s:%s" % (base, t)])
            if rc != 0:
                if any(fnmatch.fnmatch(t, o) or t == o for o in owned):
                    out.append("WARN header: test path %s does not exist at base "
                               "(owned: the builder must create it)" % t)
                else:
                    out.append("ERROR header: test path %s does not exist at base and "
                               "is not in owned_files" % t)
    if hdr.get("proof_kind") not in ("platform", "portal", "agent", "deploy", "docs"):
        out.append("ERROR header: proof_kind must be platform|portal|agent|deploy|docs")
    mr = hdr.get("max_rounds")
    if not isinstance(mr, int) or mr < 1 or mr > 8:
        out.append("ERROR header: max_rounds must be an int 1..8")
    if hdr.get("item") in (hdr.get("depends_on") or []):
        out.append("ERROR header: item depends on itself")
    if packets_dir:
        for d in hdr.get("depends_on") or []:
            if not (Path(packets_dir) / d / "PACKET.md").exists():
                out.append("WARN header: depends_on %s has no packet in %s" % (d, packets_dir))
    for sec in ("## Goal", "## Witnesses", "## Steps", "## Prohibitions"):
        if sec not in ptxt:
            out.append("ERROR body: missing section %s" % sec)
    if "file:line" in ptxt and "## Witnesses" in ptxt:
        pass
    wit = ptxt.split("## Witnesses", 1)[1].split("## ", 1)[0] if "## Witnesses" in ptxt else ""
    if wit and not re.search(r"[\w./-]+\.[A-Za-z]{1,6}:\d+", wit):
        out.append("ERROR body: Witnesses has no file:line citation")
    rows, berrs = parse_benchmark(btxt)
    out += ["ERROR " + e for e in berrs]
    kinds = {r["kind"] for r in rows}
    for k in KINDS:
        if k not in kinds:
            out.append("ERROR benchmark: no [%s] line" % k)
    for r in rows:
        if r["kind"] == "test":
            # .py for platform/agent/deploy proofs; .ts/.tsx for portal (vitest) proofs.
            m = re.search(r"([\w./-]+\.(?:py|tsx?))(::\S+)?", r["text"])
            if not m:
                out.append("ERROR benchmark: %s [test] names no .py/.ts/.tsx path" % r["id"])
            elif tests and not any(m.group(1) == t or m.group(1).startswith(t)
                                   for t in tests):
                out.append("WARN benchmark: %s test path %s is not in header test_paths"
                           % (r["id"], m.group(1)))
    return out


def lint_review(review_path, benchmark_path=None):
    ids = benchmark_ids(benchmark_path) if benchmark_path else None
    ok, errs = vpschema.validate_review(review_path, ids)
    return ["ERROR review: " + e for e in errs]


def lint_findings(findings_path, benchmark_path=None, worktree=None):
    out = []
    ok, errs = vpschema.validate_findings(findings_path)
    out += ["ERROR findings: " + e for e in errs]
    if not ok:
        return out
    doc = json.loads(Path(findings_path).read_text(encoding="utf-8"))
    got = {str(ln.get("id")) for ln in doc.get("lines", [])}
    if benchmark_path:
        want = set(benchmark_ids(benchmark_path))
        missing = sorted(want - got)
        if missing:
            out.append("ERROR findings: benchmark ids without a verdict: %s"
                       % ", ".join(missing))
    if doc.get("all_pass") and worktree:
        rc, txt = _git(worktree, ["diff", "--stat", "%s..HEAD" % doc.get("commit", "HEAD")])
        # SATISFIED on an empty diff vs base is a false green (07 F-O15)
        base = None
        pk = Path(worktree) / ".vp" / "PACKET.md"
        if pk.exists():
            hdr, _ = parse_front_matter(pk.read_text(encoding="utf-8"))
            base = (hdr or {}).get("base_sha")
        if base:
            rc, txt = _git(worktree, ["diff", "--stat", "%s..HEAD" % base])
            if rc == 0 and not txt.strip():
                out.append("ERROR findings: all_pass on an EMPTY diff vs base %s" % base[:12])
    for ln in doc.get("lines", []):
        if ln.get("verdict") in ("PASS", "FAIL") and not str(ln.get("evidence", "")).strip():
            out.append("ERROR findings: %s has a verdict without evidence" % ln.get("id"))
    return out


def lint_roster(path):
    out = []
    r = json.loads(Path(path).read_text(encoding="utf-8"))
    roles = r.get("roles", {})
    b = roles.get("builder", {}).get("runner")
    s = roles.get("senior", {}).get("runner")
    f = roles.get("final", {}).get("runner")
    if b and s and b == s:
        out.append("ERROR roster: senior runner equals builder runner (%s): independence lost" % b)
    if s and f and s == f:
        out.append("ERROR roster: final runner equals senior runner (%s): independence lost" % s)
    for name, srv in (r.get("servers") or {}).items():
        x = srv.get("xdg_data_home")
        if not x or not os.path.isdir(x):
            out.append("ERROR roster: server %s xdg_data_home missing: %s" % (name, x))
        elif not os.path.exists(os.path.join(x, "opencode", "log", "opencode.log")):
            out.append("WARN roster: server %s has no opencode.log yet under %s" % (name, x))
    for g, cfg in (r.get("groups") or {}).items():
        if cfg.get("server") not in (r.get("servers") or {}):
            out.append("ERROR roster: group %s names unknown server %s" % (g, cfg.get("server")))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vplint.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pk = sub.add_parser("packet")
    pk.add_argument("packet")
    pk.add_argument("benchmark")
    pk.add_argument("--trunk", default=None)
    pk.add_argument("--packets-dir", default=None)
    rv = sub.add_parser("review")
    rv.add_argument("review")
    rv.add_argument("--benchmark", default=None)
    fd = sub.add_parser("findings")
    fd.add_argument("findings")
    fd.add_argument("--benchmark", default=None)
    fd.add_argument("--worktree", default=None)
    ro = sub.add_parser("roster")
    ro.add_argument("roster")
    for p in (pk, rv, fd, ro):
        p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "packet":
        out = lint_packet(args.packet, args.benchmark, args.trunk, args.packets_dir)
    elif args.cmd == "review":
        out = lint_review(args.review, args.benchmark)
    elif args.cmd == "findings":
        out = lint_findings(args.findings, args.benchmark, args.worktree)
    else:
        out = lint_roster(args.roster)
    errors = [o for o in out if o.startswith("ERROR")]
    if args.json:
        print(json.dumps({"ok": not errors, "findings": out}, indent=2))
    else:
        for o in out:
            print(o)
        if not out:
            print("clean")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
