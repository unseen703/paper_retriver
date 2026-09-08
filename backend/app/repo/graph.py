"""
The `graph_nodes` table -- the visible working set. **Session-scoped.**

`session_id` is the first positional argument on every function, with no
default. BUILD.md's reasoning: "if you can call it without a session, you will
eventually call it with the wrong one." The failure mode is silent cross-session
leakage, which nothing later surfaces, so the boundary is enforced by the
signature rather than by discipline.

A paper can exist in `papers` without appearing here -- rejected papers, removed
papers, and stubs discovered through nested edge fetches all live in the corpus.
Only `graph_nodes` rows are drawn.

`state` is a materialized projection of the paper's event log. Write both in one
transaction; `scripts/rebuild_state.py` (R2.3) reconstructs this table from
`interaction_events` alone and is the safety net if they ever disagree.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from app.models import GraphNode

_COLUMNS = (
    "session_id, paper_id, state, score, features, score_breakdown, depth,"
    " added_by, pos_x, pos_y, community_id"
)


def _loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _row_to_node(row: Any) -> GraphNode:
    return GraphNode(
        session_id=row[0],
        paper_id=row[1],
        state=row[2],
        score=row[3],
        features=_loads(row[4]),
        score_breakdown=_loads(row[5]),
        depth=row[6],
        added_by=row[7],
        pos_x=row[8],
        pos_y=row[9],
        community_id=row[10],
    )


def add_node(
    conn: Connection,
    session_id: int,
    paper_id: int,
    state: str,
    depth: int,
    score: float | None = None,
    features: dict[str, Any] | None = None,
    score_breakdown: dict[str, Any] | None = None,
    added_by: int | None = None,
) -> None:
    """
    Insert or update this paper's node in this session's graph.

    Idempotent: a candidate re-encountered through a second path updates rather
    than duplicating. Layout position and community are deliberately preserved
    -- an expansion must not scramble a graph the user has already arranged
    (R2.12 is explicit that existing nodes must not move).
    """
    conn.execute(
        text(
            "INSERT INTO graph_nodes (session_id, paper_id, state, score, features,"
            " score_breakdown, depth, added_by)"
            " VALUES (:session_id, :paper_id, :state, :score, :features,"
            " :score_breakdown, :depth, :added_by)"
            " ON CONFLICT (session_id, paper_id) DO UPDATE SET"
            "   state = excluded.state,"
            "   score = COALESCE(excluded.score, graph_nodes.score),"
            "   features = COALESCE(excluded.features, graph_nodes.features),"
            "   score_breakdown ="
            "     COALESCE(excluded.score_breakdown, graph_nodes.score_breakdown),"
            "   depth = excluded.depth,"
            "   added_by = COALESCE(excluded.added_by, graph_nodes.added_by)"
            # pos_x, pos_y and community_id are absent on purpose: they are the
            # user's arrangement, not the expander's to overwrite.
        ),
        {
            "session_id": session_id,
            "paper_id": paper_id,
            "state": state,
            "score": score,
            "features": json.dumps(features) if features else None,
            "score_breakdown": json.dumps(score_breakdown) if score_breakdown else None,
            "depth": depth,
            "added_by": added_by,
        },
    )


def get_nodes(
    conn: Connection, session_id: int, states: list[str] | None = None
) -> list[GraphNode]:
    """
    This session's nodes, ordered by paper_id.

    `states=None` means no filter; `states=[]` means none of them, which is not
    the same thing and is the reading a caller passing a computed list expects.
    Ordering is explicit because CLAUDE.md rule 7 forbids relying on insertion
    order.
    """
    if states is not None and not states:
        return []

    sql = f"SELECT {_COLUMNS} FROM graph_nodes WHERE session_id = :session_id"
    params: dict[str, Any] = {"session_id": session_id}
    if states:
        placeholders = ",".join(f":s{i}" for i in range(len(states)))
        sql += f" AND state IN ({placeholders})"
        params.update({f"s{i}": v for i, v in enumerate(states)})
    sql += " ORDER BY paper_id"

    return [_row_to_node(row) for row in conn.execute(text(sql), params)]


def get_node_ids(conn: Connection, session_id: int) -> set[int]:
    """Paper ids currently in this session's graph. Used by the exclusion pass."""
    rows = conn.execute(
        text("SELECT paper_id FROM graph_nodes WHERE session_id = :session_id"),
        {"session_id": session_id},
    )
    return {row[0] for row in rows}


