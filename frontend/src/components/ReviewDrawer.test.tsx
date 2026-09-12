/**
 * R2.14 -- `<ReviewDrawer>`.
 *
 * Journey:
 *
 *     As someone tuning a filter, I want to see what it threw away and why, so
 *     a filter quietly discarding good papers becomes something I notice
 *     rather than something I discover six weeks later.
 *
 * The tests worth having are about the drawer telling the truth: the counts
 * are the finding, an empty tab must say it is empty rather than look broken,
 * and Restore must only be offered where it actually does something.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReviewDrawer } from "./ReviewDrawer";
import type { ReviewBucketOut, ReviewResponse } from "../api/client";

function bucket(partial: Partial<ReviewBucketOut> = {}): ReviewBucketOut {
  return { total: 0, by_reason: [], papers: [], truncated: false, ...partial } as ReviewBucketOut;
}

function review(partial: Partial<ReviewResponse> = {}): ReviewResponse {
  return {
    quarantined: bucket(),
    rejected: bucket(),
    removed: bucket(),
    ...partial,
  } as ReviewResponse;
}

/** Render the drawer with `fetch` stubbed to return `body` for every call. */
async function renderDrawer(body: ReviewResponse, onSelect?: (id: number) => void) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, json: async () => body }) as Response),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ReviewDrawer sessionId={1} onClose={() => {}} onSelect={onSelect} />
    </QueryClientProvider>,
  );
  const drawer = await screen.findByRole("dialog", { name: /review/i });
  // The dialog renders immediately, with a Loading… placeholder, so finding it
  // proves nothing about the data. Wait for the placeholder to go.
  await waitFor(() => expect(screen.queryByText(/loading…/i)).not.toBeInTheDocument());
  return drawer;
}

const REJECTED = bucket({
  total: 6,
  by_reason: [
    { reason_code: "IS_DATASET", count: 4 },
    { reason_code: "CAT_NOT_ALLOWED", count: 2 },
  ],
  papers: [
    { paper_id: 11, title: "A benchmark suite", reason_code: "IS_DATASET", stage: "TYPE", year: 2019 },
  ],
});

describe("ReviewDrawer", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows each tab with its count, so you can see where papers went", async () => {
    const drawer = await renderDrawer(
      review({ rejected: REJECTED, quarantined: bucket({ total: 3 }) }),
    );
    expect(drawer).toHaveTextContent("Rejected (6)");
    expect(drawer).toHaveTextContent("Quarantined (3)");
    expect(drawer).toHaveTextContent("Removed (0)");
  });

  it("breaks the pile down by reason", async () => {
    // The distribution is the finding: one reason accounting for most of the
    // rejections is what tells you which threshold to go and look at.
    const drawer = await renderDrawer(review({ rejected: REJECTED }));
    expect(drawer).toHaveTextContent("IS_DATASET");
    expect(drawer).toHaveTextContent("CAT_NOT_ALLOWED");
  });

  it("lists the papers with their reason", async () => {
    const drawer = await renderDrawer(review({ rejected: REJECTED }));
    expect(drawer).toHaveTextContent("A benchmark suite");
    expect(drawer).toHaveTextContent("TYPE");
  });

  it("says an empty tab is empty rather than looking broken", async () => {
    const drawer = await renderDrawer(review());
    expect(drawer).toHaveTextContent(/nothing here/i);
  });

  it("switches tabs", async () => {
    const drawer = await renderDrawer(
      review({
        rejected: REJECTED,
        removed: bucket({
          total: 1,
          by_reason: [{ reason_code: "GC_SWEPT", count: 1 }],
          papers: [
            { paper_id: 9, title: "Swept paper", reason_code: "GC_SWEPT", stage: "SYSTEM", year: 2021 },
          ],
        }),
      }),
    );
    fireEvent.click(screen.getByRole("tab", { name: /removed/i }));
    expect(drawer).toHaveTextContent("Swept paper");
  });

  it("offers Restore only on the Removed tab", async () => {
    // Restore lifts a tombstone. A filtered paper was never in the graph, so
    // there is nothing to lift -- offering the button would promise an action
    // that cannot work.
    await renderDrawer(
      review({
        rejected: REJECTED,
        removed: bucket({
          total: 1,
          by_reason: [{ reason_code: "REMOVED", count: 1 }],
          papers: [
            { paper_id: 9, title: "Removed paper", reason_code: "REMOVED", stage: "USER", year: 2021 },
          ],
        }),
      }),
    );
    expect(screen.queryByRole("button", { name: /restore/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /removed/i }));
    expect(screen.getByRole("button", { name: /restore/i })).toBeInTheDocument();
  });

  it("reports a truncated list rather than implying it is complete", async () => {
    const drawer = await renderDrawer(
      review({
        rejected: bucket({
          total: 400,
          by_reason: [{ reason_code: "IS_DATASET", count: 400 }],
          papers: [
            { paper_id: 1, title: "One of many", reason_code: "IS_DATASET", stage: "TYPE", year: 2020 },
          ],
          truncated: true,
        }),
      }),
    );
    expect(drawer).toHaveTextContent(/showing 1 of 400/i);
    expect(drawer).toHaveTextContent(/counts above are complete/i);
  });

  it("selects a paper once it is restored, so the click is visibly not a no-op", async () => {
    const onSelect = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) =>
        init?.method === "POST"
          ? ({ ok: true, status: 200, json: async () => ({ paper_id: 9 }) } as Response)
          : ({
              ok: true,
              status: 200,
              json: async () =>
                review({
                  removed: bucket({
                    total: 1,
                    by_reason: [{ reason_code: "REMOVED", count: 1 }],
                    papers: [
                      {
                        paper_id: 9,
                        title: "Removed paper",
                        reason_code: "REMOVED",
                        stage: "USER",
                        year: 2021,
                      },
                    ],
                  }),
                }),
            } as Response),
      ),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ReviewDrawer sessionId={1} onClose={() => {}} onSelect={onSelect} />
      </QueryClientProvider>,
    );
    await screen.findByRole("dialog", { name: /review/i });
    fireEvent.click(screen.getByRole("tab", { name: /removed/i }));
    fireEvent.click(await screen.findByRole("button", { name: /restore/i }));

    await waitFor(() => expect(onSelect).toHaveBeenCalledWith(9));
  });
});
