import { useCallback, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { API_BASE, api } from "./api/client";
import { useView } from "./store/view";
import { GraphCanvas } from "./components/GraphCanvas";
import { FIXTURE_EDGES, FIXTURE_NODES } from "./components/fixture";
import { AddPaperDialog } from "./components/AddPaperDialog";
import { ExpansionControls } from "./components/ExpansionControls";
import { NodeInspector, type Neighbour } from "./components/NodeInspector";
import { ViewControls } from "./components/ViewControls";
import { StatsPanel } from "./components/StatsPanel";
import { ReviewDrawer } from "./components/ReviewDrawer";
import { CandidateList } from "./components/CandidateList";
import { ClearGraphDialog } from "./components/ClearGraphDialog";
import { SessionSwitcher } from "./components/SessionSwitcher";
import type { LabelMode } from "./components/stylesheet";
import {
  clampThreshold,
  type SavedPosition,
  type TopicGroup,
} from "./components/graphInteraction";

/**
 * The shell around the canvas: a legend, a couple of view controls, and a
 * readout for whatever the pointer is on.
 *
 * The readout follows *hover*, falling back to the current selection. That is
 * the prototype's behaviour and it is the reason its canvas worked without
 * labels on every node -- you sweep the pointer over the graph and read titles
 * in one fixed place, rather than clicking each ball to find out what it is.
 *
 * The fixture toggle is BUILD.md's R1.21 verification and stays after the
 * task: LIKED and DISLIKED cannot occur in a live graph until R2 ships
 * labelling, so a live graph exercises three of the five node styles and
 * proves nothing about the other two.
 */
export default function App() {
  const sessionId = useView((s) => s.sessionId);
  const selectedId = useView((s) => s.selectedId);
  const setSelected = useView((s) => s.setSelected);
  const setSession = useView((s) => s.setSession);

  const [showFixture, setShowFixture] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  // Open by default. PLAN.md section F: "`<CandidateList>` is the product […]
  // Don't let the graph eat all your UI effort." A panel you have to discover
  // is not where most decisions will get made.
  const [listOpen, setListOpen] = useState(true);
  const [clearOpen, setClearOpen] = useState(false);
  const [hoveredId, setHoveredId] = useState<number | null>(null);
  const [relayoutToken, setRelayoutToken] = useState(0);
  const [resetViewToken, setResetViewToken] = useState(0);
  const [labelMode, setLabelMode] = useState<LabelMode>("relevant");
  const [scoreThreshold, setScoreThreshold] = useState<number | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [topicGroup, setTopicGroup] = useState<TopicGroup>("all");
  const [topicCount, setTopicCount] = useState(0);
  const [visibleCount, setVisibleCount] = useState({ visible: 0, total: 0 });
  const [matchCount, setMatchCount] = useState(0);

  // Stable identities: the canvas calls these from effects, so a new function
  // each render would re-run the score filter and the search on every render.
  const handleVisible = useCallback(
    (visible: number, total: number) => setVisibleCount({ visible, total }),
    [],
  );
  const handleMatches = useCallback((matches: number) => setMatchCount(matches), []);
  const handleTopicCount = useCallback((matches: number) => setTopicCount(matches), []);

  /**
   * Persist the arrangement whenever the canvas says it settled (R2.12).
   *
   * Fire-and-forget on purpose. A failed save costs the user nothing they can
   * see right now -- the graph on screen is unchanged, and the next settle
   * tries again -- so surfacing it as an error banner would interrupt them
   * about something they cannot act on. It is logged, so it is not silent.
   *
   * The fixture graph is skipped: its ids are invented, and saving them would
   * write demo coordinates over the real session's arrangement.
   */
  const handlePositions = useCallback(
    (positions: SavedPosition[]) => {
      if (showFixture || positions.length === 0) return;
      api
        .savePositions(sessionId, positions)
        .catch((error) => console.warn("could not save layout positions", error));
    },
    [sessionId, showFixture],
  );

  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const graph = useQuery({
    queryKey: ["graph", sessionId],
    queryFn: () => api.graph(sessionId),
    enabled: !showFixture,
  });

  const nodes = showFixture ? FIXTURE_NODES : (graph.data?.nodes ?? []);
  const edges = showFixture ? FIXTURE_EDGES : (graph.data?.edges ?? []);

  // Hover wins over selection: the pointer is a more immediate intent than a
  // click made twenty seconds ago.
  const shown = useMemo(
    () => nodes.find((n) => n.id === (hoveredId ?? selectedId)),
    [nodes, hoveredId, selectedId],
  );

  // Built from the edges already on screen rather than fetched: the graph
  // response carries every edge between drawn nodes, so a request would ask
  // the server for something the client already has.
  // The candidates' actual score span. Seeds are excluded: they have no score,
  // and including their absent one as a zero would stretch the slider back
  // over the empty range the bounding is meant to remove.
  const scoreRange = useMemo(() => {
    const scores = nodes
      .filter((n) => n.state === "CANDIDATE" && typeof n.score === "number")
      .map((n) => n.score as number);
    if (scores.length === 0) return { min: 0, max: 0 };
    return { min: Math.min(...scores), max: Math.max(...scores) };
  }, [nodes]);

  // Clamped into whatever range the graph currently has. `null` means the
  // user has not chosen yet and resolves to the floor. Without the clamp a
  // threshold left over from a differently-scored graph could sit above the
  // new maximum: the input clamps its thumb but not its value, so the canvas
  // went empty while the slider looked like it was merely at maximum.
  const effectiveThreshold = clampThreshold(scoreThreshold, scoreRange);

  const neighbours = useMemo<Neighbour[]>(() => {
    if (selectedId == null) return [];
    const titles = new Map(nodes.map((n) => [n.id, n.title]));
    const out: Neighbour[] = [];
    for (const edge of edges) {
      if (edge.source === selectedId && titles.has(edge.target)) {
        out.push({ id: edge.target, title: titles.get(edge.target)!, direction: "cites" });
      } else if (edge.target === selectedId && titles.has(edge.source)) {
        out.push({ id: edge.source, title: titles.get(edge.source)!, direction: "cited by" });
      }
    }
    // Stable order, and outgoing first: a paper's own bibliography is the more
    // deliberate list, its citations the more arbitrary one.
    return out.sort(
      (a, b) => a.direction.localeCompare(b.direction) || a.title.localeCompare(b.title),
    );
  }, [selectedId, nodes, edges]);

  return (
    <div
      style={{
        height: "100%",
        display: "grid",
        gridTemplateRows: showFixture ? "auto 1fr auto" : "auto auto 1fr auto",
        // `minmax(0, 1fr)`, not the implicit `auto`. A grid column sized by
        // content lets its children push past the viewport, so opening the
        // inspector rendered it at x=1280 -- entirely off the right edge --
        // instead of narrowing the canvas beside it. The canvas is a flex
        // child with `flex: 1` and `minWidth: 0`, which can only shrink if the
        // column it sits in is itself bounded.
        gridTemplateColumns: "minmax(0, 1fr)",
      }}
    >
      <header style={headerStyle}>
        <strong style={{ fontSize: 14 }}>Citation graph</strong>
        <span style={{ color: "var(--muted)" }}>
          {showFixture
            ? `${FIXTURE_NODES.length} fixture nodes`
            : graph.data
              ? `${graph.data.meta.node_count} nodes · ${graph.data.meta.edge_count} edges · ` +
                `${Math.round(graph.data.meta.crawl_completeness * 100)}% crawled`
              : "…"}
        </span>

        <Legend />

        <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
          {health.isError && (
            <span style={{ color: "salmon" }} title={API_BASE}>
              backend unreachable
            </span>
          )}
          {health.data && (
            <span style={{ color: "var(--muted)", fontSize: 12 }}>
              db {health.data.db} · s2 {health.data.s2_reachable}
            </span>
          )}
          <button onClick={() => setDialogOpen(true)} disabled={showFixture}>
            + Add paper
          </button>
          {/* R2.16. First in the toolbar: everything to its right operates on
              whichever session this names. */}
          <SessionSwitcher sessionId={sessionId} onSwitch={setSession} />
          <ExpansionControls sessionId={sessionId} disabled={showFixture} />
          {/* R2.14. The drawer is where a filter stops being a black box, so
              it needs to be one click away rather than buried. */}
          {/* R2.15. Destructive, so it sits after the constructive controls
              and opens a dialog rather than acting on the click. */}
          <button
            type="button"
            onClick={() => setClearOpen(true)}
            disabled={showFixture || nodes.length === 0}
            title="Empty this graph — the papers already fetched are kept"
          >
            Clear
          </button>
          <button
            type="button"
            onClick={() => setListOpen((was) => !was)}
            disabled={showFixture}
            aria-expanded={listOpen}
            title="The ranked candidate table — where most decisions get made"
          >
            Ranked
          </button>
          <button
            type="button"
            onClick={() => setReviewOpen((was) => !was)}
            disabled={showFixture}
            aria-expanded={reviewOpen}
            title="What the filters set aside, and what you removed"
          >
            Review
          </button>
          <button onClick={() => setRelayoutToken((t) => t + 1)} title="Re-run the layout">
            Tidy
          </button>
          <button onClick={() => setShowFixture((v) => !v)}>
            {showFixture ? "My graph" : "Style fixture"}
          </button>
        </span>
      </header>

      {!showFixture && (
        <ViewControls
          labelMode={labelMode}
          onLabelMode={setLabelMode}
          scoreThreshold={effectiveThreshold}
          onScoreThreshold={setScoreThreshold}
          scoreRange={scoreRange}
          searchQuery={searchQuery}
          onSearchQuery={setSearchQuery}
          topicGroup={topicGroup}
          onTopicGroup={setTopicGroup}
          topicCount={topicCount}
          onResetView={() => setResetViewToken((t) => t + 1)}
          visible={visibleCount.visible}
          total={visibleCount.total}
          matches={matchCount}
        />
      )}

      <main style={{ display: "flex", minHeight: 0, minWidth: 0 }}>
        {/* Left of the canvas, because the inspector opens on the right and
            the two are read together: pick a row here, read the detail there.
            A fixed width rather than a flex share — a ranked table is a column
            of known shape, and letting it grow with the window would stretch
            the title column past the point where the eye can scan it. */}
        {!showFixture && listOpen && (
          <aside
            aria-label="Ranked candidates"
            style={{
              width: 400,
              flexShrink: 0,
              display: "flex",
              flexDirection: "column",
              minHeight: 0,
              borderRight: "1px solid #333",
            }}
          >
            <CandidateList
              sessionId={sessionId}
              selectedId={selectedId}
              onSelect={setSelected}
              disabled={showFixture}
            />
          </aside>
        )}

        <div style={{ position: "relative", flex: 1, minWidth: 0 }}>
        {!showFixture && graph.isPending && <Overlay>Loading the graph…</Overlay>}
        {!showFixture && graph.isError && (
          <Overlay tone="error">
            Could not reach the backend at <code>{API_BASE}</code>. Start it, or check{" "}
            <code>frontend/.env.local</code> if that URL looks wrong.
          </Overlay>
        )}
        {!showFixture && !graph.isPending && !graph.isError && nodes.length === 0 && (
          <Overlay>
            No papers yet. R1.22 adds the dialog; until then use the CLI:{" "}
            <code>uv run python -m app.cli seed "…" --expand</code>
          </Overlay>
        )}

        <GraphCanvas
          nodes={nodes}
          edges={edges}
          selectedId={selectedId}
          onSelect={setSelected}
          onHover={setHoveredId}
          relayoutToken={relayoutToken}
          resetViewToken={resetViewToken}
          labelMode={labelMode}
          scoreThreshold={effectiveThreshold}
          searchQuery={searchQuery}
          topicGroup={topicGroup}
          onTopicCount={handleTopicCount}
          onVisibleCount={handleVisible}
          onMatchCount={handleMatches}
          onPositions={handlePositions}
        />

          <p style={hintStyle}>drag to move · scroll to zoom · hover to trace · click to inspect</p>

          {/* R2.13. Bottom-left, opposite the hint and clear of the inspector,
              which opens on the right. It is a reference readout rather than a
              control, so it sits out of the way rather than in the toolbar. */}
          <div style={statsAnchorStyle}>
            <StatsPanel sessionId={sessionId} disabled={showFixture} />
          </div>
        </div>

        {/* The inspector is the detail query's only consumer now, so selecting
            a node fetches once and renders everything rather than fetching to
            show three author names in the footer. */}
        {!showFixture && clearOpen && (
          <ClearGraphDialog
            sessionId={sessionId}
            nodeCount={nodes.length}
            onClose={() => setClearOpen(false)}
            // The selection is an id into a graph that no longer exists.
            onCleared={() => setSelected(null)}
          />
        )}

        {!showFixture && reviewOpen && (
          <ReviewDrawer
            sessionId={sessionId}
            onClose={() => setReviewOpen(false)}
            // Select the restored paper so the click is visibly not a no-op:
            // the graph refetches and the node it brought back is highlighted.
            onSelect={setSelected}
          />
        )}

        {!showFixture && selectedId != null && (
          <NodeInspector
            sessionId={sessionId}
            paperId={selectedId}
            neighbours={neighbours}
            onSelect={setSelected}
            onClose={() => setSelected(null)}
          />
        )}
      </main>

      <footer style={footerStyle}>
        {shown ? (
          // The line is nowrap-ellipsised, so on a narrow window the title is
          // cut and the year, state, citations and degree after it are pushed
          // off entirely. The title attribute is what makes that recoverable.
          <span title={shown.title}>
            <strong style={{ color: "var(--text)" }}>{shown.title}</strong>
            {shown.year ? ` · ${shown.year}` : ""} · {shown.state} ·{" "}
            {(shown.citation_count ?? 0).toLocaleString()} citations · in {shown.in_degree} / out{" "}
            {shown.out_degree}
            {shown.score != null ? ` · score ${shown.score.toFixed(2)}` : ""}
          </span>
        ) : (
          <span>Hover a node to read it. Click to pin it here. Full inspector at R1.22.</span>
        )}
      </footer>

      {dialogOpen && (
        <AddPaperDialog sessionId={sessionId} onClose={() => setDialogOpen(false)} />
      )}
    </div>
  );
}

// The prototype's four, in its order. `candidate` is grey rather than amber:
// running the archived UI showed unlabelled papers drawn flat #a0aec0, with
// no score ramp anywhere.
// `reachable` is false for states no code path can produce yet. Showing them
// at full strength invites the reader to hunt for green and red nodes and
// conclude the graph is broken when the truth is that labelling ships at R2.
const SWATCHES: { color: string; label: string; reachable: boolean }[] = [
  { color: "#4299e1", label: "seed", reachable: true },
  { color: "#a0aec0", label: "candidate", reachable: true },
  { color: "#48bb78", label: "liked", reachable: false },
  { color: "#fc8181", label: "disliked", reachable: false },
];

function Legend() {
  return (
    <span style={{ display: "flex", gap: 12, alignItems: "center", fontSize: 12 }}>
      {SWATCHES.map(({ color, label, reachable }) => (
        <span
          key={label}
          style={{ display: "flex", alignItems: "center", gap: 5, opacity: reachable ? 1 : 0.4 }}
          title={reachable ? undefined : "Labelling arrives at R2"}
        >
          <span
            style={{ width: 9, height: 9, borderRadius: "50%", background: color, flexShrink: 0 }}
          />
          <span style={{ color: "var(--muted)" }}>{label}</span>
        </span>
      ))}
      <span style={{ color: "var(--muted)" }}>· size = citations</span>
      {/* The year ramp needs saying, or a viewer reads the lighter candidates
          as "more relevant" -- the meaning the amber ramp used to carry. */}
      <span style={{ display: "flex", alignItems: "center", gap: 5 }}>
        <span style={{ color: "var(--muted)" }}>· shade =</span>
        <span
          style={{
            width: 34,
            height: 8,
            borderRadius: 4,
            background: "linear-gradient(90deg, #6b7a90, #dfe6ef)",
            flexShrink: 0,
          }}
        />
        <span style={{ color: "var(--muted)" }}>older → newer</span>
      </span>
    </span>
  );
}

function Overlay({ children, tone }: { children: React.ReactNode; tone?: "error" }) {
  return (
    <p
      style={{
        position: "absolute",
        top: 16,
        left: 16,
        margin: 0,
        zIndex: 2,
        color: tone === "error" ? "salmon" : "var(--muted)",
        pointerEvents: "none",
      }}
    >
      {children}
    </p>
  );
}

const headerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 16,
  padding: "10px 16px",
  borderBottom: "1px solid var(--border)",
  background: "var(--panel)",
};

