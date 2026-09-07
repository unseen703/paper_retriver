"""Domain models. Import from here, not from the submodule."""

from app.models.domain import (
    DISCOVERED_VIA,
    Author,
    CandidateFeatures,
    CrawlState,
    Edge,
    FilterDecision,
    GraphNode,
    InteractionEvent,
    Outcome,
    Paper,
    PaperStub,
    PaperType,
    ScoredCandidate,
    VenueTier,
    normalize_title,
)

__all__ = [
    "DISCOVERED_VIA",
    "Author",
    "CandidateFeatures",
    "CrawlState",
    "Edge",
    "FilterDecision",
    "GraphNode",
    "InteractionEvent",
    "Outcome",
    "Paper",
    "PaperStub",
    "PaperType",
    "ScoredCandidate",
    "VenueTier",
    "normalize_title",
]
