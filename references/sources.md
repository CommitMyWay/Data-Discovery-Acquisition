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

**Method**: `app-store-scraper` Python library OR iTunes RSS feed (no API key needed)

```python
from app_store_scraper import AppStore

app = AppStore(country="vn", app_name="zalopay", app_id="1107454800")
app.review(how_many=500)
reviews = app.reviews  # list of dicts
```

**Alternative (RSS)**: `https://itunes.apple.com/rss/customerreviews/id={app_id}/sortBy=mostRecent/json?country=vn&limit=50`
- Paginate by appending `&page=2`, `&page=3`, etc. — stops returning at page ~10 (Apple hard limit ~500 reviews via RSS)

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

**Method**: PRAW (Python Reddit API Wrapper) — requires client_id + client_secret, OR use `requests` against the public JSON API (no auth needed for read-only)

### No-auth approach (preferred for portability)
```python
import requests
headers = {"User-Agent": "ReviewBot/1.0"}
url = f"https://www.reddit.com/search.json?q={query}&sort=new&limit=100&t=year"
resp = requests.get(url, headers=headers)
posts = resp.json()["data"]["children"]
```

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

## 5. Tinhte.vn — crawl4ai

**Method**: `crawl4ai` with Playwright (Chromium). Replaces `requests+BeautifulSoup`.

**Why crawl4ai here**: Tinhte uses JS-rendered lazy loading and Cloudflare protection. A plain HTTP client gets bot-blocked or returns incomplete HTML.

### Config used
```python
BrowserConfig(headless=True, browser_type="chromium")

CrawlerRunConfig(
    cache_mode=CacheMode.BYPASS,
    magic=True,            # Hides automation signals (navigator.webdriver etc.)
    simulate_user=True,    # Human-like mouse + scroll behavior
    override_navigator=True,
    remove_consent_popups=True,
    scan_full_page=True,   # Auto-scrolls for lazy-loaded content
    user_agent_mode="random",
    extraction_strategy=JsonCssExtractionStrategy(schema),
)
```

### Search extraction schema
```python
{
    "baseSelector": "div.contentRow",
    "fields": [
        {"name": "title",   "selector": "h3.contentRow-title a", "type": "text"},
        {"name": "href",    "selector": "h3.contentRow-title a", "type": "attribute", "attribute": "href"},
        {"name": "date",    "selector": "time",                   "type": "attribute", "attribute": "datetime"},
        {"name": "snippet", "selector": "div.contentRow-snippet", "type": "text"},
    ]
}
```

### Post content extraction schema
```python
{
    "baseSelector": "article.message",
    "fields": [
        {"name": "author",  "selector": "a.username, span.username",       "type": "text"},
        {"name": "content", "selector": "div.bbWrapper, div.message-body", "type": "text"},
        {"name": "date",    "selector": "time",                            "type": "attribute", "attribute": "datetime"},
    ]
}
```

### Concurrent fetching
Post pages are batched via `arun_many()` — all discovered post URLs are fetched concurrently (up to `max_concurrent=4`), reducing total crawl time by ~3–4×.

**Fallback**: If `extracted_content` is empty (selector miss), the crawler falls back to `result.markdown` — crawl4ai's clean-text rendering of the page.

**No rating**: Tinhte has no star rating — `rating: null`.

---

## 6. Voz.vn — crawl4ai

**Method**: `crawl4ai` with Playwright. Same approach as Tinhte (both XenForo-based).

**Why crawl4ai here**: Voz has aggressive anti-bot on search pages and loads thread content dynamically. Also subject to login walls for some content.

### Search extraction schema
```python
{
    "baseSelector": "li.block-row",
    "fields": [
        {"name": "title", "selector": "h3.contentRow-title a", "type": "text"},
        {"name": "href",  "selector": "h3.contentRow-title a", "type": "attribute", "attribute": "href"},
        {"name": "date",  "selector": "time.u-dt",             "type": "attribute", "attribute": "datetime"},
    ]
}
```

### Thread message extraction schema
```python
{
    "baseSelector": "article.message",
    "fields": [
        {"name": "author",  "selector": "a.username",          "type": "text"},
        {"name": "content", "selector": "div.bbWrapper",       "type": "text"},
        {"name": "date",    "selector": "time.u-dt",           "type": "attribute", "attribute": "datetime"},
        {"name": "likes",   "selector": "a.reactionsBar-link", "type": "attribute", "attribute": "title"},
    ]
}
```

### Concurrent multi-page thread fetching
All thread page URLs (`thread/`, `thread/page-2/`, `thread/page-3/`) are collected upfront and fed to `arun_many()` in one batch. 404s (non-existent pages) are handled silently.

**Like count parsing**: `likes` attribute returns e.g. `"12 people reacted"` — parsed with regex `(\d[\d,]*)` → integer.

**No rating**: Voz has no star rating — `rating: null`.
