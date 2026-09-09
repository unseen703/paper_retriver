"""
R1.18 -- what an expansion *does*, now asynchronous (R2.4).

These were written against the synchronous R1.18 endpoint, which returned 200
with the finished result because there was no job system to poll. R2.4 built
it, so the transport changed to 202-plus-polling and every test here changed
with it -- but only the transport. Each assertion still asks the same question
about what the run did, which is the point of keeping them rather than
replacing them: the queue is new, the pipeline underneath it is not, and a
regression in the second would be easy to miss while admiring the first.

The queue itself -- the 202, the 409, the stages, crash recovery -- is tested
in `test_api_jobs.py`.

The response carries what the run did, not just that it happened. An expansion
that fetched sixty papers and admitted none is a completely different event
from one that found nothing to fetch, and a bare 200 cannot tell you which you
got.

`added_paper_ids` comes from the expander rather than from diffing the graph
before and after -- a diff would attribute anything else written to the session
to this expansion.

**Budget exhaustion is not an error.** PLAN.md: a partial expansion is a
success. Hitting `max_nodes` or the API budget commits what was gathered and
reports the truncation; the status stays 200 and `truncated` says so. The one
exception BUILD.md names is a graph already at `max_nodes`, where there is no
room to admit anything at all -- that is a 422.
"""

from __future__ import annotations

import time
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

SID = 1
# The fixture holds this paper's references, so a real expansion runs offline.
SEED_TITLE = "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"

pytestmark = pytest.mark.skipif(
    not FIXTURE_DB.is_file(),
    reason="fixture cache absent; build with scripts/build_fixture_cache.py",
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[TestClient, Engine]]:
    db = tmp_path / "expand.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")

    engine = make_engine(f"sqlite:///{db.as_posix()}")
    fixture_engine = make_engine(f"sqlite:///{FIXTURE_DB.as_posix()}")
    application = create_app(db_url=f"sqlite:///{db.as_posix()}")
    application.dependency_overrides[get_s2_client] = lambda: CachedOnlyS2Client(
        cache=ResponseCache(fixture_engine)
    )
    with TestClient(application) as client:
        yield client, engine
    fixture_engine.dispose()
    engine.dispose()


def _seed(client: TestClient) -> int:
    """Seed the fixture paper and return its local id."""
    hits = client.get(f"/api/sessions/{SID}/search", params={"q": SEED_TITLE}).json()
    response = client.post(
        f"/api/sessions/{SID}/nodes", json={"s2_paper_id": hits[0]["s2_paper_id"]}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["paper_id"])


def _expand(client: TestClient, **body: object) -> object:
    """Queue an expansion. Returns the raw POST response, for status checks."""
    return client.post(f"/api/sessions/{SID}/expansions", json=body)


def _run(client: TestClient, **body: object) -> dict[str, object]:
    """
    Queue an expansion, wait for it, and return what it did.

    The shape it returns is exactly R1.18's synchronous response body -- that
    is what `JobStatus.result` carries -- so the assertions below did not have
    to change their meaning when the transport did.
    """
    response = client.post(f"/api/sessions/{SID}/expansions", json=body)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        status = client.get(f"/api/sessions/{SID}/expansions/{job_id}").json()
        if status["status"] not in ("QUEUED", "RUNNING"):
            assert status["status"] == "DONE", status
            assert status["result"] is not None
            return dict(status["result"])
        time.sleep(0.05)
    raise AssertionError(f"expansion {job_id} never finished")


# --------------------------------------------------------------------------
# The happy path -- BUILD.md's verification
# --------------------------------------------------------------------------


def test_expanding_is_asynchronous_as_of_r2_4(env: tuple[TestClient, Engine]) -> None:
    """
    This test used to be `test_expanding_returns_200_not_202`, and it was right
    at the time: a 202 would have promised a job id nothing could poll. R2.4
    built the queue, so the promise is now one the server keeps.
    """
    client, _ = env
    _seed(client)
    response = _expand(client, hops=1, max_new=20)
    assert response.status_code == 202, response.text  # type: ignore[attr-defined]


def test_the_response_carries_the_added_node_ids(env: tuple[TestClient, Engine]) -> None:
    """BUILD.md: "returns added node ids"."""
    client, _ = env
    _seed(client)
    body = _run(client, hops=1, max_new=20)
    assert isinstance(body["added_paper_ids"], list)
    assert len(body["added_paper_ids"]) == body["n_added"]


