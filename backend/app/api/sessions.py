"""
`GET` and `POST /api/sessions` (R2.16).

The last piece of the session contract, and the one that makes the rest of it
worth anything. BUILD.md's verification is the payoff: create a second session,
seed a paper the first already fetched, and it costs **zero API calls**.

That falls out of a boundary drawn in R1.1 rather than out of anything here.
`papers`, `authors` and `edges` are global facts; `graph_nodes`,
`interaction_events` and `expansions` are opinions, keyed by session. A paper
one session paid for is simply already in the corpus for the next one. If the
boundary had been drawn wrong, the only symptom would be a bill -- which is why
`test_api_sessions.py` asserts it against the client's own call counter.

**No DELETE.** Removing a session would orphan its events, and the log is the
audit trail that `scripts/rebuild_state.py` reconstructs from. R2.15's clear
already empties a graph without destroying its history, which is what anyone
actually wants when they say "get rid of this".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import Engine, text

from app.api.deps import get_engine
from app.schemas.sessions import CreateSessionRequest, SessionOut

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("", response_model=list[SessionOut])
def list_sessions(engine: Annotated[Engine, Depends(get_engine)]) -> list[SessionOut]:
    """
    Every session, oldest first, each with the size of its graph.

    The node count is what makes a switcher usable: without it every row looks
    identical, and the session holding your work is indistinguishable from the
    empty one you made by accident.

    Ordered by id rather than by name or recency -- a list someone reads
    top-down should not reshuffle as they add to it (CLAUDE.md rule 7).
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT s.id, s.name, s.created_at,"
                "       (SELECT COUNT(*) FROM graph_nodes g WHERE g.session_id = s.id)"
                " FROM sessions s ORDER BY s.id"
            )
        )
        return [
            SessionOut(id=int(i), name=str(name), created_at=str(created), node_count=int(count))
            for i, name, created, count in rows
        ]


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def create_session(
    body: CreateSessionRequest,
    engine: Annotated[Engine, Depends(get_engine)],
) -> SessionOut:
    """
    Start a new, empty graph over the same corpus.

    Nothing is copied from any existing session -- that is the point. The
    papers are already there; the opinions are not.
    """
    created_at = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO sessions (name, created_at) VALUES (:name, :created_at) RETURNING id"
            ),
            {"name": body.name.strip(), "created_at": created_at},
        ).fetchone()
    if row is None:  # pragma: no cover - RETURNING always yields a row
        raise RuntimeError("session insert produced no id")

    return SessionOut(id=int(row[0]), name=body.name.strip(), created_at=created_at, node_count=0)


__all__ = ["router"]
