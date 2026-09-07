# Progress tracker

From `docs/BUILD.md` Appendix C. Tick a release only when its gate is
observably true — not when its tasks are merely written.

| Release | Gate | Done |
|---|---|:--:|
| R0 | Fetch twice → zero network calls on the second; `cs.CL` for BERT, `cs.CV` for ResNet | ☐ |
| R1 | Browser: "BERT" → Expand ×2 → ~60 post-2015 core-ML nodes in <90s | ☐ |
| — | **Use it for a week. Keep an annoyance list.** | ☐ |
| R2 | 20 min of curation: layout stable, no removed paper returns, every rejection reversible | ☐ |
| R3 | One YAML weight change instantly reorders the candidate list; breakdown explains any rank | ☐ |
| R4 | `make eval` beats all six baselines (or documents honestly where it doesn't), with CIs | ☐ |
| R5 | Liking 5 papers measurably raises Recall@20 — with the delta reported | ☐ |
| R6 | Non-citation channel recovers papers the graph cannot reach — with the count reported | ☐ |
| — | **Stop unless R4 shows a specific gap R7/R8 would close.** | ☐ |

## Task log

| Task | Size | Status | Verified by |
|---|---|---|---|
| R0.1 — Repo scaffold | S | ✅ done | full gate — 67 passed |
| R0.2 — Config loader | S | ✅ done | `uv run python -c "from app.config import settings, filters; print(settings.db_path, filters.config_version)"` → `data/app.db 620fa7f96045`; full gate — 98 passed |
| —    — verify gate    | S | ✅ done | `uv run python scripts/verify.py` — OK: all 4 checks passed; 112 tests |
| R0.3 — Domain models | S | next | — |

## Prerequisites

| # | Item | Status |
|---|---|---|
| P1 | Python 3.11+, Node 20+, git | ✅ Python 3.14.7, Node 24.20.0, git 2.55.0 |
| P2 | `uv` installed | ✅ uv 0.12.5 |
| P3 | Semantic Scholar API key | ✅ present in `.env` as `S2_API_KEY` |
| P4 | Read current S2 API docs → `docs/s2-api-notes.md` | ☐ **open — do before R0.7** |
| P5 | arXiv metadata source chosen | ✅ Kaggle Cornell arXiv dataset |
| P6 | Empty GitHub repo, CI enabled | ◐ `git init` done locally; no remote, not pushed |

## Deviations from BUILD.md as written

1. **`docs/BUILD.md` and `docs/PLAN.md` were at the repo root.** `CLAUDE.md`
   references them under `docs/`, so they were moved there at R0.1. No content
   changed.
2. **`make` is not installed on this machine** (Windows, no GNU Make), and
   `make lint && make test` would fail anyway -- `&&` is a parser error in
   PowerShell 5.1. `scripts/verify.py` is the fix: one command, no
   separators, runs lint + format + typecheck + test and reports *every*
   failure rather than stopping at the first. `make verify` and CI both
   call it, so the gate cannot drift between local and CI.

   ```
   uv run python scripts/verify.py
   ```
3. **`app` is the import root, not `backend.app`.** R0.2's verify command is
   `from app.config import settings, filters`, so `pyproject.toml` installs
   `backend/app` as the `app` package (hatchling) and pytest adds `backend`
   to `pythonpath`. The R0.1 Makefile `dev` target said `backend.app.main`
   and was corrected to `app.main --app-dir backend`.
4. **`applied_keyword_action` is a model field, not a YAML key.** Appendix A
   states the rule as a comment ("keyword hits QUARANTINE only, never
   auto-reject"). It is encoded as a `Literal["quarantine"]` field so R1.5
   can assert against it. `filters.yaml` stays verbatim.
5. **`types-PyYAML` added to dev deps.** Not in R0.1's list, but `services/`
   is under strict mypy and loads YAML; without stubs R1.4 would fail
   typecheck for an unrelated reason.
6. **`addopts = "-p no:dash"`** was added to the pytest config. An ambient
   `dash` install registers a pytest plugin that imports `pydantic` during
   collection and aborts the entire run when it is absent. Not in BUILD.md;
   without it `make test` cannot start outside the uv venv.
