/**
 * Parsing a reading list, and summarising what happened to it.
 *
 * Pure on purpose — the file reading, the network and the React live in
 * `BulkImport.tsx`, and everything with a rule in it lives here where a test
 * is a function call.
 *
 * **No bulk endpoint.** PLAN.md §G lists "bulk-import endpoints" under
 * *Deliberately not built*, and that decision holds: this loops the existing
 * `GET /search` + `POST /nodes` per title. A server-side bulk route would need
 * its own partial-failure semantics, its own progress reporting and its own
 * rate-limit accounting — three things the single-paper path already has.
 */

/** What became of one line of the file. */
export type ImportOutcome = "added" | "duplicate" | "not_found" | "rejected" | "error";

export interface ImportResult {
  title: string;
  outcome: ImportOutcome;
  /** The filter's reason code, or an error message. Null when it just worked. */
  detail: string | null;
}

/**
 * One title per line, cleaned up.
 *
 * Blank lines and `#` comments go, because a list people maintain by hand
 * acquires both. Surrounding whitespace goes for the same reason a pasted
 * title is trimmed before it reaches the search endpoint: it is never
 * meaningful and it changes the cache key.
 *
 * **Duplicates are dropped case-insensitively.** A list assembled from several
 * sources repeats titles, and each repeat would otherwise cost a search call
 * against a rate-limited API to learn something the first one already
 * established. Order is preserved and the first spelling wins, so the report
 * reads in the same order as the file.
 *
 * A UTF-8 BOM is stripped. Notepad writes one, it is invisible, and it would
 * otherwise become part of the first title and make that one search fail for
 * a reason nobody could see.
 */
export function parseTitles(text: string): string[] {
  const seen = new Set<string>();
  const titles: string[] = [];

  for (const line of text.replace(/^﻿/, "").split(/\r?\n/)) {
    const title = line.trim();
    if (!title || title.startsWith("#")) continue;

    const key = title.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    titles.push(title);
  }
  return titles;
}

/**
 * Counts per outcome, for the one-line summary.
 *
 * Every outcome is reported, including the zeroes the caller asks for, because
 * "nothing was rejected" and "rejections were not counted" must not look the
 * same — the same reason `<StatsPanel>` renders its zero states.
 */
export function summarize(results: readonly ImportResult[]): Record<ImportOutcome, number> {
  const counts: Record<ImportOutcome, number> = {
    added: 0,
    duplicate: 0,
    not_found: 0,
    rejected: 0,
    error: 0,
  };
  for (const result of results) counts[result.outcome] += 1;
  return counts;
}

/** The summary line: only the outcomes that happened, in a fixed order. */
export function summaryLine(results: readonly ImportResult[]): string {
  const counts = summarize(results);
  const parts: string[] = [];
  if (counts.added) parts.push(`${counts.added} added`);
  if (counts.duplicate) parts.push(`${counts.duplicate} already in the graph`);
  if (counts.not_found) parts.push(`${counts.not_found} not found`);
  if (counts.rejected) parts.push(`${counts.rejected} filtered out`);
  if (counts.error) parts.push(`${counts.error} failed`);
  return parts.join(" · ");
}
