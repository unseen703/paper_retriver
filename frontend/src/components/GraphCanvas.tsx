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
import cola from "cytoscape-cola";
import type { GraphEdgeOut, GraphNodeOut } from "../api/client";
import {
  type LabelMode,
  stylesheet,
  toElementData,
  withLabelFlags,
  yearRange,
} from "./stylesheet";

cytoscape.use(fcose);
// A SECOND layout engine, deliberately, and a documented departure from
// CLAUDE.md's "Cytoscape.js + fcose" -- approved before adding it.
//
// The two do different jobs. fcose is a batch layout: it computes final
// positions and stops, which is what you want for placing a graph. cola runs
// a continuous constraint simulation, which is what makes a drag feel like
// pulling on a net rather than sliding a sticker -- the archived prototype
// got that from d3.forceSimulation running forever with alphaTarget bumped
// during a drag, and nothing fcose exposes reproduces it.
//
// cola is a first-party Cytoscape extension, so the part of the lock that
// matters -- not React Flow, not raw D3 -- is untouched.
cytoscape.use(cola);

// At R1 there are no communities, so edge length is constant. PLAN.md's
// community-aware version (45 inside a cluster, 220 across) arrives with
// Leiden at R6 -- "at R1, before communities exist, use a constant
// idealEdgeLength -- it's fine for 100 nodes".
const IDEAL_EDGE_LENGTH = 95;

// How far a node wanders from its rest position while idling, in graph units.
// Small on purpose: enough that the graph is visibly alive, not so much that
// edges visibly stretch or a node you are aiming at moves out from under the
// cursor.
const FLOAT_RADIUS = 3.5;

// How long the batch layout animates into place.
const LAYOUT_ANIMATION_MS = 600;

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
  /** Currently selected paper, so keyboard navigation knows where it is. */
  selectedId?: number | null;
  onSelect?: (paperId: number | null) => void;
  onHover?: (paperId: number | null) => void;
  /** Bumping this re-runs the layout, for a "tidy up" control. */
  relayoutToken?: number;
  /** Bumping this returns the viewport to a fitted view. */
  resetViewToken?: number;
  /** hidden | all | relevant, the prototype's three modes. */
  labelMode?: LabelMode;
  /** Hide unlabelled candidates scoring below this. */
  scoreThreshold?: number;
  /** Title substring to highlight; empty clears. */
  searchQuery?: string;
  /** Reports how many nodes survive the score filter, for the readout. */
  onVisibleCount?: (visible: number, total: number) => void;
  /** Reports how many nodes match the search. */
  onMatchCount?: (matches: number) => void;
}

