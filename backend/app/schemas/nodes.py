"""
Request and response models for `POST /api/sessions/{sid}/nodes`.

`extra="forbid"` on the request is deliberate and matches how `config.py`
treats the YAML files. A typo'd field that is silently ignored -- `forse` for
`force` -- produces a request that succeeds while doing the opposite of what
was asked, and the only place that surfaces is production.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AddNodeRequest(BaseModel):
    """Add one paper to a session's graph."""

    model_config = ConfigDict(extra="forbid")

    s2_paper_id: str = Field(min_length=1)
    # There is deliberately no `as_state`. It used to be here as
    # `Literal["SEED"]`, declared in the OpenAPI contract and therefore in the
    # generated TypeScript -- and never read, because `add_seed` hard-codes
    # SEED. A field a caller can set that changes nothing is a promise the
    # server does not keep, and it becomes a live bug the moment R2 widens the
    # Literal: the request would validate, the caller would believe it had
    # asked for a state, and a SEED would be created regardless.
    #
    # R2 adds states through `PATCH /nodes/{id}`, which owns transition
    # validation. Until then `extra="forbid"` turns a stale caller's
    # assumption into a 422 they can see.
    force: bool = Field(
        default=False,
        description=(
            "Admit the paper even if the filter cascade rejects it. The"
            " rejection is still recorded -- an override is never silent."
        ),
    )


class NodeResponse(BaseModel):
    """A node as the graph holds it, plus the title so the UI can render it."""

    session_id: int
    paper_id: int
    state: str
    depth: int
    title: str
    score: float | None = None
    year: int | None = None


class RejectionDetail(BaseModel):
    """
    Why the cascade refused a paper.

    `reason_code` is the point of it: "rejected" is not actionable, "PRE_ERA"
    names the rule and lets the UI offer `force` against something the user can
    read and disagree with.
    """

    detail: str
    reason_code: str
    stage: str


class RejectedResponse(BaseModel):
    """
    The 422 body, wrapped in FastAPI's standard error envelope.

    The nesting is deliberate rather than incidental. FastAPI puts every
    `HTTPException` payload under `detail`, so the 404, 409 and 503 from this
    same endpoint all arrive as `{"detail": ...}`. Flattening only the 422
    would give the frontend one error shape to special-case, and would make
    this declared model disagree with what the endpoint actually emits -- which
    matters more than usual here, because `make types` generates the
    frontend's error handling from it.
    """

    detail: RejectionDetail


class NodeDetail(BaseModel):
    """
    Everything R1.22's `<NodeInspector>` renders.

    Two kinds of fact, deliberately in one response because the panel shows
    them together:

        global      title, authors, venue, date, citations, type, categories
        per-session state, depth, score, features, score_breakdown, degrees

    The abstract is **not** here. CLAUDE.md is explicit that abstracts are
    stored for embeddings only; shipping one per inspector click spends bytes
    on something the design says is not for reading, and no LLM stage consumes
    it.
    """

    paper_id: int
    s2_paper_id: str
    title: str
    # Names in byline order. Only populated at all because of the R1.15
    # author-name fix -- `paper_authors` was permanently empty before it.
    authors: list[str] = Field(default_factory=list)
    venue: str | None = None
    year: int | None = None
    publication_date: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None

    citation_count: int = 0
    reference_count: int = 0
    influential_citation_count: int = 0

    paper_type: str | None = None
    # The primary category is called out separately: cross-listing is the whole
    # reason the topic filter exists, so an undifferentiated list would hide the
    # field that actually decided admission.
    primary_arxiv_category: str | None = None
    arxiv_categories: list[str] = Field(default_factory=list)
    crawl_state: str

    state: str
    depth: int
    score: float | None = None
    # Degrees within this session's graph, matching GET /graph. The inspector
    # must not contradict the picture beside it.
    in_degree: int = 0
    out_degree: int = 0
    features: dict[str, Any] = Field(default_factory=dict)
    score_breakdown: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-term contributions to the score. Empty until R3 ships real"
            " features -- present-and-empty so the UI renders 'unavailable'"
            " from the shape rather than from a special case."
        ),
    )


__all__ = [
    "AddNodeRequest",
    "NodeDetail",
    "NodeResponse",
    "RejectedResponse",
    "RejectionDetail",
]
