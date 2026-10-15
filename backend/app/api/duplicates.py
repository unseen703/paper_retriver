"""
`GET /api/duplicates` -- probable duplicate papers, for review (R6).

Global, not session-scoped: papers are facts about the world (BUILD.md
session_id contract), so is a duplicate of one. **Read-only; nothing is ever
merged from here.** Both cosine and normalized-title similarity must agree
(`services.dedup.duplicate_candidates`). Empty when the vector store is empty,
because without vectors there is no evidence either way, not an error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Engine

from app.api.deps import get_embedding_stem, get_engine
from app.repo import papers as papers_repo
from app.schemas.duplicates import DuplicatePairOut, DuplicatePaperOut, DuplicatesResponse
from app.services.dedup import duplicate_candidates
from app.services.embeddings import EmbeddingStore

router = APIRouter(prefix="/api", tags=["duplicates"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


@router.get("/duplicates", response_model=DuplicatesResponse)
def list_duplicates(
    engine: Annotated[Engine, Depends(get_engine)],
    stem: Annotated[Path, Depends(get_embedding_stem)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DuplicatesResponse:
    """Pairs ordered `(-cosine, a, b)`; `total` is the whole set, not the page."""
    store = EmbeddingStore.load(stem)
    with engine.connect() as conn:
        ids = papers_repo.list_ids(conn)
        papers = {p.id: p for p in papers_repo.get_papers_by_ids(conn, ids)}

    titles = {i: p.title for i, p in papers.items() if p.title}
    pairs = duplicate_candidates(store, titles)

    def out(paper_id: int) -> DuplicatePaperOut:
        p = papers[paper_id]
        return DuplicatePaperOut(
            id=paper_id,
            title=p.title,
            year=p.year,
            venue=p.venue,
            citation_count=p.citation_count,
        )

    page = [
        DuplicatePairOut(
            a=out(c.a), b=out(c.b), cosine=c.cosine, title_similarity=c.title_similarity
        )
        for c in pairs[offset : offset + limit]
    ]
    return DuplicatesResponse(pairs=page, total=len(pairs))
