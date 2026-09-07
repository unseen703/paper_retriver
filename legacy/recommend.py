"""
recommend.py
------------
Main entry point.

Usage
-----
First run (resolve your seed titles and get an initial shortlist):
    python recommend.py --seeds seed_titles.txt

Later runs (re-uses everything already fetched, pulls fresh candidates around
any papers you liked last time, and asks again):
    python recommend.py --seeds seed_titles.txt --top 15

Each candidate is shown with its title, year, citation count, why it was
recommended, and a shortened abstract. You respond:
    y = like it (becomes a seed for next time)
    n = not relevant (used as a negative signal)
    s = skip / not sure (won't be re-shown, but also won't count against it)
    q = stop the session early

State (papers, citation edges, and your labels) persists in paper_store.sqlite3
next to this script, so you can stop and resume anytime.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from storage import Paper, Store
from semantic_scholar_client import SemanticScholarClient, s2_record_to_kwargs
from openalex_client import OpenAlexClient
from api_cache import ApiCache
from citation_network import build_graph, score_candidates
from exploration import explore_paper
from graph_expander import expand_network


def load_titles(path: Path) -> list[str]:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def resolve_seed_titles(client: SemanticScholarClient, store: Store, titles: list[str]) -> list[Paper]:
    resolved = []
    for title in titles:
        match = client.search_paper_by_title(title)
        if not match:
            print(f"  [!] no Semantic Scholar match for: {title!r} -- skipping", file=sys.stderr)
            store.record_seed_title(title, None, 0.0)
            continue
        paper = Paper(**{**s2_record_to_kwargs(match), "abstract": None})
        # /search/match doesn't return abstract/embedding -- fetch the full record once.
        try:
            full = client.get_paper(paper.paper_id)
            paper = Paper(**s2_record_to_kwargs(full))
        except Exception as exc:
            print(f"  [!] could not fetch full record for {paper.paper_id!r}: {exc} -- using partial", file=sys.stderr)
        store.upsert_paper(paper, label="seed")
        store.record_seed_title(title, paper.paper_id, 1.0)
        resolved.append(paper)
        print(f"  [ok] {title!r} -> {paper.title} ({paper.year})")
    return resolved


DEFAULT_MAX_NODES = 2000


def expand_seed_network(client, oa_client, store: Store, seed_papers: list[Paper],
                        per_paper_limit: int) -> int:
    """
    Expand the network around resolved seed papers. max_new is kept equal to
    max_nodes -- expand_network's own default (300) would otherwise silently
    throttle growth far below the budget this caller actually configured.
    """
    return expand_network(
        client, oa_client, store,
        [p.paper_id for p in seed_papers],
        hops=1,
        max_nodes=DEFAULT_MAX_NODES,
        max_new=DEFAULT_MAX_NODES,
        per_paper_limit=per_paper_limit,
    )


def wrap(text: str | None, width: int = 100) -> str:
    if not text:
        return "(no abstract available)"
    return "\n".join(textwrap.wrap(text, width=width)[:4])


def run_feedback_session(store: Store, ranked, top_n: int, s2_client, oa_client) -> None:
    print(f"\n=== Top {min(top_n, len(ranked))} candidate papers ===\n")
    for i, sc in enumerate(ranked[:top_n], start=1):
        p = sc.paper
        print(f"[{i}/{min(top_n, len(ranked))}] {p.title}  ({p.year or 'n/a'}, {p.citation_count} citations)")
        print(f"      score={sc.combined_score:.3f}  why: {'; '.join(sc.reasons)}")
        print(f"      {wrap(p.abstract)}")
        if p.arxiv_id:
            print(f"      arXiv: {p.arxiv_id}")
        while True:
            choice = input("      (y)es interested / (n)o / (s)kip / (q)uit session > ").strip().lower()
            if choice in ("y", "n", "s", "q"):
                break
            print("      please enter y, n, s, or q")
        if choice == "q":
            print("Ending session early.")
            break
        elif choice == "y":
            store.set_label(p.paper_id, "liked")
            try:
                r = explore_paper(p.paper_id, s2_client, oa_client, store)
                print(f"      explored: +{r['refs_added']} refs, +{r['cites_added']} citing papers "
                      f"(skipped {r['refs_skipped'] + r['cites_skipped']} out-of-domain)")
            except Exception as e:
                print(f"      [!] exploration failed: {e}", file=sys.stderr)
        elif choice == "n":
            store.set_label(p.paper_id, "disliked")
        else:
            store.set_label(p.paper_id, "skipped")
        print()


def main():
    ap = argparse.ArgumentParser(description="Citation-network-based paper recommender")
    ap.add_argument("--seeds", type=Path, default=Path(__file__).parent / "seed_titles.txt",
                     help="text file with one seed paper title per line")
    ap.add_argument("--db", type=Path, default=Path(__file__).parent / "paper_store.sqlite3")
    ap.add_argument("--top", type=int, default=10, help="how many candidates to review this session")
    ap.add_argument("--per-paper-neighbors", type=int, default=30,
                     help="max references/citations pulled per seed paper")
    ap.add_argument("--skip-expand", action="store_true",
                     help="don't re-fetch neighbors from the API; just re-rank what's already stored")
    args = ap.parse_args()

    store = Store(args.db)
    cache = ApiCache()
    client = SemanticScholarClient(cache=cache)
    oa_client = OpenAlexClient(cache=cache)

    already_seeded = {p.title for p in store.papers_with_label(["seed", "liked"])}
    if args.seeds.exists():
        titles = load_titles(args.seeds)
        new_titles = [t for t in titles if t not in already_seeded]
        if new_titles:
            print(f"Resolving {len(new_titles)} seed title(s) against Semantic Scholar...")
            resolve_seed_titles(client, store, new_titles)
    else:
        print(f"[!] seed file {args.seeds} not found; using whatever is already in {args.db}", file=sys.stderr)

    seed_papers = store.papers_with_label(["seed", "liked"])
    negative_papers = store.papers_with_label(["disliked"])
    if not seed_papers:
        print("No seed papers resolved yet -- nothing to recommend from. Exiting.")
        return

    if not args.skip_expand:
        print(f"Expanding citation network around {len(seed_papers)} seed paper(s)...")
        expand_seed_network(client, oa_client, store, seed_papers, args.per_paper_neighbors)

    graph = build_graph(store)
    print(f"Graph has {graph.number_of_nodes()} papers, {graph.number_of_edges()} citation edges.")

    ranked = score_candidates(store, graph, seed_papers, negative_papers)

    # Blend in Semantic Scholar's own learned recommendation model as a second,
    # independent signal -- boosts anything it also flagged as relevant.
    try:
        s2_recs = client.recommend(
            [p.paper_id for p in seed_papers],
            [p.paper_id for p in negative_papers],
            limit=50,
        )
        s2_rec_ids = {r["paperId"] for r in s2_recs if r.get("paperId")}
        for sc in ranked:
            if sc.paper.paper_id in s2_rec_ids:
                sc.combined_score = min(1.0, sc.combined_score + 0.15)
                sc.reasons.append("also surfaced by Semantic Scholar's recommendation model")
        ranked.sort(key=lambda sc: sc.combined_score, reverse=True)
    except Exception as e:  # network hiccup shouldn't kill the whole run
        print(f"  [!] recommendations API call failed, continuing with graph-only ranking ({e})", file=sys.stderr)

    if not ranked:
        print("No new candidates to show (try --skip-expand off, or add more seed papers).")
        return

    run_feedback_session(store, ranked, args.top, client, oa_client)

    liked_now = store.papers_with_label(["liked"])
    print(f"\nSession done. You now have {len(liked_now) + len(seed_papers)} seed/liked papers total.")
    print("Run this script again to expand the network further and get a refined shortlist.")


if __name__ == "__main__":
    main()
