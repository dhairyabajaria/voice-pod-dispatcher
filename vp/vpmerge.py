#!/usr/bin/env python3
"""vpmerge.py -- resolve the additive-inventory merge class (D49) by arithmetic.

Route-adding items all bump the same closed inventories: the TECHNICAL.md
route 5-tuple (parsed by test_docs_truth), `assert n == N` and the three
`len(...) == N` lines in test_permission_matrix.py, `len(gated) == N` in
test_capability_contract.py, `len(derived) == len(GATED_INVENTORY) == N` in
test_capability_route_gate.py, plus the dict / set entries those tests
enumerate.  Two items that each add routes conflict on every one of those
lines, and when both bump a number by the same amount git auto-merges the
"identical" edit as ONE bump (502+2 and 502+2 -> 504 on both sides -> 504,
which is wrong: the merged tree has 506 routes).

The rule is the three-way sum: result = theirs + (ours - base), computed from
the true merge base, never from the working copy.  Only the files named in
SPECS are touched; a conflict block outside a pure both-side insertion or the
numeric line is refused, and a refusal resolves nothing (the caller aborts the
merge exactly as before).

    python3 vpmerge.py resolve [--cwd WORKTREE]     # mid-merge, by hand

stdlib only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# path -> list of regexes; every capture group is one additive integer.
SPECS = {
    "TECHNICAL.md": [
        re.compile(
            r"discovers all ([\d,]+) routes from\s+`app\.routes`;\s+([\d,]+) unauthenticated"
            r" or externally\s+authenticated routes,\s+each\s+named;\s+([\d,]+) refused to"
            r" Viewer;\s+([\d,]+) cookie-authenticated writes;\s+([\d,]+) `/admin` routes"),
    ],
    "platform/tests/test_permission_matrix.py": [
        re.compile(r'assert n == (\d+), f"discovered \{n\} routes; the reviewed route '
                   r'inventory is (\d+)"'),
        re.compile(r"assert len\(_VIEWER_REFUSED\) == (\d+)"),
        re.compile(r"assert len\(_COOKIE_WRITES\) == (\d+)"),
        re.compile(r"assert len\(_ADMIN_ROUTES\) == (\d+)"),
        # the same counts again on the assert messages' second line (union-61:
        # the message line kept the conflict alive after the assert was summed)
        re.compile(r"set; the reviewed permission matrix contains (\d+)"),
        re.compile(r"reviewed CSRF matrix contains (\d+)"),
        re.compile(r"route inventory contains (\d+)"),
    ],
    "platform/tests/test_capability_contract.py": [
        re.compile(r"assert len\(gated\) == (\d+)"),
    ],
    "platform/tests/test_capability_route_gate.py": [
        re.compile(r"assert len\(derived\) == len\(GATED_INVENTORY\) == (\d+)"),
    ],
}

# every union whose merge this module touched proves these on the merged tree
PROOF_PATHS = ("platform/tests/test_docs_truth.py",
               "platform/tests/test_permission_matrix.py",
               "platform/tests/test_capability_contract.py",
               "platform/tests/test_capability_route_gate.py")

_BLOCK = re.compile(
    r"^<<<<<<< [^\n]*\n(?P<ours>.*?)^\|\|\|\|\|\|\| [^\n]*\n(?P<base>.*?)^=======\n"
    r"(?P<theirs>.*?)^>>>>>>> [^\n]*\n", re.S | re.M)


class Refused(Exception):
    """The conflict is outside the class; nothing was written."""


def _ints(m):
    return [int(g.replace(",", "")) for g in m.groups()]


def numbers(path, text):
    """[[ints per regex] ...] or None when any regex misses (the file is not
    in the shape the class expects; refuse rather than guess)."""
    out = []
    for rx in SPECS.get(path, ()):
        m = rx.search(text)
        if not m:
            return None
        out.append(_ints(m))
    return out


def summed(path, base, ours, theirs):
    """theirs + (ours - base), per capture group; None when a side misses."""
    b, o, t = numbers(path, base), numbers(path, ours), numbers(path, theirs)
    if b is None or o is None or t is None:
        return None
    return [[t[i][j] + (o[i][j] - b[i][j]) for j in range(len(b[i]))] for i in range(len(b))]


def apply_numbers(path, text, values):
    """Rewrite each regex match's groups with `values`; regex misses are left."""
    for rx, vals in zip(SPECS.get(path, ()), values):
        m = rx.search(text)
        if not m:
            continue
        s = m.group(0)
        pieces, last = [], 0
        for gi, v in enumerate(vals, 1):
            a, b = m.start(gi) - m.start(0), m.end(gi) - m.start(0)
            pieces.append(s[last:a])
            pieces.append(str(v))
            last = b
        pieces.append(s[last:])
        text = text[:m.start(0)] + "".join(pieces) + text[m.end(0):]
    return text


