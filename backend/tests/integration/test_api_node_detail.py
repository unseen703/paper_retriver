"""
R1.19 -- `GET /api/sessions/{sid}/nodes/{paper_id}`.

Everything R1.22's `<NodeInspector>` renders: "title, authors, venue, date,
citations, type, categories, in/out degree, depth".

**The route carries `{sid}`, unlike PLAN.md's sketch of `/api/nodes/{id}`.**
The response mixes two kinds of fact and only one of them is global:

    global      title, authors, venue, date, citations, type, categories
    per-session state, depth, score, features, score_breakdown, degrees

PLAN.md's own sketch asks this endpoint for "features + breakdown", which are
`graph_nodes` columns and therefore meaningless without a session -- and its
labelling endpoint is already `PATCH /api/sessions/{sid}/nodes/{id}`. Putting
the read at a bare `/api/nodes/{id}` would leave the read and the write of the
same row on differently-scoped paths. This is the same correction made to
search at R1.15.

`score_breakdown` is empty until R3 ships real features. It is present and
empty rather than absent, so the inspector can render an "unavailable" panel
from the shape rather than from a special case.
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
MISSING_SID = 999


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "detail.db"
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


def _paper(engine: Engine, **overrides: object) -> int:
    fields: dict[str, object] = {
        "s2_paper_id": "p1",
        "title": "A Specific Paper",
        "title_norm": "a specific paper",
        "first_seen_at": "2026-01-01",
        "year": 2021,
        "publication_date": "2021-06-15",
        "venue": "NeurIPS",
        "citation_count": 4200,
        "reference_count": 55,
        "influential_citation_count": 300,
        "doi": "10.1000/abc",
        "arxiv_id": "2106.00001",
        "primary_arxiv_category": "cs.LG",
        "arxiv_categories": '["cs.LG", "cs.CL"]',
        "s2_fields": '["Computer Science"]',
        "publication_types": '["JournalArticle"]',
        "paper_type": "RESEARCH",
        "crawl_state": "METADATA",
        "abstract": "An abstract, stored for embeddings only.",
    }
    fields.update(overrides)
    cols = ", ".join(fields)
    binds = ", ".join(f":{k}" for k in fields)
    with engine.begin() as conn:
        row = conn.execute(
            text(f"INSERT INTO papers ({cols}) VALUES ({binds}) RETURNING id"), fields
        ).fetchone()
    assert row is not None
    return int(row[0])


def _node(
    engine: Engine,
    paper_id: int,
    *,
    state: str = "CANDIDATE",
    depth: int = 1,
    score: float | None = 2.5,
    features: str | None = '{"anchor_overlap": 2}',
    sid: int = SID,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth, score, features)"
                " VALUES (:sid, :pid, :st, :d, :sc, :f)"
            ),
            {"sid": sid, "pid": paper_id, "st": state, "d": depth, "sc": score, "f": features},
        )


def _authors(engine: Engine, paper_id: int, names: list[str]) -> None:
    with engine.begin() as conn:
        for position, name in enumerate(names):
            conn.execute(
                text(
                    "INSERT INTO authors (s2_author_id, name) VALUES (:aid, :n)"
                    " ON CONFLICT (s2_author_id) DO NOTHING"
                ),
                {"aid": f"a{position}", "n": name},
            )
            conn.execute(
                text(
                    "INSERT INTO paper_authors (paper_id, author_id, position)"
                    " SELECT :pid, id, :pos FROM authors WHERE s2_author_id = :aid"
                ),
                {"pid": paper_id, "aid": f"a{position}", "pos": position},
            )


def _edge(engine: Engine, citing: int, cited: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
            ),
            {"a": citing, "b": cited},
        )


def _get(client: TestClient, paper_id: int, sid: int = SID) -> dict[str, object]:
    response = client.get(f"/api/sessions/{sid}/nodes/{paper_id}")
    assert response.status_code == 200, response.text
    return dict(response.json())


@pytest.fixture
def node(engine: Engine) -> int:
    paper_id = _paper(engine)
    _node(engine, paper_id)
    return paper_id


# --------------------------------------------------------------------------
# The inspector's fields
# --------------------------------------------------------------------------


def test_a_node_returns_200(client: TestClient, node: int) -> None:
    assert client.get(f"/api/sessions/{SID}/nodes/{node}").status_code == 200


def test_the_response_carries_everything_the_inspector_renders(
    client: TestClient, node: int
) -> None:
    """BUILD.md R1.22's list, verbatim."""
    body = _get(client, node)
    assert set(body) >= {
        "paper_id",
        "title",
        "authors",
        "venue",
        "publication_date",
        "citation_count",
        "paper_type",
        "arxiv_categories",
        "in_degree",
        "out_degree",
        "depth",
    }


def test_the_node_fields_are_present(client: TestClient, node: int) -> None:
    body = _get(client, node)
    assert set(body) >= {"state", "score", "features", "score_breakdown"}


def test_the_title_and_venue_come_through(client: TestClient, node: int) -> None:
    body = _get(client, node)
    assert body["title"] == "A Specific Paper"
    assert body["venue"] == "NeurIPS"


def test_the_byline_is_returned_in_order(client: TestClient, engine: Engine) -> None:
    """
    Position matters -- first author carries meaning. This is only renderable
    at all because of the R1.15 author-name fix; before it, paper_authors was
    permanently empty.
    """
    paper_id = _paper(engine)
    _node(engine, paper_id)
    _authors(engine, paper_id, ["Ada Lovelace", "Grace Hopper", "Alan Turing"])
    assert _get(client, paper_id)["authors"] == ["Ada Lovelace", "Grace Hopper", "Alan Turing"]


