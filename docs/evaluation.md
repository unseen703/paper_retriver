# Evaluation

How we decide whether the ranker works. `make eval` (`uv run python eval/run.py`)
writes `eval/results/eval.json`; the sweep and ablations are `eval/sweep.py`.

**Status: protocol and limitations only.** The results tables below are empty on
purpose. The committed corpus has 6 eligible benchmark targets against the
150–300 the protocol asks for (only 12 of 1,596 papers have stored references;
the rest are stubs), and no `data/app.db` exists in CI. Numbers from 6 cases would
be noise with confidence intervals attached. Fill the tables after a crawl that
reaches the target count, from the JSON, not by hand.

## Protocol

1. **Targets**: core-ML papers, 2019–2024, with ≥15 *stored* reference edges
   (`build_benchmark.eligible_targets`).
2. **Case**: 3 seeds sampled from the target's references (seeded per target);
   ground truth = the remaining references.
3. **Temporal cutoff**: `min(seed.publication_date)`. Candidates not provably on
   or before it are invisible (`eval/temporal.py`); undated candidates are dropped,
   which costs recall rather than leaking the future.
4. **Pool**: what an expansion from the seeds would have offered, built by
   `eval/pools.py`; the ranker's features are computed on that pool.
5. **Methods**: the ranker, plus offline baselines — random, citation count,
   unranked 1-hop BFS, citations/year, anchor-overlap only.
   S2 `/recommendations` needs the network and is listed under `not_run`.
6. **Metrics**: Recall@{10,20,50}, NDCG@20, MRR, Hit@10, mean over cases with
   bootstrap 95% CIs (`eval/metrics.py`).
7. **Ablations / sweep**: leave-one-weight-out and weight grids (`eval/sweep.py`).
8. **Simulated user** (R5.5): ground-truth papers revealed as likes one at a
   time, weights Rocchio-nudged, Recall@20 re-measured. Revealed papers leave
   both ranking and truth.
9. **Reproducibility**: one seed, sorted ids, `(-score, paper_id)` ordering,
   sorted-key JSON. Same corpus, seed and config give a byte-identical file.

Always report `pool_recall_ceiling` beside any score: ground truth outside the
pool is a miss for every method, and a ranker's 0.2 means something different
under a ceiling of 0.25 than 0.9.

## Results

| Method | Recall@10 | Recall@20 | Recall@50 | NDCG@20 | MRR | Hit@10 |
|---|---|---|---|---|---|---|
| ranker | — | — | — | — | — | — |
| random | — | — | — | — | — | — |
| citation_count | — | — | — | — | — | — |
| unranked_bfs | — | — | — | — | — | — |
| citations_per_year | — | — | — | — | — | — |
| anchor_overlap | — | — | — | — | — | — |

Ablations (≥3), the simulated-user curve, and whether PPR beats co-citation
(R5; `ppr` stays at weight 0.00 until it does): **pending a full benchmark.**
If PPR does not beat co-citation, keep the simpler model and say so here.

## Failure analysis

Pending results. Known in advance: per-case difficulty is dominated by which
three references were sampled. One case had a cutoff of 2009-07-19, leaving 12
of 56 ground-truth papers reachable at all.

## Limitations

- **Reference lists are a biased proxy for relevance.** They omit concurrent
  work, omit deliberately uncited competitors, and include obligatory citations
  the author never read.
- **Recall is capped by reachability.** A relevant paper beyond the pool (or past
  `max_depth`) is a miss regardless of ranking.
- **Citation-count baselines are artificially strong.** Popular papers appear in
  many reference lists; beating that baseline narrowly is not impressive.
- **Author self-citation** inflates ground truth for prolific authors.
- **Leakage that remains.** `citation_count` and the citing side of co-citation
  are today's values, not as-of-cutoff, so they leak slightly in the ranker's
  favour. The temporal guard covers *which papers are visible*, not *what is
  known about them*.
- **Filters** applied to the pool are those the harness can reconstruct; the pool
  may be wider than a live expansion's.
- **Era floor**: pre-2015 ground truth is stored as boundary papers but never
  drawn, so it counts against the ceiling for every method.
