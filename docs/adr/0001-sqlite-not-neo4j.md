# ADR 0001 — SQLite, not Neo4j

**Status:** accepted · **Date:** 2026-09-08 · **Supersedes:** nothing

## Context

This is a citation-graph recommender. The obvious reading is "graph problem,
therefore graph database", and Neo4j is the default answer to that reading.
The question this record settles is why a project whose central object is a
graph stores it in a relational database instead.

The workload is specific, and it is not what a graph database optimises for:

* **One user, one machine, no network.** There is no cluster, no concurrent
  writer, and no service boundary to cross.
* **The corpus is small.** PLAN.md caps a session's graph at 2,000 nodes. The
  whole corpus after months of use is tens of thousands of papers — a size
  where the entire graph fits in memory many times over.
* **Traversals are shallow.** Expansion is one hop. Bibliographic coupling and
  co-citation are two-hop counts. Nothing here needs variable-length path
  matching, which is the query class Neo4j exists to make fast.
* **Most of the work is not graph work.** Filtering, deduplication, caching
  API responses, and the audit log are all ordinary relational problems, and
  they are the majority of the code.

## Decision

Store papers, edges, sessions, events and filter decisions in **SQLite in WAL
mode**, accessed through **SQLAlchemy Core**. Rebuild a **NetworkX** `DiGraph`
from SQL whenever a graph algorithm is needed.

## Consequences

**The database is a file.** Backup is `cp`, inspection is any SQLite browser,
and the fixture that makes the whole test suite run offline is a committed
`.db`. Reproducing a bug means sending someone a file.

**Graph algorithms are free.** NetworkX has PageRank, personalised PageRank,
components and centralities already. Rebuilding a 2,000-node graph from SQL is
milliseconds — PLAN.md's instruction is to measure it and *not* build cache
invalidation until profiling demands it, because a stale graph cache is a much
worse bug than a slow one.

**Multi-hop traversal in SQL is unpleasant**, and we accept that cost because
we do not do it. If a future release genuinely needs variable-length paths,
NetworkX still answers it in memory; the day that stops being true is the day
to revisit this record.

**No operational surface.** Nothing to install, run, secure, or upgrade. For a
local-first single-user tool that is not a minor convenience — a graph
database would be the only daemon in the system, and the only thing that could
be down.

## Alternatives considered

**Neo4j.** Rejected. Its advantages — variable-length path queries,
distributed storage, a graph query language — address none of the four
workload facts above, while adding a server process, a second data model to
keep in sync, and a much heavier test setup. The Cypher ergonomics are real
but buy little when the traversals are one hop deep.

**PostgreSQL.** Rejected as premature rather than wrong. It brings a server
process and a connection story for a workload with exactly one writer. SQLite
in WAL mode handles that writer with no daemon at all. If this ever became
multi-user, Postgres — not Neo4j — is the migration.

**An embedded graph store (e.g. KùzuDB).** Rejected on maturity and on the
same reasoning as Neo4j: the graph half of the problem is already solved by
NetworkX, and the relational half would then need a second store anyway.

## What would change our mind

* Sessions routinely exceeding ~50,000 nodes, where rebuilding the NetworkX
  graph per request stops being milliseconds.
* A real need for variable-length path queries — "papers within 4 hops that
  share an author" — evaluated over the full corpus rather than a session.
* More than one concurrent writer, which SQLite's single-writer model makes
  genuinely awkward.

None of these are true today, and the first two are measurable rather than
matters of taste. Measure before revisiting.