def resolve_diff3(path, marked):
    """Resolve every diff3 conflict block in `marked`: a block whose base side
    is empty is a both-side insertion -> ours + theirs, in that order.  Any
    other block is refused (the numeric lines were made identical on all
    three sides before the merge, so they never reach here)."""
    def one(m):
        ours, base, theirs = m.group("ours"), m.group("base"), m.group("theirs")
        if not base.strip():
            return ours + theirs
        line = marked.count("\n", 0, m.start()) + 1
        raise Refused("%s:%d: conflict is not a both-side insertion or an inventory "
                      "number" % (path, line))
    out = _BLOCK.sub(one, marked)
    if "<<<<<<<" in out or ">>>>>>>" in out:
        raise Refused("%s: a conflict marker survived (non-diff3 block?)" % path)
    return out


def merge_texts(path, base, ours, theirs):
    """Full three-way merge of one class file.  The inventory numbers are
    first rewritten to their sum on ALL three sides (so git sees them as
    unchanged), then `git merge-file --diff3` merges the rest and only
    both-side insertions are accepted from the conflict blocks."""
    values = summed(path, base, ours, theirs)
    if SPECS.get(path) and values is None:
        raise Refused("%s: inventory line missing on one side; cannot sum" % path)
    if values:
        base, ours, theirs = (apply_numbers(path, t, values) for t in (base, ours, theirs))
    with tempfile.TemporaryDirectory() as td:
        names = []
        for tag, txt in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = os.path.join(td, tag)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(txt)
            names.append(p)
        cp = subprocess.run(["git", "merge-file", "-p", "--diff3",
                             "-L", "ours", "-L", "base", "-L", "theirs"] + names,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True)
    if cp.returncode < 0:
        raise Refused("%s: git merge-file failed: %s" % (path, cp.stderr.strip()[:200]))
    return resolve_diff3(path, cp.stdout) if cp.returncode > 0 else cp.stdout


# -- git plumbing ------------------------------------------------------------

def _show(git, wt, spec):
    rc, out, err = git(["-C", str(wt), "show", spec])
    return out if rc == 0 else None


def _both_changed(git, wt, base, ours, theirs):
    def changed(a, b):
        rc, out, _ = git(["-C", str(wt), "diff", "--name-only", "%s..%s" % (a, b)])
        return set(l.strip() for l in out.splitlines() if l.strip()) if rc == 0 else set()
    return sorted(changed(base, ours) & changed(base, theirs))


