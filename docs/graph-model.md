# Graph model

> **These definitions are copied verbatim from `docs/PLAN.md`**, on BUILD.md's
> instruction at R1.23. They are reproduced rather than summarised because
> every one of them has already been the subject of a bug:
>
> * confusing *references* with *citations* inverts an entire expansion;
> * confusing *discovered* with *candidate* is what made rejected papers
>   reappear in the ranking pool at R1.13;
> * confusing *removal* with *deletion* is what tombstones exist to prevent.
>
> If this file and PLAN.md ever disagree, PLAN.md wins and this file is stale.

## Definitions (write these into `docs/graph-model.md` verbatim)

| Term | Definition |
|---|---|
| **Node** | A canonical paper (`papers` row with `canonical_paper_id IS NULL`). |
| **Edge** | A directed `CITES` relation, `citing → cited`. One row per pair. |
| **References of P** | `{c : edge(P, c)}` — outgoing. Bounded, curated, older-biased. |
| **Citations of P** | `{c : edge(c, P)}` — incoming. Unbounded, uncurated, newer-biased. |
| **Same relation?** | Yes. Same edge set, opposite traversal. Direction is stored once; *traversal* direction is expansion metadata. |
| **Discovered** | Exists in `papers` (possibly as a STUB). |
| **Candidate** | Passed filters, scored, in `graph_nodes` with `state = CANDIDATE`. |
| **Rejected** | Has a `filter_decisions` row with `outcome = REJECT`. Stays in `papers`, absent from `graph_nodes`. |
| **Quarantined** | `outcome = QUARANTINE`. Absent from graph, listed in the review drawer. |
| **Removal** | Delete the `graph_nodes` row, append a `REMOVED` event. Paper and edges persist in the corpus. |
| **Orphaned** | Unlabeled, and no path to `anchors = SEED ∪ LIKED` in the undirected projection within `max_depth`. |
| **Anchor** | A SEED or LIKED node. Anchors justify the existence of candidates and seed PPR. |

## Two orthogonal state axes

Collapsing these into one enum is why your proposed state list has overlaps (`UNLABELED` vs `POTENTIAL_CANDIDATE`) and category errors (`FILTERED` is not a user label).

```
CORPUS STATE  (system-owned, per paper, global)
  STUB ──fetch──▶ METADATA ──filter──┬──▶ ELIGIBLE
                                     ├──▶ QUARANTINED ──(user review)──▶ ELIGIBLE | REJECTED
                                     └──▶ REJECTED    ──(user restore)──▶ ELIGIBLE

GRAPH STATE   (user-owned, per (session, paper); only ELIGIBLE papers may enter)
                     ┌──────────────────────────────┐
   user adds ───────▶│            SEED              │──remove──▶ (out, tombstoned)
   by title          └──────────────────────────────┘
                     ▲ no like/dislike allowed → 409

   expansion ──────▶ CANDIDATE ──like────▶ LIKED ──unlike──▶ CANDIDATE
                        │  ▲               │   │
                        │  └──un-dislike───┘   └──dislike──▶ DISLIKED
                        │                                        │
                     dislike                                  remove
                        │                                        │
                        ▼                                        ▼
                    DISLIKED ──remove──▶ (out, tombstoned) ◀─────┘

   (out, tombstoned) ──user Restore only──▶ CANDIDATE
                      never re-added automatically
```

## Rules

1. **SEED accepts only `remove`.** `POST /nodes/{id}/like` on a seed → `409 SEED_NOT_LABELABLE`. Your rule, encoded.
2. **DISLIKED nodes stay in the graph.** They carry signal (negative PPR anchor, repulsion in layout) and hiding them means you'd refetch them. Render them dimmed, not deleted.
3. **REMOVED is a tombstone.** Expansion excludes any paper whose latest event is `REMOVED`. Re-discovery surfaces it in a "Previously removed (N)" list with a Restore button. **The system never auto-resurrects a removed paper** — that's the answer to your §14 question, and it's the difference between a tool that respects the user and one that argues with them.
4. **DISLIKED → LIKED is legal and requires no ceremony.** Un-disliking is the answer to "what if a disliked paper becomes relevant again" for papers still in the graph; Restore covers the removed case.
5. **Every state change triggers a defined recomputation:**

   | Transition | Side effects |
   |---|---|
   | `SEED_ADDED` | fetch metadata, enqueue expansion, node becomes anchor, rescore all |
   | `→ LIKED` | node becomes anchor, add to PPR restart set, add to frontier, rescore all |
   | `→ DISLIKED` | add to negative set, remove from frontier, rescore all |
   | `→ CANDIDATE` (from LIKED) | drop from anchors, **run GC** (its dependents may now be orphans) |
   | `REMOVED` | write tombstone, **run GC**, then recompute layout with survivors pinned |

6. **GC never sweeps a labeled node**, and every sweep writes `GC_SWEPT` events with `actor = SYSTEM` so the cascade is auditable and undoable.
7. **GC previews before it commits.** `DELETE /nodes/{id}?dry_run=true` returns `{would_remove: [...], count: 34}`. The UI shows "This will also remove 34 orphaned candidates. [Cancel] [Remove]". Deleting a third of someone's graph without warning is unacceptable, and the dry-run endpoint is also how you test the GC.
