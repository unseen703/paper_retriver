/**
 * The Cytoscape stylesheet, BUILD.md Appendix B.6.
 *
 * Two encoding rules are load-bearing rather than decorative.
 *
 * **Shape carries `paper_type`, so meaning never rests on colour alone.** A
 * survey is a diamond whatever colour its state makes it, which keeps the
 * graph readable for a red/green-colourblind reader and in a greyscale
 * screenshot.
 *
 * **Size is log citations, not raw.** Raw counts span five orders of magnitude
 * in this corpus -- a 191k-citation hub next to a 12-citation preprint -- so a
 * linear map makes every node either a dot or the whole viewport.
 */
import type cytoscape from "cytoscape";

import type { GraphNodeOut } from "../api/client";

// `as cytoscape.StylesheetJson` rather than a direct annotation: @types/cytoscape
// models numeric style properties as `number`, but Cytoscape accepts a
// `mapData(...)` expression string for any of them at runtime -- that is the
// entire mechanism behind size-by-citations and colour-by-score below. The
// types are narrower than the library, so the cast is describing reality
// rather than hiding a mistake. Every value here is from BUILD.md B.6 verbatim.
export const stylesheet = [
  {
    selector: "node",
    style: {
      label: "data(shortTitle)",
      "font-size": 8,
      color: "#e6e8ec",
      "text-valign": "bottom",
      "text-wrap": "ellipsis",
      "text-max-width": 90,
      width: "mapData(logCites, 0, 12, 14, 48)",
      height: "mapData(logCites, 0, 12, 14, 48)",
    },
  },

  {
    selector: 'node[state="SEED"]',
    style: {
      "background-color": "#1d4ed8",
      shape: "hexagon",
      "border-width": 3,
      "border-color": "#1e293b",
    },
  },
  { selector: 'node[state="LIKED"]', style: { "background-color": "#16a34a", "border-width": 2 } },
  {
    selector: 'node[state="DISLIKED"]',
    style: {
      "background-color": "#9ca3af",
      opacity: 0.4,
      "border-style": "dashed",
      "border-width": 1,
    },
  },
  {
    selector: 'node[state="CANDIDATE"]',
    style: { "background-color": "mapData(score, 0, 5, #e5e7eb, #f59e0b)" },
  },
  // Shape carries paper_type so meaning never rests on colour alone.
  { selector: 'node[paperType="SURVEY"]', style: { shape: "diamond" } },

  {
    selector: "edge",
    style: {
      width: 1,
      "line-color": "#cbd5e1",
      "curve-style": "bezier",
      "target-arrow-shape": "triangle",
      "target-arrow-color": "#cbd5e1",
      "arrow-scale": 0.6,
      opacity: "mapData(influential, 0, 1, 0.25, 0.9)",
    },
  },

  { selector: ".hl", style: { "border-width": 4, "border-color": "#f43f5e", "z-index": 10 } },
  { selector: ".fade", style: { opacity: 0.12 } },
] as unknown as cytoscape.StylesheetJson;

/** Cytoscape needs the display fields precomputed; `mapData` cannot call log(). */
export function toElementData(node: GraphNodeOut) {
  return {
    id: String(node.id),
    state: node.state,
    // `?? 0` rather than a filter: a node with no score is a seed, and seeds
    // are coloured by their own selector, so the value is never read.
    score: node.score ?? 0,
    // log1p, not log: a paper with zero citations is common (a 2026 preprint)
    // and log(0) is -Infinity, which Cytoscape renders as a zero-size node.
    logCites: Math.log1p(node.citation_count ?? 0),
    paperType: node.paper_type ?? "UNKNOWN",
    shortTitle: shorten(node.title),
    title: node.title,
  };
}

/**
 * Titles are truncated here rather than by `text-max-width` alone.
 *
 * Cytoscape's ellipsis wraps on the rendered string, so a 200-character title
 * still costs 200 characters of layout text measurement per node per frame.
 * Cutting at a word boundary first keeps the label legible and the measurement
 * cheap.
 */
export function shorten(title: string, max = 42): string {
  if (title.length <= max) return title;
  const cut = title.slice(0, max);
  const lastSpace = cut.lastIndexOf(" ");
  return `${(lastSpace > max * 0.6 ? cut.slice(0, lastSpace) : cut).trimEnd()}…`;
}
