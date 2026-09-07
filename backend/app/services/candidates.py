"""
Candidate pooling and the R1 prescore (PLAN.md sections E2, E3).

**Pooling is the point.** BUILD.md: "UNION candidates from ALL frontier nodes.
Compute anchor_overlap during the union -- it is the whole reason for pooling.
Do not take per-node top-K."

Per-node top-K is the obvious implementation and it silently destroys the one
signal citation structure gives you that a keyword search cannot: a paper cited
by three of your seeds is far stronger evidence than three papers each cited
once, and taking the best K per node throws that distinction away before
anything can measure it. Hence `anchor_overlap` is computed *during* the union
and is the dominant prescore term at weight 2.0.

**The hub guard** skips forward expansion from any anchor whose citation count
exceeds `forward_expand_max`. Attention Is All You Need has 191,436 citations;
expanding forward from it returns an arbitrary slice of a fifth of modern ML and
exhausts the budget on noise. Backward from a hub stays allowed -- a hub's own
bibliography is finite and unusually informative. The skip is logged, because a
silent skip is indistinguishable from a paper that genuinely has no citations.

This module reads the DB but writes no SQL: everything goes through `repo/`
(CLAUDE.md rule 3). `prescore` itself is pure.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from sqlalchemy import Connection

from app.config import FiltersConfig
from app.repo import edges as edges_repo
from app.repo import events as events_repo
from app.repo import filter_decisions as decisions_repo
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo

logger = logging.getLogger(__name__)

# Papers older than this get no recency bump (Appendix B.3).
RECENCY_CUTOFF_YEARS = 1.5
# A brand-new paper is not infinitely hot; age is floored before dividing.
MIN_AGE_YEARS = 0.5


@dataclass(frozen=True, slots=True)
class PoolEntry:
    """
    One candidate, with the evidence that put it in the pool.

    `anchor_overlap` and `source_ids` are the pooling product: how many frontier
    nodes reached this paper, and which ones. R1.10's per-source cap allocates
    against `source_ids`.
    """

    paper_id: int
    anchor_overlap: int
    citation_count: int
    age_years: float
    direction: str  # BACKWARD | FORWARD
    intents: tuple[str, ...] = ()
    is_influential: bool = False
    source_ids: tuple[int, ...] = field(default_factory=tuple)


def prescore(entry: PoolEntry, cfg: FiltersConfig) -> float:
    """
    R1's ranker (Appendix B.3). Real features arrive at R3.

    Pure and deterministic: no clock, no DB, no dependence on iteration order.
    """
    cpy = entry.citation_count / max(entry.age_years, MIN_AGE_YEARS)
    # Only the excess above the hub threshold is penalised, so the term is
    # exactly zero for ordinary papers rather than merely small.
    hub = math.log1p(max(0, entry.citation_count - cfg.forward_expand_max)) / 10
    return (
        2.0 * entry.anchor_overlap  # dominant signal
        + 0.8 * ("methodology" in entry.intents)
        + 0.5 * entry.is_influential
        + 0.4 * math.log1p(cpy) / 5
        + 0.3 * (1.0 if entry.age_years < RECENCY_CUTOFF_YEARS else 0.0)
        - 0.6 * hub
    )


def _age_years(year: int | None, as_of_year: int) -> float:
    """Unknown year is treated as old, so it earns no recency bump."""
    if year is None:
        return 99.0
    return max(0.0, float(as_of_year - year))


def build_pool(
    conn: Connection,
    session_id: int,
    frontier: list[int],
    cfg: FiltersConfig,
    as_of_year: int,
) -> list[PoolEntry]:
    """
    Every candidate reachable from any frontier node, unioned, with
    `anchor_overlap` counted during the union.

    Excluded: the frontier itself, papers already in this session's graph,
    papers tombstoned in this session, and papers whose filter verdict is not
    ACCEPT. Exclusion is session-scoped -- a paper removed in one workspace is
    still a candidate in another.
    """
    if not frontier:
        return []

    frontier_set = set(frontier)
    # BUILD.md's stage-4 exclusion, all three clauses. The filter_decisions
    # clause is the one that is easy to omit and impossible to notice: a
    # rejected paper has a decision row but no graph node, so nothing else
    # keeps it out of the pool.
    excluded = (
        frontier_set
        | graph_repo.get_node_ids(conn, session_id)
        | events_repo.removed_paper_ids(conn, session_id)
        | decisions_repo.non_accepted_paper_ids(conn, session_id)
    )

    # Hub anchors contribute their references but not their citations.
    anchors = papers_repo.get_papers_by_ids(conn, list(frontier))
    hub_ids = {
        p.id for p in anchors if p.id is not None and p.citation_count > cfg.forward_expand_max
    }
    for paper in anchors:
        if paper.id in hub_ids:
            logger.info(
                "HUB_SKIP_FORWARD paper_id=%s citations=%s threshold=%s",
                paper.id,
                paper.citation_count,
                cfg.forward_expand_max,
            )

    # Accumulate per candidate rather than per (anchor, candidate) pair -- that
    # accumulation IS anchor_overlap.
    # A SET of anchors, not a running count. A mutual citation (A cites X and
    # X cites A) produces two edges that both touch A, and counting edges gave
    # that pair a free +2.0 on the dominant prescore term.
    sources: dict[int, set[int]] = {}
    # Every direction seen, resolved deterministically below. Keeping only the
    # first would make the verdict depend on row order, and R1.10 allocates a
    # budget floor per direction.
    seen_directions: dict[int, set[str]] = {}
    intents: dict[int, set[str]] = {}
    influential: dict[int, bool] = {}

    for edge in edges_repo.get_edges_for(conn, list(frontier)):
        if edge.citing_id in frontier_set:
            anchor, candidate, direction = edge.citing_id, edge.cited_id, "BACKWARD"
        elif edge.cited_id in frontier_set:
            anchor, candidate, direction = edge.cited_id, edge.citing_id, "FORWARD"
        else:  # pragma: no cover - get_edges_for only returns touching edges
            continue

        if candidate in excluded:
            continue
        if direction == "FORWARD" and anchor in hub_ids:
            continue

        sources.setdefault(candidate, set()).add(anchor)
        seen_directions.setdefault(candidate, set()).add(direction)
        intents.setdefault(candidate, set()).update(edge.intents)
        influential[candidate] = influential.get(candidate, False) or edge.is_influential

    papers = {
        p.id: p for p in papers_repo.get_papers_by_ids(conn, sorted(sources)) if p.id is not None
    }

    # Sorted, so the pool is deterministic regardless of dict iteration order.
    return [
        PoolEntry(
            paper_id=paper_id,
            anchor_overlap=len(sources[paper_id]),
            citation_count=papers[paper_id].citation_count,
            age_years=_age_years(papers[paper_id].year, as_of_year),
            # BACKWARD when both apply: a reference list is bounded and
            # deliberate, so it is the safer bucket to charge the budget to.
            direction=("BACKWARD" if "BACKWARD" in seen_directions[paper_id] else "FORWARD"),
            intents=tuple(sorted(intents[paper_id])),
            is_influential=influential[paper_id],
            source_ids=tuple(sorted(sources[paper_id])),
        )
        for paper_id in sorted(sources)
        if paper_id in papers
    ]


__all__ = ["MIN_AGE_YEARS", "RECENCY_CUTOFF_YEARS", "PoolEntry", "build_pool", "prescore"]
