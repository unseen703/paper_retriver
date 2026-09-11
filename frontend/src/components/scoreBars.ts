/**
 * Turning a `score_breakdown` into drawable bars (R3.c).
 *
 * Pure. The arithmetic and the ordering live here where a test is a function
 * call; `ScoreBreakdown.tsx` only draws what comes out.
 *
 * Named `scoreBars` rather than `scoreBreakdown` on purpose: a helper
 * differing only in case from `ScoreBreakdown.tsx` resolves to the same module
 * on a case-insensitive filesystem, which is exactly how the bulk-import
 * component came out `undefined`.
 */

export interface Bar {
  name: string;
  /** The signed contribution, as the server computed it. */
  value: number;
  /**
   * Width as a fraction of the widest bar, 0–1.
   *
   * Relative rather than absolute: a score lives on no fixed range, so sizing
   * against an invented maximum would make the bars decorative.
   */
  magnitude: number;
  negative: boolean;
}

type Breakdown = Record<string, number> | null | undefined;

/**
 * Bars, biggest contribution first.
 *
 * **Ordered by absolute value**, because a term that costs the paper 0.6 is
 * exactly as interesting as one that earns it 0.6 — and `hub` is negative by
 * design, so the most informative bar is often below zero.
 *
 * Ties break on the name so the order cannot drift between renders
 * (CLAUDE.md rule 7).
 */
export function toBars(breakdown: Breakdown): Bar[] {
  if (!breakdown) return [];

  const entries = Object.entries(breakdown);
  if (entries.length === 0) return [];

  const widest = Math.max(...entries.map(([, value]) => Math.abs(value)));

  return entries
    .map(([name, value]) => ({
      name,
      value,
      // Guarded: every term at zero is degenerate but reachable, and a NaN
      // width renders as a bar of no particular size rather than an error.
      magnitude: widest === 0 ? 0 : Math.abs(value) / widest,
      negative: value < 0,
    }))
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value) || a.name.localeCompare(b.name));
}

/**
 * The total the bars add up to.
 *
 * BUILD.md asks for this explicitly: the terms must sum to the score. The
 * server guarantees it in `score_paper`; this is the display's half, and it is
 * worth stating separately because a breakdown that quietly loses a term looks
 * like an explanation while being one.
 */
export function sumOf(breakdown: Breakdown): number {
  if (!breakdown) return 0;
  return Object.values(breakdown).reduce((total, value) => total + value, 0);
}
