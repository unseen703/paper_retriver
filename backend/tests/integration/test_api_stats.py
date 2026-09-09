"""
R2.13 -- `GET /api/sessions/{sid}/stats`.

Journey:

    As someone curating a graph, I want to know what is actually in it, so
    "this looks about right" can be replaced by a number I can check.

BUILD.md's verification is the flat one: **counts match the DB**. That is why
this is a server endpoint rather than a component adding up what the canvas
happens to have loaded. `GET /graph` can be filtered by state, and a panel
computing its totals from a filtered response would confidently report a
subset as the whole -- the failure being that it looks completely fine.

Components is a graph algorithm, so it goes through NetworkX (CLAUDE.md's
locked decision), over the **undirected** projection -- the same projection the
sweep marks over. Two papers that both cite a third are connected in any sense
a reader means by "connected", and a directed reading would report a graph made
entirely of singletons.

Two definitions worth stating, because both have a plausible wrong version:

**Average degree counts each edge twice**, once at each endpoint -- that is what
degree means, and `2E/N` is the standard figure. The tempting `E/N` is the
average *out*-degree of a directed graph, which is a different quantity that
happens to look similar on paper.

**Density is measured against the undirected maximum**, `2E / (N(N-1))`, for the
same reason components are: the graph is being described as a network, not as a
citation direction.
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
    db = tmp_path / "stats.db"
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


class Graph:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.ids: dict[str, int] = {}

    def node(self, name: str, state: str = "CANDIDATE", *, crawled: bool = True) -> Graph:
        """`crawled=False` stores a STUB, which is what drags completeness down."""
        with self.engine.begin() as conn:
            paper_id = conn.execute(
                text(
                    "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at,"
                    " crawl_state) VALUES (:s, :t, :t, '2026-01-01', :c) RETURNING id"
                ),
                {"s": name, "t": f"Paper {name}", "c": "METADATA" if crawled else "STUB"},
            ).scalar()
            assert paper_id is not None
            conn.execute(
                text(
                    "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                    " VALUES (:sid, :pid, :st, 1)"
                ),
                {"sid": SID, "pid": paper_id, "st": state},
            )
        self.ids[name] = int(paper_id)
        return self

    def edge(self, citing: str, cited: str) -> Graph:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                    " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
                ),
                {"a": self.ids[citing], "b": self.ids[cited]},
            )
        return self


def _stats(client: TestClient) -> dict[str, object]:
    response = client.get(f"/api/sessions/{SID}/stats")
    assert response.status_code == 200, response.text
    return dict(response.json())


# --------------------------------------------------------------------------
# BUILD.md's verification: counts match the DB
# --------------------------------------------------------------------------


def test_the_counts_match_the_database(client: TestClient, engine: Engine) -> None:
    graph = Graph(engine).node("S", "SEED").node("A").node("B").node("C")
    graph.edge("A", "S").edge("B", "S").edge("C", "B")

    body = _stats(client)
    with engine.connect() as conn:
        nodes = conn.execute(
            text("SELECT COUNT(*) FROM graph_nodes WHERE session_id=:s"), {"s": SID}
        ).scalar()
    assert body["node_count"] == nodes == 4
    assert body["edge_count"] == 3


def test_per_state_counts_are_broken_out(client: TestClient, engine: Engine) -> None:
    Graph(engine).node("S", "SEED").node("L", "LIKED").node("D", "DISLIKED").node("C").node("C2")
    assert _stats(client)["by_state"] == {
        "SEED": 1,
        "LIKED": 1,
        "DISLIKED": 1,
        "CANDIDATE": 2,
    }


def test_a_state_with_no_nodes_is_zero_rather_than_missing(
    client: TestClient, engine: Engine
) -> None:
    """
    Present-and-zero, so the panel renders every row from the shape rather than
    from a special case -- and so "no papers disliked yet" is distinguishable
    from "this build forgot to count them".
    """
    Graph(engine).node("S", "SEED")
    by_state = _stats(client)["by_state"]
    assert by_state == {"SEED": 1, "LIKED": 0, "DISLIKED": 0, "CANDIDATE": 0}  # type: ignore[comparison-overlap]


def test_an_empty_graph_reports_zeroes_rather_than_failing(client: TestClient) -> None:
    """
    A brand-new session opens the panel too. Dividing by a node count of zero
    is the obvious way for this endpoint to 500 on day one.
    """
    body = _stats(client)
    assert body["node_count"] == 0
    assert body["edge_count"] == 0
    assert body["components"] == 0
    assert body["avg_degree"] == 0.0
    assert body["density"] == 0.0


# --------------------------------------------------------------------------
# The graph measures
# --------------------------------------------------------------------------


def test_components_counts_connected_pieces(client: TestClient, engine: Engine) -> None:
    graph = Graph(engine).node("A").node("B").node("C").node("D")
    graph.edge("A", "B")  # {A,B}, then {C}, {D}
    assert _stats(client)["components"] == 3


def test_components_are_undirected(client: TestClient, engine: Engine) -> None:
    """
    Two papers that both cite a third are connected in any sense a reader means
    by the word. Counting components over directed edges reports a graph of
    singletons, which is never the answer anyone wants from this panel.
    """
    graph = Graph(engine).node("S", "SEED").node("A").node("B")
    graph.edge("A", "S").edge("B", "S")
    assert _stats(client)["components"] == 1


def test_an_isolated_node_is_its_own_component(client: TestClient, engine: Engine) -> None:
    Graph(engine).node("alone")
    assert _stats(client)["components"] == 1


def test_average_degree_counts_both_endpoints(client: TestClient, engine: Engine) -> None:
    """
    2E/N, the standard figure. The tempting E/N is the average *out*-degree of a
    directed graph -- a different quantity that looks similar enough on paper to
    survive review.
    """
    graph = Graph(engine).node("A").node("B").node("C").node("D")
    graph.edge("A", "B").edge("B", "C")
    # 2 edges, 4 nodes -> 2*2/4 = 1.0
    assert _stats(client)["avg_degree"] == 1.0


def test_density_is_measured_against_the_undirected_maximum(
    client: TestClient, engine: Engine
) -> None:
    """2E / (N(N-1)). A triangle over 3 nodes is complete, so exactly 1.0."""
    graph = Graph(engine).node("A").node("B").node("C")
    graph.edge("A", "B").edge("B", "C").edge("C", "A")
    assert _stats(client)["density"] == 1.0


def test_density_of_a_single_node_is_zero_not_a_division_by_zero(
    client: TestClient, engine: Engine
) -> None:
    """N(N-1) is 0 when N is 1. The honest answer is 0.0, not a 500."""
    Graph(engine).node("only")
    assert _stats(client)["density"] == 0.0


def test_edges_to_papers_outside_the_graph_are_not_counted(
    client: TestClient, engine: Engine
) -> None:
    """
    An edge into a boundary paper is real and structurally useful, but it is not
    an edge of *this graph* -- the panel sits next to a picture that does not
    draw it, and a number that disagrees with the picture beside it is worse
    than no number.
    """
    graph = Graph(engine).node("A").node("B")
    graph.edge("A", "B")
    with engine.begin() as conn:
        boundary = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES ('old', 'Boundary', 'boundary', '2026-01-01') RETURNING id"
            )
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
            ),
            {"a": graph.ids["A"], "b": boundary},
        )
    assert _stats(client)["edge_count"] == 1


# --------------------------------------------------------------------------
# Crawl completeness
# --------------------------------------------------------------------------


def test_crawl_completeness_is_the_fraction_fully_fetched(
    client: TestClient, engine: Engine
) -> None:
    """
    PLAN.md section I: PageRank over a partially crawled graph is biased, so R3
    suppresses that column below 0.6. The panel is where you find out.
    """
    Graph(engine).node("a", crawled=True).node("b", crawled=True).node("c", crawled=False).node(
        "d", crawled=False
    )
    assert _stats(client)["crawl_completeness"] == 0.5


def test_crawl_completeness_of_an_empty_graph_is_zero(client: TestClient) -> None:
    assert _stats(client)["crawl_completeness"] == 0.0


# --------------------------------------------------------------------------
# Scoping and contract
# --------------------------------------------------------------------------


def test_another_sessions_nodes_are_not_counted(client: TestClient, engine: Engine) -> None:
    Graph(engine).node("mine", "SEED")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
        other = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES ('x', 'X', 'x', '2026-01-01') RETURNING id"
            )
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (2, :p, 'CANDIDATE', 1)"
            ),
            {"p": other},
        )
    assert _stats(client)["node_count"] == 1


def test_a_removed_node_stops_being_counted(client: TestClient, engine: Engine) -> None:
    """The panel describes the graph, and a removed paper is not in it."""
    graph = Graph(engine).node("S", "SEED").node("B")
    graph.edge("B", "S")
    assert _stats(client)["node_count"] == 2
    client.delete(f"/api/sessions/{SID}/nodes/{graph.ids['B']}", params={"dry_run": "false"})
    assert _stats(client)["node_count"] == 1


def test_an_unknown_session_is_a_404(client: TestClient) -> None:
    assert client.get("/api/sessions/999/stats").status_code == 404


def test_the_route_is_in_the_openapi_schema(client: TestClient) -> None:
    assert "/api/sessions/{sid}/stats" in client.get("/openapi.json").json()["paths"]


def test_pagerank_is_absent_until_r3(client: TestClient, engine: Engine) -> None:
    """
    BUILD.md: "No PageRank yet (R3)." Absent rather than present-and-null: a
    field that exists and is always null invites a panel to render an empty row
    for a metric nobody has computed.
    """
    Graph(engine).node("a")
    assert "pagerank" not in _stats(client)
