# Citation-Graph Paper Recommender — Engineering Plan

**Audience:** solo developer, targeting a beginner AI/ML Engineer role.
**Review stance:** senior SWE / AI systems engineer. Opinionated. Where your proposal is wrong I say so.

> **Verification note:** I cannot browse from this environment. All Semantic Scholar (S2) API specifics below — field names, batch limits, rate limits — reflect my knowledge as of mid-2026 and should be verified against the current docs before you build the client. The *architecture* does not depend on the exact numbers; the client is designed to tolerate them changing.

---

## Assumptions I'm making (stated, not asked)

| # | Assumption | Why it's safe |
|---|---|---|
| A1 | Single user, local-first. No auth, no multi-tenancy. | You said personal project. Auth adds zero portfolio value here. |
| A2 | Target graph size: 200–3,000 visible nodes; corpus (all papers ever seen) 10k–100k rows. | This single number kills Neo4j, Celery, and Postgres for V1. |
| A3 | You have an S2 API key. | Free, and the plan assumes ~1 req/s budgeting. |
| A4 | Python 3.11+ backend, React+TypeScript frontend. | You proposed it; it's correct. |
| A5 | "Applied AI" exclusion is a *soft* judgment that needs human review, not a solvable classification problem. | Design consequence: quarantine queue, not silent rejection. |
| A6 | **Sessions kept** — named workspaces, multiple independent graphs. `session_id` column from R0, switcher UI at R2. | Your decision. Facts about papers are global (shared corpus, shared cache); opinions are session-scoped. See BUILD.md §The session_id contract for the exact scoping boundary. |
| A7 | **Corpus year floor = 2015.** Papers published before 2015 are never admitted as graph nodes. | Your decision. Post-2015 core ML has near-total arXiv coverage, which simplifies the topic filter substantially (see §E4 STAGE 0). |
| A8 | **`cs.CV` is denied outright**, not quarantined. | Your decision. Removes the hardest filter case. |
| A9 | **No LLM/RAG stage.** The roadmap ends at R6, with two optional measured experiments (R7 LTR, R8 GNN). | Your decision. Abstracts are still stored — R6 embeddings need them. |

---

# A. Critical architecture review

## GOOD — keep as proposed

- **Citation graph as the primary representation.** Correct and differentiating. Co-citation and bibliographic coupling are genuinely strong relevance signals that pure-embedding systems throw away. This is the reason the project is interesting.
- **Separating candidate generation / filtering / ranking.** This is the right architectural spine. Make it literally three modules with three test suites.
- **User state stored separately from paper metadata.** Correct. Paper facts come from S2 and are refreshable; your labels are yours and must survive a metadata refetch.
- **Graph algorithms before LLMs.** Correct, and unusual. Most portfolio projects do the reverse and look identical to each other.
- **User-controlled expansion budget.** Correct. Uncontrolled BFS is the #1 way this project dies.
- **Refusing to add a GNN for resume points.** Keep that discipline. A GNN on 2k nodes with 30 labels will lose to personalized PageRank, and a senior reviewer will know it.
- **Seed papers cannot be liked/disliked, only removed.** Good, clean rule. Encode it as a validation error (HTTP 409), not a UI-only convention.

## CHANGE — proposals that are wrong or under-specified

### C1. "Top 10 from references + top 10 from citations" — replace with a pooled, budgeted cut

Two problems.

**Symmetry is wrong.** Reference lists are 20–80 entries, author-curated, high-precision, biased toward older/foundational work. Citation lists are 0 to 100,000+ entries, uncurated, biased toward recent work, and mostly noise. Giving them equal fixed budgets over-samples the noisy direction and under-samples the clean one.

**Per-node top-K destroys your best signal.** The strongest structural indicator that a candidate belongs in your graph is **how many of your anchors point at it (or are pointed at by it)**. If you take top-10 from node A, then top-10 from node B independently, you never observe that paper X was reachable from both A and B. That overlap count *is* bibliographic coupling / co-citation degree restricted to the anchor set, and it dominates every metadata feature.

**Replacement:** collect the union of all reachable candidates from *all* frontier nodes into one pool, compute overlap-aware features on the pool, then take a single budgeted cut with per-direction *floors* (not fixed splits). Details in §E.

### C2. Expansion should be frontier-driven, not "expand the node I clicked"

If the user must manually pick which node to expand, the system is a graph browser, not a recommender. Maintain a **priority queue of unexpanded nodes** scored by expansion value (anchor proximity, unexplored degree, score). `Expand 1 hop` = pop `k` frontier nodes within the API budget. Manual "expand this node" stays available as an override.

### C3. arXiv subfield filtering does not work the way you assume — this is the biggest factual gap in your plan

**Semantic Scholar does not return the arXiv primary category.** It returns `externalIds.ArXiv` (the bare ID) and `fieldsOfStudy` / `s2FieldsOfStudy`, which are coarse ("Computer Science", "Medicine", "Mathematics"). There is no `cs.LG` in an S2 paper record. Your entire §5 filter design depends on data S2 doesn't give you.

Three options:

| Option | Cost | Verdict |
|---|---|---|
| arXiv API / OAI-PMH per paper | 1 extra HTTP call per paper, rate-limited, another failure mode | No |
| **Bulk arXiv metadata snapshot → local lookup table** | One-time ~4GB download, parse to SQLite (~2.7M rows), then O(1) offline lookups | **Yes, V1** |
| Classify from title+abstract | Fuzzy, needs a model, needs labels | Later, as fallback |

Do the bulk load. Sources: the arXiv metadata dataset on Kaggle (Cornell), or arXiv's OAI-PMH endpoint (`export.arxiv.org/oai2`, `metadataPrefix=arXiv`, `set=cs`) with a resumption-token harvester. Build `arxiv_meta(arxiv_id PK, primary_category, categories, updated)`. Zero rate limit, fully reproducible, and writing the harvester is a real data-engineering artifact for the portfolio.

**Coverage reality:** roughly 70–90% of modern core-ML papers have arXiv IDs; older and non-CS work does not. You need a documented fallback chain, not a single filter (see §E4).

### C4. "Max h-index among authors" is a weak signal with a real cost — demote it

Problems, in order of severity:

1. **It measures a person's career, not this paper.** A first-year PhD student's best paper and their advisor's worst paper get the same author prior.
2. **Last-author effect.** A famous PI is on every paper from a 40-person lab. The signal saturates and stops discriminating.
3. **h-index is monotonically increasing with career age** — so it's partly a proxy for "author is old", which correlates with "paper is old", which you already have.
4. **It costs API calls.** Author h-index needs either nested `authors.hIndex` on the paper request (inflates payload) or separate `/author/{id}` calls. You'd be spending your scarcest resource on your weakest feature.

**Where it actually earns its keep:** cold start. A paper published 6 weeks ago has zero citations and zero graph structure, so every other quality signal reads as "worthless". Author prior is the only evidence available. So: fetch h-index **lazily**, only for papers that already survived filtering and reached the top-N shortlist, and use it **only** in the recency lane and as a tiebreaker. Log-scale and cap it (`min(log1p(h), log1p(60))`).

### C5. Raw citation count must be age- and field-normalized before it enters any score

`citation_count` as a raw feature guarantees your recommender only ever surfaces 2017–2021 papers. For an LLM-research tool that's a fatal product defect.

- **V1:** `log1p(citation_count / max(months_since_pub/12, 0.5))` — citations per year, log-scaled, with a floor so 3-month-old papers don't divide by ~0.
- **V2:** percentile within a `(publication_year, venue_tier)` cohort, computed from your own corpus. Much better; requires enough corpus to build cohorts.
- **Always:** a dedicated recency lane in the budget that raw quality scores cannot compete for (§E5).

### C6. "Orphaned" — your candidate definitions are wrong; use reachability + mark-and-sweep

- `degree == 0` — almost never fires. Removing one node rarely strands a neighbour that has other edges.
- `disconnected component` — closer, but a candidate cluster can stay internally connected while being detached from anything you care about.

**Correct semantics:** a candidate exists *only* because it connects to something you care about. Define

> `anchors = {SEED} ∪ {LIKED}`
> A node is **orphaned** if it is unlabeled and has no path to any anchor in the undirected projection within `max_depth`.

Implement as **mark-and-sweep garbage collection**, not recursive deletion: BFS from the anchor set marking reachable nodes, then sweep unmarked-and-unlabeled nodes. This is O(V+E), terminates by construction, has no recursion-depth risk, and is trivially unit-testable against hand-built graphs. Recursive cascade deletion is the same result with more ways to be wrong.

Two non-negotiable rules:
1. **Never sweep a labeled node.** A LIKED or DISLIKED node stays even if disconnected — the user's judgment outranks graph topology.
2. **Removal must write a tombstone.** Otherwise the next expansion re-adds the paper and the user removes it again forever. This is the bug everyone hits. See §D.

### C7. Edge representation: your Options A/B/C are a false trichotomy

There is **exactly one directed edge per citation pair**: `citing → cited`. "References" and "citations" are *the same edge set traversed in opposite directions*. Storing them as two edge types duplicates every row and creates a consistency bug the first time you fetch a pair from both sides.

What you *do* need stored per edge is **provenance and semantics**, which is different from direction:

```sql
edges(citing_id, cited_id,           -- the edge; PK(citing_id, cited_id)
      discovered_via,                -- BACKWARD | FORWARD | BOTH  (how we found it)
      is_influential,                -- S2 flag
      intents,                       -- ['methodology','background','result']
      first_seen_at)
```

`intents` is underused and valuable: `intents contains 'methodology'` means the citing paper actually *used* the cited work rather than name-dropping it in the intro. That is a much better relevance signal than citation count.

**Consequences of getting this right:**

| Concern | Why direction matters |
|---|---|
| PageRank | Edges point citing→cited, so mass flows *toward* cited papers: PageRank = authority/seminality. Run it on the **reversed** graph and you get hub-ness ≈ survey-ness. Two useful features, one algorithm. Running it undirected destroys both. |
| Traversal | Backward is bounded (~50 refs); forward is unbounded (~10⁵). Completely different cost models and guard rails. |
| Ranking | "Cited by 4 of your liked papers" and "cites 4 of your liked papers" are different recommendations (descendant vs. ancestor). Users want both, labeled. |
| Layout / clustering | Here you *want* the undirected projection. Expose it as a **view**, don't store it. |
| Future GNN | Directed graph + explicit reverse relation = two message-passing channels (R-GCN style). Collapsing to undirected throws that away permanently. |

**Decision: single directed `CITES` edge, with provenance and intent columns. Undirected only as a computed view.**

### C8. Interaction history: use an append-only event log, not `liked_at`/`disliked_at` columns

Columns-per-state doesn't extend (what's the 5th column?), can't answer "did I dislike this before liking it?", and loses the ordering you'll need for learning-to-rank later. An append-only `interaction_events` table plus a materialized current-state row costs you ~20 extra lines and gives you: free undo, free audit trail, free replay-to-rebuild, and free training data at R8. Cheap event sourcing, correctly applied.

### C9. API surface: you're modeling "papers" when you mean "graph membership"

