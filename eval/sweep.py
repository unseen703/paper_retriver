"""
The config sweep (R4.5).

BUILD.md: "Config sweep over weight grids -> `eval/results/*.json`".

`ranking.yaml` carries weights nobody measured -- `venue: 0.20` says so in its
own comment. This module is the measurement: take cases whose candidates already
carry persisted features, re-score them under every point of a weight grid, and
report each configuration's metrics with confidence intervals.

**Pure and offline.** Features are inputs, not computed here. That is the same
property `PUT /api/config` relies on -- rescoring needs no API calls -- and it
is what makes a sweep of hundreds of configurations take seconds.

**A sweep is a hypothesis generator, not a verdict.** Picking the best of N
configurations on the benchmark that produced the numbers overfits it. Every
result therefore carries its CI, the grid itself is recorded, and the winner is
reported with the baseline-config result beside it so the write-up can say how
much of the gap is inside the noise.

Deterministic throughout: configurations are enumerated in sorted-axis order,
ranking ties break on `paper_id`, and the JSON is written with sorted keys
(CLAUDE.md rule 7).
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from metrics import bootstrap_ci, hit_at_k, ndcg_at_k, recall_at_k, reciprocal_rank

from app.services.ranking import score_paper

#: The cutoffs BUILD.md names for recall.
RECALL_KS = (10, 20, 50)


@dataclass(frozen=True, slots=True)
class ScoredPool:
    """One benchmark case, ready to rank: features per candidate, and the truth."""

    case_id: int
    features: Mapping[int, Mapping[str, float]]
    ground_truth: frozenset[int]


def weight_grid(
    base: Mapping[str, float], axes: Mapping[str, Sequence[float]]
) -> list[dict[str, float]]:
    """
    The cartesian product of `axes` laid over `base`.

    An axis naming a weight that is not in `base` raises: a typo would
    otherwise sweep a weight that nothing reads and report a flat, meaningless
    result (the config-correspondence failure CLAUDE.md rule 8 describes).
    Axes are enumerated in sorted order so the grid is the same list each run.
    """
    unknown = sorted(set(axes) - set(base))
    if unknown:
        raise ValueError(f"sweep axes not in the weights: {unknown}")

    names = sorted(axes)
    grid: list[dict[str, float]] = []
    for combo in itertools.product(*(axes[n] for n in names)):
        weights = dict(base)
        weights.update(zip(names, combo, strict=True))
        grid.append(weights)
    return grid


def rank_pool(pool: ScoredPool, weights: Mapping[str, float]) -> list[int]:
    """Candidate ids best-first under `weights`; ties break on ascending id."""
    scored = {pid: score_paper(feats, weights)[0] for pid, feats in pool.features.items()}
    return sorted(scored, key=lambda pid: (-scored[pid], pid))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def evaluate_config(pools: Sequence[ScoredPool], weights: Mapping[str, float]) -> dict[str, object]:
    """Mean metric and 95% CI per metric over all cases, for one configuration."""
    per_case: dict[str, list[float]] = {f"recall@{k}": [] for k in RECALL_KS}
    per_case.update({"ndcg@20": [], "mrr": [], "hit@10": []})

    for pool in sorted(pools, key=lambda p: p.case_id):
        ranked = rank_pool(pool, weights)
        for k in RECALL_KS:
            per_case[f"recall@{k}"].append(recall_at_k(ranked, pool.ground_truth, k))
        per_case["ndcg@20"].append(ndcg_at_k(ranked, pool.ground_truth, 20))
        per_case["mrr"].append(reciprocal_rank(ranked, pool.ground_truth))
        per_case["hit@10"].append(hit_at_k(ranked, pool.ground_truth, 10))

    metrics = {
        name: {"mean": _mean(vals), "ci95": list(bootstrap_ci(vals))}
        for name, vals in sorted(per_case.items())
    }
    return {"weights": dict(sorted(weights.items())), "metrics": metrics}


def run_sweep(
    pools: Sequence[ScoredPool],
    base: Mapping[str, float],
    axes: Mapping[str, Sequence[float]],
    *,
    select_on: str = "recall@20",
) -> dict[str, object]:
    """
    Evaluate every grid point, plus `base` itself, and name the best on `select_on`.

    `base` is always evaluated and reported separately from the winner: the
    interesting number is the gap between them and whether their CIs overlap.
    Ties for best go to the earlier grid point, which is deterministic.
    """
    results = [evaluate_config(pools, w) for w in weight_grid(base, axes)]
    baseline = evaluate_config(pools, base)

    def mean_of(r: dict[str, object]) -> float:
        metrics = r["metrics"]
        assert isinstance(metrics, dict)
        return float(metrics[select_on]["mean"])

    best = max(range(len(results)), key=lambda i: (mean_of(results[i]), -i)) if results else None
    return {
        "n_cases": len(pools),
        "select_on": select_on,
        "axes": {k: list(v) for k, v in sorted(axes.items())},
        "baseline": baseline,
        "best": results[best] if best is not None else None,
        "results": results,
    }


def write_results(sweep: Mapping[str, object], path: Path) -> None:
    """Sorted keys, trailing newline: identical input gives an identical file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sweep, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = [
    "RECALL_KS",
    "ScoredPool",
    "evaluate_config",
    "rank_pool",
    "run_sweep",
    "weight_grid",
    "write_results",
]
