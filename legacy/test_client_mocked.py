"""
Validates semantic_scholar_client.py parsing/plumbing against fake HTTP responses
that mirror the real Graph API's documented schema -- no real network call made.
Run with: python test_client_mocked.py
"""
from unittest.mock import patch, MagicMock

from semantic_scholar_client import SemanticScholarClient, s2_record_to_kwargs


FAKE_MATCH = {"data": [{
    "paperId": "abc123", "title": "Enzyme Function Prediction using Contrastive Learning",
    "year": 2023, "authors": [{"name": "A. Author"}], "citationCount": 12,
    "externalIds": {"ArXiv": "2301.00001"},
}]}

FAKE_PAPER = {
    "paperId": "abc123", "title": "Enzyme Function Prediction using Contrastive Learning",
    "abstract": "We propose a contrastive method...", "year": 2023, "venue": "NeurIPS",
    "authors": [{"name": "A. Author"}], "citationCount": 12,
    "externalIds": {"ArXiv": "2301.00001"},
    "tldr": {"text": "A contrastive learning method for enzyme function."},
    "embedding": {"model": "specter_v2", "vector": [0.1, 0.2, 0.3]},
}

FAKE_REFERENCES = {"data": [{"citedPaper": {"paperId": "ref1", "title": "Prior work A", "citationCount": 5}}]}
FAKE_CITATIONS = {"data": [{"citingPaper": {"paperId": "cite1", "title": "Follow-up B", "citationCount": 3}}]}
FAKE_RECS = {"recommendedPapers": [{"paperId": "rec1", "title": "Recommended C", "citationCount": 8}]}


def fake_response(json_body, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    resp.raise_for_status.side_effect = None
    return resp


def main():
    client = SemanticScholarClient()

    with patch.object(client.session, "request") as mock_req:
        mock_req.return_value = fake_response(FAKE_MATCH)
        match = client.search_paper_by_title("Enzyme function prediction using contrastive learning")
        assert match["paperId"] == "abc123"
        print("search_paper_by_title OK ->", match["title"])

        mock_req.return_value = fake_response(FAKE_PAPER)
        full = client.get_paper("abc123")
        kwargs = s2_record_to_kwargs(full)
        assert kwargs["arxiv_id"] == "2301.00001"
        assert kwargs["embedding"] == [0.1, 0.2, 0.3]
        assert kwargs["tldr"].startswith("A contrastive")
        print("get_paper + s2_record_to_kwargs OK ->", kwargs["title"], kwargs["arxiv_id"])

        mock_req.return_value = fake_response(FAKE_REFERENCES)
        refs = client.get_references("abc123")
        assert refs[0]["paperId"] == "ref1"
        print("get_references OK ->", [r["title"] for r in refs])

        mock_req.return_value = fake_response(FAKE_CITATIONS)
        cites = client.get_citations("abc123")
        assert cites[0]["paperId"] == "cite1"
        print("get_citations OK ->", [c["title"] for c in cites])

        mock_req.return_value = fake_response(FAKE_RECS)
        recs = client.recommend(["abc123"], [], limit=10)
        assert recs[0]["paperId"] == "rec1"
        print("recommend OK ->", [r["title"] for r in recs])

    # 429 retry path
    with patch.object(client.session, "request") as mock_req, patch("time.sleep", return_value=None):
        mock_req.side_effect = [fake_response({}, status=429), fake_response(FAKE_MATCH)]
        match = client.search_paper_by_title("retry test")
        assert match["paperId"] == "abc123"
        print("429 backoff-and-retry OK")

    print("\nAll mocked client assertions passed.")


if __name__ == "__main__":
    main()
