"""
Ranking: rank-percentile normalization, and the weighted sum (R3).

**Pure.** CLAUDE.md rule 3: no I/O, no DB, no network. Features come in as a
dict, weights come in as a dict, a number comes out. That is what makes
`PUT /api/config` able to rescore an entire graph without a single API call,
and it is what lets every test in `test_ranking.py` be a plain function call.

**Rank-percentile, not z-score.** PLAN.md is emphatic and the reason is
arithmetic rather than taste:

    "Citation counts are power-law distributed; one hub gives a z-score of 40
    and flattens every other feature to noise. Percentile rank is
    outlier-immune and makes weights directly interpretable as relative
    importance."

Concretely: a pool holding papers with 3, 5, 9 and 190,000 citations gives the
hub a z-score around 1.7 and squeezes the other three into a band 0.0002 wide.
A `quality` weight of 0.4 then contributes nothing at all for anyone except the
hub -- and nothing about the resulting score looks wrong, which is why this is
worth a module rather than a line.

Under rank-percentile those three papers land at 0, 1/3 and 2/3 regardless of
how far away the hub is. The weight then means what a reader would assume it
means: relative importance.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar

#: Whatever a caller keys its pool by -- a paper id here, a name in the tests.
#: Generic so the key type survives the call rather than widening to a union
#: every caller then has to narrow again.
K = TypeVar("K")

#: What a degenerate pool scores. A pool where every value is identical -- or
#: which holds a single paper -- has no ranking to express, and the midpoint
#: says "this feature has nothing to contribute here" without favouring anyone.
NEUTRAL = 0.5


def rank_percentile(values: Mapping[K, float]) -> dict[K, float]:
    """
    Map each value to its rank as a fraction of the range, in [0, 1].

    Ties share a percentile. Two papers with the same citation count are
    equally good on that feature, and breaking the tie by id would let
    insertion order leak into a score -- the kind of thing that makes a ranking
    irreproducible without ever looking wrong.

    The endpoints are pinned at 0 and 1 so a weight means the same thing in
    every pool, which is the entire purpose of normalizing.
    """
    if not values:
        return {}

    distinct = sorted(set(values.values()))
    if len(distinct) == 1:
        # Nothing distinguishes anyone. Dividing by a zero range is the obvious
        # way to crash here; the midpoint is the honest answer instead.
        return dict.fromkeys(values, NEUTRAL)

    # Rank of each distinct value, normalized by the number of *gaps* rather
    # than the number of values -- that is what puts the smallest at 0 and the
    # largest at 1 instead of at 1/n and 1.
    span = len(distinct) - 1
    rank_of = {value: index / span for index, value in enumerate(distinct)}
    return {key: rank_of[value] for key, value in values.items()}


def citations_per_year(citation_count: int, year: int | None, as_of_year: int) -> float:
    """
    Age-normalized citations: how fast a paper accumulates them, not how many.

    A 2016 paper with 400 citations and a 2024 paper with 120 are not ranked by
    raw count without deciding that older is better -- which for a discovery
    tool is precisely backwards, since the old ones are the ones you have
    already read.

    The denominator is `max(1, age)`. A paper published this year has age 0,
    and dividing by it would be an infinity that dominates every ranking it
    appears in; treating its first year as a whole one merely understates it
    slightly, which is the safe direction.

    An unknown year returns the raw count. PLAN.md's rule for missing fields is
    that they degrade a score rather than raise, and the count is the honest
    unnormalized fallback -- `rank_percentile` will place it among its peers
    regardless.
    """
    if year is None:
        return float(citation_count)
    age = max(1, as_of_year - year + 1)
    return citation_count / age


def recency(year: int | None, as_of_year: int, half_life: float = 4.0) -> float:
    """
    Exponential decay by age, in [0, 1]. A paper from this year scores 1.

    Exponential rather than linear because "5 years old versus 6" matters far
    less than "this year versus last", and a linear ramp has to pick an
    arbitrary cutoff at which a paper's recency becomes exactly zero.

    `half_life=4` means a four-year-old paper scores 0.5. An unknown year
    scores 0: it cannot be shown to be recent, and guessing in the generous
    direction would let every paper with missing metadata outrank a real
    five-year-old one.
    """
    if year is None:
        return 0.0
    age = max(0, as_of_year - year)
    return float(2.0 ** (-age / half_life))


def score_paper(
    features: Mapping[str, float], weights: Mapping[str, float]
) -> tuple[float, dict[str, float]]:
    """
    The weighted sum, and the per-term contributions behind it.

    Returns `(score, breakdown)` where the breakdown's values sum to the score.
    `<ScoreBreakdown>` renders those as bars beside the total, and a breakdown
    that does not add up is worse than none -- it looks like an explanation
    while being one.

    **The weights decide what counts.** A feature with no weight is ignored, so
    a new signal can be computed and persisted before anyone decides what it is
    worth. A weight with no feature contributes nothing, because treating a
    missing feature as a zero -- or worse, as a default -- would invent a score
    out of a gap in the data.

    **A zero weight is omitted from the breakdown.** `ranking.yaml` ships
    several at 0.00 for features that arrive in later releases, and rendering a
    zero-length bar for each would fill the panel with terms that say nothing.

    Terms are accumulated in sorted order so two runs agree byte for byte
    (CLAUDE.md rule 7). Float addition is not associative, and a ranking that
    changes in its last digits between runs makes every bug in it
    unfalsifiable.
    """
    breakdown: dict[str, float] = {}
    for name in sorted(weights):
        weight = weights[name]
        if weight == 0.0 or name not in features:
            continue
        breakdown[name] = weight * features[name]

    return sum(breakdown.values()), breakdown


__all__ = ["NEUTRAL", "citations_per_year", "rank_percentile", "recency", "score_paper"]
