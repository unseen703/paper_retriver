/**
 * `<ExpansionControls>` — the result line, and which session it belongs to.
 *
 * Journey:
 *
 *     As someone working across several graphs, I want what the toolbar tells
 *     me to be about the graph I am looking at, so I do not act on a number
 *     that belongs to a different workspace.
 *
 * This component had no tests. Code review found the same defect here that it
 * found in `<CandidateList>`: session-scoped state in a component that is never
 * remounted when the session changes. The candidate table leaked rows; this
 * leaks a summary line, which is quieter and therefore worse — "added 18 of 20"
 * is a sentence you believe.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ExpansionControls } from "./ExpansionControls";

/** A queue that accepts immediately and reports a finished job on first poll. */
function stubExpansion(added: number) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const json =
        init?.method === "POST"
          ? { job_id: 7, session_id: 1, status: "QUEUED", poll: "/x" }
          : {
              job_id: 7,
              session_id: 1,
              status: "DONE",
              stage: "DONE",
              progress: { pool: 3, filtered: 0, added, api_calls: 2, cache_hits: 0 },
              error: null,
              result: {
                n_pool: 3,
                n_added: added,
                boundary: 0,
                api_calls: 2,
                truncated: false,
                error: null,
                added_paper_ids: [1],
              },
              started_at: null,
              finished_at: null,
            };
      void url;
      return { ok: true, status: 200, json: async () => json } as Response;
    }),
  );
}

function view(sessionId: number, client: QueryClient) {
  return (
    <QueryClientProvider client={client}>
      <ExpansionControls sessionId={sessionId} />
    </QueryClientProvider>
  );
}

describe("ExpansionControls", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reports what the expansion added", async () => {
    stubExpansion(18);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(view(1, client));

    fireEvent.click(screen.getByRole("button", { name: /expand/i }));
    expect(await screen.findByText(/added 18 of/i)).toBeInTheDocument();
  });

  it("drops the result line when the session changes", async () => {
    /**
     * **The defect.** `result` is local state and the component is never keyed
     * on `sessionId`, so an expansion run in one graph kept reporting "added 18
     * of 20" after switching to another — a summary about a workspace the user
     * is no longer looking at, and one that reads as fact.
     *
     * `useView.setSession` already clears `selectedId` for exactly this reason;
     * this is the same invariant, missed in a second place.
     */
    stubExpansion(18);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = render(view(1, client));

    fireEvent.click(screen.getByRole("button", { name: /expand/i }));
    await screen.findByText(/added 18 of/i);

    rerender(view(2, client));

    await waitFor(() => expect(screen.queryByText(/added 18 of/i)).not.toBeInTheDocument());
  });

  it("keeps the result line while the session is unchanged", async () => {
    // The other half: a re-render for any other reason must not throw away a
    // summary the user is still reading.
    stubExpansion(4);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = render(view(1, client));

    fireEvent.click(screen.getByRole("button", { name: /expand/i }));
    await screen.findByText(/added 4 of/i);

    rerender(view(1, client));
    expect(screen.getByText(/added 4 of/i)).toBeInTheDocument();
  });
});
