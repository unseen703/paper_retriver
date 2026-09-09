"""
Graph statistics (R2.13).

The first real use of NetworkX, which CLAUDE.md locks in for graph algorithms.
Connected components is the reason: it is the one figure here that is not a
count or a ratio, and hand-rolling a union-find to avoid a declared dependency
would be the wrong kind of thrift.

**Everything is measured over the undirected projection.** Two papers that both
cite a third are connected in any sense a reader means by the word, so a
directed reading of components would report a graph made almost entirely of
singletons. Density and average degree follow the same convention for
consistency: the panel describes a network, not a citation direction.

**Only edges with both endpoints in the graph count.** An edge into a boundary
paper is real and structurally load-bearing -- bibliographic coupling depends
on exactly those -- but it is not an edge of *this* graph. The panel sits
beside a picture that does not draw it, and a number contradicting the picture
next to it is worse than no number.

The graph is built from SQL on each request rather than cached. PLAN.md's rule
is "rebuilt from SQL on mutation", and at R2's ceiling of 2000 nodes the build
is milliseconds -- a cache here would be an invalidation bug waiting for R2.15
to clear the graph out from under it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
from sqlalchemy import Connection, text

from app.models import NODE_STATES
from app.repo import graph as graph_repo


@dataclass(frozen=True, slots=True)
class GraphStats:
    """What `<StatsPanel>` v1 renders. No PageRank -- that is R3."""

    node_count: int = 0
    edge_count: int = 0
    by_state: dict[str, int] = field(default_factory=dict)
    components: int = 0
    avg_degree: float = 0.0
    density: float = 0.0
    crawl_completeness: float = 0.0


def _internal_edges(conn: Connection, session_id: int) -> list[tuple[int, int]]:
    """Edges whose *both* endpoints have a node in this session."""
    rows = conn.execute(
        text(
            "SELECT e.citing_id, e.cited_id FROM edges e"
            " JOIN graph_nodes a ON a.paper_id = e.citing_id AND a.session_id = :sid"
            " JOIN graph_nodes b ON b.paper_id = e.cited_id  AND b.session_id = :sid"
        ),
        {"sid": session_id},
    )
    return [(int(citing), int(cited)) for citing, cited in rows]


def _crawl_completeness(conn: Connection, session_id: int) -> float:
    """
    |non-stub| / |nodes|.

    PLAN.md section I: PageRank over a partially crawled graph is biased, so R3
    suppresses that column below 0.6. This panel is where that number becomes
    visible before it silently distorts a ranking.
    """
    row = conn.execute(
        text(
            "SELECT COUNT(*), SUM(CASE WHEN p.crawl_state != 'STUB' THEN 1 ELSE 0 END)"
            " FROM graph_nodes g JOIN papers p ON p.id = g.paper_id"
            " WHERE g.session_id = :sid"
        ),
        {"sid": session_id},
    ).fetchone()
    if row is None or not row[0]:
        return 0.0
    return round(float(row[1] or 0) / float(row[0]), 4)


def compute_stats(conn: Connection, session_id: int) -> GraphStats:
    """
    Measure this session's graph.

    Every divisor is guarded. A brand-new session opens the panel too, and
    dividing by a node count of zero is the obvious way for this to 500 on day
    one -- so an empty graph reports zeroes, which is the true answer rather
    than a fallback.
    """
    by_state_ids = graph_repo.get_nodes_by_state(conn, session_id)
    # Every state present with a count, including the ones at zero. A panel
    # should render its rows from the shape rather than from a special case,
    # and "nothing disliked yet" must be distinguishable from "this build
    # forgot to count them".
    by_state = {state: len(by_state_ids.get(state, ())) for state in sorted(NODE_STATES)}

    node_ids = {pid for ids in by_state_ids.values() for pid in ids}
    node_count = len(node_ids)
    edges = _internal_edges(conn, session_id)

    # Annotated: nx.Graph is generic over its node type, and mypy cannot infer
    # int from `add_nodes_from` alone.
    projection: nx.Graph[int] = nx.Graph()
    projection.add_nodes_from(node_ids)
    # nx.Graph collapses A->B and B->A into one edge, which is what "undirected
    # projection" means and what keeps a reciprocal citation pair from counting
    # twice in the density.
    projection.add_edges_from(edges)

    edge_count = projection.number_of_edges()
    components = nx.number_connected_components(projection) if node_count else 0

    # 2E/N: each edge contributes a degree at both endpoints. The tempting E/N
    # is the average out-degree of a directed graph -- a different quantity
    # that looks similar enough on paper to survive a review.
    avg_degree = round(2 * edge_count / node_count, 4) if node_count else 0.0
    # 2E / (N(N-1)), the undirected maximum. N(N-1) is 0 for a single node, and
    # the honest answer there is 0.0 rather than an exception.
    possible = node_count * (node_count - 1)
    density = round(2 * edge_count / possible, 4) if possible else 0.0

    return GraphStats(
        node_count=node_count,
        edge_count=edge_count,
        by_state=by_state,
        components=components,
        avg_degree=avg_degree,
        density=density,
        crawl_completeness=_crawl_completeness(conn, session_id),
    )


__all__ = ["GraphStats", "compute_stats"]
