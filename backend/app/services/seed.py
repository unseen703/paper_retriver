"""
Seeding: how a paper enters a graph deliberately rather than by expansion.

    fetch metadata -> dedup -> cascade -> upsert -> add SEED node -> log event

**Seeds may bypass the filters, but never silently.** `force=True` admits a
paper the cascade rejected, and the rejection is still written to
`filter_decisions` and recorded in the event payload. BUILD.md: "you want to
know you overrode it." A silent override is how a corpus fills with papers
nobody remembers admitting, and R2.14's drawer is where that has to stay
visible.

**Rejection errors carry their `reason_code`.** "This paper was rejected" is not
an actionable message; "CAT_PRIMARY_APPLIED" tells you it was a primary-cs.CV
paper and that `--force` is the answer if you meant it.

The event is not optional bookkeeping. `graph_nodes.state` is a materialized
projection of the event log, and R2.3 reconstructs it from events alone -- a
seed written without its `SEED_ADDED` event would disappear on the next rebuild.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine

from app.clients.s2 import S2Client
from app.config import FiltersConfig
from app.models import GraphNode, Outcome
from app.repo import events as events_repo
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.services.categories import CategoryResolver
from app.services.filters.cascade import run_cascade

logger = logging.getLogger(__name__)


class SeedError(RuntimeError):
    """Base for every reason a seed could not be added."""


class AlreadyPresent(SeedError):
    """This paper is already in this session's graph."""

    def __init__(self, paper_id: int) -> None:
        super().__init__(f"paper {paper_id} is already in this session")
        self.paper_id = paper_id


class SeedNotFound(SeedError):
    """
    S2 has never heard of this id.

    Distinct from `SeedRejected` because the answers differ: a rejected paper
    can be forced, an unknown one cannot. Collapsing them would make the API
    offer an override that could not possibly work, and would force callers to
    string-match a `reason_code` to tell a 404 from a 422.
    """

    def __init__(self, s2_paper_id: str) -> None:
        super().__init__(f"Semantic Scholar has no paper {s2_paper_id!r}")
        self.s2_paper_id = s2_paper_id


class SeedRejected(SeedError):
    """
    The cascade rejected the paper and `force` was not set.

    Carries `reason_code` so the caller can say *why*, and so a UI can offer
    "add anyway" against a specific, nameable rule.
    """

    def __init__(self, reason_code: str, stage: str) -> None:
        super().__init__(f"seed rejected at stage {stage}: {reason_code}")
        self.reason_code = reason_code
        self.stage = stage


async def add_seed(
    engine: Engine,
    client: S2Client,
    session_id: int,
    s2_paper_id: str,
    cfg: FiltersConfig,
    as_of_year: int,
    force: bool = False,
) -> GraphNode:
    """Add `s2_paper_id` to `session_id` as a SEED at depth 0."""
    # A seed is worth a full metadata call -- unlike a boundary paper, it will
    # be expanded from and displayed. get_papers is the only metadata path, so
    # a paper already fetched in another session costs nothing here.
    papers = await client.get_papers([s2_paper_id])
    if not papers:
        raise SeedNotFound(s2_paper_id)
    # Resolve the arXiv category before the cascade sees the paper -- the
    # topic stage's primary-category rung is what denies cs.CV, and it
    # cannot fire on an unenriched record.
    (paper,) = CategoryResolver(engine).enrich([papers[0]])

    # Nothing raises inside this block. Raising here would roll the transaction
    # back, and the two rows written before the failure are exactly the ones
    # that must survive it:
    #
    #   papers            a rejected paper is still evidence. It has references
    #                     the graph's papers share, so it still couples them --
    #                     the same boundary-paper reasoning as `ingest.py`, and
    #                     PLAN.md is explicit that rejection means "not
    #                     recommendable", never "not stored".
    #   filter_decisions  the verdict is meant to be cached forever. Discarding
    #                     it means re-running the cascade on every retry of the
    #                     same paper, and losing the audit trail of what was
    #                     refused -- which is what R2.14's review drawer reads.
    #
    # So the decision is *made* inside the transaction and *reported* after it
    # commits.
    node: GraphNode | None = None
    already_present = False

    with engine.begin() as conn:
        paper_id = papers_repo.upsert_paper(conn, paper)
        if paper.authors:
            papers_repo.set_authors(conn, paper_id, list(paper.authors))

        already_present = paper_id in graph_repo.get_node_ids(conn, session_id)

        # Run the cascade even when forcing: the verdict is the record of what
        # was overridden.
        decision = run_cascade(conn, session_id, paper_id, paper, cfg, as_of_year)
        overridden = decision.outcome is not Outcome.ACCEPT
        admit = not already_present and (not overridden or force)

        if admit:
            if overridden:
                logger.info(
                    "SEED_FORCED paper_id=%s reason=%s stage=%s",
                    paper_id,
                    decision.reason_code,
                    decision.stage,
                )
            graph_repo.add_node(conn, session_id, paper_id, "SEED", depth=0)
            payload: dict[str, object] = {"s2_paper_id": s2_paper_id}
            if overridden:
                # `forced` is set only when an override actually happened, not
                # merely when force was permitted.
                payload["forced"] = True
                payload["reason_code"] = decision.reason_code
            events_repo.append_event(conn, session_id, paper_id, "SEED_ADDED", payload=payload)
            (node,) = [n for n in graph_repo.get_nodes(conn, session_id) if n.paper_id == paper_id]

    if already_present:
        raise AlreadyPresent(paper_id)
    if node is None:
        raise SeedRejected(decision.reason_code, str(decision.stage))
    return node


__all__ = ["AlreadyPresent", "SeedError", "SeedNotFound", "SeedRejected", "add_seed"]
