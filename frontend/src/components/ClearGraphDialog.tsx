/**
 * Clear the graph (R2.15).
 *
 * BUILD.md: "UI requires typing 'clear'". That is a deliberately awkward
 * control, and the awkwardness is the feature — a confirm dialog you can
 * dismiss with a reflexive Enter is not a confirmation, it is a speed bump you
 * learn to jump. Typing the word requires reading the sentence above it.
 *
 * **The dialog says what survives, not just what goes.** "This cannot be
 * undone" is true of most of the graph and false of the expensive part: the
 * corpus stays, so re-seeding the same papers afterwards costs no API calls.
 * Someone hesitating over this button is usually worried about the wrong
 * thing, and the fix is to tell them.
 *
 * **The node count is in the sentence.** Clearing 4 papers and clearing 400
 * are different decisions, and the only place that difference is visible is
 * right here.
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

/** BUILD.md names the word. Lower-cased on comparison, not on input — the
 *  user should see exactly what they typed. */
const REQUIRED_WORD = "clear";

export interface ClearGraphDialogProps {
  sessionId: number;
  /** Shown in the sentence, so the size of the decision is visible. */
  nodeCount: number;
  onClose: () => void;
}

export function ClearGraphDialog({ sessionId, nodeCount, onClose }: ClearGraphDialogProps) {
  const [typed, setTyped] = useState("");
  const queryClient = useQueryClient();

  const clear = useMutation({
    mutationFn: () => api.clearGraph(sessionId),
    onSuccess: () => {
      // Everything that describes the graph is now wrong.
      queryClient.invalidateQueries({ queryKey: ["graph", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["stats", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["review", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["search", sessionId] });
      onClose();
    },
  });

  const armed = typed.trim().toLowerCase() === REQUIRED_WORD;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Clear the graph"
      style={{
        position: "absolute",
        top: "20%",
        left: "50%",
        transform: "translateX(-50%)",
        width: 420,
        padding: 18,
        background: "var(--panel, #1a202c)",
        border: "1px solid var(--line, #2d3748)",
        borderRadius: 8,
        zIndex: 20,
      }}
    >
      <h2 style={{ margin: "0 0 8px", fontSize: 14 }}>Clear the graph?</h2>

      <p style={{ fontSize: 12, lineHeight: 1.45, color: "var(--muted)" }}>
        This removes all <strong>{nodeCount}</strong> papers from this session&rsquo;s graph, along
        with your labels and the arrangement you have built.
      </p>
      <p style={{ fontSize: 12, lineHeight: 1.45, color: "var(--dim)" }}>
        {/* The reassurance is the useful half: the thing people fear losing is
            the thing that does not go. */}
        The papers themselves are kept, with their citations and everything already fetched from
        Semantic Scholar — so building a new graph over them costs no API calls.
      </p>

      <label style={{ display: "block", fontSize: 12, margin: "12px 0 4px" }}>
        Type <code>{REQUIRED_WORD}</code> to confirm
        <input
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
          // Autofocus is safe here precisely because the field does nothing
          // until it contains the right word.
          autoFocus
          aria-label={`Type ${REQUIRED_WORD} to confirm`}
          style={{ display: "block", width: "100%", marginTop: 4 }}
        />
      </label>

      {clear.isError && (
        <p role="status" style={{ color: "salmon", fontSize: 12 }}>
          The graph could not be cleared. Nothing was removed.
        </p>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 12 }}>
        <button type="button" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          onClick={() => clear.mutate()}
          disabled={!armed || clear.isPending}
          style={{ color: armed ? "salmon" : undefined }}
        >
          {clear.isPending ? "Clearing…" : "Clear graph"}
        </button>
      </div>
    </div>
  );
}
