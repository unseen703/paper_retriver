"""
api_cache.py
------------
Simple file-based JSON cache for API responses.

Cache files are written to ./cache/ next to this script.
Cache keys are SHA-256 hashes of the request fingerprint (api:endpoint:params).
There is no TTL — entries are permanent so re-runs never re-hit rate limits.
Delete the ./cache/ directory to force a full refresh.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(__file__).parent / "cache"


class ApiCache:
    def __init__(self, cache_dir: Path | str = DEFAULT_CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def get(self, key: str) -> Optional[Any]:
        path = self._path(key)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                path.unlink(missing_ok=True)
        return None

    def set(self, key: str, data: Any) -> None:
        path = self._path(key)
        try:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.warning("Cache write failed for key %r: %s", key, exc)
