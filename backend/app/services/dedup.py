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

import re

from app.models import CanonicalKey, Paper, normalize_title, surname_of

# Only a trailing "v<digits>" is a version suffix. Splitting on "v" -- as the
# BUILD.md sketch does -- would truncate any id that happens to contain one.
_ARXIV_VERSION = re.compile(r"v\d+$")


def strip_arxiv_version(arxiv_id: str) -> str:
    """`1706.03762v3` -> `1706.03762`. Leaves `cs.CV/0701001` intact."""
    return _ARXIV_VERSION.sub("", arxiv_id.strip())


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


__all__ = [
    "CanonicalKey",
    "canonical_key",
    "first_author_surname",
    "normalize_title",
    "strip_arxiv_version",
]
