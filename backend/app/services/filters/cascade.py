"""
The filter cascade: stage 0 -> 1 -> 2, short-circuiting on the first non-ACCEPT.

**Layering note.** Every other module in this package is a pure function
(CLAUDE.md rule 3). This one is the orchestrator BUILD.md R1.6 specifies with a
`conn` parameter, so it is not pure -- but it writes no SQL itself. Persistence
goes through `repo/filter_decisions.py`, which keeps rule 3's other half
("nothing above repo/ writes SQL") intact and leaves the stages independently
testable.

Order is cheapest-first, and it is load-bearing: a 2014 dataset paper reports
`PRE_ERA`, not `IS_DATASET`, because era is one integer comparison and type
inspection is a regex. Short-circuiting means the expensive stages never see the
papers the cheap ones already excluded.

**The global-verdict cache** is what makes this affordable at corpus scale. A
rejection that does not depend on the session -- pre-era, dataset, applied
category -- is written with `session_id = NULL` and reused on every later
encounter, in any session. BUILD.md's verification is that a second run performs
zero filter evaluations for globally-rejected papers.

Only rejections are cached. An ACCEPT is re-evaluated every time: a config
change can turn it into a rejection, and a stale accept would silently readmit a
paper the new rules exclude.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection

from app.config import FiltersConfig
from app.models import FilterDecision, Paper
from app.repo import filter_decisions as decisions_repo
from app.services.filters.base import should_continue
from app.services.filters.era_filter import era_filter
from app.services.filters.topic_filter import topic_filter
from app.services.filters.type_filter import type_filter


@dataclass
class CascadeStats:
    """
    Counters for what the cascade actually did.

    `evaluated` vs `cache_hits` is how BUILD.md's verification is measured, and
    keeping it as a caller-supplied object means the expansion (R1.11) can
    accumulate across a whole run and record it in the `expansions` row.
    """

    evaluated: int = 0
    cache_hits: int = 0

    def __add__(self, other: CascadeStats) -> CascadeStats:
        return CascadeStats(
            evaluated=self.evaluated + other.evaluated,
            cache_hits=self.cache_hits + other.cache_hits,
        )


def run_cascade(
    conn: Connection,
    session_id: int,
    paper_id: int,
    paper: Paper,
    cfg: FiltersConfig,
    as_of_year: int,
    stats: CascadeStats | None = None,
) -> FilterDecision:
    """
    Decide whether `paper` may enter the graph, and record why.

    `paper_id` is the internal surrogate id; `paper` is the domain object the
    stages inspect. Both are needed because the decision is *about* the domain
    object but is *filed against* the row.
    """
    stats = stats if stats is not None else CascadeStats()

    cached = decisions_repo.find_global_rejection(conn, paper_id, cfg.config_version)
    if cached is not None:
        stats.cache_hits += 1
        return cached

    stats.evaluated += 1

    # Lambdas, not a tuple of results. A tuple would evaluate every stage before
    # the loop began, which returns the right verdict while doing none of the
    # short-circuiting the cheapest-first ordering exists for.
    stages = (
        lambda: era_filter(paper, cfg),
        lambda: type_filter(paper, cfg, as_of_year),
        lambda: topic_filter(paper, cfg),
    )

    decision = None
    for stage in stages:
        decision = stage()
        if not should_continue(decision):
            break

    assert decision is not None  # noqa: S101 - `stages` is a non-empty literal
    decisions_repo.record(conn, session_id, paper_id, decision, cfg.config_version)
    return decision


__all__ = ["CascadeStats", "run_cascade"]
