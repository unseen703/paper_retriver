/**
 * R3.b -- `<CandidateList>`.
 *
 * Journey:
 *
 *     As someone deciding what to read next, I want the candidates as a ranked
 *     table, because the graph shows me structure and the table shows me an
 *     order I can act on.
 *
 * PLAN.md section F is blunt: "`<CandidateList>` is the product. A
 * force-directed graph is excellent for understanding *structure* and terrible
 * for reading a ranked list. Users will make most decisions from the table."
 *
 * **The load-bearing test is that this component does not sort.** The server's
 * `_sort_key` carries three rules with plausible wrong versions -- unknown
 * scores last rather than first, unknown is not zero, ties break on paper_id.
 * Re-implementing any of that in TypeScript means two orderings that agree
 * today and drift silently later, and the symptom would be a table that looks
 * perfectly sorted while disagreeing with every other view of the same data.
 * So the component renders the order it was given, and changing the sort is a
 * refetch.
 *
 * **The other is that a missing score is not a zero.** `hub` carries a negative
 * weight, so a real score can be below zero; rendering an unscored paper as
 * "0.00" would place it in the middle of a range it is not part of.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CandidateList, type CandidateListProps } from "./CandidateList";
import type { CandidatesResponse } from "../api/client";

type Row = CandidatesResponse["candidates"][number];

function row(partial: Partial<Row> = {}): Row {
  return {
    id: 1,
    title: "A paper",
    score: 1.5,
    score_breakdown: { overlap: 1.0, quality: 0.5 },
    year: 2022,
    venue: "NeurIPS",
    citation_count: 40,
    paper_type: "RESEARCH",
    primary_arxiv_category: "cs.LG",
    depth: 1,
    ...partial,
  } as Row;
}

function body(rows: Row[], total?: number): CandidatesResponse {
  return { candidates: rows, total: total ?? rows.length } as CandidatesResponse;
}

/** Captures every URL fetched, so sort/limit params can be asserted. */
function stubFetch(payload: CandidatesResponse) {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      urls.push(String(url));
      return { ok: true, status: 200, json: async () => payload } as Response;
    }),
  );
  return urls;
}

/**
 * Render with a stubbed fetch and wait for the table.
 *
 * The stub lives here rather than at each call site: a test that forgot it hit
 * the real `fetch`, rendered the error branch, and failed with a message about
 * text content that gave no hint the request was the problem.
 *
 * Returns the captured URLs so sort parameters can be asserted.
 */
function mount(
  payload: CandidatesResponse,
  props: Partial<CandidateListProps> = {},
): string[] {
  const urls = stubFetch(payload);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CandidateList sessionId={1} selectedId={null} onSelect={() => {}} {...props} />
    </QueryClientProvider>,
  );
  return urls;
}

async function renderList(
  payload: CandidatesResponse,
  props: Partial<CandidateListProps> = {},
) {
  mount(payload, props);
  return screen.findByRole("table", { name: /candidates/i });
}

function titles(): string[] {
  return screen
    .getAllByRole("row")
    .slice(1) // drop the header row
    .map((r) => within(r).getAllByRole("cell")[1]?.textContent ?? "");
}

