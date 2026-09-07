# TDD Evidence Report — Features v2

## Source
Derived during TDD run from user instruction verbatim:
> "there are few for feature i want and few things that i would like to be fixed.
> 1. a graph visualization, via networkx via custom web gui
> 2. I want it to fetch candidate papers which has highest citation, not just the latest one
> 3. also I want recommender and graph expander as separate code
> 4. also graph expander should have parameter like depth/level … citation_count (weight 0.5) and (0.25 weight) top 10 conference, top 10 journals. Try to implement page rank type centre identifier
> 5. avoid papers that are of some applied fields … anything that is not in theoretical/empirical research related to Agents/LLM"

Plus 7 confirmed bugs from the `/code-review` high-effort run.

---

## User Journeys

1. **Venue scoring** — As the system, when I encounter a paper from NeurIPS/ICML/ACL/etc., I assign it a higher priority than a paper from an unknown venue.
2. **Field filtering** — As the system, when I encounter a paper tagged Biology or Medicine (with no CS tag), I skip it to keep the graph on-topic.
3. **DOI deduplication returns canonical ID** — As the graph expander, when I upsert a paper whose DOI matches an existing paper under a different S2 ID, I receive the existing paper's ID back so edges are attached to the right node.
4. **Label protection** — As the store, when a paper already has a `liked` or `disliked` label, I never overwrite it with a lower-priority label like `seed`.
5. **source_api COALESCE direction** — As the store, when a better-quality API re-ingests a paper, the new `source_api` value replaces the old one.
6. **co-citation correctness** — As the metrics engine, when computing co-citation, I count both seeds-that-cite-candidate and candidate-that-cites-seeds (both directions).
7. **Search fallback exception** — As the S2 client, when both `/match` and `/search` raise exceptions, I return `None` instead of propagating the exception.
8. **Empty match not cached** — As the S2 client, when `/match` returns an empty data list, I do not cache the empty response so later real hits can be fetched.
9. **No orphan edges** — As the graph expander, when DOI deduplication remaps a paper ID, all edges use the canonical ID so no edge endpoint is missing from the papers table.
10. **Graph viz API** — As a browser client, I can call `/api/graph` to get nodes+links JSON and `/api/paper/<id>` to get paper detail.
11. **Citation count ranking** — As the candidate scorer, papers with more citations receive a higher combined score even if they lack a network relationship to seeds.

---

## Task Report

### Bug fixes from code-review

| Bug | Fix location | Validation |
|-----|-------------|-----------|
| `upsert_paper` returned `None` — orphan edges | `storage.py:217` | `test_returns_own_id_when_no_dedup`, `test_all_edge_endpoints_have_paper_rows` |
| Label protection missing | `storage.py:175-180` | `test_liked_not_downgraded_to_seed`, `test_disliked_not_overwritten_by_seed` |
| COALESCE(`papers.source_api`, `excluded.source_api`) kept stale | `storage.py:201` | `test_source_api_updates_when_better_api_ingests` |
| `co_cit` ran same generator as `in_deg` | `citation_network.py:80` | `test_co_cit_includes_candidate_to_seed_direction` |
| Search fallback propagated exception | `semantic_scholar_client.py:119-131` | `test_fallback_exception_returns_none_not_raises` |
| Empty `/match` result cached | `semantic_scholar_client.py:113` | `test_empty_match_does_not_call_cache_set_with_empty_data` |
| `update_distance` / `add_edges` called with stale `pid` | `graph_expander.py:105-120` | `test_all_edge_endpoints_have_paper_rows` |

### New features

