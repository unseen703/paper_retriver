/**
 * The Cytoscape canvas (R1.21), tuned to feel like the archived D3 prototype.
 *
 * **The graph lives in the Cytoscape instance, never in React state.**
 * CLAUDE.md is explicit, and the reason is concrete: a layout moves every node
 * on every frame, so mirroring positions into state would re-render the tree
 * sixty times a second. The instance is held in a `useRef`, which React never
 * reads and never diffs. This component renders one empty `<div>`; everything
 * visible is drawn into a canvas.
 *
 * What makes the prototype feel responsive is four things, all of them here:
 *
 *   drag      nodes are grabbable, and a dragged node stays where it is put
 *   hover     the node and its neighbours light up, the rest fades back
 *   zoom/pan  wheel and drag on the background
 *   settling  the layout animates into place instead of snapping
 *
 * The hover trace is the one that earns the most. At this density you cannot
 * follow a single paper's citations by eye; pointing at it and having its
 * neighbourhood light is how you actually read the graph.
 */
import { useEffect, useRef } from "react";
import cytoscape from "cytoscape";
import fcose from "cytoscape-fcose";
import type { GraphEdgeOut, GraphNodeOut } from "../api/client";
import { stylesheet, toElementData, withLabelFlags } from "./stylesheet";

cytoscape.use(fcose);

// At R1 there are no communities, so edge length is constant. PLAN.md's
// community-aware version (45 inside a cluster, 220 across) arrives with
// Leiden at R6 -- "at R1, before communities exist, use a constant
// idealEdgeLength -- it's fine for 100 nodes".
const IDEAL_EDGE_LENGTH = 95;

/**
 * Deterministic starting positions for nodes that have none.
 *
 * Three attempts, and the reasons the first two failed are the useful part.
 *
 * **All at (0, 0)** -- what Cytoscape does by default. `randomize: false` means
 * fcose starts from wherever nodes already are, so the first layout began with
 * every node stacked on one point. A force simulation with no asymmetry to
 * work with resolved that into a straight diagonal line.
 *
 * **A perfect ring** -- fixed the line, and produced a ring. A circle under
 * symmetric repulsion is a stable configuration: it is a local minimum, so
 * fcose had no gradient to descend and the nodes stayed roughly where they
 * were put. The graph rendered as a necklace with the seeds in the middle.
 *
 * **A seeded scatter** -- this. A hash of the node id drives a small
 * deterministic PRNG, so positions are irregular enough to break the symmetry
 * and still a pure function of the ids. Same graph, same layout, every load
 * (CLAUDE.md rule 7) -- without `randomize: true`, which would buy the same
 * asymmetry at the cost of a picture that rearranges itself on every reload.
 */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function seedScatter(ids: number[]): Map<number, cytoscape.Position> {
  // Radius grows with sqrt(n) so density stays roughly constant as the graph
  // grows, rather than the scatter becoming a dense blob at 200 nodes.
  const spread = Math.max(240, Math.sqrt(Math.max(ids.length, 1)) * 90);
  return new Map(
    ids.map((id) => {
      // Seeded per node id, not by array position: a node's start point must
      // not move when an unrelated node appears ahead of it in the response.
      const rand = mulberry32(id * 2654435761);
      const angle = rand() * 2 * Math.PI;
      // sqrt of the uniform sample gives an even area distribution; without it
      // points bunch toward the centre.
      const radius = Math.sqrt(rand()) * spread;
      return [id, { x: radius * Math.cos(angle), y: radius * Math.sin(angle) }];
    }),
  );
}

export interface GraphCanvasProps {
  nodes: GraphNodeOut[];
  edges: GraphEdgeOut[];
  onSelect?: (paperId: number | null) => void;
  onHover?: (paperId: number | null) => void;
  /** Bumping this re-runs the layout, for a "tidy up" control. */
  relayoutToken?: number;
}

