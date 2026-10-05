"""
Reciprocal rank fusion (R6): merge the graph pool and the embedding pool into
one ordering.

Pure -- no I/O, no DB, no network. RRF is rank-based on purpose: a prescore and
a cosine are on incomparable scales, and fusing ranks needs no calibration.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.services.candidates import PoolEntry

# Cormack et al. (2009). Larger k flattens the head; 60 is the published default.
RRF_K = 60


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[int]],
    *,
    k: int = RRF_K,
    limit: int | None = None,
) -> list[tuple[int, float]]:
    """
    Fuse best-first `rankings` of paper ids into ``[(paper_id, score)]``.

    A paper's score is ``sum(1 / (k + rank))`` over the rankings that contain
    it (rank is 1-based). A paper appearing twice in one ranking counts once, at
    its best position. Ties break on paper_id ascending, so the result never
    depends on input iteration order.
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    scores: dict[int, float] = {}
    for ranking in rankings:
        seen: set[int] = set()
        position = 0
        for paper_id in ranking:
            if paper_id in seen:
                continue
            seen.add(paper_id)
            position += 1
            scores[paper_id] = scores.get(paper_id, 0.0) + 1.0 / (k + position)
    fused = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return fused if limit is None else fused[:limit]


# Below this share of the pool having a vector, fusing would reward the papers
# that happen to be embedded rather than the ones that are similar. Untuned.
MIN_EMBEDDING_COVERAGE = 0.8


def fuse_pool(
    scored: Sequence[tuple[PoolEntry, float]],
    embedding_ranking: Sequence[int],
    *,
    min_coverage: float = MIN_EMBEDDING_COVERAGE,
) -> list[tuple[PoolEntry, float]]:
    """
    Re-score a graph pool by fusing its prescore ranking with an embedding
    ranking of the same papers (R6.13).

    `embedding_ranking` is best-first and may name papers outside the pool
    (ignored) or omit pool papers (they have no vector). Fusion only happens
    when at least `min_coverage` of the pool is ranked; otherwise `scored` is
    returned unchanged, so a half-filled embedding store cannot skew selection.

    This reorders the pool; it does not admit anything new. A paper reached
    only by embedding has no filter verdict, and admitting one is the cascade's
    job, not this function's.
    """
    if not scored:
        return []
    pool_ids = {entry.paper_id for entry, _ in scored}
    in_pool = [pid for pid in embedding_ranking if pid in pool_ids]
    if len(set(in_pool)) < min_coverage * len(pool_ids) or not in_pool:
        return list(scored)
    graph_ranking = [
        entry.paper_id for entry, _ in sorted(scored, key=lambda t: (-t[1], t[0].paper_id))
    ]
    fused = dict(reciprocal_rank_fusion([graph_ranking, in_pool]))
    return [(entry, fused[entry.paper_id]) for entry, _ in scored]


__all__ = ["MIN_EMBEDDING_COVERAGE", "RRF_K", "fuse_pool", "reciprocal_rank_fusion"]
