import pytest

from app.services.fusion import RRF_K, reciprocal_rank_fusion


def test_scores_worked_by_hand() -> None:
    fused = dict(reciprocal_rank_fusion([[10, 20], [20, 30]], k=60))
    assert fused[10] == pytest.approx(1 / 61)
    assert fused[20] == pytest.approx(1 / 62 + 1 / 61)
    assert fused[30] == pytest.approx(1 / 62)


def test_paper_in_both_channels_outranks_single_channel_leader() -> None:
    fused = reciprocal_rank_fusion([[1, 2, 3], [9, 3]])
    assert [pid for pid, _ in fused][0] == 3


def test_embedding_only_paper_survives() -> None:
    """The channel's point: a paper the graph pool lacks still appears."""
    assert 99 in {pid for pid, _ in reciprocal_rank_fusion([[1, 2], [99]])}


def test_ties_break_on_paper_id() -> None:
    fused = reciprocal_rank_fusion([[5], [3]])
    assert [pid for pid, _ in fused] == [3, 5]


def test_input_order_of_rankings_is_irrelevant() -> None:
    a, b = [4, 1, 7], [7, 2, 4]
    assert reciprocal_rank_fusion([a, b]) == reciprocal_rank_fusion([b, a])


def test_duplicate_in_one_ranking_counts_once_at_best_position() -> None:
    assert reciprocal_rank_fusion([[1, 2, 1]], k=60) == reciprocal_rank_fusion([[1, 2]], k=60)


def test_limit_and_empty_inputs() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[]]) == []
    assert len(reciprocal_rank_fusion([[1, 2, 3]], limit=2)) == 2


def test_nonpositive_k_rejected() -> None:
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([[1]], k=0)


def test_default_k_is_published_constant() -> None:
    assert RRF_K == 60


# --- R6.13: fuse_pool -------------------------------------------------------

from app.services.candidates import PoolEntry  # noqa: E402
from app.services.fusion import fuse_pool  # noqa: E402


def _entry(pid: int) -> PoolEntry:
    return PoolEntry(
        paper_id=pid, anchor_overlap=1, citation_count=0, age_years=1.0, direction="BACKWARD"
    )


def test_fuse_pool_scores_worked_by_hand() -> None:
    # graph order 1,2,3 ; embedding order 3,2,1
    scored = [(_entry(1), 3.0), (_entry(2), 2.0), (_entry(3), 1.0)]
    out = {e.paper_id: s for e, s in fuse_pool(scored, [3, 2, 1])}
    assert out[1] == pytest.approx(1 / 61 + 1 / 63)
    assert out[2] == pytest.approx(2 / 62)
    assert out[3] == pytest.approx(1 / 63 + 1 / 61)


def test_fuse_pool_lets_the_embedding_overturn_a_graph_tie_at_the_top() -> None:
    scored = [(_entry(1), 5.0), (_entry(2), 4.0), (_entry(3), 3.0)]
    out = fuse_pool(scored, [2, 3, 1])
    best = max(out, key=lambda t: (t[1], -t[0].paper_id))
    assert best[0].paper_id == 2


def test_fuse_pool_never_adds_papers_and_keeps_input_order() -> None:
    scored = [(_entry(7), 1.0), (_entry(4), 2.0)]
    out = fuse_pool(scored, [99, 4, 7])  # 99 is not in the pool
    assert [e.paper_id for e, _ in out] == [7, 4]


def test_fuse_pool_is_a_no_op_below_the_coverage_floor() -> None:
    scored = [(_entry(i), float(i)) for i in range(1, 6)]
    assert fuse_pool(scored, [1, 2]) == scored  # 2/5 < 0.8


def test_fuse_pool_with_no_embeddings_or_empty_pool() -> None:
    scored = [(_entry(1), 1.0)]
    assert fuse_pool(scored, []) == scored
    assert fuse_pool([], [1, 2]) == []


def test_fuse_pool_is_deterministic_under_tied_prescores() -> None:
    a = [(_entry(2), 1.0), (_entry(1), 1.0)]
    b = list(reversed(a))
    sa = {e.paper_id: s for e, s in fuse_pool(a, [1, 2])}
    sb = {e.paper_id: s for e, s in fuse_pool(b, [1, 2])}
    assert sa == sb
