---
name: user-review-aggregator
description: >
  Aggregates user reviews for Vietnamese fintech apps (ZaloPay, MoMo, ShopeePay, VNPay, etc.) 
  from 6 sources: Google Play, Apple App Store, YouTube (comments), Reddit, Tinhte, 
  and Voz. Handles discovery of app/content IDs per source, collects reviews using the platform's built-in web_search/web_fetch tools (skipping any source that is blocked), deduplicates across sources, and qualifies reviews 
  by recency (12 months), minimum length, language (VN/EN), star rating, and spam signals. 
  Use this skill whenever the user wants to collect, analyze, or audit user reviews/feedback 
  for Vietnamese fintech or payment apps — even if they don't say "crawl" or "scrape". 
  Triggers on: "reviews for MoMo", "what are users saying about ZaloPay", 
  "collect feedback from app stores", "scrape Tinhte for VNPay reviews", 
  "aggregate user opinions on ShopeePay".
---

# User Review Aggregator — Vietnamese Fintech

Collects and analyses user reviews for Vietnamese fintech/payment apps across 6 platforms in four phases: **Discover**, **Fetch**, **Extract & Process**, then **Agent Analysis**.

> `web_search` and `web_fetch` are **built-in OpenClaw platform tools** the agent calls directly during its turn — they are not skills in `/app/skills/`, and not Python functions. All discovery and fetching happens through them.

---

## Phase 1 — Discover

Before fetching any content, identify the correct URLs and content handles for each source.

### App Store and Google Play

For apps listed in `references/fintech-apps.md`, build the store URLs directly from the pre-resolved IDs — no search needed:

- **App Store RSS feed** (JSON): `https://itunes.apple.com/rss/customerreviews/id={ios_id}/sortBy=mostRecent/json?country=vn`
- **Google Play store page**: `https://play.google.com/store/apps/details?id={android_id}&hl=vi&gl=VN`

The `ios_id` and `android_id` values are in `APP_REGISTRY` inside `scripts/agent_api.py` and in `references/fintech-apps.md`.

### YouTube, Reddit, Tinhte, Voz

Use `web_search` with the per-source query from `APP_REGISTRY` (fields `youtube_query`, `reddit_query`, `tinhte_query`, `voz_query`) to collect candidate URLs. For example, for MoMo:

- YouTube: `web_search("MoMo ví điện tử review đánh giá")` — collect top video page URLs
- Reddit: `web_search("MoMo Vietnam e-wallet payment site:reddit.com")` — collect thread URLs
- Tinhte: `web_search("momo ví điện tử site:tinhte.vn")` — collect thread URLs
- Voz: `web_search("momo ví điện tử site:voz.vn")` — collect thread URLs

### Apps not in `fintech-apps.md`

For apps not pre-registered, use `web_search` to find their store listings and discussion threads:

- Google Play: `web_search("{app_name} app Google Play Vietnam")`
- App Store: `web_search("{app_name} app Apple App Store Vietnam")`
- YouTube/Reddit/Tinhte/Voz: adapt the queries above substituting the app name

---

## Phase 2 — Fetch

Call `web_fetch` on each URL discovered in Phase 1. The table below shows the recommended target and what to expect from each source:

| Source | `web_fetch` target | Notes |
|--------|-------------------|-------|
| App Store | `https://itunes.apple.com/rss/customerreviews/id={ios_id}/sortBy=mostRecent/json?country=vn&limit=50&page={n}` | Clean JSON; iterate `feed.entry[]` for rating/title/content/date (paginate pages 1–10; see references/sources.md) |
| Reddit | append `.json` to the thread URL | Clean JSON; read post + `replies` tree |
| Tinhte / Voz | thread URLs from Phase 1 | Readable post text; if gated/blocked, skip and note it |
| Google Play | `https://play.google.com/store/apps/details?id={android_id}&hl=vi&gl=VN` | JS-rendered — rely on `web_search` review snippets plus whatever the page yields |
| YouTube | the video page from Phase 1 | Titles + visible comments only; transcripts and full comment trees are NOT reachable |

**Honest expectation:** JSON sources (App Store RSS, Reddit `.json`) are reliable and consistently structured. JS-heavy sources (Google Play, YouTube) are best-effort — the agent takes whatever text `web_fetch` returns and extracts what it can. **There is no fallback dataset** — a source that fails is skipped and noted in the final analysis.

---

## Phase 3 — Extract & Process

### Building raw review records

Read each fetched payload and pull the following fields per review into a `raw_reviews` list:

