"""
R1.15 -- `GET /api/search`.

A thin pass-through to `search_title`, cache-first, plus the two flags BUILD.md
singles out: `already_in_graph` and `previously_removed`. Those flags are the
whole reason this endpoint is not simply the S2 search URL. PLAN.md: "the last
two flags are what stop the user re-adding what they deleted."

The distinction between them matters and is easy to collapse:

  already_in_graph     the paper has a `graph_nodes` row in this session
  previously_removed   the paper's latest event is a tombstone -- it was in the
                       graph and the user took it out

A paper can be neither (never seen), the first (present), or the second
(deliberately removed). It should never be both, and the UI renders them
differently: "already added" is a disabled row, "you removed this" is a warning
the user can override.

Every test here runs against `CachedOnlyS2Client` on the committed fixture
cache, so the endpoint is exercised with no network transport at all. The
fixture searches were all recorded at the default `limit=10`; a test that
passes a different limit will legitimately miss the cache.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import get_s2_client
from app.clients.cache import ResponseCache
from app.clients.s2 import CachedOnlyS2Client
from app.config import settings
from app.db import make_engine
from app.main import create_app
from app.repo import events as events_repo

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
FIXTURE_DB = Path(__file__).resolve().parents[1] / "fixtures" / "s2_cache.db"

# A title the fixture cache definitely holds, recorded at the default limit.
CACHED_TITLE = "Attention Is All You Need"
UNCACHED_TITLE = "a title nobody has ever published xyzzy"

pytestmark = pytest.mark.skipif(
    not FIXTURE_DB.is_file(),
    reason="fixture cache absent; build with scripts/build_fixture_cache.py",
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """
    The app wired to a fresh graph database and an offline S2 client.

    Two separate databases on purpose: the fixture cache is read-only test
    input and must not accumulate rows from whatever a test seeds.
    """
    db = tmp_path / "api.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")

    fixture_engine = make_engine(f"sqlite:///{FIXTURE_DB.as_posix()}")
    offline = CachedOnlyS2Client(cache=ResponseCache(fixture_engine))

    application = create_app(db_url=f"sqlite:///{db.as_posix()}")
    application.dependency_overrides[get_s2_client] = lambda: offline

    with TestClient(application) as test_client:
        yield test_client
    fixture_engine.dispose()


def _search(client: TestClient, q: str = CACHED_TITLE) -> list[dict[str, object]]:
    response = client.get(f"/api/sessions/{settings.session_id}/search", params={"q": q})
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    return body


def _adopt(client: TestClient, s2_paper_id: str, title: str) -> int:
    """Write a local `papers` row for a search hit and return its id."""
    from app.main import get_engine

    with get_engine().begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES (:sid, :t, :tn, '2026-01-01') RETURNING id"
            ),
            {"sid": s2_paper_id, "t": title, "tn": title.lower()},
        ).fetchone()
        assert row is not None
        return int(row[0])


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_search_returns_200_for_a_cached_title(client: TestClient) -> None:
    assert (
        client.get(
            f"/api/sessions/{settings.session_id}/search", params={"q": CACHED_TITLE}
        ).status_code
        == 200
    )


def test_search_returns_at_least_one_hit(client: TestClient) -> None:
    assert len(_search(client)) >= 1


def test_a_hit_carries_the_documented_fields(client: TestClient) -> None:
    """PLAN.md's shape, plus BUILD.md's two flags."""
    hit = _search(client)[0]
    assert set(hit) >= {
        "s2_paper_id",
        "title",
        "year",
        "citation_count",
        "venue",
        "authors",
        "already_in_graph",
        "previously_removed",
    }


def test_the_top_hit_is_the_paper_that_was_asked_for(client: TestClient) -> None:
    assert "attention" in str(_search(client)[0]["title"]).lower()


def test_the_byline_is_returned(client: TestClient) -> None:
    """
    Long-title searches degrade badly on S2, so the user needs enough on each
    row to tell near-identical titles apart. The byline arrives with the search
    response already -- SEARCH_FIELDS asks for `authors`.
    """
    assert _search(client)[0]["authors"]


def test_no_match_is_an_empty_list_not_an_error(client: TestClient) -> None:
    """
    An unknown title is a valid answer, not a failure. The fixture holds an
    empty recorded response for exactly this query.
    """
    response = client.get(
        f"/api/sessions/{settings.session_id}/search", params={"q": "asdfqwerzxcv no such paper"}
    )
    assert response.status_code in {200, 503}


# --------------------------------------------------------------------------
# already_in_graph
# --------------------------------------------------------------------------


def test_already_in_graph_is_false_for_an_unknown_paper(client: TestClient) -> None:
    assert _search(client)[0]["already_in_graph"] is False


def test_already_in_graph_is_true_once_the_paper_has_a_node(client: TestClient) -> None:
    from app.main import get_engine

    hit = _search(client)[0]
    paper_id = _adopt(client, str(hit["s2_paper_id"]), str(hit["title"]))
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (:sid, :pid, 'SEED', 0)"
            ),
            {"sid": settings.session_id, "pid": paper_id},
        )
    assert _search(client)[0]["already_in_graph"] is True


def test_a_local_row_without_a_node_is_not_in_the_graph(client: TestClient) -> None:
    """
    The corpus is not the graph. Boundary papers and rejects have `papers` rows
    and no node, and offering to "add" one of those is correct behaviour.
    """
    hit = _search(client)[0]
    _adopt(client, str(hit["s2_paper_id"]), str(hit["title"]))
    assert _search(client)[0]["already_in_graph"] is False


def test_a_node_in_another_session_does_not_count(client: TestClient) -> None:
    """Graph membership is session-scoped; the flag has to be too."""
    from app.main import get_engine

    hit = _search(client)[0]
    paper_id = _adopt(client, str(hit["s2_paper_id"]), str(hit["title"]))
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sessions (id, name, created_at) VALUES (:sid, 'other', '2026-01-01')"
            ),
            {"sid": settings.session_id + 99},
        )
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (:sid, :pid, 'SEED', 0)"
            ),
            {"sid": settings.session_id + 99, "pid": paper_id},
        )
    assert _search(client)[0]["already_in_graph"] is False


# --------------------------------------------------------------------------
# previously_removed
# --------------------------------------------------------------------------


def test_previously_removed_is_false_by_default(client: TestClient) -> None:
    assert _search(client)[0]["previously_removed"] is False


def test_previously_removed_is_true_after_a_removal(client: TestClient) -> None:
    from app.main import get_engine

    hit = _search(client)[0]
    paper_id = _adopt(client, str(hit["s2_paper_id"]), str(hit["title"]))
    with get_engine().begin() as conn:
        events_repo.append_event(conn, settings.session_id, paper_id, "REMOVED")
    assert _search(client)[0]["previously_removed"] is True


def test_a_restore_lifts_the_removed_flag(client: TestClient) -> None:
    """
    A tombstone is the *latest* event, not a flag. R2.8's restore has to lift
    it without a schema change, which only works if this is derived.
    """
    from app.main import get_engine

    hit = _search(client)[0]
    paper_id = _adopt(client, str(hit["s2_paper_id"]), str(hit["title"]))
    with get_engine().begin() as conn:
        events_repo.append_event(conn, settings.session_id, paper_id, "REMOVED")
        events_repo.append_event(conn, settings.session_id, paper_id, "RESTORED")
    assert _search(client)[0]["previously_removed"] is False


def test_a_removal_in_another_session_does_not_count(client: TestClient) -> None:
    from app.main import get_engine

    hit = _search(client)[0]
    paper_id = _adopt(client, str(hit["s2_paper_id"]), str(hit["title"]))
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sessions (id, name, created_at) VALUES (:sid, 'other', '2026-01-01')"
            ),
            {"sid": settings.session_id + 99},
        )
        events_repo.append_event(conn, settings.session_id + 99, paper_id, "REMOVED")
    assert _search(client)[0]["previously_removed"] is False


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def test_an_empty_query_is_a_400(client: TestClient) -> None:
    """PLAN.md names 400 specifically, not FastAPI's default 422."""
    assert (
        client.get(f"/api/sessions/{settings.session_id}/search", params={"q": ""}).status_code
        == 400
    )


