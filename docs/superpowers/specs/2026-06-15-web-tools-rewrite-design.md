# Design — Rewrite `user-review-aggregator` to use built-in `web_search` / `web_fetch`

**Date:** 2026-06-15
**Status:** Approved (design phase)
**Skill:** `user-review-aggregator`

---

## Problem

The `user-review-aggregator` skill currently collects Vietnamese-fintech app reviews
two ways, both inside Python:

1. **Delegated (default):** `run_research()` HTTP-POSTs a remote `review-crawler-service`
   AgentBase endpoint and polls the async job (`scripts/crawl_client.py`).
2. **Local:** Python crawlers in `scripts/sources/__init__.py` fetch via `urllib`,
   `google-play-scraper`, and `yt-dlp`, with retry/backoff and a fallback dataset
   (`scripts/crawl.py`).

We want the **agent's built-in `web_search` and `web_fetch` tools** to do the discovery
and fetching instead. These are native OpenClaw platform tools the agent (Claude) invokes
during its turn — **not** skills in `/app/skills/`, and **not** functions callable from
Python. That last fact is the crux: collection can no longer happen inside `run_research()`;
the *agent* must drive it, and Python's role shrinks to deterministic post-processing.

## Goals

- The agent uses `web_search` (discovery) and `web_fetch` (content) as the **only**
  collection mechanism.
- Python keeps the **processing** pipeline only: dedup, qualification, near-dup, stats,
  references.
- Preserve the downstream `data` contract so the analysis phase of the skill is unchanged.

## Non-goals

- No changes to `review-crawler-service/` or `momo-zalopay-crawl4ai/` (separate projects).
- No CLI. No fallback dataset. No retry/backoff machinery (those belonged to the crawlers).

## Chosen approach — "Agent extracts, Python processes"

`SKILL.md` drives the agent through four phases: **Discover → Fetch → Extract+Process →
Analyze**. The agent handles the messy, format-varying extraction (what `web_fetch`'s
readable output is good for); Python stays deterministic for the parts that must be
reproducible (dedup, spam/recency/language qualification, stats). `pipeline.py` is reused
untouched.

Rejected alternatives:
- **Per-source Python parsers fed by the agent** — recreates the source-specific code we're
  deleting and re-couples Python to each site's HTML/JSON format. Brittle.
- **Fully agent-native (delete all Python)** — loses the deterministic dedup/spam/recency
  filters that give the skill its qualification guarantees.

---

## File-level changes

### Delete
- `scripts/crawl.py` — `BaseCrawler` retry/fallback.
- `scripts/crawl_client.py` — HTTP delegation to `review-crawler-service`.
- `scripts/sources/__init__.py` and the `scripts/sources/` package — the 6 crawlers.
- `scripts/main.py` — CLI that drove the crawlers (processing-only, no CLI).
- `tests/test_crawl_client.py` — tests a deleted module.

### Add
- `scripts/processing.py` — schema/normalization helpers `make_id()`, `to_iso()`,
  `make_review()`, lifted from the deleted `sources/__init__.py`. Source-agnostic; turns the
  agent's raw records into the canonical review schema.

### Keep unchanged
- `scripts/pipeline.py` — dedup, `qualify`, `mark_near_duplicates`, language detection.
- `references/fintech-apps.md` — app store IDs still used to build fetch URLs.
- `references/qualification.md` — qualify gate is unchanged.
- `output/*.json` — example outputs.

### Rewrite
- `scripts/agent_api.py` — drop both crawl paths; expose `process_reviews()`; keep
  `APP_REGISTRY` and the `references`/`stats` builders.
- `SKILL.md` — four-phase agent-driven web-tool workflow.
- `references/sources.md` — per-source `web_search` query + `web_fetch` URL + fields +
  reachability, replacing crawler internals.
- `references/data-pipeline.md` — keep schema/id/dedup; replace "Fallback Dataset" with the
  raw-record input contract; update execution order step 1.
