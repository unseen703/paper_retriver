"""
Information-retrieval metrics for the benchmark (R4.1).

BUILD.md asks for Recall@{10,20,50}, NDCG@20, MRR, Hit@10 and bootstrap 95%
confidence intervals. PLAN.md M9 explains why this release comes before
personalization: there is no point tuning weights against a number nobody has
checked.

**Pure, and deliberately dependency-free.** Every function here takes a ranked
list and a set of ground-truth ids and returns a float. No DB, no config, no
clock -- which is what lets each one be tested against an answer worked out by
hand rather than against whatever the code produced the first time.

**Relevance is binary.** Reference lists give "cited" or "not cited"; there are
no graded judgements to be had, so DCG's gain is 1 or 0 and the formulae
simplify accordingly. Pretending to a graded scale would dress up a judgement
the data does not contain.
"""

from __future__ import annotations

import math
import random
from collections.abc import Collection, Sequence
from typing import TypeVar

ID = TypeVar("ID")

#: Resamples per interval. A thousand is the usual floor for a stable 95%
#: interval and costs milliseconds at benchmark sizes.
BOOTSTRAP_ITERATIONS = 1000

#: Fixed so an interval is reproducible (CLAUDE.md rule 7). A resampling method
#: is random by nature, and an interval that moved between runs would make
#: every comparison between two weight configurations unfalsifiable.
BOOTSTRAP_SEED = 20260916


def _first_k_unique(ranked: Sequence[ID], k: int) -> list[ID]:
    """
    The first `k` results, with repeats collapsed.

    A ranker that emits the same paper twice has not found two papers, and
    counting it twice would let a buggy ranker score better than a correct one.
    """
    seen: set[ID] = set()
    out: list[ID] = []
    for item in ranked[:k]:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def recall_at_k(ranked: Sequence[ID], relevant: Collection[ID], k: int) -> float:
    """
    Share of the ground truth that appears in the top `k`.

    The denominator is what there *was* to find, not what was returned.
    Dividing by `k` would make a short list look better than a long one holding
    the same hits -- which is precision's question answered under recall's name.

    No ground truth returns 0.0 rather than raising: a benchmark case whose
    references were entirely filtered out is a real thing to hit, and losing a
    whole sweep to a ZeroDivisionError halfway through is not a useful failure.
    """
    truth = set(relevant)
    if not truth:
        return 0.0
    found = sum(1 for item in _first_k_unique(ranked, k) if item in truth)
    return found / len(truth)


def hit_at_k(ranked: Sequence[ID], relevant: Collection[ID], k: int) -> float:
    """1.0 if anything relevant made the top `k`. The weakest useful question."""
    truth = set(relevant)
    return 1.0 if any(item in truth for item in ranked[:k]) else 0.0


def reciprocal_rank(ranked: Sequence[ID], relevant: Collection[ID]) -> float:
    """
    1 / (rank of the first relevant result), one-indexed. Zero if none.

    Zero rather than undefined, because MRR averages across cases and a case
    that found nothing has to contribute something. Dropping misses instead
    would report the mean over only the cases that worked.
    """
    truth = set(relevant)
    for index, item in enumerate(ranked, start=1):
        if item in truth:
            return 1 / index
    return 0.0


def ndcg_at_k(ranked: Sequence[ID], relevant: Collection[ID], k: int) -> float:
    """
    Normalized discounted cumulative gain at `k`, binary relevance.

    What NDCG buys over recall is position sensitivity: a ranker that finds the
    same papers lower down scores less, which is the difference between a list
    someone reads and a list someone scrolls.

    **IDCG is computed over what is achievable in `k` slots**, not over all of
    ground truth. With two relevant papers and one slot, the best any ranker
    could do is one hit at rank 1 -- measuring the ideal against both would cap
    the achievable score below 1 and make a perfect ranking look imperfect.
    """
    truth = set(relevant)
    if not truth:
        return 0.0

    gains = _first_k_unique(ranked, k)
    dcg = sum(1 / math.log2(rank + 1) for rank, item in enumerate(gains, start=1) if item in truth)
    ideal_hits = min(len(truth), k)
    idcg = sum(1 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def bootstrap_ci(
    values: Sequence[float],
    confidence: float = 0.95,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """
    Percentile bootstrap interval for the mean of `values`.

    The interval is the point of the whole harness. A headline "Recall@20 =
    0.41" over a 150-case benchmark is worth very little without a width beside
    it, and the width is what says whether one weight configuration actually
    beat another or merely sampled better.

    Resampling with replacement makes no assumption about the distribution,
    which matters because per-case recall is bounded, discrete and usually
    nowhere near normal.

    Seeded, so two runs of the same benchmark produce the same interval.
    """
    if not values:
        return (0.0, 0.0)

    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(iterations))

    tail = (1 - confidence) / 2
    low = means[int(tail * iterations)]
    high = means[min(int((1 - tail) * iterations), iterations - 1)]
    return (low, high)


__all__ = [
    "BOOTSTRAP_ITERATIONS",
    "BOOTSTRAP_SEED",
    "bootstrap_ci",
    "hit_at_k",
    "ndcg_at_k",
    "recall_at_k",
    "reciprocal_rank",
]
