"""
R2.12 -- persisting the layout.

Journey:

    As someone who has arranged a graph, I want it to look the same when I come
    back to it, so the spatial memory I built up is not thrown away by a page
    reload.

PLAN.md M6 calls layout instability the thing that "kills usability", and
BUILD.md calls R2.12 "the single biggest UX win in R2". The `pos_x`/`pos_y`
columns have been in the schema since migration 0001 and `GET /graph` has
returned them since R1.17 -- but nothing has ever written one, so every reload
started from scratch.

Design decisions these tests pin:

**PUT, not POST.** Saving the same arrangement twice is the same arrangement.
The client saves after every layout and after dragging stops, so this endpoint
is called often and must be safely repeatable.

**Bulk, not per node.** A 200-node graph settling would otherwise be 200
requests, each with its own transaction, for one logical event.

**A paper that is no longer in the graph is skipped, not an error.** The client
computes positions from what it has on screen, and a node can be removed
between the layout settling and the save landing. That is a race, not a
mistake: the rest of the arrangement is still correct and still worth keeping.
The response reports how many rows were written so the skip is visible rather
than silent.

**A non-finite coordinate is a 422.** NaN reaches the database happily and
comes back as a position Cytoscape cannot draw, so the node vanishes with no
error anywhere -- exactly the kind of silent corruption that is unfindable
later.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, text

from app.db import make_engine
from app.main import create_app
from app.schemas.graph import NodePosition

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SID = 1


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "positions.db"
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


def _node(engine: Engine, name: str, state: str = "CANDIDATE") -> int:
    with engine.begin() as conn:
        paper_id = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES (:s, :t, :t, '2026-01-01') RETURNING id"
            ),
            {"s": name, "t": f"Paper {name}"},
        ).scalar()
        assert paper_id is not None
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (:sid, :pid, :st, 1)"
            ),
            {"sid": SID, "pid": paper_id, "st": state},
        )
    return int(paper_id)


def _save(client: TestClient, positions: list[dict[str, object]]) -> object:
    return client.put(f"/api/sessions/{SID}/positions", json={"positions": positions})


def _stored(engine: Engine, paper_id: int) -> tuple[float | None, float | None]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT pos_x, pos_y FROM graph_nodes WHERE session_id=:s AND paper_id=:p"),
            {"s": SID, "p": paper_id},
        ).fetchone()
    assert row is not None
    return (row[0], row[1])


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


def test_a_position_is_written(client: TestClient, engine: Engine) -> None:
    paper_id = _node(engine, "a")
    response = _save(client, [{"paper_id": paper_id, "x": 12.5, "y": -40.25}])
    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    assert _stored(engine, paper_id) == (12.5, -40.25)


def test_many_positions_go_in_one_request(client: TestClient, engine: Engine) -> None:
    """
    A 200-node graph settling is one logical event. Sending it as 200 requests,
    each with its own transaction, would be the same arrangement at 200 times
    the cost.
    """
    ids = [_node(engine, f"p{i}") for i in range(25)]
    body = [{"paper_id": pid, "x": float(i), "y": float(-i)} for i, pid in enumerate(ids)]
    response = _save(client, body)
    assert response.json()["saved"] == 25  # type: ignore[attr-defined]
    assert _stored(engine, ids[7]) == (7.0, -7.0)


def test_saving_twice_is_the_same_as_saving_once(client: TestClient, engine: Engine) -> None:
    """PUT. The client saves after every layout, so repeating must be harmless."""
    paper_id = _node(engine, "a")
    _save(client, [{"paper_id": paper_id, "x": 1.0, "y": 2.0}])
    _save(client, [{"paper_id": paper_id, "x": 1.0, "y": 2.0}])
    assert _stored(engine, paper_id) == (1.0, 2.0)


def test_a_later_save_replaces_an_earlier_one(client: TestClient, engine: Engine) -> None:
    paper_id = _node(engine, "a")
    _save(client, [{"paper_id": paper_id, "x": 1.0, "y": 2.0}])
    _save(client, [{"paper_id": paper_id, "x": 99.0, "y": -99.0}])
    assert _stored(engine, paper_id) == (99.0, -99.0)


def test_an_empty_save_is_accepted_and_does_nothing(client: TestClient) -> None:
    """
    The client may settle a layout on an empty graph. A 422 there would make
    the console noisy about a non-event.
    """
    response = _save(client, [])
    assert response.status_code == 200  # type: ignore[attr-defined]
    assert response.json()["saved"] == 0  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Reading it back
# --------------------------------------------------------------------------


def test_the_graph_endpoint_returns_a_saved_position(client: TestClient, engine: Engine) -> None:
    """
    The round trip is the whole feature. `GET /graph` has carried `pos` since
    R1.17 and it was always null, because nothing wrote one.
    """
    paper_id = _node(engine, "a")
    _save(client, [{"paper_id": paper_id, "x": 8.0, "y": 9.0}])
    nodes = client.get(f"/api/sessions/{SID}/graph").json()["nodes"]
    saved = next(n for n in nodes if n["id"] == paper_id)
    assert saved["pos"] == {"x": 8.0, "y": 9.0}


def test_a_node_never_saved_has_no_position(client: TestClient, engine: Engine) -> None:
    """
    Null, not (0, 0). The origin is a real place, and a graph of unsaved nodes
    reported as sitting there would be drawn as a single point.
    """
    paper_id = _node(engine, "a")
    nodes = client.get(f"/api/sessions/{SID}/graph").json()["nodes"]
    assert next(n for n in nodes if n["id"] == paper_id)["pos"] is None


# --------------------------------------------------------------------------
# What is refused, and what is merely skipped
# --------------------------------------------------------------------------


def test_a_paper_with_no_node_here_is_skipped_rather_than_failing(
    client: TestClient, engine: Engine
) -> None:
    """
    A node can be removed between the layout settling and the save landing.
    That is a race, not a mistake -- the rest of the arrangement is still
    correct, and failing the whole request would throw it away.
    """
    present = _node(engine, "a")
    response = _save(
        client,
        [{"paper_id": present, "x": 1.0, "y": 1.0}, {"paper_id": 424242, "x": 2.0, "y": 2.0}],
    )
    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    assert response.json()["saved"] == 1, "the skip is reported rather than silent"  # type: ignore[attr-defined]
    assert _stored(engine, present) == (1.0, 1.0)


def test_a_null_coordinate_is_refused(client: TestClient, engine: Engine) -> None:
    """
    What a browser actually sends when a coordinate goes bad.

    `JSON.stringify({x: NaN})` produces `{"x": null}` -- JavaScript has no way
    to spell NaN in JSON, so it substitutes null. That is the shape a broken
    layout arrives in, and accepting it would store a null over a good
    position: the node loses its place and the next reload scatters it.
    """
    paper_id = _node(engine, "a")
    assert _save(client, [{"paper_id": paper_id, "x": None, "y": 0.0}]).status_code == 422  # type: ignore[attr-defined]


def test_a_non_finite_coordinate_is_refused_at_the_model(engine: Engine) -> None:
    """
    Tested against the model rather than through HTTP, because NaN cannot be
    sent: `json.dumps` refuses it outright, so no compliant client can produce
    the request. The guard still earns its place -- Python's own decoder
    *accepts* a literal `NaN` token, so a non-browser caller can deliver one,
    and it would reach a float column without complaint and come back as a
    position Cytoscape cannot draw. The node would then vanish with nothing
    logged anywhere.
    """
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            NodePosition(paper_id=1, x=bad, y=0.0)
        with pytest.raises(ValidationError):
            NodePosition(paper_id=1, x=0.0, y=bad)


def test_an_ordinary_coordinate_still_passes_the_finite_check(engine: Engine) -> None:
    """The guard against over-rejecting: negatives and zero are real places."""
    assert NodePosition(paper_id=1, x=-1234.5, y=0.0).x == -1234.5


def test_an_unknown_body_field_is_refused(client: TestClient, engine: Engine) -> None:
    paper_id = _node(engine, "a")
    response = _save(client, [{"paper_id": paper_id, "x": 1.0, "y": 2.0, "z": 3.0}])
    assert response.status_code == 422  # type: ignore[attr-defined]


def test_an_unknown_session_is_a_404(client: TestClient) -> None:
    assert client.put("/api/sessions/999/positions", json={"positions": []}).status_code == 404


def test_the_route_is_in_the_openapi_schema(client: TestClient) -> None:
    """`make types` generates the frontend's client from this."""
    assert "/api/sessions/{sid}/positions" in client.get("/openapi.json").json()["paths"]


