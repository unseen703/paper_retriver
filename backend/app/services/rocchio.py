"""
Rocchio-style weight nudging (R5.3).

PLAN.md R5: "after N labels, shift weights toward features that discriminate
liked from disliked". Classic Rocchio moves a query vector toward the centroid
of relevant documents and away from the non-relevant one; here the "query" is
the weight vector and the documents are feature dicts.

**Pure** (CLAUDE.md rule 3): feature dicts and weights in, weights out. Nothing
is persisted -- the active weights live in memory behind `PUT /api/config`, and
wiring a nudge to a label is R5.4.

Rules that keep it from doing damage:

  * **Nothing happens before `min_labels`.** Three likes say nothing about
    taste; nudging on them would make the first few clicks steer everything.
  * **A zero weight stays zero.** `ppr` and `dislike` ship at 0.00 *because the
    benchmark has not yet shown they help*. Learning them from a handful of
    clicks would switch on exactly what R4 has not vindicated.
  * **A weight never changes sign.** The sign in `ranking.yaml` is the sign in
    the score (CLAUDE.md rule 8); `hub` is negative on purpose, and a few
    liked hubs must not turn it into a reward. Magnitude is bounded to
    [0, `MAX_SCALE` x |base|].
  * **A missing feature is skipped, not zeroed.** Features are absent when the
    data cannot support them; treating that as 0 would invent a signal.
  * With no dislikes the comparison point is `NEUTRAL` (0.5), the
    rank-percentile value that means "no information".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.services.ranking import NEUTRAL

#: Labels (liked + disliked) required before any nudge.
MIN_LABELS = 5

#: Step size: how far a unit of discrimination moves a weight.
RATE = 0.25

#: A weight may grow to at most this multiple of its configured magnitude.
MAX_SCALE = 2.0


def _mean(vectors: Sequence[Mapping[str, float]], name: str) -> float | None:
    values = [v[name] for v in vectors if name in v]
    return sum(values) / len(values) if values else None


def nudge_weights(
    base: Mapping[str, float],
    liked: Sequence[Mapping[str, float]],
    disliked: Sequence[Mapping[str, float]],
    *,
    min_labels: int = MIN_LABELS,
    rate: float = RATE,
) -> dict[str, float]:
    """Return `base` shifted toward features that separate liked from disliked."""
    if not liked or len(liked) + len(disliked) < min_labels:
        return dict(base)

    out: dict[str, float] = {}
    for name in sorted(base):
        weight = base[name]
        liked_mean = _mean(liked, name)
        if weight == 0.0 or liked_mean is None:
            out[name] = weight
            continue
        disliked_mean = _mean(disliked, name)
        against = NEUTRAL if disliked_mean is None else disliked_mean
        moved = weight + rate * (liked_mean - against) * abs(weight)
        bound = MAX_SCALE * abs(weight)
        # Clamp inside the base weight's own sign; never flip it.
        out[name] = min(max(moved, 0.0), bound) if weight > 0 else max(min(moved, 0.0), -bound)
    return out


__all__ = ["MAX_SCALE", "MIN_LABELS", "RATE", "nudge_weights"]
