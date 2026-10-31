from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from app.db import make_engine
from app.services.communities import refresh_communities

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SID = 1


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "comm.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


def _build(engine: Engine, names: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    ids: dict[str, int] = {}
    with engine.begin() as conn:
        for n in names:
            row = conn.execute(
                text(
                    "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                    " VALUES (:n, :n, :n, '2026-01-01') RETURNING id"
                ),
                {"n": n},
            ).fetchone()
            assert row is not None
            ids[n] = int(row[0])
            conn.execute(
                text(
                    "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                    " VALUES (:s, :p, 'CANDIDATE', 1)"
                ),
                {"s": SID, "p": ids[n]},
            )
        for a, b in edges:
            conn.execute(
                text(
                    "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                    " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
                ),
                {"a": ids[a], "b": ids[b]},
            )
    return ids


def _stored(engine: Engine) -> dict[int, int | None]:
    with engine.connect() as conn:
        return {
            r[0]: r[1]
            for r in conn.execute(
                text("SELECT paper_id, community_id FROM graph_nodes WHERE session_id=:s"),
                {"s": SID},
            )
        }


def test_refresh_persists_one_id_per_node_and_is_idempotent(engine: Engine) -> None:
    names = ["a", "b", "c", "x", "y", "z"]
    edges = [("a", "b"), ("b", "c"), ("c", "a"), ("x", "y"), ("y", "z"), ("z", "x")]
    ids = _build(engine, names, edges)
    with engine.begin() as conn:
        first = refresh_communities(conn, SID)
    stored = _stored(engine)
    assert stored == first
    assert all(v is not None for v in stored.values())
    assert stored[ids["a"]] == stored[ids["b"]] == stored[ids["c"]]
    assert stored[ids["a"]] != stored[ids["x"]]
    with engine.begin() as conn:
        assert refresh_communities(conn, SID) == first
