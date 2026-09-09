/**
 * R2.16 -- `<SessionSwitcher>`.
 *
 * Journey:
 *
 *     As someone exploring two different questions, I want a second graph that
 *     does not disturb the first, and I want to be able to tell them apart.
 *
 * The tests worth having are about the switcher being readable and about
 * creating one landing you in it -- making a workspace and then having to go
 * find it in a dropdown is a step nobody wants.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SessionSwitcher } from "./SessionSwitcher";
import type { SessionOut } from "../api/client";

const SESSIONS: SessionOut[] = [
  { id: 1, name: "language models", created_at: "2026-01-01", node_count: 127 },
  { id: 2, name: "vision", created_at: "2026-02-01", node_count: 0 },
];

async function renderSwitcher(onSwitch = vi.fn(), created?: SessionOut) {
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
    init?.method === "POST"
      ? ({ ok: true, status: 201, json: async () => created } as Response)
      : ({ ok: true, status: 200, json: async () => SESSIONS } as Response),
  );
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SessionSwitcher sessionId={1} onSwitch={onSwitch} />
    </QueryClientProvider>,
  );
  await waitFor(() => expect(screen.getByLabelText(/session/i)).toBeEnabled());
  return { fetchMock, onSwitch };
}

describe("SessionSwitcher", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists every session", async () => {
    await renderSwitcher();
    expect(screen.getByRole("option", { name: /language models/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /vision/i })).toBeInTheDocument();
  });

  it("shows each session's size, so they are distinguishable", async () => {
    // Without the count the list is a row of names, and the session holding
    // your work looks exactly like the empty one you made by accident.
    await renderSwitcher();
    expect(screen.getByRole("option", { name: /language models \(127\)/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /vision \(0\)/i })).toBeInTheDocument();
  });

  it("reports the chosen session", async () => {
    const { onSwitch } = await renderSwitcher();
    fireEvent.change(screen.getByLabelText(/session/i), { target: { value: "2" } });
    expect(onSwitch).toHaveBeenCalledWith(2);
  });

  it("will not create a session with a blank name", async () => {
    await renderSwitcher();
    fireEvent.click(screen.getByRole("button", { name: /\+ session/i }));
    fireEvent.change(screen.getByLabelText(/new session name/i), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: /^create$/i })).toBeDisabled();
  });

  it("switches into a session as soon as it is created", async () => {
    const fresh: SessionOut = { id: 7, name: "fresh", created_at: "2026-03-01", node_count: 0 };
    const { onSwitch } = await renderSwitcher(vi.fn(), fresh);
    fireEvent.click(screen.getByRole("button", { name: /\+ session/i }));
    fireEvent.change(screen.getByLabelText(/new session name/i), { target: { value: "fresh" } });
    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));

    await waitFor(() => expect(onSwitch).toHaveBeenCalledWith(7));
  });

  it("can back out of naming without creating anything", async () => {
    const { fetchMock } = await renderSwitcher();
    fetchMock.mockClear();
    fireEvent.click(screen.getByRole("button", { name: /\+ session/i }));
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.getByLabelText(/session/i)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
