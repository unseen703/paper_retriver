"""
R4.5 -- the config sweep.

Journey:

    As someone about to trust a weight in ranking.yaml, I want to see what each
    setting does to Recall@20 on the benchmark, with an interval, so I can tell
    a real improvement from a lucky draw.

Expectations are worked out by hand, not observed from a run.
"""

from __future__ import annotations

import json

import pytest
from sweep import (
    ScoredPool,
    evaluate_config,
    rank_pool,
    run_sweep,
    weight_grid,
    write_results,
)

BASE = {"overlap": 2.0, "quality": 0.4, "hub": -0.6}


def _pool(case_id: int = 1) -> ScoredPool:
    # 1 and 2 are relevant. 1 has the overlap; 2 has the quality; 3 is a
    # high-quality hub that is not relevant.
    return ScoredPool(
        case_id=case_id,
        features={
            1: {"overlap": 1.0, "quality": 0.2, "hub": 0.0},
            2: {"overlap": 0.0, "quality": 0.9, "hub": 0.0},
            3: {"overlap": 0.0, "quality": 1.0, "hub": 1.0},
        },
        ground_truth=frozenset({1, 2}),
    )


def test_grid_is_the_cartesian_product_over_base() -> None:
    grid = weight_grid(BASE, {"quality": [0.0, 1.0], "overlap": [1.0, 2.0, 3.0]})
    assert len(grid) == 6
    assert all(g["hub"] == -0.6 for g in grid)
    assert {(g["overlap"], g["quality"]) for g in grid} == {
        (o, q) for o in (1.0, 2.0, 3.0) for q in (0.0, 1.0)
    }


def test_grid_enumeration_is_deterministic() -> None:
    a = weight_grid(BASE, {"quality": [0.0, 1.0], "overlap": [1.0, 2.0]})
    b = weight_grid(BASE, {"overlap": [1.0, 2.0], "quality": [0.0, 1.0]})
    assert a == b


def test_an_axis_that_names_no_weight_raises() -> None:
    with pytest.raises(ValueError, match="qualty"):
        weight_grid(BASE, {"qualty": [0.0]})


def test_ranking_follows_the_weights() -> None:
    # overlap-heavy: 1 (2.08) > 2 (0.36)... 3 is 0.4 - 0.6 = -0.2
    assert rank_pool(_pool(), BASE) == [1, 2, 3]
    # quality-only: 3 (1.0) > 2 (0.9) > 1 (0.2)
    assert rank_pool(_pool(), {"quality": 1.0}) == [3, 2, 1]


def test_ties_break_on_ascending_paper_id() -> None:
    pool = ScoredPool(1, {9: {"a": 1.0}, 4: {"a": 1.0}, 7: {"a": 1.0}}, frozenset({4}))
    assert rank_pool(pool, {"a": 1.0}) == [4, 7, 9]


def test_hub_sign_is_the_sign_in_the_score() -> None:
    # Flipping hub positive must lift the hub (3) above where it sat.
    assert rank_pool(_pool(), {"quality": 0.4, "hub": 5.0})[0] == 3


def test_metrics_are_worked_by_hand() -> None:
    # Under quality-only the ranking is [3, 2, 1]. Both relevant papers are in the
    # top 3, so recall@10 = 1; the first hit is at rank 2, so mrr = 0.5.
    m = evaluate_config([_pool()], {"quality": 1.0})["metrics"]
    assert isinstance(m, dict)
    assert m["recall@10"]["mean"] == 1.0
    assert m["mrr"]["mean"] == 0.5
    assert m["hit@10"]["mean"] == 1.0


def test_ci_brackets_the_mean() -> None:
    pools = [_pool(i) for i in range(1, 6)]
    m = evaluate_config(pools, BASE)["metrics"]
    assert isinstance(m, dict)
    low, high = m["ndcg@20"]["ci95"]
    assert low <= m["ndcg@20"]["mean"] <= high


def test_sweep_reports_baseline_and_picks_best_with_stable_ties() -> None:
    # Truth is only paper 2. mrr aside, recall@10 is 1 for every config here, so
    # all grid points tie and the FIRST must win.
    pool = ScoredPool(1, _pool().features, frozenset({2}))
    out = run_sweep([pool], BASE, {"quality": [0.0, 1.0]})
    results = out["results"]
    assert isinstance(results, list) and len(results) == 2
    assert out["best"] == results[0]
    assert out["baseline"]["weights"] == dict(sorted(BASE.items()))  # type: ignore[index]
    assert out["n_cases"] == 1


def test_sweep_picks_the_better_config_on_the_selection_metric() -> None:
    pool = ScoredPool(
        1,
        {
            1: {"quality": 1.0, "overlap": 0.0},
            2: {"quality": 0.0, "overlap": 1.0},
            3: {"quality": 0.5, "overlap": 0.5},
        },
        frozenset({2}),
    )
    out = run_sweep(
        [pool],
        {"quality": 0.0, "overlap": 1.0},
        {"quality": [0.0, 5.0]},
        select_on="mrr",
    )
    best = out["best"]
    assert isinstance(best, dict)
    assert best["weights"]["quality"] == 0.0  # overlap alone ranks 2 first


def test_results_file_is_byte_identical_across_runs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pools = [_pool(i) for i in range(1, 4)]
    axes = {"quality": [0.0, 1.0], "overlap": [1.0, 2.0]}
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    write_results(run_sweep(pools, BASE, axes), a)
    write_results(run_sweep(list(reversed(pools)), BASE, axes), b)
    assert a.read_bytes() == b.read_bytes()
    assert json.loads(a.read_text())["n_cases"] == 3


def test_empty_benchmark_yields_zeros_not_a_crash() -> None:
    out = run_sweep([], BASE, {"quality": [0.0]})
    assert out["n_cases"] == 0
    m = out["baseline"]["metrics"]  # type: ignore[index]
    assert m["recall@20"]["mean"] == 0.0
