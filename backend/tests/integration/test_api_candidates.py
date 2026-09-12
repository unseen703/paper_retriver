"""
R3.a -- `GET /api/sessions/{sid}/candidates`.

Journey:

    As someone deciding what to read next, I want the candidates as a ranked
    list, because a force-directed graph is good for seeing structure and bad
    for reading an ordered set of choices.

PLAN.md is blunt about this one:

    "`<CandidateList>` is the product. A force-directed graph is excellent for
    understanding *structure* and terrible for reading a ranked list. Users will
    make most decisions from the table."

The endpoint is specified in PLAN.md section G and was never built, which is why
`<CandidateList>` could not be. `GET /graph` is the wrong thing to build it on:
that endpoint answers "what should the canvas draw", returns edges and positions
nobody reading a table wants, and is deliberately filterable by state in a way
that would make a ranked list quietly show a subset.

**Three orderings that all have a plausible wrong version.**

*Ties.* Two papers with the same score must come back in the same order every
time. Leaving it to SQLite means the order can change between runs for no
visible reason, and CLAUDE.md rule 7 exists because a ranking that drifts makes
every bug in it unfalsifiable.

*Unscored nodes.* A node that no feature pass has reached has `score = NULL`.
Sorting NULL as zero would place it above anything with a negative score, which
is a claim the data does not support; sorting it first -- SQLite's own default
for `ORDER BY ... DESC` -- would put the least-known papers at the top of the
recommendation list. They go last.

*`total` is the whole set, not the page.* A table that says "50 of 50" when
there are 213 candidates is lying in the direction that stops you looking.
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
    db = tmp_path / "candidates.db"
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

    def node(
        self,
        name: str,
        *,
        state: str = "CANDIDATE",
        score: float | None = None,
        year: int = 2020,
        citations: int = 10,
        venue: str | None = "NeurIPS",
        session_id: int = SID,
        breakdown: str | None = None,
    ) -> Graph:
        with self.engine.begin() as conn:
            paper_id = conn.execute(
                text(
                    "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at,"
                    " year, citation_count, venue, crawl_state)"
                    " VALUES (:s, :t, :t, '2026-01-01', :y, :c, :v, 'METADATA') RETURNING id"
                ),
                {"s": name, "t": f"Paper {name}", "y": year, "c": citations, "v": venue},
            ).scalar()
            assert paper_id is not None
            if session_id != SID:
                conn.execute(
                    text(
                        "INSERT OR IGNORE INTO sessions (id, name, created_at)"
                        " VALUES (:sid, 'other', '2026-01-01')"
                    ),
                    {"sid": session_id},
                )
            conn.execute(
                text(
                    "INSERT INTO graph_nodes (session_id, paper_id, state, depth, score,"
                    " score_breakdown) VALUES (:sid, :p, :st, 1, :sc, :b)"
                ),
                {"sid": session_id, "p": paper_id, "st": state, "sc": score, "b": breakdown},
            )
        self.ids[name] = int(paper_id)
        return self

    def orphan(self, name: str) -> Graph:
        """
        A graph node whose paper row is gone.

        **The schema forbids this.** `graph_nodes.paper_id` is a foreign key, so
        enforcement has to be switched off to build the state at all -- and the
        pragma has to run outside a transaction, because SQLite ignores it
        inside one.

        That is worth knowing rather than working around: the endpoint's
        `paper is None` branch is insurance against a corrupted file or a
        connection that never enabled enforcement, not against anything the
        application can reach. The test exists so the branch is not merely
        assumed to work.
        """
        self.node(name)
        raw = self.engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.execute("DELETE FROM papers WHERE id = ?", (self.ids[name],))
            cursor.close()
            raw.commit()
        finally:
            raw.close()
        return self


def _get(client: TestClient, **params: object) -> dict:
    response = client.get(f"/api/sessions/{SID}/candidates", params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _titles(payload: dict) -> list[str]:
    return [row["title"] for row in payload["candidates"]]


# --------------------------------------------------------------------------
# What comes back
# --------------------------------------------------------------------------


def test_only_candidates_are_listed(client: TestClient, engine: Engine) -> None:
    """
    The graph holds seeds and labelled papers too. A recommendation list that
    included them would be recommending what you have already decided about.
    """
    graph = Graph(engine)
    graph.node("cand", state="CANDIDATE", score=0.5)
    graph.node("seed", state="SEED", score=0.9)
    graph.node("liked", state="LIKED", score=0.8)
    graph.node("disliked", state="DISLIKED", score=0.7)

    assert _titles(_get(client)) == ["Paper cand"]


def test_the_list_is_ordered_by_score_descending(client: TestClient, engine: Engine) -> None:
    graph = Graph(engine)
    graph.node("low", score=0.1).node("high", score=0.9).node("mid", score=0.5)

    assert _titles(_get(client)) == ["Paper high", "Paper mid", "Paper low"]


def test_the_score_breakdown_comes_with_it(client: TestClient, engine: Engine) -> None:
    """
    `<ScoreBreakdown>` renders beside each row. Making the table fetch
    `/nodes/{id}` per row to get it would be a request per visible paper.
    """
    Graph(engine).node("a", score=0.5, breakdown='{"overlap": 0.4, "hub": -0.1}')

    row = _get(client)["candidates"][0]
    assert row["score_breakdown"] == {"overlap": 0.4, "hub": -0.1}


def test_the_columns_a_reader_decides_from_are_present(client: TestClient, engine: Engine) -> None:
    Graph(engine).node("a", score=0.5, year=2021, citations=42, venue="ICML")

    row = _get(client)["candidates"][0]
    assert row["year"] == 2021
    assert row["citation_count"] == 42
    assert row["venue"] == "ICML"
    assert row["score"] == 0.5


# --------------------------------------------------------------------------
# Ordering -- the three cases with a plausible wrong version
# --------------------------------------------------------------------------


def test_ties_are_broken_deterministically(client: TestClient, engine: Engine) -> None:
    """
    CLAUDE.md rule 7. Equal scores must not leave the order to SQLite, or the
    table reshuffles between reloads with nothing to explain it.
    """
    graph = Graph(engine)
    graph.node("b", score=0.5).node("a", score=0.5).node("c", score=0.5)

    first = _titles(_get(client))
    assert first == _titles(_get(client))
    # Ascending paper_id, which is insertion order here: b, a, c.
    assert first == ["Paper b", "Paper a", "Paper c"]


def test_unscored_nodes_sort_last_not_first(client: TestClient, engine: Engine) -> None:
    """
    **SQLite's own default is the wrong answer.** `ORDER BY score DESC` puts
    NULLs first, so the papers nothing has scored would head the recommendation
    list. They are the least-known, not the best.
    """
    graph = Graph(engine)
    graph.node("unscored", score=None).node("scored", score=0.1)

    assert _titles(_get(client)) == ["Paper scored", "Paper unscored"]


def test_an_unscored_node_does_not_outrank_a_negative_score(
    client: TestClient, engine: Engine
) -> None:
    """
    The other half of the same point: treating NULL as 0.0 would place an
    unscored paper above a genuinely poor one, which the data does not support.
    `hub` carries a negative weight, so negative totals are ordinary.
    """
    graph = Graph(engine)
    graph.node("unscored", score=None).node("negative", score=-0.4)

    assert _titles(_get(client)) == ["Paper negative", "Paper unscored"]


def test_unscored_nodes_are_still_deterministic_among_themselves(
    client: TestClient, engine: Engine
) -> None:
    graph = Graph(engine)
    graph.node("z", score=None).node("y", score=None)

    assert _titles(_get(client)) == _titles(_get(client))


# --------------------------------------------------------------------------
# Limit and total
# --------------------------------------------------------------------------


def test_limit_caps_the_rows(client: TestClient, engine: Engine) -> None:
    graph = Graph(engine)
    for i in range(5):
        graph.node(f"p{i}", score=float(i))

    assert len(_get(client, limit=2)["candidates"]) == 2


def test_total_counts_every_candidate_not_just_the_page(client: TestClient, engine: Engine) -> None:
    """
    A table reading "50 of 50" when there are 213 is lying in the direction
    that stops you looking for the rest.
    """
    graph = Graph(engine)
    for i in range(5):
        graph.node(f"p{i}", score=float(i))

    payload = _get(client, limit=2)
    assert payload["total"] == 5
    assert len(payload["candidates"]) == 2


def test_total_ignores_non_candidates(client: TestClient, engine: Engine) -> None:
    graph = Graph(engine)
    graph.node("cand", score=0.5)
    graph.node("seed", state="SEED", score=0.9)

    assert _get(client)["total"] == 1


def test_limit_keeps_the_top_of_the_ranking(client: TestClient, engine: Engine) -> None:
    """Truncation must come after ordering, not before it."""
    graph = Graph(engine)
    graph.node("worst", score=0.1).node("best", score=0.9).node("middle", score=0.5)

    assert _titles(_get(client, limit=1)) == ["Paper best"]


def test_a_limit_below_one_is_rejected(client: TestClient) -> None:
    """
    `limit=0` is a request for nothing, which is never what a caller means.
    Returning an empty list would look like "no candidates".
    """
    assert client.get(f"/api/sessions/{SID}/candidates", params={"limit": 0}).status_code == 422


# --------------------------------------------------------------------------
# Sorting
# --------------------------------------------------------------------------


def test_sorting_by_year_puts_the_newest_first(client: TestClient, engine: Engine) -> None:
    graph = Graph(engine)
    graph.node("old", score=0.9, year=2016).node("new", score=0.1, year=2025)

    assert _titles(_get(client, sort="year")) == ["Paper new", "Paper old"]


def test_sorting_by_citations_puts_the_most_cited_first(client: TestClient, engine: Engine) -> None:
    graph = Graph(engine)
    graph.node("few", score=0.9, citations=3).node("many", score=0.1, citations=900)

    assert _titles(_get(client, sort="citations")) == ["Paper many", "Paper few"]


def test_an_unknown_sort_is_a_422_not_a_silent_default(client: TestClient) -> None:
    """
    Silently falling back to `score` for `sort=scroe` gives a plausible-looking
    list that is not the one that was asked for -- the worst kind of wrong,
    because nothing about it looks wrong.
    """
    response = client.get(f"/api/sessions/{SID}/candidates", params={"sort": "scroe"})
    assert response.status_code == 422
    assert "scroe" in response.text or "sort" in response.text


def test_every_sort_is_deterministic(client: TestClient, engine: Engine) -> None:
    """A tie on year or citations needs the same stable key score does."""
    graph = Graph(engine)
    graph.node("b", score=0.5, year=2020, citations=10)
    graph.node("a", score=0.5, year=2020, citations=10)

    for key in ("score", "year", "citations"):
        assert _titles(_get(client, sort=key)) == _titles(_get(client, sort=key))


# --------------------------------------------------------------------------
# Scoping and edges
# --------------------------------------------------------------------------


def test_another_session_is_invisible(client: TestClient, engine: Engine) -> None:
    """The session_id contract, at the endpoint."""
    graph = Graph(engine)
    graph.node("mine", score=0.5)
    graph.node("theirs", score=0.9, session_id=2)

    assert _titles(_get(client)) == ["Paper mine"]


def test_an_unknown_session_is_a_404(client: TestClient) -> None:
    assert client.get("/api/sessions/999/candidates").status_code == 404


def test_an_empty_session_returns_an_empty_list(client: TestClient) -> None:
    payload = _get(client)
    assert payload["candidates"] == []
    assert payload["total"] == 0


def test_a_node_whose_paper_is_gone_is_skipped_not_fatal(
    client: TestClient, engine: Engine
) -> None:
    """
    A broken foreign key is not a drawable row. `GET /graph` skips them rather
    than raising, on the grounds that the rest is still worth showing; a list
    that 500s because one row is corrupt is strictly worse.
    """
    graph = Graph(engine)
    graph.node("good", score=0.5)
    graph.orphan("broken")

    assert _titles(_get(client)) == ["Paper good"]
