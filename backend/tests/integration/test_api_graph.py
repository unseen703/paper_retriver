"""
R1.17 -- `GET /api/sessions/{sid}/graph`.

The whole graph in one response. PLAN.md is explicit: "One call, whole graph.
At 2k nodes this is ~400KB -- fine. Do NOT paginate until you've measured a
problem." A paginated graph endpoint is a category error anyway; a layout needs
every node and edge before it can place any of them.

**Only edges whose two endpoints are both in the graph are returned.** The
corpus is much larger than the graph -- boundary papers, rejects and stubs all
have edges -- and `repo.edges.get_edges_for` deliberately returns every edge
*touching* a set of papers, which is right for pooling and wrong here. An edge
pointing at a paper with no node would make Cytoscape either drop it or invent
a phantom node, and phantom nodes in a citation graph are indistinguishable
from real findings.

**Degrees are within the displayed graph, not the corpus.** How many of *these*
papers cite this one is the signal the picture shows; the global figure is
already carried separately as `citation_count`. Conflating them would make the
inspector claim a node has 40 in-edges in a graph that draws 3.

`crawl_completeness` is PLAN.md section I: `|non-stub| / |nodes|`. R3 suppresses
the PageRank column below 60%, so the figure has to travel with the graph
rather than being recomputed by the client.
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

from app.db import make_engine
from app.main import create_app

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"

SID = 1


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "graph.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


@pytest.fixture
def client(tmp_path: Path, engine: Engine) -> Iterator[TestClient]:
    url = str(engine.url)
    with TestClient(create_app(db_url=url)) as test_client:
        yield test_client


def _paper(
    engine: Engine,
    s2_id: str,
    title: str,
    *,
    year: int = 2020,
    citations: int = 10,
    paper_type: str = "RESEARCH",
    crawl_state: str = "METADATA",
) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at, year,"
                " citation_count, paper_type, crawl_state)"
                " VALUES (:s, :t, :tn, '2026-01-01', :y, :c, :pt, :cs) RETURNING id"
            ),
            {
                "s": s2_id,
                "t": title,
                "tn": title.lower(),
                "y": year,
                "c": citations,
                "pt": paper_type,
                "cs": crawl_state,
            },
        ).fetchone()
    assert row is not None
    return int(row[0])


def _node(
    engine: Engine,
    paper_id: int,
    state: str = "CANDIDATE",
    *,
    score: float | None = 1.5,
    depth: int = 1,
    sid: int = SID,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, score, depth)"
                " VALUES (:sid, :pid, :st, :sc, :d)"
            ),
            {"sid": sid, "pid": paper_id, "st": state, "sc": score, "d": depth},
        )


def _edge(engine: Engine, citing: int, cited: int, *, influential: bool = False) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO edges (citing_id, cited_id, discovered_via, is_influential,"
                " first_seen_at) VALUES (:a, :b, 'BACKWARD', :inf, '2026-01-01')"
            ),
            {"a": citing, "b": cited, "inf": 1 if influential else 0},
        )


def _graph(client: TestClient, **params: object) -> dict[str, object]:
    response = client.get(f"/api/sessions/{SID}/graph", params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


@pytest.fixture
def two_node_graph(engine: Engine) -> tuple[int, int]:
    """A cites B, both in the graph."""
    a = _paper(engine, "a", "Citing Paper", year=2022)
    b = _paper(engine, "b", "Cited Paper", year=2019)
    _node(engine, a, "SEED", depth=0)
    _node(engine, b, "CANDIDATE")
    _edge(engine, a, b)
    return a, b


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_an_empty_graph_is_a_200(client: TestClient) -> None:
    """No nodes is a valid graph, not a 404."""
    assert client.get(f"/api/sessions/{SID}/graph").status_code == 200


def test_an_empty_graph_has_empty_collections(client: TestClient) -> None:
    body = _graph(client)
    assert body["nodes"] == []
    assert body["edges"] == []


def test_the_response_has_nodes_edges_and_meta(client: TestClient) -> None:
    assert set(_graph(client)) >= {"nodes", "edges", "meta"}


def test_a_node_carries_the_documented_fields(
    client: TestClient, two_node_graph: tuple[int, int]
) -> None:
    """BUILD.md's list, verbatim."""
    node = _graph(client)["nodes"][0]  # type: ignore[index]
    assert set(node) >= {
        "id",
        "title",
        "state",
        "score",
        "year",
        "citation_count",
        "paper_type",
        "in_degree",
        "out_degree",
        "pos",
    }


def test_an_edge_carries_the_documented_fields(
    client: TestClient, two_node_graph: tuple[int, int]
) -> None:
    edge = _graph(client)["edges"][0]  # type: ignore[index]
    assert set(edge) >= {"source", "target", "is_influential"}