def resolve_merge(git, wt, ours, theirs):
    """Called on a FAILED `git merge` (index holds the conflict).  Resolves
    every conflicted file that is in the class, fixes the numbers of class
    files git merged cleanly, stages them and returns {path: note}.  Raises
    Refused (after touching nothing) when any conflicted file is outside the
    class or a block is not resolvable; the caller then aborts the merge."""
    wt = Path(wt)
    rc, out, _ = git(["-C", str(wt), "diff", "--name-only", "--diff-filter=U"])
    conflicted = [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []
    outside = [p for p in conflicted if p not in SPECS]
    if outside:
        raise Refused("conflict outside the inventory class: %s" % ", ".join(outside))
    rc, mb, _ = git(["-C", str(wt), "merge-base", ours, theirs])
    base = mb.strip() if rc == 0 and mb.strip() else None
    if not base:
        raise Refused("no merge base between %s and %s" % (ours[:12], theirs[:12]))
    planned = {}
    for p in conflicted:
        b = _show(git, wt, ":1:%s" % p)
        o = _show(git, wt, ":2:%s" % p)
        t = _show(git, wt, ":3:%s" % p)
        if b is None or o is None or t is None:
            raise Refused("%s: not all three index stages present" % p)
        planned[p] = (merge_texts(p, b, o, t), "conflict resolved")
    for p in _both_changed(git, wt, base, ours, theirs):
        if p in planned or p not in SPECS:
            continue
        b, o, t = (_show(git, wt, "%s:%s" % (s, p)) for s in (base, ours, theirs))
        if b is None or o is None or t is None:
            continue
        values = summed(p, b, o, t)
        if values is None:
            raise Refused("%s: inventory line missing on one side; cannot sum" % p)
        cur = (wt / p).read_text(encoding="utf-8")
        fixed = apply_numbers(p, cur, values)
        if fixed != cur:
            planned[p] = (fixed, "identical bump re-summed to %s" % values)
    # nothing raised: write everything, then stage
    for p, (text, _note) in planned.items():
        (wt / p).write_text(text, encoding="utf-8")
    if planned:
        rc, out, err = git(["-C", str(wt), "add", "--"] + list(planned))
        if rc != 0:
            raise Refused("git add failed: %s" % (err or out)[:200])
    return {p: n for p, (_t, n) in planned.items()}


def fix_clean_merge(git, wt, ours, theirs):
    """Called after a SUCCESSFUL merge: re-sum class files both sides changed
    (git's identical-edit shortcut is wrong for counters).  Writes and stages
    the fixes; returns {path: note}.  The caller amends the merge commit."""
    wt = Path(wt)
    rc, mb, _ = git(["-C", str(wt), "merge-base", ours, theirs])
    base = mb.strip() if rc == 0 and mb.strip() else None
    if not base:
        return {}
    fixed = {}
    for p in _both_changed(git, wt, base, ours, theirs):
        if p not in SPECS or not (wt / p).exists():
            continue
        b, o, t = (_show(git, wt, "%s:%s" % (s, p)) for s in (base, ours, theirs))
        if b is None or o is None or t is None:
            continue
        values = summed(p, b, o, t)
        if values is None:
            continue
        cur = (wt / p).read_text(encoding="utf-8")
        new = apply_numbers(p, cur, values)
        if new != cur:
            (wt / p).write_text(new, encoding="utf-8")
            fixed[p] = "identical bump re-summed to %s" % values
    if fixed:
        git(["-C", str(wt), "add", "--"] + list(fixed))
    return fixed


def _cli_git(args):
    cp = subprocess.run(["git"] + list(args), stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        universal_newlines=True)
    return cp.returncode, cp.stdout, cp.stderr


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vpmerge.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rs = sub.add_parser("resolve", help="resolve the class in an in-progress merge")
    rs.add_argument("--cwd", default=".")
    args = ap.parse_args(argv)
    wt = Path(args.cwd).resolve()
    rc, head, _ = _cli_git(["-C", str(wt), "rev-parse", "HEAD"])
    rc2, mh, _ = _cli_git(["-C", str(wt), "rev-parse", "MERGE_HEAD"])
    if rc != 0 or rc2 != 0:
        print("no merge in progress in %s (MERGE_HEAD missing)" % wt, file=sys.stderr)
        return 2
    try:
        done = resolve_merge(_cli_git, wt, head.strip(), mh.strip())
    except Refused as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 1
    for p, note in done.items():
        print("%s: %s" % (p, note))
    if not done:
        print("nothing in the class needed resolving")
    print("staged; finish with: git commit --no-edit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
