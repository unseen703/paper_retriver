"""
Budget allocation -- the anti-explosion mechanism (Appendix B.4, PLAN.md E5).

Pure function: pool in, selection out. No DB, no clock, no randomness.

Without this, an expansion returns whatever the ranker liked best, which in
practice is fifty papers from one prolific seed's reference list. Each rung
counters a specific, observed failure:

    recency lane      15%   score tracks citations and citations accrue with
                            age, so pure ranking never surfaces last month's
                            paper
    direction floors  20%   backward yields thin out with depth -- a 2018
                      each  paper's references are mostly pre-2015 -- and
                            without a floor the ranker abandons the direction
    remainder         rest  by score
    per-source cap    40%   one hub-adjacent seed would otherwise dominate

Two deliberate choices worth stating.

**Ties break on `paper_id`, never list position.** Otherwise the same pool in a
different order gives a different expansion, which makes every result
unreproducible (CLAUDE.md rule 7).

**The source cap is relaxed rather than returning short.** If every candidate
comes from one anchor, returning 8 of a requested 20 is worse than exceeding the
cap: the cap exists to diversify a varied pool, not to shrink a homogeneous one.
"""

from __future__ import annotations

from collections import Counter

from app.config import Budget
from app.services.candidates import PoolEntry

# The recency lane's definition of "recent". Distinct from the prescore's
# 1.5-year bump: this reserves slots, that adjusts a score.
RECENT_YEARS = 1.0

Scored = tuple[PoolEntry, float]


def _top(candidates: list[Scored], n: int) -> list[Scored]:
    """Best n, ties broken on paper_id so the result never depends on order."""
    if n <= 0:
        return []
    return sorted(candidates, key=lambda c: (-c[1], c[0].paper_id))[:n]


def allocate(pool: list[Scored], cfg: Budget, budget: int) -> list[Scored]:
    """
    Choose up to `budget` candidates from `pool`.

    `pool` is (entry, score) pairs; R1 supplies the prescore, R3 the real one.
    """
    if budget <= 0 or not pool:
        return []

    picked: list[Scored] = []
    chosen: set[int] = set()

    def take(candidates: list[Scored], n: int) -> None:
        for entry, score in _top([c for c in candidates if c[0].paper_id not in chosen], n):
            picked.append((entry, score))
            chosen.add(entry.paper_id)

    # 1. Recency lane, reserved first so ranking cannot crowd it out.
    take(
        [c for c in pool if c[0].age_years < RECENT_YEARS],
        max(1, int(cfg.recency_lane_frac * budget)),
    )

    # 2. A floor per direction, computed against what remains so a starved
    #    direction still gets slots.
    remaining = budget - len(picked)
    for direction in ("BACKWARD", "FORWARD"):
        take(
            [c for c in pool if c[0].direction == direction],
            int(cfg.direction_floor_frac * remaining),
        )

    # 3. Everything else, purely by score.
    take(pool, budget - len(picked))

    return _enforce_source_cap(picked, pool, chosen, cfg, budget)


def _enforce_source_cap(
    picked: list[Scored],
    pool: list[Scored],
    chosen: set[int],
    cfg: Budget,
    budget: int,
) -> list[Scored]:
    """
    Drop the weakest picks from any over-represented source, then refill.

    Refilling matters: dropping alone would silently return fewer than the
    caller asked for. If no substitute exists -- every candidate shares the one
    source -- the dropped picks come back, because a short result is worse than
    a concentrated one.
    """
    cap = max(1, int(cfg.source_cap_frac * budget))

    counts: Counter[int] = Counter()
    kept: list[Scored] = []
    dropped: list[Scored] = []
    # Worst-first drop: sort best-first and cap as we go.
    for entry, score in sorted(picked, key=lambda c: (-c[1], c[0].paper_id)):
        if any(counts[src] >= cap for src in entry.source_ids):
            dropped.append((entry, score))
            continue
        counts.update(entry.source_ids)
        kept.append((entry, score))

    if len(kept) == len(picked):
        return kept

    # Refill from candidates that respect the cap.
    for entry, score in sorted(pool, key=lambda c: (-c[1], c[0].paper_id)):
        if len(kept) >= budget:
            break
        if entry.paper_id in chosen or entry.paper_id in {e.paper_id for e, _ in kept}:
            continue
        if any(counts[src] >= cap for src in entry.source_ids):
            continue
        counts.update(entry.source_ids)
        kept.append((entry, score))

    # Nothing else available: prefer a full result over a diverse one.
    for candidate in dropped:
        if len(kept) >= budget:
            break
        kept.append(candidate)

    return sorted(kept, key=lambda c: (-c[1], c[0].paper_id))


__all__ = ["RECENT_YEARS", "allocate"]
