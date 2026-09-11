"""
Response shapes for `GET /api/sessions/{sid}/candidates` (R3.a).

Separate from `schemas/graph.py` because the two answer different questions.
`GET /graph` answers "what should the canvas draw" -- it carries edges,
positions and in-graph degrees, and is filterable by state. This answers "what
should I read next", which needs none of those and does need the score
breakdown, the venue, and an honest total.

PLAN.md section F: "`<CandidateList>` is the product. A force-directed graph is
excellent for understanding *structure* and terrible for reading a ranked list."
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CandidateOut(BaseModel):
    """One row of the ranked table."""

    # The `papers` id, matching `GraphNodeOut.id`, so selecting a row can
    # highlight the same node on the canvas without a lookup table.
    id: int
    title: str
    score: float | None = None
    #: Per-term contributions, summing to `score`. Carried on the row rather
    #: than fetched per paper from `/nodes/{id}`: `<ScoreBreakdown>` renders
    #: beside every visible row, and a request per row is a request per row.
    score_breakdown: dict[str, float] = Field(default_factory=dict)

    year: int | None = None
    venue: str | None = None
    citation_count: int | None = None
    paper_type: str | None = None
    primary_arxiv_category: str | None = None
    #: Hops from the nearest seed. The cheapest available answer to "why is this
    #: in my graph at all?", which is the first question a surprising row raises.
    depth: int | None = None


class CandidatesResponse(BaseModel):
    candidates: list[CandidateOut]
    #: Every CANDIDATE in the session, **not** the number returned. A table
    #: reading "50 of 50" when there are 213 is wrong in the direction that
    #: stops the reader looking for the rest.
    total: int
