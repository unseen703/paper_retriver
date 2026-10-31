import { describe, expect, it } from "vitest";
import { MIN_HULL_NODES, communityHulls, convexHull, hullLabels, type Hull } from "./communityHulls";

describe("convexHull", () => {
  it("drops interior and collinear points", () => {
    const hull = convexHull([
      { x: 0, y: 0 },
      { x: 4, y: 0 },
      { x: 4, y: 4 },
      { x: 0, y: 4 },
      { x: 2, y: 2 },
      { x: 2, y: 0 },
    ]);
    expect(hull).toHaveLength(4);
    expect(hull).not.toContainEqual({ x: 2, y: 2 });
    expect(hull).not.toContainEqual({ x: 2, y: 0 });
  });

  it("is independent of input order and does not mutate it", () => {
    const pts = [
      { x: 3, y: 1 },
      { x: 0, y: 0 },
      { x: 5, y: 5 },
      { x: 1, y: 4 },
    ];
    const copy = pts.map((p) => ({ ...p }));
    expect(convexHull(pts)).toEqual(convexHull([...pts].reverse()));
    expect(pts).toEqual(copy);
  });

  it("returns fewer than three points unchanged", () => {
    expect(convexHull([{ x: 1, y: 1 }])).toEqual([{ x: 1, y: 1 }]);
  });
});

describe("communityHulls", () => {
  const tri = (id: number | null, dx = 0) => [
    { x: dx, y: 0, communityId: id },
    { x: dx + 10, y: 0, communityId: id },
    { x: dx + 5, y: 8, communityId: id },
  ];

  it("skips unassigned nodes", () => {
    expect(communityHulls(tri(null))).toEqual([]);
  });

  it("skips communities too small to enclose an area", () => {
    expect(MIN_HULL_NODES).toBe(3);
    expect(communityHulls(tri(1).slice(0, 2))).toEqual([]);
    // Three collinear nodes collapse to a segment: no area, no hull.
    expect(
      communityHulls([0, 1, 2].map((i) => ({ x: i, y: 0, communityId: 1 }))),
    ).toEqual([]);
  });

  it("returns one hull per community, ordered by id", () => {
    const hulls = communityHulls([...tri(7, 100), ...tri(2)]);
    expect(hulls.map((h) => h.communityId)).toEqual([2, 7]);
  });
});

describe("hullLabels", () => {
  const hull = (communityId: number): Hull => ({
    communityId,
    points: [
      { x: 0, y: 0 },
      { x: 6, y: 0 },
      { x: 0, y: 3 },
    ],
  });

  it("anchors a label at the mean of the outline", () => {
    const out = hullLabels([hull(1)], new Map([[1, "graph neural"]]));
    expect(out).toEqual([{ communityId: 1, text: "graph neural", x: 2, y: 1 }]);
  });

  it("skips empty, blank and unknown names instead of drawing blanks", () => {
    const names = new Map([
      [1, ""],
      [2, "   "],
    ]);
    expect(hullLabels([hull(1), hull(2), hull(3)], names)).toEqual([]);
  });

  it("keeps hull order", () => {
    const names = new Map([
      [5, "b"],
      [2, "a"],
    ]);
    expect(hullLabels([hull(2), hull(5)], names).map((l) => l.communityId)).toEqual([2, 5]);
  });
});
