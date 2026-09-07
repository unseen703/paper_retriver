"""
R1.4 -- the filter base and the era stage.

Pure `Paper -> FilterDecision`. No DB, no I/O (CLAUDE.md rule 3), which is what
lets R1.7's checkpoint replay the whole cascade over fixtures with nothing
mocked.

The case that matters is `year is None`. BUILD.md is emphatic: quarantine, "not
silently admitted". S2 omits `year` constantly -- on preprints, on workshop
papers, on anything with an incomplete record -- so treating unknown as
acceptable would let the year floor leak exactly the papers whose provenance is
weakest. Quarantine keeps them visible and reversible in R2.14's drawer instead.

Era verdicts are `is_global=True`: a 2014 paper is a 2014 paper in every session
forever, so the decision is cacheable across sessions (BUILD.md session_id
contract) and R1.6 skips re-evaluating it.
"""

from __future__ import annotations

import pytest

from app.config import filters as filters_cfg
from app.models import Outcome, Paper
from app.services.filters import era_filter
from app.services.filters.base import FilterStage


def _paper(**over: object) -> Paper:
    base: dict[str, object] = {
        "s2_paper_id": "s1",
        "title": "Some Paper",
        "first_seen_at": "2026-01-01",
    }
    base.update(over)
    return Paper(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# BUILD.md's three named cases
# --------------------------------------------------------------------------


def test_2014_is_rejected_as_pre_era() -> None:
    decision = era_filter(_paper(year=2014), filters_cfg)
    assert decision.outcome is Outcome.REJECT
    assert decision.reason_code == "PRE_ERA"


def test_2015_is_accepted() -> None:
    """The floor is inclusive -- 2015 is in the corpus, not on its edge."""
    decision = era_filter(_paper(year=2015), filters_cfg)
    assert decision.outcome is Outcome.ACCEPT
    assert decision.reason_code == "OK"


def test_an_unknown_year_is_quarantined_not_admitted() -> None:
    """
    BUILD.md: "None -> quarantine, not silently admitted." S2 omits year
    constantly; accepting on absence would leak exactly the weakest records.
    """
    decision = era_filter(_paper(year=None), filters_cfg)
    assert decision.outcome is Outcome.QUARANTINE
    assert decision.reason_code == "YEAR_UNKNOWN"


# --------------------------------------------------------------------------
# Boundary behaviour around the floor
# --------------------------------------------------------------------------


@pytest.mark.parametrize("year", [1990, 2000, 2013, 2014])
def test_everything_below_the_floor_is_rejected(year: int) -> None:
    assert era_filter(_paper(year=year), filters_cfg).outcome is Outcome.REJECT


@pytest.mark.parametrize("year", [2015, 2016, 2020, 2026])
def test_everything_from_the_floor_up_is_accepted(year: int) -> None:
    assert era_filter(_paper(year=year), filters_cfg).outcome is Outcome.ACCEPT


def test_the_floor_comes_from_config_not_a_literal() -> None:
    """Changing filters.yaml must move the boundary without a code change."""
    assert filters_cfg.year_floor == 2015
    below = era_filter(_paper(year=filters_cfg.year_floor - 1), filters_cfg)
    at = era_filter(_paper(year=filters_cfg.year_floor), filters_cfg)
    assert (below.outcome, at.outcome) == (Outcome.REJECT, Outcome.ACCEPT)


def test_a_future_year_is_accepted_rather_than_treated_as_corrupt() -> None:
    """S2 carries next-year publication dates for in-press papers."""
    assert era_filter(_paper(year=2027), filters_cfg).outcome is Outcome.ACCEPT


# --------------------------------------------------------------------------
# Decision shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize("year", [2014, 2015, None])
def test_every_era_verdict_is_global(year: int | None) -> None:
    """
    A 2014 paper is a 2014 paper in every session forever, so the verdict is
    cacheable across sessions and R1.6 need not re-evaluate it.
    """
    assert era_filter(_paper(year=year), filters_cfg).is_global is True


@pytest.mark.parametrize("year", [2014, 2015, None])
def test_every_era_verdict_names_the_era_stage(year: int | None) -> None:
    assert era_filter(_paper(year=year), filters_cfg).stage == FilterStage.ERA


def test_a_rejection_records_the_year_it_saw(year: int = 2014) -> None:
    """R2.14's drawer groups by reason_code and shows why; details carry the why."""
    decision = era_filter(_paper(year=year), filters_cfg)
    assert decision.details["year"] == 2014
    assert decision.details["year_floor"] == filters_cfg.year_floor


def test_every_branch_returns_a_distinct_reason_code() -> None:
    codes = {era_filter(_paper(year=y), filters_cfg).reason_code for y in (2014, 2015, None)}
    assert codes == {"PRE_ERA", "OK", "YEAR_UNKNOWN"}


# --------------------------------------------------------------------------
# The base helpers every stage shares
# --------------------------------------------------------------------------


def test_stages_are_an_enum_not_loose_strings() -> None:
    """filter_decisions.stage is queried by R2.14; typos there are invisible."""
    assert {s.value for s in FilterStage} == {"ERA", "TYPE", "TOPIC", "APPLIED", "DUPLICATE"}


def test_accept_is_the_only_outcome_that_continues_the_cascade() -> None:
    from app.services.filters.base import should_continue

    assert should_continue(era_filter(_paper(year=2020), filters_cfg)) is True
    assert should_continue(era_filter(_paper(year=2014), filters_cfg)) is False
    assert should_continue(era_filter(_paper(year=None), filters_cfg)) is False


def test_the_filter_is_pure() -> None:
    """Same input, same verdict, and the paper is untouched."""
    paper = _paper(year=2014)
    first = era_filter(paper, filters_cfg)
    second = era_filter(paper, filters_cfg)
    assert first == second
    assert paper.year == 2014
