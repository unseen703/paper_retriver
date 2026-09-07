"""
R0.3 -- domain models.

Frozen dataclasses with no DB and no HTTP knowledge. Two properties matter and
both are asserted here rather than assumed:

* **Immutability.** These objects cross layer boundaries (client -> service ->
  repo). If a filter could mutate the Paper it is judging, a rejection in one
  stage could silently change the input to the next.
* **Optionality that matches reality.** BUILD.md R0.7 requires every S2 field to
  be Optional, because S2 omits `year`, `abstract` and `venue` constantly. A
  model that demands them turns a degraded score into a crash.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.models import (
    Author,
    CandidateFeatures,
    CrawlState,
    Edge,
    FilterDecision,
    Outcome,
    Paper,
    PaperStub,
    PaperType,
    ScoredCandidate,
)

ALL_MODELS = [
    Paper,
    Author,
    Edge,
    PaperStub,
    FilterDecision,
    CandidateFeatures,
    ScoredCandidate,
]


# --------------------------------------------------------------------------
# Every model is a frozen dataclass
# --------------------------------------------------------------------------


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_model_is_a_dataclass(model: type) -> None:
    assert dataclasses.is_dataclass(model)


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_model_is_frozen(model: type) -> None:
    assert model.__dataclass_params__.frozen, f"{model.__name__} must be frozen"


# --------------------------------------------------------------------------
# Paper
# --------------------------------------------------------------------------


def _paper(**over: object) -> Paper:
    base: dict[str, object] = {
        "s2_paper_id": "abc123",
        "title": "Attention Is All You Need",
        "first_seen_at": "2026-09-07T12:00:00Z",
    }
    base.update(over)
    return Paper(**base)  # type: ignore[arg-type]


def test_paper_needs_only_id_title_and_first_seen() -> None:
    """Everything S2 can omit must be omittable here too."""
    paper = _paper()
    assert paper.year is None
    assert paper.abstract is None
    assert paper.venue is None
    assert paper.doi is None
    assert paper.primary_arxiv_category is None


def test_paper_cannot_be_mutated() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _paper().title = "something else"  # type: ignore[misc]


def test_paper_defaults_match_the_schema() -> None:
    """PLAN.md section C: counts default 0, crawl_state STUB, type UNKNOWN."""
    paper = _paper()
    assert paper.citation_count == 0
    assert paper.reference_count == 0
    assert paper.influential_citation_count == 0
    assert paper.crawl_state is CrawlState.STUB
    assert paper.paper_type is PaperType.UNKNOWN


def test_title_norm_is_derived_not_supplied() -> None:
    """Dedup (R1.3) depends on this being computed one way, everywhere."""
    assert _paper(title="  Deep   RESIDUAL Learning!  ").title_norm == "deep residual learning"


def test_title_norm_strips_punctuation_and_collapses_whitespace() -> None:
    assert _paper(title="BERT: Pre-training of Deep\tBidirectional").title_norm == (
        "bert pretraining of deep bidirectional"
    )


def test_paper_is_hashable_so_it_can_live_in_sets() -> None:
    assert len({_paper(), _paper()}) == 1


def test_json_ish_fields_are_tuples_not_lists() -> None:
    """A list field would make the frozen dataclass unhashable and mutable."""
    paper = _paper(arxiv_categories=("cs.LG", "cs.CL"))
    assert paper.arxiv_categories == ("cs.LG", "cs.CL")
    with pytest.raises(TypeError):
        hash(_paper(arxiv_categories=["cs.LG"]))


def test_stub_papers_are_identifiable() -> None:
    """R0.9 writes STUBs from nested edge fetches; features must never run on one."""
    assert _paper().is_stub is True
    assert _paper(crawl_state=CrawlState.METADATA).is_stub is False


# --------------------------------------------------------------------------
# PaperStub, Author, Edge
# --------------------------------------------------------------------------


def test_paper_stub_carries_only_what_a_search_hit_returns() -> None:
    stub = PaperStub(s2_paper_id="x", title="T")
    assert stub.year is None
    assert stub.citation_count is None


def test_author_h_index_is_optional_until_lazily_fetched() -> None:
    """PLAN.md section C: h_index is NULL until fetched (C4)."""
    assert Author(s2_author_id="a1", name="Ashish Vaswani").h_index is None


def test_edge_direction_is_citing_to_cited() -> None:
    edge = Edge(citing_id=1, cited_id=2, discovered_via="BACKWARD", first_seen_at="2026-09-07")
    assert (edge.citing_id, edge.cited_id) == (1, 2)


def test_edge_rejects_a_self_citation() -> None:
    """Mirrors the CHECK constraint so the error surfaces before the INSERT."""
    with pytest.raises(ValueError, match="self"):
        Edge(citing_id=7, cited_id=7, discovered_via="BACKWARD", first_seen_at="2026-09-07")


def test_edge_rejects_an_unknown_direction() -> None:
    with pytest.raises(ValueError):
        Edge(citing_id=1, cited_id=2, discovered_via="SIDEWAYS", first_seen_at="2026-09-07")


# --------------------------------------------------------------------------
# FilterDecision
# --------------------------------------------------------------------------


def test_outcome_enum_has_exactly_three_members() -> None:
    assert {o.value for o in Outcome} == {"ACCEPT", "QUARANTINE", "REJECT"}


def test_filter_decision_carries_the_five_documented_fields() -> None:
    d = FilterDecision(
        outcome=Outcome.REJECT,
        stage="TYPE",
        reason_code="IS_DATASET",
        details={"matched": "dataset"},
        is_global=True,
    )
    assert d.outcome is Outcome.REJECT
    assert d.stage == "TYPE"
    assert d.reason_code == "IS_DATASET"
    assert d.details == {"matched": "dataset"}
    assert d.is_global is True


def test_global_decisions_are_the_cacheable_ones() -> None:
    """BUILD.md session_id contract: is_global -> session_id NULL on write."""
    era = FilterDecision(
        outcome=Outcome.REJECT, stage="TOPIC", reason_code="PRE_ERA", is_global=True
    )
    in_graph = FilterDecision(
        outcome=Outcome.REJECT, stage="DUPLICATE", reason_code="ALREADY_IN_GRAPH", is_global=False
    )
    assert era.is_global is True
    assert in_graph.is_global is False


def test_filter_decision_details_default_to_empty_not_none() -> None:
    assert FilterDecision(outcome=Outcome.ACCEPT, stage="TYPE", reason_code="OK").details == {}


# --------------------------------------------------------------------------
# CandidateFeatures / ScoredCandidate
# --------------------------------------------------------------------------


def test_candidate_features_default_to_zero() -> None:
    """A missing signal degrades the score; it never raises (CLAUDE.md rule 6)."""
    f = CandidateFeatures(paper_id=1)
    assert f.overlap == 0.0
    assert f.quality == 0.0
    assert f.recency == 0.0
    assert f.hub == 0.0


def test_scored_candidate_keeps_the_breakdown_that_powers_why() -> None:
    sc = ScoredCandidate(
        paper_id=1,
        score=2.5,
        features=CandidateFeatures(paper_id=1, overlap=1.0),
        breakdown={"overlap": 2.0, "quality": 0.5},
    )
    assert sc.score == 2.5
    assert sc.breakdown["overlap"] == 2.0


def test_scored_candidates_sort_by_score_desc_then_paper_id() -> None:
    """CLAUDE.md rule 7: stable sort key (-score, paper_id), no dict ordering."""
    a = ScoredCandidate(paper_id=9, score=1.0, features=CandidateFeatures(paper_id=9))
    b = ScoredCandidate(paper_id=2, score=1.0, features=CandidateFeatures(paper_id=2))
    c = ScoredCandidate(paper_id=5, score=3.0, features=CandidateFeatures(paper_id=5))
    assert [x.paper_id for x in sorted([a, b, c], key=ScoredCandidate.sort_key)] == [5, 2, 9]
