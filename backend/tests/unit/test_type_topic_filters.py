"""
R1.5 -- stages 1 (TYPE) and 2 (TOPIC) of PLAN.md section E4.

Every branch returns a distinct `reason_code`, so R2.14's drawer can group by
cause rather than showing an undifferentiated pile of rejections.

Three things here are load-bearing:

**Primary category wins, both ways.** `primary in CORE_ALLOW` accepts even when
cross-listed to a denied category; `primary in APPLIED_DENY` rejects even when
cross-listed to a core one. Batch Normalization is primary cs.LG cross-listed
cs.CV, and PLAN.md is explicit that it must pass -- "much methodological vision
work is primary cs.LG, so you lose less than the deny list suggests". Getting
this backwards would delete a large slice of core ML.

**The survey policy is an OR, not an AND.** `citations >= 200 OR
citations_per_year >= 40`. The second disjunct is what keeps a good survey from
last year, which cannot yet have 200 citations.

**Venue matching cannot be exact.** `CORE_VENUES` holds acronyms; S2 returns
expanded names -- the live R0.8 run gave "Neural Information Processing
Systems", not "NeurIPS". Exact matching would make stage 2c dead code, and its
failure mode is silent: papers that should pass on venue fall through to
QUARANTINE instead.
"""

from __future__ import annotations

import pytest

from app.config import filters as cfg
from app.models import Outcome, Paper
from app.services.filters import FilterStage, topic_filter, type_filter

AS_OF = 2026


def _paper(**over: object) -> Paper:
    base: dict[str, object] = {
        "s2_paper_id": "s1",
        "title": "Some Paper",
        "first_seen_at": "2026-01-01",
        "year": 2020,
    }
    base.update(over)
    return Paper(**base)  # type: ignore[arg-type]


# ==========================================================================
# STAGE 1 -- TYPE
# ==========================================================================


def test_a_dataset_publication_type_is_rejected() -> None:
    d = type_filter(_paper(publication_types=("Dataset",)), cfg, AS_OF)
    assert (d.outcome, d.reason_code) == (Outcome.REJECT, "IS_DATASET")


def test_an_ordinary_research_paper_passes() -> None:
    d = type_filter(_paper(title="Attention Is All You Need"), cfg, AS_OF)
    assert (d.outcome, d.reason_code) == (Outcome.ACCEPT, "OK")


def test_the_stage_is_named_type() -> None:
    assert type_filter(_paper(), cfg, AS_OF).stage == FilterStage.TYPE


# -- survey policy: the OR is the point --------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "A Survey of Deep Learning",
        "Survey on Graph Neural Networks",
        "A Review of Reinforcement Learning",
        "Review of Transformers",
        "An Overview of Diffusion Models",
    ],
)
def test_survey_titles_are_detected(title: str) -> None:
    """Leading survey/review/overview, with or without an article."""
    d = type_filter(_paper(title=title, citation_count=5, year=2016), cfg, AS_OF)
    assert d.reason_code == "LOW_CITE_SURVEY"


def test_a_highly_cited_survey_is_accepted() -> None:
    d = type_filter(
        _paper(title="A Comprehensive Survey on Graph Neural Networks", citation_count=9000),
        cfg,
        AS_OF,
    )
    assert (d.outcome, d.reason_code) == (Outcome.ACCEPT, "SURVEY_WELL_CITED")


def test_a_recent_survey_passes_on_citations_per_year_alone() -> None:
    """
    The OR's whole purpose. A 2025 survey with 60 citations cannot reach 200,
    but 60/2 = 30... so make it clearly above the per-year floor instead.
    """
    d = type_filter(
        _paper(title="A Survey of Agentic LLMs", citation_count=120, year=2025), cfg, AS_OF
    )
    assert d.outcome is Outcome.ACCEPT
    assert d.reason_code == "SURVEY_WELL_CITED"


def test_a_stale_low_citation_survey_is_rejected() -> None:
    d = type_filter(
        _paper(title="A Survey of Expert Systems", citation_count=12, year=2016), cfg, AS_OF
    )
    assert (d.outcome, d.reason_code) == (Outcome.REJECT, "LOW_CITE_SURVEY")


def test_the_survey_thresholds_come_from_config() -> None:
    policy = cfg.paper_type_policy["SURVEY"]
    assert policy.citation_count_min == 200
    assert policy.or_citations_per_year_min == 40