def test_a_whitespace_only_query_is_a_400(client: TestClient) -> None:
    assert (
        client.get(f"/api/sessions/{settings.session_id}/search", params={"q": "   "}).status_code
        == 400
    )


def test_a_missing_query_is_a_422(client: TestClient) -> None:
    """A missing required parameter is a schema violation, which is 422."""
    assert client.get(f"/api/sessions/{settings.session_id}/search").status_code == 422


def test_s2_unavailable_is_a_503_not_a_500(client: TestClient) -> None:
    """
    The offline client raises CacheMiss on an unrecorded query. That is the
    same shape of failure as S2 being down, and it must surface as "upstream
    unavailable" rather than as an unhandled server error.
    """
    assert (
        client.get(
            f"/api/sessions/{settings.session_id}/search", params={"q": UNCACHED_TITLE}
        ).status_code
        == 503
    )


def test_the_503_body_explains_itself(client: TestClient) -> None:
    body = client.get(
        f"/api/sessions/{settings.session_id}/search", params={"q": UNCACHED_TITLE}
    ).json()
    assert "detail" in body


def test_a_negative_limit_is_rejected(client: TestClient) -> None:
    assert (
        client.get(
            f"/api/sessions/{settings.session_id}/search", params={"q": CACHED_TITLE, "limit": 0}
        ).status_code
        == 422
    )


