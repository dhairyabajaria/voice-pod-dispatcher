#!/usr/bin/env python3
"""guard_controls.py -- run one recorded guard-mutation control and stamp its log.

GUARD-MUTATION-REGISTRY-SPEC §"The problem, stated so it can be falsified": a guard
passes only when it has been SHOWN to refuse -- a single edit that bypasses it, the
node that reddened, and the log that shows the red.  A test's existence proves
nothing; neither does a sentence saying the test would fail.

This is the executable half.  Each control in guard_registry.json names an `edit`
(an exact old/new pair) and a `node`.  This runner:

  1. mirrors the package into a scratch tree -- the LIVE driver is never mutated
     (a live edit is an AUTHORITY_MISMATCH and rides the next reload untested),
  2. asserts the node is GREEN unmutated -- without that, a red proves nothing,
  3. applies exactly one edit,
  4. asserts the node is RED, and captures the observed failure,
  5. restores by rebuilding the mirror from disk rather than by un-applying the
     edit: an inverse-patch restore leaves the tree mutated whenever the sequence
     dies between steps, and a stale mutation reads as a real defect later.

The log is named by RUN, never by purpose: two runs of the same control must not
collide, or the second silently destroys the first's evidence.

Usage:
    guard_controls.py list
    guard_controls.py run <guard_id> [--control N] [--registry PATH]
"""
from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

VP = Path(__file__).resolve().parent
DISPATCHER = VP.parent
REGISTRY = VP / "guard_registry.json"
LOGS = VP / "guard-controls"


def utc_stamp() -> str:
    """RUN-<utc>, read from the clock and never computed by hand."""
    return "RUN-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_registry(path: Path = REGISTRY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_symbol(path: str, symbol: str, root: Path = DISPATCHER):
    """The guard's own source text, located by AST and never by grep.

    SPEC clause 1: a renamed or deleted guard must fail LOUDLY rather than drop
    out of the population.  grep cannot tell `def open_answers` from the string
    "open_answers" in a docstring, and a name that has moved to a different class
    still matches -- so the population would silently change shape while every
    check kept passing.  `symbol` is dotted: "Class.method" or "function".
    Returns the exact source segment, or None when it no longer resolves.
    """
    src = (root / path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    parts = symbol.split(".")
    node = tree
    for want in parts:
        nxt = None
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                    and child.name == want:
                nxt = child
                break
        if nxt is None:
            return None
        node = nxt
    return ast.get_source_segment(src, node)


def source_sha256(path: str, symbol: str, root: Path = DISPATCHER):
    """SPEC clause 2: a REWRITTEN guard must not coast on an old red.  Hashing
    the whole FILE would redden on every unrelated edit and be turned off within
    a day; hashing the guard's own segment changes exactly when the guard does."""
    seg = resolve_symbol(path, symbol, root)
    return None if seg is None else hashlib.sha256(seg.encode("utf-8")).hexdigest()


def mirror(root: Path) -> Path:
    """A scratch copy laid out like the real tree: the harness resolves
    CONTROL_DIR as VP.parent.parent/'voice-pod'/..., so a copy anywhere else
    fails every scheduler-backed test for a PATH reason that looks exactly
    like a defect (2026-09-20: 106 reds, none of them real)."""
    if root.exists():
        shutil.rmtree(root)
    (root / "dispatcher").mkdir(parents=True)
    shutil.copytree(VP, root / "dispatcher" / "vp")
    for sibling in DISPATCHER.glob("*.py"):
        shutil.copy2(sibling, root / "dispatcher" / sibling.name)
    real_vp = DISPATCHER.parent / "voice-pod"
    if real_vp.exists():
        os.symlink(real_vp, root / "voice-pod")
    return root


def run_node(root: Path, node: str, timeout_s: int = 900) -> tuple[int, str]:
    """pytest one node in the mirror.  The SUMMARY LINE is the verdict: an exit
    code can be 0 through a pipeline, and a completion notice is not a result."""
    env = dict(os.environ, PYTHONPATH=str(root / "dispatcher"))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "-p", "no:cacheprovider"],
        cwd=str(root / "dispatcher" / "vp"), env=env, capture_output=True,
        text=True, timeout=timeout_s)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def apply_edit(root: Path, path: str, old: str, new: str) -> None:
    f = root / "dispatcher" / path
    src = f.read_text(encoding="utf-8")
    n = src.count(old)
    if n != 1:
        raise SystemExit("edit is not unique in %s: %d occurrences of %r" % (path, n, old[:60]))
    f.write_text(src.replace(old, new), encoding="utf-8")


