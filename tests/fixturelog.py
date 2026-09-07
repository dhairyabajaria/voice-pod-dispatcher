"""Mark output that came from a FIXTURE, so a real finding is the only unmarked one in a suite log.

BOSS, 2026-09-06, reading a green suite log: it carried three "=== UNWIRED TEST FILES ===" blocks
and several bare "FAIL" gate verdicts. Every one was a fixture — a gate deliberately driven to fail,
a census deliberately given an unwired stub — and the suite exited 0. But a reader skimming for
trouble sees the word FAIL at the start of a line and stops there, and the one time it matters will
be the time it is real. A log in which findings and props are typographically identical does not
become safe because the exit code is correct; it just moves the reading cost onto whoever skims it.

Two tools, and they are not interchangeable:
  prefixed()  captures a block that runs REAL code (mergegate's main, a subprocess) and re-emits
              every line with a `[fixture]` prefix. Use it around anything that prints a verdict.
  flat(x)     collapses a multi-line value to one line, for a check()'s detail argument. A detail
              is meant to sit at the end of a PASS/FAIL line; when it contains newlines its tail
              lands at column 0 and starts reading as output in its own right.

This file is deliberately NOT named test_*.py: it is a helper, and run_all.sh's census would
otherwise require a runner line for it.
"""
import contextlib, io, sys

PREFIX = "[fixture] "


@contextlib.contextmanager
def prefixed(label=""):
    """Run a block, then re-emit everything it printed with a [fixture] prefix.

    Output is held until the block ends. That is a deliberate trade: interleaving would need a line
    buffer per stream and this is a test log, not a live console. Output is still emitted if the
    block RAISES — a crash's own output is the most useful kind, and swallowing it to preserve a
    tidy prefix would be exactly the sort of silence these tests exist to prevent.
    """
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            yield buf
    finally:
        text = buf.getvalue()
        if text.strip():
            head = PREFIX + (f"--- {label} ---" if label else "--- output below is from a fixture, not a finding ---")
            print(head)
            for line in text.rstrip("\n").split("\n"):
                print(PREFIX + line)


def flat(value, limit=200):
    """One line, always: newlines become ⏎ so a detail can never start a line of its own."""
    s = value if isinstance(value, str) else str(value)
    s = " ⏎ ".join(part.strip() for part in s.split("\n"))
    return s[:limit] + ("…" if len(s) > limit else "")
