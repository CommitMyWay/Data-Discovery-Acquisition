from __future__ import annotations

"""
sources/__init__.py — Source crawler implementations for all 6 platforms.
Each class inherits from BaseCrawler and implements crawl() → list[dict].

Source strategy:
  Google Play  → google-play-scraper  (internal Play API, no scraping needed)
  App Store    → iTunes RSS API       (clean JSON, no scraping needed)
  YouTube      → yt-dlp               (gold standard, handles auth + subtitles)
  Reddit       → public .json API     (read-only, no auth needed)
  Tinhte       → crawl4ai             (JS rendering + anti-bot for XenForo)
  Voz          → crawl4ai             (JS rendering + anti-bot for XenForo)
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

from scripts.crawl import BaseCrawler, CrawlError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_id(source: str, author: str, content: str) -> str:
    normalized = re.sub(r'\s+', ' ', content.lower()).strip()
    normalized = re.sub(r'[^\w\s\u00c0-\u024f\u1e00-\u1eff]', '', normalized)
    key = f"{source}::{author}::{normalized[:200]}"
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _to_iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(dt, (int, float)):
        return datetime.utcfromtimestamp(dt).replace(tzinfo=timezone.utc).isoformat()
    if isinstance(dt, str):
        return dt  # Already ISO-ish
    return str(dt)


def _make_review(
    source, app, author, rating, content, date, url,
    metadata=None, **kwargs
) -> dict:
    return {
        "id": _make_id(source, author or "", content or ""),
        "source": source,
        "app": app,
        "author": author,
        "rating": int(rating) if rating is not None else None,
        "content": (content or "").strip(),
        "date": _to_iso(date),
        "url": url,
        "language": None,       # Filled by pipeline.py
        "qualified": None,      # Filled by pipeline.py
        "disqualification_reasons": [],
        "metadata": {
            "thumbs_up": 0,
            "reply_content": None,
            "high_quality": False,
            "raw_source_id": None,
            "video_id": None,
            "video_title": None,
            "video_url": None,
            "is_transcript": False,
            "subreddit": None,
            "post_score": None,
            "comment_depth": None,
            "thread_title": None,
            "like_count": None,
            "from_fallback": False,
            **(metadata or {}),
        },
    }


# ---------------------------------------------------------------------------
# 1. Google Play
# ---------------------------------------------------------------------------

class GooglePlayCrawler(BaseCrawler):
    def __init__(self, app_id: str, **kwargs):
        super().__init__(source_name="google_play", **kwargs)
        self.app_id = app_id

    def _fetch_batch(self, continuation_token=None):
        from google_play_scraper import reviews, Sort
        return reviews(
            self.app_id,
            lang="vi",
            country="vn",
            sort=Sort.NEWEST,
            count=200,
            continuation_token=continuation_token,
        )

    def crawl(self) -> list:
        results = []
        token = None

        for page in range(10):  # Max 10 pages = ~2000 reviews
            batch, token = self.fetch_with_retry(self._fetch_batch, continuation_token=token)
            for r in batch:
                if not r.get("content"):
                    continue
                results.append(_make_review(
                    source="google_play",
                    app=self.app_name,
                    author=r.get("userName", "anonymous"),
                    rating=r.get("score"),
                    content=r.get("content", ""),
                    date=r.get("at"),
                    url=f"https://play.google.com/store/apps/details?id={self.app_id}",
                    metadata={
                        "thumbs_up": r.get("thumbsUpCount", 0),
                        "reply_content": r.get("replyContent"),
                        "raw_source_id": r.get("reviewId"),
                    },
                ))
            if not token or len(batch) == 0:
                break
            time.sleep(1)

        logger.info("[google_play] Fetched %d reviews for %s", len(results), self.app_id)
        return results


# ---------------------------------------------------------------------------
# 2. Apple App Store
# ---------------------------------------------------------------------------

class AppStoreCrawler(BaseCrawler):
    def __init__(self, app_id: str, **kwargs):
        super().__init__(source_name="app_store", **kwargs)
        self.app_id = app_id

    def _fetch_page(self, page: int):
        import requests

        url = (
            f"https://itunes.apple.com/rss/customerreviews/id={self.app_id}"
            f"/sortBy=mostRecent/json?country=vn&limit=50&page={page}"
        )
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data.get("feed", {}).get("entry", [])

    def crawl(self) -> list:
        results = []
        for page in range(1, 11):  # Max 10 pages
            entries = self.fetch_with_retry(self._fetch_page, page=page)
            if not entries:
                break

            # Skip first entry if it's app metadata (no "im:rating" field)
            for entry in entries:
                if "im:rating" not in entry:
                    continue
                content = entry.get("content", {}).get("label", "")
                if not content:
                    continue
                results.append(_make_review(
                    source="app_store",
                    app=self.app_name,
                    author=entry.get("author", {}).get("name", {}).get("label", "anonymous"),
                    rating=entry.get("im:rating", {}).get("label"),
                    content=content,
                    date=entry.get("updated", {}).get("label"),
                    url=entry.get("link", {}).get("attributes", {}).get("href", ""),
                    metadata={
                        "raw_source_id": entry.get("id", {}).get("label"),
                        "review_title": entry.get("title", {}).get("label"),
                    },
                ))
            time.sleep(2)

        logger.info("[app_store] Fetched %d reviews for app %s", len(results), self.app_id)
        return results


# ---------------------------------------------------------------------------
# 3. YouTube (comments + transcripts)
# ---------------------------------------------------------------------------

class YouTubeCrawler(BaseCrawler):
    def __init__(self, search_query: str, max_videos: int = 10, **kwargs):
        super().__init__(source_name="youtube", **kwargs)
        self.search_query = search_query
        self.max_videos = max_videos

    def _search_videos(self) -> list:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "extract_flat": True,
            "playlistend": self.max_videos,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(
                f"ytsearch{self.max_videos}:{self.search_query}",
                download=False
            )
        return [e for e in result.get("entries", []) if e]

    def _get_comments(self, video_id: str) -> list:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "getcomments": True,
            "extractor_args": {"youtube": {"comment_sort": ["top"]}},
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
        return info.get("comments", []) or []

    def _get_transcript(self, video_id: str) -> str | None:
        import yt_dlp, glob, os, tempfile
        tmpdir = tempfile.mkdtemp()
        ydl_opts = {
            "quiet": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["vi", "vi-VN", "en"],
            "skip_download": True,
            "outtmpl": os.path.join(tmpdir, "%(id)s"),
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
            # Find any .vtt or .srt file
            sub_files = glob.glob(os.path.join(tmpdir, "*.vtt")) + \
                        glob.glob(os.path.join(tmpdir, "*.srt"))
            if not sub_files:
                return None
            with open(sub_files[0], "r", encoding="utf-8") as f:
                raw = f.read()
            # Strip VTT/SRT timestamps and tags
            text = re.sub(r'\d{2}:\d{2}[\d:.,]+ --> [\d:.,]+\s*', '', raw)
            text = re.sub(r'<[^>]+>', '', text)
            text = re.sub(r'WEBVTT.*?\n\n', '', text, flags=re.DOTALL)
            text = re.sub(r'\n{2,}', '\n', text).strip()
            return text if len(text) > 50 else None
        except Exception as e:
            logger.debug("[youtube] Transcript fetch failed for %s: %s", video_id, e)
            return None
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def crawl(self) -> list:
        results = []
        videos = self.fetch_with_retry(self._search_videos)

        for video in videos:
            vid_id = video.get("id")
            vid_title = video.get("title", "")
            vid_url = f"https://www.youtube.com/watch?v={vid_id}"
            vid_date = _to_iso(video.get("timestamp"))

            # Comments
            try:
                comments = self.fetch_with_retry(self._get_comments, vid_id)
                for c in comments:
                    results.append(_make_review(
                        source="youtube",
                        app=self.app_name,
                        author=c.get("author", "anonymous"),
                        rating=None,
                        content=c.get("text", ""),
                        date=_to_iso(c.get("timestamp")),
                        url=vid_url,
                        metadata={
                            "video_id": vid_id,
                            "video_title": vid_title,
                            "video_url": vid_url,
                            "is_transcript": False,
                            "thumbs_up": c.get("like_count", 0),
                        },
                    ))
            except CrawlError as e:
                logger.warning("[youtube] Comments failed for %s: %s", vid_id, e)

            # Transcript
            try:
                transcript = self.fetch_with_retry(self._get_transcript, vid_id)
                if transcript:
                    results.append(_make_review(
                        source="youtube",
                        app=self.app_name,
                        author="[transcript]",
                        rating=None,
                        content=transcript,
                        date=vid_date,
                        url=vid_url,
                        metadata={
                            "video_id": vid_id,
                            "video_title": vid_title,
                            "video_url": vid_url,
                            "is_transcript": True,
                        },
                    ))
            except CrawlError as e:
                logger.warning("[youtube] Transcript failed for %s: %s", vid_id, e)

            time.sleep(3)

        logger.info("[youtube] Fetched %d items for query: %s", len(results), self.search_query)
        return results


# ---------------------------------------------------------------------------
# 4. Reddit
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 4. Reddit  —  crawl4ai (old.reddit.com, no credentials needed)
# ---------------------------------------------------------------------------

# old.reddit.com has stable, static HTML — far easier to parse than new Reddit's
# React SPA. magic=True handles the occasional Cloudflare check.

_REDDIT_SEARCH_SCHEMA = {
    "name": "RedditSearchResults",
    "baseSelector": "div.thing.link",
    "fields": [
        {"name": "title",     "selector": "a.title",               "type": "text"},
        {"name": "href",      "selector": "a.title",               "type": "attribute", "attribute": "href"},
        {"name": "date",      "selector": "time.live-timestamp",   "type": "attribute", "attribute": "datetime"},
        {"name": "score",     "selector": "span.score.unvoted",    "type": "text"},
        {"name": "subreddit", "selector": "a.subreddit",           "type": "text"},
    ],
}

_REDDIT_POST_SCHEMA = {
    "name": "RedditPostContent",
    "baseSelector": "div.thing.link",
    "fields": [
        {"name": "selftext", "selector": "div.usertext-body div.md", "type": "text"},
        {"name": "title",    "selector": "a.title",                  "type": "text"},
        {"name": "author",   "selector": "a.author",                 "type": "text"},
        {"name": "date",     "selector": "time.live-timestamp",      "type": "attribute", "attribute": "datetime"},
        {"name": "score",    "selector": "span.score.unvoted",       "type": "text"},
    ],
}

_REDDIT_COMMENTS_SCHEMA = {
    "name": "RedditComments",
    "baseSelector": "div.comment",
    "fields": [
        {"name": "author",  "selector": "a.author",               "type": "text"},
        {"name": "content", "selector": "div.usertext-body",      "type": "text"},
        {"name": "date",    "selector": "time.live-timestamp",    "type": "attribute", "attribute": "datetime"},
        {"name": "score",   "selector": "span.score",             "type": "text"},
    ],
}


class RedditCrawler(BaseCrawler):
    """
    Reddit crawler using crawl4ai against old.reddit.com.

    No credentials required — old.reddit.com serves static HTML that
    crawl4ai's Playwright browser renders cleanly. The magic=True flag
    handles Cloudflare checks that occasionally appear.

    Strategy:
      Phase A — Search old.reddit.com, collect post URLs
      Phase B — Fetch each post page concurrently via arun_many()
                Extract: post selftext + all comments
    """
    BASE_URL = "https://old.reddit.com"

    def __init__(self, search_query: str, max_search_pages: int = 3,
                 max_concurrent: int = 4, **kwargs):
        super().__init__(source_name="reddit", **kwargs)
        self.search_query = search_query
        self.max_search_pages = max_search_pages
        self.max_concurrent = max_concurrent

    async def _async_crawl(self) -> list:
        from crawl4ai import AsyncWebCrawler
        from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

        browser_cfg = _make_browser_config()
        results = []
        post_meta: dict[str, dict] = {}  # url → {title, subreddit, date, score}

        async with AsyncWebCrawler(config=browser_cfg) as crawler:

            # ── Phase A: Collect post URLs from search ───────────────────────
            search_cfg = _make_run_config(
                wait_for="css:div.thing.link",
                extraction_strategy=JsonCssExtractionStrategy(_REDDIT_SEARCH_SCHEMA),
                mean_delay=1.5,
            )

            next_url = (
                f"{self.BASE_URL}/search"
                f"?q={quote(self.search_query)}&sort=new&t=year&type=link"
            )

            for page in range(self.max_search_pages):
                if not next_url:
                    break

                result = await crawler.arun(url=next_url, config=search_cfg)

                if not result.success:
                    logger.warning("[reddit] Search page %d failed: %s", page + 1, result.error_message)
                    break

                try:
                    rows = json.loads(result.extracted_content or "[]")
                except (json.JSONDecodeError, TypeError):
                    rows = []

                if not rows:
                    break

                for row in rows:
                    href = row.get("href", "")
                    if not href or href in post_meta:
                        continue
                    # Ensure we use old.reddit.com for post pages too
                    post_url = re.sub(r'https?://(www\.)?reddit\.com', self.BASE_URL, href)
                    if not post_url.startswith("http"):
                        post_url = self.BASE_URL + post_url
                    post_meta[post_url] = {
                        "title":     row.get("title", "").strip(),
                        "subreddit": row.get("subreddit", ""),
                        "date":      row.get("date"),
                        "score":     row.get("score", "0"),
                    }

                # Find next-page link from rendered HTML
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(result.html or "", "lxml")
                next_el = soup.select_one("span.next-button a")
                next_url = next_el["href"] if next_el else None

            logger.info("[reddit] Discovered %d posts", len(post_meta))

            # ── Phase B: Fetch post content + comments concurrently ──────────
            # Fetch post page with both schemas — run two passes per URL
            # (post body and comments use different baseSelectors)
            post_cfg = _make_run_config(
                wait_for="css:div.thing.link",
                extraction_strategy=JsonCssExtractionStrategy(_REDDIT_POST_SCHEMA),
                mean_delay=1.5,
            )
            comment_cfg = _make_run_config(
                wait_for="css:div.comment",
                extraction_strategy=JsonCssExtractionStrategy(_REDDIT_COMMENTS_SCHEMA),
                mean_delay=1.5,
            )

            post_urls = list(post_meta.keys())

            # Fetch post body
            post_responses = await crawler.arun_many(urls=post_urls, config=post_cfg)
            for resp in post_responses:
                meta = post_meta.get(resp.url, {})
                if not resp.success:
                    logger.debug("[reddit] Post fetch failed %s: %s", resp.url, resp.error_message)
                    continue
                try:
                    posts = json.loads(resp.extracted_content or "[]")
                except (json.JSONDecodeError, TypeError):
                    posts = []

                for p in posts:
                    selftext = (p.get("selftext") or "").strip()
                    title = (p.get("title") or meta.get("title", "")).strip()
                    content = f"{title}. {selftext}".strip() if selftext else title
                    if not content:
                        continue
                    try:
                        score = int(re.sub(r'[^\d]', '', p.get("score") or "0") or "0")
                    except ValueError:
                        score = 0
                    results.append(_make_review(
                        source="reddit", app=self.app_name,
                        author=(p.get("author") or "anonymous").strip(),
                        rating=None,
                        content=content,
                        date=p.get("date") or meta.get("date"),
                        url=resp.url,
                        metadata={
                            "subreddit": meta.get("subreddit"),
                            "post_score": score,
                            "comment_depth": 0,
                            "thread_title": title,
                        },
                    ))

            # Fetch comments
            comment_responses = await crawler.arun_many(urls=post_urls, config=comment_cfg)
            for resp in comment_responses:
                meta = post_meta.get(resp.url, {})
                if not resp.success:
                    continue
                try:
                    comments = json.loads(resp.extracted_content or "[]")
                except (json.JSONDecodeError, TypeError):
                    comments = []

                for c in comments:
                    content = (c.get("content") or "").strip()
                    if not content:
                        continue
                    try:
                        score = int(re.sub(r'[^\d]', '', c.get("score") or "0") or "0")
                    except ValueError:
                        score = 0
                    results.append(_make_review(
                        source="reddit", app=self.app_name,
                        author=(c.get("author") or "anonymous").strip(),
                        rating=None,
                        content=content,
                        date=c.get("date"),
                        url=resp.url,
                        metadata={
                            "subreddit": meta.get("subreddit"),
                            "post_score": score,
                            "comment_depth": 1,
                            "thread_title": meta.get("title", ""),
                        },
                    ))

        logger.info("[reddit] Fetched %d items for query: %s", len(results), self.search_query)
        return results

    def crawl(self) -> list:
        return _run_async(self._async_crawl())


# ---------------------------------------------------------------------------
# Shared crawl4ai config (used by both Tinhte and Voz)
# ---------------------------------------------------------------------------

def _make_browser_config():
    from crawl4ai import BrowserConfig

    return BrowserConfig(
        headless=True,
        browser_type="chromium",
    )


def _make_run_config(
    wait_for: str = None,
    extraction_strategy=None,
    css_selector: str = None,
    page_timeout: int = 30_000,
    mean_delay: float = 1.5,
):
    from crawl4ai import CacheMode, CrawlerRunConfig

    return CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,          # Always fetch fresh
        magic=True,                            # Auto anti-bot: hides automation signals
        simulate_user=True,                    # Human-like mouse/scroll behavior
        override_navigator=True,               # Spoof navigator.webdriver
        remove_consent_popups=True,            # Auto-dismiss cookie banners
        scan_full_page=True,                   # Scroll full page for lazy-loaded content
        user_agent_mode="random",              # Rotate user agents per request
        page_timeout=page_timeout,
        wait_for=wait_for,
        extraction_strategy=extraction_strategy,
        css_selector=css_selector,
        mean_delay=mean_delay,                 # Randomized inter-action delay
        max_range=0.5,                         # ± 0.5s jitter on mean_delay
        verbose=False,
    )


def _run_async(coro):
    """Run an async coroutine from sync context safely."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Already inside an event loop (e.g. Jupyter) — use nest_asyncio
            import nest_asyncio
            nest_asyncio.apply()
            return loop.run_until_complete(coro)
    except RuntimeError:
        pass
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 5. Tinhte.vn  —  crawl4ai
# ---------------------------------------------------------------------------

