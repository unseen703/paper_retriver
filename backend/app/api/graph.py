"""
`GET /api/sessions/{sid}/graph` (R1.17) -- the whole graph, in one response.

**Not paginated.** PLAN.md is blunt: "One call, whole graph. At 2k nodes this
is ~400KB -- fine. Do NOT paginate until you've measured a problem." Beyond
the size argument, a paginated graph is a category error: a force-directed
layout cannot place any node until it has seen every node and edge, so a
client would have to page through the whole thing before drawing anything.
A test pins BUILD.md's figure -- a 21-node graph under 50KB.

Three things here are easy to get subtly wrong, and all three fail quietly:

**Only edges with both endpoints in the graph.** The corpus is much larger
than the graph. `repo.edges.get_edges_for` returns every edge *touching* a set
of papers, which is what pooling needs and the opposite of what drawing needs
-- an edge into a boundary paper would make Cytoscape either drop it or invent
a node, and an invented node in a citation graph looks exactly like a real
finding. `get_edges_between` returns the induced subgraph instead.

**The filter has to narrow edges too.** `states=SEED` returning an edge into a
CANDIDATE that was not returned breaks the same invariant by a different route.

**Degrees describe the response, not the corpus.** How many of *these* papers
cite this one is what the picture shows; `citation_count` already carries the
global figure.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import Engine

from app.api.deps import get_engine
from app.config import filters
from app.models import NODE_STATES
from app.repo import edges as edges_repo
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.schemas.graph import GraphEdgeOut, GraphMeta, GraphNodeOut, GraphResponse, Position

router = APIRouter(prefix="/api/sessions", tags=["graph"])


def _parse_states(raw: str | None) -> list[str] | None:
    """
    "SEED,LIKED" -> ["SEED", "LIKED"]; absent -> None (no filter).

    An unrecognised state is a 422 rather than an empty result. Silently
    returning nothing for `states=SEDE` looks like the graph was wiped, and
    that is a bad way to learn you typed the parameter wrong.
    """
    if raw is None:
        return None
    wanted = [part.strip().upper() for part in raw.split(",") if part.strip()]
    unknown = sorted(set(wanted) - NODE_STATES)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown state(s) {unknown}; expected any of {sorted(NODE_STATES)}",
        )
    return wanted


@router.get("/{sid}/graph", response_model=GraphResponse)
def get_graph(
    sid: Annotated[int, Path(ge=1, description="Session id.")],
    engine: Annotated[Engine, Depends(get_engine)],
    states: Annotated[
        str | None,
        Query(description="Comma-separated states to include, e.g. `SEED,LIKED`."),
    ] = None,
) -> GraphResponse:
    """This session's graph: every node, every edge between them, and meta."""
    wanted_states = _parse_states(states)

    with engine.connect() as conn:
        nodes = graph_repo.get_nodes(conn, sid, wanted_states)
        paper_ids = [n.paper_id for n in nodes]
        papers = {p.id: p for p in papers_repo.get_papers_by_ids(conn, paper_ids)}
        edges = edges_repo.get_edges_between(conn, paper_ids)

    in_degree: dict[int, int] = {}
    out_degree: dict[int, int] = {}
    for edge in edges:
        out_degree[edge.citing_id] = out_degree.get(edge.citing_id, 0) + 1
        in_degree[edge.cited_id] = in_degree.get(edge.cited_id, 0) + 1

    out_nodes: list[GraphNodeOut] = []
    non_stub = 0
    for node in nodes:
        paper = papers.get(node.paper_id)
        if paper is None:
            # A graph node whose paper is gone is a broken foreign key, not a
            # drawable node. Skipping beats raising: the rest of the graph is
            # still worth showing.
            continue
        if paper.crawl_state.value != "STUB":
            non_stub += 1
        out_nodes.append(
            GraphNodeOut(
                id=node.paper_id,
                title=paper.title,
                state=node.state,
                score=node.score,
                year=paper.year,
                citation_count=paper.citation_count,
                paper_type=paper.paper_type.value if paper.paper_type else None,
                in_degree=in_degree.get(node.paper_id, 0),
                out_degree=out_degree.get(node.paper_id, 0),
                pos=(
                    Position(x=node.pos_x, y=node.pos_y)
                    if node.pos_x is not None and node.pos_y is not None
                    else None
                ),
            )
        )

    return GraphResponse(
        nodes=out_nodes,
        edges=[
            GraphEdgeOut(source=e.citing_id, target=e.cited_id, is_influential=e.is_influential)
            for e in edges
        ],
        meta=GraphMeta(
            node_count=len(out_nodes),
            edge_count=len(edges),
            config_version=filters.config_version,
            # Zero over zero is 1.0, not 0.0: an empty graph is not "completely
            # uncrawled", and reporting 0.0 would have R3 suppress a PageRank
            # column that has nothing to suppress.
            crawl_completeness=(non_stub / len(out_nodes)) if out_nodes else 1.0,
        ),
    )


__all__ = ["router"]
