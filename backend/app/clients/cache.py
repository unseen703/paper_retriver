"""
Verbatim response cache, keyed on (endpoint, params).

Two decisions carry the weight here.

**The key must not depend on dict ordering.** Python dicts preserve insertion
order, so `{"a":1,"b":2}` and `{"b":2,"a":1}` serialize to different strings. If
the key followed that, the same logical request built by two code paths would
miss, and R0.8's "second run makes zero network calls" checkpoint would fail for
a reason almost invisible in a debugger. `json.dumps(sort_keys=True)` fixes it.
List order is *not* sorted -- `ids=[x,y]` and `ids=[y,x]` are genuinely
different batch requests.

**Responses are stored exactly as received.** Normalizing on write means a later
parser change silently reinterprets rows already on disk, and you lose the
ability to reproduce what the API actually said. Normalization belongs on read.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, text


def cache_key(endpoint: str, params: dict[str, Any]) -> str:
    """sha256 over the endpoint plus its params, canonicalized."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{endpoint}\x00{canonical}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheEntry:
    endpoint: str
    params_hash: str
    status_code: int
    response: str  # verbatim JSON text
    fetched_at: str


class ResponseCache:
    """
    Async facade over the `api_cache` table.

    SQLite work runs in a thread so a slow disk cannot block the event loop that
    the expansion worker and the API share.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.hits = 0
        self.misses = 0

    # -- reads ----------------------------------------------------------------

    async def get_entry(self, endpoint: str, params: dict[str, Any]) -> CacheEntry | None:
        return await asyncio.to_thread(self._get_entry_sync, endpoint, cache_key(endpoint, params))

    async def get(self, endpoint: str, params: dict[str, Any]) -> Any | None:
        """
        The cached payload, or None. A cached `null` and a miss both read as
        None here; use `has()` when that distinction matters -- S2 returns null
        for unknown ids, and that is a real answer worth not re-fetching.
        """
        entry = await self.get_entry(endpoint, params)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(entry.response)

    async def has(self, endpoint: str, params: dict[str, Any]) -> bool:
        return await self.get_entry(endpoint, params) is not None

    # -- writes ---------------------------------------------------------------

    async def put(
        self,
        endpoint: str,
        params: dict[str, Any],
        status_code: int,
        response: Any,
    ) -> None:
        body = json.dumps(response, ensure_ascii=False)
        await asyncio.to_thread(
            self._put_sync, endpoint, cache_key(endpoint, params), status_code, body
        )

    # -- sync internals -------------------------------------------------------

    def _get_entry_sync(self, endpoint: str, key: str) -> CacheEntry | None:
        sql = text(
            "SELECT endpoint, params_hash, status_code, response, fetched_at"
            " FROM api_cache WHERE endpoint = :endpoint AND params_hash = :key"
        )
        with self._engine.connect() as conn:
            row = conn.execute(sql, {"endpoint": endpoint, "key": key}).fetchone()
        return None if row is None else CacheEntry(*row)

    def _put_sync(self, endpoint: str, key: str, status_code: int, body: str) -> None:
        sql = text(
            "INSERT INTO api_cache (endpoint, params_hash, status_code, response, fetched_at)"
            " VALUES (:endpoint, :key, :status, :response, :now)"
            " ON CONFLICT (endpoint, params_hash) DO UPDATE SET"
            "   status_code = excluded.status_code,"
            "   response = excluded.response,"
            "   fetched_at = excluded.fetched_at"
        )
        with self._engine.begin() as conn:
            conn.execute(
                sql,
                {
                    "endpoint": endpoint,
                    "key": key,
                    "status": status_code,
                    "response": body,
                    "now": datetime.now(UTC).isoformat(),
                },
            )


__all__ = ["CacheEntry", "ResponseCache", "cache_key"]
