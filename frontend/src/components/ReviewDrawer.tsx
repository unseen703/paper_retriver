/**
 * `<ReviewDrawer>` (R2.14) — what the graph is not showing you, and why.
 *
 * PLAN.md M5: "A filter you cannot audit is a filter you cannot tune, and you
 * will silently discard good papers for weeks without noticing."
 *
 * **Three tabs, because there are three different kinds of absence.**
 * Quarantined is "probably applied ML but I'm not sure" — PLAN.md notes those
 * are most of the hard cases, which makes them the papers most worth a glance
 * and the worst ones to bury under a pile of confident rejections. Rejected is
 * the cascade saying no. Removed is *you* saying no, or the sweep drawing a
 * consequence from it.
 *
 * **The reason breakdown sits above the rows, not beside them.** The question
 * this drawer answers is "what is my filter throwing away", and the answer is
 * a distribution — one reason accounting for 400 papers is the finding. The
 * individual rows are how you check whether that reason is doing the right
 * thing.
 *
 * **Only Removed offers Restore.** Restoring a tombstoned paper is one call
 * that puts it back as a CANDIDATE. Un-rejecting a filtered paper is a
 * different act — it means overriding the cascade, which `POST /nodes` already
 * does with `force`, and it needs the s2 id rather than the local one. Rather
 * than half-implement it, the filter tabs explain the reason and leave the
 * override to the Add-paper dialog, which is built for it.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ReviewBucketOut } from "../api/client";

type TabKey = "quarantined" | "rejected" | "removed";

const TABS: { key: TabKey; label: string; hint: string }[] = [
  {
    key: "quarantined",
    label: "Quarantined",
    hint: "Held for review — the filter was not confident either way.",
  },
  { key: "rejected", label: "Rejected", hint: "The filter cascade refused these, with a reason." },
  { key: "removed", label: "Removed", hint: "You removed these, or the sweep collected them." },
];

export interface ReviewDrawerProps {
  sessionId: number;
  onClose: () => void;
  /** Focus a restored paper on the canvas, so Restore is visibly not a no-op. */
  onSelect?: (paperId: number) => void;
}

function Bucket({
  bucket,
  canRestore,
  onRestore,
  restoringId,
}: {
  bucket: ReviewBucketOut;
  canRestore: boolean;
  onRestore: (paperId: number) => void;
  restoringId: number | null;
}) {
  if (bucket.total === 0) {
    return (
      <p style={{ color: "var(--dim)", fontSize: 12, margin: "10px 0" }}>
        Nothing here — the filter has not set anything aside in this category.
      </p>
    );
  }

  return (
    <>
      {/* The distribution first: one reason accounting for most of the pile is
          the finding, and it is invisible if you only ever read rows. */}
      <ul style={{ listStyle: "none", padding: 0, margin: "8px 0 12px", fontSize: 12 }}>
        {bucket.by_reason.map((reason) => (
          <li
            key={reason.reason_code}
            style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "2px 0" }}
          >
            <code style={{ color: "var(--muted)" }}>{reason.reason_code}</code>
            <span style={{ fontVariantNumeric: "tabular-nums" }}>{reason.count}</span>
          </li>
        ))}
      </ul>

      <ul style={{ listStyle: "none", padding: 0, margin: 0, fontSize: 12 }}>
        {bucket.papers.map((paper) => (
          <li
            key={paper.paper_id}
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 8,
              padding: "4px 0",
              borderTop: "1px solid var(--line, #2d3748)",
            }}
          >
            <span style={{ flex: 1 }}>
              {paper.title}
              {paper.year ? <span style={{ color: "var(--dim)" }}> · {paper.year}</span> : null}
              <br />
              <code style={{ color: "var(--dim)", fontSize: 11 }}>
                {paper.reason_code} · {paper.stage}
              </code>
            </span>
            {canRestore && (
              <button
                type="button"
                onClick={() => onRestore(paper.paper_id)}
                disabled={restoringId === paper.paper_id}
              >
                {restoringId === paper.paper_id ? "…" : "Restore"}
              </button>
            )}
          </li>
        ))}
      </ul>

      {bucket.truncated && (
        <p style={{ color: "var(--dim)", fontSize: 11, margin: "8px 0 0" }}>
          Showing {bucket.papers.length} of {bucket.total}. The counts above are complete.
        </p>
      )}
    </>
  );
}

export function ReviewDrawer({ sessionId, onClose, onSelect }: ReviewDrawerProps) {
  const [tab, setTab] = useState<TabKey>("rejected");
  const [restoringId, setRestoringId] = useState<number | null>(null);
  const queryClient = useQueryClient();

  const review = useQuery({
    queryKey: ["review", sessionId],
    queryFn: () => api.review(sessionId),
  });

  const restore = useMutation({
    mutationFn: (paperId: number) => api.restore(sessionId, paperId),
    onSuccess: (node) => {
      // The paper is back in the graph, so three things are now stale: the
      // graph itself, the counts, and this drawer.
      queryClient.invalidateQueries({ queryKey: ["graph", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["stats", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["review", sessionId] });
      onSelect?.(node.paper_id);
    },
    onSettled: () => setRestoringId(null),
  });

  const current: ReviewBucketOut | undefined = review.data?.[tab];

  return (
    <aside
      role="dialog"
      aria-label="Review drawer"
      style={{
        position: "absolute",
        top: 0,
        right: 0,
        bottom: 0,
        width: 380,
        overflowY: "auto",
        padding: 14,
        background: "var(--panel, #1a202c)",
        borderLeft: "1px solid var(--line, #2d3748)",
        zIndex: 5,
      }}
    >
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h2 style={{ margin: 0, fontSize: 13 }}>Review</h2>
        <button type="button" onClick={onClose} aria-label="Close review drawer">
          ×
        </button>
      </header>

      <div role="tablist" style={{ display: "flex", gap: 4, margin: "10px 0 4px" }}>
        {TABS.map(({ key, label, hint }) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            title={hint}
            onClick={() => setTab(key)}
            style={{ fontWeight: tab === key ? 600 : 400 }}
          >
            {/* The count lives in the tab, so you can see where the papers went
                without opening each one. */}
            {label} ({review.data ? review.data[key].total : "…"})
          </button>
        ))}
      </div>

      <p style={{ color: "var(--dim)", fontSize: 11, margin: "0 0 4px" }}>
        {TABS.find((t) => t.key === tab)?.hint}
      </p>

      {review.isError && <p style={{ color: "salmon", fontSize: 12 }}>Could not load the review.</p>}
      {!review.data && !review.isError && (
        <p style={{ color: "var(--dim)", fontSize: 12 }}>Loading…</p>
      )}

      {current && (
        <Bucket
          bucket={current}
          // Only a tombstone can be lifted by Restore. See the module note.
          canRestore={tab === "removed"}
          restoringId={restoringId}
          onRestore={(paperId) => {
            setRestoringId(paperId);
            restore.mutate(paperId);
          }}
        />
      )}

      {restore.isError && (
        <p style={{ color: "salmon", fontSize: 12 }}>That paper could not be restored.</p>
      )}
    </aside>
  );
}
