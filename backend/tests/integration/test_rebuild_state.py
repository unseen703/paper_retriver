"""
R2.3 -- `scripts/rebuild_state.py`, the event-log safety net.

Journey:

    As the person who has to trust this data, I want `graph_nodes.state`
    reconstructible from `interaction_events` alone, so that if the two ever
    disagree I can tell which one is wrong.

`graph_nodes.state` is a materialised projection. The log is the source of
truth. That claim is worth nothing unless something can actually perform the
reconstruction, which is what this script is: BUILD.md calls it "your event-log
safety net".

The verification BUILD.md asks for is here as
`test_rebuild_matches_after_twenty_mixed_labels` -- twenty mixed transitions
through the real endpoint, then a rebuild, then assert the result is identical
to what the live table already held.
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
from app.services.rebuild import rebuild_states, states_from_events

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SID = 1


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "rebuild.db"
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


def _seed_node(engine: Engine, s2_id: str, state: str = "CANDIDATE") -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES (:s, :t, :t, '2026-01-01') RETURNING id"
            ),
            {"s": s2_id, "t": f"paper {s2_id}"},
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
        if state == "SEED":
            conn.execute(
                text(
                    "INSERT INTO interaction_events"
                    " (session_id, paper_id, event_type, actor, created_at)"
                    " VALUES (:sid, :pid, 'SEED_ADDED', 'USER', '2026-01-01')"
                ),
                {"sid": SID, "pid": paper_id},
            )
    return paper_id


def _live_states(engine: Engine) -> dict[int, str]:
    with engine.connect() as conn:
        return {
            r[0]: r[1]
            for r in conn.execute(
                text("SELECT paper_id, state FROM graph_nodes WHERE session_id = :s"),
                {"s": SID},
            )
        }


# --------------------------------------------------------------------------
# Deriving state from the log
# --------------------------------------------------------------------------


def test_a_seed_added_event_yields_a_seed(engine: Engine) -> None:
    paper_id = _seed_node(engine, "s", "SEED")
    with engine.connect() as conn:
        assert states_from_events(conn, SID)[paper_id] == "SEED"


def test_the_latest_label_event_wins(engine: Engine, client: TestClient) -> None:
    """The log is history; the projection is its last word."""
    paper_id = _seed_node(engine, "a")
    for target in ("LIKED", "DISLIKED", "LIKED"):
        client.patch(f"/api/sessions/{SID}/nodes/{paper_id}", json={"state": target})
    with engine.connect() as conn:
        assert states_from_events(conn, SID)[paper_id] == "LIKED"


def test_unlabeled_returns_a_paper_to_candidate(engine: Engine, client: TestClient) -> None:
    paper_id = _seed_node(engine, "a")
    client.patch(f"/api/sessions/{SID}/nodes/{paper_id}", json={"state": "LIKED"})
    client.patch(f"/api/sessions/{SID}/nodes/{paper_id}", json={"state": "CANDIDATE"})
    with engine.connect() as conn:
        assert states_from_events(conn, SID)[paper_id] == "CANDIDATE"


def test_a_tombstoned_paper_is_absent_rather_than_stateful(engine: Engine) -> None:
    """
    A removed paper has no graph state at all -- it is out of the graph. Giving
    it one would resurrect it on the next rebuild, which is exactly what
    tombstones exist to prevent.
    """
    paper_id = _seed_node(engine, "a")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO interaction_events"
                " (session_id, paper_id, event_type, actor, created_at)"
                " VALUES (:sid, :pid, 'REMOVED', 'USER', '2026-02-01')"
            ),
            {"sid": SID, "pid": paper_id},
        )
    with engine.connect() as conn:
        assert paper_id not in states_from_events(conn, SID)


def test_a_restore_brings_a_paper_back_as_a_candidate(engine: Engine) -> None:
    """PLAN.md: restore is the only path back from a tombstone, and it lands on CANDIDATE."""
    paper_id = _seed_node(engine, "a")
    with engine.begin() as conn:
        for event in ("REMOVED", "RESTORED"):
            conn.execute(
                text(
                    "INSERT INTO interaction_events"
                    " (session_id, paper_id, event_type, actor, created_at)"
                    " VALUES (:sid, :pid, :e, 'USER', '2026-02-01')"
                ),
                {"sid": SID, "pid": paper_id, "e": event},
            )
    with engine.connect() as conn:
        assert states_from_events(conn, SID)[paper_id] == "CANDIDATE"


def test_events_from_another_session_are_ignored(engine: Engine) -> None:
    paper_id = _seed_node(engine, "a")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'o', '2026-01-01')")
        )
        conn.execute(
            text(
                "INSERT INTO interaction_events"
                " (session_id, paper_id, event_type, actor, created_at)"
                " VALUES (2, :pid, 'LIKED', 'USER', '2026-02-01')"
            ),
            {"pid": paper_id},
        )
    with engine.connect() as conn:
        assert states_from_events(conn, SID).get(paper_id) != "LIKED"


# --------------------------------------------------------------------------
# BUILD.md's stated verification
# --------------------------------------------------------------------------


def test_rebuild_matches_after_twenty_mixed_labels(engine: Engine, client: TestClient) -> None:
    """
    BUILD.md R2.3: "Rebuild after 20 mixed labels; assert identical to
    materialized state."

    This is the test that makes "the log is the source of truth" a fact rather
    than a claim.
    """
    papers = [_seed_node(engine, f"p{i}") for i in range(10)]
    _seed_node(engine, "seed", "SEED")

    plan = ["LIKED", "DISLIKED", "CANDIDATE", "LIKED", "DISLIKED"]
    applied = 0
    for index, paper_id in enumerate(papers):
        for step in range(2):
            target = plan[(index + step) % len(plan)]
            response = client.patch(f"/api/sessions/{SID}/nodes/{paper_id}", json={"state": target})
            assert response.status_code == 200, response.text
            applied += 1
    assert applied == 20

    before = _live_states(engine)
    with engine.connect() as conn:
        derived = states_from_events(conn, SID)
    assert derived == before


def test_rebuild_repairs_a_corrupted_projection(engine: Engine, client: TestClient) -> None:
    """
    The safety net doing its job. Corrupt the materialised table directly, then
    rebuild: the log wins, which is the whole point of calling it the source of
    truth.
    """
    paper_id = _seed_node(engine, "a")
    client.patch(f"/api/sessions/{SID}/nodes/{paper_id}", json={"state": "LIKED"})
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE graph_nodes SET state='DISLIKED' WHERE paper_id=:p"), {"p": paper_id}
        )

    changed = rebuild_states(engine, SID)
    assert changed == 1
    assert _live_states(engine)[paper_id] == "LIKED"


def test_rebuild_is_idempotent(engine: Engine, client: TestClient) -> None:
    """A second run must change nothing, or the two are not really in agreement."""
    paper_id = _seed_node(engine, "a")
    client.patch(f"/api/sessions/{SID}/nodes/{paper_id}", json={"state": "LIKED"})
    rebuild_states(engine, SID)
    assert rebuild_states(engine, SID) == 0


def test_rebuild_reports_zero_when_already_consistent(engine: Engine, client: TestClient) -> None:
    paper_id = _seed_node(engine, "a")
    client.patch(f"/api/sessions/{SID}/nodes/{paper_id}", json={"state": "DISLIKED"})
    assert rebuild_states(engine, SID) == 0


def test_rebuild_leaves_a_node_with_no_events_alone(engine: Engine) -> None:
    """
    An expansion writes a CANDIDATE node without an interaction event -- the
    user has not done anything to it. Sweeping those away would delete most of
    the graph on the first rebuild.
    """
    paper_id = _seed_node(engine, "a")
    assert rebuild_states(engine, SID) == 0
    assert _live_states(engine)[paper_id] == "CANDIDATE"
