"""
Request and response models for `POST /api/sessions/{sid}/expansions`.

The response is deliberately wide. An expansion that fetched sixty papers and
admitted none is a completely different event from one that found nothing to
fetch, and a bare `{"added": 3}` cannot tell you which you got -- which is
exactly the ambiguity that hid three separate bugs during R1's checkpoints.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExpandRequest(BaseModel):
    """One expansion's parameters. Every field optional; the defaults expand."""

    model_config = ConfigDict(extra="forbid")

    # A `Literal[1]` rather than an int with a range. R1 expands exactly one
    # hop, and accepting `hops: 3` while quietly doing one would tell the
    # client it reached depth 3. Multi-hop arrives at R2.5; until then the
    # honest answer to asking for it is a 422.
    hops: Literal[1] = 1
    max_new: int = Field(default=20, ge=1, le=100)
    # Hard stops, both "commit what you have" rather than "raise" -- a partial
    # expansion is a success.
    api_call_budget: int = Field(default=60, ge=1, le=1000)
    max_nodes: int = Field(default=2000, ge=1, le=100_000)


class ExpandResponse(BaseModel):
    """What the run actually did."""

    n_pool: int = Field(description="Candidates pooled before ranking.")
    n_added: int
    added_paper_ids: list[int] = Field(
        default_factory=list,
        description="Papers admitted by this run, sorted. Usable against GET /graph.",
    )

    boundary: int = Field(
        description=(
            "Papers stored with their edges but denied a node. Should be a"
            " large fraction on a backward pass; zero means the year floor has"
            " started deleting evidence rather than withholding"
            " recommendations."
        )
    )
    backward_fetched: int = 0
    forward_fetched: int = 0
    hub_skipped: int = Field(
        default=0, description="Papers too heavily cited to expand forward from."
    )

    api_calls: int = 0
    cache_hits: int = 0
    truncated: bool = False
    error: str | None = Field(
        default=None,
        description=(
            "Why the run stopped early, if it did. Not a failure -- the result"
            " is still committed and the status is still 200."
        ),
    )


__all__ = ["ExpandRequest", "ExpandResponse"]