def test_meta_carries_the_documented_fields(client: TestClient) -> None:
    meta = _graph(client)["meta"]
    assert set(meta) >= {  # type: ignore[arg-type]
        "node_count",
        "edge_count",
        "config_version",
        "crawl_completeness",
    }


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


def test_every_graph_node_is_returned(client: TestClient, two_node_graph: tuple[int, int]) -> None:
    assert len(_graph(client)["nodes"]) == 2  # type: ignore[arg-type]


def test_a_paper_without_a_node_is_not_returned(client: TestClient, engine: Engine) -> None:
    """The corpus is not the graph -- boundary papers must not be drawn."""
    _paper(engine, "orphan", "Boundary Paper", year=2013)
    assert _graph(client)["nodes"] == []


def test_nodes_from_another_session_are_not_returned(client: TestClient, engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
    paper_id = _paper(engine, "p", "Other Session Paper")
    _node(engine, paper_id, sid=2)
    assert _graph(client)["nodes"] == []


def test_the_node_id_is_the_paper_id(client: TestClient, two_node_graph: tuple[int, int]) -> None:
    """
    Edges reference papers, so the node id the client sees has to be the same
    key the edges use -- otherwise the frontend needs a lookup table to draw a
    single line.
    """
    a, b = two_node_graph
    assert {n["id"] for n in _graph(client)["nodes"]} == {a, b}  # type: ignore[index,union-attr]


def test_nodes_are_ordered_deterministically(client: TestClient, engine: Engine) -> None:
    """CLAUDE.md rule 7. A graph that reorders between calls churns the layout."""
    for i in range(5):
        _node(engine, _paper(engine, f"p{i}", f"Paper {i}"))
    first = [n["id"] for n in _graph(client)["nodes"]]  # type: ignore[index,union-attr]
    second = [n["id"] for n in _graph(client)["nodes"]]  # type: ignore[index,union-attr]
    assert first == second == sorted(first)


def test_the_score_is_carried_through(client: TestClient, engine: Engine) -> None:
    _node(engine, _paper(engine, "p", "Scored"), score=2.75)
    assert _graph(client)["nodes"][0]["score"] == 2.75  # type: ignore[index]


def test_an_unscored_node_reports_a_null_score(client: TestClient, engine: Engine) -> None:
    """A seed has no score. Reporting 0.0 would sort it below every candidate."""
    _node(engine, _paper(engine, "p", "Seed"), "SEED", score=None, depth=0)
    assert _graph(client)["nodes"][0]["score"] is None  # type: ignore[index]


def test_the_position_is_null_until_a_layout_is_saved(
    client: TestClient, two_node_graph: tuple[int, int]
) -> None:
    """R2.12 persists positions; until then the client lays out from scratch."""
    assert _graph(client)["nodes"][0]["pos"] is None  # type: ignore[index]


def test_a_saved_position_is_returned(client: TestClient, engine: Engine) -> None:
    paper_id = _paper(engine, "p", "Placed")
    _node(engine, paper_id)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE graph_nodes SET pos_x = 12.5, pos_y = -3.5"
                " WHERE session_id = :sid AND paper_id = :pid"
            ),
            {"sid": SID, "pid": paper_id},
        )
    assert _graph(client)["nodes"][0]["pos"] == {"x": 12.5, "y": -3.5}  # type: ignore[index]


# --------------------------------------------------------------------------
# Edges -- the part that is easy to get wrong
# --------------------------------------------------------------------------


def test_an_edge_between_two_graph_nodes_is_returned(
    client: TestClient, two_node_graph: tuple[int, int]
) -> None:
    a, b = two_node_graph
    assert _graph(client)["edges"] == [  # type: ignore[comparison-overlap]
        {"source": a, "target": b, "is_influential": False}
    ]


def test_an_edge_to_a_paper_outside_the_graph_is_dropped(
    client: TestClient, engine: Engine
) -> None:
    """
    The whole point of this endpoint's edge query. A node cites a boundary
    paper; the boundary paper has no node, so drawing that edge would either
    lose it or invent a node that is not in the graph.
    """
    inside = _paper(engine, "in", "In The Graph")
    outside = _paper(engine, "out", "Boundary Paper", year=2013)
    _node(engine, inside, "SEED", depth=0)
    _edge(engine, inside, outside)
    body = _graph(client)
    assert len(body["nodes"]) == 1  # type: ignore[arg-type]
    assert body["edges"] == []


