"""
Admitting computational biochemistry by keyword.

Journey:

    As someone working on reaction prediction, I want the enzymatic side of the
    field -- metabolic routes, catalytic sites, enzyme function -- in my graph,
    because it is the same modelling problem with a protein catalyst.

The second corridor through STAGE 2. `test_reaction_ml_rescue.py` opened the
first, for small-molecule reaction chemistry; this one covers the biological
catalyst. Both run before the category rules, for the same reason: these papers
live under `q-bio.*` and `physics.chem-ph`, which `APPLIED_DENY` refuses
outright, so a check that runs afterwards has nothing left to save.

**Its own reason code, deliberately.** `BIOCHEM_ML`, not `REACTION_ML`. The
review drawer groups by reason code, so a separate code is what makes it
possible to see how many papers arrived through each corridor -- and to close
one of them without touching the other if its precision turns out bad. Sharing
a code would mean tuning two lists blind.

**This corridor is wider than the chemistry one, by explicit decision.** The
binding-site and docking terms admit structure-based drug discovery, which is a
large field. That was chosen knowingly; the tests below pin what it costs so
the widening stays visible rather than being discovered later from a graph full
of papers nobody wanted.

**The protein guard still holds.** `FIELD_NON_CS` and `APPLIED_DENY` exist to
keep general structural biology out, and none of these keywords is generic
protein vocabulary -- "protein structure prediction" is still refused. The tests
at the bottom pin that boundary, because it is the one this file is most likely
to erode.
"""

from __future__ import annotations

import pytest

from app.config import FiltersConfig, load_filters
from app.models import Outcome, Paper
from app.services.filters.topic_filter import _is_biochem_ml, _is_reaction_ml, topic_filter


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
# Subfield 1 — metabolic reaction prediction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Metabolic pathway prediction with graph neural networks",
        "Predicting metabolic pathways from genome annotations",
        "A metabolic reaction database for machine learning",
        "Retrobiosynthesis planning with transformers",
        "Biosynthetic pathway discovery in actinomycetes",
        "Metabolite prediction for untargeted metabolomics",
        "Biotransformation prediction for xenobiotics",
    ],
)
def test_metabolic_reaction_papers_are_admitted(title: str, cfg: FiltersConfig) -> None:
    """
    `q-bio.BM` and `q-bio.MN` are on APPLIED_DENY. These are the same modelling
    problem as small-molecule retrosynthesis, with an enzyme doing the work.
    """
    decision = topic_filter(_paper(title, "q-bio.MN"), cfg)
    assert decision.outcome is Outcome.ACCEPT, decision.reason_code
    assert decision.reason_code == "BIOCHEM_ML"


# --------------------------------------------------------------------------
# Subfield 2 — enzyme reactive-site prediction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Active site prediction with equivariant networks",
        "Catalytic site prediction from sequence alone",
        "Catalytic residue identification in uncharacterized enzymes",
        "Learning the reactive site of an enzymatic transformation",
        "Enzyme active site geometry from language models",
    ],
)
def test_reactive_site_papers_are_admitted(title: str, cfg: FiltersConfig) -> None:
    decision = topic_filter(_paper(title, "q-bio.BM"), cfg)
    assert decision.outcome is Outcome.ACCEPT, decision.reason_code
    assert decision.reason_code == "BIOCHEM_ML"


def test_catalytic_site_needs_the_task_word(cfg: FiltersConfig) -> None:
    """
    `catalytic site prediction`, not bare `catalytic site`. A DFT surface-science
    paper names the catalytic site of a zeolite and has nothing to do with this
    tool; the sibling test file already pins that DFT stays out, and a bare term
    here would quietly undo it.
    """
    decision = topic_filter(
        _paper("Density functional theory of the catalytic site in zeolites", "physics.chem-ph"),
        cfg,
    )
    assert decision.outcome is not Outcome.ACCEPT


