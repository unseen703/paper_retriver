/**
 * `<NodeInspector>` (R1.22) -- everything known about the selected paper.
 *
 * BUILD.md's list: "title, authors, venue, date, citations, type, categories,
 * in/out degree, depth".
 *
 * Two fields carry more meaning than their labels suggest, so both are
 * annotated in place rather than left for the reader to misread:
 *
 * **in/out degree are within this graph, not the corpus.** A hub with 190k
 * global citations can sit here with in-degree 1. The panel says so, because
 * a reader who assumes otherwise concludes the data is wrong.
 *
 * **crawl state STUB means the metadata was never fetched.** A stub's zero
 * citation count is an absence, not a measurement, and at 5% crawl
 * completeness most of a fresh graph is stubs. Showing "0 citations" without
 * that context invites a false conclusion about the paper.
 *
 * `score_breakdown` is empty until R3, and the panel says that rather than
 * rendering an empty box -- absence with a reason reads as progress, absence
 * without one reads as breakage.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

export interface Neighbour {
  id: number;
  title: string;
  /** BACKWARD means this paper cites the neighbour. */
  direction: "cites" | "cited by";
}

export interface NodeInspectorProps {
  sessionId: number;
  paperId: number | null;
  /** The selected paper's neighbours in the drawn graph, for the jump list. */
  neighbours?: Neighbour[];
  onSelect?: (paperId: number) => void;
  onClose: () => void;
}

