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
}

export const useView = create<ViewState>((set) => ({
  // Single-user, single-workspace until R2 adds session management. The id is
  // held here rather than hard-coded at each call site so that change is one
  // edit rather than a search-and-replace.
  sessionId: 1,
  selectedId: null,
  setSelected: (id) => set({ selectedId: id }),
}));
