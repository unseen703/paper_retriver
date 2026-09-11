/**
 * `<BulkImport>` — a reading list in, seeds out.
 *
 * One title per line. The file is read in the browser and never uploaded:
 * PLAN.md §G lists "bulk-import endpoints" under *Deliberately not built*, and
 * that holds. This loops the existing `GET /search` + `POST /nodes`, which
 * already have the partial-failure semantics, the progress reporting and the
 * rate-limit accounting a bulk route would have to grow from scratch.
 *
 * **Strictly sequential, and that is the feature.** Semantic Scholar allows
 * about one request a second with a key. Firing forty searches at once would
 * earn forty 429s and import nothing, so the loop waits — and because a
 * forty-line file therefore takes over a minute, it reports progress per title
 * and can be stopped.
 *
 * **Every line gets an outcome.** A run that says "34 added" and silently
 * drops six is the failure that matters here: the six are exactly the papers
 * the user will later assume are in the graph. Not-found, already-present and
 * filtered-out are different answers and are shown as different answers.
 */
import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "../api/client";
import { parseTitles, summaryLine, type ImportResult } from "./readingList";

export interface BulkImportProps {
  sessionId: number;
}

const OUTCOME_COLOR: Record<ImportResult["outcome"], string> = {
  added: "var(--ok, #48bb78)",
  duplicate: "var(--dim)",
  not_found: "var(--warn, #d69e2e)",
  rejected: "var(--warn, #d69e2e)",
  error: "var(--danger, #e53e3e)",
};

const OUTCOME_LABEL: Record<ImportResult["outcome"], string> = {
  added: "added",
  duplicate: "already in",
  not_found: "not found",
  rejected: "filtered",
  error: "failed",
};

/** Classify one failure. The status code is the signal; the body explains it. */
function outcomeOf(error: unknown): { outcome: ImportResult["outcome"]; detail: string | null } {
  if (error instanceof ApiError) {
    // 409: the paper is already a node here. Not a failure — the list simply
    // overlapped the graph, which is the normal case on a second run.
    if (error.status === 409) return { outcome: "duplicate", detail: null };
    // 422: a filter refused it, and `reason_code` says which rule. Worth
    // surfacing per row, because "filtered out" without the rule is a dead end.
    if (error.status === 422) return { outcome: "rejected", detail: error.reasonCode };
    return { outcome: "error", detail: error.message };
  }
  return { outcome: "error", detail: error instanceof Error ? error.message : String(error) };
}

