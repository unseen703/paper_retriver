"""
R0.10 -- arxiv_meta persistence.

The properties that matter for a 2.7M-row load that can be interrupted:
idempotence (rerunning costs nothing and corrupts nothing), and a watermark so
the OAI delta resumes exactly where the snapshot ended.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from app.clients.arxiv import ArxivRecord
from app.db import make_engine
from app.repo import arxiv_meta

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "meta.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


def _rec(arxiv_id: str, cats: str = "cs.LG cs.CV", updated: str = "2024-01-01") -> ArxivRecord:
    parts = tuple(cats.split())
    return ArxivRecord(
        arxiv_id=arxiv_id, primary_category=parts[0], categories=parts, updated=updated
    )


def test_load_then_get_round_trips(engine: Engine) -> None:
    arxiv_meta.load(engine, [_rec("1706.03762", "cs.CL cs.LG")])
    got = arxiv_meta.get(engine, "1706.03762")
    assert got is not None
    assert got.primary_category == "cs.CL"
    assert got.categories == ("cs.CL", "cs.LG")


def test_get_returns_none_for_an_unknown_id(engine: Engine) -> None:
    assert arxiv_meta.get(engine, "9999.99999") is None


def test_loading_twice_is_idempotent(engine: Engine) -> None:
    """A resumed load must not duplicate what it already wrote."""
    records = [_rec(f"20{i:02d}.00001") for i in range(20)]
    arxiv_meta.load(engine, records)
    arxiv_meta.load(engine, records)
    assert arxiv_meta.count(engine) == 20


def test_a_later_load_overwrites_an_earlier_one(engine: Engine) -> None:
    """This is how the OAI delta corrects a stale snapshot row."""
    arxiv_meta.load(engine, [_rec("2501.00001", "cs.CV", updated="2025-01-01")])
    arxiv_meta.load(engine, [_rec("2501.00001", "cs.LG cs.CV", updated="2025-06-01")])
    got = arxiv_meta.get(engine, "2501.00001")
    assert got is not None
    assert got.primary_category == "cs.LG"
    assert got.updated == "2025-06-01"


def test_batching_writes_everything(engine: Engine) -> None:
    arxiv_meta.load(engine, (_rec(f"x{i}") for i in range(250)), batch_size=100)
    assert arxiv_meta.count(engine) == 250


def test_get_many_bulk_lookup(engine: Engine) -> None:
    arxiv_meta.load(engine, [_rec("a"), _rec("b"), _rec("c")])
    found = arxiv_meta.get_many(engine, ["a", "c", "missing"])
    assert set(found) == {"a", "c"}


def test_get_many_chunks_past_the_sqlite_parameter_cap(engine: Engine) -> None:
    """SQLite caps bound parameters at 999; a 1200-id join must not blow up."""
    ids = [f"p{i}" for i in range(1200)]
    arxiv_meta.load(engine, [_rec(i) for i in ids])
    assert len(arxiv_meta.get_many(engine, ids)) == 1200


def test_get_many_with_no_ids_touches_nothing(engine: Engine) -> None:
    assert arxiv_meta.get_many(engine, []) == {}


def test_max_updated_is_the_oai_resume_watermark(engine: Engine) -> None:
    arxiv_meta.load(
        engine,
        [
            _rec("a", updated="2024-03-01"),
            _rec("b", updated="2025-07-14"),
            _rec("c", updated="2023-01-01"),
        ],
    )
    assert arxiv_meta.max_updated(engine) == "2025-07-14"


def test_max_updated_on_an_empty_table_is_none(engine: Engine) -> None:
    assert arxiv_meta.max_updated(engine) is None


def test_category_counts_is_the_build_md_verification_query(engine: Engine) -> None:
    arxiv_meta.load(
        engine,
        [_rec("a", "cs.LG"), _rec("b", "cs.LG"), _rec("c", "cs.CL")],
    )
    assert arxiv_meta.category_counts(engine) == [("cs.LG", 2), ("cs.CL", 1)]


def test_cross_listed_categories_survive_the_round_trip(engine: Engine) -> None:
    """Batch Normalization is cs.LG x-listed cs.CV; both must persist."""
    arxiv_meta.load(engine, [_rec("1502.03167", "cs.LG cs.CV")])
    got = arxiv_meta.get(engine, "1502.03167")
    assert got is not None
    assert got.primary_category == "cs.LG"
    assert "cs.CV" in got.categories
