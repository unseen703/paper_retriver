"""
`POST /api/sessions/{sid}/expansions` and `GET .../expansions/{job_id}` (R2.4).

**Asynchronous as of R2.4.** R1.18 ran the expansion inside the request and
returned 200 with the result, deliberately, because a job id nothing could poll
would have been a worse promise than a slow request. That trade is now paid
off: PLAN.md's shape -- 202 with an id, then polling -- is what this does.

**The job id is the expansion id.** BUILD.md asks for a `jobs` table; PLAN.md
section C defines `expansions` with every column a job needs and no `jobs`
table at all. Two tables would mean two rows per run to keep in step. See
`repo/expansions.py`.

**One expansion per session, and the second POST is a 409.** Scoped to the
session rather than to the process: two sessions expanding at once is
reasonable, while two runs inside one session would interleave writes to the
same `graph_nodes` rows and leave a graph neither of them intended. The 409
carries the id of the run already in flight, so a client that lost track can
start polling it rather than only being told no.

**Validation still happens before the 202.** A bad `max_new`, a multi-hop
request, or a graph already at `max_nodes` are refused synchronously. Accepting
those into the queue would turn a mistake the caller could fix immediately into
a job they have to poll to discover failed.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy import Engine

from app.api.deps import existing_session, get_engine, get_worker
from app.repo import expansions as expansions_repo
from app.repo import graph as graph_repo
from app.schemas.expansions import (
    ExpandRequest,
    ExpandResponse,
    JobAccepted,
    JobCancelled,
    JobProgress,
    JobStatus,
)
from app.services.expansion import ExpandParams
from app.services.jobs import JobAlreadyRunning, JobWorker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["expansions"])


@router.post(
    "/{sid}/expansions",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_expansion(
    sid: Annotated[int, Depends(existing_session)],
    engine: Annotated[Engine, Depends(get_engine)],
    worker: Annotated[JobWorker, Depends(get_worker)],
    response: Response,
    body: ExpandRequest | None = None,
) -> JobAccepted:
    """Queue an expansion and answer with the id to poll."""
    params_in = body or ExpandRequest()

    # Refuse a full graph before queueing anything. `expand` would handle this
    # gracefully -- it clamps to the remaining room and reports truncation --
    # but a job that can only ever admit zero papers is not worth the round
    # trip, and the caller can act on this answer right now.
    with engine.connect() as conn:
        # A count, not the ids: materialising 2000 paper ids to take a length
        # is the same over-fetch that made search scale with graph size.
        current = sum(graph_repo.count_by_state(conn, sid).values())
    if current >= params_in.max_nodes:
        raise HTTPException(
            status_code=422,
            detail=(
                f"session {sid} is at max_nodes ({current}/{params_in.max_nodes});"
                " remove nodes or raise max_nodes before expanding"
            ),
        )

    try:
        job_id = worker.enqueue(
            sid,
            ExpandParams(
                max_new=params_in.max_new,
                api_call_budget=params_in.api_call_budget,
                max_nodes=params_in.max_nodes,
            ),
        )
    except JobAlreadyRunning as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": str(exc),
                "error_code": "EXPANSION_IN_FLIGHT",
                # The id of the run already going, so a client that lost track
                # of its job can poll that one instead of being stuck.
                "job_id": exc.job_id,
            },
        ) from exc

    poll = f"/api/sessions/{sid}/expansions/{job_id}"
    # Location is what 202 is for: the id is in the body for convenience, and
    # in the header for anything that speaks HTTP rather than this schema.
    response.headers["Location"] = poll
    logger.info("expansion_queued session=%s job_id=%s", sid, job_id)
    return JobAccepted(job_id=job_id, session_id=sid, status="QUEUED", poll=poll)


@router.delete("/{sid}/expansions/{job_id}", response_model=JobCancelled)
def cancel_expansion(
    sid: Annotated[int, Depends(existing_session)],
    job_id: Annotated[int, Path(ge=1, description="The id returned by the 202.")],
    engine: Annotated[Engine, Depends(get_engine)],
) -> JobCancelled:
    """
    Ask a job to stop -- PLAN.md section G's cooperative cancel.

    A QUEUED job stops outright, because `claim_next` will never pick up a
    cancelled row. A RUNNING one is asked to stop at its next checkpoint, and
    anything it already committed stays: those papers were fetched with real
    API calls, and discarding them would throw away what the run already cost.

    **409, not 200, for a job that has finished.** There is nothing to stop, and
    reporting success for an action that did nothing is what makes a cancel
    button untrustworthy the first time someone checks whether it worked.
    """
    with engine.begin() as conn:
        # 404 before 409: an id this session does not own must read as absent
        # rather than leaking that it exists and is merely finished.
        if expansions_repo.get(conn, sid, job_id) is None:
            raise HTTPException(status_code=404, detail=f"no expansion {job_id} in session {sid}")
        was = expansions_repo.cancel(conn, sid, job_id)

    if was is None:
        raise HTTPException(
            status_code=409,
            detail=f"expansion {job_id} is not running; there is nothing to cancel",
        )

    logger.info("expansion_cancelled session=%s job_id=%s was=%s", sid, job_id, was)
    return JobCancelled(job_id=job_id, session_id=sid, was=was)


@router.get("/{sid}/expansions/{job_id}", response_model=JobStatus)
def get_expansion(
    sid: Annotated[int, Depends(existing_session)],
    job_id: Annotated[int, Path(ge=1, description="The id returned by the 202.")],
    engine: Annotated[Engine, Depends(get_engine)],
) -> JobStatus:
    """One job's status, stage and counters. Poll this about once a second."""
    with engine.connect() as conn:
        record = expansions_repo.get(conn, sid, job_id)
        added = (
            graph_repo.node_ids_added_by(conn, sid, job_id)
            if record is not None and record["status"] == "DONE"
            else []
        )
    if record is None:
        # Scoped to the session, so another session's job reads as absent
        # rather than leaking its counts.
        raise HTTPException(status_code=404, detail=f"no expansion {job_id} in session {sid}")

    return JobStatus(
        job_id=int(record["id"]),
        session_id=int(record["session_id"]),
        status=str(record["status"]),
        stage=expansions_repo.stage_of(record),
        progress=JobProgress(
            pool=record["n_pool"],
            filtered=record["n_filtered"],
            added=record["n_added"],
            api_calls=record["api_calls"],
            cache_hits=record["cache_hits"],
        ),
        error=record["error"],
        result=_result_of(record, added),
        started_at=record["started_at"],
        finished_at=record["finished_at"],
    )


def _result_of(record: dict[str, Any], added: list[int]) -> ExpandResponse | None:
    """
    The finished run's numbers, or None while it is still going.

    `added_paper_ids` is read back from `graph_nodes.added_by` rather than
    carried in memory: the worker that computed it is a different thread from
    the one answering this request, and a process restart between the two must
    not lose the answer. `added_by` is written by the same transaction that
    admitted the nodes, so it cannot disagree with the graph.
    """
    if record["status"] != "DONE":
        return None
    return ExpandResponse(
        n_pool=record["n_pool"] or 0,
        n_added=record["n_added"] or 0,
        added_paper_ids=added,
        boundary=record["n_filtered"] or 0,
        api_calls=record["api_calls"] or 0,
        cache_hits=record["cache_hits"] or 0,
        # Derived, because no column holds it -- and "does `error` say
        # anything" is the wrong derivation. `error` also carries "no anchors
        # in this session", a run that did not stop early because there was
        # nothing to stop. Truncation means the run left candidates on the
        # table, which is exactly `n_pool > n_added`.
        truncated=bool(record["error"]) and (record["n_pool"] or 0) > (record["n_added"] or 0),
        error=record["error"],
    )


__all__ = ["router"]
