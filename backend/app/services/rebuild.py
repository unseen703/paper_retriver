"""
Reconstructing `graph_nodes.state` from `interaction_events` (R2.3).

`graph_nodes.state` is a materialised projection; the event log is the source
of truth. That is a claim worth nothing unless something can actually perform
the reconstruction and prove the two agree -- BUILD.md calls this "your
event-log safety net", and it is the thing that makes the claim checkable.

The derivation is deliberately small, because a complicated one would need its
own safety net:

    SEED_ADDED            -> SEED
    LIKED / DISLIKED      -> that state
    UNLABELED             -> CANDIDATE
    REMOVED / GC_SWEPT    -> no state at all; the paper is out of the graph
    RESTORED              -> CANDIDATE

**A tombstoned paper is absent from the result, not set to some state.** Giving
it one would resurrect it on the next rebuild, which is precisely what
tombstones exist to prevent.

**A node with no events keeps whatever it has.** An expansion writes CANDIDATE
rows without an interaction event -- the user has not done anything to those
papers. Treating "no events" as "no state" would delete most of the graph on
the first run.
"""

from __future__ import annotations

import logging

from sqlalchemy import Connection, Engine, text

logger = logging.getLogger(__name__)

# Event type -> the state it implies. Absent from this map means the event says
# nothing about graph state (an expansion record, say) and is ignored.
_STATE_FOR = {
    "SEED_ADDED": "SEED",
    "LIKED": "LIKED",
    "DISLIKED": "DISLIKED",
    "UNLABELED": "CANDIDATE",
    "RESTORED": "CANDIDATE",
}

# Events that take a paper out of the graph entirely.
_TOMBSTONES = ("REMOVED", "GC_SWEPT")


def states_from_events(conn: Connection, session_id: int) -> dict[int, str]:
    """
    Derive each paper's graph state in this session from its event log alone.

    Papers whose latest meaningful event is a tombstone are omitted rather than
    given a state -- they are out of the graph, and the distinction between
    "absent" and "present with some state" is the whole tombstone mechanism.

    Ordered by `id`, not `created_at`: timestamps are written by the
    application and two events in the same transaction can share one to the
    microsecond, while the primary key is monotonic by construction.
    """
    rows = conn.execute(
        text(
            "SELECT paper_id, event_type FROM interaction_events"
            " WHERE session_id = :sid ORDER BY id"
        ),
        {"sid": session_id},
    )

    derived: dict[int, str] = {}
    for paper_id, event_type in rows:
        if event_type in _TOMBSTONES:
            derived.pop(paper_id, None)
            continue
        state = _STATE_FOR.get(event_type)
        if state is not None:
            derived[paper_id] = state
    return derived


def rebuild_states(engine: Engine, session_id: int) -> int:
    """
    Make `graph_nodes.state` agree with the log. Returns how many rows changed.

    A return of 0 is the healthy answer and the reason this is worth running
    on a schedule: it is cheap, and the first non-zero result is the first
    evidence that a write path forgot its event.

    Nodes with no events are left exactly as they are -- see the module
    docstring on why that is not the same as having no state.
    """
    changed = 0
    with engine.begin() as conn:
        derived = states_from_events(conn, session_id)
        current = {
            row[0]: row[1]
            for row in conn.execute(
                text("SELECT paper_id, state FROM graph_nodes WHERE session_id = :sid"),
                {"sid": session_id},
            )
        }
        for paper_id, state in derived.items():
            if paper_id not in current or current[paper_id] == state:
                continue
            logger.warning(
                "REBUILD_MISMATCH session=%s paper_id=%s materialised=%s log=%s",
                session_id,
                paper_id,
                current[paper_id],
                state,
            )
            conn.execute(
                text(
                    "UPDATE graph_nodes SET state = :st WHERE session_id = :sid AND paper_id = :pid"
                ),
                {"st": state, "sid": session_id, "pid": paper_id},
            )
            changed += 1
    return changed


__all__ = ["rebuild_states", "states_from_events"]
