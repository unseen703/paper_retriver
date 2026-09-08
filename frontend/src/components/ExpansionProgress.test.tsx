/**
 * R2.9 -- the expansion progress row.
 *
 * Journey:
 *
 *     As someone who just clicked Expand, I want to see the run moving, so a
 *     job that takes half a minute is distinguishable from one that hung.
 *
 * BUILD.md's verification is "progress advances visibly through stages", and
 * the tests that matter here are the ones about *not lying*: a count that is
 * null means "not yet" and must not render as a zero, because "pooled 0" and
 * "has not pooled yet" describe a broken run and a healthy one respectively.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ExpansionProgress } from "./ExpansionProgress";
import type { JobStatus } from "../api/client";

function status(partial: Partial<JobStatus> = {}): JobStatus {
  return {
    job_id: 1,
    session_id: 1,
    status: "RUNNING",
    stage: "FETCHING",
    progress: { pool: null, filtered: null, added: null, api_calls: null, cache_hits: null },
    error: null,
    result: null,
    started_at: null,
    finished_at: null,
    ...partial,
  } as JobStatus;
}

describe("ExpansionProgress", () => {
  it("renders nothing when there is no job", () => {
    const { container } = render(<ExpansionProgress status={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("names the stage in words rather than in the server's vocabulary", () => {
    render(<ExpansionProgress status={status({ stage: "FETCHING" })} />);
    expect(screen.getByRole("status")).toHaveTextContent("fetching citations");
  });

  it("advances through the stages", () => {
    const { rerender } = render(<ExpansionProgress status={status({ stage: "FETCHING" })} />);
    expect(screen.getByRole("status")).toHaveTextContent("fetching citations");
    rerender(<ExpansionProgress status={status({ stage: "POOLING" })} />);
    expect(screen.getByRole("status")).toHaveTextContent("finding candidates");
    rerender(<ExpansionProgress status={status({ stage: "DONE", status: "DONE" })} />);
    expect(screen.getByRole("status")).toHaveTextContent("done");
  });

  it("shows a count once it is known", () => {
    render(
      <ExpansionProgress
        status={status({
          stage: "POOLING",
          progress: { pool: null, filtered: 12, added: null, api_calls: 4, cache_hits: null },
        })}
      />,
    );
    const row = screen.getByRole("status");
    expect(row).toHaveTextContent("4 API calls");
    expect(row).toHaveTextContent("12 boundary");
  });

  it("omits a count that is not known yet rather than showing zero", () => {
    // The load-bearing one. `pool: null` means pooling has not happened;
    // rendering it as "0 pooled" would report a healthy run as one that found
    // nothing, which is the difference between waiting and giving up.
    render(
      <ExpansionProgress
        status={status({
          stage: "FETCHING",
          progress: { pool: null, filtered: null, added: null, api_calls: 3, cache_hits: null },
        })}
      />,
    );
    expect(screen.getByRole("status")).not.toHaveTextContent("pooled");
  });

  it("shows a real zero, because zero is an answer", () => {
    render(
      <ExpansionProgress
        status={status({
          stage: "ADDING",
          progress: { pool: 40, filtered: 3, added: 0, api_calls: 9, cache_hits: 0 },
        })}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("0 added");
  });

  it("says a failed job failed", () => {
    render(<ExpansionProgress status={status({ status: "FAILED", stage: "FAILED" })} />);
    expect(screen.getByRole("status")).toHaveTextContent("failed");
  });

  it("announces politely, so a screen reader is not interrupted every second", () => {
    render(<ExpansionProgress status={status()} />);
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
  });
});
