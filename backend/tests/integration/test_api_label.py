"""
R2.2 -- `PATCH /api/sessions/{sid}/nodes/{paper_id}`.

Journey:

    As someone curating a graph, I want one endpoint that changes a paper's
    label, so that every label change is validated the same way and audited the
    same way.

PLAN.md is explicit that this is **one endpoint, not /like + /dislike +
/unlike**: "One state machine, one validation path, one audit write." Three
endpoints means three places to forget the event, and the event log is the
source of truth that `scripts/rebuild_state.py` reconstructs from.

The two writes -- the `interaction_events` row and the materialised
`graph_nodes.state` -- must land in one transaction. If they can diverge, the
rebuild script silently disagrees with the live table and there is no way to
tell which is right.
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
    db = tmp_path / "label.db"
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


def _node(engine: Engine, s2_id: str, state: str, *, depth: int = 1) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at, year)"
                " VALUES (:s, :t, :t, '2026-01-01', 2020) RETURNING id"
            ),
            {"s": s2_id, "t": f"paper {s2_id}"},
        ).fetchone()
        assert row is not None
        paper_id = int(row[0])
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth, score)"
                " VALUES (:sid, :pid, :st, :d, 2.0)"
            ),
            {"sid": SID, "pid": paper_id, "st": state, "d": depth},
        )
    return paper_id


def _patch(client: TestClient, paper_id: int, state: str) -> object:
    return client.patch(f"/api/sessions/{SID}/nodes/{paper_id}", json={"state": state})


def _state(engine: Engine, paper_id: int) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT state FROM graph_nodes WHERE session_id=:s AND paper_id=:p"),
                {"s": SID, "p": paper_id},
            ).scalar()
        )


def _events(engine: Engine, paper_id: int) -> list[str]:
    with engine.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT event_type FROM interaction_events"
                    " WHERE session_id=:s AND paper_id=:p ORDER BY id"
                ),
                {"s": SID, "p": paper_id},
            )
        ]


# --------------------------------------------------------------------------
# The happy paths
# --------------------------------------------------------------------------


def test_liking_a_candidate_returns_200(client: TestClient, engine: Engine) -> None:
    paper_id = _node(engine, "a", "CANDIDATE")
    assert _patch(client, paper_id, "LIKED").status_code == 200  # type: ignore[attr-defined]


def test_liking_a_candidate_changes_the_materialised_state(
    client: TestClient, engine: Engine
) -> None:
    paper_id = _node(engine, "a", "CANDIDATE")
    _patch(client, paper_id, "LIKED")
    assert _state(engine, paper_id) == "LIKED"


def test_liking_a_candidate_appends_the_event(client: TestClient, engine: Engine) -> None:
    """
    The event log is the source of truth; `graph_nodes.state` is a projection
    of it. A state change without its event vanishes on the next rebuild.
    """
    paper_id = _node(engine, "a", "CANDIDATE")
    _patch(client, paper_id, "LIKED")
    assert _events(engine, paper_id) == ["LIKED"]


def test_unliking_records_an_unlabeled_event(client: TestClient, engine: Engine) -> None:
    paper_id = _node(engine, "a", "LIKED")
    _patch(client, paper_id, "CANDIDATE")
    assert _events(engine, paper_id) == ["UNLABELED"]
    assert _state(engine, paper_id) == "CANDIDATE"


def test_disliking_then_liking_keeps_both_events(client: TestClient, engine: Engine) -> None:
    """Append-only: the log is history, not current state."""
    paper_id = _node(engine, "a", "CANDIDATE")
    _patch(client, paper_id, "DISLIKED")
    _patch(client, paper_id, "LIKED")
    assert _events(engine, paper_id) == ["DISLIKED", "LIKED"]


def test_the_response_carries_the_node_and_a_rescored_count(
    client: TestClient, engine: Engine
) -> None:
    """BUILD.md: 200 returns `{node, rescored_count}`."""
    paper_id = _node(engine, "a", "CANDIDATE")
    body = _patch(client, paper_id, "LIKED").json()  # type: ignore[attr-defined]
    assert body["node"]["state"] == "LIKED"
    assert body["node"]["paper_id"] == paper_id
    assert isinstance(body["rescored_count"], int)


def test_relabelling_to_the_same_state_is_a_no_op(client: TestClient, engine: Engine) -> None:
    """
    A double-click must not write two events into an append-only log, and must
    not be an error either -- the user got what they asked for.
    """
    paper_id = _node(engine, "a", "LIKED")
    assert _patch(client, paper_id, "LIKED").status_code == 200  # type: ignore[attr-defined]
    assert _events(engine, paper_id) == []


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["LIKED", "DISLIKED", "CANDIDATE"])
def test_labelling_a_seed_is_a_409(client: TestClient, engine: Engine, target: str) -> None:
    """
    PLAN.md names 409 specifically. A seed is a paper you chose on purpose;
    liking it says nothing and disliking it contradicts adding it. Removal is
    the way out.
    """
    paper_id = _node(engine, "seed", "SEED", depth=0)
    response = _patch(client, paper_id, target)
    assert response.status_code == 409  # type: ignore[attr-defined]


def test_the_409_names_the_rule(client: TestClient, engine: Engine) -> None:
    paper_id = _node(engine, "seed", "SEED", depth=0)
    body = _patch(client, paper_id, "LIKED").json()  # type: ignore[attr-defined]
    assert body["detail"]["error_code"] == "SEED_NOT_LABELABLE"


def test_a_refused_transition_writes_nothing(client: TestClient, engine: Engine) -> None:
    """A refusal that left a trace would corrupt the rebuild."""
    paper_id = _node(engine, "seed", "SEED", depth=0)
    _patch(client, paper_id, "LIKED")
    assert _events(engine, paper_id) == []
    assert _state(engine, paper_id) == "SEED"


def test_promoting_to_seed_is_a_409(client: TestClient, engine: Engine) -> None:
    """
    Seeding runs a metadata fetch and the filter cascade. Relabelling would
    produce a seed that went through neither, with a depth that lies about how
    it got there.
    """
    paper_id = _node(engine, "a", "CANDIDATE")
    response = _patch(client, paper_id, "SEED")
    assert response.status_code == 409  # type: ignore[attr-defined]


def test_an_unknown_state_is_a_422(client: TestClient, engine: Engine) -> None:
    paper_id = _node(engine, "a", "CANDIDATE")
    assert _patch(client, paper_id, "LKIED").status_code == 422  # type: ignore[attr-defined]


def test_a_paper_with_no_node_here_is_a_404(client: TestClient, engine: Engine) -> None:
    """A boundary paper is in the corpus and not in the graph; it has no state."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES ('b', 'boundary', 'boundary', '2026-01-01') RETURNING id"
            )
        ).fetchone()
    assert row is not None
    assert _patch(client, int(row[0]), "LIKED").status_code == 404  # type: ignore[attr-defined]


