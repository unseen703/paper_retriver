import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "./index.css";

// TanStack Query owns server state; Zustand owns view state; the graph itself
// lives in the Cytoscape instance and never in React state (CLAUDE.md).
//
// `refetchOnWindowFocus` is off deliberately. Every refetch of the graph is a
// layout the user did not ask for, and alt-tabbing back to a rearranged canvas
// is the single most annoying thing a graph tool can do.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { refetchOnWindowFocus: false, retry: 1, staleTime: 30_000 },
  },
});

const root = document.getElementById("root");
if (!root) throw new Error("#root is missing from index.html");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
