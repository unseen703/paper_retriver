"""
graph_viz.py
------------
Flask web server for interactive D3.js citation graph visualization.

Usage:
    python graph_viz.py [--db paper_store.sqlite3] [--port 5000]

Then open http://localhost:5000 in your browser.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import threading
import uuid
from collections import Counter
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from citation_network import build_graph
from exploration import explore_paper
from expansion import expand_citations, expand_references
from graph_expander import expand_network
from semantic_scholar_client import SemanticScholarClient, s2_record_to_kwargs
from storage import Paper, Store
from venue_config import venue_score

logger = logging.getLogger(__name__)

_expand_jobs: dict[str, dict] = {}  # job_id -> {status, added, pruned, error}

DB_PATH: Path = Path(__file__).parent / "paper_store.sqlite3"
SEED_TITLES_FILE: Path = Path(__file__).parent / "seed_papers.txt"

app = Flask(__name__, template_folder="templates")


@app.route("/")
def index():
    return render_template("graph.html")


def _load_seed_titles_file() -> list[str]:
    """Read non-blank, non-comment lines from SEED_TITLES_FILE."""
    if not SEED_TITLES_FILE.exists():
        return []
    lines = [ln.strip() for ln in SEED_TITLES_FILE.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def _resolve_seeds_from_file(s2: SemanticScholarClient, store: Store) -> list[str]:
    """
    Resolve every title in SEED_TITLES_FILE against Semantic Scholar, upsert
    each as label="seed", and record it in the seed_titles table (the file
    is the source of truth -- this brings the DB row back in sync with it).
    Returns the resolved paper_ids.
    """
    resolved_ids: list[str] = []
    for title in _load_seed_titles_file():
        try:
            match = s2.search_paper_by_title(title)
            if not match:
                store.record_seed_title(title, None, 0.0)
                logger.warning("No S2 match for seed title %r", title)
                continue
            kwargs = s2_record_to_kwargs(match)
            try:
                full = s2.get_paper(kwargs["paper_id"])
                if full:
                    kwargs = s2_record_to_kwargs(full)
            except Exception:
                logger.exception("Could not fetch full record for %r; using partial match", title)
            store.upsert_paper(Paper(**kwargs), label="seed")
            store.record_seed_title(title, kwargs["paper_id"], 1.0)
            resolved_ids.append(kwargs["paper_id"])
        except Exception:
            logger.exception("Failed to resolve seed title %r", title)
    return resolved_ids


_SCORE_EQUAL = 1.0 / 3.0
_SCORE_2026_CIT = _SCORE_EQUAL * 0.8            # 20% less than equal share
_SCORE_2026_OTHER = (1.0 - _SCORE_2026_CIT) / 2.0  # redistributed to venue + conn


def _node_score(paper, n_seeds: int) -> float:
    """Weighted relevance score [0, 1] matching the graph expander priority formula."""
    cit = min(1.0, math.log1p(paper.citation_count or 0) / math.log1p(10000))
    v = venue_score(paper.venue)
    conn = len(paper.seed_connections or []) / max(n_seeds, 1)
    if (paper.year or 0) >= 2026:
        return _SCORE_2026_CIT * cit + _SCORE_2026_OTHER * v + _SCORE_2026_OTHER * conn
    return _SCORE_EQUAL * cit + _SCORE_EQUAL * v + _SCORE_EQUAL * conn


@app.route("/api/graph")
def api_graph():
    store = Store(DB_PATH)
    g = build_graph(store)
    # seed_connections (populated by graph_expander/exploration) is built
    # against the seed+liked set, not "seed" alone -- the denominator here
    # must match, or conn can exceed 1.0 once any paper is liked.
    n_seeds = len(store.papers_with_label(["seed", "liked"]))

    nodes = []
    for pid in g.nodes():
        paper = store.get_paper(pid)
        if paper:
            nodes.append({
                "id": pid,
                "title": paper.title or pid,
                "year": paper.year,
                "citation_count": paper.citation_count or 0,
                "label": paper.label,
                "authors": (paper.authors or [])[:3],
                "venue": paper.venue,
                "distance": paper.distance_from_seed,
                "score": round(_node_score(paper, n_seeds), 4),
            })

    node_id_set = {n["id"] for n in nodes}
    links = [
        {"source": u, "target": v}
        for u, v in g.edges()
        if u in node_id_set and v in node_id_set
    ]
    return jsonify({"nodes": nodes, "links": links})


@app.route("/api/graph/expand-prune", methods=["POST"])
def api_expand_prune():
    body = request.get_json(silent=True) or {}
    hops = int(body.get("hops", 1))
    max_new = max(10, min(2000, int(body.get("max_papers", 300))))
    store = Store(DB_PATH)

    # Prune disliked papers synchronously (fast)
    pruned = store.delete_papers_by_label("disliked")

    # Collect seeds for expansion
    seeds = store.papers_with_label(["seed", "liked"])
    seed_ids = [p.paper_id for p in seeds]
    initial_seed_count = len(seed_ids)

    # If the network has no papers at all, bootstrap from SEED_TITLES_FILE
    # before expanding, rather than just reporting nothing to do -- there's
    # nothing destructive here since the network is already empty.
    network_is_empty = not store.all_paper_ids()
    will_rebuild = network_is_empty and not seed_ids and bool(_load_seed_titles_file())

    if not seed_ids and not will_rebuild:
        return jsonify({"status": "done", "pruned": pruned, "added": 0,
                        "message": "No seed/liked papers to expand from"})

    job_id = uuid.uuid4().hex[:8]
    _expand_jobs[job_id] = {"status": "running", "pruned": pruned, "added": 0}

    def _run():
        try:
            from api_cache import ApiCache
            from openalex_client import OpenAlexClient
            from semantic_scholar_client import SemanticScholarClient
            try:
                from dotenv import load_dotenv
                load_dotenv()
            except ImportError:
                pass
            cache = ApiCache()
            s2 = SemanticScholarClient(api_key=os.environ.get("S2_API_KEY"), cache=cache)
            oa = OpenAlexClient(cache=cache)
            store2 = Store(DB_PATH)

            run_seed_ids = list(seed_ids)
            if not run_seed_ids:
                run_seed_ids.extend(_resolve_seeds_from_file(s2, store2))

            added = expand_network(s2, oa, store2, run_seed_ids, hops=hops, max_new=max_new)
            _expand_jobs[job_id]["added"] = added
            _expand_jobs[job_id]["status"] = "done"
        except Exception as exc:
            logger.exception("Expansion failed")
            _expand_jobs[job_id]["status"] = "error"
            _expand_jobs[job_id]["error"] = str(exc)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "running",
                    "pruned": pruned, "seed_count": initial_seed_count,
                    "rebuilding": will_rebuild})


def _run_expansion_job(kind: str, body: dict):
    """
    Shared plumbing for the two expansion buttons. `kind` is "references"
    (uncapped) or "citations" (capped by the GUI's max_papers budget).
    """
    store = Store(DB_PATH)
    seed_ids = [p.paper_id for p in store.papers_with_label(["seed", "liked"])]
    network_is_empty = not store.all_paper_ids()
    will_bootstrap = network_is_empty and not seed_ids and bool(_load_seed_titles_file())

    if not seed_ids and not will_bootstrap:
        return jsonify({"status": "done", "added": 0,
                        "message": "No seed/liked papers to expand from"})

    max_papers = max(1, min(20000, int(body.get("max_papers", 300))))
    job_id = uuid.uuid4().hex[:8]
    _expand_jobs[job_id] = {"status": "running", "added": 0, "kind": kind}

    def _run():
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        try:
            from api_cache import ApiCache
            from openalex_client import OpenAlexClient
            cache = ApiCache()
            s2 = SemanticScholarClient(api_key=os.environ.get("S2_API_KEY"), cache=cache)
            oa = OpenAlexClient(cache=cache)
            store2 = Store(DB_PATH)

            run_ids = list(seed_ids)
            if not run_ids:
                run_ids.extend(_resolve_seeds_from_file(s2, store2))

            if kind == "references":
                # Uncapped: a seed's bibliography goes in whole.
                res = expand_references(s2, oa, store2, run_ids)
            else:
                # The GUI budget applies here and only here.
                res = expand_citations(s2, oa, store2, run_ids, max_total=max_papers)

            _expand_jobs[job_id].update(res)
            _expand_jobs[job_id]["status"] = "done"
        except Exception as exc:
            logger.exception("%s expansion failed", kind)
            _expand_jobs[job_id]["status"] = "error"
            _expand_jobs[job_id]["error"] = str(exc)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "running", "kind": kind,
                    "seed_count": len(seed_ids), "rebuilding": will_bootstrap})


@app.route("/api/graph/expand-references", methods=["POST"])
def api_expand_references():
    """Expand the references of every seed/liked paper. No cap."""
    return _run_expansion_job("references", request.get_json(silent=True) or {})


@app.route("/api/graph/expand-citations", methods=["POST"])
def api_expand_citations():
    """Expand citing papers, ranked, under the GUI's max_papers budget
    split equally across the seed papers."""
    return _run_expansion_job("citations", request.get_json(silent=True) or {})


@app.route("/api/expand/status/<job_id>")
def api_expand_status(job_id: str):
    job = _expand_jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)


_VALID_LABELS = {"liked", "disliked", "seed", "skipped"}


@app.route("/api/paper/<paper_id>/label", methods=["POST"])
def api_set_label(paper_id: str):
    store = Store(DB_PATH)
    if not store.get_paper(paper_id):
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    new_label = body.get("label")  # None means clear the label
    if new_label is not None and new_label not in _VALID_LABELS:
        return jsonify({"error": f"invalid label {new_label!r}"}), 400
    store.set_label(paper_id, new_label)

    resp = {"ok": True, "paper_id": paper_id, "label": new_label}
    if new_label == "liked":
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        try:
            from api_cache import ApiCache
            from openalex_client import OpenAlexClient
            cache = ApiCache()
            s2 = SemanticScholarClient(api_key=os.environ.get("S2_API_KEY"), cache=cache)
            oa = OpenAlexClient(cache=cache)
            resp["explored"] = explore_paper(paper_id, s2, oa, store)
        except Exception:
            logger.exception("Exploration failed for liked paper %s", paper_id)
    return jsonify(resp)


@app.route("/api/paper/add-by-title", methods=["POST"])
def api_add_paper_by_title():
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    s2 = SemanticScholarClient(api_key=os.environ.get("S2_API_KEY"))
    record = s2.search_paper_by_title(title)
    if not record:
        return jsonify({"error": f"No paper found for title {title!r}"}), 404

    kwargs = s2_record_to_kwargs(record)
    paper = Paper(**kwargs)
    store = Store(DB_PATH)
    store.upsert_paper(paper, label="seed")
    stored = store.get_paper(paper.paper_id)
    return jsonify({
        "paper_id": stored.paper_id,
        "title": stored.title,
        "year": stored.year,
        "citation_count": stored.citation_count,
        "venue": stored.venue,
        "label": stored.label,
        "authors": stored.authors or [],
    })


@app.route("/api/paper/search")
def api_search_papers():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"papers": []})
    store = Store(DB_PATH)
    papers = store.search_papers(q)
    return jsonify({"papers": [
        {
            "paper_id": p.paper_id,
            "title": p.title,
            "year": p.year,
            "citation_count": p.citation_count or 0,
            "label": p.label,
        }
        for p in papers
    ]})


@app.route("/api/paper/<paper_id>/neighbors")
def api_paper_neighbors(paper_id: str):
    store = Store(DB_PATH)
    if not store.get_paper(paper_id):
        return jsonify({"error": "not found"}), 404
    neighbors = store.get_neighbors(paper_id)
    return jsonify({"paper_id": paper_id, "neighbors": neighbors})


@app.route("/api/papers/seeds-liked")
def api_seeds_liked():
    store = Store(DB_PATH)
    papers = store.papers_with_label(["seed", "liked"])
    return jsonify({"papers": [
        {
            "paper_id": p.paper_id,
            "title": p.title,
            "year": p.year,
            "citation_count": p.citation_count or 0,
            "label": p.label,
        }
        for p in papers
    ]})


_RANKED_DEFAULT_LABELS = {"unlabeled", "seed", "liked"}


@app.route("/api/papers/ranked")
def api_papers_ranked():
    """
    Every paper ranked by indegree (number of other papers in the network
    that cite it), optionally filtered by label. `labels` is a comma-separated
    list drawn from {"unlabeled", "seed", "liked", "disliked", "skipped"};
    omitting it defaults to unlabeled+seed+liked. An empty `labels=` value
    means "nothing selected" -> returns no papers.
    """
    store = Store(DB_PATH)
    limit = max(1, min(1000, int(request.args.get("limit", 200))))
    labels_param = request.args.get("labels")
    allowed = (
        set(_RANKED_DEFAULT_LABELS) if labels_param is None
        else {l.strip() for l in labels_param.split(",") if l.strip()}
    )

    indegree = Counter(cited for _citing, cited in store.all_edges())

    papers = []
    for row in store.all_papers_with_metrics():
        label = row.get("label")
        if (label or "unlabeled") not in allowed:
            continue
        pid = row["paper_id"]
        papers.append({
            "paper_id": pid,
            "title": row.get("title") or pid,
            "year": row.get("year"),
            "citation_count": row.get("citation_count") or 0,
            "label": label,
            "indegree": indegree.get(pid, 0),
        })
    papers.sort(key=lambda p: p["indegree"], reverse=True)
    return jsonify({"papers": papers[:limit], "total_matching": len(papers)})


@app.route("/api/paper/<paper_id>", methods=["DELETE"])
def api_delete_paper(paper_id: str):
    store = Store(DB_PATH)
    if not store.get_paper(paper_id):
        return jsonify({"error": "not found"}), 404
    deleted = store.delete_paper_cascade(paper_id)
    return jsonify({"ok": True, "paper_id": paper_id, "deleted": deleted})


@app.route("/api/graph/clear-and-rebuild", methods=["POST"])
def api_clear_and_rebuild():
    """
    Wipe the ENTIRE network -- papers, edges, network_metrics, and
    seed_titles. Nothing is restored automatically: the "Prune disliked &
    expand from liked/seed" button already auto-bootstraps from
    SEED_TITLES_FILE the next time it runs on an empty network, so
    restoring seeds here too would just be redundant. A pure DB wipe needs
    no S2 calls, so this runs synchronously.
    """
    store = Store(DB_PATH)
    store.clear_network()
    store.clear_seed_titles()
    return jsonify({"ok": True})


@app.route("/api/paper/<paper_id>")
def api_paper(paper_id: str):
    store = Store(DB_PATH)
    paper = store.get_paper(paper_id)
    if not paper:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "paper_id": paper.paper_id,
        "title": paper.title,
        "abstract": paper.abstract,
        "year": paper.year,
        "authors": paper.authors or [],
        "venue": paper.venue,
        "citation_count": paper.citation_count,
        "label": paper.label,
        "distance_from_seed": paper.distance_from_seed,
        "field_of_study": paper.field_of_study or [],
    })


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Citation graph web visualizer")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    DB_PATH = args.db
    app.run(debug=True, port=args.port)
