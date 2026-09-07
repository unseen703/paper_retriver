"""
openalex_client.py
------------------
Fallback / enrichment wrapper around the OpenAlex API.

Used when Semantic Scholar has no data for a given paper.
Polite pool activated by including a mailto= parameter on every request.
Rate: ~10 req/sec; we add a 0.11 s sleep between calls as a courtesy.

Docs: https://docs.openalex.org
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openalex.org"
MAILTO = "da25s020@smail.iitm.ac.in"


class OpenAlexClient:
    def __init__(self, cache=None, max_retries: int = 3, timeout: float = 15.0):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"CitationNetworkBuilder/1.0 (mailto:{MAILTO})"
        })
        self.cache = cache
        self.max_retries = max_retries
        self.timeout = timeout
        self._last_request_time: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_time
        wait = 0.11 - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_time = time.monotonic()

    def _request(self, url: str, params: Optional[dict] = None,
                 cache_key: Optional[str] = None) -> Optional[dict]:
        if cache_key and self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        base_params = {"mailto": MAILTO}
        if params:
            base_params.update(params)

        self._throttle()
        delay = 2.0
        resp = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=base_params, timeout=self.timeout)
            except requests.exceptions.RequestException as exc:
                logger.warning("OpenAlex request error (attempt %d): %s", attempt + 1, exc)
                if attempt == self.max_retries - 1:
                    return None
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if resp.status_code == 429:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                self._last_request_time = time.monotonic()
                continue
            if resp.status_code == 404:
                return None
            if not resp.ok:
                logger.warning("OpenAlex HTTP %d for %s", resp.status_code, url)
                return None
            data = resp.json()
            if cache_key and self.cache:
                self.cache.set(cache_key, data)
            return data

        return None

    # ---- search / lookup -------------------------------------------------------

    def search_by_title(self, title: str) -> Optional[dict]:
        """Best-match title search. Returns single work dict or None."""
        data = self._request(
            f"{BASE_URL}/works",
            params={"filter": f"title.search:{title}", "per-page": 1},
            cache_key=f"oa:title:{title}",
        )
        if data and data.get("results"):
            return data["results"][0]
        return None

    def get_work_by_doi(self, doi: str) -> Optional[dict]:
        clean = doi.lstrip("https://doi.org/").lstrip("http://doi.org/")
        return self._request(
            f"{BASE_URL}/works/doi:{clean}",
            cache_key=f"oa:doi:{clean}",
        )

    def get_work(self, oa_id: str) -> Optional[dict]:
        """Fetch a work by its OpenAlex ID (W123456789 or full URI)."""
        if oa_id.startswith("https://"):
            url = oa_id
        else:
            bare = oa_id.lstrip("W")
            url = f"{BASE_URL}/works/W{bare}"
        return self._request(url, cache_key=f"oa:work:{oa_id}")

    # ---- graph traversal -------------------------------------------------------

    def get_references(self, work: dict) -> list[dict]:
        """Return full metadata for each paper in work['referenced_works']."""
        ref_uris: list[str] = work.get("referenced_works") or []
        if not ref_uris:
            return []
        results = []
        chunk = 50
        for i in range(0, len(ref_uris), chunk):
            ids = "|".join(ref_uris[i: i + chunk])
            data = self._request(
                f"{BASE_URL}/works",
                params={"filter": f"openalex_id:{ids}", "per-page": chunk},
            )
            if data and data.get("results"):
                results.extend(data["results"])
        return results

    def get_citations(self, oa_id: str, limit: int = 100) -> list[dict]:
        """Return papers that cite the given work."""
        bare = oa_id.split("/")[-1]  # strip URI prefix if present
        results = []
        cursor = "*"
        while len(results) < limit:
            data = self._request(
                f"{BASE_URL}/works",
                params={
                    "filter": f"cites:{bare}",
                    "per-page": min(200, limit - len(results)),
                    "cursor": cursor,
                },
            )
            if not data or not data.get("results"):
                break
            results.extend(data["results"])
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        return results[:limit]

    # ---- helpers ---------------------------------------------------------------

    @staticmethod
    def reconstruct_abstract(inverted_index: Optional[dict]) -> Optional[str]:
        if not inverted_index:
            return None
        word_positions: list[tuple[int, str]] = []
        for word, positions in inverted_index.items():
            for pos in positions:
                word_positions.append((pos, word))
        word_positions.sort()
        return " ".join(w for _, w in word_positions) or None

    def to_paper_kwargs(self, work: dict) -> dict:
        """Normalize an OpenAlex works object to storage.Paper kwargs."""
        doi_raw = work.get("doi") or ""
        doi = doi_raw.replace("https://doi.org/", "").replace("http://doi.org/", "") or None
        ext_ids = work.get("ids") or {}
        arxiv_url = ext_ids.get("arxiv") or ""
        arxiv_id = arxiv_url.split("arxiv.org/abs/")[-1] if "arxiv" in arxiv_url else None

        authors = []
        for a in (work.get("authorships") or []):
            name = (a.get("author") or {}).get("display_name")
            if name:
                authors.append(name)

        venue = None
        loc = work.get("primary_location") or {}
        src = loc.get("source") or {}
        if src.get("display_name"):
            venue = src["display_name"]

        concepts = [c["display_name"] for c in (work.get("concepts") or [])
                    if c.get("level", 99) <= 1 and c.get("display_name")]

        abstract = self.reconstruct_abstract(work.get("abstract_inverted_index"))
        oa_id = work.get("id", "")
        paper_id = oa_id.split("/")[-1] if oa_id else None  # e.g. "W2741809807"

        return dict(
            paper_id=paper_id,
            title=work.get("display_title") or work.get("title") or "",
            abstract=abstract,
            year=work.get("publication_year"),
            venue=venue,
            authors=authors,
            arxiv_id=arxiv_id,
            doi=doi,
            citation_count=work.get("cited_by_count") or 0,
            tldr=None,
            embedding=None,
            field_of_study=concepts,
            source_api="openalex",
        )
