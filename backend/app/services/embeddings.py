"""
R6.1 -- embedding store: a NumPy matrix plus an id index, brute-force cosine.

No vector database (CLAUDE.md locked decision). At 3k x 768 a full matrix-vector
product is sub-millisecond, so the index is a sorted list of paper ids and the
search is one `matrix @ query`.

Two files, written together: ``<stem>.npy`` (float32, rows L2-normalised on
insert so cosine is a dot product) and ``<stem>.json`` (ids, per-row text hash).
The text hash is the cache-invalidation key: a paper whose title+abstract
changed must be re-embedded, and `needs_embedding` says so.

Determinism: rows are kept sorted by paper id, ties in `top_k` break on
paper id, and nothing depends on dict or set iteration order.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from app.models.domain import Paper

DIM_SPECTER_V2 = 768


def text_hash(title: str | None, abstract: str | None) -> str:
    """Hash of the text that was embedded. Missing parts hash as empty."""
    blob = f"{title or ''}\x1f{abstract or ''}".encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _normalise(vec: Sequence[float]) -> NDArray[np.float32]:
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 0 else arr


class EmbeddingStore:
    def __init__(self, dim: int = DIM_SPECTER_V2) -> None:
        self.dim = dim
        self._rows: dict[int, tuple[NDArray[np.float32], str]] = {}

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, paper_id: int) -> bool:
        return paper_id in self._rows

    def put(self, paper_id: int, vector: Sequence[float], digest: str) -> bool:
        """Store one vector. A wrong-length or all-zero vector is refused, not raised."""
        if len(vector) != self.dim:
            return False
        unit = _normalise(vector)
        if not np.any(unit):
            return False
        self._rows[paper_id] = (unit, digest)
        return True

    def needs_embedding(self, paper_id: int, digest: str) -> bool:
        """True when the paper has no vector, or its text changed since it was embedded."""
        row = self._rows.get(paper_id)
        return row is None or row[1] != digest

    def discard(self, paper_id: int) -> None:
        self._rows.pop(paper_id, None)

    def get(self, paper_id: int) -> NDArray[np.float32] | None:
        """The stored unit vector, or None. Callers must not mutate it."""
        row = self._rows.get(paper_id)
        if row is None:
            return None
        vec: NDArray[np.float32] = row[0]
        return vec

    def top_k(
        self,
        query: Sequence[float],
        k: int,
        *,
        exclude: Iterable[int] = (),
    ) -> list[tuple[int, float]]:
        """Best `k` by cosine, ordered `(-score, paper_id)`. Zero query -> empty."""
        q = _normalise(query)
        if k <= 0 or len(q) != self.dim or not np.any(q):
            return []
        skip = frozenset(exclude)
        ids = sorted(i for i in self._rows if i not in skip)
        if not ids:
            return []
        matrix = np.stack([self._rows[i][0] for i in ids])
        # float64 scores so the sort key is not at the mercy of float32 jitter
        scores = (matrix @ q).astype(np.float64)
        ranked = sorted(zip(ids, scores.tolist(), strict=True), key=lambda t: (-t[1], t[0]))
        return ranked[:k]

    def save(self, stem: Path) -> None:
        ids = sorted(self._rows)
        matrix = (
            np.stack([self._rows[i][0] for i in ids])
            if ids
            else np.zeros((0, self.dim), dtype=np.float32)
        )
        meta = {"dim": self.dim, "ids": ids, "hashes": [self._rows[i][1] for i in ids]}
        stem.parent.mkdir(parents=True, exist_ok=True)
        npy, js = stem.with_suffix(".npy"), stem.with_suffix(".json")
        # temp-then-rename so a crash never leaves a matrix and index that disagree
        with open(npy.with_name(npy.name + ".tmp"), "wb") as fh:
            np.save(fh, matrix)
        js.with_name(js.name + ".tmp").write_text(json.dumps(meta), encoding="utf-8")
        os.replace(npy.with_name(npy.name + ".tmp"), npy)
        os.replace(js.with_name(js.name + ".tmp"), js)

    @classmethod
    def load(cls, stem: Path, dim: int = DIM_SPECTER_V2) -> EmbeddingStore:
        """Missing or inconsistent files give an empty store: re-embedding is the repair."""
        store = cls(dim)
        npy, js = stem.with_suffix(".npy"), stem.with_suffix(".json")
        if not (npy.exists() and js.exists()):
            return store
        try:
            meta = json.loads(js.read_text(encoding="utf-8"))
            matrix = np.load(npy)
        except (OSError, ValueError):
            return store
        ids, hashes = meta.get("ids", []), meta.get("hashes", [])
        if (
            meta.get("dim") != dim
            or matrix.ndim != 2
            or matrix.shape != (len(ids), dim)
            or len(hashes) != len(ids)
        ):
            return store
        for pid, h, row in zip(ids, hashes, matrix, strict=True):
            # Stored rows are already unit length; re-normalising would move the
            # float32 bits and make save -> load -> save non-idempotent.
            store._rows[int(pid)] = (np.asarray(row, dtype=np.float32), str(h))
        return store


def centroid(store: EmbeddingStore, paper_ids: Iterable[int]) -> NDArray[np.float32] | None:
    """
    Mean of the stored vectors for `paper_ids` (those without one are ignored),
    or None when none has a vector. Ids are summed in sorted order so the float
    result is identical on every run.
    """
    vecs = [v for pid in sorted(set(paper_ids)) if (v := store.get(pid)) is not None]
    if not vecs:
        return None
    mean: NDArray[np.float32] = np.mean(np.stack(vecs), axis=0).astype(np.float32)
    return mean


def embedding_candidates(
    store: EmbeddingStore,
    anchor_ids: Iterable[int],
    k: int,
    *,
    exclude: Iterable[int] = (),
) -> list[tuple[int, float]]:
    """
    The embedding channel (R6): the `k` papers nearest the anchors' centroid,
    ordered ``(-cosine, paper_id)``. Anchors are always excluded from their own
    results. Nothing here looks at citation edges -- that independence is the
    channel's whole point: it can return papers the graph cannot reach.

    Anchors with no vector are ignored; if none has one the channel is silent
    (empty list), never an error.
    """
    anchors = sorted(set(anchor_ids))
    query = centroid(store, anchors)
    if query is None:
        return []
    return store.top_k(query.tolist(), k, exclude=[*anchors, *exclude])


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """What a backfill did. `unavailable` is S2 having no vector -- not an error."""

    requested: int
    stored: int
    unavailable: int
    skipped_fresh: int


EmbeddingFetcher = Callable[[list[str]], Awaitable[dict[str, list[float]]]]


async def backfill(
    store: EmbeddingStore,
    papers: Iterable[Paper],
    fetch: EmbeddingFetcher,
) -> BackfillResult:
    """
    Fill `store` for `papers` that lack a current vector, via `fetch`
    (``S2Client.get_embeddings``). Mutates the store; the caller saves it.

    Stubs and papers with no database id are skipped: a stub has no abstract, so
    its text hash would say "fresh" for a vector of a title alone. Ids are
    requested in sorted order so the cache key is the same on every run.
    """
    wanted: dict[str, tuple[int, str]] = {}
    skipped_fresh = 0
    for paper in papers:
        if paper.id is None or paper.is_stub:
            continue
        digest = text_hash(paper.title, paper.abstract)
        if not store.needs_embedding(paper.id, digest):
            skipped_fresh += 1
            continue
        wanted[paper.s2_paper_id] = (paper.id, digest)
    s2_ids = sorted(wanted)
    vectors = await fetch(s2_ids) if s2_ids else {}
    stored = 0
    for s2_id in s2_ids:
        vec = vectors.get(s2_id)
        if vec is None:
            continue
        paper_id, digest = wanted[s2_id]
        if store.put(paper_id, vec, digest):
            stored += 1
    return BackfillResult(
        requested=len(s2_ids),
        stored=stored,
        unavailable=len(s2_ids) - stored,
        skipped_fresh=skipped_fresh,
    )
