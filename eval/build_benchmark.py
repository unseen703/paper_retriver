"""
Building the benchmark (R4.3).

BUILD.md: "sample 150-300 held-out target papers (core-ML, 2019-2024, >=15
references). For each: seeds = 3 randomly chosen references; ground truth = the
remaining references."

The shape of a case *is* the experiment, so `make_case` is a pure function over
`(target_id, references, rng)` -- testable without a database, a fixture or a
network. `eligible_targets` holds the one SQL query and is tested separately.

**What this measures, and what it cannot.** PLAN.md's limitations section is not
a disclaimer to be written at the end; it is a property of the ground truth and
belongs beside the code that builds it:

  * A reference list is a *biased* proxy for relevance. It omits concurrent
    work, omits deliberately-uncited competitors, and includes obligatory
    citations the author never read.
  * Recall is capped by graph reachability. A relevant paper four hops away is
    unreachable at `max_depth=3` and counts as a miss however good the ranking.
  * Popular papers appear in many reference lists, so a citation-count baseline
    is artificially strong on this benchmark. Beating it by a little is not
    impressive, and the write-up has to say so.

None of that is fixable by better sampling.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Connection, text
from temporal import cutoff_from_seeds

#: BUILD.md's floor. With ten references, three seeds leave seven to find, and
#: Recall@20 over seven items is noise dressed as a measurement.
MIN_REFERENCES = 15

#: "seeds = 3 randomly chosen references". Three is enough for
#: `anchor_overlap` to mean something -- a candidate reached from two of three
#: seeds is a different claim from one reached from the only seed there was.
SEEDS_PER_CASE = 3

#: BUILD.md's window. Recent enough that the corpus has the references, old
#: enough that the paper has accumulated some.
YEAR_FROM = 2019
YEAR_TO = 2024


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """One question: from these seeds, find these papers, seeing only the past."""

    target_id: int
    seed_ids: tuple[int, ...]
    ground_truth_ids: tuple[int, ...]
    cutoff: date


def make_case(
    target_id: int,
    references: Sequence[tuple[int, str | None]],
    rng: random.Random,
) -> BenchmarkCase | None:
    """
    Split one paper's references into seeds and ground truth, or return None.

    Returns None -- rather than a degraded case -- when the paper has too few
    references or its seeds carry no usable date. A case with no cutoff cannot
    be evaluated honestly, and one evaluated with the guard off would outscore
    every honest case in the benchmark.

    **Deduplicated, self-reference removed, and sorted before sampling.** The
    self-reference would be a guaranteed hit; a duplicate would be counted
    twice; and S2 returns references in whatever order it likes, so sorting is
    what makes the split a function of the ids alone rather than of the order
    they arrived in (CLAUDE.md rule 7).
    """
    by_id: dict[int, str | None] = {}
    for paper_id, published in references:
        if paper_id == target_id:
            continue
        by_id.setdefault(paper_id, published)

    if len(by_id) < MIN_REFERENCES:
        return None

    ordered = sorted(by_id)
    seeds = sorted(rng.sample(ordered, SEEDS_PER_CASE))

    cutoff = cutoff_from_seeds([by_id[s] for s in seeds])
    if cutoff is None:
        return None

    return BenchmarkCase(
        target_id=target_id,
        seed_ids=tuple(seeds),
        ground_truth_ids=tuple(paper_id for paper_id in ordered if paper_id not in set(seeds)),
        cutoff=cutoff,
    )


def eligible_targets(
    conn: Connection,
    *,
    year_from: int = YEAR_FROM,
    year_to: int = YEAR_TO,
    min_references: int = MIN_REFERENCES,
    limit: int = 300,
) -> list[int]:
    """
    Papers that could be a benchmark case, ordered by id.

    Counts the edges actually stored rather than trusting `papers.reference_count`
    -- that column is what S2 reported, and a case needs references this corpus
    can actually reach. Grading against references nobody crawled would measure
    the crawl rather than the ranker.

    Ordered by `paper_id`, not randomly: the sampling that matters happens in
    `make_case` under a seeded RNG, and a randomly-ordered query would make the
    benchmark depend on SQLite's mood.
    """
    rows = conn.execute(
        text(
            "SELECT p.id FROM papers p"
            " JOIN edges e ON e.citing_id = p.id"
            " WHERE p.year BETWEEN :year_from AND :year_to"
            " GROUP BY p.id"
            " HAVING COUNT(DISTINCT e.cited_id) >= :min_references"
            " ORDER BY p.id LIMIT :limit"
        ),
        {
            "year_from": year_from,
            "year_to": year_to,
            "min_references": min_references,
            "limit": limit,
        },
    )
    return [int(row[0]) for row in rows]


def references_of(conn: Connection, paper_id: int) -> list[tuple[int, str | None]]:
    """This paper's stored references, with the dates the cutoff is built from."""
    rows = conn.execute(
        text(
            "SELECT p.id, COALESCE(p.publication_date, CAST(p.year AS TEXT))"
            " FROM edges e JOIN papers p ON p.id = e.cited_id"
            " WHERE e.citing_id = :paper_id ORDER BY p.id"
        ),
        {"paper_id": paper_id},
    )
    return [(int(row[0]), row[1]) for row in rows]


__all__ = [
    "MIN_REFERENCES",
    "SEEDS_PER_CASE",
    "YEAR_FROM",
    "YEAR_TO",
    "BenchmarkCase",
    "eligible_targets",
    "make_case",
    "references_of",
]
