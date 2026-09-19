"""
R4.4 -- the baseline suite.

Journey:

    As someone reading "Recall@20 = 0.41", I want to know what the same number
    is for doing something trivial, so I can tell whether the ranker earned it.

BUILD.md: "**Baseline suite (mandatory -- a metric without baselines is
decoration)**", and names seven. Five of them are pure functions over a
candidate pool and live here. The sixth is S2's own `/recommendations`, which
needs the network and belongs to the harness; the seventh is the real ranker.

**The citation-count baseline is the one that matters, and it is stacked in its
own favour.** PLAN.md says so plainly: popular papers appear in many reference
lists, so ranking by citations is artificially strong on a benchmark built from
reference lists. Beating it by a little is not a result, and the write-up has
to say that rather than celebrate it.

Every baseline is deterministic and ties break on `paper_id` (CLAUDE.md rule 7)
-- otherwise two runs of the same benchmark would disagree for reasons that have
nothing to do with the ranking being measured.
"""

from __future__ import annotations

import random

from baselines import (
    Candidate,
    by_anchor_overlap,
    by_citation_count,
    by_citations_per_year,
    uniformly_random,
    unranked_bfs,
)

AS_OF = 2026


def _c(
    paper_id: int, *, citations: int = 10, year: int | None = 2020, overlap: int = 1
) -> Candidate:
    return Candidate(paper_id=paper_id, citation_count=citations, year=year, anchor_overlap=overlap)


POOL = [
    _c(3, citations=500, year=2016, overlap=1),
    _c(1, citations=100, year=2024, overlap=3),
    _c(2, citations=100, year=2015, overlap=2),
]

ALL_BASELINES = (
    lambda pool: by_citation_count(pool),
    lambda pool: by_anchor_overlap(pool),
    lambda pool: by_citations_per_year(pool, AS_OF),
    lambda pool: unranked_bfs(pool),
    lambda pool: uniformly_random(pool, random.Random(1)),
)


# --------------------------------------------------------------------------
# Properties every baseline must have
# --------------------------------------------------------------------------


def test_every_baseline_returns_each_candidate_exactly_once() -> None:
    # A baseline that drops or duplicates candidates is not ranking the same
    # pool the real ranker sees, so the comparison would be meaningless.
    for rank in ALL_BASELINES:
        assert sorted(rank(POOL)) == [1, 2, 3]


def test_every_baseline_is_deterministic() -> None:
    """
    Rule 7. Two runs of the same benchmark must produce the same ordering, or a
    weight change and a reshuffle become indistinguishable.
    """
    for rank in ALL_BASELINES:
        assert rank(POOL) == rank(POOL)


def test_every_baseline_survives_an_empty_pool() -> None:
    for rank in ALL_BASELINES:
        assert rank([]) == []


# --------------------------------------------------------------------------
# What each one actually ranks by
# --------------------------------------------------------------------------


def test_citation_count_ranks_by_raw_popularity() -> None:
    assert by_citation_count(POOL)[0] == 3


def test_citation_count_breaks_ties_on_id() -> None:
    # Papers 1 and 2 both have 100 citations.
    assert by_citation_count(POOL)[1:] == [1, 2]


def test_citations_per_year_prefers_the_faster_accumulator() -> None:
    """
    The point of this baseline is that it asks a *different* question from raw
    count -- the one the real ranker's `quality` feature asks -- so the case has
    to be one where the two disagree.

        paper 1:  200 / (2026 - 2024 + 1) = 200 / 3  = 66.7   <- fewer, faster
        paper 3:  500 / (2026 - 2016 + 1) = 500 / 11 = 45.5   <- more, slower

    An earlier version of this test used the shared pool, where 500 since 2016
    is 45/yr against 100 since 2024 at 33/yr -- the older paper wins on rate
    too, so it demonstrated nothing and the arithmetic was simply mine being
    wrong.
    """
    pool = [_c(3, citations=500, year=2016), _c(1, citations=200, year=2024)]
    assert by_citations_per_year(pool, AS_OF) == [1, 3]
    assert by_citation_count(pool) == [3, 1], "raw count must still prefer the older paper"


def test_citations_per_year_treats_an_unknown_year_as_the_raw_count() -> None:
    # CLAUDE.md rule 6: a missing field degrades, never raises. Matches
    # `ranking.citations_per_year`, so the baseline and the feature agree.
    pool = [_c(1, citations=50, year=None), _c(2, citations=10, year=2025)]
    assert by_citations_per_year(pool, AS_OF)[0] == 1


def test_anchor_overlap_ranks_by_how_many_seeds_agree() -> None:
    """
    The single feature PLAN.md calls dominant, on its own. If the full ranker
    cannot beat this, most of R3 is not earning its place -- which is exactly
    the kind of finding the harness exists to produce.
    """
    assert by_anchor_overlap(POOL) == [1, 2, 3]


def test_unranked_bfs_keeps_the_pool_in_id_order() -> None:
    """
    The "did ranking help at all?" control: everything reachable in one hop, in
    no particular order. Id order rather than arrival order, because arrival
    order depends on which anchor was fetched first.
    """
    assert unranked_bfs(POOL) == [1, 2, 3]


def test_random_is_a_permutation_and_seed_dependent() -> None:
    assert sorted(uniformly_random(POOL, random.Random(1))) == [1, 2, 3]
    orderings = {tuple(uniformly_random(POOL, random.Random(s))) for s in range(12)}
    assert len(orderings) > 1, "a 'random' baseline that never varies is not random"


def test_random_is_the_floor_the_others_must_clear() -> None:
    # Not a behaviour test so much as a statement of purpose: if a baseline
    # cannot beat shuffling, the pool itself is carrying the score.
    assert uniformly_random(POOL, random.Random(5)) != []
