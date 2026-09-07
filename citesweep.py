#!/usr/bin/env python3
"""Citation sweep — resolve every citation a report and its ledger row make, at the candidate sha.

  citesweep.py <item-id> [--root DIR] [--report PATH] [--artifact ID] [--sha SHA] [--out FILE]
  citesweep.py --selftest                 known-answer check (must-pass + must-bite corpus)

Mechanises [[voicepod-sql-citations-are-stale-by-default]]: a bare `232:507` names neither a file
nor a live constraint, and an empty grep looks exactly like a clean result. Four citation shapes
are resolved against the tree AS CHECKED OUT AT THE CANDIDATE SHA — never against the lane
worktree, whose next build has already dirtied it:

  NNN:LINE          migration NNN exists and has at least LINE lines
  NNN_<name>.sql    that migration file exists
  path:line         file exists and the line is within its length
  path::test_name   the file exists and `def test_name(` is present in it. A citation that names
                    no path (`same file ::test_x`, a ledger PROOF continuation) claims only that
                    the test exists, and is resolved by definition search over the tree; one whose
                    file is not Python (`Knowledge.tsx::GapQueue.submit`) is a symbol check.

WARNING ROW, NEVER A FAIL (BOSS, 2026-09-05 16:15). A stale citation is a report defect, not a
code defect, and a guard that punishes disclosure teaches quiet fixes. The gate's PASS/FAIL is
untouched by anything in here.

THE FAILURE MODE THIS TOOL MUST NOT HAVE is scoring zero because it examined nothing — an
unfound report, a wrong root, a regex that matched none of the shapes actually in use. That is
indistinguishable from a spotless report unless the tool says so, so `found == 0` prints
EXAMINED NOTHING in the row, the file, and on stdout, and §PROVENANCE always lists the files it
opened and their byte counts even when it opened none.

Bare basenames are cited constantly (`jobs.py:231`, `test_migration_runner.py:753`), so a path
that does not resolve literally is looked up by basename across the tree. A basename matching
more than one file is resolved against the first and marked `ambiguous` in the file — resolving
it strictly would flag ~a third of real citations and the row would be ignored within a day.
"""
import argparse
import os
import re
import subprocess
import sys
from datetime import datetime

CN = os.environ.get("CN", "/Users/dhairyabajaria/Claude Code/Calling New")
D = os.path.join(CN, "test-logs", "driver")
GATES = os.path.join(D, "gates")
REPORT_DIR = os.path.join("audit", "plan-execution-2026-09-04", "reports")
LEDGER = os.path.join("plans", "EXECUTION_LEDGER.md")
MIG_DIRS = [os.path.join("platform", "db", "migrations"), os.path.join("db", "migrations")]
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", "dist", "build", ".mypy_cache"}
CODE_EXT = "py|sql|md|ts|tsx|js|json|yaml|yml|sh|toml|cfg|txt|log"

