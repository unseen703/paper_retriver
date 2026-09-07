"""
The R1.13 checkpoint's finding: stub writes were discarding data already in hand.

`upsert_stub(conn, s2_id, title, year)` stores three fields. But S2's nested
edge records carry far more -- `NEIGHBOR_FIELDS` requests externalIds, venue,
citationCount, publicationTypes and fieldsOfStudy, and they arrive in the same
response as the title. Throwing them away costs nothing at write time and
everything afterwards:

  * `arxiv_id` is gone, so the arXiv category cannot even be re-derived later
    without spending another API call on a paper that already answered.
  * `citation_count` reads 0, which zeroes the prescore's citations-per-year
    term for every candidate.

That last one is why the first checkpoint run scored all twenty candidates at
exactly 2.0: every prescore term except `anchor_overlap` was nulled by the write,
so "ranking" was a tie broken by paper_id.

PLAN.md's rule is "one row, **no metadata call**" -- do not spend an API request
on a paper that may never be recommendable. It does not say discard the metadata
that arrived for free.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

from app.clients.arxiv import ArxivRecord
from app.config import filters as cfg
from app.db import make_engine
from app.models import CrawlState, Paper
from app.repo import arxiv_meta
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.services.categories import CategoryResolver
from app.services.ingest import ingest_neighbour

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SESSION = 1
AS_OF = 2026


@pytest.fixture
def engine_and_conn(tmp_path: Path) -> Iterator[tuple[object, Connection]]:
    db = tmp_path / "stub.db"
    c = Config(str(ALEMBIC_INI))
    c.set_main_option("script_location", str(ALEMBIC_INI.parent))
    c.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(c, "head")
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    arxiv_meta.load(engine, [ArxivRecord("1802.05365", "cs.CL", ("cs.CL",), "2018-03-22")])
    with engine.begin() as conn:
        yield engine, conn
    engine.dispose()


def _neighbour() -> Paper:
    """A neighbour exactly as S2 returns it inside a references response."""
    return Paper(
        s2_paper_id="elmo",
        title="Deep Contextualized Word Representations",
        first_seen_at="2026-01-01",
        year=2018,
        arxiv_id="1802.05365",
        venue="North American Chapter of the Association for Computational Linguistics",
        citation_count=11000,
        doi="10.18653/v1/N18-1202",
        s2_fields=("Computer Science",),
    )


def _stored(conn: Connection) -> Paper:
    paper_id = papers_repo.find_by_s2_id(conn, "elmo")
    assert paper_id is not None
    (paper,) = papers_repo.get_papers_by_ids(conn, [paper_id])
    return paper


def _anchor(conn: Connection) -> int:
    anchor_id = papers_repo.upsert_paper(
        conn,
        Paper(s2_paper_id="anchor", title="Anchor", first_seen_at="2026-01-01", year=2019),
    )
    graph_repo.add_node(conn, SESSION, anchor_id, "SEED", depth=0)
    return anchor_id


# --------------------------------------------------------------------------
# What arrived for free must be kept
# --------------------------------------------------------------------------


def test_the_arxiv_id_survives_ingestion(engine_and_conn: tuple[object, Connection]) -> None:
    """
    Without it the category is unrecoverable: the join key is gone, so deriving
    it later would need another API call for data already received.
    """
    engine, conn = engine_and_conn
    ingest_neighbour(
        conn,
        SESSION,
        _anchor(conn),
        _neighbour(),
        "BACKWARD",
        cfg,
        AS_OF,
        resolver=CategoryResolver(engine),
    )
    assert _stored(conn).arxiv_id == "1802.05365"


def test_the_citation_count_survives_ingestion(
    engine_and_conn: tuple[object, Connection],
) -> None:
    """
    Zeroing this zeroes the prescore's citations-per-year term for every
    candidate, which is what made all twenty checkpoint candidates score 2.0.
    """
    engine, conn = engine_and_conn
    ingest_neighbour(
        conn,
        SESSION,
        _anchor(conn),
        _neighbour(),
        "BACKWARD",
        cfg,
        AS_OF,
        resolver=CategoryResolver(engine),
    )
    assert _stored(conn).citation_count == 11000


def test_the_venue_survives_ingestion(engine_and_conn: tuple[object, Connection]) -> None:
    """The topic filter's VENUE_CORE rung needs it."""
    engine, conn = engine_and_conn
    ingest_neighbour(
        conn,
        SESSION,
        _anchor(conn),
        _neighbour(),
        "BACKWARD",
        cfg,
        AS_OF,
        resolver=CategoryResolver(engine),
    )
    assert _stored(conn).venue is not None