def test_an_unknown_session_is_a_404(client: TestClient, engine: Engine) -> None:
    paper_id = _node(engine, "a", "CANDIDATE")
    response = client.patch(f"/api/sessions/999/nodes/{paper_id}", json={"state": "LIKED"})
    assert response.status_code == 404


def test_an_unknown_body_field_is_rejected(client: TestClient, engine: Engine) -> None:
    paper_id = _node(engine, "a", "CANDIDATE")
    response = client.patch(
        f"/api/sessions/{SID}/nodes/{paper_id}", json={"state": "LIKED", "sate": "x"}
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Session scoping and the schema
# --------------------------------------------------------------------------


def test_labelling_does_not_leak_across_sessions(client: TestClient, engine: Engine) -> None:
    paper_id = _node(engine, "a", "CANDIDATE")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (2, :pid, 'CANDIDATE', 1)"
            ),
            {"pid": paper_id},
        )
    _patch(client, paper_id, "LIKED")
    with engine.connect() as conn:
        other = conn.execute(
            text("SELECT state FROM graph_nodes WHERE session_id=2 AND paper_id=:p"),
            {"p": paper_id},
        ).scalar()
    assert other == "CANDIDATE"


def test_the_route_is_in_the_openapi_schema(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "patch" in paths["/api/sessions/{sid}/nodes/{paper_id}"]


# --------------------------------------------------------------------------
# The sweep that un-liking triggers
# --------------------------------------------------------------------------


def test_unliking_never_sweeps_the_paper_you_un_liked(client: TestClient, engine: Engine) -> None:
    """
    Un-liking the graph's only anchor leaves that paper a candidate reachable
    from nothing -- including itself. Sweeping it would turn "unlike" into
    "delete", which is a different verb with a different audit trail, and the
    user asked to stop favouring a paper rather than to lose it.
    """
    paper_id = _node(engine, "lonely", "LIKED")
    assert _patch(client, paper_id, "CANDIDATE").status_code == 200  # type: ignore[attr-defined]
    assert _state(engine, paper_id) == "CANDIDATE"
    assert _events(engine, paper_id) == ["UNLABELED"]


def test_unliking_does_sweep_the_orphans_it_creates(client: TestClient, engine: Engine) -> None:
    """
    The other half. A candidate that hung off the liked node -- and off nothing
    else -- loses its justification the moment that node stops being an anchor.
    """
    liked = _node(engine, "L", "LIKED")
    orphan = _node(engine, "C", "CANDIDATE")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
            ),
            {"a": orphan, "b": liked},
        )

    _patch(client, liked, "CANDIDATE")

    with engine.connect() as conn:
        remaining = {
            r[0]
            for r in conn.execute(
                text("SELECT paper_id FROM graph_nodes WHERE session_id = :s"), {"s": SID}
            )
        }
    assert liked in remaining, "the un-liked paper itself is protected"
    assert orphan not in remaining, "its dependent lost its only justification"


def test_a_swept_orphan_is_attributed_to_the_system(client: TestClient, engine: Engine) -> None:
    """The user un-liked one paper; they did not remove the other."""
    liked = _node(engine, "L", "LIKED")
    orphan = _node(engine, "C", "CANDIDATE")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
            ),
            {"a": orphan, "b": liked},
        )
    _patch(client, liked, "CANDIDATE")

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type, actor FROM interaction_events"
                " WHERE session_id=:s AND paper_id=:p"
            ),
            {"s": SID, "p": orphan},
        ).fetchall()
    assert rows == [("GC_SWEPT", "SYSTEM")]