def test_every_edge_endpoint_is_a_returned_node(client: TestClient, engine: Engine) -> None:
    """The invariant that makes the response renderable without a lookup."""
    ids = [_paper(engine, f"p{i}", f"Paper {i}") for i in range(4)]
    for paper_id in ids[:3]:
        _node(engine, paper_id)
    _edge(engine, ids[0], ids[1])
    _edge(engine, ids[1], ids[2])
    _edge(engine, ids[2], ids[3])  # to a paper with no node

    body = _graph(client)
    node_ids = {n["id"] for n in body["nodes"]}  # type: ignore[index,union-attr]
    for edge in body["edges"]:  # type: ignore[union-attr]
        assert edge["source"] in node_ids
        assert edge["target"] in node_ids


def test_the_edge_direction_is_citing_to_cited(client: TestClient, engine: Engine) -> None:
    """
    PLAN.md section D: direction is stored once, citing -> cited. Flipping it
    in the API would silently invert every arrow the user reads.
    """
    citing = _paper(engine, "new", "Newer Paper", year=2023)
    cited = _paper(engine, "old", "Older Paper", year=2017)
    _node(engine, citing)
    _node(engine, cited)
    _edge(engine, citing, cited)
    edge = _graph(client)["edges"][0]  # type: ignore[index]
    assert edge["source"] == citing
    assert edge["target"] == cited


def test_the_influential_flag_is_carried(client: TestClient, engine: Engine) -> None:
    a = _paper(engine, "a", "A")
    b = _paper(engine, "b", "B")
    _node(engine, a)
    _node(engine, b)
    _edge(engine, a, b, influential=True)
    assert _graph(client)["edges"][0]["is_influential"] is True  # type: ignore[index]


def test_edges_are_ordered_deterministically(client: TestClient, engine: Engine) -> None:
    ids = [_paper(engine, f"p{i}", f"Paper {i}") for i in range(4)]
    for paper_id in ids:
        _node(engine, paper_id)
    _edge(engine, ids[2], ids[3])
    _edge(engine, ids[0], ids[1])
    _edge(engine, ids[1], ids[2])
    pairs = [(e["source"], e["target"]) for e in _graph(client)["edges"]]  # type: ignore[index,union-attr]
    assert pairs == sorted(pairs)


def test_an_edge_is_returned_once_not_twice(client: TestClient, engine: Engine) -> None:
    """Both endpoints are in the requested set; that must not duplicate it."""
    a = _paper(engine, "a", "A")
    b = _paper(engine, "b", "B")
    _node(engine, a)
    _node(engine, b)
    _edge(engine, a, b)
    assert len(_graph(client)["edges"]) == 1  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Degrees
# --------------------------------------------------------------------------


def test_degrees_count_only_edges_inside_the_graph(client: TestClient, engine: Engine) -> None:
    """
    A hub with 190k global citations may have in_degree 1 here. The picture
    shows the graph, so the degree has to describe the graph.
    """
    hub = _paper(engine, "hub", "Hub", citations=190_000)
    citer = _paper(engine, "citer", "Citer")
    outsider = _paper(engine, "outsider", "Not In Graph")
    _node(engine, hub, "SEED", depth=0)
    _node(engine, citer)
    _edge(engine, citer, hub)
    _edge(engine, outsider, hub)

    node = next(n for n in _graph(client)["nodes"] if n["id"] == hub)  # type: ignore[index,union-attr]
    assert node["in_degree"] == 1
    assert node["citation_count"] == 190_000


def test_out_degree_counts_what_a_node_cites(client: TestClient, engine: Engine) -> None:
    citing = _paper(engine, "citing", "Citing")
    _node(engine, citing)
    for i in range(3):
        cited = _paper(engine, f"c{i}", f"Cited {i}")
        _node(engine, cited)
        _edge(engine, citing, cited)
    node = next(n for n in _graph(client)["nodes"] if n["id"] == citing)  # type: ignore[index,union-attr]
    assert node["out_degree"] == 3
    assert node["in_degree"] == 0


def test_an_isolated_node_has_zero_degrees(client: TestClient, engine: Engine) -> None:
    _node(engine, _paper(engine, "lonely", "Lonely"))
    node = _graph(client)["nodes"][0]  # type: ignore[index]
    assert node["in_degree"] == 0
    assert node["out_degree"] == 0


# --------------------------------------------------------------------------
# The states filter
# --------------------------------------------------------------------------


@pytest.fixture
def mixed_states(engine: Engine) -> None:
    for i, state in enumerate(("SEED", "CANDIDATE", "LIKED", "DISLIKED")):
        _node(engine, _paper(engine, f"p{i}", f"Paper {i}"), state)


def test_all_states_are_returned_by_default(client: TestClient, mixed_states: None) -> None:
    assert len(_graph(client)["nodes"]) == 4  # type: ignore[arg-type]


def test_the_states_filter_narrows_the_response(client: TestClient, mixed_states: None) -> None:
    body = _graph(client, states="SEED,LIKED")
    assert {n["state"] for n in body["nodes"]} == {"SEED", "LIKED"}  # type: ignore[index,union-attr]