# --------------------------------------------------------------------------
# BUILD.md's verification, at the level the server can answer for
# --------------------------------------------------------------------------


def test_positions_survive_a_new_node_arriving(client: TestClient, engine: Engine) -> None:
    """
    BUILD.md: "Expand 3x and confirm existing nodes do not move."

    The browser half of that is the layout's `fixedNodeConstraint`; the half
    the server owns is that admitting a new node must not disturb the stored
    positions of the ones already there. `add_node` deliberately omits pos_x
    and pos_y from its upsert for exactly this reason, and this pins it.
    """
    first = _node(engine, "a")
    _save(client, [{"paper_id": first, "x": 5.0, "y": 6.0}])

    for round_number in range(3):
        _node(engine, f"new{round_number}")
        assert _stored(engine, first) == (5.0, 6.0), f"moved after round {round_number}"


def test_relabelling_does_not_move_a_node(client: TestClient, engine: Engine) -> None:
    """
    Liking a paper is not a request to rearrange the graph. `add_node`'s upsert
    is the shared path for both, so this is easy to break by adding pos_x to
    its SET clause.
    """
    paper_id = _node(engine, "a")
    _save(client, [{"paper_id": paper_id, "x": 3.0, "y": 4.0}])
    assert (
        client.patch(f"/api/sessions/{SID}/nodes/{paper_id}", json={"state": "LIKED"}).status_code
        == 200
    )
    assert _stored(engine, paper_id) == (3.0, 4.0)
