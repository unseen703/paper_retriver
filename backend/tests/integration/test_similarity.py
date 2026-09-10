"""
R3 -- co-citation and bibliographic coupling.

Journey:

    As someone looking for papers like the ones I already like, I want the
    ranking to know *why* two papers are related, so a paper that shares my
    seed's whole reference list outranks one that merely happens to be famous.

PLAN.md calls these the measures that should carry most of the ranking weight:

    "Prefer **local** measures (co-citation, bibliographic coupling, anchor
    overlap) for ranking -- they depend only on 2-hop neighbourhoods you
    actually fetched, so they're far less crawl-sensitive."

Two different relations, and the difference matters:

    bibliographic coupling(A, B)  papers that BOTH A and B cite.
                                  Fixed at publication -- it never changes.
    co-citation(A, B)             papers that cite BOTH A and B.
                                  Grows over time as the field cites them together.

**These are computed over the corpus, not over the drawn graph.** That is the
single most important thing in this file, and it is what BUILD.md's boundary
test protects: two modern papers whose only shared reference is a 2014 paper
are coupled *through* that paper, and the 2014 paper is deliberately not in the
graph. Restricting to `graph_nodes` -- which is right for PageRank -- would
silently zero the best feature in the system.

R1.9 asserted the structural precondition for this and said "bib_coupling
itself arrives at R3". BUILD.md asks for that test to be run again here,
against the real feature. It is the first one below.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from app.db import make_engine
from app.services.similarity import compute_similarity

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SID = 1


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "similarity.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


class Corpus:
    """
    Papers and edges. `drawn=False` makes a boundary paper -- in the corpus
    with its edges, deliberately absent from the graph, exactly as `ingest.py`
    stores a pre-2015 reference.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.ids: dict[str, int] = {}

    def paper(self, name: str, *, drawn: bool = True, year: int = 2020) -> Corpus:
        with self.engine.begin() as conn:
            paper_id = conn.execute(
                text(
                    "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at, year)"
                    " VALUES (:s, :t, :t, '2026-01-01', :y) RETURNING id"
                ),
                {"s": name, "t": f"Paper {name}", "y": year},
            ).scalar()
            assert paper_id is not None
            if drawn:
                conn.execute(
                    text(
                        "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                        " VALUES (:sid, :p, 'CANDIDATE', 1)"
                    ),
                    {"sid": SID, "p": paper_id},
                )
        self.ids[name] = int(paper_id)
        return self

    def cites(self, citing: str, cited: str) -> Corpus:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                    " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
                ),
                {"a": self.ids[citing], "b": self.ids[cited]},
            )
        return self

    def similarity(self, *anchors: str) -> object:
        with self.engine.connect() as conn:
            return compute_similarity(conn, [self.ids[a] for a in anchors])


# --------------------------------------------------------------------------
# BUILD.md's critical test, now against the real feature
# --------------------------------------------------------------------------


def test_two_modern_papers_sharing_a_2014_reference_are_coupled(engine: Engine) -> None:
    """
    BUILD.md, R1.9 and again here: "two modern papers whose only shared
    reference is a 2014 paper must have non-zero bib_coupling. This test
    protects the design; without it the year floor silently degrades your best
    feature."

    R1.9 could only assert the precondition -- that the old paper and both its
    edges survive ingestion. This asserts the thing itself.

    The 2014 paper is a boundary paper: in the corpus, not in the graph.
    Computing coupling over `graph_nodes` instead of over `edges` would return
    zero here, and would look entirely reasonable while doing it.
    """
    corpus = Corpus(engine)
    corpus.paper("anchor").paper("candidate")
    corpus.paper("adam_2014", drawn=False, year=2014)
    corpus.cites("anchor", "adam_2014").cites("candidate", "adam_2014")

    coupling = corpus.similarity("anchor").bib_coupling  # type: ignore[attr-defined]
    assert coupling[corpus.ids["candidate"]] == 1, "the year floor must not zero the coupling"


def test_a_boundary_paper_couples_several_candidates_at_once(engine: Engine) -> None:
    """The 2014 paper is doing real work for every paper that cites it."""
    corpus = Corpus(engine)
    corpus.paper("anchor").paper("a").paper("b").paper("c")
    corpus.paper("old", drawn=False, year=2013)
    for name in ("anchor", "a", "b", "c"):
        corpus.cites(name, "old")

    coupling = corpus.similarity("anchor").bib_coupling  # type: ignore[attr-defined]
    assert all(coupling[corpus.ids[n]] == 1 for n in ("a", "b", "c"))


# --------------------------------------------------------------------------
# Bibliographic coupling
# --------------------------------------------------------------------------


def test_coupling_counts_shared_references(engine: Engine) -> None:
    corpus = Corpus(engine)
    corpus.paper("anchor").paper("close").paper("far")
    corpus.paper("r1").paper("r2").paper("r3")
    corpus.cites("anchor", "r1").cites("anchor", "r2")
    corpus.cites("close", "r1").cites("close", "r2")  # both shared
    corpus.cites("far", "r3")  # nothing shared

    coupling = corpus.similarity("anchor").bib_coupling  # type: ignore[attr-defined]
    assert coupling[corpus.ids["close"]] == 2
    assert corpus.ids["far"] not in coupling


