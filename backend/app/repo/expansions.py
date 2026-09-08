"""
The `expansions` table. **Session-scoped** -- `session_id` first positional.

One row per expansion run, and it is the audit trail. Without `n_pool`,
`n_filtered`, `n_added`, `api_calls` and `cache_hits` written down, "why did I
only get four papers?" is unanswerable an hour later, and the honest reply
becomes "run it again and watch".

`error` carries truncation, not just failure. A run that hit `max_nodes` is
still `DONE` -- BUILD.md: "a partial expansion is a success" -- but the reason
it stopped early has to be recoverable.

**This is also R2.4's job queue.** BUILD.md asks for a `jobs` table; PLAN.md
section C defines no such table and defines this one instead, with exactly the
columns a job needs -- `status` already spelled `QUEUED|RUNNING|DONE|FAILED|
CANCELLED`, the input in `params`, counters for progress, `error`, and both
timestamps. A separate `jobs` table would duplicate every one of those and
leave two rows per expansion to keep in step. So the job id *is* the expansion
id, and no schema was invented (CLAUDE.md rule 2).

The one thing PLAN.md's polling shape asks for and no column holds is `stage`.
It is derived rather than stored -- see `stage_of` -- for the same reason a
tombstone is derived from the event log: a stored stage is a second place for
the truth to live, and the one that goes stale when a process dies mid-run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, text

#: A job that has not finished. Both count against the one-at-a-time rule:
#: a QUEUED second run would start the moment the first ended, which is not
#: what "already running" means to whoever asked.
ACTIVE_STATUSES = ("QUEUED", "RUNNING")

#: Every column the polling endpoint reads. Named once so the SELECT and the
#: dict it becomes cannot drift apart.
_COLUMNS = (
    "id",
    "session_id",
    "status",
    "params",
    "n_pool",
    "n_filtered",
    "n_added",
    "api_calls",
    "cache_hits",
    "error",
    "started_at",
    "finished_at",
)
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM expansions"


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _row(row: Any) -> dict[str, Any]:
    record = dict(zip(_COLUMNS, row, strict=True))
    record["params"] = json.loads(record["params"]) if record["params"] else {}
    return record


def start(conn: Connection, session_id: int, params: dict[str, Any], config_version: str) -> int:
    """Open a RUNNING row before any work, so a crash leaves evidence."""
    row = conn.execute(
        text(
            "INSERT INTO expansions (session_id, status, params, config_version, started_at)"
            " VALUES (:session_id, 'RUNNING', :params, :config_version, :started_at)"
            " RETURNING id"
        ),
        {
            "session_id": session_id,
            "params": json.dumps(params),
            "config_version": config_version,
            "started_at": _utcnow(),
        },
    ).fetchone()
    if row is None:  # pragma: no cover - RETURNING always yields a row
        raise RuntimeError("expansion insert produced no id")
    return int(row[0])


def finish(
    conn: Connection,
    session_id: int,
    expansion_id: int,
    *,
    status: str,
    n_pool: int,
    n_filtered: int,
    n_added: int,
    api_calls: int,
    cache_hits: int,
    error: str | None = None,
) -> None:
    conn.execute(
        text(
            "UPDATE expansions SET status = :status, n_pool = :n_pool,"
            " n_filtered = :n_filtered, n_added = :n_added, api_calls = :api_calls,"
            " cache_hits = :cache_hits, error = :error, finished_at = :finished_at"
            " WHERE id = :expansion_id AND session_id = :session_id"
        ),
        {
            "status": status,
            "n_pool": n_pool,
            "n_filtered": n_filtered,
            "n_added": n_added,
            "api_calls": api_calls,
            "cache_hits": cache_hits,
            "error": error,
            "finished_at": _utcnow(),
            "expansion_id": expansion_id,
            "session_id": session_id,
        },
    )


def enqueue(conn: Connection, session_id: int, params: dict[str, Any], config_version: str) -> int:
    """
    Insert a QUEUED row for the worker to pick up (R2.4). Returns the job id.

    Distinct from `start`, which opens a row already RUNNING for a caller that
    is about to do the work itself. Enqueuing records the *request*, and the
    gap between the two is the whole point of a 202: the id is answerable
    before anything has happened.
    """
    row = conn.execute(
        text(
            "INSERT INTO expansions (session_id, status, params, config_version)"
            " VALUES (:session_id, 'QUEUED', :params, :config_version) RETURNING id"
        ),
        {
            "session_id": session_id,
            "params": json.dumps(params),
            "config_version": config_version,
        },
    ).fetchone()
    if row is None:  # pragma: no cover - RETURNING always yields a row
        raise RuntimeError("expansion insert produced no id")
    return int(row[0])


def active(conn: Connection, session_id: int) -> dict[str, Any] | None:
    """The session's unfinished job, if it has one. What the 409 is built on."""
    placeholders = ",".join(f":s{i}" for i in range(len(ACTIVE_STATUSES)))
    row = conn.execute(
        text(
            f"{_SELECT} WHERE session_id = :session_id"
            f" AND status IN ({placeholders}) ORDER BY id LIMIT 1"
        ),
        {
            "session_id": session_id,
            **{f"s{i}": v for i, v in enumerate(ACTIVE_STATUSES)},
        },
    ).fetchone()
    return _row(row) if row is not None else None


