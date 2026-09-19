"""
R4.2 -- the temporal cutoff, and the leakage test.

Journey:

    As someone reporting a Recall@20, I want to know the system could not have
    seen the answers, so the number measures retrieval rather than memory.

BUILD.md is unusually blunt about this one:

    "**The leakage test.** Assert no ground-truth paper is visible to the system
    before its cutoff. *This is the most important single test in the project*
    -- without it every headline number is fiction."

The benchmark takes a target paper, samples three of its references as seeds,
and holds the rest back as ground truth. The cutoff is `min(seed publication
date)`: anything published after the earliest seed is the future as far as this
case is concerned, and a system that can see the future scores well for a reason
that has nothing to do with recommending.

**Both halves of an unknown date err toward exclusion, in opposite directions.**
A partial date is a range, not a point. For a *seed* the cutoff takes the
earliest day that range could mean, and for a *candidate* visibility takes the
latest -- so "2020" as a seed means 2020-01-01 and "2020" as a candidate means
2020-12-31. Any other pairing lets an ambiguous date smuggle a paper through,
and it is exactly the ambiguous ones nobody would notice.
"""

from __future__ import annotations

from datetime import date

import pytest
from temporal import cutoff_from_seeds, enforce_cutoff, visible_at

# --------------------------------------------------------------------------
# The cutoff
# --------------------------------------------------------------------------


def test_the_cutoff_is_the_earliest_seed() -> None:
    """
    `min`, not `max`. The seeds are what the user is imagined to have started
    from, and the system may only see what existed when the *first* of them
    did -- taking the latest would hand it a window in which most of the
    ground truth had already appeared.
    """
    assert cutoff_from_seeds(["2021-06-01", "2019-03-15", "2020-01-01"]) == date(2019, 3, 15)


def test_a_partial_seed_date_takes_its_earliest_day() -> None:
    # "2019" could mean any day that year; the earliest is the only reading
    # that cannot accidentally widen the window.
    assert cutoff_from_seeds(["2019"]) == date(2019, 1, 1)
    assert cutoff_from_seeds(["2019-07"]) == date(2019, 7, 1)


def test_an_undated_seed_is_ignored_rather_than_guessed() -> None:
    assert cutoff_from_seeds(["2020-05-05", None, ""]) == date(2020, 5, 5)


def test_no_usable_seed_date_means_no_case() -> None:
    """
    None, not "the beginning of time". A case with no cutoff cannot be
    evaluated honestly, and the builder drops it rather than running it with
    the guard switched off.
    """
    assert cutoff_from_seeds([None, "", "not-a-date"]) is None
    assert cutoff_from_seeds([]) is None


# --------------------------------------------------------------------------
# Visibility
# --------------------------------------------------------------------------


CUTOFF = date(2020, 6, 15)


def test_a_paper_published_before_the_cutoff_is_visible() -> None:
    assert visible_at("2019-01-01", CUTOFF) is True


def test_a_paper_published_after_the_cutoff_is_not() -> None:
    assert visible_at("2020-06-16", CUTOFF) is False


def test_a_paper_published_on_the_cutoff_is_visible() -> None:
    """
    BUILD.md says reject anything published *after* the cutoff. A paper the
    seed could have cited on the day it appeared is not the future.
    """
    assert visible_at("2020-06-15", CUTOFF) is True


def test_a_partial_candidate_date_takes_its_latest_day() -> None:
    """
    The opposite rounding to the cutoff's, and the reason is asymmetric risk.
    "2020" as a candidate could mean December; reading it as January would let
    a paper from six months after the cutoff count as visible.
    """
    assert visible_at("2020", date(2020, 12, 31)) is True
    assert visible_at("2020", date(2020, 12, 30)) is False
    assert visible_at("2019", CUTOFF) is True


def test_an_undated_candidate_is_not_visible() -> None:
    """
    **The conservative direction, and the one that matters.** A paper whose
    date is unknown cannot be *shown* to predate the cutoff, and admitting it
    would let exactly the papers nobody can check leak the future.

    It costs recall -- an undated paper that was genuinely old counts as a miss
    -- and that is the right way to be wrong here.
    """
    assert visible_at(None, CUTOFF) is False
    assert visible_at("", CUTOFF) is False
    assert visible_at("no idea", CUTOFF) is False


# --------------------------------------------------------------------------
# The leakage test itself
# --------------------------------------------------------------------------


def test_enforce_cutoff_admits_only_the_past() -> None:
    candidates = [
        (1, "2018-01-01"),
        (2, "2020-06-15"),
        (3, "2020-06-16"),
        (4, None),
        (5, "2025-12-31"),
    ]
    assert enforce_cutoff(candidates, CUTOFF) == [1, 2]


def test_no_ground_truth_paper_from_the_future_survives_the_guard() -> None:
    """
    **BUILD.md's named test, stated as the property it is.**

    Ground truth is the target's remaining references, and plenty of them
    postdate the earliest seed -- those are legitimately unreachable and count
    as misses. What must never happen is one of them reaching the ranker: a
    system that can see the answers scores beautifully for a reason that has
    nothing to do with recommending, and every number in `docs/evaluation.md`
    would be fiction.
    """
    ground_truth = [(i, f"20{20 + i % 6}-0{1 + i % 9}-01") for i in range(1, 40)]
    admitted = set(enforce_cutoff(ground_truth, CUTOFF))

    leaked = [
        (paper_id, published)
        for paper_id, published in ground_truth
        if paper_id in admitted and not visible_at(published, CUTOFF)
    ]
    assert leaked == [], f"the guard admitted papers from after the cutoff: {leaked}"


def test_a_cutoff_of_none_admits_nothing() -> None:
    """
    A case with no usable cutoff must not quietly evaluate with the guard off.
    Returning everything here would turn an unevaluable case into the
    best-scoring one in the benchmark.
    """
    assert enforce_cutoff([(1, "2000-01-01"), (2, "2019-01-01")], None) == []


def test_the_guard_preserves_the_order_it_was_given() -> None:
    # It filters; the ranking is someone else's decision. Reordering here would
    # silently rewrite the thing being measured.
    candidates = [(3, "2001-01-01"), (1, "2002-01-01"), (2, "2003-01-01")]
    assert enforce_cutoff(candidates, CUTOFF) == [3, 1, 2]


def test_an_empty_candidate_list_is_empty_not_an_error() -> None:
    assert enforce_cutoff([], CUTOFF) == []


@pytest.mark.parametrize("bad", ["2020-13-01", "2020-02-30", "20-20-20", "2o2o"])
def test_an_unparseable_date_is_treated_as_unknown(bad: str) -> None:
    # Malformed beats missing for confusion value: a date that looks like a
    # date but is not must take the same conservative path, not raise mid-sweep.
    assert visible_at(bad, CUTOFF) is False