export function GraphCanvas({
  nodes,
  edges,
  selectedId = null,
  onSelect,
  onHover,
  relayoutToken = 0,
  resetViewToken = 0,
  labelMode = "relevant",
  scoreThreshold = 0,
  searchQuery = "",
  onVisibleCount,
  onMatchCount,
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
  // The continuous simulation, alive only while a drag is in flight.
  const liveRef = useRef<cytoscape.Layouts | null>(null);
  // The relayoutToken value the last layout ran for. Comparing against 0 meant
  // the guard stopped working after the first Tidy click, so every subsequent
  // refetch rearranged a graph the user had arranged by hand.
  const lastTokenRef = useRef(0);
  // The clicked node whose neighbourhood is pinned, or null. Hover is
  // suppressed while this is set.
  const pinnedRef = useRef<number | null>(null);
  const draggingRef = useRef(false);
  // Set whenever something has moved nodes for real -- a drag, a layout -- so
  // the float re-reads its rest positions instead of yanking them back.
  const rebaseRef = useRef(true);
  // Whether the graph has been framed once. After that, framing is the user's
  // business.
  const hasFittedRef = useRef(false);
  // True while a batch layout is still moving nodes. The float must hold off
  // until then: it captures rest positions once and pushes nodes back onto
  // them every frame, so starting mid-animation froze the layout half-settled
  // and fought fcose for control of every node.
  const layoutRunningRef = useRef(false);

  /**
   * Frame the graph, once, and only if it can actually be framed.
   *
   * `fit()` computes a zoom from the viewport, so calling it against a
   * container that has not resolved its height yet is a silent no-op -- it
   * returns without changing anything and without complaining. The bug that
   * caused was subtle: the layout's `ready` fires before the CSS grid has
   * sized anything, so the fit did nothing, the "already framed" flag was set
   * anyway, and the ResizeObserver -- which fires later, with a real viewport
   * -- then declined to fit because the flag said the job was done. The graph
   * sat at zoom 1 with a 1826x1686 layout in a 1280x572 window.
   *
   * So the flag is set by a fit that WORKED, never by one that was attempted.
   */
  const fitOnce = useRef((instance: cytoscape.Core, force = false) => {
    if (hasFittedRef.current && !force) return;
    const box = instance.container();
    if (!box || box.clientHeight < 1 || box.clientWidth < 1) return;
    if (instance.nodes().empty()) return;
    instance.resize();
    instance.fit(undefined, 40);
    hasFittedRef.current = true;
  });
  const selectedIdRef = useRef(selectedId);
  // An external deselection -- Escape, the inspector's close button -- has to
  // release the pin too, or hover stays dead with nothing lit to explain why.
  useEffect(() => {
    if (selectedId === null && pinnedRef.current !== null) {
      pinnedRef.current = null;
      cy.current?.elements().removeClass("hl fade hover");
    }
  }, [selectedId]);
  onSelectRef.current = onSelect;
  onHoverRef.current = onHover;
  selectedIdRef.current = selectedId;

  // Mount once. The instance outlives every render.
  useEffect(() => {
    if (!container.current) return;

    // Per-instance, not per-component. Refs survive React StrictMode's
    // mount/unmount/mount, so the first mount's layout claimed the one-time
    // fit, the instance it fitted was then destroyed, and the second mount's
    // layout found the flag already true and declined -- leaving the graph
    // unframed at zoom 1. A fresh Cytoscape instance has never been framed,
    // whatever a leftover ref says.
    hasFittedRef.current = false;

    const instance = cytoscape({
      container: container.current,
      style: stylesheet,
      // Box selection fights click-to-inspect, and nothing yet acts on a
      // multi-selection.
      boxSelectionEnabled: false,
      // Cytoscape's default (1). It was 0.2, which I lowered to stop a
      // trackpad overshooting -- and which turned every zoom into a long
      // scroll. Overshooting is recoverable in one flick; needing ten flicks
      // to read a small node is not.
      wheelSensitivity: 1,
      minZoom: 0.05,
      maxZoom: 8,
    });

    // --- hover: light the neighbourhood, fade the rest --------------------
    const clearHighlight = () => {
      instance.elements().removeClass("hl fade hover");
    };

    instance.on("mouseover", "node", (event) => {
      // A pinned neighbourhood outranks the pointer. The prototype's highlight
      // came from clicking, so it survived moving the mouse away to read the
      // detail panel; a hover that overwrote it would undo the thing the click
      // was for.
      if (pinnedRef.current !== null) return;
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
      if (pinnedRef.current !== null) return;
      const node = event.target as cytoscape.NodeSingular;
      clearHighlight();
      // Back to whatever `withLabelFlags` decided; removeStyle drops the
      // per-element override rather than blanking the label.
      node.removeStyle("label");
      onHoverRef.current?.(null);
      if (container.current) container.current.style.cursor = "default";
    });

    // --- click: select and PIN the neighbourhood --------------------------
    //
    // The prototype's `highlightNeighborhood` toggles: clicking the already
    // highlighted node clears it. That matters because the highlight is how
    // you read a paper's connections, and you need to be able to put it away
    // without hunting for empty canvas.
    instance.on("tap", "node", (event) => {
      const node = event.target as cytoscape.NodeSingular;
      const id = Number(node.id());
      if (pinnedRef.current === id) {
        pinnedRef.current = null;
        clearHighlight();
        onSelectRef.current?.(null);
        return;
      }
      pinnedRef.current = id;
      clearHighlight();
      instance.elements().addClass("fade");
      node.closedNeighborhood().removeClass("fade").addClass("hl");
      node.addClass("hover");
      onSelectRef.current?.(id);
    });

    instance.on("tap", (event) => {
      if (event.target === instance) {
        pinnedRef.current = null;
        onSelectRef.current?.(null);
        clearHighlight();
      }
    });

    // --- drag: the network follows, then settles --------------------------
    //
    // Dragging previously moved one node and nothing else: edges stretched
    // rigidly and neighbours stayed put, because fcose had already finished.
    // A cola simulation runs for the duration of the drag instead, so pulling
    // a paper drags its neighbourhood with it and the graph relaxes when you
    // let go -- the prototype's behaviour.
    //
    // It starts on grab and stops on release rather than running forever: a
    // permanently live simulation means nodes drift under the cursor when you
    // are trying to click one, and burns a frame budget continuously for a
    // graph that is not moving.
    // `dragstart`, NOT `grab`. Cytoscape fires `grab` on mousedown, so every
    // CLICK was starting the physics simulation -- which is why nodes appeared
    // to swim away when you selected one. `dragstart` fires only once the
    // pointer actually moves.
    instance.on("dragstart", "node", (event) => {
      const node = event.target as cytoscape.NodeSingular;
      draggingRef.current = true;
      // The dragged node is the anchor -- it follows the pointer, and the
      // simulation solves around it.
      node.lock();
      liveRef.current?.stop();
      liveRef.current = instance.layout({
        name: "cola",
        infinite: true,
        fit: false,
        // Respect what the user has already placed; only unpinned nodes move.
        handleDisconnected: true,
        nodeSpacing: () => 12,
        edgeLength: IDEAL_EDGE_LENGTH,
        randomize: false,
      } as cytoscape.LayoutOptions);
      liveRef.current.run();
    });

    instance.on("free", "node", (event) => {
      const node = event.target as cytoscape.NodeSingular;
      node.unlock();
      draggingRef.current = false;
      // The drag moved things, so the float has to re-base or every node
      // snaps back to where the layout last put it.
      rebaseRef.current = true;
      if (!liveRef.current) return;
      // Where the user put it is where it stays: the next batch layout treats
      // it as a fixed constraint rather than a suggestion.
      node.data("pinned", true);
      // A short tail after release, so the graph eases to rest instead of
      // freezing mid-motion.
      window.setTimeout(() => {
        liveRef.current?.stop();
        liveRef.current = null;
        rebaseRef.current = true;
      }, 400);
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
      // Keep the graph framed until the first layout has settled, but do NOT
      // claim the one-time fit -- only the layout's `ready` may set that flag.
      //
      // Claiming it here was a bug: the observer fires while the opening
      // layout is still animating, so it fitted against the seed-scatter
      // bounding box, marked the graph as framed, and the real layout -- which
      // ends up far larger -- then declined to re-fit. The result was a
      // 1826x1686 graph sitting at zoom 1 in a 1280x572 viewport with most of
      // itself off-screen.
      //
      // After that first framing, `resize()` alone is the right behaviour: it
      // updates the viewport and keeps the zoom and pan someone has chosen.
      fitOnce.current(instance);
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
    // Panning counts too. Tracking only the wheel meant a user who dragged the
    // background to reach a distant cluster had that pan thrown away by the
    // next window resize, while a zoom would have been respected -- which
    // makes the behaviour look arbitrary rather than protective.
    instance.on("dragpan", markAdjusted);

    return () => {
      observer.disconnect();
      el.removeEventListener("wheel", markAdjusted);
      liveRef.current?.stop();
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
    const labels = withLabelFlags(nodes, labelMode);
    const years = yearRange(nodes);
    let added = 0;

    instance.batch(() => {
      // Remove what is gone first, so a removed node cannot leave a dangling
      // edge that Cytoscape would refuse to render.
      instance.nodes().forEach((element) => {
        if (!wanted.has(element.id())) element.remove();
      });

      for (const node of nodes) {
        const data = { ...toElementData(node, years), label: labels.get(node.id) };
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

    // Did somebody press Tidy since the last layout? Comparing the token
    // against its previous value, not against 0. Against 0 the guard stopped
    // working the moment the counter left 0, so after one Tidy click every
    // subsequent refetch silently rearranged a graph the user had arranged by
    // hand.
    const tidyRequested = relayoutToken !== lastTokenRef.current;
    lastTokenRef.current = relayoutToken;

    // Nothing new arrived and nobody asked: the existing arrangement is still
    // correct, and re-running the layout would rearrange a picture the user
    // has learned.
    if (added === 0 && !tidyRequested) return;

    // Re-framing is now something the USER asks for, not something that
    // happens to them.
    //
    // The previous rule re-fitted whenever nodes arrived, so that an expansion
    // could not land papers off-screen. It solved that and created something
    // worse: the view zoomed out from under you every time anything changed,
    // which is disorienting in a way that a missed node is not. Tidy and Reset
    // zoom both re-frame on request, and either is one click away.
    if (tidyRequested) userAdjusted.current = false;
    // A fresh graph -- every node new -- is laid out from the deterministic
    // scatter rather than from whatever happens to be on screen. That makes
    // the first layout idempotent, which it has to be: StrictMode runs this
    // effect twice, and without the reset the second run inherited the first
    // run's half-animated positions, so the result depended on timing.
    // Growth is left alone, so an expansion still extends the picture the user
    // has learned instead of rearranging it (PLAN.md M6).
    const freshGraph = added === instance.nodes().length;
    rebaseRef.current = true;
    layoutRunningRef.current = true;
    layoutRef.current = runLayout(instance, freshGraph ? scatter : null, () => {
      // `force` on Tidy: an explicit request to rearrange includes re-framing.
      fitOnce.current(instance, tidyRequested);
    }, () => {
      layoutRunningRef.current = false;
      // Whatever the layout settled on is the new rest position.
      rebaseRef.current = true;
    });
  }, [nodes, edges, relayoutToken, labelMode]);

  // --- idle float ----------------------------------------------------------
  //
  // The prototype's graph never stopped moving, because d3.forceSimulation
  // keeps ticking: nodes drifted like things suspended in water. A settled
  // fcose layout is completely still, which reads as a diagram rather than a
  // network.
  //
  // This is a drift, not a physics simulation, and the distinction is the
  // whole design. A real idle simulation slowly destroys the layout and makes
  // nodes wander away from where you last saw them. Each node instead
  // oscillates around its OWN rest position on a small circle, so the graph
  // breathes while its structure stays exactly where the layout put it.
  //
  // Phase and period come from the node id, so the motion is deterministic and
  // nodes do not pulse in unison -- a graph where everything moves together
  // looks like a rendering glitch rather than like floating.
  //
  // It stops on selection, per the request: once you have picked a paper you
  // are reading it, and a moving target is hostile.
  useEffect(() => {
    const instance = cy.current;
    if (!instance) return;

    // Frozen while a node is selected, while dragging, or while cola is
    // solving -- three different reasons the graph should hold still.
    if (selectedId !== null) return;

    let frame = 0;
    let bases = new Map<string, cytoscape.Position>();
    const started = performance.now();

    const captureBases = () => {
      bases = new Map(instance.nodes().map((n) => [n.id(), { ...n.position() }]));
      rebaseRef.current = false;
    };
    // Deliberately NOT captured here. The layout may still be animating when
    // this effect runs, and rest positions read mid-animation are wrong --
    // the tick captures them once nothing else is moving anything.
    rebaseRef.current = true;

    const tick = (now: number) => {
      frame = requestAnimationFrame(tick);
      // Four reasons to hold still, all of them something else moving nodes.
      if (draggingRef.current || liveRef.current || layoutRunningRef.current) return;
      if (rebaseRef.current) captureBases();

      const t = (now - started) / 1000;
      instance.batch(() => {
        instance.nodes().forEach((node) => {
          const base = bases.get(node.id());
          if (!base) return;
          // Deterministic per node: a cheap hash of the id drives both the
          // phase and a small period jitter.
          const seed = Number(node.id()) * 2654435761;
          const phase = (seed % 1000) / 1000 * Math.PI * 2;
          const period = 5 + ((seed >>> 7) % 40) / 10; // 5.0s to 8.9s
          const w = (Math.PI * 2) / period;
          node.position({
            x: base.x + Math.cos(t * w + phase) * FLOAT_RADIUS,
            y: base.y + Math.sin(t * w + phase * 1.3) * FLOAT_RADIUS,
          });
        });
      });
    };

    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      // Park every node back on its rest position, so pausing does not leave
      // the layout permanently offset by wherever the drift happened to be.
      if (bases.size === 0) return;
      instance.batch(() => {
        instance.nodes().forEach((node) => {
          const base = bases.get(node.id());
          if (base) node.position({ ...base });
        });
      });
    };
  }, [nodes, selectedId]);

  // --- score threshold -----------------------------------------------------
  // The prototype's `applyScoreFilter`: hide unlabelled candidates below the
  // slider, keep everything the user has labelled regardless. A seed you added
  // by hand should never vanish because its score is low -- it has no score,
  // and hiding it would look like the tool losing your work.
  useEffect(() => {
    const instance = cy.current;
    if (!instance) return;
    let visible = 0;
    instance.batch(() => {
      instance.nodes().forEach((node) => {
        const isCandidate = node.data("state") === "CANDIDATE";
        const show = !isCandidate || (node.data("score") ?? 0) >= scoreThreshold;
        node.toggleClass("hidden", !show);
        if (show) visible += 1;
      });
    });
    onVisibleCount?.(visible, instance.nodes().length);
  }, [nodes, scoreThreshold, onVisibleCount]);

  // --- title search --------------------------------------------------------
  // Client-side, unlike the prototype, which round-tripped to
  // `/api/paper/search`. The whole graph is already in memory, so a request
  // would add latency and load to answer a question the browser can answer
  // instantly -- and it would filter the corpus rather than the graph, which
  // is a different question from the one the box appears to ask.
  useEffect(() => {
    const instance = cy.current;
    if (!instance) return;
    const query = searchQuery.trim().toLowerCase();
    instance.nodes().removeClass("match");
    if (!query) {
      onMatchCount?.(0);
      return;
    }
    const matched = instance
      .nodes()
      .filter((node) => String(node.data("title") ?? "").toLowerCase().includes(query));
    matched.addClass("match");
    onMatchCount?.(matched.length);
  }, [nodes, searchQuery, onMatchCount]);

  // --- reset zoom ----------------------------------------------------------
  useEffect(() => {
    const instance = cy.current;
    if (!instance || resetViewToken === 0 || instance.nodes().empty()) return;
    // Animated, like the prototype's 500ms transition to zoomIdentity: a
    // viewport that teleports leaves you re-finding where you were.
    instance.animate({ fit: { eles: instance.elements(), padding: 40 } }, { duration: 400 });
    userAdjusted.current = false;
  }, [resetViewToken]);

  // Keyboard access. Cytoscape draws into a canvas, so there is no DOM for a
  // screen reader and no focusable target for a keyboard -- without this the
  // entire visualisation is skipped between the header buttons and the footer.
  //
  // Arrow keys walk the node list in the same stable order the API returns
  // (by paper id), which is at least predictable; Home and End jump to the
  // ends, Escape clears. Selecting through the keyboard drives exactly the
  // same `onSelect` the mouse does, so the footer readout -- already a live
  // region visually -- becomes the screen-reader output for free.
  const step = (delta: number) => {
    const instance = cy.current;
    if (!instance || nodes.length === 0) return;
    const ordered = [...nodes].sort((a, b) => a.id - b.id);
    const current = ordered.findIndex((n) => n.id === selectedIdRef.current);
    // No selection yet: Down/Right starts at the first node, Up/Left at the last.
    const next =
      current === -1
        ? delta > 0
          ? 0
          : ordered.length - 1
        : (current + delta + ordered.length) % ordered.length;
    const node = ordered[next];
    onSelectRef.current?.(node.id);
    // Bring it into view and highlight it, so keyboard selection looks like
    // hover selection rather than nothing happening.
    const element = instance.getElementById(String(node.id));
    instance.elements().removeClass("hl fade hover");
    instance.elements().addClass("fade");
    element.closedNeighborhood().removeClass("fade").addClass("hl");
    element.addClass("hover");
    instance.animate({ center: { eles: element } }, { duration: 200 });
  };

  return (
    <div
      ref={container}
      // `application` rather than `img`: arrow keys mean "move the selection"
      // here, so a screen reader has to stop intercepting them.
      role="application"
      aria-label={`Citation graph, ${nodes.length} papers. Use arrow keys to move between papers.`}
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === "ArrowDown" || event.key === "ArrowRight") {
          event.preventDefault();
          step(1);
        } else if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
          event.preventDefault();
          step(-1);
        } else if (event.key === "Home") {
          event.preventDefault();
          selectedIdRef.current = null;
          step(1);
        } else if (event.key === "End") {
          event.preventDefault();
          selectedIdRef.current = null;
          step(-1);
        } else if (event.key === "Escape") {
          onSelectRef.current?.(null);
          cy.current?.elements().removeClass("hl fade hover");
        }
      }}
      style={{ width: "100%", height: "100%", outline: "none" }}
    />
  );
}

function runLayout(
  instance: cytoscape.Core,
  resetTo: Map<number, cytoscape.Position> | null,
  onFrame: () => void,
  onSettled: () => void,
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
      animationDuration: LAYOUT_ANIMATION_MS,
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
        // First framing only -- see `hasFittedRef`. A layout triggered by an
        // expansion must not yank the viewport away from wherever the user
        // has it.
        onFrame();
      },
      // fcose's animated path never emits `layoutstop`, so the animation's own
      // duration is what says when nodes stop moving and the float may take
      // over.
      stop: () => {
        onSettled();
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
  // `stop` is unreliable on fcose's animated path -- instrumenting it showed
  // layoutstart -> layoutready and nothing after -- so the animation duration
  // is the backstop that actually releases the float.
  window.setTimeout(onSettled, LAYOUT_ANIMATION_MS + 120);
  return layout;
}
