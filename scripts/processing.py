"""
processing.py — Normalize agent-collected raw review records into the canonical
review schema. Source-agnostic: the agent (via web_search/web_fetch) supplies
plain dicts with whatever fields it extracted; make_review() fills the rest.
"""

import hashlib
import re
from datetime import datetime, timezone


def make_id(source: str, author: str, content: str) -> str:
    normalized = re.sub(r"\s+", " ", (content or "").lower()).strip()
    normalized = re.sub(r"[^\w\sÀ-ɏḀ-ỿ]", "", normalized)
    key = f"{source}::{author}::{normalized[:200]}"
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def to_iso(dt):
    if dt is None:
        return None
    if isinstance(dt, datetime):
        # Label as UTC (assume input is already UTC / source-local); not a conversion.
        return dt.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(dt, (int, float)):
        return datetime.utcfromtimestamp(dt).replace(tzinfo=timezone.utc).isoformat()
    if isinstance(dt, str):
        return dt  # Already ISO-ish
    return str(dt)


_METADATA_DEFAULTS = {
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
    "review_title": None,
    "from_fallback": False,
}


def make_review(raw: dict, default_app: str = None) -> dict:
    """Normalize one agent-supplied raw record into the canonical schema."""
    source = raw.get("source")
    app = raw.get("app") or default_app
    author = raw.get("author")
    content = (raw.get("content") or "").strip()

    rating = raw.get("rating")
    if rating is not None:
        try:
            rating = int(rating)
        except (ValueError, TypeError):
            rating = None

    return {
        "id": raw.get("id") or make_id(source or "", author or "", content),
        "source": source,
        "app": app,
        "author": author,
        "rating": rating,
        "content": content,
        "date": to_iso(raw.get("date")),
        "url": raw.get("url"),
        "language": None,        # filled by pipeline.qualify
        "qualified": None,       # filled by pipeline.qualify
        "disqualification_reasons": [],
        "metadata": {**_METADATA_DEFAULTS, **(raw.get("metadata") or {})},
    }
