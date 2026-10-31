"""
Stage 3: is this paper's *contribution* core ML, or ML applied to something else?

Category metadata cannot answer that -- a primary cs.LG paper can still be
"fine-tuning an LLM for radiology triage" -- so this stage compares the paper's
embedding to two centroids built from labelled exemplars (PLAN.md section E4).

**It never rejects.** PLAN.md: "Never let stage 3 auto-reject in any version. It
is the stage most likely to be wrong and it fails silently." A clear core
verdict is ACCEPT; everything else, including a confident applied verdict, is
QUARANTINE and lands in the review drawer, which is where a wrong guess becomes
visible and reversible.

Pure: vectors in, `FilterDecision` out. Building the exemplar centroids from
the drawer decisions, and calling this from the cascade, need vectors in the
embedding store and are not done here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np
from numpy.typing import NDArray

from app.models import FilterDecision
from app.services.filters.base import FilterStage, accept, quarantine

#: Cosine-margin (core minus applied) a paper must clear to be auto-accepted.
#: A module constant rather than a YAML key: nothing tunes it yet, and a config
#: knob that nothing reads is the bug CLAUDE.md rule 8 describes.
ACCEPT_MARGIN = 0.05


def _unit(vec: Sequence[float]) -> NDArray[np.float64]:
    arr = np.asarray(vec, dtype=np.float64)
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        raise ValueError("zero vector has no direction")
    unit: NDArray[np.float64] = arr / norm
    return unit


def exemplar_centroid(vectors: Iterable[Sequence[float]]) -> NDArray[np.float64]:
    """Unit-length mean of the unit-length exemplars. Raises on an empty set."""
    units = [_unit(v) for v in vectors]
    if not units:
        raise ValueError("an exemplar centroid needs at least one vector")
    return _unit(np.mean(np.stack(units), axis=0))


def applied_filter(
    vector: Sequence[float] | None,
    core_centroid: Sequence[float],
    applied_centroid: Sequence[float],
    accept_margin: float = ACCEPT_MARGIN,
) -> FilterDecision:
    """
    ACCEPT only when the paper is closer to the core centroid by `accept_margin`.

    No vector -> QUARANTINE (`APPLIED_NO_EMBEDDING`): absence of evidence is not
    a verdict, and the cascade stays silent without vectors as the channel does.
    """
    if accept_margin < 0:
        raise ValueError("accept_margin must be non-negative")
    if vector is None:
        return quarantine(FilterStage.APPLIED, "APPLIED_NO_EMBEDDING")
    v = _unit(vector)
    margin = float(v @ _unit(core_centroid) - v @ _unit(applied_centroid))
    if margin >= accept_margin:
        return accept(FilterStage.APPLIED, "APPLIED_CLEAR_CORE", margin=margin)
    if margin <= -accept_margin:
        return quarantine(FilterStage.APPLIED, "APPLIED_LIKELY_APPLIED", margin=margin)
    return quarantine(FilterStage.APPLIED, "APPLIED_UNCERTAIN", margin=margin)


__all__ = ["ACCEPT_MARGIN", "applied_filter", "exemplar_centroid"]
