"""
`POST /api/sessions/{sid}/expansions` (R1.18) -- **synchronous at R1**.

BUILD.md is explicit: return 200 with the result directly, and do not build the
job system -- that is R2.4. PLAN.md sketches a 202 with a job id, and this
deliberately does not do that. A 202 would promise a job id nothing can poll,
and building the queue now means writing the polling endpoint, the cancel
endpoint and the concurrency guard before anything has ever been expanded
through HTTP.

The trade-off is an HTTP request that can run for tens of seconds. That is
acceptable for a single-user local tool and is why R2.4 exists.

**Budget exhaustion is not an error.** PLAN.md: a partial expansion is a
success. Running out of API calls or graph room commits what was gathered and
records the truncation; the status stays 200 and `truncated` says so. The
single exception BUILD.md names is a graph already at `max_nodes` -- there is
no room to admit anything, so fetching would spend API calls to achieve
nothing. That is refused with a 422 *before* the fetch.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Engine

from app.api.deps import existing_session, get_engine, get_s2_client
from app.clients.s2 import S2Client
from app.config import filters, ranking
from app.repo import graph as graph_repo
from app.schemas.expansions import ExpandRequest, ExpandResponse
from app.services.expansion import ExpandParams, expand

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["expansions"])


@router.post("/{sid}/expansions", response_model=ExpandResponse)
async def create_expansion(
    sid: Annotated[int, Depends(existing_session)],
    client: Annotated[S2Client, Depends(get_s2_client)],
    engine: Annotated[Engine, Depends(get_engine)],
    body: ExpandRequest | None = None,
) -> ExpandResponse:
    """Expand this session's graph by one hop and return what the run did."""
    params_in = body or ExpandRequest()

    # Refuse a full graph before spending anything. `expand` would handle this
    # gracefully -- it clamps to the remaining room and reports truncation --
    # but it would fetch first, and fetching to admit zero papers is the one
    # outcome worth an error code.
    with engine.connect() as conn:
        # A count, not the ids: materialising 2000 paper ids to take a
        # length is the same over-fetch that made search scale with graph
        # size, one endpoint over.
        current = sum(graph_repo.count_by_state(conn, sid).values())
    if current >= params_in.max_nodes:
        raise HTTPException(
            status_code=422,
            detail=(
                f"session {sid} is at max_nodes ({current}/{params_in.max_nodes});"
                " remove nodes or raise max_nodes before expanding"
            ),
        )

    result = await expand(
        engine,
        client,
        sid,
        ExpandParams(
            max_new=params_in.max_new,
            api_call_budget=params_in.api_call_budget,
            max_nodes=params_in.max_nodes,
        ),
        filters,
        ranking,
        # Relative to now, so the corpus era keeps meaning what it says.
        datetime.now(UTC).year,
    )

    logger.info(
        "expansion_done session=%s pool=%s added=%s api_calls=%s truncated=%s",
        sid,
        result.n_pool,
        result.n_added,
        result.api_calls,
        result.truncated,
    )
    return ExpandResponse(
        n_pool=result.n_pool,
        n_added=result.n_added,
        added_paper_ids=result.added_paper_ids,
        boundary=result.ingest.boundary,
        backward_fetched=result.backward_fetched,
        forward_fetched=result.forward_fetched,
        hub_skipped=result.hub_skipped,
        api_calls=result.api_calls,
        cache_hits=result.cache_hits,
        truncated=result.truncated,
        error=result.error,
    )


__all__ = ["router"]
