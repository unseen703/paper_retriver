"""
The one-command verification gate.

BUILD.md ends every task with a verification command, and chaining those with
`&&` is a parser error in Windows PowerShell 5.1 -- which is the shell this
project is actually developed in. `scripts/verify.py` exists so there is one
token to run, on any shell, with no separators to get wrong.

It deliberately does NOT stop at the first failure the way `&&` would: a broken
run should report every failing check at once, not make you re-run three times
to discover three problems.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from verify import CHECKS, run_checks

ROOT = Path(__file__).resolve().parents[3]


class RecordingRunner:
    """Stands in for subprocess.run; returns a scripted exit code per check."""

    def __init__(self, codes: dict[str, int] | None = None) -> None:
        self.codes = codes or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> int:
        self.calls.append(cmd)
        for name, code in self.codes.items():
            if any(name in part for part in cmd):
                return code
        return 0


# --------------------------------------------------------------------------
# What the gate covers
# --------------------------------------------------------------------------


def test_gate_covers_lint_format_typecheck_and_test() -> None:
    assert [name for name, _ in CHECKS] == ["lint", "format", "typecheck", "test"]


def test_every_check_runs_through_the_current_interpreter() -> None:
    """`python -m <tool>` works regardless of whether the venv is activated."""
    for _, cmd in CHECKS:
        assert cmd[0] == sys.executable
        assert cmd[1] == "-m"


def test_lint_and_format_cover_all_source_roots() -> None:
    by_name = dict(CHECKS)
    for name in ("lint", "format"):
        for root in ("backend", "eval", "scripts"):
            assert root in by_name[name], f"{name} does not cover {root}/"


def test_format_check_does_not_rewrite_files() -> None:
    """The gate must report drift, never silently edit the tree."""
    assert "--check" in dict(CHECKS)["format"]


# --------------------------------------------------------------------------
# Exit-code contract
# --------------------------------------------------------------------------


def test_returns_zero_when_everything_passes() -> None:
    runner = RecordingRunner()
    assert run_checks(runner) == 0
    assert len(runner.calls) == len(CHECKS)


def test_returns_nonzero_when_a_check_fails() -> None:
    assert run_checks(RecordingRunner({"mypy": 1})) != 0


@pytest.mark.parametrize("failing", ["ruff", "mypy", "pytest"])
def test_any_single_failure_fails_the_gate(failing: str) -> None:
    assert run_checks(RecordingRunner({failing: 1})) != 0


def test_all_checks_run_even_after_one_fails() -> None:
    """`&&` would stop here; running on surfaces every problem in one pass."""
    runner = RecordingRunner({"ruff": 1})
    run_checks(runner)
    assert len(runner.calls) == len(CHECKS)


def test_failure_summary_names_every_failing_check(capsys: pytest.CaptureFixture[str]) -> None:
    run_checks(RecordingRunner({"mypy": 1, "pytest": 1}))
    out = capsys.readouterr().out
    assert "typecheck" in out
    assert "test" in out


# --------------------------------------------------------------------------
# Reachable from the Makefile, and actually green right now
# --------------------------------------------------------------------------


def test_makefile_exposes_a_verify_target() -> None:
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    assert any(
        line.split(":")[0].strip() == "verify"
        for line in lines
        if ":" in line and not line.startswith("\t")
    ), "Makefile has no `verify` target"


def test_verify_script_is_discoverable_where_the_makefile_expects_it() -> None:
    assert (ROOT / "scripts" / "verify.py").is_file()


@pytest.mark.slow
def test_gate_passes_on_the_current_tree() -> None:
    """End-to-end. Deselected inside the gate's own run to avoid recursion."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