# Longest shape first: each match is masked out of the text before the next pattern runs, so
# `223_approved_assets.sql:333` is counted once as a path:line and never again as a bare filename.
RE_TESTREF = re.compile(r"([\w./-]*)::(\w+)(\[[^\]\s]*\])?")
# `path::NAME` is not always a pytest node: `core/erasure.py::ERASURE_DISPOSITIONS` names a
# module-level constant, and a `def`-only index scored four such citations unresolved on
# erasure-gap-recording-tasks while the symbol sat at core/erasure.py:105. Defs, classes and
# top-level bindings all count as definitions.
RE_PYSYM = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(|^\s*class\s+(\w+)\b|^(\w+)\s*(?::[^=\n]+)?=", re.M)
RE_PATHLINE = re.compile(rf"\b([\w./-]+\.(?:{CODE_EXT})):(\d+)\b")
RE_MIGFILE = re.compile(r"\b(\d{3}_[\w.-]+\.sql)\b")
RE_MIGLINE = re.compile(r"\b(\d{3}):(\d+)\b")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Tree:
    """The checkout under test. Every resolution goes through here and nowhere else."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.by_name = {}
        self.n_files = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                self.n_files += 1
                self.by_name.setdefault(fn, []).append(
                    os.path.relpath(os.path.join(dirpath, fn), self.root))
        # platform/ is a second root: reports cite `tests/test_x.py` from inside platform as often
        # as they cite `platform/tests/test_x.py` from the repo root.
        self.roots = [""] + [p for p in ("platform",) if os.path.isdir(os.path.join(self.root, p))]
        self._defs = None

    def defs(self):
        """python symbol -> [files defining it], built once over every .py in the tree."""
        if self._defs is None:
            self._defs = {}
            for rels in self.by_name.values():
                for rel in rels:
                    if not rel.endswith(".py"):
                        continue
                    try:
                        body = open(os.path.join(self.root, rel), errors="ignore").read()
                    except OSError:
                        continue
                    for name in RE_PYSYM.findall(body):
                        self._defs.setdefault([n for n in name if n][0], []).append(rel)
        return self._defs

    def resolve(self, path):
        """-> (relpath, ambiguous) or (None, False). Literal first, then by basename."""
        if ".." in path.split("/") or path.startswith("/"):
            return None, False
        for r in self.roots:
            cand = os.path.join(r, path) if r else path
            if os.path.isfile(os.path.join(self.root, cand)):
                return cand, False
        hits = self.by_name.get(os.path.basename(path), [])
        # A cited path with directories must agree with the candidate's tail, or it is not this file.
        if "/" in path:
            hits = [h for h in hits if h.endswith("/" + path.lstrip("./")) or h == path.lstrip("./")]
        if hits:
            return sorted(hits)[0], len(hits) > 1
        return None, False

    def migration(self, num):
        """-> relpath of migration NNN, or None. Numbered prefix is the identity, name is not."""
        for md in MIG_DIRS:
            full = os.path.join(self.root, md)
            if os.path.isdir(full):
                for fn in sorted(os.listdir(full)):
                    if fn.startswith(num + "_") and fn.endswith(".sql"):
                        return os.path.join(md, fn)
        return None

    def lines(self, rel):
        with open(os.path.join(self.root, rel), errors="ignore") as f:
            return f.read().split("\n")


def extract(text, source, residual_out=None):
    """Pull every citation out of one document. Masks each match so nothing is counted twice."""
    cites = []
    buf = list(text)
    lines = text.split("\n")

    def line_of(pos):
        return text.count("\n", 0, pos) + 1

    def mask(m):
        for i in range(m.start(), m.end()):
            buf[i] = " "

    def add(kind, pos, **kw):
        cites.append(dict(kind=kind, source=source, line=line_of(pos),
                          text=lines[line_of(pos) - 1].strip()[:200], **kw))

    # Most ::test citations name no file — `same file ::test_x`, `historical_repair::test_x`, every
    # continuation line of a ledger PROOF block. Two earlier versions guessed a file for them (the
    # last .py inside a previous ::-match; then the nearest .py above) and BOTH mis-scored the
    # calibration corpus — 12 of 25, then 7 of 26 — because §8's bullets sit below §6's file list,
    # so every one bound to the wrong file and reported a stale citation that was not stale. A
    # pathless `::name` claims only that the test EXISTS, so `check` resolves it by definition
    # search and no file is guessed here.
    for m in RE_TESTREF.finditer(text):
        lhs, name, param = m.group(1), m.group(2), m.group(3)
        named = bool(re.search(rf"\.(?:{CODE_EXT})$", lhs))
        add("path::test", m.start(), path=lhs if named else None, name=name, bound=not named,
            param=param or "", lhs=lhs)
        mask(m)
    text2 = "".join(buf)

    for m in RE_PATHLINE.finditer(text2):
        if "..." in m.group(0):  # a git diffstat abbreviation, not a citation
            continue
        add("path:line", m.start(), path=m.group(1), lineno=int(m.group(2)))
        mask(m)
    text3 = "".join(buf)

    for m in RE_MIGFILE.finditer(text3):
        add("NNN_name.sql", m.start(), path=m.group(1), num=m.group(1)[:3])
        mask(m)
    text4 = "".join(buf)

    for m in RE_MIGLINE.finditer(text4):
        add("NNN:LINE", m.start(), num=m.group(1), lineno=int(m.group(2)))
        mask(m)
    if residual_out is not None:
        # what the four shapes did NOT consume, for the separate path/sha columns below
        residual_out.append("".join(buf))
    return cites


def check(c, tree):
    """Resolve one citation in place. Sets c['ok'] and c['why']."""
    if c["kind"] == "NNN:LINE":
        rel = tree.migration(c["num"])
        if not rel:
            return c.update(ok=False, why=f"no migration {c['num']}_*.sql in the tree")
        n = len(tree.lines(rel))
        c["resolved"] = rel
        if c["lineno"] > n:
            return c.update(ok=False, why=f"{rel} has {n} lines, citation names line {c['lineno']}")
        return c.update(ok=True, why=f"{rel}:{c['lineno']} of {n}")
    if c["kind"] == "NNN_name.sql":
        rel, amb = tree.resolve(c["path"])
        if not rel:
            byno = tree.migration(c["num"])
            if byno:
                return c.update(ok=False, why=f"no {c['path']}; migration {c['num']} is {os.path.basename(byno)} (renumbered or renamed)")
            return c.update(ok=False, why=f"no such migration file, and no {c['num']}_*.sql at all")
        c["resolved"] = rel
        return c.update(ok=True, why=rel + (" (ambiguous basename)" if amb else ""))
    if c["kind"] == "path:line":
        rel, amb = tree.resolve(c["path"])
        if not rel:
            return c.update(ok=False, why="file not in the tree at this sha")
        n = len(tree.lines(rel))
        c["resolved"] = rel
        if c["lineno"] > n:
            return c.update(ok=False, why=f"{rel} has {n} lines, citation names line {c['lineno']}")
        return c.update(ok=True, why=f"{rel}:{c['lineno']} of {n}" + (" (ambiguous basename)" if amb else ""))
    # path::test_name
    if c["bound"]:
        # A pathless `::test_x` (`same file ::test_x`, a ledger PROOF continuation line, an
        # abbreviated `historical_repair::test_x`) asserts exactly one thing: that this test
        # exists. Binding it to the nearest .py named above is a GUESS, and it was wrong on 7 of
        # 26 citations in the calibration corpus — §8's bullets sit below §6's file list, so every
        # one of them bound to the last file in §6 and reported a stale citation that was not
        # stale. Resolve the claim that is actually made instead: search the tree for the def.
        where = tree.defs().get(c["name"], [])
        c["resolved"] = where[0] if where else None
        if not where:
            return c.update(ok=False, why=f"no file in the tree defines `{c['name']}` (citation names no path)")
        return c.update(ok=True, why=f"defined in {where[0]}" + (f" (+{len(where) - 1} more)" if len(where) > 1 else "")
                        + " — resolved by definition search; the citation names no path")
    rel, amb = tree.resolve(c["path"])
    if not rel:
        return c.update(ok=False, why=f"file {c['path']} not in the tree at this sha")
    c["resolved"] = rel
    body = "\n".join(tree.lines(rel))
    if not rel.endswith(".py"):
        # `Knowledge.tsx::GapQueue.submit` is the same citation shape over a non-Python file. It
        # claims a symbol, not a pytest node, so `def name(` is the wrong question and asking it
        # produced a confident false unresolved on gap-answer-portal-guard. Ask whether the file
        # names the symbol at all — the strongest claim this shape actually makes off-Python.
        got = re.search(r"\b" + re.escape(c["name"]) + r"\b", body)
        return c.update(ok=bool(got), why=f"{rel} {'names' if got else 'does not name'} `{c['name']}` (non-Python file: symbol check, not a pytest node)")
    if any(c["name"] in m for m in RE_PYSYM.findall(body)):
        return c.update(ok=True, why=f"{rel} defines {c['name']}")
    other = tree.defs().get(c["name"], [])
    return c.update(ok=False, why=f"{rel} defines no `{c['name']}`"
                    + (f" — it is defined in {other[0]} instead" if other else " — nothing in the tree defines it"))


# --- diagnostics for a zero -------------------------------------------------------------------
# 2026-09-06 (BOSS): B.010.ci-collection-floor's gate row said EXAMINED NOTHING on a 6992-byte
# report. Saying "zero is not a clean result" was right and was as far as it went; the reader still
# had to open the report to find out WHY. The report cites `.github/workflows/ci.yml` and
# `platform/tests/collection_baseline.json` — real citations, with no `:line`, which is not one of
# the four shapes. And its item declares artifact `010.ci`, which matches no ARTIFACT block, so the
# ledger half read nothing and nothing said so. A zero must name its own cause.
RE_BARE_PATH = re.compile(rf"(?<![\w:])((?:[\w.-]+/)*[\w.-]+\.(?:{CODE_EXT}))(?![\w:])")
# at least one a-f, or `20260906` (a date) counts as a commit sha and the diagnostic lies in a
# small way while explaining why another tool lied in a big one
RE_SHAREF = re.compile(r"(?<![\w/])((?=[0-9a-f]*[a-f])[0-9a-f]{8,40})(?![\w/])")
RE_LINEREF = re.compile(r"\b(?:line|lines|L)\s*(\d+)", re.I)


MAX_SHA_CHECKS = 25


def path_column(residual, tree):
    """Bare `path` with no `:line` — a fifth shape, counted SEPARATELY (BOSS, 2026-09-06).

    Deliberately NOT merged into the citation count: that count is comparable across every gate the
    program has run, and folding a new shape into it would move the number under a reader who is
    comparing gates. A new column instead of a bigger old one.
    """
    out, seen = [], set()
    for m in RE_BARE_PATH.finditer(residual):
        pth = m.group(1)
        if pth in seen:
            continue
        seen.add(pth)
        rel, amb = tree.resolve(pth)
        out.append({"path": pth, "ok": bool(rel), "resolved": rel, "ambiguous": amb})
    return out


def sha_column(residual, root):
    """Commit shas a report names — reachable in this checkout's repo, or not. Own column, and
    bounded: a report can name dozens and each is a git call."""
    out, seen = [], set()
    for m in RE_SHAREF.finditer(residual):
        h = m.group(1)
        if h in seen:
            continue
        seen.add(h)
        if len(out) >= MAX_SHA_CHECKS:
            break
        rc = subprocess.run(["git", "-C", root, "cat-file", "-e", h + "^{commit}"],
                            capture_output=True, timeout=30).returncode
        out.append({"sha": h, "ok": rc == 0})
    return out


def near_misses(docs):
    """Reference-shaped text the four patterns do NOT resolve. -> [(label, count, example)]."""
    out = []
    for label, rx in (("prose line reference (`line 42`)", RE_LINEREF),):
        hits = []
        for txt, _src in docs:
            hits += [m.group(1) for m in rx.finditer(txt or "")]
        if hits:
            uniq = list(dict.fromkeys(hits))
            out.append((label, len(hits), uniq[:2]))
    return out


def ledger_block(tree, artifact, ledger_root=None):
    """The item's own ARTIFACT block, by the same rule mergegate uses.

    READ FROM TRUNK, not from the swept tree (BOSS, 2026-09-06 23:0x). The sweep resolves the
    REPORT against the candidate sha, which is right — the report is the candidate's claim. The
    LEDGER is not the candidate's: plans/EXECUTION_LEDGER.md on trunk is the sole source of truth,
    and every lane's copy is a snapshot from whenever that lane branched. Reading the lane's copy
    made citesweep report "artifact 010.circleci-heredoc-escape matches no ARTIFACT block" AFTER
    BOSS had committed that very row to trunk: the row existed, the sweep was looking at a tree that
    predates it. A false "your ledger row is missing" costs more than a missed citation, because the
    obvious response is to go and write a row that is already there.

    Returns (block_text, label_or_None, source_note). The source is carried out, never assumed: a
    reader must be able to see WHICH ledger answered, or this fix is invisible the next time it
    matters.
    """
    root = ledger_root or tree.root
    src = "trunk" if ledger_root else "the swept tree"
    p = os.path.join(root, LEDGER)
    if not artifact:
        return "", None, src
    if not os.path.isfile(p):
        # Never fall back to the swept tree's copy. Falling back silently is the bug being fixed;
        # an unreadable trunk ledger is a NAMED failure, not a quieter wrong answer.
        return "", None, f"{src} — BUT {LEDGER} DOES NOT EXIST AT {root}, so the ledger half read nothing"
    blk, on = [], False
    for ln in open(p, errors="ignore"):
        if ln.startswith("ARTIFACT:") and ln.split()[1:2] == [artifact]:
            on = True
        elif on and ln.startswith("ARTIFACT:"):
            break
        if on:
            blk.append(ln)
    return "".join(blk), (LEDGER if blk else None), src


def find_report(tree, item_id, given=None):
    if given:
        for cand in (given, os.path.join(REPORT_DIR, os.path.basename(given))):
            if os.path.isfile(os.path.join(tree.root, cand)):
                return cand
    rd = os.path.join(tree.root, REPORT_DIR)
    if not os.path.isdir(rd):
        return None
    # Not mtime (every file in a fresh detached checkout carries the checkout's mtime) and not
    # plain name order either: `-r3.md` sorts BEFORE `.md` because `-` < `.`, so a name sort hands
    # back r1 — the oldest revision — while looking entirely reasonable. Rank by the -rN suffix.
    def rev(f):
        m = re.search(r"-r(\d+)\.md$", f)
        return (int(m.group(1)) if m else 0, f)

    hits = sorted((f for f in os.listdir(rd) if f.startswith(item_id + "-") and f.endswith(".md")), key=rev)
    return os.path.join(REPORT_DIR, hits[-1]) if hits else None


def sweep(item_id, root, report=None, artifact=None, sha=None, ledger_root=None):
    tree = Tree(root)
    opened, docs = [], []
    rep = find_report(tree, item_id, report)
    if rep:
        txt = open(os.path.join(tree.root, rep), errors="ignore").read()
        opened.append((rep, len(txt)))
        docs.append((txt, "report"))
    lb, lp, lsrc = ledger_block(tree, artifact, ledger_root)
    if lp:
        opened.append((f"{lp} (ARTIFACT: {artifact} block, read from {lsrc})", len(lb)))
        docs.append((lb, "ledger"))
    cites, residual = [], []
    for txt, src in docs:
        cites += extract(txt, src, residual)
    resid = "\n".join(residual)
    paths = path_column(resid, tree)
    shas = sha_column(resid, tree.root)
    for c in cites:
        check(c, tree)
    return dict(item=item_id, root=tree.root, sha=sha, report=rep, artifact=artifact,
                n_files=tree.n_files, opened=opened, cites=cites,
                found=len(cites), unresolved=[c for c in cites if not c.get("ok")],
                # separate columns — never folded into `found` (BOSS, 2026-09-06)
                paths=paths, shas=shas,
                # named causes for a zero, computed always so the file carries them either way
                ledger_found=bool(lp), ledger_source=lsrc,
                near=near_misses(docs) if not cites else [])


def columns(res):
    """The two separate columns, as row text. Empty when a document names none."""
    out = ""
    p = res.get("paths") or []
    if p:
        miss = [c["path"] for c in p if not c["ok"]]
        out += (f"; paths: {len(p) - len(miss)} exist / {len(miss)} missing at candidate sha"
                + (f" ({', '.join(miss[:3])})" if miss else ""))
    h = res.get("shas") or []
    if h:
        bad = [c["sha"] for c in h if not c["ok"]]
        out += (f"; shas: {len(h) - len(bad)} reachable / {len(bad)} not"
                + (f" ({', '.join(bad[:3])})" if bad else ""))
    return out


def row(res):
    """The gate row. A WARNING row: it never contributes to PASS/FAIL."""
    if res["found"] == 0:
        why = "no report or ledger row found" if not res["opened"] else \
              f"opened {len(res['opened'])} document(s) and matched no citation shape"
        bits = []
        if res.get("artifact") and not res.get("ledger_found", True):
            bits.append(f"the ledger half read NOTHING: artifact {res['artifact']!r} matches no "
                        f"ARTIFACT block in {LEDGER} as read from "
                        f"{res.get('ledger_source', 'the swept tree')}")
        for label, n, egs in (res.get("near") or [])[:3]:
            bits.append(f"{n} × {label} (e.g. {', '.join(egs)}) — a real citation this sweep "
                        f"does not resolve")
        tail = ("; " + "; ".join(bits)) if bits else ""
        return (f"citations: 0 checked, 0 unresolved — EXAMINED NOTHING of the four resolvable "
                f"shapes ({why}{tail}){columns(res)} — treat as a TOOL DEFECT, not a clean report")
    r = f"citations: {res['found']} checked, {len(res['unresolved'])} unresolved"
    r += (" (WARNING — report defect, not a gate failure)" if res["unresolved"] else "")
    return r + columns(res)


def render(res):
    L = [f"# CITATION SWEEP {res['item']} — {now()}", "", "## PROVENANCE", ""]
    L += [f"- sha: `{res['sha'] or 'UNSPECIFIED'}`",
          f"- root: `{res['root']}` ({res['n_files']} files walked)",
          f"- report: `{res['report'] or 'NONE FOUND'}`",
          f"- ledger artifact: `{res['artifact'] or 'NONE GIVEN'}`", "",
          "Files opened:" if res["opened"] else "**Files opened: NONE.**"]
    for p, n in res["opened"]:
        L.append(f"- `{p}` — {n} bytes")
    if res["found"] == 0:
        L += ["", "## WHY ZERO — named causes, not a shrug", ""]
        if res.get("artifact") and not res.get("ledger_found", True):
            L.append(f"- **The ledger half read nothing.** Artifact `{res['artifact']}` matches no "
                     f"`ARTIFACT:` block in `{LEDGER}` as read from "
                     f"**{res.get('ledger_source', 'the swept tree')}**, so only the report was "
                     f"swept. Either the item's `artifact` is wrong or the ledger row is not there "
                     f"yet. (The ledger is read from TRUNK, not from the candidate: a lane's copy "
                     f"predates any row committed after it branched, and reporting THAT as a "
                     f"missing row sends the reader to write one that already exists.)")
        for label, n, egs in (res.get("near") or []):
            L.append(f"- **{n} × {label}** — e.g. " + ", ".join(f"`{e}`" for e in egs) +
                     ". These are real references; the four shapes above do not cover them, so "
                     "they were neither resolved nor counted.")
        if not res.get("near") and res.get("ledger_found", True):
            L.append("- Nothing reference-shaped was found in what was opened either. Check the "
                     "root and the report path before believing the report is citation-free.")
    if res.get("paths") or res.get("shas"):
        L += ["", "## SEPARATE COLUMNS (never folded into the citation count)", ""]
        for c in res.get("paths") or []:
            L.append(f"- path `{c['path']}` — " + (f"exists as `{c['resolved']}`"
                     + (" (ambiguous basename)" if c.get("ambiguous") else "") if c["ok"]
                     else "**NOT FOUND at the candidate sha**"))
        for c in res.get("shas") or []:
            L.append(f"- sha `{c['sha']}` — " + ("reachable in this checkout" if c["ok"]
                     else "**not a reachable commit here**"))
    kinds = {}
    for c in res["cites"]:
        k = kinds.setdefault(c["kind"], [0, 0])
        k[0] += 1
        k[1] += 0 if c.get("ok") else 1
    L += ["", "## RESULT", "",
          f"- citations found: **{res['found']}**",
          f"- resolved: **{res['found'] - len(res['unresolved'])}**",
          f"- unresolved: **{len(res['unresolved'])}**", ""]
    if res["found"] == 0:
        L += ["> **EXAMINED NOTHING.** Zero citations found is not a clean result — every report on this",
              "> program cites migrations and tests. Read this as a defect in the sweep (wrong root, no",
              "> report at this sha, or a citation shape the patterns do not cover) until proven otherwise.", ""]
    else:
        L += ["| shape | checked | unresolved |", "|---|---|---|"]
        L += [f"| `{k}` | {v[0]} | {v[1]} |" for k, v in sorted(kinds.items())]
        L.append("")
    if res["unresolved"]:
        L += ["## UNRESOLVED", ""]
        for c in res["unresolved"]:
            L += [f"### `{c.get('path') or c.get('num')}` — {c['kind']}",
                  f"- **why:** {c['why']}",
                  f"- source: {c['source']} line {c['line']}",
                  f"- quoted: `{c['text']}`", ""]
    L += ["## GATE ROW", "", "```", row(res), "```", ""]
    return "\n".join(L)


# --- calibration -------------------------------------------------------------------------------
CAL_ROOT = os.path.join(CN, "voicepod-plan010-rebuild")
CAL_ITEM = "B.010.erasure-gap-historical-repair"
CAL_ART = "010.erasure-gap-historical-repair"
CAL_REPORT = os.path.join(REPORT_DIR, f"{CAL_ITEM}-{CAL_ART}-r2.md")
# The one citation in the must-pass corpus that SHOULD be unresolved. This r2 is a renumber-only
# rework whose §4 prints a renumber map (`historical -> current`), so it names the superseded
# filename on purpose. The sweep is right to flag it and BOSS is right to shrug at it. Named
# literally, not pattern-matched: an allowance that could absorb a second finding is not an
# allowance, it is a hole. A must-pass residual outside this list fails the selftest.
CAL_KNOWN_HISTORICAL = {"246_recording_source_historical_erasure_repair.sql"}


def selftest():
    """Both directions, per BOSS 16:15: a known-good corpus must pass clean, and the SAME corpus
    with exactly one citation perturbed must bite. One direction alone proves nothing — a tool that
    resolves everything and a tool that resolves nothing both look green on a must-pass alone."""
    import shutil
    import tempfile
    ok = True
    a = sweep(CAL_ITEM, CAL_ROOT, report=CAL_REPORT, artifact=CAL_ART, sha="trunk")
    print(f"MUST-PASS  {CAL_REPORT}")
    print(f"  {row(a)}")
    for c in a["unresolved"]:
        print(f"  UNRESOLVED {c['kind']} {c.get('path') or c.get('num')}: {c['why']}")
    unexpected = [c for c in a["unresolved"] if (c.get("path") or "") not in CAL_KNOWN_HISTORICAL]
    passed = a["found"] - len(unexpected)
    print(f"  -> must-pass {passed}/{a['found']} "
          f"({len(a['unresolved']) - len(unexpected)} known-historical allowed: {sorted(CAL_KNOWN_HISTORICAL)})")
    ok &= a["found"] > 0 and not unexpected

    # One bite per SHAPE. A single migration-shaped perturbation would leave the other three
    # checkers outside the proof set, where a checker that never fires and no checker at all look
    # exactly alike. `find` must appear in the untouched report (asserted, not assumed — a
    # perturbation of absent text proves nothing) and `repl` must be unresolvable at this sha.
    BITES = [
        ("NNN_name.sql", "247_recording_source_historical_erasure_repair.sql",
                         "947_recording_source_historical_erasure_repair.sql"),
        ("NNN:LINE", None, None),  # appended, see below — this corpus cites no NNN:LINE
        ("path:line", None, None),  # planted too: see PLANTS — a report path must be one the tmp corpus holds
        ("path::test", "::test_count_function_checks_session_and_exact_subject",
                       "::test_count_function_checks_session_and_exact_subject_XX"),
    ]
    bites = 0
    for kind, find, repl in BITES:
        tmp = tempfile.mkdtemp(prefix="citesweep-cal-")
        try:
            for rel in (CAL_REPORT, LEDGER):
                dst = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy(os.path.join(CAL_ROOT, rel), dst)
            for md in MIG_DIRS:
                src = os.path.join(CAL_ROOT, md)
                if os.path.isdir(src):
                    shutil.copytree(src, os.path.join(tmp, md))
            shutil.copytree(os.path.join(CAL_ROOT, "platform", "tests"),
                            os.path.join(tmp, "platform", "tests"))
            p = os.path.join(tmp, CAL_REPORT)
            t = open(p).read()
            base = {(c["kind"], c["line"], c["why"]) for c in
                    sweep(CAL_ITEM, tmp, report=CAL_REPORT, artifact=CAL_ART)["unresolved"]}
            if find is None:
                # This corpus names no `NNN:LINE` and no in-corpus `path:line`, so those shapes are
                # planted before they are perturbed — otherwise the strongest thing the suite could
                # say about either checker is that it was never asked a question. Each plant is a
                # PAIR: a live citation and a past-EOF one over the SAME file, so a checker that
                # flags everything fails the must-pass half and a checker that flags nothing fails
                # here. Biting because a file is missing is not evidence the length check works.
                t += ("\n\nCALIBRATION: 247:12 and tests/test_erasure_dispositions.py:5 are live; "
                      "247:999999 and tests/test_erasure_dispositions.py:999999 are past end of file.\n")
                what = ("planted live + past-EOF pairs over "
                        + ("migration 247" if kind == "NNN:LINE" else "tests/test_erasure_dispositions.py"))
            else:
                assert find in t, f"{kind} control text absent — perturbation would prove nothing"
                t = t.replace(find, repl, 1)
                what = f"`{find}` -> `{repl}`"
            open(p, "w").write(t)
            b = sweep(CAL_ITEM, tmp, report=CAL_REPORT, artifact=CAL_ART, sha="perturbed")
            new = [c for c in b["unresolved"] if (c["kind"], c["line"], c["why"]) not in base]
            hit = [c for c in new if c["kind"] == kind]
            print(f"MUST-BITE  [{kind}] {what}")
            for c in hit:
                print(f"  BIT: {c['why']}")
            if not hit:
                print(f"  NOT BITTEN — {kind} checker did not fire (new unresolved: {[c['kind'] for c in new]})")
            print(f"  -> must-bite {min(len(hit), 1)}/1")
            bites += 1 if hit else 0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nmust-bite total {bites}/{len(BITES)} shapes")
    ok &= bites == len(BITES)
    print("\nSELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("item")
    ap.add_argument("--root", default=None, help="checkout to resolve against (the gate's scratch worktree)")
    ap.add_argument("--report", default=None)
    ap.add_argument("--artifact", default=None)
    ap.add_argument("--sha", default=None)
    ap.add_argument("--ledger-root", default=None,
                    help="tree to read plans/EXECUTION_LEDGER.md from — TRUNK, not the candidate")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    root = a.root or CAL_ROOT
    if not a.sha:
        rc = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True)
        a.sha = rc.stdout.strip() or None
    res = sweep(a.item, root, a.report, a.artifact, a.sha, a.ledger_root)
    out = a.out or os.path.join(GATES, f"{a.item}.cites.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w").write(render(res))
    print(row(res))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
