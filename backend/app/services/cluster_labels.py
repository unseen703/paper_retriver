"""
Cluster labels (R6): the top TF-IDF title terms of each community.

Pure: titles in, label strings out. Every community is one "document" (its
titles joined), so a term scores high when it is frequent inside the cluster
and rare across the others -- "contrastive" labels a cluster, "learning" does
not.

**Deterministic** (CLAUDE.md rule 7): terms are ranked by `(-score, term)` and
communities are visited in id order, so equal scores never depend on dict or
set iteration order. No stemming and no network; a small stopword list covers
words that carry no topical signal in ML titles.

A single community has no "other clusters" to be rare against, so IDF is
smoothed (`log((1 + n) / (1 + df)) + 1`) and stays positive; the label then
degrades to the most frequent title words rather than raising.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping

_TOKEN = re.compile(r"[a-z][a-z0-9\-]{2,}")

STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "via",
        "using",
        "use",
        "into",
        "over",
        "under",
        "towards",
        "toward",
        "are",
        "its",
        "their",
        "this",
        "that",
        "these",
        "those",
        "our",
        "can",
        "how",
        "what",
        "why",
        "when",
        "based",
        "approach",
        "method",
        "methods",
        "model",
        "models",
        "learning",
        "neural",
        "network",
        "networks",
        "deep",
        "paper",
        "study",
        "analysis",
        "new",
        "novel",
    }
)

DEFAULT_TERMS = 3


def _tokens(title: str) -> list[str]:
    return [t for t in _TOKEN.findall(title.lower()) if t not in STOPWORDS]


def label_communities(
    titles_by_community: Mapping[int, Iterable[str | None]],
    *,
    n_terms: int = DEFAULT_TERMS,
) -> dict[int, str]:
    """
    Map each community id to a label of up to `n_terms` terms, best first.

    A community whose titles yield no usable term gets the empty string; a
    missing title (`None`) is skipped, never an error (CLAUDE.md rule 6).
    """
    counts: dict[int, Counter[str]] = {}
    for cid in sorted(titles_by_community):
        c: Counter[str] = Counter()
        for title in titles_by_community[cid]:
            if title:
                c.update(_tokens(title))
        counts[cid] = c

    n_docs = len(counts)
    doc_freq: Counter[str] = Counter()
    for c in counts.values():
        doc_freq.update(c.keys())

    labels: dict[int, str] = {}
    for cid, c in counts.items():
        total = sum(c.values())
        scored = [
            (-(tf / total) * (math.log((1 + n_docs) / (1 + doc_freq[term])) + 1.0), term)
            for term, tf in c.items()
        ]
        scored.sort()
        labels[cid] = " ".join(term for _, term in scored[:n_terms])
    return labels


__all__ = ["DEFAULT_TERMS", "STOPWORDS", "label_communities"]
