"""
features.py
-----------
Export node features from the citation graph for downstream ML / recommendation.

export_node_features() produces a DataFrame (and optionally CSV/Parquet) with
one row per paper and columns ready to feed a ranking or scoring model.

get_ranked_candidates() returns a ranked list of non-seed papers, sorted by
the strongest graph signals (co-citation > PageRank > citation count).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

try:
    import pyarrow  # noqa: F401
    _HAS_PYARROW = True
except ImportError:
    _HAS_PYARROW = False

from storage import Store


def export_node_features(
    store: Store,
    output_path: Optional[str | Path] = "node_features.csv",
) -> "Optional[pd.DataFrame]":
    """
    Join papers + network_metrics and export as CSV (and Parquet if pyarrow available).

    Columns:
        paper_id, title, year, field_of_study (first entry), citation_count,
        co_citation_freq, pagerank, in_degree, distance_from_seed, seed_connections_count
    """
    if not _HAS_PANDAS:
        logger.error("pandas is not installed — cannot export features.")
        return None

    rows = store.all_papers_with_metrics()
    if not rows:
        logger.warning("No papers in store to export.")
        return pd.DataFrame()

    records = []
    for r in rows:
        fos_raw = r.get("field_of_study")
        fos_list = json.loads(fos_raw) if fos_raw else []
        sc_raw = r.get("seed_connections")
        sc_list = json.loads(sc_raw) if sc_raw else []
        records.append({
            "paper_id": r["paper_id"],
            "title": r["title"],
            "year": r.get("year"),
            "field_of_study": fos_list[0] if fos_list else None,
            "field_of_study_all": fos_list,
            "venue": r.get("venue"),
            "citation_count": r.get("citation_count") or 0,
            "co_citation_freq": r.get("co_citation") or 0,
            "pagerank": r.get("pagerank") or 0.0,
            "in_degree": r.get("in_degree") or 0,
            "distance_from_seed": r.get("distance_from_seed"),
            "seed_connections_count": len(sc_list),
            "seed_connections": sc_list,
            "source_api": r.get("source_api"),
            "label": r.get("label"),
        })

    df = pd.DataFrame(records)

    if output_path:
        out = Path(output_path)
        df.to_csv(out, index=False)
        logger.info("Exported %d rows to %s", len(df), out)
        if _HAS_PYARROW:
            parquet_path = out.with_suffix(".parquet")
            df.to_parquet(parquet_path, index=False)
            logger.info("Also wrote %s", parquet_path)

    return df


def get_ranked_candidates(
    store: Store,
    seed_ids: list[str],
    top_k: int = 100,
) -> list[dict]:
    """
    Return up to top_k non-seed candidate papers ranked by:
      1. co_citation_freq (most shared citations with seeds first)
      2. pagerank (centrality in the subgraph)
      3. citation_count (external popularity)

    Only returns papers with label IS NULL (not yet shown to the user).
    """
    seed_set = set(seed_ids)
    rows = store.all_papers_with_metrics()

    candidates = []
    for r in rows:
        if r["paper_id"] in seed_set:
            continue
        if r.get("label") is not None:
            continue
        candidates.append({
            "paper_id": r["paper_id"],
            "title": r["title"],
            "year": r.get("year"),
            "venue": r.get("venue"),
            "citation_count": r.get("citation_count") or 0,
            "co_citation_freq": r.get("co_citation") or 0,
            "pagerank": r.get("pagerank") or 0.0,
            "in_degree": r.get("in_degree") or 0,
            "distance_from_seed": r.get("distance_from_seed"),
            "source_api": r.get("source_api"),
        })

    candidates.sort(
        key=lambda x: (x["co_citation_freq"], x["pagerank"], x["citation_count"]),
        reverse=True,
    )
    return candidates[:top_k]
