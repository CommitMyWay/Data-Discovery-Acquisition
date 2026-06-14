# Source-Specific Crawling Details

Details for each of the 6 platforms: endpoints, pagination, rate limits, known gotchas.

---

## 1. Google Play

**Method**: `google-play-scraper` Python library (no API key needed)

```python
from google_play_scraper import reviews, Sort

result, _ = reviews(
    app_id,           # e.g. "com.vinagame.zalopay"
    lang="vi",
    country="vn",
    sort=Sort.NEWEST,
    count=500,
    filter_score_with=None  # None = all ratings
)
```

**Pagination**: Use `continuation_token` returned by each call for the next batch.

**Rate limits**: No hard limit, but add `time.sleep(1)` between batches of 100+. If you hit a 429, back off 30 seconds.

**Fields available**: `reviewId`, `userName`, `score` (1-5), `at` (datetime), `content`, `thumbsUpCount`, `replyContent`, `repliedAt`

**Gotchas**:
- `at` is a Python datetime object — convert to ISO string for the output schema
- Reviews with `content == None` or empty string exist; filter them out in the source crawler
- `thumbsUpCount` is useful as a spam signal (legitimacy proxy)

---

## 2. Apple App Store

**Method**: iTunes RSS customer-reviews feed (clean JSON, no API key) fetched with the **Python standard library** (`urllib.request`). No `app-store-scraper`, no `requests`.

```python
import json, urllib.request
url = (f"https://itunes.apple.com/rss/customerreviews/id={app_id}"
       f"/sortBy=mostRecent/json?country=vn&limit=50&page={page}")
with urllib.request.urlopen(urllib.request.Request(url), timeout=20) as resp:
    entries = json.load(resp).get("feed", {}).get("entry", [])
```

**Pagination**: append `&page=2`, `&page=3`, … — stops returning at page ~10 (Apple hard limit ~500 reviews via RSS). For deeper history a third-party scraper would be needed, but RSS covers the recency window this skill targets.

**Fields available**: `title`, `review` (content), `rating`, `userName`, `date`

**Rate limits**: RSS has no published limit but will return 403 if hit too fast. Add `time.sleep(2)` between page requests.

**Gotchas**:
- App Store RSS only returns ~500 most recent reviews — use `app-store-scraper` for deeper history
- Some reviews have `rating` as string; cast to `int`
- `date` format from RSS: ISO 8601 — parse with `datetime.fromisoformat()`

---

## 3. YouTube

**Method**: `yt-dlp` for transcripts + YouTube Data API v3 for comments (API key optional for comments via scraping)

### Transcripts
```python
import yt_dlp
ydl_opts = {
    "writesubtitles": True, "writeautomaticsub": True,
    "subtitleslangs": ["vi", "vi-VN", "en"],
    "skip_download": True, "outtmpl": "/tmp/%(id)s"
}
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
```
Parse `.vtt` / `.srt` subtitle file, strip timestamps, join into clean text.

### Comments (no API key)
```python
import yt_dlp
ydl_opts = {"getcomments": True, "skip_download": True}
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
comments = info.get("comments", [])
```

### Video Search
Use `yt-dlp` to search YouTube without API key:
```python
ydl_opts = {"quiet": True, "extract_flat": True, "playlistend": 10}
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    result = ydl.extract_info(f"ytsearch10:{query}", download=False)
video_ids = [entry["id"] for entry in result["entries"]]
```

**Rate limits**: YouTube aggressively rate-limits; add `time.sleep(3)` between video requests. Transcripts fail for videos without captions — catch `yt_dlp.utils.DownloadError` gracefully.

**Gotchas**:
- Auto-generated Vietnamese subtitles have many OCR errors; include them anyway, the qualification step handles length
- Comment `like_count` is available — useful as a spam proxy
- Set `comment_sort` via extractor args for "top" vs "new" ordering

---

## 4. Reddit

**Method**: Public `.json` API via the **Python standard library** (`urllib.request`). No PRAW, no `requests`, no OAuth, no headless browser. Reddit exposes a read-only JSON view of any listing by appending `.json` to its URL.

