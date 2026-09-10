"""
Request and response models for `/api/sessions` (R2.16).

A session is a workspace over a shared corpus: a name, and the graph you have
built inside it. Nothing else, because everything else about it lives in the
tables that key on `session_id`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreateSessionRequest(BaseModel):
    """Start a new, empty graph over the same corpus."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        """
        A name of spaces passes `min_length` and is useless in a switcher --
        the row would be there and unreadable, which is worse than a refusal.
        """
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


class SessionOut(BaseModel):
    """One workspace, as the switcher lists it."""

    id: int
    name: str
    created_at: str
    node_count: int = Field(
        description=(
            "Papers in this session's graph. Without it every row in the"
            " switcher looks the same, and the session holding your work is"
            " indistinguishable from the empty one you made by accident."
        )
    )


__all__ = ["CreateSessionRequest", "SessionOut"]
