"""
Reciprocal rank fusion (R6): merge the graph pool and the embedding pool into
one ordering.

Pure -- no I/O, no DB, no network. RRF is rank-based on purpose: a prescore and
a cosine are on incomparable scales, and fusing ranks needs no calibration.
"""

from __future__ import annotations

from collections.abc import Sequence

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


__all__ = ["RRF_K", "reciprocal_rank_fusion"]
