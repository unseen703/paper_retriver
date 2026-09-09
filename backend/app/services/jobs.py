"""
The background worker (R2.4).

PLAN.md calls the mechanics "deliberately boring", and rules out Celery and
Redis for a single-user local tool: "a `jobs` table in SQLite + one worker
thread + polling is ~80 lines and does everything you need". This is that.

**The queue is the `expansions` table.** BUILD.md says "jobs table"; PLAN.md
section C defines no such table and defines `expansions` with exactly the
columns -- `status` already reading `QUEUED|RUNNING|DONE|FAILED|CANCELLED`,
`params`, the counters, `error`, both timestamps. So the job id is the
expansion id. See `repo/expansions.py`.

**One job per session, not one job at a time.** The 409 is scoped to the
session because two sessions expanding at once is a coherent thing to want,
while two expansions racing inside one session would interleave writes to the
same `graph_nodes` rows and produce a graph neither run intended.

**The coroutine runs on the API's event loop, not on this thread.** `expand` is
async and the `S2Client` owns an `httpx.AsyncClient` bound to the loop it was
built on; driving it from a second loop in this thread would either break or --
worse -- work by accident until the connection pool was reused. Giving the
worker its own client would be the other way out, and a worse one: it would
also give it its own token bucket, quietly turning one request per second into
two. So the thread owns claiming, sequencing and status; the work itself is
handed back to the loop that owns the client.

**A RUNNING row at startup means the process died.** The worker lives and dies
with the process, so nothing is executing that row. It is failed on startup
rather than left alone, because leaving it would block its session on a 409
forever with no way to see why.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine

from app.clients.s2 import S2Client
from app.config import FiltersConfig, RankingConfig
from app.repo import expansions as expansions_repo
from app.services.expansion import ExpandParams, expand

logger = logging.getLogger(__name__)

#: How long the worker sleeps when it finds nothing to do. Short enough that a
#: POST feels immediate, long enough that an idle process is not spinning.
IDLE_POLL_SECONDS = 0.05


class JobAlreadyRunning(RuntimeError):
    """
    This session already has an unfinished expansion. The caller returns 409.

    Carries the existing job's id, so the UI can poll the run that is already
    happening instead of only telling the user "no".
    """

    def __init__(self, session_id: int, job_id: int) -> None:
        super().__init__(f"session {session_id} already has expansion {job_id} in flight")
        self.session_id = session_id
        self.job_id = job_id


class JobWorker:
    """One thread, draining `expansions` rows in id order."""

    def __init__(
        self,
        engine: Engine,
        client_factory: Callable[[], S2Client],
        filters_cfg: FiltersConfig,
        ranking_cfg: RankingConfig,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._engine = engine
        # A factory rather than a client, so a test's `dependency_overrides`
        # reaches background work too. Resolving it once at construction would
        # leave the worker as the only place in the process that could still
        # open a socket during an offline test.
        self._client_factory = client_factory
        self._filters = filters_cfg
        self._ranking = ranking_cfg
        self._loop = loop
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Serialises enqueue against itself. SQLite gives no way to express
        # "at most one unfinished row per session" without a partial unique
        # index, which would be new schema; and a check-then-insert in two
        # statements lets two simultaneous POSTs both see an empty queue and
        # both insert. One process (CLAUDE.md locks that) means one lock is
        # enough, and it is honest about why it exists.
        self._enqueue_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        # Never fatal. This runs inside the lifespan, and an exception here
        # would stop the app from starting at all -- so a database that is
        # missing, unmigrated or unreadable would take down `/api/health`,
        # which is the one endpoint whose job is to tell you that. Health
        # reports the problem; the worker simply has nothing to do until it is
        # fixed, and the claim loop below is tolerant of the same failure.
        try:
            with self._engine.begin() as conn:
                abandoned = expansions_repo.abandon_running(conn)
            if abandoned:
                logger.warning("abandoned_running_expansions ids=%s", abandoned)
        except Exception as exc:  # noqa: BLE001 - startup must survive a bad database
            logger.warning("abandon_running_failed: %s", exc)

        self._thread = threading.Thread(target=self._run, name="expansion-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # -- the API's side ----------------------------------------------------

    def enqueue(self, session_id: int, params: ExpandParams) -> int:
        """
        Queue one expansion. Returns the job id, or raises `JobAlreadyRunning`.

        The check and the insert are one critical section. Without the lock,
        BUILD.md's verification -- two concurrent POSTs, the second gets 409 --
        passes only by luck of scheduling.
        """
        with self._enqueue_lock, self._engine.begin() as conn:
            existing = expansions_repo.active(conn, session_id)
            if existing is not None:
                raise JobAlreadyRunning(session_id, int(existing["id"]))
            return expansions_repo.enqueue(
                conn,
                session_id,
                {
                    "max_new": params.max_new,
                    "api_call_budget": params.api_call_budget,
                    "max_nodes": params.max_nodes,
                },
                self._filters.config_version,
            )

    # -- the thread --------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.wait(IDLE_POLL_SECONDS):
            try:
                job = self._claim()
            except Exception:  # pragma: no cover - a claim failure must not kill the worker
                logger.exception("job_claim_failed")
                continue
            if job is None:
                continue
            self._execute(job)

    def _claim(self) -> dict[str, Any] | None:
        with self._engine.begin() as conn:
            return expansions_repo.claim_next(conn)

    def _execute(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        session_id = int(job["session_id"])
        params = job["params"]
        logger.info("job_started job_id=%s session=%s", job_id, session_id)

        try:
            future = asyncio.run_coroutine_threadsafe(
                expand(
                    self._engine,
                    self._client_factory(),
                    session_id,
                    ExpandParams(
                        max_new=int(params.get("max_new", 25)),
                        api_call_budget=int(params.get("api_call_budget", 50)),
                        max_nodes=int(params.get("max_nodes", 2000)),
                    ),
                    self._filters,
                    self._ranking,
                    datetime.now(UTC).year,
                    expansion_id=job_id,
                ),
                self._loop,
            )
            result = future.result()
        except Exception as exc:  # noqa: BLE001 - the boundary; nothing above catches
            # Only genuinely unexpected failures land here. Budget exhaustion
            # and a full graph are DONE with a note, because a partial
            # expansion is a success and marking it FAILED would tell the user
            # to retry something that already worked.
            logger.exception("job_failed job_id=%s session=%s", job_id, session_id)
            with self._engine.begin() as conn:
                expansions_repo.fail(conn, session_id, job_id, f"{type(exc).__name__}: {exc}")
            return

        logger.info(
            "job_done job_id=%s session=%s added=%s truncated=%s",
            job_id,
            session_id,
            result.n_added,
            result.truncated,
        )


__all__ = ["IDLE_POLL_SECONDS", "JobAlreadyRunning", "JobWorker"]
