/**
 * R3.c — `<ScoreBreakdown>`.
 *
 * Journey:
 *
 *     As someone deciding whether to trust a rank, I want to see which
 *     features produced the score, so the number is an explanation rather than
 *     an assertion.
 *
 * PLAN.md §F: "the highest-signal-per-line-of-code component in the whole app.
 * It makes the recommender legible, it doubles as your debugging tool, and in a
 * portfolio review it demonstrates that you understand interpretability as an
 * engineering property rather than a buzzword."
 *
 * It replaced a `JSON.stringify` of the same object. Everything below is the
 * difference between dumping the data and explaining it — and two of the tests
 * are about not misleading, which is the failure mode of an explanation nobody
 * can check.
 */
import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { ScoreBreakdown } from "./ScoreBreakdown";

const REAL = { overlap: 2.0, quality: 0.16, recency: 0.075, hub: -0.4 };

function panel(breakdown: Record<string, number> | null, score?: number | null) {
  render(<ScoreBreakdown breakdown={breakdown} score={score ?? null} />);
  return screen.queryByRole("figure", { name: /score breakdown/i });
}

describe("ScoreBreakdown", () => {
  it("names every term", () => {
    const figure = panel(REAL);
    for (const name of ["overlap", "quality", "recency", "hub"]) {
      expect(figure).toHaveTextContent(name);
    }
  });

  it("shows the contribution of each term, not just its name", () => {
    // A bar with no number is a shape. The value is what makes it checkable.
    expect(panel(REAL)).toHaveTextContent("2.00");
  });

  it("shows the total the terms add up to", () => {
    // BUILD.md: the terms must sum to the score. Displaying the sum is what
    // lets a reader verify that claim instead of trusting it.
    expect(panel(REAL)).toHaveTextContent("1.835");
  });

  it("puts the largest contribution first", () => {
    const figure = panel(REAL);
    const rows = within(figure!).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("overlap");
  });

  it("ranks a large negative term above a small positive one", () => {
    /**
     * `hub` carries a negative weight — a paper everything cites is a bad
     * recommendation however good it looks. A term that costs the paper 0.4 is
     * more worth seeing than one that earns it 0.075, and sorting by signed
     * value would bury it at the bottom.
     */
    const figure = panel(REAL);
    const rows = within(figure!).getAllByRole("listitem");
    const order = rows.map((r) => r.textContent ?? "");
    expect(order.findIndex((t) => t.includes("hub"))).toBeLessThan(
      order.findIndex((t) => t.includes("recency")),
    );
  });

  it("renders a negative term as negative rather than clamping it", () => {
    // Hiding the sign would remove the single most interesting thing the
    // breakdown has to say about a hub.
    expect(panel({ hub: -0.4 })).toHaveTextContent("-0.40");
  });

  it("says so when there is no breakdown", () => {
    /**
     * The normal case for session 1, which has scores from R1's prescore and
     * no features behind them. An empty panel would read as "this paper scored
     * nothing", which is a different and wrong claim.
     */
    render(<ScoreBreakdown breakdown={null} score={2.4} />);
    expect(screen.getByRole("status")).toHaveTextContent(/no breakdown/i);
  });

  it("says so for an empty breakdown object too", () => {
    render(<ScoreBreakdown breakdown={{}} score={2.4} />);
    expect(screen.getByRole("status")).toHaveTextContent(/no breakdown/i);
  });

  it("is readable without seeing the bars", () => {
    // The bars are decoration over the numbers, not the other way round: a
    // screen reader gets name and value from the text, and the graphic itself
    // is aria-hidden.
    const figure = panel({ overlap: 1.0 });
    const row = within(figure!).getAllByRole("listitem")[0];
    expect(row).toHaveTextContent("overlap");
    expect(row).toHaveTextContent("1.00");
  });

  it("flags a stored total that disagrees with the terms", () => {
    /**
     * **The test that earns the component its keep as a debugging tool.**
     *
     * `score` is persisted alongside `score_breakdown`, and a config change
     * that rescored one without the other would leave them inconsistent. Both
     * numbers would look perfectly plausible on their own. Showing the
     * disagreement is the only way anyone finds out.
     */
    render(<ScoreBreakdown breakdown={{ overlap: 1.0 }} score={9.9} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/does not match/i);
  });

  it("does not cry mismatch over floating-point dust", () => {
    // 0.1 + 0.2 !== 0.3. A warning that fires on the last decimal place is a
    // warning people learn to ignore.
    render(<ScoreBreakdown breakdown={{ a: 0.1, b: 0.2 }} score={0.30000000000000004} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
