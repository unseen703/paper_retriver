"""
R2.5 -- mark-and-sweep garbage collection.

Journey:

    As someone curating a graph, I want removing a paper to also remove
    whatever only existed because of it, so the graph stays a set of papers
    justified by my choices rather than a pile of orphans.

PLAN.md calls this "the feature most likely to be misimplemented" and states
the rule once, precisely:

    After any removal, mark every node reachable from anchors = SEED u LIKED
    over the **undirected** projection, bounded by max_depth. Sweep every
    unmarked node whose state is CANDIDATE. Never sweep a LIKED or DISLIKED
    node.

Every worked case from that table is a test below, because each one encodes a
decision that a plausible implementation gets wrong:

  * undirected, not directed -- a candidate that *cites* a seed is reachable
    from it, and a directed walk from the seed would never find it;
  * path-based, not adjacency-based -- a chain of candidates ending at a seed
    is entirely justified, and a one-hop check would sweep all of it;
  * bounded by max_depth -- without the bound a long chain trailing off a seed
    lives forever, which is how a graph silently grows past its ceiling;
  * labelled nodes are never swept -- your judgment outranks topology, so a
    LIKED node at degree zero stays, and so does a disconnected DISLIKED one,
    which still carries negative signal that re-fetching would waste budget on.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from app.db import make_engine
from app.services.gc import gc_sweep

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SID = 1
MAX_DEPTH = 3


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "gc.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


class Graph:
    """A tiny builder, so each test reads as the shape it is describing."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.ids: dict[str, int] = {}

    def node(self, name: str, state: str = "CANDIDATE") -> Graph:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                    " VALUES (:s, :t, :t, '2026-01-01') RETURNING id"
                ),
                {"s": name, "t": name},
            ).fetchone()
            assert row is not None
            paper_id = int(row[0])
            conn.execute(
                text(
                    "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                    " VALUES (:sid, :pid, :st, :d)"
                ),
                {"sid": SID, "pid": paper_id, "st": state, "d": 0 if state == "SEED" else 1},
            )
        self.ids[name] = paper_id
        return self

    def edge(self, citing: str, cited: str) -> Graph:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                    " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
                ),
                {"a": self.ids[citing], "b": self.ids[cited]},
            )
        return self

    def drop(self, name: str) -> Graph:
        """Remove a node the way R2.6 will: the row goes, the paper stays."""
        with self.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM graph_nodes WHERE session_id=:s AND paper_id=:p"),
                {"s": SID, "p": self.ids[name]},
            )
        return self

    def names(self, paper_ids: list[int]) -> list[str]:
        back = {v: k for k, v in self.ids.items()}
        return sorted(back[p] for p in paper_ids)

    def states(self) -> dict[str, str]:
        back = {v: k for k, v in self.ids.items()}
        with self.engine.connect() as conn:
            return {
                back[r[0]]: r[1]
                for r in conn.execute(
                    text("SELECT paper_id, state FROM graph_nodes WHERE session_id=:s"),
                    {"s": SID},
                )
            }


def sweep(engine: Engine, *, dry_run: bool = False) -> list[int]:
    with engine.begin() as conn:
        return gc_sweep(conn, SID, max_depth=MAX_DEPTH, dry_run=dry_run)


# --------------------------------------------------------------------------
# PLAN.md's six worked cases
# --------------------------------------------------------------------------


def test_a_candidate_reachable_only_through_a_removed_node_is_swept(engine: Engine) -> None:
    """Case 1. C hung off B; B is gone; C has nothing justifying it."""
    g = Graph(engine).node("S", "SEED").node("B").node("C")
    g.edge("B", "S").edge("C", "B")
    g.drop("B")
    assert g.names(sweep(engine)) == ["C"]


def test_a_candidate_also_reachable_through_a_liked_node_is_kept(engine: Engine) -> None:
    """
    Case 2. "Some connectivity to some other liked/seed node" is enough. C is
    still justified even though the path it arrived by is gone.
    """
    g = Graph(engine).node("S", "SEED").node("L", "LIKED").node("B").node("C")
    g.edge("B", "S").edge("C", "B").edge("C", "L")
    g.drop("B")
    assert sweep(engine) == []
    assert "C" in g.states()


def test_a_chain_of_candidates_terminating_at_a_seed_is_kept(engine: Engine) -> None:
    """
    Case 3. Reachability is path-based, not adjacency-based. An implementation
    that only checked direct neighbours of an anchor would sweep C1 and C2 --
    both of which are transitively justified.
    """
    g = Graph(engine).node("S", "SEED").node("C1").node("C2")
    g.edge("C1", "S").edge("C2", "C1")
    assert sweep(engine) == []


def test_a_detached_candidate_component_is_swept_in_full(engine: Engine) -> None:
    """Case 4. Nothing in it is reachable from any anchor."""
    g = Graph(engine).node("S", "SEED").node("A").node("X").node("Y").node("Z")
    g.edge("A", "S")
    g.edge("X", "Y").edge("Y", "Z")
    assert g.names(sweep(engine)) == ["X", "Y", "Z"]


def test_a_liked_node_at_degree_zero_survives(engine: Engine) -> None:
    """Case 5. Your judgment outranks topology."""
    g = Graph(engine).node("S", "SEED").node("L", "LIKED")
    assert sweep(engine) == []
    assert g.states()["L"] == "LIKED"


