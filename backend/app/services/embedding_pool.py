"""
Embedding-only candidates (R6.18): papers the citation graph did not reach.

R6's justification is concurrent work with no citation path to the anchors.
`embeddings.embedding_candidates` finds such papers, and `fuse_pool` only
reorders the graph pool, so until this module nothing could admit one.

A paper reached only by embedding has never been through the cascade, so it
must pass it here. That is the whole reason this lives beside `ingest` rather
than in `fusion`: the cascade writes its verdict (`filter_decisions`), and a
paper it refuses stays refused exactly like a graph-reached one.

No edge is written -- there is none. Such an entry has `anchor_overlap=0` and no
`source_ids`, so the per-source cap never counts it, and its direction is
``EMBEDDING`` so neither BACKWARD nor FORWARD floor reserves slots for it.
"""

from __future__ import annotations

from sqlalchemy import Connection

from app.config import FiltersConfig
from app.models import Outcome
from app.repo import events as events_repo
from app.repo import filter_decisions as decisions_repo
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.services.candidates import PoolEntry, _age_years
from app.services.categories import CategoryResolver
from app.services.embeddings import EmbeddingStore, embedding_candidates
from app.services.exemplars import AppliedStage
from app.services.filters.cascade import run_cascade

# Nearest-neighbour depth asked of the channel. Untuned; the cascade and the
# budget decide how many of these are actually admitted.
EMBEDDING_POOL_K = 25

EMBEDDING_DIRECTION = "EMBEDDING"


def embedding_only_entries(
    conn: Connection,
    session_id: int,
    store: EmbeddingStore,
    anchors: list[int],
    graph_pool_ids: set[int],
    cfg: FiltersConfig,
    as_of_year: int,
    *,
    resolver: CategoryResolver | None = None,
    applied: AppliedStage | None = None,
    k: int = EMBEDDING_POOL_K,
) -> list[PoolEntry]:
    """
    Pool entries for the anchors' nearest embedding neighbours that the graph
    pool lacks and the cascade accepts, sorted by `paper_id`.

    Excluded before the cascade runs, with the same three clauses as
    `build_pool`: already in this session's graph, tombstoned here, or refused
    under the current config. Empty when no anchor has a vector.
    """
    excluded = (
        set(anchors)
        | graph_pool_ids
        | graph_repo.get_node_ids(conn, session_id)
        | events_repo.removed_paper_ids(conn, session_id)
        | decisions_repo.non_accepted_paper_ids(conn, session_id, cfg.config_version)
    )
    neighbours = embedding_candidates(store, anchors, k, exclude=sorted(excluded))
    if not neighbours:
        return []

    ids = sorted(pid for pid, _ in neighbours)
    papers = [p for p in papers_repo.get_papers_by_ids(conn, ids) if p.id is not None]
    if resolver is not None:
        papers = resolver.enrich(papers)

    entries: list[PoolEntry] = []
    for paper in sorted(papers, key=lambda p: p.id or 0):
        assert paper.id is not None
        decision = run_cascade(conn, session_id, paper.id, paper, cfg, as_of_year, applied=applied)
        if decision.outcome is not Outcome.ACCEPT:
            continue
        entries.append(
            PoolEntry(
                paper_id=paper.id,
                anchor_overlap=0,
                citation_count=paper.citation_count,
                age_years=_age_years(paper.year, as_of_year),
                direction=EMBEDDING_DIRECTION,
            )
        )
    return entries


__all__ = ["EMBEDDING_DIRECTION", "EMBEDDING_POOL_K", "embedding_only_entries"]