```python
{
    "source":   "voz",            # one of: google_play, app_store, youtube, reddit, tinhte, voz
    "app":      "MoMo",           # display name of the app
    "author":   "user1",          # username / handle (None if unavailable)
    "rating":   None,             # integer 1–5 for stores; None for forums/video
    "content":  "...",            # review or comment text
    "date":     "2026-05-01",     # ISO 8601 date string (YYYY-MM-DD)
    "url":      "https://voz.vn/t/...",  # source URL for citation
    "metadata": {"thread_title": "..."}  # optional extra fields
}
```

Valid `source` values: `google_play`, `app_store`, `youtube`, `reddit`, `tinhte`, `voz`.

### Calling `process_reviews()`

Once `raw_reviews` is assembled, call the processing function from `scripts/agent_api.py`:

```python
from scripts.agent_api import process_reviews

data = process_reviews(
    raw_reviews,                 # records the agent built from web_fetch output
    apps=["MoMo"],               # one or more apps
    goal="product",             # product | marketing | qa
    days_back=180,
    focus_area="Login",          # optional deep-dive topic
)
# data["reviews"]        → qualified reviews
# data["reviews_by_app"] → split by app
# data["references"]     → compact source links for citations
# data["stats"]          → per-app counts by source
```

`process_reviews()` runs deduplication, qualification, near-duplicate marking, and focus-area sorting on the records the agent assembled — it does not perform any network I/O.

---

## Deduplication

After all sources are collected, `pipeline.py` removes duplicates using two passes:

1. **Exact hash**: SHA-256 of normalized content (lowercased, whitespace collapsed, punctuation stripped)
2. **Composite key**: `(author_handle, date, rating)` — catches the same review posted across stores

See `references/data-pipeline.md` for the deduplication schema and edge cases.

---

## Data Qualification

All reviews pass through a qualification gate. A review is **kept** if it passes ALL active filters:

| Filter | Default threshold | Notes |
|--------|-----------------|-------|
| Recency | ≤ 365 days old | Configurable via `days_back` |
| Minimum length | ≥ 30 characters | After stripping whitespace |
| Language | `vi` or `en` | Using `langdetect`; short texts get `vi` assumed |
| Star rating | 1–5 (keep all) | Configurable via `rating_min`/`rating_max` |
| Spam/bot signals | Fail = discard | See `references/qualification.md` for rules |

Each review gets a `qualified: true/false` field plus `disqualification_reasons[]`. By default the output keeps all reviews but flags the unqualified ones; pass only qualified records to the analysis phase.

---

## Output Schema

```json
{
  "id": "sha256-hash",
  "source": "google_play",
  "app": "ZaloPay",
  "author": "user123",
  "rating": 4,
  "content": "review text",
  "date": "2024-06-01",
  "url": "https://...",
  "language": "vi",
  "qualified": true,
  "disqualification_reasons": [],
  "metadata": {}
}
```

Full schema and field notes: `references/data-pipeline.md`

---

## Platform Reference Files

Read these when you need source-specific details, known rate limits, or format quirks:

- `references/sources.md` — per-platform API/scraping details, headers, pagination
- `references/fintech-apps.md` — pre-resolved app IDs for major Vietnamese fintech apps
- `references/qualification.md` — full spam detection rules and qualification logic
- `references/data-pipeline.md` — full review schema, dedup logic, fallback format

---

## After Collection — Agent Analysis

Once `process_reviews()` returns, the agent analyses the reviews directly using its own reasoning — the agent IS the model.

```python
data = process_reviews(raw_reviews, apps=["MoMo"], goal="product", focus_area="Login")

# data["reviews"]        → list of qualified review dicts
# data["reviews_by_app"] → reviews split by app name
# data["references"]     → compact source links for citations
# data["stats"]          → per-app counts by source
# data["focus_area"]     → topic to deep-dive (if any)
# data["goal"]           → "product" | "marketing" | "qa"
```

With `data` in context, the agent should produce:

1. **Executive summary** — 2–3 sentences on overall user sentiment
2. **Top issues** — clustered by topic, ranked by severity + frequency, with sample quotes
3. **Feature gaps** — things users want that are missing or broken
4. **Competitor delta** — if multiple apps, what each does better/worse
5. **Actionable proposals** — 3–5 per team:
   - **PO**: backlog priorities with P0/P1/P2 labels
   - **QA**: specific test scenarios targeting reported failures
   - **Marketing**: messaging angles, sentiment risks to address
6. **References** — include source links from `data["references"]` whenever collection returns live records with URLs

Goal guides the depth of each section:
- `product` → emphasise bugs, performance, UX friction
- `marketing` → emphasise brand perception, competitor mentions, sentiment drivers
- `qa` → emphasise reproducible failures, error patterns, regression risks

Focus area (e.g. `"Login"`) → bubble that topic to the top of issues and proposals.
