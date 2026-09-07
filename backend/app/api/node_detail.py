"""
`GET /api/sessions/{sid}/nodes/{paper_id}` (R1.19) -- everything the inspector
renders.

**The route carries `{sid}`, unlike PLAN.md's sketch of `/api/nodes/{id}`.**
The response mixes two kinds of fact and only one is global:

    global      title, authors, venue, date, citations, type, categories
    per-session state, depth, score, features, score_breakdown, degrees

PLAN.md asks this endpoint for "features + breakdown", which are `graph_nodes`
columns and therefore meaningless without a session; and its own labelling
endpoint is already `PATCH /api/sessions/{sid}/nodes/{id}`. A bare
`/api/nodes/{id}` would leave the read and the write of one row on
differently-scoped paths. Same correction as search at R1.15.

**A paper with no node here is a 404, not a partial response.** Boundary
papers and rejects are in the corpus with edges and no `graph_nodes` row.
Returning them with null depth and score would say "in your graph, unscored"
when the truth is "not in your graph".

Degrees are counted within this session's graph, matching `GET /graph` -- an
inspector that contradicts the picture beside it is worse than no inspector.
They are computed from this paper's own neighbours rather than from the whole
session, so the cost tracks the node's degree and not the graph's size.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import Engine

from app.api.deps import existing_session, get_engine
from app.repo import edges as edges_repo
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.schemas.nodes import NodeDetail

router = APIRouter(prefix="/api/sessions", tags=["nodes"])


def _byline(raw: tuple[tuple[str, str], ...]) -> list[str]:
    """(id, name) pairs in byline order -> names. Position carries meaning."""
    return [name for _, name in raw]


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


@router.get("/{sid}/nodes/{paper_id}", response_model=NodeDetail)
def get_node_detail(
    sid: Annotated[int, Depends(existing_session)],
    paper_id: Annotated[int, Path(ge=1, description="Local paper id.")],
    engine: Annotated[Engine, Depends(get_engine)],
) -> NodeDetail:
    """Full metadata plus this session's view of one node."""
    with engine.connect() as conn:
        node = graph_repo.get_node(conn, sid, paper_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"no node {paper_id} in session {sid}")

        papers = papers_repo.get_papers_by_ids(conn, [paper_id])
        if not papers:
            # A node whose paper is gone is a broken foreign key. 404 rather
            # than 500: there is nothing to show, and nothing the caller can do.
            raise HTTPException(status_code=404, detail=f"no paper {paper_id}")
        paper = papers[0]

        # Only this paper's own neighbourhood, then narrowed to the ones with a
        # node here. Bounded by the paper's degree rather than by graph size.
        touching = edges_repo.get_edges_for(conn, [paper_id])
        neighbours = {(e.cited_id if e.citing_id == paper_id else e.citing_id) for e in touching}
        drawn = graph_repo.nodes_present(conn, sid, sorted(neighbours))

    in_degree = sum(1 for e in touching if e.cited_id == paper_id and e.citing_id in drawn)
    out_degree = sum(1 for e in touching if e.citing_id == paper_id and e.cited_id in drawn)

    return NodeDetail(
        paper_id=paper_id,
        s2_paper_id=paper.s2_paper_id,
        title=paper.title,
        authors=_byline(paper.authors),
        venue=paper.venue,
        year=paper.year,
        publication_date=paper.publication_date,
        doi=paper.doi,
        arxiv_id=paper.arxiv_id,
        citation_count=paper.citation_count,
        reference_count=paper.reference_count,
        influential_citation_count=paper.influential_citation_count,
        paper_type=paper.paper_type.value if paper.paper_type else None,
        primary_arxiv_category=paper.primary_arxiv_category,
        arxiv_categories=list(paper.arxiv_categories),
        crawl_state=paper.crawl_state.value,
        state=node.state,
        depth=node.depth,
        score=node.score,
        in_degree=in_degree,
        out_degree=out_degree,
        features=_loads(node.features),
        score_breakdown=_loads(node.score_breakdown),
    )


__all__ = ["router"]
