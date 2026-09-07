"""
R0.1 -- repo scaffold.

These assert the *shape* the rest of BUILD.md is written against: the
directory tree from PLAN.md SS K, the dependency set and tool config from
R0.1, and the Makefile targets that every later task's verification command
invokes. A later task that cannot find `app/services/filters/` or cannot run
`make test` has failed here, not there.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------
# Directory tree (BUILD.md R0.1 mkdir line, PLAN.md SS K)
# --------------------------------------------------------------------------

SCAFFOLD_DIRS = [
    "backend/app/api",
    "backend/app/schemas",
    "backend/app/services/filters",
    "backend/app/clients",
    "backend/app/repo",
    "backend/app/models",
    "backend/migrations",
    "backend/tests/unit",
    "backend/tests/integration",
    "backend/tests/fixtures",
    "frontend",
    "eval/benchmarks",
    "eval/results",
    "config",
    "data",
    "scripts",
    "docs/adr",
    "docs/media",
]


@pytest.mark.parametrize("rel", SCAFFOLD_DIRS)
def test_scaffold_directory_exists(rel: str) -> None:
    assert (ROOT / rel).is_dir(), f"missing scaffold directory: {rel}"


PACKAGE_DIRS = [
    "backend/app",
    "backend/app/api",
    "backend/app/schemas",
    "backend/app/services",
    "backend/app/services/filters",
    "backend/app/clients",
    "backend/app/repo",
    "backend/app/models",
]


@pytest.mark.parametrize("rel", PACKAGE_DIRS)
def test_python_package_is_importable(rel: str) -> None:
    """Every backend/app subpackage needs __init__.py or imports break at R1."""
    assert (ROOT / rel / "__init__.py").is_file(), f"{rel} is not a package"


# --------------------------------------------------------------------------
# pyproject.toml
# --------------------------------------------------------------------------

RUNTIME_DEPS = [
    "fastapi",
    "uvicorn",
    "httpx",
    "tenacity",
    "pydantic",
    "pydantic-settings",
    "sqlalchemy",
    "alembic",
    "networkx",
    "pyyaml",
    "structlog",
    "typer",
]
DEV_DEPS = ["pytest", "pytest-asyncio", "ruff", "mypy"]


@pytest.fixture(scope="module")
def pyproject() -> dict:
    path = ROOT / "pyproject.toml"
    assert path.is_file(), "pyproject.toml is missing"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _names(specs: list[str]) -> set[str]:
    out = set()
    for spec in specs:
        name = spec.split(";")[0]
        for sep in (">=", "<=", "==", "~=", ">", "<", "["):
            name = name.split(sep)[0]
        out.add(name.strip().lower())
    return out


@pytest.mark.parametrize("dep", RUNTIME_DEPS)
def test_runtime_dependency_declared(pyproject: dict, dep: str) -> None:
    assert dep in _names(pyproject["project"]["dependencies"])


@pytest.mark.parametrize("dep", DEV_DEPS)
def test_dev_dependency_declared(pyproject: dict, dep: str) -> None:
    groups = pyproject.get("dependency-groups", {})
    optional = pyproject["project"].get("optional-dependencies", {})
    declared: set[str] = set()
    for bundle in (*groups.values(), *optional.values()):
        declared |= _names(bundle)
    assert dep in declared


def test_ruff_line_length_is_100(pyproject: dict) -> None:
    assert pyproject["tool"]["ruff"]["line-length"] == 100


def test_mypy_is_strict_over_services_and_models(pyproject: dict) -> None:
    mypy = pyproject["tool"]["mypy"]
    assert mypy["strict"] is True
    files = " ".join(mypy["files"]) if isinstance(mypy["files"], list) else mypy["files"]
    assert "backend/app/services" in files
    assert "backend/app/models" in files


def test_pytest_asyncio_mode_is_auto(pyproject: dict) -> None:
    assert pyproject["tool"]["pytest"]["ini_options"]["asyncio_mode"] == "auto"


def test_pytest_collects_from_backend_tests(pyproject: dict) -> None:
    """Without testpaths, `make test` collects legacy/ and dies on missing deps."""
    testpaths = pyproject["tool"]["pytest"]["ini_options"]["testpaths"]
    assert "backend/tests" in (testpaths if isinstance(testpaths, list) else [testpaths])


def test_requires_python_is_311_or_newer(pyproject: dict) -> None:
    assert pyproject["project"]["requires-python"].startswith(">=3.1")


# --------------------------------------------------------------------------
# Makefile -- every later BUILD.md task verifies through one of these
# --------------------------------------------------------------------------

MAKE_TARGETS = [
    "install",
    "dev",
    "test",
    "test-unit",
    "lint",
    "typecheck",
    "migrate",
    "eval",
    "load-arxiv",
]


@pytest.fixture(scope="module")
def makefile() -> str:
    path = ROOT / "Makefile"
    assert path.is_file(), "Makefile is missing"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("target", MAKE_TARGETS)
def test_make_target_defined(makefile: str, target: str) -> None:
    assert any(
        line.split(":")[0].strip() == target
        for line in makefile.splitlines()
        if ":" in line and not line.startswith("\t")
    ), f"Makefile has no `{target}` target"


# --------------------------------------------------------------------------
# .env.example and .gitignore
# --------------------------------------------------------------------------

ENV_KEYS = ["S2_API_KEY", "DB_PATH", "S2_RATE_LIMIT", "LOG_LEVEL"]


@pytest.mark.parametrize("key", ENV_KEYS)
def test_env_example_documents_key(key: str) -> None:
    path = ROOT / ".env.example"
    assert path.is_file(), ".env.example is missing"
    assert any(
        line.strip().startswith(f"{key}=") for line in path.read_text(encoding="utf-8").splitlines()
    ), f".env.example does not document {key}"


def test_env_example_holds_no_real_key() -> None:
    """The template must never carry the value from .env."""
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("S2_API_KEY="):
            assert line.split("=", 1)[1].strip() == ""


GITIGNORE_PATTERNS = ["data/*.db", "data/*.npy", ".env", "__pycache__", "node_modules"]


@pytest.mark.parametrize("pattern", GITIGNORE_PATTERNS)
def test_gitignore_covers(pattern: str) -> None:
    path = ROOT / ".gitignore"
    assert path.is_file(), ".gitignore is missing"
    lines = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()}
    assert pattern in lines, f".gitignore does not list {pattern}"


def test_secrets_are_not_tracked_by_git() -> None:
    """`.env` holds a live S2 key -- it must be ignored, not merely present."""
    import subprocess

    proc = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT, capture_output=True)
    assert proc.returncode == 0, ".env is NOT gitignored"