def get_node(conn: Connection, session_id: int, paper_id: int) -> GraphNode | None:
    """
    One node, or None if this paper has no node in this session.

    None is a meaningful answer rather than an error: a boundary paper is in
    the corpus and not in the graph, and the caller reports that as a 404
    rather than inventing null depth and score for it.
    """
    row = conn.execute(
        text(
            f"SELECT {_COLUMNS} FROM graph_nodes"
            " WHERE session_id = :session_id AND paper_id = :paper_id"
        ),
        {"session_id": session_id, "paper_id": paper_id},
    ).fetchone()
    return _row_to_node(row) if row is not None else None


def get_nodes_by_state(conn: Connection, session_id: int) -> dict[str, set[int]]:
    """
    state -> the paper ids in it, for this session. One query.

    The sweep needs anchors and candidates together and would otherwise ask
    twice for overlapping data; grouping once also keeps the two answers
    consistent with each other, which matters when the thing being computed is
    "which of these is not reachable from those".
    """
    rows = conn.execute(
        text("SELECT state, paper_id FROM graph_nodes WHERE session_id = :session_id"),
        {"session_id": session_id},
    )
    grouped: dict[str, set[int]] = {}
    for state, paper_id in rows:
        grouped.setdefault(state, set()).add(paper_id)
    return grouped


def node_ids_added_by(conn: Connection, session_id: int, expansion_id: int) -> list[int]:
    """
    The papers one expansion admitted, sorted.

    R2.4's poll needs this after the fact: the worker thread that computed the
    list is not the thread answering the request, and a restart between the two
    must not lose the answer. `added_by` is written by the same transaction
    that admitted the nodes, so reading it back cannot disagree with the graph
    -- and a node removed since then is correctly absent, because it is.
    """
    rows = conn.execute(
        text(
            "SELECT paper_id FROM graph_nodes"
            " WHERE session_id = :session_id AND added_by = :expansion_id"
            " ORDER BY paper_id"
        ),
        {"session_id": session_id, "expansion_id": expansion_id},
    )
    return [int(row[0]) for row in rows]


def nodes_present(conn: Connection, session_id: int, paper_ids: list[int]) -> set[int]:
    """
    Which of `paper_ids` have a node in this session. Chunked.

    `get_node_ids` returns the whole session, which is the right shape for the
    exclusion pass (it needs every id anyway) and the wrong shape for
    annotating a handful of search hits -- that pulled two thousand rows to
    answer a question about ten papers, on every keystroke of a search-as-you-
    type box.
    """
    if not paper_ids:
        return set()
    found: set[int] = set()
    for start in range(0, len(paper_ids), 400):
        chunk = paper_ids[start : start + 400]
        placeholders = ",".join(f":p{i}" for i in range(len(chunk)))
        rows = conn.execute(
            text(
                "SELECT paper_id FROM graph_nodes"
                f" WHERE session_id = :session_id AND paper_id IN ({placeholders})"
            ),
            {"session_id": session_id, **{f"p{i}": v for i, v in enumerate(chunk)}},
        )
        found.update(row[0] for row in rows)
    return found


def remove_node(conn: Connection, session_id: int, paper_id: int) -> None:
    """
    Drop the node from this session's graph. Idempotent.

    The `papers` row and its edges survive: PLAN.md section C is explicit that a
    paper can exist in the corpus without being in any graph, and deleting it
    here would take the citation edges with it.

    This does NOT write the REMOVED event -- callers write both in one
    transaction, so the projection and the log cannot diverge.
    """
    conn.execute(
        text("DELETE FROM graph_nodes WHERE session_id = :session_id AND paper_id = :paper_id"),
        {"session_id": session_id, "paper_id": paper_id},
    )


def set_position(
    conn: Connection, session_id: int, paper_id: int, pos_x: float, pos_y: float
) -> None:
    """Persist a laid-out position (R2.12). Separate from add_node by design."""
    conn.execute(
        text(
            "UPDATE graph_nodes SET pos_x = :pos_x, pos_y = :pos_y"
            " WHERE session_id = :session_id AND paper_id = :paper_id"
        ),
        {"session_id": session_id, "paper_id": paper_id, "pos_x": pos_x, "pos_y": pos_y},
    )


def count_by_state(conn: Connection, session_id: int) -> dict[str, int]:
    """Per-state counts for the stats panel (R2.13)."""
    rows = conn.execute(
        text(
            "SELECT state, COUNT(*) FROM graph_nodes WHERE session_id = :session_id"
            " GROUP BY state ORDER BY state"
        ),
        {"session_id": session_id},
    )
    return {row[0]: row[1] for row in rows}


__all__ = [
    "add_node",
    "count_by_state",
    "get_node",
    "get_node_ids",
    "get_nodes",
    "get_nodes_by_state",
    "node_ids_added_by",
    "nodes_present",
    "remove_node",
    "set_position",
]
