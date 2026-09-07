"""
R1.8 -- `build_pool`, the union that makes `anchor_overlap` measurable.

BUILD.md's verification, both halves:

  * "a candidate reachable from 3 frontier nodes gets anchor_overlap == 3"
  * "a hub node produces zero forward candidates and logs HUB_SKIP_FORWARD"

The first is the design. Per-node top-K is the obvious implementation and it
silently destroys the signal: a paper cited by three of your seeds is far
stronger evidence than three papers each cited once, and taking the best K per
node throws that away before anything can measure it.

The second is the anti-explosion guard. Attention Is All You Need has 191,436
citations; expanding forward from it would return an arbitrary slice of a fifth
of modern ML. Backward from a hub stays allowed -- a hub's own bibliography is
finite and unusually informative.
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
    db = tmp_path / "pool.db"
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
# anchor_overlap -- BUILD.md's first named case
# --------------------------------------------------------------------------


def test_a_candidate_cited_by_three_frontier_nodes_has_overlap_three(
    conn: Connection,
) -> None:
    """BUILD.md's verification, verbatim."""
    anchors = [_add(conn, f"anchor{i}") for i in range(3)]
    shared = _add(conn, "shared")
    for anchor in anchors:
        edges_repo.upsert_edge(conn, anchor, shared, "BACKWARD")

    pool = build_pool(conn, SESSION, anchors, cfg, AS_OF)
    entry = next(e for e in pool if e.paper_id == shared)
    assert entry.anchor_overlap == 3


def test_a_candidate_cited_once_has_overlap_one(conn: Connection) -> None:
    anchors = [_add(conn, f"anchor{i}") for i in range(3)]
    lonely = _add(conn, "lonely")
    edges_repo.upsert_edge(conn, anchors[0], lonely, "BACKWARD")

    pool = build_pool(conn, SESSION, anchors, cfg, AS_OF)
    assert next(e for e in pool if e.paper_id == lonely).anchor_overlap == 1


def test_the_pool_is_a_union_not_a_per_node_top_k(conn: Connection) -> None:
    """
    Every reachable candidate appears exactly once. A per-node top-K would both
    drop candidates and count the survivors more than once.
    """
    anchors = [_add(conn, f"anchor{i}") for i in range(2)]
    candidates = [_add(conn, f"cand{i}") for i in range(10)]
    for anchor in anchors:
        for cand in candidates:
            edges_repo.upsert_edge(conn, anchor, cand, "BACKWARD")

    pool = build_pool(conn, SESSION, anchors, cfg, AS_OF)
    ids = [e.paper_id for e in pool]
    assert sorted(ids) == sorted(candidates)
    assert len(ids) == len(set(ids))
    assert all(e.anchor_overlap == 2 for e in pool)


def test_source_ids_record_which_anchors_reached_the_candidate(
    conn: Connection,
) -> None:
    """The per-source cap in R1.10 allocates against these."""
    anchors = [_add(conn, f"anchor{i}") for i in range(3)]
    shared = _add(conn, "shared")
    for anchor in anchors[:2]:
        edges_repo.upsert_edge(conn, anchor, shared, "BACKWARD")

    pool = build_pool(conn, SESSION, anchors, cfg, AS_OF)
    entry = next(e for e in pool if e.paper_id == shared)
    assert set(entry.source_ids) == set(anchors[:2])


def test_frontier_members_are_not_their_own_candidates(conn: Connection) -> None:
    """A paper already in the frontier is not a discovery."""
    a = _add(conn, "a")
    b = _add(conn, "b")
    edges_repo.upsert_edge(conn, a, b, "BACKWARD")
    pool = build_pool(conn, SESSION, [a, b], cfg, AS_OF)
    assert [e.paper_id for e in pool] == []


def test_an_empty_frontier_yields_an_empty_pool(conn: Connection) -> None:
    assert build_pool(conn, SESSION, [], cfg, AS_OF) == []


def test_the_pool_is_deterministically_ordered(conn: Connection) -> None:
    """CLAUDE.md rule 7 -- never rely on set or dict iteration order."""
    anchor = _add(conn, "anchor")
    cands = [_add(conn, f"c{i}") for i in range(20)]
    for c in cands:
        edges_repo.upsert_edge(conn, anchor, c, "BACKWARD")
    first = [e.paper_id for e in build_pool(conn, SESSION, [anchor], cfg, AS_OF)]
    second = [e.paper_id for e in build_pool(conn, SESSION, [anchor], cfg, AS_OF)]
    assert first == second == sorted(first)


# --------------------------------------------------------------------------
# Direction
# --------------------------------------------------------------------------


def test_a_reference_is_a_backward_candidate(conn: Connection) -> None:
    anchor = _add(conn, "anchor")
    cited = _add(conn, "cited")
    edges_repo.upsert_edge(conn, anchor, cited, "BACKWARD")
    (entry,) = build_pool(conn, SESSION, [anchor], cfg, AS_OF)
    assert entry.direction == "BACKWARD"


def test_a_citing_paper_is_a_forward_candidate(conn: Connection) -> None:
    anchor = _add(conn, "anchor")
    citing = _add(conn, "citing")
    edges_repo.upsert_edge(conn, citing, anchor, "FORWARD")
    (entry,) = build_pool(conn, SESSION, [anchor], cfg, AS_OF)
    assert entry.direction == "FORWARD"


