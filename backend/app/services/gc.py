"""
Mark-and-sweep garbage collection (R2.5).

PLAN.md calls this "the feature most likely to be misimplemented" and states
the rule once, precisely:

    After any removal, mark every node reachable from anchors = SEED u LIKED
    over the **undirected** projection, bounded by max_depth. Sweep every
    unmarked node whose state is CANDIDATE. Never sweep a LIKED or DISLIKED
    node.

Four details in that sentence each rule out a plausible implementation, and
each has a test:

**Undirected.** A candidate that *cites* a seed is reachable from it. Walking
directed edges outward from anchors would never find it, and would sweep a
paper whose entire reason for being in the graph is that it cites your seed.

**Reachability, not adjacency.** A chain of candidates ending at a seed is
justified along its whole length. Checking only an anchor's direct neighbours
sweeps the tail of every chain.

**Bounded by max_depth.** Without the bound, a candidate chain trailing off a
seed lives forever and the graph grows past its ceiling one hop at a time.

**Labelled nodes are never swept.** Your judgment outranks topology: a LIKED
node at degree zero stays, and so does a disconnected DISLIKED one -- the
latter still carries negative signal, and re-fetching it later would spend API
budget rediscovering something already judged.

Sweeping removes *graph membership*, never papers or edges. A swept paper
stays in the corpus doing structural work: it still couples the papers that
cite it. It also gets a `GC_SWEPT` event, which is a tombstone, so expansion
will not quietly bring it back.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import Connection, text

from app.repo import events as events_repo
from app.repo import graph as graph_repo

logger = logging.getLogger(__name__)

# PLAN.md: "max_depth=3 from the nearest anchor is a sane default."
DEFAULT_MAX_DEPTH = 3

#: States that anchor the graph. A candidate justifies its presence by being
#: reachable from one of these.
ANCHOR_STATES = ("SEED", "LIKED")


def _undirected_neighbours(conn: Connection, session_id: int) -> dict[int, set[int]]:
    """
    Adjacency over the undirected projection, restricted to drawn nodes.

    Only edges whose *both* endpoints have a node in this session count. An
    edge into a boundary paper is real and structurally useful, but it cannot
    justify a candidate's membership -- the boundary paper is not in the graph
    to be reached from.
    """
    rows = conn.execute(
        text(
            "SELECT e.citing_id, e.cited_id FROM edges e"
            " JOIN graph_nodes a ON a.paper_id = e.citing_id AND a.session_id = :sid"
            " JOIN graph_nodes b ON b.paper_id = e.cited_id  AND b.session_id = :sid"
        ),
        {"sid": session_id},
    )
    adjacency: dict[int, set[int]] = defaultdict(set)
    for citing, cited in rows:
        # Both directions: the projection is undirected.
        adjacency[citing].add(cited)
        adjacency[cited].add(citing)
    return adjacency


def gc_sweep(
    conn: Connection,
    session_id: int,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    dry_run: bool = False,
    protect: int | None = None,
) -> list[int]:
    """
    Remove candidates no longer justified by any anchor. Returns their ids.

    `protect` exempts one paper from the sweep, and exists for a case that is
    easy to get wrong: un-liking a paper makes it a CANDIDATE, and if it was
    the graph's only anchor it is then unreachable from any anchor -- including
    itself. Sweeping it would turn "unlike" into "delete", which is a different
    verb with a different audit trail, and the whole state machine exists to
    keep those apart. The user asked to stop favouring a paper, not to lose it.

    `dry_run` computes the same answer and writes nothing, so the confirmation
    dialog can promise exactly what the real call will do -- BUILD.md requires
    the two counts to match, and a dialog whose number differs from the outcome
    is worse than no dialog.

    The result is sorted, so two runs over the same graph agree byte for byte
    (CLAUDE.md rule 7 -- set iteration order is not a contract).
    """
    by_state = graph_repo.get_nodes_by_state(conn, session_id)
    anchors = {pid for state in ANCHOR_STATES for pid in by_state.get(state, ())}
    candidates = set(by_state.get("CANDIDATE", ()))

    adjacency = _undirected_neighbours(conn, session_id)

    # Breadth-first from every anchor at once, bounded by depth. Starting from
    # all anchors simultaneously is what makes "reachable from ANY anchor"
    # cheap -- one traversal rather than one per anchor.
    marked: set[int] = set(anchors)
    frontier: set[int] = set(anchors)
    depth = 0
    while frontier and depth < max_depth:
        nxt = {n for node in frontier for n in adjacency.get(node, ())} - marked
        marked |= nxt
        frontier = nxt
        depth += 1

    orphans = sorted(candidates - marked - ({protect} if protect is not None else set()))

    if dry_run or not orphans:
        return orphans

    for paper_id in orphans:
        graph_repo.remove_node(conn, session_id, paper_id)
        # actor=SYSTEM distinguishes a sweep from a removal the user asked for.
        # R2.14's drawer groups by that, and it is what tells you whether a
        # paper left because you removed it or because it lost its
        # justification.
        events_repo.append_event(conn, session_id, paper_id, "GC_SWEPT", actor="SYSTEM")

    logger.info(
        "GC_SWEPT session=%s count=%s anchors=%s max_depth=%s",
        session_id,
        len(orphans),
        len(anchors),
        max_depth,
    )
    return orphans


__all__ = ["ANCHOR_STATES", "DEFAULT_MAX_DEPTH", "gc_sweep"]
