"""R6.1 -- embedding store and S2 embedding fetch. Offline; expectations by hand."""

from __future__ import annotations

import math
from pathlib import Path

import httpx
import pytest

from app.clients.s2 import EMBEDDING_FIELDS
from app.models.domain import CrawlState, Paper
from app.services.embeddings import EmbeddingStore, backfill, text_hash
from tests.unit.test_s2_client import cache, make_client  # noqa: F401

DIM = 4


def _store() -> EmbeddingStore:
    return EmbeddingStore(dim=DIM)


def test_cosine_ordering_by_hand() -> None:
    s = _store()
    s.put(1, [1, 0, 0, 0], "a")
    s.put(2, [1, 1, 0, 0], "b")  # cos with x-axis = 1/sqrt(2)
    s.put(3, [0, 1, 0, 0], "c")  # orthogonal
    s.put(4, [-1, 0, 0, 0], "d")  # opposite
    got = s.top_k([2, 0, 0, 0], 4)  # scale of the query must not matter
    assert [pid for pid, _ in got] == [1, 2, 3, 4]
    assert [round(sc, 6) for _, sc in got] == [1.0, round(1 / math.sqrt(2), 6), 0.0, -1.0]


def test_ties_break_on_paper_id_regardless_of_insertion_order() -> None:
    a, b = _store(), _store()
    for pid in (9, 3, 5):
        a.put(pid, [1, 0, 0, 0], "h")
    for pid in (5, 9, 3):
        b.put(pid, [1, 0, 0, 0], "h")
    assert a.top_k([1, 0, 0, 0], 3) == b.top_k([1, 0, 0, 0], 3)
    assert [p for p, _ in a.top_k([1, 0, 0, 0], 3)] == [3, 5, 9]


def test_exclude_k_and_degenerate_queries() -> None:
    s = _store()
    s.put(1, [1, 0, 0, 0], "a")
    s.put(2, [0, 1, 0, 0], "b")
    assert [p for p, _ in s.top_k([1, 0, 0, 0], 5, exclude=[1])] == [2]
    assert s.top_k([1, 0, 0, 0], 0) == []
    assert s.top_k([0, 0, 0, 0], 3) == []
    assert s.top_k([1, 0, 0], 3) == []  # wrong dimension
    assert _store().top_k([1, 0, 0, 0], 3) == []


def test_bad_vectors_are_refused_not_raised() -> None:
    s = _store()
    assert not s.put(1, [1, 2, 3], "h")
    assert not s.put(2, [0, 0, 0, 0], "h")
    assert len(s) == 0


def test_cache_invalidation_follows_the_text() -> None:
    s = _store()
    h1 = text_hash("T", "abstract")
    assert s.needs_embedding(1, h1)
    s.put(1, [1, 0, 0, 0], h1)
    assert not s.needs_embedding(1, h1)
    assert s.needs_embedding(1, text_hash("T", "abstract, revised"))
    assert text_hash("T", None) == text_hash("T", "")
    assert text_hash("a", "b") != text_hash("ab", "")


def test_save_load_round_trip_and_byte_determinism(tmp_path: Path) -> None:
    s = _store()
    for pid, vec in ((7, [1, 2, 0, 0]), (2, [0, 0, 3, 4])):
        s.put(pid, vec, f"h{pid}")
    s.save(tmp_path / "emb")
    first = (tmp_path / "emb.npy").read_bytes(), (tmp_path / "emb.json").read_bytes()
    loaded = EmbeddingStore.load(tmp_path / "emb", dim=DIM)
    assert len(loaded) == 2 and not loaded.needs_embedding(7, "h7")
    assert loaded.top_k([0, 0, 3, 4], 1)[0][0] == 2
    loaded.save(tmp_path / "emb2")
    assert first == ((tmp_path / "emb2.npy").read_bytes(), (tmp_path / "emb2.json").read_bytes())


def test_missing_or_inconsistent_files_load_empty(tmp_path: Path) -> None:
    assert len(EmbeddingStore.load(tmp_path / "nope", dim=DIM)) == 0
    s = _store()
    s.put(1, [1, 0, 0, 0], "h")
    s.save(tmp_path / "emb")
    (tmp_path / "emb.json").write_text("{not json")
    assert len(EmbeddingStore.load(tmp_path / "emb", dim=DIM)) == 0
    s.save(tmp_path / "emb")
    assert len(EmbeddingStore.load(tmp_path / "emb", dim=8)) == 0  # dim mismatch


@pytest.mark.asyncio
async def test_get_embeddings_tolerates_missing_fields(cache) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fields"] == EMBEDDING_FIELDS
        return httpx.Response(
            200,
            json=[
                {"paperId": "a", "embedding": {"model": "specter_v2", "vector": [0.1, 0.2]}},
                {"paperId": "b", "embedding": None},
                {"paperId": "c"},
                None,
                {"paperId": "d", "embedding": {"model": "specter_v2", "vector": []}},
            ],
        )

    client, rec = make_client(cache, handler)
    assert await client.get_embeddings(["a", "b", "c", "x", "d"]) == {"a": [0.1, 0.2]}
    assert rec.count == 1
    assert await client.get_embeddings([]) == {}


def _paper(pid: int, s2: str, abstract: str | None = "abs", stub: bool = False) -> Paper:
    return Paper(
        s2_paper_id=s2,
        title=f"t{pid}",
        first_seen_at="2026-01-01",
        id=pid,
        abstract=abstract,
        crawl_state=CrawlState.STUB if stub else CrawlState.METADATA,
    )


@pytest.mark.asyncio
async def test_backfill_fetches_only_what_is_missing_or_stale() -> None:
    s = _store()
    fresh = _paper(1, "a")
    s.put(1, [1, 0, 0, 0], text_hash(fresh.title, fresh.abstract))
    changed = _paper(2, "b", abstract="new")
    s.put(2, [0, 1, 0, 0], text_hash(changed.title, "old"))
    calls: list[list[str]] = []

    async def fetch(ids: list[str]) -> dict[str, list[float]]:
        calls.append(ids)
        return {"b": [0, 0, 1, 0], "d": [0, 0, 0, 1]}  # "c" has no vector

    papers = [_paper(4, "d"), fresh, changed, _paper(3, "c"), _paper(9, "z", None, stub=True)]
    res = await backfill(s, papers, fetch)
    assert calls == [["b", "c", "d"]]  # sorted, stub and fresh excluded
    assert (res.requested, res.stored, res.unavailable, res.skipped_fresh) == (3, 2, 1, 1)
    assert 3 not in s and 9 not in s
    assert s.top_k([0, 0, 1, 0], 1)[0][0] == 2  # the stale row was replaced


@pytest.mark.asyncio
async def test_backfill_with_nothing_to_do_makes_no_call() -> None:
    async def fetch(ids: list[str]) -> dict[str, list[float]]:
        raise AssertionError("must not be called")

    res = await backfill(_store(), [_paper(1, "a", stub=True)], fetch)
    assert (res.requested, res.stored) == (0, 0)
