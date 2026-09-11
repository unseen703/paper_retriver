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
import {
  clampThreshold,
  navigableNodes,
  nodeRepulsion,
  partitionByQuery,
  positionsToSave,
  inTopicGroup,
  partitionByTopic,
} from "./graphInteraction";

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

describe("positionsToSave", () => {
  it("renames id to paper_id, the shape the endpoint takes", () => {
    expect(positionsToSave([{ id: 7, x: 1, y: 2 }])).toEqual([{ paper_id: 7, x: 1, y: 2 }]);
  });

  it("rounds to whole units", () => {
    // The fifteenth decimal changes on every layout, so without rounding every
    // save looks like a change to an arrangement nobody moved.
    expect(positionsToSave([{ id: 1, x: 10.4, y: -3.6 }])).toEqual([
      { paper_id: 1, x: 10, y: -4 },
    ]);
  });

  it("drops a NaN coordinate instead of sending it", () => {
    // JSON.stringify turns NaN into null, the server rejects null with a 422,
    // and that 422 would throw away every good position in the same request.
    const saved = positionsToSave([
      { id: 1, x: Number.NaN, y: 0 },
      { id: 2, x: 5, y: 5 },
    ]);
    expect(saved).toEqual([{ paper_id: 2, x: 5, y: 5 }]);
  });

  it("drops an infinite coordinate too", () => {
    expect(positionsToSave([{ id: 1, x: Number.POSITIVE_INFINITY, y: 0 }])).toEqual([]);
  });

  it("keeps a zero, because the origin is a real place", () => {
    expect(positionsToSave([{ id: 1, x: 0, y: 0 }])).toEqual([{ paper_id: 1, x: 0, y: 0 }]);
  });

  it("returns an empty list for an empty graph rather than throwing", () => {
    expect(positionsToSave([])).toEqual([]);
  });
});

describe("nodeRepulsion", () => {
  it("pushes a seed harder than a plain candidate", () => {
    expect(nodeRepulsion("SEED")).toBeGreaterThan(nodeRepulsion("CANDIDATE"));
  });

  it("gives a liked paper the same push as a seed", () => {
    // The reported gap: seeds already anchored their clusters apart, but a
    // liked paper got the candidate-cloud default and ended up buried in the
    // densest part of the graph -- exactly the paper you can least afford to
    // lose track of.
    expect(nodeRepulsion("LIKED")).toBe(nodeRepulsion("SEED"));
  });

  it("leaves disliked papers at the default push", () => {
    expect(nodeRepulsion("DISLIKED")).toBe(nodeRepulsion("CANDIDATE"));
  });

  it("falls back to the default for a missing state rather than throwing", () => {
    expect(nodeRepulsion(null)).toBe(nodeRepulsion("CANDIDATE"));
    expect(nodeRepulsion(undefined)).toBe(nodeRepulsion("CANDIDATE"));
  });
});

