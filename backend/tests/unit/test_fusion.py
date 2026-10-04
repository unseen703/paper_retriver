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
