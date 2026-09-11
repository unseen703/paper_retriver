"""
R2.4 -- the job queue.

Journey:

    As someone expanding a graph, I want the request to come back immediately
    with something I can watch, so a run that takes half a minute does not look
    like a frozen page.

BUILD.md's verification is the last section: two concurrent POSTs, and the
second gets a 409. That is the one that matters, because the failure it guards
against is not a slow page -- it is two expansions interleaving writes to the
same `graph_nodes` rows and leaving a graph neither of them intended.

**The job id is the expansion id.** BUILD.md says "jobs table"; PLAN.md section
C defines no such table and defines `expansions` with every column a job needs.
Two tables would be two rows per run to keep in step. There is a test below
asserting the row really is the same one.

**Stage is derived, not stored.** No column holds it, and none should: a
stored stage is a second copy of the truth, and the copy that lies when a
process dies mid-run.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

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
from app.repo import expansions as expansions_repo

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
FIXTURE_DB = Path(__file__).resolve().parents[1] / "fixtures" / "s2_cache.db"

SID = 1
SEED_TITLE = "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"

pytestmark = pytest.mark.skipif(
    not FIXTURE_DB.is_file(),
    reason="fixture cache absent; build with scripts/build_fixture_cache.py",
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[TestClient, Engine]]:
    db = tmp_path / "jobs.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")

    engine = make_engine(f"sqlite:///{db.as_posix()}")
    fixture_engine = make_engine(f"sqlite:///{FIXTURE_DB.as_posix()}")
    application = create_app(db_url=f"sqlite:///{db.as_posix()}")
    # The worker resolves its client through this same override, so background
    # work is offline for the same structural reason requests are.
    application.dependency_overrides[get_s2_client] = lambda: CachedOnlyS2Client(
        cache=ResponseCache(fixture_engine)
    )
    with TestClient(application) as client:
        yield client, engine
    fixture_engine.dispose()
    engine.dispose()


def _seed(client: TestClient) -> int:
    hits = client.get(f"/api/sessions/{SID}/search", params={"q": SEED_TITLE}).json()
    response = client.post(
        f"/api/sessions/{SID}/nodes", json={"s2_paper_id": hits[0]["s2_paper_id"]}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["paper_id"])


def _queue(client: TestClient, **body: object) -> Any:
    return client.post(f"/api/sessions/{SID}/expansions", json=body)


def _poll(client: TestClient, job_id: int) -> dict[str, Any]:
    response = client.get(f"/api/sessions/{SID}/expansions/{job_id}")
    assert response.status_code == 200, response.text
    return dict(response.json())


def _await_done(client: TestClient, job_id: int, timeout: float = 60.0) -> dict[str, Any]:
    """Poll until the job leaves QUEUED/RUNNING, like the UI will."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = _poll(client, job_id)
        if body["status"] not in ("QUEUED", "RUNNING"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished; last status {body['status']}")


# --------------------------------------------------------------------------
# Accepting the work
# --------------------------------------------------------------------------


def test_queueing_an_expansion_returns_202(env: tuple[TestClient, Engine]) -> None:
    """
    R1.18 returned 200 with the finished result and had a test named
    `test_expanding_returns_200_not_202` pinning it. R2.4 is the turn where
    that inverts, because now there is something to poll.
    """
    client, _ = env
    _seed(client)
    response = _queue(client)
    assert response.status_code == 202, response.text


def test_the_202_carries_an_id_and_where_to_poll(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    _seed(client)
    body = _queue(client).json()
    assert body["job_id"] >= 1
    assert body["status"] == "QUEUED"
    assert body["poll"] == f"/api/sessions/{SID}/expansions/{body['job_id']}"


def test_the_202_sets_a_location_header(env: tuple[TestClient, Engine]) -> None:
    """What a 202 is for, for anything that speaks HTTP rather than this schema."""
    client, _ = env
    _seed(client)
    response = _queue(client)
    assert response.headers["location"] == response.json()["poll"]


def test_the_job_is_a_row_in_expansions(env: tuple[TestClient, Engine]) -> None:
    """
    BUILD.md says "jobs table"; PLAN.md section C defines `expansions` and no
    `jobs` table. This pins that the job id really is the expansion id rather
    than a parallel bookkeeping scheme.
    """
    client, engine = env
    _seed(client)
    job_id = _queue(client).json()["job_id"]
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT session_id, status FROM expansions WHERE id = :i"), {"i": job_id}
        ).fetchone()
    assert row is not None
    assert row[0] == SID
    assert row[1] in ("QUEUED", "RUNNING", "DONE")


# --------------------------------------------------------------------------
# Doing the work
# --------------------------------------------------------------------------


def test_the_worker_runs_the_job_to_completion(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    _seed(client)
    job_id = _queue(client).json()["job_id"]
    assert _await_done(client, job_id)["status"] == "DONE"


def test_a_finished_job_carries_the_result(env: tuple[TestClient, Engine]) -> None:
    """
    The client needs the admitted ids to update its picture. Making it refetch
    the whole graph to discover three new nodes would undo the point of
    reporting them.
    """
    client, _ = env
    _seed(client)
    job_id = _queue(client).json()["job_id"]
    body = _await_done(client, job_id)
    assert body["result"] is not None
    assert body["result"]["n_added"] >= 1
    assert len(body["result"]["added_paper_ids"]) == body["result"]["n_added"]


def test_the_added_ids_are_really_in_the_graph(env: tuple[TestClient, Engine]) -> None:
    client, engine = env
    _seed(client)
    job_id = _queue(client).json()["job_id"]
    added = _await_done(client, job_id)["result"]["added_paper_ids"]
    with engine.connect() as conn:
        present = {
            r[0]
            for r in conn.execute(
                text("SELECT paper_id FROM graph_nodes WHERE session_id = :s"), {"s": SID}
            )
        }
    assert set(added) <= present


def test_an_unfinished_job_has_no_result(env: tuple[TestClient, Engine]) -> None:
    """
    Null rather than an empty result. A zero-count result would be
    indistinguishable from a finished run that admitted nothing.
    """
    client, _ = env
    _seed(client)
    job_id = _queue(client).json()["job_id"]
    body = _poll(client, job_id)
    if body["status"] in ("QUEUED", "RUNNING"):
        assert body["result"] is None


def test_progress_counters_are_filled_in_by_the_end(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    _seed(client)
    job_id = _queue(client).json()["job_id"]
    progress = _await_done(client, job_id)["progress"]
    assert progress["pool"] is not None
    assert progress["added"] is not None
    assert progress["api_calls"] is not None


def test_the_stage_ends_at_done(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    _seed(client)
    job_id = _queue(client).json()["job_id"]
    assert _await_done(client, job_id)["stage"] == "DONE"


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"status": "QUEUED", "n_filtered": None, "n_pool": None, "n_added": None}, "QUEUED"),
        ({"status": "RUNNING", "n_filtered": None, "n_pool": None, "n_added": None}, "FETCHING"),
        ({"status": "RUNNING", "n_filtered": 12, "n_pool": None, "n_added": None}, "POOLING"),
        ({"status": "RUNNING", "n_filtered": 12, "n_pool": 340, "n_added": None}, "RANKING"),
        ({"status": "RUNNING", "n_filtered": 12, "n_pool": 340, "n_added": 20}, "ADDING"),
        ({"status": "DONE", "n_filtered": 12, "n_pool": 340, "n_added": 20}, "DONE"),
        ({"status": "FAILED", "n_filtered": None, "n_pool": None, "n_added": None}, "FAILED"),
        # Zero is a real answer, not a missing one. Reading it as "not yet"
        # would leave a run that pooled nothing stuck on POOLING forever.
        ({"status": "RUNNING", "n_filtered": 0, "n_pool": 0, "n_added": 0}, "ADDING"),
    ],
)
def test_the_stage_is_derived_from_the_counters(record: dict[str, Any], expected: str) -> None:
    """
    Stage has no column, and should not: a stored stage is a second copy of
    the truth, and it is the copy that lies when a process dies mid-run. The
    counters already say how far the run got.

    Tested directly rather than through the API because catching a job in a
    given stage over a cached fixture is a race -- the worker finishes in
    milliseconds offline.
    """
    assert expansions_repo.stage_of(record) == expected


