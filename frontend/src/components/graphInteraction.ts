/**
 * Graph interaction decisions that are worth testing, extracted from the
 * effects they were tangled into.
 *
 * Both of these were review findings, and in both cases "this logic has
 * nowhere to live" was as much the defect as the wrong comparison. Inside a
 * `useEffect` closure over a Cytoscape instance there is no way to ask "which
 * nodes should the arrow keys visit?" without rendering a canvas.
 */
import type { GraphNodeOut } from "../api/client";

export interface ScoreRange {
  min: number;
  max: number;
}

/**
 * Is this node currently drawn?
 *
 * The score threshold hides weak *candidates* only. Anything the user has
 * touched -- a seed they added by hand, a paper they liked or disliked -- stays
 * visible whatever the slider says. A seed has no score at all, so treating
 * absence as zero would make the user's own choices the first thing to vanish.
 */
export function isVisibleAt(node: GraphNodeOut, threshold: number): boolean {
  if (node.state !== "CANDIDATE") return true;
  return (node.score ?? 0) >= threshold;
}

/**
 * The nodes the arrow keys may land on, in a stable order.
 *
 * Keyboard navigation used to walk the full node list and ignore the filter,
 * so with 44 of 98 shown roughly half of all key presses selected something
 * `display: none` -- the footer and inspector changed, the viewport animated
 * to empty space, and nothing visible was selected. Whatever the eye can see
 * is what the keyboard should reach.
 *
 * Ordered by id rather than array position so the sequence does not reshuffle
 * when the graph response happens to come back in a different order.
 */
export function navigableNodes(nodes: GraphNodeOut[], threshold: number): GraphNodeOut[] {
  return nodes.filter((n) => isVisibleAt(n, threshold)).sort((a, b) => a.id - b.id);
}

/**
 * Keep a chosen threshold inside the range the slider is drawn against.
 *
 * `null` means "not chosen yet" and resolves to the floor: a slider that
 * starts mid-range would hide papers before anyone touched it.
 *
 * The clamp matters because the range is recomputed from whatever nodes are
 * loaded, while the threshold is an absolute score the user picked earlier.
 * When those disagree the input element clamps its *thumb* but not its value,
 * so the graph showed 0 of N while the control sat at what looked like its
 * maximum -- and dragging left did nothing until the value fell back in range.
 */
export function clampThreshold(value: number | null, range: ScoreRange): number {
  if (value === null) return range.min;
  return Math.min(Math.max(value, range.min), range.max);
}
