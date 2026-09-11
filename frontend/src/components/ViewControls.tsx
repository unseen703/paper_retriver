/**
 * `<ViewControls>` -- the prototype's canvas controls, ported.
 *
 * Four things, all of which answer a question the graph alone cannot:
 *
 *   label mode      hidden / all / relevant. Thirty-seven titles at once is
 *                   unreadable, none at all is unnavigable, and which you want
 *                   depends on whether you are reading the graph or its shape.
 *   score threshold hide weak candidates. The prototype's slider, including
 *                   the "N/M papers visible" readout -- without the count the
 *                   slider silently destroys graph and you cannot tell how much.
 *   title search    find a paper you know is in there. Match count included
 *                   for the same reason: "no matches" and "the box is broken"
 *                   look identical otherwise.
 *   reset zoom      get back after exploring.
 *
 * Labelled nodes are never hidden by the threshold, so a seed you added by
 * hand cannot vanish because it has no score.
 */
import type { LabelMode } from "./stylesheet";
import type { TopicGroup } from "./graphInteraction";

/**
 * The topic filter's three settings.
 *
 * Coarse buckets rather than a list of arXiv categories: the question is "show
 * me the chemistry side of this graph", not "show me cond-mat.mtrl-sci". A
 * dropdown of forty categories answers a question nobody asked while making
 * the common one harder.
 */
const TOPICS: { value: TopicGroup; label: string; title: string }[] = [
  { value: "all", label: "all", title: "Every paper in the graph" },
  { value: "cs", label: "CS / ML", title: "cs.*, stat.*, math.* — the machine-learning side" },
  {
    value: "chem",
    label: "chemistry",
    title: "Retrosynthesis, reaction and reactivity prediction, catalysis",
  },
  {
    value: "biochem",
    label: "biochem",
    title:
      "Metabolic pathways, enzyme function and active sites, binding and docking — q-bio.* too",
  },
];

export interface ViewControlsProps {
  labelMode: LabelMode;
  onLabelMode: (mode: LabelMode) => void;
  scoreThreshold: number;
  onScoreThreshold: (value: number) => void;
  /** The graph's own score span, so the slider's travel is all usable. */
  scoreRange: { min: number; max: number };
  searchQuery: string;
  topicGroup: TopicGroup;
  onTopicGroup: (group: TopicGroup) => void;
  /** How many nodes are in the chosen group, for the count beside the buttons. */
  topicCount: number;
  onSearchQuery: (value: string) => void;
  onResetView: () => void;
  visible: number;
  total: number;
  matches: number;
}

const MODES: { value: LabelMode; label: string; title: string }[] = [
  { value: "hidden", label: "none", title: "Hide every label — read the shape" },
  { value: "relevant", label: "relevant", title: "Seeds, labelled papers, and strong candidates" },
  { value: "all", label: "all", title: "Every label — dense, but complete" },
];

export function ViewControls({
  labelMode,
  onLabelMode,
  scoreThreshold,
  onScoreThreshold,
  scoreRange,
  searchQuery,
  topicGroup,
  onTopicGroup,
  topicCount,
  onSearchQuery,
  onResetView,
  visible,
  total,
  matches,
}: ViewControlsProps) {
  return (
    <div style={bar}>
      <span style={group}>
        <span style={dim}>labels</span>
        {MODES.map((mode) => (
          <button
            key={mode.value}
            onClick={() => onLabelMode(mode.value)}
            title={mode.title}
            aria-pressed={labelMode === mode.value}
            style={labelMode === mode.value ? activeBtn : undefined}
          >
            {mode.label}
          </button>
        ))}
      </span>

      {/* The topic filter. Beside `labels` because both answer "what am I
          looking at", rather than `min score`, which answers "how much". */}
      <span style={group}>
        <span style={dim}>topic</span>
        {TOPICS.map((topic) => (
          <button
            key={topic.value}
            onClick={() => onTopicGroup(topic.value)}
            title={topic.title}
            aria-pressed={topicGroup === topic.value}
            style={topicGroup === topic.value ? activeBtn : undefined}
          >
            {topic.label}
          </button>
        ))}
        {topicGroup !== "all" && (
          // The count is the useful part: a filter that shows nothing and a
          // filter that is broken look identical without it.
          <span style={dim}>{topicCount} shown</span>
        )}
      </span>

      <span style={group}>
        <label style={dim} htmlFor="score-threshold">
          min score
        </label>
        {/* Bounded by the graph's own scores rather than a fixed 0..4. R1's
            prescores land in a narrow band -- 2.01 to 2.49 on the current
            graph -- so a fixed scale left roughly 88% of the slider's travel
            doing nothing at all, and the first 12% doing everything. Same
            failure as a fixed year ramp, same fix. */}
        <input
          id="score-threshold"
          type="range"
          min={scoreRange.min}
          max={scoreRange.max}
          step={Math.max((scoreRange.max - scoreRange.min) / 100, 0.001)}
          value={scoreThreshold}
          disabled={scoreRange.max <= scoreRange.min}
          onChange={(event) => onScoreThreshold(Number(event.target.value))}
          style={{ width: 110, padding: 0 }}
        />
        {/* The count is the point of the slider. Dragging it without one just
            makes papers disappear. */}
        <span style={dim}>
          {scoreThreshold.toFixed(2)} · {visible}/{total} shown
        </span>
      </span>

      <span style={group}>
        <input
          type="search"
          value={searchQuery}
          onChange={(event) => onSearchQuery(event.target.value)}
          placeholder="Find in graph…"
          aria-label="Find a paper in the graph by title"
          style={{ width: 150 }}
        />
        {searchQuery.trim() !== "" && (
          <span style={{ ...dim, color: matches > 0 ? "#48bb78" : "salmon" }}>
            {matches > 0 ? `${matches} match${matches === 1 ? "" : "es"}` : "no matches"}
          </span>
        )}
      </span>

      <button onClick={onResetView} title="Fit the whole graph in view">
        Reset zoom
      </button>
    </div>
  );
}

const bar: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 18,
  flexWrap: "wrap",
  padding: "7px 16px",
  borderBottom: "1px solid var(--border)",
  background: "var(--panel)",
  fontSize: 12,
};

const group: React.CSSProperties = { display: "flex", alignItems: "center", gap: 6 };

const dim: React.CSSProperties = { color: "var(--dim)", whiteSpace: "nowrap" };

const activeBtn: React.CSSProperties = {
  background: "#2b6cb0",
  borderColor: "#2b6cb0",
  color: "#bee3f8",
};
