"""
resolver.py
-----------
Resolve a list of mixed seed inputs (title strings or URLs) to canonical paper
records, preferring Semantic Scholar paperId and falling back to OpenAlex.

Supported URL formats:
  - https://doi.org/10.xxxx/...
  - https://arxiv.org/abs/2301.00001
  - https://www.semanticscholar.org/paper/<title>/<S2ID>
  - https://api.semanticscholar.org/graph/v1/paper/<S2ID>
  - https://openalex.org/W1234567890

For title-only inputs:
  1. Try S2 /paper/search/match  (fast, single best match)
  2. Fall back to OpenAlex title.search
  3. If still nothing, flag as unresolved rather than guessing
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from typing import Optional

from semantic_scholar_client import SemanticScholarClient, s2_record_to_kwargs
from openalex_client import OpenAlexClient
from storage import Paper, Store

logger = logging.getLogger(__name__)

# ---- URL pattern matching -----------------------------------------------------

_DOI_PATTERN = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|DOI:|doi:)(10\.\S+)", re.IGNORECASE
)
_ARXIV_PATTERN = re.compile(
    r"(?:https?://arxiv\.org/abs/|ARXIV:|arxiv:)([0-9]{4}\.[0-9]+(?:v\d+)?)",
    re.IGNORECASE,
)
_S2_URL_PATTERN = re.compile(
    r"semanticscholar\.org/paper/[^/]+/([0-9a-f]{40})", re.IGNORECASE
)
_S2_API_PATTERN = re.compile(r"api\.semanticscholar\.org/[^/]+/paper/([0-9a-f]{40})")
_S2_ID_BARE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_OA_PATTERN = re.compile(r"openalex\.org/(W\d+)", re.IGNORECASE)


@dataclass
class ResolvedSeed:
    input_text: str
    paper_id: str          # canonical S2 paperId (or OA ID prefixed "OA:" as fallback)
    title: str
    year: Optional[int]
    source: str            # 'semantic_scholar' | 'openalex'
    confidence: float      # 0.0–1.0


def _extract_identifier(text: str) -> tuple[str, str] | None:
    """Return (kind, value) from a URL/identifier string, or None if it's a plain title."""
    text = text.strip()
    for pat, kind in (
        (_DOI_PATTERN, "doi"),
        (_ARXIV_PATTERN, "arxiv"),
        (_S2_URL_PATTERN, "s2"),
        (_S2_API_PATTERN, "s2"),
        (_OA_PATTERN, "openalex"),
    ):
        m = pat.search(text)
        if m:
            return kind, m.group(1)
    if _S2_ID_BARE.match(text):
        return "s2", text
    return None


def _s2_id_arg(kind: str, value: str) -> str:
    if kind == "doi":
        return f"DOI:{value}"
    if kind == "arxiv":
        return f"ARXIV:{value}"
    return value  # bare S2 ID


def resolve_seeds(
    inputs: list[str],
    s2_client: SemanticScholarClient,
    oa_client: OpenAlexClient,
    store: Store,
    *,
    verbose: bool = True,
) -> tuple[list[ResolvedSeed], list[str]]:
    """
    Resolve a list of seed inputs.

    Returns:
        resolved   - list of ResolvedSeed for successful matches
        unresolved - list of input strings that could not be matched
    """
    resolved: list[ResolvedSeed] = []
    unresolved: list[str] = []

    for text in inputs:
        text = text.strip()
        if not text:
            continue

        ident = _extract_identifier(text)
        seed = None

        if ident:
            kind, value = ident
            if kind == "openalex":
                seed = _resolve_via_openalex_id(value, s2_client, oa_client, store, text)
            else:
                seed = _resolve_via_s2_id(_s2_id_arg(kind, value), s2_client, oa_client, store, text)
        else:
            # Plain title — try S2 first, then OpenAlex
            seed = _resolve_via_title(text, s2_client, oa_client, store)

        if seed:
            if verbose:
                print(f"  [ok] {text!r}\n       -> {seed.title!r} ({seed.year}) via {seed.source}")
            resolved.append(seed)
        else:
            if verbose:
                print(f"  [!!] Could not resolve: {text!r} — needs manual confirmation", file=sys.stderr)
            unresolved.append(text)

    return resolved, unresolved


