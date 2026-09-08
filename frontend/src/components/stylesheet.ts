/**
 * Cytoscape stylesheet, tuned to match the archived D3 prototype's look.
 *
 * The prototype drew small round balls whose radius grew as the square root of
 * citations, coloured by label, with labels shown only where they earned their
 * space. That reads better than BUILD.md Appendix B.6's original spec at this
 * graph's density, so three things changed from B.6 and the reasons are worth
 * recording:
 *
 * **Every node is a circle.** B.6 gave SEED a hexagon and SURVEY a diamond so
 * that "meaning never rests on colour alone" -- a real accessibility property.
 * Rounds lose it, so the compensation is a heavy dark ring on seeds and a
 * dashed ring on disliked nodes: the distinction still survives greyscale and
 * colourblindness, it just lives in the border rather than the silhouette.
 *
 * **Radius is sqrt(citations), not log.** `max(5, min(28, 4 + sqrt(c) * 0.35))`
 * is the prototype's formula verbatim. On this corpus it spreads 37 nodes over
 * 17 distinct radii (6.4px to the 28px cap), where log compressed the middle
 * and made everything look the same size. sqrt is also the perceptually honest
 * one: it makes *area* proportional to citations, and area is what the eye
 * actually compares.
 *
 * **Labels are conditional.** The prototype's default was "relevant only".
 * Drawing 37 titles at once is unreadable, and text measurement is the
 * expensive part of a Cytoscape frame.
 */
import type cytoscape from "cytoscape";

import type { GraphNodeOut } from "../api/client";

// The prototype's palette, kept so the two look like the same tool.
const COLOR = {
  SEED: "#4299e1",
  LIKED: "#48bb78",
  DISLIKED: "#fc8181",
  CANDIDATE: "#a0aec0",
} as const;

// The prototype's selection/neighbour colour. Yellow, not orange -- it is the
// one hue not already carrying meaning in the node palette.
const HIGHLIGHT = "#f6e05e";
// `.link { stroke: #4a5568 }`. Edges stay this colour in every state.
const EDGE = "#4a5568";

