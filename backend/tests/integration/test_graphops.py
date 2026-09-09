"""
R3 -- `services/graphops.py`: the graph as a graph.

Journey:

    As someone reading a ranked list, I want the structural signals behind it
    to mean what they claim, so a number that is an artefact of where I stopped
    crawling does not get presented to me as importance.

BUILD.md asks for a `nx.DiGraph` built from SQL, PageRank and reverse PageRank,
and in/out degree. PLAN.md section I then spends a page explaining why naive
PageRank over this graph is *wrong*, and that analysis is the part with teeth:

    "Your graph is a crawl, not a corpus. Frontier nodes have artificially
    truncated degree: a node whose citations you never fetched looks
    unimportant purely because you stopped fetching. Naive PageRank therefore
    systematically over-ranks well-crawled central nodes and under-ranks the
    frontier -- which is exactly backwards for discovery."

So the tests here come in two halves. The first checks the arithmetic against
answers that can be worked out by hand. The second checks that the arithmetic
is refused when it would be meaningless, which is the half that actually
protects a user.

**Direction matters and both directions are useful.** PageRank over
`citing -> cited` measures being *cited by* important papers -- influence.
Reversed, it measures *citing* many important papers, which is what a survey
does. PLAN.md calls that hub-ness, and it is a different question rather than
a worse answer to the same one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from app.db import make_engine
from app.services.graphops import (
    COMPLETENESS_FLOOR,
    build_digraph,
    compute_signals,
)

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SID = 1


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "graphops.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


class Graph:
    """Builder. `crawled` defaults true -- most tests are not about crawl bias."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.ids: dict[str, int] = {}

    def node(self, name: str, *, crawled: bool = True, state: str = "CANDIDATE") -> Graph:
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
                    " VALUES (:sid, :p, :st, 1)"
                ),
                {"sid": SID, "p": paper_id, "st": state},
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

    def signals(self) -> object:
        with self.engine.connect() as conn:
            return compute_signals(conn, SID)


# --------------------------------------------------------------------------
# Building the graph
# --------------------------------------------------------------------------


def test_the_digraph_has_a_node_per_graph_node(engine: Engine) -> None:
    Graph(engine).node("a").node("b").node("c")
    with engine.connect() as conn:
        assert build_digraph(conn, SID).number_of_nodes() == 3


def test_edges_run_from_citing_to_cited(engine: Engine) -> None:
    """
    CLAUDE.md: "Edges are a single directed CITES relation, citing -> cited."
    Reversing this inverts every ranking, and inverts it plausibly -- the
    numbers still look like numbers.
    """
    graph = Graph(engine).node("citing").node("cited")
    graph.edge("citing", "cited")
    with engine.connect() as conn:
        digraph = build_digraph(conn, SID)
    assert digraph.has_edge(graph.ids["citing"], graph.ids["cited"])
    assert not digraph.has_edge(graph.ids["cited"], graph.ids["citing"])


def test_edges_to_papers_outside_the_graph_are_excluded(engine: Engine) -> None:
    """
    A boundary paper is in the corpus with its edges and deliberately not in the
    graph. Including it here would give it a PageRank score for a node nothing
    draws.
    """
    graph = Graph(engine).node("a")
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
            {"a": graph.ids["a"], "b": boundary},
        )
    with engine.connect() as conn:
        assert build_digraph(conn, SID).number_of_nodes() == 1


def test_an_empty_graph_builds_without_failing(engine: Engine) -> None:
    with engine.connect() as conn:
        assert build_digraph(conn, SID).number_of_nodes() == 0


# --------------------------------------------------------------------------
# Degrees
# --------------------------------------------------------------------------


def test_in_degree_counts_citations_received(engine: Engine) -> None:
    graph = Graph(engine).node("hub").node("a").node("b")
    graph.edge("a", "hub").edge("b", "hub")
    signals = graph.signals()
    assert signals.in_degree[graph.ids["hub"]] == 2  # type: ignore[attr-defined]
    assert signals.in_degree[graph.ids["a"]] == 0  # type: ignore[attr-defined]