# --------------------------------------------------------------------------
# BUILD.md's verification -- one expansion per session
# --------------------------------------------------------------------------


def test_two_concurrent_posts_and_the_second_is_a_409(env: tuple[TestClient, Engine]) -> None:
    """
    BUILD.md R2.4: "Two concurrent POSTs -> second gets 409."

    Genuinely concurrent, from two threads, because the bug this guards is a
    race: a check-then-insert in two statements lets both requests see an empty
    queue and both insert. Sequential calls would pass even then.
    """
    client, _ = env
    _seed(client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [f.result() for f in [pool.submit(_queue, client) for _ in range(2)]]

    codes = sorted(r.status_code for r in responses)
    assert codes == [202, 409], [r.status_code for r in responses]


def test_the_409_names_the_job_already_in_flight(env: tuple[TestClient, Engine]) -> None:
    """
    So a client that lost track of its job can poll the running one instead of
    only being told no.
    """
    client, engine = env
    _seed(client)
    # A RUNNING row written directly, not one queued through the endpoint. The
    # worker only ever claims QUEUED rows, so this one stays in flight for as
    # long as the test needs -- where queueing a real job and then forcing it
    # to RUNNING races the worker finishing it, which offline it usually wins.
    with engine.begin() as conn:
        first = int(
            conn.execute(
                text(
                    "INSERT INTO expansions (session_id, status, params, config_version,"
                    " started_at) VALUES (:s, 'RUNNING', '{}', 'v1', '2026-01-01') RETURNING id"
                ),
                {"s": SID},
            ).scalar()
            or 0
        )

    response = _queue(client)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "EXPANSION_IN_FLIGHT"
    assert detail["job_id"] == first


def test_a_second_expansion_is_allowed_once_the_first_finishes(
    env: tuple[TestClient, Engine],
) -> None:
    """The 409 is a lock, not a limit -- it has to lift."""
    client, _ = env
    _seed(client)
    first = _queue(client).json()["job_id"]
    _await_done(client, first)
    assert _queue(client).status_code == 202


def test_another_session_is_not_blocked(env: tuple[TestClient, Engine]) -> None:
    """
    The rule is one job per session, not one per process. Two sessions
    expanding at once is a coherent thing to want; two runs inside one session
    would interleave writes to the same rows.
    """
    client, engine = env
    _seed(client)
    with engine.begin() as conn:
        # Session 1 held busy by a row the worker will not claim.
        conn.execute(
            text(
                "INSERT INTO expansions (session_id, status, params, config_version, started_at)"
                " VALUES (:s, 'RUNNING', '{}', 'v1', '2026-01-01')"
            ),
            {"s": SID},
        )
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
    assert client.post("/api/sessions/2/expansions", json={}).status_code == 202


# --------------------------------------------------------------------------
# Refusals that still happen before the 202
# --------------------------------------------------------------------------


def test_a_full_graph_is_still_a_422_not_a_queued_job(env: tuple[TestClient, Engine]) -> None:
    """
    Queueing a job that can only ever admit zero papers would turn an answer
    the caller can act on now into one they have to poll for.
    """
    client, _ = env
    _seed(client)
    assert _queue(client, max_nodes=1).status_code == 422


def test_an_invalid_body_is_still_a_422(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    _seed(client)
    assert _queue(client, hops=3).status_code == 422
    assert _queue(client, max_new=0).status_code == 422
    assert _queue(client, max_neww=5).status_code == 422


def test_an_unknown_session_is_a_404(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    assert client.post("/api/sessions/999/expansions", json={}).status_code == 404


# --------------------------------------------------------------------------
# Cancelling — PLAN.md section G's `DELETE /api/expansions/{job_id}`
# --------------------------------------------------------------------------
#
# "Cooperative" is the operative word. A QUEUED job can be cancelled outright
# because nothing has happened yet. A RUNNING one is asked to stop at the next
# checkpoint, and whatever it already committed stays committed — papers were
# fetched and rows were written, and pretending otherwise would mean either
# lying about the state or rolling back work the user paid API calls for.


def _pause_worker() -> None:
    """
    Stop the background worker so a QUEUED job stays queued.

    Without this, every test that cancels a QUEUED row is racing the worker --
    `claim_next` takes any queued job regardless of session, so the test passes
    or fails depending on which won. A flaky test is worse than a failing one:
    it gets re-run until it goes green and then believed.
    """
    from app.api import deps

    deps.get_worker().stop()


def test_cancelling_a_queued_job_reports_it_cancelled(env: tuple[TestClient, Engine]) -> None:
    client, engine = env
    _seed(client)
    _pause_worker()
    with engine.begin() as conn:
        job_id = expansions_repo.enqueue(conn, SID, {"hops": 1, "max_new": 5}, "testcfg")

    response = client.delete(f"/api/sessions/{SID}/expansions/{job_id}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"


def test_a_cancelled_job_is_never_claimed(env: tuple[TestClient, Engine]) -> None:
    """
    The point of cancelling something QUEUED. `claim_next` is the only way work
    starts, so a cancelled row it refuses to pick up can never run.
    """
    _, engine = env
    with engine.begin() as conn:
        job_id = expansions_repo.enqueue(conn, SID, {"hops": 1, "max_new": 5}, "testcfg")
        assert expansions_repo.cancel(conn, SID, job_id) == "QUEUED"
        assert expansions_repo.claim_next(conn) is None


def test_finishing_does_not_resurrect_a_cancelled_job(env: tuple[TestClient, Engine]) -> None:
    """
    **The subtle one.** Cancelling a RUNNING job is a race by construction: the
    worker is mid-flight and will call `finish` when it stops. `finish` wrote
    `status` unconditionally, so a job cancelled a moment before completion
    came back as DONE — the cancel silently undone, which is worse than a
    cancel that fails loudly.
    """
    _, engine = env
    with engine.begin() as conn:
        job_id = expansions_repo.enqueue(conn, SID, {"hops": 1, "max_new": 5}, "testcfg")
        expansions_repo.claim_next(conn)  # -> RUNNING
        expansions_repo.cancel(conn, SID, job_id)
        expansions_repo.finish(
            conn,
            SID,
            job_id,
            status="DONE",
            n_pool=3,
            n_filtered=1,
            n_added=2,
            api_calls=4,
            cache_hits=0,
        )
        record = expansions_repo.get(conn, SID, job_id)

    assert record is not None
    assert record["status"] == "CANCELLED", "a cancelled job must stay cancelled"


def test_the_counters_a_cancelled_run_earned_are_kept(env: tuple[TestClient, Engine]) -> None:
    """
    Cancelling stops the work; it does not erase what the work already cost.
    An api_calls count of zero on a job that spent twenty would misreport the
    budget the run actually consumed.
    """
    _, engine = env
    with engine.begin() as conn:
        job_id = expansions_repo.enqueue(conn, SID, {"hops": 1, "max_new": 5}, "testcfg")
        expansions_repo.claim_next(conn)
        expansions_repo.cancel(conn, SID, job_id)
        expansions_repo.finish(
            conn,
            SID,
            job_id,
            status="DONE",
            n_pool=3,
            n_filtered=1,
            n_added=2,
            api_calls=4,
            cache_hits=0,
        )
        record = expansions_repo.get(conn, SID, job_id)

    assert record is not None
    assert record["api_calls"] == 4
    assert record["n_added"] == 2


def test_cancelling_a_finished_job_is_a_409(env: tuple[TestClient, Engine]) -> None:
    """
    There is nothing to stop. A 200 here would report success for an action
    that did nothing, which is the reading that makes a cancel button untrustworthy.
    """
    client, _ = env
    _seed(client)
    job_id = _queue(client).json()["job_id"]
    _await_done(client, job_id)

    response = client.delete(f"/api/sessions/{SID}/expansions/{job_id}")
    assert response.status_code == 409, response.text


def test_cancelling_twice_is_a_409(env: tuple[TestClient, Engine]) -> None:
    client, engine = env
    _seed(client)
    _pause_worker()
    with engine.begin() as conn:
        job_id = expansions_repo.enqueue(conn, SID, {"hops": 1, "max_new": 5}, "testcfg")

    assert client.delete(f"/api/sessions/{SID}/expansions/{job_id}").status_code == 200
    assert client.delete(f"/api/sessions/{SID}/expansions/{job_id}").status_code == 409


def test_cancelling_an_unknown_job_is_a_404(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    _seed(client)
    assert client.delete(f"/api/sessions/{SID}/expansions/424242").status_code == 404


def test_cancelling_another_sessions_job_is_a_404(env: tuple[TestClient, Engine]) -> None:
    """Absent, not forbidden — the same reasoning that put {sid} in the route."""
    client, engine = env
    _seed(client)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
        job_id = expansions_repo.enqueue(conn, SID, {"hops": 1, "max_new": 5}, "testcfg")

    assert client.delete(f"/api/sessions/2/expansions/{job_id}").status_code == 404


def test_cancelling_clears_the_way_for_a_new_expansion(env: tuple[TestClient, Engine]) -> None:
    """
    What cancelling is *for*. An active job makes the next POST a 409, so a run
    that is no longer wanted blocks the session until it is cancelled.
    """
    client, engine = env
    _seed(client)
    _pause_worker()
    with engine.begin() as conn:
        job_id = expansions_repo.enqueue(conn, SID, {"hops": 1, "max_new": 5}, "testcfg")

    assert _queue(client).status_code == 409
    assert client.delete(f"/api/sessions/{SID}/expansions/{job_id}").status_code == 200
    assert _queue(client).status_code == 202


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------


def test_an_unknown_job_is_a_404(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    _seed(client)
    assert client.get(f"/api/sessions/{SID}/expansions/424242").status_code == 404


def test_another_sessions_job_is_a_404(env: tuple[TestClient, Engine]) -> None:
    """
    Absent, not forbidden. A stale id from another session must not leak that
    session's counts -- the same reasoning that put {sid} in the route.
    """
    client, engine = env
    _seed(client)
    job_id = _queue(client).json()["job_id"]
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
    assert client.get(f"/api/sessions/2/expansions/{job_id}").status_code == 404


def test_both_routes_are_in_the_openapi_schema(env: tuple[TestClient, Engine]) -> None:
    """`make types` generates the frontend's client from this."""
    client, _ = env
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/sessions/{sid}/expansions" in paths
    assert "/api/sessions/{sid}/expansions/{job_id}" in paths


def test_the_202_is_the_declared_response(env: tuple[TestClient, Engine]) -> None:
    client, _ = env
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/api/sessions/{sid}/expansions"]["post"]["responses"]
    assert "202" in responses


# --------------------------------------------------------------------------
# Crash recovery
# --------------------------------------------------------------------------


def test_a_running_job_left_by_a_dead_process_is_failed_at_startup(tmp_path: Path) -> None:
    """
    The worker lives and dies with the process, so a RUNNING row at startup is
    one nothing is executing. Left alone it would block its session on a 409
    forever, for a job that will never finish and with no way to see why.
    """
    db = tmp_path / "crash.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as conn:
        # Session 1 already exists -- migration 0001 seeds it.
        conn.execute(
            text(
                "INSERT INTO expansions (id, session_id, status, params, config_version,"
                " started_at) VALUES (7, 1, 'RUNNING', '{}', 'v1', '2026-01-01')"
            )
        )

    application = create_app(db_url=f"sqlite:///{db.as_posix()}")
    with TestClient(application) as client:
        body = client.get(f"/api/sessions/{SID}/expansions/7").json()
        assert body["status"] == "FAILED"
        assert "interrupted" in (body["error"] or "")
        # And the session is usable again rather than stuck on a 409.
        assert client.post(f"/api/sessions/{SID}/expansions", json={}).status_code == 202
    engine.dispose()


def test_a_missing_database_does_not_stop_the_app_from_starting(tmp_path: Path) -> None:
    """
    The worker's startup sweep runs inside the lifespan, so an exception there
    stops the whole app -- including `/api/health`, whose entire job is to tell
    you the database is unreachable. Reporting the problem beats refusing to
    boot and saying nothing.
    """
    application = create_app(db_url=f"sqlite:///{tmp_path / 'never-migrated.db'}")
    with TestClient(application) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health").json()["db"].startswith("error")


# --------------------------------------------------------------------------
# Review findings on R2.4 itself
# --------------------------------------------------------------------------


def test_a_run_with_no_anchors_is_not_reported_as_truncated(
    env: tuple[TestClient, Engine],
) -> None:
    """
    `truncated` is derived from the row, because no column holds it -- and the
    first version derived it as "does `error` have anything in it", which is
    wrong for the one case that sets `error` without stopping early.

    A session with no seeds has nothing to expand from. That run did not run
    out of anything; there was nothing to run out of. Calling it truncated
    invites the user to raise a budget that was never the constraint.
    """
    client, _ = env  # deliberately not seeded
    job_id = _queue(client).json()["job_id"]
    body = _await_done(client, job_id)
    assert body["status"] == "DONE"
    assert body["result"]["error"]  # the reason is still reported
    assert body["result"]["truncated"] is False


def test_the_worker_survives_a_job_that_explodes(env: tuple[TestClient, Engine]) -> None:
    """
    A worker thread that dies takes every later job with it: they sit QUEUED
    forever, and their sessions stay blocked on a 409 for work nothing is
    doing. `_execute` was called outside the loop's `try`, and its own error
    handler writes to the database -- so a failure there escaped and ended the
    thread.

    Provoked by making the run itself raise, then checking that a perfectly
    ordinary job queued afterwards still completes.
    """
    client, _ = env
    _seed(client)
    from app.api import deps

    live = deps.get_worker()
    original = live._client_factory

    def exploding() -> Any:
        raise RuntimeError("boom")

    live._client_factory = exploding  # type: ignore[assignment]
    first = _queue(client).json()["job_id"]
    assert _await_done(client, first)["status"] == "FAILED"

    live._client_factory = original  # type: ignore[assignment]
    second = _queue(client).json()["job_id"]
    assert _await_done(client, second)["status"] == "DONE", "the worker survived the first job"
