# Citation-graph paper recommender

Find the papers you should be reading, by walking the citation graph outward
from the ones you already trust.

You give it a few seed papers. It fetches their references and citations from
Semantic Scholar, filters out what is structurally related but not useful to
you — datasets, pre-2015 work, applied-CV papers, low-signal surveys — ranks
what is left, and draws the result as a graph you can explore.

Local-first, single user, one SQLite file. No account, no server to deploy, no
cloud anything.

---

## Status

**R1 complete** — the walking skeleton works end to end. You can seed a paper,
expand its neighbourhood, and explore the result in the browser.

Not built yet: labelling (like / dislike / remove), garbage collection, real
ranking features, and the evaluation harness. Those are R2–R4;
[`docs/BUILD.md`](docs/BUILD.md) is the task queue.

---

## What it looks like

> **The GIF that belongs here has not been recorded.** BUILD.md R1.23 asks for
> a recording of the 90-second flow — add a paper, expand twice, click a node
> — and screen capture has to be done by a person running the app. Everything
> it would show is described under [Using it](#using-it).

---

## Quick start

Requires Python 3.11+ with [`uv`](https://docs.astral.sh/uv/), and Node 20+.

```bash
uv sync && cd frontend && npm install && cd ..
```

Put your Semantic Scholar API key in `.env` (gitignored, never committed):

```bash
S2_API_KEY = "your-key-here"
```

Create the database:

```bash
uv run alembic -c backend/migrations/alembic.ini upgrade head
```

The arXiv category filter needs the Kaggle arXiv metadata dump. Without it
every paper falls through to weaker signals and the `cs.CV` deny rule never
fires:

```bash
uv run python scripts/load_arxiv_meta.py
```

Then run the two halves, in separate shells:

```bash
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --app-dir backend
```

```bash
cd frontend && npm run dev
```

Open <http://localhost:5173>.

> Port 5173 is pinned deliberately: it is the only origin the backend's CORS
> allowlist permits. Vite refuses to start rather than silently moving to 5174
> and leaving every request blocked in the browser with nothing in the server
> log.

---

## Using it

**Add a paper.** Search by a distinctive fragment of the title rather than the
whole thing — Semantic Scholar's relevance search degrades badly on long exact
strings. If the filters reject it you are told which rule fired, and offered
the override against that named rule.

**Expand.** One hop, synchronously. The result line reports what actually
happened — `added 20 of 20 · 102 boundary · 3 API calls` — because "added 0 of
20 from a pool of 34" and "added 0 of 20 from a pool of 0" are completely
different problems, and a bare success message hides both.

**Explore.** Drag a node and the network follows it. Hover to light a paper's
neighbourhood and fade the rest. Click to pin that view and open the inspector,
which lists every connected paper with the direction of the citation. Nodes
drift gently while idle and hold still once you select one.

Node **size** is citations, **shade** is publication year, **colour** is
state. The score slider hides weak candidates; the label modes trade
readability against completeness.

Everything works from the keyboard: tab to the graph, arrows to move between
papers, Escape to clear.

---

## The CLI

The web app is not the only way in, and the CLI stays useful — it is how the
build checkpoints are verified.

```bash
uv run python -m app.cli show
```

```bash
uv run python -m app.cli seed "Reflexion: Language Agents with Verbal" --expand
```

```bash
uv run python -m app.cli filter-report --title "ReAct: Synergizing Reasoning and Acting" --limit 50
```

`filter-report` prints the accept/reject verdict for every reference of a
paper, with the rule that decided each one. It is the fastest way to find out
why the recommendations look wrong.

---

## Design notes worth knowing before reading the code

**The corpus is not the graph.** A paper can be stored with all its edges and
still not be drawn. Pre-2015 papers are the clearest case: the year floor is a
*graph-admission* rule, not an ingestion rule, because two modern papers that
both cite Adam (2014) are related *through* Adam, and dropping Adam makes that
relationship invisible. A representative graph here holds 127 nodes drawn from
229 stored papers — the other 102 are boundary papers and rejects, all still
doing structural work.

**Facts are global, opinions are session-scoped.** Papers, edges and authors
are shared across every session. Membership, state, score and position are
per-session. That is why seeding a second session with an already-crawled
paper costs zero API calls.

**Nothing silently re-enters the graph.** Removal writes a tombstone, and
expansion excludes tombstoned papers. Getting one back is an explicit restore,
never an accident.

**One request per second.** The whole design bends around the Semantic Scholar
rate limit: batch-first fetching, an immutable response cache, and a per-
expansion API budget that stops cleanly rather than overrunning. A partial
expansion is a success, and says so.

More in [`docs/PLAN.md`](docs/PLAN.md) (architecture),
[`docs/graph-model.md`](docs/graph-model.md) (definitions, verbatim), and
[`docs/adr/`](docs/adr/) (decisions and what would reverse them).

---

## Development

```bash
uv run python scripts/verify.py
```

One command for lint, format, typecheck and tests. It exists because
PowerShell 5.1 treats `a && b` as a parse error, so chaining the steps by hand
fails on Windows — and it runs everything before reporting rather than
stopping at the first failure.

```bash
cd frontend && npm run types
```

Regenerates the frontend's API types from the running backend's OpenAPI
schema. **Never hand-write them**: a hand-written type that drifts from the
server compiles perfectly and fails at runtime, which is the whole failure
mode generation removes.

The test suite runs with the network genuinely unavailable. `CachedOnlyS2Client`
installs a transport that raises, so an uncached request is a structural
impossibility rather than a mocking convention.

---

## Layout

```
backend/app/
  api/          HTTP only — nothing here writes SQL
  services/     the pipeline: filters, dedup, pooling, budget, expansion
  repo/         the only place SQL lives
  clients/      Semantic Scholar, arXiv, the response cache
frontend/src/
  components/   GraphCanvas (Cytoscape), dialog, inspector, controls
  api/          the generated types and the one fetch wrapper
docs/           PLAN.md (architecture), BUILD.md (task queue), adr/
legacy/         the archived Flask prototype this replaces
```
