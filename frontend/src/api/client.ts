/**
 * The one place that talks to the backend.
 *
 * Every response type is imported from `src/types/api.ts`, which is generated
 * from the live OpenAPI schema by `npm run types`. CLAUDE.md forbids
 * hand-writing these: a hand-written type that drifts from the server compiles
 * perfectly and fails at runtime, which is the whole failure mode generation
 * exists to remove.
 */
import type { components } from "../types/api";

// Vite exposes VITE_-prefixed vars only. The default matches the backend's
// bind address; the backend deliberately listens on loopback, so this is not
// configurable to a LAN address by accident.
export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

export type SearchHit = components["schemas"]["SearchHit"];
export type GraphResponse = components["schemas"]["GraphResponse"];
export type GraphNodeOut = components["schemas"]["GraphNodeOut"];
export type GraphEdgeOut = components["schemas"]["GraphEdgeOut"];
export type NodeDetail = components["schemas"]["NodeDetail"];
export type NodeResponse = components["schemas"]["NodeResponse"];
export type ExpandResponse = components["schemas"]["ExpandResponse"];
export type HealthResponse = components["schemas"]["HealthResponse"];

/**
 * An HTTP error carrying the status, so callers can branch on 409 vs 422
 * rather than string-matching a message.
 *
 * The backend's error bodies are `{detail: ...}` where detail is a string for
 * most codes and an object carrying `reason_code` for a filter rejection.
 * Both shapes are preserved here rather than flattened to a string.
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.name = "ApiError";
  }

  /** The filter rule that rejected a paper, if this was a 422 from POST /nodes. */
  get reasonCode(): string | null {
    if (this.detail && typeof this.detail === "object" && "reason_code" in this.detail) {
      return String((this.detail as { reason_code: unknown }).reason_code);
    }
    return null;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail: unknown = response.statusText;
    try {
      detail = (await response.json())?.detail ?? detail;
    } catch {
      // A non-JSON error body is still an error; keep the status text.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<HealthResponse>("/api/health"),

  search: (sid: number, q: string, limit = 10) =>
    request<SearchHit[]>(
      `/api/sessions/${sid}/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),

  graph: (sid: number, states?: string[]) =>
    request<GraphResponse>(
      `/api/sessions/${sid}/graph${states?.length ? `?states=${states.join(",")}` : ""}`,
    ),

  node: (sid: number, paperId: number) =>
    request<NodeDetail>(`/api/sessions/${sid}/nodes/${paperId}`),

  addNode: (sid: number, s2PaperId: string, force = false) =>
    request<NodeResponse>(`/api/sessions/${sid}/nodes`, {
      method: "POST",
      body: JSON.stringify({ s2_paper_id: s2PaperId, force }),
    }),

  expand: (sid: number, maxNew = 20) =>
    request<ExpandResponse>(`/api/sessions/${sid}/expansions`, {
      method: "POST",
      body: JSON.stringify({ hops: 1, max_new: maxNew }),
    }),
};
