/**
 * `expandAndWait` -- the client half of R2.4's job queue.
 *
 * Journey:
 *
 *     As someone expanding a graph, I want the button to finish when the run
 *     finishes, so a job that takes half a minute is not indistinguishable
 *     from one that silently failed.
 *
 * The loop is small and every one of its mistakes is expensive: stopping on
 * "not DONE" spins forever on a FAILED job, stopping on the first poll reports
 * success before anything happened, and forgetting to stop at all leaks a
 * timer for the life of the page. Each has a test.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type JobStatus } from "./client";

const SID = 1;

/** A JobStatus with only the fields the loop reads. */
function status(partial: Partial<JobStatus>): JobStatus {
  return {
    job_id: 7,
    session_id: SID,
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

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

describe("expandAndWait", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  /** Drive the loop to completion, flushing the 1s sleep between polls. */
  async function run(promise: Promise<unknown>) {
    await vi.runAllTimersAsync();
    return promise;
  }

  it("queues, then polls until the job leaves RUNNING", async () => {
    const bodies = [
      { job_id: 7, session_id: SID, status: "QUEUED", poll: "" },
      status({ status: "RUNNING", stage: "FETCHING" }),
      status({ status: "RUNNING", stage: "RANKING" }),
      status({ status: "DONE", stage: "DONE" }),
    ];
    const fetchMock = vi.fn(async () => jsonResponse(bodies.shift()));
    vi.stubGlobal("fetch", fetchMock);

    const result = (await run(api.expandAndWait(SID, 20))) as JobStatus;

    expect(result.status).toBe("DONE");
    // One POST plus three polls: it must not stop at the first non-QUEUED
    // status, which would report a still-running job as finished.
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("stops on FAILED rather than polling forever", async () => {
    // The reason the loop checks for "not QUEUED and not RUNNING" instead of
    // checking for DONE: a failed job is terminal, and waiting for it to
    // become DONE would hang until the page is closed.
    const bodies = [
      { job_id: 7, session_id: SID, status: "QUEUED", poll: "" },
      status({ status: "FAILED", stage: "FAILED", error: "boom" }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(bodies.shift())),
    );

    const result = (await run(api.expandAndWait(SID, 20))) as JobStatus;
    expect(result.status).toBe("FAILED");
    expect(result.error).toBe("boom");
  });

  it("reports every poll to onProgress, so a caller can show the stage", async () => {
    const bodies = [
      { job_id: 7, session_id: SID, status: "QUEUED", poll: "" },
      status({ stage: "FETCHING" }),
      status({ stage: "POOLING" }),
      status({ status: "DONE", stage: "DONE" }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(bodies.shift())),
    );

    const seen: string[] = [];
    await run(api.expandAndWait(SID, 20, (s) => seen.push(s.stage)));
    expect(seen).toEqual(["FETCHING", "POOLING", "DONE"]);
  });

  it("aborts when its signal is aborted", async () => {
    const bodies = [{ job_id: 7, session_id: SID, status: "QUEUED", poll: "" }];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(bodies.shift() ?? status({}))),
    );

    const controller = new AbortController();
    controller.abort();
    const pending = api.expandAndWait(SID, 20, undefined, controller.signal);
    // Attach the rejection handler *before* advancing timers. Running them
    // first leaves the rejection unobserved for a tick, which vitest reports
    // as an unhandled error even though the test itself passes.
    const rejects = expect(pending).rejects.toThrow();
    await vi.runAllTimersAsync();
    await rejects;
  });
});
