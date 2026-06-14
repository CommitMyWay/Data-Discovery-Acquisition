from __future__ import annotations

"""
sources/__init__.py — Source crawler implementations for all 6 platforms.
Each class inherits from BaseCrawler and implements crawl() → list[dict].

Source strategy (no browser engine required — see references/sources.md):
  Google Play  → google-play-scraper  (internal Play API, pure-Python, no browser)
  App Store    → iTunes RSS API       (stdlib urllib, clean JSON)
  YouTube      → yt-dlp               (pure-Python, handles auth + subtitles)
  Reddit       → public .json API     (stdlib urllib, read-only, no auth, no browser)
  Tinhte       → static HTML          (stdlib urllib + html.parser; best-effort → fallback)
  Voz          → static HTML          (stdlib urllib + html.parser; best-effort → fallback)

Only google-play-scraper and yt-dlp are third-party, and both are pure-Python
wheels (no Playwright/Chromium). The other four sources use the standard library
only, so this module imports cleanly even when nothing is pip-installed — the two
third-party libs are imported lazily inside their crawlers.
"""

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote

from scripts.crawl import BaseCrawler, CrawlError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stdlib HTTP helpers (replace `requests` + crawl4ai/Playwright)
# ---------------------------------------------------------------------------

# A realistic browser UA — XenForo forums (Tinhte/Voz) and Reddit serve clean
# HTML/JSON to this without a headless browser. urlopen raises HTTPError/URLError
# on any failure, which propagates through BaseCrawler.fetch_with_retry → retry →
# CrawlError → load_fallback(). That preserves the skill's retry/fallback contract.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
}


