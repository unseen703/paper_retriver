import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { API_BASE, api } from "./api/client";
import { useView } from "./store/view";
import { GraphCanvas } from "./components/GraphCanvas";
import { FIXTURE_EDGES, FIXTURE_NODES } from "./components/fixture";

/**
 * R1.21's shell: the canvas, plus enough chrome to tell whether the backend is
 * alive. The controls and the inspector arrive at R1.22.
 *
 * The fixture toggle is BUILD.md's verification and stays after this task.
 * LIKED and DISLIKED cannot occur in a live graph until R2 ships labelling, so
 * a live graph exercises three of the five node styles and proves nothing
 * about the other two.
 */
export default function App() {
  const sessionId = useView((s) => s.sessionId);
  const selectedId = useView((s) => s.selectedId);
  const setSelected = useView((s) => s.setSelected);
  const [showFixture, setShowFixture] = useState(false);

  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const graph = useQuery({
    queryKey: ["graph", sessionId],
    queryFn: () => api.graph(sessionId),
    enabled: !showFixture,
  });

  const nodes = showFixture ? FIXTURE_NODES : (graph.data?.nodes ?? []);
  const edges = showFixture ? FIXTURE_EDGES : (graph.data?.edges ?? []);
  const selected = nodes.find((n) => n.id === selectedId);

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateRows: "auto 1fr auto" }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 16,
          padding: "10px 16px",
          borderBottom: "1px solid var(--border)",
          background: "var(--panel)",
        }}
      >
        <strong style={{ fontSize: 14 }}>Citation graph</strong>
        <span style={{ color: "var(--muted)" }}>
          {showFixture
            ? `${FIXTURE_NODES.length} fixture nodes`
            : graph.data
              ? `${graph.data.meta.node_count} nodes · ${graph.data.meta.edge_count} edges · ` +
                `${Math.round(graph.data.meta.crawl_completeness * 100)}% crawled`
              : "…"}
        </span>
        <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 12 }}>
          {health.isError && (
            <span style={{ color: "salmon" }} title={API_BASE}>
              backend unreachable
            </span>
          )}
          {health.data && (
            <span style={{ color: "var(--muted)" }}>
              db {health.data.db} · s2 {health.data.s2_reachable}
            </span>
          )}
          <button onClick={() => setShowFixture((v) => !v)}>
            {showFixture ? "Show my graph" : "Show style fixture"}
          </button>
        </span>
      </header>

      <main style={{ position: "relative", minHeight: 0 }}>
        {!showFixture && graph.isPending && (
          <p style={{ position: "absolute", inset: "16px auto auto 16px", color: "var(--muted)" }}>
            Loading the graph…
          </p>
        )}
        {!showFixture && graph.isError && (
          <p style={{ position: "absolute", inset: "16px auto auto 16px", color: "salmon" }}>
            {/* Name the URL actually used, not the default. A hard-coded
                "port 8000" sent debugging to the right port and the wrong
                problem when VITE_API_BASE pointed somewhere else. */}
            Could not reach the backend at <code>{API_BASE}</code>. Start it, or check
            <code>frontend/.env.local</code> if that URL looks wrong.
          </p>
        )}
        {nodes.length === 0 && !graph.isPending && !graph.isError && !showFixture && (
          <p style={{ position: "absolute", inset: "16px auto auto 16px", color: "var(--muted)" }}>
            No papers yet. R1.22 adds the dialog; until then use the CLI:{" "}
            <code>uv run python -m app.cli seed "…" --expand</code>
          </p>
        )}
        <GraphCanvas nodes={nodes} edges={edges} onSelect={setSelected} />
      </main>

      <footer
        style={{
          padding: "8px 16px",
          borderTop: "1px solid var(--border)",
          background: "var(--panel)",
          color: "var(--muted)",
          minHeight: 34,
        }}
      >
        {selected ? (
          <span>
            <strong style={{ color: "var(--text)" }}>{selected.title}</strong>
            {selected.year ? ` · ${selected.year}` : ""} · {selected.state} ·{" "}
            {selected.citation_count?.toLocaleString() ?? 0} citations · in {selected.in_degree} /
            out {selected.out_degree}
          </span>
        ) : (
          <span>Click a node. The full inspector arrives at R1.22.</span>
        )}
      </footer>
    </div>
  );
}