def claim_next(conn: Connection) -> dict[str, Any] | None:
    """
    Take the oldest QUEUED job and mark it RUNNING, atomically. Returns it.

    The claim is a single UPDATE with the selection inside it rather than a
    SELECT followed by an UPDATE. One worker thread makes that academic today,
    but a claim that can be observed half-done is the kind of thing that stays
    correct only as long as nobody adds a second worker, and the atomic form
    costs nothing.
    """
    row = conn.execute(
        text(
            "UPDATE expansions SET status = 'RUNNING', started_at = :now"
            " WHERE id = (SELECT id FROM expansions WHERE status = 'QUEUED'"
            "             ORDER BY id LIMIT 1)"
            f" RETURNING {', '.join(_COLUMNS)}"
        ),
        {"now": _utcnow()},
    ).fetchone()
    return _row(row) if row is not None else None


def get(conn: Connection, session_id: int, expansion_id: int) -> dict[str, Any] | None:
    """
    One job, scoped to its session.

    Scoped rather than global so a stale id from another session reads as
    absent instead of leaking that session's counts -- the same reasoning that
    put `{sid}` in the route.
    """
    row = conn.execute(
        text(f"{_SELECT} WHERE id = :expansion_id AND session_id = :session_id"),
        {"expansion_id": expansion_id, "session_id": session_id},
    ).fetchone()
    return _row(row) if row is not None else None


def record_progress(
    conn: Connection,
    session_id: int,
    expansion_id: int,
    **counters: int | None,
) -> None:
    """
    Write the counters known so far, mid-run.

    Progress has to be readable from the database rather than from the worker's
    memory, because the thing polling it is a different request on a different
    thread -- and because a run interrupted halfway should leave behind how far
    it got rather than nothing at all.

    Only the named counters are touched; a column not passed keeps its value,
    so an early call cannot blank out something a later stage already wrote.
    """
    known = {k: v for k, v in counters.items() if k in _COLUMNS and v is not None}
    if not known:
        return
    assignments = ", ".join(f"{name} = :{name}" for name in known)
    conn.execute(
        text(
            f"UPDATE expansions SET {assignments}"
            " WHERE id = :expansion_id AND session_id = :session_id"
        ),
        {**known, "expansion_id": expansion_id, "session_id": session_id},
    )


def fail(conn: Connection, session_id: int, expansion_id: int, error: str) -> None:
    """
    Mark a job FAILED.

    Reserved for an *unexpected* error. Budget exhaustion and a full graph are
    DONE with a note in `error`, because a partial expansion is a success and
    calling it FAILED would tell the user to retry something that worked.
    """
    conn.execute(
        text(
            "UPDATE expansions SET status = 'FAILED', error = :error, finished_at = :now"
            " WHERE id = :expansion_id AND session_id = :session_id"
        ),
        {"error": error, "now": _utcnow(), "expansion_id": expansion_id, "session_id": session_id},
    )


def abandon_running(conn: Connection) -> list[int]:
    """
    Fail every RUNNING job. Called once at startup; returns the ids.

    A RUNNING row can only be left behind by a process that died, since the
    worker lives and dies with it. Leaving those rows alone would block their
    sessions forever on a 409 for a job nothing is executing, and the user has
    no way to see why. Failing them says what happened and unblocks the
    session.
    """
    rows = conn.execute(
        text(
            "UPDATE expansions SET status = 'FAILED', finished_at = :now,"
            " error = COALESCE(error, 'interrupted: the server stopped while this was running')"
            " WHERE status = 'RUNNING' RETURNING id"
        ),
        {"now": _utcnow()},
    ).fetchall()
    return [int(row[0]) for row in rows]


def stage_of(record: dict[str, Any]) -> str:
    """
    Which part of the pipeline a job is in, derived from what it has recorded.

    Not a column. The counters already say how far the run got, and a stored
    stage would be a second copy of that -- one that a killed process leaves
    pointing at a step nothing is performing. Deriving it means the answer
    cannot disagree with the evidence.

    The names match PLAN.md's pipeline: FETCHING is the S2 round trips and the
    ingest cascade, POOLING is bibliographic coupling over what came back,
    RANKING is prescore and allocation.
    """
    status = record["status"]
    if status != "RUNNING":
        return str(status)
    if record["n_filtered"] is None:
        return "FETCHING"
    if record["n_pool"] is None:
        return "POOLING"
    if record["n_added"] is None:
        return "RANKING"
    return "ADDING"


def latest(conn: Connection, session_id: int) -> dict[str, Any] | None:
    """The session's most recent run, whatever its status."""
    row = conn.execute(
        text(
            "SELECT id, status, n_pool, n_filtered, n_added, api_calls, cache_hits, error"
            " FROM expansions WHERE session_id = :session_id ORDER BY id DESC LIMIT 1"
        ),
        {"session_id": session_id},
    ).fetchone()
    if row is None:
        return None
    keys = ("id", "status", "n_pool", "n_filtered", "n_added", "api_calls", "cache_hits", "error")
    return dict(zip(keys, row, strict=True))


__all__ = [
    "ACTIVE_STATUSES",
    "abandon_running",
    "active",
    "claim_next",
    "enqueue",
    "fail",
    "finish",
    "get",
    "latest",
    "record_progress",
    "stage_of",
    "start",
]