`POST /papers` and `DELETE /papers/{id}` are misleading — you never delete a paper, you remove it from a graph while keeping it in the corpus (you must, or the blocklist can't work). Rename to node/membership operations. Full API in §G.

## REMOVE — do not build these

| Thing | Why not |
|---|---|
| **Neo4j / any graph DB** | At 10³–10⁴ nodes, NetworkX in-process is faster than a network round-trip to Neo4j, and SQLite persists it fine. A reviewer asking "why Neo4j?" and hearing "because it's a graph" is a *negative* signal. If you want to show graph-DB awareness, write a paragraph in `docs/adr/` explaining why you didn't. That reads better than using it. |
| **Celery + Redis (V1)** | Two extra processes and an ops dependency for a single-user app. A `jobs` table in SQLite + one worker thread + polling is ~80 lines and does everything you need. |
| **Spectral clustering** | Leiden dominates it on citation graphs on every axis. Don't offer it as an option. |
| **Docker Compose (V1)** | A `Makefile` and a `uv`/`pip-tools` lockfile give you reproducibility. Add a single Dockerfile at R4 when you want a "clone and run" story. |
| **GNN as a feature** | See §H, R8. Only justified as a *documented experiment*, possibly with a negative result. |
| **A vector database** | SQLite + a NumPy array of 3k × 768 floats is 9MB. Brute-force cosine over 3k vectors is sub-millisecond. Introducing pgvector/Qdrant/Chroma here is pure resume padding and reviewers notice. |

## DEFER — right idea, wrong time

- **Embeddings (R6, not R3).** And reframe the goal: the interesting use isn't "semantic similarity search" — that's a worse Google Scholar. It's **fixing the citation graph's structural blind spot**: concurrent work that is highly relevant but cites nothing in your graph. Hybrid retrieval where embeddings supply candidates the graph *cannot* reach is a defensible contribution. Use SPECTER2 (trained on citation signal, paper-level) rather than a generic sentence encoder — and check whether S2 will just serve you `embedding.specter_v2` as a field, which would make inference cost zero.
- **Learning-to-rank (R8).** The blocker isn't the model, it's label count. You will personally generate maybe 100–300 labels, which is not enough for LambdaMART. **Workaround worth building:** auto-generate training triples from citation data — for a held-out paper P, `(seeds = 3 of P's references, positive = another of P's references, negatives = sampled non-references)`. That yields thousands of triples for free and makes LTR actually viable. This is the same machinery as your eval harness, so build the harness first.
- **PageRank statistics in the stats panel (R3).** See the crawl-bias warning in §I — reporting PageRank before you understand its bias on a partially crawled graph is worse than not reporting it.

## MISSING — gaps that will bite you

### M1. Deduplication / entity resolution
The arXiv preprint and the NeurIPS version of the same paper are frequently **two S2 records with different IDs**, and S2's own data contains genuine duplicates. Untreated, you get twin nodes, split citation counts, and double-counted overlap features. Canonicalization chain: `DOI → arXiv ID → normalize(title) + first_author_surname + year`, with `canonical_paper_id` as a nullable self-FK and edge-merge on union. Needed in **V1** — retrofitting dedup after you have a populated graph is miserable.

### M2. Hub explosion guard
"Attention Is All You Need" has well over 100,000 citations. Forward-expanding it is (a) meaningless — its citations are the entire field — and (b) fatal to your API budget, since the S2 citations endpoint pages at ~1,000 and offers no server-side sort by citation count, so "top citations of a hub" costs 100+ requests. **Rule: never forward-expand a node above `FORWARD_EXPAND_MAX` (default 2,000 citations). Backward expansion is always permitted** (bounded by reference count, capped at 200). This one rule prevents most runaway scenarios.

### M3. API budget accounting, and batch-first client design
The single biggest efficiency lever is `POST /paper/batch` (up to ~500 IDs per request, verify current limit). Most implementations do N single-paper GETs and hit rate limits in minutes. Design the client so that **the only way to fetch paper metadata is through a batching layer** that accumulates IDs and flushes. Second lever: the `fields` parameter — request exactly the fields you need, and use *nested* fields (`references.title,references.citationCount,references.externalIds`) so a single references call returns everything you need to prescore the candidates with **zero additional requests**.

### M4. Immutable raw-response cache
Store every S2 response verbatim, keyed by `(endpoint, sha256(params))`, with `fetched_at`. This is the highest-leverage single decision in the project:
- offline development (unplug the network and keep working);
- deterministic tests (the cache *is* your fixture set);
- reproducibility (re-derive any result from stored bytes);
- free re-normalization when you change your schema — no refetch.

Normalize on read, never on write. Raw stays raw.

### M5. Filter observability
Every rejection writes a row with a **reason code** and is visible in the UI as a "Rejected (N)" drawer with a Restore action. A filter you cannot audit is a filter you cannot tune, and you will silently discard good papers for weeks without noticing. Also: filters return **three** outcomes, not two — `ACCEPT | QUARANTINE | REJECT`. Quarantine is for "probably applied ML but I'm not sure", which is most of the hard cases.

### M6. Layout stability
Re-running a force layout from scratch after every expansion re-scrambles the whole graph and destroys the user's spatial memory. Persist `(x, y)` per node; on expansion, **pin existing nodes and let only new nodes settle** (`fcose` with `fixedNodeConstraint` and `randomize: false`). Run layout in a Web Worker so the UI doesn't freeze.

### M7. Config as a versioned artifact
Ranking weights, filter thresholds, and budgets live in `config/ranking.yaml` with a `config_version` string that gets **stamped into every expansion record and filter decision**. Without this you cannot reproduce yesterday's results, cannot A/B two weight sets, and cannot run a config sweep in the eval harness.

### M8. Determinism
Stable sort keys everywhere (`ORDER BY score DESC, paper_id ASC`), fixed seeds for PPR and layout, no reliance on dict/set iteration order. Non-deterministic recommendations make every bug unreproducible.

### M9. Evaluation comes before personalization
Your roadmap puts evaluation at the end. **This is the ordering I'd argue hardest to change.** You cannot tune like/dislike weighting, or justify PPR over co-citation, or claim any of it works, without a metric and baselines. Move the harness to R4, before personalization. See §I.

---

# B. Recommended architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  FRONTEND — React 18 + TS + Vite                                     │
│                                                                      │
│  GraphCanvas (Cytoscape.js + fcose, Web Worker layout)               │
│  SearchPanel │ NodeInspector │ CandidateList │ StatsPanel            │
│  ExpansionControls │ RejectedDrawer                                  │
│                                                                      │
│  TanStack Query (server state)  ·  Zustand (view state: selection,   │
│                                     highlight, filters, layout)      │
└─────────────────────────────┬────────────────────────────────────────┘
                              │ REST + JSON (polling for jobs)
┌─────────────────────────────▼────────────────────────────────────────┐
│  BACKEND — FastAPI (single process, uvicorn) + 1 worker thread       │
│                                                                      │
│  api/          thin routers, Pydantic v2 schemas, no logic           │
│  ├─ graph.py   nodes, edges, membership, GC                          │
│  ├─ search.py  S2 title search passthrough                           │
│  ├─ expand.py  enqueue + poll expansion jobs                         │
│  └─ stats.py   metrics                                               │
│                                                                      │
│  services/     ── the actual system ──                               │
│  ├─ frontier.py     which nodes to expand next                       │
│  ├─ candidates.py   pooled candidate generation + prescore           │
│  ├─ filters/        cascade: type → topic → applied  (3 outcomes)    │
│  ├─ dedup.py        canonicalization + node merge                    │
│  ├─ ranking.py      feature build → rank-normalize → weighted score  │
│  ├─ graphops.py     NetworkX: PageRank, PPR, co-cite, bib-couple, GC │
│  └─ layout.py       Leiden communities → cluster hints               │
│                                                                      │
│  clients/                                                            │
│  ├─ s2.py       batch-first, token-bucket, tenacity retries, cache   │
│  └─ arxiv.py    bulk harvester (offline, run once)                   │
│                                                                      │
│  jobs.py        SQLite-backed queue, one worker thread, poll status   │
│  repo/          SQLAlchemy Core; the ONLY place SQL lives            │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
┌─────────────────────────────▼────────────────────────────────────────┐
│  SQLite (WAL mode)  ·  app.db                                        │
│    papers · authors · paper_authors · edges                          │
│    graph_nodes · interaction_events · filter_decisions               │
│    expansions · jobs · api_cache · arxiv_meta                        │
│                                                                      │
│  In-process NetworkX DiGraph, rebuilt from SQL on mutation           │
│  (at 3k nodes this is ~10ms; do not build a cache-invalidation       │
│   scheme until you have measured that it's needed)                   │
└──────────────────────────────────────────────────────────────────────┘
```

**Stack, with justification for each entry:**

| Layer | Choice | Why not the alternative |
|---|---|---|
| Backend | FastAPI + Pydantic v2 | Async matters (S2 I/O-bound), free OpenAPI docs = free API documentation deliverable |
| DB | SQLite, WAL | Single file, zero ops, ACID, inspectable with any tool. Postgres buys nothing at this scale. |
| DB access | SQLAlchemy **Core** + Alembic | Core not ORM: your queries are graph-shaped and set-based, the ORM will fight you. Alembic because you *will* change the schema every release. |
| Graph algos | NetworkX | Battle-tested, readable, correct. Switch to `igraph`/`graph-tool` only if profiling shows PageRank is your bottleneck (it won't be — S2 latency is). |
| HTTP | `httpx` + `tenacity` | Async, and tenacity gives declarative retry/backoff you can test. |
| Jobs | SQLite `jobs` table + one worker thread | Celery+Redis is two processes for one user. |
| Frontend | React + TS + Vite | — |
| Graph viz | **Cytoscape.js + fcose** | See §F for the full comparison |
| Server state | TanStack Query | Caching, refetch, mutation invalidation — all things you'd otherwise hand-roll |
| View state | Zustand | Selection/highlight changes at 60fps; Redux ceremony is unwarranted; do **not** put the graph itself in either — it lives in Cytoscape |
| Logging | `structlog`, JSON to file | Grep-able request/expansion traces |

**Two hard architectural rules that make later stages cheap:**

1. **Features are computed and persisted; scores are derived on read.** Store `node_features(paper_id, feature_name, value)` (or a JSON blob) and compute `score` in a pure function of `(features, weights)`. This means changing weights re-ranks instantly with **zero API calls and zero recomputation** — which is what makes a config sweep in the eval harness possible at all.
2. **Nothing above `repo/` writes SQL, and nothing below `api/` knows about HTTP.** The services layer must be callable from a CLI, because the CLI is how you'll run evaluation and debug expansion for the entire life of the project.

---

# C. Data model

```sql
-- ═══ CORPUS: every paper ever seen, whether or not it's in a graph ═══

CREATE TABLE papers (
  id                    INTEGER PRIMARY KEY,        -- internal surrogate (survives merges)
  s2_paper_id           TEXT UNIQUE NOT NULL,
  s2_corpus_id          INTEGER,
  canonical_paper_id    INTEGER REFERENCES papers(id),  -- NULL = is canonical; else merged into

  title                 TEXT NOT NULL,
  title_norm            TEXT NOT NULL,              -- lowercase, punct-stripped, ws-collapsed
  abstract              TEXT,
  year                  INTEGER,
  publication_date      TEXT,                        -- ISO; often NULL, hence year
  venue                 TEXT,
  venue_tier            TEXT,                        -- A_STAR|A|OTHER|PREPRINT (from allowlist)

  citation_count        INTEGER DEFAULT 0,
  reference_count       INTEGER DEFAULT 0,
  influential_citation_count INTEGER DEFAULT 0,

  doi                   TEXT,
  arxiv_id              TEXT,
  primary_arxiv_category TEXT,                       -- joined from arxiv_meta
  arxiv_categories      TEXT,                        -- JSON array
  s2_fields             TEXT,                        -- JSON, S2's coarse labels
  publication_types     TEXT,                        -- JSON, e.g. ["Review"]

  paper_type            TEXT NOT NULL DEFAULT 'UNKNOWN',
                        -- RESEARCH|SURVEY|DATASET|BENCHMARK|POSITION|UNKNOWN
  crawl_state           TEXT NOT NULL DEFAULT 'STUB',
                        -- STUB (title+id only, from a nested edge fetch)
                        -- METADATA (full record fetched)
                        -- REFS_DONE | CITES_DONE | EXPANDED
  first_seen_at         TEXT NOT NULL,
  metadata_fetched_at   TEXT
);
CREATE INDEX ix_papers_title_norm ON papers(title_norm);
CREATE INDEX ix_papers_arxiv ON papers(arxiv_id);
CREATE INDEX ix_papers_doi ON papers(doi);

CREATE TABLE authors (
  id            INTEGER PRIMARY KEY,
  s2_author_id  TEXT UNIQUE NOT NULL,
  name          TEXT NOT NULL,
  h_index       INTEGER,                 -- NULL until lazily fetched (see C4)
  citation_count INTEGER,
  paper_count   INTEGER,
  fetched_at    TEXT
);

CREATE TABLE paper_authors (
  paper_id  INTEGER NOT NULL REFERENCES papers(id),
  author_id INTEGER NOT NULL REFERENCES authors(id),
  position  INTEGER NOT NULL,            -- 0-based; first & last author matter
  PRIMARY KEY (paper_id, author_id)
);

-- ═══ EDGES: one row per citation pair, direction = citing -> cited ═══

CREATE TABLE edges (
  citing_id      INTEGER NOT NULL REFERENCES papers(id),
  cited_id       INTEGER NOT NULL REFERENCES papers(id),
  discovered_via TEXT NOT NULL,          -- BACKWARD|FORWARD|BOTH
  is_influential INTEGER DEFAULT 0,
  intents        TEXT,                   -- JSON ["methodology","background","result"]
  first_seen_at  TEXT NOT NULL,
  PRIMARY KEY (citing_id, cited_id),
  CHECK (citing_id != cited_id)
);
CREATE INDEX ix_edges_cited ON edges(cited_id);   -- reverse traversal

-- ═══ GRAPH: the visible working set. A paper may be in papers but not here. ═══

CREATE TABLE graph_nodes (
  session_id      INTEGER NOT NULL REFERENCES sessions(id),
  paper_id        INTEGER NOT NULL REFERENCES papers(id),
  state           TEXT NOT NULL,          -- SEED|CANDIDATE|LIKED|DISLIKED
  score           REAL,
  features        TEXT,                   -- JSON: raw + normalized features
  score_breakdown TEXT,                   -- JSON: per-term contributions (powers "why?")
  depth           INTEGER NOT NULL,       -- hops from nearest anchor at insert time
  added_by        INTEGER REFERENCES expansions(id),
  pos_x           REAL,
  pos_y           REAL,                   -- persisted layout (M6)
  community_id    INTEGER,                -- Leiden, R6
  PRIMARY KEY (session_id, paper_id)
);
CREATE INDEX ix_graph_state ON graph_nodes(session_id, state);

-- ═══ HISTORY: append-only. graph_nodes.state is the materialized projection. ═══

CREATE TABLE interaction_events (
  id         INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL,
  paper_id   INTEGER NOT NULL REFERENCES papers(id),
  event_type TEXT NOT NULL,
             -- SEED_ADDED|LIKED|DISLIKED|UNLABELED|REMOVED|RESTORED|GC_SWEPT
  actor      TEXT NOT NULL DEFAULT 'USER',   -- USER|SYSTEM (GC writes SYSTEM)
  payload    TEXT,                            -- JSON: cascade counts, prior state
  created_at TEXT NOT NULL
);
CREATE INDEX ix_events_paper ON interaction_events(session_id, paper_id, id);

-- REMOVED tombstone = latest event for (session, paper) is REMOVED.
-- Expansion MUST left-join this and exclude. Nothing silently re-enters the graph.

-- ═══ OBSERVABILITY ═══

CREATE TABLE filter_decisions (
  id             INTEGER PRIMARY KEY,
  paper_id       INTEGER NOT NULL REFERENCES papers(id),
  session_id     INTEGER,
  outcome        TEXT NOT NULL,       -- ACCEPT|QUARANTINE|REJECT
  stage          TEXT NOT NULL,       -- TYPE|TOPIC|APPLIED|DUPLICATE
  reason_code    TEXT NOT NULL,       -- IS_DATASET, LOW_CITE_SURVEY, CAT_NOT_ALLOWED, ...
  details        TEXT,                -- JSON: matched category, threshold, score
  config_version TEXT NOT NULL,
  decided_at     TEXT NOT NULL
);

CREATE TABLE expansions (
  id             INTEGER PRIMARY KEY,
  session_id     INTEGER NOT NULL,
  status         TEXT NOT NULL,       -- QUEUED|RUNNING|DONE|FAILED|CANCELLED
  params         TEXT NOT NULL,       -- JSON: budget, hops, direction floors, anchors
  config_version TEXT NOT NULL,
  n_pool         INTEGER, n_filtered INTEGER, n_added INTEGER,
  api_calls      INTEGER, cache_hits INTEGER,
  error          TEXT,
  started_at TEXT, finished_at TEXT
);

CREATE TABLE api_cache (
  id          INTEGER PRIMARY KEY,
  endpoint    TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  response    TEXT NOT NULL,          -- verbatim JSON, never normalized (M4)
  fetched_at  TEXT NOT NULL,
  UNIQUE (endpoint, params_hash)
);

CREATE TABLE arxiv_meta (             -- bulk-loaded once, offline (C3)
  arxiv_id         TEXT PRIMARY KEY,
  primary_category TEXT NOT NULL,
  categories       TEXT NOT NULL,     -- JSON array
  updated          TEXT
);

CREATE TABLE sessions (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
);
```

**Design notes.**

- **Internal integer PK + `s2_paper_id` unique.** Dedup merges rewrite `canonical_paper_id`; a surrogate key means edges and events don't have to be rewritten in lockstep.
- **`papers` vs `graph_nodes` is the answer to "can a paper exist without being in the graph?"** Yes — and it's essential. Rejected papers, removed papers, and stubs discovered via nested edge fetches all live in `papers`. Only `graph_nodes` rows are drawn.
- **`crawl_state` is what makes partial crawling honest.** A `STUB` paper has a title and nothing else. Never compute features on a stub, and never report PageRank for a node whose neighbours are stubs (§I).
- **Materialized state + event log.** `graph_nodes.state` is a cache of "replay events for this paper". Write both in one transaction. Add a `scripts/rebuild_state.py` that reconstructs `graph_nodes.state` purely from events — it's 30 lines and it's your safety net.

---

# D. Graph model and state machine

## Definitions (write these into `docs/graph-model.md` verbatim)

| Term | Definition |
|---|---|
| **Node** | A canonical paper (`papers` row with `canonical_paper_id IS NULL`). |
| **Edge** | A directed `CITES` relation, `citing → cited`. One row per pair. |
| **References of P** | `{c : edge(P, c)}` — outgoing. Bounded, curated, older-biased. |
| **Citations of P** | `{c : edge(c, P)}` — incoming. Unbounded, uncurated, newer-biased. |
| **Same relation?** | Yes. Same edge set, opposite traversal. Direction is stored once; *traversal* direction is expansion metadata. |
| **Discovered** | Exists in `papers` (possibly as a STUB). |
| **Candidate** | Passed filters, scored, in `graph_nodes` with `state = CANDIDATE`. |
| **Rejected** | Has a `filter_decisions` row with `outcome = REJECT`. Stays in `papers`, absent from `graph_nodes`. |
| **Quarantined** | `outcome = QUARANTINE`. Absent from graph, listed in the review drawer. |
| **Removal** | Delete the `graph_nodes` row, append a `REMOVED` event. Paper and edges persist in the corpus. |
| **Orphaned** | Unlabeled, and no path to `anchors = SEED ∪ LIKED` in the undirected projection within `max_depth`. |
| **Anchor** | A SEED or LIKED node. Anchors justify the existence of candidates and seed PPR. |

## Two orthogonal state axes

Collapsing these into one enum is why your proposed state list has overlaps (`UNLABELED` vs `POTENTIAL_CANDIDATE`) and category errors (`FILTERED` is not a user label).

```
CORPUS STATE  (system-owned, per paper, global)
  STUB ──fetch──▶ METADATA ──filter──┬──▶ ELIGIBLE
                                     ├──▶ QUARANTINED ──(user review)──▶ ELIGIBLE | REJECTED
                                     └──▶ REJECTED    ──(user restore)──▶ ELIGIBLE

GRAPH STATE   (user-owned, per (session, paper); only ELIGIBLE papers may enter)
                     ┌──────────────────────────────┐
   user adds ───────▶│            SEED              │──remove──▶ (out, tombstoned)
   by title          └──────────────────────────────┘
                     ▲ no like/dislike allowed → 409

   expansion ──────▶ CANDIDATE ──like────▶ LIKED ──unlike──▶ CANDIDATE
                        │  ▲               │   │
                        │  └──un-dislike───┘   └──dislike──▶ DISLIKED
                        │                                        │
                     dislike                                  remove
                        │                                        │
                        ▼                                        ▼
                    DISLIKED ──remove──▶ (out, tombstoned) ◀─────┘

   (out, tombstoned) ──user Restore only──▶ CANDIDATE
                      never re-added automatically
```

## Rules

1. **SEED accepts only `remove`.** `POST /nodes/{id}/like` on a seed → `409 SEED_NOT_LABELABLE`. Your rule, encoded.
2. **DISLIKED nodes stay in the graph.** They carry signal (negative PPR anchor, repulsion in layout) and hiding them means you'd refetch them. Render them dimmed, not deleted.
3. **REMOVED is a tombstone.** Expansion excludes any paper whose latest event is `REMOVED`. Re-discovery surfaces it in a "Previously removed (N)" list with a Restore button. **The system never auto-resurrects a removed paper** — that's the answer to your §14 question, and it's the difference between a tool that respects the user and one that argues with them.
4. **DISLIKED → LIKED is legal and requires no ceremony.** Un-disliking is the answer to "what if a disliked paper becomes relevant again" for papers still in the graph; Restore covers the removed case.
5. **Every state change triggers a defined recomputation:**

   | Transition | Side effects |
   |---|---|
   | `SEED_ADDED` | fetch metadata, enqueue expansion, node becomes anchor, rescore all |
   | `→ LIKED` | node becomes anchor, add to PPR restart set, add to frontier, rescore all |
   | `→ DISLIKED` | add to negative set, remove from frontier, rescore all |
   | `→ CANDIDATE` (from LIKED) | drop from anchors, **run GC** (its dependents may now be orphans) |
   | `REMOVED` | write tombstone, **run GC**, then recompute layout with survivors pinned |

6. **GC never sweeps a labeled node**, and every sweep writes `GC_SWEPT` events with `actor = SYSTEM` so the cascade is auditable and undoable.
7. **GC previews before it commits.** `DELETE /nodes/{id}?dry_run=true` returns `{would_remove: [...], count: 34}`. The UI shows "This will also remove 34 orphaned candidates. [Cancel] [Remove]". Deleting a third of someone's graph without warning is unacceptable, and the dry-run endpoint is also how you test the GC.

## Paper type: attribute, not node type

Use `paper_type` as an attribute. Surveys and datasets participate in the citation graph identically — a survey cites and is cited by exactly the same mechanism. A separate node type would fork every traversal, every query, and every layout rule to buy nothing. Type drives **policy** (filter thresholds) and **rendering** (shape/badge), which is all you need.

```yaml
# config/filters.yaml
paper_type_policy:
  RESEARCH:  { action: accept }
  SURVEY:    { action: accept_if, citation_count_min: 200, or_citations_per_year_min: 40 }
  BENCHMARK: { action: quarantine }     # sometimes a real methods contribution
  DATASET:   { action: reject }
  POSITION:  { action: quarantine }
```

Note `SURVEY` uses `citation_count OR citations_per_year` — a 2024 survey with 90 citations is more important than a 2016 survey with 210, and a pure absolute threshold silently excludes every recent survey.

---

# E. Recommendation pipeline

Your pipeline is roughly right. Four changes: **dedup moves before filtering** (don't spend classifier work on a paper you already have), **budget allocation becomes an explicit stage**, **scoring splits into two tiers**, and **frontier selection is added at the front**.

```
                        ┌──────────────────────────────────┐
                        │  TRIGGER                         │
                        │  seed added │ "Expand 1 hop"     │
                        │             │ label changed      │
                        └───────────────┬──────────────────┘
                                        ▼
  ① FRONTIER SELECTION ──── pick k unexpanded nodes by expansion value
     score = anchor_proximity · unexplored_degree · node_score
     GUARD: skip forward-expansion if citation_count > 2000     [M2]
                                        ▼
  ② EDGE FETCH ──────────── batched S2 calls, cache-first
     /paper/{id}/references?fields=title,year,citationCount,externalIds,
                                   publicationTypes,authors,intents,isInfluential
     /paper/{id}/citations?<same>&limit=1000
     → nested fields mean candidates arrive pre-populated: 0 extra calls   [M3]
                                        ▼
  ③ POOL ────────────────── UNION all candidates from ALL frontier nodes
     ★ single pool is what makes anchor_overlap computable            [C1]
                                        ▼
  ④ DEDUP / EXCLUDE ─────── canonicalize (DOI→arXiv→title_norm+author+year)
     drop: already in graph │ REMOVED tombstone │ previously REJECTED
     merge duplicates, union their edges                              [M1]
                                        ▼
  ⑤ FILTER CASCADE ──────── 4 stages, 3 outcomes, every decision logged
     ├─ TYPE     dataset/survey/benchmark policy
     ├─ TOPIC    arXiv primary category allowlist (+ fallback chain)
     ├─ APPLIED  core-vs-applied heuristic → mostly QUARANTINE
     └─ SANITY   no title, no year, retracted
                          ACCEPT ──▶ ⑥      QUARANTINE ──▶ review drawer
                          REJECT ──▶ filter_decisions                 [M5]
                                        ▼
  ⑥ PRESCORE ───────────── cheap, metadata-only, prunes pool to ~3×budget
     no API calls; this is what keeps expansion affordable
                                        ▼
  ⑦ ENRICH ─────────────── POST /paper/batch for survivors only (≤500/req)
     + lazy author h-index for the top shortlist only               [C4]
                                        ▼
  ⑧ FEATURE BUILD ───────── graph features need the candidates provisionally
     inserted; compute on a scratch graph, persist to node_features
                                        ▼
  ⑨ RANK ────────────────── rank-normalize each feature → weighted sum
     persist score_breakdown for "why?"                             [B-rule-1]
                                        ▼
  ⑩ BUDGET ALLOCATION ──── recency lane 15% │ direction floors 20/20
     per-source cap 40% │ remainder by score desc, tie-break paper_id
                                        ▼
  ⑪ COMMIT ──────────────── one transaction: insert graph_nodes, edges,
     events, expansion record. Update crawl_state.
                                        ▼
  ⑫ POST-COMMIT ─────────── GC sweep · Leiden communities (R6) ·
     incremental layout with existing nodes pinned                   [M6]
                                        ▼
  ⑬ USER FEEDBACK ───────── like/dislike/remove → back to ①
```

## E1. Frontier selection (①)

```python
def expansion_value(node) -> float:
    return (
        1.0 / (1 + node.depth_from_anchor)          # near anchors first
        * (1 + 0.5 * node.is_anchor)                # anchors first of all
        * unexplored_fraction(node)                 # don't re-expand exhausted nodes
        * (0.3 + 0.7 * node.score_percentile)       # prefer good nodes
    )
```
`unexplored_fraction` uses `crawl_state`: `EXPANDED` → 0, `REFS_DONE` → 0.5, `METADATA` → 1.0. This makes repeated "Expand 1 hop" clicks naturally walk outward instead of re-fetching the same nodes.

## E2. Prescore (⑥) — no API calls

Everything here is available from the nested fields returned in ②.

```python
prescore = (
    2.0  * anchor_overlap                # ★ dominant. # of distinct anchors linking to it
  + 0.8  * (1 if 'methodology' in intents else 0)
  + 0.5  * is_influential
  + 0.4  * norm(log1p(citations_per_year))
  + 0.3  * recency_bonus                 # only if age < 18 months
  - 0.6  * hub_penalty                   # log-scaled above 2000 citations
)
```

`anchor_overlap` is the whole reason for pooling. A paper cited by three of your liked papers is a near-certain keeper regardless of its citation count; a paper cited by one seed and nothing else is a coin flip. **No metadata feature comes close to this.** If you build only one feature, build this one.

## E3. Candidate generation ≠ ranking: the two must use different signals

This is the separation you asked for, made concrete. Generation is about *reachability*; ranking is about *desirability*. Using the same signal for both collapses the distinction and biases you toward whatever generation already favours.

| Signal | Generation | Ranking | Note |
|---|:--:|:--:|---|
| Graph adjacency to frontier | ✅ core | — | defines the pool; useless as a ranker (everything in the pool has it) |
| `anchor_overlap` | ✅ prescore | ✅ strong | the one signal legitimately used in both |
| Citation intent (`methodology`) | ✅ | ✅ | |
| `is_influential` | ✅ | ✅ | |
| citations/year | ✅ weak | ✅ | age-normalized only |
| Personalized PageRank | — | ✅ **primary, R5** | needs the graph built; can't generate |
| Co-citation / bib-coupling | ✅ 2-hop gen, R4 | ✅ | |
| Recency | ✅ lane quota | ✅ | |
| Author h-index | ❌ never | ✅ weak, cold-start only | too expensive for generation |
| Embedding similarity | ✅ **R6 — reaches what the graph cannot** | ✅ | the real reason to add embeddings |
| Dislike proximity | ❌ | ✅ penalty | |
| Venue tier | ❌ | ✅ weak prior | |

## E4. Filter cascade (⑤) — fallback chain, because category data is incomplete

```
STAGE 0 · ERA             (cheapest possible check — run it first)
  publication year < YEAR_FLOOR (2015)              → REJECT   PRE_ERA
      ↳ CRITICAL: reject from the *graph*, not from the *corpus*.
        The paper row and its edges are still stored. See "On the year floor" below.

STAGE 1 · TYPE            (cheap, deterministic, from publication_types + title)
  publication_types ∋ 'Dataset'                      → REJECT   IS_DATASET
  title ~ /^(a )?(survey|review|overview)\b/i
    or publication_types ∋ 'Review'                  → apply survey policy
  title ~ /\b(benchmark|dataset|test ?suite)\b/i     → QUARANTINE  MAYBE_BENCHMARK
      ↳ soft only: "Benchmarking LLM reasoning" may be a real contribution

STAGE 2 · TOPIC           (fallback chain — first hit wins, record which fired)
  0. reaction-ML vocabulary hit, and not already in a core category
       retrosynthesis · reaction/reactivity/yield prediction ·
       synthesis planning · reaction outcome · catalyst design
                                                     → pass  (REACTION_ML)
       ↳ runs FIRST. The papers it rescues are denied by category at (a),
         so a check that runs after (a) has nothing left to save.
         See "On the reaction-ML corridor" below.
  a. arxiv_meta.primary_category present?
       ∈ CORE_ALLOW  → pass  (CAT_PRIMARY_CORE)
       ∈ APPLIED_DENY→ REJECT (CAT_PRIMARY_APPLIED)
       else          → QUARANTINE
  b. no arXiv ID, but any category ∈ CORE_ALLOW      → pass, lower confidence
  c. no arXiv at all → venue_tier ∈ CORE_VENUES      → pass  (VENUE_CORE)
        NeurIPS ICML ICLR ACL EMNLP NAACL AAAI IJCAI COLT AISTATS TMLR JMLR COLM
  d. s2_fields = ['Computer Science'] only           → QUARANTINE
  e. s2_fields ∋ Medicine|Biology|Chemistry|Finance
        and ∌ Computer Science                       → REJECT (FIELD_NON_CS)
  f. otherwise                                       → QUARANTINE

STAGE 3 · APPLIED         (the genuinely hard one — see below)

STAGE 4 · SANITY
  title empty │ year NULL and date NULL │ retracted   → REJECT
```

### On the reaction-ML corridor

The wanted set here is an **intersection, not a category**, which is why it needs
its own rule rather than an edit to `CORE_ALLOW` or `APPLIED_DENY`.

"Every `physics.chem-ph` paper" is far too wide — most of that category is
spectroscopy and electronic structure, nothing to do with this tool. "Only
`cs.LG`" is too narrow, because the retrosynthesis literature publishes into
chemistry venues. What identifies the field is its **vocabulary**: a paper about
retrosynthesis or reaction-yield prediction is essentially always a
machine-learning paper. The terms *are* the intersection.

**Precision is the risk, not recall.** A list that is too eager admits all of
computational chemistry through a door meant for a corridor of it, and the
symptom — a graph slowly filling with papers nobody wanted — is slow and hard to
attribute. `reaction_ml_keywords` therefore excludes bare "reaction" and bare
"prediction"; half of `test_reaction_ml_rescue.py` asserts what must stay *out*.

Two consequences worth stating:

- **A core-category paper keeps its own reason code.** A `cs.LG` retrosynthesis
  paper was already being admitted correctly as `CAT_PRIMARY_CORE`. Re-labelling
  it `REACTION_ML` would rewrite history in the review drawer, which groups by
  reason code.
- **`applied_keywords` lost `molecul`, `protein folding` and `drug discovery`**
  when this landed — the same vocabulary this rule admits. The two lists would
  have pulled in opposite directions the moment STAGE 3 was actually built. The
  clinical, financial and agricultural guards stay; those are a different
  question from chemistry.

```yaml
# config/filters.yaml
year_floor: 2015           # STAGE 0. Configurable; 2015 per your decision.

CORE_ALLOW:  [cs.LG, cs.AI, cs.CL, cs.NE, stat.ML, cs.MA]
BORDERLINE:  [cs.IR, cs.CY, cs.DS, math.OC]          # → QUARANTINE, user decides
APPLIED_DENY:[cs.CV, cs.RO, cs.SE, cs.HC, cs.CR, cs.DB, cs.NI, cs.SD,
              q-bio.*, q-fin.*, eess.*, physics.*, econ.*, astro-ph.*]
```

**On cs.CL:** keep it in CORE_ALLOW. A large share of foundational LLM work is primary cs.CL; excluding it would gut the corpus.

**On cs.CV (now denied):** this costs you real papers — ViT, CLIP, diffusion models, and most multimodal LLM work are primary cs.CV, and they are methodologically core. Two things soften it:

- **Primary-category-wins still applies.** A paper that is primary `cs.LG` and cross-listed `cs.CV` passes. In practice much methodological vision work is primary cs.LG, so you lose less than the deny list suggests.
- **The loss is visible, not silent.** Every rejection lands in the review drawer with `reason_code = CAT_PRIMARY_APPLIED`. If after a few weeks you find yourself restoring cs.CV papers repeatedly, move it to BORDERLINE — it's a one-line YAML change, and the drawer is the evidence that tells you to make it.

**On the year floor — the one part that needs care.** Rejecting pre-2015 papers from the *graph* is straightforward. Rejecting them from the *corpus* would quietly break your best feature.

Bibliographic coupling measures shared references. Two 2023 papers that both cite Kingma & Ba's Adam (2014) and both cite Hochreiter's LSTM (1997) are coupled through those old papers. If you never store the old papers or their edges, that coupling is invisible and `bib_coupling` degrades badly on exactly the pairs you most want to link.

So the year floor is a **graph-admission** rule, not an ingestion rule:

| Layer | Pre-2015 paper |
|---|---|
| `papers` row | **stored** (as STUB — title, year, id; no full metadata fetch) |
| `edges` rows | **stored**, both directions |
| `graph_nodes` row | **never created** |
| Rendered in the UI | no |
| Usable for bib-coupling / co-citation | **yes** — this is the whole point |
| Expanded from (frontier) | no — never spend API budget on them |

Call these **boundary papers** in the code and docs. They're cheap (one row, no metadata call) and they preserve the structural signal. Concretely: when you fetch references and hit a pre-2015 paper, upsert it with `crawl_state='STUB'`, write the edge, log a `PRE_ERA` filter decision, and move on.

Two consequences worth knowing:

1. **Backward expansion gets less productive as you go deeper.** A 2018 paper's reference list is heavily pre-2015, so a backward hop from it may yield only 3–4 admissible candidates out of 40. The direction floors in §E5 handle this gracefully — the ranker naturally shifts budget forward — but don't be surprised when backward yields look thin.
2. **Your corpus is now firmly "the deep-learning era".** That's a coherent, defensible scope. Say so in the README rather than letting a reviewer discover it as an apparent gap: *"scoped to 2015+ literature; pre-2015 work is retained as citation-structure evidence but not recommended."*

**Stage 3 is the part that cannot be solved with metadata.** A paper can be primary cs.LG and still be "fine-tuning an LLM for radiology triage". Category data is blind to this because the *method* is core and the *contribution* is applied.

- **V1:** keyword veto list on title+abstract (`clinical, patient, radiolog, EHR, crop, portfolio, stock return, seismic, protein folding, molecul, wafer, churn`) → **QUARANTINE, never auto-REJECT**. A veto hit means "look at this", not "delete this". Precision here is genuinely bad and pretending otherwise will lose you good papers.
- **R6:** SPECTER2 embeddings + a labeled exemplar set (~30 core, ~30 applied), score by centroid distance margin. Auto-accept/reject only outside the margin; quarantine inside it.
- **Optional R7:** one cached LLM call per uncertain paper, structured output `{is_core_contribution: bool, confidence: float, reason: str}`. Batched, cached by paper_id, cost is pennies. Treat the output as advisory input to the quarantine queue, not as a decision.

Never let stage 3 auto-reject in any version. It is the stage most likely to be wrong and it fails silently.

## E5. Budget allocation (⑩) — the anti-explosion mechanism

```python
def allocate(pool, budget=20):
    recency_slots = max(1, int(0.15 * budget))     # age < 12mo, else raw quality wins forever
    picked = top_by_score([p for p in pool if p.age_months < 12], recency_slots)

    remaining = budget - len(picked)
    # floors, not fixed splits: guarantee both directions are represented,
    # but let the ranker win the majority
    for direction in ('BACKWARD', 'FORWARD'):
        floor = int(0.20 * remaining)
        picked += top_by_score(pool_for(direction) - picked, floor)

    picked += top_by_score(pool - picked, budget - len(picked))

    # diversity: no single frontier node may supply >40% of the batch
    return enforce_source_cap(picked, cap=0.40 * budget)
```

Floors answer your question "what if one direction produces mostly irrelevant papers": the ranker naturally starves it, but the floor keeps a minimum so a temporarily-bad direction can recover. Fixed 50/50 splits cannot adapt; pure ranking can collapse to one direction permanently.

**Every explosion-prevention mechanism, and when it lands:**

| Mechanism | Release | Notes |
|---|:--:|---|
| Corpus year floor (2015) | R1 | prunes a large fraction of every reference list before any other work |
| Max new nodes per expansion (user-set) | R1 | the primary control |
| Max total graph nodes (hard cap ~2,000) | R1 | refuse expansion past it with a clear error |
| Deduplication | R1 | prevents twin-node inflation |
| REMOVED / REJECTED exclusion | R2 | prevents re-add churn |
| Hub forward-expansion guard | R1 | **the single most important one** |
| Corpus/graph separation | R1 | fetching ≠ displaying |
| Max depth from anchor (default 3) | R2 | |
| Score threshold floor | R3 | add nothing below score X even if budget remains |
| Per-source diversity cap | R3 | |
| GC on removal | R2 | shrinks as well as grows |
| API call budget per expansion | R1 | hard stop; partial results are fine, log the truncation |

## E6. Sync vs async

| Operation | Mode | Rationale |
|---|---|---|
| Title search | sync | one API call, <1s |
| Add seed | sync fetch + async expand | user must see the node appear immediately |
| Expand 1 hop | **async job** | 5–60s depending on budget; blocking the UI here is unacceptable |
| Like/dislike | sync write + async rescore | write must be instant; rescoring 2k nodes takes ~200ms so it can also be sync at V1 |
| Remove + GC | sync | must be atomic and immediately visible |
| Stats | sync, cached 5s | |

**Job mechanics (deliberately boring):** `POST /expansions` inserts a `jobs` row and returns `202` with a job id. One background worker thread polls `SELECT * FROM jobs WHERE status='QUEUED' ORDER BY id LIMIT 1`. Frontend polls `GET /expansions/{id}` every 1s and shows `{stage: 'FILTERING', pool: 340, added: 0}`. Upgrade to SSE at R5 if the polling bothers you. Do not install Redis for this.

## E7. Failure handling for S2

| Failure | Handling |
|---|---|
| `429` | token-bucket limiter (~1 req/s, configurable) + exponential backoff with jitter, honour `Retry-After`. Never retry more than 5×. |
| `5xx` | retry 3× with backoff, then mark the *sub-task* failed and continue. **A partial expansion is a success**: commit what you got, set `expansions.error`, and show "added 14 of 20 (3 fetches failed)". Never lose the whole batch to one bad node. |
| `404` / null paper | mark `papers.crawl_state='STUB'`, `metadata_fetched_at=now`, don't retry for 30 days. S2 has genuine gaps. |
| Timeout | 10s connect / 30s read; count as 5xx |
| Malformed field / schema drift | Pydantic model with **all fields optional**. A missing field must degrade the score, not raise. Log a `SCHEMA_DRIFT` warning with the paper id. |
| Whole API down | everything served from `api_cache` still works. The graph is readable and labelable offline; only expansion fails, with a clear banner. |

## E8. Ranking (⑨)

**Normalize by rank-percentile within the pool, not z-score.** Citation counts are power-law distributed; one hub gives a z-score of 40 and flattens every other feature to noise. Percentile rank is outlier-immune and makes weights directly interpretable as relative importance.

```python
def score(f, w):                       # f = features dict, w = weights from YAML
    pos = (w.ppr      * f['ppr_liked_pct']          # R5
         + w.cocite   * f['cocitation_pct']         # R4
         + w.bibcoup  * f['bib_coupling_pct']       # R4
         + w.overlap  * f['anchor_overlap_pct']     # R1
         + w.quality  * f['cites_per_year_pct']     # R1
         + w.recency  * f['recency_pct']            # R1
         + w.venue    * f['venue_tier_score']       # R3
         + w.author   * f['author_prior_pct'])      # R3, cold-start only
    neg = (w.dislike  * f['dislike_proximity_pct']  # R5
         + w.hub      * f['hub_penalty'])           # R1
    return pos - neg
```

Persist `score_breakdown = {term: contribution}`. This gives you honest explanations at zero cost:

> *"Cited by 3 papers you liked (+0.42), high methodology-citation rate (+0.18), published 2024 (+0.09), 1 hop from seed."*

That is a better explanation than an LLM would produce, because it's *true*. Build this at R3 and treat the LLM version at R7 as prose polish over the same numbers — never as the source of the explanation.

---

# F. UI architecture

## Graph library choice

| Library | Strengths | Why not for V1 |
|---|---|---|
| **React Flow** | Beautiful node-based editors | Wrong tool. DOM-per-node, dies around 500 nodes, no graph algorithms, no automatic layout worth using. It's for building Zapier, not analysing networks. |
| **D3-force** | Total control, custom forces | You reimplement hit-testing, selection, styling, zoom semantics, and neighbourhood queries. Weeks of work you'd be redoing badly. |
| **Sigma.js + Graphology** | WebGL, comfortable at 50k+ nodes; Graphology has Louvain and metrics built in | More assembly required; you don't need 50k. **Keep as the escape hatch.** |
| **Cytoscape.js + fcose** | Purpose-built for exactly this (bio/citation networks). Canvas renderer to ~10k. CSS-like selector styling driven straight off node state. `.closedNeighborhood()` makes your highlight feature one line. `fcose` supports `fixedNodeConstraint` — which is what makes stable incremental layout possible. Compound nodes give you cluster hulls for free at R6. | Slightly dated API. Irrelevant. |

**Pick Cytoscape.js + fcose.** It is the only option where every one of your six UI requirements maps to a built-in feature rather than custom code.

**Insulate yourself:** the graph store holds plain `{nodes, edges}` and a `GraphCanvas` adapter translates to Cytoscape. If you ever outgrow it, swapping to Sigma touches one file.

## Meeting requirement 8.iii — "close to its cluster, far from others"

Force layouts don't do this by default; edge-heavy graphs collapse into one blob. Two-part fix:

1. **Detect communities** (Leiden/Louvain over the undirected projection) → `community_id` per node.
2. **Make edge length a function of community membership:**

```js
{
  name: 'fcose',
  quality: 'proof',
  randomize: false,                        // incremental, don't re-scramble  [M6]
  fixedNodeConstraint: existingNodes.map(n => ({ nodeId: n.id, position: n.pos })),
  idealEdgeLength: e =>
    e.source().data('community') === e.target().data('community') ? 45 : 220,
  nodeRepulsion: n => n.data('state') === 'SEED' ? 20000 : 6000,
  nodeSeparation: 90,
  numIter: 2500,
}
```

Same-community edges pull tight; cross-community edges act as long springs. This produces visibly separated clusters with strong internal cohesion. At R1, before communities exist, use a constant `idealEdgeLength` — it's fine for 100 nodes.

Run layout in a **Web Worker** (Cytoscape supports headless layout) and apply final positions; otherwise 1,000 nodes freezes the tab for seconds.

## Component tree

```
<App>
├── <Toolbar>
│   ├── <AddPaperDialog>          title → GET /search → pick → POST /nodes
│   ├── <ExpansionControls>       hops(1) · maxNew(20) · direction bias · [Expand]
│   ├── <ClearGraphButton>        typed confirmation ("clear") — destructive
│   └── <SessionSwitcher>         R2
├── <SearchBar>                   local fuzzy filter over loaded nodes; DIMS
│                                 non-matches, never mutates the graph  (8.ii)
├── <GraphCanvas>                 Cytoscape wrapper — the only stateful component
│   ├─ state → style mapping (see below)
│   ├─ tap → setSelected(id)
│   ├─ selected → .closedNeighborhood().addClass('hl'); rest .addClass('fade')
│   └─ layout worker
├── <NodeInspector>               right panel, on selection
│   ├── metadata: title, authors, venue, date, citations, type, categories
│   ├── graph: in-degree, out-degree, depth, community, score
│   ├── <ScoreBreakdown>          bar chart from score_breakdown JSON  ← do this
│   └── actions: Like · Dislike · Remove(dry-run first) · Expand from here
├── <CandidateList>               ranked table of CANDIDATE nodes, sortable.
│                                 The actual recommendation surface — the graph
│                                 is for structure, the list is for reading.
├── <StatsPanel>
└── <ReviewDrawer>                Quarantined (N) · Rejected (N) · Removed (N)
                                  each row: reason_code + Restore            [M5]
```

**Two UI points worth arguing for:**

- **`<CandidateList>` is the product.** A force-directed graph is excellent for understanding *structure* and terrible for reading a ranked list. Users will make most decisions from the table. Don't let the graph eat all your UI effort.
- **`<ScoreBreakdown>` is the highest-signal-per-line-of-code component in the whole app.** It makes the recommender legible, it doubles as your debugging tool, and in a portfolio review it demonstrates that you understand interpretability as an engineering property rather than a buzzword.

## State → visual encoding

| State | Fill | Shape | Size | Border |
|---|---|---|---|---|
| SEED | deep blue | hexagon | largest | thick |
| LIKED | green | ellipse | large | medium |
| DISLIKED | grey-red, 40% opacity | ellipse | small | dashed |
| CANDIDATE (top-ranked) | amber | ellipse | scaled by score | thin |
| CANDIDATE (other) | light grey | ellipse | small | thin |
| SURVEY (any state) | + diamond badge | — | — | — |

Size by `log1p(citation_count)`, capped. Edge opacity by `is_influential`. Never encode meaning in colour alone — the survey badge and shape differences matter for accessibility and for screenshots that survive greyscale.

## State management split

- **TanStack Query** — `/graph`, `/nodes/{id}`, `/candidates`, `/stats`, expansion polling. Mutations invalidate `['graph']`. Free optimistic updates for like/dislike.
- **Zustand** — `selectedNodeId`, `highlightedIds`, `searchQuery`, `visibleStates`, `expansionParams`. High-frequency, purely local.
- **Cytoscape's own instance** — the graph itself. Do *not* mirror node positions into React state; you'll cause a re-render per animation frame.

---

# G. Backend / API

## Module boundaries

| Module | Owns | Must not |
|---|---|---|
| `api/` | HTTP, Pydantic schemas, status codes | contain business logic |
| `services/candidates.py` | frontier selection, pooling, prescore | know about HTTP |
| `services/filters/` | cascade, one file per stage, pure functions | touch the DB except to log decisions |
| `services/dedup.py` | canonicalization, node merge | |
| `services/ranking.py` | features → normalize → score. **pure** | fetch anything |
| `services/graphops.py` | NetworkX: PageRank, reverse PageRank, degree | |
| `services/similarity.py` | co-citation, bibliographic coupling | join to `graph_nodes` |
| `services/features.py` | compute → normalize → persist → score | reach for runtime weights |
| `services/gc.py` | mark-and-sweep | |
| `clients/s2.py` | batching, rate limit, retries, cache, normalization | know about the graph |
| `repo/` | all SQL | |
| `jobs.py` | queue, worker | |

**Three splits this table originally didn't anticipate, each for a reason worth keeping:**

- **`similarity.py` is separate from `graphops.py` and must not join to `graph_nodes`.** Co-citation and bibliographic coupling run over the **corpus**, not the drawn graph — that is precisely what makes boundary papers (pre-2015, stored with edges but no `graph_nodes` row) do their job. Folding it into `graphops.py`, which builds its `DiGraph` *from* `graph_nodes`, would silently discard them and the R1.9 test would be the only thing that noticed.
- **`gc.py` is separate** because the sweep's exemption set is a question about *event history* ("whose last event did the user write?"), not about graph structure.
- **`features.py` takes weights as an argument rather than reading the active config.** The runtime weights live in `api/config.py` because a `PUT` may have overridden them; a service reaching up for them would invert the dependency and put `services/` on the wrong side of the layer rule.

`services/ranking.py` and `services/filters/` being **pure functions over dataclasses** is what makes them exhaustively unit-testable without a database or network. Enforce it.

## Endpoints

```
── Search & ingestion ──────────────────────────────────────────────
GET    /api/search?q=<title>&limit=10
       → S2 title search, cache-first. 200 [{s2_id,title,year,authors,
         citation_count,venue,already_in_graph,previously_removed}]
       The last two flags are what stop the user re-adding what they deleted.
       Errors: 400 empty q · 503 S2 unavailable (with cache-age note)

POST   /api/sessions/{sid}/nodes        {s2_paper_id, as_state:"SEED"}
       → sync: fetch metadata, dedup, insert, event. 201 {node}
       → then auto-enqueue an expansion (returns job id in body)
       Errors: 404 unknown id · 409 already present · 422 filtered
               (with reason_code + a force=true override)

── Graph read ──────────────────────────────────────────────────────
GET    /api/sessions/{sid}/graph?states=SEED,LIKED,CANDIDATE
       → 200 {nodes:[{id,title,state,score,pos,in_deg,out_deg,type,
                      community,year,citation_count}],
              edges:[{source,target,is_influential,intents}],
              meta:{node_count,edge_count,config_version}}
       One call, whole graph. At 2k nodes this is ~400KB — fine.
       Do NOT paginate until you've measured a problem.

GET    /api/nodes/{id}                → full metadata + features + breakdown
GET    /api/sessions/{sid}/candidates?limit=50&sort=score
       → the ranked recommendation list

── Labelling ───────────────────────────────────────────────────────
PATCH  /api/sessions/{sid}/nodes/{id}   {state:"LIKED"|"DISLIKED"|"CANDIDATE"}
       Single endpoint, not /like + /dislike + /unlike. One state machine,
       one validation path, one audit write.
       → 200 {node, rescored_count}
       Errors: 409 SEED_NOT_LABELABLE · 422 invalid transition

DELETE /api/sessions/{sid}/nodes/{id}?dry_run=true|false
       dry_run=true  → 200 {would_remove:[{id,title}], count}   ← always call first
       dry_run=false → 200 {removed:[ids], gc_swept:[ids]}
       Writes REMOVED + GC_SWEPT events. Tombstones. Corpus untouched.

── Expansion (async) ───────────────────────────────────────────────
POST   /api/sessions/{sid}/expansions
       {hops:1, max_new:20, anchors:[ids]|null, direction_bias:0.5,
        api_call_budget:60}
       → 202 {job_id, status:"QUEUED"}
       Errors: 409 expansion already running (one at a time — concurrent
               expansions racing on the same pool is a bug factory)
               422 graph at max_nodes

GET    /api/expansions/{job_id}
       → 200 {status, stage, progress:{pool,filtered,added},
              api_calls, cache_hits, error, result_node_ids}
DELETE /api/expansions/{job_id}          → cooperative cancel

── Review & admin ──────────────────────────────────────────────────
GET    /api/sessions/{sid}/rejections?outcome=QUARANTINE|REJECT|REMOVED
       → grouped by reason_code, with counts                        [M5]
POST   /api/sessions/{sid}/nodes/{id}/restore   → tombstone/rejection override
DELETE /api/sessions/{sid}/graph                → clear (requires ?confirm=true)
GET    /api/sessions/{sid}/stats
GET    /api/config                              → active weights + version
PUT    /api/config                              → update weights, triggers rescore
                                                  (no API calls — that's the payoff
                                                   of persisting features)  [B-rule-1]
GET    /api/health                              → db ok, s2 reachable, cache size
```

**Deliberately not built:** `PUT /papers/{id}` (S2 owns paper facts), user/auth endpoints, `/recommendations` as distinct from `/candidates` (same thing, one name), bulk-import endpoints, GraphQL.

## Semantic Scholar client design

```python
class S2Client:
    """Batch-first. Every fetch path goes through the cache and the limiter."""

    async def search_title(self, q: str, limit: int) -> list[PaperStub]: ...

    async def get_papers(self, s2_ids: list[str], fields: FieldSet) -> list[Paper]:
        """Chunks into ≤500-id POST /paper/batch requests. The ONLY metadata path.
        A single-paper getter would let callers accidentally do N+1 fetching."""

    async def get_references(self, s2_id, fields, limit=1000) -> list[EdgeRecord]:
    async def get_citations(self, s2_id, fields, limit=1000) -> list[EdgeRecord]:
        """Nested fields so candidates arrive prescoreable with 0 extra calls."""

    async def get_authors(self, s2_ids: list[str]) -> list[Author]:
        """Lazy, shortlist-only. Never called during candidate generation."""
```

Layers, innermost first: `token bucket (1 rps, configurable)` → `api_cache lookup` → `httpx` → `tenacity retry` → `Pydantic parse (all-optional)` → `normalize to domain dataclass`.

Add a `S2Client` subclass `CachedOnlyS2Client` that raises on any cache miss. Point your tests at it and they become fully offline and deterministic. This is 15 lines and it transforms your testing story.

## Security (proportionate)

| Concern | Handling |
|---|---|
| API key | `.env`, never committed; `.env.example` checked in; `pydantic-settings` for loading |
| Input validation | Pydantic on every request. Cap `max_new ≤ 100`, `hops ≤ 2`, `q ≤ 300` chars |
| SQL injection | parameterized only — SQLAlchemy Core handles this; never f-string a query |
| XSS | React escapes by default. **Never** `dangerouslySetInnerHTML` on a title or abstract — S2 titles contain LaTeX and occasional HTML |
| CORS | explicit allowlist `http://localhost:5173`. Not `*`, even locally — it's one line and reviewers check |
| Rate limiting | client-side token bucket protects *S2*; also cap concurrent expansions at 1 |
| Bind address | `127.0.0.1` only. Do not expose to `0.0.0.0`. |
| Untrusted text | S2 titles and abstracts are third-party input. They are only ever *displayed* (React-escaped) and *embedded*, never interpreted. No LLM stage means no prompt-injection surface — worth one line in the README, since it's a deliberate scope decision rather than an oversight. |

---

# H. Development roadmap

Nine releases, down from your eleven. **The one ordering change I'd argue hardest for: the evaluation harness moves to R4, before personalization.** You cannot tune like/dislike weighting or defend PPR-over-co-citation without a metric. Building personalization first means guessing, then discovering at R9 that you guessed wrong and having no record of it.

Every release ends with the app **usable end to end**. Complexity is in ideal-days for one person working evenings.

---

## R0 — Skeleton, S2 client, arXiv bulk load
**Complexity: 3–4 days**

**Objective:** be able to fetch, cache, and classify papers from a Python REPL. No UI, no graph.

- Repo structure, `uv`/`pip-tools` lockfile, `Makefile`, `.env.example`, `ruff` + `mypy` + `pytest` in CI
- SQLite schema + Alembic baseline migration (all tables from §C — write them all now, populating comes later; schema churn is cheaper before there's data)
- `clients/s2.py`: batch-first, token bucket, tenacity, `api_cache`, all-optional Pydantic models
- `scripts/load_arxiv_meta.py`: bulk harvest → `arxiv_meta` (~2.7M rows). Resumable, idempotent.
- `cli.py`: `fetch <title>`, `refs <id>`, `cites <id>`, `stats`

**Tests:** cached-fixture tests for the client (429/500/404/malformed/schema-drift). arXiv loader against a 100-row sample.
**DoD:** `python -m cli fetch "Attention Is All You Need"` prints normalized metadata including `primary_arxiv_category`, and the **second** run makes zero network calls.
**Not yet:** any web layer, any graph, any ranking.
**Demonstrates:** API client engineering, caching, bulk data ingestion, retry/rate-limit design, schema design.

---

## R1 — Walking skeleton
**Complexity: 5–7 days** · *This is your "simple working version".*

**Objective:** title in → citation graph on screen. Every layer touched, nothing deep.

- Backend: `POST /nodes`, `GET /graph`, `GET /search`, sync 1-hop expansion (async at R2)
- Candidate generation: pooled (§E1–E2), `anchor_overlap` + citations/year prescore
- Filters: stage 1 (type) + stage 2a/2c (arXiv primary category, venue fallback) only
- **Dedup (M1) and the hub guard (M2) ship here.** Both are miserable to retrofit.
- Hard caps: `max_new` (default 20), `max_nodes` 2000, `api_call_budget` 60
- Frontend: Vite + Cytoscape canvas, fcose, state-based colours, click → metadata panel

**Tests:** unit — prescore, dedup canonicalization, hub guard, type filter. Integration — seed→expand→graph against cached fixtures. E2E (Playwright) — add seed, see ≥5 nodes.
**DoD:** enter "BERT", get a ~25-node graph of core-ML papers in <30s, no datasets, no medical-AI papers, reproducible from cache.
**Not yet:** like/dislike, GC, PageRank, embeddings, async jobs, sessions.
**Demonstrates:** vertical-slice delivery, graph modelling, filtering, React+viz integration.

---

## R2 — Interaction, state machine, GC
**Complexity: 5–6 days**

**Objective:** the graph becomes yours to curate.

- `interaction_events` + materialized state; `scripts/rebuild_state.py`
- `PATCH /nodes/{id}` with full transition validation (SEED → 409)
- `DELETE /nodes/{id}` with **dry-run**, mark-and-sweep GC, tombstones
- Async expansion: `jobs` table + worker thread + polling; progress UI
- Neighbour highlight (`.closedNeighborhood()`), local search dimming, clear-graph with typed confirmation
- Stats v1: nodes, edges, per-state counts, components, avg degree, density
- `<ReviewDrawer>`: rejected/quarantined/removed with reason codes + Restore
- Sessions (multiple named graphs)
- **Layout position persistence with pinning (M6)**

**Tests:** state-machine table test (all transitions × all states, including illegal ones). GC against hand-built graphs: chain, diamond, disconnected candidate cluster, labeled-node-must-survive, anchor-downgrade cascade. E2E: like → expand → verify tombstone excluded.
**DoD:** curate a 100-node graph for 20 minutes without the layout scrambling, without re-seeing a removed paper, and with every rejection explainable.
**Not yet:** any ranking beyond prescore.
**Demonstrates:** state-machine design, event sourcing, graph algorithms (reachability/GC), async job design, UX judgment (dry-run before destructive ops).

---

## R3 — Ranking, features, interpretability
**Complexity: 6–8 days**

**Objective:** candidates are ordered by something defensible, and the order is explainable.

- `node_features` persisted; `services/ranking.py` as a pure function
- Features: PageRank + reverse PageRank, in/out degree, co-citation count, bibliographic coupling, age-normalized citations, recency, venue tier, lazy author prior
- **Rank-percentile normalization** (§E8)
- `config/ranking.yaml` + `config_version` stamping + `PUT /api/config` → instant rescore, zero API calls
- `<CandidateList>` ranked table; `<ScoreBreakdown>` bars in the inspector
- Score threshold floor and per-source diversity cap in budget allocation
- **Crawl-bias handling:** compute PageRank only over nodes with `crawl_state != STUB`; surface a `crawl_completeness` figure in stats and refuse to display PageRank below 60% (§I)

**Tests:** PageRank against a graph with a known analytic answer; co-citation/bib-coupling on hand-built cases; rank-normalization monotonicity + outlier immunity; determinism (same input+config → byte-identical order); golden-file test on score_breakdown.
**DoD:** flipping one weight in the YAML visibly and instantly reorders the candidate list, and the inspector shows why any given paper ranks where it does.
**Not yet:** personalization, embeddings.
**Demonstrates:** feature engineering, graph algorithms, IR ranking, config management, interpretability, determinism.

---

## R4 — Evaluation harness and baselines ⭐
**Complexity: 5–7 days** · *The single highest-value release for job applications.*

**Objective:** answer "does this work?" with numbers instead of vibes.

- `eval/build_benchmark.py`: sample 150–300 held-out target papers (core-ML, 2019–2024, ≥15 references). For each: seeds = 3 randomly chosen references; ground truth = the remaining references. **Enforce a temporal cutoff** — the harness must reject any candidate published after `min(seed.publication_date)`, or you leak the future and every number is fiction.
- `eval/run.py`: for each case, run generation→filter→rank with a given config; compute Recall@{10,20,50}, NDCG@20, MRR, Hit@10; aggregate with bootstrap 95% CIs
- **Baseline suite (mandatory — a metric without baselines is decoration):**
  1. random from the pool
  2. citation-count only
  3. unranked 1-hop BFS
  4. citations/year only
  5. `anchor_overlap` only
  6. S2's own `/recommendations` endpoint ← the one that hurts, and the one reviewers respect you for including
  7. your full ranker
- Config sweep over weight grids; results to `eval/results/*.json`
- `docs/evaluation.md`: protocol, results table, ablations, failure analysis, limitations

**Limitations you must write down (this section is graded harder than the results):**
- Reference lists are a *biased* proxy for relevance: they omit concurrent work, omit deliberately-uncited competitors, and include obligatory citations the author never read.
- Recall is capped by graph reachability — a relevant paper 4 hops away is unreachable at `max_depth=3` and counts as a miss regardless of your ranking.
- Popular papers appear in many reference lists, so **citation-count baselines are artificially strong on this benchmark.** Beating baseline #2 by a little is not impressive; report it honestly.
- Author self-citation inflates ground truth for prolific authors.

**Tests:** leakage test — assert no ground-truth paper is visible to the system before its cutoff. Reproducibility — same seed and config → identical metrics.
**DoD:** `make eval` produces a table where your ranker beats every baseline except possibly S2's, with CIs and at least three ablations.
**Not yet:** LTR, embeddings.
**Demonstrates:** **evaluation design, IR metrics, baselines, leakage awareness, scientific honesty.** This is what separates an engineer from someone who assembled a demo. Lead with it.

---

## R5 — Personalization
**Complexity: 4–5 days**

- Personalized PageRank (random walk with restart) on the LIKED∪SEED set, NetworkX `pagerank(personalization=...)`
- Dislike proximity as a penalty term (PPR from the disliked set, subtracted)
- Rocchio-style weight nudging: after N labels, shift weights toward features that discriminate liked from disliked
- Feedback loop: labelling triggers rescore + reorders the candidate list live
- **Measure everything against R4.** Simulate a user in the harness: reveal ground-truth papers as "likes" one at a time and verify Recall@20 rises. If PPR doesn't beat co-citation on the benchmark, *say so in the docs and keep the simpler model.* A documented negative result is a strong signal.

**Tests:** PPR determinism (fixed seed, fixed tolerance); PPR concentrates mass near the restart set on synthetic graphs; simulated-feedback monotonicity.
**DoD:** liking 5 papers measurably improves Recall@20 on the benchmark, with the delta reported.
**Demonstrates:** recommender systems, relevance feedback, closing the loop between algorithm and measurement.

---

## R6 — Embeddings and clustering
**Complexity: 5–7 days**

- SPECTER2 over title+abstract (check whether S2 serves `embedding.specter_v2` directly — if so, zero inference cost). Store as a NumPy `.npy` beside SQLite; brute-force cosine over 3k × 768 is sub-millisecond. **No vector DB.**
- **The point of this release:** embedding-based candidate generation that reaches papers the citation graph *cannot* — concurrent work with no citation path. Report how many benchmark recoveries came only from this channel; that number is the release's justification.
- Hybrid fusion: reciprocal rank fusion of graph-generated and embedding-generated pools
- Dedup upgrade: cosine > 0.97 + title similarity → duplicate candidates for review
- Leiden communities → `community_id` → cluster-aware `idealEdgeLength` (§F) + convex hulls + auto-labeling from TF-IDF of cluster titles
- Filter stage 3 upgrade: exemplar-centroid core-vs-applied classifier with a quarantine margin

**Tests:** embedding cache invalidation; RRF ordering; Leiden determinism at fixed seed; classifier precision/recall on your accumulated quarantine labels.
**DoD:** measured recall improvement attributable specifically to the non-citation channel, plus visibly separated labeled clusters.
**Demonstrates:** embeddings, hybrid retrieval, community detection, and — importantly — restraint in not adding infrastructure you don't need.

---

## R7 — Learning-to-rank *(optional experiment)*
**Complexity: 5–8 days**

Only if R4 shows your hand-tuned weights leave headroom. Train on **auto-generated citation triples** (§DEFER) rather than your ~200 hand labels — thousands of examples for free. LambdaMART (LightGBM) or a pairwise logistic model. Compare against the hand-tuned linear ranker on the R4 benchmark and **report the delta honestly, including if it's negative.**

**With no deadline, R6 is a good place to stop and live with the tool.** R0–R6 is a complete, measured system. R7 and R8 are worth doing only if the evaluation harness shows a specific gap they'd close — which is exactly the discipline the project is meant to demonstrate.

## R8 — GNN *(experiment only)*
Do not ship a GNN as a feature. If you're curious, run it as a documented experiment: GraphSAGE/R-GCN link prediction on the citation graph vs. your R5 ranker, on the R4 benchmark. Write it up in `docs/experiments/gnn.md`. **A rigorous negative result here is a stronger hiring signal than a GNN shipped without comparison** — it shows you can evaluate hype, which is a large part of the actual job.

---

# I. Evaluation strategy

Covered operationally in R4. Three additional points that matter technically.

### PageRank on a partially crawled graph is biased — say so

Your graph is a crawl, not a corpus. Frontier nodes have artificially truncated degree: a node whose citations you never fetched looks unimportant purely because you stopped fetching. Naive PageRank therefore **systematically over-ranks well-crawled central nodes and under-ranks the frontier** — which is exactly backwards for discovery.

Mitigations, all cheap:
1. Compute PageRank only over the subgraph where `crawl_state != STUB`.
2. Report `crawl_completeness = |non-stub| / |nodes|` in stats; suppress the PageRank column below 60%.
3. Prefer **local** measures (co-citation, bibliographic coupling, anchor overlap) for ranking — they depend only on 2-hop neighbourhoods you actually fetched, so they're far less crawl-sensitive.
4. Run PageRank on the *undirected* projection too and compare — divergence between the two is a useful crawl-bias diagnostic.

Writing this analysis into `docs/algorithms.md` is worth more in a review than the PageRank implementation itself. It shows you understand what your numbers *mean*.

### Online signals (R5+)

Log to `interaction_events` and compute: label rate (fraction of surfaced candidates labelled at all), **like rate among top-10 vs. ranks 11–50** (this is your live precision@k proxy and the cleanest online signal you'll get), removal rate, and time-to-first-like per expansion. With one user these are directional only — never present them as statistically meaningful.

### Sanity checks that catch real bugs

- **Known-graph test:** seed "Attention Is All You Need" alone. If "BERT", "GPT-2/3", "T5", and "RoBERTa" don't surface in the top 20, something is broken. Encode this as an actual test.
- **Filter audit:** hand-label 100 random accepted papers as core/applied. Report precision. If it's under 80%, fix the filter before doing anything else.
- **Explosion test:** click Expand 10 times with `max_new=20`. Assert nodes ≤ 200 + seeds, and that API calls per expansion stay bounded.

---

# J. Testing strategy

```
                     ▲
              E2E    │  ~6 Playwright specs  (slow, high value, few)
                     │  add-seed · expand · like→rescore · remove→GC
                     │  search-dims · clear-confirm
              ───────┼──────────────────────────────────
       Integration   │  ~30 tests, all against CachedOnlyS2Client
                     │  full expansion pipeline · API endpoints ·
                     │  migrations up/down · state rebuild from events
              ───────┼──────────────────────────────────
              Unit   │  ~150 tests  (fast, run on save)
                     │  filters · prescore · ranking · normalization ·
                     │  state machine · GC · dedup · budget allocation
                     ▲
```

**The decision that makes all of this work: `CachedOnlyS2Client`.** It raises on cache miss, so your entire integration suite runs offline, deterministically, in seconds, with no rate limits and no flakiness. Populate `tests/fixtures/s2_cache.db` once from real calls and commit it. Every S2-touching test uses it.

**High-value test cases, specifically:**

| Area | Cases |
|---|---|
| Citation direction | `edge(A,B)` means A cites B; `references(A) ∋ B`; `citations(B) ∋ A`; fetching the same pair from both endpoints yields **one** row with `discovered_via='BOTH'` |
| GC / orphans | chain A→B→C, remove B; diamond (two paths to anchor, remove one); candidate-only cluster; **labeled node survives disconnection**; LIKED→CANDIDATE downgrade cascades; GC is idempotent |
| State machine | full transition matrix incl. illegal ones; SEED rejects like/dislike; DISLIKED→LIKED allowed; REMOVED excluded from next expansion; rebuild-from-events matches materialized state |
| Filters | dataset rejected; low-cite survey rejected; high-cite survey accepted; recent-high-CPY survey accepted; cs.RO and cs.CV rejected; primary cs.LG + cross-list cs.CV **accepted**; no-arXiv+NeurIPS accepted; medical→REJECT; **every case asserts the `reason_code`** |
| Year floor / boundary papers | 2014 paper gets no `graph_nodes` row; its `papers` row and edges **are** written; it is never selected by the frontier; `bib_coupling` between two modern papers sharing only a 2014 reference is **non-zero** (this is the test that protects the design); `year=NULL` is quarantined, not silently admitted |
| Dedup | arXiv+conference pair merges; edges union on merge; punctuation/case/unicode title variants; different papers with similar titles do **not** merge (the false-positive direction is the dangerous one) |
| Ranking | determinism; rank-normalization is outlier-immune; monotone in each feature holding others fixed; weight change reorders without refetch; breakdown terms sum to score |
| Explosion guards | budget respected exactly; hub node never forward-expanded; max_nodes refuses with 422; api_call_budget hard-stops and commits partial results |
| S2 client | 429 backoff honours Retry-After; 500 retries then degrades; 404 marks STUB; missing fields don't raise; batch chunks at 500; cache hit makes zero HTTP calls |
| Eval harness | **no temporal leakage** (the most important single test in the project); fixed seed → identical metrics |

Run `ruff`, `mypy --strict` on `services/`, and the unit+integration suites in CI on every push. A green badge on a project like this is worth real credibility.

---

# K. Repository structure

```
paper-recommender/
├── README.md                  ← the most important file; see §L
├── Makefile                   dev, test, eval, migrate, load-arxiv
├── pyproject.toml             deps + ruff/mypy/pytest config
├── .env.example
├── docker/Dockerfile          added R4
│
├── backend/
│   ├── app/
│   │   ├── main.py            FastAPI app, CORS, lifespan
│   │   ├── config.py          pydantic-settings
│   │   ├── api/               graph.py search.py expand.py stats.py config.py
│   │   ├── schemas/           Pydantic request/response
│   │   ├── services/
│   │   │   ├── frontier.py candidates.py dedup.py ranking.py graphops.py
│   │   │   ├── layout.py expansion.py       ← orchestrates the §E pipeline
│   │   │   └── filters/  base.py type_filter.py topic_filter.py applied_filter.py
│   │   ├── clients/           s2.py arxiv.py cache.py rate_limit.py
│   │   ├── repo/              papers.py edges.py graph.py events.py cache.py
│   │   ├── models/            domain dataclasses (NOT ORM entities)
│   │   ├── jobs.py
│   │   └── cli.py             Typer — stays useful forever
│   ├── migrations/            Alembic
│   └── tests/  unit/ integration/ fixtures/s2_cache.db
│
├── frontend/
│   ├── src/
│   │   ├── components/  GraphCanvas/ NodeInspector/ CandidateList/
│   │   │                StatsPanel/ SearchBar/ Toolbar/ ReviewDrawer/
│   │   ├── graph/       cytoscapeConfig.ts styles.ts layoutWorker.ts
│   │   ├── api/         client.ts queries.ts   (TanStack)
│   │   ├── store/       viewStore.ts           (Zustand)
│   │   └── types/       generated from OpenAPI — don't hand-write these
│   └── tests/ e2e/
│
├── eval/
│   ├── build_benchmark.py  run.py  baselines.py  metrics.py
│   ├── benchmarks/*.json   results/*.json
│
├── config/  ranking.yaml  filters.yaml  venues.yaml
├── data/    app.db  arxiv_meta.db  embeddings.npy   (gitignored)
├── scripts/ load_arxiv_meta.py rebuild_state.py export_bibtex.py
└── docs/
    ├── architecture.md  graph-model.md  algorithms.md  data-model.md
    ├── evaluation.md ⭐  scope.md  limitations.md
    ├── adr/  0001-sqlite-not-neo4j.md  0002-directed-edges.md
    │         0003-cytoscape.md  0004-no-vector-db.md
    └── media/  demo.gif  screenshots/
```

The `docs/adr/` directory is disproportionately valuable. Four short files explaining what you *didn't* build and why demonstrate exactly the judgment that separates a mid-level engineer from a beginner. "I considered Neo4j and rejected it because at 10³ nodes an in-process NetworkX graph is faster than the network hop" is a better interview answer than any amount of Neo4j experience.

---

# L. Portfolio strategy

## What to lead with

Order your README this way, because reviewers read the first 30 seconds and skim the rest:

1. **A GIF.** Type a title → graph appears → click Expand → new nodes settle → click a node → score breakdown. 20 seconds. This is your entire pitch.
2. **The evaluation table.** Your ranker vs. six baselines with confidence intervals. Numbers above the fold.
3. **One architecture diagram.**
4. **"Design decisions"** — five bullets, each linking to an ADR.
5. **"Limitations"** — honest, specific. This section makes senior reviewers trust everything above it.

## Value per component, ranked

| Component | Signal for an AI Engineer role | Why |
|---|:--:|---|
| **Evaluation harness + baselines + leakage control** | ★★★★★ | Almost no portfolio project has this. It's the core of the actual job. |
| **Filter cascade with reason codes and a review queue** | ★★★★★ | Real ML systems are 80% data quality plumbing, and this proves you know it. |
| **S2 client: batching, rate limits, retries, immutable cache** | ★★★★☆ | Direct proxy for production data-pipeline work. |
| **Graph semantics + state machine, documented** | ★★★★☆ | Shows you specify before you code. Rare and immediately visible. |
| **Crawl-bias analysis of PageRank** | ★★★★☆ | Shows you understand what your metrics mean, not just how to call the function. |
| **Ranking with persisted features + interpretable breakdown** | ★★★★☆ | Feature-store thinking in miniature; interpretability as engineering. |
| **ADRs, especially the "why not Neo4j / why not a vector DB" ones** | ★★★★☆ | The clearest possible evidence of judgment. |
| Async job design | ★★★☆☆ | Solid, unremarkable. |
| Cytoscape graph UI | ★★★☆☆ | High *demo* value, moderate engineering value. Necessary, don't over-invest. |
| Embeddings / hybrid retrieval | ★★★☆☆ | Commodity — **unless** you frame it as fixing the graph's blind spot and measure it. Then ★★★★. |
| GNN | ★☆☆☆☆ as a feature, ★★★★☆ as a measured experiment | The framing is the entire value. |

## Things that look impressive and aren't — actively avoid

Neo4j "because graphs" · Kubernetes for a single-user app · microservices · Celery+Redis for one background task · a vector DB for 3,000 vectors · an "agentic" wrapper around a function call · a GNN with no baseline · LLM-as-ranker with no measurement · five model providers behind an abstraction layer you'll never use twice.

Every one of these reads as "added technology to look senior", which is the opposite of looking senior. Your differentiator is **measurement and judgment**, not stack size.

## How to talk about it

Lead with a problem statement, not a tech list: *"Citation-graph structure carries relevance signal that embedding search discards — specifically co-citation and bibliographic coupling. I built a recommender around that, then measured whether the claim holds."* Then show the table. Then be ready with a specific thing that didn't work and what you changed — that question decides most interviews.

---

# M. Risks and failure modes

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|:--:|:--:|---|
| 1 | **Hub explosion** — forward-expanding a 100k-citation paper burns your API budget and adds noise | High | High | Hard `FORWARD_EXPAND_MAX=2000` guard in R1. Backward expansion always safe. |
| 2 | **arXiv category coverage gap** — non-arXiv and pre-2010 papers have no category | Certain | Medium | Documented fallback chain (venue → s2_fields → quarantine). Report coverage % in stats so you know how blind you are. |
| 3 | **S2 rate limits / schema drift** | High | Medium | Token bucket, batch-first, immutable cache, all-optional Pydantic, `SCHEMA_DRIFT` logging. App stays fully usable read-only from cache. |
| 4 | **Filter false negatives** — silently discarding good papers | High | High | Three outcomes not two; every rejection logged with a reason code and restorable from the UI; periodic hand-audit of 100 accepted + 100 rejected. |
| 5 | **PageRank crawl bias** — ranking artifacts of your crawl order | Certain | Medium | Non-stub subgraph only; completeness metric; suppress below 60%; prefer local measures; document it. |
| 6 | **Eval temporal leakage** — every reported number becomes fiction | Medium | **Critical** | Explicit cutoff enforcement plus a dedicated leakage test. This is the one bug that invalidates the whole project's headline claim. |
| 7 | **Dedup misses** — twin nodes double-count overlap features | Medium | Medium | Multi-key canonicalization in R1; embedding-assisted detection at R6; a dedup audit report in the CLI. |
| 8 | **Layout instability** kills usability | High if unaddressed | High | Persist positions, pin existing nodes, `randomize:false`, Web Worker. |
| 9 | **Label sparsity** blocks LTR | Certain | Low | Auto-generate citation triples; keep the hand-tuned linear model as the shipped default. |
| 10 | **Scope creep** — the classic killer of solo projects | High | High | Every release ships usable. If R4 slips, R5–R9 are optional; R0–R4 already make a strong portfolio project on their own. |
| 11 | **Year floor silently kills bib-coupling** — if boundary papers aren't stored, the feature degrades invisibly | Medium | High | Boundary-paper design in §E4 plus the explicit non-zero-coupling test in §J. |
| 12 | **Cytoscape performance wall** past ~5k nodes | Low (capped at 2k) | Low | Renderer-agnostic store; Sigma.js swap touches one file. |

---

# N. V1 implementation plan (R0 + R1) — start here

Ordered so that each step is independently verifiable and nothing is blocked waiting on something else.

### Week 1 — foundation, no UI

1. `pyproject.toml`, `ruff`, `mypy`, `pytest`, `Makefile`, `.env.example`. Push, get CI green on an empty test.
2. **Write the full §C schema** as Alembic migration `0001`. All tables now — schema churn is free before there's data.
3. `models/` domain dataclasses: `Paper`, `Author`, `Edge`, `CandidateFeatures`. Plain dataclasses, no ORM.
4. `clients/cache.py` — `api_cache` read/write keyed on `(endpoint, sha256(sorted params))`.
5. `clients/rate_limit.py` — async token bucket, configurable rate.
6. `clients/s2.py` — `search_title`, `get_papers` (batch), `get_references`, `get_citations`. All-optional Pydantic models → normalize to dataclasses. Tenacity on 429/5xx honouring `Retry-After`.
7. `cli.py fetch "<title>"`. **Checkpoint: run it twice; the second run must make zero network calls.**
8. `scripts/load_arxiv_meta.py` → `arxiv_meta`. Resumable, idempotent, `--limit` for testing.
9. `cli.py fetch` now joins `primary_arxiv_category`. **Checkpoint: "BERT" reports `cs.CL`.**
10. `CachedOnlyS2Client` + commit `tests/fixtures/s2_cache.db`. Write the client test suite against it.

### Week 2 — pipeline, still no UI

11. `repo/papers.py`, `repo/edges.py`, `repo/graph.py` — idempotent upserts, edges `ON CONFLICT` merging `discovered_via`.
12. `services/dedup.py` — `canonicalize(paper) -> key` over DOI → arXiv → `title_norm|first_author|year`; `merge_papers()`. **Test the false-positive direction hardest.**
13. `services/filters/era_filter.py` (year floor, stage 0) + `type_filter.py` + `topic_filter.py`, pure functions returning `FilterDecision(outcome, stage, reason_code, details)`. `config/filters.yaml`. **Write the boundary-paper path here** — pre-2015 papers get a `papers` row and edges but no `graph_nodes` row.
14. `services/candidates.py` — pooled generation from a frontier list + `anchor_overlap` + prescore. Hub guard lives here.
15. `services/expansion.py` — the §E pipeline, one function, one transaction, writes an `expansions` row. Budget allocation with the recency lane and direction floors.
16. `cli.py seed "<title>" --expand --max-new 20`. **Checkpoint: 25-node graph in SQLite, no datasets, no medical papers, `filter_decisions` populated. Inspect the DB by hand and confirm it looks right.**
17. Integration test: seed → expand → assert node count, no rejected papers present, budget respected.

*At step 17 you have a working recommender. Everything after this is presentation.*

### Week 3 — API and UI

18. `api/search.py` → `GET /api/search`. Verify at `/docs`.
19. `api/graph.py` → `POST /nodes`, `GET /graph`, `GET /nodes/{id}`.
20. `api/expand.py` → `POST /expansions` **synchronous at R1** (async is R2 — don't build the job system yet).
21. CORS for `localhost:5173`; bind `127.0.0.1`.
22. Vite + React + TS. Generate types from OpenAPI (`openapi-typescript`) — never hand-write them.
23. `api/client.ts` + TanStack Query hooks.
24. `<GraphCanvas>` — Cytoscape, fcose, state→style stylesheet, `fit()` on load.
25. `<AddPaperDialog>` — search, results list, select, `POST /nodes`, invalidate `['graph']`.
26. `<NodeInspector>` — click a node, show metadata.
27. `<ExpansionControls>` — `max_new` input + Expand button + spinner.
28. Playwright: add "BERT" → assert ≥5 nodes rendered.

### Week 3.5 — make it presentable

29. `README.md` with the GIF, a real architecture diagram, and setup steps that actually work on a clean clone.
30. `docs/graph-model.md` (the §D definitions verbatim) and `docs/adr/0001-sqlite-not-neo4j.md`.
31. Record the GIF.

**Then stop and use it for a week before starting R2.** You will discover three things that matter more than anything on this plan, and they'll be things only a real user of your own tool would notice.

---

## Resolved decisions

| # | Decision | Effect on the plan |
|---|---|---|
| 1 | **No deadline.** | Build the full R0→R6 arc in order. No compression. The "use it for a week before R2" pause is now genuinely worth taking, and so is the one after R6. |
| 2 | **`cs.CV` denied.** | Moved from BORDERLINE to APPLIED_DENY. Stage 2 is simpler; stage 3 (applied-vs-core) becomes less load-bearing. Cost is spelled out in §E4 — you lose primary-cs.CV methodological work, but primary-category-wins means cross-listed cs.LG papers still pass. |
| 3 | **Year floor 2015, corpus scoped to the deep-learning era.** | New STAGE 0 filter. Big win: post-2015 arXiv coverage is high, so the venue-fallback path (stage 2c) can stay crude and you save several days. Big trap, now handled: pre-2015 papers must still be stored as **boundary papers** or bibliographic coupling breaks — see §E4. |
| 4 | **Sessions kept** (`session_id` column from R0, switcher UI at R2). | Confirmed. Scoping boundary specified in BUILD.md: `papers`/`edges`/`api_cache`/`arxiv_meta` are global, `graph_nodes`/`interaction_events`/`expansions` are session-scoped, `filter_decisions.session_id` is nullable (global vs session verdicts). Payoff: seeding session B with a paper already crawled in session A costs zero API calls. |
| 5 | **No LLM/RAG stage.** | Old R7 deleted. R8→R7 (LTR), R9→R8 (GNN), both optional and both gated on the evaluation harness showing a real gap. `docs/security.md` becomes `docs/scope.md`. Abstracts are still stored — R6 embeddings need them. |

## Confirmed removal semantics

Your description matches the mark-and-sweep definition in §C6, so it stands as written. Stating it once more precisely, since this is the feature most likely to be misimplemented:

> After any removal, mark every node reachable from `anchors = SEED ∪ LIKED` over the **undirected** projection, bounded by `max_depth`. Sweep every unmarked node whose state is `CANDIDATE`. Never sweep a `LIKED` or `DISLIKED` node.

Worked cases:

| Situation | Outcome |
|---|---|
| Candidate C reachable only through removed node B | swept |
| Candidate C reachable through B **and** through liked node L | **kept** — "some connectivity to some other liked/seed node" |
| Candidate C reachable only via a chain of other candidates that terminates at a seed | **kept** — reachability is path-based, not adjacency-based |
| An entire candidate-only component detached from all anchors | swept in full |
| A `LIKED` node left with degree 0 | **kept** — your judgment outranks topology |
| A `DISLIKED` node left disconnected | **kept**, dimmed — it still carries negative signal and re-fetching it would be wasted budget |
| `LIKED` downgraded to `CANDIDATE` (it stops being an anchor) | run the sweep — its dependents may now be orphans |

The bounded-depth clause matters: without it, a long candidate chain trailing off from a seed stays alive forever. `max_depth=3` from the nearest anchor is a sane default.


---

**Execution:** see `BUILD.md` for the task-by-task build plan derived from this document.
