"""
R1.16 -- `POST /api/sessions/{sid}/nodes`.

The only deliberate way into the graph. Search looks; this adds.

BUILD.md fixes the error contract, and each code means something different to
the UI, which is why they are not collapsed into one 400:

    404  S2 has never heard of this id            -- nothing to add
    409  already in this session's graph          -- offer to focus it instead
    422  the cascade rejected it                  -- offer `force`, naming the rule
    503  S2 is unreachable and it is not cached   -- retry later

The 422 body carries `reason_code`. "This paper was rejected" is not actionable;
"PRE_ERA" tells the user it predates the corpus floor and that forcing is a
deliberate choice they can make.

**A forced seed is never silent.** `force=true` admits a rejected paper, and the
rejection is still written to `filter_decisions` and recorded in the event
payload. A corpus that fills with papers nobody remembers admitting is the
failure mode this prevents.

Everything runs on `CachedOnlyS2Client` against the committed fixture cache --
no network transport exists in this file.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import get_s2_client
from app.clients.cache import ResponseCache
from app.clients.s2 import CachedOnlyS2Client
from app.db import make_engine
from app.main import create_app

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
FIXTURE_DB = Path(__file__).resolve().parents[1] / "fixtures" / "s2_cache.db"

SID = 1
# 2018, primary cs.CL -- the cascade accepts it.
ACCEPTED_TITLE = "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
# 2013 -- PRE_ERA. Year-based, so it rejects even with `arxiv_meta` empty,
# which keeps this test independent of the Kaggle bulk load.
REJECTED_TITLE = "Efficient Estimation of Word Representations in Vector Space"

pytestmark = pytest.mark.skipif(
    not FIXTURE_DB.is_file(),
    reason="fixture cache absent; build with scripts/build_fixture_cache.py",
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
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


class _UnknowingClient(CachedOnlyS2Client):
    """
    A client for which S2 answers "no such paper" rather than not answering.

    The offline client cannot express this: any id it has not cached raises
    `CacheMiss`, which is indistinguishable from S2 being down and correctly
    surfaces as a 503. A real client hitting a real unknown id gets a `null`
    entry back and `get_papers` returns `[]`. That is the case this reproduces
    -- overriding the service boundary, not the network, so the offline
    guarantee is untouched.
    """

    async def get_papers(self, s2_ids: list[str], fields: str | None = None) -> list[Any]:
        return []


@pytest.fixture
def unknowing_client(tmp_path: Path) -> Iterator[TestClient]:
    db = tmp_path / "unknowing.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")

    fixture_engine = make_engine(f"sqlite:///{FIXTURE_DB.as_posix()}")
    application = create_app(db_url=f"sqlite:///{db.as_posix()}")
    application.dependency_overrides[get_s2_client] = lambda: _UnknowingClient(
        cache=ResponseCache(fixture_engine)
    )
    with TestClient(application) as test_client:
        yield test_client
    fixture_engine.dispose()


def _s2_id(client: TestClient, title: str) -> str:
    """Resolve a fixture title to its S2 id through the search endpoint."""
    response = client.get(f"/api/sessions/{SID}/search", params={"q": title})
    assert response.status_code == 200, response.text
    hits = response.json()
    assert hits, f"fixture cache holds no search result for {title!r}"
    return str(hits[0]["s2_paper_id"])


def _add(client: TestClient, s2_id: str, **body: object) -> object:
    return client.post(f"/api/sessions/{SID}/nodes", json={"s2_paper_id": s2_id, **body})


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_adding_a_paper_returns_201(client: TestClient) -> None:
    response = _add(client, _s2_id(client, ACCEPTED_TITLE))
    assert response.status_code == 201, response.text  # type: ignore[attr-defined]


def test_the_response_carries_the_node(client: TestClient) -> None:
    body = _add(client, _s2_id(client, ACCEPTED_TITLE)).json()  # type: ignore[attr-defined]
    assert set(body) >= {"paper_id", "session_id", "state", "depth", "title"}


def test_the_node_is_a_seed_at_depth_zero(client: TestClient) -> None:
    body = _add(client, _s2_id(client, ACCEPTED_TITLE)).json()  # type: ignore[attr-defined]
    assert body["state"] == "SEED"
    assert body["depth"] == 0


def test_the_node_lands_in_the_requested_session(client: TestClient) -> None:
    body = _add(client, _s2_id(client, ACCEPTED_TITLE)).json()  # type: ignore[attr-defined]
    assert body["session_id"] == SID


def test_a_graph_node_row_is_written(client: TestClient) -> None:
    from app.main import get_engine

    _add(client, _s2_id(client, ACCEPTED_TITLE))
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM graph_nodes")).scalar() == 1


def test_the_paper_is_stored_with_its_metadata(client: TestClient) -> None:
    """A seed is worth a full metadata call -- it will be expanded from."""
    from app.main import get_engine

    _add(client, _s2_id(client, ACCEPTED_TITLE))
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT title, year, crawl_state FROM papers")).fetchone()
    assert row is not None
    assert row[1] is not None
    assert row[2] == "METADATA"


def test_the_byline_is_persisted(client: TestClient) -> None:
    """
    The author-name fix at R1.15 is what makes this possible; before it,
    `paper_authors` stayed empty no matter what was seeded.
    """
    from app.main import get_engine

    _add(client, _s2_id(client, ACCEPTED_TITLE))
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM paper_authors")).scalar() > 0


def test_a_seed_added_event_is_written(client: TestClient) -> None:
    """
    `graph_nodes.state` is a projection of the event log, and R2.3 rebuilds it
    from events alone. A node written without its event vanishes on rebuild.
    """
    from app.main import get_engine

    _add(client, _s2_id(client, ACCEPTED_TITLE))
    with get_engine().connect() as conn:
        events = conn.execute(text("SELECT event_type FROM interaction_events")).fetchall()
    assert [e[0] for e in events] == ["SEED_ADDED"]


def test_the_new_node_shows_up_in_search_as_already_in_graph(client: TestClient) -> None:
    """The round trip R1.22's dialog depends on."""
    s2_id = _s2_id(client, ACCEPTED_TITLE)
    _add(client, s2_id)
    hits = client.get(f"/api/sessions/{SID}/search", params={"q": ACCEPTED_TITLE}).json()
    assert next(h for h in hits if h["s2_paper_id"] == s2_id)["already_in_graph"] is True


