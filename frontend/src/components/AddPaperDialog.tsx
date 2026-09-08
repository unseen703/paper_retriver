/**
 * `<AddPaperDialog>` (R1.22) -- the deliberate way into the graph.
 *
 * Search is debounced at 300ms, which is not a performance nicety here. Every
 * uncached query costs a real Semantic Scholar request against a budget of one
 * per second, so firing per keystroke would spend the day's allowance on
 * prefixes of a title nobody meant to search for.
 *
 * **Both session flags are surfaced, and they mean different things.**
 * `already_in_graph` disables the row -- adding it would only produce a 409.
 * `previously_removed` does NOT disable it: the user took that paper out on
 * purpose and may equally have changed their mind, so it warns and lets them
 * through. Treating the two the same is how a tool ends up either nagging
 * about papers you deliberately removed or silently re-adding them.
 *
 * The 422 path is why `POST /nodes` returns a `reason_code` rather than a
 * message: a rejected paper is offered again with `force`, named by the rule
 * that refused it, so the override is a specific decision rather than a shrug.
 */
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, type SearchHit } from "../api/client";

const DEBOUNCE_MS = 300;
// Below this a title search returns mostly noise, and each one costs a request.
const MIN_QUERY = 3;

export interface AddPaperDialogProps {
  sessionId: number;
  onClose: () => void;
}

