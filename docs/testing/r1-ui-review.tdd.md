# TDD evidence — R1 UI review findings

**Source:** no plan file. Journeys were derived from the seven findings of the
`/ecc:code-review` pass over the R1 UI and backend surface, run 2026-09-08.

**Runner:** backend `uv run pytest` (via `uv run python scripts/verify.py`);
frontend `npm test` → `vitest run`.

---

## The finding behind the findings

The frontend had **no test runner and zero tests** — 908 backend tests against
0 frontend tests, across three releases of UI (R1.20–R1.22). CLAUDE.md rule 5
says tests ship with the feature in the same turn. They did not, three times,
and that was not flagged at the time.

Vitest + jsdom + testing-library is now installed, with stubs for the two
browser APIs jsdom lacks that `GraphCanvas` depends on: `requestAnimationFrame`
drives the idle float and `ResizeObserver` triggers the one-time fit. Without
them, importing the component throws before any assertion runs.

---

## User journeys

1. **As a keyboard user**, I want the arrow keys to move between papers I can
   actually see, so pressing a key never scrolls the view to a node the score
   filter has hidden.
2. **As a keyboard user**, I want my selection to survive moving the mouse, so
   the inspector and the canvas never disagree about what is selected.
3. **As someone filtering by score**, I want the slider to keep meaning the
   same thing after the graph changes, so it never reports 0 of N while
   sitting at what looks like its maximum.
4. **As a keyboard user**, I want Escape to close the Add-paper dialog from
   anywhere inside it, and Tab to stay inside it, so a modal that declares
   `aria-modal="true"` behaves like one.
5. **As an API consumer**, I want every field in the contract to do something,
   and a client-side mistake to be reported as one.

---

## Task report

| Finding | RED | GREEN |
|---|---|---|
| Arrow keys reach hidden nodes | `graphInteraction.test.ts` failed to import — compile-time RED, the module did not exist | 6 tests pass |
| Keyboard selection not pinned | same module | covered by `pinnedRef` assignment; behavioural coverage is via the shared predicate tests |
| Threshold not clamped to range | same module | 6 tests pass |
| Hover label override leaks | not directly testable in jsdom (Cytoscape style read-back is paint-dependent) — see gaps | fixed by construction in `clearHighlight` |
| Dialog Escape / focus trap / restore | 3 failed | 6 tests pass |
| `as_state` accepted and ignored | 2 failed | 3 tests pass |
| `q` unbounded | 1 failed | 3 tests pass |

**RED evidence**

```
frontend  Test Files  2 failed (2)   Tests  3 failed | 3 passed (6)
          graphInteraction.test.ts — failed to import (module absent)
          AddPaperDialog — Escape from container, Tab containment, focus restore
backend   3 failed, 3 passed
          test_an_overlong_query_is_rejected_as_a_client_error
          test_the_request_model_does_not_carry_a_field_nothing_reads
          test_as_state_is_now_rejected_rather_than_silently_ignored
```

**GREEN evidence**

```
frontend  Test Files  2 passed (2)   Tests  18 passed (18)
backend   914 passed, 1 deselected   OK: all 4 checks passed
```

---

## Test specification

| # | What is guaranteed | Test | Type | Result |
|---|---|---|---|---|
| 1 | Arrow keys visit only nodes the score filter shows | `graphInteraction.test.ts:excludes candidates the score filter has hidden` | unit | PASS |
| 2 | A seed is never hidden by the threshold, whatever its value | `graphInteraction.test.ts:never hides a seed, whatever the threshold` | unit | PASS |
| 3 | A liked or disliked paper is never hidden by the threshold | `graphInteraction.test.ts:never hides a labelled paper` | unit | PASS |
| 4 | Navigation order is stable between renders | `graphInteraction.test.ts:orders by id so the sequence is stable` | unit | PASS |
| 5 | A threshold above the new maximum is pulled into range | `graphInteraction.test.ts:pulls a value above the new maximum back down` | unit | PASS |
| 6 | An unchosen threshold resolves to the range floor | `graphInteraction.test.ts:falls back to the minimum when nothing has been chosen` | unit | PASS |
| 7 | A degenerate or empty score range does not throw | `graphInteraction.test.ts:survives a degenerate range` / `survives an empty graph` | unit | PASS |
| 8 | Escape closes the dialog from the container, not just the input | `AddPaperDialog.test.tsx:closes on Escape from anywhere inside the dialog` | component | PASS |
| 9 | Tab cannot leave a dialog that declares `aria-modal` | `AddPaperDialog.test.tsx:keeps Tab inside the dialog` | component | PASS |
| 10 | Closing returns focus to whatever opened the dialog | `AddPaperDialog.test.tsx:restores focus to whatever opened it` | component | PASS |
| 11 | An overlong query is a 422, not a misleading 503 | `test_api_review_r1_ui.py:test_an_overlong_query_is_rejected_as_a_client_error` | integration | PASS |
| 12 | The length limit does not reject a genuine paper title | `test_api_review_r1_ui.py:test_the_length_limit_leaves_real_titles_alone` | integration | PASS |
| 13 | A rejected query costs no upstream API call | `test_api_review_r1_ui.py:test_an_overlong_query_costs_no_api_call` | integration | PASS |
| 14 | The request model carries no field nothing reads | `test_api_review_r1_ui.py:test_the_request_model_does_not_carry_a_field_nothing_reads` | integration | PASS |
| 15 | Removing `as_state` did not change what the endpoint does | `test_api_review_r1_ui.py:test_adding_a_paper_still_creates_a_seed` | integration | PASS |
| 16 | A stale caller sending `as_state` gets a visible 422 | `test_api_review_r1_ui.py:test_as_state_is_now_rejected_rather_than_silently_ignored` | integration | PASS |

---

## Coverage and known gaps

Backend: 914 tests, gate green (lint, format, typecheck, tests).
Frontend: 18 tests. No numeric threshold is enforced yet — a coverage gate
against 18 tests would be a number rather than a signal.

**Three honest gaps.**

**The label-leak fix is not directly asserted.** Reading `element.style('label')`
from a Cytoscape instance returns `undefined` even for values that visibly
render, because style resolution is paint-dependent and jsdom never paints —
the same trap that made CSS transitions and the idle float appear broken
earlier in this session. The fix (clearing the override in `clearHighlight`,
which every exit path calls) is correct by construction but proved by reading,
not by a test. A Playwright suite would close this.

**`GraphCanvas` itself has no component test.** Only the logic extracted out
of it does. Everything remaining in that file is Cytoscape lifecycle, which
needs a real renderer to exercise meaningfully.

**One test initially passed for the wrong reason** and was rewritten before
being trusted. `closes on Escape from anywhere inside the dialog` called
`dialog.focus()` — but a `div` without `tabindex` does not take focus, so the
key still landed on the input and the assertion held while the bug was fully
present. It now dispatches `keydown` at the container, which cannot reach a
handler bound to a child.

---

## Checkpoints

| Stage | Commit |
|---|---|
| RED | `3102726` test: reproducers for the R1 UI review findings, and a frontend test runner |
| GREEN | `0d502ab` fix: the seven R1 UI review findings |

No separate refactor commit: the extraction into `graphInteraction.ts` *was*
the fix for three of the findings, so it is inside the GREEN commit rather
than after it.