const footerStyle: React.CSSProperties = {
  padding: "8px 16px",
  borderTop: "1px solid var(--border)",
  background: "var(--panel)",
  color: "var(--muted)",
  minHeight: 34,
  // A single line that never reflows: the readout changes on every hover, and
  // a wrapping footer would make the canvas jump as the pointer moves.
  whiteSpace: "nowrap",
  overflow: "hidden",
  textOverflow: "ellipsis",
};

const statsAnchorStyle: React.CSSProperties = {
  position: "absolute",
  left: 12,
  bottom: 34,
  padding: "8px 10px",
  borderRadius: 6,
  // Same translucent slate as the other floating chrome, so the panel reads as
  // part of the canvas furniture rather than a card dropped on top of it.
  background: "rgba(26, 32, 44, 0.82)",
  border: "1px solid var(--line, #2d3748)",
  pointerEvents: "auto",
};

const hintStyle: React.CSSProperties = {
  position: "absolute",
  bottom: 10,
  right: 14,
  margin: 0,
  fontSize: 11,
  // Full-strength --muted (7.28:1). It was --muted at 0.55 opacity, which
  // composites to #5f6774 and measures 3.31:1 -- below WCAG AA, and on the one
  // line that teaches the entire interaction model. Nothing else advertises
  // that hovering traces a neighbourhood, so a reader who cannot see this
  // never discovers it.
  color: "var(--muted)",
  pointerEvents: "none",
};
