"""Response shapes for `GET /api/duplicates` (R6 dedup upgrade)."""

from __future__ import annotations

from pydantic import BaseModel


class DuplicatePaperOut(BaseModel):
    id: int
    title: str | None = None
    year: int | None = None
    venue: str | None = None
    citation_count: int | None = None


class DuplicatePairOut(BaseModel):
    """Two stored papers that look like one paper twice. `a.id < b.id`."""

    a: DuplicatePaperOut
    b: DuplicatePaperOut
    cosine: float
    title_similarity: float


class DuplicatesResponse(BaseModel):
    pairs: list[DuplicatePairOut]
    total: int
