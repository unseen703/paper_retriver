/**
 * `<ExpansionControls>` (R1.22) -- run one hop and report what it did.
 *
 * The result line is the point of this component, not the button. Expansion is
 * synchronous at R1 and can run for tens of seconds spending real API budget,
 * so "it finished" is not a useful thing to say. Three separate outcomes need
 * distinguishing and all three look identical from a bare success:
 *
 *   added 18 of 20                     it worked
 *   added 0 of 20, pooled 34           it fetched, and the filters took
 *                                      everything -- a corpus problem
 *   added 0 of 20, pooled 0            nothing to fetch -- a seed problem
 *
 * Three of R1's bugs were found by reading numbers like these rather than by a
 * test, so the endpoint reports them and this renders them.
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, type ExpandResponse, type JobStatus } from "../api/client";
import { ExpansionProgress } from "./ExpansionProgress";

// The server enforces 1..100; matching it here means the spinner never starts
// on a request that is going to come back 422.
const MAX_NEW_LIMIT = 100;

export interface ExpansionControlsProps {
  sessionId: number;
  disabled?: boolean;
}

export function ExpansionControls({ sessionId, disabled }: ExpansionControlsProps) {
  const queryClient = useQueryClient();
  const [maxNew, setMaxNew] = useState(20);
  const [result, setResult] = useState<ExpandResponse | null>(null);

  // R2.4 made expansion a job: POST returns 202 with an id, and the result
  // arrives by polling. `expandAndWait` hides the loop, so this component
  // still reads as one call; R2.9 will surface the intermediate stages it
  // already receives through `onProgress`.
  // R2.9: every poll lands here, so the row below shows the run advancing
  // rather than only its beginning and its end.
  const [progress, setProgress] = useState<JobStatus | null>(null);

  const expand = useMutation({
    mutationFn: async () => {
      const status = await api.expandAndWait(sessionId, maxNew, setProgress);
      if (status.status === "CANCELLED") {
        // Not an error: the user asked for this. Reporting "the expansion did
        // not finish" for a deliberate stop would make the button look broken
        // at the exact moment it worked.
        return status.result ?? null;
      }
      if (status.status !== "DONE") {
        // A FAILED job is an error here even though the HTTP calls all
        // succeeded -- otherwise the button would report success for a run
        // that did nothing.
        throw new Error(status.error ?? "The expansion did not finish.");
      }
      return status.result ?? null;
    },
    onSuccess: (data) => {
      setResult(data);
      queryClient.invalidateQueries({ queryKey: ["graph", sessionId] });
      // R2.13: the panel counts the graph, so it is stale the moment the
      // graph changes. Refetched rather than adjusted locally -- the whole
      // point of the endpoint is that the server does the counting.
      queryClient.invalidateQueries({ queryKey: ["stats", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["health"] });
      // Flags on any open search list are stale the moment nodes are added.
      queryClient.invalidateQueries({ queryKey: ["search", sessionId] });
    },
    onError: () => setResult(null),
    // The stage row is about a run in flight. Leaving the last one on screen
    // after a failure would show "adding to graph" beside an error saying
    // nothing was added.
    onSettled: () => setProgress(null),
  });

  const errorMessage = (() => {
    const error = expand.error;
    if (!error) return null;
    if (error instanceof ApiError && error.status === 422) {
      // The graph is full. The detail names the ceiling, which is actionable.
      return String(error.detail ?? "Graph is at its node limit.");
    }
    if (error instanceof ApiError && error.status === 409) {
      // One expansion per session. The detail carries the running job's id.
      return "An expansion is already running for this session.";
    }
    if (error instanceof ApiError && error.status === 503) {
      return "Semantic Scholar is unavailable. Nothing was added.";
    }
    return "Expansion failed. Nothing was added.";
  })();

  return (
    <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
        <span style={{ color: "var(--dim)" }}>max new</span>
        <input
          type="number"
          min={1}
          max={MAX_NEW_LIMIT}
          value={maxNew}
          // Clamped on the way in rather than validated on submit: an input
          // that silently refuses is worse than one that corrects itself.
          onChange={(event) =>
            setMaxNew(Math.min(MAX_NEW_LIMIT, Math.max(1, Number(event.target.value) || 1)))
          }
          style={{ width: 58 }}
          aria-label="Maximum new papers to add"
        />
      </label>

      <button onClick={() => expand.mutate()} disabled={disabled || expand.isPending}>
        {expand.isPending ? "Expanding…" : "Expand"}
      </button>

      {/* R2.9 replaces the old static "this can take a minute" with what the
          run is actually doing. The fallback covers the gap between the click
          and the first poll, which would otherwise show nothing at all. */}
      {expand.isPending &&
        (progress ? (
          <ExpansionProgress
            status={progress}
            onCancel={() => {
              // Fire and forget: `expandAndWait` is still polling and will see
              // the CANCELLED status on its next tick, which is what actually
              // ends the run in this component. A 409 here means the job
              // finished first, and the poll is about to say so anyway.
              void api.cancelExpansion(sessionId, progress.job_id).catch(() => {});
            }}
          />
        ) : (
          <span style={{ fontSize: 12, color: "var(--dim)" }} role="status">
            queueing…
          </span>
        ))}

      {errorMessage && (
        <span style={{ fontSize: 12, color: "salmon" }} role="status">
          {errorMessage}
        </span>
      )}

      {result && !expand.isPending && !errorMessage && (
        <span style={{ fontSize: 12, color: "var(--muted)" }} role="status">
          added {result.n_added} of {maxNew}
          {/* Pool size separates "the filters rejected everything" from
              "there was nothing to fetch" -- the same result otherwise. */}
          {result.n_added === 0 ? ` · pooled ${result.n_pool}` : ""}
          {result.boundary > 0 ? ` · ${result.boundary} boundary` : ""}
          {result.api_calls > 0 ? ` · ${result.api_calls} API calls` : " · all cached"}
          {result.truncated ? ` · truncated` : ""}
          {result.error ? ` · ${result.error}` : ""}
        </span>
      )}
    </span>
  );
}
