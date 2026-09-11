"""
`GET /api/sessions/{sid}/candidates` -- the ranked table (R3.a).

PLAN.md section G specifies this endpoint and section F explains why it matters
more than the canvas does:

    "`<CandidateList>` is the product. A force-directed graph is excellent for
    understanding *structure* and terrible for reading a ranked list. Users will
    make most decisions from the table."

**Not a filtered `GET /graph`.** That endpoint answers a different question --
what the canvas should draw -- and carries edges, positions and in-graph degrees
that a reader of a table does not want. More importantly it is deliberately
filterable by state, so building a "ranked list" on it invites a caller to show
a subset of the candidates while believing it has them all.

**Ordering is the whole job, and each of its three cases has a plausible wrong
version.** See `_sort_key`.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Engine

from app.api.deps import existing_session, get_engine
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.schemas.candidates import CandidateOut, CandidatesResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["candidates"])

#: The only state a recommendation list may draw from. Seeds and labelled
#: papers are decisions already made; listing them would be recommending what
#: the user has already ruled on.
CANDIDATE_STATE = "CANDIDATE"

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

#: A `Literal` rather than a validated string, so FastAPI rejects `sort=scroe`
#: with a 422 naming the valid values. Falling back to `score` would answer a
#: question nobody asked with a list that looks entirely plausible.
SortKey = Literal["score", "year", "citations"]


def _sort_key(sort: SortKey, row: CandidateOut) -> tuple[int, float, int]:
    """
    Descending by the chosen column, unknowns last, ties by `paper_id`.

    **Unknowns last, not first.** SQLite's `ORDER BY score DESC` puts NULLs
    first, so the papers nothing has scored yet would head the recommendation
    list -- they are the least-known, not the best. The leading 0/1 in the tuple
    is what pushes them to the end regardless of the column being sorted on.

    **Unknown is not zero.** Substituting 0.0 for a missing score would rank an
    unscored paper above a genuinely poor one, and negative totals are ordinary
    here because `hub` carries a negative weight.

    **Ties break on `paper_id`.** Leaving equal scores to the database means the
    table can reshuffle between reloads with nothing to explain it, which is
    what CLAUDE.md rule 7 exists to prevent.
    """
    value: float | int | None = {
        "score": row.score,
        "year": row.year,
        "citations": row.citation_count,
    }[sort]

    if value is None:
        return (1, 0.0, row.id)
    return (0, -float(value), row.id)


@router.get("/{sid}/candidates", response_model=CandidatesResponse)
def list_candidates(
    sid: Annotated[int, Depends(existing_session)],
    engine: Annotated[Engine, Depends(get_engine)],
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_LIMIT, description="Maximum rows to return."),
    ] = DEFAULT_LIMIT,
    sort: Annotated[
        SortKey,
        Query(description="Column to order by, descending."),
    ] = "score",
) -> CandidatesResponse:
    """This session's candidates, ranked."""
    with engine.connect() as conn:
        nodes = graph_repo.get_nodes(conn, sid, [CANDIDATE_STATE])
        papers = {p.id: p for p in papers_repo.get_papers_by_ids(conn, [n.paper_id for n in nodes])}

    rows: list[CandidateOut] = []
    for node in nodes:
        paper = papers.get(node.paper_id)
        if paper is None:
            # A node whose paper row is gone is a broken foreign key, not a
            # readable row. `GET /graph` skips these on the grounds that the
            # rest is still worth showing, and a list that 500s because one row
            # is corrupt is strictly worse than one that is short by one.
            logger.warning("candidate_paper_missing session=%s paper=%s", sid, node.paper_id)
            continue
        rows.append(
            CandidateOut(
                id=node.paper_id,
                title=paper.title,
                score=node.score,
                score_breakdown=node.score_breakdown,
                year=paper.year,
                venue=paper.venue,
                citation_count=paper.citation_count,
                paper_type=paper.paper_type.value if paper.paper_type else None,
                primary_arxiv_category=paper.primary_arxiv_category,
                depth=node.depth,
            )
        )

    rows.sort(key=lambda row: _sort_key(sort, row))

    # `total` counts every candidate, not the page. Truncating first and
    # reporting the truncated length would tell a reader looking at 50 rows
    # that 50 is all there is.
    return CandidatesResponse(candidates=rows[:limit], total=len(rows))
