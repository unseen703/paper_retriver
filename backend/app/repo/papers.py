"""
The `papers` table. **Global** -- no `session_id`, by contract.

A paper's title and year do not depend on which graph you are looking at, so
this module is deliberately unable to accept a session. BUILD.md puts it as
"that's the type system enforcing the boundary for you", and a test in
test_repo_papers.py inspects these signatures to keep it true.

The subtle rule here is that **`crawl_state` never regresses**. The same paper
arrives twice: once fetched in full, once as a bare neighbour inside somebody
else's reference list. If the stub write won, the abstract would be erased and
the paper would silently stop being scoreable, with nothing to indicate why.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from app.models import CrawlState, Paper, PaperType, VenueTier, normalize_title

# ("doi", value) | ("arxiv", value) | ("title", norm, surname, year)
CanonicalKey = tuple[Any, ...]

# Higher wins. A write may raise the crawl state but never lower it.
_CRAWL_RANK = {
    CrawlState.STUB: 0,
    CrawlState.METADATA: 1,
    CrawlState.REFS_DONE: 2,
    CrawlState.CITES_DONE: 2,
    CrawlState.EXPANDED: 3,
}

_COLUMNS = (
    "id, s2_paper_id, s2_corpus_id, canonical_paper_id, title, abstract, year,"
    " publication_date, venue, venue_tier, citation_count, reference_count,"
    " influential_citation_count, doi, arxiv_id, primary_arxiv_category,"
    " arxiv_categories, s2_fields, publication_types, paper_type, crawl_state,"
    " first_seen_at, metadata_fetched_at"
)


def _json_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        return tuple(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return ()


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
    )


def find_by_s2_id(conn: Connection, s2_paper_id: str) -> int | None:
    return conn.execute(
        text("SELECT id FROM papers WHERE s2_paper_id = :s2_id"), {"s2_id": s2_paper_id}
    ).scalar()


def _current_crawl_state(conn: Connection, s2_paper_id: str) -> CrawlState | None:
    raw = conn.execute(
        text("SELECT crawl_state FROM papers WHERE s2_paper_id = :s2_id"),
        {"s2_id": s2_paper_id},
    ).scalar()
    return CrawlState(raw) if raw else None


def upsert_paper(conn: Connection, paper: Paper) -> int:
    """Insert or update by `s2_paper_id`. Returns the internal surrogate id."""
    existing = _current_crawl_state(conn, paper.s2_paper_id)
    crawl_state = paper.crawl_state
    if existing is not None and _CRAWL_RANK[existing] > _CRAWL_RANK[crawl_state]:
        crawl_state = existing

    conn.execute(
        text(
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
            "   title = excluded.title,"
            "   title_norm = excluded.title_norm,"
            # COALESCE, not excluded: a later fetch with a narrower field set
            # must not blank out data an earlier, richer one supplied.
            "   abstract = COALESCE(excluded.abstract, papers.abstract),"
            "   year = COALESCE(excluded.year, papers.year),"
            "   publication_date = COALESCE(excluded.publication_date, papers.publication_date),"
            "   venue = COALESCE(excluded.venue, papers.venue),"
            "   venue_tier = COALESCE(excluded.venue_tier, papers.venue_tier),"
            # Counts default to 0 on a stub, so writing them unconditionally
            # would let a bare neighbour record reset a hub's 191k citations to
            # zero -- and the paper would then rank near the bottom forever.
            # Only a write at least as complete as what is stored may update.
            "   citation_count = CASE WHEN :is_downgrade THEN papers.citation_count"
            "     ELSE excluded.citation_count END,"
            "   reference_count = CASE WHEN :is_downgrade THEN papers.reference_count"
            "     ELSE excluded.reference_count END,"
            "   influential_citation_count = CASE WHEN :is_downgrade"
            "     THEN papers.influential_citation_count"
            "     ELSE excluded.influential_citation_count END,"
            "   doi = COALESCE(excluded.doi, papers.doi),"
            "   arxiv_id = COALESCE(excluded.arxiv_id, papers.arxiv_id),"
            "   primary_arxiv_category ="
            "     COALESCE(excluded.primary_arxiv_category, papers.primary_arxiv_category),"
            "   arxiv_categories = COALESCE(excluded.arxiv_categories, papers.arxiv_categories),"
            "   s2_fields = COALESCE(excluded.s2_fields, papers.s2_fields),"
            "   publication_types ="
            "     COALESCE(excluded.publication_types, papers.publication_types),"
            "   paper_type = CASE WHEN :is_downgrade THEN papers.paper_type"
            "     ELSE excluded.paper_type END,"
            "   crawl_state = excluded.crawl_state,"
            # first_seen_at records when the corpus first saw the paper, not
            # the most recent refetch. Never overwritten.
            "   metadata_fetched_at ="
            "     COALESCE(excluded.metadata_fetched_at, papers.metadata_fetched_at)"
        ),
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
            "crawl_state": crawl_state.value,
            # True when a lower-fidelity record is overwriting a richer one.
            "is_downgrade": crawl_state is not paper.crawl_state,
            "first_seen_at": paper.first_seen_at,
            "metadata_fetched_at": paper.metadata_fetched_at,
        },
    )
    paper_id = find_by_s2_id(conn, paper.s2_paper_id)
    assert paper_id is not None  # noqa: S101 - just inserted
    return paper_id


def upsert_stub(conn: Connection, s2_id: str, title: str, year: int | None) -> int:
    """
    Write the little we know about a nested neighbour.

    Never downgrades an existing row: `upsert_paper` keeps the higher
    crawl_state, and every optional column here is COALESCEd.
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


def _utcnow() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def get_papers_by_ids(conn: Connection, ids: list[int]) -> list[Paper]:
    """Returns papers in the order requested; unknown ids are skipped."""
    if not ids:
        return []
    found: dict[int, Paper] = {}
    for start in range(0, len(ids), 500):  # SQLite caps bound params at 999
        chunk = ids[start : start + 500]
        placeholders = ",".join(f":p{i}" for i in range(len(chunk)))
        rows = conn.execute(
            text(f"SELECT {_COLUMNS} FROM papers WHERE id IN ({placeholders})"),
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
        _, title_norm, _surname, year = key
        return conn.execute(
            text(
                "SELECT id FROM papers WHERE title_norm = :title_norm"
                " AND (:year IS NULL OR year IS NULL OR year = :year)"
                " ORDER BY id LIMIT 1"
            ),
            {"title_norm": title_norm, "year": year},
        ).scalar()
    raise ValueError(f"unknown canonical key kind: {kind!r}")


__all__ = [
    "CanonicalKey",
    "find_by_canonical_key",
    "find_by_s2_id",
    "get_papers_by_ids",
    "upsert_paper",
    "upsert_stub",
]
