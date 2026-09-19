"""
R4.3 -- building one benchmark case.

Journey:

    As someone measuring this recommender, I want each case to be a fair
    question, so a good score means the ranker found papers rather than that
    the case was easy.

BUILD.md: "sample 150-300 held-out target papers (core-ML, 2019-2024, >=15
references). For each: seeds = 3 randomly chosen references; ground truth = the
remaining references."

The shape of a case *is* the experiment, so it is built by a pure function over
`(target, references)` and tested without a database. The SQL that finds
eligible targets is separate and tested separately.

**PLAN.md's limitations section applies to every number this produces**, and is
worth stating here rather than only in the write-up: a reference list is a
biased proxy for relevance. It omits concurrent work, omits deliberately-uncited
competitors, and includes obligatory citations the author never read. None of
that is fixable by better sampling; it is a property of the ground truth, and
the honest response is to say so.
"""

from __future__ import annotations

import random
from datetime import date

from build_benchmark import MIN_REFERENCES, SEEDS_PER_CASE, make_case


def _refs(n: int, year: int = 2018) -> list[tuple[int, str | None]]:
    """`n` references, each with a usable date."""
    return [(i, f"{year}-01-{1 + (i % 28):02d}") for i in range(1, n + 1)]


def _rng() -> random.Random:
    return random.Random(20260916)


# --------------------------------------------------------------------------
# The split
# --------------------------------------------------------------------------


def test_a_case_takes_three_seeds() -> None:
    case = make_case(999, _refs(20), _rng())
    assert case is not None
    assert len(case.seed_ids) == SEEDS_PER_CASE


def test_ground_truth_is_every_reference_that_is_not_a_seed() -> None:
    """
    The experiment in one line: the seeds are what the user started from, the
    rest is what a perfect recommender would find.
    """
    case = make_case(999, _refs(20), _rng())
    assert case is not None
    assert set(case.seed_ids) | set(case.ground_truth_ids) == set(range(1, 21))
    assert len(case.ground_truth_ids) == 20 - SEEDS_PER_CASE


def test_seeds_and_ground_truth_never_overlap() -> None:
    """
    A seed in the ground truth is a free hit -- the system is handed the paper
    and then rewarded for returning it. That inflates every metric at once and
    looks like a good result.
    """
    case = make_case(999, _refs(30), _rng())
    assert case is not None
    assert not set(case.seed_ids) & set(case.ground_truth_ids)


def test_the_target_is_never_its_own_ground_truth() -> None:
    # A paper that cites itself would otherwise be a guaranteed hit.
    refs = _refs(20) + [(999, "2018-05-05")]
    case = make_case(999, refs, _rng())
    assert case is not None
    assert 999 not in case.ground_truth_ids
    assert 999 not in case.seed_ids


def test_a_duplicated_reference_is_counted_once() -> None:
    refs = _refs(20) + [(5, "2018-01-05")]
    case = make_case(999, refs, _rng())
    assert case is not None
    assert len(set(case.ground_truth_ids)) == len(case.ground_truth_ids)


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------


def test_a_paper_with_too_few_references_is_not_a_case() -> None:
    """
    BUILD.md's floor. With ten references, three seeds leave seven to find, and
    Recall@20 over seven items is noise dressed as a measurement.
    """
    assert make_case(999, _refs(MIN_REFERENCES - 1), _rng()) is None


def test_exactly_the_minimum_is_enough() -> None:
    assert make_case(999, _refs(MIN_REFERENCES), _rng()) is not None


def test_a_case_whose_seeds_have_no_usable_date_is_dropped() -> None:
    """
    No cutoff means no guard, and a case evaluated without the guard would
    outscore every honest one in the benchmark. Dropping it is the only safe
    answer -- see `temporal.cutoff_from_seeds`.
    """
    undated = [(i, None) for i in range(1, 21)]
    assert make_case(999, undated, _rng()) is None


def test_the_cutoff_is_the_earliest_seed_date() -> None:
    refs = [(1, "2016-01-01"), (2, "2017-01-01"), (3, "2018-01-01")]
    refs += [(i, "2015-01-01") for i in range(4, 25)]
    case = make_case(999, refs, random.Random(1))
    assert case is not None
    seed_dates = {paper_id: published for paper_id, published in refs}
    earliest = min(seed_dates[s] for s in case.seed_ids)
    assert case.cutoff == date.fromisoformat(earliest)


# --------------------------------------------------------------------------
# Determinism -- rule 7, and the thing that makes a sweep comparable
# --------------------------------------------------------------------------


def test_the_same_seed_builds_the_same_case() -> None:
    """
    A config sweep compares runs against each other. If the split moved between
    runs, a weight change and a resample would be indistinguishable, and every
    comparison in `docs/evaluation.md` would be meaningless.
    """
    first = make_case(999, _refs(40), random.Random(7))
    second = make_case(999, _refs(40), random.Random(7))
    assert first == second


def test_a_different_seed_builds_a_different_split() -> None:
    # Otherwise the "random" sample is not sampling anything.
    first = make_case(999, _refs(40), random.Random(1))
    second = make_case(999, _refs(40), random.Random(2))
    assert first is not None and second is not None
    assert first.seed_ids != second.seed_ids


def test_the_split_does_not_depend_on_reference_order() -> None:
    """
    S2 returns a reference list in whatever order it likes, and two runs over
    the same paper must produce the same case (rule 7). Sorting before sampling
    is what makes the id the only thing that matters.
    """
    refs = _refs(30)
    shuffled = list(reversed(refs))
    assert make_case(999, refs, random.Random(3)) == make_case(999, shuffled, random.Random(3))