### No-auth, no-install approach (stdlib only)
```python
import json, urllib.request
req = urllib.request.Request(
    f"https://www.reddit.com/search.json?q={query}&sort=new&limit=100&t=year",
    headers={"User-Agent": "Mozilla/5.0 ... Chrome/124 Safari/537.36"},
)
with urllib.request.urlopen(req, timeout=20) as resp:
    posts = json.load(resp)["data"]["children"]
```
A descriptive/browser-like `User-Agent` is **required** — Reddit 429s the default Python UA. The crawler paginates with the `after` cursor and walks each post's comment tree via `{permalink}.json` (the `replies` field nests recursively).

### Key subreddits to search
- `r/VietNam`, `r/vietnam` — general VN discussions
- `r/fintech`, `r/personalfinance` — fintech mentions
- `r/u_` — user profiles (less useful, skip)

For each post, also fetch comments:
```python
url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json?limit=200"
```

**Fields available**: `author`, `selftext` (post body), `title`, `score`, `created_utc`, `permalink`, `num_comments`

**Rate limits**: 60 requests/minute without OAuth. Add `time.sleep(1)` between requests. Use a descriptive `User-Agent` to avoid blocks.

**Gotchas**:
- `created_utc` is a Unix timestamp — convert with `datetime.utcfromtimestamp()`
- Reddit has no "rating" field — set `rating: null` in the output schema
- Many relevant posts are in Vietnamese — ensure language detection handles these

---

## 5. Tinhte.vn — static XenForo HTML (stdlib)

**Method**: `urllib.request` (stdlib) to fetch server-rendered XenForo HTML, parsed with `html.parser` (stdlib). **No browser engine** — crawl4ai/Playwright was removed to keep the skill installable in restricted environments.

**Trade-off**: Tinhte occasionally Cloudflare-gates its search page. When that happens the fetch raises, retries exhaust, and the crawler falls back to the dataset (see SKILL.md → Retry & Fallback). This is **best-effort live crawling** — expect Tinhte to fall back more often than the JSON-API sources.

### Strategy
1. **Phase A — discover threads**: GET `https://tinhte.vn/search?q={query}&t=post&p={page}`. A stdlib `HTMLParser` collects every `<a href>`; threads are the hrefs matching `/threads?/<slug>.<id>`.
2. **Phase B — fetch each thread**: GET the thread URL and parse posts.

### Post extraction (stdlib HTMLParser)
XenForo posts are `<article class="message ..." data-author="username">` blocks:
- **author** ← the `data-author` attribute on the `<article>` (most stable signal)
- **content** ← text inside the first `<div class="bbWrapper">` (div-depth tracked so nested quotes don't end it early)
- **date** ← the first `<time datetime="...">` inside the article

No CSS-selector library is needed — the parser keys off `data-author` + `bbWrapper`, which both Tinhte and Voz share.

**No rating**: Tinhte has no star rating — `rating: null`.

---

## 6. Voz.vn — static XenForo HTML (stdlib)

**Method**: Same stdlib approach as Tinhte (`urllib.request` + `html.parser`). Voz runs the same XenForo engine — only the thread URL shape differs (`/t/<slug>.<id>/` instead of `/threads/`).

**Trade-off**: Voz has aggressive anti-bot on its search page and login walls on some content, so live crawling is best-effort and falls back to the dataset when blocked.

### Strategy
1. **Discover threads**: GET `https://voz.vn/search/?q={query}&type=post&page={page}`; collect hrefs matching `/t/<slug>.<id>`, normalized to the thread root (strip `/post-N` and `/page-N`).
2. **Page through each thread**: thread root is page 1; subsequent pages are `{thread}/page-2/`, `/page-3/`, … An empty/404 page ends paging for that thread.
3. **Extract posts**: identical XenForo parsing as Tinhte — `data-author` for the author, first `div.bbWrapper` for content, first `<time datetime>` for the date.

**Like count**: not extracted in the stdlib version — `metadata.like_count` is `null`. (The old crawl4ai parser read it from `a.reactionsBar-link[title]`; reinstate via a targeted parser if you need it.)

**No rating**: Voz has no star rating — `rating: null`.