# --------------------------------------------------------------------------
# 409 -- already present
# --------------------------------------------------------------------------


def test_adding_the_same_paper_twice_is_a_409(client: TestClient) -> None:
    """BUILD.md's verification: POST → 201, repeat → 409."""
    s2_id = _s2_id(client, ACCEPTED_TITLE)
    assert _add(client, s2_id).status_code == 201  # type: ignore[attr-defined]
    assert _add(client, s2_id).status_code == 409  # type: ignore[attr-defined]


def test_a_duplicate_add_does_not_create_a_second_node(client: TestClient) -> None:
    from app.main import get_engine

    s2_id = _s2_id(client, ACCEPTED_TITLE)
    _add(client, s2_id)
    _add(client, s2_id)
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM graph_nodes")).scalar() == 1


def test_a_duplicate_add_writes_no_second_event(client: TestClient) -> None:
    """A rejected write must not leave a trace suggesting it happened."""
    from app.main import get_engine

    s2_id = _s2_id(client, ACCEPTED_TITLE)
    _add(client, s2_id)
    _add(client, s2_id)
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM interaction_events")).scalar() == 1


# --------------------------------------------------------------------------
# 404 -- no such paper
# --------------------------------------------------------------------------


def test_an_unknown_id_is_a_404(unknowing_client: TestClient) -> None:
    """
    Distinct from 422: there is nothing to add, so `force` would not help and
    the UI must not offer it. Distinct from 503 too -- "S2 says no such paper"
    is an answer, "S2 could not be asked" is not, and only one is worth a retry.
    """
    assert _add(unknowing_client, "0000000000000000000000000000000000000000").status_code == 404  # type: ignore[attr-defined]


def test_an_unknown_id_writes_nothing(unknowing_client: TestClient) -> None:
    from app.main import get_engine

    _add(unknowing_client, "0000000000000000000000000000000000000000")
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM papers")).scalar() == 0


# --------------------------------------------------------------------------
# 422 -- the cascade rejected it
# --------------------------------------------------------------------------


def test_a_filtered_paper_is_a_422(client: TestClient) -> None:
    assert _add(client, _s2_id(client, REJECTED_TITLE)).status_code == 422  # type: ignore[attr-defined]


def test_the_422_body_names_the_rule(client: TestClient) -> None:
    """
    "Rejected" is not actionable. "PRE_ERA" is: it says which rule fired and
    lets the UI offer `force` against a rule the user can read.
    """
    body = _add(client, _s2_id(client, REJECTED_TITLE)).json()  # type: ignore[attr-defined]
    # Nested under `detail`: FastAPI's standard error envelope, shared with the
    # 404, 409 and 503 this endpoint also returns.
    assert body["detail"]["reason_code"] == "PRE_ERA"
    assert "stage" in body["detail"]


def test_a_rejected_paper_gets_no_node(client: TestClient) -> None:
    from app.main import get_engine

    _add(client, _s2_id(client, REJECTED_TITLE))
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM graph_nodes")).scalar() == 0


