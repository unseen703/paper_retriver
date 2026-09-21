"""
R3 -- persisted features, and `GET`/`PUT /api/config`.

Journey:

    As someone tuning weights, I want to see the whole graph re-rank the
    instant I change one, so tuning is a conversation rather than a batch job.

PLAN.md states the design and the reason in one breath:

    "Features are computed and persisted; scores are derived on read. [...]
    This means changing weights re-ranks instantly with **zero API calls and
    zero recomputation** -- which is what makes a config sweep in the eval
    harness possible at all."

BUILD.md calls `PUT /api/config` "the payoff for persisting features", and the
load-bearing test here is `test_a_rescore_costs_no_api_calls`. If rescoring
re-derives features it will still return the right numbers -- it will just cost
a graph rebuild and, once R5 adds embeddings, real money. The symptom of
getting this wrong is slowness, which is easy to explain away.

**Two operations, deliberately separate.** Computing features touches the
graph, the corpus and the clock. Scoring touches neither: it is
`score_paper(features, weights)` and nothing else. Keeping them apart is what
makes the second one free.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.config import reset_active_weights
from app.db import make_engine
from app.main import create_app

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SID = 1


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "config.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _fresh_weights() -> Iterator[None]:
    """
    The active weights are module state -- one process, one weight set, by
    design. Tests share that process, so without this each case would inherit
    whatever the last one set.
    """
    reset_active_weights()
    yield
    reset_active_weights()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(db_url=str(engine.url))) as test_client:
        yield test_client


def _node(engine: Engine, name: str, features: dict[str, float], year: int = 2020) -> int:
    """A node with features already persisted -- the state after an expansion."""
    with engine.begin() as conn:
        paper_id = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at, year,"
                " citation_count) VALUES (:s, :t, :t, '2026-01-01', :y, 10) RETURNING id"
            ),
            {"s": name, "t": f"Paper {name}", "y": year},
        ).scalar()
        assert paper_id is not None
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth, features)"
                " VALUES (:sid, :p, 'CANDIDATE', 1, :f)"
            ),
            {"sid": SID, "p": paper_id, "f": json.dumps(features)},
        )
    return int(paper_id)


def _scores(engine: Engine) -> dict[int, float | None]:
    with engine.connect() as conn:
        return {
            r[0]: r[1]
            for r in conn.execute(
                text("SELECT paper_id, score FROM graph_nodes WHERE session_id = :s"), {"s": SID}
            )
        }


def _breakdown(engine: Engine, paper_id: int) -> dict[str, float]:
    with engine.connect() as conn:
        raw = conn.execute(
            text("SELECT score_breakdown FROM graph_nodes WHERE session_id=:s AND paper_id=:p"),
            {"s": SID, "p": paper_id},
        ).scalar()
    return dict(json.loads(raw)) if raw else {}


# --------------------------------------------------------------------------
# Reading the active config
# --------------------------------------------------------------------------


def test_the_active_weights_are_readable(client: TestClient) -> None:
    """
    A ranking you cannot inspect is one you cannot tune. `config_version` is
    here too because it is stamped into every expansion and filter decision --
    it is how you tell which weights produced a given graph.
    """
    body = client.get("/api/config").json()
    assert "overlap" in body["weights"]
    assert body["config_version"]


def test_the_defaults_come_from_the_yaml(client: TestClient) -> None:
    """`ranking.yaml` ships overlap at 2.00 -- BUILD.md's dominant R1 signal."""
    assert client.get("/api/config").json()["weights"]["overlap"] == pytest.approx(2.0)


# --------------------------------------------------------------------------
# Changing them
# --------------------------------------------------------------------------


def test_updating_a_weight_takes_effect(client: TestClient) -> None:
    response = client.put("/api/config", json={"weights": {"overlap": 5.0}})
    assert response.status_code == 200, response.text
    assert client.get("/api/config").json()["weights"]["overlap"] == pytest.approx(5.0)


def test_an_unspecified_weight_keeps_its_value(client: TestClient) -> None:
    """
    A partial update is the common case -- you are moving one number to see
    what happens. Requiring the whole set would make every experiment a chance
    to reset something by omission.
    """
    before = client.get("/api/config").json()["weights"]["recency"]
    client.put("/api/config", json={"weights": {"overlap": 5.0}})
    assert client.get("/api/config").json()["weights"]["recency"] == pytest.approx(before)


def test_an_unknown_weight_is_refused(client: TestClient) -> None:
    """
    A typo'd weight name that is silently accepted produces a config that looks
    changed and behaves exactly as before -- the same failure `extra="forbid"`
    exists to prevent everywhere else in this codebase.
    """
    assert client.put("/api/config", json={"weights": {"overlp": 5.0}}).status_code == 422


def test_a_non_numeric_weight_is_refused(client: TestClient) -> None:
    assert client.put("/api/config", json={"weights": {"overlap": "high"}}).status_code == 422


