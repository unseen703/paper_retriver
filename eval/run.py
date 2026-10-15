"""
`make eval` (R4.6): benchmark, baselines, ranker -> `eval/results/eval.json`.

    uv run python eval/run.py [--db URL] [--seed N] [--out DIR] [--limit N]

Everything here is offline: the pools come from the stored corpus and the
features from `pools.build_scored_pool`. The one baseline PLAN.md names that
needs the network -- S2's `/recommendations` -- is therefore *not* run, and the
results file says so under `not_run` instead of omitting it quietly.

**Reproducible by construction.** One seed drives case sampling and the random
baseline; ids are processed in sorted order; the JSON is written with sorted
keys. Same corpus, seed and config give a byte-identical file (CLAUDE.md
rule 7).
"""

from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from baselines import (
    Candidate,
    by_anchor_overlap,
    by_citation_count,
    by_citations_per_year,
    uniformly_random,
    unranked_bfs,
)
from build_benchmark import BenchmarkCase, eligible_targets, make_case, references_of
from channel import channel_ids, not_run, recovery, summarize
from pools import build_scored_pool
from simulate import simulate_user
from sqlalchemy import Connection
from sweep import ablation_configs, rank_pool, summarize_rankings, write_results

from app.config import filters, ranking
from app.db import make_engine
from app.repo import papers as papers_repo
from app.services.candidates import PoolEntry
from app.services.embeddings import EmbeddingStore

DEFAULT_SEED = 20260916
DEFAULT_OUT = Path("eval/results")

METHODS = (
    "ranker",
    "random",
    "citation_count",
    "unranked_bfs",
    "citations_per_year",
    "anchor_overlap",
)

#: Named in PLAN.md, requires the network, so reported as not run.
NOT_RUN = {
    "s2_recommendations": "needs the network; this harness runs offline against the stored corpus",
}


def build_cases(conn: Connection, seed: int, limit: int) -> list[BenchmarkCase]:
    """Eligible targets in id order, each split under its own seeded RNG."""
    cases: list[BenchmarkCase] = []
    for target in eligible_targets(conn, limit=limit):
        # Per-target RNG: dropping one target must not reshuffle every other
        # case's split, which a single shared stream would do.
        case = make_case(target, references_of(conn, target), random.Random(f"{seed}:{target}"))
        if case is not None:
            cases.append(case)
    return cases


def _candidates(entries: Sequence[PoolEntry], years: Mapping[int, int | None]) -> list[Candidate]:
    return [
        Candidate(e.paper_id, e.citation_count, years.get(e.paper_id), e.anchor_overlap)
        for e in entries
    ]


def run_eval(
    conn: Connection,
    *,
    seed: int,
    as_of_year: int,
    limit: int = 300,
    embeddings: EmbeddingStore | None = None,
) -> dict[str, object]:
    """Score the real ranker and every offline baseline on the same pools."""
    cases = build_cases(conn, seed, limit)
    rankings: dict[str, dict[int, list[int]]] = {name: {} for name in METHODS}
    truths: dict[int, frozenset[int]] = {}
    pools = []
    reachable: list[float] = []
    channel = []

    for case_id, case in enumerate(cases, start=1):
        pool, entries = build_scored_pool(conn, case_id, case, filters, as_of_year)
        truths[case_id] = pool.ground_truth
        pools.append(pool)
        years = {
            p.id: p.year
            for p in papers_repo.get_papers_by_ids(conn, [e.paper_id for e in entries])
            if p.id is not None
        }
        cands = _candidates(entries, years)

        rankings["ranker"][case_id] = rank_pool(pool, ranking.weights.model_dump())
        rankings["random"][case_id] = uniformly_random(cands, random.Random(f"{seed}:r:{case_id}"))
        rankings["citation_count"][case_id] = by_citation_count(cands)
        rankings["unranked_bfs"][case_id] = unranked_bfs(cands)
        rankings["citations_per_year"][case_id] = by_citations_per_year(cands, as_of_year)
        rankings["anchor_overlap"][case_id] = by_anchor_overlap(cands)

        # The ceiling no ranking can pass: ground truth that is in the pool at all.
        truth = pool.ground_truth
        if embeddings is not None and len(embeddings):
            channel.append(recovery(truth, pool.features, channel_ids(conn, embeddings, case)))
        reachable.append(len(truth & set(pool.features)) / len(truth) if truth else 0.0)

    # Leave-one-weight-out: what each term in the score is worth. Same pools,
    # same metrics code; only the weight under test changes.
    base = ranking.weights.model_dump()
    ablations = {
        name: summarize_rankings({p.case_id: rank_pool(p, w) for p in pools}, truths)
        for name, w in ablation_configs(base).items()
    }

    simulated = simulate_user(pools, base, seed)

    return {
        "ablations": ablations,
        "simulated_user": simulated,
        "n_cases": len(cases),
        "seed": seed,
        "as_of_year": as_of_year,
        "config_version": ranking.config_version,
        # Reported beside the scores: a Recall@50 of 0.2 means something
        # different under a ceiling of 0.25 than under 0.9.
        "pool_recall_ceiling": sum(reachable) / len(reachable) if reachable else 0.0,
        "embedding_channel": (
            summarize(channel)
            if channel
            else not_run("no embedding store given (--embeddings), or it is empty")
        ),
        "methods": {name: summarize_rankings(rankings[name], truths) for name in METHODS},
        "not_run": NOT_RUN,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite URL (default: the configured database)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--as-of-year", type=int, default=datetime.now().year)
    parser.add_argument("--embeddings", type=Path, help="embedding store stem (.npy/.json)")
    args = parser.parse_args(argv)

    engine = make_engine(args.db)
    with engine.connect() as conn:
        store = EmbeddingStore.load(args.embeddings) if args.embeddings else None
        result = run_eval(
            conn, seed=args.seed, as_of_year=args.as_of_year, limit=args.limit, embeddings=store
        )

    if result["n_cases"] == 0:
        print("no eligible benchmark cases in this corpus", file=sys.stderr)
        return 1
    path = args.out / "eval.json"
    write_results(result, path)
    print(f"{result['n_cases']} cases -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