# Structured extraction schemas for Tinhte
_TINHTE_SEARCH_SCHEMA = {
    "name": "TinhteSearchResults",
    "baseSelector": "div.contentRow",
    "fields": [
        {"name": "title",   "selector": "h3.contentRow-title a, a.contentRow-title", "type": "text"},
        {"name": "href",    "selector": "h3.contentRow-title a, a.contentRow-title", "type": "attribute", "attribute": "href"},
        {"name": "date",    "selector": "time",                                       "type": "attribute", "attribute": "datetime"},
        {"name": "snippet", "selector": "div.contentRow-snippet",                     "type": "text"},
    ],
}

_TINHTE_POST_SCHEMA = {
    "name": "TinhtePost",
    "baseSelector": "article.message",
    "fields": [
        {"name": "author",  "selector": "a.username, span.username",                  "type": "text"},
        {"name": "content", "selector": "div.bbWrapper, div.message-body",            "type": "text"},
        {"name": "date",    "selector": "time",                                       "type": "attribute", "attribute": "datetime"},
    ],
}


class TinhteCrawler(BaseCrawler):
    """
    Tinhte.vn crawler using crawl4ai (Playwright-based).

    Improvements over requests+BeautifulSoup:
    - Renders JS-heavy pages (lazy-loaded content, login walls)
    - magic=True + simulate_user: bypasses Cloudflare/bot detection
    - JsonCssExtractionStrategy: structured, resilient extraction
    - arun_many(): concurrent post fetching (3–4× faster)
    """
    BASE_URL = "https://tinhte.vn"

    def __init__(self, search_query: str, max_pages: int = 5, max_concurrent: int = 4, **kwargs):
        super().__init__(source_name="tinhte", **kwargs)
        self.search_query = search_query
        self.max_pages = max_pages
        self.max_concurrent = max_concurrent  # Concurrent post page fetches

    async def _async_crawl(self) -> list:
        from crawl4ai import AsyncWebCrawler
        from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

        browser_cfg = _make_browser_config()
        results = []
        seen_urls = set()
        post_urls_meta = []  # (post_url, title, snippet, date_from_search)

        async with AsyncWebCrawler(config=browser_cfg) as crawler:

            # ── Phase A: Collect post URLs from search results ──────────────
            search_cfg = _make_run_config(
                wait_for="css:div.contentRow",
                extraction_strategy=JsonCssExtractionStrategy(_TINHTE_SEARCH_SCHEMA),
            )
            for page in range(1, self.max_pages + 1):
                url = f"{self.BASE_URL}/search?q={quote(self.search_query)}&t=post&p={page}"
                result = await crawler.arun(url=url, config=search_cfg)

                if not result.success:
                    logger.warning("[tinhte] Search page %d failed: %s", page, result.error_message)
                    break

                try:
                    rows = json.loads(result.extracted_content or "[]")
                except (json.JSONDecodeError, TypeError):
                    rows = []

                if not rows:
                    break

                for row in rows:
                    href = row.get("href", "")
                    if not href:
                        continue
                    post_url = href if href.startswith("http") else self.BASE_URL + href
                    if post_url in seen_urls:
                        continue
                    seen_urls.add(post_url)
                    post_urls_meta.append((
                        post_url,
                        row.get("title", "").strip(),
                        row.get("snippet", "").strip(),
                        row.get("date"),
                    ))

            logger.info("[tinhte] Discovered %d unique posts", len(post_urls_meta))

            # ── Phase B: Fetch post content concurrently ─────────────────────
            post_cfg = _make_run_config(
                wait_for="css:article.message, div.bbWrapper",
                extraction_strategy=JsonCssExtractionStrategy(_TINHTE_POST_SCHEMA),
                mean_delay=2.0,
            )
            urls_only = [u for u, *_ in post_urls_meta]
            meta_map = {u: (t, s, d) for u, t, s, d in post_urls_meta}

            # arun_many handles concurrency + rate limiting internally
            responses = await crawler.arun_many(urls=urls_only, config=post_cfg)

            for resp in responses:
                post_url = resp.url
                title, snippet, search_date = meta_map.get(post_url, ("", "", None))

                if not resp.success:
                    logger.debug("[tinhte] Post fetch failed %s: %s", post_url, resp.error_message)
                    # Fallback: use snippet from search
                    if snippet:
                        results.append(_make_review(
                            source="tinhte", app=self.app_name,
                            author="anonymous", rating=None,
                            content=f"{title}. {snippet}",
                            date=search_date, url=post_url,
                            metadata={"thread_title": title},
                        ))
                    continue

                try:
                    messages = json.loads(resp.extracted_content or "[]")
                except (json.JSONDecodeError, TypeError):
                    messages = []

                if not messages:
                    # Use clean markdown as fallback if CSS extraction misses
                    content = resp.markdown or snippet
                    if content:
                        results.append(_make_review(
                            source="tinhte", app=self.app_name,
                            author="anonymous", rating=None,
                            content=f"{title}. {content[:2000]}",
                            date=search_date, url=post_url,
                            metadata={"thread_title": title},
                        ))
                    continue

                for msg in messages:
                    content = (msg.get("content") or "").strip()
                    if not content:
                        continue
                    results.append(_make_review(
                        source="tinhte", app=self.app_name,
                        author=(msg.get("author") or "anonymous").strip(),
                        rating=None,
                        content=f"{title}. {content}",
                        date=msg.get("date") or search_date,
                        url=post_url,
                        metadata={"thread_title": title},
                    ))

        logger.info("[tinhte] Fetched %d posts for query: %s", len(results), self.search_query)
        return results

    def crawl(self) -> list:
        return _run_async(self._async_crawl())


