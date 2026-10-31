"""
R6.11 -- `embed-backfill` wires the backfill service to the DB and the .npy store.

No network: `fetch` is a stub standing in for `S2Client.get_embeddings`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from app.cli import run_embed_backfill
from app.db import make_engine
from app.models.domain import CrawlState, Paper
from app.repo import papers as papers_repo
from app.services.embeddings import DIM_SPECTER_V2, EmbeddingStore

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "e.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


def _paper(s2_id: str, state: CrawlState) -> Paper:
    return Paper(
        s2_paper_id=s2_id,
        title=f"title {s2_id}",
        abstract=f"abstract {s2_id}" if state is not CrawlState.STUB else None,
        year=2020,
        first_seen_at="2026-01-01T00:00:00Z",
        crawl_state=state,
    )


def _vec(i: int) -> list[float]:
    v = [0.0] * DIM_SPECTER_V2
    v[i] = 1.0
    return v


def _seed(engine: Engine) -> dict[str, int]:
    with engine.begin() as conn:
        return {
            "a": papers_repo.upsert_paper(conn, _paper("a", CrawlState.METADATA)),
            "b": papers_repo.upsert_paper(conn, _paper("b", CrawlState.METADATA)),
            "stub": papers_repo.upsert_paper(conn, _paper("stub", CrawlState.STUB)),
        }


async def test_backfill_stores_vectors_and_skips_stubs(engine: Engine, tmp_path: Path) -> None:
    ids = _seed(engine)
    requested: list[list[str]] = []

    async def fetch(s2_ids: list[str]) -> dict[str, list[float]]:
        requested.append(s2_ids)
        return {"a": _vec(0)}  # S2 has nothing for "b"

    stem = tmp_path / "emb"
    result = await run_embed_backfill(engine, fetch, stem)

    assert requested == [["a", "b"]]  # sorted, stub never asked for
    assert (result.requested, result.stored, result.unavailable) == (2, 1, 1)
    store = EmbeddingStore.load(stem)
    assert ids["a"] in store and ids["b"] not in store and ids["stub"] not in store


async def test_second_run_fetches_only_what_is_still_missing(
    engine: Engine, tmp_path: Path
) -> None:
    _seed(engine)
    stem = tmp_path / "emb"

    async def first(s2_ids: list[str]) -> dict[str, list[float]]:
        return {"a": _vec(0), "b": _vec(1)}

    await run_embed_backfill(engine, first, stem)

    calls: list[list[str]] = []

    async def second(s2_ids: list[str]) -> dict[str, list[float]]:
        calls.append(s2_ids)
        return {}

    result = await run_embed_backfill(engine, second, stem)
    assert calls == []  # everything fresh: zero fetches
    assert result.skipped_fresh == 2 and result.requested == 0


async def test_empty_corpus_still_writes_the_store(engine: Engine, tmp_path: Path) -> None:
    async def fetch(s2_ids: list[str]) -> dict[str, list[float]]:
        raise AssertionError("nothing to fetch")

    stem = tmp_path / "emb"
    await run_embed_backfill(engine, fetch, stem)
    assert stem.with_suffix(".npy").exists() and stem.with_suffix(".json").exists()
