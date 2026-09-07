"""
venue_config.py
---------------
Tier-based venue scoring for CS/AI/ML/NLP research.

Tier 1 (score=1.0): Top-tier conferences and journals in the field.
Tier 2 (score=0.7): Strong second-tier venues.
Default (score=0.3): Any other named venue.
No venue (score=0.0): Unknown / missing venue.
"""

from __future__ import annotations

TIER1: frozenset[str] = frozenset({
    # Neural / ML
    "neurips", "nips",
    "advances in neural information processing systems",
    "icml", "international conference on machine learning",
    "iclr", "international conference on learning representations",
    # NLP
    "acl", "annual meeting of the association for computational linguistics",
    "emnlp", "empirical methods in natural language processing",
    "naacl", "north american chapter of the association for computational linguistics",
    "coling", "international conference on computational linguistics",
    "findings of the association for computational linguistics",
    # CV (multimodal LLM)
    "cvpr", "computer vision and pattern recognition",
    "eccv", "european conference on computer vision",
    "iccv", "international conference on computer vision",
    # AI / general
    "aaai", "association for the advancement of artificial intelligence",
    # Data / Web
    "kdd", "knowledge discovery and data mining",
    # Journals
    "jmlr", "journal of machine learning research",
    "tacl", "transactions of the association for computational linguistics",
    "ieee tpami", "ieee transactions on pattern analysis and machine intelligence",
})

TIER2: frozenset[str] = frozenset({
    "aistats", "artificial intelligence and statistics",
    "conll", "conference on computational natural language learning",
    "eacl", "european chapter of the association for computational linguistics",
    "ijcai", "international joint conference on artificial intelligence",
    "uai", "uncertainty in artificial intelligence",
    "colt", "computational learning theory",
    "recsys", "acm conference on recommender systems",
    "wsdm", "web search and data mining",
    "www", "world wide web conference",
    "sigir",
    "acm mm", "acm multimedia",
    "interspeech",
    "icassp",
    "corl", "conference on robot learning",
    "icdm", "ieee international conference on data mining",
    "ecir", "european conference on information retrieval",
})


def venue_score(venue: str | None) -> float:
    """Return a [0, 1] quality score for a venue string."""
    if not venue:
        return 0.0
    v = venue.lower().strip()
    if not v:
        return 0.0
    for token in TIER1:
        if token in v:
            return 1.0
    for token in TIER2:
        if token in v:
            return 0.7
    return 0.3