def test_a_review_publication_type_triggers_the_survey_policy() -> None:
    """Not every survey says so in its title."""
    d = type_filter(
        _paper(title="Transformers in Vision", publication_types=("Review",), citation_count=3),
        cfg,
        AS_OF,
    )
    assert d.reason_code == "LOW_CITE_SURVEY"


def test_survey_as_a_mid_title_word_is_not_a_survey() -> None:
    """The regex is anchored: "We Survey the Landscape" is not a survey paper."""
    d = type_filter(_paper(title="Learning to Survey Unknown Terrain"), cfg, AS_OF)
    assert d.outcome is Outcome.ACCEPT


def test_a_survey_with_an_unknown_year_is_not_a_division_by_zero() -> None:
    d = type_filter(_paper(title="A Survey of Things", citation_count=5, year=None), cfg, AS_OF)
    assert d.outcome is Outcome.REJECT


# -- benchmark: quarantine, never reject -------------------------------------


@pytest.mark.parametrize(
    "title",
    ["GLUE: A Benchmark for NLU", "A New Dataset for QA", "HELM Test Suite", "A Testsuite for RL"],
)
def test_benchmark_titles_are_quarantined_not_rejected(title: str) -> None:
    """
    PLAN.md: soft only -- "Benchmarking LLM reasoning" may be a real
    contribution, so this holds for review rather than discarding.
    """
    d = type_filter(_paper(title=title), cfg, AS_OF)
    assert (d.outcome, d.reason_code) == (Outcome.QUARANTINE, "MAYBE_BENCHMARK")


def test_an_explicit_dataset_type_still_beats_a_benchmark_title() -> None:
    """Stage order: the hard signal wins over the soft one."""
    d = type_filter(
        _paper(title="A Benchmark for Vision", publication_types=("Dataset",)), cfg, AS_OF
    )
    assert d.reason_code == "IS_DATASET"


def test_every_type_branch_has_a_distinct_reason_code() -> None:
    codes = {
        type_filter(p, cfg, AS_OF).reason_code
        for p in (
            _paper(publication_types=("Dataset",)),
            _paper(title="A Survey of X", citation_count=5, year=2016),
            _paper(title="A Survey of X", citation_count=9000),
            _paper(title="A Benchmark for X"),
            _paper(title="Ordinary Paper"),
        )
    }
    assert codes == {
        "IS_DATASET",
        "LOW_CITE_SURVEY",
        "SURVEY_WELL_CITED",
        "MAYBE_BENCHMARK",
        "OK",
    }


# ==========================================================================
# STAGE 2 -- TOPIC, the a->f fallback chain
# ==========================================================================

# -- (a) primary category wins ----------------------------------------------


def test_a_core_primary_category_passes() -> None:
    d = topic_filter(_paper(primary_arxiv_category="cs.CL"), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.ACCEPT, "CAT_PRIMARY_CORE")


def test_an_applied_primary_category_is_rejected() -> None:
    d = topic_filter(_paper(primary_arxiv_category="cs.CV"), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.REJECT, "CAT_PRIMARY_APPLIED")


def test_cs_lg_cross_listed_to_cs_cv_MUST_ACCEPT() -> None:
    """
    BUILD.md names this explicitly. Batch Normalization is primary cs.LG,
    cross-listed cs.CV. Rejecting on membership rather than primary would
    delete a large slice of core ML.
    """
    d = topic_filter(
        _paper(primary_arxiv_category="cs.LG", arxiv_categories=("cs.LG", "cs.CV")), cfg
    )
    assert (d.outcome, d.reason_code) == (Outcome.ACCEPT, "CAT_PRIMARY_CORE")


def test_cs_cv_cross_listed_to_cs_lg_still_rejects() -> None:
    """The mirror. Primary wins in both directions, not just the friendly one."""
    d = topic_filter(
        _paper(primary_arxiv_category="cs.CV", arxiv_categories=("cs.CV", "cs.LG")), cfg
    )
    assert (d.outcome, d.reason_code) == (Outcome.REJECT, "CAT_PRIMARY_APPLIED")


def test_a_borderline_primary_category_is_quarantined() -> None:
    d = topic_filter(_paper(primary_arxiv_category="cs.IR"), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.QUARANTINE, "CAT_PRIMARY_BORDERLINE")