def test_a_paper_is_not_coupled_with_itself(engine: Engine) -> None:
    """
    An anchor shares every reference with itself, so without the guard it would
    top its own ranking -- and it is already in the graph, which is why it is an
    anchor.
    """
    corpus = Corpus(engine)
    corpus.paper("anchor").paper("r1")
    corpus.cites("anchor", "r1")
    assert corpus.ids["anchor"] not in corpus.similarity("anchor").bib_coupling  # type: ignore[attr-defined]


def test_coupling_counts_each_shared_reference_once(engine: Engine) -> None:
    """
    Two anchors both citing the same paper as a candidate does is one shared
    reference, not two. Counting it twice would make the measure reward
    redundancy among anchors rather than similarity to them.
    """
    corpus = Corpus(engine)
    corpus.paper("a1").paper("a2").paper("candidate").paper("shared")
    corpus.cites("a1", "shared").cites("a2", "shared").cites("candidate", "shared")

    coupling = corpus.similarity("a1", "a2").bib_coupling  # type: ignore[attr-defined]
    assert coupling[corpus.ids["candidate"]] == 1


def test_coupling_rises_with_more_shared_references(engine: Engine) -> None:
    """Monotone in the thing it claims to measure."""
    corpus = Corpus(engine)
    corpus.paper("anchor").paper("one").paper("three")
    for i in range(3):
        corpus.paper(f"r{i}")
        corpus.cites("anchor", f"r{i}")
        corpus.cites("three", f"r{i}")
    corpus.cites("one", "r0")

    coupling = corpus.similarity("anchor").bib_coupling  # type: ignore[attr-defined]
    assert coupling[corpus.ids["three"]] > coupling[corpus.ids["one"]]


# --------------------------------------------------------------------------
# Co-citation
# --------------------------------------------------------------------------


def test_co_citation_counts_papers_citing_both(engine: Engine) -> None:
    corpus = Corpus(engine)
    corpus.paper("anchor").paper("together").paper("apart")
    corpus.paper("citer1").paper("citer2")
    corpus.cites("citer1", "anchor").cites("citer1", "together")
    corpus.cites("citer2", "anchor").cites("citer2", "together")
    corpus.cites("citer1", "apart")  # cited alongside, but only once

    co_cited = corpus.similarity("anchor").co_citation  # type: ignore[attr-defined]
    assert co_cited[corpus.ids["together"]] == 2
    assert co_cited[corpus.ids["apart"]] == 1


def test_co_citation_and_coupling_are_different_measures(engine: Engine) -> None:
    """
    The reason both exist. `descendant` cites the anchor's references but is
    never cited alongside it; `peer` is cited alongside it but shares no
    references. Collapsing the two into one "similarity" number would lose the
    distinction between "builds on the same work" and "is discussed in the same
    breath".
    """
    corpus = Corpus(engine)
    corpus.paper("anchor").paper("descendant").paper("peer").paper("ref").paper("citer")
    corpus.cites("anchor", "ref").cites("descendant", "ref")
    corpus.cites("citer", "anchor").cites("citer", "peer")

    signals = corpus.similarity("anchor")
    assert signals.bib_coupling.get(corpus.ids["descendant"]) == 1  # type: ignore[attr-defined]
    assert corpus.ids["descendant"] not in signals.co_citation  # type: ignore[attr-defined]
    assert signals.co_citation.get(corpus.ids["peer"]) == 1  # type: ignore[attr-defined]
    assert corpus.ids["peer"] not in signals.bib_coupling  # type: ignore[attr-defined]


def test_a_paper_is_not_co_cited_with_itself(engine: Engine) -> None:
    corpus = Corpus(engine)
    corpus.paper("anchor").paper("citer")
    corpus.cites("citer", "anchor")
    assert corpus.ids["anchor"] not in corpus.similarity("anchor").co_citation  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Shape and determinism
# --------------------------------------------------------------------------


def test_no_anchors_gives_no_similarity(engine: Engine) -> None:
    """
    A session with no seeds and no likes has nothing to be similar *to*. Empty
    is the honest answer; zeroes would be a ranking over nothing.
    """
    corpus = Corpus(engine)
    corpus.paper("a")
    with engine.connect() as conn:
        signals = compute_similarity(conn, [])
    assert signals.bib_coupling == {}
    assert signals.co_citation == {}


def test_an_anchor_with_no_edges_yields_nothing(engine: Engine) -> None:
    corpus = Corpus(engine)
    corpus.paper("lonely")
    assert corpus.similarity("lonely").bib_coupling == {}  # type: ignore[attr-defined]


def test_the_same_corpus_gives_the_same_answer_twice(engine: Engine) -> None:
    """CLAUDE.md rule 7 -- these numbers feed a ranking."""
    corpus = Corpus(engine)
    corpus.paper("anchor").paper("a").paper("b").paper("r1").paper("r2")
    corpus.cites("anchor", "r1").cites("anchor", "r2")
    corpus.cites("a", "r1").cites("b", "r1").cites("b", "r2")

    assert corpus.similarity("anchor").bib_coupling == corpus.similarity("anchor").bib_coupling  # type: ignore[attr-defined]


def test_papers_with_no_relationship_are_absent_rather_than_zero(engine: Engine) -> None:
    """
    Absent, not zero. A sparse dict is the shape of a sparse relation, and it
    keeps the ranking from carrying a zero for every paper in the corpus.
    """
    corpus = Corpus(engine)
    corpus.paper("anchor").paper("unrelated").paper("r1")
    corpus.cites("anchor", "r1")
    assert corpus.ids["unrelated"] not in corpus.similarity("anchor").bib_coupling  # type: ignore[attr-defined]
