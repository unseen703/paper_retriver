"""
Request-scoped dependencies, and the process-wide resources behind them.

Two things outlive a request and must not be rebuilt per call: the SQLAlchemy
engine (which owns the connection pool) and the `S2Client` (which owns an
httpx connection pool *and* the rate limiter). Rebuilding the client per
request would give every request its own token bucket, which quietly turns a
1-req/sec limit into 1-req/sec-per-request -- the exact failure the limiter
exists to prevent.

They live here rather than in `main` so that routers can depend on them
without importing the app, which would be a cycle. `main`'s lifespan calls
`set_runtime` on startup and `clear_runtime` on shutdown.

**These functions are the override points for tests.** A test swaps in
`CachedOnlyS2Client` with

    application.dependency_overrides[get_s2_client] = lambda: offline

which is a structural offline guarantee rather than a mock: that client has no
usable transport at all.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Path
from sqlalchemy import Engine, text

from app.clients.s2 import S2Client

_engine: Engine | None = None
_client: S2Client | None = None


def set_runtime(engine: Engine, client: S2Client) -> None:
    """Install the process-wide resources. Called once, by the lifespan."""
    global _engine, _client
    _engine = engine
    _client = client


def clear_runtime() -> None:
    """Drop the references on shutdown so a second app in the same process
    cannot inherit a disposed engine."""
    global _engine, _client
    _engine = None
    _client = None


def get_engine() -> Engine:
    """The process-wide engine."""
    if _engine is None:  # pragma: no cover - lifespan always runs first
        raise RuntimeError("engine not initialised; is the app running?")
    return _engine


def get_s2_client() -> S2Client:
    """
    The process-wide S2 client.

    Overridden in tests. Never construct one per request -- see the module
    docstring on why that breaks rate limiting.
    """
    if _client is None:  # pragma: no cover - lifespan always runs first
        raise RuntimeError("S2 client not initialised; is the app running?")
    return _client


def existing_session(
    sid: Annotated[int, Path(ge=1, description="Session id.")],
    engine: Annotated[Engine, Depends(get_engine)],
) -> int:
    """
    Resolve `{sid}` to a session that actually exists, or 404.

    Every session-scoped route depends on this, for two reasons.

    **A write to an unknown session was a 500.** `graph_nodes.session_id` has a
    foreign key and `db.py` sets `PRAGMA foreign_keys=ON`, so a bad id reached
    SQLite and came back as a raw IntegrityError with a stack trace.

    **A read of an unknown session was a 200 with an empty graph** -- which is
    worse, because it is indistinguishable from a real but empty workspace. A
    client holding a stale id after a database reset would render "no papers
    yet" rather than an error, and then 500 on the first write.

    It runs before the endpoint body, so an unknown id costs no S2 request.
    """
    with engine.connect() as conn:
        found = conn.execute(text("SELECT 1 FROM sessions WHERE id = :sid"), {"sid": sid}).scalar()
    if not found:
        raise HTTPException(status_code=404, detail=f"no session {sid}")
    return sid


__all__ = [
    "clear_runtime",
    "existing_session",
    "get_engine",
    "get_s2_client",
    "set_runtime",
]
