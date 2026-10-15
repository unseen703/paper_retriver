"""R6 dedup upgrade -- `GET /api/duplicates` lists review pairs and merges nothing."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.deps import get_embedding_stem
from app.db import make_engine
from app.main import create_app
from app.models.domain import CrawlState, Paper
from app.repo import papers as papers_repo
from app.services.embeddings import DIM_SPECTER_V2, EmbeddingStore

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "dup.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


@pytest.fixture
def stem(tmp_path: Path) -> Path:
    return tmp_path / "emb"


@pytest.fixture
def client(engine: Engine, stem: Path) -> Iterator[TestClient]:
    app = create_app(db_url=str(engine.url))
    app.dependency_overrides[get_embedding_stem] = lambda: stem
    with TestClient(app) as c:
        yield c


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


def test_no_vectors_is_an_empty_list_not_an_error(client: TestClient) -> None:
    r = client.get("/api/duplicates")
    assert r.status_code == 200
    assert r.json() == {"pairs": [], "total": 0}


def test_lists_pair_with_metadata_and_merges_nothing(
    client: TestClient, engine: Engine, stem: Path
) -> None:
    with engine.begin() as conn:
        a = papers_repo.upsert_paper(conn, _paper("a", "Attention Is All You Need"))
        b = papers_repo.upsert_paper(conn, _paper("b", "Attention is all you need."))
        c = papers_repo.upsert_paper(conn, _paper("c", "Attention Is All You Need"))
    store = EmbeddingStore()
    store.put(a, _vec(0), "h")
    store.put(b, _vec(0), "h")
    store.put(c, _vec(1), "h")  # same title, unrelated vector: not a duplicate
    store.save(stem)

    body = client.get("/api/duplicates").json()

    assert body["total"] == 1
    pair = body["pairs"][0]
    assert (pair["a"]["id"], pair["b"]["id"]) == (a, b)
    assert pair["a"]["year"] == 2020
    assert pair["cosine"] >= 0.97
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM papers")).scalar_one() == 3


def test_total_is_the_whole_set_and_offset_pages(
    client: TestClient, engine: Engine, stem: Path
) -> None:
    store = EmbeddingStore()
    with engine.begin() as conn:
        for n in range(3):
            x = papers_repo.upsert_paper(conn, _paper(f"x{n}", f"Topic {n} study"))
            y = papers_repo.upsert_paper(conn, _paper(f"y{n}", f"Topic {n} study."))
            store.put(x, _vec(n), "h")
            store.put(y, _vec(n), "h")
    store.save(stem)

    first = client.get("/api/duplicates", params={"limit": 2}).json()
    rest = client.get("/api/duplicates", params={"limit": 2, "offset": 2}).json()

    assert first["total"] == rest["total"] == 3
    assert len(first["pairs"]) == 2 and len(rest["pairs"]) == 1
    ids = [p["a"]["id"] for p in first["pairs"] + rest["pairs"]]
    assert len(set(ids)) == 3  # no pair on two pages, none skipped
