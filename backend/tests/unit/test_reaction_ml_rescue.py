"""
Admitting ML-for-reaction-chemistry by keyword.

Journey:

    As someone whose interest spans machine learning and reaction chemistry, I
    want retrosynthesis and reactivity-prediction papers in my graph, without
    also admitting all of physical chemistry.

The corpus already holds 41 chemistry and biology papers, fetched as
references, and none of them were ever drawn: `filters.yaml` lists
`physics.*` and `q-bio.*` under `APPLIED_DENY`, so they were refused with
`CAT_PRIMARY_APPLIED` and `FIELD_NON_CS`.

**The wanted set is an intersection, not a category.** "Every physics.chem-ph
paper" is far too wide -- most of it is spectroscopy and electronic structure,
nothing to do with this tool. "Only cs.LG" is too narrow, because the
retrosynthesis literature publishes into chemistry venues. What identifies the
field is its *vocabulary*: retrosynthesis, reaction prediction, reactivity
prediction. Those terms are themselves the intersection -- a paper about
retrosynthesis is essentially always a machine-learning paper.

So the rescue runs **before** the category rules rather than after. A
`physics.chem-ph` paper is denied by category, and the check that could save it
has to happen before the denial rather than trying to undo it.

**Precision is the risk, not recall.** A keyword list that is too eager admits
all of computational chemistry through a door meant for a corridor of it, and
the symptom is a graph slowly filling with papers nobody wanted. Half of these
tests are about what must *stay out*.
"""

from __future__ import annotations

import pytest

from app.config import FiltersConfig, load_filters
from app.models import Outcome, Paper
from app.services.filters.topic_filter import topic_filter


@pytest.fixture
def cfg() -> FiltersConfig:
    """The real config. These rules are only meaningful as shipped."""
    return load_filters()


def _paper(title: str, category: str | None = None, fields: list[str] | None = None) -> Paper:
    return Paper(
        s2_paper_id="x",
        title=title,
        first_seen_at="2026-01-01",
        year=2023,
        primary_arxiv_category=category,
        arxiv_categories=(category,) if category else (),
        s2_fields=tuple(fields or []),
    )


# --------------------------------------------------------------------------
# What comes in
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Retrosynthesis prediction with graph neural networks",
        "A retrosynthetic planning model for organic synthesis",
        "Reactivity prediction for organometallic catalysis",
        "Predicting reaction outcomes with transformers",
        "Reaction yield prediction from high-throughput experimentation",
        "Computer-aided synthesis planning at scale",
    ],
)
def test_reaction_ml_papers_are_admitted_despite_a_denied_category(
    title: str, cfg: FiltersConfig
) -> None:
    """
    `physics.chem-ph` is on APPLIED_DENY. These papers are the reason the
    rescue exists: the category says chemistry, the title says machine learning
    about reactions, and the second is the more specific fact.
    """
    decision = topic_filter(_paper(title, "physics.chem-ph"), cfg)
    assert decision.outcome is Outcome.ACCEPT, decision.reason_code
    assert decision.reason_code == "REACTION_ML"


def test_the_match_is_case_insensitive(cfg: FiltersConfig) -> None:
    decision = topic_filter(_paper("RETROSYNTHESIS WITH DEEP LEARNING", "physics.chem-ph"), cfg)
    assert decision.outcome is Outcome.ACCEPT


def test_a_reaction_paper_with_no_arxiv_category_is_admitted(cfg: FiltersConfig) -> None:
    """
    The chemistry literature is not all on arXiv. A paper with no category and
    no CS field would otherwise fall through to NO_SIGNAL and be quarantined.
    """
    decision = topic_filter(_paper("Retrosynthesis via template-free translation"), cfg)
    assert decision.outcome is Outcome.ACCEPT


def test_a_reaction_paper_tagged_only_chemistry_is_admitted(cfg: FiltersConfig) -> None:
    """
    `FIELD_NON_CS` rejects a paper S2 labels chemistry with no CS tag. That is
    right for chemistry in general and wrong for this corner of it.
    """
    decision = topic_filter(
        _paper("Machine learning for reaction prediction", None, ["Chemistry"]), cfg
    )
    assert decision.outcome is Outcome.ACCEPT


