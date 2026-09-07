"""
`POST /api/sessions/{sid}/nodes` (R1.16) -- the deliberate way into the graph.

Search looks; this adds. It is a thin HTTP shell over `services.seed.add_seed`,
which already owns the fetch/dedup/cascade/upsert/event sequence -- the only
work here is turning that service's exceptions into the status codes BUILD.md
specifies, because each one means something different to the UI:

    404  S2 has never heard of this id           nothing to add; `force` is useless
    409  already in this session's graph         focus it instead of adding it
    422  the cascade rejected it                 offer `force`, naming the rule
    503  S2 unreachable and not cached           worth retrying

An unknown `{sid}` is a 404 too, resolved by the `existing_session` dependency
before the body runs. It used to reach SQLite and surface as a raw FOREIGN KEY
IntegrityError -- a 500 -- and because the metadata fetch happened first, a
typo'd session id spent an S2 request to learn the id was wrong.

Collapsing 404 and 422 into one code would make the UI offer an override that
cannot possibly work, which is why `add_seed` raises two distinct exceptions
rather than one carrying a `NOT_FOUND` reason code.

**Synchronous, and no auto-expansion.** PLAN.md sketches auto-enqueueing an
expansion here, but BUILD.md R1.18 is explicit that expansion stays synchronous
and job-free until R2.4. Adding a seed that silently spends sixty API calls is
also a surprising thing for a POST to do; the client asks for expansion
separately.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Engine

from app.api.deps import existing_session, get_engine, get_s2_client
from app.clients.s2 import CacheMiss, S2Client, S2TransientError
from app.config import filters
from app.models import GraphNode
from app.repo import papers as papers_repo
from app.schemas.nodes import AddNodeRequest, NodeResponse, RejectedResponse
from app.services.seed import AlreadyPresent, SeedNotFound, SeedRejected, add_seed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["nodes"])


def _to_response(engine: Engine, node: GraphNode) -> NodeResponse:
    """Join the node to its paper so the caller gets something renderable."""
    with engine.connect() as conn:
        (paper,) = papers_repo.get_papers_by_ids(conn, [node.paper_id])
    return NodeResponse(
        session_id=node.session_id,
        paper_id=node.paper_id,
        state=node.state,
        depth=node.depth,
        title=paper.title,
        score=node.score,
        year=paper.year,
    )


@router.post(
    "/{sid}/nodes",
    status_code=status.HTTP_201_CREATED,
    response_model=NodeResponse,
    responses={422: {"model": RejectedResponse}},
)
async def add_node(
    sid: Annotated[int, Depends(existing_session)],
    body: AddNodeRequest,
    client: Annotated[S2Client, Depends(get_s2_client)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> NodeResponse:
    """Add a paper to this session's graph as a SEED at depth 0."""
    try:
        node = await add_seed(
            engine,
            client,
            sid,
            body.s2_paper_id,
            filters,
            # The corpus era is relative to now, not to when the config was
            # written, so "recent" keeps meaning recent.
            datetime.now(UTC).year,
            force=body.force,
        )
    except SeedNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AlreadyPresent as exc:
        raise HTTPException(
            status_code=409,
            detail=f"paper {exc.paper_id} is already in session {sid}",
        ) from exc
    except SeedRejected as exc:
        # A structured body, not a string: the UI offers `force` against a
        # named rule, so the rule has to be a field rather than prose.
        raise HTTPException(
            status_code=422,
            detail={
                "detail": str(exc),
                "reason_code": exc.reason_code,
                "stage": exc.stage,
            },
        ) from exc
    except (S2TransientError, CacheMiss) as exc:
        logger.warning("add_node_upstream_unavailable id=%r: %s", body.s2_paper_id, exc)
        raise HTTPException(
            status_code=503,
            detail=f"Semantic Scholar is unavailable and this id is not cached: {exc}",
        ) from exc

    logger.info("node_added session=%s paper_id=%s forced=%s", sid, node.paper_id, body.force)
    return _to_response(engine, node)


__all__ = ["router"]
