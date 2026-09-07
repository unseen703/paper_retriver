"""
Fixes for the R1.6-R1.8 review findings.

The one that matters is `anchor_overlap` over-counting. It is defined as "how
many frontier nodes reached this paper" and it carries weight 2.0 -- the
dominant term in the prescore. A mutual citation (A cites X, X cites A) produces
two edges that both touch A, so a single anchor was counted twice. That is a
free +2.0, enough to outrank a candidate genuinely shared by two anchors, and it
would present as "the ranking is a bit odd" rather than as a bug.

Direction had the same shape of problem: when a candidate is reachable both
backward and forward, whichever edge the database happened to return first won.
R1.10 allocates a budget floor per direction, so that made budget allocation
depend on row order -- exactly what CLAUDE.md rule 7 forbids.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

from app.config import filters as cfg
from app.db import make_engine
from app.models import Paper
from app.repo import edges as edges_repo
from app.repo import papers as papers_repo
from app.services.candidates import build_pool

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SESSION = 1
AS_OF = 2026


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[Connection]:
    db = tmp_path / "r168.db"
    c = Config(str(ALEMBIC_INI))
    c.set_main_option("script_location", str(ALEMBIC_INI.parent))
    c.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(c, "head")
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as connection:
        yield connection
    engine.dispose()


def _add(conn: Connection, s2_id: str, *, citations: int = 100, year: int = 2020) -> int:
    return papers_repo.upsert_paper(
        conn,
        Paper(
            s2_paper_id=s2_id,
            title=f"Paper {s2_id}",
            first_seen_at="2026-01-01",
            year=year,
            citation_count=citations,
        ),
    )


# --------------------------------------------------------------------------
# Finding 1 -- anchor_overlap counts anchors, not edges
# --------------------------------------------------------------------------


def test_a_mutual_citation_from_one_anchor_counts_once(conn: Connection) -> None:
    """
    A cites X and X cites A. Two edges, one anchor. Overlap must be 1.
    Confirmed broken before the fix: overlap=2, sources=(1, 1).
    """
    a = _add(conn, "a")
    x = _add(conn, "x")
    edges_repo.upsert_edge(conn, a, x, "BACKWARD")
    edges_repo.upsert_edge(conn, x, a, "FORWARD")

    (entry,) = build_pool(conn, SESSION, [a], cfg, AS_OF)
    assert entry.anchor_overlap == 1
    assert entry.source_ids == (a,)


def test_source_ids_never_repeat_an_anchor(conn: Connection) -> None:
    a = _add(conn, "a")
    x = _add(conn, "x")
    edges_repo.upsert_edge(conn, a, x, "BACKWARD")
    edges_repo.upsert_edge(conn, x, a, "FORWARD")

    (entry,) = build_pool(conn, SESSION, [a], cfg, AS_OF)
    assert len(entry.source_ids) == len(set(entry.source_ids))


def test_overlap_still_counts_distinct_anchors_correctly(conn: Connection) -> None:
    """The fix must not break the case the feature exists for."""
    anchors = [_add(conn, f"anchor{i}") for i in range(3)]
    shared = _add(conn, "shared")
    for anchor in anchors:
        edges_repo.upsert_edge(conn, anchor, shared, "BACKWARD")

    (entry,) = build_pool(conn, SESSION, anchors, cfg, AS_OF)
    assert entry.anchor_overlap == 3
    assert set(entry.source_ids) == set(anchors)


def test_a_mutual_citation_across_two_anchors_still_counts_two(conn: Connection) -> None:
    """Deduplicate anchors, not candidates."""
    a = _add(conn, "a")
    b = _add(conn, "b")
    x = _add(conn, "x")
    edges_repo.upsert_edge(conn, a, x, "BACKWARD")
    edges_repo.upsert_edge(conn, x, a, "FORWARD")
    edges_repo.upsert_edge(conn, b, x, "BACKWARD")

    (entry,) = build_pool(conn, SESSION, [a, b], cfg, AS_OF)
    assert entry.anchor_overlap == 2


# --------------------------------------------------------------------------
# Finding 2 -- direction must not depend on row order
# --------------------------------------------------------------------------


def test_direction_is_deterministic_when_both_apply(conn: Connection) -> None:
    """
    R1.10 allocates a floor per direction, so letting row order decide would
    make budget allocation depend on insertion order.
    """
    a = _add(conn, "a")
    x = _add(conn, "x")
    edges_repo.upsert_edge(conn, a, x, "BACKWARD")
    edges_repo.upsert_edge(conn, x, a, "FORWARD")

    first = build_pool(conn, SESSION, [a], cfg, AS_OF)[0].direction
    second = build_pool(conn, SESSION, [a], cfg, AS_OF)[0].direction
    assert first == second == "BACKWARD"


def test_a_purely_forward_candidate_stays_forward(conn: Connection) -> None:
    a = _add(conn, "a")
    citing = _add(conn, "citing")
    edges_repo.upsert_edge(conn, citing, a, "FORWARD")
    assert build_pool(conn, SESSION, [a], cfg, AS_OF)[0].direction == "FORWARD"


# --------------------------------------------------------------------------
# Finding 3 -- no hardcoded current year in production code
# --------------------------------------------------------------------------


def test_build_pool_requires_an_explicit_as_of_year() -> None:
    """
    A default of 2026 is silently wrong from 2027 onward, and it would show up
    as every paper looking a year younger than it is. The caller owns the clock,
    exactly as type_filter already requires.
    """
    import inspect

    param = inspect.signature(build_pool).parameters["as_of_year"]
    assert param.default is inspect.Parameter.empty


def test_age_uses_the_supplied_year(conn: Connection) -> None:
    a = _add(conn, "a")
    x = _add(conn, "x", year=2020)
    edges_repo.upsert_edge(conn, a, x, "BACKWARD")
    assert build_pool(conn, SESSION, [a], cfg, 2030)[0].age_years == 10.0


# --------------------------------------------------------------------------
# Finding 4 -- the log file must not be version-controlled
# --------------------------------------------------------------------------


def test_the_runtime_log_is_gitignored() -> None:
    """
    data/app.log accumulates every S2 call ever made. It was tracked, so each
    run produced a spurious diff and the file would grow in history forever.
    """
    import subprocess

    proc = subprocess.run(
        ["git", "check-ignore", "-q", "data/app.log"], cwd=ROOT, capture_output=True
    )
    assert proc.returncode == 0, "data/app.log is not gitignored"


# --------------------------------------------------------------------------
# Finding 5 -- no bare assert on a production invariant
# --------------------------------------------------------------------------


def test_the_cascade_uses_no_bare_assert() -> None:
    """`python -O` strips asserts; an invariant that matters needs a raise."""
    source = (ROOT / "backend" / "app" / "services" / "filters" / "cascade.py").read_text(
        encoding="utf-8"
    )
    assert "assert " not in source