def test_a_single_state_filters_correctly(client: TestClient, mixed_states: None) -> None:
    body = _graph(client, states="DISLIKED")
    assert len(body["nodes"]) == 1  # type: ignore[arg-type]


def test_an_unknown_state_is_rejected(client: TestClient) -> None:
    """A typo'd state silently returning an empty graph reads as data loss."""
    assert client.get(f"/api/sessions/{SID}/graph", params={"states": "SEDE"}).status_code == 422


def test_filtering_also_drops_edges_to_excluded_nodes(client: TestClient, engine: Engine) -> None:
    """
    The both-endpoints invariant has to survive the filter, not just the
    session scope -- otherwise `states=SEED` returns edges into nodes it did
    not return.
    """
    seed = _paper(engine, "seed", "Seed")
    candidate = _paper(engine, "cand", "Candidate")
    _node(engine, seed, "SEED", depth=0)
    _node(engine, candidate, "CANDIDATE")
    _edge(engine, candidate, seed)

    body = _graph(client, states="SEED")
    assert len(body["nodes"]) == 1  # type: ignore[arg-type]
    assert body["edges"] == []


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------


def test_the_counts_match_the_collections(
    client: TestClient, two_node_graph: tuple[int, int]
) -> None:
    body = _graph(client)
    assert body["meta"]["node_count"] == len(body["nodes"])  # type: ignore[index,arg-type]
    assert body["meta"]["edge_count"] == len(body["edges"])  # type: ignore[index,arg-type]


def test_the_config_version_is_reported(client: TestClient) -> None:
    """Which filters produced this graph is the first question when it looks odd."""
    from app.config import filters

    assert _graph(client)["meta"]["config_version"] == filters.config_version  # type: ignore[index]


def test_crawl_completeness_is_one_when_no_node_is_a_stub(
    client: TestClient, two_node_graph: tuple[int, int]
) -> None:
    assert _graph(client)["meta"]["crawl_completeness"] == 1.0  # type: ignore[index]


def test_crawl_completeness_counts_stubs(client: TestClient, engine: Engine) -> None:
    """
    PLAN.md section I: |non-stub| / |nodes|. R3 suppresses the PageRank column
    below 60%, so this figure has to be honest about a partial crawl.
    """
    _node(engine, _paper(engine, "full", "Full", crawl_state="METADATA"))
    _node(engine, _paper(engine, "stub1", "Stub 1", crawl_state="STUB"))
    _node(engine, _paper(engine, "stub2", "Stub 2", crawl_state="STUB"))
    _node(engine, _paper(engine, "stub3", "Stub 3", crawl_state="STUB"))
    assert _graph(client)["meta"]["crawl_completeness"] == 0.25  # type: ignore[index]


def test_crawl_completeness_of_an_empty_graph_is_one(client: TestClient) -> None:
    """
    Zero over zero. Reporting 0.0 would read as "completely uncrawled" and
    would suppress a PageRank column that has nothing to suppress.
    """
    assert _graph(client)["meta"]["crawl_completeness"] == 1.0  # type: ignore[index]


# --------------------------------------------------------------------------
# Size -- BUILD.md's verification
# --------------------------------------------------------------------------


def test_a_twenty_one_node_graph_is_under_fifty_kilobytes(
    client: TestClient, engine: Engine
) -> None:
    """
    BUILD.md's stated check. This is the evidence for not paginating: if a
    realistic graph fits in one small response, pagination buys complexity and
    nothing else.
    """
    ids = []
    for i in range(21):
        paper_id = _paper(
            engine,
            f"p{i}",
            f"A Realistically Long Paper Title About Something Specific, Number {i}",
        )
        _node(engine, paper_id)
        ids.append(paper_id)
    for i in range(20):
        _edge(engine, ids[i], ids[i + 1])

    body = client.get(f"/api/sessions/{SID}/graph").content
    assert len(body) < 50_000, f"{len(body)} bytes"


def test_the_response_parses_as_json(client: TestClient, two_node_graph: tuple[int, int]) -> None:
    raw = client.get(f"/api/sessions/{SID}/graph").content
    assert isinstance(json.loads(raw), dict)


# --------------------------------------------------------------------------
# The schema the frontend generates from
# --------------------------------------------------------------------------


def test_the_route_is_in_the_openapi_schema(client: TestClient) -> None:
    assert "/api/sessions/{sid}/graph" in client.get("/openapi.json").json()["paths"]


def test_the_response_is_typed(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    ok = schema["paths"]["/api/sessions/{sid}/graph"]["get"]["responses"]["200"]
    assert ok["content"]["application/json"]["schema"] != {}