def _http_get_text(url: str, headers: dict = None, timeout: int = 20) -> str:
    """GET a URL and return decoded text. Raises on HTTP/network error."""
    req = urllib.request.Request(url, headers={**_DEFAULT_HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _http_get_json(url: str, headers: dict = None, timeout: int = 20):
    """GET a URL and parse the body as JSON. Raises on HTTP/network/parse error."""
    return json.loads(_http_get_text(url, headers=headers, timeout=timeout))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_id(source: str, author: str, content: str) -> str:
    normalized = re.sub(r'\s+', ' ', content.lower()).strip()
    normalized = re.sub(r'[^\w\sÀ-ɏḀ-ỿ]', '', normalized)
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
        url = (
            f"https://itunes.apple.com/rss/customerreviews/id={self.app_id}"
            f"/sortBy=mostRecent/json?country=vn&limit=50&page={page}"
        )
        data = _http_get_json(url)
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
# 4. Reddit  —  public JSON API (no auth, no browser)
# ---------------------------------------------------------------------------

# reddit.com exposes a read-only JSON view of every listing by appending `.json`.
# No OAuth, no credentials, no headless browser — just HTTP + a descriptive UA.
# Rate limit is ~60 req/min unauthenticated; we sleep between requests to stay under.

class RedditCrawler(BaseCrawler):
    """
    Reddit crawler using the public `.json` endpoints via stdlib urllib.

    Strategy:
      Phase A — GET /search.json (paginated via `after`), collect post listings
      Phase B — for the top posts, GET {permalink}.json → post selftext + comments
                (comments are walked recursively through the `replies` tree)
    """
    BASE_URL = "https://www.reddit.com"

    def __init__(self, search_query: str, max_search_pages: int = 3,
                 max_posts: int = 25, **kwargs):
        super().__init__(source_name="reddit", **kwargs)
        self.search_query = search_query
        self.max_search_pages = max_search_pages
        self.max_posts = max_posts  # how many posts to fetch comment threads for

    def _search(self) -> list:
        """Return raw post `child` dicts from paginated search.json."""
        children = []
        after = None
        for page in range(self.max_search_pages):
            url = (
                f"{self.BASE_URL}/search.json"
                f"?q={quote(self.search_query)}&sort=new&t=year&limit=100&type=link"
            )
            if after:
                url += f"&after={after}"
            data = self.fetch_with_retry(_http_get_json, url)
            batch = (data.get("data") or {}).get("children") or []
            if not batch:
                break
            children.extend(batch)
            after = (data.get("data") or {}).get("after")
            if not after:
                break
            time.sleep(1)
        return children

    def _fetch_thread(self, permalink: str):
        url = f"{self.BASE_URL}{permalink}.json?limit=200&depth=4"
        return self.fetch_with_retry(_http_get_json, url)

    def _walk_comments(self, children: list, out: list, meta: dict, depth: int = 1):
        """Recursively flatten the Reddit comment `replies` tree into reviews."""
        for child in children:
            if child.get("kind") != "t1":
                continue
            cd = child.get("data") or {}
            body = (cd.get("body") or "").strip()
            if body and body not in ("[deleted]", "[removed]"):
                out.append(_make_review(
                    source="reddit", app=self.app_name,
                    author=(cd.get("author") or "anonymous"),
                    rating=None,
                    content=body,
                    date=_to_iso(cd.get("created_utc")),
                    url=self.BASE_URL + meta["permalink"],
                    metadata={
                        "subreddit": meta.get("subreddit"),
                        "post_score": cd.get("score"),
                        "comment_depth": depth,
                        "thread_title": meta.get("title", ""),
                    },
                ))
            replies = cd.get("replies")
            if isinstance(replies, dict):
                self._walk_comments(
                    (replies.get("data") or {}).get("children") or [],
                    out, meta, depth + 1,
                )

    def crawl(self) -> list:
        results = []
        posts = self._search()
        logger.info("[reddit] Discovered %d posts", len(posts))

        for child in posts[:self.max_posts]:
            d = child.get("data") or {}
            permalink = d.get("permalink")
            if not permalink:
                continue

            title = (d.get("title") or "").strip()
            selftext = (d.get("selftext") or "").strip()
            content = f"{title}. {selftext}".strip() if selftext else title
            meta = {
                "permalink": permalink,
                "subreddit": d.get("subreddit"),
                "title": title,
            }

            # Post itself
            if content:
                results.append(_make_review(
                    source="reddit", app=self.app_name,
                    author=(d.get("author") or "anonymous"),
                    rating=None,
                    content=content,
                    date=_to_iso(d.get("created_utc")),
                    url=self.BASE_URL + permalink,
                    metadata={
                        "subreddit": d.get("subreddit"),
                        "post_score": d.get("score"),
                        "comment_depth": 0,
                        "thread_title": title,
                    },
                ))

            # Comments
            try:
                listing = self._fetch_thread(permalink)
                if isinstance(listing, list) and len(listing) > 1:
                    comment_children = (
                        (listing[1].get("data") or {}).get("children") or []
                    )
                    self._walk_comments(comment_children, results, meta, depth=1)
            except CrawlError as e:
                logger.warning("[reddit] Comments failed for %s: %s", permalink, e)
            time.sleep(1)

        logger.info("[reddit] Fetched %d items for query: %s", len(results), self.search_query)
        return results


# ---------------------------------------------------------------------------
# XenForo HTML parsing (shared by Tinhte and Voz — both run XenForo)
# ---------------------------------------------------------------------------

# Tinhte and Voz serve server-rendered XenForo HTML. We parse it with the stdlib
# html.parser instead of a headless browser. This is best-effort: if a page is
# Cloudflare-gated or the markup changes, fetch/parse fails → CrawlError →
# load_fallback(). That is the intended degraded-mode behaviour for these two.


class _LinkCollector(HTMLParser):
    """Collect every (href, link_text) pair from <a> tags in a page."""

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            # Flush any unterminated anchor before starting a new one
            if self._href is not None:
                self.links.append((self._href, "".join(self._text).strip()))
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text).strip()))
            self._href = None
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)


