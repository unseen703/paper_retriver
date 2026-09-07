"""
Edge ingestion, and the boundary-paper rule (R1.9, PLAN.md section E4).

**The year floor is a graph-admission rule, not an ingestion rule.** This module
is where that distinction is enforced, and it is the single most load-bearing
"do nothing" in the codebase:

    papers row       stored, as a STUB
    edges rows       stored, both directions
    graph_nodes row  NEVER created
    bib-coupling     yes -- this is the entire point
    expanded from    no; a paper with no graph node can never be a frontier

Bibliographic coupling measures shared references. Two 2023 papers that both
cite Adam (2014) are coupled through it. Drop Adam from the corpus and that
coupling is invisible. The R1.7 checkpoint measured 15 of BERT's 50 references
as pre-2015 -- GloVe, word2vec, ImageNet -- so this is a third of the structural
evidence, concentrated on exactly the shared foundations that make two modern
papers related.

The same reasoning applies to *every* rejection, not just PRE_ERA. A cs.CV paper
both your seeds cite still couples them, so it too is stored with its edges and
denied a node. Rejection means "not recommendable", never "not evidence".

The failure mode if this is wrong is silence: everything still runs, and the
recommendations just quietly get worse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Connection

from app.config import FiltersConfig
from app.models import CrawlState, FilterDecision, Outcome, Paper
from app.repo import edges as edges_repo
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.services.categories import CategoryResolver
from app.services.filters.cascade import CascadeStats, run_cascade

logger = logging.getLogger(__name__)


@dataclass
class IngestStats:
    """
    What an ingestion pass actually did.

    `boundary` is the number worth watching: it should be a large fraction on
    backward expansion and near zero forward. If it ever reads zero on a
    backward pass, the year floor has started deleting evidence instead of
    withholding recommendations.
    """

    admitted: int = 0
    boundary: int = 0
    cascade: CascadeStats | None = None

    def __post_init__(self) -> None:
        if self.cascade is None:
            self.cascade = CascadeStats()


def ingest_neighbour(
    conn: Connection,
    session_id: int,
    anchor_id: int,
    neighbour: Paper,
    direction: str,
    cfg: FiltersConfig,
    as_of_year: int,
    stats: IngestStats | None = None,
    depth: int = 1,
    admit: bool = True,
    resolver: CategoryResolver | None = None,
) -> FilterDecision:
    """
    Store one fetched neighbour and its edge, then decide graph admission.

    Order matters: the paper and edge are written **before** the cascade runs,
    so a rejection cannot take the structural evidence down with it.

    `direction` is from the anchor's point of view -- BACKWARD means the anchor
    cites the neighbour, FORWARD means the neighbour cites the anchor.
    """
    stats = stats if stats is not None else IngestStats()

    # Resolve the arXiv category BEFORE filtering. It is the topic stage's
    # strongest signal and the only thing that can deny cs.CV; without it
    # every paper falls through to the weak s2_fields rungs and the deny
    # list never fires.
    if resolver is not None:
        (neighbour,) = resolver.enrich([neighbour])

    # STUB: title, year, id. No metadata call -- a paper that may never be
    # recommendable is not worth API budget (PLAN.md: "one row, no metadata
    # call"). upsert_stub never downgrades an existing richer record.
    neighbour_id = papers_repo.upsert_stub(
        conn, neighbour.s2_paper_id, neighbour.title, neighbour.year
    )

    if neighbour_id == anchor_id:
        # S2 really does return self-citations; the CHECK constraint would
        # reject the insert, so skip rather than raise.
        logger.info("SELF_CITATION_SKIPPED paper_id=%s", anchor_id)
        return run_cascade(
            conn, session_id, neighbour_id, neighbour, cfg, as_of_year, stats.cascade
        )

    if direction == "BACKWARD":
        edges_repo.upsert_edge(conn, anchor_id, neighbour_id, "BACKWARD")
    else:
        edges_repo.upsert_edge(conn, neighbour_id, anchor_id, "FORWARD")

    decision = run_cascade(
        conn, session_id, neighbour_id, neighbour, cfg, as_of_year, stats.cascade
    )

    if decision.outcome is Outcome.ACCEPT:
        # `admit=False` when a later stage owns admission. The expansion
        # (R1.11) ranks and budgets before adding nodes, so ingesting one
        # here would put every accepted paper in the graph regardless of
        # max_new.
        if admit:
            graph_repo.add_node(conn, session_id, neighbour_id, "CANDIDATE", depth=depth)
        stats.admitted += 1
        return decision

    # Rejected or quarantined: the row and the edge stay, the node does not.
    # This is the boundary-paper rule, and it applies to every non-accept
    # verdict rather than only to PRE_ERA.
    stats.boundary += 1
    logger.info(
        "BOUNDARY_PAPER paper_id=%s reason=%s crawl_state=%s",
        neighbour_id,
        decision.reason_code,
        CrawlState.STUB,
    )
    return decision


__all__ = ["IngestStats", "ingest_neighbour"]
