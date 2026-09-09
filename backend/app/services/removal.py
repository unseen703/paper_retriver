"""
Removal and restore (R2.6, R2.8).

Removal takes **graph membership**, never papers or edges. A removed paper
stays in the corpus with everything it was connected by, because it is still
evidence: two papers that both cite it are still coupled through it, and
deleting it would make that relationship invisible. This is the same
boundary-paper reasoning as `ingest.py`, applied to a decision the user made
rather than one a filter made.

**The dry run must predict the real outcome exactly.** PLAN.md marks it
"always call first", and BUILD.md requires the counts to match. A confirmation
dialog whose number differs from what happens teaches you to distrust the tool
at the one moment you most need to trust it -- so both paths run the *same*
computation, and the only difference is whether anything is written.

**Removed and swept are reported separately.** Two different things happened:
you removed one paper, and the system collected others as a consequence.
Merging them into one list hides which was your decision, and R2.14's review
drawer needs the distinction to group by.

**Restore is the only way back, and never automatic.** A tombstone is derived
from the latest event rather than stored as a flag, which is exactly what lets
`RESTORED` lift it with no schema change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import Engine

from app.repo import events as events_repo
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.services.gc import gc_sweep

logger = logging.getLogger(__name__)


class NodeNotInSession(RuntimeError):
    """No node for this paper in this session -- nothing to remove or restore."""

    def __init__(self, session_id: int, paper_id: int) -> None:
        super().__init__(f"no node {paper_id} in session {session_id}")
        self.session_id = session_id
        self.paper_id = paper_id


class NotRemoved(RuntimeError):
    """
    Asked to restore a paper that was never removed.

    Succeeding silently would make "restore" sound like it did something when
    the paper had not gone anywhere.
    """

    def __init__(self, session_id: int, paper_id: int) -> None:
        super().__init__(f"paper {paper_id} is not removed in session {session_id}")
        self.session_id = session_id
        self.paper_id = paper_id


@dataclass(frozen=True, slots=True)
class RemovalPlan:
    """What a removal would do, or did."""

    removed: tuple[int, ...] = ()
    gc_swept: tuple[int, ...] = ()
    #: (paper_id, title) for everything above, so a dialog can name the papers
    #: rather than only count them -- "this will remove 7 papers" is not
    #: something anyone can consent to.
    titles: dict[int, str] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.removed) + len(self.gc_swept)


def remove_node(
    engine: Engine, session_id: int, paper_id: int, *, dry_run: bool = True
) -> RemovalPlan:
    """
    Remove one node and collect whatever it was the only justification for.

    `dry_run` defaults to True. PLAN.md says to call it first, and the safe
    reading of an omitted parameter is the one that does not destroy anything.
    """
    with engine.begin() as conn:
        if graph_repo.get_node(conn, session_id, paper_id) is None:
            raise NotRemovedOrMissing(session_id, paper_id)

        if dry_run:
            # Predict by simulating: take the node out inside a transaction
            # that is then rolled back, so the sweep sees exactly the graph the
            # real call would. Computing it any other way would be a second
            # implementation of the rule, free to disagree with the first.
            graph_repo.remove_node(conn, session_id, paper_id)
            swept = tuple(gc_sweep(conn, session_id, dry_run=True))
            titles = _titles(conn, [paper_id, *swept])
            conn.rollback()
            return RemovalPlan(removed=(paper_id,), gc_swept=swept, titles=titles)

        graph_repo.remove_node(conn, session_id, paper_id)
        events_repo.append_event(conn, session_id, paper_id, "REMOVED", actor="USER")
        swept = tuple(gc_sweep(conn, session_id))
        titles = _titles(conn, [paper_id, *swept])

    logger.info("node_removed session=%s paper_id=%s swept=%s", session_id, paper_id, len(swept))
    return RemovalPlan(removed=(paper_id,), gc_swept=swept, titles=titles)


def restore_node(engine: Engine, session_id: int, paper_id: int) -> int:
    """
    Bring a tombstoned paper back as a CANDIDATE. Returns its id.

    Never automatic -- PLAN.md is explicit that this is the only path back, and
    that expansion must not rediscover a paper you removed on purpose.

    It returns as CANDIDATE rather than to whatever it was: the label it
    carried was part of a judgment you then reversed by removing it, and
    quietly restoring that label would put words in your mouth.
    """
    with engine.begin() as conn:
        if papers_repo.get_papers_by_ids(conn, [paper_id]) == []:
            raise NodeNotInSession(session_id, paper_id)
        if paper_id not in events_repo.removed_paper_ids(conn, session_id):
            raise NotRemoved(session_id, paper_id)

        events_repo.append_event(conn, session_id, paper_id, "RESTORED", actor="USER")
        graph_repo.add_node(conn, session_id, paper_id, "CANDIDATE", depth=1)

    logger.info("node_restored session=%s paper_id=%s", session_id, paper_id)
    return paper_id


def _titles(conn: object, paper_ids: list[int]) -> dict[int, str]:
    from sqlalchemy import Connection

    assert isinstance(conn, Connection)
    return {
        p.id: p.title for p in papers_repo.get_papers_by_ids(conn, paper_ids) if p.id is not None
    }


# Kept as an alias so the two "nothing here" failures read distinctly at the
# call site while sharing one type for the 404 mapping.
NotRemovedOrMissing = NodeNotInSession


__all__ = [
    "NodeNotInSession",
    "NotRemoved",
    "RemovalPlan",
    "remove_node",
    "restore_node",
]