def test_out_degree_counts_references_made(engine: Engine) -> None:
    graph = Graph(engine).node("survey").node("a").node("b")
    graph.edge("survey", "a").edge("survey", "b")
    assert graph.signals().out_degree[graph.ids["survey"]] == 2  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# PageRank, against answers you can work out by hand
# --------------------------------------------------------------------------


def test_a_two_cycle_splits_rank_evenly(engine: Engine) -> None:
    """
    A <-> B is symmetric, so any correct PageRank gives both exactly 0.5 --
    independently of the damping factor. The cheapest possible analytic check,
    and it catches a reversed edge direction immediately.
    """
    graph = Graph(engine).node("a").node("b")
    graph.edge("a", "b").edge("b", "a")
    ranks = graph.signals().pagerank  # type: ignore[attr-defined]
    assert ranks[graph.ids["a"]] == pytest.approx(0.5, abs=1e-6)
    assert ranks[graph.ids["b"]] == pytest.approx(0.5, abs=1e-6)


def test_a_three_cycle_splits_rank_evenly(engine: Engine) -> None:
    graph = Graph(engine).node("a").node("b").node("c")
    graph.edge("a", "b").edge("b", "c").edge("c", "a")
    ranks = graph.signals().pagerank  # type: ignore[attr-defined]
    for name in ("a", "b", "c"):
        assert ranks[graph.ids[name]] == pytest.approx(1 / 3, abs=1e-6)


def test_a_star_gives_the_hub_the_analytic_value(engine: Engine) -> None:
    """
    Three leaves all citing one hub, no other edges.

    The hub is a **dangling node** -- it cites nothing -- and that is the whole
    interest of this case. PageRank redistributes a dangling node's mass evenly
    across every node, which is not a detail one can skip on a citation graph:
    every paper whose references have not been fetched is dangling, so on a
    partially crawled graph most of them are.

    With d = 0.85 and n = 4, writing h for the hub and l for a leaf:

        l = (1-d)/n + d*h/n              a leaf is cited by nobody
        h = (1-d)/n + d*h/n + 3*d*l      the hub is cited by all three

    Substituting gives h = 0.133125 / 0.245625, and l follows. Derived by hand
    -- an earlier version of this test assumed the dangling mass simply
    vanished, which is the mistake this case exists to catch.
    """
    graph = Graph(engine).node("hub").node("a").node("b").node("c")
    graph.edge("a", "hub").edge("b", "hub").edge("c", "hub")
    ranks = graph.signals().pagerank  # type: ignore[attr-defined]

    d, n = 0.85, 4
    teleport = (1 - d) / n
    hub = (teleport + 3 * d * teleport) / (1 - d / n - 3 * d * d / n)
    leaf = teleport + d * hub / n

    assert hub + 3 * leaf == pytest.approx(1.0, abs=1e-9), "the derivation itself is consistent"
    assert ranks[graph.ids["hub"]] == pytest.approx(hub, abs=1e-6)
    assert ranks[graph.ids["a"]] == pytest.approx(leaf, abs=1e-6)


def test_ranks_sum_to_one(engine: Engine) -> None:
    graph = Graph(engine).node("a").node("b").node("c")
    graph.edge("a", "b").edge("b", "c")
    assert sum(graph.signals().pagerank.values()) == pytest.approx(1.0, abs=1e-6)  # type: ignore[attr-defined]


def test_reverse_pagerank_finds_the_survey_not_the_classic(engine: Engine) -> None:
    """
    PLAN.md: reverse PageRank is hub-ness, which approximates survey-ness. The
    paper citing everything is not the important one -- it is the one that
    tells you what the important ones are, and that is a different question.
    """
    graph = Graph(engine).node("survey").node("a").node("b").node("c")
    graph.edge("survey", "a").edge("survey", "b").edge("survey", "c")
    signals = graph.signals()

    survey = graph.ids["survey"]
    # Forward: the survey cites everything and is cited by nothing, so it is
    # the *least* important paper in the graph.
    assert signals.pagerank[survey] == min(signals.pagerank.values())  # type: ignore[attr-defined]
    # Reversed: it is the most hub-like, by the same structure.
    assert signals.reverse_pagerank[survey] == max(signals.reverse_pagerank.values())  # type: ignore[attr-defined]


