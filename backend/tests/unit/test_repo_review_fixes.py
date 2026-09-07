"""
Fixes for the R1.1 code review findings.

Three of them, all in the repo layer:

1. **`upsert_edge` issued 2 statements per edge** (measured: 200 edges -> 400
   statements). It read the prior row to union `intents` even when the caller
   supplied none, which is the common case for citation edges. One hub's
   reference fetch is 200 edges and a full expansion is thousands, so this sits
   directly on the hot path R1.11 will hammer.

2. **`upsert_paper` issued 3 statements per paper** -- a SELECT for the current
   crawl_state, the INSERT, and a SELECT to recover the id. SQLite 3.35+
   supports RETURNING, and the crawl_state guard folds into the same CASE
   expression that already protects the counts.

3. **`find_by_canonical_key` silently discarded the author surname.** The key is
   `("title", norm, surname, year)` and the surname was destructured into `_`
   and never used, so two unrelated papers sharing a normalized title resolved
   to the same id. R1.3 says to test the false-positive direction hardest; this
   was that direction, unguarded.

Statement counts are asserted rather than described. A performance claim nobody
measures is a performance claim that quietly regresses.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, event, text

from app.db import make_engine
from app.models import Paper
from app.repo import edges as edges_repo
from app.repo import papers as papers_repo

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"


class Counter:
    """Counts SQL statements actually issued on an engine."""

    def __init__(self, engine: Engine) -> None:
        self.n = 0
        event.listen(engine, "before_cursor_execute", self._on)

    def _on(self, *_args: object, **_kw: object) -> None:
        self.n += 1

    def reset(self) -> None:
        self.n = 0


@pytest.fixture
def counted(tmp_path: Path) -> Iterator[tuple[Connection, Counter]]:
    db = tmp_path / "counted.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    counter = Counter(engine)
    with engine.begin() as conn:
        yield conn, counter
    engine.dispose()


def _paper(s2_id: str = "s1", **over: object) -> Paper:
    base: dict[str, object] = {
        "s2_paper_id": s2_id,
        "title": "Attention Is All You Need",
        "first_seen_at": "2026-01-01T00:00:00Z",
    }
    base.update(over)
    return Paper(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Finding 1 -- upsert_edge on the hot path
# --------------------------------------------------------------------------


def test_upsert_edge_without_intents_is_one_statement(
    counted: tuple[Connection, Counter],
) -> None:
    """The common case: a citation edge carrying no intents needs no read."""
    conn, counter = counted
    a = papers_repo.upsert_paper(conn, _paper("a", title="A"))
    b = papers_repo.upsert_paper(conn, _paper("b", title="B"))
    counter.reset()
    edges_repo.upsert_edge(conn, a, b, "BACKWARD")
    assert counter.n == 1


def test_a_reference_batch_costs_one_statement_per_edge(
    counted: tuple[Connection, Counter],
) -> None:
    """200 references -- one hub's bibliography -- must not cost 400 round trips."""
    conn, counter = counted
    root = papers_repo.upsert_paper(conn, _paper("root", title="Root"))
    ids = [papers_repo.upsert_paper(conn, _paper(f"r{i}", title=f"R{i}")) for i in range(200)]
    counter.reset()
    for cited in ids:
        edges_repo.upsert_edge(conn, root, cited, "BACKWARD")
    assert counter.n == 200


def test_upsert_edge_with_intents_reads_once_to_union(
    counted: tuple[Connection, Counter],
) -> None:
    """
    Unioning intents genuinely needs the prior value, so this path stays at 2.
    Asserted so the cost is a documented choice rather than an accident.
    """
    conn, counter = counted
    a = papers_repo.upsert_paper(conn, _paper("a", title="A"))
    b = papers_repo.upsert_paper(conn, _paper("b", title="B"))
    counter.reset()
    edges_repo.upsert_edge(conn, a, b, "BACKWARD", intents=("methodology",))
    assert counter.n == 2


def test_the_direction_merge_still_works_without_a_python_read(
    counted: tuple[Connection, Counter],
) -> None:
    """BACKWARD + FORWARD -> BOTH, now decided by SQL rather than a prior SELECT."""
    conn, _ = counted
    a = papers_repo.upsert_paper(conn, _paper("a", title="A"))
    b = papers_repo.upsert_paper(conn, _paper("b", title="B"))
    edges_repo.upsert_edge(conn, a, b, "BACKWARD")
    edges_repo.upsert_edge(conn, a, b, "FORWARD")
    (edge,) = edges_repo.get_edges_for(conn, [a])
    assert edge.discovered_via == "BOTH"


