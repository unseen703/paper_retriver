"""
Review findings from the R1.15-R1.17 pass, one test per finding.

Kept in its own file, like `test_review_r1_9_to_11.py`, because these are the
cases the feature tests did not think to ask about. Every one of them passes a
plausible request and gets a wrong answer rather than an error, which is the
class of bug this project keeps producing.

  1. POST to an unknown session raised a raw FOREIGN KEY IntegrityError -> 500.
  2. Search annotated its flags against `settings.session_id` while the other
     two endpoints used the path `sid`, so the flags described a different
     session than the one being written to.
  3. GET /graph on an unknown session returned 200 and an empty graph, which
     reads as an empty workspace rather than a bad id.
  4. `?states=` (present but empty) returned an empty graph instead of being
     treated as no filter.
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

CACHED_TITLE = "Attention Is All You Need"
OTHER_SID = 2
MISSING_SID = 999

pytestmark = pytest.mark.skipif(
    not FIXTURE_DB.is_file(),
    reason="fixture cache absent; build with scripts/build_fixture_cache.py",
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[TestClient, Engine]]:
    db = tmp_path / "review.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")

    engine = make_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sessions (id, name, created_at)"
                " VALUES (:sid, 'second workspace', '2026-01-01')"
            ),
            {"sid": OTHER_SID},
        )

    fixture_engine = make_engine(f"sqlite:///{FIXTURE_DB.as_posix()}")
    application = create_app(db_url=f"sqlite:///{db.as_posix()}")
    application.dependency_overrides[get_s2_client] = lambda: CachedOnlyS2Client(
        cache=ResponseCache(fixture_engine)
    )
    with TestClient(application) as client:
        yield client, engine
    fixture_engine.dispose()
    engine.dispose()


def _s2_id(client: TestClient, sid: int = 1) -> str:
    response = client.get(f"/api/sessions/{sid}/search", params={"q": CACHED_TITLE})
    assert response.status_code == 200, response.text
    return str(response.json()[0]["s2_paper_id"])


# --------------------------------------------------------------------------
# 1. An unknown session must not 500
# --------------------------------------------------------------------------


def test_posting_to_an_unknown_session_is_a_404(env: tuple[TestClient, Engine]) -> None:
    """
    Was a raw sqlite3 FOREIGN KEY IntegrityError. graph_nodes.session_id has an
    FK and db.py sets PRAGMA foreign_keys=ON, so a bad sid reached the database
    and came back as a 500 with a stack trace.
    """
    client, _ = env
    response = client.post(
        f"/api/sessions/{MISSING_SID}/nodes", json={"s2_paper_id": _s2_id(client)}
    )
    assert response.status_code == 404, response.text


def test_the_unknown_session_404_names_the_session(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    body = client.post(
        f"/api/sessions/{MISSING_SID}/nodes", json={"s2_paper_id": _s2_id(client)}
    ).json()
    assert str(MISSING_SID) in str(body["detail"])


def test_an_unknown_session_costs_no_api_call(env: tuple[TestClient, Engine]) -> None:
    """
    The session check has to run before the metadata fetch. Validating after it
    means a typo'd session id spends one of 3600 daily requests to learn the id
    was wrong.
    """
    client, _ = env
    s2_id = _s2_id(client)
    before = client.app.dependency_overrides[get_s2_client]().cache_hits  # type: ignore[attr-defined]
    client.post(f"/api/sessions/{MISSING_SID}/nodes", json={"s2_paper_id": s2_id})
    after = client.app.dependency_overrides[get_s2_client]().cache_hits  # type: ignore[attr-defined]
    assert after == before


def test_getting_an_unknown_sessions_graph_is_a_404(env: tuple[TestClient, Engine]) -> None:
    """
    Was 200 with an empty graph. A stale session id after a database reset then
    renders as "no papers yet" instead of an error, and every write to it 500s.
    """
    client, _ = env
    assert client.get(f"/api/sessions/{MISSING_SID}/graph").status_code == 404


def test_a_real_but_empty_session_is_still_a_200(env: tuple[TestClient, Engine]) -> None:
    """The distinction that makes the 404 worth having."""
    client, _ = env
    response = client.get(f"/api/sessions/{OTHER_SID}/graph")
    assert response.status_code == 200
    assert response.json()["nodes"] == []


# --------------------------------------------------------------------------
# 2. Search flags must describe the session being written to
# --------------------------------------------------------------------------


def test_search_flags_follow_the_session_in_the_path(env: tuple[TestClient, Engine]) -> None:
    """
    The bug: `_annotate` used `settings.session_id` while POST /nodes used the
    path `sid`. A paper seeded in session 2 reported already_in_graph=false,
    so the UI offered to add it and the add came back 409.
    """
    client, _ = env
    s2_id = _s2_id(client, OTHER_SID)
    assert (
        client.post(f"/api/sessions/{OTHER_SID}/nodes", json={"s2_paper_id": s2_id}).status_code
        == 201
    )

    hits = client.get(f"/api/sessions/{OTHER_SID}/search", params={"q": CACHED_TITLE}).json()
    assert next(h for h in hits if h["s2_paper_id"] == s2_id)["already_in_graph"] is True


def test_a_paper_seeded_elsewhere_is_not_flagged_here(env: tuple[TestClient, Engine]) -> None:
    """The same scoping, in the direction that used to accidentally work."""
    client, _ = env
    s2_id = _s2_id(client, OTHER_SID)
    client.post(f"/api/sessions/{OTHER_SID}/nodes", json={"s2_paper_id": s2_id})

    hits = client.get("/api/sessions/1/search", params={"q": CACHED_TITLE}).json()
    assert next(h for h in hits if h["s2_paper_id"] == s2_id)["already_in_graph"] is False


def test_removal_flags_are_session_scoped_too(env: tuple[TestClient, Engine]) -> None:
    client, engine = env
    s2_id = _s2_id(client, OTHER_SID)
    body = client.post(f"/api/sessions/{OTHER_SID}/nodes", json={"s2_paper_id": s2_id}).json()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO interaction_events"
                " (session_id, paper_id, event_type, actor, created_at)"
                " VALUES (:sid, :pid, 'REMOVED', 'USER', '2026-02-01')"
            ),
            {"sid": OTHER_SID, "pid": body["paper_id"]},
        )

    here = client.get(f"/api/sessions/{OTHER_SID}/search", params={"q": CACHED_TITLE}).json()
    there = client.get("/api/sessions/1/search", params={"q": CACHED_TITLE}).json()
    assert next(h for h in here if h["s2_paper_id"] == s2_id)["previously_removed"] is True
    assert next(h for h in there if h["s2_paper_id"] == s2_id)["previously_removed"] is False


def test_search_on_an_unknown_session_is_a_404(env: tuple[TestClient, Engine]) -> None:
    """Consistent with the other two endpoints, and it costs no API call."""
    client, _ = env
    assert (
        client.get(f"/api/sessions/{MISSING_SID}/search", params={"q": CACHED_TITLE}).status_code
        == 404
    )


# --------------------------------------------------------------------------
# 4. An empty states filter means "no filter", not "no states"
# --------------------------------------------------------------------------


def _seed_two_states(client: TestClient, engine: Engine) -> None:
    s2_id = _s2_id(client)
    client.post("/api/sessions/1/nodes", json={"s2_paper_id": s2_id})
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES ('extra', 'Extra Paper', 'extra paper', '2026-01-01') RETURNING id"
            )
        ).fetchone()
        assert row is not None
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (1, :pid, 'CANDIDATE', 1)"
            ),
            {"pid": row[0]},
        )


def test_an_empty_states_value_returns_the_whole_graph(env: tuple[TestClient, Engine]) -> None:
    """
    A frontend that builds `states=${selected.join(',')}` sends `states=` when
    nothing is selected. Returning an empty graph blanks the canvas with a 200
    and no error -- the same "reads as data loss" failure that makes an unknown
    state a 422.
    """
    client, engine = env
    _seed_two_states(client, engine)
    body = client.get("/api/sessions/1/graph", params={"states": ""}).json()
    assert len(body["nodes"]) == 2


def test_a_whitespace_only_states_value_returns_the_whole_graph(
    env: tuple[TestClient, Engine],
) -> None:
    client, engine = env
    _seed_two_states(client, engine)
    body = client.get("/api/sessions/1/graph", params={"states": " , "}).json()
    assert len(body["nodes"]) == 2


def test_a_real_states_filter_still_narrows(env: tuple[TestClient, Engine]) -> None:
    """The fix must not turn every filter into a no-op."""
    client, engine = env
    _seed_two_states(client, engine)
    body = client.get("/api/sessions/1/graph", params={"states": "SEED"}).json()
    assert {n["state"] for n in body["nodes"]} == {"SEED"}


# --------------------------------------------------------------------------
# 6/7. Cost of annotation, and repo chunking
# --------------------------------------------------------------------------


def test_annotation_does_not_scale_with_graph_size(env: tuple[TestClient, Engine]) -> None:
    """
    `_annotate` used to pull every node id and every tombstone in the session
    to answer a question about ten papers. It now queries only the ids it is
    annotating, so a large graph does not make search slower.
    """
    client, engine = env
    with engine.begin() as conn:
        for i in range(200):
            row = conn.execute(
                text(
                    "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                    " VALUES (:s, :t, :t, '2026-01-01') RETURNING id"
                ),
                {"s": f"bulk{i}", "t": f"bulk paper {i}"},
            ).fetchone()
            assert row is not None
            conn.execute(
                text(
                    "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                    " VALUES (1, :pid, 'CANDIDATE', 1)"
                ),
                {"pid": row[0]},
            )

    response = client.get("/api/sessions/1/search", params={"q": CACHED_TITLE})
    assert response.status_code == 200
    assert all(h["already_in_graph"] is False for h in response.json())


def test_the_id_lookup_survives_more_ids_than_sqlite_allows_variables(
    env: tuple[TestClient, Engine],
) -> None:
    """
    `find_ids_by_s2_ids` expanded one bind parameter per id with no chunking,
    while the two neighbouring functions in `repo/edges.py` chunk at 400 for
    exactly this reason. Unreachable from /api/search today, which caps at 100
    -- but the convention next door says otherwise, so a future caller would
    hit "too many SQL variables" rather than degrade.
    """
    from app.repo import papers as papers_repo

    _, engine = env
    with engine.connect() as conn:
        found = papers_repo.find_ids_by_s2_ids(conn, [f"missing-{i}" for i in range(5_000)])
    assert found == {}
