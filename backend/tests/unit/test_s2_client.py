"""
R0.7 -- S2 client.

Everything here runs against an httpx MockTransport, so the whole file is
offline and deterministic. The behaviours worth pinning:

* **`NEIGHBOR_FIELDS` excludes `authors.hIndex`.** The reference and citation
  endpoints 400 on it, and in the archived prototype that 400 was swallowed by a
  broad except, so every reference fetch silently returned nothing while the run
  reported success. See docs/s2-api-notes.md.
* **A missing field never raises** (CLAUDE.md rule 6). S2 omits year, abstract
  and venue constantly.
* **Batch responses are positionally aligned with the ids sent**, `null`
  included. Zipping them against a filtered list misattributes metadata --
  silently, and to the wrong paper.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

from app.clients.cache import ResponseCache
from app.clients.s2 import (
    NEIGHBOR_FIELDS,
    SEARCH_FIELDS,
    S2Client,
    S2Paper,
    S2TransientError,
    to_paper,
)
from app.db import make_engine
from app.models import CrawlState

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"


def _paper_json(**over: Any) -> dict[str, Any]:
    base = {
        "paperId": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
        "title": "Attention Is All You Need",
        "year": 2017,
        "citationCount": 100000,
        "venue": "NeurIPS",
        "externalIds": {"ArXiv": "1706.03762", "DOI": "10.5555/3295222"},
        "authors": [{"authorId": "1", "name": "Ashish Vaswani"}],
        "publicationTypes": ["JournalArticle"],
        "fieldsOfStudy": ["Computer Science"],
        "abstract": "The dominant sequence transduction models...",
    }
    base.update(over)
    return base


@pytest_asyncio.fixture
async def cache(tmp_path: Path) -> AsyncIterator[ResponseCache]:
    db = tmp_path / "s2.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    yield ResponseCache(engine)
    engine.dispose()


class Recorder:
    """Counts requests and replays scripted responses."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    @property
    def count(self) -> int:
        return len(self.requests)


def make_client(cache: ResponseCache, handler: Any, **kw: Any) -> tuple[S2Client, Recorder]:
    rec = Recorder(handler)
    client = S2Client(
        cache=cache,
        transport=httpx.MockTransport(rec),
        rate=1000.0,  # the limiter has its own tests; do not pay for it here
        # Same reasoning for the retry ladder: production waits real seconds,
        # these tests must not. It is a parameter rather than a constant so the
        # default stays honest -- a hard-coded fast backoff shipped to
        # production is what broke the fixture rebuild against a 429.
        backoff_base=kw.pop("backoff_base", 0.001),
        **kw,
    )
    return client, rec


# --------------------------------------------------------------------------
# Field sets -- the expensive lesson from legacy/
# --------------------------------------------------------------------------


def test_neighbor_fields_exclude_author_h_index() -> None:
    """/references and /citations 400 on authors.hIndex. See docs/s2-api-notes.md."""
    assert "authors.hIndex" not in NEIGHBOR_FIELDS
    assert "authors" in NEIGHBOR_FIELDS


def test_search_fields_include_author_h_index() -> None:
    """/paper/search does accept it, and cold-start author scoring wants it."""
    assert "authors.hIndex" in SEARCH_FIELDS


async def test_get_references_requests_only_supported_fields(cache: ResponseCache) -> None:
    client, rec = make_client(cache, lambda r: httpx.Response(200, json={"data": []}))
    await client.get_references("abc")
    assert "authors.hIndex" not in rec.requests[0].url.params["fields"]


async def test_get_citations_requests_only_supported_fields(cache: ResponseCache) -> None:
    client, rec = make_client(cache, lambda r: httpx.Response(200, json={"data": []}))
    await client.get_citations("abc")
    assert "authors.hIndex" not in rec.requests[0].url.params["fields"]


# --------------------------------------------------------------------------
# search_title
# --------------------------------------------------------------------------


