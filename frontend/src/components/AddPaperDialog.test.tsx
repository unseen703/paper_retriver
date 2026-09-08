/**
 * The dialog's keyboard contract.
 *
 * Journey:
 *
 *   As a keyboard user, I want Escape to close the Add-paper dialog from
 *   wherever I am inside it, and Tab to stay inside it, so that a modal that
 *   declares `aria-modal="true"` actually behaves like one.
 *
 * The reported finding: Escape was handled by an `onKeyDown` on the search
 * input alone, so it stopped working the moment focus moved into the results
 * list; there was no focus trap despite `aria-modal`; and focus was not
 * restored on close, dropping a keyboard user back at the top of the page.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { AddPaperDialog } from "./AddPaperDialog";

function renderDialog(onClose = vi.fn()) {
  // `retry: false` so a failing fetch in jsdom resolves immediately rather
  // than holding the test open through the retry ladder.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={client}>
      <AddPaperDialog sessionId={1} onClose={onClose} />
    </QueryClientProvider>
  );
  return { onClose, ...render(ui) };
}

describe("AddPaperDialog keyboard behaviour", () => {
  it("focuses the search input on open", async () => {
    renderDialog();
    expect(await screen.findByLabelText("Paper title")).toHaveFocus();
  });

  it("closes on Escape from the search input", async () => {
    const user = userEvent.setup();
    const { onClose } = renderDialog();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on Escape from anywhere inside the dialog", () => {
    // Dispatched at the dialog container, not the input. With the handler
    // bound to the input, a keydown on the container never reaches it --
    // events bubble upward, not down. An earlier version of this test called
    // `dialog.focus()` and passed for the wrong reason: a div without tabindex
    // does not take focus, so the key still landed on the input.
    const { onClose } = renderDialog();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps Tab inside the dialog", async () => {
    // `aria-modal="true"` tells assistive tech the rest of the page is inert.
    // Without a trap, Tab walked straight out into it.
    const user = userEvent.setup();
    renderDialog();
    const dialog = screen.getByRole("dialog");
    for (let i = 0; i < 6; i += 1) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });

  it("restores focus to whatever opened it", async () => {
    const opener = document.createElement("button");
    opener.textContent = "+ Add paper";
    document.body.appendChild(opener);
    opener.focus();

    const { unmount } = renderDialog();
    expect(screen.getByLabelText("Paper title")).toHaveFocus();

    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it("is labelled and marked modal", () => {
    renderDialog();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleName("Add a paper");
  });
});
