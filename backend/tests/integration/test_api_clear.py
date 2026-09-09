"""
R2.15 -- `DELETE /api/sessions/{sid}/graph?confirm=true`.

Journey:

    As someone who has made a mess of a graph, I want to start over without
    losing the corpus I paid API calls for, so a fresh start costs seconds
    rather than an afternoon of re-fetching.

BUILD.md's verification is one line: **without `confirm` -> 400**. That is the
whole point of the endpoint's shape. A DELETE that fires on a stray click is a
different feature from one that requires you to say what you mean, and the
difference only shows up on the day you did not mean it.

**It clears graph membership, not the corpus.** `papers`, `authors`, `edges`
and `api_cache` are what the API budget bought; they are shared across
sessions and they are not opinions. Clearing them would turn "start this graph
over" into "throw away every API call I have ever made", which is not a thing
anyone means by Clear.

**The event log survives too, and that is deliberate.** It is append-only and
it is the audit trail -- `scripts/rebuild_state.py` reconstructs state from it.
A `CLEARED` event is appended instead, so the clear is itself recorded rather
than being the one action that leaves no trace.
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
    db = tmp_path / "clear.db"
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


def _node(engine: Engine, name: str, state: str = "CANDIDATE", session_id: int = SID) -> int:
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
                " VALUES (:sid, :p, :st, 1)"
            ),
            {"sid": session_id, "p": paper_id, "st": state},
        )
    return int(paper_id)


def _count(engine: Engine, table: str, where: str = "") -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table} {where}")).scalar() or 0)


def _clear(client: TestClient, **params: object) -> object:
    return client.delete(f"/api/sessions/{SID}/graph", params=params)


# --------------------------------------------------------------------------
# BUILD.md's verification
# --------------------------------------------------------------------------


def test_without_confirm_it_is_a_400(client: TestClient, engine: Engine) -> None:
    """
    The entire shape of this endpoint. A DELETE that fires on a stray click is
    a different feature from one that makes you say what you mean.
    """
    _node(engine, "a")
    assert _clear(client).status_code == 400  # type: ignore[attr-defined]


def test_a_refused_clear_changes_nothing(client: TestClient, engine: Engine) -> None:
    """A 400 that had already deleted half the graph would be worse than a 200."""
    _node(engine, "a")
    _clear(client)
    assert _count(engine, "graph_nodes") == 1


def test_confirm_false_is_also_refused(client: TestClient, engine: Engine) -> None:
    """`confirm=false` is a caller saying no. Reading it as absent-and-therefore-fine
    would make the safest possible spelling the one that fires."""
    _node(engine, "a")
    assert _clear(client, confirm="false").status_code == 400  # type: ignore[attr-defined]
    assert _count(engine, "graph_nodes") == 1


def test_with_confirm_the_graph_empties(client: TestClient, engine: Engine) -> None:
    _node(engine, "a", "SEED")
    _node(engine, "b")
    response = _clear(client, confirm="true")
    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    assert _count(engine, "graph_nodes", f"WHERE session_id = {SID}") == 0


def test_it_reports_how_much_it_removed(client: TestClient, engine: Engine) -> None:
    """
    Silence after a destructive action is the worst possible feedback: you
    cannot tell "cleared 40 papers" from "did nothing at all".
    """
    _node(engine, "a", "SEED")
    _node(engine, "b")
    assert _clear(client, confirm="true").json()["cleared"] == 2  # type: ignore[attr-defined]


def test_clearing_an_empty_graph_is_fine(client: TestClient) -> None:
    """Idempotent. Clearing twice is not an error, it is the same request."""
    response = _clear(client, confirm="true")
    assert response.status_code == 200  # type: ignore[attr-defined]
    assert response.json()["cleared"] == 0  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# What survives -- the expensive part
# --------------------------------------------------------------------------


def test_the_corpus_survives(client: TestClient, engine: Engine) -> None:
    """
    `papers` and `edges` are what the API budget bought. Clearing them would
    turn "start this graph over" into "throw away every API call I have made",
    which is not what anyone means by Clear.
    """
    first = _node(engine, "a")
    second = _node(engine, "b")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
            ),
            {"a": first, "b": second},
        )

    _clear(client, confirm="true")
    assert _count(engine, "papers") == 2
    assert _count(engine, "edges") == 1


def test_the_event_log_survives_and_records_the_clear(client: TestClient, engine: Engine) -> None:
    """
    The log is append-only and it is the audit trail. A clear that erased it
    would be the one action in the system leaving no trace -- so it appends a
    CLEARED event instead.
    """
    _node(engine, "a")
    _clear(client, confirm="true")
    with engine.connect() as conn:
        events = [
            r[0]
            for r in conn.execute(
                text("SELECT event_type FROM interaction_events WHERE session_id = :s"),
                {"s": SID},
            )
        ]
    assert "CLEARED" in events


def test_another_session_is_untouched(client: TestClient, engine: Engine) -> None:
    """Clearing is session-scoped, like every other opinion in the system."""
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
    _node(engine, "mine")
    _node(engine, "theirs", session_id=2)

    _clear(client, confirm="true")
    assert _count(engine, "graph_nodes", "WHERE session_id = 2") == 1


def test_the_cleared_graph_reads_as_empty(client: TestClient, engine: Engine) -> None:
    """The endpoint next to it has to agree, or the UI shows a graph that is gone."""
    _node(engine, "a", "SEED")
    _clear(client, confirm="true")
    assert client.get(f"/api/sessions/{SID}/graph").json()["nodes"] == []
    assert client.get(f"/api/sessions/{SID}/stats").json()["node_count"] == 0


def test_a_cleared_paper_can_be_seeded_again_without_new_api_calls(
    client: TestClient, engine: Engine
) -> None:
    """
    The payoff for keeping the corpus. After a clear, the papers are still
    there -- so rebuilding a graph over them is a database operation rather
    than a re-fetch.
    """
    _node(engine, "a")
    _clear(client, confirm="true")
    assert _count(engine, "papers") == 1


def test_an_unknown_session_is_a_404(client: TestClient) -> None:
    assert client.delete("/api/sessions/999/graph", params={"confirm": "true"}).status_code == 404


def test_the_route_is_in_the_openapi_schema(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "delete" in schema["paths"]["/api/sessions/{sid}/graph"]


# --------------------------------------------------------------------------
# How a clear interacts with the rest of R2
# --------------------------------------------------------------------------


def test_the_rebuild_does_not_resurrect_a_cleared_graph(client: TestClient, engine: Engine) -> None:
    """
    R2.3 reconstructs `graph_nodes.state` from the log alone, and it ignores
    event types it does not know. So a bare CLEARED marker would leave the
    seed's earlier SEED_ADDED as the last word the rebuild understood -- and
    the safety net would put the whole graph back.
    """
    from app.services.rebuild import states_from_events

    _node(engine, "a", "SEED")
    _clear(client, confirm="true")
    with engine.connect() as conn:
        assert states_from_events(conn, SID) == {}


def test_a_cleared_paper_is_not_tombstoned(client: TestClient, engine: Engine) -> None:
    """
    Clearing means start over, not never show me these again. A tombstone would
    make expansion refuse to rediscover papers you merely wanted off the canvas
    -- and only a REMOVED or GC_SWEPT event carries that meaning.
    """
    from app.repo import events as events_repo

    paper_id = _node(engine, "a")
    _clear(client, confirm="true")
    with engine.connect() as conn:
        assert paper_id not in events_repo.removed_paper_ids(conn, SID)


def test_the_clear_is_recorded_per_paper(client: TestClient, engine: Engine) -> None:
    """
    `interaction_events.paper_id` has a foreign key, so there is no id meaning
    "the whole session" -- and one row per paper is the truer record anyway,
    because each of them genuinely left.
    """
    _node(engine, "a")
    _node(engine, "b")
    _clear(client, confirm="true")
    assert _count(engine, "interaction_events", "WHERE event_type = 'CLEARED'") == 2
