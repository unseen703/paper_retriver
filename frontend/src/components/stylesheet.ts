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

export const stylesheet = [
  {
    selector: "node",
    style: {
      shape: "ellipse",
      width: "data(diameter)",
      height: "data(diameter)",
      "background-color": COLOR.CANDIDATE,
      "border-width": 0,
      // Transitions are what make the graph feel alive under the cursor rather
      // than snapping between states.
      "transition-property": "background-color, border-width, opacity",
      "transition-duration": "120ms",
    },
  },

  // Labels are opt-in per node -- see `withLabelFlags`.
  {
    selector: "node[label]",
    style: {
      label: "data(label)",
      "font-size": 9,
      color: "#cbd5e0",
      "text-valign": "bottom",
      "text-margin-y": 3,
      "text-wrap": "ellipsis",
      "text-max-width": 110,
      "text-outline-width": 2,
      "text-outline-color": "#14161a",
      "text-outline-opacity": 0.9,
    },
  },

  {
    selector: 'node[state="SEED"]',
    style: {
      "background-color": COLOR.SEED,
      // Carries the same information the hexagon did, in the border.
      "border-width": 3,
      "border-color": "#1a365d",
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
  // Grey at score 0, amber at the top -- so a strong candidate reads as
  // interesting without becoming a fifth colour to learn.
  {
    selector: 'node[state="CANDIDATE"]',
    style: { "background-color": "mapData(score, 0, 4, #a0aec0, #d69e2e)" },
  },

  {
    selector: "edge",
    style: {
      width: "mapData(influential, 0, 1, 0.8, 1.8)",
      "line-color": "#4a5568",
      "curve-style": "straight",
      "target-arrow-shape": "triangle",
      "target-arrow-color": "#4a5568",
      "arrow-scale": 0.55,
      opacity: "mapData(influential, 0, 1, 0.35, 0.75)",
      "transition-property": "line-color, opacity",
      "transition-duration": "120ms",
    },
  },

  // --- interaction states -------------------------------------------------
  // The hovered node and its neighbourhood stay lit; everything else drops
  // back. This is what makes a dense graph readable: you trace one paper's
  // connections by pointing at it rather than by squinting.
  {
    selector: "node.hl",
    style: { "border-width": 3, "border-color": "#f6ad55", "z-index": 20 },
  },
  {
    selector: "edge.hl",
    style: { "line-color": "#f6ad55", "target-arrow-color": "#f6ad55", opacity: 1, width: 2 },
  },
  { selector: ".fade", style: { opacity: 0.08 } },
  {
    selector: "node:selected",
    style: { "border-width": 4, "border-color": "#f6e05e", "z-index": 30 },
  },
  // Cytoscape's own grab cue, so a draggable node looks draggable.
  { selector: "node:active", style: { "overlay-opacity": 0.15, "overlay-color": "#f6ad55" } },
] as unknown as cytoscape.StylesheetJson;

/** The prototype's radius curve, verbatim. Doubled because Cytoscape sizes by diameter. */
export function diameterFor(citationCount: number | null | undefined): number {
  const radius = Math.max(5, Math.min(28, 4 + Math.sqrt(citationCount ?? 0) * 0.35));
  return radius * 2;
}

export function toElementData(node: GraphNodeOut) {
  return {
    id: String(node.id),
    state: node.state,
    // `?? 0` is safe: a seed has no score and is coloured by its own selector.
    score: node.score ?? 0,
    diameter: diameterFor(node.citation_count),
    citations: node.citation_count ?? 0,
    paperType: node.paper_type ?? "UNKNOWN",
    title: node.title,
  };
}

/**
 * Decide which nodes carry a visible label.
 *
 * Seeds always, plus the most-cited handful. Everything else reveals its title
 * on hover, which is the prototype's "relevant only" default and the reason
 * its canvas stayed readable at this density.
 */
export function withLabelFlags(nodes: GraphNodeOut[], topN = 8): Map<number, string | undefined> {
  const ranked = [...nodes]
    .filter((n) => n.state !== "SEED")
    // Stable: citations first, then id, so the labelled set does not flicker
    // between renders when two papers tie.
    .sort((a, b) => (b.citation_count ?? 0) - (a.citation_count ?? 0) || a.id - b.id)
    .slice(0, topN)
    .map((n) => n.id);
  const labelled = new Set<number>([
    ...ranked,
    ...nodes.filter((n) => n.state === "SEED").map((n) => n.id),
  ]);
  return new Map(nodes.map((n) => [n.id, labelled.has(n.id) ? shorten(n.title) : undefined]));
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