export function GraphCanvas({
  nodes,
  edges,
  onSelect,
  onHover,
  relayoutToken = 0,
}: GraphCanvasProps) {
  const container = useRef<HTMLDivElement>(null);
  const cy = useRef<cytoscape.Core | null>(null);
  // Held in refs so the mount effect never lists them as dependencies; a new
  // inline callback each render would otherwise tear down and rebuild the
  // whole graph on every parent render.
  const onSelectRef = useRef(onSelect);
  const onHoverRef = useRef(onHover);
  // Set once the user zooms; suppresses the automatic re-fit so a window
  // resize cannot yank the view away from where they put it.
  const userAdjusted = useRef(false);
  // The layout currently running, so a new one can stop it first. Without
  // this, React StrictMode's double-mount left two animated layouts racing and
  // the second started from wherever the first had got to -- which made the
  // final positions depend on timing, and broke determinism.
  const layoutRef = useRef<cytoscape.Layouts | null>(null);
  onSelectRef.current = onSelect;
  onHoverRef.current = onHover;

  // Mount once. The instance outlives every render.
  useEffect(() => {
    if (!container.current) return;

    const instance = cytoscape({
      container: container.current,
      style: stylesheet,
      // Box selection fights click-to-inspect, and nothing yet acts on a
      // multi-selection.
      boxSelectionEnabled: false,
      // Cytoscape's default is aggressive enough to overshoot on a trackpad.
      wheelSensitivity: 0.2,
      minZoom: 0.05,
      maxZoom: 8,
    });

    // --- hover: light the neighbourhood, fade the rest --------------------
    const clearHighlight = () => {
      instance.elements().removeClass("hl fade hover");
    };

    instance.on("mouseover", "node", (event) => {
      const node = event.target as cytoscape.NodeSingular;
      const near = node.closedNeighborhood();
      instance.elements().addClass("fade");
      near.removeClass("fade").addClass("hl");
      // The pointed-at node gets a white rim on top of the neighbourhood's
      // yellow, so it stays distinguishable from what it lit up -- the
      // prototype's `.node circle:hover` versus `.node.neighbor circle`.
      node.addClass("hover");
      // A label for the hovered node even when it is not one of the labelled
      // few -- this is how you read an unlabelled ball without clicking it.
      node.style("label", node.data("title"));
      onHoverRef.current?.(Number(node.id()));
      if (container.current) container.current.style.cursor = "pointer";
    });

    instance.on("mouseout", "node", (event) => {
      const node = event.target as cytoscape.NodeSingular;
      clearHighlight();
      // Back to whatever `withLabelFlags` decided; removeStyle drops the
      // per-element override rather than blanking the label.
      node.removeStyle("label");
      onHoverRef.current?.(null);
      if (container.current) container.current.style.cursor = "default";
    });

    // --- click: select, background click clears ---------------------------
    instance.on("tap", "node", (event) => {
      onSelectRef.current?.(Number(event.target.id()));
    });
    instance.on("tap", (event) => {
      if (event.target === instance) {
        onSelectRef.current?.(null);
        clearHighlight();
      }
    });

    // --- drag: a moved node stays moved -----------------------------------
    // `grabbable` is Cytoscape's default, but the free-drag handler matters:
    // without it the next data sync would re-run the layout and undo the
    // user's arrangement. Locking on drop makes the layout treat it as fixed.
    instance.on("free", "node", (event) => {
      (event.target as cytoscape.NodeSingular).data("pinned", true);
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
    // looks exactly like "nothing rendered".
    //
    // Re-fitting here is not optional. `resize()` alone updates the viewport
    // and leaves the zoom that was computed against the old one, so a layout
    // that ran before the grid resolved stayed fitted to a zero-height box --
    // the graph rendered at zoom 1 with half of it off-screen.
    //
    // Guarded by `userAdjusted`, so a window resize does not yank the view
    // back from wherever someone has zoomed to.
    let lastSize = "";
    const observer = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      const size = box ? `${Math.round(box.width)}x${Math.round(box.height)}` : "";
      if (size === lastSize) return;
      lastSize = size;
      instance.resize();
      if (!userAdjusted.current && instance.nodes().nonempty()) {
        instance.fit(undefined, 40);
      }
    });
    observer.observe(container.current);

    // Real user gestures only. `fit()` also changes zoom and pan, so watching
    // Cytoscape's own viewport event would make the canvas mark itself as
    // user-adjusted the first time it fitted.
    const markAdjusted = () => {
      userAdjusted.current = true;
    };
    const el = container.current;
    el.addEventListener("wheel", markAdjusted, { passive: true });

    return () => {
      observer.disconnect();
      el.removeEventListener("wheel", markAdjusted);
      instance.destroy();
      cy.current = null;
    };
  }, []);

  // Sync data. Cytoscape diffs by id, so this is an update rather than a
  // rebuild -- a node already on screen keeps its position.
  useEffect(() => {
    const instance = cy.current;
    if (!instance) return;

    layoutRef.current?.stop();
    const wanted = new Set(nodes.map((n) => String(n.id)));
    const scatter = seedScatter(nodes.map((n) => n.id));
    const labels = withLabelFlags(nodes);
    let added = 0;

    instance.batch(() => {
      // Remove what is gone first, so a removed node cannot leave a dangling
      // edge that Cytoscape would refuse to render.
      instance.nodes().forEach((element) => {
        if (!wanted.has(element.id())) element.remove();
      });

      for (const node of nodes) {
        const data = { ...toElementData(node), label: labels.get(node.id) };
        const existing = instance.getElementById(data.id);
        if (existing.nonempty()) {
          // Preserve `pinned`: a data() call replaces the whole object, which
          // would silently unpin every node the user had arranged.
          existing.data({ ...data, pinned: existing.data("pinned") });
        } else {
          added += 1;
          instance.add({
            group: "nodes",
            data,
            // A saved position from R2.12 if there is one; otherwise a
            // deterministic scattered point, so the layout starts from
            // something asymmetric enough for fcose to improve on.
            position: node.pos
              ? { x: node.pos.x, y: node.pos.y }
              : (scatter.get(node.id) ?? { x: 0, y: 0 }),
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
    // Nothing new arrived, so the existing arrangement is still correct.
    // Re-running the layout here would rearrange a picture the user has
    // learned every time the graph query refetched.
    if (added === 0 && relayoutToken === 0) return;

    // "Tidy" is an explicit request to rearrange, which includes re-framing.
    if (relayoutToken > 0) userAdjusted.current = false;
    // A fresh graph -- every node new -- is laid out from the deterministic
    // scatter rather than from whatever happens to be on screen. That makes
    // the first layout idempotent, which it has to be: StrictMode runs this
    // effect twice, and without the reset the second run inherited the first
    // run's half-animated positions, so the result depended on timing.
    // Growth is left alone, so an expansion still extends the picture the user
    // has learned instead of rearranging it (PLAN.md M6).
    const freshGraph = added === instance.nodes().length;
    layoutRef.current = runLayout(instance, freshGraph ? scatter : null);
  }, [nodes, edges, relayoutToken]);

  return <div ref={container} style={{ width: "100%", height: "100%" }} />;
}

function runLayout(
  instance: cytoscape.Core,
  resetTo: Map<number, cytoscape.Position> | null,
): cytoscape.Layouts {
  if (resetTo) {
    instance.nodes().forEach((node) => {
      const start = resetTo.get(Number(node.id()));
      if (start && !node.data("pinned")) node.position({ ...start });
    });
  }

  // Pin what the user has dragged and anything with a saved position.
  // PLAN.md M6: an expansion must grow the graph outward, not rearrange a
  // picture the user has learned.
  const pinned: { nodeId: string; position: cytoscape.Position }[] = [];
  instance.nodes().forEach((node) => {
    if (node.data("pinned")) {
      pinned.push({ nodeId: node.id(), position: { ...node.position() } });
    }
  });

  // Stop whatever was running: two animated layouts on one instance fight
  // over every node's position.
  const layout = instance.layout({
      name: "fcose",
      quality: "proof",
      // Deterministic: the same graph lays out the same way twice.
      // CLAUDE.md rule 7, and PLAN.md M6.
      randomize: false,
      // Animated, unlike the first cut. Watching the graph settle is most of
      // what made the prototype feel like a live network rather than a
      // picture, and it costs nothing -- fcose computes the final positions
      // either way, this only animates the transition to them.
      animate: true,
      animationDuration: 600,
      animationEasing: "ease-out-cubic",
      // Fit from the `ready` callback, not through fcose's own `fit` option
      // and not on `layoutstop`. Three facts, each established by measuring
      // rather than by reading the docs:
      //
      //   * fcose's `fit: true` with `animate: true` fits against the
      //     positions it STARTS from, so the graph was drawn at zoom 1 with an
      //     852px-tall bounding box in a 630px viewport -- most of it simply
      //     off-screen, which reads as "the layout is broken".
      //   * fcose's animated path never emits `layoutstop` and never calls the
      //     `stop` option. Instrumenting the live instance showed it firing
      //     exactly layoutstart -> layoutready and then nothing, so both of
      //     those hooks are dead ends here.
      //   * `ready` fires with the FINAL positions already assigned -- the
      //     bounding box measured there is byte-identical to the one after the
      //     animation finishes (709x852 both times). The animation only tweens
      //     the transition to positions that are already decided.
      //
      // So `ready` is both the earliest and the only reliable hook.
      fit: false,
      ready: () => {
        instance.fit(undefined, 40);
      },
      idealEdgeLength: () => IDEAL_EDGE_LENGTH,
      // Seeds push harder, so the clusters they anchor stay apart.
      nodeRepulsion: (node: cytoscape.NodeSingular) =>
        node.data("state") === "SEED" ? 20000 : 6000,
      nodeSeparation: 90,
      numIter: 2500,
    ...(pinned.length ? { fixedNodeConstraint: pinned } : {}),
  } as cytoscape.LayoutOptions);

  layout.run();
  return layout;
}
