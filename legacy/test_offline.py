"""
Offline sanity check for storage.py + citation_network.py using synthetic data
(no network calls). Run with: python test_offline.py
"""
import tempfile
from pathlib import Path

from storage import Paper, Store
from citation_network import build_graph, score_candidates


def make_paper(pid, title, embedding=None, citation_count=10):
    return Paper(paper_id=pid, title=title, abstract=f"Abstract of {title}",
                 year=2023, citation_count=citation_count, embedding=embedding)


def main():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "test.sqlite3")

        seed = make_paper("S1", "Enzyme Function Prediction via Contrastive Learning", embedding=[1, 0, 0])
        store.upsert_paper(seed, label="seed")

        # candidate cited by the seed (a reference) -- should score decently
        ref_candidate = make_paper("C1", "Protein Language Models for Function", embedding=[0.9, 0.1, 0])
        store.upsert_paper(ref_candidate)
        store.add_edges("S1", ["C1"])

        # candidate that cites the seed AND shares a reference with it -- should score highest
        strong_candidate = make_paper("C2", "Reaction-Aware Enzyme Representation Learning", embedding=[0.95, 0.05, 0])
        store.upsert_paper(strong_candidate)
        store.add_edges("C2", ["S1"])  # C2 cites S1
        store.add_edges("C2", ["C1"])  # C2 also cites C1, same as S1 does -> bibliographic coupling
        store.add_edges("S1", ["C1"])

        # irrelevant candidate, unrelated embedding, no direct graph link to seed
        weak_candidate = make_paper("C3", "Large Scale Image Classification", embedding=[0, 0, 1])
        store.upsert_paper(weak_candidate)
        store.add_edges("C3", ["ZZZ_unrelated"])
        store.upsert_paper(make_paper("ZZZ_unrelated", "Unrelated paper"))

        graph = build_graph(store)
        ranked = score_candidates(store, graph, [seed], [])

        print("Ranking (highest score first):")
        for sc in ranked:
            print(f"  {sc.paper.paper_id:>4}  score={sc.combined_score:.3f}  reasons={sc.reasons}")

        ids_in_order = [sc.paper.paper_id for sc in ranked]
        assert ids_in_order[0] == "C2", f"expected C2 (strongest link) first, got {ids_in_order}"
        assert "C3" in ids_in_order, "weak/unrelated candidate should still appear, just ranked low"
        assert ids_in_order.index("C2") < ids_in_order.index("C3")
        assert ids_in_order.index("C1") < ids_in_order.index("C3")
        print("\nAll assertions passed.")


if __name__ == "__main__":
    main()