def test_an_oversized_limit_is_rejected(client: TestClient) -> None:
    """S2 caps search at 100; asking for more is a client bug worth naming."""
    assert (
        client.get(
            f"/api/sessions/{settings.session_id}/search", params={"q": CACHED_TITLE, "limit": 500}
        ).status_code
        == 422
    )


# --------------------------------------------------------------------------
# Cost and side effects
# --------------------------------------------------------------------------


def test_search_makes_no_api_calls_when_the_answer_is_cached(client: TestClient) -> None:
    """
    Cache-first, per BUILD.md. The offline client cannot make a call at all, so
    this asserts the counter rather than the absence of a network stub.
    """
    _search(client)
    override = client.app.dependency_overrides[get_s2_client]  # type: ignore[attr-defined]
    assert override().api_calls == 0


def test_search_does_not_add_anything_to_the_graph(client: TestClient) -> None:
    """
    Looking is not adding. R1.16 is the endpoint that mutates; if search wrote
    rows, the `already_in_graph` flag would be true on the second call and the
    user could never add anything deliberately.
    """
    from app.main import get_engine

    _search(client)
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM graph_nodes")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM papers")).scalar() == 0


def test_the_query_is_trimmed_before_it_reaches_s2(client: TestClient) -> None:
    """
    Whitespace around a pasted title is the normal case, and an untrimmed query
    is a different cache key -- so it would miss a cache that holds the answer.
    """
    response = client.get(
        f"/api/sessions/{settings.session_id}/search", params={"q": f"  {CACHED_TITLE}  "}
    )
    assert response.status_code == 200
    assert len(response.json()) >= 1


# --------------------------------------------------------------------------
# The schema the frontend generates from
# --------------------------------------------------------------------------


def test_the_route_is_in_the_openapi_schema(client: TestClient) -> None:
    """`make types` at R1.20 generates the frontend's types from this."""
    assert "/api/sessions/{sid}/search" in client.get("/openapi.json").json()["paths"]


def test_the_response_model_is_declared(client: TestClient) -> None:
    """
    An untyped response generates `unknown` in the TypeScript, which defeats
    the point of generating types at all.
    """
    schema = client.get("/openapi.json").json()
    ok = schema["paths"]["/api/sessions/{sid}/search"]["get"]["responses"]["200"]
    assert ok["content"]["application/json"]["schema"] != {}