- `tests/test_agent_api.py` — test `process_reviews()` instead of delegation.
- `scripts/requirements.txt` — keep only `langdetect`; drop `google-play-scraper`, `yt-dlp`.
- `evals/evals.json` — light edits (eval #2 drops fallback path + CSV).

---

## `process_reviews()` interface & data contract

`agent_api.py` exposes one **synchronous** entry point (no I/O — the agent already fetched):

```python
def process_reviews(
    raw_reviews: list[dict],      # records the agent built from web_fetch output
    apps: list[str],              # e.g. ["MoMo", "ZaloPay"]
    goal: str,                    # "product" | "marketing" | "qa"
    days_back: int = 180,
    focus_area: str = None,
    rating_min: int = 1,
    rating_max: int = 5,
    min_length: int = 30,
    allowed_langs: list[str] = None,   # default ["vi", "en"]
) -> dict:
```

**Steps (reuse `pipeline.py` verbatim):**
1. Normalize each raw record via `processing.make_review()` → canonical schema, stable `id`,
   ISO-normalized `date`.
2. `deduplicate()` → `qualify(...)` → `mark_near_duplicates()`.
3. Keep qualified; split by app; build `references` + per-app `stats`; apply `focus_area`
   sorting.

**Raw record the agent supplies** (only these fields required; rest defaulted by
`make_review`):

```python
{"source": "voz", "app": "MoMo", "author": "user1", "rating": None,
 "content": "...", "date": "2026-05-01", "url": "https://voz.vn/t/...",
 "metadata": {"thread_title": "..."}}   # metadata optional
```

`source` values: `google_play`, `app_store`, `youtube`, `reddit`, `tinhte`, `voz`.

**Return shape — identical to the current `run_research()`** so the analysis phase doesn't
change:

```python
{ "apps": [...], "goal": ..., "focus_area": ...,
  "reviews": [...], "reviews_by_app": {...},
  "references": [...], "stats": {...}, "params": {...} }
```

The delegated-only keys (`service_results`, `reviews_by_source`) are dropped. The keys the
analysis actually reads (`reviews`, `reviews_by_app`, `references`, `stats`) are preserved.
`APP_REGISTRY` stays in `agent_api.py` — used to resolve display names in `process_reviews()`
and as discovery reference data the agent reads in Phase 1.

---

## `SKILL.md` — four-phase workflow

Framing line up top: *`web_search` and `web_fetch` are built-in OpenClaw platform tools the
agent calls directly — not skills, not Python.* Frontmatter (name/description/triggers)
unchanged.

**Phase 1 — Discover** (`web_search` + known IDs)
- App stores: build URLs directly from `fintech-apps.md` IDs (no search).
- YouTube / Reddit / Tinhte / Voz: `web_search` with the per-source query from
  `APP_REGISTRY` to collect candidate URLs.

**Phase 2 — Fetch** (`web_fetch` per URL)

| Source | `web_fetch` target | Reachability |
|--------|-------------------|--------------|
| App Store | `itunes.apple.com/rss/customerreviews/id={id}/sortBy=mostRecent/json?country=vn` | Clean JSON |
| Reddit | `{thread_url}.json` | Clean JSON |
| Tinhte / Voz | thread URLs from search | Readable post text; may be gated → skip on failure |
| Google Play | `play.google.com/store/apps/details?id={id}&hl=vi&gl=VN` | JS-rendered; rely on search snippets + page yield |
| YouTube | video page from search | Titles + visible comments only; transcripts/comment-trees not reachable |

Honest expectations: JSON sources reliable; JS-heavy sources best-effort. **No fallback
dataset** — a source that fails is skipped and noted.

**Phase 3 — Extract & process**
Agent reads each fetched payload, pulls `author / rating / content / date / url` per review
into a `raw_reviews` list, then:

```python
from scripts.agent_api import process_reviews
data = process_reviews(raw_reviews, apps=["MoMo"], goal="product",
                       days_back=180, focus_area="Login")
```

**Phase 4 — Analyze** — unchanged: exec summary, top issues, feature gaps, competitor delta,
PO/QA/Marketing proposals, references.

Removed sections: retry/backoff, AgentBase endpoint config, local smoke test,
`crawl_service_url`/`REVIEW_CRAWLER_*` env vars.

---

## Error handling / edge cases

- **Source fetch fails or is gated:** agent skips it and notes it in the analysis (no
  fallback dataset exists). Never blocks the other sources.
- **No reviews collected at all:** `process_reviews([])` returns empty `reviews`/`stats`
  without error; SKILL.md tells the agent to report that no qualified reviews were found.
- **Malformed raw record:** `make_review()` defaults missing fields (rating→None,
  metadata→{}); `qualify()` flags too-short/wrong-language records as unqualified.
- **Unknown app name:** `process_reviews()` still resolves display names via `APP_REGISTRY`
  partial match; unknown apps fall back to the name as given.

---

## Testing

`tests/test_agent_api.py` (rewritten):
- Lightweight "imports without heavy crawl deps" check.
- Port the references test to `process_reviews()`: one raw record → normalized, qualified,
  expected `references` entry produced.
- New: an unqualified record (too short / wrong language) is excluded from `reviews`.
- Remove both delegation tests.

`tests/test_crawl_client.py` — deleted.

Run: `cd user-review-aggregator && /usr/bin/python3 -m unittest discover -s tests`.

---

## Out of scope / future

- Restoring YouTube transcripts or full Google Play review pagination would require a
  browser-capable fetch path; not part of this rewrite.
