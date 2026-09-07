/**
 * The Cytoscape canvas (R1.21).
 *
 * **The graph lives in the Cytoscape instance, never in React state.**
 * CLAUDE.md is explicit, and the reason is concrete: fcose moves every node on
 * every animation frame, so mirroring positions into state would re-render the
 * whole tree sixty times a second during a layout. The instance is held in a
 * `useRef`, which React never reads and never diffs.
 *
 * The component therefore renders exactly one thing -- an empty `<div>` -- and
 * everything visible inside it is drawn by Cytoscape into a canvas.
 *
 * **`randomize: false`** is layout stability, PLAN.md M6. With randomize on,
 * adding one node re-scrambles the entire picture and the user loses the
 * spatial memory they were using to navigate. Existing nodes are additionally
 * pinned through `fixedNodeConstraint`, so an expansion grows the graph
 * outward instead of rearranging it.
 */
import { useEffect, useRef } from "react";
import cytoscape from "cytoscape";
import fcose from "cytoscape-fcose";
import type { GraphEdgeOut, GraphNodeOut } from "../api/client";
import { stylesheet, toElementData } from "./stylesheet";

cytoscape.use(fcose);

// At R1 there are no communities, so edge length is constant. PLAN.md's
// community-aware version (45 inside a cluster, 220 across) arrives with
// Leiden at R6 -- "at R1, before communities exist, use a constant
// idealEdgeLength -- it's fine for 100 nodes".
const IDEAL_EDGE_LENGTH = 80;

/**
 * Deterministic starting positions for nodes that have none.
 *
 * `randomize: false` means fcose starts from wherever the nodes already are.
 * A freshly added node is at (0, 0), so a first layout starts with every node
 * stacked on one point -- and a force simulation with no asymmetry to work
 * with resolves that into a straight diagonal line rather than a graph. That
 * is what the first render of this component actually produced.
 *
 * The obvious fix, `randomize: true`, buys a usable layout at the cost of
 * CLAUDE.md rule 7: the same graph would lay out differently on every load.
 * Seeding a circle instead keeps both properties -- the ring is a pure
 * function of the sorted node ids, so the layout is reproducible, and it gives
 * fcose the spread it needs to separate clusters.
 *
 * Ordering is by id, not by array position, so a node's start point does not
 * move when an unrelated node is added ahead of it in the response.
 */
function seedRing(ids: number[]): Map<number, cytoscape.Position> {
  const sorted = [...ids].sort((a, b) => a - b);
  // Grows with node count so a large graph does not start impossibly dense.
  const radius = Math.max(200, sorted.length * 12);
  const step = (2 * Math.PI) / Math.max(sorted.length, 1);
  return new Map(
    sorted.map((id, index) => [
      id,
      { x: radius * Math.cos(index * step), y: radius * Math.sin(index * step) },
    ]),
  );
}

export interface GraphCanvasProps {
  nodes: GraphNodeOut[];
  edges: GraphEdgeOut[];
  onSelect?: (paperId: number | null) => void;
}

