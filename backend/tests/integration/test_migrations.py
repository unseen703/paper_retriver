"""
R0.4 -- Alembic baseline.

The whole PLAN.md section C schema lands in one migration, including columns
nothing uses until R6. Schema churn is free before there is data.

The tests that matter most here are the two silent-failure guards:

* **`PRAGMA foreign_keys`.** SQLite defaults foreign key enforcement OFF. A
  schema full of REFERENCES clauses that enforce nothing looks correct in every
  code review and corrupts data at runtime. Asserted by actually attempting a
  bad insert, not by reading the pragma back.
* **`CHECK (citing_id != cited_id)`.** Self-citations arrive from S2 in real
  data and would otherwise become self-loops that break PageRank.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.db import make_engine

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"

EXPECTED_TABLES = {
    "papers",
    "authors",
    "paper_authors",
    "edges",
    "graph_nodes",
    "interaction_events",
    "filter_decisions",
    "expansions",
    "api_cache",
    "arxiv_meta",
    "sessions",
}


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture
def migrated(tmp_path: Path) -> Iterator[Path]:
    db = tmp_path / "test.db"
    command.upgrade(_alembic_config(db), "head")
    yield db


def _conn(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(db)
    c.execute("PRAGMA foreign_keys = ON")
    return c


# --------------------------------------------------------------------------
# Every table from PLAN.md section C, now
# --------------------------------------------------------------------------


def test_upgrade_creates_every_planned_table(migrated: Path) -> None:
    with _conn(migrated) as c:
        got = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert got >= EXPECTED_TABLES, f"missing: {EXPECTED_TABLES - got}"


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("papers", "canonical_paper_id"),
        ("papers", "title_norm"),
        ("papers", "crawl_state"),
        ("papers", "primary_arxiv_category"),
        ("papers", "venue_tier"),
        ("graph_nodes", "pos_x"),
        ("graph_nodes", "pos_y"),
        ("graph_nodes", "community_id"),  # unused until R6, present anyway
        ("graph_nodes", "score_breakdown"),
        ("edges", "intents"),
        ("filter_decisions", "config_version"),
        ("expansions", "cache_hits"),
    ],
)
def test_column_exists(migrated: Path, table: str, column: str) -> None:
    with _conn(migrated) as c:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    assert column in cols


# --------------------------------------------------------------------------
# Constraints, asserted behaviourally
# --------------------------------------------------------------------------


def test_foreign_keys_are_actually_enforced(migrated: Path) -> None:
    """SQLite defaults FK enforcement OFF -- the nastiest silent bug in R0.4."""
    with _conn(migrated) as c:
        c.execute(
            "INSERT INTO papers (id, s2_paper_id, title, title_norm, first_seen_at)"
            " VALUES (1, 's1', 'T', 't', '2026-01-01')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                " VALUES (1, 999, 'BACKWARD', '2026-01-01')"
            )


def test_engine_connect_hook_turns_foreign_keys_on(migrated: Path) -> None:
    """The app's own engine must not rely on the test harness setting it."""
    engine = make_engine(f"sqlite:///{migrated.as_posix()}")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_engine_uses_wal(migrated: Path) -> None:
    engine = make_engine(f"sqlite:///{migrated.as_posix()}")
    with engine.connect() as conn:
        assert str(conn.execute(text("PRAGMA journal_mode")).scalar()).lower() == "wal"


def test_edges_reject_self_citations(migrated: Path) -> None:
    with _conn(migrated) as c:
        c.execute(
            "INSERT INTO papers (id, s2_paper_id, title, title_norm, first_seen_at)"
            " VALUES (1, 's1', 'T', 't', '2026-01-01')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                " VALUES (1, 1, 'BACKWARD', '2026-01-01')"
            )


def test_papers_s2_id_is_unique(migrated: Path) -> None:
    with _conn(migrated) as c:
        c.execute(
            "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
            " VALUES ('dup', 'T', 't', '2026-01-01')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                " VALUES ('dup', 'Other', 'other', '2026-01-01')"
            )


