# Progress tracker

From `docs/BUILD.md` Appendix C. Tick a release only when its gate is
observably true — not when its tasks are merely written.

| Release | Gate | Done |
|---|---|:--:|
| R0 | Fetch twice → zero network calls on the second; `cs.CL` for BERT, `cs.CV` for ResNet | ✅ |
| R1 | Browser: "BERT" → Expand ×2 → ~60 post-2015 core-ML nodes in <90s | ✅ |
| — | **Use it for a week. Keep an annoyance list.** | ◐ in progress — the annoyance list is what produced the topic-filter work below |
| R2 | 20 min of curation: layout stable, no removed paper returns, every rejection reversible | ✅ |
| R3 | One YAML weight change instantly reorders the candidate list; breakdown explains any rank | ◐ the rescore half is true and demonstrated; `<CandidateList>` and `<ScoreBreakdown>` are not built |
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
| R1.1 — repo/papers.py, repo/edges.py | M | ✅ | 35 tests; session_id absent by contract |
| R1.2 — repo/graph.py, repo/events.py | M | ✅ | 33 tests; session 1 invisible to session 2 |
| R1.3 — services/dedup.py | M | ✅ | 33 tests; "Attention Is All You Need" vs its negation |
| R1.4 — filters/base + era_filter | M | ✅ | 24 tests; None year quarantines |
| R1.5 — type_filter + topic_filter | L | ✅ | 60 tests; cs.LG×cs.CV must pass |
| R1.6 — filters/cascade + logging | M | ✅ | 27 tests; re-run gives cache_hits=4, evaluated=6 |
| **R1.7 ◆ CHECKPOINT** | S | ✅ | 50 BERT refs: 26 accept, 15 PRE_ERA, 3 VENUE_CORE |
| R1.8 — candidates: pool + prescore | L | ✅ | 35 tests; overlap==3, HUB_SKIP_FORWARD |
| R1.9 — Boundary papers | M | ✅ | 15 tests; the bib-coupling design test |
| R1.10 — services/budget.py | M | ✅ | 27 tests; budget=20 exact, no source over 8 |
| R1.11 — services/expansion.py | L | ✅ | 15 tests; partial expansion is a success |
| R1.12 — services/seed.py | M | ✅ | 16 tests; force logged, not silent |
| **R1.13 ◆ CHECKPOINT** | M | ✅ | 21 nodes, 60 papers, 59 edges, scores 2.43→2.13 |
| R1.14 — FastAPI app + health | S | ✅ | 17 tests; live curl, CORS allowlist verified |
| R1.15–R1.19 — REST surface (search, seed, expand, graph, nodes) | L | ✅ | OpenAPI generated; frontend types via `make types`, never hand-written |
| R1.20–R1.23 — Vite + Cytoscape/fcose shell, inspector, expand controls | L | ✅ | Browser: "BERT" → Expand ×2 → post-2015 core-ML graph |
| R2.1–R2.5 — Sessions, rebuild-from-events, GC sweep, restore | L | ✅ | Sweep exemption is history-based (`user_held_paper_ids`), not state-based |
| R2.6–R2.8 — Removal, tombstones, depth-at-removal | M | ✅ | Depth read back from the tombstone payload rather than re-derived |
| R2.9–R2.12 — Expansion progress, search dimming, layout persistence | M | ✅ | Reload keeps saved positions; found only by running the app |
| R2.13–R2.16 — StatsPanel, ReviewDrawer, ClearGraph, SessionSwitcher | L | ✅ | PR #28 (merged). Projection built from one query, so stats are consistent by construction |
| R3 — `services/graphops.py` | M | ✅ | PageRank + reverse + undirected over non-STUB; suppressed below 60% completeness (absent, not zero) |
| R3 — `services/similarity.py` | M | ✅ | Co-citation + bibliographic coupling over the **corpus**, not the drawn graph — the R1.9 boundary test |
| R3 — `services/ranking.py` (**pure**) | M | ✅ | Rank-percentile, not z-score; breakdown terms sum to the score |
| R3 — `PUT /api/config` | S | ✅ | Instant rescore from persisted features, zero API calls |
| R3 — `services/features.py` | M | ✅ | 13 tests; normalized before storage so the pool is the session, not whatever was loaded |
| R3 — `<CandidateList>` | M | next | — |

