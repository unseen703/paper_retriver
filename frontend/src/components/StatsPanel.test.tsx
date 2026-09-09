/**
 * R2.13 -- `<StatsPanel>`.
 *
 * Journey:
 *
 *     As someone curating a graph, I want to know what is actually in it, so
 *     "this looks about right" can be replaced by a number I can check.
 *
 * The tests that matter are the ones about not misleading: a zero must render
 * as a zero rather than vanish, and the two numbers that quietly invalidate
 * R3's scores -- thin crawl coverage and a fragmented graph -- must say so
 * rather than sit there as bare figures nobody knows how to read.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StatsPanel } from "./StatsPanel";
import type { StatsResponse } from "../api/client";

function stats(partial: Partial<StatsResponse> = {}): StatsResponse {
  return {
    node_count: 10,
    edge_count: 12,
    by_state: { SEED: 1, LIKED: 2, DISLIKED: 3, CANDIDATE: 4 },
    components: 1,
    avg_degree: 2.4,
    density: 0.2667,
    crawl_completeness: 0.9,
    ...partial,
  } as StatsResponse;
}

/** Render with a fresh QueryClient and a stubbed fetch returning `body`. */
async function renderPanel(body: StatsResponse) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, json: async () => body }) as Response),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <StatsPanel sessionId={1} />
    </QueryClientProvider>,
  );
  return screen.findByRole("region", { name: /graph statistics/i });
}

describe("StatsPanel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the node and edge counts", async () => {
    const panel = await renderPanel(stats());
    expect(panel).toHaveTextContent("nodes");
    expect(panel).toHaveTextContent("10");
    expect(panel).toHaveTextContent("12");
  });

  it("renders every state, including the ones at zero", async () => {
    // "Nothing disliked yet" and "this build forgot to count them" must not
    // look the same. A missing row is indistinguishable from a broken one.
    const panel = await renderPanel(
      stats({ by_state: { SEED: 1, LIKED: 0, DISLIKED: 0, CANDIDATE: 0 } }),
    );
    for (const label of ["seed", "liked", "disliked", "candidate"]) {
      expect(panel).toHaveTextContent(label);
    }
  });

  it("warns when too little of the graph has been crawled", async () => {
    // PLAN.md section I: below 0.6, structural scores are biased toward
    // whatever happens to have been fetched. R3 suppresses PageRank there.
    const panel = await renderPanel(stats({ crawl_completeness: 0.42 }));
    expect(panel).toHaveTextContent(/biased/i);
    expect(panel).toHaveTextContent("42%");
  });

  it("stays quiet about coverage once the graph is well crawled", async () => {
    const panel = await renderPanel(stats({ crawl_completeness: 0.95 }));
    expect(panel).not.toHaveTextContent(/biased/i);
  });

  it("says so when the graph is in several disconnected pieces", async () => {
    const panel = await renderPanel(stats({ components: 4 }));
    expect(panel).toHaveTextContent(/disconnected pieces/i);
  });

  it("stays quiet when the graph is connected", async () => {
    const panel = await renderPanel(stats({ components: 1 }));
    expect(panel).not.toHaveTextContent(/disconnected pieces/i);
  });

  it("renders an empty graph as zeroes rather than blanks", async () => {
    const panel = await renderPanel(
      stats({
        node_count: 0,
        edge_count: 0,
        by_state: { SEED: 0, LIKED: 0, DISLIKED: 0, CANDIDATE: 0 },
        components: 0,
        avg_degree: 0,
        density: 0,
        crawl_completeness: 0,
      }),
    );
    expect(panel).toHaveTextContent("0.00");
    expect(panel).toHaveTextContent("0%");
  });
});
