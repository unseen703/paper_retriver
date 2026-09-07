"""
tests/test_domain_filter.py
-----------------------------
TDD tests for #3: a much stricter admission filter.

Only papers whose subject is CORE AI / data science are admitted. A paper
that merely APPLIES AI/LLMs as a tool inside another domain (chemistry,
medicine, finance, manufacturing, robotics, cybersecurity, ...) is
rejected even though S2 tags it "Computer Science".

Gates, applied in order:
  1. Broad field gate  -- existing is_relevant_paper() on fieldsOfStudy.
  2. Subdomain gate    -- the title/venue/abstract must hit at least one
                          CORE_AI_SUBDOMAIN term.
  3. Applied gate      -- reject if an APPLIED_DOMAIN term appears
                          (chemistry/medicine/finance/... signals).

User journey: As a user, I only want core AI/DS research in my network --
not "LLMs for drug discovery" or "deep learning for fraud detection",
which are other fields using AI as a tool.

Affected API: field_filter.py -- CORE_AI_SUBDOMAINS, APPLIED_DOMAIN_TERMS,
is_core_ai_paper(title, venue, abstract, field_of_study).

Callers: pytest only.
User verbatim: "while adding any paper to network apply the filter of the
domain and sub domain as dicussed above only filed directly realted to AI
and data scince should be here and no Applied paper even in this field.
check they should belong to provideed list of sub domain. then do keyword
matching the paper to check if its applied paper or not. ... [chemistry,
medicine, finance, manufacturing, robotics , cybersecurity, any other
domain which uses LLM/AI as tool] should not be added to network."
"""

from __future__ import annotations

import pytest


# ── the vocabularies themselves ──────────────────────────────────────────────

class TestVocabularies:

    def test_core_subdomains_defined(self):
        from field_filter import CORE_AI_SUBDOMAINS
        assert len(CORE_AI_SUBDOMAINS) >= 15

    def test_core_subdomains_cover_expected_areas(self):
        from field_filter import CORE_AI_SUBDOMAINS
        joined = " ".join(CORE_AI_SUBDOMAINS).lower()
        for area in ("language model", "reinforcement learning", "neural",
                     "transformer", "retrieval", "agent", "benchmark"):
            assert area in joined, f"expected a subdomain term covering {area!r}"

    def test_applied_domain_terms_cover_user_named_domains(self):
        from field_filter import APPLIED_DOMAIN_TERMS
        joined = " ".join(APPLIED_DOMAIN_TERMS).lower()
        for domain in ("chemistry", "medic", "financ", "manufactur",
                       "robot", "cybersecurity"):
            assert domain in joined, f"expected an applied term covering {domain!r}"

    def test_vocabularies_are_lowercase(self):
        """Matching is case-insensitive by lowercasing the haystack, so the
        needles must already be lowercase or they can never match."""
        from field_filter import CORE_AI_SUBDOMAINS, APPLIED_DOMAIN_TERMS
        for term in list(CORE_AI_SUBDOMAINS) + list(APPLIED_DOMAIN_TERMS):
            assert term == term.lower(), f"{term!r} must be lowercase"


# ── the subdomain gate ───────────────────────────────────────────────────────

class TestCoreSubdomainGate:

    def test_accepts_clear_core_ai_title(self):
        from field_filter import is_core_ai_paper
        assert is_core_ai_paper(
            title="Attention Is All You Need",
            venue="NeurIPS",
            abstract="We propose the Transformer, a new neural network architecture.",
            field_of_study=["Computer Science"],
        )

    def test_accepts_llm_agent_paper(self):
        from field_filter import is_core_ai_paper
        assert is_core_ai_paper(
            title="ReAct: Synergizing Reasoning and Acting in Language Models",
            venue="ICLR",
            abstract="We explore the use of large language models as agents.",
            field_of_study=["Computer Science"],
        )

    def test_rejects_paper_with_no_core_subdomain_signal(self):
        from field_filter import is_core_ai_paper
        assert not is_core_ai_paper(
            title="A Survey of Cellular Automata in Traffic Flow",
            venue="Some Journal",
            abstract="We survey traffic flow modelling approaches.",
            field_of_study=["Computer Science"],
        )

    def test_matches_subdomain_from_abstract_when_title_is_generic(self):
        from field_filter import is_core_ai_paper
        assert is_core_ai_paper(
            title="Scaling Up: A Study",
            venue="ArXiv",
            abstract="We train a large language model with reinforcement learning.",
            field_of_study=["Computer Science"],
        )

    @pytest.mark.parametrize("title,venue", [
        # Real references of "Attention Is All You Need" that an overly
        # narrow vocabulary wrongly rejected.
        ("Deep Residual Learning for Image Recognition", "CVPR"),
        ("Layer Normalization", "ArXiv"),
        ("Dropout: a simple way to prevent neural networks from overfitting", "JMLR"),
        ("Adam: A Method for Stochastic Optimization", "ICLR"),
        ("Long Short-Term Memory", "Neural Computation"),
        ("Batch Normalization: Accelerating Deep Network Training", "ICML"),
        ("Generative Adversarial Networks", "NeurIPS"),
        ("Auto-Encoding Variational Bayes", "ICLR"),
    ])
    def test_admits_foundational_ai_papers(self, title, venue):
        from field_filter import is_core_ai_paper
        assert is_core_ai_paper(title=title, venue=venue, abstract=None,
                                field_of_study=["Computer Science"]), \
            f"{title!r} is foundational AI work and must be admitted"

    def test_core_ai_venue_alone_is_a_sufficient_signal(self):
        """Publication at a top AI venue is itself strong evidence the work
        is core AI, even when the title uses no recognised term."""
        from field_filter import is_core_ai_paper
        assert is_core_ai_paper(
            title="Layer Normalization", venue="NeurIPS",
            abstract=None, field_of_study=["Computer Science"],
        )

    def test_core_venue_does_not_rescue_an_applied_paper(self):
        """A NeurIPS paper about drug discovery is still applied work."""
        from field_filter import is_core_ai_paper
        assert not is_core_ai_paper(
            title="Molecular Property Prediction for Drug Discovery",
            venue="NeurIPS", abstract=None, field_of_study=["Computer Science"],
        )


