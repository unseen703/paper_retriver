"""
R2.1 -- the graph state machine, as a data table rather than branching code.

Journey:

    As someone curating a graph, I want every label change to follow one
    documented rule, so that an illegal transition is refused with a nameable
    reason instead of half-applied.

BUILD.md is specific about the shape: `TRANSITIONS: dict[(from, to), Rule]`,
where a rule carries `allowed`, `error_code` and `side_effects` -- not an
if/else chain. The reason is testability: a table can be enumerated over every
(state x state) pair, including the illegal ones, and this file does exactly
that. Branching code can only be tested where someone thought to look.

The machine is PLAN.md's "Two orthogonal state axes" diagram, and the rule
that carries the most weight is the one about seeds:

    SEED has no like/dislike edge at all -> 409.

A seed is a statement that you chose this paper deliberately. Liking it is
meaningless and disliking it is contradictory; the way out is removal, which
is a different verb with a different audit trail.
"""

from __future__ import annotations

import pytest

from app.models import NODE_STATES
from app.services.transitions import TRANSITIONS, TransitionError, plan_transition

# Every ordered pair, so nothing can be legal by omission.
ALL_PAIRS = [(a, b) for a in sorted(NODE_STATES) for b in sorted(NODE_STATES)]


# --------------------------------------------------------------------------
# The table itself
# --------------------------------------------------------------------------


def test_the_table_covers_every_state_pair() -> None:
    """
    A missing key is an undefined transition, and undefined transitions are how
    a state machine acquires behaviour nobody chose. Absence must be explicit.
    """
    assert set(TRANSITIONS) == set(ALL_PAIRS)


@pytest.mark.parametrize(("source", "target"), ALL_PAIRS)
def test_every_rule_names_itself(source: str, target: str) -> None:
    """A disallowed rule must carry a reason; an allowed one must not need an excuse."""
    rule = TRANSITIONS[(source, target)]
    if rule.allowed:
        assert rule.error_code is None
    else:
        assert rule.error_code, f"{source}->{target} is refused without saying why"


# --------------------------------------------------------------------------
# What the user may do
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("CANDIDATE", "LIKED"),
        ("CANDIDATE", "DISLIKED"),
        ("LIKED", "CANDIDATE"),
        ("LIKED", "DISLIKED"),
        ("DISLIKED", "CANDIDATE"),
        ("DISLIKED", "LIKED"),
    ],
)
def test_the_labelling_transitions_are_allowed(source: str, target: str) -> None:
    """PLAN.md's diagram: candidate, liked and disliked are freely interchangeable."""
    assert TRANSITIONS[(source, target)].allowed


@pytest.mark.parametrize("target", ["LIKED", "DISLIKED", "CANDIDATE"])
def test_a_seed_cannot_be_labelled(target: str) -> None:
    """
    The rule with the most weight. A seed is a paper you chose on purpose:
    liking it says nothing and disliking it contradicts the act of adding it.
    Removal is the way out, and it is a different verb.
    """
    rule = TRANSITIONS[("SEED", target)]
    assert not rule.allowed
    assert rule.error_code == "SEED_NOT_LABELABLE"


@pytest.mark.parametrize("source", ["CANDIDATE", "LIKED", "DISLIKED"])
def test_nothing_can_be_promoted_to_seed(source: str) -> None:
    """
    Seeding is `POST /nodes`, which fetches metadata and runs the cascade.
    Relabelling something a seed would produce a seed that never went through
    either, and depth 0 would be a lie about how it entered the graph.
    """
    rule = TRANSITIONS[(source, "SEED")]
    assert not rule.allowed
    assert rule.error_code == "CANNOT_PROMOTE_TO_SEED"


@pytest.mark.parametrize("state", sorted(NODE_STATES))
def test_a_transition_to_the_same_state_is_a_no_op_not_an_error(state: str) -> None:
    """
    Clicking "like" on an already-liked paper is not a mistake worth an error.
    It is allowed and does nothing, so a double-click cannot write two events.
    """
    rule = TRANSITIONS[(state, state)]
    assert rule.allowed
    assert rule.side_effects == ()


# --------------------------------------------------------------------------
# Side effects the caller must perform
# --------------------------------------------------------------------------


def test_liking_a_candidate_records_the_event() -> None:
    rule = TRANSITIONS[("CANDIDATE", "LIKED")]
    assert "LIKED" in rule.side_effects


def test_unliking_triggers_a_sweep() -> None:
    """
    PLAN.md: "LIKED downgraded to CANDIDATE (it stops being an anchor) -> run
    the sweep -- its dependents may now be orphans." The table has to say so,
    because the caller cannot infer it from the states alone.
    """
    assert "SWEEP" in TRANSITIONS[("LIKED", "CANDIDATE")].side_effects


def test_disliking_a_liked_paper_also_triggers_a_sweep() -> None:
    """It stops being an anchor by this route too."""
    assert "SWEEP" in TRANSITIONS[("LIKED", "DISLIKED")].side_effects


def test_liking_a_candidate_does_not_sweep() -> None:
    """Gaining an anchor cannot orphan anything. Sweeping would be wasted work."""
    assert "SWEEP" not in TRANSITIONS[("CANDIDATE", "LIKED")].side_effects


# --------------------------------------------------------------------------
# plan_transition -- the callable front door
# --------------------------------------------------------------------------


def test_plan_transition_returns_the_rule_for_a_legal_move() -> None:
    rule = plan_transition("CANDIDATE", "LIKED")
    assert rule.allowed


def test_plan_transition_raises_with_the_error_code() -> None:
    with pytest.raises(TransitionError) as excinfo:
        plan_transition("SEED", "LIKED")
    assert excinfo.value.error_code == "SEED_NOT_LABELABLE"


def test_plan_transition_rejects_an_unknown_target_state() -> None:
    """A typo'd state must not fall through the table into a KeyError."""
    with pytest.raises(TransitionError) as excinfo:
        plan_transition("CANDIDATE", "LKIED")
    assert excinfo.value.error_code == "UNKNOWN_STATE"


def test_plan_transition_rejects_an_unknown_source_state() -> None:
    with pytest.raises(TransitionError) as excinfo:
        plan_transition("GONE", "LIKED")
    assert excinfo.value.error_code == "UNKNOWN_STATE"
