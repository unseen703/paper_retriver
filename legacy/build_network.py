"""
build_network.py
----------------
Main CLI for building a citation network from a list of seed papers.

Usage
-----
  python build_network.py --seeds "Attention is All You Need" \\
                                   "https://arxiv.org/abs/1810.04805" \\
                          --hops 2 --max-nodes 500 --export features.csv

  python build_network.py --seeds-file seed_titles.txt \\
                          --hops 1 --max-nodes 200

Seed inputs can be:
  - Free-text paper titles
  - DOI URLs (https://doi.org/10.xxx)
  - arXiv URLs (https://arxiv.org/abs/XXXX.XXXXX)
  - Semantic Scholar paper URLs or IDs
  - OpenAlex work URLs (https://openalex.org/W...)

Output
------
  - SQLite database (paper_store.sqlite3) with papers + edges + metrics
  - CSV (and Parquet if pyarrow is installed) with node features
  - Console table of top-K ranked candidate papers
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Load .env before importing project modules that read env vars
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv optional; keys can be set in shell environment

from api_cache import ApiCache
from semantic_scholar_client import SemanticScholarClient
from openalex_client import OpenAlexClient
from storage import Store
from resolver import resolve_seeds
from graph_expander import expand_network, hydrate_ids
from citation_network import build_graph, compute_metrics
from features import export_node_features, get_ranked_candidates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("build_network")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Build a citation network from seed papers and export ML features."
    )
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--seeds", nargs="+", metavar="SEED",
        help="One or more seed papers (title strings or URLs). Quote titles with spaces.",
    )
    group.add_argument(
        "--seeds-file", type=Path, metavar="FILE",
        help="Text file with one seed per line (# comments ignored).",
    )
    ap.add_argument(
        "--hops", type=int, default=2,
        help="BFS expansion depth (default: 2). 1=immediate neighbors, 2=neighbors of neighbors.",
    )
    ap.add_argument(
        "--max-nodes", type=int, default=1000,
        help="Maximum total papers in the network (default: 1000).",
    )
    ap.add_argument(
        "--per-paper", type=int, default=50,
        help="Max references/citations fetched per paper (default: 50).",
    )
    ap.add_argument(
        "--max-new", type=int, default=None,
        help="Max papers added in a single expansion run (default: same as --max-nodes).",
    )
    ap.add_argument(
        "--export", type=Path, default=Path("node_features.csv"),
        help="Output path for node feature CSV (default: node_features.csv).",
    )
    ap.add_argument(
        "--db", type=Path, default=Path(__file__).parent / "paper_store.sqlite3",
        help="SQLite database path.",
    )
    ap.add_argument(
        "--top", type=int, default=20,
        help="Number of top candidates to print at the end (default: 20).",
    )
    ap.add_argument(
        "--skip-expand", action="store_true",
        help="Skip network expansion; only recompute metrics and export features.",
    )
    ap.add_argument(
        "--hydrate", action="store_true",
        help="After expansion, batch-fetch full metadata for all new nodes via S2 batch API.",
    )
    return ap.parse_args()


def merge_seed_ids_with_labeled_papers(seed_ids: list[str], store: Store) -> list[str]:
    """
    Merge freshly-resolved seed_ids with any papers already labeled 'seed'
    or 'liked' in the store (e.g. liked via the CLI feedback loop, or added
    via the GUI's add-by-title, which stores as 'seed'). Ensures a re-run
    treats those papers as expansion starting points too, so their
    references get the same full/uncapped treatment as any other seed.
    Order preserved; no duplicates.
    """
    merged = list(seed_ids)
    seen = set(merged)
    for p in store.papers_with_label(["seed", "liked"]):
        if p.paper_id not in seen:
            merged.append(p.paper_id)
            seen.add(p.paper_id)
    return merged


def load_seeds_from_file(path: Path) -> list[str]:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def print_candidates_table(candidates: list[dict], top_n: int) -> None:
    if not candidates:
        print("\n(No unlabeled candidates found.)")
        return
    print(f"\n{'='*80}")
    print(f"Top {min(top_n, len(candidates))} Candidate Papers")
    print(f"{'='*80}")
    fmt = "{:>3}  {:>4}  {:>6}  {:>7}  {:>8}  {}"
    print(fmt.format("#", "Year", "CoSeed", "PgRank", "CiteCnt", "Title"))
    print("-" * 80)
    for i, c in enumerate(candidates[:top_n], 1):
        title = (c["title"] or "")[:55]
        if len(c["title"] or "") > 55:
            title += "…"
        print(fmt.format(
            i,
            c["year"] or "n/a",
            c["co_citation_freq"],
            f"{c['pagerank']:.4f}",
            c["citation_count"],
            title,
        ))


def main() -> None:
    args = parse_args()

    # ---- collect seed inputs --------------------------------------------------
    if args.seeds:
        seed_inputs = args.seeds
    else:
        if not args.seeds_file.exists():
            print(f"[error] seeds file not found: {args.seeds_file}", file=sys.stderr)
            sys.exit(1)
        seed_inputs = load_seeds_from_file(args.seeds_file)

    if not seed_inputs:
        print("[error] No seeds provided.", file=sys.stderr)
        sys.exit(1)

    if len(seed_inputs) < 5 or len(seed_inputs) > 10:
        print(
            f"[warn] {len(seed_inputs)} seed(s) provided. Recommended range is 5–10.",
            file=sys.stderr,
        )

    # ---- init clients ---------------------------------------------------------
    cache = ApiCache()
    s2 = SemanticScholarClient(cache=cache)
    oa = OpenAlexClient(cache=cache)
    store = Store(args.db)

    print(f"\nInitialized. DB: {args.db}  Cache: {cache.cache_dir}")
    print(f"S2 API key: {'set' if s2.api_key else 'NOT set (rate-limited)'}")

    # ---- resolve seeds --------------------------------------------------------
    print(f"\nResolving {len(seed_inputs)} seed paper(s)...")
    resolved, unresolved = resolve_seeds(seed_inputs, s2, oa, store, verbose=True)

    if unresolved:
        print(f"\n[warn] {len(unresolved)} seed(s) could not be resolved:")
        for u in unresolved:
            print(f"  - {u!r}")

    if not resolved:
        print("[error] No seeds resolved. Cannot build network.", file=sys.stderr)
        sys.exit(1)

    seed_ids = [r.paper_id for r in resolved]
    print(f"\n{len(resolved)} seeds resolved. Seed paper IDs:")
    for r in resolved:
        print(f"  {r.paper_id}  ({r.source}, conf={r.confidence:.2f})")

    # Pick up any papers already liked (CLI) or added via the GUI (stored as
    # 'seed') since the last run -- they get the same uncapped-reference
    # treatment as any other seed.
    before = len(seed_ids)
    seed_ids = merge_seed_ids_with_labeled_papers(seed_ids, store)
    if len(seed_ids) > before:
        print(f"Also including {len(seed_ids) - before} existing liked/GUI-added paper(s) as seeds.")

    # ---- expand graph ---------------------------------------------------------
    if not args.skip_expand:
        print(f"\nExpanding citation network (hops={args.hops}, max_nodes={args.max_nodes})...")
        max_new = args.max_new if args.max_new is not None else args.max_nodes
        added = expand_network(
            s2, oa, store, seed_ids,
            hops=args.hops,
            max_nodes=args.max_nodes,
            max_new=max_new,
            per_paper_limit=args.per_paper,
        )
        print(f"Expansion done. Added {added} new papers.")

        if args.hydrate:
            all_ids = list(store.all_paper_ids())
            print(f"Batch-hydrating metadata for {len(all_ids)} papers...")
            hydrated = hydrate_ids(all_ids, s2, store)
            print(f"Hydrated {hydrated} papers.")
    else:
        print("Skipping expansion (--skip-expand).")

    # ---- compute graph metrics ------------------------------------------------
    graph = build_graph(store)
    print(f"\nGraph: {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges.")
    print("Computing graph metrics (PageRank, co-citation, in-degree)...")
    compute_metrics(store, graph, seed_ids)
    print("Metrics computed.")

    # ---- export features -------------------------------------------------------
    print(f"\nExporting node features to {args.export}...")
    df = export_node_features(store, output_path=args.export)
    if df is not None:
        print(f"Exported {len(df)} rows × {len(df.columns)} columns.")

    # ---- ranked candidates preview -------------------------------------------
    candidates = get_ranked_candidates(store, seed_ids, top_k=args.top)
    print_candidates_table(candidates, args.top)

    total = store.all_paper_ids()
    print(f"\nDone. Total papers in DB: {len(total)}")
    print(f"Feature file: {args.export}")


if __name__ == "__main__":
    main()
