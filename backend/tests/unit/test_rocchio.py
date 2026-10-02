"""
R5.3 -- Rocchio-style weight nudging.

Journey:

    As someone who has liked and disliked a few papers, I want the ranker to
    lean toward what my likes have in common, without a handful of clicks
    being able to rewrite the configuration.
"""

from __future__ import annotations

import pytest

from app.services.rocchio import MAX_SCALE, nudge_weights

BASE = {"overlap": 2.0, "quality": 0.4, "hub": -0.6, "ppr": 0.0}


def _likes(n: int, **f: float) -> list[dict[str, float]]:
    return [dict(f) for _ in range(n)]


def test_below_min_labels_nothing_moves() -> None:
    assert nudge_weights(BASE, _likes(2, overlap=1.0), _likes(2, overlap=0.0)) == BASE


def test_no_likes_nothing_moves() -> None:
    assert nudge_weights(BASE, [], _likes(9, overlap=1.0)) == BASE


def test_discriminating_feature_is_boosted() -> None:
    # liked mean 1.0 vs disliked mean 0.0 -> 2.0 + 0.25 * 1.0 * 2.0 = 2.5
    out = nudge_weights(BASE, _likes(3, overlap=1.0), _likes(3, overlap=0.0))
    assert out["overlap"] == pytest.approx(2.5)
    assert out["quality"] == 0.4  # no data -> unchanged


def test_feature_disliked_papers_have_more_of_is_reduced() -> None:
    out = nudge_weights(BASE, _likes(3, quality=0.0), _likes(3, quality=1.0))
    # 0.4 + 0.25 * (0 - 1) * 0.4 = 0.3
    assert out["quality"] == pytest.approx(0.3)


def test_no_dislikes_compares_against_neutral() -> None:
    out = nudge_weights(BASE, _likes(5, overlap=0.5), [])
    assert out["overlap"] == 2.0


def test_zero_weight_stays_zero() -> None:
    out = nudge_weights(BASE, _likes(5, ppr=1.0), _likes(5, ppr=0.0))
    assert out["ppr"] == 0.0


def test_negative_weight_never_flips_sign() -> None:
    out = nudge_weights(BASE, _likes(5, hub=1.0), _likes(5, hub=0.0), rate=100.0)
    assert out["hub"] == 0.0  # clamped at zero, not positive
    assert out["hub"] <= 0.0


def test_magnitude_is_bounded() -> None:
    out = nudge_weights(BASE, _likes(5, overlap=1.0), _likes(5, overlap=0.0), rate=100.0)
    assert out["overlap"] == pytest.approx(MAX_SCALE * 2.0)


def test_missing_feature_is_skipped_not_zeroed() -> None:
    liked = [{"overlap": 1.0}, {"quality": 1.0}, {"overlap": 1.0}]
    out = nudge_weights(BASE, liked, _likes(3, overlap=0.0))
    assert out["overlap"] > 2.0
    assert out["quality"] > 0.4  # mean over the one liked paper that has it


def test_deterministic_and_does_not_mutate_inputs() -> None:
    liked, disliked = _likes(3, overlap=1.0), _likes(3, overlap=0.0)
    base = dict(BASE)
    assert nudge_weights(base, liked, disliked) == nudge_weights(base, liked, disliked)
    assert base == BASE