async def test_search_title_returns_stubs(cache: ResponseCache) -> None:
    client, _ = make_client(cache, lambda r: httpx.Response(200, json={"data": [_paper_json()]}))
    stubs = await client.search_title("attention")
    assert len(stubs) == 1
    assert stubs[0].title == "Attention Is All You Need"
    assert stubs[0].year == 2017


async def test_search_title_on_no_results_returns_empty(cache: ResponseCache) -> None:
    client, _ = make_client(cache, lambda r: httpx.Response(200, json={"data": []}))
    assert await client.search_title("nonexistent") == []


async def test_search_title_tolerates_a_missing_data_key(cache: ResponseCache) -> None:
    client, _ = make_client(cache, lambda r: httpx.Response(200, json={}))
    assert await client.search_title("q") == []


# --------------------------------------------------------------------------
# get_papers -- the only metadata path
# --------------------------------------------------------------------------


async def test_get_papers_uses_a_batch_post(cache: ResponseCache) -> None:
    client, rec = make_client(cache, lambda r: httpx.Response(200, json=[_paper_json()]))
    await client.get_papers(["abc"])
    assert rec.requests[0].method == "POST"
    assert "/paper/batch" in str(rec.requests[0].url)


async def test_get_papers_normalizes_into_domain_papers(cache: ResponseCache) -> None:
    client, _ = make_client(cache, lambda r: httpx.Response(200, json=[_paper_json()]))
    (paper,) = await client.get_papers(["abc"])
    assert paper.title == "Attention Is All You Need"
    assert paper.arxiv_id == "1706.03762"
    assert paper.doi == "10.5555/3295222"
    assert paper.venue == "NeurIPS"
    assert paper.crawl_state is CrawlState.METADATA
    assert paper.first_seen_at  # always stamped


async def test_get_papers_drops_nulls_but_keeps_alignment(cache: ResponseCache) -> None:
    """S2 returns null in the slot of an id it does not know."""
    payload = [_paper_json(paperId="a", title="A"), None, _paper_json(paperId="c", title="C")]
    client, _ = make_client(cache, lambda r: httpx.Response(200, json=payload))
    papers = await client.get_papers(["a", "b", "c"])
    assert [p.s2_paper_id for p in papers] == ["a", "c"]


async def test_get_papers_chunks_large_id_lists(cache: ResponseCache) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["ids"]
        return httpx.Response(200, json=[_paper_json(paperId=i, title=i) for i in ids])

    client, rec = make_client(cache, handler, batch_size=5)
    papers = await client.get_papers([f"p{i}" for i in range(12)])
    assert rec.count == 3  # 5 + 5 + 2
    assert len(papers) == 12


async def test_get_papers_with_no_ids_makes_no_request(cache: ResponseCache) -> None:
    client, rec = make_client(cache, lambda r: httpx.Response(200, json=[]))
    assert await client.get_papers([]) == []
    assert rec.count == 0


async def test_a_failing_chunk_does_not_lose_the_others(cache: ResponseCache) -> None:
    """One bad chunk degrades the run; it must not abort it."""
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(500, json={"error": "boom"})
        ids = json.loads(request.content)["ids"]
        return httpx.Response(200, json=[_paper_json(paperId=i, title=i) for i in ids])

    client, _ = make_client(cache, handler, batch_size=2, max_attempts=1)
    papers = await client.get_papers(["a", "b", "c", "d"])
    assert [p.s2_paper_id for p in papers] == ["c", "d"]


# --------------------------------------------------------------------------
# Schema drift -- never raise on a missing field
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing", ["year", "abstract", "venue", "citationCount", "authors", "externalIds"]
)
async def test_a_missing_field_degrades_but_never_raises(
    cache: ResponseCache, missing: str
) -> None:
    payload = _paper_json()
    del payload[missing]
    client, _ = make_client(cache, lambda r: httpx.Response(200, json=[payload]))
    (paper,) = await client.get_papers(["abc"])
    assert paper.s2_paper_id  # survived