export function NodeInspector({
  sessionId,
  paperId,
  neighbours = [],
  onSelect,
  onClose,
}: NodeInspectorProps) {
  const detail = useQuery({
    queryKey: ["node", sessionId, paperId],
    queryFn: () => api.node(sessionId, paperId as number),
    enabled: paperId != null,
  });

  if (paperId == null) return null;

  const paper = detail.data;

  return (
    <aside style={panel} aria-label="Paper details">
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <strong style={{ fontSize: 13, color: "var(--accent)" }}>Paper</strong>
        <button onClick={onClose} aria-label="Close details" style={{ padding: "2px 8px" }}>
          ✕
        </button>
      </div>

      {detail.isPending && <p style={dim}>Loading…</p>}
      {detail.isError && <p style={{ ...dim, color: "salmon" }}>Could not load this paper.</p>}

      {paper && (
        <>
          <h2 style={title}>{paper.title}</h2>

          {(paper.authors?.length ?? 0) > 0 && (
            <p style={{ ...dim, marginTop: 6 }}>{paper.authors!.join(", ")}</p>
          )}

          <dl style={grid}>
            <Row label="state" value={paper.state} />
            <Row
              label="depth"
              value={String(paper.depth)}
              hint={paper.depth === 0 ? "a seed" : `${paper.depth} hop from a seed`}
            />
            {paper.score != null && <Row label="score" value={paper.score.toFixed(2)} />}
            <Row label="year" value={paper.publication_date ?? paper.year ?? "—"} />
            <Row label="venue" value={paper.venue ?? "—"} />
            <Row label="type" value={paper.paper_type ?? "—"} />
            <Row
              label="citations"
              value={(paper.citation_count ?? 0).toLocaleString()}
              // A stub was never fetched, so 0 is an absence rather than a
              // measurement -- and most of a fresh graph is stubs.
              hint={paper.crawl_state === "STUB" ? "not fetched yet — stub" : undefined}
            />
            <Row label="references" value={String(paper.reference_count ?? 0)} />
            <Row
              label="degree"
              value={`in ${paper.in_degree ?? 0} / out ${paper.out_degree ?? 0}`}
              hint="within this graph, not the whole corpus"
            />
            <Row
              label="category"
              value={
                paper.primary_arxiv_category
                  ? `${paper.primary_arxiv_category}${
                      (paper.arxiv_categories?.length ?? 0) > 1
                        ? ` (+${paper.arxiv_categories!.length - 1})`
                        : ""
                    }`
                  : "—"
              }
              hint={
                (paper.arxiv_categories?.length ?? 0) > 1
                  ? paper.arxiv_categories!.join(", ")
                  : undefined
              }
            />
          </dl>

          <div style={{ display: "flex", gap: 10, marginTop: 12, flexWrap: "wrap" }}>
            {paper.arxiv_id && (
              <a style={link} href={`https://arxiv.org/abs/${paper.arxiv_id}`} target="_blank" rel="noreferrer">
                arXiv
              </a>
            )}
            {paper.doi && (
              <a style={link} href={`https://doi.org/${paper.doi}`} target="_blank" rel="noreferrer">
                DOI
              </a>
            )}
            <a
              style={link}
              href={`https://www.semanticscholar.org/paper/${paper.s2_paper_id}`}
              target="_blank"
              rel="noreferrer"
            >
              Semantic Scholar
            </a>
          </div>

          {/* The prototype's neighbour sidebar. Reading a citation graph is
              mostly walking edges, and hunting for a specific small circle on
              a canvas is a bad way to do it -- a list you can click is the
              difference between exploring and squinting. Direction is shown
              because "cites" and "cited by" are different claims. */}
          {neighbours.length > 0 && (
            <>
              <h3 style={sectionHeading}>
                Connected papers <span style={{ color: "var(--dim)" }}>({neighbours.length})</span>
              </h3>
              <ul style={neighbourList}>
                {neighbours.map((n) => (
                  <li key={n.id}>
                    <button style={neighbourRow} onClick={() => onSelect?.(n.id)}>
                      <span style={directionTag(n.direction)}>{n.direction}</span>
                      <span style={{ color: "var(--muted)" }}>{n.title}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}

          <p style={{ ...dim, marginTop: 14, fontSize: 11 }}>
            {Object.keys(paper.score_breakdown ?? {}).length === 0
              ? "Score breakdown arrives at R3, with the real feature set."
              : JSON.stringify(paper.score_breakdown)}
          </p>
        </>
      )}
    </aside>
  );
}

function Row({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <>
      <dt style={{ color: "var(--dim)" }}>{label}</dt>
      <dd style={{ margin: 0, color: "var(--text)" }}>
        {value}
        {hint && <span style={{ color: "var(--dim)", fontSize: 11 }}> · {hint}</span>}
      </dd>
    </>
  );
}

const panel: React.CSSProperties = {
  width: 300,
  flexShrink: 0,
  borderLeft: "1px solid var(--border)",
  background: "var(--panel)",
  padding: 14,
  overflowY: "auto",
};

const title: React.CSSProperties = {
  fontSize: 14,
  lineHeight: 1.35,
  margin: "10px 0 0",
  color: "var(--text)",
};

const dim: React.CSSProperties = { margin: 0, fontSize: 12, color: "var(--dim)" };

const grid: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "auto 1fr",
  gap: "5px 12px",
  margin: "14px 0 0",
  fontSize: 12,
};

const link: React.CSSProperties = { color: "var(--accent)", fontSize: 12 };

const sectionHeading: React.CSSProperties = {
  fontSize: 11,
  textTransform: "uppercase",
  letterSpacing: "0.08em",
  color: "var(--dim)",
  margin: "18px 0 6px",
};

const neighbourList: React.CSSProperties = { listStyle: "none", margin: 0, padding: 0 };

const neighbourRow: React.CSSProperties = {
  display: "flex",
  gap: 6,
  alignItems: "baseline",
  width: "100%",
  textAlign: "left",
  background: "transparent",
  border: "1px solid transparent",
  borderRadius: 4,
  padding: "4px 6px",
  fontSize: 12,
  cursor: "pointer",
};

function directionTag(direction: "cites" | "cited by"): React.CSSProperties {
  return {
    flexShrink: 0,
    fontSize: 10,
    fontWeight: 600,
    borderRadius: 8,
    padding: "0 6px",
    // Outgoing is the paper's own bibliography, incoming is its reception --
    // different colours because they answer different questions.
    background: direction === "cites" ? "#2b6cb0" : "#276749",
    color: direction === "cites" ? "#bee3f8" : "#9ae6b4",
  };
}
