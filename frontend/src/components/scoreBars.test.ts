/**
 * R3.c — turning `score_breakdown` into bars.
 *
 * Journey:
 *
 *     As someone deciding whether to trust a rank, I want to see which
 *     features produced the score, so the number is an explanation rather than
 *     an assertion.
 *
 * PLAN.md §F: "`<ScoreBreakdown>` is the highest-signal-per-line-of-code
 * component in the whole app. It makes the recommender legible, it doubles as
 * your debugging tool."
 *
 * **The terms must sum to the score.** BUILD.md names that test, and it is the
 * one that matters: a breakdown that does not add up looks like an explanation
 * while being one. The server already guarantees it in `score_paper`; this
 * asserts the display does not quietly lose a term on the way to the screen.
 *
 * **Negative terms are ordinary here.** `hub` carries a negative weight — a
 * paper everything cites is a bad recommendation — so the bars have to render
 * below zero rather than clamping, which would hide the single most
 * interesting thing the breakdown has to say.
 *
 * The module is named `scoreBars`, not `scoreBreakdown`, because a helper
 * differing only in case from `ScoreBreakdown.tsx` resolves to the same file on
 * a case-insensitive filesystem — which broke the bulk-import component earlier
 * in exactly this way.
 */
import { describe, expect, it } from "vitest";
import { sumOf, toBars } from "./scoreBars";

describe("toBars", () => {
  it("makes one bar per term", () => {
    expect(toBars({ overlap: 1.0, quality: 0.4 }).map((b) => b.name)).toEqual([
      "overlap",
      "quality",
    ]);
  });

  it("orders by absolute contribution, biggest first", () => {
    // The biggest lever is what the reader is looking for, and a negative term
    // is as informative as a positive one of the same size.
    const bars = toBars({ recency: 0.2, overlap: 1.0, hub: -0.6 });
    expect(bars.map((b) => b.name)).toEqual(["overlap", "hub", "recency"]);
  });

  it("breaks ties by name so the order never drifts", () => {
    // CLAUDE.md rule 7. Two equal contributions must not swap places between
    // renders for reasons nobody can see.
    const bars = toBars({ zeta: 0.5, alpha: 0.5, mu: 0.5 });
    expect(bars.map((b) => b.name)).toEqual(["alpha", "mu", "zeta"]);
  });

  it("marks a negative term as negative", () => {
    const bars = toBars({ hub: -0.6 });
    expect(bars[0].negative).toBe(true);
    expect(bars[0].value).toBe(-0.6);
  });

  it("scales every bar against the largest absolute contribution", () => {
    // Relative, not absolute: scores live on no fixed range, so a bar sized
    // against an invented maximum would mean nothing.
    const bars = toBars({ overlap: 1.0, quality: 0.5 });
    expect(bars[0].magnitude).toBeCloseTo(1);
    expect(bars[1].magnitude).toBeCloseTo(0.5);
  });

  it("lets a negative term set the scale when it is the largest", () => {
    const bars = toBars({ hub: -0.8, overlap: 0.4 });
    expect(bars[0].name).toBe("hub");
    expect(bars[0].magnitude).toBeCloseTo(1);
    expect(bars[1].magnitude).toBeCloseTo(0.5);
  });

  it("does not divide by zero when every term is zero", () => {
    // Degenerate but reachable: a paper whose every feature landed at the
    // neutral midpoint with symmetric weights.
    const bars = toBars({ overlap: 0, hub: 0 });
    expect(bars.every((b) => b.magnitude === 0)).toBe(true);
  });

  it("returns nothing for an empty or absent breakdown", () => {
    // Session 1 has scores from R1 prescore and no breakdown at all, so this
    // is the normal case rather than an error.
    expect(toBars({})).toEqual([]);
    expect(toBars(null)).toEqual([]);
    expect(toBars(undefined)).toEqual([]);
  });

  it("ignores a term whose value is not a number", () => {
    /**
     * `score_breakdown` is a JSON column, and `NodeDetail` declares it
     * `dict[str, Any]` — so the generated type promises `unknown`, not
     * `number`. Doing arithmetic on whatever arrives produced `NaN` widths:
     * bars of no particular size, on the component whose entire job is being
     * trustworthy. Dropping the term is the honest answer; inventing a zero
     * would put a bar on the chart for a value nobody can read.
     */
    const bars = toBars({ overlap: 1.0, broken: "oops" as unknown as number });
    expect(bars.map((b) => b.name)).toEqual(["overlap"]);
  });

  it("ignores NaN and infinity", () => {
    const bars = toBars({ a: Number.NaN, b: Number.POSITIVE_INFINITY, ok: 0.5 });
    expect(bars.map((b) => b.name)).toEqual(["ok"]);
  });

  it("does not let a bad term poison the scale of the good ones", () => {
    // If a non-number reached `Math.max`, every magnitude became NaN and the
    // whole chart rendered blank rather than one row being missing.
    const bars = toBars({ overlap: 1.0, quality: 0.5, junk: null as unknown as number });
    expect(bars[0].magnitude).toBeCloseTo(1);
    expect(bars[1].magnitude).toBeCloseTo(0.5);
  });

  it("is deterministic", () => {
    const breakdown = { overlap: 1.0, quality: 0.4, hub: -0.6 };
    expect(toBars(breakdown)).toEqual(toBars(breakdown));
  });
});

describe("sumOf", () => {
  it("adds the terms up to the score", () => {
    /**
     * **BUILD.md's named test.** `score_paper` guarantees the terms sum to the
     * total on the server; this is the display's half of that promise — a
     * breakdown that loses a term on the way to the screen would look like an
     * explanation while being one.
     */
    expect(sumOf({ overlap: 2.0, quality: 0.16, recency: 0.075, hub: -0.4 })).toBeCloseTo(1.835);
  });

  it("counts a negative term as negative", () => {
    expect(sumOf({ overlap: 1.0, hub: -0.6 })).toBeCloseTo(0.4);
  });

  it("sums an empty breakdown to zero rather than failing", () => {
    expect(sumOf({})).toBe(0);
    expect(sumOf(null)).toBe(0);
  });

  it("agrees with the bars it is displayed beside", () => {
    // The two are computed separately and shown together, so they have to be
    // asserted together or they can drift.
    const breakdown = { overlap: 1.0, quality: 0.4, hub: -0.6 };
    const fromBars = toBars(breakdown).reduce((acc, bar) => acc + bar.value, 0);
    expect(fromBars).toBeCloseTo(sumOf(breakdown));
  });
});

describe("sumOf, with a JSON column's worth of surprises", () => {
  it("skips a non-numeric term rather than producing NaN", () => {
    // A single bad value turning the total into NaN would blank the headline
    // number and the mismatch warning at once — the two things that make the
    // breakdown checkable.
    expect(sumOf({ overlap: 1.0, broken: "oops" as unknown as number })).toBeCloseTo(1.0);
  });

  it("agrees with the bars when a term was dropped", () => {
    const breakdown = { overlap: 1.0, hub: -0.4, junk: undefined as unknown as number };
    const fromBars = toBars(breakdown).reduce((acc, bar) => acc + bar.value, 0);
    expect(fromBars).toBeCloseTo(sumOf(breakdown));
  });
});