def test_api_cache_is_unique_on_endpoint_and_params_hash(migrated: Path) -> None:
    with _conn(migrated) as c:
        c.execute(
            "INSERT INTO api_cache (endpoint, params_hash, status_code, response, fetched_at)"
            " VALUES ('/paper/batch', 'h1', 200, '{}', '2026-01-01')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO api_cache (endpoint, params_hash, status_code, response, fetched_at)"
                " VALUES ('/paper/batch', 'h1', 200, '{}', '2026-01-02')"
            )


# --------------------------------------------------------------------------
# Primary keys and indexes named as non-negotiable in BUILD.md R0.4
# --------------------------------------------------------------------------


def _pk(c: sqlite3.Connection, table: str) -> list[str]:
    rows = [r for r in c.execute(f"PRAGMA table_info({table})") if r[5]]
    return [r[1] for r in sorted(rows, key=lambda r: r[5])]


def test_edges_primary_key_is_citing_then_cited(migrated: Path) -> None:
    with _conn(migrated) as c:
        assert _pk(c, "edges") == ["citing_id", "cited_id"]


def test_graph_nodes_primary_key_is_session_then_paper(migrated: Path) -> None:
    with _conn(migrated) as c:
        assert _pk(c, "graph_nodes") == ["session_id", "paper_id"]


def test_paper_authors_primary_key(migrated: Path) -> None:
    with _conn(migrated) as c:
        assert _pk(c, "paper_authors") == ["paper_id", "author_id"]


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("edges", ["cited_id"]),  # reverse traversal
        ("graph_nodes", ["session_id", "state"]),
        ("filter_decisions", ["paper_id", "session_id"]),
        ("papers", ["title_norm"]),
        ("papers", ["arxiv_id"]),
        ("papers", ["doi"]),
        ("interaction_events", ["session_id", "paper_id", "id"]),
    ],
)
def test_index_exists_on(migrated: Path, table: str, columns: list[str]) -> None:
    with _conn(migrated) as c:
        found = False
        for idx in c.execute(f"PRAGMA index_list({table})"):
            cols = [r[2] for r in c.execute(f"PRAGMA index_info({idx[1]})")]
            if cols == columns:
                found = True
                break
    assert found, f"no index on {table}({', '.join(columns)})"


def test_filter_decisions_session_id_is_nullable(migrated: Path) -> None:
    """Global verdicts (PRE_ERA, IS_DATASET) are written with session_id NULL."""
    with _conn(migrated) as c:
        c.execute(
            "INSERT INTO papers (id, s2_paper_id, title, title_norm, first_seen_at)"
            " VALUES (1, 's1', 'T', 't', '2026-01-01')"
        )
        c.execute(
            "INSERT INTO filter_decisions"
            " (paper_id, session_id, outcome, stage, reason_code, config_version, decided_at)"
            " VALUES (1, NULL, 'REJECT', 'TOPIC', 'PRE_ERA', 'abc123', '2026-01-01')"
        )
        assert c.execute("SELECT COUNT(*) FROM filter_decisions").fetchone()[0] == 1


# --------------------------------------------------------------------------
# Seed row and round-trip
# --------------------------------------------------------------------------


def test_default_session_is_seeded(migrated: Path) -> None:
    """config.SESSION_ID is hardcoded to 1; the row has to exist for the FK."""
    with _conn(migrated) as c:
        row = c.execute("SELECT id, name FROM sessions WHERE id = 1").fetchone()
    assert row is not None
    assert row[1] == "default"


def test_downgrade_to_base_then_upgrade_again_is_clean(tmp_path: Path) -> None:
    """BUILD.md R0.4 verification. A one-way migration is not a migration."""
    db = tmp_path / "roundtrip.db"
    cfg = _alembic_config(db)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    with _conn(db) as c:
        left = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not (EXPECTED_TABLES & left), f"downgrade left tables behind: {EXPECTED_TABLES & left}"

    command.upgrade(cfg, "head")
    with _conn(db) as c:
        again = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        seeded = c.execute("SELECT COUNT(*) FROM sessions WHERE id = 1").fetchone()[0]
    assert again >= EXPECTED_TABLES
    assert seeded == 1, "re-upgrade must re-seed the default session exactly once"
