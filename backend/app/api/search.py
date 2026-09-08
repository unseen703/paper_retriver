"""
`GET /api/search` (R1.15) -- find a paper to add.

A thin pass-through to `S2Client.search_title`, which is cache-first, plus the
two session-relative flags BUILD.md singles out. PLAN.md on why those flags
exist at all: "the last two flags are what stop the user re-adding what they
deleted."

    already_in_graph     has a `graph_nodes` row in this session
    previously_removed   latest event in this session is a tombstone

Both are computed against the corpus in one pass rather than per hit -- ten
results would otherwise be ten `find_by_s2_id` round trips for a lookup the
database answers in one.

**Search is read-only.** It resolves nothing, upserts nothing, and admits
nothing; `POST /nodes` (R1.16) is the only path into the graph. If search
wrote rows, its own `already_in_graph` flag would come back true on the second
call and the user could never deliberately add anything.

**The route carries `{sid}`, unlike PLAN.md's sketch of a bare `/api/search`.**
The two flags are graph membership, not facts about a paper, and PLAN.md's own
section C9 is the argument -- "you're modeling papers when you mean graph
membership". Reading them from `settings.session_id` while `POST /nodes` wrote
to the path's `sid` meant the dialog could show "not in graph" for a paper that
was in the graph, then get a 409 when the user clicked it. Session-scoped state
belongs on a session-scoped route.

**Failures are 503, not 500.** S2 being down, rate-limited, or -- in tests --
simply not holding the answer is an upstream availability problem the caller
can retry, not a bug in this process. It is reported as such so the frontend
can say "search is unavailable" instead of "something went wrong".
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine

from app.api.deps import existing_session, get_engine, get_s2_client
from app.clients.s2 import CacheMiss, S2Client, S2TransientError
from app.models import PaperStub
from app.repo import events as events_repo
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.schemas.search import SearchHit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["search"])

# S2 caps `/paper/search` at 100 results per page. Asking for more is a client
# bug, and a silent clamp would hide it.
MAX_LIMIT = 100

# Longest title we will search for. The longest real title in the corpus is
# ~110 characters; 300 leaves generous room. Unbounded, a pasted abstract
# became a multi-kilobyte URL, servers cap request lines around 8KB, and the
# resulting 414 surfaced here as "Semantic Scholar is unavailable" -- a
# confident, wrong diagnosis of a client-side problem.
MAX_QUERY_CHARS = 300


def _annotate(engine: Engine, session_id: int, stubs: list[PaperStub]) -> list[SearchHit]:
    """
    Attach this session's history to each hit. Three queries, not 3N -- and
    all three scoped to the ids being annotated.

    The scoping matters more than it looks. `get_node_ids` and
    `removed_paper_ids` return the whole session, so annotating ten hits used
    to pull every node id plus a correlated-max scan of the session's entire
    event log. On a 2k-node graph behind a debounced search-as-you-type box
    that is thousands of rows per keystroke to answer a question about ten
    papers.
    """
    known: dict[str, int] = {}
    in_graph: set[int] = set()
    removed: set[int] = set()

    if stubs:
        with engine.connect() as conn:
            known = papers_repo.find_ids_by_s2_ids(conn, [s.s2_paper_id for s in stubs])
            if known:
                local_ids = list(known.values())
                in_graph = graph_repo.nodes_present(conn, session_id, local_ids)
                removed = events_repo.removed_among(conn, session_id, local_ids)

    hits: list[SearchHit] = []
    for stub in stubs:
        paper_id = known.get(stub.s2_paper_id)
        hits.append(
            SearchHit(
                s2_paper_id=stub.s2_paper_id,
                title=stub.title,
                year=stub.year,
                citation_count=stub.citation_count,
                venue=stub.venue,
                authors=list(stub.authors),
                # A paper with no local row is neither: it has no id to have a
                # node or an event under.
                already_in_graph=paper_id is not None and paper_id in in_graph,
                previously_removed=paper_id is not None and paper_id in removed,
            )
        )
    return hits


@router.get("/{sid}/search", response_model=list[SearchHit])
async def search(
    sid: Annotated[int, Depends(existing_session)],
    q: Annotated[
        str,
        Query(max_length=MAX_QUERY_CHARS, description="Paper title to search for."),
    ],
    # `Annotated` rather than a `Depends(...)` default: a call in a default
    # argument is evaluated once at import, which is a real bug for anything
    # mutable and which ruff's B008 flags. FastAPI reads either form, so there
    # is nothing to suppress.
    client: Annotated[S2Client, Depends(get_s2_client)],
    engine: Annotated[Engine, Depends(get_engine)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 10,
) -> list[SearchHit]:
    """
    Search S2 by title, annotated with what this session already knows.

    An unknown title is an empty list, not an error -- "no such paper" is a
    valid answer to a search.
    """
    # Trimmed before it reaches the client: leading whitespace on a pasted
    # title is the normal case, and an untrimmed query is a different cache
    # key, so it would miss a cache that already holds the answer.
    query = q.strip()
    if not query:
        # PLAN.md names 400 for this rather than FastAPI's default 422: the
        # parameter is present and well-typed, it is the value that is unusable.
        raise HTTPException(status_code=400, detail="q must not be empty")

    try:
        stubs = await client.search_title(query, limit=limit)
    except (S2TransientError, CacheMiss) as exc:
        logger.warning("search_upstream_unavailable q=%r: %s", query, exc)
        raise HTTPException(
            status_code=503,
            detail=f"Semantic Scholar is unavailable and this query is not cached: {exc}",
        ) from exc

    return _annotate(engine, sid, stubs)


__all__ = ["MAX_LIMIT", "router"]