export function BulkImport({ sessionId }: BulkImportProps) {
  const queryClient = useQueryClient();
  const [titles, setTitles] = useState<string[]>([]);
  const [filename, setFilename] = useState<string | null>(null);
  const [results, setResults] = useState<ImportResult[]>([]);
  const [running, setRunning] = useState(false);
  const [parseError, setParseError] = useState<string | null>(null);
  const [focused, setFocused] = useState(false);
  // A ref, not state: the loop below reads it between awaits, and a state
  // value captured in that closure would stay false however many times the
  // button was pressed.
  const stopped = useRef(false);

  async function handleFile(file: File): Promise<void> {
    setParseError(null);
    setResults([]);
    try {
      const parsed = parseTitles(await file.text());
      setTitles(parsed);
      setFilename(file.name);
      if (parsed.length === 0) {
        setParseError("That file has no titles in it — one per line, blank lines and # ignored.");
      }
    } catch (error) {
      setParseError(error instanceof Error ? error.message : "Could not read that file.");
    }
  }

  async function run(): Promise<void> {
    stopped.current = false;
    setRunning(true);
    setResults([]);
    const collected: ImportResult[] = [];

    for (const title of titles) {
      if (stopped.current) break;
      try {
        const hits = await api.search(sessionId, title, 1);
        if (hits.length === 0) {
          collected.push({ title, outcome: "not_found", detail: null });
        } else {
          await api.addNode(sessionId, hits[0].s2_paper_id);
          collected.push({ title, outcome: "added", detail: null });
        }
      } catch (error) {
        collected.push({ title, ...outcomeOf(error) });
      }
      // Replaced rather than appended so React sees a new array; the rows are
      // few and the run is paced by the network, so the copy costs nothing.
      setResults([...collected]);
    }

    setRunning(false);
    if (collected.some((r) => r.outcome === "added")) {
      queryClient.invalidateQueries({ queryKey: ["graph", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["stats", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["candidates", sessionId] });
    }
  }

  const done = results.length;

  return (
    <section aria-label="Import a list of titles" style={{ fontSize: 12, marginTop: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <label
          style={{
            border: "1px solid var(--track, #4a5568)",
            borderRadius: 3,
            padding: "2px 8px",
            cursor: running ? "default" : "pointer",
            opacity: running ? 0.5 : 1,
            // The input itself is invisible, so the focus ring has to live on
            // the label or a keyboard user cannot see where they are.
            outline: focused ? "2px solid var(--accent, #4299e1)" : "none",
            outlineOffset: 1,
          }}
        >
          Choose .txt…
          <input
            type="file"
            accept=".txt,text/plain"
            disabled={running}
            // **Visually hidden, not `display: none`.** A `display: none` input
            // is unfocusable, and the dialog's focus trap selects
            // `input:not([disabled])` — so it counted this one, tried to focus
            // it, failed, and let Tab escape the modal entirely. Clipping keeps
            // it in the tab order where both the trap and the keyboard expect.
            style={{
              position: "absolute",
              width: 1,
              height: 1,
              padding: 0,
              margin: -1,
              overflow: "hidden",
              clip: "rect(0,0,0,0)",
              whiteSpace: "nowrap",
              border: 0,
            }}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onChange={(event) => {
              const file = event.target.files?.[0];
              // Cleared so choosing the same file twice fires a change event;
              // otherwise a corrected file with the same name does nothing.
              event.target.value = "";
              if (file) void handleFile(file);
            }}
          />
        </label>

        {filename && (
          <span style={{ color: "var(--dim)" }}>
            {filename} · {titles.length} title{titles.length === 1 ? "" : "s"}
          </span>
        )}

        {titles.length > 0 && !running && (
          <button type="button" onClick={() => void run()}>
            Add {titles.length}
          </button>
        )}

        {running && (
          <>
            <span role="status" style={{ color: "var(--dim)" }}>
              {done} of {titles.length}…
            </span>
            <button
              type="button"
              onClick={() => {
                stopped.current = true;
              }}
              // The papers already added stay. They cost real API calls, and a
              // stop that undid them would be a different button.
              title="Stop — papers already added are kept"
            >
              Stop
            </button>
          </>
        )}
      </div>

      <p style={{ margin: "6px 0 0", color: "var(--dim)", lineHeight: 1.4 }}>
        One title per line. Blank lines and lines starting with <code>#</code> are ignored.
        Titles are looked up one a second, because that is the API's limit.
      </p>

      {parseError && (
        <p role="alert" style={{ margin: "6px 0 0", color: "var(--danger, #e53e3e)" }}>
          {parseError}
        </p>
      )}

      {results.length > 0 && (
        <>
          <p style={{ margin: "8px 0 4px" }}>{summaryLine(results)}</p>
          <ul
            style={{
              listStyle: "none",
              margin: 0,
              padding: 0,
              maxHeight: 160,
              overflowY: "auto",
            }}
          >
            {results.map((result) => (
              <li
                key={result.title}
                style={{ display: "flex", gap: 8, padding: "1px 0" }}
                title={result.detail ?? undefined}
              >
                <span style={{ color: OUTCOME_COLOR[result.outcome], minWidth: 66 }}>
                  {OUTCOME_LABEL[result.outcome]}
                </span>
                <span
                  style={{
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                    color: result.outcome === "added" ? "inherit" : "var(--dim)",
                  }}
                >
                  {result.title}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
