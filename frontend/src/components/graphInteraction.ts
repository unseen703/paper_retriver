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


/**
 * Does this title contain the query? Case-insensitive substring (R2.11).
 *
 * Substring rather than fuzzy on purpose. Someone typing into "find in graph"
 * is looking for a paper they already know is there, and a fuzzy matcher that
 * helpfully returns four near-misses alongside it makes that harder, not
 * easier. An empty query matches nothing rather than everything: the box is
 * off, so nothing should be marked.
 */
export function matchesQuery(title: string | null | undefined, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return false;
  return String(title ?? "").toLowerCase().includes(needle);
}

/**
 * Split loaded nodes into the ones a query matches and the ones it does not.
 *
 * BUILD.md R2.11: the search "dims non-matches, never mutates the graph or
 * calls the API". Both halves are returned rather than just the matches,
 * because dimming is the half that makes a match findable -- ringing four
 * nodes green in a field of two hundred equally bright ones is not a search
 * result, it is a puzzle.
 *
 * With no query, everything is `rest` and nothing is dimmed: the caller
 * clears both classes and the graph goes back to normal.
 */
export function partitionByQuery(
  nodes: { id: number; title?: string | null }[],
  query: string,
): { matched: number[]; rest: number[] } {
  const matched: number[] = [];
  const rest: number[] = [];
  for (const node of nodes) {
    (matchesQuery(node.title, query) ? matched : rest).push(node.id);
  }
  return { matched, rest };
}

/** One node's saved place, in the shape `PUT /positions` accepts. */
export interface SavedPosition {
  paper_id: number;
  x: number;
  y: number;
}

/**
 * Clean a set of laid-out positions for saving (R2.12).
 *
 * Two jobs, both about not corrupting a good arrangement:
 *
 * **Drop non-finite coordinates.** A layout that goes wrong produces NaN, and
 * `JSON.stringify` turns NaN into `null` -- so the server sees null where a
 * number belongs. It rejects that with a 422, which would throw away the
 * whole request including every position that was fine. Dropping the bad ones
 * here keeps the good ones.
 *
 * **Round to whole units.** Positions are pixels in Cytoscape's space; the
 * fifteenth decimal place is noise that changes on every layout and makes
 * every save look like a change.
 */
export function positionsToSave(
  entries: { id: number; x: number; y: number }[],
): SavedPosition[] {
  const out: SavedPosition[] = [];
  for (const { id, x, y } of entries) {
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    out.push({ paper_id: id, x: Math.round(x), y: Math.round(y) });
  }
  return out;
}