# --------------------------------------------------------------------------
# Subfield 3 — enzyme function prediction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Enzyme function prediction with contrastive learning",
        "Predicting enzyme function from sequence embeddings",
        "EC number prediction at proteome scale",
        "Enzyme commission classification with deep learning",
        "Enzyme annotation for metagenomic assemblies",
        "Substrate specificity prediction for glycosyltransferases",
        "Modelling enzymatic reaction kinetics with neural networks",
    ],
)
def test_enzyme_function_papers_are_admitted(title: str, cfg: FiltersConfig) -> None:
    decision = topic_filter(_paper(title, "q-bio.BM"), cfg)
    assert decision.outcome is Outcome.ACCEPT, decision.reason_code
    assert decision.reason_code == "BIOCHEM_ML"


# --------------------------------------------------------------------------
# The two widenings chosen explicitly — binding sites, and protein function
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Binding site prediction with geometric deep learning",
        "Binding affinity prediction for protein-ligand complexes",
        "Protein-ligand interaction modelling at scale",
        "Molecular docking with learned scoring functions",
        "Ligand binding site detection in apo structures",
    ],
)
def test_binding_site_and_docking_are_admitted(title: str, cfg: FiltersConfig) -> None:
    """
    **The widest rule in this file, and it was chosen knowingly.** These terms
    admit structure-based drug discovery, which is a large literature and only
    partly about reactions.

    It has its own reason code precisely so the drawer can show what it costs.
    If the graph starts filling with docking papers, `BIOCHEM_ML` is the tab
    that will say so, and these five lines are what to delete.
    """
    decision = topic_filter(_paper(title, "q-bio.BM"), cfg)
    assert decision.outcome is Outcome.ACCEPT, decision.reason_code
    assert decision.reason_code == "BIOCHEM_ML"


@pytest.mark.parametrize(
    "title",
    [
        "Protein function prediction with language models",
        "GO term prediction from structure",
        "Gene ontology prediction for uncharacterized proteins",
    ],
)
def test_protein_function_prediction_is_admitted(title: str, cfg: FiltersConfig) -> None:
    """
    The superset of enzyme function prediction. Admitted by explicit choice --
    the task is the same, and the enzyme case is hard to separate from it
    without reading the paper.
    """
    decision = topic_filter(_paper(title, "q-bio.BM"), cfg)
    assert decision.outcome is Outcome.ACCEPT, decision.reason_code
    assert decision.reason_code == "BIOCHEM_ML"


# --------------------------------------------------------------------------
# What stays out — the half that matters more
# --------------------------------------------------------------------------


def test_protein_structure_prediction_is_still_refused(cfg: FiltersConfig) -> None:
    """
    **The boundary this file is most likely to erode.** Structure prediction is
    not function prediction and not a reaction; admitting it would pull in the
    entire AlphaFold-adjacent literature, which is not what this tool is for.

    `test_reaction_ml_rescue.py` asserts the same title stays out. Two files now
    depend on it, which is the point -- it is the load-bearing edge of both
    corridors.
    """
    decision = topic_filter(_paper("Protein structure prediction from sequence", "q-bio.BM"), cfg)
    assert decision.outcome is not Outcome.ACCEPT


@pytest.mark.parametrize(
    "title",
    [
        "Genome assembly from long reads",
        "Single-cell RNA sequencing of the mouse cortex",
        "Phylogenetic tree inference under model misspecification",
        "CRISPR screening in primary T cells",
        "Population genetics of adaptive introgression",
        "Cryo-EM structure of the ribosome at 2.1 angstroms",
    ],
)
def test_ordinary_biology_is_still_denied(title: str, cfg: FiltersConfig) -> None:
    """
    The rescue is a corridor, not a door. `q-bio.*` is on APPLIED_DENY for a
    reason and the vast majority of it must stay refused.
    """
    decision = topic_filter(_paper(title, "q-bio.GN"), cfg)
    assert decision.outcome is not Outcome.ACCEPT


def test_the_word_enzyme_alone_does_not_admit_a_paper(cfg: FiltersConfig) -> None:
    """
    "Enzyme" is a noun, not a field. A crystallography paper naming one is still
    a crystallography paper.
    """
    decision = topic_filter(_paper("Crystal structure of a bacterial enzyme", "q-bio.BM"), cfg)
    assert decision.outcome is not Outcome.ACCEPT