# --------------------------------------------------------------------------
# What stays out -- the half that matters more
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Density functional theory for transition metal oxides",
        "NMR spectroscopy of protein side chains",
        "A new synthesis of substituted pyridines",
        "Crystal structure of a copper complex",
        "Thermodynamics of solvation in ionic liquids",
    ],
)
def test_ordinary_chemistry_is_still_denied(title: str, cfg: FiltersConfig) -> None:
    """
    The rescue is a corridor, not a door. Admitting all of physical chemistry
    would fill the graph with papers nobody asked for, and the symptom would be
    slow and hard to attribute.
    """
    decision = topic_filter(_paper(title, "physics.chem-ph"), cfg)
    assert decision.outcome is not Outcome.ACCEPT


def test_the_word_reaction_alone_does_not_admit_a_paper(cfg: FiltersConfig) -> None:
    """
    "Reaction" is a common word -- reaction time, chain reaction, reaction to a
    policy. Matching it alone would let the list leak far outside chemistry.
    """
    decision = topic_filter(_paper("Human reaction time in visual search", "q-bio.NC"), cfg)
    assert decision.outcome is not Outcome.ACCEPT


def test_a_biology_paper_is_not_admitted_by_the_word_prediction(cfg: FiltersConfig) -> None:
    decision = topic_filter(_paper("Protein structure prediction from sequence", "q-bio.BM"), cfg)
    assert decision.outcome is not Outcome.ACCEPT


# --------------------------------------------------------------------------
# The rescue must not disturb what already worked
# --------------------------------------------------------------------------


def test_a_core_cs_paper_is_still_accepted_for_its_own_reason(cfg: FiltersConfig) -> None:
    """
    The rescue runs first, so it must not shadow the ordinary path. A cs.LG
    paper should still be accepted as CAT_PRIMARY_CORE -- the reason code is
    what the review drawer groups by, and re-labelling every paper REACTION_ML
    would make that tab meaningless.
    """
    decision = topic_filter(_paper("Attention is all you need", "cs.LG"), cfg)
    assert decision.outcome is Outcome.ACCEPT
    assert decision.reason_code == "CAT_PRIMARY_CORE"


def test_an_applied_cv_paper_is_still_rejected(cfg: FiltersConfig) -> None:
    """cs.CV is on APPLIED_DENY and nothing here should have changed that."""
    decision = topic_filter(_paper("Object detection in aerial imagery", "cs.CV"), cfg)
    assert decision.outcome is Outcome.REJECT


def test_a_reaction_ml_paper_in_a_core_category_keeps_the_core_reason(
    cfg: FiltersConfig,
) -> None:
    """
    A cs.LG retrosynthesis paper was already being admitted, correctly, for its
    category. It should not start arriving under a different label just because
    the rescue now exists -- that would silently rewrite history in the drawer.
    """
    decision = topic_filter(_paper("Retrosynthesis with graph networks", "cs.LG"), cfg)
    assert decision.reason_code == "CAT_PRIMARY_CORE"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_the_keywords_are_configuration_not_code(cfg: FiltersConfig) -> None:
    """
    M7: thresholds and vocabularies live in `config/filters.yaml` with a
    `config_version`, so a change is reproducible and stamped into every
    decision it caused.
    """
    assert cfg.reaction_ml_keywords
    assert any("retrosynth" in k for k in cfg.reaction_ml_keywords)


def test_the_chemistry_terms_left_the_applied_keywords(cfg: FiltersConfig) -> None:
    """
    `applied_keywords` used to carry 'molecul', 'protein folding' and 'drug
    discovery' -- the exact vocabulary `reaction_ml_keywords` now admits. The
    two lists would have pulled in opposite directions the moment the APPLIED
    stage PLAN.md sketches was actually built.

    The rest of the list stays. Clinical, financial and agricultural
    applications are a different question from chemistry, and removing those
    guards is not what was asked for.
    """
    joined = " ".join(cfg.applied_keywords)
    assert "molecul" not in joined
    assert "protein folding" not in joined
    assert "drug discovery" not in joined
    assert "clinical" in cfg.applied_keywords, "the non-chemistry guards stay"
    assert "credit risk" in cfg.applied_keywords