def test_a_disconnected_disliked_node_survives(engine: Engine) -> None:
    """
    Case 6. It still carries negative signal, and re-fetching it later would
    spend API budget to rediscover something already judged.
    """
    g = Graph(engine).node("S", "SEED").node("D", "DISLIKED")
    assert sweep(engine) == []
    assert g.states()["D"] == "DISLIKED"


# --------------------------------------------------------------------------
# The undirected projection
# --------------------------------------------------------------------------


def test_reachability_is_undirected(engine: Engine) -> None:
    """
    A candidate that CITES a seed is reachable from it. A directed walk
    outward from the seed would never find C, and would sweep a paper whose
    entire reason for being there is that it cites the seed.
    """
    g = Graph(engine).node("S", "SEED").node("C")
    g.edge("C", "S")
    assert sweep(engine) == []


def test_a_candidate_cited_by_a_seed_is_also_reachable(engine: Engine) -> None:
    g = Graph(engine).node("S", "SEED").node("C")
    g.edge("S", "C")
    assert sweep(engine) == []


# --------------------------------------------------------------------------
# The depth bound
# --------------------------------------------------------------------------


def test_a_chain_beyond_max_depth_is_swept(engine: Engine) -> None:
    """
    PLAN.md: "without it, a long candidate chain trailing off from a seed stays
    alive forever". At max_depth 3, C4 is four hops out and is not justified.
    """
    g = Graph(engine).node("S", "SEED")
    for i in range(1, 5):
        g.node(f"C{i}")
    g.edge("C1", "S").edge("C2", "C1").edge("C3", "C2").edge("C4", "C3")
    assert g.names(sweep(engine)) == ["C4"]


def test_everything_within_max_depth_survives(engine: Engine) -> None:
    g = Graph(engine).node("S", "SEED")
    for i in range(1, 4):
        g.node(f"C{i}")
    g.edge("C1", "S").edge("C2", "C1").edge("C3", "C2")
    assert sweep(engine) == []


# --------------------------------------------------------------------------
# Mechanics
# --------------------------------------------------------------------------


def test_dry_run_reports_without_removing(engine: Engine) -> None:
    g = Graph(engine).node("S", "SEED").node("X")
    assert g.names(sweep(engine, dry_run=True)) == ["X"]
    assert "X" in g.states(), "dry run must not touch the graph"


def test_the_dry_run_count_equals_the_real_count(engine: Engine) -> None:
    """
    BUILD.md R2.6's verification, at the level it is decided. A dry run whose
    number differs from the real one makes the confirmation dialog a lie.
    """
    g = Graph(engine).node("S", "SEED").node("X").node("Y")
    g.edge("X", "Y")
    predicted = sweep(engine, dry_run=True)
    actual = sweep(engine)
    assert predicted == actual


def test_a_sweep_writes_gc_swept_events_attributed_to_the_system(engine: Engine) -> None:
    """
    `actor=SYSTEM` distinguishes a sweep from a removal the user asked for --
    which is what R2.14's review drawer groups by, and what tells you whether
    a paper left because you removed it or because it lost its justification.
    """
    g = Graph(engine).node("S", "SEED").node("X")
    sweep(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type, actor FROM interaction_events"
                " WHERE session_id=:s AND paper_id=:p"
            ),
            {"s": SID, "p": g.ids["X"]},
        ).fetchall()
    assert rows == [("GC_SWEPT", "SYSTEM")]


def test_a_swept_node_is_tombstoned_and_stays_out(engine: Engine) -> None:
    """A GC_SWEPT event is a tombstone, so expansion must never bring it back."""
    from app.repo import events as events_repo

    g = Graph(engine).node("S", "SEED").node("X")
    sweep(engine)
    with engine.connect() as conn:
        assert g.ids["X"] in events_repo.removed_paper_ids(conn, SID)


def test_a_second_sweep_is_a_no_op(engine: Engine) -> None:
    """Idempotency. A sweep that keeps finding work has not finished."""
    Graph(engine).node("S", "SEED").node("X")
    assert sweep(engine) != []
    assert sweep(engine) == []


def test_the_corpus_is_untouched(engine: Engine) -> None:
    """
    Sweeping removes graph membership, never papers or edges. A swept paper is
    still evidence -- it still couples the papers that cite it.
    """
    g = Graph(engine).node("S", "SEED").node("X")
    g.edge("X", "S")
    sweep(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM papers")).scalar() == 2
        assert conn.execute(text("SELECT COUNT(*) FROM edges")).scalar() == 1


def test_a_graph_with_no_anchors_sweeps_every_candidate(engine: Engine) -> None:
    """
    Nothing justifies anything. Harsh, and correct: removing the last seed
    should not leave a hundred orphans behind pretending to be recommendations.
    """
    g = Graph(engine).node("A").node("B")
    g.edge("A", "B")
    assert g.names(sweep(engine)) == ["A", "B"]


def test_results_are_sorted_for_determinism(engine: Engine) -> None:
    """CLAUDE.md rule 7. Set iteration order is not a contract."""
    g = Graph(engine).node("S", "SEED")
    for name in ("Z", "Y", "X"):
        g.node(name)
    assert sweep(engine, dry_run=True) == sorted(sweep(engine, dry_run=True))


def test_another_session_is_unaffected(engine: Engine) -> None:
    g = Graph(engine).node("S", "SEED").node("X")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'o', '2026-01-01')")
        )
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (2, :pid, 'CANDIDATE', 1)"
            ),
            {"pid": g.ids["X"]},
        )
    sweep(engine)
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT COUNT(*) FROM graph_nodes WHERE session_id=2")).scalar() == 1
        )
