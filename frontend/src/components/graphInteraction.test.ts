/**
 * Review findings from the R1 UI pass, as executable guarantees.
 *
 * Journeys these come from:
 *
 *   As a keyboard user, I want the arrow keys to move between papers I can
 *   actually see, so that pressing a key never scrolls the view to a node the
 *   score filter has hidden.
 *
 *   As someone filtering by score, I want the slider to keep meaning the same
 *   thing after the graph changes, so that it never reports 0 of N while
 *   sitting at what looks like its maximum.
 *
 * Both were reported as findings against `GraphCanvas.step` and `App`'s
 * threshold state, where the logic was tangled into effects and could not be
 * tested. The fix extracts it, which is why these are pure-function tests: the
 * bug was as much "this decision has nowhere to live" as it was a wrong
 * comparison.
 */
import { describe, expect, it } from "vitest";
import type { GraphNodeOut } from "../api/client";
import { clampThreshold, navigableNodes, partitionByQuery } from "./graphInteraction";

function node(id: number, over: Partial<GraphNodeOut> = {}): GraphNodeOut {
  return {
    id,
    title: `Paper ${id}`,
    state: "CANDIDATE",
    score: 2.0,
    year: 2020,
    citation_count: 10,
    paper_type: "RESEARCH",
    in_degree: 0,
    out_degree: 0,
    pos: null,
    ...over,
  } as GraphNodeOut;
}

// --------------------------------------------------------------------------
// navigableNodes -- arrow keys must not land on hidden nodes
// --------------------------------------------------------------------------

describe("navigableNodes", () => {
  it("returns every node when the threshold is at the floor", () => {
    const nodes = [node(3), node(1), node(2)];
    expect(navigableNodes(nodes, 0).map((n) => n.id)).toEqual([1, 2, 3]);
  });

  it("excludes candidates the score filter has hidden", () => {
    // The reported bug: with 44 of 98 shown, roughly half of all arrow presses
    // selected a node that was `display: none`, panning the viewport to
    // nothing.
    const nodes = [node(1, { score: 2.4 }), node(2, { score: 2.1 }), node(3, { score: 2.45 })];
    expect(navigableNodes(nodes, 2.3).map((n) => n.id)).toEqual([1, 3]);
  });

  it("never hides a seed, whatever the threshold", () => {
    // A seed has no score. Hiding it would look like the tool losing a paper
    // the user added by hand.
    const nodes = [node(1, { state: "SEED", score: null }), node(2, { score: 2.0 })];
    expect(navigableNodes(nodes, 4).map((n) => n.id)).toEqual([1]);
  });

  it("never hides a labelled paper", () => {
    const nodes = [
      node(1, { state: "LIKED", score: 0.1 }),
      node(2, { state: "DISLIKED", score: 0.1 }),
      node(3, { score: 0.1 }),
    ];
    expect(navigableNodes(nodes, 2).map((n) => n.id)).toEqual([1, 2]);
  });

  it("orders by id so the sequence is stable between renders", () => {
    const nodes = [node(30), node(4), node(100)];
    expect(navigableNodes(nodes, 0).map((n) => n.id)).toEqual([4, 30, 100]);
  });

  it("returns an empty list rather than throwing on an empty graph", () => {
    expect(navigableNodes([], 0)).toEqual([]);
  });
});

// --------------------------------------------------------------------------
// clampThreshold -- the slider must stay inside the range it is drawn against
// --------------------------------------------------------------------------

describe("clampThreshold", () => {
  it("keeps a value already inside the range", () => {
    expect(clampThreshold(2.2, { min: 2.0, max: 2.5 })).toBe(2.2);
  });

  it("pulls a value above the new maximum back down", () => {
    // The reported bug: a threshold left over from a graph scoring 2.01-2.49,
    // applied to one scoring 0-1, hid every node while the slider thumb sat at
    // its maximum -- so the control looked correct and the graph looked empty.
    expect(clampThreshold(2.4, { min: 0, max: 1 })).toBe(1);
  });

  it("pushes a value below the new minimum back up", () => {
    expect(clampThreshold(0.2, { min: 2.0, max: 2.5 })).toBe(2.0);
  });

  it("falls back to the minimum when nothing has been chosen yet", () => {
    // A slider that starts mid-range would hide papers before it was touched.
    expect(clampThreshold(null, { min: 2.0, max: 2.5 })).toBe(2.0);
  });

  it("survives a degenerate range where every paper scores the same", () => {
    expect(clampThreshold(5, { min: 2.0, max: 2.0 })).toBe(2.0);
  });

  it("survives an empty graph, where there is no range at all", () => {
    expect(clampThreshold(null, { min: 0, max: 0 })).toBe(0);
  });
});

describe("partitionByQuery", () => {
  const nodes = [
    { id: 1, title: "Attention Is All You Need" },
    { id: 2, title: "BERT: Pre-training of Deep Bidirectional Transformers" },
    { id: 3, title: "Deep Residual Learning" },
    { id: 4, title: null },
  ];

  it("matches case-insensitively on a substring", () => {
    expect(partitionByQuery(nodes, "deep").matched).toEqual([2, 3]);
  });

  it("returns the non-matches too, because dimming them is what finds the match", () => {
    // BUILD.md R2.11: "dims non-matches". Ringing two nodes green in a field
    // of two hundred equally bright ones is a puzzle, not a search result.
    expect(partitionByQuery(nodes, "deep").rest).toEqual([1, 4]);
  });

  it("matches nothing when the query is empty, so the box being off marks nothing", () => {
    const { matched, rest } = partitionByQuery(nodes, "");
    expect(matched).toEqual([]);
    expect(rest).toEqual([1, 2, 3, 4]);
  });

  it("treats whitespace as empty", () => {
    expect(partitionByQuery(nodes, "   ").matched).toEqual([]);
  });

  it("survives a node with no title rather than throwing", () => {
    expect(partitionByQuery(nodes, "null").matched).toEqual([]);
  });

  it("never mutates the graph -- it only reports ids", () => {
    const before = JSON.stringify(nodes);
    partitionByQuery(nodes, "deep");
    expect(JSON.stringify(nodes)).toBe(before);
  });
});
