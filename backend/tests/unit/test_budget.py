"""
R1.10 -- budget allocation (Appendix B.4, PLAN.md section E5).

This is the anti-explosion mechanism. Without it an expansion returns whatever
the ranker liked best, which in practice means fifty papers from one prolific
seed's reference list.

The ladder, in order:

    recency lane      15% of budget, papers under a year old
    direction floors  20% each of what remains, so neither direction starves
    remainder         by score
    per-source cap    no single anchor supplies more than 40%

Each rung exists to counter a specific failure. The recency lane exists because
score correlates with citations and citations accrue with age, so pure ranking
never surfaces last month's paper. The direction floors exist because backward
yields thin out as you go deeper (a 2018 paper's references are mostly
pre-2015), and without a floor the ranker abandons that direction entirely. The
source cap exists because one hub-adjacent seed can otherwise dominate.

BUILD.md's verification: budget=20 returns exactly 20 from a large pool, at
least 3 recent if available, no source over 8, and deterministic under shuffled
input with ties broken on paper_id.
"""

from __future__ import annotations

import random

import pytest

from app.config import ranking as ranking_cfg
from app.services.budget import allocate
from app.services.candidates import PoolEntry


def _entry(
    paper_id: int,
    *,
    score: float = 1.0,
    age: float = 5.0,
    direction: str = "BACKWARD",
    source: int = 100,
) -> tuple[PoolEntry, float]:
    entry = PoolEntry(
        paper_id=paper_id,
        anchor_overlap=1,
        citation_count=50,
        age_years=age,
        direction=direction,
        source_ids=(source,),
    )
    return entry, score


def _pool(n: int, **kw: object) -> list[tuple[PoolEntry, float]]:
    return [_entry(i, score=float(n - i), **kw) for i in range(n)]  # type: ignore[arg-type]


def _ids(picked: list[tuple[PoolEntry, float]]) -> list[int]:
    return [e.paper_id for e, _ in picked]


# --------------------------------------------------------------------------
# BUILD.md's four named verifications
# --------------------------------------------------------------------------


def test_a_large_pool_returns_exactly_the_budget() -> None:
    assert len(allocate(_pool(200), ranking_cfg.budget, 20)) == 20


def test_at_least_three_recent_papers_when_available() -> None:
    """15% of 20 is 3. The recency lane is reserved before anything else."""
    pool = _pool(100, age=8.0)
    pool += [_entry(1000 + i, score=0.01, age=0.5)[0:2] for i in range(10)]
    picked = allocate(pool, ranking_cfg.budget, 20)
    assert sum(1 for e, _ in picked if e.age_years < 1.0) >= 3


def test_no_single_source_exceeds_the_cap() -> None:
    """40% of 20 is 8. One prolific seed must not supply the whole expansion."""
    pool = [_entry(i, score=float(100 - i), source=7)[0:2] for i in range(50)]
    pool += [_entry(500 + i, score=1.0, source=8 + i)[0:2] for i in range(50)]
    picked = allocate(pool, ranking_cfg.budget, 20)
    from collections import Counter

    counts = Counter(src for e, _ in picked for src in e.source_ids)
    assert max(counts.values()) <= 8


def test_allocation_is_deterministic_under_shuffled_input() -> None:
    """CLAUDE.md rule 7. Ties break on paper_id, never on list position."""
    pool = _pool(60)
    rng = random.Random(0)
    shuffled = pool[:]
    rng.shuffle(shuffled)
    assert _ids(allocate(pool, ranking_cfg.budget, 20)) == _ids(
        allocate(shuffled, ranking_cfg.budget, 20)
    )


def test_equal_scores_break_on_paper_id() -> None:
    pool = [_entry(i, score=1.0)[0:2] for i in (9, 3, 7, 1, 5)]
    assert _ids(allocate(pool, ranking_cfg.budget, 3)) == [1, 3, 5]


# --------------------------------------------------------------------------
# The recency lane
# --------------------------------------------------------------------------


def test_a_recent_paper_is_taken_over_a_better_scoring_old_one() -> None:
    """
    The lane's whole purpose: score tracks citations, citations accrue with age,
    so pure ranking never surfaces a paper published last month.
    """
    pool = [_entry(i, score=100.0, age=10.0)[0:2] for i in range(10)]
    pool.append(_entry(999, score=0.001, age=0.2)[0:2])
    assert 999 in _ids(allocate(pool, ranking_cfg.budget, 5))


