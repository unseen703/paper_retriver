"""
Request and response models for `POST /api/sessions/{sid}/nodes`.

`extra="forbid"` on the request is deliberate and matches how `config.py`
treats the YAML files. A typo'd field that is silently ignored -- `forse` for
`force` -- produces a request that succeeds while doing the opposite of what
was asked, and the only place that surfaces is production.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AddNodeRequest(BaseModel):
    """Add one paper to a session's graph."""

    model_config = ConfigDict(extra="forbid")

    s2_paper_id: str = Field(min_length=1)
    # A `Literal` rather than a free string: R1 adds seeds only. CANDIDATE is
    # the expander's to write and LIKED/DISLIKED belong to R2's labelling
    # endpoint, which owns the transition validation. Accepting them here would
    # be a second, unvalidated way into the state machine.
    as_state: Literal["SEED"] = "SEED"
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


__all__ = ["AddNodeRequest", "NodeResponse", "RejectedResponse", "RejectionDetail"]
