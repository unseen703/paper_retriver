"""
`GET` and `PUT /api/config` (R3) -- the weights, and instant re-ranking.

BUILD.md calls this "the payoff for persisting features", and PLAN.md says why
it is possible at all:

    "Features are computed and persisted; scores are derived on read. [...]
    changing weights re-ranks instantly with **zero API calls and zero
    recomputation**."

So `PUT` does exactly two things: replace the active weights, and rescore from
features that are already in the database. There is no path from here to the
S2 client, which is the structural version of "zero API calls" rather than the
hopeful one.

**A partial update keeps everything else.** Moving one number to see what
happens is the common case; requiring the whole set would make every
experiment a chance to reset something by omission. An *unknown* name is a 422
though -- a typo'd weight that is silently accepted produces a config that
looks changed and behaves exactly as before, which is the same failure
`extra="forbid"` guards against everywhere else in this codebase.

**The override lives in memory and resets on restart.** Persisting it would
need somewhere to put it, and PLAN.md section C defines no table for runtime
config -- inventing one would break CLAUDE.md rule 2. `config/ranking.yaml`
remains the durable answer; this is the tuning loop on top of it, and `GET`
reports whether what you are looking at is the file or an override.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine

from app.api.deps import get_engine
from app.config import Weights, ranking
from app.services.scoring import rescore_all

router = APIRouter(prefix="/api/config", tags=["config"])

#: The active weights. Starts as the file's, replaced by PUT.
#:
#: Module state rather than a dependency because there is exactly one weight
#: set for the process -- they are global by design, not per session, which is
#: also why a rescore touches every session.
_active: dict[str, float] = ranking.weights.model_dump()
_overridden = False


class WeightUpdate(BaseModel):
    """
    A partial weight update.

    Every field optional so one number can move on its own; `extra="forbid"`
    so a typo is a 422 rather than a silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    ppr: float | None = None
    cocite: float | None = None
    bibcoup: float | None = None
    overlap: float | None = None
    quality: float | None = None
    recency: float | None = None
    venue: float | None = None
    author: float | None = None
    dislike: float | None = None
    hub: float | None = None


class ConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weights: WeightUpdate = Field(default_factory=WeightUpdate)


class ConfigResponse(BaseModel):
    """The active weights, and where they came from."""

    weights: dict[str, float]
    config_version: str = Field(
        description=(
            "The version stamped into every expansion and filter decision --"
            " how you tell which weights produced a given graph."
        )
    )
    overridden: bool = Field(
        default=False,
        description=(
            "True when the active weights differ from config/ranking.yaml"
            " because of a PUT. Overrides are in memory and reset on restart."
        ),
    )


class RescoreResponse(ConfigResponse):
    rescored: int = Field(description="Nodes rescored, across every session. Weights are global.")


def _response() -> ConfigResponse:
    return ConfigResponse(
        weights=dict(_active), config_version=ranking.config_version, overridden=_overridden
    )


@router.get("", response_model=ConfigResponse)
def get_config() -> ConfigResponse:
    """The active weights. A ranking you cannot inspect is one you cannot tune."""
    return _response()


@router.put("", response_model=RescoreResponse)
def update_config(
    body: ConfigUpdateRequest,
    engine: Annotated[Engine, Depends(get_engine)],
) -> RescoreResponse:
    """
    Replace some weights and re-rank every graph from persisted features.

    No API calls and no feature recomputation -- see the module docstring.
    """
    global _overridden

    changes = body.weights.model_dump(exclude_none=True)
    _active.update(changes)
    if changes:
        _overridden = True

    rescored = rescore_all(engine, _active)
    base = _response()
    return RescoreResponse(
        weights=base.weights,
        config_version=base.config_version,
        overridden=base.overridden,
        rescored=rescored,
    )


def reset_active_weights() -> None:
    """
    Restore the file's weights. **For tests**, which share a process and would
    otherwise leak one case's override into the next.
    """
    global _overridden
    _active.clear()
    _active.update(ranking.weights.model_dump())
    _overridden = False


__all__ = ["Weights", "reset_active_weights", "router"]
