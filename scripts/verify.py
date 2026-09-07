#!/usr/bin/env python
"""
Run the whole verification gate in one command.

    uv run python scripts/verify.py

BUILD.md ends every task with a verification command. Chaining those with `&&`
is a parser error in Windows PowerShell 5.1 -- the shell this project is
developed in -- and `make lint && make test` fails for exactly the same reason,
so installing GNU Make would not have fixed it. One script, one token, no
separators.

Unlike `&&`, this does not stop at the first failure. Three broken checks
should cost one run, not three.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence

SOURCE_ROOTS = ["backend", "eval", "scripts"]

# `python -m <tool>` rather than a bare `ruff`/`mypy`/`pytest`, so this works
# whether or not the virtualenv happens to be activated on PATH.
CHECKS: list[tuple[str, list[str]]] = [
    ("lint", [sys.executable, "-m", "ruff", "check", *SOURCE_ROOTS]),
    ("format", [sys.executable, "-m", "ruff", "format", "--check", *SOURCE_ROOTS]),
    ("typecheck", [sys.executable, "-m", "mypy"]),
    # Deselect the end-to-end test that shells back into this script, or the
    # gate recurses into itself once per run.
    ("test", [sys.executable, "-m", "pytest", "-q", "-m", "not slow"]),
]

Runner = Callable[[list[str]], int]


def _run(cmd: list[str]) -> int:
    return subprocess.run(cmd).returncode


def run_checks(
    run: Runner = _run,
    checks: Sequence[tuple[str, list[str]]] = tuple(CHECKS),
) -> int:
    """Run every check. Returns 0 only if all of them passed."""
    failed: list[str] = []

    for name, cmd in checks:
        print(f"\n=== {name} ===", flush=True)
        if run(cmd) != 0:
            failed.append(name)

    print()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        print(f"passed {len(checks) - len(failed)}/{len(checks)} checks")
        return 1

    print(f"OK: all {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_checks())