def summary_line(out: str) -> str:
    """the last pytest summary line -- `N passed`/`N failed`, the only verdict."""
    for line in reversed(out.strip().splitlines()):
        if " passed" in line or " failed" in line or " error" in line:
            return line.strip()
    return "(no pytest summary line -- the run did not finish)"


def observed(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("E "):
            return line[2:].strip()[:300]
    return ""


def run_control(guard_id: str, index: int, registry_path: Path, scratch: Path) -> dict:
    reg = load_registry(registry_path)
    entry = next((g for g in reg["guards"] if g["guard_id"] == guard_id), None)
    if entry is None:
        raise SystemExit("unknown guard_id %s" % guard_id)
    control = entry["controls"][index]
    stamp = utc_stamp()
    root = mirror(scratch / ("guard-%s-%s" % (guard_id.replace(".", "_"), stamp)))
    lines = ["# guard control: %s [%d]" % (guard_id, index),
             "# stamp: %s" % stamp,
             "# path: %s  symbol: %s" % (entry["path"], entry["symbol"]),
             "# edit: %s" % control["edit"],
             "# node: %s" % control["node"], ""]

    rc, out = run_node(root, control["node"])
    green = summary_line(out)
    lines += ["## 1. BASELINE (unmutated) -- a red proves nothing unless this is green", green, ""]
    if " failed" in green or " error" in green:
        lines += ["BASELINE IS NOT GREEN -- the control is void, not passing.", out[-3000:]]
        return _write(entry, control, stamp, lines, ok=False, obs="")

    apply_edit(root, entry["path"], control["_edit_old"], control["_edit_new"])
    rc, out = run_node(root, control["node"])
    red = summary_line(out)
    obs = observed(out)
    lines += ["## 2. MUTATED -- exactly one edit applied", red,
              "observed: %s" % (obs or "(none captured)"), "",
              "## 3. pytest output (mutated run, tail)", out[-4000:]]
    ok = " failed" in red
    if not ok:
        lines += ["", "THE GUARD DID NOT BITE: the edit bypassed it and the node stayed green.",
                  "This is the vacuous-guard finding the registry exists to catch."]
    shutil.rmtree(root, ignore_errors=True)   # restore by rebuild, never by inverse patch
    return _write(entry, control, stamp, lines, ok, obs)


def _write(entry, control, stamp, lines, ok, obs) -> dict:
    d = LOGS / entry["guard_id"].replace(".", "_")
    d.mkdir(parents=True, exist_ok=True)
    log = d / ("%s.log" % stamp)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rel = str(log.relative_to(VP))
    print(json.dumps({"guard_id": entry["guard_id"], "node": control["node"],
                      "bit": ok, "observed": obs, "log": rel, "stamp": stamp}, indent=2))
    return {"log": rel, "observed": obs, "bit": ok, "ts": stamp}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("list", "run"))
    ap.add_argument("guard_id", nargs="?")
    ap.add_argument("--control", type=int, default=0)
    ap.add_argument("--registry", default=str(REGISTRY))
    ap.add_argument("--scratch", default=os.environ.get("TMPDIR", "/tmp"))
    args = ap.parse_args(argv)
    reg = load_registry(Path(args.registry))
    if args.cmd == "list":
        for g in reg["guards"]:
            print("%-52s %s  (%d control(s))" % (g["guard_id"], g["symbol"], len(g["controls"])))
        return 0
    if not args.guard_id:
        ap.error("run needs a guard_id")
    res = run_control(args.guard_id, args.control, Path(args.registry), Path(args.scratch))
    return 0 if res["bit"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