class _XenForoMessageParser(HTMLParser):
    """
    Extract posts from XenForo `<article class="message ..." data-author="...">`
    blocks. Author comes from the `data-author` attribute; body text from the
    first `<div class="bbWrapper">`; date from the first `<time datetime="...">`.
    """

    def __init__(self):
        super().__init__()
        self.messages: list[dict] = []
        self._article_level = 0          # 0 = not inside a message article
        self._author = None
        self._date = None
        self._parts: list[str] = []
        self._in_body = False
        self._body_div_level = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()

        if tag == "article" and "message" in classes:
            # Start of a (top-level) message
            self._article_level = 1
            self._author = a.get("data-author")
            self._date = None
            self._parts = []
            self._in_body = False
            self._body_div_level = 0
            return

        if self._article_level == 0:
            return

        if tag == "article":
            self._article_level += 1

        if tag == "time" and self._date is None and a.get("datetime"):
            self._date = a.get("datetime")

        if tag == "div" and not self._in_body and "bbWrapper" in classes:
            self._in_body = True
            self._body_div_level = 1
            return

        if self._in_body and tag == "div":
            self._body_div_level += 1

    def handle_endtag(self, tag):
        if self._article_level == 0:
            return

        if self._in_body and tag == "div":
            self._body_div_level -= 1
            if self._body_div_level == 0:
                self._in_body = False
            return

        if tag == "article":
            self._article_level -= 1
            if self._article_level == 0:
                content = re.sub(r"\s+", " ", "".join(self._parts)).strip()
                if content:
                    self.messages.append({
                        "author": (self._author or "anonymous").strip(),
                        "content": content,
                        "date": self._date,
                    })
                self._author = None
                self._date = None
                self._parts = []
                self._in_body = False
                self._body_div_level = 0

    def handle_data(self, data):
        if self._article_level and self._in_body:
            self._parts.append(data)


def _absolute(base: str, href: str) -> str:
    return href if href.startswith("http") else base + href


# ---------------------------------------------------------------------------
# 5. Tinhte.vn  —  static XenForo HTML (stdlib)
# ---------------------------------------------------------------------------

class TinhteCrawler(BaseCrawler):
    """
    Tinhte.vn crawler over server-rendered XenForo HTML (stdlib urllib).

    Phase A — fetch search result pages, collect thread URLs
    Phase B — fetch each thread, extract messages (author/content/date)

    Best-effort: Tinhte sometimes Cloudflare-gates search. On failure the base
    class falls back to the dataset, so the pipeline never crashes.
    """
    BASE_URL = "https://tinhte.vn"
    _THREAD_RE = re.compile(r"/threads?/[\w\-]+\.\d+")

    def __init__(self, search_query: str, max_pages: int = 5,
                 max_threads: int = 25, **kwargs):
        super().__init__(source_name="tinhte", **kwargs)
        self.search_query = search_query
        self.max_pages = max_pages
        self.max_threads = max_threads

    def _discover_threads(self) -> dict:
        """Return {thread_url: title} discovered from search pages."""
        found: dict[str, str] = {}
        for page in range(1, self.max_pages + 1):
            url = f"{self.BASE_URL}/search?q={quote(self.search_query)}&t=post&p={page}"
            html = self.fetch_with_retry(_http_get_text, url)
            collector = _LinkCollector()
            collector.feed(html)
            new_this_page = 0
            for href, text in collector.links:
                if not href or not self._THREAD_RE.search(href):
                    continue
                thread_url = _absolute(self.BASE_URL, self._THREAD_RE.search(href).group(0) + "/")
                if thread_url not in found:
                    found[thread_url] = text
                    new_this_page += 1
            if new_this_page == 0:
                break
            time.sleep(1.5)
        return found

    def crawl(self) -> list:
        results = []
        threads = self._discover_threads()
        logger.info("[tinhte] Discovered %d unique threads", len(threads))

        for thread_url, title in list(threads.items())[:self.max_threads]:
            try:
                html = self.fetch_with_retry(_http_get_text, thread_url)
            except CrawlError as e:
                logger.debug("[tinhte] Thread fetch failed %s: %s", thread_url, e)
                continue

            parser = _XenForoMessageParser()
            parser.feed(html)

            for msg in parser.messages:
                content = msg["content"]
                if not content:
                    continue
                results.append(_make_review(
                    source="tinhte", app=self.app_name,
                    author=msg["author"],
                    rating=None,
                    content=f"{title}. {content}" if title else content,
                    date=msg["date"],
                    url=thread_url,
                    metadata={"thread_title": title},
                ))
            time.sleep(2)

        logger.info("[tinhte] Fetched %d posts for query: %s", len(results), self.search_query)
        return results