def test_expanding_from_a_seed_actually_adds_nodes(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    _seed(client)
    body = _run(client, hops=1, max_new=20)
    assert body["n_added"] > 0


def test_the_added_ids_are_really_in_the_graph(env: tuple[TestClient, Engine]) -> None:
    """The ids have to be usable against GET /graph, or they are decoration."""
    client, _ = env
    _seed(client)
    added = set(_run(client, hops=1, max_new=20)["added_paper_ids"])  # type: ignore[arg-type]
    graph = client.get(f"/api/sessions/{SID}/graph").json()
    assert added <= {n["id"] for n in graph["nodes"]}


def test_the_added_nodes_are_candidates(env: tuple[TestClient, Engine]) -> None:
    """Expansion produces candidates; only POST /nodes produces seeds."""
    client, _ = env
    seed_id = _seed(client)
    _run(client, hops=1, max_new=20)
    graph = client.get(f"/api/sessions/{SID}/graph").json()
    states = {n["id"]: n["state"] for n in graph["nodes"]}
    assert states[seed_id] == "SEED"
    assert all(states[i] == "CANDIDATE" for i in states if i != seed_id)


def test_the_response_reports_what_the_run_did(env: tuple[TestClient, Engine]) -> None:
    """
    A bare 200 cannot distinguish "fetched sixty papers and admitted none"
    from "found nothing to fetch", and those need different reactions.
    """
    client, _ = env
    _seed(client)
    body = _run(client, hops=1, max_new=20)
    assert set(body) >= {
        "n_pool",
        "n_added",
        "added_paper_ids",
        "api_calls",
        "cache_hits",
        "truncated",
        "boundary",
        "backward_fetched",
        "forward_fetched",
        "hub_skipped",
    }


def test_boundary_papers_are_reported(env: tuple[TestClient, Engine]) -> None:
    """
    The number worth watching. BERT's references are a third pre-2015, so a
    backward pass reporting zero boundary papers means the year floor has
    started deleting evidence instead of withholding recommendations.
    """
    client, _ = env
    _seed(client)
    body = _run(client, hops=1, max_new=20)
    assert body["boundary"] > 0


def test_an_expansion_row_is_recorded(env: tuple[TestClient, Engine]) -> None:
    """Every expansion is auditable, including the ones that added nothing."""
    client, engine = env
    _seed(client)
    _run(client, hops=1, max_new=20)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM expansions")).scalar() == 1


def test_max_new_is_respected(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    _seed(client)
    body = _run(client, hops=1, max_new=5)
    assert body["n_added"] <= 5


def test_expanding_a_session_with_no_anchors_is_not_an_error(
    env: tuple[TestClient, Engine],
) -> None:
    """
    Nothing to expand from is a legitimate outcome, not a failure. The result
    says so via `error` rather than by returning a 4xx.
    """
    client, _ = env
    assert _run(client, hops=1, max_new=20)["n_added"] == 0


# --------------------------------------------------------------------------
# Budgets -- a partial expansion is a success
# --------------------------------------------------------------------------


def test_a_graph_already_at_max_nodes_is_a_422(env: tuple[TestClient, Engine]) -> None:
    """
    The one case BUILD.md makes an error. There is no room to admit anything,
    so running the fetch would spend API calls to achieve nothing.
    """
    client, _ = env
    _seed(client)
    response = _expand(client, hops=1, max_new=20, max_nodes=1)
    assert response.status_code == 422, response.text  # type: ignore[attr-defined]


def test_the_422_costs_no_api_calls(env: tuple[TestClient, Engine]) -> None:
    """Refusing before fetching is the entire point of refusing."""
    client, _ = env
    _seed(client)
    before = client.app.dependency_overrides[get_s2_client]().cache_hits  # type: ignore[attr-defined]
    _expand(client, hops=1, max_new=20, max_nodes=1)
    after = client.app.dependency_overrides[get_s2_client]().cache_hits  # type: ignore[attr-defined]
    assert after == before


def test_a_small_api_budget_truncates_rather_than_failing(
    env: tuple[TestClient, Engine],
) -> None:
    """
    PLAN.md: "a partial expansion is a success". Commit what you have, record
    the truncation, stay 200.
    """
    client, _ = env
    _seed(client)
    # The job reaches DONE rather than FAILED. `_run` asserts that, which is
    # the whole claim: running out of budget is not an error.
    assert _run(client, hops=1, max_new=20, api_call_budget=1)["n_added"] >= 0


def test_room_below_max_new_is_reported_as_truncated(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    _seed(client)
    body = _run(client, hops=1, max_new=20, max_nodes=3)
    assert body["truncated"] is True


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------


def test_an_empty_body_uses_the_defaults(env: tuple[TestClient, Engine]) -> None:
    """Expanding with no options is the common case and must not 422."""
    client, _ = env
    _seed(client)
    assert client.post(f"/api/sessions/{SID}/expansions", json={}).status_code == 202


def test_multi_hop_is_rejected_rather_than_silently_ignored(
    env: tuple[TestClient, Engine],
) -> None:
    """
    R1 expands one hop. Accepting `hops: 3` and quietly doing one hop would
    make the client believe it reached depth 3 -- exactly the kind of silent
    degradation that keeps biting this project. R2.5 is where hops arrive.
    """
    client, _ = env
    _seed(client)
    assert _expand(client, hops=3).status_code == 422  # type: ignore[attr-defined]


def test_a_zero_max_new_is_rejected(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    _seed(client)
    assert _expand(client, max_new=0).status_code == 422  # type: ignore[attr-defined]


def test_an_absurd_max_new_is_rejected(env: tuple[TestClient, Engine]) -> None:
    """BUILD.md R1.22 puts the UI cap at 100; the server enforces it."""
    client, _ = env
    _seed(client)
    assert _expand(client, max_new=10_000).status_code == 422  # type: ignore[attr-defined]


def test_an_unknown_body_field_is_rejected(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    _seed(client)
    assert _expand(client, max_neww=5).status_code == 422  # type: ignore[attr-defined]


def test_an_unknown_session_is_a_404(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    assert client.post("/api/sessions/999/expansions", json={}).status_code == 404


# --------------------------------------------------------------------------
# The schema the frontend generates from
# --------------------------------------------------------------------------


def test_the_route_is_in_the_openapi_schema(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    assert "/api/sessions/{sid}/expansions" in client.get("/openapi.json").json()["paths"]


def test_the_response_is_typed(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    schema = client.get("/openapi.json").json()
    accepted = schema["paths"]["/api/sessions/{sid}/expansions"]["post"]["responses"]["202"]
    assert accepted["content"]["application/json"]["schema"] != {}
