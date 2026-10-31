"""
Simulated-user eval (R5.5): does liking papers raise Recall@20?

BUILD.md: "reveal ground-truth papers as 'likes' one at a time; assert
Recall@20 rises". For each case, a seeded order over the ground-truth papers
that are actually in the pool is revealed as likes. After `n` likes the weights
are Rocchio-nudged (`services/rocchio.py`, the same function the product's
label path uses), the pool is re-ranked, and Recall@20 is measured.

**What is held fixed so the delta means something.** The revealed papers are
removed from the ranking *and* from the truth. Otherwise a like would "raise"
recall by pinning a paper the user already found to the top. Only cases with
more reachable truth than the largest step are used at every step, so the
case set does not change between points on the curve.

**What this does not simulate.** Only the Rocchio weight nudge. The `ppr` and
`dislike` features also react to labels in the product, but they need a graph
and a session, and their weights are 0.00 until the benchmark vindicates them.
No dislikes are simulated: a reference list names no papers the author
rejected, and inventing negatives would put the answer in the question.

**Pure** over `ScoredPool`s and a base weight vector: no DB, no network. The
result is a measurement, reported with CIs; whether the curve rises is for the
write-up to say, not for this module to assert about real data.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from metrics import bootstrap_ci, recall_at_k
from sweep import ScoredPool, rank_pool

from app.services.rocchio import nudge_weights

#: Likes revealed before each measurement. 0 is the no-feedback control; 5 is
#: where Rocchio's `MIN_LABELS` first lets it act, so 1 and 3 should be flat.
LIKE_STEPS = (0, 1, 3, 5, 8)

K = 20


def reveal_order(pool: ScoredPool, seed: int | str) -> list[int]:
    """Reachable ground truth in a seeded, reproducible order."""
    reachable = sorted(pool.ground_truth & set(pool.features))
    random.Random(f"{seed}:{pool.case_id}").shuffle(reachable)
    return reachable


def recall_after_likes(
    pool: ScoredPool, base: Mapping[str, float], revealed: Sequence[int]
) -> float:
    """Recall@K over the truth not yet revealed, under weights nudged by `revealed`."""
    liked = [pool.features[p] for p in revealed]
    weights = nudge_weights(base, liked, [])
    shown = set(revealed)
    ranked = [p for p in rank_pool(pool, weights) if p not in shown]
    return recall_at_k(ranked, pool.ground_truth - shown, K)


def simulate_user(
    pools: Sequence[ScoredPool],
    base: Mapping[str, float],
    seed: int | str,
    steps: Sequence[int] = LIKE_STEPS,
) -> dict[str, object]:
    """Mean Recall@20 and 95% CI at each like count, over comparable cases."""
    top = max(steps)
    usable = [p for p in sorted(pools, key=lambda p: p.case_id) if len(reveal_order(p, seed)) > top]

    curve: dict[str, dict[str, object]] = {}
    for n in steps:
        vals = [recall_after_likes(p, base, reveal_order(p, seed)[:n]) for p in usable]
        curve[str(n)] = {
            "mean": sum(vals) / len(vals) if vals else 0.0,
            "ci95": list(bootstrap_ci(vals)),
        }
    return {
        "metric": f"recall@{K}",
        "n_cases": len(usable),
        "n_cases_skipped": len(pools) - len(usable),
        "likes": list(steps),
        "curve": curve,
        "simulated": "rocchio weight nudge only; ppr/dislike label effects are not simulated",
    }


__all__ = ["K", "LIKE_STEPS", "recall_after_likes", "reveal_order", "simulate_user"]
