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
    #: Effects the table asked for that the caller has not run yet. `SWEEP`
    #: appears here until R2.5 implements mark-and-sweep; it is surfaced rather
    #: than dropped so the gap is visible in logs instead of silent.
    pending_effects: tuple[str, ...] = ()
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
                continue  # R2.5 owns this; reported via pending_effects.
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
        updated = graph_repo.get_node(conn, session_id, paper_id)
        assert updated is not None  # written one statement ago, in this transaction

    pending = tuple(e for e in rule.side_effects if e == "SWEEP")
    if pending:
        # Loud rather than silent: leaving LIKED can orphan its dependents, and
        # until R2.5 lands nothing collects them.
        logger.info(
            "SWEEP_PENDING session=%s paper_id=%s from=%s to=%s (R2.5 not implemented)",
            session_id,
            paper_id,
            node.state,
            target,
        )

    logger.info(
        "label_changed session=%s paper_id=%s %s -> %s", session_id, paper_id, node.state, target
    )
    # rescored_count is 0 until R3: no feature set exists to rescore against,
    # and reporting a fabricated number would be worse than reporting none.
    return LabelResult(node=updated, pending_effects=pending, rescored_count=0)


__all__ = ["LabelResult", "NodeNotInSession", "TransitionError", "apply_label"]
