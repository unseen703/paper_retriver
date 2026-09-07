"""
The `GET /api/search` response model.

This is a wire type, deliberately separate from `models.PaperStub`. The domain
object describes what S2 returned; this describes what the user is being shown,
and the two differ by exactly the fields that only mean something relative to a
session -- `already_in_graph` and `previously_removed`.

Keeping them separate is what lets `make types` generate honest TypeScript: the
frontend gets a type that carries the flags, rather than a paper type with two
fields bolted on that are absent everywhere else a paper appears.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchHit(BaseModel):
    """One search result, annotated with this session's history of it."""

    s2_paper_id: str
    title: str
    year: int | None = None
    citation_count: int | None = None
    venue: str | None = None
    # Byline only -- names, in order, no ids. The search list needs it to tell
    # near-identical titles apart, and nothing downstream reads it.
    authors: list[str] = Field(default_factory=list)

    already_in_graph: bool = Field(
        default=False,
        description="This paper has a node in the current session's graph.",
    )
    previously_removed: bool = Field(
        default=False,
        description=(
            "The paper's latest event in this session is a tombstone -- it was"
            " in the graph and was removed. Derived, not stored, so a restore"
            " lifts it."
        ),
    )


__all__ = ["SearchHit"]
