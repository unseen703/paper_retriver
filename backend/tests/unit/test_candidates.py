"""
R1.8 -- candidate pooling and the R1 prescore.

**Pooling is the whole point of the task.** BUILD.md: "UNION candidates from ALL
frontier nodes. Compute anchor_overlap during the union -- it is the whole
reason for pooling. Do not take per-node top-K."

Per-node top-K would be the obvious implementation and it destroys the signal:
a paper cited by three of your seeds is far stronger evidence than a paper cited
once by each of three unrelated ones, and taking the best K per node throws that
distinction away before it can be measured. `anchor_overlap` is the dominant
term in the prescore precisely because it is the one signal citation structure
gives you that a keyword search cannot.

**The hub guard** stops forward expansion from a paper with more citations than
`forward_expand_max` (2000). Attention Is All You Need has 191,436; forward
expansion from it would return an arbitrary slice of a fifth of modern ML and
exhaust the budget on noise. Backward from a hub is still fine -- a hub's own
reference list is finite and highly informative.
"""

from __future__ import annotations

import math

import pytest

from app.config import filters as cfg
from app.services.candidates import PoolEntry, prescore


def _entry(**over: object) -> PoolEntry:
    base: dict[str, object] = {
        "paper_id": 1,
        "anchor_overlap": 1,
        "citation_count": 100,
        "age_years": 3.0,
        "direction": "BACKWARD",
        "intents": (),
        "is_influential": False,
        "source_ids": (10,),
    }
    base.update(over)
    return PoolEntry(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# anchor_overlap dominates, by design
# --------------------------------------------------------------------------


def test_overlap_raises_the_score() -> None:
    assert prescore(_entry(anchor_overlap=3), cfg) > prescore(_entry(anchor_overlap=1), cfg)


def test_overlap_outweighs_every_other_single_term() -> None:
    """
    Two anchors beat a paper that wins on influence, recency, intent and
    citations combined. That ordering is the design, not an accident.
    """
    shared = _entry(anchor_overlap=3, citation_count=10, age_years=8.0)
    decorated = _entry(
        anchor_overlap=1,
        citation_count=5000,
        age_years=0.5,
        intents=("methodology",),
        is_influential=True,
    )
    assert prescore(shared, cfg) > prescore(decorated, cfg)


def test_overlap_contributes_two_points_each() -> None:
    delta = prescore(_entry(anchor_overlap=2), cfg) - prescore(_entry(anchor_overlap=1), cfg)
    assert delta == pytest.approx(2.0)


# --------------------------------------------------------------------------
# The remaining terms
# --------------------------------------------------------------------------


def test_a_methodology_intent_helps() -> None:
    """Citing someone's method is stronger evidence than citing for background."""
    assert prescore(_entry(intents=("methodology",)), cfg) > prescore(
        _entry(intents=("background",)), cfg
    )


def test_influence_helps() -> None:
    assert prescore(_entry(is_influential=True), cfg) > prescore(_entry(is_influential=False), cfg)


def test_a_recent_paper_gets_the_recency_bump() -> None:
    assert prescore(_entry(age_years=1.0), cfg) > prescore(_entry(age_years=5.0), cfg)


def test_the_recency_bump_has_a_hard_edge_at_18_months() -> None:
    just_inside = prescore(_entry(age_years=1.4, citation_count=0), cfg)
    just_outside = prescore(_entry(age_years=1.6, citation_count=0), cfg)
    assert just_inside - just_outside == pytest.approx(0.3)


def test_citations_per_year_beats_raw_citations() -> None:
    """A 2-year-old paper with 200 citations outranks a 20-year-old with 400."""
    fast = _entry(citation_count=200, age_years=2.0)
    slow = _entry(citation_count=400, age_years=20.0)
    assert prescore(fast, cfg) > prescore(slow, cfg)


def test_a_very_young_paper_does_not_divide_by_zero() -> None:
    assert math.isfinite(prescore(_entry(age_years=0.0, citation_count=50), cfg))


def test_age_is_floored_at_half_a_year() -> None:
    """Appendix B.3: max(age_years, 0.5). A brand-new paper is not infinitely hot."""
    assert prescore(_entry(age_years=0.0), cfg) == prescore(_entry(age_years=0.4), cfg)


# --------------------------------------------------------------------------
# The hub penalty
# --------------------------------------------------------------------------


def test_a_hub_is_penalised() -> None:
    normal = _entry(citation_count=500, anchor_overlap=1)
    hub = _entry(citation_count=191_436, anchor_overlap=1)
    assert prescore(hub, cfg) < prescore(normal, cfg)


def test_the_penalty_only_applies_above_the_threshold() -> None:
    """
    Below forward_expand_max the hub term is exactly zero, not merely small.

    The tolerance is loose on purpose: these two entries differ by one citation,
    so they differ slightly through the citations-per-year term too. What is
    being asserted is that NO hub penalty applies -- the smallest real one, at
    one citation over the threshold, is 0.6 * log1p(1) / 10 = 0.042, three
    orders of magnitude above this bound.
    """
    below = _entry(citation_count=cfg.forward_expand_max - 1, age_years=3.0)
    at = _entry(citation_count=cfg.forward_expand_max, age_years=3.0)
    assert prescore(below, cfg) == pytest.approx(prescore(at, cfg), abs=1e-3)


def test_one_citation_over_the_threshold_does_apply_a_penalty() -> None:
    """
    The other half: the boundary is real, not merely untested.

    Same 1e-3 tolerance and the same reason -- the extra citation also moves the
    citations-per-year term by about 4e-5. The hub penalty being measured is
    0.6 * log1p(1) / 10 = 0.042.
    """
    at = _entry(citation_count=cfg.forward_expand_max, age_years=3.0)
    over = _entry(citation_count=cfg.forward_expand_max + 1, age_years=3.0)
    penalty = prescore(at, cfg) - prescore(over, cfg)
    assert penalty == pytest.approx(0.6 * math.log1p(1) / 10, abs=1e-3)


def test_a_hub_can_still_win_on_overlap() -> None:
    """
    The penalty is a discount, not an exclusion. A hub cited by four of your
    seeds is still the most relevant thing in the pool.
    """
    hub_shared = _entry(citation_count=191_436, anchor_overlap=4)
    plain = _entry(citation_count=100, anchor_overlap=1)
    assert prescore(hub_shared, cfg) > prescore(plain, cfg)


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_prescore_is_deterministic() -> None:
    entry = _entry(anchor_overlap=2, intents=("methodology", "background"))
    assert prescore(entry, cfg) == prescore(entry, cfg)


def test_intent_order_does_not_change_the_score() -> None:
    """CLAUDE.md rule 7: never depend on iteration order."""
    a = _entry(intents=("methodology", "background"))
    b = _entry(intents=("background", "methodology"))
    assert prescore(a, cfg) == prescore(b, cfg)


def test_prescore_is_pure() -> None:
    entry = _entry(anchor_overlap=2)
    prescore(entry, cfg)
    assert entry.anchor_overlap == 2
