"""R6 dedup upgrade, surfaced -- `dup-report` reads the DB and the .npy store, merges nothing."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from app.cli import run_dup_report
from app.db import make_engine
from app.models.domain import CrawlState, Paper
from app.repo import papers as papers_repo
from app.services.embeddings import DIM_SPECTER_V2, EmbeddingStore

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "d.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


def _paper(s2_id: str, title: str) -> Paper:
    return Paper(
        s2_paper_id=s2_id,
        title=title,
        abstract="x",
        year=2020,
        first_seen_at="2026-01-01T00:00:00Z",
        crawl_state=CrawlState.METADATA,
    )


def _vec(i: int) -> list[float]:
    v = [0.0] * DIM_SPECTER_V2
    v[i] = 1.0
    return v


def test_flags_near_identical_pair_and_merges_nothing(engine: Engine, tmp_path: Path) -> None:
    with engine.begin() as conn:
        a = papers_repo.upsert_paper(conn, _paper("a", "Attention Is All You Need"))
        b = papers_repo.upsert_paper(conn, _paper("b", "Attention is all you need."))
        c = papers_repo.upsert_paper(conn, _paper("c", "Attention Is All You Need"))
    store = EmbeddingStore()
    store.put(a, _vec(0), "h")
    store.put(b, _vec(0), "h")
    store.put(c, _vec(1), "h")  # same title, unrelated vector: not flagged
    stem = tmp_path / "emb"
    store.save(stem)

    pairs = run_dup_report(engine, stem)

    assert [(p.a, p.b) for p in pairs] == [(a, b)]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM papers")).scalar_one() == 3


def test_no_vectors_means_no_candidates(engine: Engine, tmp_path: Path) -> None:
    with engine.begin() as conn:
        papers_repo.upsert_paper(conn, _paper("a", "t"))
    assert run_dup_report(engine, tmp_path / "none") == []
