"""
R2.16 -- `GET` and `POST /api/sessions`.

Journey:

    As someone exploring two different questions, I want a second graph that
    does not disturb the first, and I want it to cost nothing for papers I have
    already fetched.

BUILD.md's verification is the payoff for the whole session contract:

    Create session 2, seed a different paper, confirm session 1 unchanged and
    **zero new API calls** for any already-crawled paper.

That second clause is the one worth the effort. `papers`, `authors` and `edges`
are global; only opinions are session-scoped. If that boundary is right, a
paper session 1 already paid for is free in session 2 -- and if it is subtly
wrong, the only symptom is a bill, which is exactly the kind of bug that hides
for months. It is asserted here against the client's own call counter rather
than inferred from timing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.deps import get_s2_client
from app.clients.cache import ResponseCache
from app.clients.s2 import CachedOnlyS2Client
from app.db import make_engine
from app.main import create_app

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
FIXTURE_DB = Path(__file__).resolve().parents[1] / "fixtures" / "s2_cache.db"
SEED_TITLE = "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "sessions.db"
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


# --------------------------------------------------------------------------
# Listing and creating
# --------------------------------------------------------------------------


def test_the_default_session_is_listed(client: TestClient) -> None:
    """Migration 0001 seeds session 1. A switcher with nothing in it is broken."""
    sessions = client.get("/api/sessions").json()
    assert [s["id"] for s in sessions] == [1]


def test_a_session_can_be_created(client: TestClient) -> None:
    response = client.post("/api/sessions", json={"name": "vision"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] != 1
    assert body["name"] == "vision"


def test_a_created_session_appears_in_the_list(client: TestClient) -> None:
    created = client.post("/api/sessions", json={"name": "vision"}).json()
    ids = [s["id"] for s in client.get("/api/sessions").json()]
    assert created["id"] in ids


def test_sessions_are_listed_oldest_first(client: TestClient) -> None:
    """
    A switcher is a list someone reads top-down, and the session you started
    with should not migrate around it as you add more. Stable order, by id
    (CLAUDE.md rule 7).
    """
    client.post("/api/sessions", json={"name": "b"})
    client.post("/api/sessions", json={"name": "c"})
    ids = [s["id"] for s in client.get("/api/sessions").json()]
    assert ids == sorted(ids)


def test_the_list_carries_a_node_count(client: TestClient, engine: Engine) -> None:
    """
    Otherwise every session in the switcher looks identical, and the one with
    your work in it is indistinguishable from the empty one you made by
    accident.
    """
    with engine.begin() as conn:
        paper_id = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES ('p', 'P', 'p', '2026-01-01') RETURNING id"
            )
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (1, :p, 'SEED', 0)"
            ),
            {"p": paper_id},
        )
    listed = {s["id"]: s["node_count"] for s in client.get("/api/sessions").json()}
    assert listed[1] == 1


def test_a_nameless_session_is_refused(client: TestClient) -> None:
    assert client.post("/api/sessions", json={"name": "  "}).status_code == 422
    assert client.post("/api/sessions", json={}).status_code == 422


def test_an_unknown_field_is_refused(client: TestClient) -> None:
    assert client.post("/api/sessions", json={"name": "x", "id": 9}).status_code == 422


def test_an_absurd_name_is_refused(client: TestClient) -> None:
    assert client.post("/api/sessions", json={"name": "x" * 500}).status_code == 422


def test_a_new_session_starts_empty(client: TestClient) -> None:
    created = client.post("/api/sessions", json={"name": "fresh"}).json()
    assert client.get(f"/api/sessions/{created['id']}/graph").json()["nodes"] == []


def test_a_new_session_is_immediately_usable(client: TestClient) -> None:
    """
    Every session-scoped route depends on `existing_session`, so a session that
    is created but not usable would 404 on its own graph.
    """
    created = client.post("/api/sessions", json={"name": "fresh"}).json()
    assert client.get(f"/api/sessions/{created['id']}/stats").status_code == 200
    assert client.get(f"/api/sessions/{created['id']}/review").status_code == 200


def test_the_routes_are_in_the_openapi_schema(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/sessions" in paths
    assert {"get", "post"} <= set(paths["/api/sessions"])


# --------------------------------------------------------------------------
# BUILD.md's verification -- the payoff for the session contract
# --------------------------------------------------------------------------


@pytest.mark.skipif(not FIXTURE_DB.is_file(), reason="fixture cache absent")
def test_seeding_an_already_crawled_paper_in_a_new_session_costs_no_api_calls(
    tmp_path: Path,
) -> None:
    """
    BUILD.md R2.16, and the reason the session_id contract was worth getting
    right: "confirm session 1 unchanged and **zero new API calls** for any
    already-crawled paper."

    `papers`, `authors` and `edges` are global; only opinions are
    session-scoped. If that boundary holds, the second session pays nothing for
    what the first already fetched. If it is subtly wrong the only symptom is a
    bill, which is the kind of bug that hides for months -- so this asserts
    against the client's own counter rather than inferring anything from speed.
    """
    db = tmp_path / "two.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")

    fixture_engine = make_engine(f"sqlite:///{FIXTURE_DB.as_posix()}")
    offline = CachedOnlyS2Client(cache=ResponseCache(fixture_engine))
    application = create_app(db_url=f"sqlite:///{db.as_posix()}")
    application.dependency_overrides[get_s2_client] = lambda: offline

    with TestClient(application) as client:
        hits = client.get("/api/sessions/1/search", params={"q": SEED_TITLE}).json()
        s2_id = hits[0]["s2_paper_id"]
        assert client.post("/api/sessions/1/nodes", json={"s2_paper_id": s2_id}).status_code == 201

        second = client.post("/api/sessions", json={"name": "second"}).json()["id"]
        # Everything this paper needs is now in the corpus. Whatever the second
        # session does, it must not be a fetch.
        before = offline.api_calls
        response = client.post(f"/api/sessions/{second}/nodes", json={"s2_paper_id": s2_id})
        assert response.status_code == 201, response.text
        assert offline.api_calls == before, "an already-crawled paper must cost nothing"

        # ...and session 1 is untouched by any of it.
        first_graph = client.get("/api/sessions/1/graph").json()
        assert len(first_graph["nodes"]) == 1

    fixture_engine.dispose()


def test_an_opinion_in_one_session_does_not_leak_into_another(
    client: TestClient, engine: Engine
) -> None:
    """
    The other half of the contract, and the cheaper half to test: the same
    paper can be a SEED here and absent there, because states live in
    `graph_nodes` keyed by session.
    """
    second = client.post("/api/sessions", json={"name": "second"}).json()["id"]
    with engine.begin() as conn:
        paper_id = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES ('shared', 'Shared', 'shared', '2026-01-01') RETURNING id"
            )
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (1, :p, 'LIKED', 0)"
            ),
            {"p": paper_id},
        )

    assert client.get(f"/api/sessions/{second}/graph").json()["nodes"] == []
    assert client.get("/api/sessions/1/graph").json()["nodes"][0]["state"] == "LIKED"


def test_clearing_one_session_leaves_the_other_alone(client: TestClient, engine: Engine) -> None:
    """R2.15 meets R2.16 -- the destructive action has to respect the boundary too."""
    second = client.post("/api/sessions", json={"name": "second"}).json()["id"]
    with engine.begin() as conn:
        for sid in (1, second):
            paper_id = conn.execute(
                text(
                    "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                    f" VALUES ('p{sid}', 'P', 'p', '2026-01-01') RETURNING id"
                )
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                    " VALUES (:s, :p, 'SEED', 0)"
                ),
                {"s": sid, "p": paper_id},
            )

    client.delete("/api/sessions/1/graph", params={"confirm": "true"})
    assert len(client.get(f"/api/sessions/{second}/graph").json()["nodes"]) == 1