**R0, R1 and R2 complete. R3 is the ranking pipeline minus its two UI surfaces.**
Suite at **1259 backend / 95 frontend**; full gate green.

### Findings worth keeping

- **`hub` carried the wrong sign.** `ranking.yaml` read `hub: 0.60  # subtracted`,
  but `score_paper` multiplies — a comment cannot subtract, so hubs were being
  *rewarded*. Invisible until `features.py` first populated the feature. On the
  104-node chemistry session the top results moved from *Attention Is All You
  Need* / *BERT* / *Neural Message Passing* to *ChemDFM* / *Electron flow
  matching for generative reaction*. `test_active_r1_weights` had asserted
  `0.60` — the test encoded the bug rather than the intent.
- **Corpus structure differs sharply by session.** Session 1 is a one-hop star
  (226 of 229 papers STUB, one paper with more than a single citer), so every
  structural signal is zero or suppressed there. Session 2 ("Chemical reaction
  prediction", 104 papers) has real structure: **334 coupled, 48 co-cited.**
  Test local measures against session 2.
- **`cocite` and `bibcoup` are computed and stored but weighted 0.00.** Now that
  session 2 gives them real signal, raising them is one `PUT /api/config`.
- **The reaction-ML topic rescue admits 16 of the 41** chemistry/biology papers
  already in the corpus. They will not appear until a re-expansion runs — no API
  cost, since `config_version` changed and the cached rejections get re-opened.

### Branch state at R3 (2026-09-10)

PR #28 is merged (`5421405`). Three commits sit on `r2-13-16-remaining` *after*
that merge and are **unmerged with no open PR**: `8fcb41d` (PUT /api/config),
`35f3a20` (topic filter), `8d83dc7` (features + the sign fix). They need a fresh
PR off `origin/main`.

## Prerequisites

| # | Item | Status |
|---|---|---|
| P1 | Python 3.11+, Node 20+, git | ✅ Python 3.14.7, Node 24.20.0, git 2.55.0 |
| P2 | `uv` installed | ✅ uv 0.12.5 |
| P3 | Semantic Scholar API key | ✅ present in `.env` as `S2_API_KEY` |
| P4 | Read current S2 API docs → `docs/s2-api-notes.md` | ✅ written (from `legacy/` + BUILD.md; not re-verified live — see the ⚠️ list in that file) |
| P5 | arXiv metadata source chosen | ✅ **both** — Kaggle snapshot for bulk, OAI-PMH for the delta |
| P6 | Empty GitHub repo, CI enabled | ✅ remote live, CI green; PRs #19–#26 and #28 merged (#27 closed unmerged — an accidental revert) |

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

## Working notes — things that have cost time twice

- **Piping `verify.py` to `tail` hides its exit code.** The script returns 1
  correctly; the pipeline reports the exit status of `tail`. Use
  `set -o pipefail`, or a gate failure will read as a pass.
- **`prettier` is not a project dependency.** Running it reformatted 165 and
  then 289 lines of unrelated code and both had to be reverted.
- **Do not stage `frontend/src` wholesale.** It has twice swept uncommitted
  work into an unrelated commit.
- **pysqlite opens no read transaction for a SELECT.** Two consecutive SELECTs
  on one connection can see different snapshots — observed directly as a count
  of 3 followed by a count of 8.
- **Windows line endings churn `frontend/package-lock.json`.** `npm` rewrites
  all 3537 lines; `git diff --ignore-all-space` shows the change is empty.
  Restore it from HEAD rather than committing it.