describe("inTopicGroup", () => {
  it("shows everything under 'all'", () => {
    expect(inTopicGroup("cs.LG", "all")).toBe(true);
    expect(inTopicGroup("physics.chem-ph", "all")).toBe(true);
    expect(inTopicGroup(null, "all")).toBe(true);
  });

  it("puts ordinary machine learning in cs", () => {
    expect(inTopicGroup("cs.LG", "cs", "Attention is all you need")).toBe(true);
    expect(inTopicGroup("stat.ML", "cs", "Variational inference")).toBe(true);
  });

  it("recognises chemistry by its category", () => {
    expect(inTopicGroup("physics.chem-ph", "chem", "Density functional theory")).toBe(true);
    expect(inTopicGroup("cond-mat.mtrl-sci", "chem", "Perovskite stability")).toBe(true);
  });

  it("files q-bio under biochemistry rather than chemistry", () => {
    // A deliberate move. `q-bio.*` used to land in "chemistry" because it was
    // the only non-CS bucket there was. Now that biochemistry has its own
    // group, quantitative biology belongs there -- and leaving it in chemistry
    // would mean the biochemistry tab missed the one category that is always
    // biology.
    expect(inTopicGroup("q-bio.QM", "biochem", "Protein assay")).toBe(true);
    expect(inTopicGroup("q-bio.QM", "chem", "Protein assay")).toBe(false);
  });

  it("recognises chemistry by vocabulary even under a CS category", () => {
    // The correction the real graph forced. A session of 104 retrosynthesis
    // papers held zero chemistry categories -- every one was cs.LG, which is
    // arXiv's correct filing for machine learning about chemistry. Grouping by
    // category alone put all 104 under "CS" and left "chemistry" empty.
    expect(inTopicGroup("cs.LG", "chem", "Retrosynthesis with graph networks")).toBe(true);
    expect(
      inTopicGroup("cs.AI", "chem", "Predicting Organic Reaction Outcomes"),
    ).toBe(true);
  });

  it("gives chemistry precedence when a paper is both", () => {
    // The more specific fact wins, matching the backend filter where the
    // reaction-ML rescue runs before the category rules. Filing a cs.LG
    // retrosynthesis paper under CS would empty the tab that exists to find it.
    const title = "Retrosynthesis prediction";
    expect(inTopicGroup("cs.LG", "chem", title)).toBe(true);
    expect(inTopicGroup("cs.LG", "cs", title)).toBe(false);
  });

  it("keeps ordinary ML out of chemistry", () => {
    expect(inTopicGroup("cs.LG", "chem", "Attention is all you need")).toBe(false);
    expect(inTopicGroup("cs.CL", "chem", "Neural machine translation")).toBe(false);
  });

  it("treats a node with neither signal as belonging to no group", () => {
    expect(inTopicGroup(null, "cs", "Some journal paper")).toBe(false);
    expect(inTopicGroup(null, "chem", "Some journal paper")).toBe(false);
    expect(inTopicGroup(null, "biochem", "Some journal paper")).toBe(false);
  });

  // ----------------------------------------------------------------------
  // Biochemistry -- the third group
  // ----------------------------------------------------------------------

  it("recognises biochemistry by vocabulary", () => {
    // **These are the papers that prompted the group.** Every biochemistry
    // paper in the real corpus has a NULL arXiv category -- they are journal
    // papers, not preprints -- so a category-based rule finds none of them and
    // they fall into no group at all, invisible under every specific filter.
    for (const title of [
      "Predicting Novel Metabolic Pathways through Subgraph Mining",
      "A general model for predicting enzyme functions based on enzymatic reactions",
      "NICEpath: Finding metabolic pathways in large networks",
      "Binding site prediction with geometric deep learning",
      "Molecular docking with learned scoring functions",
    ]) {
      expect(inTopicGroup(null, "biochem", title)).toBe(true);
    }
  });

  it("gives biochemistry precedence over chemistry when a paper is both", () => {
    // "enzymatic reaction" contains "reaction", so this matches both
    // vocabularies. Biochemistry is the more specific reading, which is also
    // the order the backend cascade checks them in.
    const title = "Curating enzymatic reaction rules for biosynthesis";
    expect(inTopicGroup(null, "biochem", title)).toBe(true);
    expect(inTopicGroup(null, "chem", title)).toBe(false);
  });

  it("gives biochemistry precedence over CS", () => {
    const title = "Enzyme function prediction with contrastive learning";
    expect(inTopicGroup("cs.LG", "biochem", title)).toBe(true);
    expect(inTopicGroup("cs.LG", "cs", title)).toBe(false);
  });

  it("keeps reaction chemistry out of biochemistry", () => {
    // The corridors are separate on the backend and must read as separate
    // here, or neither tab answers the question it exists for.
    expect(inTopicGroup("cs.LG", "chem", "Retrosynthesis with graph networks")).toBe(true);
    expect(inTopicGroup("cs.LG", "biochem", "Retrosynthesis with graph networks")).toBe(false);
  });

  it("keeps ordinary ML out of biochemistry", () => {
    expect(inTopicGroup("cs.LG", "biochem", "Attention is all you need")).toBe(false);
  });

  it("catches the protein work that makes up a real biochemistry graph", () => {
    /**
     * **Titles taken verbatim from the "protein-reaction relation" session.**
     *
     * The first version of this list carried only metabolic and enzyme
     * vocabulary and matched 1 of that session's 22 papers — the filter existed
     * and the papers were still invisible, which is the bug it was added to
     * fix. Real titles rather than invented ones, because invented ones are
     * what passed the first time.
     */
    for (const title of [
      "Evaluating Protein Transfer Learning with TAPE",
      "Generative Models for Graph-Based Protein Design",
      "Learning Protein Structure with a Differentiable Simulator",
      "Lightweight MSA Design Advances Protein Folding From Evolutionary Embeddings",
      "Dynamics-inspired Structure Hallucination for Protein-protein Interaction Modeling",
      "Rethinking Text-based Protein Understanding: Retrieval or LLM?",
      "NbBench: benchmarking language models for comprehensive nanobody tasks",
      "Universal Biological Sequence Reranking for Improved De Novo Peptide Sequencing",
      "Empowering Biomedical Discovery with AI Agents",
      "Dual modality feature fused neural network integrating binding site information for drug target affinity prediction",
    ]) {
      expect(inTopicGroup(null, "biochem", title)).toBe(true);
    }
  });

  it("still keeps the general-ML papers in that same session out", () => {
    // The other half. A biochemistry session holds method papers too, and a
    // filter that swept in Attention and BERT would group the whole graph.
    for (const title of [
      "Attention is All you Need",
      "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
      "Chronos-2: From Univariate to Universal Forecasting",
      "Symbol-Equivariant Recurrent Reasoning Models",
      "Self-Training With Noisy Student Improves ImageNet Classification",
    ]) {
      expect(inTopicGroup("cs.LG", "biochem", title)).toBe(false);
    }
  });

  it("splits a graph into matched and dimmed", () => {
    const nodes = [
      { id: 1, primary_arxiv_category: "cs.LG", title: "Attention is all you need" },
      { id: 2, primary_arxiv_category: "cs.LG", title: "Retrosynthesis with GNNs" },
      { id: 3, primary_arxiv_category: null, title: "Untitled" },
    ];
    expect(partitionByTopic(nodes, "chem")).toEqual({ matched: [2], rest: [1, 3] });
    expect(partitionByTopic(nodes, "cs")).toEqual({ matched: [1], rest: [2, 3] });
  });

  it("matches everything when the filter is off", () => {
    const nodes = [{ id: 1, primary_arxiv_category: "cs.LG", title: "x" }];
    expect(partitionByTopic(nodes, "all")).toEqual({ matched: [1], rest: [] });
  });
});
