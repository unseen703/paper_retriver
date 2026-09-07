"""
Semantic Scholar Graph API client.

Layer order, innermost out: rate limit -> cache lookup -> httpx -> tenacity ->
Pydantic -> normalize to dataclass. See docs/s2-api-notes.md for the API
contract this is written against.

Two things here are load-bearing and easy to get wrong.

**`NEIGHBOR_FIELDS` is not `SEARCH_FIELDS`.** `/paper/{id}/references` and
`/paper/{id}/citations` reject `authors.hIndex` with a 400. In the archived
prototype that 400 was swallowed, so every reference fetch silently returned
nothing while the run reported success. Two constants, deliberately.

**Every Pydantic field is Optional** (CLAUDE.md rule 6). S2 omits `year`,
`abstract` and `venue` constantly. A missing field degrades a score and logs
SCHEMA_DRIFT; it never raises.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.clients.cache import ResponseCache
from app.clients.rate_limit import TokenBucket
from app.logging_setup import log_s2_request
from app.models import Author, CrawlState, Paper, PaperStub

logger = logging.getLogger(__name__)

BASE_URL = "https://api.semanticscholar.org/graph/v1"

# `authors.name` is NOT redundant next to `authors`, and leaving it out is a
# silent data-loss bug rather than a cosmetic one. Naming ANY author sub-field
# replaces S2's default author projection instead of extending it, so
#   authors,authors.hIndex        -> [{"authorId": "...", "hIndex": 26}]
#   authors.name,authors.hIndex   -> [{"authorId": "...", "name": "Ashish
#                                      Vaswani", "hIndex": 26}]
# Both verified live against /paper/search. Without the name, `to_paper` drops
# every author (it requires both id and name), so `Paper.authors` came back
# empty for every fetch -- which left `authors` and `paper_authors` at zero
# rows, made dedup's canonical key fall back to its no-author variant on every
# comparison, and would have shown an empty byline in R1.19's inspector.
SEARCH_FIELDS = (
    "paperId,corpusId,title,abstract,year,publicationDate,venue,citationCount,"
    "referenceCount,influentialCitationCount,externalIds,publicationTypes,"
    "fieldsOfStudy,authors.name,authors.hIndex"
)

# The /paper/{id}/references and /paper/{id}/citations endpoints support a
# NARROWER field set. Asking them for authors.hIndex returns
#   {"error":"Unrecognized or unsupported fields: [authors.hIndex]"}
# as a 400. Keep neighbour fetches on the supported set -- and note that bare
# `authors` DOES include names, because no sub-field is named to displace the
# default projection. That asymmetry is why only SEARCH_FIELDS needed the fix.
NEIGHBOR_FIELDS = (
    "paperId,corpusId,title,abstract,year,publicationDate,venue,citationCount,"
    "referenceCount,influentialCitationCount,externalIds,publicationTypes,"
    "fieldsOfStudy,authors"
)

# S2 documents 500 ids per POST /paper/batch. Smaller chunks mean one bad id
# poisons less, and a chunk failure costs less to retry.
BATCH_SIZE = 100
MAX_NEIGHBORS = 1000

# Ceiling on an honoured `Retry-After`, in seconds. S2 can answer a 429 with a
# value in the thousands; obeying it literally would hold an HTTP request for
# that long, since nothing above the client imposes a deadline. Five attempts
# at 10s each is ~40s of waiting before the caller gets its 503, which is long
# enough to ride out a burst and short enough that a browser has not given up.
MAX_RETRY_AFTER = 10.0


class S2TransientError(RuntimeError):
    """5xx that survived its retries. The caller continues; it does not abort."""


class CacheMiss(RuntimeError):
    """Raised only by CachedOnlyS2Client (R0.9) when a fixture is absent."""


# ---------------------------------------------------------------------------
# Wire models -- every field Optional, extras ignored
# ---------------------------------------------------------------------------


class S2Author(BaseModel):
    model_config = ConfigDict(extra="ignore")
    authorId: str | None = None
    name: str | None = None
    hIndex: int | None = None


class S2Paper(BaseModel):
    model_config = ConfigDict(extra="ignore")
    paperId: str | None = None
    corpusId: int | None = None
    title: str | None = None
    abstract: str | None = None
    year: int | None = None
    publicationDate: str | None = None
    venue: str | None = None
    citationCount: int | None = None
    referenceCount: int | None = None
    influentialCitationCount: int | None = None
    externalIds: dict[str, Any] | None = None
    publicationTypes: list[str] | None = None
    fieldsOfStudy: list[str] | None = None
    authors: list[S2Author] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    """A citation pair as S2 reports it, before ids are resolved to rows."""

    citing_s2_id: str
    cited_s2_id: str
    is_influential: bool = False
    intents: tuple[str, ...] = ()
    # The neighbour's own metadata, so a STUB can be written without a second call.
    paper: Paper | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _external(ids: dict[str, Any] | None, key: str) -> str | None:
    if not ids:
        return None
    value = ids.get(key)
    return None if value is None else str(value)


def to_paper(raw: S2Paper, *, crawl_state: CrawlState = CrawlState.METADATA) -> Paper | None:
    """Normalize a wire record. Returns None if it lacks the two required fields."""
    if not raw.paperId or not raw.title:
        logger.info("SCHEMA_DRIFT: record without paperId or title: %s", raw.paperId)
        return None
    return Paper(
        s2_paper_id=raw.paperId,
        title=raw.title,
        first_seen_at=_now(),
        s2_corpus_id=raw.corpusId,
        abstract=raw.abstract,
        year=raw.year,
        publication_date=raw.publicationDate,
        venue=raw.venue,
        citation_count=raw.citationCount or 0,
        reference_count=raw.referenceCount or 0,
        influential_citation_count=raw.influentialCitationCount or 0,
        doi=_external(raw.externalIds, "DOI"),
        arxiv_id=_external(raw.externalIds, "ArXiv"),
        authors=tuple((a.authorId, a.name) for a in raw.authors if a.authorId and a.name),
        s2_fields=tuple(raw.fieldsOfStudy or ()),
        publication_types=tuple(raw.publicationTypes or ()),
        crawl_state=crawl_state,
        metadata_fetched_at=_now() if crawl_state is CrawlState.METADATA else None,
    )


def to_stub(raw: S2Paper) -> PaperStub | None:
    if not raw.paperId or not raw.title:
        return None
    return PaperStub(
        s2_paper_id=raw.paperId,
        title=raw.title,
        year=raw.year,
        citation_count=raw.citationCount,
        venue=raw.venue,
        external_ids=tuple(sorted((k, str(v)) for k, v in (raw.externalIds or {}).items())),
        # Unlike `to_paper`, a missing authorId is kept: this byline is only
        # ever displayed, never joined, and dropping the unidentified authors
        # would silently show the user a truncated author list.
        authors=tuple(a.name for a in raw.authors if a.name),
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class S2Client:
    def __init__(
        self,
        cache: ResponseCache,
        *,
        api_key: str | None = None,
        rate: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        batch_size: int = BATCH_SIZE,
        max_attempts: int = 5,
        timeout: float = 30.0,
        backoff_base: float = 1.0,
    ) -> None:
        self._cache = cache
        self._bucket = TokenBucket(rate=rate)
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        # Seconds for the first retry; doubles per attempt. Tests pass a tiny
        # value so the retry paths stay fast. It must NOT be scaled down by
        # default: a hard-coded 0.01 multiplier here made the whole backoff
        # ladder 10ms->160ms, which is no backoff at all against a 429, and
        # made the fixture rebuild fail on S2's rate limiter.
        self._backoff_base = backoff_base
        headers = {"x-api-key": api_key} if api_key else {}
        self._http = httpx.AsyncClient(
            base_url=BASE_URL, headers=headers, transport=transport, timeout=timeout
        )
        self.api_calls = 0
        self.cache_hits = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- transport ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any | None:
        """
        One logical call. Returns the decoded payload, None for 404/null.

        The cache is consulted before the rate limiter is paid, so a fully
        cached run costs no wall-clock time -- which is what makes R0.8's
        checkpoint observable rather than merely true.
        """
        key_params: dict[str, Any] = dict(params or {})
        if json_body is not None:
            key_params["__body"] = json_body

        started = time.monotonic()
        if await self._cache.has(path, key_params):
            self.cache_hits += 1
            payload = await self._cache.get(path, key_params)
            log_s2_request(
                endpoint=path,
                cache_hit=True,
                status=None,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            return payload

        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            await self._bucket.acquire()
            self.api_calls += 1
            call_started = time.monotonic()
            try:
                response = await self._http.request(method, path, params=params, json=json_body)
            except httpx.HTTPError as exc:  # network-level
                log_s2_request(
                    endpoint=path,
                    cache_hit=False,
                    status=None,
                    duration_ms=(time.monotonic() - call_started) * 1000,
                )
                last_error = exc
                await self._backoff(attempt)
                continue
            log_s2_request(
                endpoint=path,
                cache_hit=False,
                status=response.status_code,
                duration_ms=(time.monotonic() - call_started) * 1000,
            )
            if response.status_code == 404:
                await self._cache.put(path, key_params, 404, None)
                return None

            if response.status_code == 429:
                await self._backoff(attempt, response.headers.get("Retry-After"))
                last_error = S2TransientError("429 rate limited")
                continue

            if response.status_code >= 500:
                last_error = S2TransientError(f"{response.status_code} from {path}")
                await self._backoff(attempt)
                continue

            if response.status_code >= 400:
                # 400 here almost always means an unsupported field. Surface it
                # loudly rather than returning empty -- that is exactly the bug
                # that hid the authors.hIndex problem for a day.
                raise S2TransientError(f"{response.status_code} from {path}: {response.text[:200]}")

            payload = response.json()
            # Only successes are cached. Caching a 503 poisons the cache for as
            # long as it lives.
            await self._cache.put(path, key_params, response.status_code, payload)
            return payload

        raise S2TransientError(f"{path} failed after {self._max_attempts} attempts: {last_error}")

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if attempt >= self._max_attempts:
            return
        if retry_after:
            try:
                honoured = float(retry_after)
            except ValueError:
                honoured = None
            if honoured is not None:
                # Capped at MAX_RETRY_AFTER, and scaled by the same base as the
                # exponential ladder. Both matter now that these calls sit
                # behind HTTP: S2 can answer a 429 with `Retry-After: 3600`, and
                # obeying it literally would park a request handler for a
                # minute per attempt with no deadline above it -- roughly four
                # minutes before the client sees its 503. It also has to respect
                # backoff_base, or a test that ever exercises this branch sleeps
                # for real seconds while the ladder beside it does not.
                await asyncio.sleep(min(honoured, MAX_RETRY_AFTER) * self._backoff_base)
                return
        await asyncio.sleep(min(2.0 ** (attempt - 1), 30.0) * self._backoff_base)

    # -- public API ---------------------------------------------------------

    async def search_title(self, q: str, limit: int = 10) -> list[PaperStub]:
        params = {"query": q, "fields": SEARCH_FIELDS, "limit": limit}
        payload = await self._request("GET", "/paper/search", params=params)
        rows = (payload or {}).get("data") or []
        stubs = [to_stub(S2Paper.model_validate(r)) for r in rows if r]
        return [s for s in stubs if s is not None]

    async def get_papers(self, s2_ids: list[str], fields: str = SEARCH_FIELDS) -> list[Paper]:
        """
        The ONLY metadata path. Chunks into batch POSTs, so callers cannot
        accidentally do N+1 fetching.

        A chunk that fails is logged and skipped: one bad chunk degrades the
        run, it does not abort it.
        """
        papers: list[Paper] = []
        for start in range(0, len(s2_ids), self._batch_size):
            chunk = s2_ids[start : start + self._batch_size]
            try:
                payload = await self._request(
                    "POST", "/paper/batch", params={"fields": fields}, json_body={"ids": chunk}
                )
            except S2TransientError as exc:
                logger.warning("batch of %d ids failed, skipping: %s", len(chunk), exc)
                continue
            # Positionally aligned with `chunk`, null where S2 knows nothing.
            for raw in payload or []:
                if raw is None:
                    continue
                paper = to_paper(S2Paper.model_validate(raw))
                if paper is not None:
                    papers.append(paper)
        return papers

    async def get_references(self, s2_id: str, limit: int = 200) -> list[EdgeRecord]:
        return await self._edges(s2_id, "references", "citedPaper", limit)

    async def get_citations(self, s2_id: str, limit: int = MAX_NEIGHBORS) -> list[EdgeRecord]:
        return await self._edges(s2_id, "citations", "citingPaper", limit)

    async def _edges(self, s2_id: str, kind: str, node_key: str, limit: int) -> list[EdgeRecord]:
        payload = await self._request(
            "GET",
            f"/paper/{s2_id}/{kind}",
            params={"fields": NEIGHBOR_FIELDS, "limit": min(limit, MAX_NEIGHBORS)},
        )
        out: list[EdgeRecord] = []
        for row in (payload or {}).get("data") or []:
            raw = row.get(node_key)
            if not raw:
                continue
            neighbour = to_paper(S2Paper.model_validate(raw), crawl_state=CrawlState.STUB)
            if neighbour is None:
                continue
            citing, cited = (
                (s2_id, neighbour.s2_paper_id)
                if node_key == "citedPaper"
                else (neighbour.s2_paper_id, s2_id)
            )
            # S2 does return self-citations; the CHECK constraint would reject
            # them, so drop them here rather than at the INSERT.
            if citing == cited:
                continue
            out.append(
                EdgeRecord(
                    citing_s2_id=citing,
                    cited_s2_id=cited,
                    is_influential=bool(row.get("isInfluential")),
                    intents=tuple(row.get("intents") or ()),
                    paper=neighbour,
                )
            )
        return out

    async def get_authors(self, s2_ids: list[str]) -> list[Author]:
        """Where h_index actually comes from -- it is absent on neighbour fetches."""
        authors: list[Author] = []
        for start in range(0, len(s2_ids), self._batch_size):
            chunk = s2_ids[start : start + self._batch_size]
            try:
                payload = await self._request(
                    "POST",
                    "/author/batch",
                    params={"fields": "authorId,name,hIndex,citationCount,paperCount"},
                    json_body={"ids": chunk},
                )
            except S2TransientError as exc:
                logger.warning("author batch failed, skipping: %s", exc)
                continue
            for raw in payload or []:
                if not raw or not raw.get("authorId"):
                    continue
                authors.append(
                    Author(
                        s2_author_id=str(raw["authorId"]),
                        name=raw.get("name") or "",
                        h_index=raw.get("hIndex"),
                        citation_count=raw.get("citationCount"),
                        paper_count=raw.get("paperCount"),
                        fetched_at=_now(),
                    )
                )
        return authors


class CachedOnlyS2Client(S2Client):
    """
    Reads the cache and nothing else. Any miss raises `CacheMiss`.

    This is what makes the committed fixture DB a hard offline guarantee rather
    than a convention. A mock transport can be bypassed by a code path that
    builds its own client; this cannot, because there is no transport to reach.
    A test that starts raising CacheMiss is telling you the fixture needs
    extending -- rerun scripts/build_fixture_cache.py -- not that it should
    quietly fall back to the network.
    """

    def __init__(self, cache: ResponseCache, **kwargs: Any) -> None:
        kwargs.pop("transport", None)
        super().__init__(cache, transport=_ForbiddenTransport(), **kwargs)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any | None:
        key_params: dict[str, Any] = dict(params or {})
        if json_body is not None:
            key_params["__body"] = json_body
        if not await self._cache.has(path, key_params):
            raise CacheMiss(f"{method} {path} {key_params} is not in the fixture cache")
        self.cache_hits += 1
        return await self._cache.get(path, key_params)


class _ForbiddenTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise CacheMiss(f"network access is disabled: {request.method} {request.url}")


__all__ = [
    "BATCH_SIZE",
    "BASE_URL",
    "CachedOnlyS2Client",
    "NEIGHBOR_FIELDS",
    "SEARCH_FIELDS",
    "CacheMiss",
    "EdgeRecord",
    "S2Client",
    "S2Paper",
    "S2TransientError",
    "to_paper",
    "to_stub",
]
