"""
The `GET /api/health` response model.

R1.14 returned a bare `dict[str, Any]`, which FastAPI renders in OpenAPI as an
untyped object -- so `make types` generated `unknown` for every field and the
frontend could not read one of them without a cast. A generated type that has
to be cast away is worse than no generated type, because the cast is where the
drift hides.

The fields are unchanged; only the declaration is new.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Four things worth knowing at 2am, not `{"status": "ok"}`."""

    db: str = Field(description='"ok", or "error: <ExceptionType>" if unreachable.')
    s2_reachable: str = Field(
        description=(
            '"configured" or "no_api_key". Deliberately not a live probe -- a'
            " monitoring loop that costs an upstream request becomes a"
            " rate-limit problem, and at one request per second that is the"
            " entire budget."
        )
    )
    cache_rows: int
    node_count: int = Field(description="Nodes in the configured session, not globally.")
    session_id: int
    config_version: str = Field(
        description="Which filter config produced this graph. The first question when it looks odd."
    )


__all__ = ["HealthResponse"]
