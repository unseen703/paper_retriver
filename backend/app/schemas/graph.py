"""
The `GET /api/sessions/{sid}/graph` response.

Shaped for Cytoscape rather than for the database: a node carries everything
the stylesheet and the inspector need, an edge carries only what an arrow
needs, and nothing requires a second request to render.

The one field worth reading twice is `in_degree` / `out_degree`. They count
edges **within this response**, not within the corpus -- see `GraphNodeOut`.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class StatsResponse(BaseModel):
    """
    `<StatsPanel>` v1 (R2.13).

    Deliberately no `pagerank`. BUILD.md defers it to R3, and a field that
    exists and is always null invites a panel to render an empty row for a
    metric nobody has computed -- the shape should say what is available.
    """

    node_count: int
    edge_count: int = Field(
        description=(
            "Edges with both endpoints in this graph. An edge into a boundary"
            " paper is real and structurally useful, but the panel sits beside"
            " a picture that does not draw it."
        )
    )
    by_state: dict[str, int] = Field(
        description="Every state, including the ones at zero, so a row is never missing."
    )
    components: int = Field(description="Connected pieces of the undirected projection.")
    avg_degree: float = Field(
        ge=0.0, description="2E/N -- each edge counts at both of its endpoints."
    )
    density: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "2E / (N(N-1)), against the undirected maximum. Bounded here on"
            " purpose: a density above 1.0 is arithmetically impossible, so if"
            " one is ever computed it means the edge count and the node count"
            " came from different reads -- and a loud 500 beats shipping an"
            " absurd number to a panel nobody would think to doubt."
        ),
    )
    crawl_completeness: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "|non-stub| / |nodes|. PLAN.md section I: PageRank over a partially"
            " crawled graph is biased, and R3 suppresses that column below 0.6."
        ),
    )


class NodePosition(BaseModel):
    """Where one node sits, as the client laid it out (R2.12)."""

    model_config = ConfigDict(extra="forbid")

    paper_id: int = Field(ge=1)
    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _must_be_finite(cls, value: float) -> float:
        """
        Reject NaN and the infinities.

        JSON has no way to spell them, but Python's decoder accepts them and a
        float column stores them without complaint. They come back as a
        position Cytoscape cannot draw, so the node silently disappears with
        nothing logged anywhere. The boundary is the only place this is
        findable.
        """
        if not math.isfinite(value):
            raise ValueError("coordinates must be finite")
        return value


class SavePositionsRequest(BaseModel):
    """
    A whole arrangement in one request (R2.12).

    Bulk because a settled layout is one event: a 200-node graph sent one node
    at a time would be 200 transactions describing a single act.

    The cap is `max_nodes` with room to spare. A graph cannot exceed that, so a
    larger list is either a bug or something not worth writing.
    """

    model_config = ConfigDict(extra="forbid")

    positions: list[NodePosition] = Field(default_factory=list, max_length=5000)


class SavePositionsResponse(BaseModel):
    """How many rows were actually written."""

    saved: int = Field(
        description=(
            "Rows updated. Lower than what was sent when a node was removed"
            " between the layout settling and this request landing -- a race,"
            " not an error, but one worth being able to see."
        )
    )


__all__ = [
    "GraphEdgeOut",
    "GraphMeta",
    "GraphNodeOut",
    "GraphResponse",
    "NodePosition",
    "Position",
    "SavePositionsRequest",
    "SavePositionsResponse",
    "StatsResponse",
]
