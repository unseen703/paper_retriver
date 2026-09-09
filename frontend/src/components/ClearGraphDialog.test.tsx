/**
 * R2.15 -- clearing the graph.
 *
 * Journey:
 *
 *     As someone who has made a mess of a graph, I want to start over without
 *     losing the corpus I paid API calls for, and without ever doing it by
 *     accident.
 *
 * BUILD.md: "UI requires typing 'clear'". The awkwardness is the feature. A
 * dialog you can dismiss with a reflexive Enter is not a confirmation, and
 * every test here is about the button staying inert until the user has
 * demonstrably read the sentence.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ClearGraphDialog } from "./ClearGraphDialog";

function renderDialog(onClose = vi.fn(), nodeCount = 127, onCleared = vi.fn()) {
  const fetchMock = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ cleared: nodeCount }),
  }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ClearGraphDialog
        sessionId={1}
        nodeCount={nodeCount}
        onClose={onClose}
        onCleared={onCleared}
      />
    </QueryClientProvider>,
  );
  return { fetchMock, onClose, onCleared };
}

const confirmButton = () => screen.getByRole("button", { name: /^clear graph$/i });
const wordField = () => screen.getByLabelText(/type clear to confirm/i);

describe("ClearGraphDialog", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("starts with the confirm button disabled", () => {
    renderDialog();
    expect(confirmButton()).toBeDisabled();
  });

  it("stays disabled for a nearly-right word", () => {
    // The point of the control is that it cannot be satisfied by pattern
    // recognition -- only by typing the word.
    renderDialog();
    fireEvent.change(wordField(), { target: { value: "clean" } });
    expect(confirmButton()).toBeDisabled();
  });

  it("arms once the word is typed", () => {
    renderDialog();
    fireEvent.change(wordField(), { target: { value: "clear" } });
    expect(confirmButton()).toBeEnabled();
  });

  it("accepts the word regardless of case or stray spaces", () => {
    // Refusing " Clear" would be pedantry rather than safety: the user has
    // plainly read the sentence and typed the word.
    renderDialog();
    fireEvent.change(wordField(), { target: { value: "  Clear " } });
    expect(confirmButton()).toBeEnabled();
  });

  it("disarms again if the word is edited away", () => {
    renderDialog();
    const field = wordField();
    fireEvent.change(field, { target: { value: "clear" } });
    fireEvent.change(field, { target: { value: "clea" } });
    expect(confirmButton()).toBeDisabled();
  });

  it("says how many papers are at stake", () => {
    // Clearing 4 papers and clearing 400 are different decisions, and this is
    // the only place that difference is visible.
    renderDialog(vi.fn(), 412);
    expect(screen.getByRole("dialog")).toHaveTextContent("412");
  });

  it("says what survives, not only what goes", () => {
    // The expensive thing is the corpus, and it is exactly what people fear
    // losing when they hesitate over this button.
    renderDialog();
    expect(screen.getByRole("dialog")).toHaveTextContent(/no API calls/i);
  });

  it("sends the confirm flag when fired", async () => {
    const { fetchMock } = renderDialog();
    fireEvent.change(wordField(), { target: { value: "clear" } });
    fireEvent.click(confirmButton());

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("confirm=true");
    expect(init.method).toBe("DELETE");
  });

  it("does not call the API while unarmed", () => {
    const { fetchMock } = renderDialog();
    fireEvent.click(confirmButton());
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("closes once the graph is cleared", async () => {
    const onClose = vi.fn();
    renderDialog(onClose);
    fireEvent.change(wordField(), { target: { value: "clear" } });
    fireEvent.click(confirmButton());
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("tells the app the graph is gone, so nothing keeps pointing into it", async () => {
    // `selectedId` is an id into a graph that no longer exists. Left alone,
    // the inspector stays open and fetches a node the session no longer has,
    // which comes back 404 -- an error panel for a paper nobody deleted on
    // purpose.
    const onCleared = vi.fn();
    renderDialog(vi.fn(), 5, onCleared);
    fireEvent.change(wordField(), { target: { value: "clear" } });
    fireEvent.click(confirmButton());
    await waitFor(() => expect(onCleared).toHaveBeenCalled());
  });

  it("does not report a clear that never happened", () => {
    const onCleared = vi.fn();
    renderDialog(vi.fn(), 5, onCleared);
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCleared).not.toHaveBeenCalled();
  });

  it("cancels without calling anything", () => {
    const onClose = vi.fn();
    const { fetchMock } = renderDialog(onClose);
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onClose).toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
