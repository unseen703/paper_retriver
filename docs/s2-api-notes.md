# Semantic Scholar API notes

BUILD.md prerequisite P4. This is the spec `app/clients/s2.py` is built against.

**Provenance, stated plainly:** these notes come from the archived prototype in
`legacy/`, which ran against the live API in September 2026, plus BUILD.md's own
figures. They were **not** re-verified against the published docs while writing
R0.7 — this session had no network access. Anything marked ⚠️ below is the kind
of thing that drifts; re-check it the first time a live call misbehaves.

Base URL: `https://api.semanticscholar.org/graph/v1`

## Rate limits

| Condition | Limit |
|---|---|
| With an API key (`x-api-key` header) | 1 request/second |
| Without a key | Substantially lower, and shared |

`S2_RATE_LIMIT=1.0` in `.env` matches the keyed limit. The token bucket
(`app/clients/rate_limit.py`) paces pre-emptively; tenacity handles the 429s
that still arrive because the limit is shared across everything using the key.

## Field sets — the expensive lesson

**The `/paper/{id}/references` and `/paper/{id}/citations` endpoints accept a
narrower field set than `/paper/search` and `/paper/{id}`.** Asking them for
`authors.hIndex` returns HTTP 400:

```json
{"error":"Unrecognized or unsupported fields: [authors.hIndex]"}
```

In the prototype this 400 was swallowed by a broad `except` in the neighbour
fetcher, so **every reference fetch silently returned zero results** while the
run reported success. It cost a day to find, and it is the single most useful
thing in `legacy/`. Hence two constants, not one:

```python
SEARCH_FIELDS   = "...,authors.hIndex,..."   # /paper/search, /paper/{id}
NEIGHBOR_FIELDS = "..."                      # /paper/{id}/references, /citations
```

The consequence for ranking: **author h-index is not available on neighbour
fetches**. `authors.h_index` is NULL until lazily fetched (PLAN.md §C4), which is
why `ranking.yaml` has `author: 0.00` until R3.

## Endpoints used

| Endpoint | Method | Notes |
|---|---|---|
| `/paper/search` | GET | Relevance-ordered. `limit` ≤ 100. |
| `/paper/search/match` | GET | Single best title match, or 404. |
| `/paper/batch` | POST | ids in the body, `fields` in the query string. ⚠️ Documented max **500 ids**; the prototype used 5 because small batches keep one bad id from poisoning a whole chunk. |
| `/paper/{id}/references` | GET | `limit` ≤ 1000. Narrow field set. |
| `/paper/{id}/citations` | GET | `limit` ≤ 1000. Narrow field set. |
| `/author/batch` | POST | Where h-index actually comes from. |

Accepted id forms: bare S2 id, `DOI:10.…`, `ARXIV:1706.03762`, `CorpusId:…`.

## Error handling contract (BUILD.md R0.7)

| Status | Behaviour |
|---|---|
| `429` | Back off honouring `Retry-After`, max 5 attempts. |
| `5xx` | 3 retries, then raise `S2TransientError` — the caller continues rather than aborting the expansion. |
| `404` / `null` entry | Return `None`; the caller writes a STUB row. |
| Missing field | **Never raise.** Log `SCHEMA_DRIFT` with the paper id and degrade. |

The last row is CLAUDE.md rule 6: every S2 Pydantic field is Optional. A missing
field degrades a score; it never crashes a run.

## Batch semantics worth remembering

- `POST /paper/batch` returns a list **positionally aligned with the ids sent**,
  with `null` in the slot of any id S2 does not know. Do not assume the response
  is the same length as a filtered input, and do not zip it against a
  de-duplicated list.
- A batch counts as **one** request against the rate limit regardless of how many
  ids it carries. This is the main reason `get_papers` is the only metadata path
  — a single-paper getter invites N+1 fetching.

## Things to re-verify when convenient ⚠️

1. Whether `embedding.specter_v2` is served on the batch endpoint (matters at R6).
2. The exact max ids per `POST /paper/batch` (500 assumed).
3. Whether `/paper/{id}/references` still caps `limit` at 1000.
4. Current `publicationTypes` vocabulary — `type_filter.py` (R1.5) keys off it.
