"""
R1.1 -- repo/papers.py and repo/edges.py.

These two modules are the *global* half of the session_id contract: a paper's
title and year, and the fact that A cites B, do not depend on which graph you
are looking at. BUILD.md enforces that with the type system -- neither module
may accept `session_id` at all -- and one test here asserts exactly that by
inspecting the signatures, because a stray parameter is easy to add and
impossible to notice later.

Two behaviours carry real weight:

* **Every write is idempotent.** Expansion re-encounters the same paper through
  many paths. Running each write twice and asserting the row count is unchanged
  is BUILD.md's own instruction.
* **`discovered_via` merges rather than overwrites.** A paper found first as a
  reference and later as a citation is BOTH, and that provenance is what the
  direction-floor budget (R1.10) allocates against. A last-write-wins column
  silently destroys it.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from app.db import make_engine
from app.models import CrawlState, Paper
from app.repo import edges as edges_repo
from app.repo import papers as papers_repo

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[Connection]:
    db = tmp_path / "repo.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as connection:
        yield connection
    engine.dispose()


def _paper(s2_id: str = "s1", **over: object) -> Paper:
    base: dict[str, object] = {
        "s2_paper_id": s2_id,
        "title": "Attention Is All You Need",
        "first_seen_at": "2026-01-01T00:00:00Z",
    }
    base.update(over)
    return Paper(**base)  # type: ignore[arg-type]


def _count(conn: Connection, table: str) -> int:
    return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)


# --------------------------------------------------------------------------
# The contract: these modules are global, so no session_id anywhere
# --------------------------------------------------------------------------


@pytest.mark.parametrize("module", [papers_repo, edges_repo], ids=lambda m: m.__name__)
def test_no_public_function_accepts_a_session_id(module: object) -> None:
    """
    BUILD.md: "Functions in repo/papers.py and repo/edges.py must NOT accept
    session_id at all -- that's the type system enforcing the boundary."
    """
    offenders = []
    for name, fn in inspect.getmembers(module, inspect.isfunction):
        if name.startswith("_") or fn.__module__ != module.__name__:  # type: ignore[attr-defined]
            continue
        if "session_id" in inspect.signature(fn).parameters:
            offenders.append(name)
    assert not offenders, f"session_id leaked into global repo: {offenders}"


# --------------------------------------------------------------------------
# upsert_paper
# --------------------------------------------------------------------------


def test_upsert_paper_returns_an_internal_id(conn: Connection) -> None:
    """A surrogate id, not the S2 id -- dedup merges rewrite canonical_paper_id."""
    paper_id = papers_repo.upsert_paper(conn, _paper())
    assert isinstance(paper_id, int)
    assert paper_id > 0


def test_upsert_paper_is_idempotent(conn: Connection) -> None:
    first = papers_repo.upsert_paper(conn, _paper())
    second = papers_repo.upsert_paper(conn, _paper())
    assert first == second
    assert _count(conn, "papers") == 1


def test_upsert_paper_updates_changed_metadata(conn: Connection) -> None:
    """A refetch at the same crawl_state carries newer counts through."""
    paper_id = papers_repo.upsert_paper(
        conn, _paper(citation_count=10, crawl_state=CrawlState.METADATA)
    )
    papers_repo.upsert_paper(
        conn, _paper(citation_count=99, venue="NeurIPS", crawl_state=CrawlState.METADATA)
    )
    (stored,) = papers_repo.get_papers_by_ids(conn, [paper_id])
    assert stored.citation_count == 99
    assert stored.venue == "NeurIPS"


def test_first_seen_at_is_never_overwritten(conn: Connection) -> None:
    """It records when the corpus first saw the paper, not the last refetch."""
    paper_id = papers_repo.upsert_paper(conn, _paper(first_seen_at="2026-01-01T00:00:00Z"))
    papers_repo.upsert_paper(conn, _paper(first_seen_at="2026-09-07T00:00:00Z"))
    (stored,) = papers_repo.get_papers_by_ids(conn, [paper_id])
    assert stored.first_seen_at == "2026-01-01T00:00:00Z"


def test_title_norm_is_computed_on_write(conn: Connection) -> None:
    """Dedup indexes on it, so the repo cannot rely on callers supplying it."""
    paper_id = papers_repo.upsert_paper(conn, _paper(title="  Deep   RESIDUAL Learning!  "))
    stored = conn.execute(
        text("SELECT title_norm FROM papers WHERE id = :id"), {"id": paper_id}
    ).scalar()
    assert stored == "deep residual learning"


def test_json_columns_round_trip(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(
        conn,
        _paper(
            arxiv_categories=("cs.LG", "cs.CV"),
            s2_fields=("Computer Science",),
            publication_types=("JournalArticle",),
        ),
    )
    (stored,) = papers_repo.get_papers_by_ids(conn, [paper_id])
    assert stored.arxiv_categories == ("cs.LG", "cs.CV")
    assert stored.s2_fields == ("Computer Science",)
    assert stored.publication_types == ("JournalArticle",)


# --------------------------------------------------------------------------
# Stubs -- crawl_state must never regress
# --------------------------------------------------------------------------


def test_upsert_stub_creates_a_minimal_row(conn: Connection) -> None:
    paper_id = papers_repo.upsert_stub(conn, "s-new", "Some Referenced Paper", 2020)
    (stored,) = papers_repo.get_papers_by_ids(conn, [paper_id])
    assert stored.crawl_state is CrawlState.STUB
    assert stored.year == 2020


def test_a_stub_never_downgrades_an_existing_full_record(conn: Connection) -> None:
    """
    The important one. A paper we already fetched in full is re-encountered as
    a nested neighbour in someone else's reference list. Letting the stub write
    win would erase the abstract and reset crawl_state, and the paper would
    silently stop being scoreable.
    """
    paper_id = papers_repo.upsert_paper(
        conn,
        _paper(
            abstract="The dominant sequence transduction models...",
            crawl_state=CrawlState.METADATA,
            citation_count=191436,
        ),
    )
    papers_repo.upsert_stub(conn, "s1", "Attention Is All You Need", 2017)
    (stored,) = papers_repo.get_papers_by_ids(conn, [paper_id])
    assert stored.crawl_state is CrawlState.METADATA
    assert stored.abstract is not None
    assert stored.citation_count == 191436


def test_a_full_record_upgrades_an_existing_stub(conn: Connection) -> None:
    paper_id = papers_repo.upsert_stub(conn, "s1", "Attention Is All You Need", 2017)
    papers_repo.upsert_paper(conn, _paper(abstract="Full text.", crawl_state=CrawlState.METADATA))
    (stored,) = papers_repo.get_papers_by_ids(conn, [paper_id])
    assert stored.crawl_state is CrawlState.METADATA
    assert stored.abstract == "Full text."


def test_upsert_stub_is_idempotent(conn: Connection) -> None:
    a = papers_repo.upsert_stub(conn, "s1", "T", 2020)
    b = papers_repo.upsert_stub(conn, "s1", "T", 2020)
    assert a == b
    assert _count(conn, "papers") == 1


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------


def test_get_papers_by_ids_preserves_the_requested_order(conn: Connection) -> None:
    ids = [papers_repo.upsert_paper(conn, _paper(f"s{i}", title=f"T{i}")) for i in range(3)]
    got = papers_repo.get_papers_by_ids(conn, [ids[2], ids[0], ids[1]])
    assert [p.s2_paper_id for p in got] == ["s2", "s0", "s1"]


def test_get_papers_by_ids_skips_unknown_ids(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(conn, _paper())
    assert len(papers_repo.get_papers_by_ids(conn, [paper_id, 9999])) == 1


def test_get_papers_by_ids_with_no_ids_returns_empty(conn: Connection) -> None:
    assert papers_repo.get_papers_by_ids(conn, []) == []


def test_find_by_canonical_key_on_doi(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(conn, _paper(doi="10.5555/3295222"))
    assert papers_repo.find_by_canonical_key(conn, ("doi", "10.5555/3295222")) == paper_id


def test_doi_lookup_is_case_insensitive(conn: Connection) -> None:
    """DOIs are case-insensitive by spec, and S2 is inconsistent about case."""
    paper_id = papers_repo.upsert_paper(conn, _paper(doi="10.1109/CVPR.2016.90"))
    assert papers_repo.find_by_canonical_key(conn, ("doi", "10.1109/cvpr.2016.90")) == paper_id


def test_find_by_canonical_key_on_arxiv_id(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(conn, _paper(arxiv_id="1706.03762"))
    assert papers_repo.find_by_canonical_key(conn, ("arxiv", "1706.03762")) == paper_id


def test_find_by_canonical_key_on_title(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(conn, _paper(title="Attention Is All You Need", year=2017))
    key = ("title", "attention is all you need", None, 2017)
    assert papers_repo.find_by_canonical_key(conn, key) == paper_id


def test_find_by_canonical_key_returns_none_when_absent(conn: Connection) -> None:
    assert papers_repo.find_by_canonical_key(conn, ("doi", "10.0000/nope")) is None


def test_find_by_s2_id(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(conn, _paper("abc123"))
    assert papers_repo.find_by_s2_id(conn, "abc123") == paper_id
    assert papers_repo.find_by_s2_id(conn, "nope") is None


# --------------------------------------------------------------------------
# Edges -- provenance merge is the whole point
# --------------------------------------------------------------------------


@pytest.fixture
def two_papers(conn: Connection) -> tuple[int, int]:
    return (
        papers_repo.upsert_paper(conn, _paper("citing", title="Citing Paper")),
        papers_repo.upsert_paper(conn, _paper("cited", title="Cited Paper")),
    )


def test_upsert_edge_creates_one_row(conn: Connection, two_papers: tuple[int, int]) -> None:
    citing, cited = two_papers
    edges_repo.upsert_edge(conn, citing, cited, "BACKWARD")
    assert _count(conn, "edges") == 1


def test_upsert_edge_is_idempotent(conn: Connection, two_papers: tuple[int, int]) -> None:
    citing, cited = two_papers
    edges_repo.upsert_edge(conn, citing, cited, "BACKWARD")
    edges_repo.upsert_edge(conn, citing, cited, "BACKWARD")
    assert _count(conn, "edges") == 1


def test_backward_then_forward_merges_to_both(
    conn: Connection, two_papers: tuple[int, int]
) -> None:
    """
    BUILD.md's named case. Provenance is what the direction-floor budget
    allocates against, so a last-write-wins column would destroy it.
    """
    citing, cited = two_papers
    edges_repo.upsert_edge(conn, citing, cited, "BACKWARD")
    edges_repo.upsert_edge(conn, citing, cited, "FORWARD")
    (edge,) = edges_repo.get_edges_for(conn, [citing])
    assert edge.discovered_via == "BOTH"


def test_the_same_direction_twice_stays_that_direction(
    conn: Connection, two_papers: tuple[int, int]
) -> None:
    citing, cited = two_papers
    edges_repo.upsert_edge(conn, citing, cited, "FORWARD")
    edges_repo.upsert_edge(conn, citing, cited, "FORWARD")
    (edge,) = edges_repo.get_edges_for(conn, [citing])
    assert edge.discovered_via == "FORWARD"


def test_both_is_absorbing(conn: Connection, two_papers: tuple[int, int]) -> None:
    citing, cited = two_papers
    edges_repo.upsert_edge(conn, citing, cited, "BACKWARD")
    edges_repo.upsert_edge(conn, citing, cited, "FORWARD")
    edges_repo.upsert_edge(conn, citing, cited, "BACKWARD")
    (edge,) = edges_repo.get_edges_for(conn, [citing])
    assert edge.discovered_via == "BOTH"


def test_is_influential_is_ored_never_cleared(
    conn: Connection, two_papers: tuple[int, int]
) -> None:
    """One endpoint reporting influential is enough; the other must not clear it."""
    citing, cited = two_papers
    edges_repo.upsert_edge(conn, citing, cited, "BACKWARD", is_influential=True)
    edges_repo.upsert_edge(conn, citing, cited, "FORWARD", is_influential=False)
    (edge,) = edges_repo.get_edges_for(conn, [citing])
    assert edge.is_influential is True


def test_intents_are_unioned_and_ordered(conn: Connection, two_papers: tuple[int, int]) -> None:
    citing, cited = two_papers
    edges_repo.upsert_edge(conn, citing, cited, "BACKWARD", intents=("methodology",))
    edges_repo.upsert_edge(conn, citing, cited, "FORWARD", intents=("background", "methodology"))
    (edge,) = edges_repo.get_edges_for(conn, [citing])
    # Sorted, so the stored value is deterministic rather than set-ordered.
    assert edge.intents == ("background", "methodology")


def test_first_seen_at_on_an_edge_is_not_overwritten(
    conn: Connection, two_papers: tuple[int, int]
) -> None:
    citing, cited = two_papers
    edges_repo.upsert_edge(conn, citing, cited, "BACKWARD", first_seen_at="2026-01-01")
    edges_repo.upsert_edge(conn, citing, cited, "FORWARD", first_seen_at="2026-09-07")
    (edge,) = edges_repo.get_edges_for(conn, [citing])
    assert edge.first_seen_at == "2026-01-01"


def test_a_self_citation_is_rejected_before_the_insert(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(conn, _paper())
    with pytest.raises(ValueError, match="self"):
        edges_repo.upsert_edge(conn, paper_id, paper_id, "BACKWARD")
    assert _count(conn, "edges") == 0


def test_an_unknown_direction_is_rejected(conn: Connection, two_papers: tuple[int, int]) -> None:
    citing, cited = two_papers
    with pytest.raises(ValueError):
        edges_repo.upsert_edge(conn, citing, cited, "SIDEWAYS")


# --------------------------------------------------------------------------
# get_edges_for
# --------------------------------------------------------------------------


def test_get_edges_for_finds_both_directions(conn: Connection) -> None:
    """A node's neighbourhood is what it cites AND what cites it."""
    a = papers_repo.upsert_paper(conn, _paper("a", title="A"))
    b = papers_repo.upsert_paper(conn, _paper("b", title="B"))
    c = papers_repo.upsert_paper(conn, _paper("c", title="C"))
    edges_repo.upsert_edge(conn, a, b, "BACKWARD")  # a cites b
    edges_repo.upsert_edge(conn, c, a, "FORWARD")  # c cites a
    pairs = {(e.citing_id, e.cited_id) for e in edges_repo.get_edges_for(conn, [a])}
    assert pairs == {(a, b), (c, a)}


def test_get_edges_for_returns_each_edge_once(conn: Connection) -> None:
    """Asking for both endpoints of one edge must not duplicate it."""
    a = papers_repo.upsert_paper(conn, _paper("a", title="A"))
    b = papers_repo.upsert_paper(conn, _paper("b", title="B"))
    edges_repo.upsert_edge(conn, a, b, "BACKWARD")
    assert len(edges_repo.get_edges_for(conn, [a, b])) == 1


def test_get_edges_for_with_no_ids_returns_empty(conn: Connection) -> None:
    assert edges_repo.get_edges_for(conn, []) == []


def test_get_edges_for_chunks_past_the_sqlite_parameter_cap(conn: Connection) -> None:
    """A 2000-node graph would otherwise exceed SQLite's 999-parameter limit."""
    ids = [papers_repo.upsert_paper(conn, _paper(f"s{i}", title=f"T{i}")) for i in range(600)]
    for i in range(0, 598, 2):
        edges_repo.upsert_edge(conn, ids[i], ids[i + 1], "BACKWARD")
    assert len(edges_repo.get_edges_for(conn, ids)) == 299
