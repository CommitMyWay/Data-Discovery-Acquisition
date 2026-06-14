# Source-Specific Collection Details

> Collection uses the agent's built-in `web_search`/`web_fetch` tools — no crawler libraries, no browser engine, no fallback dataset.

Details for each of the 6 platforms: query patterns, fetch targets, field mappings, and reachability notes.

---

## 1. Google Play

### Phase 1 — Discovery

`web_search` query: `"{app} review site:play.google.com"` plus a broader review search such as `"{app} đánh giá Google Play"`. Supplement extracted reviews with any useful snippets returned in search results.

### Phase 2 — Fetch

`web_fetch` target:

```text
https://play.google.com/store/apps/details?id={android_id}&hl=vi&gl=VN
```

(`{android_id}` is the value from `APP_REGISTRY` / `references/fintech-apps.md`.)

### Fields to extract

| Source field | Raw record field |
|---|---|
| reviewer name | `author` |
| `score` (1–5) | `rating` |
| review body | `content` |
| review date | `date` |

`rating` is always an integer in the range 1–5.

### Reachability note

The Play Store page is JS-rendered; `web_fetch` returns a partial HTML snapshot. Extract what is visible and supplement with review text found in `web_search` snippets for the same query.

---

## 2. Apple App Store

### Phase 1 — Discovery

URL is built directly from `ios_id` — no `web_search` step needed.

### Phase 2 — Fetch

`web_fetch` target (iterate pages 1–10):

```text
https://itunes.apple.com/rss/customerreviews/id={ios_id}/sortBy=mostRecent/json?country=vn&limit=50&page={n}
```

(`{ios_id}` is the value from `APP_REGISTRY` / `references/fintech-apps.md`.)

### Fields to extract

Entries are in `feed.entry[]` of the JSON response.

| JSON path | Raw record field |
|---|---|
| `author.name.label` | `author` |
| `im:rating.label` | `rating` |
| `content.label` | `content` |
| `updated.label` | `date` |
| `title.label` | `metadata.review_title` |

### Reachability note

Returns clean JSON with no authentication required. The RSS feed is capped at approximately 500 most-recent reviews across pages 1–10.

---

## 3. YouTube

### Phase 1 — Discovery

`web_search` query: the `youtube_query` value from `APP_REGISTRY` / `references/fintech-apps.md`. Collect video URLs from search results.

### Phase 2 — Fetch

`web_fetch` each video page URL identified in Phase 1.

### Fields to extract

| Source | Raw record field |
|---|---|
| Comment author (visible on page) | `author` |
| Comment text | `content` |
| *(no star rating)* | `rating` → `None` |
| Video title | `metadata.video_title` |
| Video URL | `metadata.video_url` |

### Reachability note

`web_fetch` retrieves only the visible portion of a video page. Full comment trees and video transcripts are **not** reachable via `web_fetch`; collect whatever comment text is rendered in the initial page response.

---

## 4. Reddit

### Phase 1 — Discovery

`web_search` query: the `reddit_query` value from `APP_REGISTRY` / `references/fintech-apps.md`. Target subreddits include `r/VietNam`, `r/vietnam`, and `r/fintech`.

### Phase 2 — Fetch

`web_fetch` target: append `.json` to the thread URL found in Phase 1:

```text
{thread_url}.json
```

### Fields to extract

| JSON path | Raw record field |
|---|---|
| Post `title` + `selftext` | `content` (post body) |
| Comment `body` | `content` (per comment) |
| *(no star rating)* | `rating` → `None` |
| `subreddit` | `metadata.subreddit` |
| Post `score` | `metadata.post_score` |

`author` is taken from the `author` field on each post or comment object.

### Reachability note

Reddit's `.json` endpoint returns clean JSON with no authentication required. Both post bodies and top-level comment replies are accessible without OAuth.

---

## 5. Tinhte.vn

### Phase 1 — Discovery

`web_search` query: `"{tinhte_query} site:tinhte.vn"` where `tinhte_query` is the value from `APP_REGISTRY` / `references/fintech-apps.md`.

### Phase 2 — Fetch

`web_fetch` each thread URL returned in Phase 1 search results.

### Fields to extract

| Source | Raw record field |
|---|---|
| Post author | `author` |
| Post body text | `content` |
| *(no star rating)* | `rating` → `None` |
| Thread title | `metadata.thread_title` |

### Reachability note

Best-effort. Tinhte may gate its content behind Cloudflare or login prompts. Skip the source gracefully on fetch failure — do not retry indefinitely.

---

## 6. Voz.vn

### Phase 1 — Discovery

`web_search` query: `"{voz_query} site:voz.vn"` where `voz_query` is the value from `APP_REGISTRY` / `references/fintech-apps.md`.

### Phase 2 — Fetch

`web_fetch` each thread URL returned in Phase 1 search results.

### Fields to extract

| Source | Raw record field |
|---|---|
| Post author | `author` |
| Post body text | `content` |
| *(no star rating)* | `rating` → `None` |
| Thread title | `metadata.thread_title` |

### Reachability note

Best-effort. Voz applies anti-bot measures and login walls on some content. Skip the source gracefully on fetch failure — do not retry indefinitely.
