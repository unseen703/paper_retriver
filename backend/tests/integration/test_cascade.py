"""
R1.6 -- the filter cascade and its decision log.

Stage 0 -> 1 -> 2, short-circuiting on the first non-ACCEPT, with every verdict
persisted to `filter_decisions` stamped with the `config_version` that produced
it. Without that stamp a past run is uninterpretable the moment a threshold
moves, which is the entire reason the column exists.

The property that earns its keep is the **global-verdict cache**. BUILD.md's
verification is that re-running makes *zero* additional filter evaluations for
globally-rejected papers. That is the payoff for the `is_global` flag threaded
through since R1.4: a 2014 paper is a 2014 paper in every session forever, so
its rejection is written with `session_id = NULL`, read back on the next run,
and never recomputed. On a mature corpus this skips most of the filter work.

Session-scoped verdicts are the opposite and must never be cached globally --
"already in this graph" is true of one workspace and false of another.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from app.config import filters as cfg
from app.db import make_engine
from app.models import Outcome, Paper
from app.repo import papers as papers_repo
from app.services.filters.cascade import CascadeStats, run_cascade

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SESSION = 1
AS_OF = 2026


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[Connection]:
    db = tmp_path / "cascade.db"
    c = Config(str(ALEMBIC_INI))
    c.set_main_option("script_location", str(ALEMBIC_INI.parent))
    c.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(c, "head")
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as connection:
        yield connection
    engine.dispose()


def _paper(s2_id: str = "s1", **over: object) -> Paper:
    base: dict[str, object] = {
        "s2_paper_id": s2_id,
        "title": "An Ordinary Research Paper",
        "first_seen_at": "2026-01-01",
        "year": 2020,
        "primary_arxiv_category": "cs.LG",
    }
    base.update(over)
    return Paper(**base)  # type: ignore[arg-type]


def _stored(conn: Connection, paper: Paper) -> int:
    return papers_repo.upsert_paper(conn, paper)


def _decisions(conn: Connection) -> list[tuple]:
    return list(
        conn.execute(
            text(
                "SELECT paper_id, session_id, outcome, stage, reason_code, config_version"
                " FROM filter_decisions ORDER BY id"
            )
        )
    )


# --------------------------------------------------------------------------
# Stage order and short-circuiting
# --------------------------------------------------------------------------


def test_a_clean_paper_reaches_accept(conn: Connection) -> None:
    paper = _paper()
    pid = _stored(conn, paper)
    d = run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    assert d.outcome is Outcome.ACCEPT


def test_a_biochemistry_paper_reaches_accept_through_the_whole_cascade(
    conn: Connection,
) -> None:
    """
    **End to end, not just `topic_filter`.** The unit tests prove the topic
    stage admits these titles; this proves nothing upstream refuses them first.
    Era and type run before topic and short-circuit, so a paper the topic stage
    would welcome can still be thrown out by a stage that never mentions it.

    The realistic shape matters: these are journal papers, so the arXiv category
    is null and `q-bio.*` never appears. A rule keyed on category would find
    none of them.
    """
    paper = _paper(
        "biochem",
        title="A general model for predicting enzyme functions based on enzymatic reactions",
        primary_arxiv_category=None,
        year=2023,
    )
    pid = _stored(conn, paper)
    decision = run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)

    assert decision.outcome is Outcome.ACCEPT, decision.reason_code
    assert decision.reason_code == "BIOCHEM_ML"


def test_a_biochemistry_verdict_is_cached_like_any_other(conn: Connection) -> None:
    """
    An ACCEPT is re-evaluated rather than cached, so what this really checks is
    that admitting through the new corridor files a decision row at the current
    config version -- which is what keeps the paper out of the stale-verdict
    exclusion `build_pool` applies.
    """
    paper = _paper(
        "biochem2",
        title="Metabolic pathway prediction with graph neural networks",
        primary_arxiv_category=None,
        year=2023,
    )
    pid = _stored(conn, paper)
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)

    rows = [r for r in _decisions(conn) if r[0] == pid]
    assert rows, "the verdict must be filed, or pooling cannot tell it was decided"
    assert rows[-1][2] == "ACCEPT"
    assert rows[-1][5] == cfg.config_version


def test_a_pre_era_biochemistry_paper_is_still_refused(conn: Connection) -> None:
    """
    The corridor admits a *topic*, not a free pass. A 2010 metabolic-pathway
    paper is still outside the corpus year floor, and era runs first.
    """
    paper = _paper(
        "old-biochem",
        title="Metabolic pathway prediction in silico",
        primary_arxiv_category=None,
        year=2010,
    )
    pid = _stored(conn, paper)
    decision = run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)

    assert decision.outcome is not Outcome.ACCEPT
    assert decision.reason_code == "PRE_ERA"


def test_era_runs_first_and_short_circuits(conn: Connection) -> None:
    """
    A 2014 dataset paper must report PRE_ERA, not IS_DATASET. Era is the
    cheapest check, so running it first is what keeps the cascade cheap.
    """
    paper = _paper(year=2014, publication_types=("Dataset",))
    pid = _stored(conn, paper)
    d = run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    assert d.reason_code == "PRE_ERA"


def test_type_runs_before_topic(conn: Connection) -> None:
    """A cs.CV dataset paper reports IS_DATASET, not CAT_PRIMARY_APPLIED."""
    paper = _paper(publication_types=("Dataset",), primary_arxiv_category="cs.CV")
    pid = _stored(conn, paper)
    d = run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    assert d.reason_code == "IS_DATASET"


def test_a_short_circuit_writes_only_the_deciding_stage(conn: Connection) -> None:
    """One row per paper per run, not one per stage attempted."""
    paper = _paper(year=2014)
    pid = _stored(conn, paper)
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    assert len(_decisions(conn)) == 1


def test_quarantine_also_stops_the_cascade(conn: Connection) -> None:
    """Only ACCEPT continues; QUARANTINE is a verdict, not a maybe-continue."""
    paper = _paper(year=None, primary_arxiv_category="cs.CV")
    pid = _stored(conn, paper)
    d = run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    assert (d.outcome, d.reason_code) == (Outcome.QUARANTINE, "YEAR_UNKNOWN")


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_every_decision_is_persisted(conn: Connection) -> None:
    paper = _paper()
    pid = _stored(conn, paper)
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    (row,) = _decisions(conn)
    assert row[0] == pid
    assert row[2] == "ACCEPT"


def test_the_config_version_is_stamped(conn: Connection) -> None:
    """Without it, a past run is uninterpretable once a threshold moves."""
    paper = _paper(year=2014)
    pid = _stored(conn, paper)
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    (row,) = _decisions(conn)
    assert row[5] == cfg.config_version


def test_a_global_verdict_is_written_with_a_null_session(conn: Connection) -> None:
    """
    BUILD.md session_id contract: global verdicts carry session_id NULL so they
    can be reused by every session.
    """
    paper = _paper(year=2014)
    pid = _stored(conn, paper)
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    (row,) = _decisions(conn)
    assert row[1] is None


def test_the_details_survive_as_json(conn: Connection) -> None:
    paper = _paper(year=2014)
    pid = _stored(conn, paper)
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    import json

    raw = conn.execute(text("SELECT details FROM filter_decisions")).scalar()
    assert json.loads(raw)["year"] == 2014


# --------------------------------------------------------------------------
# The global-verdict cache -- BUILD.md's named verification
# --------------------------------------------------------------------------


def test_a_second_run_reuses_the_cached_global_rejection(conn: Connection) -> None:
    """
    BUILD.md: "re-running makes zero additional filter evaluations for
    globally-rejected papers."
    """
    paper = _paper(year=2014)
    pid = _stored(conn, paper)

    first = CascadeStats()
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF, stats=first)
    assert first.evaluated == 1
    assert first.cache_hits == 0

    second = CascadeStats()
    d = run_cascade(conn, SESSION, pid, paper, cfg, AS_OF, stats=second)
    assert second.evaluated == 0
    assert second.cache_hits == 1
    assert d.reason_code == "PRE_ERA"


def test_a_cached_verdict_does_not_write_a_duplicate_row(conn: Connection) -> None:
    paper = _paper(year=2014)
    pid = _stored(conn, paper)
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    assert len(_decisions(conn)) == 1


def test_a_cached_global_verdict_is_visible_to_another_session(conn: Connection) -> None:
    """The whole point of session_id NULL: session 2 pays nothing for it."""
    conn.execute(
        text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
    )
    paper = _paper(year=2014)
    pid = _stored(conn, paper)
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)

    stats = CascadeStats()
    run_cascade(conn, 2, pid, paper, cfg, AS_OF, stats=stats)
    assert stats.evaluated == 0
    assert stats.cache_hits == 1


def test_an_accept_is_not_cached_as_a_skip(conn: Connection) -> None:
    """
    Only REJECT is worth caching. An accepted paper must be re-evaluated,
    because a config change can turn it into a rejection.
    """
    paper = _paper()
    pid = _stored(conn, paper)
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    stats = CascadeStats()
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF, stats=stats)
    assert stats.evaluated == 1


def test_a_verdict_from_a_different_config_version_is_not_reused(conn: Connection) -> None:
    """
    A cached rejection is only valid for the config that produced it. Changing
    a threshold must re-open every decision it could have changed.
    """
    paper = _paper(year=2014)
    pid = _stored(conn, paper)
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)
    conn.execute(text("UPDATE filter_decisions SET config_version = 'stale00000000'"))

    stats = CascadeStats()
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF, stats=stats)
    assert stats.evaluated == 1


# --------------------------------------------------------------------------
# Corpus-wide behaviour over the R0.9 fixture cast
# --------------------------------------------------------------------------


FIXTURES: list[tuple[str, dict, str]] = [
    ("bert", {"year": 2019, "primary_arxiv_category": "cs.CL"}, "CAT_PRIMARY_CORE"),
    ("attention", {"year": 2017, "primary_arxiv_category": "cs.CL"}, "CAT_PRIMARY_CORE"),
    ("adam", {"year": 2015, "primary_arxiv_category": "cs.LG"}, "CAT_PRIMARY_CORE"),
    ("gpt3", {"year": 2020, "primary_arxiv_category": "cs.CL"}, "CAT_PRIMARY_CORE"),
    (
        "batchnorm",
        {
            "year": 2015,
            "primary_arxiv_category": "cs.LG",
            "arxiv_categories": ("cs.LG", "cs.CV"),
        },
        "CAT_PRIMARY_CORE",
    ),
    (
        "gnn_survey",
        {
            "year": 2019,
            "title": "A Comprehensive Survey on Graph Neural Networks",
            "citation_count": 9000,
            "primary_arxiv_category": "cs.LG",
        },
        "CAT_PRIMARY_CORE",
    ),
    ("word2vec", {"year": 2013, "primary_arxiv_category": "cs.CL"}, "PRE_ERA"),
    ("resnet", {"year": 2015, "primary_arxiv_category": "cs.CV"}, "CAT_PRIMARY_APPLIED"),
    (
        "imagenet",
        {
            "year": 2009,
            "primary_arxiv_category": None,
            "title": "ImageNet: A Large-Scale Hierarchical Image Database",
        },
        "PRE_ERA",
    ),
    (
        "skin_cancer",
        {
            "year": 2017,
            # Explicitly none: a Nature medical paper has no arXiv entry, which
            # is exactly why it has to fall through to the s2_fields rung.
            "primary_arxiv_category": None,
            "s2_fields": ("Medicine",),
            "title": "Dermatologist-level Classification",
        },
        "FIELD_NON_CS",
    ),
]


@pytest.mark.parametrize(("name", "kwargs", "expected_code"), FIXTURES)
def test_fixture_cascade_verdict(
    conn: Connection, name: str, kwargs: dict, expected_code: str
) -> None:
    paper = _paper(name, **kwargs)
    pid = _stored(conn, paper)
    assert run_cascade(conn, SESSION, pid, paper, cfg, AS_OF).reason_code == expected_code


def test_the_accept_set_over_the_fixture_cast_is_exactly_the_core_six(
    conn: Connection,
) -> None:
    """
    BUILD.md: "assert the accept set is exactly the expected N". Six core papers
    in, four out for four distinct reasons.
    """
    accepted = set()
    for name, kwargs, _ in FIXTURES:
        paper = _paper(name, **kwargs)
        pid = _stored(conn, paper)
        if run_cascade(conn, SESSION, pid, paper, cfg, AS_OF).outcome is Outcome.ACCEPT:
            accepted.add(name)
    assert accepted == {"bert", "attention", "adam", "gpt3", "batchnorm", "gnn_survey"}


def test_one_decision_row_per_fixture(conn: Connection) -> None:
    for name, kwargs, _ in FIXTURES:
        paper = _paper(name, **kwargs)
        run_cascade(conn, SESSION, _stored(conn, paper), paper, cfg, AS_OF)
    assert len(_decisions(conn)) == len(FIXTURES)


def test_rerunning_the_whole_cast_evaluates_only_the_accepts(conn: Connection) -> None:
    """
    The cache's payoff, measured. Four rejects are cached; only the six accepted
    papers are re-evaluated.
    """
    stored = []
    for name, kwargs, _ in FIXTURES:
        paper = _paper(name, **kwargs)
        stored.append((_stored(conn, paper), paper))
    for pid, paper in stored:
        run_cascade(conn, SESSION, pid, paper, cfg, AS_OF)

    stats = CascadeStats()
    for pid, paper in stored:
        run_cascade(conn, SESSION, pid, paper, cfg, AS_OF, stats=stats)
    assert stats.cache_hits == 4
    assert stats.evaluated == 6