describe("CandidateList", () => {
  afterEach(() => vi.unstubAllGlobals());

  // ------------------------------------------------------------------
  // Rendering the server's order
  // ------------------------------------------------------------------

  it("renders the rows in the order the server sent them", async () => {
    await renderList(
      body([
        row({ id: 1, title: "First", score: 0.1 }),
        row({ id: 2, title: "Second", score: 0.9 }),
        row({ id: 3, title: "Third", score: 0.5 }),
      ]),
    );

    // Deliberately NOT score order. The server decides; re-sorting here would
    // be a second implementation of `_sort_key` free to drift from the first.
    expect(titles()).toEqual(["First", "Second", "Third"]);
  });

  it("shows the columns a reader decides from", async () => {
    const table = await renderList(
      body([row({ title: "Attention", year: 2017, venue: "NeurIPS", citation_count: 191436 })]),
    );
    expect(table).toHaveTextContent("Attention");
    expect(table).toHaveTextContent("2017");
    expect(table).toHaveTextContent("NeurIPS");
  });

  // ------------------------------------------------------------------
  // A missing score is not a zero
  // ------------------------------------------------------------------

  it("renders an unscored paper as a dash, not as zero", async () => {
    const table = await renderList(body([row({ title: "Unscored", score: null })]));
    const dataRow = within(table).getAllByRole("row")[1];
    expect(within(dataRow).getByText("—")).toBeInTheDocument();
    expect(dataRow).not.toHaveTextContent("0.00");
  });

  it("renders a negative score as negative", async () => {
    // `hub` carries a negative weight, so totals below zero are ordinary and
    // must not be clamped or shown as absent.
    const table = await renderList(body([row({ title: "Poor", score: -0.42 })]));
    expect(table).toHaveTextContent("-0.42");
  });

  // ------------------------------------------------------------------
  // Sorting is a refetch, not a client-side re-order
  // ------------------------------------------------------------------

  it("asks the server for a different sort when a header is clicked", async () => {
    const urls = mount(body([row()]));
    await screen.findByRole("table", { name: /candidates/i });

    fireEvent.click(screen.getByRole("button", { name: /year/i }));

    await vi.waitFor(() => {
      expect(urls.some((u) => u.includes("sort=year"))).toBe(true);
    });
  });

  it("requests score order by default", async () => {
    const urls = mount(body([row()]));
    await screen.findByRole("table", { name: /candidates/i });
    expect(urls[0]).toContain("sort=score");
  });

  it("keeps the current rows on screen while a new sort loads", async () => {
    /**
     * Observed in the running app: clicking a header blanked the entire table
     * and replaced it with "Ranking…" until the response arrived. Re-sorting is
     * the most common thing anyone does here, so the table would spend much of
     * its life flashing empty — and an empty table is the same shape as "no
     * candidates", which is a different and alarming message.
     */
    await renderList(body([row({ id: 1, title: "Stays put" })]));

    fireEvent.click(screen.getByRole("button", { name: /year/i }));

    // Still the old rows, not a loading placeholder.
    expect(screen.getByText("Stays put")).toBeInTheDocument();
    expect(screen.queryByText(/ranking/i)).not.toBeInTheDocument();
  });

  it("does not show the previous session's rows while a new session loads", async () => {
    /**
     * **A cross-session leak, and it needed a cold cache to see.**
     *
     * `keepPreviousData` is `previousData => previousData`, fed from a
     * per-observer field that does not check whether the *session* segment of
     * the query key changed. `<CandidateList>` is not keyed on `sessionId`, so
     * switching sessions kept the same observer and the same rows.
     *
     * Reproduced in the running app: switching from session 2 (104 chemistry
     * papers) to a never-visited session 3 (22 protein papers) rendered 50 rows
     * of session 2's papers under session 3's header. Clicking one would call
     * `onSelect` with a paper id that belongs to a different graph — the
     * session_id contract broken at the last possible moment.
     *
     * Keeping rows across a *sort* change is the intended behaviour and is
     * asserted separately above; only a session change must drop them.
     */
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    stubFetch(body([row({ id: 1, title: "Session one paper" })]));

    const { rerender } = render(
      <QueryClientProvider client={client}>
        <CandidateList sessionId={1} selectedId={null} onSelect={() => {}} />
      </QueryClientProvider>,
    );
    await screen.findByText("Session one paper");

    // Never-fetched session: nothing is cached for it, which is the only
    // condition under which the placeholder is reached.
    stubFetch(body([row({ id: 99, title: "Session two paper" })]));
    rerender(
      <QueryClientProvider client={client}>
        <CandidateList sessionId={2} selectedId={null} onSelect={() => {}} />
      </QueryClientProvider>,
    );

    expect(screen.queryByText("Session one paper")).not.toBeInTheDocument();
  });

  it("marks which column the server ordered by", async () => {
    await renderList(body([row()]));
    // aria-sort is how a screen reader learns the table is ordered at all.
    expect(screen.getByRole("columnheader", { name: /score/i })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });

  // ------------------------------------------------------------------
  // total vs shown
  // ------------------------------------------------------------------

  it("says how many candidates there are when the list is truncated", async () => {
    await renderList(body([row({ id: 1 }), row({ id: 2 })], 213));
    expect(screen.getByRole("status")).toHaveTextContent("213");
  });

  it("does not claim truncation when everything is shown", async () => {
    await renderList(body([row({ id: 1 }), row({ id: 2 })], 2));
    expect(screen.getByRole("status")).toHaveTextContent("2");
  });

  // ------------------------------------------------------------------
  // Selection -- shared with the canvas
  // ------------------------------------------------------------------

  it("selects a paper when its row is clicked", async () => {
    const onSelect = vi.fn();
    await renderList(body([row({ id: 77, title: "Pick me" })]), { onSelect });

    fireEvent.click(screen.getByText("Pick me"));
    expect(onSelect).toHaveBeenCalledWith(77);
  });

  it("marks the selected row", async () => {
    const table = await renderList(
      body([row({ id: 5, title: "Chosen" }), row({ id: 6, title: "Other" })]),
      { selectedId: 5 },
    );
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveAttribute("aria-selected", "true");
    expect(rows[1]).toHaveAttribute("aria-selected", "false");
  });

  it("is keyboard reachable", async () => {
    // A table you can only use with a mouse is the product being unusable for
    // part of its audience, and this one is the product.
    const onSelect = vi.fn();
    await renderList(body([row({ id: 9, title: "Reachable" })]), { onSelect });

    fireEvent.keyDown(screen.getByText("Reachable").closest("tr")!, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith(9);
  });

  // ------------------------------------------------------------------
  // The states that are not a list
  // ------------------------------------------------------------------

  it("says so when there are no candidates", async () => {
    mount(body([]));
    // By text, not by `role=status`: the loading state is also a status, and
    // `findByRole` would resolve on "Ranking…" and assert against the wrong one.
    expect(await screen.findByText(/no candidates/i)).toBeInTheDocument();
  });

  it("reports an error rather than rendering an empty table", async () => {
    // An empty table and a failed request look identical, and one of them
    // means "expand your graph" while the other means "the server is down".
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 500, text: async () => "boom" }) as Response),
    );
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <CandidateList sessionId={1} selectedId={null} onSelect={() => {}} />
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/unavailable/i)).toBeInTheDocument();
  });

  it("fetches nothing while disabled", async () => {
    const urls = mount(body([row()]), { disabled: true });
    expect(urls).toHaveLength(0);
  });
});