def test_a_rejected_paper_still_enters_the_corpus(client: TestClient) -> None:
    """
    The boundary-paper rule. A pre-2015 paper both seeds cite still couples
    them, so the row is kept -- rejection means "not recommendable", never
    "not evidence".
    """
    from app.main import get_engine

    _add(client, _s2_id(client, REJECTED_TITLE))
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM papers")).scalar() == 1


def test_force_admits_a_rejected_paper(client: TestClient) -> None:
    response = _add(client, _s2_id(client, REJECTED_TITLE), force=True)
    assert response.status_code == 201, response.text  # type: ignore[attr-defined]


def test_a_forced_seed_records_the_override(client: TestClient) -> None:
    """
    BUILD.md: "you want to know you overrode it." A silent override is how a
    corpus fills with papers nobody remembers admitting.
    """
    from app.main import get_engine

    _add(client, _s2_id(client, REJECTED_TITLE), force=True)
    with get_engine().connect() as conn:
        payload = conn.execute(
            text("SELECT payload FROM interaction_events WHERE event_type = 'SEED_ADDED'")
        ).scalar()
    assert payload is not None
    assert "forced" in str(payload)
    assert "PRE_ERA" in str(payload)


def test_a_forced_seed_still_records_the_decision(client: TestClient) -> None:
    """The verdict is the record of what was overridden, so it is still written."""
    from app.main import get_engine

    _add(client, _s2_id(client, REJECTED_TITLE), force=True)
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM filter_decisions")).scalar() >= 1


def test_force_is_not_needed_for_an_accepted_paper(client: TestClient) -> None:
    """And an unforced accept must not be marked as an override."""
    from app.main import get_engine

    _add(client, _s2_id(client, ACCEPTED_TITLE))
    with get_engine().connect() as conn:
        payload = conn.execute(text("SELECT payload FROM interaction_events")).scalar()
    assert "forced" not in str(payload)


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------


def test_a_missing_s2_paper_id_is_a_422(client: TestClient) -> None:
    assert client.post(f"/api/sessions/{SID}/nodes", json={}).status_code == 422


def test_an_empty_s2_paper_id_is_a_422(client: TestClient) -> None:
    assert _add(client, "").status_code == 422  # type: ignore[attr-defined]


def test_as_state_defaults_to_seed(client: TestClient) -> None:
    body = _add(client, _s2_id(client, ACCEPTED_TITLE)).json()  # type: ignore[attr-defined]
    assert body["state"] == "SEED"


def test_a_non_seed_as_state_is_rejected(client: TestClient) -> None:
    """
    R1 only adds seeds. CANDIDATE comes from expansion and LIKED/DISLIKED from
    R2's labelling endpoint; accepting them here would create a second,
    unvalidated path into the state machine.
    """
    response = _add(client, _s2_id(client, ACCEPTED_TITLE), as_state="LIKED")
    assert response.status_code == 422  # type: ignore[attr-defined]


def test_an_unknown_body_field_is_rejected(client: TestClient) -> None:
    """A typo'd field silently ignored is a bug you find in production."""
    response = _add(client, _s2_id(client, ACCEPTED_TITLE), forse=True)
    assert response.status_code == 422  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Upstream failure
# --------------------------------------------------------------------------


def test_an_uncached_id_on_an_offline_client_is_a_503(client: TestClient) -> None:
    """
    Not a 404: "S2 could not be asked" and "S2 says no such paper" are
    different answers, and only one of them is worth retrying.
    """
    assert _add(client, "an-id-that-is-not-in-the-fixture-cache").status_code == 503  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Session scoping
# --------------------------------------------------------------------------


def test_a_node_added_to_one_session_is_absent_from_another(client: TestClient) -> None:
    from app.main import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
    _add(client, _s2_id(client, ACCEPTED_TITLE))
    with get_engine().connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM graph_nodes WHERE session_id = 2")).scalar()
    assert count == 0


def test_the_same_paper_can_seed_two_sessions(client: TestClient) -> None:
    """
    Facts about papers are global; graph membership is an opinion and is
    per-session. The 409 must not leak across that boundary.
    """
    from app.main import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
    s2_id = _s2_id(client, ACCEPTED_TITLE)
    assert _add(client, s2_id).status_code == 201  # type: ignore[attr-defined]
    second = client.post("/api/sessions/2/nodes", json={"s2_paper_id": s2_id})
    assert second.status_code == 201, second.text


# --------------------------------------------------------------------------
# The schema the frontend generates from
# --------------------------------------------------------------------------


def test_the_route_is_in_the_openapi_schema(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/sessions/{sid}/nodes" in paths


def test_the_201_response_is_typed(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    created = schema["paths"]["/api/sessions/{sid}/nodes"]["post"]["responses"]["201"]
    assert created["content"]["application/json"]["schema"] != {}
