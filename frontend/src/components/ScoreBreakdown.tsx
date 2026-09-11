/**
 * `<ScoreBreakdown>` (R3.c) — why this paper ranks where it does.
 *
 * PLAN.md §F: "the highest-signal-per-line-of-code component in the whole app.
 * It makes the recommender legible, it doubles as your debugging tool, and in a
 * portfolio review it demonstrates that you understand interpretability as an
 * engineering property rather than a buzzword."
 *
 * It replaces a `JSON.stringify` of the same object, which is the difference
 * between dumping the data and explaining it.
 *
 * **Three things here are not decoration.**
 *
 * *The total is shown.* BUILD.md requires the terms to sum to the score, and
 * printing the sum is what lets a reader check that rather than trust it.
 *
 * *Negative bars point the other way.* `hub` carries a negative weight — a
 * paper everything cites is a bad recommendation however good it looks — so
 * clamping or unsigning would hide the most interesting thing the breakdown
 * has to say.
 *
 * *A stored score that disagrees with its terms is called out.* `score` and
 * `score_breakdown` are persisted together and could be rescored apart; both
 * numbers look plausible alone, and the disagreement is the only symptom.
 */
import { toBars, sumOf } from "./scoreBars";

export interface ScoreBreakdownProps {
  breakdown: Record<string, number> | null | undefined;
  /** The score as stored, for the consistency check. */
  score: number | null | undefined;
}

/**
 * Tolerance for "the stored score matches its terms".
 *
 * Loose enough to ignore float dust — 0.1 + 0.2 is not 0.3 — and tight enough
 * that a real rescore mismatch cannot hide inside it. A warning that fires on
 * the last decimal place is a warning people learn to ignore.
 */
const EPSILON = 1e-6;

const POSITIVE = "var(--accent, #4299e1)";
const NEGATIVE = "var(--danger, #e53e3e)";

/** Half the width, so a bar can grow either side of a centre line. */
const HALF = 50;

export function ScoreBreakdown({ breakdown, score }: ScoreBreakdownProps) {
  const bars = toBars(breakdown);

  if (bars.length === 0) {
    // Not an empty panel: "nothing contributed" and "nothing was computed" are
    // different claims, and only one of them is true here. Session 1 carries
    // R1 prescores with no features behind them, so this is ordinary.
    return (
      <p role="status" style={{ fontSize: 11, color: "var(--dim)", margin: "10px 0 0" }}>
        No breakdown for this paper — it was scored before the feature pass ran.
      </p>
    );
  }

  const total = sumOf(breakdown);
  const mismatched = score != null && Math.abs(score - total) > EPSILON;

  return (
    <figure
      role="figure"
      aria-label="Score breakdown"
      style={{ margin: "12px 0 0", fontSize: 11 }}
    >
      <figcaption
        style={{
          display: "flex",
          justifyContent: "space-between",
          color: "var(--dim)",
          marginBottom: 4,
        }}
      >
        <span>score breakdown</span>
        <span style={{ fontVariantNumeric: "tabular-nums" }}>{total.toFixed(3)}</span>
      </figcaption>

      <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {bars.map((bar) => (
          <li
            key={bar.name}
            style={{ display: "flex", alignItems: "center", gap: 6, padding: "1px 0" }}
          >
            <span style={{ minWidth: 54, color: "var(--dim)" }}>{bar.name}</span>

            {/* The graphic is hidden from assistive technology: the name and
                the number beside it already say everything it shows, and a bar
                announced as "image" would be noise. */}
            <span
              aria-hidden
              style={{
                position: "relative",
                flex: 1,
                height: 8,
                background: "var(--track, #2d3748)",
                borderRadius: 2,
              }}
            >
              {/* Centre line: zero. Bars grow right when a term earns the
                  paper score and left when it costs it, so the sign is visible
                  as a direction rather than only as a minus sign. */}
              <span
                style={{
                  position: "absolute",
                  left: `${HALF}%`,
                  top: -1,
                  bottom: -1,
                  width: 1,
                  background: "var(--border-strong, #4a5568)",
                }}
              />
              <span
                style={{
                  position: "absolute",
                  top: 0,
                  bottom: 0,
                  width: `${bar.magnitude * HALF}%`,
                  ...(bar.negative
                    ? { right: `${HALF}%`, background: NEGATIVE }
                    : { left: `${HALF}%`, background: POSITIVE }),
                  borderRadius: 2,
                }}
              />
            </span>

            <span
              style={{
                minWidth: 42,
                textAlign: "right",
                fontVariantNumeric: "tabular-nums",
                color: bar.negative ? NEGATIVE : "inherit",
              }}
            >
              {bar.value.toFixed(2)}
            </span>
          </li>
        ))}
      </ul>

      {mismatched && (
        <p role="alert" style={{ margin: "6px 0 0", color: NEGATIVE, lineHeight: 1.35 }}>
          The stored score ({score?.toFixed(3)}) does not match its terms (
          {total.toFixed(3)}) — the weights changed without a rescore.
        </p>
      )}
    </figure>
  );
}
