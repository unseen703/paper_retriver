"""
Co-citation and bibliographic coupling (R3).

PLAN.md calls these the measures that should carry most of the ranking weight:

    "Prefer **local** measures (co-citation, bibliographic coupling, anchor
    overlap) for ranking -- they depend only on 2-hop neighbourhoods you
    actually fetched, so they're far less crawl-sensitive."

Two different relations, and the difference is worth keeping:

    bibliographic coupling(A, B)   papers that BOTH A and B cite.
                                   Fixed at publication; it never changes.
    co-citation(A, B)              papers that cite BOTH A and B.
                                   Grows as the field cites them together.

One says "builds on the same work", the other says "is discussed in the same
breath". Collapsing them into a single similarity number would throw away the
distinction, and they genuinely disagree -- a paper can score high on either
while scoring zero on the other.

**These run over the corpus, not over the drawn graph, and that is the whole
design.** BUILD.md's boundary test is the reason:

    "two modern papers whose only shared reference is a 2014 paper must have
    non-zero bib_coupling. This test protects the design; without it the year
    floor silently degrades your best feature."

A pre-2015 reference is stored with all its edges and deliberately given no
`graph_nodes` row -- that is what a boundary paper *is*. Restricting these
queries to graph membership, which is correct for PageRank, would return zero
for exactly the papers coupling is best at finding, and would look completely
reasonable while doing it.

**Absent, not zero.** Both results are sparse dicts. Most pairs of papers share
nothing, and materialising a zero for each would mean carrying a number for
every paper in the corpus to say "no relationship" -- which is what the missing
key already says.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Connection, text


@dataclass(frozen=True, slots=True)
class SimilarityScores:
    """
    How related each paper is to the anchor set.

    Sparse on purpose: a paper with no relationship is absent rather than
    carrying a zero.
    """

    #: paper_id -> how many references it shares with any anchor.
    bib_coupling: dict[int, int] = field(default_factory=dict)
    #: paper_id -> how many papers cite it *and* an anchor.
    co_citation: dict[int, int] = field(default_factory=dict)


def _counts(conn: Connection, sql: str, anchors: list[int]) -> dict[int, int]:
    """
    Run one of the two symmetric queries and collect its counts.

    Anchors are bound as individual parameters rather than interpolated. They
    are internal ids and could be interpolated safely today, but a query that
    is only safe because of where its input happens to come from is one
    refactor away from not being.
    """
    placeholders = ",".join(f":a{i}" for i in range(len(anchors)))
    rows = conn.execute(
        text(sql.format(anchors=placeholders)),
        {f"a{i}": v for i, v in enumerate(anchors)},
    )
    return {int(paper_id): int(n) for paper_id, n in rows}


def compute_similarity(conn: Connection, anchor_ids: list[int]) -> SimilarityScores:
    """
    Coupling and co-citation of every paper against the anchor set.

    `anchor_ids` is the frontier -- SEED and LIKED, the papers whose company a
    candidate is being judged by. With no anchors there is nothing to be
    similar *to*, and empty is the honest answer rather than a ranking over
    nothing.

    Both queries deliberately omit any join to `graph_nodes`. See the module
    docstring: the shared reference is very often a boundary paper, and that is
    the case the feature exists for.
    """
    if not anchor_ids:
        return SimilarityScores()

    # Papers that cite something an anchor also cites. COUNT(DISTINCT) so two
    # anchors citing the same paper is one shared reference and not two --
    # otherwise the measure would reward redundancy among anchors rather than
    # similarity to them.
    coupling = _counts(
        conn,
        "SELECT mine.citing_id, COUNT(DISTINCT mine.cited_id)"
        " FROM edges mine"
        " JOIN edges theirs ON theirs.cited_id = mine.cited_id"
        " WHERE theirs.citing_id IN ({anchors})"
        "   AND mine.citing_id NOT IN ({anchors})"
        " GROUP BY mine.citing_id"
        # Ordered so two runs agree byte for byte (CLAUDE.md rule 7); these
        # numbers feed a ranking.
        " ORDER BY mine.citing_id",
        anchor_ids,
    )

    # Papers cited by something that also cites an anchor. The mirror image:
    # swap the roles of citing and cited throughout.
    co_citation = _counts(
        conn,
        "SELECT mine.cited_id, COUNT(DISTINCT mine.citing_id)"
        " FROM edges mine"
        " JOIN edges theirs ON theirs.citing_id = mine.citing_id"
        " WHERE theirs.cited_id IN ({anchors})"
        "   AND mine.cited_id NOT IN ({anchors})"
        " GROUP BY mine.cited_id"
        " ORDER BY mine.cited_id",
        anchor_ids,
    )

    return SimilarityScores(bib_coupling=coupling, co_citation=co_citation)


__all__ = ["SimilarityScores", "compute_similarity"]