# ---------------------------------------------------------------------------
# 6. Voz.vn  —  crawl4ai
# ---------------------------------------------------------------------------

_VOZ_SEARCH_SCHEMA = {
    "name": "VozSearchResults",
    "baseSelector": "li.block-row",
    "fields": [
        {"name": "title", "selector": "h3.contentRow-title a", "type": "text"},
        {"name": "href",  "selector": "h3.contentRow-title a", "type": "attribute", "attribute": "href"},
        {"name": "date",  "selector": "time.u-dt",             "type": "attribute", "attribute": "datetime"},
    ],
}

_VOZ_THREAD_SCHEMA = {
    "name": "VozThreadMessages",
    "baseSelector": "article.message",
    "fields": [
        {"name": "author",  "selector": "a.username",          "type": "text"},
        {"name": "content", "selector": "div.bbWrapper",       "type": "text"},
        {"name": "date",    "selector": "time.u-dt",           "type": "attribute", "attribute": "datetime"},
        {"name": "likes",   "selector": "a.reactionsBar-link", "type": "attribute", "attribute": "title"},
    ],
}


class VozCrawler(BaseCrawler):
    """
    Voz.vn crawler using crawl4ai (Playwright-based).

    Voz uses XenForo — same engine as Tinhte but different selectors.
    Key improvement: arun_many() fetches all thread pages concurrently,
    cutting crawl time from ~O(threads × pages × 2s) to ~O(pages × 2s).
    """
    BASE_URL = "https://voz.vn"

    def __init__(self, search_query: str, max_pages: int = 5,
                 max_thread_pages: int = 3, max_concurrent: int = 4, **kwargs):
        super().__init__(source_name="voz", **kwargs)
        self.search_query = search_query
        self.max_pages = max_pages
        self.max_thread_pages = max_thread_pages
        self.max_concurrent = max_concurrent

    def _normalize_thread_url(self, href: str) -> str:
        """Strip post anchors and ensure absolute URL."""
        url = re.sub(r'/post-\d+/?$', '/', href)
        url = url if url.startswith("http") else self.BASE_URL + url
        if not url.endswith("/"):
            url += "/"
        return url

    async def _async_crawl(self) -> list:
        from crawl4ai import AsyncWebCrawler
        from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

        browser_cfg = _make_browser_config()
        results = []
        seen_threads = set()
        thread_meta: dict[str, str] = {}  # url → title

        async with AsyncWebCrawler(config=browser_cfg) as crawler:

            # ── Phase A: Discover thread URLs from search ────────────────────
            search_cfg = _make_run_config(
                wait_for="css:li.block-row",
                extraction_strategy=JsonCssExtractionStrategy(_VOZ_SEARCH_SCHEMA),
            )
            for page in range(1, self.max_pages + 1):
                url = (f"{self.BASE_URL}/search/"
                       f"?q={quote(self.search_query)}&type=post&page={page}")
                result = await crawler.arun(url=url, config=search_cfg)

                if not result.success:
                    logger.warning("[voz] Search page %d failed: %s", page, result.error_message)
                    break

                try:
                    rows = json.loads(result.extracted_content or "[]")
                except (json.JSONDecodeError, TypeError):
                    rows = []

                if not rows:
                    break

                for row in rows:
                    href = row.get("href", "")
                    if not href:
                        continue
                    thread_url = self._normalize_thread_url(href)
                    if thread_url in seen_threads:
                        continue
                    seen_threads.add(thread_url)
                    thread_meta[thread_url] = row.get("title", "").strip()

            logger.info("[voz] Discovered %d unique threads", len(seen_threads))

            # ── Phase B: Build all page URLs for all threads ─────────────────
            # e.g. thread with 3 pages → 3 URLs to fetch
            all_page_urls = []
            for t_url in seen_threads:
                all_page_urls.append(t_url)  # page 1
                for pg in range(2, self.max_thread_pages + 1):
                    all_page_urls.append(f"{t_url}page-{pg}/")

            # ── Phase C: Fetch all thread pages concurrently ─────────────────
            thread_cfg = _make_run_config(
                wait_for="css:article.message",
                extraction_strategy=JsonCssExtractionStrategy(_VOZ_THREAD_SCHEMA),
                mean_delay=2.0,
            )
            responses = await crawler.arun_many(urls=all_page_urls, config=thread_cfg)

            for resp in responses:
                # Resolve back to thread root for metadata lookup
                thread_root = self._normalize_thread_url(
                    re.sub(r'page-\d+/$', '', resp.url)
                )
                title = thread_meta.get(thread_root, "")

                if not resp.success:
                    # 404 = thread page doesn't exist (normal for last page) — skip quietly
                    if "404" not in str(resp.error_message):
                        logger.debug("[voz] Thread page failed %s: %s", resp.url, resp.error_message)
                    continue

                try:
                    messages = json.loads(resp.extracted_content or "[]")
                except (json.JSONDecodeError, TypeError):
                    messages = []

                for msg in messages:
                    content = (msg.get("content") or "").strip()
                    if not content:
                        continue

                    # Parse like count from e.g. "12 people reacted" → 12
                    likes_raw = msg.get("likes") or "0"
                    try:
                        likes = int(re.match(r"(\d[\d,]*)", likes_raw).group(1).replace(",", ""))
                    except (AttributeError, ValueError):
                        likes = 0

                    results.append(_make_review(
                        source="voz", app=self.app_name,
                        author=(msg.get("author") or "anonymous").strip(),
                        rating=None,
                        content=content,
                        date=msg.get("date"),
                        url=thread_root,
                        metadata={"thread_title": title, "like_count": likes},
                    ))

        logger.info("[voz] Fetched %d posts for query: %s", len(results), self.search_query)
        return results

    def crawl(self) -> list:
        return _run_async(self._async_crawl())
