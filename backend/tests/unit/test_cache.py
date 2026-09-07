"""
R0.6 -- response cache.

Two properties decide whether R0.8's checkpoint (second run makes zero network
calls) can ever pass:

* **The key is stable across dict ordering.** Python preserves insertion order,
  so `{"a":1,"b":2}` and `{"b":2,"a":1}` serialize differently. If the key
  followed insertion order, an identical request built by a different code path
  would miss, and the cache would look broken in a way that is very hard to see.
* **The response is stored verbatim.** Normalizing on write means a later parser
  change silently reinterprets responses already on disk, and you can never
  reproduce what the API actually said.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from alembic import command
from alembic.config import Config

from app.clients.cache import ResponseCache, cache_key
from app.db import make_engine

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"


@pytest_asyncio.fixture
async def cache(tmp_path: Path) -> AsyncIterator[ResponseCache]:
    db = tmp_path / "cache.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    yield ResponseCache(engine)
    engine.dispose()


# --------------------------------------------------------------------------
# cache_key
# --------------------------------------------------------------------------


def test_key_is_stable_across_dict_ordering() -> None:
    """The bug that would make R0.8's zero-network-calls checkpoint unreachable."""
    assert cache_key("/paper/search", {"q": "bert", "limit": 10}) == cache_key(
        "/paper/search", {"limit": 10, "q": "bert"}
    )


def test_key_is_deterministic_across_calls() -> None:
    a = cache_key("/paper/batch", {"ids": ["x", "y"]})
    b = cache_key("/paper/batch", {"ids": ["x", "y"]})
    assert a == b


def test_distinct_params_produce_distinct_keys() -> None:
    assert cache_key("/paper/search", {"q": "bert"}) != cache_key("/paper/search", {"q": "gpt"})


def test_distinct_endpoints_produce_distinct_keys() -> None:
    assert cache_key("/paper/search", {"q": "bert"}) != cache_key("/paper/match", {"q": "bert"})


def test_list_order_is_significant() -> None:
    """['x','y'] and ['y','x'] are different batch requests, not the same one."""
    assert cache_key("/paper/batch", {"ids": ["x", "y"]}) != cache_key(
        "/paper/batch", {"ids": ["y", "x"]}
    )


def test_key_is_a_sha256_hex_digest() -> None:
    key = cache_key("/paper/search", {"q": "bert"})
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_empty_params_are_valid() -> None:
    assert len(cache_key("/health", {})) == 64


# --------------------------------------------------------------------------
# put / get round-trip
# --------------------------------------------------------------------------


async def test_put_then_get_round_trips(cache: ResponseCache) -> None:
    payload = {"data": [{"paperId": "abc", "title": "Attention Is All You Need"}]}
    await cache.put("/paper/search", {"q": "attention"}, 200, payload)
    assert await cache.get("/paper/search", {"q": "attention"}) == payload


async def test_get_returns_none_on_a_miss(cache: ResponseCache) -> None:
    assert await cache.get("/paper/search", {"q": "never fetched"}) is None


async def test_a_miss_is_distinguishable_from_a_cached_null(cache: ResponseCache) -> None:
    """S2 returns null for unknown ids; that is a real answer worth caching."""
    await cache.put("/paper/xyz", {}, 404, None)
    assert await cache.get("/paper/xyz", {}) is None
    assert await cache.has("/paper/xyz", {}) is True
    assert await cache.has("/paper/never", {}) is False


async def test_response_is_stored_verbatim(cache: ResponseCache) -> None:
    """Never normalize on write (PLAN.md M4)."""
    raw = {"z": 1, "a": {"nested": [3, 2, 1]}, "unexpected_field": "keep me"}
    await cache.put("/paper/batch", {"ids": ["q"]}, 200, raw)
    assert await cache.get("/paper/batch", {"ids": ["q"]}) == raw


async def test_stored_row_keeps_the_status_code(cache: ResponseCache) -> None:
    await cache.put("/paper/x", {}, 404, None)
    entry = await cache.get_entry("/paper/x", {})
    assert entry is not None
    assert entry.status_code == 404


async def test_reput_overwrites_rather_than_violating_the_unique_index(
    cache: ResponseCache,
) -> None:
    await cache.put("/paper/search", {"q": "a"}, 200, {"v": 1})
    await cache.put("/paper/search", {"q": "a"}, 200, {"v": 2})
    assert await cache.get("/paper/search", {"q": "a"}) == {"v": 2}


async def test_entries_from_different_params_do_not_collide(cache: ResponseCache) -> None:
    await cache.put("/paper/search", {"q": "a"}, 200, {"which": "a"})
    await cache.put("/paper/search", {"q": "b"}, 200, {"which": "b"})
    assert await cache.get("/paper/search", {"q": "a"}) == {"which": "a"}
    assert await cache.get("/paper/search", {"q": "b"}) == {"which": "b"}


async def test_unicode_and_latex_survive_the_round_trip(cache: ResponseCache) -> None:
    """S2 titles contain LaTeX and accented names constantly."""
    payload = {"title": r"Attention $\alpha$ -- Bengio, Schölkopf, 北京"}
    await cache.put("/paper/x", {}, 200, payload)
    assert await cache.get("/paper/x", {}) == payload


async def test_stored_json_is_readable_without_the_cache_layer(cache: ResponseCache) -> None:
    """The cache table is a debugging surface; keep it plain JSON."""
    await cache.put("/paper/x", {}, 200, {"a": 1})
    entry = await cache.get_entry("/paper/x", {})
    assert entry is not None
    assert json.loads(entry.response) == {"a": 1}


# --------------------------------------------------------------------------
# Call accounting -- R0.8's checkpoint reads these
# --------------------------------------------------------------------------


async def test_cache_counts_hits_and_misses(cache: ResponseCache) -> None:
    await cache.put("/paper/x", {}, 200, {"a": 1})
    await cache.get("/paper/x", {})
    await cache.get("/paper/y", {})
    assert cache.hits == 1
    assert cache.misses == 1
