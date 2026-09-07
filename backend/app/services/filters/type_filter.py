"""
Stage 1 of the cascade: what kind of artefact is this? (PLAN.md section E4)

Cheap and deterministic -- `publication_types` plus a title regex, no network.

The survey policy is an **OR**: `citation_count >= 200` OR
`citations_per_year >= 40`. The second disjunct is the whole point. A genuinely
good survey published last year cannot have accumulated 200 citations, and an
AND would reject exactly the recent surveys most worth reading. Both thresholds
come from `filters.yaml`, so tuning them is a config change.

Benchmarks QUARANTINE rather than REJECT. PLAN.md: "Benchmarking LLM reasoning"
may be a real contribution, so a title match means "look at this", not "delete
this".
"""

from __future__ import annotations

import re

from app.config import FiltersConfig
from app.models import FilterDecision, Paper
from app.services.filters.base import FilterStage, accept, quarantine, reject

# Anchored at the title's start, because a survey announces itself there.
#
# Wider than BUILD.md's `^(a )?(survey|review|overview)\b`, deliberately: that
# pattern misses "A Comprehensive Survey on Graph Neural Networks" and every
# other "A Systematic Review of ..." title, which is most of them. Missing them
# is not harmless -- an uncited "A Comprehensive Survey of X" would bypass the
# citation bar entirely and be accepted as an ordinary paper.
#
# Up to two words may sit between the article and the keyword, and the keyword
# must be followed by of/on/in/for, punctuation, or the end of the title. That
# trailing requirement is what keeps "Learning to Survey Unknown Terrain" out:
# "Learning to" fits the two-word slot, but "Unknown" is not a preposition.
_SURVEY_TITLE = re.compile(
    r"^(?:(?:a|an|the)\s+)?(?:\w+\s+){0,2}(survey|review|overview)\b"
    r"(?=\s+(?:of|on|in|for)\b|\s*[:\-]|$)",
    re.IGNORECASE,
)

# Not anchored -- a benchmark can be named anywhere in the title -- but on word
# boundaries so "Datasets" matches and "Datasetting" would not.
_BENCHMARK_TITLE = re.compile(r"\b(benchmark|benchmarks|dataset|datasets|test ?suite)\b", re.I)


def citations_per_year(paper: Paper, as_of_year: int) -> float:
    """
    Citation rate. Returns 0.0 when the year is unknown rather than raising.

    `as_of_year` is passed in rather than read from the clock so this stays a
    pure function (CLAUDE.md rule 3) -- a filter that consults `datetime.now()`
    gives different verdicts on different days and cannot be replayed.
    """
    if paper.year is None:
        return 0.0
    age = max(1, as_of_year - paper.year + 1)
    return paper.citation_count / age


def _survey_verdict(paper: Paper, cfg: FiltersConfig, as_of_year: int) -> FilterDecision:
    policy = cfg.paper_type_policy["SURVEY"]
    min_citations = policy.citation_count_min or 0
    min_rate = policy.or_citations_per_year_min or 0
    rate = citations_per_year(paper, as_of_year)

    # OR, not AND: either bar clears it.
    if paper.citation_count >= min_citations or rate >= min_rate:
        return accept(
            FilterStage.TYPE,
            "SURVEY_WELL_CITED",
            citation_count=paper.citation_count,
            citations_per_year=round(rate, 2),
        )
    return reject(
        FilterStage.TYPE,
        "LOW_CITE_SURVEY",
        citation_count=paper.citation_count,
        citations_per_year=round(rate, 2),
        citation_count_min=min_citations,
        or_citations_per_year_min=min_rate,
    )


def type_filter(paper: Paper, cfg: FiltersConfig, as_of_year: int) -> FilterDecision:
    types = {t.lower() for t in paper.publication_types}
    title = paper.title or ""

    # Hard signal first: an explicit Dataset type beats any title heuristic.
    if "dataset" in types:
        return reject(FilterStage.TYPE, "IS_DATASET", publication_types=list(types))

    if _SURVEY_TITLE.match(title) or "review" in types:
        return _survey_verdict(paper, cfg, as_of_year)

    if _BENCHMARK_TITLE.search(title):
        return quarantine(FilterStage.TYPE, "MAYBE_BENCHMARK", title=title)

    return accept(FilterStage.TYPE)


__all__ = ["citations_per_year", "type_filter"]
