"""
R4.1 -- the IR metrics, against answers worked out by hand.

Journey:

    As someone claiming this recommender works, I want numbers that mean what
    the literature means by them, so "Recall@20 = 0.41" is a claim a reader can
    check rather than a figure I chose.

BUILD.md: "Recall@{10,20,50}, NDCG@20, MRR, Hit@10, bootstrap 95% CIs".

**Every expected value here is derived, not observed.** A metric test whose
expectation came from running the implementation proves only that the code does
what it did yesterday. Each case below either works the arithmetic out in the
docstring or uses a degenerate input whose answer is forced.

The whole release exists because, as PLAN.md M9 puts it, evaluation comes before
personalization -- there is no point tuning weights against a number nobody has
checked.
"""

from __future__ import annotations

import math

import pytest
from metrics import bootstrap_ci, hit_at_k, ndcg_at_k, recall_at_k, reciprocal_rank

# --------------------------------------------------------------------------
# Recall@k
# --------------------------------------------------------------------------


def test_recall_counts_the_share_of_ground_truth_found() -> None:
    """Three of five relevant papers inside the top 10 is 0.6, by definition."""
    ranked = [1, 2, 3, 90, 91, 92, 93, 94, 95, 96]
    assert recall_at_k(ranked, {1, 2, 3, 4, 5}, 10) == pytest.approx(0.6)


def test_recall_ignores_anything_past_k() -> None:
    # The cut is the point: a paper at rank 11 is one the reader never saw.
    ranked = [90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 1]
    assert recall_at_k(ranked, {1}, 10) == pytest.approx(0.0)
    assert recall_at_k(ranked, {1}, 11) == pytest.approx(1.0)


def test_recall_is_a_share_of_ground_truth_not_of_the_list() -> None:
    """
    Recall's denominator is what there was to find, not what was returned.
    Dividing by `k` would make a short list look better than a long one holding
    the same hits -- precision's question, answered under recall's name.
    """
    assert recall_at_k([1, 2], {1, 2, 3, 4}, 10) == pytest.approx(0.5)


def test_recall_with_no_ground_truth_is_zero_rather_than_a_crash() -> None:
    # A benchmark case whose ground truth was entirely filtered out is a real
    # thing to hit, and dividing by zero mid-sweep would lose the whole run.
    assert recall_at_k([1, 2, 3], set(), 10) == 0.0


def test_recall_counts_a_repeated_id_once() -> None:
    # A ranker that emits the same paper twice has not found two papers.
    assert recall_at_k([1, 1, 1], {1, 2}, 10) == pytest.approx(0.5)


# --------------------------------------------------------------------------
# Hit@k
# --------------------------------------------------------------------------


def test_hit_is_one_when_anything_relevant_appears() -> None:
    assert hit_at_k([9, 8, 1], {1, 2}, 10) == 1.0


def test_hit_is_zero_when_nothing_relevant_appears() -> None:
    assert hit_at_k([9, 8, 7], {1, 2}, 10) == 0.0


def test_hit_respects_the_cut() -> None:
    assert hit_at_k([9, 8, 1], {1}, 2) == 0.0


# --------------------------------------------------------------------------
# Reciprocal rank
# --------------------------------------------------------------------------


def test_reciprocal_rank_is_one_over_the_first_hit() -> None:
    """First relevant paper at position 3, one-indexed, so 1/3."""
    assert reciprocal_rank([9, 8, 1, 2], {1, 2}) == pytest.approx(1 / 3)


def test_reciprocal_rank_is_one_when_the_first_result_is_relevant() -> None:
    assert reciprocal_rank([1, 9, 8], {1}) == pytest.approx(1.0)


def test_reciprocal_rank_is_zero_when_nothing_is_found() -> None:
    # Zero, not undefined: MRR averages over cases and a miss has to contribute.
    assert reciprocal_rank([9, 8, 7], {1}) == 0.0


# --------------------------------------------------------------------------
# NDCG@k -- binary relevance
# --------------------------------------------------------------------------


def test_ndcg_is_one_when_every_hit_is_at_the_top() -> None:
    """A perfect ranking scores 1 by construction: DCG equals IDCG."""
    assert ndcg_at_k([1, 2, 9, 8], {1, 2}, 4) == pytest.approx(1.0)


def test_ndcg_worked_by_hand() -> None:
    """
    ranked = [a, b, c], relevant = {a, c}, k = 3.

        DCG  = 1/log2(1+1) + 1/log2(3+1) = 1 + 0.5          = 1.5
        IDCG = 1/log2(1+1) + 1/log2(2+1) = 1 + 0.6309297... = 1.6309297...
        NDCG = 1.5 / 1.6309297...        = 0.9197207...
    """
    expected = 1.5 / (1 + 1 / math.log2(3))
    assert ndcg_at_k(["a", "b", "c"], {"a", "c"}, 3) == pytest.approx(expected)


def test_ndcg_rewards_putting_the_hit_first() -> None:
    # The property that makes NDCG worth having over Recall: it is sensitive to
    # position, so a ranker that finds the same papers lower down scores less.
    assert ndcg_at_k([1, 9, 8], {1}, 3) > ndcg_at_k([9, 8, 1], {1}, 3)


def test_ndcg_is_zero_when_nothing_relevant_is_retrieved() -> None:
    assert ndcg_at_k([9, 8, 7], {1}, 3) == 0.0


def test_ndcg_with_no_ground_truth_is_zero_rather_than_a_crash() -> None:
    # IDCG is zero here, and dividing by it would end the sweep.
    assert ndcg_at_k([1, 2], set(), 3) == 0.0


def test_ndcg_ideal_accounts_for_ground_truth_beyond_k() -> None:
    """
    Two relevant papers but only one slot: the best any ranker could do is one
    hit at rank 1, so that is what IDCG must measure. Computing IDCG over all
    of ground truth instead would cap the achievable score below 1 and make a
    perfect ranking look imperfect.
    """
    assert ndcg_at_k([1], {1, 2}, 1) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Bootstrap confidence intervals
# --------------------------------------------------------------------------


def test_the_interval_brackets_the_mean() -> None:
    values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    low, high = bootstrap_ci(values)
    assert low < sum(values) / len(values) < high


def test_a_tighter_spread_gives_a_narrower_interval() -> None:
    # The whole reason for reporting an interval: it says how much the headline
    # number is worth, and a benchmark of 12 cases earns a wide one.
    spread = bootstrap_ci([0.0, 0.25, 0.5, 0.75, 1.0] * 4)
    tight = bootstrap_ci([0.5, 0.5, 0.5, 0.5, 0.51] * 4)
    assert (tight[1] - tight[0]) < (spread[1] - spread[0])


def test_the_interval_is_deterministic() -> None:
    """
    CLAUDE.md rule 7. A resampling method is random by nature, so the seed is
    fixed -- an interval that moved between runs would make every comparison
    between two configurations unfalsifiable.
    """
    values = [0.1, 0.4, 0.2, 0.9, 0.3]
    assert bootstrap_ci(values) == bootstrap_ci(values)


def test_identical_values_give_a_degenerate_interval() -> None:
    # Every resample is the same sample, so there is no spread to report.
    low, high = bootstrap_ci([0.5] * 10)
    assert low == pytest.approx(0.5)
    assert high == pytest.approx(0.5)


def test_an_empty_sample_has_no_interval() -> None:
    assert bootstrap_ci([]) == (0.0, 0.0)


def test_a_single_case_still_returns_something_reportable() -> None:
    # One benchmark case is a bad benchmark, not a crash.
    assert bootstrap_ci([0.7]) == (pytest.approx(0.7), pytest.approx(0.7))
