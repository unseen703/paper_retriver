"""
The baseline suite (R4.4).

BUILD.md: "**Baseline suite (mandatory -- a metric without baselines is
decoration)**". Seven are named; five are pure functions over a candidate pool
and live here:

    1. random from the pool          the floor everything must clear
    2. citation-count only           the one that is hard to beat
    3. unranked 1-hop BFS            did ranking help at all?
    4. citations/year only           `quality` on its own
    5. anchor_overlap only           the dominant feature on its own

The sixth -- S2's own `/recommendations` -- needs the network and belongs to the
harness rather than to a pure module. The seventh is the real ranker.

**Baseline 2 is stacked in its own favour, and that has to be said out loud.**
PLAN.md: popular papers appear in many reference lists, so ranking by raw
citations is artificially strong on a benchmark *built from* reference lists.
Beating it by a little is not a result.

**Baseline 5 is the uncomfortable one.** `anchor_overlap` is a single integer
counted during pooling. If the full R3 ranker -- six features, rank-percentile
normalization, a weighted sum -- cannot beat it, most of R3 is not earning its
place, and the honest response is to write that down rather than to tune until
the number moves.

Every function is deterministic and ties break on `paper_id` (CLAUDE.md rule 7).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

#: Matches `ranking.citations_per_year`'s guard: a paper published this year has
#: age 0, and dividing by it would be an infinity that dominates any ranking it
#: appears in.
MIN_AGE_YEARS = 1


@dataclass(frozen=True, slots=True)
class Candidate:
    """
    The minimum a baseline needs.

    Deliberately not `PoolEntry`: that carries expansion-specific fields --
    direction, source ids, intents -- which no baseline may look at, and taking
    it here would let one quietly start to.
    """

    paper_id: int
    citation_count: int
    year: int | None
    anchor_overlap: int


def _ranked(pool: Sequence[Candidate], key) -> list[int]:  # type: ignore[no-untyped-def]
    """
    Sort descending by `key`, ties broken by ascending `paper_id`.

    The tie-break is not a detail. Reference-list benchmarks produce ties
    constantly -- every paper with the same citation count, every paper reached
    from one seed -- and leaving them to sort stability would make two runs
    disagree for reasons unrelated to the ranking being measured.
    """
    return [c.paper_id for c in sorted(pool, key=lambda c: (-key(c), c.paper_id))]


def uniformly_random(pool: Sequence[Candidate], rng: random.Random) -> list[int]:
    """
    Shuffle. The floor: a ranker that cannot beat this has found nothing, and
    the pool itself is carrying whatever score it gets.
    """
    ids = sorted(c.paper_id for c in pool)
    rng.shuffle(ids)
    return ids


def by_citation_count(pool: Sequence[Candidate]) -> list[int]:
    """
    Raw popularity. The baseline PLAN.md warns is artificially strong here,
    because the ground truth is a reference list and popular papers appear in
    many of them.
    """
    return _ranked(pool, lambda c: c.citation_count)


def by_citations_per_year(pool: Sequence[Candidate], as_of_year: int) -> list[int]:
    """
    Age-normalized popularity -- the real ranker's `quality` feature alone.

    An unknown year falls back to the raw count, matching
    `ranking.citations_per_year` exactly. If the two disagreed, this would stop
    being a baseline for that feature and become a different function with a
    misleading name.
    """

    def rate(candidate: Candidate) -> float:
        if candidate.year is None:
            return float(candidate.citation_count)
        return candidate.citation_count / max(MIN_AGE_YEARS, as_of_year - candidate.year + 1)

    return _ranked(pool, rate)


def by_anchor_overlap(pool: Sequence[Candidate]) -> list[int]:
    """
    How many seeds reached this paper, and nothing else. The dominant feature
    on its own, and the bar R3 has to clear to justify itself.
    """
    return _ranked(pool, lambda c: c.anchor_overlap)


def unranked_bfs(pool: Sequence[Candidate]) -> list[int]:
    """
    Everything reachable in one hop, unranked -- the "did ranking help at all?"
    control.

    Id order rather than the order the pool arrived in: arrival order depends on
    which anchor happened to be fetched first, which is a property of the crawl
    rather than of the method being measured.
    """
    return sorted(c.paper_id for c in pool)


__all__ = [
    "Candidate",
    "by_anchor_overlap",
    "by_citation_count",
    "by_citations_per_year",
    "unranked_bfs",
    "uniformly_random",
]
