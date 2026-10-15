"""
Canonical keys for duplicate detection. Pure functions (CLAUDE.md rule 3).

The asymmetry that shapes everything here: **a missed duplicate costs one
redundant node; a wrong merge silently corrupts the graph.** Merging two
distinct papers attributes one's citations to the other, and no later stage can
detect it -- the recommendations just quietly get worse.

So the normalization is deliberately conservative. "Attention Is All You Need"
and "Attention Is Not All You Need" are both real papers, and any rule
aggressive enough to merge them is too aggressive to keep.

Key precedence, strongest identifier first:

    doi    -> globally unique, lowercased (case-insensitive by spec)
    arxiv  -> unique once the version suffix is stripped
    title  -> (normalized title, first author surname, year), the fallback

Title keys carry the surname because a shared title alone is not evidence:
"Deep Learning" is the title of several unrelated papers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher

import numpy as np

from app.models import (
    CanonicalKey,
    Paper,
    normalize_title,
    strip_arxiv_version,
    surname_of,
)
from app.services.embeddings import EmbeddingStore


# Only a trailing "v<digits>" is a version suffix. Splitting on "v" -- as the
# BUILD.md sketch does -- would truncate any id that happens to contain one.
def first_author_surname(paper: Paper) -> str | None:
    """
    Surname of the byline's first author, lowercased, or None.

    None is common and expected -- a paper reconstructed from the database
    carries no byline -- so callers must treat it as "unknown", never as
    "different".
    """
    if not paper.authors:
        return None
    _, name = paper.authors[0]
    return surname_of(name) or None


def canonical_key(paper: Paper) -> CanonicalKey:
    """
    The identity this paper should dedup on.

    Falsy checks rather than `is not None`: S2 returns `""` for a missing DOI
    about as often as it omits the field, and an empty-string key would collide
    every paper that lacks one.
    """
    if paper.doi:
        return ("doi", paper.doi.strip().lower())
    if paper.arxiv_id:
        return ("arxiv", strip_arxiv_version(paper.arxiv_id))
    return ("title", normalize_title(paper.title), first_author_surname(paper), paper.year)


COSINE_MIN = 0.97
TITLE_SIMILARITY_MIN = 0.85


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    """A pair surfaced for human review. `a < b`; nothing is ever merged from this."""

    a: int
    b: int
    cosine: float
    title_similarity: float


def title_similarity(a: str, b: str) -> float:
    """Ratio in [0, 1] over normalized titles; two empty titles are not similar."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb, autojunk=False).ratio()


def duplicate_candidates(
    store: EmbeddingStore,
    titles: Mapping[int, str],
    *,
    cosine_min: float = COSINE_MIN,
    title_min: float = TITLE_SIMILARITY_MIN,
) -> list[DuplicateCandidate]:
    """
    Pairs that look like one paper stored twice (R6 dedup upgrade), for review.

    BOTH signals must clear their threshold. Embeddings alone call a paper and
    its follow-up near-identical; titles alone merge "Deep Learning" with every
    other "Deep Learning". Requiring both keeps the dangerous direction (a wrong
    merge) rare, and the output is a review list, never an automatic merge --
    the key-based `canonical_key` remains the only thing that merges.

    Papers without a vector or a title are ignored. Ordered
    ``(-cosine, a, b)``; ids are visited in sorted order so the result is
    identical on every run.
    """
    ids = sorted(i for i in titles if i in store)
    if len(ids) < 2:
        return []
    vecs = [v for i in ids if (v := store.get(i)) is not None]
    matrix = np.stack(vecs).astype(np.float64)
    sims = matrix @ matrix.T
    out: list[DuplicateCandidate] = []
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            cos = float(sims[x, y])
            if cos < cosine_min:
                continue
            ts = title_similarity(titles[ids[x]], titles[ids[y]])
            if ts >= title_min:
                out.append(DuplicateCandidate(ids[x], ids[y], cos, ts))
    out.sort(key=lambda c: (-c.cosine, c.a, c.b))
    return out


__all__ = [
    "CanonicalKey",
    "DuplicateCandidate",
    "canonical_key",
    "duplicate_candidates",
    "title_similarity",
    "first_author_surname",
    "normalize_title",
    "strip_arxiv_version",
]