async def test_an_unknown_extra_field_is_ignored(cache: ResponseCache) -> None:
    """S2 adds fields; that must never be a client error."""
    client, _ = make_client(
        cache, lambda r: httpx.Response(200, json=[_paper_json(brandNewField={"x": 1})])
    )
    (paper,) = await client.get_papers(["abc"])
    assert paper.title == "Attention Is All You Need"


async def test_a_null_year_survives(cache: ResponseCache) -> None:
    client, _ = make_client(cache, lambda r: httpx.Response(200, json=[_paper_json(year=None)]))
    (paper,) = await client.get_papers(["abc"])
    assert paper.year is None


# --------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------


async def test_get_references_returns_edge_records(cache: ResponseCache) -> None:
    payload = {
        "data": [
            {
                "citedPaper": _paper_json(paperId="ref1", title="Ref"),
                "isInfluential": True,
                "intents": ["methodology"],
            }
        ]
    }
    client, _ = make_client(cache, lambda r: httpx.Response(200, json=payload))
    (rec,) = await client.get_references("citing1")
    assert rec.citing_s2_id == "citing1"
    assert rec.cited_s2_id == "ref1"
    assert rec.is_influential is True
    assert rec.intents == ("methodology",)


async def test_get_citations_reverses_the_direction(cache: ResponseCache) -> None:
    """A citing paper cites us: edge is them -> us, never us -> them."""
    payload = {"data": [{"citingPaper": _paper_json(paperId="cit1", title="Citer")}]}
    client, _ = make_client(cache, lambda r: httpx.Response(200, json=payload))
    (rec,) = await client.get_citations("cited1")
    assert rec.citing_s2_id == "cit1"
    assert rec.cited_s2_id == "cited1"


async def test_edge_rows_with_a_null_paper_are_skipped(cache: ResponseCache) -> None:
    payload = {"data": [{"citedPaper": None}, {"citedPaper": _paper_json(paperId="ok")}]}
    client, _ = make_client(cache, lambda r: httpx.Response(200, json=payload))
    assert [r.cited_s2_id for r in await client.get_references("x")] == ["ok"]


async def test_self_citations_from_s2_are_dropped(cache: ResponseCache) -> None:
    """The CHECK constraint would reject these; drop them before the INSERT."""
    payload = {"data": [{"citedPaper": _paper_json(paperId="same")}]}
    client, _ = make_client(cache, lambda r: httpx.Response(200, json=payload))
    assert await client.get_references("same") == []


# --------------------------------------------------------------------------
# Caching and call accounting -- what R0.8 checks
# --------------------------------------------------------------------------


async def test_a_repeated_search_makes_no_second_request(cache: ResponseCache) -> None:
    client, rec = make_client(cache, lambda r: httpx.Response(200, json={"data": [_paper_json()]}))
    await client.search_title("attention")
    await client.search_title("attention")
    assert rec.count == 1
    assert client.api_calls == 1
    assert client.cache_hits == 1


async def test_a_repeated_batch_makes_no_second_request(cache: ResponseCache) -> None:
    client, rec = make_client(cache, lambda r: httpx.Response(200, json=[_paper_json()]))
    await client.get_papers(["abc"])
    await client.get_papers(["abc"])
    assert rec.count == 1
    assert client.api_calls == 1


async def test_counters_start_at_zero(cache: ResponseCache) -> None:
    client, _ = make_client(cache, lambda r: httpx.Response(200, json={"data": []}))
    assert client.api_calls == 0
    assert client.cache_hits == 0


# --------------------------------------------------------------------------
# Error contract
# --------------------------------------------------------------------------


async def test_persistent_5xx_raises_s2_transient_error(cache: ResponseCache) -> None:
    client, _ = make_client(
        cache, lambda r: httpx.Response(503, json={"error": "down"}), max_attempts=2
    )
    with pytest.raises(S2TransientError):
        await client.search_title("q")


async def test_5xx_then_success_recovers(cache: ResponseCache) -> None:
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"data": [_paper_json()]})

    client, _ = make_client(cache, handler, max_attempts=3)
    assert len(await client.search_title("q")) == 1


