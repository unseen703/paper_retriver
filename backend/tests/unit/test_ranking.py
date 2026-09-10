"""
R3 -- `services/ranking.py`: rank-percentile normalization and scoring.

Journey:

    As someone tuning weights, I want a weight of 0.4 to mean the same thing on
    every feature, so that changing one number has the effect I expected rather
    than an effect I have to discover by experiment.

**Not a z-score, and the reason is the whole design.** PLAN.md:

    "Normalize by rank-percentile within the pool, not z-score. Citation counts
    are power-law distributed; one hub gives a z-score of 40 and flattens every
    other feature to noise. Percentile rank is outlier-immune and makes weights
    directly interpretable as relative importance."

That is not a stylistic preference. A citation distribution with one 190,000-cite
paper in it makes every other paper's z-score indistinguishable from zero, so a
`quality` weight of 0.4 would silently contribute nothing for anyone except the
hub. The test named `test_one_enormous_outlier_does_not_flatten_everything_else`
is the one that pins it.

CLAUDE.md rule 3: this module is **pure**. No I/O, no DB, no network -- which is
what lets every case below be a plain function call with no fixture at all.
"""

from __future__ import annotations

import pytest

from app.services.ranking import (
    citations_per_year,
    rank_percentile,
    recency,
    score_paper,
)

# --------------------------------------------------------------------------
# Rank-percentile normalization
# --------------------------------------------------------------------------


def test_the_largest_value_scores_one_and_the_smallest_zero() -> None:
    """
    The endpoints anchor the scale. Without them a weight would mean something
    slightly different in every pool, which is exactly what normalization is
    for avoiding.
    """
    normalized = rank_percentile({"a": 10.0, "b": 20.0, "c": 30.0})
    assert normalized["c"] == pytest.approx(1.0)
    assert normalized["a"] == pytest.approx(0.0)


def test_the_middle_value_sits_in_the_middle() -> None:
    normalized = rank_percentile({"a": 1.0, "b": 2.0, "c": 3.0})
    assert normalized["b"] == pytest.approx(0.5)


def test_one_enormous_outlier_does_not_flatten_everything_else() -> None:
    """
    **The test this module exists for.** Citation counts are power-law
    distributed; a real corpus contains a paper with 190,000 citations next to
    papers with 3, 5 and 9.

    Under a z-score the hub scores about 1.7 and the other three land within
    0.0002 of each other -- so a `quality` weight of 0.4 contributes nothing
    for anyone but the hub, and the tuner cannot tell because the number is
    still there.

    Under rank-percentile the three ordinary papers stay evenly spread across
    the range, which is what makes the weight mean what it says.
    """
    normalized = rank_percentile({"a": 3.0, "b": 5.0, "c": 9.0, "hub": 190_000.0})

    assert normalized["hub"] == pytest.approx(1.0)
    # Evenly spaced by rank, entirely unaffected by how far away the hub is.
    assert normalized["a"] == pytest.approx(0.0)
    assert normalized["b"] == pytest.approx(1 / 3)
    assert normalized["c"] == pytest.approx(2 / 3)


def test_moving_the_outlier_further_away_changes_nothing() -> None:
    """Outlier-immunity, stated directly: the ranks are what matter."""
    near = rank_percentile({"a": 3.0, "b": 5.0, "hub": 100.0})
    far = rank_percentile({"a": 3.0, "b": 5.0, "hub": 10_000_000.0})
    assert near == far


def test_ties_share_a_percentile() -> None:
    """
    Two papers with the same citation count are equally good on that feature.
    Breaking the tie by id would let an arbitrary insertion order leak into a
    score, which is the kind of thing that makes a ranking unreproducible.
    """
    normalized = rank_percentile({"a": 5.0, "b": 5.0, "c": 9.0})
    assert normalized["a"] == normalized["b"]


def test_every_value_identical_gives_everyone_the_same_score() -> None:
    """
    Degenerate but real: a pool where nothing distinguishes anyone on this
    feature. Dividing by a zero range is the obvious way to crash here, and
    0.5 -- the middle -- says "this feature has nothing to contribute" without
    favouring anyone.
    """
    normalized = rank_percentile({"a": 7.0, "b": 7.0, "c": 7.0})
    assert set(normalized.values()) == {0.5}


def test_a_single_paper_is_not_a_ranking() -> None:
    """One paper is both the best and the worst. 0.5 is the honest answer."""
    assert rank_percentile({"only": 42.0}) == {"only": 0.5}


def test_an_empty_pool_normalizes_to_nothing() -> None:
    assert rank_percentile({}) == {}


def test_negative_values_are_handled() -> None:
    """Nothing here assumes positivity; a future feature may be signed."""
    normalized = rank_percentile({"a": -10.0, "b": 0.0, "c": 10.0})
    assert normalized["a"] == pytest.approx(0.0)
    assert normalized["c"] == pytest.approx(1.0)


def test_normalization_is_deterministic() -> None:
    values = {"a": 3.0, "b": 1.0, "c": 2.0}
    assert rank_percentile(values) == rank_percentile(values)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def test_the_score_is_the_weighted_sum() -> None:
    total, breakdown = score_paper(
        {"overlap": 1.0, "recency": 0.5}, {"overlap": 2.0, "recency": 0.4}
    )
    assert total == pytest.approx(2.0 * 1.0 + 0.4 * 0.5)
    assert breakdown == {"overlap": pytest.approx(2.0), "recency": pytest.approx(0.2)}


