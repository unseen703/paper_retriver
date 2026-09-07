"""
Fixes for the R1.3-R1.5 review findings.

All three are about the same thing: **the same paper must produce the same
canonical key regardless of where it came from.** Dedup compares a freshly
fetched S2 paper against what is already stored, so any asymmetry between those
two representations makes the comparison silently miss, and a missed dedup
becomes a duplicate node with split citation evidence.

1. `_row_to_paper` never populated `authors`, so a Paper loaded from the
   database keyed as `(..., None, year)` while the same paper from S2 keyed as
   `(..., 'lecun', year)`. Confirmed by running both.
2. Surname extraction existed twice -- `repo.papers.surname_of` and
   `services.dedup.first_author_surname` -- with independent regexes. They
   agreed on the day they were written and nothing made them keep agreeing,
   even though `find_by_canonical_key` compares the output of one against the
   other.
3. `CanonicalKey` was declared twice with different types: `tuple[Any, ...]` in
   the repo and a precise union in dedup.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

from app.db import make_engine
from app.models import Paper
from app.repo import papers as papers_repo
from app.services.dedup import canonical_key, first_author_surname

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[Connection]:
    db = tmp_path / "review.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as connection:
        yield connection
    engine.dispose()


def _s2_paper() -> Paper:
    return Paper(
        s2_paper_id="s1",
        title="Deep Learning",
        first_seen_at="2026-01-01",
        year=2015,
        authors=(("a-lecun", "Yann LeCun"), ("a-bengio", "Yoshua Bengio")),
    )


# --------------------------------------------------------------------------
# Finding 1 -- the key must not depend on the paper's provenance
# --------------------------------------------------------------------------


def test_a_round_tripped_paper_keeps_its_first_author(conn: Connection) -> None:
    fetched = _s2_paper()
    paper_id = papers_repo.upsert_paper(conn, fetched)
    papers_repo.set_authors(conn, paper_id, list(fetched.authors))
    (loaded,) = papers_repo.get_papers_by_ids(conn, [paper_id])
    assert first_author_surname(loaded) == "lecun"


def test_the_canonical_key_survives_a_database_round_trip(conn: Connection) -> None:
    """
    The finding itself. Dedup compares fetched papers against stored ones; if
    the two representations key differently, every title-keyed comparison
    silently misses.
    """
    fetched = _s2_paper()
    paper_id = papers_repo.upsert_paper(conn, fetched)
    papers_repo.set_authors(conn, paper_id, list(fetched.authors))
    (loaded,) = papers_repo.get_papers_by_ids(conn, [paper_id])
    assert canonical_key(loaded) == canonical_key(fetched)


def test_byline_order_survives_the_round_trip(conn: Connection) -> None:
    """position 0 must come back first, or the wrong surname enters the key."""
    fetched = _s2_paper()
    paper_id = papers_repo.upsert_paper(conn, fetched)
    papers_repo.set_authors(conn, paper_id, list(fetched.authors))
    (loaded,) = papers_repo.get_papers_by_ids(conn, [paper_id])
    assert loaded.authors[0][1] == "Yann LeCun"


def test_a_paper_with_no_stored_authors_still_loads(conn: Connection) -> None:
    paper_id = papers_repo.upsert_paper(conn, _s2_paper())
    (loaded,) = papers_repo.get_papers_by_ids(conn, [paper_id])
    assert loaded.authors == ()


def test_loading_many_papers_still_costs_one_query(conn: Connection) -> None:
    """The author join must not reintroduce the N+1 the last review removed."""
    from sqlalchemy import event

    ids = []
    for i in range(20):
        pid = papers_repo.upsert_paper(
            conn,
            Paper(s2_paper_id=f"p{i}", title=f"T{i}", first_seen_at="2026-01-01"),
        )
        papers_repo.set_authors(conn, pid, [(f"a{i}", f"Author {i}")])
        ids.append(pid)

    n = {"q": 0}

    def _count(*_a: object, **_k: object) -> None:
        n["q"] += 1

    event.listen(conn.engine, "before_cursor_execute", _count)
    try:
        loaded = papers_repo.get_papers_by_ids(conn, ids)
    finally:
        event.remove(conn.engine, "before_cursor_execute", _count)

    assert len(loaded) == 20
    assert n["q"] == 1


# --------------------------------------------------------------------------
# Finding 2 -- one surname implementation, not two
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Yann LeCun",
        "Geoffrey E. Hinton",
        "Jürgen Schmidhuber",
        "Kaiming He",
        "J. Smith-Jones",
        "Ilya Sutskever",
    ],
)
def test_the_repo_and_dedup_agree_on_every_surname(name: str) -> None:
    """
    find_by_canonical_key compares the repo's extraction against the key dedup
    built. Two independent regexes agreeing today is not the same as agreeing
    tomorrow, so they are now one function and this asserts it.
    """
    from app.models import surname_of

    paper = Paper(s2_paper_id="x", title="t", first_seen_at="d", authors=(("a1", name),))
    assert surname_of(name) == first_author_surname(paper)


def test_surname_extraction_lives_in_one_place() -> None:
    """models is the layer both repo/ and services/ already share."""
    from app.models import surname_of
    from app.repo.papers import surname_of as repo_surname

    assert repo_surname is surname_of


# --------------------------------------------------------------------------
# Finding 3 -- one CanonicalKey type
# --------------------------------------------------------------------------


def test_canonical_key_is_declared_once() -> None:
    from app.models import CanonicalKey as ModelKey
    from app.repo.papers import CanonicalKey as RepoKey
    from app.services.dedup import CanonicalKey as DedupKey

    assert RepoKey is ModelKey
    assert DedupKey is ModelKey


# --------------------------------------------------------------------------
# Finding 4 -- venue matching should not rebuild regexes per paper
# --------------------------------------------------------------------------


def test_venue_patterns_are_compiled_once() -> None:
    """
    A 2000-node expansion runs this per candidate; rebuilding 15 patterns each
    time is avoidable work on the hot path.
    """
    from app.services.filters.topic_filter import _VENUE_PATTERNS

    assert len(_VENUE_PATTERNS) >= 15
    assert all(hasattr(p, "search") for _, p in _VENUE_PATTERNS)


def test_venue_matching_still_works_after_precompilation() -> None:
    from app.config import filters as cfg
    from app.services.filters import is_core_venue

    assert is_core_venue("Neural Information Processing Systems", cfg.core_venues) is True
    assert is_core_venue("NeurIPS", cfg.core_venues) is True
    assert is_core_venue("Journal of Clinical Radiology", cfg.core_venues) is False
    assert is_core_venue(None, cfg.core_venues) is False
