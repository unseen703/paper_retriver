"""
`arxiv_meta` persistence. Global reference data, so no `session_id` -- see
BUILD.md "The session_id contract": facts about papers are global.

Everything is `INSERT ... ON CONFLICT DO UPDATE`, so a re-run of the loader is
free and the OAI delta can overwrite snapshot rows for papers that were revised
after the snapshot was cut.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from sqlalchemy import Connection, Engine, text

from app.clients.arxiv import ArxivRecord

UPSERT = text(
    "INSERT INTO arxiv_meta (arxiv_id, primary_category, categories, updated)"
    " VALUES (:arxiv_id, :primary_category, :categories, :updated)"
    " ON CONFLICT (arxiv_id) DO UPDATE SET"
    "   primary_category = excluded.primary_category,"
    "   categories = excluded.categories,"
    "   updated = excluded.updated"
)


def upsert_many(conn: Connection, records: Sequence[ArxivRecord]) -> int:
    """Idempotent bulk write. Returns the number of rows sent."""
    if not records:
        return 0
    conn.execute(
        UPSERT,
        [
            {
                "arxiv_id": r.arxiv_id,
                "primary_category": r.primary_category,
                "categories": json.dumps(list(r.categories)),
                "updated": r.updated,
            }
            for r in records
        ],
    )
    return len(records)


def load(engine: Engine, records: Iterable[ArxivRecord], batch_size: int = 10_000) -> int:
    """
    Stream records into the table in batches.

    Batched rather than row-at-a-time because 2.7M individual transactions on
    SQLite takes hours; batched it is minutes.
    """
    total = 0
    batch: list[ArxivRecord] = []
    for record in records:
        batch.append(record)
        if len(batch) >= batch_size:
            with engine.begin() as conn:
                total += upsert_many(conn, batch)
            batch = []
    if batch:
        with engine.begin() as conn:
            total += upsert_many(conn, batch)
    return total


def get(engine: Engine, arxiv_id: str) -> ArxivRecord | None:
    sql = text(
        "SELECT arxiv_id, primary_category, categories, updated"
        " FROM arxiv_meta WHERE arxiv_id = :arxiv_id"
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"arxiv_id": arxiv_id}).fetchone()
    if row is None:
        return None
    return ArxivRecord(
        arxiv_id=row[0],
        primary_category=row[1],
        categories=tuple(json.loads(row[2])),
        updated=row[3],
    )


def get_many(engine: Engine, arxiv_ids: Sequence[str]) -> dict[str, ArxivRecord]:
    """Bulk lookup for the paper normalizer's join (R0.11)."""
    if not arxiv_ids:
        return {}
    out: dict[str, ArxivRecord] = {}
    with engine.connect() as conn:
        # Chunked: SQLite caps bound parameters (999 by default).
        for start in range(0, len(arxiv_ids), 500):
            chunk = list(arxiv_ids[start : start + 500])
            placeholders = ",".join(f":id{i}" for i in range(len(chunk)))
            sql = text(
                "SELECT arxiv_id, primary_category, categories, updated"
                f" FROM arxiv_meta WHERE arxiv_id IN ({placeholders})"
            )
            params = {f"id{i}": value for i, value in enumerate(chunk)}
            for row in conn.execute(sql, params):
                out[row[0]] = ArxivRecord(
                    arxiv_id=row[0],
                    primary_category=row[1],
                    categories=tuple(json.loads(row[2])),
                    updated=row[3],
                )
    return out


def count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM arxiv_meta")).scalar() or 0)


def max_updated(engine: Engine) -> str | None:
    """
    The newest `updated` in the table -- where an OAI delta should resume.

    Stored rather than assumed so the two sources meet without a gap.
    """
    with engine.connect() as conn:
        return conn.execute(text("SELECT MAX(updated) FROM arxiv_meta")).scalar()


def category_counts(engine: Engine, limit: int = 10) -> list[tuple[str, int]]:
    """BUILD.md R0.10's verification query."""
    sql = text(
        "SELECT primary_category, COUNT(*) AS n FROM arxiv_meta"
        " GROUP BY 1 ORDER BY 2 DESC LIMIT :limit"
    )
    with engine.connect() as conn:
        return [(r[0], r[1]) for r in conn.execute(sql, {"limit": limit})]


__all__ = [
    "category_counts",
    "count",
    "get",
    "get_many",
    "load",
    "max_updated",
    "upsert_many",
]
