# Citation-network paper recommender

Give it a seed list of paper titles you already like. It resolves them against
Semantic Scholar, pulls their references and citing papers, scores the resulting
candidate pool by how tightly each one is linked to your seeds, and walks you
through the top results in the terminal so you can say yes/no/skip. Anything
you like gets folded back into the seed set for next time.

## Why Semantic Scholar, not just the arXiv API

The arXiv API gives titles/abstracts/categories but **no citation links**.
Semantic Scholar's Graph API covers the large majority of arXiv papers, resolves
free-text titles to canonical records, and exposes `references`, `citations`,
citation counts, and (for many papers) SPECTER2 embeddings — everything the
network needs. It's free for light/personal use without a key; set
`SEMANTIC_SCHOLAR_API_KEY` as an environment variable if you want a higher rate
limit (get one at https://www.semanticscholar.org/product/api). This is the
standard, actively-maintained academic graph API — I cross-checked its current
endpoint shapes (`/paper/search/match`, `/paper/{id}/references`,
`/paper/{id}/citations`, `/recommendations/v1/papers`) against S2's own docs and
tutorial before writing the client, since endpoint/field names do drift over
time; the vendor-comparison "best API overall" blog posts that turn up in search
are mostly marketing and were not used for anything here.

## Files

| File | Purpose |
|---|---|
| `storage.py` | SQLite persistence: papers, citation edges, your labels (seed/liked/disliked/skipped) |
| `semantic_scholar_client.py` | API wrapper: title search, paper detail, references, citations, recommendations, with 429 backoff |
| `citation_network.py` | Builds the `networkx` graph, scores candidates via bibliographic coupling + co-citation + embedding cosine similarity |
| `recommend.py` | CLI entry point: resolves seeds, expands the graph, blends in S2's own recommendation model, runs the feedback loop |
| `seed_titles.txt` | Your seed papers (pre-filled with the list you gave me) |
| `test_offline.py`, `test_client_mocked.py` | Offline tests with synthetic/mocked data — no network needed, already run once during development |

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
# First run: resolves seed_titles.txt, builds the network, shows you the top 10
python recommend.py

# Later runs: re-uses the same SQLite store, expands the network further using
# anything you've liked so far, shows a fresh top 15
python recommend.py --top 15

# Re-rank what's already fetched without hitting the API again
python recommend.py --skip-expand
```

For each candidate you'll see the title, year, citation count, *why* it was
recommended (e.g. "cites 2 of your seed papers", "shares 3 references with your
seed papers", "high semantic similarity to your interests (0.81)"), and a short
abstract. Respond `y` / `n` / `s` / `q`. Liked papers become seeds for the next
run — the network keeps growing outward and the ranking keeps sharpening.

State lives in `paper_store.sqlite3` next to the scripts, so sessions persist
across days. Delete that file to start over.

## How ranking works

- **Candidate pool**: references and citing papers (one hop) of every paper
  currently labeled `seed` or `liked`.
- **Network score**: `2 × co-citation + bibliographic-coupling`, squashed into
  [0, 1) with diminishing returns, so one strong link matters a lot but a tenth
  weak link barely moves the needle.
- **Embedding score**: cosine similarity between the candidate's SPECTER2
  embedding and the centroid of your seed embeddings (only for papers where S2
  has computed one).
- **Combined score**: `0.6 × network_score + 0.4 × embedding_score`, then
  boosted by +0.15 (capped at 1.0) if Semantic Scholar's own recommendation
  model independently surfaced the same paper.

## Known limitation / next step

Your seed list mixes two fairly different interests — enzyme/molecular ML
(CARE, ReactZyme, EnzymeFlow, MolGen, PURE...) and general deep-learning
architecture papers (MAE, Switch Transformers, latent-space reasoning). A single
centroid will average across both and may under-rank one cluster. If that
happens in practice, the cleanest fix is to split `seed_titles.txt` into two
files and run two separate stores/sessions (`--db enzyme.sqlite3` vs
`--db dl_methods.sqlite3`), or extend `citation_network.py` to k-means the seed
embeddings into multiple centroids and take the max similarity across them —
happy to build that multi-centroid version next if the single-centroid ranking
feels like it's blending your two interests.