def test_intents_still_union_when_added_incrementally(
    counted: tuple[Connection, Counter],
) -> None:
    conn, _ = counted
    a = papers_repo.upsert_paper(conn, _paper("a", title="A"))
    b = papers_repo.upsert_paper(conn, _paper("b", title="B"))
    edges_repo.upsert_edge(conn, a, b, "BACKWARD", intents=("methodology",))
    edges_repo.upsert_edge(conn, a, b, "FORWARD", intents=("background",))
    (edge,) = edges_repo.get_edges_for(conn, [a])
    assert edge.intents == ("background", "methodology")


def test_intents_survive_a_later_write_that_carries_none(
    counted: tuple[Connection, Counter],
) -> None:
    """The read-skipping path must not silently erase intents already stored."""
    conn, _ = counted
    a = papers_repo.upsert_paper(conn, _paper("a", title="A"))
    b = papers_repo.upsert_paper(conn, _paper("b", title="B"))
    edges_repo.upsert_edge(conn, a, b, "BACKWARD", intents=("methodology",))
    edges_repo.upsert_edge(conn, a, b, "FORWARD")
    (edge,) = edges_repo.get_edges_for(conn, [a])
    assert edge.intents == ("methodology",)


# --------------------------------------------------------------------------
# Finding 7 -- SQLite MAX() is NULL-poisoning
# --------------------------------------------------------------------------


def test_is_influential_survives_a_null_stored_value(
    counted: tuple[Connection, Counter],
) -> None:
    """
    SQLite's multi-argument MAX returns NULL if ANY argument is NULL
    (verified: SELECT MAX(NULL, 1) -> NULL). The column is nullable, so a row
    written by anything other than upsert_edge would silently lose the flag.
    """
    conn, _ = counted
    a = papers_repo.upsert_paper(conn, _paper("a", title="A"))
    b = papers_repo.upsert_paper(conn, _paper("b", title="B"))
    conn.execute(
        text(
            "INSERT INTO edges (citing_id, cited_id, discovered_via, is_influential,"
            " first_seen_at) VALUES (:a, :b, 'BACKWARD', NULL, '2026-01-01')"
        ),
        {"a": a, "b": b},
    )
    edges_repo.upsert_edge(conn, a, b, "FORWARD", is_influential=True)
    (edge,) = edges_repo.get_edges_for(conn, [a])
    assert edge.is_influential is True


# --------------------------------------------------------------------------
# Finding 2 -- upsert_paper statement count
# --------------------------------------------------------------------------


def test_upsert_paper_is_one_statement(counted: tuple[Connection, Counter]) -> None:
    """RETURNING removes the id lookup; the CASE removes the crawl_state read."""
    conn, counter = counted
    counter.reset()
    papers_repo.upsert_paper(conn, _paper())
    assert counter.n == 1


def test_a_candidate_batch_costs_one_statement_per_paper(
    counted: tuple[Connection, Counter],
) -> None:
    conn, counter = counted
    counter.reset()
    for i in range(100):
        papers_repo.upsert_paper(conn, _paper(f"p{i}", title=f"T{i}"))
    assert counter.n == 100


def test_crawl_state_still_never_regresses(counted: tuple[Connection, Counter]) -> None:
    """The behaviour the extra SELECT used to buy, now done in SQL."""
    from app.models import CrawlState

    conn, _ = counted
    paper_id = papers_repo.upsert_paper(
        conn, _paper(crawl_state=CrawlState.METADATA, abstract="Full.", citation_count=191436)
    )
    papers_repo.upsert_stub(conn, "s1", "Attention Is All You Need", 2017)
    (stored,) = papers_repo.get_papers_by_ids(conn, [paper_id])
    assert stored.crawl_state is CrawlState.METADATA
    assert stored.abstract == "Full."
    assert stored.citation_count == 191436


# --------------------------------------------------------------------------
# Finding 3 -- the canonical title key must honour the author surname
# --------------------------------------------------------------------------


@pytest.fixture
def conn(counted: tuple[Connection, Counter]) -> Connection:
    return counted[0]


def test_a_title_key_with_a_different_surname_does_not_match(conn: Connection) -> None:
    """
    The false-positive direction R1.3 says to test hardest. Two unrelated
    papers can share a normalized title; the first author is what separates
    them.
    """
    paper_id = papers_repo.upsert_paper(conn, _paper(title="Deep Learning", year=2015))
    papers_repo.set_authors(conn, paper_id, [("a-lecun", "Yann LeCun")])
    assert papers_repo.find_by_canonical_key(conn, ("title", "deep learning", "hinton", 2015)) is (
        None
    )


