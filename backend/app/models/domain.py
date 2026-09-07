"""
Domain dataclasses. No ORM, no DB, no HTTP.

These are the objects that cross layer boundaries, so they are frozen: a filter
cannot mutate the Paper it is judging, and a ranker cannot mutate the features
it scored. Field names mirror PLAN.md section C so the repo layer is a
transliteration rather than a translation.

Every field S2 can omit is Optional (CLAUDE.md rule 6). S2 leaves `year`,
`abstract` and `venue` null constantly; a required field there would turn a
degraded score into a crash mid-expansion.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    ACCEPT = "ACCEPT"
    QUARANTINE = "QUARANTINE"
    REJECT = "REJECT"


class CrawlState(StrEnum):
    """How much of a paper we actually hold. See PLAN.md section C design notes."""

    STUB = "STUB"  # title + id only, from a nested edge fetch
    METADATA = "METADATA"  # full record fetched
    REFS_DONE = "REFS_DONE"
    CITES_DONE = "CITES_DONE"
    EXPANDED = "EXPANDED"


class PaperType(StrEnum):
    RESEARCH = "RESEARCH"
    SURVEY = "SURVEY"
    DATASET = "DATASET"
    BENCHMARK = "BENCHMARK"
    POSITION = "POSITION"
    UNKNOWN = "UNKNOWN"


class VenueTier(StrEnum):
    A_STAR = "A_STAR"
    A = "A"
    OTHER = "OTHER"
    PREPRINT = "PREPRINT"


DISCOVERED_VIA = frozenset({"BACKWARD", "FORWARD", "BOTH"})

# ("doi", value) | ("arxiv", value) | ("title", norm, surname, year).
# Declared here rather than in repo/ or services/ because BOTH build and
# consume these keys, and two declarations drifted apart once already.
CanonicalKey = tuple[str, str] | tuple[str, str, "str | None", "int | None"]

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")
# Only a LEADING article is dropped. Removing every "a"/"the" would merge
# genuinely different titles.
_ARTICLES = frozenset({"a", "an", "the"})
_NON_WORD = re.compile(r"[^\w]", re.UNICODE)


def normalize_title(title: str) -> str:
    """
    NFKD, lowercase, drop punctuation, collapse whitespace, drop a leading
    article. See BUILD.md R1.3.

    Punctuation is *deleted* rather than replaced with a space, so
    "Pre-training" and "Pretraining" normalize identically -- that pair is the
    common arXiv/conference duplicate the dedup stage has to catch.

    NFKD matters because a precomposed umlaut and a combining diaeresis are the
    same string to a reader and different bytes to Python; decomposing then
    stripping the combining mark folds them together.

    Deliberately NOT aggressive beyond this. "Attention Is All You Need" and
    "Attention Is Not All You Need" are different papers, and any normalization
    that merges them is worse than one that misses a duplicate.
    """
    decomposed = unicodedata.normalize("NFKD", title)
    words = _WS.sub(" ", _PUNCT.sub("", decomposed.lower())).strip().split()
    if len(words) > 1 and words[0] in _ARTICLES:
        words = words[1:]
    return " ".join(words)


def surname_of(full_name: str) -> str:
    """
    Last whitespace-separated token, lowercased, punctuation stripped.

    ONE implementation, shared by repo/papers.find_by_canonical_key (which
    extracts it from a stored byline) and services/dedup.canonical_key (which
    puts it into the key those lookups compare against). Two copies agreed on
    the day they were written and nothing made them keep agreeing.

    Crude on purpose: it only has to separate "LeCun" from "Hinton" well
    enough to stop two same-titled papers merging. It is not a name parser.
    """
    parts = full_name.strip().split()
    if not parts:
        return ""
    return _NON_WORD.sub("", parts[-1].lower())


@dataclass(frozen=True, slots=True)
class PaperStub:
    """What a search hit or a nested edge fetch gives you. Never scoreable."""

    s2_paper_id: str
    title: str
    year: int | None = None
    citation_count: int | None = None
    venue: str | None = None
    external_ids: tuple[tuple[str, str], ...] = ()

    @property
    def title_norm(self) -> str:
        return normalize_title(self.title)


@dataclass(frozen=True, slots=True)
class Paper:
    s2_paper_id: str
    title: str
    first_seen_at: str

    id: int | None = None
    s2_corpus_id: int | None = None
    canonical_paper_id: int | None = None

    abstract: str | None = None
    year: int | None = None
    publication_date: str | None = None
    venue: str | None = None
    venue_tier: VenueTier | None = None

    citation_count: int = 0
    reference_count: int = 0
    influential_citation_count: int = 0

    doi: str | None = None
    arxiv_id: str | None = None
    primary_arxiv_category: str | None = None
    # Tuples, not lists: a list field would make the frozen dataclass both
    # unhashable and quietly mutable through the shared reference.
    arxiv_categories: tuple[str, ...] = ()
    s2_fields: tuple[str, ...] = ()
    publication_types: tuple[str, ...] = ()

    # (s2_author_id, name) in byline order. Carried on the domain object rather
    # than looked up, because dedup's canonical key needs the first author and
    # S2 returns the byline with every paper fetch anyway.
    authors: tuple[tuple[str, str], ...] = ()

    paper_type: PaperType = PaperType.UNKNOWN
    crawl_state: CrawlState = CrawlState.STUB
    metadata_fetched_at: str | None = None

    @property
    def title_norm(self) -> str:
        return normalize_title(self.title)

    @property
    def is_stub(self) -> bool:
        """Never compute features on a stub (PLAN.md section C, section I)."""
        return self.crawl_state is CrawlState.STUB


@dataclass(frozen=True, slots=True)
class Author:
    s2_author_id: str
    name: str
    id: int | None = None
    # NULL until lazily fetched -- PLAN.md section C4. The S2 /references and
    # /citations endpoints reject authors.hIndex outright.
    h_index: int | None = None
    citation_count: int | None = None
    paper_count: int | None = None
    fetched_at: str | None = None


@dataclass(frozen=True, slots=True)
class Edge:
    """Direction is always citing -> cited."""

    citing_id: int
    cited_id: int
    discovered_via: str
    first_seen_at: str
    is_influential: bool = False
    intents: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Mirrors the CHECK constraint, so a bad edge fails where it was built
        # rather than several layers later inside an INSERT.
        if self.citing_id == self.cited_id:
            raise ValueError(f"edge cannot be a self citation: {self.citing_id}")
        if self.discovered_via not in DISCOVERED_VIA:
            raise ValueError(
                f"discovered_via must be one of {sorted(DISCOVERED_VIA)}, "
                f"got {self.discovered_via!r}"
            )


@dataclass(frozen=True, slots=True)
class FilterDecision:
    outcome: Outcome
    stage: str  # TYPE|TOPIC|APPLIED|DUPLICATE
    reason_code: str  # IS_DATASET, LOW_CITE_SURVEY, CAT_NOT_ALLOWED, ...
    details: dict[str, Any] = field(default_factory=dict)
    # True -> written with session_id NULL and cached forever. A 2013 dataset
    # paper is a 2013 dataset paper in every session (BUILD.md session_id
    # contract). False -> a fact about *this* graph only.
    is_global: bool = False


@dataclass(frozen=True, slots=True)
class GraphNode:
    """
    One paper's presence in one session's graph.

    `state` is the materialized projection of that paper's event log; the log in
    `interaction_events` is the source of truth, and scripts/rebuild_state.py
    (R2.3) reconstructs this from it.
    """

    session_id: int
    paper_id: int
    state: str  # SEED|CANDIDATE|LIKED|DISLIKED
    depth: int
    score: float | None = None
    features: dict[str, Any] = field(default_factory=dict)
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    added_by: int | None = None
    pos_x: float | None = None
    pos_y: float | None = None
    community_id: int | None = None


@dataclass(frozen=True, slots=True)
class InteractionEvent:
    """Append-only. Never updated, never deleted -- it is the audit trail."""

    id: int
    session_id: int
    paper_id: int
    event_type: str  # SEED_ADDED|LIKED|DISLIKED|UNLABELED|REMOVED|RESTORED|GC_SWEPT
    actor: str  # USER|SYSTEM
    created_at: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CandidateFeatures:
    """One lane per ranking weight. Missing signal degrades, never raises."""

    paper_id: int
    overlap: float = 0.0
    quality: float = 0.0
    recency: float = 0.0
    hub: float = 0.0
    ppr: float = 0.0
    cocite: float = 0.0
    bibcoup: float = 0.0
    venue: float = 0.0
    author: float = 0.0
    dislike: float = 0.0


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    paper_id: int
    score: float
    features: CandidateFeatures
    # Per-term contributions. This is what powers the UI's "why is this here?"
    breakdown: dict[str, float] = field(default_factory=dict)

    def sort_key(self) -> tuple[float, int]:
        """Stable ordering: best score first, paper_id breaking ties."""
        return (-self.score, self.paper_id)