def test_an_unlisted_primary_category_is_quarantined() -> None:
    d = topic_filter(_paper(primary_arxiv_category="cs.GT"), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.QUARANTINE, "CAT_NOT_ALLOWED")


@pytest.mark.parametrize(
    "category", ["q-bio.NC", "q-fin.ST", "eess.IV", "physics.comp-ph", "econ.EM", "astro-ph.GA"]
)
def test_wildcard_deny_families_are_matched_by_prefix(category: str) -> None:
    """APPLIED_DENY carries `q-bio.*` style entries; literal matching misses all of them."""
    d = topic_filter(_paper(primary_arxiv_category=category), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.REJECT, "CAT_PRIMARY_APPLIED")


def test_a_wildcard_prefix_does_not_over_match() -> None:
    """`physics.*` must not swallow a category that merely starts with the letters."""
    assert topic_filter(_paper(primary_arxiv_category="cs.LG"), cfg).outcome is Outcome.ACCEPT


# -- (b) no primary, but a core cross-listing --------------------------------


def test_a_core_cross_listing_passes_at_lower_confidence() -> None:
    """Newer than the snapshot: categories known, primary not resolved."""
    d = topic_filter(_paper(arxiv_categories=("cs.CV", "cs.LG")), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.ACCEPT, "CAT_CROSS_CORE")


# -- (c) venue fallback ------------------------------------------------------


@pytest.mark.parametrize(
    "venue",
    [
        "Neural Information Processing Systems",
        "International Conference on Machine Learning",
        "International Conference on Learning Representations",
        "Annual Meeting of the Association for Computational Linguistics",
        "Journal of Machine Learning Research",
        "NeurIPS",
        "ICML",
    ],
)
def test_a_core_venue_passes_when_there_is_no_arxiv_data(venue: str) -> None:
    """
    S2 returns expanded venue names, not the acronyms in CORE_VENUES. Exact
    matching would make this whole branch dead code -- silently, by pushing
    every venue-only paper into QUARANTINE.
    """
    d = topic_filter(_paper(venue=venue), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.ACCEPT, "VENUE_CORE")


def test_naacl_does_not_match_on_the_acl_substring() -> None:
    """
    "ACL" appears inside "NAACL" and "EACL". Substring matching on acronyms
    would be right by accident here and wrong elsewhere, so matching is on word
    boundaries.
    """
    d = topic_filter(_paper(venue="Some Journal of Oracle Databases"), cfg)
    assert d.reason_code != "VENUE_CORE"


def test_a_non_core_venue_does_not_pass_on_venue() -> None:
    d = topic_filter(_paper(venue="Journal of Clinical Radiology"), cfg)
    assert d.reason_code != "VENUE_CORE"


# -- (d), (e), (f) s2_fields fallbacks ---------------------------------------


def test_computer_science_alone_is_quarantined() -> None:
    """Too coarse to separate cs.CL from cs.CV, so it decides nothing."""
    d = topic_filter(_paper(s2_fields=("Computer Science",)), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.QUARANTINE, "FIELD_CS_ONLY")


@pytest.mark.parametrize("field", ["Medicine", "Biology", "Chemistry", "Economics"])
def test_a_non_cs_field_without_computer_science_is_rejected(field: str) -> None:
    d = topic_filter(_paper(s2_fields=(field,)), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.REJECT, "FIELD_NON_CS")


def test_medicine_with_computer_science_is_not_auto_rejected() -> None:
    """
    A CS paper tagged Medicine is the applied-AI case, and PLAN.md is emphatic
    that stage 3 must never auto-reject. Quarantine, so it stays reviewable.
    """
    d = topic_filter(_paper(s2_fields=("Computer Science", "Medicine")), cfg)
    assert d.outcome is not Outcome.REJECT


def test_a_paper_with_no_signal_at_all_is_quarantined() -> None:
    d = topic_filter(_paper(), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.QUARANTINE, "NO_SIGNAL")


def test_the_chain_stops_at_the_first_hit() -> None:
    """A core primary category is not overridden by a non-CS s2_field."""
    d = topic_filter(_paper(primary_arxiv_category="cs.LG", s2_fields=("Medicine",)), cfg)
    assert d.reason_code == "CAT_PRIMARY_CORE"


