"""
Structural signals over the citation graph (R3).

BUILD.md asks for a `nx.DiGraph` built from SQL, PageRank, reverse PageRank and
degrees. PLAN.md section I then spends a page explaining why naive PageRank
over *this* graph is wrong, and that analysis is the part with teeth:

    "Your graph is a crawl, not a corpus. Frontier nodes have artificially
    truncated degree: a node whose citations you never fetched looks
    unimportant purely because you stopped fetching. Naive PageRank therefore
    systematically over-ranks well-crawled central nodes and under-ranks the
    frontier -- which is exactly backwards for discovery."

All four of its mitigations are implemented here, and the reasoning is written
up in `docs/algorithms.md`:

1. PageRank runs only over the subgraph where `crawl_state != STUB`.
2. Below `COMPLETENESS_FLOOR` the number is **suppressed**, not reported.
3. Degrees are computed regardless -- local measures depend only on
   neighbourhoods actually fetched, so they survive a thin crawl.
4. PageRank over the undirected projection is computed alongside, because the
   divergence between the two is itself the crawl-bias diagnostic.

**Suppressed means absent, not zero.** A zero is a score: it would sort every
paper last and look like a considered judgment. A missing key is the honest
shape for "this number would not mean anything yet", and it is what lets the
UI say so.

**Both directions are computed because they answer different questions.**
Forward PageRank over `citing -> cited` measures being cited by important
papers, which is influence. Reversed, it measures citing many important papers
-- which is what a survey does. PLAN.md calls that hub-ness.

**No caching.** BUILD.md is explicit: "do not build cache invalidation until
profiling says to". At R2's ceiling of 2000 nodes the build is milliseconds,
and an invalidation bug would cost far more than the rebuild ever will.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
from sqlalchemy import Connection, text

#: PLAN.md section I: "suppress the PageRank column below 60%".
COMPLETENESS_FLOOR = 0.6

#: NetworkX's default, and the value the analytic tests are worked out against.
DAMPING = 0.85


@dataclass(frozen=True, slots=True)
class GraphSignals:
    """
    Structural signals per paper id.

    `pagerank` is empty when suppressed -- see the module docstring on why that
    is not a dict of zeroes.
    """

    in_degree: dict[int, int] = field(default_factory=dict)
    out_degree: dict[int, int] = field(default_factory=dict)
    pagerank: dict[int, float] = field(default_factory=dict)
    reverse_pagerank: dict[int, float] = field(default_factory=dict)
    #: PageRank over the undirected projection. Diverging from `pagerank` is a
    #: crawl-bias signal rather than an error (PLAN.md mitigation 4).
    undirected_pagerank: dict[int, float] = field(default_factory=dict)
    crawl_completeness: float = 0.0
    pagerank_suppressed: bool = False


def _rows(
    conn: Connection, session_id: int
) -> tuple[list[tuple[int, bool]], list[tuple[int, int]]]:
    """
    (paper_id, is_crawled) for every node, and the edges between them.

    Both from one pair of queries, and the edges are filtered to the node set
    in Python rather than trusted from SQL alone -- the same reasoning as
    `stats.py`: these reads are not one snapshot, and `nx.add_edges_from`
    silently creates any endpoint it has not seen.
    """
    node_rows = conn.execute(
        text(
            "SELECT g.paper_id, p.crawl_state FROM graph_nodes g"
            " JOIN papers p ON p.id = g.paper_id"
            " WHERE g.session_id = :sid ORDER BY g.paper_id"
        ),
        {"sid": session_id},
    ).fetchall()
    nodes = [(int(pid), str(crawl) != "STUB") for pid, crawl in node_rows]

    edge_rows = conn.execute(
        text(
            "SELECT e.citing_id, e.cited_id FROM edges e"
            " JOIN graph_nodes a ON a.paper_id = e.citing_id AND a.session_id = :sid"
            " JOIN graph_nodes b ON b.paper_id = e.cited_id  AND b.session_id = :sid"
            " ORDER BY e.citing_id, e.cited_id"
        ),
        {"sid": session_id},
    ).fetchall()
    known = {pid for pid, _ in nodes}
    edges = [(int(a), int(b)) for a, b in edge_rows if int(a) in known and int(b) in known]
    return nodes, edges


def build_digraph(conn: Connection, session_id: int) -> nx.DiGraph[int]:
    """
    This session's graph as a directed NetworkX graph, `citing -> cited`.

    Nodes are added in sorted id order on purpose. PageRank is a power
    iteration, and the order in which floats accumulate changes the last digits
    of the result -- so an unsorted build makes two runs over an unchanged
    graph disagree, and a ranking that reshuffles when nothing happened is both
    unusable and unreproducible (CLAUDE.md rule 7).
    """
    nodes, edges = _rows(conn, session_id)
    graph: nx.DiGraph[int] = nx.DiGraph()
    graph.add_nodes_from(pid for pid, _ in nodes)
    graph.add_edges_from(edges)
    return graph


def _pagerank(graph: nx.DiGraph[int] | nx.Graph[int]) -> dict[int, float]:
    """PageRank, or an empty dict for an empty graph (NetworkX raises there)."""
    if graph.number_of_nodes() == 0:
        return {}
    return dict(nx.pagerank(graph, alpha=DAMPING))


def compute_signals(conn: Connection, session_id: int) -> GraphSignals:
    """
    Every structural signal for this session, with the crawl-bias rules applied.

    Degrees come from the whole graph; PageRank comes from the crawled
    subgraph, and only when enough of the graph has been crawled for the number
    to mean anything.
    """
    nodes, edges = _rows(conn, session_id)
    if not nodes:
        return GraphSignals()

    graph: nx.DiGraph[int] = nx.DiGraph()
    graph.add_nodes_from(pid for pid, _ in nodes)
    graph.add_edges_from(edges)

    # Degrees over the *whole* graph. PLAN.md mitigation 3: local measures
    # depend only on neighbourhoods actually fetched, so they stay meaningful
    # where PageRank does not.
    in_degree = {pid: int(graph.in_degree(pid)) for pid, _ in nodes}
    out_degree = {pid: int(graph.out_degree(pid)) for pid, _ in nodes}

    crawled = [pid for pid, is_crawled in nodes if is_crawled]
    completeness = round(len(crawled) / len(nodes), 4)
    suppressed = completeness < COMPLETENESS_FLOOR

    if suppressed:
        # Absent, not zeroed. See the module docstring.
        return GraphSignals(
            in_degree=in_degree,
            out_degree=out_degree,
            crawl_completeness=completeness,
            pagerank_suppressed=True,
        )

    # PLAN.md mitigation 1: only the crawled subgraph. A stub's truncated
    # degree is a fact about where the crawl stopped, not about the paper.
    subgraph: nx.DiGraph[int] = nx.DiGraph()
    subgraph.add_nodes_from(sorted(crawled))
    known = set(crawled)
    subgraph.add_edges_from((a, b) for a, b in edges if a in known and b in known)

    return GraphSignals(
        in_degree=in_degree,
        out_degree=out_degree,
        pagerank=_pagerank(subgraph),
        # Hub-ness: citing many important papers, which is what a survey does.
        #
        # `copy=False` and `as_view=True` on purpose. Measured at 2000 nodes and
        # 8000 edges: one PageRank is ~6ms, but taking two full copies of the
        # graph to get the other two directions cost ~80ms on its own -- more
        # than everything else combined. Views read the same edges from a
        # different angle instead of duplicating them.
        reverse_pagerank=_pagerank(subgraph.reverse(copy=False)),
        # PLAN.md mitigation 4: the diagnostic is the divergence between this
        # and the directed answer, which cannot be seen without both.
        undirected_pagerank=_pagerank(subgraph.to_undirected(as_view=True)),
        crawl_completeness=completeness,
        pagerank_suppressed=False,
    )


__all__ = [
    "COMPLETENESS_FLOOR",
    "DAMPING",
    "GraphSignals",
    "build_digraph",
    "compute_signals",
]