export function GraphCanvas({ nodes, edges, onSelect }: GraphCanvasProps) {
  const container = useRef<HTMLDivElement>(null);
  const cy = useRef<cytoscape.Core | null>(null);
  // Held in a ref so the Cytoscape effect never has to list it as a
  // dependency; a new inline callback each render would otherwise tear down
  // and rebuild the whole graph on every parent render.
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  // Mount once. The instance outlives every render.
  useEffect(() => {
    if (!container.current) return;

    const instance = cytoscape({
      container: container.current,
      style: stylesheet,
      // Cytoscape's own box selection fights click-to-inspect, and there is
      // nothing yet that acts on a multi-selection.
      boxSelectionEnabled: false,
      wheelSensitivity: 0.2,
    });

    instance.on("tap", "node", (event) => {
      onSelectRef.current?.(Number(event.target.id()));
    });
    // Tapping the background clears the selection. Without this the inspector
    // stays pinned to a node the user has visually moved on from.
    instance.on("tap", (event) => {
      if (event.target === instance) onSelectRef.current?.(null);
    });

    cy.current = instance;

    // Dev-only handle. Everything this component draws lives in a canvas, so
    // there is no DOM to inspect when a graph looks wrong -- `window.cy` in
    // the console is the only way to ask where a node actually is. Stripped
    // from production builds by the `import.meta.env.DEV` guard.
    if (import.meta.env.DEV) {
      (window as unknown as { cy?: cytoscape.Core }).cy = instance;
    }

    // Cytoscape measures its container once, at construction. This component
    // mounts inside a CSS grid row that has not resolved its height yet, so
    // the instance captured a viewport of height 0 -- nodes were laid out at
    // correct positions and drawn into a strip with no vertical extent, which
    // looks exactly like "nothing rendered". Observing the container fixes
    // that first paint and every window resize afterwards with one mechanism.
    const observer = new ResizeObserver(() => {
      instance.resize();
      if (instance.nodes().nonempty()) instance.fit(undefined, 40);
    });
    observer.observe(container.current);

    return () => {
      observer.disconnect();
      instance.destroy();
      cy.current = null;
    };
  }, []);

  // Sync data. Cytoscape diffs by id, so this is an update rather than a
  // rebuild -- a node already on screen keeps its position.
  useEffect(() => {
    const instance = cy.current;
    if (!instance) return;

    const wanted = new Set(nodes.map((n) => String(n.id)));
    const ring = seedRing(nodes.map((n) => n.id));
    instance.batch(() => {
      // Remove what is gone first, so a removed node cannot leave a dangling
      // edge that Cytoscape would refuse to render.
      instance.nodes().forEach((element) => {
        if (!wanted.has(element.id())) element.remove();
      });

      for (const node of nodes) {
        const data = toElementData(node);
        const existing = instance.getElementById(data.id);
        if (existing.nonempty()) {
          existing.data(data);
        } else {
          instance.add({
            group: "nodes",
            data,
            // A saved position from R2.12 if there is one; otherwise a
            // deterministic point on the seed ring, so the layout below starts
            // from something other than every node at the origin.
            position: node.pos
              ? { x: node.pos.x, y: node.pos.y }
              : (ring.get(node.id) ?? { x: 0, y: 0 }),
          });
        }
      }

      const edgeIds = new Set(edges.map((e) => `${e.source}-${e.target}`));
      instance.edges().forEach((element) => {
        if (!edgeIds.has(element.id())) element.remove();
      });
      for (const edge of edges) {
        const id = `${edge.source}-${edge.target}`;
        if (instance.getElementById(id).nonempty()) continue;
        instance.add({
          group: "edges",
          data: {
            id,
            source: String(edge.source),
            target: String(edge.target),
            influential: edge.is_influential ? 1 : 0,
          },
        });
      }
    });

    if (instance.nodes().length === 0) return;

    // Pin what the user has already arranged. PLAN.md M6: an expansion must
    // grow the graph outward, not rearrange a picture the user has learned.
    const pinned = nodes
      .filter((n) => n.pos)
      .map((n) => ({ nodeId: String(n.id), position: { x: n.pos!.x, y: n.pos!.y } }));

    instance
      .layout({
        name: "fcose",
        quality: "proof",
        // Deterministic: the same graph lays out the same way twice.
        // CLAUDE.md rule 7, and PLAN.md M6.
        randomize: false,
        animate: false,
        idealEdgeLength: () => IDEAL_EDGE_LENGTH,
        // Seeds push harder, so the clusters they anchor stay apart.
        nodeRepulsion: (node: cytoscape.NodeSingular) =>
          node.data("state") === "SEED" ? 20000 : 6000,
        nodeSeparation: 90,
        numIter: 2500,
        ...(pinned.length ? { fixedNodeConstraint: pinned } : {}),
      } as cytoscape.LayoutOptions)
      .run();

    // resize() before fit(): fit computes a zoom from the viewport dimensions,
    // so fitting against a stale zero-height viewport produces a zoom of
    // Infinity and draws nothing.
    instance.resize();
    instance.fit(undefined, 40);
  }, [nodes, edges]);

  return <div ref={container} style={{ width: "100%", height: "100%" }} />;
}
