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

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine

from app.api.deps import existing_session, get_engine
from app.config import filters
from app.models import NODE_STATES
from app.repo import edges as edges_repo
from app.repo import events as events_repo
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.schemas.graph import (
    ClearGraphResponse,
    GraphEdgeOut,
    GraphMeta,
    GraphNodeOut,
    GraphResponse,
    Position,
    ReasonCount,
    ReviewBucketOut,
    ReviewPaperOut,
    ReviewResponse,
    SavePositionsRequest,
    SavePositionsResponse,
    StatsResponse,
)
from app.services.review import DEFAULT_LIMIT, MAX_LIMIT, ReviewBucket, build_review
from app.services.stats import compute_stats

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["graph"])


def _parse_states(raw: str | None) -> list[str] | None:
    """
    "SEED,LIKED" -> ["SEED", "LIKED"]; absent -> None (no filter).

    An unrecognised state is a 422 rather than an empty result. Silently
    returning nothing for `states=SEDE` looks like the graph was wiped, and
    that is a bad way to learn you typed the parameter wrong.
    """
    wanted = [part.strip().upper() for part in (raw or "").split(",") if part.strip()]
    if not wanted:
        # Covers both an absent parameter and a present-but-empty one. A client
        # that builds `states=${selected.join(",")}` sends `states=` whenever
        # nothing is selected, and repo.graph.get_nodes reads `[]` as "none of
        # these states" -- so the honest-looking query returned a blank canvas
        # with a 200 and no error. That is the same "reads as data loss"
        # failure that makes an unknown state a 422, arriving by another route.
        return None
    unknown = sorted(set(wanted) - NODE_STATES)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown state(s) {unknown}; expected any of {sorted(NODE_STATES)}",
        )
    return wanted


@router.get("/{sid}/graph", response_model=GraphResponse)
def get_graph(
    sid: Annotated[int, Depends(existing_session)],
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


@router.put("/{sid}/positions", response_model=SavePositionsResponse)
def save_positions(
    sid: Annotated[int, Depends(existing_session)],
    body: SavePositionsRequest,
    engine: Annotated[Engine, Depends(get_engine)],
) -> SavePositionsResponse:
    """
    Persist the current arrangement (R2.12).

    PLAN.md M6 calls layout instability the thing that "kills usability", and
    the `pos_x`/`pos_y` columns have been in the schema since migration 0001
    with nothing ever writing one -- so every reload threw away whatever the
    user had arranged. This is the missing half.

    **PUT, and bulk.** The client calls it after every settled layout and after
    dragging stops, so it has to be safely repeatable; and a 200-node graph
    settling is one event, not two hundred.

    **A paper with no node here is skipped rather than refused.** A node can be
    removed between the layout settling and this request landing. That is a
    race, and failing the whole request over it would discard an arrangement
    that is still correct for everything else. The `saved` count is what keeps
    the skip visible instead of silent.
    """
    with engine.begin() as conn:
        saved = graph_repo.set_positions(
            conn, sid, [(p.paper_id, p.x, p.y) for p in body.positions]
        )

    if saved != len(body.positions):
        logger.info(
            "positions_partially_saved session=%s sent=%s saved=%s",
            sid,
            len(body.positions),
            saved,
        )
    return SavePositionsResponse(saved=saved)


@router.get("/{sid}/stats", response_model=StatsResponse)
def get_stats(
    sid: Annotated[int, Depends(existing_session)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> StatsResponse:
    """
    What is actually in this session's graph (R2.13).

    A server endpoint rather than arithmetic in the panel, because BUILD.md's
    verification is "counts match the DB". `GET /graph` can be filtered by
    state, and a panel totalling a filtered response would confidently report a
    subset as the whole -- a failure whose symptom is that everything looks
    fine.
    """
    with engine.connect() as conn:
        stats = compute_stats(conn, sid)
    return StatsResponse(
        node_count=stats.node_count,
        edge_count=stats.edge_count,
        by_state=stats.by_state,
        components=stats.components,
        avg_degree=stats.avg_degree,
        density=stats.density,
        crawl_completeness=stats.crawl_completeness,
    )


def _bucket(bucket: ReviewBucket) -> ReviewBucketOut:
    return ReviewBucketOut(
        total=bucket.total,
        by_reason=[ReasonCount(reason_code=code, count=n) for code, n in bucket.by_reason],
        papers=[
            ReviewPaperOut(
                paper_id=p.paper_id,
                title=p.title,
                reason_code=p.reason_code,
                stage=p.stage,
                year=p.year,
            )
            for p in bucket.papers
        ],
        truncated=bucket.truncated,
    )


@router.get("/{sid}/review", response_model=ReviewResponse)
def get_review(
    sid: Annotated[int, Depends(existing_session)],
    engine: Annotated[Engine, Depends(get_engine)],
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_LIMIT, description="Rows per tab. Counts stay complete regardless."),
    ] = DEFAULT_LIMIT,
) -> ReviewResponse:
    """
    What the graph is not showing you, and why (R2.14).

    PLAN.md M5: "A filter you cannot audit is a filter you cannot tune, and you
    will silently discard good papers for weeks without noticing."
    """
    with engine.connect() as conn:
        review = build_review(conn, sid, limit)
    return ReviewResponse(
        quarantined=_bucket(review["quarantined"]),
        rejected=_bucket(review["rejected"]),
        removed=_bucket(review["removed"]),
    )


@router.delete("/{sid}/graph", response_model=ClearGraphResponse)
def clear_graph(
    sid: Annotated[int, Depends(existing_session)],
    engine: Annotated[Engine, Depends(get_engine)],
    confirm: Annotated[
        bool,
        Query(description="Must be true. Absent or false is a 400 -- see below."),
    ] = False,
) -> ClearGraphResponse:
    """
    Empty this session's graph (R2.15).

    **`confirm` is required and there is no default that fires.** A DELETE that
    goes off on a stray click is a different feature from one that makes you
    say what you mean, and the difference only shows up on the day you did not
    mean it. `confirm=false` is refused too: a caller saying no should not be
    read as a caller saying nothing.

    Graph membership only. The corpus is what the API budget bought and it is
    shared between sessions, so seeding the same papers again afterwards costs
    nothing. The event log survives as well -- it is append-only and it is the
    audit trail -- and a `CLEARED` event is appended, so the clear is recorded
    rather than being the one action that leaves no trace.
    """
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "clearing the graph requires ?confirm=true;"
                " this removes every node in the session (the corpus is kept)"
            ),
        )

    with engine.begin() as conn:
        # Read the ids before deleting: the log records what left, and after
        # the DELETE there is nothing left to ask.
        leaving = sorted(graph_repo.get_node_ids(conn, sid))
        cleared = graph_repo.clear_session(conn, sid)
        for paper_id in leaving:
            # One event per paper rather than a single session-level row.
            # `interaction_events.paper_id` has a foreign key, so there is no
            # id meaning "the whole session" -- and per paper is the truer
            # record regardless: each of these genuinely left the graph.
            events_repo.append_event(conn, sid, paper_id, "CLEARED", actor="USER")

    logger.info("graph_cleared session=%s cleared=%s", sid, cleared)
    return ClearGraphResponse(cleared=cleared)


__all__ = ["router"]
