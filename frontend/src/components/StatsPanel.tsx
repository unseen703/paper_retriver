/**
 * `<StatsPanel>` v1 (R2.13).
 *
 * BUILD.md: nodes, edges, per-state counts, components, avg degree, density,
 * `crawl_completeness`. No PageRank — that is R3, and the response has no such
 * field, so there is nothing here to leave conspicuously blank.
 *
 * **Every number comes from the server.** The canvas already holds a graph and
 * it would be less code to add it up here, but `GET /graph` can be filtered by
 * state — so a panel totalling what is loaded would confidently report a
 * subset as the whole. BUILD.md's verification is "counts match the DB", and
 * the only way to keep that true is to ask the DB.
 *
 * **Two numbers carry a caveat rather than a value.** `crawl_completeness`
 * below 0.6 is the threshold at which PLAN.md says PageRank stops meaning
 * anything, and a graph in many components is one where structural scores are
 * measuring several unrelated literatures at once. Both are called out where
 * they matter, because a number nobody knows how to read is decoration.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

/** PLAN.md section I: below this, R3 suppresses the PageRank column. */
const COMPLETENESS_FLOOR = 0.6;

/** In the order the state machine moves through, not alphabetical. */
const STATE_ORDER = ["SEED", "LIKED", "CANDIDATE", "DISLIKED"] as const;

export interface StatsPanelProps {
  sessionId: number;
  /** Skip fetching while the fixture graph is on screen — its ids are invented. */
  disabled?: boolean;
}

function Row({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div
      title={title}
      style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "1px 0" }}
    >
      <span style={{ color: "var(--dim)" }}>{label}</span>
      <span style={{ fontVariantNumeric: "tabular-nums" }}>{value}</span>
    </div>
  );
}

export function StatsPanel({ sessionId, disabled }: StatsPanelProps) {
  const stats = useQuery({
    queryKey: ["stats", sessionId],
    queryFn: () => api.stats(sessionId),
    enabled: !disabled,
  });

  if (disabled) return null;
  if (stats.isError) {
    return (
      <div role="status" style={{ fontSize: 12, color: "salmon" }}>
        Stats unavailable.
      </div>
    );
  }
  if (!stats.data) {
    return (
      <div role="status" style={{ fontSize: 12, color: "var(--dim)" }}>
        Counting…
      </div>
    );
  }

  const s = stats.data;
  const thin = s.crawl_completeness < COMPLETENESS_FLOOR;
  const fragmented = s.components > 1;

  return (
    <div role="region" aria-label="Graph statistics" style={{ fontSize: 12, minWidth: 190 }}>
      <Row label="nodes" value={String(s.node_count)} />
      <Row label="edges" value={String(s.edge_count)} title="Both endpoints inside this graph." />

      <div style={{ height: 6 }} />
      {STATE_ORDER.map((state) => (
        // Every state is rendered, including the zeroes: "nothing disliked
        // yet" and "this build forgot to count them" must look different.
        <Row key={state} label={state.toLowerCase()} value={String(s.by_state[state] ?? 0)} />
      ))}

      <div style={{ height: 6 }} />
      <Row
        label="components"
        value={String(s.components)}
        title="Connected pieces of the undirected projection."
      />
      <Row
        label="avg degree"
        value={s.avg_degree.toFixed(2)}
        title="2E/N — each edge counts at both of its endpoints."
      />
      <Row
        label="density"
        value={s.density.toFixed(4)}
        title="2E / (N(N−1)), against the undirected maximum."
      />
      <Row
        label="crawled"
        value={`${Math.round(s.crawl_completeness * 100)}%`}
        title="Papers whose full record was fetched, rather than a title-only stub."
      />

      {/* The caveats. A number nobody knows how to read is decoration, and
          these two are the ones that quietly invalidate R3's scores. */}
      {thin && (
        <p style={{ margin: "6px 0 0", color: "var(--warn, #d69e2e)", lineHeight: 1.35 }}>
          Under {COMPLETENESS_FLOOR * 100}% crawled — structural scores will be biased toward
          the papers already fetched.
        </p>
      )}
      {fragmented && (
        <p style={{ margin: "4px 0 0", color: "var(--dim)", lineHeight: 1.35 }}>
          {s.components} disconnected pieces — expansion has not linked them yet.
        </p>
      )}
    </div>
  );
}
