/**
 * View state -- what the user is looking at, not what the server holds.
 *
 * CLAUDE.md draws the line: TanStack Query owns server state, Zustand owns
 * view state, and the graph itself lives in the Cytoscape instance. Nothing
 * here duplicates anything the API returns; `selectedId` is an id, not a node.
 */
import { create } from "zustand";

interface ViewState {
  sessionId: number;
  selectedId: number | null;
  setSelected: (id: number | null) => void;
  /**
   * Switch workspace (R2.16).
   *
   * Clears `selectedId` in the same set. A selection is an id into the graph
   * you were looking at, and that id means a different paper -- or nothing at
   * all -- in the next one, so carrying it across would open the inspector on
   * a paper the new session has never seen.
   */
  setSession: (id: number) => void;
}

export const useView = create<ViewState>((set) => ({
  // Session 1 is seeded by migration 0001 and is where everything starts.
  // Held here rather than hard-coded at each call site, which is what made
  // R2.16's switcher a small change rather than a search-and-replace.
  sessionId: 1,
  selectedId: null,
  setSelected: (id) => set({ selectedId: id }),
  setSession: (id) => set({ sessionId: id, selectedId: null }),
}));
