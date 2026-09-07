# TDD evidence — R0.1 Repo scaffold

**Source plan:** `docs/BUILD.md` §R0.1, structure cross-checked against `docs/PLAN.md` §K.
**Git checkpoints:** not applicable — repo was not under version control when the
cycle ran (`git init` happened *as part of* R0.1, for P6). Evidence is this file.

## User journey

> As the sole developer, I want a repo whose layout, dependency set, tool config
> and Makefile targets match BUILD.md, so that every later task's verification
> command runs without first debugging the scaffold.

## Cycle

| Stage | Command | Result |
|---|---|---|
| RED | `python -m pytest -p no:dash backend/tests/unit/test_r0_1_scaffold.py -q` | **35 failed, 30 errors, 2 passed** |
| GREEN | `uv run pytest -q` | **67 passed** |
| Lint | `uv run ruff check backend eval scripts` | All checks passed |
| Format | `uv run ruff format --check backend eval scripts` | 12 files already formatted |
| Typecheck | `uv run mypy` | Success: no issues found in 3 source files |

The RED run failed for the intended reason: no `pyproject.toml`, no `Makefile`,
no `.env.example`, no `.gitignore`, and 16 of the 18 scaffold directories absent.
The 2 incidental passes were `data/` and `frontend`-adjacent paths that already
existed.

## Test specification

| # | What is guaranteed | Test | Type | Result |
|---|---|---|---|---|
| 1 | All 18 BUILD.md/PLAN.md §K scaffold directories exist | `test_scaffold_directory_exists` | unit | PASS |
| 2 | Every `backend/app` subpackage has `__init__.py`, so R1 imports resolve | `test_python_package_is_importable` | unit | PASS |
| 3 | All 12 runtime deps from R0.1 are declared | `test_runtime_dependency_declared` | unit | PASS |
| 4 | All 4 dev deps are declared | `test_dev_dependency_declared` | unit | PASS |
| 5 | ruff line-length is 100 | `test_ruff_line_length_is_100` | unit | PASS |
| 6 | mypy is strict over `services` and `models` | `test_mypy_is_strict_over_services_and_models` | unit | PASS |
| 7 | pytest `asyncio_mode` is `auto` | `test_pytest_asyncio_mode_is_auto` | unit | PASS |
| 8 | pytest collects only `backend/tests`, never `legacy/` | `test_pytest_collects_from_backend_tests` | unit | PASS |
| 9 | All 9 Makefile targets are defined | `test_make_target_defined` | unit | PASS |
| 10 | `.env.example` documents all 4 keys and carries no real value | `test_env_example_*` | unit | PASS |
| 11 | `.gitignore` lists all 5 required patterns | `test_gitignore_covers` | unit | PASS |
| 12 | `.env` is genuinely ignored by git, not merely present | `test_secrets_are_not_tracked_by_git` | unit | PASS |

Guarantee 8 is not in BUILD.md. It was added because `testpaths` is what keeps
`make test` from collecting the archived prototype in `legacy/`, whose imports
(flask, requests, thefuzz) are no longer installed.

Guarantee 12 is likewise additional: `.env` holds a live Semantic Scholar key and
`git init` ran this task, so the ignore rule is worth asserting rather than assuming.

## Coverage

No coverage number is meaningful here — R0.1 ships zero production code by
design. Coverage gating starts once `backend/app/` has behaviour (R0.2 onward).

## Known gaps

- **`make` is not installed** (Windows, no GNU Make). The `Makefile` is correct
  and asserted by test 9, but its recipes were verified by running their bodies
  directly through `uv run`, not by invoking `make`.
- **P4 is still open**: `docs/s2-api-notes.md` has not been written. BUILD.md
  wants the live S2 API contract captured before R0.7 builds a client against it.
- **P6 is partial**: local repo initialised and `.github/workflows/ci.yml`
  written, but there is no remote and nothing has been pushed, so "CI green" is
  unproven.