def test_the_same_graph_scores_identically_twice(engine: Engine) -> None:
    """
    CLAUDE.md rule 7. Node insertion order changes floating-point accumulation
    in the power iteration, so an unsorted build makes two runs over one graph
    disagree in the last digits -- and a ranking that reshuffles when nothing
    changed is unusable and unreproducible.
    """
    graph = Graph(engine).node("a").node("b").node("c").node("d")
    graph.edge("a", "b").edge("b", "c").edge("c", "d").edge("d", "a").edge("a", "c")
    first = graph.signals().pagerank  # type: ignore[attr-defined]
    second = graph.signals().pagerank  # type: ignore[attr-defined]
    assert first == second


# --------------------------------------------------------------------------
# Crawl bias -- the half that protects the user
# --------------------------------------------------------------------------


def test_pagerank_runs_only_over_the_crawled_subgraph(engine: Engine) -> None:
    """
    PLAN.md mitigation 1. A stub's citations were never fetched, so its
    truncated degree is a fact about the crawl rather than about the paper.
    Including it makes the crawl look like the literature.
    """
    graph = Graph(engine).node("a").node("b").node("stub", crawled=False)
    graph.edge("a", "b").edge("stub", "a")
    ranks = graph.signals().pagerank  # type: ignore[attr-defined]
    assert graph.ids["stub"] not in ranks
    assert graph.ids["a"] in ranks


def test_pagerank_is_suppressed_below_the_completeness_floor(engine: Engine) -> None:
    """
    PLAN.md mitigation 2: "suppress the PageRank column below 60%". Suppressed
    means absent, not zero -- a zero is a score, and it would rank every paper
    last rather than saying the number is unavailable.
    """
    graph = Graph(engine).node("a")
    for i in range(4):
        graph.node(f"stub{i}", crawled=False)
    signals = graph.signals()

    assert signals.crawl_completeness < COMPLETENESS_FLOOR  # type: ignore[attr-defined]
    assert signals.pagerank == {}, "suppressed, not zeroed"  # type: ignore[attr-defined]
    assert signals.pagerank_suppressed is True  # type: ignore[attr-defined]


def test_degrees_survive_suppression(engine: Engine) -> None:
    """
    PLAN.md mitigation 3: prefer local measures, which depend only on
    neighbourhoods actually fetched. Degree is one, so suppressing PageRank must
    not take the rest of the signals with it.
    """
    graph = Graph(engine).node("a").node("b")
    for i in range(4):
        graph.node(f"stub{i}", crawled=False)
    graph.edge("a", "b")
    signals = graph.signals()

    assert signals.pagerank_suppressed is True  # type: ignore[attr-defined]
    assert signals.in_degree[graph.ids["b"]] == 1  # type: ignore[attr-defined]


def test_a_well_crawled_graph_is_not_suppressed(engine: Engine) -> None:
    """The guard against over-suppressing: the feature has to work sometimes."""
    graph = Graph(engine).node("a").node("b").node("c")
    graph.edge("a", "b")
    signals = graph.signals()
    assert signals.pagerank_suppressed is False  # type: ignore[attr-defined]
    assert signals.pagerank != {}  # type: ignore[attr-defined]


def test_an_empty_graph_reports_no_signals_rather_than_failing(engine: Engine) -> None:
    with engine.connect() as conn:
        signals = compute_signals(conn, SID)
    assert signals.pagerank == {}
    assert signals.in_degree == {}
    assert signals.crawl_completeness == 0.0


def test_undirected_pagerank_is_offered_as_a_bias_diagnostic(engine: Engine) -> None:
    """
    PLAN.md mitigation 4: "Run PageRank on the undirected projection too and
    compare -- divergence between the two is a useful crawl-bias diagnostic."

    On a symmetric graph the two agree; the value is in the cases where they do
    not, and you cannot notice that without computing both.
    """
    graph = Graph(engine).node("a").node("b")
    graph.edge("a", "b").edge("b", "a")
    signals = graph.signals()
    assert signals.undirected_pagerank[graph.ids["a"]] == pytest.approx(0.5, abs=1e-6)  # type: ignore[attr-defined]