def test_every_topic_branch_has_a_distinct_reason_code() -> None:
    codes = {
        topic_filter(p, cfg).reason_code
        for p in (
            _paper(primary_arxiv_category="cs.CL"),
            _paper(primary_arxiv_category="cs.CV"),
            _paper(primary_arxiv_category="cs.IR"),
            _paper(primary_arxiv_category="cs.GT"),
            _paper(arxiv_categories=("cs.CV", "cs.LG")),
            _paper(venue="NeurIPS"),
            _paper(s2_fields=("Computer Science",)),
            _paper(s2_fields=("Medicine",)),
            _paper(),
        )
    }
    assert len(codes) == 9


# ==========================================================================
# The R0.9 fixtures, by their real categories
# ==========================================================================


@pytest.mark.parametrize(
    ("name", "primary", "expected_outcome", "expected_code"),
    [
        ("BERT", "cs.CL", Outcome.ACCEPT, "CAT_PRIMARY_CORE"),
        ("Attention Is All You Need", "cs.CL", Outcome.ACCEPT, "CAT_PRIMARY_CORE"),
        ("word2vec", "cs.CL", Outcome.ACCEPT, "CAT_PRIMARY_CORE"),
        ("Adam", "cs.LG", Outcome.ACCEPT, "CAT_PRIMARY_CORE"),
        ("GPT-3", "cs.CL", Outcome.ACCEPT, "CAT_PRIMARY_CORE"),
        ("ResNet", "cs.CV", Outcome.REJECT, "CAT_PRIMARY_APPLIED"),
    ],
)
def test_fixture_paper_topic_verdict(
    name: str, primary: str, expected_outcome: Outcome, expected_code: str
) -> None:
    d = topic_filter(_paper(title=name, primary_arxiv_category=primary), cfg)
    assert (d.outcome, d.reason_code) == (expected_outcome, expected_code)


def test_batch_normalization_the_fixture_that_must_pass() -> None:
    """
    The R0.9 fixture whose entire purpose is proving the deny list does not
    overreach. Primary cs.LG, cross-listed cs.CV, confirmed live at R0.11.
    """
    d = topic_filter(
        _paper(
            title="Batch Normalization",
            primary_arxiv_category="cs.LG",
            arxiv_categories=("cs.LG", "cs.CV"),
        ),
        cfg,
    )
    assert d.outcome is Outcome.ACCEPT


# --------------------------------------------------------------------------
# Categories that used to fall through to CAT_NOT_ALLOWED
# --------------------------------------------------------------------------
#
# A category in none of CORE_ALLOW, BORDERLINE or APPLIED_DENY quarantines as
# CAT_NOT_ALLOWED, which is honest but uninformative: it means the config has
# no opinion, not that the paper was judged. Two groups came out of the review
# drawer that way and now have one.


def test_programming_languages_is_core() -> None:
    """
    "Program Synthesis with Large Language Models" and AlphaCode are both
    `cs.PL`, and both are LLM-reasoning papers filed under programming
    languages. They arrived as CAT_NOT_ALLOWED -- refused for want of anyone
    having classified the category, next to the ReAct and Reflexion work they
    belong beside.
    """
    d = topic_filter(_paper(primary_arxiv_category="cs.PL"), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.ACCEPT, "CAT_PRIMARY_CORE")


@pytest.mark.parametrize("category", ["cs.DC", "cs.CE", "cs.MS"])
def test_the_systems_adjacent_categories_are_borderline(category: str) -> None:
    """
    Distributed computing, computational engineering and mathematical software
    sit next to ML systems work often enough to be worth a look, and not often
    enough to admit unread. QUARANTINE says "we are unsure" where
    CAT_NOT_ALLOWED said "nobody decided".
    """
    d = topic_filter(_paper(primary_arxiv_category=category), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.QUARANTINE, "CAT_PRIMARY_BORDERLINE")


def test_an_unclassified_category_still_quarantines() -> None:
    """
    The fall-through stays, and stays distinguishable. `quant-ph` was left
    unlisted deliberately, so it must keep reading as "no opinion" rather than
    quietly acquiring one.
    """
    d = topic_filter(_paper(primary_arxiv_category="quant-ph"), cfg)
    assert (d.outcome, d.reason_code) == (Outcome.QUARANTINE, "CAT_NOT_ALLOWED")
