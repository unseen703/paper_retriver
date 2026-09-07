import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { API_BASE, api } from "./api/client";
import { useView } from "./store/view";
import { GraphCanvas } from "./components/GraphCanvas";
import { FIXTURE_EDGES, FIXTURE_NODES } from "./components/fixture";

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

  const detail = useQuery({
    queryKey: ["node", sessionId, selectedId],
    queryFn: () => api.node(sessionId, selectedId as number),
    enabled: !showFixture && selectedId != null,
  });

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateRows: "auto 1fr auto" }}>
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
          <button onClick={() => setRelayoutToken((t) => t + 1)} title="Re-run the layout">
            Tidy
          </button>
          <button onClick={() => setShowFixture((v) => !v)}>
            {showFixture ? "My graph" : "Style fixture"}
          </button>
        </span>
      </header>

      <main style={{ position: "relative", minHeight: 0 }}>
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
          onSelect={setSelected}
          onHover={setHoveredId}
          relayoutToken={relayoutToken}
        />

        <p style={hintStyle}>drag to move · scroll to zoom · hover to trace · click to inspect</p>
      </main>

      <footer style={footerStyle}>
        {shown ? (
          <span>
            <strong style={{ color: "var(--text)" }}>{shown.title}</strong>
            {shown.year ? ` · ${shown.year}` : ""} · {shown.state} ·{" "}
            {(shown.citation_count ?? 0).toLocaleString()} citations · in {shown.in_degree} / out{" "}
            {shown.out_degree}
            {shown.score != null ? ` · score ${shown.score.toFixed(2)}` : ""}
            {/* Only when the inspector's answer is about the node on screen:
                the query is keyed to the *selection*, so a hovered node would
                otherwise borrow a different paper's byline. `authors` is
                optional in the generated type because the schema gives it a
                default. */}
            {detail.data?.paper_id === shown.id && (detail.data?.authors?.length ?? 0) > 0 && (
              <span style={{ color: "var(--muted)" }}>
                {" "}
                · {detail.data!.authors!.slice(0, 3).join(", ")}
                {detail.data!.authors!.length > 3 ? " et al." : ""}
              </span>
            )}
          </span>
        ) : (
          <span>Hover a node to read it. Click to pin it here. Full inspector at R1.22.</span>
        )}
      </footer>
    </div>
  );
}

const SWATCHES: [string, string][] = [
  ["#4299e1", "seed"],
  ["#d69e2e", "candidate"],
  ["#48bb78", "liked"],
  ["#fc8181", "disliked"],
];

function Legend() {
  return (
    <span style={{ display: "flex", gap: 12, alignItems: "center", fontSize: 12 }}>
      {SWATCHES.map(([color, label]) => (
        <span key={label} style={{ display: "flex", alignItems: "center", gap: 5 }}>
          <span
            style={{ width: 9, height: 9, borderRadius: "50%", background: color, flexShrink: 0 }}
          />
          <span style={{ color: "var(--muted)" }}>{label}</span>
        </span>
      ))}
      <span style={{ color: "var(--muted)" }}>· size = citations</span>
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
  color: "var(--muted)",
  opacity: 0.55,
  pointerEvents: "none",
};