# ---------------------------------------------------------------------------
# 6. Voz.vn  —  static XenForo HTML (stdlib)
# ---------------------------------------------------------------------------

class VozCrawler(BaseCrawler):
    """
    Voz.vn crawler over server-rendered XenForo HTML (stdlib urllib).

    Voz uses the same XenForo engine as Tinhte (different thread URL shape: /t/).
    Threads paginate as /t/<slug>.<id>/page-2/ etc.
    """
    BASE_URL = "https://voz.vn"
    _THREAD_RE = re.compile(r"/t/[\w\-]+\.\d+")

    def __init__(self, search_query: str, max_pages: int = 5,
                 max_thread_pages: int = 3, max_threads: int = 20, **kwargs):
        super().__init__(source_name="voz", **kwargs)
        self.search_query = search_query
        self.max_pages = max_pages
        self.max_thread_pages = max_thread_pages
        self.max_threads = max_threads

    def _normalize_thread_url(self, href: str) -> str:
        """Strip post anchors / page suffixes and ensure an absolute, trailing-slash URL."""
        match = self._THREAD_RE.search(href)
        path = match.group(0) if match else href
        url = _absolute(self.BASE_URL, path)
        if not url.endswith("/"):
            url += "/"
        return url

    def _discover_threads(self) -> dict:
        """Return {thread_root_url: title} discovered from search pages."""
        found: dict[str, str] = {}
        for page in range(1, self.max_pages + 1):
            url = (f"{self.BASE_URL}/search/"
                   f"?q={quote(self.search_query)}&type=post&page={page}")
            html = self.fetch_with_retry(_http_get_text, url)
            collector = _LinkCollector()
            collector.feed(html)
            new_this_page = 0
            for href, text in collector.links:
                if not href or not self._THREAD_RE.search(href):
                    continue
                thread_url = self._normalize_thread_url(href)
                if thread_url not in found:
                    found[thread_url] = text
                    new_this_page += 1
            if new_this_page == 0:
                break
            time.sleep(1.5)
        return found

    def crawl(self) -> list:
        results = []
        threads = self._discover_threads()
        logger.info("[voz] Discovered %d unique threads", len(threads))

        for thread_url, title in list(threads.items())[:self.max_threads]:
            for pg in range(1, self.max_thread_pages + 1):
                page_url = thread_url if pg == 1 else f"{thread_url}page-{pg}/"
                try:
                    html = self.fetch_with_retry(_http_get_text, page_url)
                except CrawlError as e:
                    # Last page of a thread 404s — that's expected, stop paging
                    logger.debug("[voz] Thread page failed %s: %s", page_url, e)
                    break

                parser = _XenForoMessageParser()
                parser.feed(html)
                if not parser.messages:
                    break  # no more posts on this/subsequent pages

                for msg in parser.messages:
                    content = msg["content"]
                    if not content:
                        continue
                    results.append(_make_review(
                        source="voz", app=self.app_name,
                        author=msg["author"],
                        rating=None,
                        content=content,
                        date=msg["date"],
                        url=thread_url,
                        metadata={"thread_title": title, "like_count": None},
                    ))
                time.sleep(2)

        logger.info("[voz] Fetched %d posts for query: %s", len(results), self.search_query)
        return results