def test_the_lane_takes_the_best_recent_papers_not_arbitrary_ones() -> None:
    recent = [_entry(i, score=float(i), age=0.5)[0:2] for i in range(10)]
    picked = allocate(recent, ranking_cfg.budget, 4)
    assert _ids(picked) == [9, 8, 7, 6]


def test_no_recent_papers_means_the_lane_is_simply_skipped() -> None:
    """A backward-only expansion into old literature must still fill up."""
    assert len(allocate(_pool(50, age=12.0), ranking_cfg.budget, 20)) == 20


def test_the_lane_does_not_overrun_the_budget() -> None:
    assert len(allocate(_pool(50, age=0.2), ranking_cfg.budget, 10)) == 10


# --------------------------------------------------------------------------
# Direction floors
# --------------------------------------------------------------------------


def test_a_starved_direction_still_gets_a_floor() -> None:
    """
    Backward yields thin out with depth -- a 2018 paper's references are mostly
    pre-2015. Without a floor the ranker abandons the direction entirely.
    """
    pool = [_entry(i, score=100.0, direction="FORWARD")[0:2] for i in range(50)]
    pool += [_entry(500 + i, score=0.01, direction="BACKWARD")[0:2] for i in range(10)]
    picked = allocate(pool, ranking_cfg.budget, 20)
    assert sum(1 for e, _ in picked if e.direction == "BACKWARD") >= 3


def test_a_direction_with_no_candidates_does_not_reserve_slots() -> None:
    """An unavailable floor must not shrink the result below budget."""
    pool = [_entry(i, score=float(50 - i), direction="BACKWARD")[0:2] for i in range(50)]
    assert len(allocate(pool, ranking_cfg.budget, 20)) == 20


def test_both_directions_present_yields_both() -> None:
    pool = [_entry(i, score=float(50 - i), direction="BACKWARD")[0:2] for i in range(25)]
    pool += [_entry(100 + i, score=float(50 - i), direction="FORWARD")[0:2] for i in range(25)]
    picked = allocate(pool, ranking_cfg.budget, 20)
    directions = {e.direction for e, _ in picked}
    assert directions == {"BACKWARD", "FORWARD"}


# --------------------------------------------------------------------------
# The source cap
# --------------------------------------------------------------------------


def test_a_candidate_reached_by_many_sources_is_not_penalised() -> None:
    """
    The cap limits how much ONE source contributes, not how many sources a
    candidate has. A widely-shared paper is the best kind.
    """
    shared = PoolEntry(
        paper_id=1,
        anchor_overlap=4,
        citation_count=50,
        age_years=5.0,
        direction="BACKWARD",
        source_ids=(1, 2, 3, 4),
    )
    picked = allocate([(shared, 10.0)] + _pool(30), ranking_cfg.budget, 5)
    assert 1 in _ids(picked)


def test_the_cap_is_relaxed_rather_than_returning_short() -> None:
    """
    When every candidate comes from one source, returning 8 of a requested 20
    would be worse than exceeding the cap. The cap shapes a diverse pool; it
    does not shrink a homogeneous one.
    """
    pool = [_entry(i, score=float(50 - i), source=1)[0:2] for i in range(50)]
    assert len(allocate(pool, ranking_cfg.budget, 20)) == 20


# --------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------


def test_an_empty_pool_yields_nothing() -> None:
    assert allocate([], ranking_cfg.budget, 20) == []


def test_a_pool_smaller_than_the_budget_returns_all_of_it() -> None:
    assert len(allocate(_pool(7), ranking_cfg.budget, 20)) == 7


def test_a_zero_budget_yields_nothing() -> None:
    assert allocate(_pool(50), ranking_cfg.budget, 0) == []


def test_no_candidate_is_returned_twice() -> None:
    """Four rungs each pick from the pool; double-counting would be invisible."""
    picked = allocate(_pool(200), ranking_cfg.budget, 40)
    ids = _ids(picked)
    assert len(ids) == len(set(ids))


def test_the_fractions_come_from_config() -> None:
    b = ranking_cfg.budget
    assert (b.recency_lane_frac, b.direction_floor_frac, b.source_cap_frac) == (0.15, 0.20, 0.40)


@pytest.mark.parametrize("budget", [1, 2, 3, 5, 13, 20, 47, 100])
def test_any_budget_returns_exactly_that_many_from_a_large_pool(budget: int) -> None:
    """Integer division in four rungs is where off-by-ones hide."""
    pool = _pool(300, age=0.5)
    assert len(allocate(pool, ranking_cfg.budget, budget)) == budget
