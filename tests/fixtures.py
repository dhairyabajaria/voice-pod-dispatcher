"""Shared test fixtures. Chiefly: keep a test's hands off the LIVE dispatcher state.

WHY THIS EXISTS (BOSS item 9, 2026-09-10 22:32). `dispatcher.py` resolves its paths from module
constants — `STATE_DIR` and twenty-two children of it. A test that loads the module and drives it
reads and WRITES those real paths unless it reassigns each one. Redirecting them individually is how
this keeps going wrong:

  * 2026-09-10 21:58 — `test_serverdown.py` never redirected `HOLD_DIR`, and `codex_tick` reads
    `HOLD_DIR/<slot>.feed` to decide whether a slot is parked. BOSS created the real
    `hold/CODEX-1.feed` at 21:52 while working around an unrelated item, and five checks went red
    for a reason that had nothing to do with the daemon. **The test's verdict depended on what the
    operator happened to be doing.**
  * The sweep that followed (item 8) found **12 of 35** files leaving at least one DELETABLE live
    path unredirected while driving the daemon. The daemon removes files under `ANSWER_REQ_DIR`,
    `CLEAR_DIR`, `GATE_REQ_DIR`, `HOLDCLEAR_DIR` and `HOLD_DIR` — a test reaching those destroys
    operator state. Nothing was destroyed only because those directories happened to be empty.
  * Items 6 and 7 made it sharper rather than milder: `apply_answer_requests` and
    `apply_gate_requests` now run on the DEGRADED path, which is the path every one of those tests
    takes, so deletions that used to be unreachable became reachable.

So the fix is not twelve patches. Twelve patches is how the thirteenth gets missed — the same
"one file instead of its population" shape found in the pgserver census and the route resolver the
same evening. `redirect_state` rebases THE WHOLE SET, and `test_fixtureguard.py` fails any module
that loads `dispatcher.py` without calling it, so the thirty-sixth file cannot skip it either.
"""
from __future__ import annotations

import os
import tempfile

#: Constants that are directories the daemon lists or writes into. Created eagerly, because a
#: `listdir` of a missing directory is an OSError that several call sites swallow — and a swallowed
#: error looks exactly like an empty directory, which is the failure this module exists to prevent.
_DIR_SUFFIXES = ("_DIR",)


def redirect_state(mod, root: str | None = None, *, keep: tuple[str, ...] = ()) -> str:
    """Point every live path constant on `mod` inside a private temp root. -> that root.

    Call it IMMEDIATELY after `spec.loader.exec_module(mod)` and before any per-test override:
    assignments a test makes afterwards still win, so this is a floor, not a ceiling.

    Takes only the module on purpose. An earlier design took the caller's `TMP` as well, which meant
    every call site had to name its own temp variable — thirty-five different names, and the
    insertion could not be made uniform or checked mechanically.

    A constant already inside `root` is left alone, so calling this twice is harmless.

    `keep` names constants to LEAVE pointing at the real path, for the rare check whose SUBJECT is
    the default resolution itself (`test_restartdrop.py` asserts that with no override the daemon
    reads the real `roster.json`). It is a parameter rather than an edit to the module afterwards so
    that the opt-out is greppable: `grep -rn "keep=" tests/` enumerates every deliberate live read
    in the suite, and anything not on that list is an accident. Only ever use it for a path the
    daemon READS; never for one it writes or deletes from.
    """
    root = root or tempfile.mkdtemp(prefix="dsp-state-")
    live = os.path.join(root, "live")
    os.makedirs(live, exist_ok=True)
    home = os.path.expanduser("~")
    for name in dir(mod):
        if not name.isupper():
            continue
        value = getattr(mod, name)
        # Only strings that look like real filesystem paths OUTSIDE the temp root. A constant that
        # is a number, a regex or a URL is not a path and must not be rewritten into one.
        if name in keep:
            continue
        if not isinstance(value, str) or not value.startswith(home) or value.startswith(root):
            continue
        target = os.path.join(live, name.lower())
        setattr(mod, name, target)
        if name.endswith(_DIR_SUFFIXES) or os.path.isdir(value):
            os.makedirs(target, exist_ok=True)
    return root


def live_paths(mod) -> list[str]:
    """Every constant on `mod` that still points at a real path under $HOME. -> names.

    The instrument for the guard: a non-empty list means this module can still reach live state.
    """
    home = os.path.expanduser("~")
    out = []
    for name in dir(mod):
        if not name.isupper():
            continue
        v = getattr(mod, name)
        if isinstance(v, str) and v.startswith(home) and "/T/" not in v and "/tmp" not in v:
            out.append(name)
    return sorted(out)
