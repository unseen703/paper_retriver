"""
The FastAPI application (R1.14).

    uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

Two security constraints from PLAN.md's "Security (proportionate)" table are
enforced here rather than left to convention:

**CORS is an explicit allowlist**, `http://localhost:5173` only, never `*`.
PLAN.md is blunt about the reasoning -- "it's one line and reviewers check".

**The app binds 127.0.0.1.** There is no authentication layer anywhere in this
design, so binding `0.0.0.0` would publish the entire corpus to the local
network. The Makefile's `dev` target carries the flag, and a test asserts it.

`/api/health` deliberately reports four specific things rather than
`{"status": "ok"}`. A health endpoint that cannot distinguish "the database is
gone" from "the graph is empty" answers none of the questions you actually have
at 2am, which is why such endpoints get ignored.

`s2_reachable` never makes a network call. A monitoring loop that costs an
upstream request becomes a rate-limit problem, and at one request per second
that is the entire budget -- so it reports whether a key is *configured*, which
is the failure people actually hit.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine, text

from app.api import deps
from app.api.config import router as config_router
from app.api.expansions import router as expansions_router
from app.api.graph import router as graph_router
from app.api.node_detail import router as node_detail_router
from app.api.nodes import router as nodes_router
from app.api.search import router as search_router
from app.api.sessions import router as sessions_router
from app.clients.cache import ResponseCache
from app.clients.s2 import S2Client
from app.config import filters, ranking, settings
from app.db import db_url as default_db_url
from app.db import make_engine
from app.logging_setup import bind_session, configure_logging
from app.schemas.health import HealthResponse
from app.services.jobs import JobWorker

logger = logging.getLogger(__name__)

# Explicit, not "*". The Vite dev server's port, in both spellings of
# loopback: `localhost` and `127.0.0.1` are interchangeable in every
# developer's head and are different origins to a browser. Listing only one
# means the app silently fails when opened at the other -- every request
# blocked in the console while the backend logs nothing at all, which points
# debugging at the wrong process. Two entries, still an allowlist, still not
# "*".
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


def get_engine() -> Engine:
    """
    The process-wide engine, opened by the lifespan handler.

    Kept as a re-export: the engine itself lives in `api.deps` so routers can
    depend on it without importing this module, which would be a cycle.
    """
    return deps.get_engine()


def create_app(db_url: str | None = None) -> FastAPI:
    """
    Build the app. `db_url` is injectable so tests can point at a temp database
    without monkeypatching module state.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        configure_logging(level=settings.log_level)
        bind_session(session_id=settings.session_id)
        engine = make_engine(db_url or default_db_url())
        # One client for the process, not one per request: the rate limiter
        # lives inside it, and a per-request client would give every request
        # its own token bucket -- turning 1 req/sec into 1 req/sec *each*.
        key = settings.s2_api_key.get_secret_value() if settings.s2_api_key else None
        client = S2Client(cache=ResponseCache(engine), api_key=key, rate=settings.s2_rate_limit)

        def worker_client() -> S2Client:
            """
            Resolve the S2 client the way a request would, override included.

            The worker runs outside FastAPI's dependency injection, so a test
            that swaps in `CachedOnlyS2Client` through `dependency_overrides`
            would otherwise leave background expansions as the one place in the
            process still able to open a socket. Consulting the same override
            table keeps "the tests cannot reach the network" structural rather
            than a thing to remember.
            """
            override = _app.dependency_overrides.get(deps.get_s2_client)
            return override() if override is not None else client

        # The worker hands its coroutines back to *this* loop rather than
        # running its own: the client owns an httpx pool bound to the loop it
        # was built on, and a second client would mean a second token bucket.
        worker = JobWorker(
            engine,
            worker_client,
            filters,
            ranking,
            asyncio.get_running_loop(),
        )
        deps.set_runtime(engine, client, worker)
        worker.start()
        logger.info("api_startup config_version=%s", filters.config_version)
        try:
            yield
        finally:
            # Stop the worker first: it writes to the engine, so disposing the
            # engine underneath a running job would fail the job for a reason
            # that has nothing to do with the job.
            worker.stop()
            await client.aclose()
            engine.dispose()
            deps.clear_runtime()

    application = FastAPI(
        title="Citation-graph paper recommender",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Before the session-scoped routers: `/api/sessions` must not be
    # shadowed by `/api/sessions/{sid}/...` matching an empty segment.
    application.include_router(config_router)
    application.include_router(sessions_router)
    application.include_router(search_router)
    application.include_router(nodes_router)
    application.include_router(node_detail_router)
    application.include_router(graph_router)
    application.include_router(expansions_router)

    @application.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """
        Is the database reachable, is S2 configured, how much is cached, and
        how big is the graph.

        Never raises: an endpoint that 500s when the database is down tells you
        nothing a timeout would not.
        """
        db_status = "ok"
        cache_rows = 0
        node_count = 0
        try:
            with get_engine().connect() as conn:
                cache_rows = int(conn.execute(text("SELECT COUNT(*) FROM api_cache")).scalar() or 0)
                node_count = int(
                    conn.execute(
                        text("SELECT COUNT(*) FROM graph_nodes WHERE session_id = :sid"),
                        {"sid": settings.session_id},
                    ).scalar()
                    or 0
                )
        except Exception as exc:  # noqa: BLE001 - health must report, never raise
            logger.warning("health_db_unreachable: %s", exc)
            db_status = f"error: {type(exc).__name__}"

        return HealthResponse(
            db=db_status,
            # Deliberately not a live probe -- see the module docstring.
            s2_reachable="configured" if settings.s2_api_key else "no_api_key",
            cache_rows=cache_rows,
            node_count=node_count,
            session_id=settings.session_id,
            # Which filters produced this graph is the first question when the
            # recommendations look wrong.
            config_version=filters.config_version,
        )

    return application


app = create_app()

__all__ = ["DEV_ORIGINS", "app", "create_app", "get_engine"]
