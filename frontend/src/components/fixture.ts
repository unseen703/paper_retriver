/**
 * BUILD.md R1.21's verification fixture: "hardcode a 5-node fixture; confirm
 * colours differ by state and the layout doesn't overlap".
 *
 * It stays in the tree after the canvas is wired to live data, because it is
 * the only way to see every state at once -- LIKED and DISLIKED cannot exist
 * until R2 ships labelling, so a live graph renders three of the five styles
 * and silently proves nothing about the other two.
 */
import type { GraphEdgeOut, GraphNodeOut } from "../api/client";

export const FIXTURE_NODES: GraphNodeOut[] = [
  {
    id: 1,
    title: "Attention Is All You Need",
    state: "SEED",
    score: null,
    year: 2017,
    citation_count: 191_436,
    paper_type: "RESEARCH",
    in_degree: 0,
    out_degree: 2,
    pos: null,
  },
  {
    id: 2,
    title: "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
    state: "LIKED",
    score: 3.8,
    year: 2018,
    citation_count: 82_000,
    paper_type: "RESEARCH",
    in_degree: 1,
    out_degree: 1,
    pos: null,
  },
  {
    id: 3,
    title: "A Comprehensive Survey on Graph Neural Networks",
    state: "CANDIDATE",
    score: 2.4,
    year: 2019,
    citation_count: 7_800,
    // Diamond, whatever its colour -- shape carries type so meaning never
    // rests on colour alone.
    paper_type: "SURVEY",
    in_degree: 1,
    out_degree: 0,
    pos: null,
  },
  {
    id: 4,
    title: "ImageNet: A Large-Scale Hierarchical Image Database",
    state: "DISLIKED",
    score: 0.9,
    year: 2009,
    citation_count: 55_000,
    paper_type: "DATASET",
    in_degree: 1,
    out_degree: 0,
    pos: null,
  },
  {
    id: 5,
    title: "ReAct: Synergizing Reasoning and Acting in Language Models",
    state: "CANDIDATE",
    // A low score, so the CANDIDATE colour ramp is visibly a ramp rather than
    // one flat orange.
    score: 0.4,
    year: 2022,
    citation_count: 3_100,
    paper_type: "RESEARCH",
    in_degree: 1,
    out_degree: 0,
    pos: null,
  },
];

export const FIXTURE_EDGES: GraphEdgeOut[] = [
  { source: 2, target: 1, is_influential: true },
  { source: 3, target: 1, is_influential: false },
  { source: 4, target: 2, is_influential: false },
  { source: 5, target: 2, is_influential: true },
];