export const stylesheet = [
  {
    selector: "node",
    style: {
      shape: "ellipse",
      width: "data(diameter)",
      height: "data(diameter)",
      "background-color": COLOR.CANDIDATE,
      // Every ball carries a rim in the panel colour, exactly as the
      // prototype did (`.node circle { stroke: #1a1f2e; stroke-width: 1.5 }`).
      // It is what stops adjacent circles merging into one blob at the sizes
      // this graph draws them, and it is why the prototype's dense clusters
      // stayed countable.
      "border-width": 1.5,
      "border-color": "#1a1f2e",
      // Transitions are what make the graph feel alive under the cursor rather
      // than snapping between states.
      "transition-property": "background-color, border-width, opacity",
      "transition-duration": "120ms",
    },
  },

  // Text styling lives on the BASE node selector, not on `node[label]`.
  //
  // That distinction caused a real bug. `node[label]` is a DATA selector -- it
  // matches nodes whose data carries a `label` field -- while the hover
  // handler reveals a title by setting the STYLE property of the same name.
  // A node outside the labelled few therefore got a label with none of the
  // styling below, and fell through to Cytoscape's defaults: black text at
  // 16px with no outline, which on a #0f1117 canvas is invisible.
  //
  // On the base selector these properties are inert until something actually
  // supplies a label, so nothing is lost by hoisting them.
  {
    selector: "node",
    style: {
      "font-size": 9,
      // `.node text { fill: #cbd5e0 }`, measured off the running prototype.
      color: "#cbd5e0",
      "text-valign": "bottom",
      "text-margin-y": 3,
      "text-wrap": "ellipsis",
      "text-max-width": 110,
      // 1, not 2. At a 9px font a 2px outline is ~22% of the em and the stroke
      // is centred on the glyph path, so half of it eats inward and fills in
      // the thin strokes and counters. It exists to keep a label legible where
      // it crosses an edge, which one pixel does.
      "text-outline-width": 1,
      "text-outline-color": "#0f1117",
      "text-outline-opacity": 0.9,
    },
  },
  // Which nodes carry a label by default -- see `withLabelFlags`. Hover adds
  // one to any node, and it now inherits the styling above.
  { selector: "node[label]", style: { label: "data(label)" } },

  {
    selector: 'node[state="SEED"]',
    style: {
      "background-color": COLOR.SEED,
      // Carries the same information B.6's hexagon did, in the border.
      "border-width": 3,
      "border-color": "#2b6cb0",
    },
  },
  { selector: 'node[state="LIKED"]', style: { "background-color": COLOR.LIKED } },
  {
    selector: 'node[state="DISLIKED"]',
    style: {
      "background-color": COLOR.DISLIKED,
      opacity: 0.45,
      "border-width": 1,
      "border-style": "dashed",
      "border-color": "#742a2a",
    },
  },
  // CANDIDATE is shaded by publication year, oldest dim to newest bright.
  //
  // Two findings drove this. Connected Papers -- the closest tool to this one
  // -- encodes publication year in node colour and citation count in node
  // size, because a citation graph is inherently temporal and a flat mass of
  // identical nodes hides its most obvious structure. And the earlier amber
  // ramp failed not because encoding something in colour was wrong, but
  // because it introduced a second saturated HUE that fought the edges.
  //
  // So this uses LIGHTNESS, not hue. Hue is fully spoken for -- blue seeds,
  // green liked, red disliked, yellow highlight -- and lightness was the one
  // free channel left. It is also colourblind-safe by construction: a
  // single-hue lightness ramp survives every form of colour vision
  // deficiency, which is more than the red/green pair this palette already
  // relies on elsewhere can claim.
  //
  // `yearT` is normalised 0..1 across the graph's own range, so a corpus
  // spanning 2015-2022 uses the full ramp rather than a sliver of a fixed
  // scale. The midpoint lands near #a0aec0, the prototype's flat candidate
  // grey, so a typical graph still reads as it did.
  {
    selector: 'node[state="CANDIDATE"]',
    style: { "background-color": "mapData(yearT, 0, 1, #6b7a90, #dfe6ef)" },
  },

  {
    selector: "edge",
    style: {
      // `.link { stroke: #4a5568; stroke-opacity: 0.5; stroke-width: 1 }`,
      // measured off the running prototype. Flat -- no width or opacity ramp
      // on influence. Edges are the substrate you read the nodes against, and
      // anything that makes them compete for attention makes the graph
      // harder to read, which is what the amber-node/yellow-edge combination
      // was doing.
      width: 1,
      "line-color": EDGE,
      "curve-style": "straight",
      "target-arrow-shape": "triangle",
      "target-arrow-color": EDGE,
      "arrow-scale": 0.55,
      opacity: 0.5,
      "transition-property": "opacity",
      "transition-duration": "120ms",
    },
  },

  // --- interaction states -------------------------------------------------
  // The hovered node and its neighbourhood stay lit; everything else drops
  // back. This is what makes a dense graph readable: you trace one paper's
  // connections by pointing at it rather than by squinting.
  // `.node.neighbor circle { stroke: #f6e05e; stroke-width: 2.5 }` -- a RING,
  // with the fill untouched. So a lit node still reports its state, and the
  // highlight adds information instead of replacing it.
  {
    selector: "node.hl",
    style: { "border-width": 2.5, "border-color": HIGHLIGHT, "z-index": 20 },
  },
  // Edges in the neighbourhood are NOT recoloured. The prototype has no
  // `.link.highlight` rule at all: lit edges simply keep their normal grey
  // while everything else dims, so the neighbourhood emerges by subtraction.
  // Turning them yellow put a second saturated colour next to the node rings
  // and made the two fight.
  { selector: "edge.hl", style: { opacity: 0.75 } },
  // Two different dim levels, both from the prototype: `.link.dimmed` is 0.06
  // and `.node.dimmed circle` is 0.10. Nodes need the extra because a circle
  // at 0.06 against #0f1117 is invisible, and a graph that appears to lose
  // half its nodes on hover is alarming rather than helpful.
  { selector: "node.fade", style: { opacity: 0.1 } },
  { selector: "edge.fade", style: { opacity: 0.06 } },
  {
    selector: "node:selected",
    style: { "border-width": 3, "border-color": HIGHLIGHT, "z-index": 30 },
  },
  // White on hover, as the prototype did (`.node circle:hover`), so the node
  // under the pointer is distinguishable from the neighbours it lit up.
  {
    selector: "node.hover",
    style: { "border-color": "#ffffff", "border-width": 2.5, "z-index": 40 },
  },
  // Cytoscape's own grab cue, so a draggable node looks draggable.
  { selector: "node:active", style: { "overlay-opacity": 0.12, "overlay-color": HIGHLIGHT } },

  // A title-search match. Green rather than the yellow highlight, because a
  // search result and a neighbourhood are different questions and the user can
  // be asking both at once -- a match inside the selected node's neighbourhood
  // has to be legible as both.
  {
    selector: "node.match",
    style: { "border-width": 3, "border-color": "#48bb78", "z-index": 25 },
  },

  // Dimmed because a search is active and this node does not match (R2.11).
  // Lighter than `fade` (0.1): a search narrows attention within a graph you
  // are still reading, where a neighbourhood highlight answers "what is
  // connected to this" and can afford to push everything else right back.
  { selector: "node.searchFade", style: { opacity: 0.25 } },
  { selector: "edge.searchFade", style: { opacity: 0.08 } },

  // Filtered out by the score threshold. `display: none` rather than opacity:
  // a hidden node must not catch clicks, and the layout should not reserve
  // space for it. Cytoscape hides incident edges automatically.
  { selector: ".hidden", style: { display: "none" } },
] as unknown as cytoscape.StylesheetJson;