def test_a_title_key_with_the_same_surname_matches(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(conn, _paper(title="Deep Learning", year=2015))
    papers_repo.set_authors(conn, paper_id, [("a-lecun", "Yann LeCun")])
    key = ("title", "deep learning", "lecun", 2015)
    assert papers_repo.find_by_canonical_key(conn, key) == paper_id


def test_surname_matching_is_case_insensitive(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(conn, _paper(title="Deep Learning", year=2015))
    papers_repo.set_authors(conn, paper_id, [("a-lecun", "Yann LeCun")])
    key = ("title", "deep learning", "LeCun", 2015)
    assert papers_repo.find_by_canonical_key(conn, key) == paper_id


def test_a_paper_with_no_stored_authors_still_matches(conn: Connection) -> None:
    """
    Absence of evidence is not evidence of difference. Most papers have no
    authors persisted yet, and rejecting them would make dedup match nothing.
    """
    paper_id = papers_repo.upsert_paper(conn, _paper(title="Deep Learning", year=2015))
    key = ("title", "deep learning", "anybody", 2015)
    assert papers_repo.find_by_canonical_key(conn, key) == paper_id


def test_a_key_without_a_surname_matches_on_title_and_year(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(conn, _paper(title="Deep Learning", year=2015))
    papers_repo.set_authors(conn, paper_id, [("a-lecun", "Yann LeCun")])
    assert papers_repo.find_by_canonical_key(conn, ("title", "deep learning", None, 2015)) == (
        paper_id
    )


def test_only_the_first_author_is_consulted(conn: Connection) -> None:
    """position 0. A later co-author must not make an unrelated paper match."""
    paper_id = papers_repo.upsert_paper(conn, _paper(title="Deep Learning", year=2015))
    papers_repo.set_authors(
        conn, paper_id, [("a-lecun", "Yann LeCun"), ("a-hinton", "Geoffrey Hinton")]
    )
    assert papers_repo.find_by_canonical_key(conn, ("title", "deep learning", "hinton", 2015)) is (
        None
    )


# --------------------------------------------------------------------------
# set_authors -- the persistence the surname check needs
# --------------------------------------------------------------------------


def test_set_authors_is_idempotent(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(conn, _paper())
    papers_repo.set_authors(conn, paper_id, [("a1", "Ashish Vaswani")])
    papers_repo.set_authors(conn, paper_id, [("a1", "Ashish Vaswani")])
    n = conn.execute(text("SELECT COUNT(*) FROM paper_authors")).scalar()
    assert n == 1


def test_set_authors_records_position(conn: Connection) -> None:
    """First and last author both matter for R3's author signal."""
    paper_id = papers_repo.upsert_paper(conn, _paper())
    papers_repo.set_authors(conn, paper_id, [("a1", "First Author"), ("a2", "Second Author")])
    rows = conn.execute(
        text(
            "SELECT a.name, pa.position FROM paper_authors pa"
            " JOIN authors a ON a.id = pa.author_id"
            " WHERE pa.paper_id = :p ORDER BY pa.position"
        ),
        {"p": paper_id},
    ).fetchall()
    assert [r[1] for r in rows] == [0, 1]
    assert rows[0][0] == "First Author"


def test_set_authors_with_no_authors_is_a_noop(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(conn, _paper())
    papers_repo.set_authors(conn, paper_id, [])
    assert conn.execute(text("SELECT COUNT(*) FROM paper_authors")).scalar() == 0


def test_an_author_shared_by_two_papers_is_stored_once(conn: Connection) -> None:
    a = papers_repo.upsert_paper(conn, _paper("a", title="A"))
    b = papers_repo.upsert_paper(conn, _paper("b", title="B"))
    papers_repo.set_authors(conn, a, [("a1", "Ashish Vaswani")])
    papers_repo.set_authors(conn, b, [("a1", "Ashish Vaswani")])
    assert conn.execute(text("SELECT COUNT(*) FROM authors")).scalar() == 1
    assert conn.execute(text("SELECT COUNT(*) FROM paper_authors")).scalar() == 2


@pytest.mark.parametrize(
    ("full_name", "expected"),
    [
        ("Yann LeCun", "lecun"),
        ("Geoffrey E. Hinton", "hinton"),
        ("  Jürgen   Schmidhuber  ", "schmidhuber"),
        ("Ashish Vaswani", "vaswani"),
        ("Madonna", "madonna"),
        ("", ""),
    ],
)
def test_surname_extraction(full_name: str, expected: str) -> None:
    assert papers_repo.surname_of(full_name) == expected
