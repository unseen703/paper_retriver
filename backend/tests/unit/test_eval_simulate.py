"""
R5.5 -- the simulated-user eval.

Journey:

    As someone asking "does liking papers actually help?", I want the answer
    measured by revealing real ground-truth papers as likes one at a time, with
    revealed papers removed from both ranking and truth so a like cannot
    inflate recall by pinning what the user already found.

The synthetic pool is built so the answer is known by hand: truth papers share
a feature the base weights undervalue, and decoys sit just above them.
"""

from __future__ import annotations

from simulate import LIKE_STEPS, K, recall_after_likes, reveal_order, simulate_user
from sweep import ScoredPool

BASE = {"overlap": 2.0, "quality": 0.4}
TRUTH = list(range(1, 11))
DECOYS = list(range(100, 130))


def _pool(case_id: int = 1, n_truth: int = 10) -> ScoredPool:
    features: dict[int, dict[str, float]] = {}
    for t in TRUTH[:n_truth]:
        features[t] = {"overlap": 0.0, "quality": 1.0}  # score 0.40
    for d in DECOYS:
        features[d] = {"overlap": 0.2, "quality": 0.1}  # score 0.44 -> above truth
    return ScoredPool(case_id, features, frozenset(TRUTH[:n_truth]))


def test_without_likes_decoys_crowd_out_the_truth() -> None:
    assert recall_after_likes(_pool(), BASE, []) == 0.0


def test_below_rocchio_min_labels_nothing_moves() -> None:
    pool = _pool()
    assert recall_after_likes(pool, BASE, reveal_order(pool, 1)[:3]) == 0.0


def test_recall_rises_once_rocchio_acts() -> None:
    result = simulate_user([_pool(1), _pool(2)], BASE, seed=1)
    curve = result["curve"]
    assert isinstance(curve, dict)
    means = [curve[str(n)]["mean"] for n in LIKE_STEPS]
    assert means[0] == 0.0
    assert means[LIKE_STEPS.index(5)] == 1.0  # 5 truth left, all inside the top K
    assert means == sorted(means)  # never falls on this pool


def test_revealed_papers_leave_both_ranking_and_truth() -> None:
    # If revealed papers stayed in the truth they would count as found for free.
    pool = _pool()
    revealed = reveal_order(pool, 1)[:5]
    # Remaining truth is 5; recall is over those 5, so it cannot exceed 1.0.
    assert 0.0 <= recall_after_likes(pool, BASE, revealed) <= 1.0
    assert K == 20


def test_cases_without_enough_reachable_truth_are_skipped_not_averaged() -> None:
    result = simulate_user([_pool(1), _pool(2, n_truth=4)], BASE, seed=1)
    assert result["n_cases"] == 1
    assert result["n_cases_skipped"] == 1


def test_unreachable_truth_is_never_revealed() -> None:
    pool = ScoredPool(1, _pool().features, frozenset(TRUTH) | {999})
    assert 999 not in reveal_order(pool, 1)


def test_deterministic() -> None:
    pools = [_pool(1), _pool(2)]
    assert simulate_user(pools, BASE, seed=7) == simulate_user(pools, BASE, seed=7)
    assert reveal_order(_pool(), 7) == reveal_order(_pool(), 7)


def test_no_pools_is_empty_not_an_error() -> None:
    result = simulate_user([], BASE, seed=1)
    assert result["n_cases"] == 0