export function AddPaperDialog({ sessionId, onClose }: AddPaperDialogProps) {
  const queryClient = useQueryClient();
  const [raw, setRaw] = useState("");
  const [query, setQuery] = useState("");
  const [forcing, setForcing] = useState<{ hit: SearchHit; reason: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  // Focus in on open, focus back where it came from on close.
  //
  // `aria-modal="true"` tells assistive technology the rest of the page is
  // inert, and that was a claim this dialog did not honour: Tab walked
  // straight out into the page behind it, and closing dropped focus on the
  // body rather than returning it to the button that opened the dialog -- so
  // a keyboard user restarted their traversal from the top of the document.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    inputRef.current?.focus();
    return () => opener?.focus?.();
  }, []);

  // Debounce. The timer is cleared on every keystroke, so only a pause
  // actually issues the request.
  useEffect(() => {
    const trimmed = raw.trim();
    if (trimmed.length < MIN_QUERY) {
      setQuery("");
      return;
    }
    const timer = window.setTimeout(() => setQuery(trimmed), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [raw]);

  const search = useQuery({
    queryKey: ["search", sessionId, query],
    queryFn: () => api.search(sessionId, query),
    enabled: query.length >= MIN_QUERY,
  });

  const add = useMutation({
    mutationFn: ({ hit, force }: { hit: SearchHit; force?: boolean }) =>
      api.addNode(sessionId, hit.s2_paper_id, force ?? false),
    onSuccess: () => {
      // The graph is the thing that changed; the search list's flags did too.
      queryClient.invalidateQueries({ queryKey: ["graph", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["search", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["health"] });
      setForcing(null);
      onClose();
    },
    onError: (error, variables) => {
      // 422 is not a failure to report and forget -- it is an offer. The
      // cascade named a rule, so the dialog can ask whether to override that
      // specific rule rather than just saying no.
      if (error instanceof ApiError && error.status === 422 && error.reasonCode) {
        setForcing({ hit: variables.hit, reason: error.reasonCode });
      }
    },
  });

  const errorMessage = (() => {
    const error = add.error;
    if (!(error instanceof ApiError)) return null;
    if (error.status === 422) return null; // handled by the force prompt
    if (error.status === 409) return "That paper is already in this graph.";
    if (error.status === 404) return "Semantic Scholar does not know that paper.";
    if (error.status === 503) return "Semantic Scholar is unavailable. Try again shortly.";
    return String(error.detail ?? error.message);
  })();

  return (
    <div style={backdrop} onClick={onClose} role="presentation">
      <div
        ref={panelRef}
        style={panel}
        onClick={(event) => event.stopPropagation()}
        // On the container, not the input. Bound to the input, Escape stopped
        // working the moment focus moved into the results list -- which Tab
        // does immediately -- because events bubble upward, not down.
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.stopPropagation();
            onClose();
            return;
          }
          if (event.key !== "Tab") return;
          // The trap. Cycle between the first and last focusable elements
          // rather than letting Tab leave a dialog that claims to be modal.
          const focusable = panelRef.current?.querySelectorAll<HTMLElement>(
            'input:not([disabled]), button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
          );
          if (!focusable || focusable.length === 0) return;
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          const active = document.activeElement;
          if (event.shiftKey && (active === first || !panelRef.current?.contains(active))) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && active === last) {
            event.preventDefault();
            first.focus();
          }
        }}
        role="dialog"
        aria-modal="true"
        aria-label="Add a paper"
      >
        <input
          ref={inputRef}
          value={raw}
          onChange={(event) => setRaw(event.target.value)}
          placeholder="Search by title, e.g. Attention Is All You Need"
          style={{ width: "100%" }}
          aria-label="Paper title"
        />

        <p style={status}>
          {raw.trim().length > 0 && raw.trim().length < MIN_QUERY
            ? `Keep typing — ${MIN_QUERY} characters minimum.`
            : search.isFetching
              ? "Searching…"
              : search.isError
                ? "Search is unavailable. Is the backend running?"
                : search.data
                  ? `${search.data.length} result${search.data.length === 1 ? "" : "s"}`
                  : "Long exact titles match poorly — a distinctive fragment works better."}
        </p>

        {errorMessage && <p style={{ ...status, color: "salmon" }}>{errorMessage}</p>}

        {forcing && (
          <div style={forcePrompt}>
            <strong style={{ color: "var(--text)" }}>{forcing.hit.title}</strong>
            <span>
              {" "}
              was rejected by the <code>{forcing.reason}</code> rule.
            </span>
            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <button onClick={() => add.mutate({ hit: forcing.hit, force: true })}>
                Add anyway
              </button>
              <button onClick={() => setForcing(null)}>Cancel</button>
            </div>
          </div>
        )}

        <ul style={list}>
          {(search.data ?? []).map((hit) => (
            <li key={hit.s2_paper_id}>
              <button
                style={{
                  ...row,
                  cursor: hit.already_in_graph ? "default" : "pointer",
                  opacity: hit.already_in_graph ? 0.55 : 1,
                }}
                disabled={hit.already_in_graph || add.isPending}
                onClick={() => add.mutate({ hit })}
              >
                <span style={{ color: "var(--text)" }}>{hit.title}</span>
                <span style={meta}>
                  {hit.year ?? "—"} · {(hit.citation_count ?? 0).toLocaleString()} citations
                  {hit.venue ? ` · ${hit.venue}` : ""}
                  {(hit.authors?.length ?? 0) > 0 ? ` · ${hit.authors![0]}` : ""}
                  {(hit.authors?.length ?? 0) > 1 ? " et al." : ""}
                </span>
                <span style={{ display: "flex", gap: 6, marginTop: 4 }}>
                  {hit.already_in_graph && <span style={badgeIn}>already in graph</span>}
                  {/* Not disabling: removal was a decision, and so is undoing it. */}
                  {hit.previously_removed && !hit.already_in_graph && (
                    <span style={badgeRemoved}>you removed this — add again?</span>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

const backdrop: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.55)",
  display: "flex",
  alignItems: "flex-start",
  justifyContent: "center",
  paddingTop: "12vh",
  zIndex: 50,
};

const panel: React.CSSProperties = {
  width: "min(680px, 92vw)",
  maxHeight: "70vh",
  overflow: "auto",
  background: "var(--panel)",
  border: "1px solid var(--border-strong)",
  borderRadius: 10,
  padding: 16,
};

const status: React.CSSProperties = { margin: "10px 2px 6px", fontSize: 12, color: "var(--dim)" };

const list: React.CSSProperties = { listStyle: "none", margin: 0, padding: 0 };

const row: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "flex-start",
  gap: 2,
  width: "100%",
  textAlign: "left",
  background: "transparent",
  border: "1px solid transparent",
  borderRadius: 6,
  padding: "8px 10px",
};

const meta: React.CSSProperties = { fontSize: 12, color: "var(--dim)" };

const forcePrompt: React.CSSProperties = {
  fontSize: 12,
  color: "var(--muted)",
  background: "#2d3748",
  border: "1px solid var(--border-strong)",
  borderRadius: 6,
  padding: "10px 12px",
  margin: "4px 0 10px",
};

const badgeBase: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  borderRadius: 10,
  padding: "1px 8px",
};

const badgeIn: React.CSSProperties = { ...badgeBase, background: "#2b6cb0", color: "#bee3f8" };
const badgeRemoved: React.CSSProperties = { ...badgeBase, background: "#742a2a", color: "#fed7d7" };
