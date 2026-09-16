"""
R3 -- computing and persisting `node_features`.

Journey:

    As someone reading a ranked list, I want the ranking to actually use the
    structure of my graph, so the order reflects what the papers are rather
    than what order they arrived in.

This is the missing link between everything else in R3. `graphops` measures the
graph, `similarity` measures relatedness, `ranking` turns features into a
score, and `PUT /api/config` rescores from persisted features -- but until
something *writes* those features, the rescore has nothing to read and every
score is null.

**Raw values are normalized before they are stored.** PLAN.md: rank-percentile
within the pool, not z-score. Storing raw citation counts and normalizing at
score time would work, but it would mean the normalization pool is whatever
happens to be loaded when the score is computed -- so a paper's score would
depend on what else was being looked at. Normalizing once, against the whole
session, makes the stored number mean one thing.

**Feature names match `ranking.yaml`'s weights exactly.** A feature the config
has no weight for is dead computation; a weight with no feature is a lever
connected to nothing. Both are silent, so there is a test for the correspondence
rather than a comment asking people to keep them in step.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from app.config import load_ranking
from app.db import make_engine
from app.services.features import FEATURE_NAMES, compute_and_store_features

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SID = 1
AS_OF = 2026


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "features.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


class Graph:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.ids: dict[str, int] = {}

    def node(
        self,
        name: str,
        *,
        state: str = "CANDIDATE",
        year: int = 2020,
        citations: int = 10,
        overlap: int | None = None,
        crawled: bool = True,
        venue: str | None = None,
    ) -> Graph:
        with self.engine.begin() as conn:
            paper_id = conn.execute(
                text(
                    "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at, year,"
                    " citation_count, venue, crawl_state)"
                    " VALUES (:s, :t, :t, '2026-01-01', :y, :c, :v, :cs) RETURNING id"
                ),
                {
                    "s": name,
                    "t": f"Paper {name}",
                    "y": year,
                    "c": citations,
                    "v": venue,
                    "cs": "METADATA" if crawled else "STUB",
                },
            ).scalar()
            assert paper_id is not None
            conn.execute(
                text(
                    "INSERT INTO graph_nodes (session_id, paper_id, state, depth, features)"
                    " VALUES (:sid, :p, :st, 1, :f)"
                ),
                {
                    "sid": SID,
                    "p": paper_id,
                    "st": state,
                    # What an expansion leaves behind: the raw overlap it
                    # counted during pooling, and nothing else.
                    "f": json.dumps({"anchor_overlap": overlap}) if overlap is not None else None,
                },
            )
        self.ids[name] = int(paper_id)
        return self

    def cites(self, citing: str, cited: str) -> Graph:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                    " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
                ),
                {"a": self.ids[citing], "b": self.ids[cited]},
            )
        return self

    def features(self, name: str) -> dict[str, float]:
        with self.engine.connect() as conn:
            raw = conn.execute(
                text("SELECT features FROM graph_nodes WHERE session_id=:s AND paper_id=:p"),
                {"s": SID, "p": self.ids[name]},
            ).scalar()
        return dict(json.loads(raw)) if raw else {}

    def score(self, name: str) -> float | None:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT score FROM graph_nodes WHERE session_id=:s AND paper_id=:p"),
                {"s": SID, "p": self.ids[name]},
            ).scalar()


# --------------------------------------------------------------------------
# The features that get written
# --------------------------------------------------------------------------


def test_every_node_gets_features(engine: Engine) -> None:
    graph = Graph(engine).node("a").node("b")
    assert compute_and_store_features(engine, SID, as_of_year=AS_OF) == 2
    assert graph.features("a")


def test_a_recent_paper_scores_higher_on_recency(engine: Engine) -> None:
    graph = Graph(engine).node("old", year=2016).node("new", year=2025)
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    assert graph.features("new")["recency"] > graph.features("old")["recency"]


def test_quality_is_age_normalized_not_raw_citations(engine: Engine) -> None:
    """
    A 2016 paper with 400 citations accumulates them slower than a 2024 paper
    with 120. Ranking on the raw count decides older is better, which for a
    discovery tool is backwards -- the old ones are what you have read already.
    """
    graph = Graph(engine).node("old", year=2016, citations=400)
    graph.node("new", year=2024, citations=120)
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    assert graph.features("new")["quality"] > graph.features("old")["quality"]


def test_overlap_comes_from_what_the_expansion_counted(engine: Engine) -> None:
    """
    `anchor_overlap` is computed during pooling, where the union is being built
    and the information exists -- recomputing it here would be a second
    implementation of the same count, free to disagree with the first.
    """
    graph = Graph(engine).node("wide", overlap=5).node("narrow", overlap=1)
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    assert graph.features("wide")["overlap"] > graph.features("narrow")["overlap"]


def test_a_hub_scores_high_on_hub(engine: Engine) -> None:
    """
    `hub` carries a negative weight in ranking.yaml. A paper everything cites
    is a bad recommendation however good it is -- you have already read it.
    """
    graph = Graph(engine).node("hub").node("a").node("b").node("c")
    graph.cites("a", "hub").cites("b", "hub").cites("c", "hub")
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    assert graph.features("hub")["hub"] > graph.features("a")["hub"]


def test_coupling_reaches_the_features(engine: Engine) -> None:
    """
    The local measures PLAN.md says should carry most of the ranking weight.
    Computed against the anchors, because "related" only means anything
    relative to something.
    """
    graph = Graph(engine).node("seed", state="SEED").node("close").node("far").node("ref")
    graph.cites("seed", "ref").cites("close", "ref")
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    assert graph.features("close")["bibcoup"] > graph.features("far")["bibcoup"]


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


def test_features_are_stored_normalized(engine: Engine) -> None:
    """
    In [0, 1], so a weight means the same thing on every feature. Storing the
    raw citation count instead would make the normalization pool whatever
    happened to be loaded at score time -- so a paper's score would depend on
    what else was being looked at.
    """
    Graph(engine).node("a", citations=3).node("b", citations=5).node("c", citations=190_000)
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    graph = Graph(engine)
    with engine.connect() as conn:
        for (raw,) in conn.execute(
            text("SELECT features FROM graph_nodes WHERE session_id = :s"), {"s": SID}
        ):
            for name, value in json.loads(raw).items():
                assert 0.0 <= value <= 1.0, f"{name} = {value}"
    assert graph is not None


def test_one_enormous_outlier_does_not_flatten_the_rest(engine: Engine) -> None:
    """
    The rank-percentile guarantee, end to end. Under a z-score the three
    ordinary papers would land within 0.0002 of each other and `quality` would
    contribute nothing for any of them.
    """
    graph = Graph(engine).node("a", citations=3).node("b", citations=5)
    graph.node("c", citations=9).node("hub", citations=190_000)
    compute_and_store_features(engine, SID, as_of_year=AS_OF)

    quality = {n: graph.features(n)["quality"] for n in ("a", "b", "c", "hub")}
    assert quality["hub"] == pytest.approx(1.0)
    # Evenly spread by rank, untouched by how far away the hub is.
    assert quality["c"] > quality["b"] > quality["a"]
    assert quality["c"] - quality["b"] == pytest.approx(quality["b"] - quality["a"], abs=1e-9)


# --------------------------------------------------------------------------
# Scores follow
# --------------------------------------------------------------------------


def test_scores_are_written_too(engine: Engine) -> None:
    """
    Features without a score would leave the graph looking unranked until
    somebody happened to PUT the config.
    """
    graph = Graph(engine).node("a", overlap=3).node("b", overlap=1)
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    assert graph.score("a") is not None
    assert graph.score("a") > graph.score("b")  # type: ignore[operator]


def test_the_feature_names_match_the_configured_weights(engine: Engine) -> None:
    """
    A feature the config has no weight for is dead computation; a weight with
    no feature is a lever connected to nothing. Both are silent failures, so
    the correspondence is asserted rather than left to a comment.

    Features not yet computed are listed explicitly -- being unimplemented is a
    fact worth stating, and it makes adding one a deliberate edit here.
    """
    weights = set(load_ranking().weights.model_dump())
    # `venue` left this list at R3.d. `ppr` and `dislike` arrive at R5; `author`
    # is deferred past R4 because PLAN.md C4 demotes it and it costs a per-paper
    # S2 fetch, so paying for it before the benchmark can say whether it helps
    # is backwards.
    not_yet = {"ppr", "author", "dislike"}
    assert set(FEATURE_NAMES) == weights - not_yet


def test_running_twice_is_stable(engine: Engine) -> None:
    """CLAUDE.md rule 7 -- a ranking that drifts when nothing changed is unusable."""
    graph = Graph(engine).node("a", citations=5).node("b", citations=9)
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    first = graph.features("a")
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    assert graph.features("a") == first


def test_an_empty_session_computes_nothing_rather_than_failing(engine: Engine) -> None:
    assert compute_and_store_features(engine, SID, as_of_year=AS_OF) == 0


def test_another_session_is_untouched(engine: Engine) -> None:
    graph = Graph(engine).node("mine")
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
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (2, :p, 'CANDIDATE', 1)"
            ),
            {"p": paper_id},
        )

    assert compute_and_store_features(engine, SID, as_of_year=AS_OF) == 1
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT features FROM graph_nodes WHERE session_id = 2")).scalar()
            is None
        )
    assert graph is not None


# --------------------------------------------------------------------------
# R3.d -- venue tier
# --------------------------------------------------------------------------


def test_a_core_venue_scores_higher_than_an_unknown_one(engine: Engine) -> None:
    """
    BUILD.md's R3 bullet. NeurIPS is on CORE_VENUES; a preprint server is not,
    and neither is a venue nobody listed.
    """
    graph = Graph(engine).node("core", venue="Neural Information Processing Systems")
    graph.node("other", venue="arXiv.org")
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    assert graph.features("core")["venue"] > graph.features("other")["venue"]


def test_the_venue_is_matched_by_acronym_too(engine: Engine) -> None:
    """
    S2 reports the same conference both ways -- "NeurIPS" and "Neural
    Information Processing Systems" -- so matching only the expansion would
    score the same venue differently depending on which spelling arrived.
    """
    graph = Graph(engine).node("acronym", venue="NeurIPS").node("nothing", venue="Some Journal")
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    assert graph.features("acronym")["venue"] > graph.features("nothing")["venue"]


def test_a_missing_venue_is_not_a_core_venue(engine: Engine) -> None:
    """
    CLAUDE.md rule 6: a missing field degrades a score, never raises. Most of
    the corpus is stubs with no venue at all, so this is the common path rather
    than an edge case.
    """
    graph = Graph(engine).node("none", venue=None).node("core", venue="ICML")
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    assert graph.features("none")["venue"] < graph.features("core")["venue"]


def test_venue_is_derived_from_config_not_read_from_the_column(engine: Engine) -> None:
    """
    **`papers.venue_tier` stays empty on purpose.**

    A stored tier is a second copy of a decision the config owns, and it goes
    stale the moment CORE_VENUES changes -- exactly the failure that let stale
    `filter_decisions` exclude papers the current rules admit. The tier is
    computed from `papers.venue` against the *current* `CORE_VENUES` every time
    features are recomputed, so there is nothing to go stale.
    """
    graph = Graph(engine).node("core", venue="ICLR")
    compute_and_store_features(engine, SID, as_of_year=AS_OF)
    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT venue_tier FROM papers WHERE id = :p"), {"p": graph.ids["core"]}
        ).scalar()
    assert stored is None, "the column is deliberately not written"
    assert graph.features("core")["venue"] > 0