| Feature | Implementation | Validation |
|---------|---------------|-----------|
| Venue scoring (TIER1/TIER2 sets) | `venue_config.py` | 8 `TestVenueScore` tests |
| Field filtering (`is_relevant_paper`) | `field_filter.py` | 9 `TestIsRelevantPaper` tests |
| Citation count in ranking (log scale, weight 0.15) | `citation_network.py:143-147` | `test_higher_citation_paper_ranks_above_lower` |
| Venue-weighted BFS priority | `graph_expander.py:130-141` | `test_all_edge_endpoints_have_paper_rows` (integration) |
| Flask + D3.js graph viz server | `graph_viz.py`, `templates/graph.html` | `test_api_graph_returns_nodes_and_links`, `test_api_paper_detail` |
| Add paper by title (GUI + API) | `graph_viz.py:api_add_paper_by_title`, `templates/graph.html` | 7 `TestAddPaperByTitle` tests |
| Neighborhood highlight + dim (GUI) | `templates/graph.html` — CSS + `highlightNeighborhood`/`clearHighlight` JS | 5 `TestNodeHighlightFeature` tests |
| Search graph by title (GUI + API) | `graph_viz.py:api_search_papers`, `storage.py:search_papers`, `templates/graph.html` | 7 `TestSearchPapersApi` tests |
| Neighbor sidebar on node click | `graph_viz.py:api_paper_neighbors`, `storage.py:get_neighbors`, `templates/graph.html` | 7 `TestNeighborSidebarApi` tests |
| Reduced simulation damping | `templates/graph.html` — `velocityDecay(0.7)`, drag `alphaTarget(0.05)` | 1 `TestPhysicsSettings` test |
| Fetch 500 refs sorted by citation count | `graph_expander.py` — `per_paper_limit=500`, sorted DESC in `_fetch_neighbors` | 4 `TestSortedReferencesByScore` tests |
| Equal-weight scoring (1/3 each) + 2026 citation reduction | `graph_viz.py:_node_score`, `graph_expander.py:_priority` + pre-sort | 6 `TestEqualScoringWeights` + 2 `TestBfsPriorityUpdatedFormula` tests |
| Increased hub spacing (repulsion -400, distance 150) | `templates/graph.html` — `forceManyBody().strength(-400)`, `forceLink().distance(150)` | 2 `TestGraphLayoutSpacing` layout tests |
| Labels conditional on score threshold | `templates/graph.html` — `LABEL_SCORE_THRESHOLD=0.35`, LABELED set | 2 `TestGraphLayoutSpacing` label tests |
| See seed/liked papers + remove/prune from network | `graph_viz.py:api_seeds_liked`, `api_delete_paper`; `storage.py:delete_paper`; `templates/graph.html` panel + JS | 16 `tests/test_features_v5.py` tests |
| Exploration separated from recommendation — liking a paper fully explores its refs + citing papers (no cap), filtered by field relevance | `exploration.py:explore_paper` (new module); wired into `graph_viz.py:api_set_label` and `recommend.py:run_feedback_session` | 18 `tests/test_exploration.py` tests |
| Citation-side hybrid-score ranking (citation count 0.4, network indegree 0.3, author h-index 0.2, venue 0.1) for BFS citation-direction candidates; seed papers' own references always added in full (max_new bypassed, max_nodes still enforced); genuine PageRank fallback (power iteration) replacing degree-centrality substitute | `graph_expander.py:citation_hybrid_score`, `_representative_hindex`, direction-aware `_priority`/pre-sort, seed-ref cap bypass; `semantic_scholar_client.py:SEARCH_FIELDS` (+authors.hIndex, +venue); `citation_network.py:_manual_pagerank` | 21 `tests/test_citation_ranking.py` tests |
| Uncapped-reference treatment extended to liked papers and GUI-added papers (checked via current store label, not just seed_ids membership, so it applies even when discovered mid-BFS); build_network.py re-runs pick up existing liked/GUI-added papers as extra BFS starting points | `graph_expander.py:_bypasses_ref_cap` (replaces seed_ids-only check); `build_network.py:merge_seed_ids_with_labeled_papers` | 3 new tests in `test_citation_ranking.py::TestLikedAndGuiAddedPapersAlsoUncapped`; 5 `tests/test_build_network_seeds.py` tests |
| Removing a node recursively cascade-deletes any unlabeled paper left disconnected (via BFS reachability, not just direct-neighbor degree) from every remaining seed/liked paper — sweeping whole orphaned chains/islands in one pass; disliked/skipped papers are preserved but don't anchor others' reachability; "Clear network (keep seeds)" wipes papers/edges/metrics (keeps seed_titles) and restores only the bare seed papers — expansion stays the separate existing "Prune disliked & expand" button's job | `storage.py:delete_paper_cascade` (BFS reachability sweep), `clear_network`, `get_seed_titles`; `graph_viz.py:api_delete_paper` (now cascades), `api_clear_and_rebuild` (clear + restore seeds only, no expand_network call); `templates/graph.html` Danger Zone panel + `clearAndRebuild()`/`pollClearStatus()` JS | 20 `tests/test_features_v6.py` tests |
| Expand button auto-bootstraps from stored seed titles when the network has zero papers (restores seeds, then expands in the same click) instead of reporting "no seeds to expand from" and doing nothing; unaffected when candidates or seed/liked papers already exist | `graph_viz.py:api_expand_prune` (bootstrap path inside the existing background job, before `expand_network`) | 5 `tests/test_features_v7.py` tests |
| Both "Clear network" and the expand button's auto-bootstrap now resolve seeds from `seed_papers.txt` in the project folder (the live source of truth) instead of the possibly-stale `seed_titles` DB table; clearing also wipes and repopulates that table to match the file | `storage.py:clear_seed_titles`; `graph_viz.py:SEED_TITLES_FILE`, `_load_seed_titles_file`, `_resolve_seeds_from_file` (shared by `api_clear_and_rebuild` and `api_expand_prune`) | 6 `tests/test_features_v8.py` tests + 3 updated pre-existing tests (v2/v6/v7) isolated from the real seed file |
| "Clear network" simplified to a pure, synchronous wipe (papers/edges/metrics/seed_titles) with no seed restoration and no S2 calls — restoring/expanding is entirely the Expand button's job now (its auto-bootstrap-when-empty path from the previous feature), so the button no longer claims to "keep seeds"; obsolete restore-during-clear tests removed from v6/v8 | `graph_viz.py:api_clear_and_rebuild` (job/threading removed); `templates/graph.html` — button relabeled "Clear network", `clearAndRebuild()` simplified (no polling) | 8 `tests/test_features_v9.py` tests |
| **Critical bug fix (#7 → root cause of #1):** every `get_references`/`get_citations` call was returning `400 {"error":"Unrecognized or unsupported fields: [authors.hIndex]"}` — swallowed by the try/except, so ZERO references were ever fetched. `/references` and `/citations` support a narrower field set than `/paper/search`. Split out `NEIGHBOR_FIELDS` (no `authors.hIndex`) for neighbour fetches | `semantic_scholar_client.py:NEIGHBOR_FIELDS`, `get_references`, `get_citations` | 10 `tests/test_api_fields_and_batch.py` tests + live API verification |
| **#6** Batched, fault-isolated metadata hydration: `batch_get_papers` chunks at 5 ids and skips a failing chunk instead of losing the whole run | `semantic_scholar_client.py:batch_get_papers(batch_size=5)`, `BATCH_SIZE` | 4 batch-robustness tests |
| **#3** Strict admission filter: a paper must clear the broad field gate, hit a core AI/DS subdomain term (or a core AI venue), and carry no applied-domain signal (chemistry/medicine/finance/manufacturing/robotics/cybersecurity/…) | `field_filter.py:CORE_AI_SUBDOMAINS, CORE_AI_VENUES, APPLIED_DOMAIN_TERMS, is_core_ai_paper()` | 32 `tests/test_domain_filter.py` tests; verified 10/10 real core-AI papers kept, 5/5 applied rejected |
| **#2/#4/#5** Split expansion: `expand_references` (uncapped) and `expand_citations` (hybrid-score ranked, GUI budget split equally per seed with roll-over); both process fewest-neighbours-first, dedupe already-seen papers, hydrate abstracts in batches, and isolate per-paper failures | `expansion.py:expand_references, expand_citations, order_by_fewest_neighbors` | 16 `tests/test_two_phase_expansion.py` tests (incl. #1's random-seed 50-node test) |
| **#2/#8/#9/#10** GUI: separate "Expand references" / "Expand citations" buttons (budget on citations only); Dislike now removes with cascade; Seeds & Liked list refreshes in place instead of vanishing on removal; selecting a row in the seeds or neighbours list changes the current node | `graph_viz.py:api_expand_references, api_expand_citations`; `templates/graph.html:expandReferences, expandCitations, dislikeAndRemove, refreshSeedsLiked, renderSeedsLiked, selectNodeById` | 16 `tests/test_gui_two_buttons.py` tests |
| **Bug fix:** many seed papers ended up as isolated nodes with zero edges — the BFS's outer cap check used `break` (aborting the ENTIRE queue) whenever max_nodes/max_new was hit; since all seeds are enqueued first (FIFO), an early seed's own uncapped references exhausting the ceiling silently abandoned every later-queued seed, which then never got `_fetch_neighbors` called at all. Fixed: the cap check now skips (`continue`) only the individual capped non-seed candidate, and a seed/liked paper's own references are now exempt from max_nodes too (not just max_new) | `graph_expander.py:expand_network` — outer per-dequeue check and inner mid-wave check both made properly bypass-aware | 4 `tests/test_features_v10.py` tests; verified against the real DB (8/16 seeds had 0 edges before the fix — re-expansion after the fix repairs them) |

---

## Test Specification

| # | What is guaranteed | Test | Type | Result |
|---|--------------------|------|------|--------|
| 1 | NeurIPS venue scores 1.0 | `TestVenueScore::test_neurips_is_tier1` | unit | PASS |
| 2 | ICLR venue scores 1.0 | `TestVenueScore::test_iclr_is_tier1` | unit | PASS |
| 3 | ACL venue scores 1.0 | `TestVenueScore::test_acl_is_tier1` | unit | PASS |
| 4 | AISTATS venue scores 0.7 | `TestVenueScore::test_aistats_is_tier2` | unit | PASS |
| 5 | Unknown venue scores 0.3 | `TestVenueScore::test_unknown_venue_is_default` | unit | PASS |
| 6 | None venue scores 0.0 | `TestVenueScore::test_none_venue_returns_zero` | unit | PASS |
| 7 | Empty string venue scores 0.0 | `TestVenueScore::test_empty_venue_returns_zero` | unit | PASS |
| 8 | Venue matching is case-insensitive | `TestVenueScore::test_case_insensitive` | unit | PASS |
| 9 | CS paper is kept | `TestIsRelevantPaper::test_cs_paper_kept` | unit | PASS |
| 10 | Math paper is kept | `TestIsRelevantPaper::test_math_paper_kept` | unit | PASS |
| 11 | Biology-only paper is excluded | `TestIsRelevantPaper::test_biology_only_excluded` | unit | PASS |
| 12 | Medicine-only paper is excluded | `TestIsRelevantPaper::test_medicine_only_excluded` | unit | PASS |
| 13 | Economics-only paper is excluded | `TestIsRelevantPaper::test_economics_only_excluded` | unit | PASS |
| 14 | CS+Biology paper is kept (CS wins) | `TestIsRelevantPaper::test_cs_plus_biology_kept` | unit | PASS |
| 15 | Empty fields list — kept (no false negatives) | `TestIsRelevantPaper::test_empty_fields_kept` | unit | PASS |
| 16 | None fields — kept | `TestIsRelevantPaper::test_none_fields_kept` | unit | PASS |
| 17 | Engineering-only excluded | `TestIsRelevantPaper::test_engineering_excluded` | unit | PASS |
| 18 | `upsert_paper` returns own id when no DOI collision | `TestUpsertPaperReturnsActualId::test_returns_own_id_when_no_dedup` | integration | PASS |
| 19 | `upsert_paper` returns existing id on DOI collision | `TestUpsertPaperReturnsActualId::test_returns_existing_id_on_doi_collision` | integration | PASS |
| 20 | `liked` label not downgraded to `seed` | `TestLikedLabelPreservedOnDOIDedup::test_liked_not_downgraded_to_seed` | integration | PASS |
| 21 | `disliked` label not overwritten by `seed` | `TestLikedLabelPreservedOnDOIDedup::test_disliked_not_overwritten_by_seed` | integration | PASS |
| 22 | `source_api` updates when new API ingests paper | `TestSourceApiUpdated::test_source_api_updates_when_better_api_ingests` | integration | PASS |
| 23 | co_cit counts candidate-to-seed direction | `TestCoCitationCorrect::test_co_cit_includes_candidate_to_seed_direction` | integration | PASS |
| 24 | Search fallback exception returns None | `TestSearchFallbackExceptionReturnsNone::test_fallback_exception_returns_none_not_raises` | unit | PASS |
| 25 | Empty `/match` result is not cached | `TestEmptyMatchNotCached::test_empty_match_does_not_call_cache_set_with_empty_data` | unit | PASS |
| 26 | No orphan edges after DOI dedup in expand | `TestExpandNetworkNoOrphanEdges::test_all_edge_endpoints_have_paper_rows` | integration | PASS |
| 27 | `/api/graph` returns nodes+links JSON | `TestGraphVizApi::test_api_graph_returns_nodes_and_links` | integration | PASS |
| 28 | `/api/paper/<id>` returns full paper detail | `TestGraphVizApi::test_api_paper_detail` | integration | PASS |
| 29 | Higher-citation paper ranks above lower-citation paper | `TestCandidatesCitationOrdering::test_higher_citation_paper_ranks_above_lower` | integration | PASS |
| 64 | Missing/blank title returns HTTP 400 | `TestAddPaperByTitle::test_missing_title_returns_400` | integration | PASS |
| 65 | Blank whitespace-only title returns HTTP 400 | `TestAddPaperByTitle::test_empty_title_returns_400` | integration | PASS |
| 66 | No S2 match for title returns HTTP 404 | `TestAddPaperByTitle::test_no_s2_match_returns_404` | integration | PASS |
| 67 | Matched paper is stored with label="seed" | `TestAddPaperByTitle::test_stores_paper_as_seed` | integration | PASS |
| 68 | Response JSON includes paper_id, title, year, label | `TestAddPaperByTitle::test_returns_paper_json_with_correct_fields` | integration | PASS |
| 69 | Added paper appears in /api/graph nodes | `TestAddPaperByTitle::test_added_paper_appears_in_graph_api` | integration | PASS |
| 70 | Adding same paper twice does not duplicate; label remains seed | `TestAddPaperByTitle::test_adding_duplicate_title_is_idempotent` | integration | PASS |
| 71 | `.node.dimmed` CSS rule present in served HTML | `TestNodeHighlightFeature::test_dimmed_css_class_present` | structural | PASS |
| 72 | `.node.neighbor` CSS rule present in served HTML | `TestNodeHighlightFeature::test_neighbor_css_class_present` | structural | PASS |
| 73 | `.link.dimmed` CSS rule present in served HTML | `TestNodeHighlightFeature::test_link_dimmed_css_class_present` | structural | PASS |
| 74 | `highlightNeighborhood()` JS function present in served HTML | `TestNodeHighlightFeature::test_highlight_neighborhood_function_present` | structural | PASS |
| 75 | `clearHighlight()` JS function present in served HTML | `TestNodeHighlightFeature::test_clear_highlight_function_present` | structural | PASS |

---

## Coverage

```
Command: python -m pytest -p no:dash tests/ --tb=short
Result:  297 passed in 16.10s
```

Known gaps intentionally left:
- `graph_expander.py` S2/OpenAlex live network paths — require network mocking
- `recommend.py` full CLI flow — covered by existing `test_bug_fixes.py` tests
- `build_network.py` orchestrator — needs live DB fixture

---

## Files Created / Modified

| File | Status |
|------|--------|
| `venue_config.py` | CREATED |
| `field_filter.py` | CREATED |
| `graph_viz.py` | CREATED |
| `templates/graph.html` | CREATED |
| `storage.py` | MODIFIED (3 bug fixes) |
| `citation_network.py` | MODIFIED (co_cit fix + cit_score ranking) |
| `semantic_scholar_client.py` | MODIFIED (empty cache + fallback exception) |
| `graph_expander.py` | MODIFIED (orphan edge fix + field filter + venue priority) |
| `requirements.txt` | MODIFIED (added flask>=3.0) |
| `tests/test_features_v2.py` | CREATED (29 tests) |
