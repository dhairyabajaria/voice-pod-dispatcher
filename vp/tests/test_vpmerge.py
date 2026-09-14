#!/usr/bin/env python3
"""tests/test_vpmerge.py -- the additive-inventory merge class (D49) on a real
temp git repo.  No box, no network.

    python3 tests/test_vpmerge.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import vpmerge  # noqa: E402

TECH = "TECHNICAL.md"
PM = "platform/tests/test_permission_matrix.py"
RG = "platform/tests/test_capability_route_gate.py"
APP = "platform/api/app.py"


def sh(cwd, *args):
    cp = subprocess.run(["git", "-C", str(cwd)] + list(args), stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    return cp.returncode, cp.stdout, cp.stderr


def git(cwd, *args):
    rc, out, err = sh(cwd, *args)
    assert rc == 0, (args, out, err)
    return out.strip()


def gitfn(args):
    return sh(args[1], *args[2:]) if args[0] == "-C" else sh(".", *args)


def tech(n, ua, vr, cw, ad, extra=""):
    return ("Platform tests run against real Postgres.\n"
            "the catalogue), `test_permission_matrix.py` (discovers all %d routes from\n"
            "`app.routes`; %d unauthenticated or externally authenticated routes, each\n"
            "named; %d refused to Viewer; %d cookie-authenticated writes; %d `/admin` routes).\n"
            "%sMore prose.\n" % (n, ua, vr, cw, ad, extra))


def pm(n, vr, cw, ad, viewer_rows, ledger=""):
    """The real two-line shape: the count on the assert AND on its message."""
    return ("_VIEWER_REFUSED = [\n%s]\n\n"
            "def test_inventory():\n"
            '    assert n == %d, f"discovered {n} routes; the reviewed route inventory is %d"\n'
            "%s"
            "    assert len(_VIEWER_REFUSED) == %d, (\n"
            '        f"found {len(_VIEWER_REFUSED)} routes gated above viewer\'s permission "\n'
            '        "set; the reviewed permission matrix contains %d"\n    )\n'
            "    assert len(_COOKIE_WRITES) == %d, (\n"
            '        f"found {len(_COOKIE_WRITES)} cookie-authenticated writes; the "\n'
            '        "reviewed CSRF matrix contains %d"\n    )\n'
            "    assert len(_ADMIN_ROUTES) == %d, (\n"
            '        f"found {len(_ADMIN_ROUTES)} /admin routes; the reviewed admin "\n'
            '        "route inventory contains %d"\n    )\n'
            % ("".join("    %s,\n" % r for r in viewer_rows), n, n, ledger,
               vr, vr, cw, cw, ad, ad))


def rg(n, rows):
    return ("GATED_INVENTORY = {\n%s}\n\ndef test_gate():\n"
            "    assert len(derived) == len(GATED_INVENTORY) == %d\n"
            % ("".join("    %s,\n" % r for r in rows), n))


class Repo(object):
    def __init__(self, tmp):
        self.d = Path(tmp) / "repo"
        self.d.mkdir()
        git(self.d, "init", "-q", "-b", "main")
        git(self.d, "config", "user.email", "t@t")
        git(self.d, "config", "user.name", "t")
        (self.d / "platform" / "tests").mkdir(parents=True)
        (self.d / "platform" / "api").mkdir(parents=True)

    def write(self, path, text):
        (self.d / path).write_text(text)

    def commit(self, msg):
        git(self.d, "add", "-A")
        git(self.d, "commit", "-q", "-m", msg)
        return git(self.d, "rev-parse", "HEAD")


def base_repo(tmp):
    r = Repo(tmp)
    r.write(TECH, tech(500, 34, 212, 246, 118))
    r.write(PM, pm(500, 212, 246, 118, ['("GET", "/a")']))
    r.write(RG, rg(29, ['("POST", "/campaigns"): _C']))
    r.write(APP, "include(a)\n")
    base = r.commit("base")
    return r, base


def test_conflicting_bumps_are_summed_and_entries_unioned():
    with tempfile.TemporaryDirectory() as tmp:
        r, base = base_repo(tmp)
        # ours: +2 routes, both viewer-refused, one gated
        git(r.d, "checkout", "-q", "-b", "ours")
        r.write(TECH, tech(502, 34, 214, 248, 118))
        r.write(PM, pm(502, 214, 248, 118, ['("GET", "/a")', '("GET", "/x1")', '("GET", "/x2")']))
        r.write(RG, rg(30, ['("POST", "/campaigns"): _C', '("POST", "/x1"): _X']))
        ours = r.commit("ours")
        # theirs: +1 route, admin
        git(r.d, "checkout", "-q", "-b", "theirs", base)
        r.write(TECH, tech(501, 34, 212, 246, 119))
        r.write(PM, pm(501, 212, 246, 119, ['("GET", "/a")', '("GET", "/y")']))
        r.write(RG, rg(30, ['("POST", "/campaigns"): _C', '("POST", "/y"): _Y']))
        theirs = r.commit("theirs")
        git(r.d, "checkout", "-q", "ours")
        rc, out, err = sh(r.d, "merge", "--no-ff", "--no-edit", "-m", "u", theirs)
        assert rc != 0, "expected a conflict"
        done = vpmerge.resolve_merge(gitfn, r.d, ours, theirs)
        assert set(done) == {TECH, PM, RG}, done
        t = (r.d / TECH).read_text()
        assert vpmerge.numbers(TECH, t) == [[503, 34, 214, 248, 119]], t
        p = (r.d / PM).read_text()
        assert vpmerge.numbers(PM, p) == [[503, 503], [214], [248], [119], [214], [248], [119]], p
        for row in ('("GET", "/a")', '("GET", "/x1")', '("GET", "/x2")', '("GET", "/y")'):
            assert row in p, row
        g = (r.d / RG).read_text()
        assert vpmerge.numbers(RG, g) == [[31]], g
        assert '("POST", "/x1"): _X' in g and '("POST", "/y"): _Y' in g
        assert "<<<<<<<" not in t + p + g
        rc, out, err = sh(r.d, "commit", "--no-edit")
        assert rc == 0, err
        rc, out, _ = sh(r.d, "diff", "--name-only", "--diff-filter=U")
        assert out.strip() == ""


def test_union_61_shape_ledger_comment_plus_two_line_assert():
    """union-61 (A6-1 vs A3-1a): each side adds its own ledger comment above
    the assert and bumps BOTH the assert and the message line.  Refused before
    the message-line regexes existed; now the numbers agree on all sides and
    the block is a both-side comment insertion."""
    with tempfile.TemporaryDirectory() as tmp:
        r, base = base_repo(tmp)
        git(r.d, "checkout", "-q", "-b", "ours")
        r.write(PM, pm(502, 214, 248, 118, ['("GET", "/a")', '("GET", "/x1")', '("GET", "/x2")'],
                       ledger="    # 212 -> 214: A6-1 adds two legal routes.\n"))
        ours = r.commit("ours")
        git(r.d, "checkout", "-q", "-b", "theirs", base)
        r.write(PM, pm(501, 213, 247, 118, ['("GET", "/a")', '("GET", "/y")'],
                       ledger="    # 212 -> 213: A3-1a adds PUT states.\n"))
        theirs = r.commit("theirs")
        git(r.d, "checkout", "-q", "ours")
        rc, _, _ = sh(r.d, "merge", "--no-ff", "--no-edit", "-m", "u", theirs)
        assert rc != 0
        done = vpmerge.resolve_merge(gitfn, r.d, ours, theirs)
        assert PM in done, done
        p = (r.d / PM).read_text()
        assert "<<<<<<<" not in p
        assert vpmerge.numbers(PM, p) == [[503, 503], [215], [249], [118], [215], [249], [118]], p
        assert "A6-1 adds two legal routes" in p and "A3-1a adds PUT states" in p
        assert p.index("A6-1 adds") < p.index("A3-1a adds")


def test_identical_bumps_merged_clean_by_git_are_resummed():
    with tempfile.TemporaryDirectory() as tmp:
        r, base = base_repo(tmp)
        git(r.d, "checkout", "-q", "-b", "ours")
        r.write(TECH, tech(502, 34, 212, 246, 118))          # +2 on ours
        ours = r.commit("ours")
        git(r.d, "checkout", "-q", "-b", "theirs", base)
        r.write(TECH, tech(502, 34, 212, 246, 118))          # +2 on theirs: identical text
        theirs = r.commit("theirs")
        git(r.d, "checkout", "-q", "ours")
        rc, out, err = sh(r.d, "merge", "--no-ff", "--no-edit", "-m", "u", theirs)
        assert rc == 0, (out, err)                          # git calls it clean
        assert vpmerge.numbers(TECH, (r.d / TECH).read_text()) == [[502, 34, 212, 246, 118]]
        fixed = vpmerge.fix_clean_merge(gitfn, r.d, ours, theirs)
        assert TECH in fixed, fixed
        assert vpmerge.numbers(TECH, (r.d / TECH).read_text()) == [[504, 34, 212, 246, 118]]
        git(r.d, "commit", "--amend", "--no-edit", "-q")
        assert vpmerge.numbers(TECH, git(r.d, "show", "HEAD:" + TECH) + "\n") == \
            [[504, 34, 212, 246, 118]]


def test_conflict_outside_the_class_is_refused_and_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        r, base = base_repo(tmp)
        git(r.d, "checkout", "-q", "-b", "ours")
        r.write(TECH, tech(501, 34, 212, 246, 118))
        r.write(APP, "include(a)\ninclude(x)\n")
        ours = r.commit("ours")
        git(r.d, "checkout", "-q", "-b", "theirs", base)
        r.write(TECH, tech(501, 34, 212, 246, 118))
        r.write(APP, "include(a)\ninclude(y)\n")
        theirs = r.commit("theirs")
        git(r.d, "checkout", "-q", "ours")
        rc, _, _ = sh(r.d, "merge", "--no-ff", "--no-edit", "-m", "u", theirs)
        assert rc != 0
        before = (r.d / APP).read_text()
        try:
            vpmerge.resolve_merge(gitfn, r.d, ours, theirs)
        except vpmerge.Refused as exc:
            assert APP in str(exc), exc
        else:
            raise AssertionError("app.py conflict was not refused")
        assert (r.d / APP).read_text() == before      # nothing written
        rc, out, _ = sh(r.d, "diff", "--name-only", "--diff-filter=U")
        assert APP in out
        git(r.d, "merge", "--abort")


def test_prose_conflict_in_a_class_file_is_refused():
    """The item rewrote the sentence around the tuple; that is not arithmetic."""
    with tempfile.TemporaryDirectory() as tmp:
        r, base = base_repo(tmp)
        git(r.d, "checkout", "-q", "-b", "ours")
        r.write(TECH, tech(501, 34, 212, 246, 118).replace("More prose.", "Ours prose."))
        ours = r.commit("ours")
        git(r.d, "checkout", "-q", "-b", "theirs", base)
        r.write(TECH, tech(501, 34, 212, 246, 118).replace("More prose.", "Their prose."))
        theirs = r.commit("theirs")
        git(r.d, "checkout", "-q", "ours")
        rc, _, _ = sh(r.d, "merge", "--no-ff", "--no-edit", "-m", "u", theirs)
        assert rc != 0
        try:
            vpmerge.resolve_merge(gitfn, r.d, ours, theirs)
        except vpmerge.Refused as exc:
            assert "TECHNICAL.md" in str(exc), exc
        else:
            raise AssertionError("prose conflict was not refused")
        git(r.d, "merge", "--abort")


def test_both_side_insertions_of_prose_are_kept_in_order():
    with tempfile.TemporaryDirectory() as tmp:
        r, base = base_repo(tmp)
        git(r.d, "checkout", "-q", "-b", "ours")
        r.write(TECH, tech(501, 34, 212, 246, 118, extra="- ours added a migration line\n"))
        ours = r.commit("ours")
        git(r.d, "checkout", "-q", "-b", "theirs", base)
        r.write(TECH, tech(501, 34, 212, 246, 118, extra="- theirs added a migration line\n"))
        theirs = r.commit("theirs")
        git(r.d, "checkout", "-q", "ours")
        rc, _, _ = sh(r.d, "merge", "--no-ff", "--no-edit", "-m", "u", theirs)
        assert rc != 0
        done = vpmerge.resolve_merge(gitfn, r.d, ours, theirs)
        t = (r.d / TECH).read_text()
        assert TECH in done and "<<<<<<<" not in t
        assert t.index("ours added") < t.index("theirs added")
        assert vpmerge.numbers(TECH, t) == [[502, 34, 212, 246, 118]]


def test_cli_resolve_mid_merge():
    with tempfile.TemporaryDirectory() as tmp:
        r, base = base_repo(tmp)
        git(r.d, "checkout", "-q", "-b", "ours")
        r.write(RG, rg(30, ['("POST", "/campaigns"): _C', '("POST", "/x1"): _X']))
        r.commit("ours")
        git(r.d, "checkout", "-q", "-b", "theirs", base)
        r.write(RG, rg(30, ['("POST", "/campaigns"): _C', '("POST", "/y"): _Y']))
        theirs = r.commit("theirs")
        git(r.d, "checkout", "-q", "ours")
        rc, _, _ = sh(r.d, "merge", "--no-ff", "--no-edit", "-m", "u", theirs)
        assert rc != 0
        cp = subprocess.run([sys.executable, str(HERE.parent / "vpmerge.py"), "resolve",
                             "--cwd", str(r.d)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True)
        assert cp.returncode == 0, cp.stderr
        assert RG in cp.stdout
        assert vpmerge.numbers(RG, (r.d / RG).read_text()) == [[31]]


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name)
            except Exception:
                fails += 1
                print("FAIL %s" % name)
                traceback.print_exc()
    print("%d failed" % fails)
    raise SystemExit(1 if fails else 0)
