"""
Shared vocabulary for the filter cascade (PLAN.md section E4).

Every stage is a pure `Paper -> FilterDecision`: no DB, no network, no clock
(CLAUDE.md rule 3). That is what lets R1.7's checkpoint replay the entire
cascade over the committed fixtures with nothing mocked, and what makes a
disagreement about *why* a paper was rejected answerable by reading one
function.

Two conventions the stages all follow:

**Every branch returns a distinct `reason_code`.** "Rejected" is not an
actionable answer; `LOW_CITE_SURVEY` is. R2.14's review drawer groups by these,
so a stage that reuses a code makes two different problems look like one.

**`is_global` marks a verdict as session-independent.** A 2014 paper is a 2014
paper in every session forever, so era, type and topic verdicts are cacheable
and R1.6 skips re-running them. Verdicts about *this* graph -- already present,
tombstoned, budget-exhausted -- are not.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from app.models import FilterDecision, Outcome


class FilterStage(StrEnum):
    """
    Written into `filter_decisions.stage`, which R2.14 queries.

    An enum rather than loose strings because a typo in a stage name produces no
    error -- just a row nothing ever groups on.
    """

    ERA = "ERA"
    TYPE = "TYPE"
    TOPIC = "TOPIC"
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"


def accept(stage: FilterStage, reason_code: str = "OK", **details: Any) -> FilterDecision:
    return FilterDecision(
        outcome=Outcome.ACCEPT,
        stage=stage,
        reason_code=reason_code,
        details=details,
        is_global=True,
    )


def reject(stage: FilterStage, reason_code: str, **details: Any) -> FilterDecision:
    return FilterDecision(
        outcome=Outcome.REJECT,
        stage=stage,
        reason_code=reason_code,
        details=details,
        is_global=True,
    )


def quarantine(stage: FilterStage, reason_code: str, **details: Any) -> FilterDecision:
    """
    Neither in nor out: held for human review in R2.14's drawer.

    This is the outcome for "we cannot tell", and using it honestly is what
    keeps the corpus clean without silently discarding papers whose metadata is
    merely incomplete.
    """
    return FilterDecision(
        outcome=Outcome.QUARANTINE,
        stage=stage,
        reason_code=reason_code,
        details=details,
        is_global=True,
    )


def should_continue(decision: FilterDecision) -> bool:
    """ACCEPT alone advances to the next stage; the cascade short-circuits."""
    return decision.outcome is Outcome.ACCEPT


__all__ = ["FilterStage", "accept", "quarantine", "reject", "should_continue"]
