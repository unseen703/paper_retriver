/**
 * Parsing a reading-list file.
 *
 * Journey:
 *
 *     As someone with a list of forty papers in a text file, I want to drop it
 *     in and get them into the graph, rather than pasting titles one at a time
 *     into a search box.
 *
 * The rules here all exist because a hand-maintained list is messy in
 * predictable ways: it has blank lines, it has comments, it repeats itself,
 * and if it came out of Notepad it starts with a byte-order mark nobody can
 * see. Each of those costs a rate-limited API call to discover at runtime, so
 * they are cheaper to handle here.
 */
import { describe, expect, it } from "vitest";
import { parseTitles, summarize, summaryLine, type ImportResult } from "./readingList";

function result(outcome: ImportResult["outcome"], title = "t"): ImportResult {
  return { title, outcome, detail: null };
}

describe("parseTitles", () => {
  it("takes one title per line", () => {
    expect(parseTitles("Attention is All You Need\nBERT")).toEqual([
      "Attention is All You Need",
      "BERT",
    ]);
  });

  it("trims surrounding whitespace", () => {
    // Never meaningful, and it changes the search cache key — the same reason
    // the search endpoint trims a pasted title before using it.
    expect(parseTitles("  Attention  \n\tBERT\t")).toEqual(["Attention", "BERT"]);
  });

  it("drops blank lines", () => {
    expect(parseTitles("A\n\n\nB\n   \n")).toEqual(["A", "B"]);
  });

  it("drops comment lines", () => {
    expect(parseTitles("# transformers\nA\n# chemistry\nB")).toEqual(["A", "B"]);
  });

  it("handles Windows line endings", () => {
    expect(parseTitles("A\r\nB\r\n")).toEqual(["A", "B"]);
  });

  it("strips a byte-order mark", () => {
    /**
     * Notepad writes one and it is invisible. Left in place it becomes part of
     * the first title, so exactly one search fails — the first — for a reason
     * that cannot be seen by looking at the file.
     */
    expect(parseTitles("﻿Attention\nBERT")).toEqual(["Attention", "BERT"]);
  });

  it("drops repeated titles, case-insensitively", () => {
    // A list assembled from several sources repeats itself, and every repeat
    // costs a call against a rate-limited API to learn what the first already
    // established.
    expect(parseTitles("Attention\nBERT\nattention\nATTENTION")).toEqual(["Attention", "BERT"]);
  });

  it("keeps the first spelling and the file's order", () => {
    // So the report reads in the same order as the file the user is holding.
    expect(parseTitles("BERT\nAttention\nbert")).toEqual(["BERT", "Attention"]);
  });

  it("returns nothing for an empty or comment-only file", () => {
    expect(parseTitles("")).toEqual([]);
    expect(parseTitles("# just notes\n\n# more notes")).toEqual([]);
  });

  it("does not treat a mid-line hash as a comment", () => {
    // "#" only opens a comment at the start of a line; a title may contain one.
    expect(parseTitles("Learning to rank #2 results")).toEqual(["Learning to rank #2 results"]);
  });
});

describe("summarize", () => {
  it("counts every outcome, including the zeroes", () => {
    // "nothing was rejected" and "rejections were not counted" must not look
    // the same — the same rule <StatsPanel> follows for its zero states.
    const counts = summarize([result("added"), result("added"), result("not_found")]);
    expect(counts).toEqual({ added: 2, duplicate: 0, not_found: 1, rejected: 0, error: 0 });
  });

  it("counts nothing for an empty run", () => {
    expect(summarize([])).toEqual({
      added: 0,
      duplicate: 0,
      not_found: 0,
      rejected: 0,
      error: 0,
    });
  });
});

describe("summaryLine", () => {
  it("reports only what happened", () => {
    expect(summaryLine([result("added"), result("added"), result("not_found")])).toBe(
      "2 added · 1 not found",
    );
  });

  it("is empty when nothing ran", () => {
    expect(summaryLine([])).toBe("");
  });

  it("names every outcome when all of them occurred", () => {
    const line = summaryLine([
      result("added"),
      result("duplicate"),
      result("not_found"),
      result("rejected"),
      result("error"),
    ]);
    expect(line).toBe(
      "1 added · 1 already in the graph · 1 not found · 1 filtered out · 1 failed",
    );
  });
});
