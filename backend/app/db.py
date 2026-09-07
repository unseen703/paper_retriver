"""
Engine construction and the two SQLite pragmas that are not optional.

`foreign_keys` is the important one. SQLite defaults foreign key enforcement
**off**, per connection. A schema full of REFERENCES clauses that enforce
nothing reads as correct in review and silently accepts orphan rows at runtime;
by the time it surfaces, the data is already wrong. BUILD.md R0.4 calls this out
by name, so it is set in a connect hook rather than anywhere a caller could
forget it.

`journal_mode=WAL` lets the background expansion worker write while the API
reads, which is the whole reason the single-process design works.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event

from app.config import REPO_ROOT, settings


def db_url(db_path: str | None = None) -> str:
    """Resolve settings.db_path (relative to the repo root) into a SQLite URL."""
    raw = db_path or settings.db_path
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    engine = create_engine(url or db_url(), echo=echo, future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            # Both must run before any statement the caller issues on this
            # connection -- foreign_keys in particular is per-connection state.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


__all__ = ["db_url", "make_engine"]
