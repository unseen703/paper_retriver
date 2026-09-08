"""
Applying a label change (R2.2).

The service exists so the endpoint stays a shell: validation lives in the
state-machine table, persistence lives in the repos, and this is the thing
that puts them in one transaction.

**Both writes commit together or neither does.** `graph_nodes.state` is a
materialised projection of `interaction_events`, and `scripts/rebuild_state.py`
(R2.3) reconstructs the former from the latter. If they can diverge -- an
event with no state change, or a state change with no event -- the rebuild
silently disagrees with the live table and nothing tells you which is right.
That is why this is one `engine.begin()` rather than two repo calls the caller
sequences.

**Un-liking sweeps, in the same transaction.** A liked node is an anchor, and
candidates justify their presence by reachability from one. Removing that
anchor can orphan everything hanging off it, so the state machine's `SWEEP`
effect runs here -- after the state write, so the sweep sees the new state, and
inside the same transaction, so a crash between the two cannot leave orphans
that nothing later knows to collect.

**A no-op writes nothing.** Relabelling to the state a paper already has is
allowed, because clicking "like" on an already-liked paper is not a mistake,
but it must not append an event: an append-only log with a row per double
click is a log that lies about what the user did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Engine

from app.models import GraphNode
from app.repo import events as events_repo
from app.repo import graph as graph_repo
from app.services.gc import gc_sweep
from app.services.transitions import TransitionError, plan_transition

logger = logging.getLogger(__name__)


class NodeNotInSession(RuntimeError):
    """
    This paper has no node in this session.

    Distinct from a refused transition: a boundary paper is in the corpus with
    all its edges and deliberately not in the graph, so it has no state to
    change. The caller reports 404, not 409.
    """

    def __init__(self, session_id: int, paper_id: int) -> None:
        super().__init__(f"no node {paper_id} in session {session_id}")
        self.session_id = session_id
        self.paper_id = paper_id


@dataclass(frozen=True, slots=True)
class LabelResult:
    node: GraphNode
    #: Papers the sweep collected as a consequence of this change, sorted.
    #: Non-empty only when the transition cost the graph an anchor.
    swept: tuple[int, ...] = ()
    rescored_count: int = 0


def apply_label(engine: Engine, session_id: int, paper_id: int, target: str) -> LabelResult:
    """
    Move one node to `target`, or raise.

    Raises `NodeNotInSession` (404) or `TransitionError` (409) -- the two
    refusals mean different things and the caller maps them to different codes.
    """
    with engine.begin() as conn:
        node = graph_repo.get_node(conn, session_id, paper_id)
        if node is None:
            raise NodeNotInSession(session_id, paper_id)

        # Raises TransitionError with the rule's own code. Validation happens
        # inside the transaction so a concurrent change cannot slip between
        # the check and the write.
        rule = plan_transition(node.state, target)

        if node.state == target:
            # Allowed and deliberately inert. Returning early keeps the log
            # honest: no event, no state write, nothing to undo.
            return LabelResult(node=node)

        for effect in rule.side_effects:
            if effect == "SWEEP":
                continue  # Runs after the state write -- see below.
            events_repo.append_event(conn, session_id, paper_id, effect)

        graph_repo.add_node(
            conn,
            session_id,
            paper_id,
            target,
            depth=node.depth,
            score=node.score,
            added_by=node.added_by,
        )
        # The sweep runs INSIDE the same transaction, and after the state
        # write. Both orderings matter: it must see the new state, because the
        # node that just stopped being an anchor is the whole reason to sweep;
        # and it must commit atomically with the label change, or a crash
        # between them leaves the graph holding orphans that no later
        # operation knows to collect.
        swept: tuple[int, ...] = ()
        if "SWEEP" in rule.side_effects:
            # `protect=paper_id`: the node being relabelled is never collected
            # by the sweep its own relabelling triggered. Un-liking the graph's
            # only anchor leaves that paper a candidate reachable from nothing,
            # and sweeping it would silently convert an unlike into a delete.
            swept = tuple(gc_sweep(conn, session_id, protect=paper_id))

        updated = graph_repo.get_node(conn, session_id, paper_id)
        assert updated is not None  # written one statement ago, in this transaction

    if swept:
        logger.info(
            "label_swept session=%s paper_id=%s from=%s to=%s swept=%s",
            session_id,
            paper_id,
            node.state,
            target,
            len(swept),
        )

    logger.info(
        "label_changed session=%s paper_id=%s %s -> %s", session_id, paper_id, node.state, target
    )
    # rescored_count is 0 until R3: no feature set exists to rescore against,
    # and reporting a fabricated number would be worse than reporting none.
    return LabelResult(node=updated, swept=swept, rescored_count=0)


__all__ = ["LabelResult", "NodeNotInSession", "TransitionError", "apply_label"]
