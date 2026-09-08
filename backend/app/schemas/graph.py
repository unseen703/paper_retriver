"""
The `GET /api/sessions/{sid}/graph` response.

Shaped for Cytoscape rather than for the database: a node carries everything
the stylesheet and the inspector need, an edge carries only what an arrow
needs, and nothing requires a second request to render.

The one field worth reading twice is `in_degree` / `out_degree`. They count
edges **within this response**, not within the corpus -- see `GraphNodeOut`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Position(BaseModel):
    """A laid-out position, in Cytoscape's coordinate space."""

    x: float
    y: float


class GraphNodeOut(BaseModel):
    """One drawn node."""

    # The `papers` id, deliberately: edges reference papers, so using anything
    # else here would force the client to keep a lookup table to draw one line.
    id: int
    title: str
    state: str
    score: float | None = None
    year: int | None = None
    citation_count: int | None = None
    paper_type: str | None = None

    # Degrees **inside the returned graph**, not in the corpus. A hub with
    # 190k global citations can sit here with in_degree 1, and that is the
    # honest number for a picture that draws one edge into it. The global
    # figure is already carried as `citation_count`.
    in_degree: int = 0
    out_degree: int = 0

    # Null until R2.12 persists a layout; the client lays out from scratch.
    pos: Position | None = None


class GraphEdgeOut(BaseModel):
    """One drawn edge, always citing -> cited."""

    source: int
    target: int
    is_influential: bool = False


class GraphMeta(BaseModel):
    """What the client needs to caveat what it is showing."""

    node_count: int
    edge_count: int
    # Which filter config produced this graph -- the first question when the
    # recommendations look wrong.
    config_version: str
    crawl_completeness: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "|non-stub| / |nodes|. PLAN.md section I: PageRank over a partially"
            " crawled graph is biased, so R3 suppresses that column below 0.6."
        ),
    )


class GraphResponse(BaseModel):
    """The whole graph. Not paginated -- see the router's docstring."""

    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    meta: GraphMeta


__all__ = ["GraphEdgeOut", "GraphMeta", "GraphNodeOut", "GraphResponse", "Position"]
