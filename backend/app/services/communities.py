"""
Community detection (R6): seeded Louvain over the undirected projection.

PLAN.md section F wants a `community_id` per node so layout can pull same-
community edges tight and push cross-community ones long. This module only
assigns the ids; the layout hints, hulls and labels build on them.

**Louvain, not Leiden -- a deliberate substitution.** BUILD.md says Leiden,
PLAN.md section F says "Leiden/Louvain". NetworkX's `leiden_communities` is
only a dispatch stub (it raises `NotImplementedError` without an external
backend), and CLAUDE.md locks NetworkX for graph algorithms: `leidenalg` plus
`igraph` would be a second graph stack for one function. Louvain ships in
NetworkX, takes a seed, and is what PLAN.md names as the alternative. Swapping
to Leiden later changes one call; ids are renumbered below either way.

**Deterministic by construction** (CLAUDE.md rule 7). The algorithm is seeded,
and the ids it returns are an accident of its internal order, so they are
discarded: communities are renumbered by `(-size, smallest member paper id)`.
Two runs over the same graph agree, and `0` is always the biggest cluster. The
graph is also built from sorted inputs, because insertion order leaks into
the algorithm's tie-breaking.

Direction is dropped on purpose -- "citing -> cited" is the stored relation,
but cohesion is symmetric, and PLAN.md specifies the undirected projection.
"""

from __future__ import annotations

from collections.abc import Iterable

import networkx as nx
from sqlalchemy import Connection

from app.repo import edges as edges_repo
from app.repo import graph as graph_repo

#: Fixed so a rerun cannot reshuffle clusters on screen.
LEIDEN_SEED = 0
LEIDEN_RESOLUTION = 1.0


def detect_communities(
    node_ids: Iterable[int],
    edges: Iterable[tuple[int, int]],
    *,
    seed: int = LEIDEN_SEED,
    resolution: float = LEIDEN_RESOLUTION,
) -> dict[int, int]:
    """
    Map every node to a community id. Pure: no I/O.

    Edges with an endpoint outside `node_ids` and self-loops are ignored. A node
    with no edges is its own community rather than being dropped -- every drawn
    node gets an id, so the UI never has to special-case `None`.
    """
    nodes = sorted(set(node_ids))
    if not nodes:
        return {}
    wanted = set(nodes)
    graph: nx.Graph[int] = nx.Graph()
    graph.add_nodes_from(nodes)
    undirected = {
        (min(a, b), max(a, b)) for a, b in edges if a != b and a in wanted and b in wanted
    }
    graph.add_edges_from(sorted(undirected))

    found = nx.community.louvain_communities(graph, resolution=resolution, seed=seed)
    ordered = sorted(found, key=lambda members: (-len(members), min(members)))
    return {paper_id: cid for cid, members in enumerate(ordered) for paper_id in members}


def refresh_communities(conn: Connection, session_id: int) -> dict[int, int]:
    """Recompute and persist this session's communities. Returns the assignment."""
    ids = sorted(graph_repo.get_node_ids(conn, session_id))
    edges = edges_repo.get_edges_between(conn, ids)
    assignment = detect_communities(ids, [(e.citing_id, e.cited_id) for e in edges])
    graph_repo.set_communities(conn, session_id, assignment)
    return assignment


__all__ = ["LEIDEN_RESOLUTION", "LEIDEN_SEED", "detect_communities", "refresh_communities"]