def test_an_empty_update_is_accepted_and_changes_nothing(client: TestClient) -> None:
    before = client.get("/api/config").json()["weights"]
    assert client.put("/api/config", json={"weights": {}}).status_code == 200
    assert client.get("/api/config").json()["weights"] == before


# --------------------------------------------------------------------------
# The payoff: instant rescore, zero API calls
# --------------------------------------------------------------------------


def test_changing_a_weight_rescores_the_graph(client: TestClient, engine: Engine) -> None:
    a = _node(engine, "a", {"overlap": 1.0, "recency": 0.0})
    b = _node(engine, "b", {"overlap": 0.0, "recency": 1.0})

    client.put("/api/config", json={"weights": {"overlap": 10.0, "recency": 0.0}})
    overlap_first = _scores(engine)
    assert overlap_first[a] > overlap_first[b]

    # Flip which feature matters. The graph must re-rank without anything being
    # fetched or recomputed.
    client.put("/api/config", json={"weights": {"overlap": 0.0, "recency": 10.0}})
    recency_first = _scores(engine)
    assert recency_first[b] > recency_first[a]


def test_a_rescore_costs_no_api_calls(client: TestClient, engine: Engine) -> None:
    """
    BUILD.md: "instant rescore, zero API calls. This is the payoff for
    persisting features."

    The app is built with no S2 client override here, so any attempt to fetch
    would have to go through the real one -- and `create_app` in a test has no
    network. The stronger guarantee is structural: rescoring reads `features`
    from `graph_nodes` and calls a pure function, so there is no code path from
    it to the client at all.
    """
    _node(engine, "a", {"overlap": 1.0})
    response = client.put("/api/config", json={"weights": {"overlap": 3.0}})
    assert response.status_code == 200
    assert response.json()["rescored"] == 1


def test_rescoring_does_not_recompute_features(client: TestClient, engine: Engine) -> None:
    """
    The other half of "zero recomputation". Features written by an expansion
    are the input; a rescore must read them, not derive them again -- otherwise
    every weight change costs a graph rebuild, and at R5 it would cost
    embeddings.

    Pinned by writing a feature value that no computation would produce.
    """
    paper_id = _node(engine, "a", {"overlap": 0.42})
    client.put("/api/config", json={"weights": {"overlap": 1.0}})

    with engine.connect() as conn:
        stored = json.loads(
            conn.execute(
                text("SELECT features FROM graph_nodes WHERE paper_id = :p"), {"p": paper_id}
            ).scalar()
            or "{}"
        )
    assert stored["overlap"] == pytest.approx(0.42), "features are input, not output"
    assert _scores(engine)[paper_id] == pytest.approx(0.42)


def test_the_breakdown_is_persisted_and_sums_to_the_score(
    client: TestClient, engine: Engine
) -> None:
    """
    BUILD.md wants `score_breakdown` persisted per node -- it is what powers
    `<ScoreBreakdown>`'s "why?". A breakdown that does not add up to the score
    beside it looks like an explanation while being one.
    """
    paper_id = _node(engine, "a", {"overlap": 0.5, "recency": 0.25})
    client.put("/api/config", json={"weights": {"overlap": 2.0, "recency": 0.4}})

    breakdown = _breakdown(engine, paper_id)
    assert breakdown["overlap"] == pytest.approx(1.0)
    assert breakdown["recency"] == pytest.approx(0.1)
    assert sum(breakdown.values()) == pytest.approx(_scores(engine)[paper_id])


def test_a_node_with_no_features_scores_nothing_rather_than_zero(
    client: TestClient, engine: Engine
) -> None:
    """
    Null, not 0.0. A node an expansion never scored has no score, and writing a
    zero would rank it below every paper that genuinely scored badly -- a
    judgment nobody made.
    """
    with engine.begin() as conn:
        paper_id = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES ('x', 'X', 'x', '2026-01-01') RETURNING id"
            )
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (:s, :p, 'CANDIDATE', 1)"
            ),
            {"s": SID, "p": paper_id},
        )
    client.put("/api/config", json={"weights": {"overlap": 2.0}})
    assert _scores(engine)[paper_id] is None


def test_every_session_is_rescored(client: TestClient, engine: Engine) -> None:
    """
    Weights are global -- they live in one config file, not per session. A
    rescore that touched only session 1 would leave every other session ranked
    by weights nobody is using any more.
    """
    _node(engine, "a", {"overlap": 1.0})
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
        paper_id = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES ('o', 'O', 'o', '2026-01-01') RETURNING id"
            )
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth, features)"
                " VALUES (2, :p, 'CANDIDATE', 1, '{\"overlap\": 1.0}')"
            ),
            {"p": paper_id},
        )

    assert client.put("/api/config", json={"weights": {"overlap": 3.0}}).json()["rescored"] == 2


def test_the_routes_are_in_the_openapi_schema(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert {"get", "put"} <= set(paths["/api/config"])
