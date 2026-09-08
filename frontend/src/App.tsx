import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { API_BASE, api } from "./api/client";
import { useView } from "./store/view";
import { GraphCanvas } from "./components/GraphCanvas";
import { FIXTURE_EDGES, FIXTURE_NODES } from "./components/fixture";
import { AddPaperDialog } from "./components/AddPaperDialog";
import { ExpansionControls } from "./components/ExpansionControls";
import { NodeInspector } from "./components/NodeInspector";

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

  const [showFixture, setShowFixture] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [hoveredId, setHoveredId] = useState<number | null>(null);
  const [relayoutToken, setRelayoutToken] = useState(0);

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

  return (
    <div
      style={{
        height: "100%",
        display: "grid",
        gridTemplateRows: "auto 1fr auto",
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
          <ExpansionControls sessionId={sessionId} disabled={showFixture} />
          <button onClick={() => setRelayoutToken((t) => t + 1)} title="Re-run the layout">
            Tidy
          </button>
          <button onClick={() => setShowFixture((v) => !v)}>
            {showFixture ? "My graph" : "Style fixture"}
          </button>
        </span>
      </header>

      <main style={{ display: "flex", minHeight: 0, minWidth: 0 }}>
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
        />

          <p style={hintStyle}>drag to move · scroll to zoom · hover to trace · click to inspect</p>
        </div>

        {/* The inspector is the detail query's only consumer now, so selecting
            a node fetches once and renders everything rather than fetching to
            show three author names in the footer. */}
        {!showFixture && selectedId != null && (
          <NodeInspector
            sessionId={sessionId}
            paperId={selectedId}
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
