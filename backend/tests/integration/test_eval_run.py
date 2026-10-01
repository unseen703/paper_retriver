"""
R4.6 -- `make eval`: the runner that feeds real pools to the harness.

Journey:

    As someone who wants to know whether the ranker works, I run one command and
    get every method's metrics on the same cases, with the ceiling beside them,
    and a second run gives the same file.

The corpus is built here rather than read from the fixture DB: the fixture is
99% stubs, so it yields too few eligible targets to test anything about the
shape of a case.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pools import NO_SESSION, build_scored_pool
from run import METHODS, NOT_RUN, build_cases, main, run_eval
from sqlalchemy import Connection, Engine

from app.config import filters
from app.db import make_engine
from app.models import Paper
from app.repo import edges as edges_repo
from app.repo import papers as papers_repo
from app.services.candidates import build_pool
from app.services.features import FEATURE_NAMES

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
AS_OF = 2026
SEED = 7
N_REFS = 18


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "eval.db"
    c = Config(str(ALEMBIC_INI))
    c.set_main_option("script_location", str(ALEMBIC_INI.parent))
    c.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(c, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


def _add(conn: Connection, key: str, *, year: int, date: str | None, citations: int = 10) -> int:
    return papers_repo.upsert_paper(
        conn,
        Paper(
            s2_paper_id=key,
            title=f"Paper {key}",
            first_seen_at="2026-01-01",
            year=year,
            publication_date=date,
            citation_count=citations,
        ),
    )


def _corpus(engine: Engine) -> dict[str, int]:
    """
    One target citing 18 references that cite each other (R_i cites every
    R_j, j < i), so whichever three become seeds can reach most of the rest.
    Plus one later paper that cites a reference: it exists only after the cutoff.
    """
    ids: dict[str, int] = {}
    with engine.begin() as conn:
        refs = [
            _add(conn, f"r{i}", year=2018, date="2018-03-01", citations=10 + i)
            for i in range(N_REFS)
        ]
        ids.update({f"r{i}": pid for i, pid in enumerate(refs)})
        target = _add(conn, "target", year=2022, date="2022-06-01")
        ids["target"] = target
        for ref in refs:
            edges_repo.upsert_edge(conn, target, ref, "BACKWARD")
        for i, citing in enumerate(refs):
            for cited in refs[:i]:
                edges_repo.upsert_edge(conn, citing, cited, "BACKWARD")
        future = _add(conn, "future", year=2024, date="2024-01-01", citations=9999)
        ids["future"] = future
        # Few references, so it is a leak candidate but not itself a benchmark
        # target (which needs at least 15).
        for ref in refs[:6]:
            edges_repo.upsert_edge(conn, future, ref, "FORWARD")
    return ids


# --------------------------------------------------------------------------
# What the runner reports
# --------------------------------------------------------------------------


def test_every_method_is_scored_on_the_same_cases(engine: Engine) -> None:
    _corpus(engine)
    with engine.connect() as conn:
        result = run_eval(conn, seed=SEED, as_of_year=AS_OF)

    assert result["n_cases"] == 1
    methods = result["methods"]
    assert isinstance(methods, dict)
    assert sorted(methods) == sorted(METHODS)
    for metrics in methods.values():
        assert {"recall@10", "recall@20", "recall@50", "ndcg@20", "mrr", "hit@10"} == set(metrics)


def test_the_ranker_finds_something_on_a_reachable_case(engine: Engine) -> None:
    _corpus(engine)
    with engine.connect() as conn:
        result = run_eval(conn, seed=SEED, as_of_year=AS_OF)
    methods = result["methods"]
    assert isinstance(methods, dict)
    assert methods["ranker"]["recall@50"]["mean"] > 0.0
    ceiling = result["pool_recall_ceiling"]
    assert isinstance(ceiling, float) and 0.0 < ceiling <= 1.0


def test_the_unrun_baseline_is_reported_not_omitted(engine: Engine) -> None:
    _corpus(engine)
    with engine.connect() as conn:
        result = run_eval(conn, seed=SEED, as_of_year=AS_OF)
    assert result["not_run"] == NOT_RUN
    assert "s2_recommendations" in NOT_RUN


# --------------------------------------------------------------------------
# Leakage
# --------------------------------------------------------------------------


def test_a_paper_from_after_the_cutoff_is_never_a_candidate(engine: Engine) -> None:
    """
    `future` cites the early references, so it can neighbour a seed -- and was
    published in 2024, long after the 2018 cutoff. If it reached the pool, the
    system would be recommending from the future.
    """
    ids = _corpus(engine)
    with engine.connect() as conn:
        (case,) = build_cases(conn, SEED, 300)
    # Wire `future` to the actual seeds, so the test cannot pass vacuously.
    with engine.begin() as conn:
        for seed in case.seed_ids:
            edges_repo.upsert_edge(conn, ids["future"], seed, "FORWARD")
    with engine.connect() as conn:
        unguarded = build_pool(conn, NO_SESSION, list(case.seed_ids), filters, AS_OF)
        pool, entries = build_scored_pool(conn, 1, case, filters, AS_OF)

    assert ids["future"] in {e.paper_id for e in unguarded}
    assert ids["future"] not in pool.features
    assert ids["future"] not in {e.paper_id for e in entries}
    assert ids["target"] not in pool.features


def test_pool_features_cover_exactly_the_computed_features(engine: Engine) -> None:
    """Rule 8: every feature the ranker is weighted on is populated here."""
    _corpus(engine)
    with engine.connect() as conn:
        (case,) = build_cases(conn, SEED, 300)
        pool, _ = build_scored_pool(conn, 1, case, filters, AS_OF)
    assert pool.features
    for feats in pool.features.values():
        assert set(feats) == set(FEATURE_NAMES)


# --------------------------------------------------------------------------
# Reproducibility, and the entry point
# --------------------------------------------------------------------------


def test_same_seed_and_corpus_give_identical_output(engine: Engine) -> None:
    _corpus(engine)
    with engine.connect() as conn:
        a = run_eval(conn, seed=SEED, as_of_year=AS_OF)
        b = run_eval(conn, seed=SEED, as_of_year=AS_OF)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_main_writes_the_results_file(engine: Engine, tmp_path: Path) -> None:
    _corpus(engine)
    out = tmp_path / "out"
    url = str(engine.url)
    assert main(["--db", url, "--out", str(out), "--seed", str(SEED), "--as-of-year", "2026"]) == 0
    written = json.loads((out / "eval.json").read_text(encoding="utf-8"))
    assert written["n_cases"] == 1


def test_main_fails_loudly_on_a_corpus_with_no_cases(engine: Engine, tmp_path: Path) -> None:
    assert main(["--db", str(engine.url), "--out", str(tmp_path / "out")]) == 1
    assert not (tmp_path / "out" / "eval.json").exists()
