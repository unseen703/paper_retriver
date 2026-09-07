"""
The `expansions` table. **Session-scoped** -- `session_id` first positional.

One row per expansion run, and it is the audit trail. Without `n_pool`,
`n_filtered`, `n_added`, `api_calls` and `cache_hits` written down, "why did I
only get four papers?" is unanswerable an hour later, and the honest reply
becomes "run it again and watch".

`error` carries truncation, not just failure. A run that hit `max_nodes` is
still `DONE` -- BUILD.md: "a partial expansion is a success" -- but the reason
it stopped early has to be recoverable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, text


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


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


def latest(conn: Connection, session_id: int) -> dict[str, Any] | None:
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


__all__ = ["finish", "latest", "start"]
