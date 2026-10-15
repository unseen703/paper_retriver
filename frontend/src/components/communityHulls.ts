/**
 * Convex hulls around communities, as pure geometry.
 *
 * The canvas draws what this returns and nothing else, so the rules -- which
 * nodes count, when a group is too small to enclose, how the outline is
 * ordered -- live here where they can be tested without a Cytoscape instance.
 */

export interface HullPoint {
  readonly x: number;
  readonly y: number;
}

export interface HullInput extends HullPoint {
  /** Null when community detection has not run or the node is unassigned. */
  readonly communityId: number | null;
}

export interface Hull {
  readonly communityId: number;
  /** Counter-clockwise outline, no repeated closing point. */
  readonly points: readonly HullPoint[];
}

/** Fewer than three points enclose no area; a hull there would be a line or a dot. */
export const MIN_HULL_NODES = 3;

function cross(o: HullPoint, a: HullPoint, b: HullPoint): number {
  return (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
}

/** Andrew's monotone chain. Collinear points are dropped; input is not mutated. */
export function convexHull(points: readonly HullPoint[]): HullPoint[] {
  const sorted = [...points].sort((a, b) => a.x - b.x || a.y - b.y);
  if (sorted.length < 3) return sorted;

  const build = (input: readonly HullPoint[]): HullPoint[] => {
    const chain: HullPoint[] = [];
    for (const p of input) {
      while (chain.length >= 2 && cross(chain[chain.length - 2], chain[chain.length - 1], p) <= 0) {
        chain.pop();
      }
      chain.push(p);
    }
    chain.pop();
    return chain;
  };

  return [...build(sorted), ...build([...sorted].reverse())];
}

/**
 * One hull per community with at least `MIN_HULL_NODES` distinct positioned
 * nodes, ordered by community id so the draw order is stable between frames.
 */
export function communityHulls(nodes: readonly HullInput[]): Hull[] {
  const groups = new Map<number, HullPoint[]>();
  for (const node of nodes) {
    if (node.communityId === null) continue;
    const group = groups.get(node.communityId) ?? [];
    group.push({ x: node.x, y: node.y });
    groups.set(node.communityId, group);
  }

  const hulls: Hull[] = [];
  for (const communityId of [...groups.keys()].sort((a, b) => a - b)) {
    const points = convexHull(groups.get(communityId) ?? []);
    if (points.length >= MIN_HULL_NODES) hulls.push({ communityId, points });
  }
  return hulls;
}
