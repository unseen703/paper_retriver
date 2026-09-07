# Progress tracker

From `docs/BUILD.md` Appendix C. Tick a release only when its gate is
observably true — not when its tasks are merely written.

| Release | Gate | Done |
|---|---|:--:|
| R0 | Fetch twice → zero network calls on the second; `cs.CL` for BERT, `cs.CV` for ResNet | ✅ |
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
| R0.1 — Repo scaffold | S | ✅ | 67 tests |
| R0.2 — Config loader | S | ✅ | `settings.db_path` + `filters.config_version` print |
| — verify gate | S | ✅ | `uv run python scripts/verify.py` |
| R0.3 — Domain models | S | ✅ | 34 tests, construction + immutability |
| R0.4 — Alembic baseline | M | ✅ | 32 tests; `downgrade base` → `upgrade head` clean |
| R0.5 — Token-bucket limiter | S | ✅ | 5 acquisitions @10/s in 0.4–0.6s |
| R0.6 — Response cache | S | ✅ | put/get round-trip; key stable across dict order |
| R0.7 — S2 client | L | ✅ | 34 tests offline; `cli fetch` prints normalized metadata |
| **R0.8 ◆ CHECKPOINT** | S | ✅ | run 1 `api_calls=2`; run 2 `api_calls=0, cache_hits=2` |
| R0.9 — CachedOnly + fixtures | M | ✅ | 16 tests, network disabled; 612KB fixture committed |
| R0.10 — arXiv bulk loader | L | ✅ | 3,156,710 rows, 0 unparseable; watermark 2026-09-04 |
| **R0.11 ◆ CHECKPOINT** | S | ✅ | BERT→cs.CL, ResNet→cs.CV, BatchNorm→cs.LG |
| R0.12 — Structured logging | S | ✅ | `grep s2_request data/app.log` shows JSON lines |
| R1.1 — repo/papers.py, repo/edges.py | M | next | — |

**R0 complete.** Suite at 327; full gate green.

## Prerequisites

| # | Item | Status |
|---|---|---|
| P1 | Python 3.11+, Node 20+, git | ✅ Python 3.14.7, Node 24.20.0, git 2.55.0 |
| P2 | `uv` installed | ✅ uv 0.12.5 |
| P3 | Semantic Scholar API key | ✅ present in `.env` as `S2_API_KEY` |
| P4 | Read current S2 API docs → `docs/s2-api-notes.md` | ✅ written (from `legacy/` + BUILD.md; not re-verified live — see the ⚠️ list in that file) |
| P5 | arXiv metadata source chosen | ✅ **both** — Kaggle snapshot for bulk, OAI-PMH for the delta |
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
