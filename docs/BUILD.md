# BUILD.md — Executable Build Plan

Companion to `PLAN.md`. That document holds the *why*; this one holds the *what next*.

**How to use it.** Work top to bottom. Every task has a verification command — do not proceed past a failing one. Checkpoints (◆) are hard gates: at each one, something observable works end to end. If a checkpoint fails, the bug is in the tasks since the last checkpoint, which is a small search space by design.

Tasks are `R<release>.<n>`. Sizes: **S** ≤ 1h, **M** 1–3h, **L** 3–8h.

**Locked decisions** (from PLAN.md §Resolved decisions): SQLite · FastAPI · Cytoscape.js+fcose · directed `CITES` edges · sessions kept · year floor 2015 with boundary papers · `cs.CV` denied · no LLM stage · evaluation at R4.

---

## Table of contents

- [Prerequisites](#prerequisites)
- [The session_id contract](#the-session_id-contract) ← read before writing any SQL
- [R0 — Foundation](#r0--foundation-12-tasks) · 12 tasks
- [R1 — Walking skeleton](#r1--walking-skeleton-22-tasks) · 22 tasks
- [R2 — Interaction and GC](#r2--interaction-and-gc-16-tasks) · 16 tasks
- [R3–R6 — Task checklists](#r3r6--task-checklists)
- [Appendix A — Config files](#appendix-a--config-files-verbatim)
- [Appendix B — Code skeletons](#appendix-b--code-skeletons-for-the-tricky-parts)
- [Appendix C — Progress tracker](#appendix-c--progress-tracker)

---

## Prerequisites

Do these before task R0.1.

| # | Item | How |
|---|---|---|
| P1 | Python 3.11+, Node 20+, git | — |
| P2 | `uv` installed | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| P3 | **Semantic Scholar API key** | Request at the S2 API site. Approval can take days — do this first so it isn't blocking. The plan works without one at a lower rate limit. |
| P4 | **Read the current S2 API docs** | Verify: batch endpoint max IDs, rate limits, whether `embedding.specter_v2` is served, exact nested-field syntax. My numbers are from mid-2026 and *will* be slightly wrong. Write what you find into `docs/s2-api-notes.md` — it becomes the spec your client is built against. |
| P5 | arXiv metadata source chosen | Kaggle Cornell arXiv dataset (simpler) or OAI-PMH harvest (more impressive, resumable). Either is fine. |
| P6 | Empty GitHub repo, CI enabled | — |

---

## The session_id contract

You asked to keep sessions. Getting the scoping boundary wrong here means either duplicate API calls or cross-session data leaks, so fix it once, now, and never think about it again.

**The rule:** *facts about papers are global; opinions about papers are session-scoped.*

| Table | Scope | Why |
|---|---|---|
| `papers` | **global** | A paper's title and year don't depend on which graph you're looking at. Fetch once, reuse everywhere. |
| `authors`, `paper_authors` | **global** | Same. |
| `edges` | **global** | The citation graph is a fact about the world. This is the big win: seeding session B with a paper already crawled in session A costs **zero API calls**. |
| `api_cache` | **global** | Obviously. |
| `arxiv_meta` | **global** | Reference data. |
| `sessions` | — | The registry itself. |
| `graph_nodes` | **session** | PK `(session_id, paper_id)`. A paper can be LIKED in one session and absent in another. |
| `interaction_events` | **session** | Your labels are per-workspace. |
| `expansions` | **session** | |
| `filter_decisions` | **`session_id` NULLABLE** | The subtle one. See below. |

**Why `filter_decisions.session_id` is nullable.** Filter verdicts come in two kinds:

- **Global verdicts** — `PRE_ERA`, `IS_DATASET`, `CAT_PRIMARY_APPLIED`, `FIELD_NON_CS`. A 2013 dataset paper is a 2013 dataset paper in every session forever. Write these with `session_id = NULL` and **cache the verdict**: never re-run stage 0/1/2 on a paper that already has a global rejection. On a mature corpus this skips most of your filter work.
- **Session verdicts** — `ALREADY_IN_GRAPH`, `REMOVED_TOMBSTONE`, `BELOW_SCORE_THRESHOLD`, `BUDGET_EXHAUSTED`. These are about *this* graph.

So the exclusion query in the pipeline is:

```sql
-- Stage ④ exclusion, both scopes in one pass
WHERE p.id NOT IN (SELECT paper_id FROM graph_nodes WHERE session_id = :sid)
  AND p.id NOT IN (SELECT paper_id FROM filter_decisions
                   WHERE outcome = 'REJECT'
                     AND (session_id IS NULL OR session_id = :sid))
  AND p.id NOT IN (SELECT paper_id FROM latest_event_view
                   WHERE session_id = :sid AND event_type = 'REMOVED')
```

**Enforcement:** every function in `repo/graph.py`, `repo/events.py`, and `repo/expansions.py` takes `session_id` as its **first positional argument**. No defaults, no `Optional`. If you can call it without a session, you will eventually call it with the wrong one. Functions in `repo/papers.py` and `repo/edges.py` must **not** accept `session_id` at all — that's the type system enforcing the boundary for you.

**R0/R1 shortcut:** create session `id=1, name='default'` in the baseline migration and hardcode `SESSION_ID = 1` in one constant in `config.py`. The column is threaded through everything from day one; only the *UI* for switching is deferred to R2.5. This costs you nothing now and saves a migration later.

---

## R0 — Foundation (12 tasks)

**Objective:** fetch, cache, and classify papers from a REPL. No web layer, no graph.
**Estimated: 3–4 days.**

### R0.1 — Repo scaffold · S

```
mkdir -p backend/app/{api,schemas,services/filters,clients,repo,models} \
         backend/{migrations,tests/{unit,integration,fixtures}} \
         frontend eval/{benchmarks,results} config data scripts docs/{adr,media}
```

`pyproject.toml` with: `fastapi uvicorn httpx tenacity pydantic pydantic-settings sqlalchemy alembic networkx pyyaml structlog typer` and dev `pytest pytest-asyncio ruff mypy`.

Add to `pyproject.toml`:
```toml
[tool.ruff]
line-length = 100
[tool.mypy]
strict = true
files = ["backend/app/services", "backend/app/models"]  # strict where it matters
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

`Makefile`: `install dev test test-unit lint typecheck migrate eval load-arxiv`.
`.env.example`: `S2_API_KEY=`, `DB_PATH=data/app.db`, `S2_RATE_LIMIT=1.0`, `LOG_LEVEL=INFO`.
`.gitignore`: `data/*.db`, `data/*.npy`, `.env`, `__pycache__`, `node_modules`.

**Verify:** `make lint && make test` green on an empty suite. Push; CI green.

### R0.2 — Config loader · S

`app/config.py` — `pydantic-settings` `Settings` reading `.env`. Include `SESSION_ID: int = 1`.
Also load `config/ranking.yaml` and `config/filters.yaml` (Appendix A) into typed models, exposing `config_version` as a string derived from `sha256` of the file contents — that's what gets stamped into every record.

**Verify:** `python -c "from app.config import settings, filters; print(settings.db_path, filters.config_version)"`

### R0.3 — Domain models · S

`app/models/` — **frozen dataclasses**, no ORM, no DB knowledge:
`Paper`, `Author`, `Edge`, `PaperStub`, `FilterDecision(outcome, stage, reason_code, details, is_global)`, `CandidateFeatures`, `ScoredCandidate`.

`outcome` is an enum: `ACCEPT | QUARANTINE | REJECT`.

**Verify:** `pytest tests/unit/test_models.py` — construction and immutability.

### R0.4 — Alembic baseline: the full schema · M

Write **every table** from PLAN.md §C in migration `0001_baseline.py`. All of them, now, including ones unused until R6 (`community_id`, `pos_x/pos_y`). Schema churn is free before there's data.

Non-negotiables in this migration:
- `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON` in the engine's connect hook (SQLite defaults FKs **off** — a genuinely nasty silent bug).
- `edges` PK `(citing_id, cited_id)` + `CHECK (citing_id != cited_id)` + index on `cited_id`.
- `graph_nodes` PK `(session_id, paper_id)`, index on `(session_id, state)`.
- `filter_decisions.session_id` nullable, index on `(paper_id, session_id)`.
- Seed row: `INSERT INTO sessions (id, name, created_at) VALUES (1, 'default', ...)`.

**Verify:** `make migrate` then `alembic downgrade base && alembic upgrade head`. Round-trip must be clean.

### R0.5 — Token-bucket rate limiter · S

`app/clients/rate_limit.py` — see Appendix B.1.

**Verify:** unit test — 5 acquisitions at rate 10/s takes ≥0.4s and <0.6s.

### R0.6 — Response cache · S

`app/clients/cache.py`:
```python
def cache_key(endpoint: str, params: dict) -> str:   # sha256 of endpoint + sorted json
async def get(endpoint, params) -> dict | None
async def put(endpoint, params, status_code, response) -> None
```
Store the response **verbatim**. Never normalize on write.

**Verify:** put → get round-trips; distinct params produce distinct keys; key is stable across dict ordering.

### R0.7 — S2 client · L

`app/clients/s2.py`. Pydantic models with **every field `Optional`**.

```python
class S2Client:
    async def search_title(self, q: str, limit: int = 10) -> list[PaperStub]
    async def get_papers(self, s2_ids: list[str], fields: FieldSet) -> list[Paper]
        """Chunks into batch POSTs. The ONLY metadata path — no single-paper getter,
           so callers cannot accidentally do N+1 fetching."""
    async def get_references(self, s2_id: str, limit: int = 200) -> list[EdgeRecord]
    async def get_citations(self, s2_id: str, limit: int = 1000) -> list[EdgeRecord]
    async def get_authors(self, s2_ids: list[str]) -> list[Author]
```

Layer order, innermost out: rate limit → cache lookup → httpx → tenacity → Pydantic → normalize to dataclass.

Nested field sets so candidates arrive prescoreable with zero extra calls:
```python
REF_FIELDS = ("title,year,publicationDate,citationCount,externalIds,"
              "publicationTypes,venue,authors,contexts,intents,isInfluential")
```

Handling: `429` → backoff honouring `Retry-After`, max 5 attempts. `5xx` → 3 retries then raise `S2TransientError` (caller continues). `404`/null → return `None`, caller writes a STUB. Missing field → **never raise**; log `SCHEMA_DRIFT` with the paper id.

**Verify:** `python -m app.cli fetch "Attention Is All You Need"` prints normalized metadata.

### R0.8 ◆ CHECKPOINT — cache actually works · S

Run R0.7's command **twice**. Instrument the client with a call counter.

**Verify:** second run reports `api_calls=0, cache_hits=N`. If not, stop and fix the cache before writing anything else — every later task depends on this.

### R0.9 — `CachedOnlyS2Client` + fixtures · M

Subclass that raises `CacheMiss` on any miss. Populate `tests/fixtures/s2_cache.db` with ~15 papers chosen to cover your real cases:

| Fixture | Purpose |
|---|---|
| Attention Is All You Need | hub (forward-expand guard) |
| BERT | primary cs.CL |
| A 2013 paper (e.g. word2vec-era) | boundary paper / `PRE_ERA` |
| An ImageNet-style dataset paper | `IS_DATASET` |
| A high-citation survey | survey-accept path |
| A recent low-citation survey | survey-reject path |
| A medical-AI paper | `FIELD_NON_CS` |
| A primary cs.CV paper | `CAT_PRIMARY_APPLIED` |
| A primary cs.LG paper cross-listed cs.CV | **must pass** — the case that stops the deny list overreaching |
| A NeurIPS paper with no arXiv ID | `VENUE_CORE` fallback |
| An arXiv-preprint / conference duplicate pair | dedup |
| A paper with a null abstract and null year | schema drift + `year IS NULL` quarantine |

Commit the fixture DB. **This is the single highest-leverage 2 hours in the project** — it makes every subsequent test offline, fast, and deterministic.

**Verify:** `pytest tests/integration/ -k s2` passes with the network **disabled**.

### R0.10 — arXiv bulk loader · L

`scripts/load_arxiv_meta.py` → `arxiv_meta(arxiv_id, primary_category, categories, updated)`.

Requirements: `--limit N` for testing, resumable (checkpoint file or resumption token), idempotent (`INSERT OR REPLACE`), progress logging every 50k rows. Expect ~2.7M rows total, ~200k for `cs.*` alone if you filter.

**Verify:** `python scripts/load_arxiv_meta.py --limit 1000` then `SELECT primary_category, COUNT(*) FROM arxiv_meta GROUP BY 1 ORDER BY 2 DESC LIMIT 10`.

### R0.11 ◆ CHECKPOINT — categories resolve · S

Join `arxiv_meta` in the paper normalizer so `Paper.primary_arxiv_category` populates.

**Verify:** `python -m app.cli fetch "BERT"` reports `cs.CL`. `fetch "Deep Residual Learning"` reports `cs.CV`. Both must be right — one confirms the join, the other confirms you'll actually deny cs.CV.

### R0.12 — Structured logging · S

`structlog`, JSON to `data/app.log`. Bind `session_id`, `expansion_id`, `config_version` into context. Log every S2 call as `{endpoint, cache_hit, status, duration_ms}`.

**Verify:** `grep s2_request data/app.log | tail -5` shows structured lines.

**R0 DONE when:** you can fetch any paper by title, twice, with the second fetch making zero network calls, and get a correct arXiv primary category. Tests pass offline.

---

## R1 — Walking skeleton (22 tasks)

**Objective:** type a title → a filtered citation graph renders in a browser.
**Estimated: 5–7 days.** This is your first usable release.

### Backend: repositories

### R1.1 — `repo/papers.py`, `repo/edges.py` · M

```python
# NOTE: no session_id parameter — these are global (see contract)
def upsert_paper(conn, paper: Paper) -> int                    # returns internal id
def upsert_stub(conn, s2_id: str, title: str, year: int|None) -> int
def get_papers_by_ids(conn, ids: list[int]) -> list[Paper]
def find_by_canonical_key(conn, key: CanonicalKey) -> int | None

def upsert_edge(conn, citing_id: int, cited_id: int, via: str, **kw) -> None
    """ON CONFLICT: merge discovered_via (BACKWARD + FORWARD -> BOTH),
       OR the is_influential flag, union the intents array."""
def get_edges_for(conn, paper_ids: list[int]) -> list[Edge]
```

Every write is idempotent. Run each twice in tests and assert row counts don't change.

**Verify:** `pytest tests/unit/test_repo_papers.py` — includes the discovered_via merge case.

### R1.2 — `repo/graph.py`, `repo/events.py` · M

`session_id` first positional, no default:
```python
def add_node(conn, session_id, paper_id, state, depth, score, features, added_by) -> None
def get_nodes(conn, session_id, states: list[str]|None = None) -> list[GraphNode]
def get_node_ids(conn, session_id) -> set[int]
def remove_node(conn, session_id, paper_id) -> None
def append_event(conn, session_id, paper_id, event_type, actor, payload) -> int
def latest_state(conn, session_id, paper_id) -> str | None
def removed_paper_ids(conn, session_id) -> set[int]
```

**Verify:** unit tests; plus one asserting `add_node` for session 1 is invisible to session 2.

### R1.3 — `services/dedup.py` · M

```python
def canonical_key(p: Paper) -> CanonicalKey:
    if p.doi:      return ("doi", p.doi.lower())
    if p.arxiv_id: return ("arxiv", p.arxiv_id.split("v")[0])   # strip version suffix
    return ("title", normalize_title(p.title), first_author_surname(p), p.year)

def normalize_title(t: str) -> str:
    # lowercase, NFKD unicode, strip punctuation, collapse whitespace, strip
    # leading articles
```

**Verify:** test the false-positive direction hardest — "Attention Is All You Need" vs "Attention Is Not All You Need" must **not** collide. Then: arXiv `v1`/`v3` collapse; case/punctuation variants collide; preprint+conference pair merges.

### Backend: filters

### R1.4 — `services/filters/base.py` + `era_filter.py` · M

Pure functions, `Paper -> FilterDecision`. No DB access, no I/O.

```python
def era_filter(p: Paper, cfg: FilterConfig) -> FilterDecision:
    if p.year is None:
        return FilterDecision(QUARANTINE, "ERA", "YEAR_UNKNOWN", is_global=True)
    if p.year < cfg.year_floor:
        return FilterDecision(REJECT, "ERA", "PRE_ERA", is_global=True)
    return FilterDecision(ACCEPT, "ERA", "OK", is_global=True)
```

**Verify:** 2014 → `PRE_ERA`; 2015 → accept; `None` → quarantine, not silently admitted.

### R1.5 — `type_filter.py` and `topic_filter.py` · L

Implement PLAN.md §E4 stages 1 and 2 exactly. Every branch returns a distinct `reason_code`.

Survey policy must be `citation_count >= 200 OR citations_per_year >= 40` — the OR is what keeps recent good surveys.

Topic stage: primary category wins. `primary ∈ CORE_ALLOW` → accept even if cross-listed to a denied category. `primary ∈ APPLIED_DENY` → reject even if cross-listed to a core one.

**Verify:** one test per fixture paper from R0.9, each asserting the exact `reason_code`. The primary-cs.LG-cross-listed-cs.CV case must **accept**.

### R1.6 — `filters/cascade.py` + decision logging · M

```python
def run_cascade(conn, session_id, p: Paper, cfg) -> FilterDecision:
    """Stage 0→1→2→4, short-circuit on first non-ACCEPT.
       Check the global-verdict cache first (see session_id contract).
       Persist every decision with config_version."""
```

**Verify:** integration test — cascade over all 15 fixtures; assert the accept set is exactly the expected 6; assert `filter_decisions` has one row per paper; assert re-running makes zero additional filter evaluations for globally-rejected papers.

### R1.7 ◆ CHECKPOINT — filtering is correct · S

CLI: `python -m app.cli filter-report --limit 50` — fetch 50 references of BERT, run the cascade, print a table of `title | year | primary_cat | outcome | reason_code`.

**Verify:** read every row yourself. No datasets accepted, no pre-2015 accepted, no cs.CV accepted, no medical accepted. If any row looks wrong, fix it now — this filter gates every paper the system will ever show you.

### Backend: pipeline

### R1.8 — `services/candidates.py`: pooling + prescore · L

```python
def build_pool(conn, session_id, frontier: list[int]) -> list[PoolEntry]:
    """UNION candidates from ALL frontier nodes. Compute anchor_overlap during
       the union — it is the whole reason for pooling. Do not take per-node top-K."""

def prescore(e: PoolEntry) -> float   # Appendix B.3
```

Hub guard lives here: skip forward expansion for any node with `citation_count > cfg.forward_expand_max` (2000). Backward is always allowed, capped at 200 refs.

**Verify:** unit test — a candidate reachable from 3 frontier nodes gets `anchor_overlap == 3`; a hub node produces zero forward candidates and logs `HUB_SKIP_FORWARD`.

### R1.9 — Boundary papers · M

In the edge-ingestion path: when a fetched reference/citation fails the era filter, still `upsert_stub` + `upsert_edge`, log `PRE_ERA`, and **do not** create a `graph_nodes` row.

**Verify:** the critical test — two modern papers whose only shared reference is a 2014 paper must have **non-zero** `bib_coupling`. This test protects the design; without it the year floor silently degrades your best feature. (The feature itself lands at R3; write the test now against a hand-built graph.)

### R1.10 — `services/budget.py` · M

Appendix B.4. Recency lane 15% → direction floors 20% each → remainder by score → per-source cap 40%.

**Verify:** `budget=20` returns exactly 20 when the pool is large; ≥3 are `age < 12mo` if available; no single source exceeds 8; deterministic under shuffled input (tie-break on `paper_id`).

### R1.11 — `services/expansion.py` · L

The §E pipeline as one orchestrating function, one transaction:

```python
async def expand(conn, session_id: int, params: ExpandParams) -> ExpansionResult:
    # ① frontier  ② edge fetch  ③ pool  ④ dedup+exclude  ⑤ filter
    # ⑥ prescore/prune to 3×budget  ⑦ enrich (batch)  ⑧ features  ⑨ rank
    # ⑩ budget  ⑪ commit  → write expansions row
```

Hard stops: `api_call_budget` (default 60) and `max_nodes` (2000). On hitting either, **commit what you have** and record the truncation in `expansions.error`. A partial expansion is a success.

R1 uses prescore as the final ranking (real features arrive at R3).

**Verify:** integration test against `CachedOnlyS2Client`: seed BERT, expand with `max_new=20`, assert node count, assert no rejected paper present, assert `api_calls <= 60`, assert the `expansions` row is populated.

### R1.12 — `services/seed.py` · M

```python
async def add_seed(conn, session_id, s2_paper_id) -> GraphNode:
    """fetch metadata → dedup → cascade (force=True bypasses, logged) →
       upsert → add_node(state=SEED, depth=0) → append SEED_ADDED event"""
```
Seeds bypass filters with `force=True` but the decision is still logged — you want to know you overrode it.

**Verify:** adding the same seed twice raises `AlreadyPresent`; adding a filtered paper without `force` raises with the `reason_code` attached.

### R1.13 ◆ CHECKPOINT — the recommender works · M

```bash
python -m app.cli seed "BERT" --expand --max-new 20
python -m app.cli show --session 1
```

**Verify by hand.** Open `data/app.db` in a SQLite browser. Confirm: ~21 `graph_nodes` rows; edges present; `filter_decisions` populated with sensible reason codes; several `papers` rows with no `graph_nodes` row (boundary papers and rejects — this proves corpus/graph separation); one `expansions` row with real counts.

**At this point you have a working paper recommender.** Everything after R1.13 is presentation. If you got here and the candidate list looks good, the project's core hypothesis is validated.

### Backend: HTTP

### R1.14 — FastAPI app + health · S

`app/main.py`: app, CORS allowlist `http://localhost:5173` (explicit, not `*`), bind `127.0.0.1`, lifespan opening the DB, `GET /api/health` → `{db, s2_reachable, cache_rows, node_count}`.

**Verify:** `uvicorn app.main:app --reload`, then `curl localhost:8000/api/health` and open `/docs`.

### R1.15 — `GET /api/search` · S

Passthrough to `search_title`, cache-first. Response includes `already_in_graph` and `previously_removed` flags — these are what stop you re-adding what you deleted.

**Verify:** `curl "localhost:8000/api/search?q=bert&limit=5"`

### R1.16 — `POST /api/sessions/{sid}/nodes` · M

Body `{s2_paper_id, as_state: "SEED", force: false}`. Sync add. Errors: `404` unknown, `409` present, `422` filtered (body carries `reason_code`).

**Verify:** `curl -X POST .../sessions/1/nodes -d '{"s2_paper_id":"..."}'` → 201. Repeat → 409.

### R1.17 — `GET /api/sessions/{sid}/graph` · M

Whole graph in one response — no pagination until you've measured a problem. Nodes carry `{id, title, state, score, year, citation_count, paper_type, in_degree, out_degree, pos}`. Edges carry `{source, target, is_influential}`. Meta carries `{node_count, edge_count, config_version, crawl_completeness}`.

**Verify:** response for a 21-node graph is < 50KB and parses.

### R1.18 — `POST /api/sessions/{sid}/expansions` — **synchronous at R1** · S

Return `200` with the result directly. Do **not** build the job system yet; that's R2.4. `422` if the graph is at `max_nodes`.

**Verify:** `curl -X POST .../expansions -d '{"hops":1,"max_new":20}'` returns added node ids in under 60s.

### R1.19 — `GET /api/nodes/{id}` · S

Full metadata + features + `score_breakdown` (empty until R3).

### Frontend

### R1.20 — Vite scaffold + generated types · M

`npm create vite@latest frontend -- --template react-ts`. Add `@tanstack/react-query`, `zustand`, `cytoscape`, `cytoscape-fcose`, `openapi-typescript`.

`make types` → `npx openapi-typescript http://localhost:8000/openapi.json -o src/types/api.ts`. **Never hand-write these types.** Add it to the Makefile so it's one command after any schema change.

**Verify:** `npm run dev` serves; `src/types/api.ts` exists and contains your schemas.

### R1.21 — `<GraphCanvas>` · L

Cytoscape wrapper. Stylesheet from Appendix B.6 (state → colour/shape/size). fcose layout with `randomize: false`. `fit()` on load. Click → `setSelected(id)` in the Zustand store.

Keep the Cytoscape instance in a `useRef`. **Never mirror node positions into React state** — that's a re-render per animation frame.

**Verify:** hardcode a 5-node fixture; confirm colours differ by state and the layout doesn't overlap.

### R1.22 — `<AddPaperDialog>` + `<ExpansionControls>` + `<NodeInspector>` · L

- Dialog: input → debounced `GET /search` → results list showing year, citations, venue, and the `already_in_graph` badge → select → `POST /nodes` → invalidate `['graph']`.
- Controls: `max_new` number input (default 20, max 100) + Expand button + spinner + result toast ("added 18 of 20").
- Inspector: right panel on selection — title, authors, venue, date, citations, type, categories, in/out degree, depth.

**Verify:** Playwright — type "BERT", select the first result, assert ≥5 nodes render; click Expand, assert node count increases.

### R1.23 ◆ CHECKPOINT — R1 complete · S

**Verify by using it:** open the browser, add "BERT", click Expand twice. You should see a ~60-node graph of post-2015 core-ML papers with no datasets, no cs.CV, no medical AI. Click nodes and read metadata. Total time under 90 seconds.

Then: `README.md` with a GIF of exactly that, `docs/graph-model.md` (PLAN.md §D definitions verbatim), `docs/adr/0001-sqlite-not-neo4j.md`.

**Now stop and use it for a week.** Seed it with papers you actually care about. Keep a text file of annoyances. That file should drive your R2 priorities more than this document does.

---

## R2 — Interaction and GC (16 tasks)

**Objective:** the graph becomes yours to curate. **Estimated: 5–6 days.**

| # | Task | Size | Verify |
|---|---|:--:|---|
| R2.1 | State machine as a **data table** (`TRANSITIONS: dict[(from,to), Rule]`), not if/else. Rules carry `allowed`, `error_code`, `side_effects`. | M | Table-driven test over all (state × state) pairs incl. illegal; `SEED → LIKED` yields `SEED_NOT_LABELABLE` |
| R2.2 | `PATCH /api/sessions/{sid}/nodes/{id}` — one endpoint, not like/dislike/unlike. Writes event + materialized state in one transaction. | M | 409 on seed; 200 returns `{node, rescored_count}` |
| R2.3 | `scripts/rebuild_state.py` — reconstruct `graph_nodes.state` from `interaction_events` alone. | S | Rebuild after 20 mixed labels; assert identical to materialized state. Your event-log safety net. |
| R2.4 | Job queue: `jobs` table + one worker thread + `POST /expansions` → `202 {job_id}`; `GET /expansions/{id}` → `{status, stage, progress}`. `409` if one is already running for the session. | L | Two concurrent POSTs → second gets 409 |
| R2.5 | Mark-and-sweep GC (Appendix B.5) | L | Six hand-built graph cases from PLAN.md §Confirmed removal semantics, incl. **labeled node survives disconnection** and idempotency |
| R2.6 | `DELETE /nodes/{id}?dry_run=true` → `{would_remove, count}`; `false` → executes, writes `REMOVED` + `GC_SWEPT` events with `actor=SYSTEM` | M | Dry-run count equals actual removal count exactly |
| R2.7 | Tombstone exclusion wired into pipeline stage ④ | S | Remove a node, expand again, assert it does not return |
| R2.8 | Restore: `POST /nodes/{id}/restore` — the only path back from a tombstone. Never automatic. | S | Restored node reappears as CANDIDATE |
| R2.9 | Frontend: expansion progress UI (poll 1s, show stage + counts) | M | Progress advances visibly through stages |
| R2.10 | Neighbour highlight — `cy.$id(x).closedNeighborhood().addClass('hl')`, everything else `.addClass('fade')` | S | Click a node; it and its neighbours stay bright |
| R2.11 | `<SearchBar>` — local fuzzy filter over **loaded** nodes; dims non-matches, never mutates the graph or calls the API | S | Type a substring; matches stay bright; node count unchanged |
| R2.12 | Layout position persistence + pinning: save `pos_x/pos_y` on layout end; on expansion pass `fixedNodeConstraint` for existing nodes | M | **Expand 3× and confirm existing nodes do not move.** The single biggest UX win in R2. |
| R2.13 | `<StatsPanel>` v1 — nodes, edges, per-state counts, components, avg degree, density, `crawl_completeness`. No PageRank yet (R3). | M | Counts match the DB |
| R2.14 | `<ReviewDrawer>` — Quarantined / Rejected / Removed tabs, grouped by `reason_code` with counts, each row restorable | L | Rejected count matches `filter_decisions`; Restore works |
| R2.15 | Clear graph: `DELETE /graph?confirm=true`, UI requires typing "clear" | S | Without `confirm` → 400 |
| R2.16 | Session switcher UI + `POST/GET /api/sessions` | M | Create session 2, seed a different paper, confirm session 1 unchanged and **zero new API calls** for any already-crawled paper |

**◆ R2 DONE when:** you curate a 100-node graph for 20 minutes — liking, disliking, removing — and the layout never scrambles, no removed paper ever returns, and every rejection is visible and reversible in the drawer. R2.16's zero-API-call result is the payoff for getting the session contract right.

---

## R3–R6 — Task checklists

Less granular deliberately: by R3 you'll know the codebase better than this document does, and your week of real usage will have reordered the priorities.

### R3 — Ranking and interpretability · 6–8 days

- [x] `services/graphops.py`: build `nx.DiGraph` from SQL (measured; subgraph **views**, not copies — 118ms → 85ms)
- [x] PageRank + reverse PageRank (hub-ness ≈ survey-ness), in/out degree
- [x] Co-citation and bibliographic coupling counts — the R1.9 boundary-paper test re-run against the real feature. Lives in `services/similarity.py`, not `graphops.py`; it runs over the **corpus**, not the drawn graph
- [x] Crawl-bias handling: PageRank over `crawl_state != STUB` only; suppressed below 60% completeness (**absent from the feature dict, not zero**); documented in `docs/algorithms.md`
- [x] Age-normalized citations, recency
- [x] Rank-percentile normalization (Appendix B.7) — **not** z-score
- [x] `node_features` persisted (`services/features.py`); `services/ranking.py` a **pure** function of `(features, weights)`
- [x] `PUT /api/config` → instant rescore, zero API calls. This is the payoff for persisting features.
- [x] `score_breakdown` persisted per node
- [x] Per-source diversity cap in budget allocation — `_enforce_source_cap`, relaxed rather than returning short

Remaining, in execution order. Each is one commit; review after every third.

- [ ] **R3.a — `GET /api/sessions/{sid}/candidates?limit=50&sort=score`.** Specified in PLAN.md §G, never built. `<CandidateList>` cannot exist without it, so it comes first. Ranked CANDIDATE nodes with score, breakdown and the metadata a reader decides from.
- [ ] **R3.b — `<CandidateList>`** — sortable ranked table. **This, not the graph, is where you'll make most decisions.** Row selection drives `selectedNodeId`, so the graph and the table share one selection.
- [ ] **R3.c — `<ScoreBreakdown>`** — horizontal bars per term. Highest signal-per-line in the app. `GET /nodes/{id}` already returns `features` and `score_breakdown`; this is frontend-only.
  *After R3.c the R3 gate is met: flip one weight → the list reorders → the breakdown says why.*
- [ ] **R3.d — venue tier.** `papers.venue_tier` exists and is **0/635 populated**; `venue` is populated for all 635 and `CORE_VENUES` is already in `filters.yaml`. A normalizer, a backfill, and a feature — no schema change.
- [ ] **R3.e — score threshold floor.** `ranking.yaml` carries `budget.score_floor: 0.0`, `config.py` parses it, and **nothing reads it** — a lever connected to nothing, the exact failure `test_features.py` asserts against for weights. Wire it into budget allocation and add the matching test.
- [ ] ~~Lazy author h-index~~ — **deferred past R4, deliberately.** PLAN.md §C4 already demotes it ("a weak signal with a real cost"), its weight is 0.00, and the table holds 66 authors with **zero** h-index values, so it needs a per-paper S2 author fetch. Paying that API cost for a signal nothing can yet show is worth something is backwards; R4 is what decides whether it earns its place.
- [x] Tests: PageRank vs analytic answer · normalization outlier-immunity · monotonicity per feature · byte-identical determinism · breakdown terms sum to score

**Config invariant learned here, the hard way:** the sign in `ranking.yaml` *is* the sign in the score. `hub` shipped as `0.60  # subtracted`; `score_paper` multiplies, so hubs were rewarded for months of writing. It was invisible until R3 first populated the feature, and `test_active_r1_weights` had asserted the wrong value — the test encoded the bug. `test_subtractive_weights_are_actually_negative` now pins it.

### R4 — Evaluation ⭐ · 5–7 days

The most valuable release. Do not skip or defer it.

- [ ] `eval/build_benchmark.py` — 150–300 held-out targets (core-ML, 2019–2024, ≥15 refs). Seeds = 3 sampled references; ground truth = the rest.
- [ ] **Temporal cutoff enforcement** — reject any candidate published after `min(seed.publication_date)`
- [ ] **The leakage test.** Assert no ground-truth paper is visible to the system before its cutoff. *This is the most important single test in the project* — without it every headline number is fiction.
- [ ] `eval/metrics.py` — Recall@{10,20,50}, NDCG@20, MRR, Hit@10, bootstrap 95% CIs
- [ ] `eval/baselines.py` — all seven: random · citation-count · unranked BFS · citations/year · anchor-overlap-only · **S2's own `/recommendations`** · your ranker
- [ ] Config sweep over weight grids → `eval/results/*.json`
- [ ] `docs/evaluation.md` — protocol, results table, ≥3 ablations, failure analysis, and the limitations section from PLAN.md §R4 (that section is graded harder than the results)
- [ ] `make eval` reproducible: same seed + config → identical metrics

### R5 — Personalization · 4–5 days

- [ ] Personalized PageRank on `SEED ∪ LIKED` (`nx.pagerank(personalization=...)`, fixed tolerance for determinism)
- [ ] Dislike proximity as a subtracted penalty term
- [ ] Rocchio-style weight nudging after N labels
- [ ] Live rescore + candidate reorder on every label
- [ ] **Simulated-user eval:** reveal ground-truth papers as "likes" one at a time; assert Recall@20 rises
- [ ] **If PPR doesn't beat co-citation on the benchmark, keep the simpler model and write that up.** A documented negative result is a strong signal.

### R6 — Embeddings and clustering · 5–7 days

- [ ] SPECTER2 over title+abstract (check first whether S2 serves `embedding.specter_v2` — if so, inference cost is zero)
- [ ] Store as `data/embeddings.npy` + an id index. Brute-force cosine over 3k×768 is sub-millisecond. **No vector DB.**
- [ ] **The release's actual justification:** embedding-generated candidates that the citation graph *cannot* reach (concurrent work with no citation path). Report how many benchmark recoveries came only from this channel. If that number is near zero, the release didn't earn its place — say so.
- [ ] Reciprocal rank fusion of graph and embedding pools
- [ ] Dedup upgrade: cosine > 0.97 + title similarity → duplicate candidates for review
- [ ] Leiden communities → `community_id` → cluster-aware `idealEdgeLength` (45 intra / 220 inter) + convex hulls + TF-IDF cluster labels
- [ ] Filter stage 3 upgrade: exemplar-centroid core-vs-applied classifier with a quarantine margin, trained on your accumulated drawer decisions
- [ ] **Reconsider `cs.CV`.** By now the drawer will show whether you've been restoring cs.CV papers. If you have, move it to BORDERLINE — one line of YAML, and you'll have the evidence.

**After R6: stop.** R0–R6 is a complete, measured system. R7 (LTR) and R8 (GNN) are optional experiments, justified only if R4 shows a specific gap. That restraint is itself the thing the project demonstrates.

---

## Appendix A — Config files (verbatim)

### `config/filters.yaml`
```yaml
year_floor: 2015

paper_type_policy:
  RESEARCH:  { action: accept }
  SURVEY:    { action: accept_if, citation_count_min: 200, or_citations_per_year_min: 40 }
  BENCHMARK: { action: quarantine }
  DATASET:   { action: reject }
  POSITION:  { action: quarantine }

CORE_ALLOW:   [cs.LG, cs.AI, cs.CL, cs.NE, stat.ML, cs.MA, cs.PL]
BORDERLINE:   [cs.IR, cs.CY, cs.DS, math.OC, cs.DC, cs.CE, cs.MS]
APPLIED_DENY: [cs.CV, cs.RO, cs.SE, cs.HC, cs.CR, cs.DB, cs.NI, cs.SD,
               "q-bio.*", "q-fin.*", "eess.*", "physics.*", "econ.*", "astro-ph.*"]

CORE_VENUES: [NeurIPS, ICML, ICLR, ACL, EMNLP, NAACL, AAAI, IJCAI,
              COLT, AISTATS, TMLR, JMLR, COLM, EACL, CoNLL]

applied_keywords: [clinical, patient, radiolog, EHR, diagnosis, crop, agricultur,
                   portfolio, stock return, credit risk, seismic, protein folding,
                   molecul, drug discovery, wafer, churn, fraud detection]
# keyword hits QUARANTINE only, never auto-reject

forward_expand_max: 2000
max_reference_fetch: 200
max_nodes: 2000
max_depth: 3
```

### `config/ranking.yaml`
```yaml
# R1 uses prescore only. These activate at R3.
weights:
  ppr:      0.00   # R5
  cocite:   0.00   # R3
  bibcoup:  0.00   # R3
  overlap:  2.00   # R1  ← dominant. Start here.
  quality:  0.40
  recency:  0.30
  venue:    0.00   # R3
  author:   0.00   # R3, cold-start only
  dislike:  0.00   # R5, subtracted
  hub:      0.60   # subtracted
budget:
  recency_lane_frac: 0.15
  direction_floor_frac: 0.20
  source_cap_frac: 0.40
  score_floor: 0.0   # R3
```

---

## Appendix B — Code skeletons for the tricky parts

### B.1 Token bucket
```python
class TokenBucket:
    def __init__(self, rate: float, capacity: float | None = None):
        self.rate = rate
        self.capacity = capacity or max(1.0, rate)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                await asyncio.sleep((n - self._tokens) / self.rate)
```

### B.2 Edge upsert with provenance merge
```sql
INSERT INTO edges (citing_id, cited_id, discovered_via, is_influential,
                   intents, first_seen_at)
VALUES (:citing, :cited, :via, :infl, :intents, :now)
ON CONFLICT (citing_id, cited_id) DO UPDATE SET
  discovered_via = CASE
      WHEN edges.discovered_via = excluded.discovered_via THEN edges.discovered_via
      ELSE 'BOTH' END,
  is_influential = MAX(edges.is_influential, excluded.is_influential),
  intents = :merged_intents;   -- union computed in Python
```

### B.3 Prescore (R1's ranker)
```python
def prescore(e: PoolEntry, cfg) -> float:
    cpy = e.citation_count / max(e.age_years, 0.5)
    hub = math.log1p(max(0, e.citation_count - cfg.forward_expand_max)) / 10
    return (2.0 * e.anchor_overlap                      # ← dominant signal
          + 0.8 * ("methodology" in e.intents)
          + 0.5 * e.is_influential
          + 0.4 * math.log1p(cpy) / 5
          + 0.3 * (1.0 if e.age_years < 1.5 else 0.0)
          - 0.6 * hub)
```

### B.4 Budget allocation
```python
def allocate(pool, cfg, budget: int) -> list[PoolEntry]:
    def top(cands, n):
        # stable, deterministic tie-break — see PLAN.md §M8
        return sorted(cands, key=lambda c: (-c.score, c.paper_id))[:n]

    picked: list[PoolEntry] = []
    recent = [p for p in pool if p.age_years < 1.0]
    picked += top(recent, max(1, int(cfg.recency_lane_frac * budget)))

    remaining = budget - len(picked)
    for direction in ("BACKWARD", "FORWARD"):
        avail = [p for p in pool if p.direction == direction and p not in picked]
        picked += top(avail, int(cfg.direction_floor_frac * remaining))

    picked += top([p for p in pool if p not in picked], budget - len(picked))
    return enforce_source_cap(picked, int(cfg.source_cap_frac * budget))
```

### B.5 Mark-and-sweep GC
```python
def gc_sweep(conn, session_id: int, cfg, dry_run: bool = False) -> list[int]:
    """Orphan = unlabeled AND no path to any anchor over the UNDIRECTED
       projection within max_depth. Labeled nodes are never swept."""
    anchors = repo.graph.get_node_ids_by_state(conn, session_id, ["SEED", "LIKED"])
    g = graphops.build_undirected(conn, session_id)

    marked: set[int] = set(anchors)
    frontier, depth = set(anchors), 0
    while frontier and depth < cfg.max_depth:
        nxt = {n for f in frontier for n in g.neighbors(f)} - marked
        marked |= nxt
        frontier, depth = nxt, depth + 1

    candidates = repo.graph.get_node_ids_by_state(conn, session_id, ["CANDIDATE"])
    orphans = sorted(candidates - marked)          # sorted → deterministic

    if not dry_run:
        for pid in orphans:
            repo.graph.remove_node(conn, session_id, pid)
            repo.events.append_event(conn, session_id, pid, "GC_SWEPT", actor="SYSTEM")
    return orphans
```

### B.6 Cytoscape stylesheet
```js
const stylesheet = [
  { selector: 'node', style: {
      label: 'data(shortTitle)', 'font-size': 8, 'text-valign': 'bottom',
      'text-wrap': 'ellipsis', 'text-max-width': 90,
      width:  'mapData(logCites, 0, 12, 14, 48)',
      height: 'mapData(logCites, 0, 12, 14, 48)' } },

  { selector: 'node[state="SEED"]',      style: {
      'background-color': '#1d4ed8', shape: 'hexagon',
      'border-width': 3, 'border-color': '#1e293b' } },
  { selector: 'node[state="LIKED"]',     style: {
      'background-color': '#16a34a', 'border-width': 2 } },
  { selector: 'node[state="DISLIKED"]',  style: {
      'background-color': '#9ca3af', opacity: 0.4,
      'border-style': 'dashed', 'border-width': 1 } },
  { selector: 'node[state="CANDIDATE"]', style: {
      'background-color': 'mapData(score, 0, 5, #e5e7eb, #f59e0b)' } },
  // shape carries paper_type so meaning never rests on colour alone
  { selector: 'node[paperType="SURVEY"]', style: { shape: 'diamond' } },

  { selector: 'edge', style: {
      width: 1, 'line-color': '#cbd5e1', 'curve-style': 'bezier',
      'target-arrow-shape': 'triangle', 'target-arrow-color': '#cbd5e1',
      'arrow-scale': 0.6, opacity: 'mapData(influential, 0, 1, 0.25, 0.9)' } },

  { selector: '.hl',   style: { 'border-width': 4, 'border-color': '#f43f5e', 'z-index': 10 } },
  { selector: '.fade', style: { opacity: 0.12 } },
];
```

### B.7 Rank-percentile normalization
```python
def rank_normalize(values: list[float]) -> list[float]:
    """Percentile rank in [0,1]. Outlier-immune, unlike z-score — one
       100k-citation hub would otherwise flatten every other feature to noise.
       Ties share the average rank."""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out
```

---

## Appendix C — Progress tracker

Copy into `docs/progress.md` and tick as you go.

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
