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

from sqlalchemy import Engine

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


__all__ = ["clear_runtime", "get_engine", "get_s2_client", "set_runtime"]
