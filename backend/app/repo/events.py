"""
The `interaction_events` table -- append-only history. **Session-scoped.**

`session_id` first positional, no default, same contract as `repo/graph.py`.

Nothing here updates or deletes. `graph_nodes.state` is a cache of "replay this
paper's events"; the log is the source of truth, and R2.3's
`scripts/rebuild_state.py` reconstructs the projection from it. That only works
if the log is genuinely append-only.

**Ordering is by `id`, never by `created_at`.** Twenty labels applied inside one
second share an ISO timestamp to the microsecond on a fast machine, so a
timestamp sort makes the rebuild non-deterministic. The monotonic rowid is the
only safe key, and there is a test that pins it.

**A tombstone is a query, not a flag**: the latest event for (session, paper) is
REMOVED or GC_SWEPT. Deriving it is what makes R2.8's restore work -- RESTORED
simply becomes the newer event, with nothing to remember to clear.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, text

from app.models import InteractionEvent

# Events after which a paper is considered removed from the session's graph.
TOMBSTONE_EVENTS = ("REMOVED", "GC_SWEPT")


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def append_event(
    conn: Connection,
    session_id: int,
    paper_id: int,
    event_type: str,
    actor: str = "USER",
    payload: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> int:
    """
    Append one event. Returns its id.

    `actor` distinguishes a user's removal from a cascade sweep, which is what
    R2.6 reports back and what R2.14's review drawer groups by.
    """
    row = conn.execute(
        text(
            "INSERT INTO interaction_events"
            " (session_id, paper_id, event_type, actor, payload, created_at)"
            " VALUES (:session_id, :paper_id, :event_type, :actor, :payload, :created_at)"
            " RETURNING id"
        ),
        {
            "session_id": session_id,
            "paper_id": paper_id,
            "event_type": event_type,
            "actor": actor,
            "payload": json.dumps(payload) if payload else None,
            "created_at": created_at or _utcnow(),
        },
    ).fetchone()
    if row is None:  # pragma: no cover - RETURNING always yields a row
        raise RuntimeError(f"event insert produced no id for paper {paper_id}")
    return int(row[0])


def get_events(conn: Connection, session_id: int, paper_id: int) -> list[InteractionEvent]:
    """This paper's full history in this session, oldest first."""
    rows = conn.execute(
        text(
            "SELECT id, session_id, paper_id, event_type, actor, created_at, payload"
            " FROM interaction_events"
            " WHERE session_id = :session_id AND paper_id = :paper_id"
            " ORDER BY id"
        ),
        {"session_id": session_id, "paper_id": paper_id},
    )
    return [
        InteractionEvent(
            id=row[0],
            session_id=row[1],
            paper_id=row[2],
            event_type=row[3],
            actor=row[4],
            created_at=row[5],
            payload=_loads(row[6]),
        )
        for row in rows
    ]


def latest_state(conn: Connection, session_id: int, paper_id: int) -> str | None:
    """
    The most recent event type, or None if nothing has happened.

    ORDER BY id, not created_at -- see the module docstring.
    """
    return conn.execute(
        text(
            "SELECT event_type FROM interaction_events"
            " WHERE session_id = :session_id AND paper_id = :paper_id"
            " ORDER BY id DESC LIMIT 1"
        ),
        {"session_id": session_id, "paper_id": paper_id},
    ).scalar()


def removed_among(conn: Connection, session_id: int, paper_ids: list[int]) -> set[int]:
    """
    Which of `paper_ids` are tombstoned in this session. Chunked.

    Same relationship to `removed_paper_ids` as `graph.nodes_present` has to
    `get_node_ids`: the unrestricted version scans every event row for the
    session through a correlated max(id), which is the right cost when the
    caller needs the whole exclusion set and badly wrong when it needs to
    annotate ten search results.
    """
    if not paper_ids:
        return set()
    tombstones = ",".join(f":t{i}" for i in range(len(TOMBSTONE_EVENTS)))
    found: set[int] = set()
    for start in range(0, len(paper_ids), 400):
        chunk = paper_ids[start : start + 400]
        placeholders = ",".join(f":p{i}" for i in range(len(chunk)))
        rows = conn.execute(
            text(
                "SELECT e.paper_id FROM interaction_events e"
                " WHERE e.session_id = :session_id"
                f"   AND e.paper_id IN ({placeholders})"
                "   AND e.id = ("
                "     SELECT MAX(inner_e.id) FROM interaction_events inner_e"
                "     WHERE inner_e.session_id = e.session_id AND inner_e.paper_id = e.paper_id"
                "   )"
                f"   AND e.event_type IN ({tombstones})"
            ),
            {
                "session_id": session_id,
                **{f"p{i}": v for i, v in enumerate(chunk)},
                **{f"t{i}": v for i, v in enumerate(TOMBSTONE_EVENTS)},
            },
        )
        found.update(row[0] for row in rows)
    return found


def user_held_paper_ids(conn: Connection, session_id: int) -> set[int]:
    """
    Papers whose most recent event the **user** authored.

    The sweep's exemption set. `graph_nodes.state` records what a paper is now;
    this records whether the user put it in that position, which is a different
    question and the one that decides whether an automatic process may take it
    away.

    Un-liking writes `UNLABELED`; restoring writes `RESTORED`. Both leave a
    paper an ordinary CANDIDATE that topology alone would collect, and
    collecting it would silently reverse the action that produced it. An
    expansion's candidates have no events at all, so they stay sweepable --
    which is the whole point of the sweep.

    Derived per paper via the same correlated max(id) as `removed_paper_ids`,
    for the same reason: nothing is stored, so the answer follows the log.
    """
    rows = conn.execute(
        text(
            "SELECT e.paper_id FROM interaction_events e"
            " WHERE e.session_id = :session_id"
            "   AND e.actor = 'USER'"
            "   AND e.id = ("
            "     SELECT MAX(inner_e.id) FROM interaction_events inner_e"
            "     WHERE inner_e.session_id = e.session_id AND inner_e.paper_id = e.paper_id"
            "   )"
        ),
        {"session_id": session_id},
    )
    return {row[0] for row in rows}


def removed_paper_ids(conn: Connection, session_id: int) -> set[int]:
    """
    Papers whose latest event is a tombstone.

    This is the exclusion set expansion must left-join against (PLAN.md section
    C: "Nothing silently re-enters the graph"). Computed per paper via a
    correlated max(id) rather than stored, so RESTORED lifts it automatically.
    """
    placeholders = ",".join(f":t{i}" for i in range(len(TOMBSTONE_EVENTS)))
    rows = conn.execute(
        text(
            "SELECT e.paper_id FROM interaction_events e"
            " WHERE e.session_id = :session_id"
            "   AND e.id = ("
            "     SELECT MAX(inner_e.id) FROM interaction_events inner_e"
            "     WHERE inner_e.session_id = e.session_id AND inner_e.paper_id = e.paper_id"
            "   )"
            f"   AND e.event_type IN ({placeholders})"
        ),
        {
            "session_id": session_id,
            **{f"t{i}": v for i, v in enumerate(TOMBSTONE_EVENTS)},
        },
    )
    return {row[0] for row in rows}


__all__ = [
    "TOMBSTONE_EVENTS",
    "append_event",
    "get_events",
    "latest_state",
    "removed_among",
    "removed_paper_ids",
    "user_held_paper_ids",
]