def test_the_resolved_category_is_persisted(
    engine_and_conn: tuple[object, Connection],
) -> None:
    """
    The resolver enriches the in-memory object before filtering; the enriched
    value has to reach the row too, or `show` and R3 both have to re-join.
    """
    engine, conn = engine_and_conn
    ingest_neighbour(
        conn,
        SESSION,
        _anchor(conn),
        _neighbour(),
        "BACKWARD",
        cfg,
        AS_OF,
        resolver=CategoryResolver(engine),
    )
    assert _stored(conn).primary_arxiv_category == "cs.CL"


def test_the_doi_survives_ingestion(engine_and_conn: tuple[object, Connection]) -> None:
    """Dedup's strongest key. Losing it forces the weaker title key."""
    engine, conn = engine_and_conn
    ingest_neighbour(
        conn,
        SESSION,
        _anchor(conn),
        _neighbour(),
        "BACKWARD",
        cfg,
        AS_OF,
        resolver=CategoryResolver(engine),
    )
    assert _stored(conn).doi == "10.18653/v1/N18-1202"


# --------------------------------------------------------------------------
# Still a stub, and still cheap
# --------------------------------------------------------------------------


def test_the_paper_is_still_marked_a_stub(
    engine_and_conn: tuple[object, Connection],
) -> None:
    """
    Richer than before, but no metadata call was made. crawl_state records how
    the row was obtained, not how many columns it happens to have.
    """
    engine, conn = engine_and_conn
    ingest_neighbour(
        conn,
        SESSION,
        _anchor(conn),
        _neighbour(),
        "BACKWARD",
        cfg,
        AS_OF,
        resolver=CategoryResolver(engine),
    )
    assert _stored(conn).crawl_state is CrawlState.STUB


def test_a_stub_still_never_downgrades_a_full_record(
    engine_and_conn: tuple[object, Connection],
) -> None:
    """The R1.1 guarantee must survive this change."""
    engine, conn = engine_and_conn
    anchor_id = _anchor(conn)
    papers_repo.upsert_paper(
        conn,
        Paper(
            s2_paper_id="elmo",
            title="Deep Contextualized Word Representations",
            first_seen_at="2026-01-01",
            year=2018,
            abstract="Full abstract from a metadata call.",
            citation_count=11000,
            crawl_state=CrawlState.METADATA,
        ),
    )
    ingest_neighbour(
        conn,
        SESSION,
        anchor_id,
        _neighbour(),
        "BACKWARD",
        cfg,
        AS_OF,
        resolver=CategoryResolver(engine),
    )
    stored = _stored(conn)
    assert stored.crawl_state is CrawlState.METADATA
    assert stored.abstract == "Full abstract from a metadata call."


def test_a_neighbour_with_no_extra_fields_still_works(
    engine_and_conn: tuple[object, Connection],
) -> None:
    """S2 omits most fields on plenty of records; that must not raise."""
    engine, conn = engine_and_conn
    bare = Paper(s2_paper_id="bare", title="Bare Record", first_seen_at="2026-01-01", year=2020)
    ingest_neighbour(
        conn,
        SESSION,
        _anchor(conn),
        bare,
        "BACKWARD",
        cfg,
        AS_OF,
        resolver=CategoryResolver(engine),
    )
    paper_id = papers_repo.find_by_s2_id(conn, "bare")
    assert paper_id is not None