def test_the_word_pathway_alone_does_not_admit_a_paper(cfg: FiltersConfig) -> None:
    """
    Signalling pathways, neural pathways, clinical pathways. Only the metabolic
    sense belongs here.
    """
    decision = topic_filter(_paper("Signalling pathway activation in cancer", "q-bio.CB"), cfg)
    assert decision.outcome is not Outcome.ACCEPT


def test_ordinary_chemistry_is_still_denied(cfg: FiltersConfig) -> None:
    """This file must not re-open what the chemistry corridor deliberately shut."""
    decision = topic_filter(
        _paper("NMR spectroscopy of protein side chains", "physics.chem-ph"), cfg
    )
    assert decision.outcome is not Outcome.ACCEPT


# --------------------------------------------------------------------------
# The rescue must not disturb what already worked
# --------------------------------------------------------------------------


def test_a_core_cs_paper_is_still_accepted_for_its_own_reason(cfg: FiltersConfig) -> None:
    """
    Both corridors run before the category rules, so neither may shadow the
    ordinary path. A cs.LG paper is still CAT_PRIMARY_CORE.
    """
    decision = topic_filter(_paper("Attention is all you need", "cs.LG"), cfg)
    assert decision.outcome is Outcome.ACCEPT
    assert decision.reason_code == "CAT_PRIMARY_CORE"


def test_a_biochem_paper_in_a_core_category_keeps_the_core_reason(cfg: FiltersConfig) -> None:
    """
    A cs.LG enzyme-function paper was already admitted correctly for its
    category. Relabelling it BIOCHEM_ML would rewrite history in the drawer.
    """
    decision = topic_filter(_paper("Enzyme function prediction with GNNs", "cs.LG"), cfg)
    assert decision.reason_code == "CAT_PRIMARY_CORE"


def test_the_chemistry_corridor_still_reports_its_own_code(cfg: FiltersConfig) -> None:
    """
    Two corridors, two codes. A retrosynthesis paper must not start arriving as
    BIOCHEM_ML just because this file exists -- that would make both tabs
    meaningless at once.
    """
    decision = topic_filter(_paper("Retrosynthesis prediction with GNNs", "physics.chem-ph"), cfg)
    assert decision.outcome is Outcome.ACCEPT
    assert decision.reason_code == "REACTION_ML"


def test_an_applied_cv_paper_is_still_rejected(cfg: FiltersConfig) -> None:
    decision = topic_filter(_paper("Object detection in aerial imagery", "cs.CV"), cfg)
    assert decision.outcome is Outcome.REJECT


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_the_keywords_are_configuration_not_code(cfg: FiltersConfig) -> None:
    """
    M7: vocabularies live in `config/filters.yaml` under a `config_version`, so
    a change is reproducible and stamped into every decision it caused. The
    version is a sha256 of the file's bytes, so editing the list re-stamps it
    without anyone having to remember to.
    """
    assert cfg.biochem_ml_keywords
    assert any("enzyme" in k for k in cfg.biochem_ml_keywords)


def test_the_two_corridors_are_separate_lists(cfg: FiltersConfig) -> None:
    """
    Separate lists, not one merged list, for the same reason they have separate
    reason codes: either can be narrowed or closed without disturbing the other.
    """
    assert not set(cfg.biochem_ml_keywords) & set(cfg.reaction_ml_keywords)


def test_when_both_corridors_match_biochemistry_wins(cfg: FiltersConfig) -> None:
    """
    **The ordering invariant, and it was found the hard way.**

    "Retrobiosynthesis planning" matches `retrobiosynth` in the biochemistry
    list *and* `synthesis planning` in the chemistry list. Both readings are
    defensible; the biological one is more specific, and a drawer is more useful
    when the narrower corridor claims the paper. A chemistry-first order labels
    it REACTION_ML on what is really an incidental substring.

    The two asserts below matter together: without the first, this test would
    still pass if someone removed `synthesis planning` from the chemistry list,
    and the ordering it is guarding would no longer be exercised at all.
    """
    title = "Retrobiosynthesis planning with transformers"
    assert _is_biochem_ml(_paper(title), cfg), "the overlap this test guards must be real"
    assert _is_reaction_ml(_paper(title), cfg), "the overlap this test guards must be real"

    decision = topic_filter(_paper(title, "q-bio.MN"), cfg)
    assert decision.reason_code == "BIOCHEM_ML"
