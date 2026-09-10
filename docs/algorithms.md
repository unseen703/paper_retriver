# Algorithms — what the numbers mean

PLAN.md section I asks for this document specifically, and says why:

> Writing this analysis into `docs/algorithms.md` is worth more in a review
> than the PageRank implementation itself. It shows you understand what your
> numbers *mean*.

Implementation: `backend/app/services/graphops.py`. Tests:
`backend/tests/integration/test_graphops.py`.

---

## The central problem: this graph is a crawl, not a corpus

Every structural measure here assumes it is looking at a network. It is
looking at *the part of a network we happened to fetch*, and the difference is
not a rounding error.

A paper at the frontier has artificially truncated degree. Its citations were
never fetched, so it looks unimportant — **not because it is, but because we
stopped**. Naive PageRank therefore systematically over-ranks the
well-crawled centre and under-ranks the frontier.

That is exactly backwards for a discovery tool. The frontier is where the
papers you have not read live.

### The four mitigations, all implemented

| # | Mitigation | Where |
|---|---|---|
| 1 | PageRank runs only over `crawl_state != STUB` | `compute_signals` builds a subgraph |
| 2 | Suppress the number below 60% completeness | `COMPLETENESS_FLOOR`, `pagerank_suppressed` |
| 3 | Prefer local measures — degrees, co-citation, coupling | degrees computed regardless of suppression |
| 4 | Compute undirected PageRank too; divergence is the diagnostic | `undirected_pagerank` |

**Suppressed means absent, not zero.** A zero is a score: it sorts every paper
last and looks like a considered judgment. A missing key is the honest shape
for "this number would not mean anything yet".

**As of this writing the live graph sits at 2% completeness** — 3 of 127
papers are non-stub. Every structural score would currently be measuring the
three seeds. The StatsPanel says so in words, and PageRank is suppressed.

---

## PageRank, in both directions

Edges run `citing → cited` (CLAUDE.md, locked). So:

- **Forward PageRank** — rank flows from a citing paper to the papers it
  cites. High score means *cited by important papers*. This is influence.
- **Reverse PageRank** — the same computation on the reversed graph. High
  score means *citing many important papers*. This is hub-ness, and it
  approximates survey-ness.

These are different questions, not competing answers to one question. A survey
is the least important paper in the forward ranking and the most important in
the reverse one, and both readings are correct.

### Dangling nodes are the common case here

A paper whose references we have not fetched cites nothing *in this graph*. It
is a dangling node, and on a partially crawled graph most papers are. PageRank
redistributes a dangling node's mass evenly across all nodes; this is not an
edge case to be noted and skipped, it is the dominant behaviour of the
algorithm on this data.

`test_a_star_gives_the_hub_the_analytic_value` pins it against a derivation
worked out by hand. An earlier version of that test assumed the dangling mass
simply vanished — which is the mistake the case now exists to catch.

### Damping

α = 0.85, NetworkX's default. The analytic tests are derived against that
value rather than against whatever the library happens to do.

---

## Determinism

Nodes are inserted in sorted id order. PageRank is a power iteration, and the
order in which floats accumulate changes the last digits of the result — so an
unsorted build makes two runs over an unchanged graph disagree.

A ranking that reshuffles when nothing changed is unusable, and worse,
unreproducible: every bug in it becomes unfalsifiable. CLAUDE.md rule 7.

---

## Cost

BUILD.md: *"measure it — expect ~10ms at 2k nodes; do **not** build cache
invalidation until profiling says to."*

Measured at **2000 nodes, 7995 edges** (median of 5 runs, Windows, SQLite on
local disk):

| Stage | Time |
|---|---|
| SQL — nodes + edges | 15 ms |
| Build the `DiGraph` | 4.5 ms |
| **One PageRank** | **6 ms** |
| Degrees | 0.6 ms |
| **`compute_signals` (all three directions)** | **85 ms** |

BUILD.md's ~10ms estimate is right *for one PageRank*. The whole call is three
of them plus the SQL.

An earlier version cost **118 ms**, because `reverse()` and `to_undirected()`
copy the graph by default — two full duplications of 2000 nodes and 8000 edges,
about 80 ms between them, more than everything else combined. Both are now
taken as views (`copy=False`, `as_view=True`), which read the same edges from a
different angle instead of duplicating them.

**No caching, deliberately.** 85 ms on demand is not what "profiling says to"
looks like, and an invalidation bug would cost more than the rebuild ever will
— especially with R2.15's clear and R2.16's session switching both able to
invalidate the whole graph underneath a cache.

---

## Co-citation and bibliographic coupling

Implementation: `backend/app/services/similarity.py`.

    bibliographic coupling(A, B)   papers that BOTH A and B cite.
                                   Fixed at publication; never changes.
    co-citation(A, B)              papers that cite BOTH A and B.
                                   Grows as the field cites them together.

One says *builds on the same work*, the other *is discussed in the same
breath*. They genuinely disagree — a paper can score high on one and zero on
the other — so they stay separate rather than collapsing into a single
"similarity".

### These run over the corpus, not the drawn graph

The single most important line in that module. BUILD.md:

> two modern papers whose only shared reference is a 2014 paper must have
> non-zero bib_coupling. This test protects the design; without it the year
> floor silently degrades your best feature.

A pre-2015 reference is stored with all its edges and deliberately given no
`graph_nodes` row — that is what a boundary paper *is*. Restricting these
queries to graph membership, which is correct for PageRank, returns zero for
exactly the papers coupling is best at finding, and looks entirely reasonable
doing it. R1.9 asserted the precondition; `test_similarity.py` now asserts the
feature.

---

## ⚠ Every structural signal is currently empty on the live corpus

Measured against `data/app.db`, and worth stating plainly because it is a fact
about the data rather than about the code:

| | |
|---|---|
| Papers | 229 — of which **226 are STUB**, 3 are METADATA |
| Edges | 226 |
| Edges *from* a seed (its references) | 99 |
| Edges *to* a seed (papers citing it) | 127 |
| Papers with more than one citer | **1** |
| PageRank | suppressed (2% completeness) |
| Bibliographic coupling | 0 papers |
| Co-citation | 0 papers |

The corpus is a **one-hop star**: three seeds at the centre, their references
below, their citers above, and almost no edges among the periphery. Only the
seeds have been crawled, so nothing on the rim has references of its own.

Coupling needs two papers citing the same third paper. Co-citation needs two
papers cited by the same third. On a star, neither exists — so both are
structurally zero, and PageRank is suppressed for the same underlying reason.

**This is not something to fix in code.** All three measures are tested and
correct; the graph simply has not been crawled deep enough to have the
structure they measure. What it needs is expansion from the current candidates
so the rim gains edges among itself.

Until then R3's ranking has almost nothing structural to rank on, and any
weight put on these features would be weight on zeroes.

---

## What is not here yet

- **Age-normalized citations, recency, venue tier.**
- **Rank-percentile normalization** — PLAN.md Appendix B.7 is explicit that
  this is *not* a z-score, because the citation distribution is heavy-tailed
  and a z-score lets one outlier dominate a whole feature.
