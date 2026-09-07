"""
R1.9 -- boundary papers.

BUILD.md calls the bib-coupling test below "the critical test -- this protects
the design", and it is the only test in the suite written for a feature that
does not exist yet (bibliographic coupling lands at R3).

The rule, from PLAN.md section E4: **the year floor is a graph-admission rule,
not an ingestion rule.**

    papers row      stored (as STUB)
    edges rows      stored, both directions
    graph_nodes row NEVER created
    bib-coupling    yes -- this is the whole point
    expanded from   no, never spend API budget on them

Why it matters, concretely. Two 2023 papers that both cite Adam (2014) are
coupled through it. Drop Adam from the corpus and that coupling is invisible.
The R1.7 checkpoint showed 15 of BERT's 50 references are pre-2015 -- GloVe,
word2vec, ImageNet -- so this is not a rare corner: it is a third of the
evidence, concentrated on exactly the shared foundations that make two modern
papers related.

The failure mode is silent. Everything still works; recommendations just get
quietly worse in a way no error surfaces.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from app.config import filters as cfg
from app.db import make_engine
from app.models import CrawlState, Outcome, Paper
from app.repo import edges as edges_repo
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.services.ingest import IngestStats, ingest_neighbour

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SESSION = 1
AS_OF = 2026


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[Connection]:
    db = tmp_path / "boundary.db"
    c = Config(str(ALEMBIC_INI))
    c.set_main_option("script_location", str(ALEMBIC_INI.parent))
    c.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(c, "head")
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as connection:
        yield connection
    engine.dispose()


def _paper(s2_id: str, year: int, **over: object) -> Paper:
    base: dict[str, object] = {
        "s2_paper_id": s2_id,
        "title": f"Paper {s2_id}",
        "first_seen_at": "2026-01-01",
        "year": year,
        "primary_arxiv_category": "cs.LG",
    }
    base.update(over)
    return Paper(**base)  # type: ignore[arg-type]


def _anchor(conn: Connection, s2_id: str, year: int = 2023) -> int:
    paper_id = papers_repo.upsert_paper(conn, _paper(s2_id, year))
    graph_repo.add_node(conn, SESSION, paper_id, "SEED", depth=0)
    return paper_id


# --------------------------------------------------------------------------
# THE CRITICAL TEST -- BUILD.md's words
# --------------------------------------------------------------------------


def test_two_modern_papers_sharing_a_2014_reference_are_still_coupled(
    conn: Connection,
) -> None:
    """
    BUILD.md: "two modern papers whose only shared reference is a 2014 paper
    must have non-zero bib_coupling. This test protects the design; without it
    the year floor silently degrades your best feature."

    bib_coupling itself arrives at R3. This asserts the structural precondition
    it will need: the shared old paper and both its edges exist in the corpus.
    """
    modern_a = _anchor(conn, "modern_a")
    modern_b = _anchor(conn, "modern_b")
    old = _paper("adam_2014", 2014, title="Adam: A Method for Stochastic Optimization")

    for citing in (modern_a, modern_b):
        ingest_neighbour(conn, SESSION, citing, old, "BACKWARD", cfg, AS_OF)

    # The coupling R3 will compute: papers cited by BOTH modern papers.
    shared = conn.execute(
        text(
            "SELECT e1.cited_id FROM edges e1 JOIN edges e2 ON e1.cited_id = e2.cited_id"
            " WHERE e1.citing_id = :a AND e2.citing_id = :b"
        ),
        {"a": modern_a, "b": modern_b},
    ).fetchall()
    assert len(shared) == 1, "the shared 2014 reference vanished; coupling is now zero"


def test_the_boundary_paper_is_stored_in_the_corpus(conn: Connection) -> None:
    anchor = _anchor(conn, "modern")
    ingest_neighbour(conn, SESSION, anchor, _paper("old", 2014), "BACKWARD", cfg, AS_OF)
    assert papers_repo.find_by_s2_id(conn, "old") is not None


def test_the_boundary_paper_gets_no_graph_node(conn: Connection) -> None:
    """Stored, but never drawn and never recommended."""
    anchor = _anchor(conn, "modern")
    ingest_neighbour(conn, SESSION, anchor, _paper("old", 2014), "BACKWARD", cfg, AS_OF)
    old_id = papers_repo.find_by_s2_id(conn, "old")
    assert old_id not in graph_repo.get_node_ids(conn, SESSION)


def test_the_edge_to_the_boundary_paper_survives(conn: Connection) -> None:
    """The edge is the coupling evidence. Losing it loses the whole point."""
    anchor = _anchor(conn, "modern")
    ingest_neighbour(conn, SESSION, anchor, _paper("old", 2014), "BACKWARD", cfg, AS_OF)
    old_id = papers_repo.find_by_s2_id(conn, "old")
    pairs = {(e.citing_id, e.cited_id) for e in edges_repo.get_edges_for(conn, [anchor])}
    assert (anchor, old_id) in pairs


def test_the_boundary_paper_is_stored_as_a_stub(conn: Connection) -> None:
    """
    PLAN.md: "one row, no metadata call". Never spend API budget fetching full
    metadata for a paper that can never be recommended.
    """
    anchor = _anchor(conn, "modern")
    ingest_neighbour(conn, SESSION, anchor, _paper("old", 2014), "BACKWARD", cfg, AS_OF)
    old_id = papers_repo.find_by_s2_id(conn, "old")
    (stored,) = papers_repo.get_papers_by_ids(conn, [old_id or 0])
    assert stored.crawl_state is CrawlState.STUB


def test_the_pre_era_decision_is_logged(conn: Connection) -> None:
    """A boundary paper must be explicable, not merely absent from the graph."""
    anchor = _anchor(conn, "modern")
    ingest_neighbour(conn, SESSION, anchor, _paper("old", 2014), "BACKWARD", cfg, AS_OF)
    reason = conn.execute(
        text("SELECT reason_code FROM filter_decisions ORDER BY id DESC LIMIT 1")
    ).scalar()
    assert reason == "PRE_ERA"


def test_a_boundary_paper_is_reported_as_such(conn: Connection) -> None:
    anchor = _anchor(conn, "modern")
    stats = IngestStats()
    ingest_neighbour(conn, SESSION, anchor, _paper("old", 2014), "BACKWARD", cfg, AS_OF, stats)
    assert stats.boundary == 1
    assert stats.admitted == 0


# --------------------------------------------------------------------------
# The same treatment for every other rejection, not just PRE_ERA
# --------------------------------------------------------------------------


def test_a_rejected_cs_cv_paper_also_keeps_its_edge(conn: Connection) -> None:
    """
    Structure is evidence regardless of why a paper was excluded. A cs.CV paper
    two of your seeds both cite still couples them.
    """
    anchor = _anchor(conn, "modern")
    cv = _paper("cv", 2020, primary_arxiv_category="cs.CV")
    ingest_neighbour(conn, SESSION, anchor, cv, "BACKWARD", cfg, AS_OF)

    cv_id = papers_repo.find_by_s2_id(conn, "cv")
    assert cv_id is not None
    assert cv_id not in graph_repo.get_node_ids(conn, SESSION)
    assert len(edges_repo.get_edges_for(conn, [anchor])) == 1


def test_an_accepted_paper_does_get_a_graph_node(conn: Connection) -> None:
    """The control. Without this, "no node" could just mean nothing works."""
    anchor = _anchor(conn, "modern")
    stats = IngestStats()
    ingest_neighbour(conn, SESSION, anchor, _paper("new", 2022), "BACKWARD", cfg, AS_OF, stats)
    new_id = papers_repo.find_by_s2_id(conn, "new")
    assert new_id in graph_repo.get_node_ids(conn, SESSION)
    assert stats.admitted == 1


def test_an_admitted_neighbour_records_its_depth(conn: Connection) -> None:
    anchor = _anchor(conn, "modern")
    ingest_neighbour(conn, SESSION, anchor, _paper("new", 2022), "BACKWARD", cfg, AS_OF, depth=2)
    (node,) = [n for n in graph_repo.get_nodes(conn, SESSION) if n.paper_id != anchor]
    assert node.depth == 2


def test_a_quarantined_paper_is_also_stored_without_a_node(conn: Connection) -> None:
    """
    QUARANTINE is not ACCEPT, so no node -- but the evidence is kept, same as a
    rejection. A paper S2 gave no year for still couples the papers citing it.
    """
    anchor = _anchor(conn, "modern")
    unknown_year = dataclasses.replace(_paper("noyear", 2020), year=None)
    decision = ingest_neighbour(conn, SESSION, anchor, unknown_year, "BACKWARD", cfg, AS_OF)
    assert decision.outcome is Outcome.QUARANTINE
    noyear_id = papers_repo.find_by_s2_id(conn, "noyear")
    assert noyear_id is not None
    assert noyear_id not in graph_repo.get_node_ids(conn, SESSION)
    assert len(edges_repo.get_edges_for(conn, [anchor])) == 1


# --------------------------------------------------------------------------
# Idempotence and direction
# --------------------------------------------------------------------------


def test_ingesting_the_same_neighbour_twice_is_idempotent(conn: Connection) -> None:
    anchor = _anchor(conn, "modern")
    old = _paper("old", 2014)
    ingest_neighbour(conn, SESSION, anchor, old, "BACKWARD", cfg, AS_OF)
    ingest_neighbour(conn, SESSION, anchor, old, "BACKWARD", cfg, AS_OF)
    assert conn.execute(text("SELECT COUNT(*) FROM edges")).scalar() == 1
    assert conn.execute(text("SELECT COUNT(*) FROM papers")).scalar() == 2


def test_a_forward_neighbour_gets_the_edge_the_right_way_round(conn: Connection) -> None:
    """FORWARD means they cite us: the edge is them -> us."""
    anchor = _anchor(conn, "modern")
    citing = _paper("citing", 2024)
    ingest_neighbour(conn, SESSION, anchor, citing, "FORWARD", cfg, AS_OF)
    citing_id = papers_repo.find_by_s2_id(conn, "citing")
    pairs = {(e.citing_id, e.cited_id) for e in edges_repo.get_edges_for(conn, [anchor])}
    assert (citing_id, anchor) in pairs


def test_a_self_citation_is_skipped_rather_than_raising(conn: Connection) -> None:
    """S2 returns them; the CHECK constraint would reject the insert."""
    anchor = _anchor(conn, "modern")
    same = _paper("modern", 2023)
    ingest_neighbour(conn, SESSION, anchor, same, "BACKWARD", cfg, AS_OF)
    assert conn.execute(text("SELECT COUNT(*) FROM edges")).scalar() == 0


def test_boundary_papers_are_never_frontier_candidates(conn: Connection) -> None:
    """
    "Expanded from: no -- never spend API budget on them." A boundary paper has
    no graph_nodes row, so it can never be selected as a frontier anchor.
    """
    anchor = _anchor(conn, "modern")
    ingest_neighbour(conn, SESSION, anchor, _paper("old", 2014), "BACKWARD", cfg, AS_OF)
    anchors = graph_repo.get_nodes(conn, SESSION, states=["SEED", "LIKED"])
    assert [n.paper_id for n in anchors] == [anchor]
