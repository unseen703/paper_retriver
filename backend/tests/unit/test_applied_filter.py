import pytest

from app.models import Outcome
from app.services.filters.applied_filter import (
    ACCEPT_MARGIN,
    applied_filter,
    exemplar_centroid,
)

CORE = [1.0, 0.0]
APPLIED = [0.0, 1.0]


def test_clear_core_is_accepted() -> None:
    d = applied_filter([0.9, 0.1], CORE, APPLIED)
    assert d.outcome is Outcome.ACCEPT
    assert d.reason_code == "APPLIED_CLEAR_CORE"


def test_clear_applied_is_quarantined_never_rejected() -> None:
    d = applied_filter([0.05, 1.0], CORE, APPLIED)
    assert d.outcome is Outcome.QUARANTINE
    assert d.reason_code == "APPLIED_LIKELY_APPLIED"


def test_inside_the_margin_is_quarantined() -> None:
    d = applied_filter([1.0, 0.99], CORE, APPLIED)
    assert d.outcome is Outcome.QUARANTINE
    assert d.reason_code == "APPLIED_UNCERTAIN"


def test_no_vector_never_accepts() -> None:
    d = applied_filter(None, CORE, APPLIED)
    assert d.outcome is Outcome.QUARANTINE
    assert d.reason_code == "APPLIED_NO_EMBEDDING"


@pytest.mark.parametrize("vec", [[1, 0], [0, 1], [1, 1], [-1, 0.2], [0.3, -0.9]])
def test_stage_three_can_never_reject(vec: list[float]) -> None:
    assert applied_filter(vec, CORE, APPLIED).outcome is not Outcome.REJECT


def test_margin_is_scale_invariant_and_worked_by_hand() -> None:
    d = applied_filter([30.0, 0.0], CORE, [0.0, 7.0])
    assert d.details["margin"] == pytest.approx(1.0)
    assert applied_filter([3.0, 4.0], CORE, APPLIED).details["margin"] == pytest.approx(-0.2)


def test_margin_boundary_is_inclusive() -> None:
    assert applied_filter([1.0, 0.0], CORE, APPLIED, accept_margin=1.0).outcome is Outcome.ACCEPT


def test_default_margin_constant_is_what_the_default_uses() -> None:
    # cos 30deg - cos 60deg = 0.366; a margin just above flips the verdict.
    v = [3**0.5 / 2, 0.5]
    assert applied_filter(v, CORE, APPLIED, ACCEPT_MARGIN).outcome is Outcome.ACCEPT
    assert applied_filter(v, CORE, APPLIED, 0.4).outcome is Outcome.QUARANTINE


def test_deterministic() -> None:
    assert applied_filter([0.7, 0.2], CORE, APPLIED) == applied_filter([0.7, 0.2], CORE, APPLIED)


def test_exemplar_centroid_is_unit_mean() -> None:
    c = exemplar_centroid([[2.0, 0.0], [0.0, 5.0]])
    assert list(c) == pytest.approx([2**-0.5, 2**-0.5])


def test_bad_inputs_raise() -> None:
    with pytest.raises(ValueError):
        exemplar_centroid([])
    with pytest.raises(ValueError):
        applied_filter([0.0, 0.0], CORE, APPLIED)
    with pytest.raises(ValueError):
        applied_filter([1.0, 0.0], CORE, APPLIED, accept_margin=-0.1)
