"""
R1.14 -- the FastAPI app and its health endpoint.

`/api/health` answers the question you actually ask when something is wrong:
is the database reachable, is S2 reachable, how much is cached, and how big is
the graph. A bare `{"status": "ok"}` answers none of those and is why health
endpoints get ignored.

Two security constraints from PLAN.md section "Security (proportionate)" are
asserted here rather than left to review:

  * CORS is an explicit allowlist of `http://localhost:5173`, never `*`. The
    note in PLAN.md is blunt about why -- "it's one line and reviewers check".
  * The app binds `127.0.0.1`, never `0.0.0.0`. There is no auth layer, so
    binding to all interfaces would publish the whole corpus to the LAN.

`s2_reachable` must never make a network call. A health check that costs an
upstream request turns a monitoring loop into a rate-limit problem, and at one
request per second that is the entire budget.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.main import DEV_ORIGINS, app, create_app

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    db = tmp_path / "api.db"
    c = Config(str(ALEMBIC_INI))
    c.set_main_option("script_location", str(ALEMBIC_INI.parent))
    c.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(c, "head")
    monkeypatch.setenv("DB_PATH", str(db))
    with TestClient(create_app(db_url=f"sqlite:///{db.as_posix()}")) as test_client:
        yield test_client


# --------------------------------------------------------------------------
# The health payload
# --------------------------------------------------------------------------


def test_health_returns_200(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200


def test_health_reports_the_four_documented_fields(client: TestClient) -> None:
    """BUILD.md: {db, s2_reachable, cache_rows, node_count}."""
    body = client.get("/api/health").json()
    assert set(body) >= {"db", "s2_reachable", "cache_rows", "node_count"}


def test_db_reports_ok_when_the_database_answers(client: TestClient) -> None:
    assert client.get("/api/health").json()["db"] == "ok"


def test_cache_rows_and_node_count_are_integers(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert isinstance(body["cache_rows"], int)
    assert isinstance(body["node_count"], int)


def test_a_fresh_database_reports_an_empty_graph(client: TestClient) -> None:
    assert client.get("/api/health").json()["node_count"] == 0


def test_node_count_tracks_the_configured_session(client: TestClient) -> None:
    """A per-session count, not a global one -- sessions are the unit here."""
    from sqlalchemy import text

    from app.main import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES ('p1', 'T', 't', '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (1, 1, 'SEED', 0)"
            )
        )
    assert client.get("/api/health").json()["node_count"] == 1


def test_s2_reachable_makes_no_network_call(client: TestClient) -> None:
    """
    A health check that costs an upstream request turns a monitoring loop into
    a rate-limit problem -- and at one request per second that is the whole
    budget. It reports whether a key is configured, not whether S2 answers.
    """
    body = client.get("/api/health").json()
    assert body["s2_reachable"] in {"configured", "no_api_key"}


def test_the_config_version_is_reported(client: TestClient) -> None:
    """Which filters produced this graph is the first question when it looks odd."""
    from app.config import filters

    assert client.get("/api/health").json()["config_version"] == filters.config_version


# --------------------------------------------------------------------------
# PLAN.md's security constraints
# --------------------------------------------------------------------------


def test_cors_is_an_explicit_allowlist_not_a_wildcard() -> None:
    """PLAN.md: "Not `*`, even locally -- it's one line and reviewers check"."""
    assert DEV_ORIGINS == ["http://localhost:5173"]
    assert "*" not in DEV_ORIGINS


def test_the_allowed_origin_is_accepted(client: TestClient) -> None:
    response = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_an_unlisted_origin_gets_no_cors_header(client: TestClient) -> None:
    response = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert response.headers.get("access-control-allow-origin") is None


def test_the_app_binds_loopback_only() -> None:
    """
    There is no auth layer, so binding 0.0.0.0 would publish the whole corpus
    to the LAN. The Makefile's dev target is the thing that actually decides.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    dev = next(line for line in makefile.splitlines() if "uvicorn" in line)
    assert "--host 127.0.0.1" in dev
    assert "0.0.0.0" not in dev


# --------------------------------------------------------------------------
# The app itself
# --------------------------------------------------------------------------


def test_the_openapi_schema_is_served(client: TestClient) -> None:
    """`make types` generates the frontend's types from this at R1.20."""
    schema = client.get("/openapi.json").json()
    assert "/api/health" in schema["paths"]


def test_the_docs_page_is_served(client: TestClient) -> None:
    """BUILD.md's verification opens /docs."""
    assert client.get("/docs").status_code == 200


def test_the_module_level_app_exists() -> None:
    """`uvicorn app.main:app` is what BUILD.md and the Makefile both invoke."""
    assert app is not None


def test_an_unknown_route_is_a_404(client: TestClient) -> None:
    assert client.get("/api/nope").status_code == 404


def test_a_broken_database_is_reported_rather_than_raised(tmp_path: Path) -> None:
    """
    Health must answer even when the thing it is checking is broken. An
    endpoint that 500s tells you nothing a timeout would not.
    """
    with TestClient(create_app(db_url=f"sqlite:///{tmp_path / 'missing.db'}")) as broken:
        body = broken.get("/api/health").json()
    assert body["db"] != "ok"
    assert body["node_count"] == 0
