/**
 * `<CandidateList>` (R3.b) -- the ranked table.
 *
 * PLAN.md section F, stated plainly:
 *
 *     "`<CandidateList>` is the product. A force-directed graph is excellent
 *     for understanding *structure* and terrible for reading a ranked list.
 *     Users will make most decisions from the table."
 *
 * **This component does not sort.** It renders the order the server sent and
 * changes the order by asking again. The server's `_sort_key` carries three
 * rules that each have a plausible wrong version -- unknown scores sort last
 * rather than first, unknown is not zero, ties break on `paper_id` -- and
 * re-implementing them here would be a second definition of "ranked", free to
 * agree today and drift later. A table that looks perfectly sorted while
 * disagreeing with the canvas and the inspector is a bad way to find that out.
 *
 * **A missing score renders as a dash, not 0.00.** `hub` carries a negative
 * weight, so genuine scores go below zero; showing an unscored paper as zero
 * would place it in the middle of a range it is not part of.
 *
 * **Selection is shared with the canvas.** Clicking a row sets the same
 * `selectedId` the graph uses, so the table and the picture are always talking
 * about the same paper. That is the entire reason this is worth building
 * rather than reading the graph.
 *
 * On `aria-selected` over `role="grid"`: a grid promises full two-dimensional
 * arrow-key navigation between cells, which this does not implement. A native
 * table with selectable rows is the honest description of what this is.
 */
import { memo, useCallback, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, type CandidateOut, type CandidateSort } from "../api/client";

/** Column header label -> the `sort` value the server understands. */
const SORTABLE: ReadonlyArray<{ key: CandidateSort; label: string }> = [
  { key: "score", label: "score" },
  { key: "year", label: "year" },
  { key: "citations", label: "cites" },
];

const DEFAULT_LIMIT = 50;

const cell: React.CSSProperties = {
  padding: "3px 8px",
  whiteSpace: "nowrap",
  fontVariantNumeric: "tabular-nums",
};

/**
 * The title column absorbs whatever width is left over.
 *
 * It had a fixed `maxWidth` first, which pushed year, venue and cites off the
 * right edge of the panel — the table was wider than its container and the
 * columns that make a row comparable were the ones that fell off. A fixed
 * layout with an elastic title column is what keeps every column on screen at
 * any panel width.
 */
const titleCell: React.CSSProperties = {
  padding: "3px 8px",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const headerButton: React.CSSProperties = {
  background: "none",
  border: "none",
  color: "inherit",
  font: "inherit",
  cursor: "pointer",
  padding: 0,
};

/** `—` rather than a number: absent is not a value. See the module note. */
function formatScore(score: number | null | undefined): string {
  return score == null ? "—" : score.toFixed(2);
}

/**
 * A sortable column header.
 *
 * `aria-sort` is `descending` whenever this column is the active one: every
 * ordering this endpoint offers is best-first, which is what "ranked" means.
 * Without it a screen reader has no way to learn the table is ordered at all.
 */
function SortHeader({
  column,
  sort,
  onSort,
  align,
}: {
  column: CandidateSort;
  sort: CandidateSort;
  onSort: (next: CandidateSort) => void;
  align: "left" | "right";
}) {
  const label = SORTABLE.find((entry) => entry.key === column)?.label ?? column;
  return (
    <th
      scope="col"
      aria-sort={sort === column ? "descending" : "none"}
      style={{ ...cell, textAlign: align }}
    >
      <button type="button" style={headerButton} onClick={() => onSort(column)}>
        {label}
      </button>
    </th>
  );
}

interface RowProps {
  candidate: CandidateOut;
  selected: boolean;
  onSelect: (id: number) => void;
}

/**
 * Memoized so that changing the selection re-renders two rows rather than all
 * of them. At the 500-row cap the difference is the table feeling immediate
 * versus visibly lagging behind the click that caused it.
 */
const Row = memo(function Row({ candidate, selected, onSelect }: RowProps) {
  const choose = () => onSelect(candidate.id);
  return (
    <tr
      aria-selected={selected}
      tabIndex={0}
      onClick={choose}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          choose();
        }
      }}
      style={{
        cursor: "pointer",
        background: selected ? "var(--sel, #2b3a55)" : undefined,
      }}
    >
      <td style={{ ...cell, textAlign: "right" }}>{formatScore(candidate.score)}</td>
      <td style={titleCell} title={candidate.title}>
        {candidate.title}
      </td>
      <td style={{ ...cell, textAlign: "right", color: "var(--dim)" }}>
        {candidate.year ?? "—"}
      </td>
      <td style={{ ...titleCell, color: "var(--dim)" }} title={candidate.venue ?? ""}>
        {candidate.venue ?? "—"}
      </td>
      <td style={{ ...cell, textAlign: "right", color: "var(--dim)" }}>
        {candidate.citation_count?.toLocaleString() ?? "—"}
      </td>
    </tr>
  );
});

