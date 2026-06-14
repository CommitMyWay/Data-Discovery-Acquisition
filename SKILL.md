---
name: user-review-aggregator
description: >
  Aggregates user reviews for Vietnamese fintech apps (ZaloPay, MoMo, ShopeePay, VNPay, etc.) 
  from 6 sources: Google Play, Apple App Store, YouTube (comments + transcript), Reddit, Tinhte, 
  and Voz. Handles discovery of app/content IDs per source, crawls with auto-retry and fallback 
  to a local dataset when live sources fail, deduplicates across sources, and qualifies reviews 
  by recency (12 months), minimum length, language (VN/EN), star rating, and spam signals. 
  Use this skill whenever the user wants to collect, analyze, or audit user reviews/feedback 
  for Vietnamese fintech or payment apps — even if they don't say "crawl" or "scrape". 
  Triggers on: "reviews for MoMo", "what are users saying about ZaloPay", 
  "collect feedback from app stores", "scrape Tinhte for VNPay reviews", 
  "aggregate user opinions on ShopeePay".
---

# User Review Aggregator — Vietnamese Fintech

Collects, cleans, and qualifies user reviews for Vietnamese fintech/payment apps across 6 platforms in two phases: **Discovery** then **Crawl & Process**.

## Phase 1 — Discovery

Given a target app (e.g. "ZaloPay"), identify the correct content handles for each source before any crawling begins.

### Steps
1. **Check `references/fintech-apps.md`** first — it has pre-resolved IDs for the major apps (ZaloPay, MoMo, ShopeePay, VNPay, ViettelMoney, etc.). Use these directly when available.
2. For apps **not** in that file, discover IDs dynamically:
   - Google Play: search `https://play.google.com/store/search?q={app_name}&c=apps&hl=vi&gl=VN`
   - App Store: `https://itunes.apple.com/search?term={app_name}&country=vn&entity=software&limit=5`
   - YouTube: build a search query like `"{app_name} review đánh giá"` — collect top 10 video IDs
   - Reddit: search terms like `"{app_name} Vietnam fintech"` across r/VietNam, r/vietnam, r/fintech
   - Tinhte: `https://tinhte.vn/search?q={app_name}`
   - Voz: `https://voz.vn/search/?q={app_name}&type=post`
3. Present discovered targets to the user and confirm before crawling — especially for ambiguous apps.

---

## Phase 2 — Crawl & Process

Call `run_research()` from `agent_api.py`. It handles all sources, retry logic, fallback, dedup, and qualification — then returns structured data the agent reasons over directly.

```python
import asyncio
from scripts.agent_api import run_research

data = await run_research(
    apps=["ZaloPay"],                    # one or more apps
    goal="product",                      # product | marketing | qa
    days_back=180,
    focus_area="Login",                  # optional deep-dive topic
    sources=["google_play", "app_store", "youtube", "reddit", "tinhte", "voz"],
    fallback_dataset_path="/path/to/fallback.json",  # optional
)
```

The agent then reads `data["reviews"]` and produces the analysis natively — no second API call needed.

### Key parameters
| Parameter | Purpose |
|-----------|---------|
| `apps` | List of app names — pre-resolved from `references/fintech-apps.md` |
| `goal` | Shapes which insights to emphasise in analysis |
| `days_back` | Recency window; default 180 |
| `focus_area` | Optional topic to surface first (e.g. `"OTP"`, `"Thanh toán"`) |
| `sources` | Omit any source to skip it |
| `fallback_dataset_path` | JSON file used when a live source fails after retries |
| `rating_min` / `rating_max` | Filter by star rating; default 1–5 |

---

## Retry & Fallback Behavior

Each crawler wraps requests in exponential-backoff retry (see `scripts/crawl.py`). The sequence per source:
1. Try live fetch — on any HTTP error or timeout → wait `2^attempt` seconds, retry up to `--max-retries`
2. After all retries exhausted → log warning, load matching records from `--fallback-dataset` for this source
3. If no fallback data exists for this source → log and continue (don't crash the pipeline)

The fallback dataset is a JSON array with the same schema as live-collected reviews (see `references/data-pipeline.md`).

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
| Recency | ≤ 365 days old | Configurable via `--days-back` |
| Minimum length | ≥ 30 characters | After stripping whitespace |
| Language | `vi` or `en` | Using `langdetect`; short texts get `vi` assumed |
| Star rating | 1–5 (keep all) | Configurable via `--rating-min/max` |
| Spam/bot signals | Fail = discard | See `references/qualification.md` for rules |

Each review gets a `qualified: true/false` field plus `disqualification_reasons[]`. By default the output keeps all reviews but flags the unqualified ones; use `--only-qualified` to filter to passing reviews only.

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

Read these when you need source-specific crawling details, known rate limits, or format quirks:
- `references/sources.md` — per-platform API/scraping details, headers, pagination
- `references/fintech-apps.md` — pre-resolved app IDs for major Vietnamese fintech apps
- `references/qualification.md` — full spam detection rules and qualification logic
- `references/data-pipeline.md` — full review schema, dedup logic, fallback format

---

## After Collection — Agent Analysis

Once `run_research()` returns, the agent analyses the reviews directly using its own reasoning — no second API call needed. The agent IS the model.

```python
data = await run_research(apps=["MoMo"], goal="product", focus_area="Login")

# data["reviews"]        → list of qualified review dicts
# data["reviews_by_app"] → reviews split by app name
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

Goal guides the depth of each section:
- `product` → emphasise bugs, performance, UX friction
- `marketing` → emphasise brand perception, competitor mentions, sentiment drivers  
- `qa` → emphasise reproducible failures, error patterns, regression risks

Focus area (e.g. `"Login"`) → bubble that topic to the top of issues and proposals.
