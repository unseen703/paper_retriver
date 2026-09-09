/**
 * What an expansion is doing right now (R2.9).
 *
 * BUILD.md: "poll 1s, show stage + counts", verified by "progress advances
 * visibly through stages". The polling itself lives in `api.expandAndWait`;
 * this renders what each poll reports.
 *
 * **A stage bar, not a percentage.** The run has no total to be a fraction of
 * -- how many papers the pool will hold is not known until pooling finishes,
 * which is itself one of the stages. A progress bar that invents a denominator
 * is the kind that sits at 90% for a minute, and it teaches you to ignore it.
 * Naming the stage is both honest and more useful: "FETCHING, 12 API calls" is
 * something you can act on, where "40%" is not.
 *
 * **The counts appear as they become true.** Each is null until the stage that
 * produces it completes, and null renders as nothing rather than as a zero --
 * "0 pooled" and "not pooled yet" are different facts, and confusing them is
 * how a working run looks like a broken one.
 */
import type { JobStatus } from "../api/client";

/** In pipeline order, which is what makes the row read as progress. */
const STAGES = ["QUEUED", "FETCHING", "POOLING", "RANKING", "ADDING", "DONE"] as const;

/** Plain-language labels. `stage` is the server's vocabulary, not the user's. */
const LABEL: Record<string, string> = {
  QUEUED: "queued",
  FETCHING: "fetching citations",
  POOLING: "finding candidates",
  RANKING: "ranking",
  ADDING: "adding to graph",
  DONE: "done",
  FAILED: "failed",
  CANCELLED: "cancelled",
};

export interface ExpansionProgressProps {
  status: JobStatus | null;
}

export function ExpansionProgress({ status }: ExpansionProgressProps) {
  if (!status) return null;

  const reached = STAGES.indexOf(status.stage as (typeof STAGES)[number]);
  const failed = status.status === "FAILED" || status.status === "CANCELLED";
  const { progress } = status;

  // Only counters that have a value. See the note above on null vs zero.
  const counts: string[] = [];
  if (progress.api_calls != null) counts.push(`${progress.api_calls} API calls`);
  if (progress.filtered != null) counts.push(`${progress.filtered} boundary`);
  if (progress.pool != null) counts.push(`${progress.pool} pooled`);
  if (progress.added != null) counts.push(`${progress.added} added`);

  return (
    <div
      // `status` rather than `alert`: this updates about once a second, and a
      // live region that interrupts on every poll is unusable with a screen
      // reader. `polite` lets it finish the current sentence first.
      role="status"
      aria-live="polite"
      style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 12 }}
    >
      <span aria-hidden style={{ display: "flex", gap: 3 }}>
        {STAGES.map((stage, index) => (
          <span
            key={stage}
            title={LABEL[stage]}
            style={{
              width: 14,
              height: 4,
              borderRadius: 2,
              background: failed
                ? "var(--danger, #e53e3e)"
                : index <= reached
                  ? "var(--accent, #4299e1)"
                  : "var(--track, #4a5568)",
              // A stage that has not been reached is present but unlit, so the
              // row does not change width as the run advances -- a bar that
              // grows a segment at a time reads as motion the run did not make.
              opacity: index <= reached ? 1 : 0.35,
            }}
          />
        ))}
      </span>

      <span style={{ color: failed ? "var(--danger, #e53e3e)" : "var(--dim)" }}>
        {LABEL[status.stage] ?? status.stage}
        {counts.length > 0 && ` · ${counts.join(" · ")}`}
      </span>
    </div>
  );
}

export { LABEL as STAGE_LABELS, STAGES };
