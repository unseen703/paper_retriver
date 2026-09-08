"""
The graph state machine (R2.1), as a **data table**.

BUILD.md asks for `TRANSITIONS: dict[(from, to), Rule]` rather than branching
code, and the reason is testability: a table can be enumerated over every
(state x state) pair -- including every illegal one -- so a transition cannot
acquire behaviour by being forgotten. An if/else chain can only be tested
where somebody thought to look, and the interesting cases in a state machine
are precisely the ones nobody thought of.

The machine is PLAN.md's "Two orthogonal state axes" diagram. Three rules
carry most of the weight:

**A seed cannot be labelled.** A seed is a paper you chose deliberately, by
title, through a path that fetched its metadata and ran the filter cascade.
Liking it says nothing; disliking it contradicts the act of adding it. The way
out of a seed is removal, which is a different verb with a different audit
trail. PLAN.md marks this `409`.

**Nothing can be promoted to a seed.** Seeding runs a fetch and a cascade.
Relabelling a candidate as a seed would produce one that went through neither,
and its depth would be a lie about how it entered the graph.

**Leaving LIKED sweeps.** A liked node is an anchor, and candidates justify
their existence by reachability from an anchor. Un-liking one can orphan
everything that hung off it, so the rule says `SWEEP` -- the caller cannot
infer that from the two state names alone, which is exactly why side effects
belong in the table rather than in the caller's head.

Side effects are *names*, not callables. This module stays a pure description
of the machine; the service layer decides what `SWEEP` costs and when to pay
it. That keeps this file testable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import NODE_STATES


class TransitionError(RuntimeError):
    """
    A refused transition, carrying the code the API turns into a status.

    The code is the point: "you cannot do that" is not actionable, while
    `SEED_NOT_LABELABLE` names the rule and lets a UI explain it.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class Rule:
    """One cell of the table."""

    allowed: bool
    # None exactly when allowed. A refusal without a reason is a dead end for
    # whoever hits it.
    error_code: str | None = None
    # Names of the effects the caller must perform, in order. Currently:
    #   LIKED / DISLIKED / UNLABELED  -- the interaction_events row to append
    #   SWEEP                         -- run mark-and-sweep (R2.5) afterwards
    side_effects: tuple[str, ...] = ()


# The event each destination state records. A transition to the same state
# records nothing -- see the no-op rule below.
_EVENT_FOR = {"LIKED": "LIKED", "DISLIKED": "DISLIKED", "CANDIDATE": "UNLABELED"}


def _build() -> dict[tuple[str, str], Rule]:
    """
    Construct the full table, every ordered pair present.

    Built rather than written out longhand so that adding a state to
    `NODE_STATES` cannot leave a hole: a missing key is an undefined
    transition, and undefined transitions are how a state machine ends up with
    behaviour nobody chose.
    """
    table: dict[tuple[str, str], Rule] = {}
    for source in sorted(NODE_STATES):
        for target in sorted(NODE_STATES):
            table[(source, target)] = _rule_for(source, target)
    return table


def _rule_for(source: str, target: str) -> Rule:
    # A no-op, and deliberately not an error. Clicking "like" on an
    # already-liked paper is not a mistake worth refusing, and treating it as
    # one would make a double-click write two events into an append-only log.
    if source == target:
        return Rule(allowed=True)

    if source == "SEED":
        return Rule(
            allowed=False,
            error_code="SEED_NOT_LABELABLE",
            side_effects=(),
        )

    if target == "SEED":
        return Rule(
            allowed=False,
            error_code="CANNOT_PROMOTE_TO_SEED",
            side_effects=(),
        )

    effects = [_EVENT_FOR[target]]
    # Leaving LIKED means the graph loses an anchor, and anything reachable
    # only through it is now an orphan. PLAN.md: "LIKED downgraded to CANDIDATE
    # (it stops being an anchor) -> run the sweep."
    if source == "LIKED":
        effects.append("SWEEP")
    return Rule(allowed=True, side_effects=tuple(effects))


TRANSITIONS: dict[tuple[str, str], Rule] = _build()


def plan_transition(source: str, target: str) -> Rule:
    """
    Look up a transition, or raise with its reason.

    Unknown states raise rather than KeyError-ing out of the table, so a typo
    in a request body becomes a nameable 422 instead of a 500.
    """
    if source not in NODE_STATES or target not in NODE_STATES:
        raise TransitionError(
            "UNKNOWN_STATE",
            f"states must be one of {sorted(NODE_STATES)}; got {source!r} -> {target!r}",
        )
    rule = TRANSITIONS[(source, target)]
    if not rule.allowed:
        raise TransitionError(
            rule.error_code or "NOT_ALLOWED",
            f"{source} -> {target} is not a permitted transition",
        )
    return rule


__all__ = ["TRANSITIONS", "Rule", "TransitionError", "plan_transition"]