export interface CandidateListProps {
  sessionId: number;
  selectedId: number | null;
  onSelect: (id: number) => void;
  /** Skip fetching while the fixture graph is on screen — its ids are invented. */
  disabled?: boolean;
}

export function CandidateList({
  sessionId,
  selectedId,
  onSelect,
  disabled,
}: CandidateListProps) {
  const [sort, setSort] = useState<CandidateSort>("score");

  const candidates = useQuery({
    queryKey: ["candidates", sessionId, sort],
    queryFn: () => api.candidates(sessionId, sort, DEFAULT_LIMIT),
    enabled: !disabled,
    // Changing the sort is a new query key, so without this the table blanks
    // to "Ranking…" on every header click. Re-sorting is the most common thing
    // anyone does here, and an empty table is the same shape as "no
    // candidates" — a different and much more alarming message.
    placeholderData: keepPreviousData,
  });

  // Stable identity, so `Row`'s memo is not defeated by a new function every
  // render -- which would make the memo pure overhead.
  const handleSelect = useCallback((id: number) => onSelect(id), [onSelect]);

  if (disabled) return null;

  if (candidates.isError) {
    // An empty table and a failed request look identical, and one of them
    // means "expand your graph" while the other means "the server is down".
    return (
      <div role="status" style={{ fontSize: 12, color: "salmon", padding: 8 }}>
        Candidates unavailable.
      </div>
    );
  }

  if (!candidates.data) {
    return (
      <div role="status" style={{ fontSize: 12, color: "var(--dim)", padding: 8 }}>
        Ranking…
      </div>
    );
  }

  const { candidates: rows, total } = candidates.data;

  if (rows.length === 0) {
    return (
      <div role="status" style={{ fontSize: 12, color: "var(--dim)", padding: 8 }}>
        No candidates yet — expand the graph to find some.
      </div>
    );
  }

  return (
    <div style={{ fontSize: 12, overflow: "auto", minHeight: 0 }}>
      <table
        aria-label="Candidates"
        // `fixed` so the columns obey the widths below instead of being sized
        // by their content — a long title would otherwise widen the table past
        // the panel and push the numeric columns out of view.
        style={{ borderCollapse: "collapse", width: "100%", tableLayout: "fixed" }}
      >
        <colgroup>
          <col style={{ width: 46 }} />
          {/* title: everything left over */}
          <col />
          <col style={{ width: 42 }} />
          <col style={{ width: 74 }} />
          <col style={{ width: 54 }} />
        </colgroup>
        <thead>
          {/* Header and body must declare the same columns in the same order.
              Venue is deliberately a plain header: the server offers no venue
              sort, and a button that does nothing is worse than a label. */}
          <tr>
            <SortHeader column="score" sort={sort} onSort={setSort} align="right" />
            <th scope="col" style={{ ...cell, textAlign: "left" }}>
              title
            </th>
            <SortHeader column="year" sort={sort} onSort={setSort} align="right" />
            <th scope="col" style={{ ...cell, textAlign: "left" }}>
              venue
            </th>
            <SortHeader column="citations" sort={sort} onSort={setSort} align="right" />
          </tr>
        </thead>
        <tbody>
          {rows.map((candidate) => (
            <Row
              key={candidate.id}
              candidate={candidate}
              selected={candidate.id === selectedId}
              onSelect={handleSelect}
            />
          ))}
        </tbody>
      </table>

      <div role="status" style={{ color: "var(--dim)", padding: "4px 8px" }}>
        {rows.length < total
          ? `Showing top ${rows.length} of ${total} candidates`
          : `${total} candidate${total === 1 ? "" : "s"}`}
      </div>
    </div>
  );
}