# ── the applied-domain gate ──────────────────────────────────────────────────

class TestAppliedDomainRejection:

    @pytest.mark.parametrize("title", [
        "Large Language Models for Drug Discovery",
        "Deep Learning for Cancer Diagnosis from Radiology Images",
        "LLM Agents for Algorithmic Trading and Portfolio Management",
        "Reinforcement Learning for Robotic Grasping in Manufacturing",
        "Transformer Models for Network Intrusion Detection",
        "Neural Networks for Molecular Property Prediction in Chemistry",
    ])
    def test_rejects_applied_papers_even_with_core_ai_terms(self, title):
        """These all contain core AI terms, but the subject is another
        field using AI as a tool -- they must be rejected."""
        from field_filter import is_core_ai_paper
        assert not is_core_ai_paper(
            title=title,
            venue="ArXiv",
            abstract="",
            field_of_study=["Computer Science"],
        ), f"{title!r} is an applied paper and must be rejected"

    def test_rejects_applied_signal_found_only_in_abstract(self):
        from field_filter import is_core_ai_paper
        assert not is_core_ai_paper(
            title="Fine-Tuning Large Language Models",
            venue="ArXiv",
            abstract="We fine-tune an LLM to assist clinical diagnosis of patients in hospitals.",
            field_of_study=["Computer Science"],
        )

    def test_rejects_applied_signal_found_only_in_venue(self):
        from field_filter import is_core_ai_paper
        assert not is_core_ai_paper(
            title="Deep Learning Architectures",
            venue="Journal of Medicinal Chemistry",
            abstract="",
            field_of_study=["Computer Science"],
        )

    def test_applied_gate_beats_subdomain_gate(self):
        """A paper can hit MANY core terms and still be applied -- the
        applied gate must win."""
        from field_filter import is_core_ai_paper
        assert not is_core_ai_paper(
            title="Transformer Neural Networks and Reinforcement Learning for Cancer Treatment Planning",
            venue="NeurIPS",
            abstract="A deep learning benchmark for oncology.",
            field_of_study=["Computer Science"],
        )


# ── interaction with the existing broad-field gate ───────────────────────────

class TestBroadFieldGateStillApplies:

    def test_rejects_biology_only_paper_regardless_of_keywords(self):
        from field_filter import is_core_ai_paper
        assert not is_core_ai_paper(
            title="Neural Network Models of Protein Folding",
            venue="Nature",
            abstract="",
            field_of_study=["Biology"],
        )

    def test_missing_fields_falls_back_to_keyword_gates(self):
        """Unknown fieldsOfStudy shouldn't auto-admit anymore -- the
        keyword gates still have to pass."""
        from field_filter import is_core_ai_paper
        assert is_core_ai_paper(
            title="Sparse Attention in Transformer Language Models",
            venue="", abstract="", field_of_study=None,
        )
        assert not is_core_ai_paper(
            title="A History of the Steam Engine",
            venue="", abstract="", field_of_study=None,
        )


# ── robustness ───────────────────────────────────────────────────────────────

class TestFilterRobustness:

    def test_handles_all_none_inputs_without_raising(self):
        from field_filter import is_core_ai_paper
        assert is_core_ai_paper(title=None, venue=None, abstract=None,
                                field_of_study=None) is False

    def test_case_insensitive_matching(self):
        from field_filter import is_core_ai_paper
        assert is_core_ai_paper(
            title="LARGE LANGUAGE MODELS AND TRANSFORMERS",
            venue=None, abstract=None, field_of_study=["Computer Science"],
        )

    def test_original_is_relevant_paper_still_exported(self):
        """graph_expander/exploration still import it; don't break them."""
        from field_filter import is_relevant_paper
        assert is_relevant_paper(["Computer Science"]) is True
        assert is_relevant_paper(["Biology"]) is False
