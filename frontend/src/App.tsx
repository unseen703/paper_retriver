import { useQuery } from "@tanstack/react-query";
import { api } from "./api/client";
import { useView } from "./store/view";

/**
 * R1.20's shell. The canvas arrives at R1.21 and the controls at R1.22.
 *
 * It shows health rather than a placeholder because the first thing that goes
 * wrong in this project is the backend not running, and a blank page cannot
 * tell you that.
 */
export default function App() {
  const sessionId = useView((s) => s.sessionId);
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const graph = useQuery({
    queryKey: ["graph", sessionId],
    queryFn: () => api.graph(sessionId),
  });

  return (
    <div style={{ padding: 24, display: "grid", gap: 16 }}>
      <h1 style={{ margin: 0, fontSize: 18 }}>Citation graph</h1>

      <section
        style={{
          background: "var(--panel)",
          border: "1px solid var(--border)",
          borderRadius: 8,
          padding: 16,
        }}
      >
        {health.isPending && <p style={{ margin: 0 }}>Contacting the backend…</p>}
        {health.isError && (
          <p style={{ margin: 0, color: "salmon" }}>
            Backend unreachable. Start it with{" "}
            <code>uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --app-dir backend</code>
          </p>
        )}
        {health.data && (
          <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 16px" }}>
            <dt style={{ color: "var(--muted)" }}>database</dt>
            <dd style={{ margin: 0 }}>{health.data.db}</dd>
            <dt style={{ color: "var(--muted)" }}>semantic scholar</dt>
            <dd style={{ margin: 0 }}>{health.data.s2_reachable}</dd>
            <dt style={{ color: "var(--muted)" }}>cached responses</dt>
            <dd style={{ margin: 0 }}>{health.data.cache_rows}</dd>
            <dt style={{ color: "var(--muted)" }}>nodes</dt>
            <dd style={{ margin: 0 }}>{health.data.node_count}</dd>
            <dt style={{ color: "var(--muted)" }}>config</dt>
            <dd style={{ margin: 0 }}>{health.data.config_version}</dd>
          </dl>
        )}
      </section>

      {graph.data && (
        <p style={{ margin: 0, color: "var(--muted)" }}>
          {graph.data.meta.node_count} nodes, {graph.data.meta.edge_count} edges, crawl
          completeness {Math.round(graph.data.meta.crawl_completeness * 100)}%. The canvas
          arrives at R1.21.
        </p>
      )}
    </div>
  );
}
