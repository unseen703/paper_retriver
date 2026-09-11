# Project instructions

Citation-graph paper recommender for AI/ML research. Solo project, local-first, single user.

## Read these before writing code

- `docs/BUILD.md` — the task queue. Tasks are `R<release>.<n>`. Work one at a time.
- `docs/PLAN.md` — the architecture reference. Consult the relevant section; don't read it whole.

**`docs/BUILD.md` §"The session_id contract" is mandatory reading before writing any SQL or repo function.**

## Locked decisions — do not change these without asking me

- SQLite (WAL) + SQLAlchemy **Core** (not ORM) + Alembic. **No Neo4j, no Postgres.**
- FastAPI, single process, one background worker thread. **No Celery, no Redis.**
- NetworkX for graph algorithms, rebuilt from SQL on mutation.
- React + TypeScript + Vite + Cytoscape.js + fcose. **Not React Flow, not raw D3.**
- TanStack Query for server state, Zustand for view state. The graph lives in the Cytoscape instance, never in React state.
- **No vector database.** NumPy `.npy` + brute-force cosine.
- **No LLM/RAG stage.** Abstracts are stored for embeddings only.
- Edges are a single directed `CITES` relation, `citing → cited`. Undirected only as a computed view.
- Corpus year floor is 2015, but pre-2015 papers are still stored as **boundary papers** (row + edges, no `graph_nodes` row). Never drop them — bibliographic coupling depends on them.
- Frontend API types are generated from OpenAPI (`make types`). **Never hand-write them.**

## Rules

1. **One BUILD.md task per commit, on one branch.** Implement it, run the gate, commit it, then continue. Review with `ecc:code-review` after every third. One PR per batch — *not* a stacked PR per stage. Never stage `frontend/src` wholesale; it has twice swept my uncommitted work into an unrelated commit.
2. **Never invent schema.** The tables are in PLAN.md §C and Alembic migration `0001`. If you need a column that isn't there, say so and stop.
3. **Layer boundaries are enforced:** nothing above `repo/` writes SQL; nothing below `api/` knows about HTTP. `services/ranking.py` and `services/filters/` are **pure functions** — no I/O, no DB, no network.
4. **Session scoping:** functions in `repo/graph.py`, `repo/events.py`, `repo/expansions.py` take `session_id` as the first positional argument, no default. Functions in `repo/papers.py` and `repo/edges.py` must not accept it at all.
5. **Tests ship with the feature**, in the same turn. Use `CachedOnlyS2Client` and the committed fixture DB — tests must pass with the network disabled.
6. **All S2 Pydantic fields are Optional.** A missing field degrades a score; it never raises.
7. **Determinism:** stable sort keys everywhere (`-score, paper_id`), fixed seeds for PPR and layout. Never rely on dict or set iteration order.
8. **A config value is code, and a comment cannot change arithmetic.** The sign in `ranking.yaml` *is* the sign in the score — `hub` shipped as `0.60  # subtracted` and rewarded hubs until R3 first computed the feature. Every weight must have a feature that populates it, and every computed feature must have a weight; both failures are silent, so assert the correspondence in a test rather than leaving a comment asking people to keep them in step. `budget.score_floor` is currently parsed and read by nothing — that is the same bug in its dormant form.
9. If you think a locked decision is wrong, **say so and stop.** Don't work around it.

## Commands

**`make` is not installed on this machine** (Windows, no GNU Make), and the
Makefile's `&&` chains are a parser error in PowerShell 5.1 anyway. The targets
below still exist and CI calls them; locally, run the underlying command.

The gate — lint, format, typecheck and test in one command, reporting *every*
failure rather than stopping at the first:

Run it from the **repo root** — `scripts/verify.py` is not under `backend/`:

```bash
uv run python scripts/verify.py
```

```bash
cd frontend && npm test
```

**Never pipe `verify.py` into `head` or `tail`.** It returns 1 correctly, but a
pipeline reports the exit status of the last command, so a failing gate reads as
a pass. Use `set -o pipefail` if you must pipe it.

**`prettier` is not a project dependency.** Do not run it — it reformats hundreds
of unrelated lines, and that churn has had to be reverted twice.

| Makefile target | What it runs |
|---|---|
| `make install` · `make dev` · `make migrate` | `uv sync` · uvicorn `app.main --app-dir backend` · `alembic upgrade head` |
| `make test` · `make test-unit` · `make lint` · `make typecheck` | pytest · pytest `tests/unit` · ruff · mypy |
| `make verify` | `scripts/verify.py` — all four, and what CI runs |
| `make types` | regenerate `frontend/src/types` from OpenAPI. **Never hand-write these.** |
| `make load-arxiv` · `make eval` | arXiv bulk load · R4 benchmark (not built yet) |
