/**
 * `<BulkImport>` — the loop, and the two ways it has to be stoppable.
 *
 * Journey:
 *
 *     As someone importing a forty-title reading list, I want to be able to
 *     change my mind, so a run I no longer want stops spending my API budget.
 *
 * The pure parsing lives in `readingList.ts` and is tested there. What is left
 * here is the part with a clock and a network in it, and it shipped without
 * tests — which is how the unmount bug below survived to code review.
 *
 * **The run is not just slow, it is expensive.** Every title costs a
 * rate-limited Semantic Scholar search, and every hit costs a write to the
 * graph. A loop that keeps going after the user has walked away is spending a
 * real budget on results nobody will see.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BulkImport } from "./BulkImport";

/** Counts calls and lets each response be released on demand. */
function stubApi() {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(String(url));
      // One tick per request, so a test can unmount "mid-loop".
      await new Promise((r) => setTimeout(r, 5));
      const isSearch = String(url).includes("/search");
      return {
        ok: true,
        status: 200,
        json: async () => (isSearch ? [{ s2_paper_id: "p1", title: "A paper" }] : { paper_id: 1 }),
      } as Response;
    }),
  );
  return calls;
}

function mount(titles: string[]) {
  const calls = stubApi();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <BulkImport sessionId={1} />
    </QueryClientProvider>,
  );
  const input = screen.getByLabelText(/import a list of titles/i).querySelector("input")!;
  const file = new File([titles.join("\n")], "list.txt", { type: "text/plain" });
  // jsdom's File has .text(); the component reads the file itself.
  fireEvent.change(input, { target: { files: [file] } });
  return { ...view, calls };
}

const MANY = Array.from({ length: 12 }, (_, i) => `Paper number ${i}`);

describe("BulkImport", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("parses a dropped file and offers to add what it found", async () => {
    mount(["Attention Is All You Need", "# a comment", "", "BERT"]);
    expect(await screen.findByRole("button", { name: /add 2/i })).toBeInTheDocument();
  });

  it("stops making requests when Stop is pressed", async () => {
    const { calls } = mount(MANY);
    fireEvent.click(await screen.findByRole("button", { name: /add 12/i }));

    await screen.findByRole("button", { name: /stop/i });
    fireEvent.click(screen.getByRole("button", { name: /stop/i }));

    await waitFor(() => expect(screen.queryByRole("button", { name: /stop/i })).toBeNull());
    const settled = calls.length;
    await new Promise((r) => setTimeout(r, 120));
    // At most the one request already in flight may land after the click.
    expect(calls.length).toBeLessThanOrEqual(settled + 1);
    expect(calls.length).toBeLessThan(MANY.length * 2);
  });

  it("stops making requests when the dialog closes mid-run", async () => {
    /**
     * **The bug code review caught, and the reason this file exists.**
     *
     * `AddPaperDialog` closes on Escape and on a backdrop click, and neither is
     * disabled while an import is running. Unmounting `<BulkImport>` removed the
     * Stop button but not the loop: it held its closure over `titles` and
     * `sessionId` and kept calling `api.search` and `api.addNode` once a second
     * for every remaining title — invisible, unstoppable, still writing to the
     * graph and still spending the API budget.
     *
     * Worse, reopening the dialog and starting a second import gave two
     * concurrent one-per-second loops against the same rate limit, which is
     * exactly the thing the sequential design exists to prevent.
     */
    const { calls, unmount } = mount(MANY);
    fireEvent.click(await screen.findByRole("button", { name: /add 12/i }));
    await screen.findByRole("button", { name: /stop/i });

    unmount();
    const atUnmount = calls.length;
    await new Promise((r) => setTimeout(r, 200));

    expect(calls.length).toBeLessThanOrEqual(atUnmount + 1);
    expect(calls.length).toBeLessThan(MANY.length * 2);
  });

  it("reports an outcome for every title it processed", async () => {
    // A run that says "added 34" and silently drops six is the failure that
    // matters: the six are the papers the user will assume are in the graph.
    mount(["One", "Two"]);
    fireEvent.click(await screen.findByRole("button", { name: /add 2/i }));
    await waitFor(() => expect(screen.getAllByRole("listitem")).toHaveLength(2));
    expect(screen.getByText(/2 added/i)).toBeInTheDocument();
  });
});