/** The prototype's radius curve, verbatim. Doubled because Cytoscape sizes by diameter. */
export function diameterFor(citationCount: number | null | undefined): number {
  const radius = Math.max(5, Math.min(28, 4 + Math.sqrt(citationCount ?? 0) * 0.35));
  return radius * 2;
}

export interface YearRange {
  min: number;
  max: number;
}

/**
 * The graph's own year span, for normalising the candidate shade.
 *
 * Normalising against the graph rather than a fixed scale means a corpus
 * covering 2015-2022 uses the whole ramp instead of a sliver of it. Papers
 * with no year are excluded here and fall back to the midpoint below, which
 * is the prototype's flat grey -- an unknown year should look ordinary, not
 * like the oldest paper in the graph.
 */
export function yearRange(nodes: GraphNodeOut[]): YearRange {
  const years = nodes.map((n) => n.year).filter((y): y is number => typeof y === "number");
  if (years.length === 0) return { min: 0, max: 0 };
  return { min: Math.min(...years), max: Math.max(...years) };
}

export function toElementData(node: GraphNodeOut, years: YearRange) {
  const span = years.max - years.min;
  return {
    id: String(node.id),
    state: node.state,
    // `?? 0` is safe: a seed has no score and is coloured by its own selector.
    score: node.score ?? 0,
    diameter: diameterFor(node.citation_count),
    citations: node.citation_count ?? 0,
    year: node.year,
    // 0.5 for a missing year or a single-year graph: the midpoint of the ramp,
    // which is the ordinary grey. Reporting 0 would draw an unknown year as
    // the oldest paper present, which is a claim the data does not make.
    yearT:
      typeof node.year === "number" && span > 0 ? (node.year - years.min) / span : 0.5,
    paperType: node.paper_type ?? "UNKNOWN",
    title: node.title,
  };
}

/** The prototype's three label modes, verbatim: hidden | all | relevant. */
export type LabelMode = "hidden" | "all" | "relevant";

// `LABEL_SCORE_THRESHOLD` in the prototype. A candidate scoring above this is
// interesting enough to name without being asked.
const RELEVANT_SCORE = 0.35;

/**
 * Decide which nodes carry a visible label.
 *
 * The prototype's `_labelVisible`, ported: "always show labeled
 * (seed/liked/disliked/skipped) nodes, plus unlabeled candidates above the
 * relevance-score threshold."
 *
 * "relevant" is the default because thirty-seven titles at once is unreadable
 * and text measurement is the expensive part of a Cytoscape frame -- but
 * "all" exists for when you are actually reading the graph rather than
 * navigating it, and "hidden" for when you are looking at its shape.
 */
export function withLabelFlags(
  nodes: GraphNodeOut[],
  mode: LabelMode = "relevant",
): Map<number, string | undefined> {
  return new Map(
    nodes.map((n) => {
      const show =
        mode === "all" ||
        (mode !== "hidden" && (n.state !== "CANDIDATE" || (n.score ?? 0) >= RELEVANT_SCORE));
      return [n.id, show ? shorten(n.title) : undefined];
    }),
  );
}

/**
 * Truncate at a word boundary before Cytoscape sees the string.
 *
 * `text-max-width` ellipsises the rendered text, but the full string is still
 * measured every frame. Cutting first keeps the label legible and the layout
 * cheap.
 */
export function shorten(title: string, max = 40): string {
  if (title.length <= max) return title;
  const cut = title.slice(0, max);
  const lastSpace = cut.lastIndexOf(" ");
  return `${(lastSpace > max * 0.6 ? cut.slice(0, lastSpace) : cut).trimEnd()}…`;
}