async def test_a_404_returns_empty_rather_than_raising(cache: ResponseCache) -> None:
    """The caller writes a STUB; a missing paper is not an error."""
    client, _ = make_client(cache, lambda r: httpx.Response(404, json={"error": "not found"}))
    assert await client.search_title("nope") == []


async def test_a_failed_response_is_not_cached(cache: ResponseCache) -> None:
    """Caching a 503 would poison the cache for as long as it lives."""
    client, _ = make_client(cache, lambda r: httpx.Response(503, json={}), max_attempts=1)
    with pytest.raises(S2TransientError):
        await client.search_title("q")
    assert await cache.has(
        "/paper/search", {"query": "q", "fields": SEARCH_FIELDS, "limit": 10}
    ) is (False)


async def test_the_api_key_is_sent_as_a_header(cache: ResponseCache) -> None:
    client, rec = make_client(
        cache, lambda r: httpx.Response(200, json={"data": []}), api_key="secret-key"
    )
    await client.search_title("q")
    assert rec.requests[0].headers["x-api-key"] == "secret-key"


async def test_no_api_key_header_when_none_configured(cache: ResponseCache) -> None:
    client, rec = make_client(cache, lambda r: httpx.Response(200, json={"data": []}), api_key=None)
    await client.search_title("q")
    assert "x-api-key" not in rec.requests[0].headers


# --------------------------------------------------------------------------
# The arXiv id, and where S2 actually puts it
# --------------------------------------------------------------------------


def _ids(external: dict[str, object] | None) -> str | None:
    paper = to_paper(S2Paper(paperId="p", title="A paper", externalIds=external))
    assert paper is not None
    return paper.arxiv_id


def test_the_arxiv_key_is_used_when_present() -> None:
    assert _ids({"ArXiv": "1706.03762"}) == "1706.03762"


def test_the_arxiv_id_is_recovered_from_the_doi() -> None:
    """
    **The bug this exists for.** S2 frequently omits the `ArXiv` key for papers
    that are plainly on arXiv, and puts the id in the DataCite DOI instead.
    DeepSeek-R1 comes back with exactly this shape:

        {"DBLP": "journals/corr/abs-2501-12948",
         "DOI": "10.48550/arXiv.2501.12948",
         "CorpusId": 284488789}

    Reading only `ArXiv` left `arxiv_id` null, so nothing joined to the local
    arXiv snapshot, so `primary_arxiv_category` was null, so `topic_filter`
    fell past every category rule and quarantined a core ML paper as
    FIELD_CS_ONLY -- with a reason code that pointed nowhere near the cause.
    """
    assert (
        _ids(
            {
                "DBLP": "journals/corr/abs-2501-12948",
                "DOI": "10.48550/arXiv.2501.12948",
                "CorpusId": 284488789,
            }
        )
        == "2501.12948"
    )


def test_the_explicit_key_wins_over_the_doi() -> None:
    """A fallback, not a replacement: when S2 says ArXiv, that is the answer."""
    assert _ids({"ArXiv": "2210.03821", "DOI": "10.48550/arXiv.9999.99999"}) == "2210.03821"


def test_the_doi_match_is_case_insensitive() -> None:
    """S2 returns the capitalisation inconsistently -- arxiv, arXiv, ArXiv."""
    assert _ids({"DOI": "10.48550/arxiv.2210.03821"}) == "2210.03821"


def test_an_ordinary_doi_yields_no_arxiv_id() -> None:
    """
    Vygotsky's *Thinking and Speech* arrives as a Springer chapter. Inventing
    an arXiv id here would send it to the snapshot to be joined against nothing,
    and a wrong id is worse than a missing one.
    """
    assert _ids({"DOI": "10.1007/978-981-10-4625-4_21"}) is None


def test_no_external_ids_at_all_is_not_an_error() -> None:
    assert _ids(None) is None
    assert _ids({}) is None
