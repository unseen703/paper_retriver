"""
Stage 2 of the cascade: is this paper in scope? (PLAN.md section E4)

A fallback chain, first hit wins, and every hit records which rung fired. The
chain exists because category data is genuinely incomplete: journal-only papers
have no arXiv id at all, and papers newer than the snapshot have no resolved
primary category until the OAI delta runs.

    a. primary category   -> CORE_ALLOW pass / APPLIED_DENY reject / else quarantine
    b. cross-listing      -> any core category, lower confidence
    c. venue              -> a core venue is evidence when arXiv data is absent
    d. Computer Science alone -> too coarse to decide, quarantine
    e. non-CS fields only -> reject
    f. nothing            -> quarantine

**Primary category wins, both ways.** `cs.LG` cross-listed `cs.CV` accepts;
`cs.CV` cross-listed `cs.LG` rejects. PLAN.md: "much methodological vision work
is primary cs.LG, so you lose less than the deny list suggests." Deciding on
membership instead would delete a large slice of core ML.

**Venue matching is not exact.** `CORE_VENUES` holds acronyms and S2 returns
expanded names -- the live R0.8 run returned "Neural Information Processing
Systems", not "NeurIPS". Exact matching would make rung (c) dead code, and it
would fail silently by pushing venue-only papers into quarantine.
"""

from __future__ import annotations

import re

from app.config import FiltersConfig
from app.models import FilterDecision, Paper
from app.services.filters.base import FilterStage, accept, quarantine, reject

# Fields that indicate the work is *about* something other than CS. Rejected
# only when Computer Science is absent -- a paper tagged both is the applied-AI
# case, and PLAN.md forbids auto-rejecting those.
NON_CS_FIELDS = frozenset(
    {
        "medicine",
        "biology",
        "chemistry",
        "physics",
        "economics",
        "business",
        "geology",
        "materials science",
        "environmental science",
        "agricultural and food sciences",
    }
)

# S2 returns expanded conference names; CORE_VENUES holds acronyms. Mapping the
# two is what keeps rung (c) alive. Not in filters.yaml because it is a matching
# detail, not a tunable policy -- CORE_VENUES remains the list you edit.
VENUE_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "NeurIPS": ("neural information processing systems",),
    "ICML": ("international conference on machine learning",),
    "ICLR": ("international conference on learning representations",),
    "ACL": ("annual meeting of the association for computational linguistics",),
    "EMNLP": ("empirical methods in natural language processing",),
    "NAACL": ("north american chapter of the association for computational linguistics",),
    "AAAI": ("aaai conference on artificial intelligence",),
    "IJCAI": ("international joint conference on artificial intelligence",),
    "COLT": ("conference on learning theory", "annual conference on learning theory"),
    "AISTATS": ("international conference on artificial intelligence and statistics",),
    "TMLR": ("transactions on machine learning research",),
    "JMLR": ("journal of machine learning research",),
    "COLM": ("conference on language modeling",),
    "EACL": ("european chapter of the association for computational linguistics",),
    "CoNLL": ("conference on computational natural language learning",),
}


def _matches_category(category: str, patterns: list[str]) -> bool:
    """
    Exact match, or prefix match for `family.*` entries.

    APPLIED_DENY carries `q-bio.*`, `eess.*` and friends; comparing them
    literally would deny nothing at all.
    """
    for pattern in patterns:
        if pattern.endswith(".*"):
            if category.startswith(pattern[:-1]):
                return True
        elif category == pattern:
            return True
    return False


def is_core_venue(venue: str | None, core_venues: list[str]) -> bool:
    """
    True if `venue` names one of the core venues, by acronym or expansion.

    Acronyms match on word boundaries, never as substrings: "ACL" occurs inside
    "NAACL" and "EACL", and a substring test would be accidentally right there
    and wrong on something like "Oracle".
    """
    if not venue:
        return False
    normalized = venue.strip().lower()
    for acronym in core_venues:
        if re.search(rf"\b{re.escape(acronym.lower())}\b", normalized):
            return True
        for expansion in VENUE_EXPANSIONS.get(acronym, ()):
            if expansion in normalized:
                return True
    return False


def topic_filter(paper: Paper, cfg: FiltersConfig) -> FilterDecision:
    # (a) The primary category is the strongest signal available, and it wins
    #     outright in both directions.
    primary = paper.primary_arxiv_category
    if primary:
        if _matches_category(primary, cfg.core_allow):
            return accept(FilterStage.TOPIC, "CAT_PRIMARY_CORE", primary_category=primary)
        if _matches_category(primary, cfg.applied_deny):
            return reject(FilterStage.TOPIC, "CAT_PRIMARY_APPLIED", primary_category=primary)
        if _matches_category(primary, cfg.borderline):
            return quarantine(FilterStage.TOPIC, "CAT_PRIMARY_BORDERLINE", primary_category=primary)
        return quarantine(FilterStage.TOPIC, "CAT_NOT_ALLOWED", primary_category=primary)

    # (b) No resolved primary -- newer than the snapshot, typically -- but a
    #     core cross-listing is still evidence, at lower confidence.
    if any(_matches_category(c, cfg.core_allow) for c in paper.arxiv_categories):
        return accept(FilterStage.TOPIC, "CAT_CROSS_CORE", categories=list(paper.arxiv_categories))

    # (c) No arXiv data at all: a core venue carries the paper.
    if is_core_venue(paper.venue, cfg.core_venues):
        return accept(FilterStage.TOPIC, "VENUE_CORE", venue=paper.venue)

    fields = {f.strip().lower() for f in paper.s2_fields}

    # (d) S2's coarse label alone cannot separate cs.CL from cs.CV.
    if fields == {"computer science"}:
        return quarantine(FilterStage.TOPIC, "FIELD_CS_ONLY", s2_fields=sorted(fields))

    # (e) Squarely outside CS. Note the `and not CS` guard: a paper tagged both
    #     is the applied-AI case, which stage 3 handles and never auto-rejects.
    if fields & NON_CS_FIELDS and "computer science" not in fields:
        return reject(FilterStage.TOPIC, "FIELD_NON_CS", s2_fields=sorted(fields))

    # (f) Nothing to go on.
    return quarantine(FilterStage.TOPIC, "NO_SIGNAL", s2_fields=sorted(fields))


__all__ = ["NON_CS_FIELDS", "VENUE_EXPANSIONS", "is_core_venue", "topic_filter"]
