import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { ApiError } from "./api/client";
import "./index.css";

// TanStack Query owns server state; Zustand owns view state; the graph itself
// lives in the Cytoscape instance and never in React state (CLAUDE.md).
//
// `refetchOnWindowFocus` is off deliberately. Every refetch of the graph is a
// layout the user did not ask for, and alt-tabbing back to a rearranged canvas
// is the single most annoying thing a graph tool can do.
//
// Retries are conditional, not a flat `retry: 1`. Two kinds of error must not
// be re-sent:
//
//   4xx    a 404 or 422 will not become a 200 on a second try, so the retry is
//          pure waste.
//   503    this backend returns 503 when Semantic Scholar is unreachable OR
//          rate-limiting. The backend has already exhausted its own retry
//          ladder by then, so re-running the query makes a second full ladder
//          run against an upstream that is already refusing -- on a budget of
//          one request per second.
//
// That leaves genuine server faults, which are worth exactly one retry.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      staleTime: 30_000,
      retry: (failureCount, error) => {
        if (error instanceof ApiError) {
          if (error.status < 500 || error.status === 503) return false;
        }
        return failureCount < 1;
      },
    },
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
