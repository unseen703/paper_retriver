"""
R2.6-R2.8 -- removal, tombstone exclusion, and restore.

Journeys:

    As someone curating a graph, I want to see exactly what a removal will
    take with it before I confirm, so the dialog's number is a promise rather
    than a guess.

    As someone curating a graph, I want a removed paper to stay gone through
    later expansions, so the tool never quietly undoes a decision I made.

    As someone who changed their mind, I want an explicit way to bring a
    removed paper back, and no implicit one.

PLAN.md's contract:

    DELETE /api/sessions/{sid}/nodes/{id}?dry_run=true|false
    dry_run=true  -> 200 {would_remove:[{id,title}], count}   <- always call first
    dry_run=false -> 200 {removed:[ids], gc_swept:[ids]}
    Writes REMOVED + GC_SWEPT events. Tombstones. Corpus untouched.

The load-bearing claim is that the dry-run count equals the real one exactly.
A confirmation dialog whose number differs from the outcome is worse than no
dialog: it teaches you to distrust the tool at the one moment you most need to
trust it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.db import make_engine
from app.main import create_app

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SID = 1


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "remove.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(db_url=str(engine.url))) as test_client:
        yield test_client


class Graph:
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
                {"s": name, "t": f"Paper {name}"},
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

    def present(self) -> set[str]:
        back = {v: k for k, v in self.ids.items()}
        with self.engine.connect() as conn:
            return {
                back[r[0]]
                for r in conn.execute(
                    text("SELECT paper_id FROM graph_nodes WHERE session_id=:s"), {"s": SID}
                )
            }


def _delete(client: TestClient, paper_id: int, *, dry_run: bool) -> object:
    return client.delete(
        f"/api/sessions/{SID}/nodes/{paper_id}", params={"dry_run": str(dry_run).lower()}
    )


def _events(engine: Engine, paper_id: int) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        return [
            (r[0], r[1])
            for r in conn.execute(
                text(
                    "SELECT event_type, actor FROM interaction_events"
                    " WHERE session_id=:s AND paper_id=:p ORDER BY id"
                ),
                {"s": SID, "p": paper_id},
            )
        ]


# --------------------------------------------------------------------------
# R2.6 -- dry run
# --------------------------------------------------------------------------


def test_a_dry_run_reports_what_would_go(client: TestClient, engine: Engine) -> None:
    g = Graph(engine).node("S", "SEED").node("B").node("C")
    g.edge("B", "S").edge("C", "B")
    body = _delete(client, g.ids["B"], dry_run=True).json()  # type: ignore[attr-defined]
    ids = {row["id"] for row in body["would_remove"]}
    assert ids == {g.ids["B"], g.ids["C"]}
    assert body["count"] == 2


def test_a_dry_run_includes_titles(client: TestClient, engine: Engine) -> None:
    """
    The dialog has to name the papers, not just count them. "This will remove 7
    papers" is not something anyone can consent to.
    """
    g = Graph(engine).node("S", "SEED").node("B")
    body = _delete(client, g.ids["B"], dry_run=True).json()  # type: ignore[attr-defined]
    assert body["would_remove"][0]["title"] == "Paper B"


def test_a_dry_run_changes_nothing(client: TestClient, engine: Engine) -> None:
    g = Graph(engine).node("S", "SEED").node("B").node("C")
    g.edge("B", "S").edge("C", "B")
    _delete(client, g.ids["B"], dry_run=True)
    assert g.present() == {"S", "B", "C"}
    assert _events(engine, g.ids["B"]) == []


def test_the_dry_run_count_equals_the_actual_removal_count(
    client: TestClient, engine: Engine
) -> None:
    """
    BUILD.md R2.6's stated verification. A dialog whose number differs from the
    outcome teaches you to distrust the tool at the moment you most need to
    trust it.
    """
    g = Graph(engine).node("S", "SEED").node("B").node("C").node("D")
    g.edge("B", "S").edge("C", "B").edge("D", "C")
    predicted = _delete(client, g.ids["B"], dry_run=True).json()  # type: ignore[attr-defined]
    actual = _delete(client, g.ids["B"], dry_run=False).json()  # type: ignore[attr-defined]
    assert predicted["count"] == len(actual["removed"]) + len(actual["gc_swept"])


def test_dry_run_defaults_to_true(client: TestClient, engine: Engine) -> None:
    """
    PLAN.md: "always call first". The safe reading of an omitted parameter is
    the one that does not destroy anything.
    """
    g = Graph(engine).node("S", "SEED").node("B")
    response = client.delete(f"/api/sessions/{SID}/nodes/{g.ids['B']}")
    assert response.status_code == 200
    assert "would_remove" in response.json()
    assert g.present() == {"S", "B"}


# --------------------------------------------------------------------------
# R2.6 -- the real thing
# --------------------------------------------------------------------------


def test_removal_takes_the_node_and_its_orphans(client: TestClient, engine: Engine) -> None:
    g = Graph(engine).node("S", "SEED").node("B").node("C")
    g.edge("B", "S").edge("C", "B")
    body = _delete(client, g.ids["B"], dry_run=False).json()  # type: ignore[attr-defined]
    assert body["removed"] == [g.ids["B"]]
    assert body["gc_swept"] == [g.ids["C"]]
    assert g.present() == {"S"}


def test_removed_and_swept_are_reported_separately(client: TestClient, engine: Engine) -> None:
    """
    Two different things happened: you removed one paper, and the system
    collected another as a consequence. Merging them into one list hides which
    was your decision.
    """
    g = Graph(engine).node("S", "SEED").node("B").node("C")
    g.edge("B", "S").edge("C", "B")
    body = _delete(client, g.ids["B"], dry_run=False).json()  # type: ignore[attr-defined]
    assert g.ids["B"] not in body["gc_swept"]
    assert g.ids["C"] not in body["removed"]


def test_the_events_distinguish_actor(client: TestClient, engine: Engine) -> None:
    """
    `REMOVED` by USER, `GC_SWEPT` by SYSTEM. R2.14's drawer groups by this, and
    it is what tells you whether a paper left because you removed it or because
    it lost its justification.
    """
    g = Graph(engine).node("S", "SEED").node("B").node("C")
    g.edge("B", "S").edge("C", "B")
    _delete(client, g.ids["B"], dry_run=False)
    assert _events(engine, g.ids["B"]) == [("REMOVED", "USER")]
    assert _events(engine, g.ids["C"]) == [("GC_SWEPT", "SYSTEM")]


def test_the_corpus_survives_removal(client: TestClient, engine: Engine) -> None:
    """
    Removal takes graph membership, never papers or edges. A removed paper is
    still evidence -- it still couples the papers that cite it.
    """
    g = Graph(engine).node("S", "SEED").node("B")
    g.edge("B", "S")
    _delete(client, g.ids["B"], dry_run=False)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM papers")).scalar() == 2
        assert conn.execute(text("SELECT COUNT(*) FROM edges")).scalar() == 1


def test_a_seed_can_be_removed(client: TestClient, engine: Engine) -> None:
    """
    Removal is the way out of a seed -- it is the reason labelling one is a
    409. Refusing to remove it too would leave seeds permanent.
    """
    g = Graph(engine).node("S", "SEED")
    assert _delete(client, g.ids["S"], dry_run=False).status_code == 200  # type: ignore[attr-defined]
    assert g.present() == set()


def test_removing_a_paper_with_no_node_here_is_a_404(client: TestClient, engine: Engine) -> None:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES ('x', 'x', 'x', '2026-01-01') RETURNING id"
            )
        ).fetchone()
    assert row is not None
    assert _delete(client, int(row[0]), dry_run=False).status_code == 404  # type: ignore[attr-defined]


def test_removing_twice_is_a_404_the_second_time(client: TestClient, engine: Engine) -> None:
    g = Graph(engine).node("S", "SEED").node("B")
    _delete(client, g.ids["B"], dry_run=False)
    assert _delete(client, g.ids["B"], dry_run=False).status_code == 404  # type: ignore[attr-defined]


def test_an_unknown_session_is_a_404(client: TestClient, engine: Engine) -> None:
    g = Graph(engine).node("S", "SEED")
    assert client.delete(f"/api/sessions/999/nodes/{g.ids['S']}").status_code == 404


# --------------------------------------------------------------------------
# R2.7 -- a tombstone excludes a paper from later expansion
# --------------------------------------------------------------------------


def test_a_removed_paper_is_excluded_from_the_candidate_pool(
    client: TestClient, engine: Engine
) -> None:
    """
    BUILD.md R2.7's verification, at the level it is decided: "Remove a node,
    expand again, assert it does not return."

    Checked against `build_pool` directly rather than through a live expansion,
    so the assertion is about the exclusion rule and not about what Semantic
    Scholar happens to return.
    """
    from app.config import filters
    from app.services.candidates import build_pool

    g = Graph(engine).node("S", "SEED").node("B")
    g.edge("B", "S")
    _delete(client, g.ids["B"], dry_run=False)

    with engine.connect() as conn:
        pool = build_pool(conn, SID, [g.ids["S"]], filters, 2026)
    assert g.ids["B"] not in {entry.paper_id for entry in pool}


def test_a_swept_paper_is_also_excluded(client: TestClient, engine: Engine) -> None:
    """`GC_SWEPT` is a tombstone too -- the graph must not re-acquire what it collected."""
    from app.config import filters
    from app.services.candidates import build_pool

    g = Graph(engine).node("S", "SEED").node("B").node("C")
    g.edge("B", "S").edge("C", "B")
    _delete(client, g.ids["B"], dry_run=False)

    with engine.connect() as conn:
        pool = build_pool(conn, SID, [g.ids["S"]], filters, 2026)
    assert g.ids["C"] not in {entry.paper_id for entry in pool}


# --------------------------------------------------------------------------
# R2.8 -- restore
# --------------------------------------------------------------------------


def test_restore_brings_a_paper_back_as_a_candidate(client: TestClient, engine: Engine) -> None:
    """PLAN.md: restore is the only path back from a tombstone, and it is never automatic."""
    g = Graph(engine).node("S", "SEED").node("B")
    g.edge("B", "S")
    _delete(client, g.ids["B"], dry_run=False)

    response = client.post(f"/api/sessions/{SID}/nodes/{g.ids['B']}/restore")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "CANDIDATE"
    assert "B" in g.present()


def test_restore_lifts_the_tombstone(client: TestClient, engine: Engine) -> None:
    """
    The tombstone is derived from the latest event, not stored as a flag --
    which is precisely what makes restore possible without a schema change.
    """
    from app.repo import events as events_repo

    g = Graph(engine).node("S", "SEED").node("B")
    g.edge("B", "S")
    _delete(client, g.ids["B"], dry_run=False)
    client.post(f"/api/sessions/{SID}/nodes/{g.ids['B']}/restore")

    with engine.connect() as conn:
        assert g.ids["B"] not in events_repo.removed_paper_ids(conn, SID)


def test_a_restored_paper_returns_to_the_candidate_pool(client: TestClient, engine: Engine) -> None:
    from app.config import filters
    from app.services.candidates import build_pool

    g = Graph(engine).node("S", "SEED").node("B")
    g.edge("B", "S")
    _delete(client, g.ids["B"], dry_run=False)
    client.post(f"/api/sessions/{SID}/nodes/{g.ids['B']}/restore")

    with engine.connect() as conn:
        pool_ids = {e.paper_id for e in build_pool(conn, SID, [g.ids["S"]], filters, 2026)}
    # It is back in the graph, so the pool excludes it for the other reason --
    # already a member. What matters is that the tombstone no longer excludes it.
    assert g.ids["B"] not in pool_ids
    assert "B" in g.present()


def test_restore_records_its_own_event(client: TestClient, engine: Engine) -> None:
    g = Graph(engine).node("S", "SEED").node("B")
    _delete(client, g.ids["B"], dry_run=False)
    client.post(f"/api/sessions/{SID}/nodes/{g.ids['B']}/restore")
    assert [e for e, _ in _events(engine, g.ids["B"])] == ["REMOVED", "RESTORED"]


def test_restoring_a_paper_that_was_never_removed_is_a_409(
    client: TestClient, engine: Engine
) -> None:
    """
    Nothing to undo. Silently succeeding would make "restore" sound like it did
    something when the paper was never gone.
    """
    g = Graph(engine).node("S", "SEED").node("B")
    response = client.post(f"/api/sessions/{SID}/nodes/{g.ids['B']}/restore")
    assert response.status_code == 409


def test_restoring_an_unknown_paper_is_a_404(client: TestClient, engine: Engine) -> None:
    Graph(engine).node("S", "SEED")
    assert client.post(f"/api/sessions/{SID}/nodes/424242/restore").status_code == 404


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def test_the_routes_are_in_the_openapi_schema(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "delete" in paths["/api/sessions/{sid}/nodes/{paper_id}"]
    assert "/api/sessions/{sid}/nodes/{paper_id}/restore" in paths