# --------------------------------------------------------------------------
# The hub guard -- BUILD.md's second named case
# --------------------------------------------------------------------------


def test_a_hub_produces_no_forward_candidates(conn: Connection) -> None:
    """
    BUILD.md's verification. Expanding forward from a 191k-citation paper would
    return an arbitrary slice of a fifth of modern ML.
    """
    hub = _add(conn, "hub", citations=191_436)
    citing = _add(conn, "citing")
    edges_repo.upsert_edge(conn, citing, hub, "FORWARD")

    pool = build_pool(conn, SESSION, [hub], cfg, AS_OF)
    assert [e for e in pool if e.direction == "FORWARD"] == []


def test_a_hub_still_yields_backward_candidates(conn: Connection) -> None:
    """A hub's own bibliography is finite and unusually informative."""
    hub = _add(conn, "hub", citations=191_436)
    cited = _add(conn, "cited")
    edges_repo.upsert_edge(conn, hub, cited, "BACKWARD")

    pool = build_pool(conn, SESSION, [hub], cfg, AS_OF)
    assert [e.paper_id for e in pool] == [cited]


def test_the_hub_skip_is_logged(conn: Connection, caplog: pytest.LogCaptureFixture) -> None:
    """BUILD.md: "logs HUB_SKIP_FORWARD". A silent skip is indistinguishable
    from a paper that genuinely has no citations."""
    hub = _add(conn, "hub", citations=191_436)
    citing = _add(conn, "citing")
    edges_repo.upsert_edge(conn, citing, hub, "FORWARD")

    with caplog.at_level("INFO"):
        build_pool(conn, SESSION, [hub], cfg, AS_OF)
    assert "HUB_SKIP_FORWARD" in caplog.text


def test_the_hub_threshold_comes_from_config(conn: Connection) -> None:
    below = _add(conn, "below", citations=cfg.forward_expand_max - 1)
    citing = _add(conn, "citing")
    edges_repo.upsert_edge(conn, citing, below, "FORWARD")
    assert len(build_pool(conn, SESSION, [below], cfg, AS_OF)) == 1


def test_only_the_hub_anchor_loses_its_forward_edges(conn: Connection) -> None:
    """The guard is per anchor, not a global switch."""
    hub = _add(conn, "hub", citations=191_436)
    normal = _add(conn, "normal", citations=50)
    citing_hub = _add(conn, "citing_hub")
    citing_normal = _add(conn, "citing_normal")
    edges_repo.upsert_edge(conn, citing_hub, hub, "FORWARD")
    edges_repo.upsert_edge(conn, citing_normal, normal, "FORWARD")

    pool = build_pool(conn, SESSION, [hub, normal], cfg, AS_OF)
    assert [e.paper_id for e in pool] == [citing_normal]


# --------------------------------------------------------------------------
# Exclusion
# --------------------------------------------------------------------------


def test_a_paper_already_in_the_graph_is_not_a_candidate(conn: Connection) -> None:
    from app.repo import graph as graph_repo

    anchor = _add(conn, "anchor")
    existing = _add(conn, "existing")
    edges_repo.upsert_edge(conn, anchor, existing, "BACKWARD")
    graph_repo.add_node(conn, SESSION, existing, "CANDIDATE", depth=1)

    assert build_pool(conn, SESSION, [anchor], cfg, AS_OF) == []


def test_a_tombstoned_paper_never_returns(conn: Connection) -> None:
    """
    PLAN.md section C: "Nothing silently re-enters the graph." R2.7 depends on
    this, and a removed paper reappearing is the most visible possible bug.
    """
    from app.repo import events as events_repo

    anchor = _add(conn, "anchor")
    removed = _add(conn, "removed")
    edges_repo.upsert_edge(conn, anchor, removed, "BACKWARD")
    events_repo.append_event(conn, SESSION, removed, "REMOVED")

    assert build_pool(conn, SESSION, [anchor], cfg, AS_OF) == []


def test_a_restored_paper_is_a_candidate_again(conn: Connection) -> None:
    from app.repo import events as events_repo

    anchor = _add(conn, "anchor")
    paper = _add(conn, "paper")
    edges_repo.upsert_edge(conn, anchor, paper, "BACKWARD")
    events_repo.append_event(conn, SESSION, paper, "REMOVED")
    events_repo.append_event(conn, SESSION, paper, "RESTORED")

    assert [e.paper_id for e in build_pool(conn, SESSION, [anchor], cfg, AS_OF)] == [paper]


def test_exclusion_is_per_session(conn: Connection) -> None:
    from sqlalchemy import text

    from app.repo import events as events_repo

    conn.execute(
        text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
    )
    anchor = _add(conn, "anchor")
    paper = _add(conn, "paper")
    edges_repo.upsert_edge(conn, anchor, paper, "BACKWARD")
    events_repo.append_event(conn, SESSION, paper, "REMOVED")

    assert build_pool(conn, SESSION, [anchor], cfg, AS_OF) == []
    assert [e.paper_id for e in build_pool(conn, 2, [anchor], cfg, AS_OF)] == [paper]