# ---- resolution helpers -------------------------------------------------------

def _resolve_via_s2_id(
    paper_id_arg: str,
    s2: SemanticScholarClient,
    oa: OpenAlexClient,
    store: Store,
    original: str,
) -> Optional[ResolvedSeed]:
    try:
        record = s2.get_paper(paper_id_arg)
    except Exception as exc:
        logger.warning("S2 fetch failed for %r: %s", paper_id_arg, exc)
        record = None

    if record and record.get("paperId"):
        kwargs = s2_record_to_kwargs(record)
        paper = Paper(**kwargs)
        store.upsert_paper(paper, label="seed")
        store.record_seed_title(original, paper.paper_id, 1.0)
        return ResolvedSeed(original, paper.paper_id, paper.title, paper.year,
                            "semantic_scholar", 1.0)

    # Try OpenAlex by DOI if we had one
    doi = paper_id_arg.replace("DOI:", "")
    if doi.startswith("10."):
        return _resolve_via_oa_doi(doi, s2, oa, store, original)
    return None


def _resolve_via_openalex_id(
    oa_id: str,
    s2: SemanticScholarClient,
    oa: OpenAlexClient,
    store: Store,
    original: str,
) -> Optional[ResolvedSeed]:
    work = oa.get_work(oa_id)
    if not work:
        return None
    doi = (work.get("doi") or "").replace("https://doi.org/", "")
    if doi:
        # Try to get S2 record so we have a proper S2 paperId
        seed = _resolve_via_s2_id(f"DOI:{doi}", s2, oa, store, original)
        if seed:
            return seed
    # Fall back to storing as OA-sourced paper
    return _store_oa_work(work, oa, store, original, confidence=0.95)


def _resolve_via_oa_doi(
    doi: str,
    s2: SemanticScholarClient,
    oa: OpenAlexClient,
    store: Store,
    original: str,
) -> Optional[ResolvedSeed]:
    work = oa.get_work_by_doi(doi)
    if not work:
        return None
    return _store_oa_work(work, oa, store, original, confidence=0.9)


def _resolve_via_title(
    title: str,
    s2: SemanticScholarClient,
    oa: OpenAlexClient,
    store: Store,
) -> Optional[ResolvedSeed]:
    # 1. Semantic Scholar search
    try:
        match = s2.search_paper_by_title(title)
    except Exception as exc:
        logger.warning("S2 title search failed: %s", exc)
        match = None

    if match and match.get("paperId"):
        try:
            full = s2.get_paper(match["paperId"])
        except Exception:
            full = match
        if full and full.get("paperId"):
            kwargs = s2_record_to_kwargs(full)
            paper = Paper(**kwargs)
            store.upsert_paper(paper, label="seed")
            store.record_seed_title(title, paper.paper_id, 0.9)
            return ResolvedSeed(title, paper.paper_id, paper.title, paper.year,
                                "semantic_scholar", 0.9)

    # 2. OpenAlex title search fallback
    try:
        work = oa.search_by_title(title)
    except Exception as exc:
        logger.warning("OpenAlex title search failed: %s", exc)
        work = None

    if work:
        seed = _store_oa_work(work, oa, store, title, confidence=0.75)
        if seed:
            store.record_seed_title(title, seed.paper_id, 0.75)
            return seed

    store.record_seed_title(title, None, 0.0)
    return None


def _store_oa_work(
    work: dict,
    oa: OpenAlexClient,
    store: Store,
    original: str,
    confidence: float,
) -> Optional[ResolvedSeed]:
    kwargs = oa.to_paper_kwargs(work)
    if not kwargs.get("paper_id"):
        return None
    paper = Paper(**kwargs)
    store.upsert_paper(paper, label="seed")
    return ResolvedSeed(original, paper.paper_id, paper.title, paper.year,
                        "openalex", confidence)
