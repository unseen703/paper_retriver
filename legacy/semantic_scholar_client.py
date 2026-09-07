"""
semantic_scholar_client.py
---------------------------
Resilient wrapper around the Semantic Scholar Graph API.

Rate limiting: authenticated key (S2_API_KEY in .env) gives 1 req/sec.
_throttle() enforces this pre-emptively; exponential backoff handles 429s.
Batch endpoint (POST /paper/batch) fetches up to 500 papers in one request.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.semanticscholar.org/graph/v1"
RECS_URL = "https://api.semanticscholar.org/recommendations/v1"

PAPER_FIELDS = (
    "paperId,title,abstract,year,venue,authors,citationCount,"
    "fieldsOfStudy,externalIds,tldr,embedding.specter_v2"
)
SEARCH_FIELDS = "paperId,title,year,authors,authors.hIndex,citationCount,fieldsOfStudy,externalIds,venue"

# The /paper/{id}/references and /paper/{id}/citations endpoints support a
# NARROWER field set than /paper/search and /paper/{id}. Asking them for
# authors.hIndex returns
#   {"error":"Unrecognized or unsupported fields: [authors.hIndex]"}
# as a 400 -- which _fetch_neighbors swallows, silently yielding zero
# references for every paper. Keep neighbour fetches on the supported set.
NEIGHBOR_FIELDS = "paperId,title,year,authors,citationCount,fieldsOfStudy,externalIds,venue"

# S2 accepts up to 500 ids per POST /paper/batch, but small batches keep a
# single bad id from poisoning a large chunk and give steadier progress.
BATCH_SIZE = 5


class SemanticScholarClient:
    def __init__(self, api_key: Optional[str] = None, max_retries: int = 4,
                 timeout: float = 15.0, cache=None):
        self.api_key = api_key or os.environ.get("S2_API_KEY")
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({"x-api-key": self.api_key})
        self.max_retries = max_retries
        self.timeout = timeout
        self.cache = cache
        self._last_request_time: float = 0.0
        # 1 req/sec when authenticated, otherwise be conservative
        self._min_interval: float = 1.05 if self.api_key else 3.0

    # ---- rate limiting -------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_time
        wait = self._min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_time = time.monotonic()

    # ---- low-level request with backoff on 429s ------------------------------

    def _request(self, method: str, url: str, cache_key: Optional[str] = None,
                 **kwargs) -> dict:
        if cache_key and self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        self._throttle()
        delay = 2.0
        resp = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.exceptions.RequestException as exc:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if resp.status_code == 429:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                self._last_request_time = time.monotonic()
                continue
            if resp.status_code >= 500:
                if attempt == self.max_retries - 1:
                    resp.raise_for_status()
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            resp.raise_for_status()
            data = resp.json()
            if cache_key and self.cache:
                self.cache.set(cache_key, data)
            return data

        if resp is not None:
            resp.raise_for_status()
        return {}

    # ---- title resolution ----------------------------------------------------

    def search_paper_by_title(self, title: str) -> Optional[dict]:
        """Resolve a title string to the best-matching S2 paper record."""
        # Check cache first (only successful hits are stored here)
        if self.cache:
            cached = self.cache.get(f"s2:match:{title}")
            if cached is not None:
                candidates = cached.get("data") or []
                if candidates:
                    return candidates[0]

        # /match endpoint — cache manually only on a real hit (empty results are not cached)
        try:
            data = self._request(
                "GET",
                f"{BASE_URL}/paper/search/match",
                params={"query": title, "fields": SEARCH_FIELDS},
            )
            candidates = data.get("data") or []
            if candidates:
                if self.cache:
                    self.cache.set(f"s2:match:{title}", data)
                return candidates[0]
        except Exception as exc:
            logger.warning("S2 /match failed for %r: %s — falling back to /search", title, exc)

        # Fallback — wrapped so one title's failure doesn't abort the whole seed loop
        try:
            fallback_key = f"s2:search:{title}"
            fallback = self._request(
                "GET",
                f"{BASE_URL}/paper/search",
                cache_key=fallback_key,
                params={"query": title, "fields": SEARCH_FIELDS, "limit": 1},
            )
            results = fallback.get("data") or []
            return results[0] if results else None
        except Exception as exc:
            logger.warning("S2 /search also failed for %r: %s — no match found", title, exc)
            return None

    # ---- paper detail / graph traversal --------------------------------------

    def get_paper(self, paper_id: str) -> dict:
        key = f"s2:paper:{paper_id}"
        return self._request(
            "GET", f"{BASE_URL}/paper/{paper_id}",
            cache_key=key,
            params={"fields": PAPER_FIELDS},
        )

    def get_references(self, paper_id: str, limit: int = 100) -> list[dict]:
        data = self._request(
            "GET",
            f"{BASE_URL}/paper/{paper_id}/references",
            params={"fields": NEIGHBOR_FIELDS, "limit": min(limit, 1000)},
        )
        return [d["citedPaper"] for d in data.get("data", []) if d.get("citedPaper")]

    def get_citations(self, paper_id: str, limit: int = 100) -> list[dict]:
        data = self._request(
            "GET",
            f"{BASE_URL}/paper/{paper_id}/citations",
            params={"fields": NEIGHBOR_FIELDS, "limit": min(limit, 1000)},
        )
        return [d["citingPaper"] for d in data.get("data", []) if d.get("citingPaper")]

    def batch_get_papers(self, ids: list[str],
                         fields: Optional[str] = None,
                         batch_size: int = BATCH_SIZE) -> list[Optional[dict]]:
        """
        Fetch paper metadata through POST /paper/batch in small chunks.

        Each chunk is isolated: if one chunk fails (a bad id, a transient
        400/500), it is logged and skipped so the remaining chunks still
        contribute their results, rather than the whole hydration run
        dying on a single bad batch.
        """
        if not ids:
            return []
        fields = fields or PAPER_FIELDS
        results: list[Optional[dict]] = []
        for i in range(0, len(ids), batch_size):
            chunk = ids[i: i + batch_size]
            try:
                data = self._request(
                    "POST",
                    f"{BASE_URL}/paper/batch",
                    params={"fields": fields},
                    json={"ids": chunk},
                )
            except Exception as exc:
                logger.warning("batch_get_papers: chunk %s failed, skipping: %s", chunk, exc)
                continue
            # API returns an array in the same order; missing papers are null
            results.extend(data if isinstance(data, list) else [])
        return results

    # ---- recommendations endpoint --------------------------------------------

    def recommend(self, positive_ids: list[str], negative_ids: list[str],
                  limit: int = 40) -> list[dict]:
        if not positive_ids:
            return []
        payload = {"positivePaperIds": positive_ids, "negativePaperIds": negative_ids}
        data = self._request(
            "POST",
            f"{RECS_URL}/papers",
            params={"fields": SEARCH_FIELDS, "limit": limit},
            json=payload,
        )
        return data.get("recommendedPapers", [])


def s2_record_to_kwargs(record: dict) -> dict:
    """Normalize a raw S2 JSON record into storage.Paper kwargs."""
    external = record.get("externalIds") or {}
    embedding = None
    emb_field = record.get("embedding")
    if isinstance(emb_field, dict):
        embedding = emb_field.get("vector")
    fields_of_study = record.get("fieldsOfStudy") or []
    return dict(
        paper_id=record["paperId"],
        title=record.get("title") or "",
        abstract=record.get("abstract"),
        year=record.get("year"),
        venue=record.get("venue"),
        authors=[a.get("name") for a in (record.get("authors") or []) if a.get("name")],
        arxiv_id=external.get("ArXiv"),
        doi=external.get("DOI"),
        citation_count=record.get("citationCount") or 0,
        tldr=(record.get("tldr") or {}).get("text") if isinstance(record.get("tldr"), dict) else None,
        embedding=embedding,
        field_of_study=fields_of_study,
        source_api="semantic_scholar",
    )
