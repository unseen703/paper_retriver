"""
The `papers` table. **Global** -- no `session_id`, by contract.

A paper's title and year do not depend on which graph you are looking at, so
this module is deliberately unable to accept a session. BUILD.md puts it as
"that's the type system enforcing the boundary for you", and a test in
test_repo_papers.py inspects these signatures to keep it true.

Two rules do the real work here.

**`crawl_state` never regresses.** The same paper arrives twice: once fetched in
full, once as a bare neighbour inside somebody else's reference list. If the
stub write won, the abstract would be erased, the citation count reset to zero,
and the paper would sink to the bottom of every ranking with nothing to say why.
Every lossy column is guarded by `_IS_DOWNGRADE`.

**Writes are one statement.** The guard is evaluated in SQL against the stored
row's rank and `RETURNING` supplies the id, so upserting a candidate costs one
round trip rather than three. An expansion upserts hundreds at a time.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, text

from app.models import (
    CanonicalKey,
    CrawlState,
    Paper,
    PaperType,
    VenueTier,
    normalize_title,
    surname_of,
)

# Higher wins. A write may raise the crawl state but never lower it.
_CRAWL_RANK = {
    CrawlState.STUB: 0,
    CrawlState.METADATA: 1,
    CrawlState.REFS_DONE: 2,
    CrawlState.CITES_DONE: 2,
    CrawlState.EXPANDED: 3,
}

# The same ranking, expressed in SQL so the guard needs no prior SELECT.
_STORED_RANK = (
    "CASE papers.crawl_state"
    " WHEN 'STUB' THEN 0 WHEN 'METADATA' THEN 1 WHEN 'REFS_DONE' THEN 2"
    " WHEN 'CITES_DONE' THEN 2 WHEN 'EXPANDED' THEN 3 ELSE 0 END"
)
_IS_DOWNGRADE = f":crawl_rank < ({_STORED_RANK})"

_COLUMNS = (
    "id, s2_paper_id, s2_corpus_id, canonical_paper_id, title, abstract, year,"
    " publication_date, venue, venue_tier, citation_count, reference_count,"
    " influential_citation_count, doi, arxiv_id, primary_arxiv_category,"
    " arxiv_categories, s2_fields, publication_types, paper_type, crawl_state,"
    " first_seen_at, metadata_fetched_at"
)

_UPSERT = text(
    "INSERT INTO papers (s2_paper_id, s2_corpus_id, title, title_norm, abstract,"
    " year, publication_date, venue, venue_tier, citation_count, reference_count,"
    " influential_citation_count, doi, arxiv_id, primary_arxiv_category,"
    " arxiv_categories, s2_fields, publication_types, paper_type, crawl_state,"
    " first_seen_at, metadata_fetched_at)"
    " VALUES (:s2_paper_id, :s2_corpus_id, :title, :title_norm, :abstract,"
    " :year, :publication_date, :venue, :venue_tier, :citation_count, :reference_count,"
    " :influential_citation_count, :doi, :arxiv_id, :primary_arxiv_category,"
    " :arxiv_categories, :s2_fields, :publication_types, :paper_type, :crawl_state,"
    " :first_seen_at, :metadata_fetched_at)"
    " ON CONFLICT (s2_paper_id) DO UPDATE SET"
    "   s2_corpus_id = COALESCE(excluded.s2_corpus_id, papers.s2_corpus_id),"
    # COALESCE, not excluded: a later fetch with a narrower field set must not
    # blank out data an earlier, richer one supplied.
    "   title = CASE WHEN " + _IS_DOWNGRADE + " THEN papers.title ELSE excluded.title END,"
    "   title_norm = CASE WHEN " + _IS_DOWNGRADE + " THEN papers.title_norm"
    "     ELSE excluded.title_norm END,"
    "   abstract = COALESCE(excluded.abstract, papers.abstract),"
    "   year = COALESCE(excluded.year, papers.year),"
    "   publication_date = COALESCE(excluded.publication_date, papers.publication_date),"
    "   venue = COALESCE(excluded.venue, papers.venue),"
    "   venue_tier = COALESCE(excluded.venue_tier, papers.venue_tier),"
    # Counts default to 0 on a stub, so writing them unconditionally would let a
    # bare neighbour record reset a hub's 191k citations to zero.
    "   citation_count = CASE WHEN " + _IS_DOWNGRADE + " THEN papers.citation_count"
    "     ELSE excluded.citation_count END,"
    "   reference_count = CASE WHEN " + _IS_DOWNGRADE + " THEN papers.reference_count"
    "     ELSE excluded.reference_count END,"
    "   influential_citation_count = CASE WHEN "
    + _IS_DOWNGRADE
    + "     THEN papers.influential_citation_count"
    "     ELSE excluded.influential_citation_count END,"
    "   doi = COALESCE(excluded.doi, papers.doi),"
    "   arxiv_id = COALESCE(excluded.arxiv_id, papers.arxiv_id),"
    "   primary_arxiv_category ="
    "     COALESCE(excluded.primary_arxiv_category, papers.primary_arxiv_category),"
    "   arxiv_categories = COALESCE(excluded.arxiv_categories, papers.arxiv_categories),"
    "   s2_fields = COALESCE(excluded.s2_fields, papers.s2_fields),"
    "   publication_types = COALESCE(excluded.publication_types, papers.publication_types),"
    "   paper_type = CASE WHEN " + _IS_DOWNGRADE + " THEN papers.paper_type"
    "     ELSE excluded.paper_type END,"
    "   crawl_state = CASE WHEN " + _IS_DOWNGRADE + " THEN papers.crawl_state"
    "     ELSE excluded.crawl_state END,"
    # first_seen_at records when the corpus first saw the paper, not the most
    # recent refetch. Never overwritten.
    "   metadata_fetched_at ="
    "     COALESCE(excluded.metadata_fetched_at, papers.metadata_fetched_at)"
    " RETURNING id"
)

_WORD = re.compile(r"[^\w]", re.UNICODE)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _json_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        return tuple(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return ()


def _parse_byline(raw: str | None) -> tuple[tuple[str, str], ...]:
    """GROUP_CONCAT output back into (s2_author_id, name) pairs, byline order."""
    if not raw:
        return ()
    out: list[tuple[str, str]] = []
    for entry in raw.split(""):
        author_id, _, name = entry.partition("")
        if author_id and name:
            out.append((author_id, name))
    return tuple(out)


def _row_to_paper(row: Any) -> Paper:
    return Paper(
        id=row[0],
        s2_paper_id=row[1],
        s2_corpus_id=row[2],
        canonical_paper_id=row[3],
        title=row[4],
        abstract=row[5],
        year=row[6],
        publication_date=row[7],
        venue=row[8],
        venue_tier=VenueTier(row[9]) if row[9] else None,
        citation_count=row[10] or 0,
        reference_count=row[11] or 0,
        influential_citation_count=row[12] or 0,
        doi=row[13],
        arxiv_id=row[14],
        primary_arxiv_category=row[15],
        arxiv_categories=_json_list(row[16]),
        s2_fields=_json_list(row[17]),
        publication_types=_json_list(row[18]),
        paper_type=PaperType(row[19]) if row[19] else PaperType.UNKNOWN,
        crawl_state=CrawlState(row[20]) if row[20] else CrawlState.STUB,
        first_seen_at=row[21],
        metadata_fetched_at=row[22],
        authors=_parse_byline(row[23]) if len(row) > 23 else (),
    )


def find_by_s2_id(conn: Connection, s2_paper_id: str) -> int | None:
    return conn.execute(
        text("SELECT id FROM papers WHERE s2_paper_id = :s2_id"), {"s2_id": s2_paper_id}
    ).scalar()


def find_ids_by_s2_ids(conn: Connection, s2_paper_ids: list[str]) -> dict[str, int]:
    """
    s2_paper_id -> local id, for the ids that exist. One query, not N.

    Ids absent from the corpus are simply absent from the mapping, so a caller
    reads a miss as "we have never seen this paper" without a second lookup.
    """
    if not s2_paper_ids:
        return {}
    # Chunked at 400, matching `repo/edges.py`. One bind parameter per id would
    # blow SQLite's SQLITE_MAX_VARIABLE_NUMBER on a large list, and the two
    # neighbouring functions in this layer already establish the convention --
    # so a caller reading them would reasonably assume this one is safe too.
    found: dict[str, int] = {}
    for start in range(0, len(s2_paper_ids), 400):
        chunk = s2_paper_ids[start : start + 400]
        placeholders = ",".join(f":s{i}" for i in range(len(chunk)))
        rows = conn.execute(
            text(f"SELECT s2_paper_id, id FROM papers WHERE s2_paper_id IN ({placeholders})"),
            {f"s{i}": v for i, v in enumerate(chunk)},
        )
        found.update({row[0]: row[1] for row in rows})
    return found


def upsert_paper(conn: Connection, paper: Paper) -> int:
    """Insert or update by `s2_paper_id` in one statement. Returns the id."""
    row = conn.execute(
        _UPSERT,
        {
            "s2_paper_id": paper.s2_paper_id,
            "s2_corpus_id": paper.s2_corpus_id,
            "title": paper.title,
            "title_norm": normalize_title(paper.title),
            "abstract": paper.abstract,
            "year": paper.year,
            "publication_date": paper.publication_date,
            "venue": paper.venue,
            "venue_tier": paper.venue_tier.value if paper.venue_tier else None,
            "citation_count": paper.citation_count,
            "reference_count": paper.reference_count,
            "influential_citation_count": paper.influential_citation_count,
            "doi": paper.doi,
            "arxiv_id": paper.arxiv_id,
            "primary_arxiv_category": paper.primary_arxiv_category,
            "arxiv_categories": json.dumps(list(paper.arxiv_categories))
            if paper.arxiv_categories
            else None,
            "s2_fields": json.dumps(list(paper.s2_fields)) if paper.s2_fields else None,
            "publication_types": json.dumps(list(paper.publication_types))
            if paper.publication_types
            else None,
            "paper_type": paper.paper_type.value,
            "crawl_state": paper.crawl_state.value,
            "crawl_rank": _CRAWL_RANK[paper.crawl_state],
            "first_seen_at": paper.first_seen_at,
            "metadata_fetched_at": paper.metadata_fetched_at,
        },
    ).fetchone()
    if row is None:  # pragma: no cover - RETURNING always yields a row
        raise RuntimeError(f"upsert produced no id for {paper.s2_paper_id}")
    return int(row[0])


def upsert_stub(conn: Connection, s2_id: str, title: str, year: int | None) -> int:
    """
    Write the little we know about a nested neighbour.

    Never downgrades an existing row: the crawl-state guard keeps the higher
    state and every optional column is COALESCEd.
    """
    return upsert_paper(
        conn,
        Paper(
            s2_paper_id=s2_id,
            title=title,
            year=year,
            first_seen_at=_utcnow(),
            crawl_state=CrawlState.STUB,
        ),
    )


def set_authors(conn: Connection, paper_id: int, authors: list[tuple[str, str]]) -> None:
    """
    Attach `(s2_author_id, name)` pairs in order. Position 0 is the first author.

    Idempotent, and an author shared by two papers is stored once. Dedup's
    canonical title key needs the first author to tell two same-titled papers
    apart, which is the only reason this exists at R1.1.
    """
    for position, (s2_author_id, name) in enumerate(authors):
        conn.execute(
            text(
                "INSERT INTO authors (s2_author_id, name) VALUES (:s2_id, :name)"
                " ON CONFLICT (s2_author_id) DO UPDATE SET name = excluded.name"
            ),
            {"s2_id": s2_author_id, "name": name},
        )
        author_id = conn.execute(
            text("SELECT id FROM authors WHERE s2_author_id = :s2_id"), {"s2_id": s2_author_id}
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO paper_authors (paper_id, author_id, position)"
                " VALUES (:paper_id, :author_id, :position)"
                " ON CONFLICT (paper_id, author_id) DO UPDATE SET position = excluded.position"
            ),
            {"paper_id": paper_id, "author_id": author_id, "position": position},
        )


def get_papers_by_ids(conn: Connection, ids: list[int]) -> list[Paper]:
    """
    Returns papers in the order requested; unknown ids are skipped.

    The byline comes back with them. Without it a Paper loaded here would key
    differently under `canonical_key` than the same paper fetched from S2 --
    `('title', norm, None, year)` against `('title', norm, 'lecun', year)` --
    and every title-keyed dedup comparison across the two would silently miss.

    One `GROUP_CONCAT` rather than a second query or a per-paper lookup: the
    join stays inside the single statement the last review established.
    """
    if not ids:
        return []
    found: dict[int, Paper] = {}
    for start in range(0, len(ids), 500):  # SQLite caps bound params at 999
        chunk = ids[start : start + 500]
        placeholders = ",".join(f":p{i}" for i in range(len(chunk)))
        rows = conn.execute(
            text(
                f"SELECT {_COLUMNS},"
                # ORDER BY inside GROUP_CONCAT keeps byline order, which decides
                # which surname reaches the key.
                "  (SELECT GROUP_CONCAT(a.s2_author_id || '\x1f' || a.name, '\x1e')"
                "     FROM paper_authors pa JOIN authors a ON a.id = pa.author_id"
                "    WHERE pa.paper_id = papers.id ORDER BY pa.position) AS byline"
                f" FROM papers WHERE id IN ({placeholders})"
            ),
            {f"p{i}": v for i, v in enumerate(chunk)},
        )
        for row in rows:
            found[row[0]] = _row_to_paper(row)
    return [found[i] for i in ids if i in found]


def find_by_canonical_key(conn: Connection, key: CanonicalKey) -> int | None:
    """
    Resolve a dedup key to an internal id. See services/dedup.py (R1.3) for how
    keys are built.
    """
    kind = key[0]
    if kind == "doi":
        # DOIs are case-insensitive by spec and S2 is inconsistent about case.
        return conn.execute(
            text("SELECT id FROM papers WHERE LOWER(doi) = :doi"), {"doi": str(key[1]).lower()}
        ).scalar()
    if kind == "arxiv":
        return conn.execute(
            text("SELECT id FROM papers WHERE arxiv_id = :arxiv_id"), {"arxiv_id": key[1]}
        ).scalar()
    if kind == "title":
        return _find_by_title(conn, key)
    raise ValueError(f"unknown canonical key kind: {kind!r}")


def _find_by_title(conn: Connection, key: CanonicalKey) -> int | None:
    """
    Title keys also carry the first author's surname, and it is load-bearing:
    two unrelated papers can share a normalized title, and the first author is
    what separates them. R1.3 says to test that false-positive direction
    hardest.

    A paper with no authors stored still matches -- absence of evidence is not
    evidence of difference, and most rows have no authors persisted, so
    rejecting them would make dedup match nothing.

    The surname comparison happens in Python because SQLite cannot extract the
    last word of a name. Candidates are few: `title_norm` is indexed.
    """
    _, title_norm, surname, year = key
    rows = conn.execute(
        text(
            "SELECT p.id, a.name FROM papers p"
            " LEFT JOIN paper_authors pa ON pa.paper_id = p.id AND pa.position = 0"
            " LEFT JOIN authors a ON a.id = pa.author_id"
            " WHERE p.title_norm = :title_norm"
            "   AND (:year IS NULL OR p.year IS NULL OR p.year = :year)"
            " ORDER BY p.id"
        ),
        {"title_norm": title_norm, "year": year},
    ).fetchall()

    wanted = surname_of(surname) if surname else None
    for paper_id, first_author in rows:
        if wanted is None or first_author is None:
            return int(paper_id)
        if surname_of(first_author) == wanted:
            return int(paper_id)
    return None


__all__ = [
    "CanonicalKey",
    "find_by_canonical_key",
    "find_by_s2_id",
    "get_papers_by_ids",
    "set_authors",
    "surname_of",
    "upsert_paper",
    "upsert_stub",
]