def test_the_breakdown_terms_sum_to_the_score() -> None:
    """
    BUILD.md's stated test. `<ScoreBreakdown>` renders these as bars beside the
    total, and a breakdown that does not add up is worse than no breakdown --
    it looks like an explanation while being one.
    """
    features = {"overlap": 0.8, "quality": 0.3, "recency": 0.9, "hub": 0.2}
    weights = {"overlap": 2.0, "quality": 0.4, "recency": 0.3, "hub": -0.6}
    total, breakdown = score_paper(features, weights)
    assert sum(breakdown.values()) == pytest.approx(total)


def test_a_negative_weight_subtracts() -> None:
    """
    `hub` is subtracted in `ranking.yaml`. A hub is a paper everything cites,
    which makes it a bad recommendation however high it scores elsewhere --
    you have already read it.
    """
    total, breakdown = score_paper({"hub": 1.0}, {"hub": -0.6})
    assert total == pytest.approx(-0.6)
    assert breakdown["hub"] < 0


def test_a_zero_weight_contributes_nothing_and_is_not_reported() -> None:
    """
    `ranking.yaml` ships several weights at 0.00 for features that arrive in
    later releases. Rendering a bar of length zero for each would fill the
    breakdown with terms that say nothing.
    """
    _, breakdown = score_paper({"overlap": 1.0, "ppr": 0.9}, {"overlap": 2.0, "ppr": 0.0})
    assert "ppr" not in breakdown


def test_a_feature_with_no_weight_is_ignored() -> None:
    """
    A feature computed but not yet weighted must not leak into the score. The
    config is what decides, and it should be possible to add a feature before
    deciding what it is worth.
    """
    total, _ = score_paper({"overlap": 1.0, "experimental": 99.0}, {"overlap": 2.0})
    assert total == pytest.approx(2.0)


def test_a_weight_with_no_feature_contributes_nothing() -> None:
    """
    The mirror case, and the more dangerous one: a weighted feature that was
    never computed for this paper. Treating a missing feature as anything but
    absent would invent a score out of a gap in the data.
    """
    total, breakdown = score_paper({"overlap": 1.0}, {"overlap": 2.0, "cocite": 0.5})
    assert total == pytest.approx(2.0)
    assert "cocite" not in breakdown


def test_scoring_nothing_gives_zero_rather_than_failing() -> None:
    total, breakdown = score_paper({}, {})
    assert total == 0.0
    assert breakdown == {}


def test_the_same_inputs_give_a_byte_identical_score() -> None:
    """
    CLAUDE.md rule 7. Summing a dict in iteration order is deterministic in
    Python, but only because the terms are accumulated in a fixed order -- so
    the sort is explicit rather than incidental.
    """
    features = {"recency": 0.9, "overlap": 0.8, "quality": 0.3}
    weights = {"overlap": 2.0, "quality": 0.4, "recency": 0.3}
    assert score_paper(features, weights) == score_paper(features, weights)


def test_scoring_is_monotone_in_each_feature() -> None:
    """
    BUILD.md asks for monotonicity per feature. Raising a positively-weighted
    feature must not lower the score -- an obvious property, and precisely the
    one a sign error breaks while leaving every total looking plausible.
    """
    weights = {"overlap": 2.0, "quality": 0.4}
    low, _ = score_paper({"overlap": 0.2, "quality": 0.5}, weights)
    high, _ = score_paper({"overlap": 0.8, "quality": 0.5}, weights)
    assert high > low


# --------------------------------------------------------------------------
# Age normalization and recency
# --------------------------------------------------------------------------


def test_citations_are_normalized_by_age() -> None:
    """
    Rate, not total. Ranking by raw count decides that older is better, which
    for a discovery tool is backwards -- the old ones are what you have already
    read.
    """
    old = citations_per_year(400, 2016, as_of_year=2026)  # 400 over 11 years
    new = citations_per_year(120, 2024, as_of_year=2026)  # 120 over 3
    assert new > old


def test_a_paper_published_this_year_does_not_divide_by_zero() -> None:
    """
    Age 0 is a real case every January. An infinity here would dominate every
    ranking it appeared in; counting the first year as whole understates the
    paper slightly, which is the safe direction to be wrong in.
    """
    assert citations_per_year(10, 2026, as_of_year=2026) == pytest.approx(10.0)


def test_an_unknown_year_falls_back_to_the_raw_count() -> None:
    """
    CLAUDE.md rule 6: a missing field degrades a score, never raises.
    `rank_percentile` still places it among its peers.
    """
    assert citations_per_year(50, None, as_of_year=2026) == pytest.approx(50.0)


def test_a_paper_from_this_year_is_maximally_recent() -> None:
    assert recency(2026, as_of_year=2026) == pytest.approx(1.0)


def test_recency_halves_over_the_half_life() -> None:
    assert recency(2022, as_of_year=2026, half_life=4.0) == pytest.approx(0.5)
    assert recency(2018, as_of_year=2026, half_life=4.0) == pytest.approx(0.25)


def test_recency_decays_rather_than_reaching_zero() -> None:
    """
    Exponential, not a linear ramp: "5 years old versus 6" matters far less
    than "this year versus last", and a ramp has to invent a cutoff at which
    recency becomes exactly nothing.
    """
    assert 0.0 < recency(1990, as_of_year=2026) < 0.01


def test_an_unknown_year_is_not_recent() -> None:
    """
    Zero, not neutral. Guessing generously would let every paper with missing
    metadata outrank a real five-year-old one.
    """
    assert recency(None, as_of_year=2026) == 0.0


def test_a_future_year_is_not_more_than_maximally_recent() -> None:
    """
    S2 publication dates run ahead of the calendar for anything in press. It
    should be as recent as possible, not more recent than possible.
    """
    assert recency(2027, as_of_year=2026) == pytest.approx(1.0)