def test_a_paper_with_no_authors_returns_an_empty_list(client: TestClient, node: int) -> None:
    """CLAUDE.md rule 6: a missing field degrades, it never raises."""
    assert _get(client, node)["authors"] == []


def test_the_arxiv_categories_are_a_list(client: TestClient, node: int) -> None:
    assert _get(client, node)["arxiv_categories"] == ["cs.LG", "cs.CL"]


def test_the_primary_category_is_distinguished(client: TestClient, node: int) -> None:
    """
    Cross-listing is why the topic filter exists, so the inspector has to show
    which category is primary rather than an undifferentiated list.
    """
    assert _get(client, node)["primary_arxiv_category"] == "cs.LG"


def test_the_identifiers_are_returned(client: TestClient, node: int) -> None:
    """Enough to open the paper: the inspector links out on these."""
    body = _get(client, node)
    assert body["doi"] == "10.1000/abc"
    assert body["arxiv_id"] == "2106.00001"
    assert body["s2_paper_id"] == "p1"


def test_the_abstract_is_not_returned(client: TestClient, node: int) -> None:
    """
    CLAUDE.md: abstracts are stored for embeddings only. Shipping one per
    inspector click is bytes spent on something the design says is not for
    reading, and there is no LLM stage that would consume it.
    """
    assert "abstract" not in _get(client, node)


# --------------------------------------------------------------------------
# Session-scoped fields
# --------------------------------------------------------------------------


def test_the_depth_is_reported(client: TestClient, node: int) -> None:
    assert _get(client, node)["depth"] == 1


def test_the_state_and_score_are_reported(client: TestClient, node: int) -> None:
    body = _get(client, node)
    assert body["state"] == "CANDIDATE"
    assert body["score"] == 2.5


def test_the_features_are_returned_as_an_object(client: TestClient, node: int) -> None:
    assert _get(client, node)["features"] == {"anchor_overlap": 2}


def test_the_score_breakdown_is_empty_but_present(client: TestClient, node: int) -> None:
    """
    Empty until R3. Present-and-empty rather than absent so the inspector can
    render "not available yet" from the shape instead of a special case.
    """
    assert _get(client, node)["score_breakdown"] == {}


def test_the_same_paper_reports_different_depths_in_different_sessions(
    client: TestClient, engine: Engine
) -> None:
    """The reason this route is session-scoped at all."""
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
    paper_id = _paper(engine)
    _node(engine, paper_id, depth=1, state="CANDIDATE")
    _node(engine, paper_id, depth=0, state="SEED", sid=2)

    assert _get(client, paper_id)["depth"] == 1
    assert _get(client, paper_id, sid=2)["state"] == "SEED"


def test_degrees_count_only_edges_inside_the_graph(client: TestClient, engine: Engine) -> None:
    """Consistent with GET /graph -- the inspector must not contradict the picture."""
    subject = _paper(engine, s2_paper_id="subject", title="Subject", title_norm="subject")
    inside = _paper(engine, s2_paper_id="inside", title="Inside", title_norm="inside")
    outside = _paper(engine, s2_paper_id="outside", title="Outside", title_norm="outside")
    _node(engine, subject)
    _node(engine, inside)
    _edge(engine, inside, subject)
    _edge(engine, outside, subject)

    body = _get(client, subject)
    assert body["in_degree"] == 1
    assert body["citation_count"] == 4200


def test_out_degree_counts_what_the_node_cites(client: TestClient, engine: Engine) -> None:
    citing = _paper(engine, s2_paper_id="citing", title="Citing", title_norm="citing")
    cited = _paper(engine, s2_paper_id="cited", title="Cited", title_norm="cited")
    _node(engine, citing)
    _node(engine, cited)
    _edge(engine, citing, cited)
    assert _get(client, citing)["out_degree"] == 1


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def test_an_unknown_paper_is_a_404(client: TestClient) -> None:
    assert client.get(f"/api/sessions/{SID}/nodes/424242").status_code == 404


def test_a_paper_with_no_node_in_this_session_is_a_404(client: TestClient, engine: Engine) -> None:
    """
    A boundary paper is in the corpus and not in the graph. This route reports
    a *node*, so a paper without one has no depth, state or score to report --
    reporting nulls for those would say "this is in your graph, scoreless"
    rather than "this is not in your graph".
    """
    paper_id = _paper(engine, s2_paper_id="boundary", title="Boundary", title_norm="boundary")
    assert client.get(f"/api/sessions/{SID}/nodes/{paper_id}").status_code == 404


def test_a_node_in_another_session_is_a_404_here(client: TestClient, engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
    paper_id = _paper(engine)
    _node(engine, paper_id, sid=2)
    assert client.get(f"/api/sessions/{SID}/nodes/{paper_id}").status_code == 404


def test_an_unknown_session_is_a_404(client: TestClient, node: int) -> None:
    assert client.get(f"/api/sessions/{MISSING_SID}/nodes/{node}").status_code == 404


# --------------------------------------------------------------------------
# The schema the frontend generates from
# --------------------------------------------------------------------------


def test_the_route_is_in_the_openapi_schema(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/sessions/{sid}/nodes/{paper_id}" in paths


def test_the_response_is_typed(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    ok = schema["paths"]["/api/sessions/{sid}/nodes/{paper_id}"]["get"]["responses"]["200"]
    assert ok["content"]["application/json"]["schema"] != {}
