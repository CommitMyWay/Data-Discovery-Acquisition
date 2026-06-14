"""
agent_api.py — Processing interface for the OpenClaw agent.

Collection is done by the agent itself using the built-in web_search and
web_fetch tools (see SKILL.md). The agent assembles the raw review records it
extracted and passes them here. process_reviews() runs the deterministic
pipeline (dedup, qualification, near-dup, stats) and returns analysis-ready data.

    from scripts.agent_api import process_reviews

    data = process_reviews(
        raw_reviews,            # list of dicts the agent built from web_fetch output
        apps=["MoMo", "ZaloPay"],
        goal="product",
        days_back=180,
        focus_area="Login",
    )
    # data["reviews"], data["reviews_by_app"], data["references"], data["stats"]
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.processing import make_review
from scripts.pipeline import deduplicate, qualify, mark_near_duplicates

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App registry — pre-resolved IDs (mirrors references/fintech-apps.md).
# Used to resolve display names here and as discovery reference data the agent
# reads in Phase 1 of SKILL.md.
# ---------------------------------------------------------------------------

APP_REGISTRY = {
    "momo": {
        "display_name": "MoMo",
        "android_id": "com.mservice.momotransfer",
        "ios_id": "918751511",
        "youtube_query": "MoMo ví điện tử review đánh giá",
        "reddit_query": "MoMo Vietnam e-wallet payment",
        "tinhte_query": "momo ví điện tử",
        "voz_query": "momo ví điện tử",
    },
    "zalopay": {
        "display_name": "ZaloPay",
        "android_id": "com.vinagame.zalopay",
        "ios_id": "1107454800",
        "youtube_query": "ZaloPay review đánh giá ví điện tử",
        "reddit_query": "ZaloPay Vietnam payment wallet",
        "tinhte_query": "zalopay",
        "voz_query": "zalopay",
    },
    "shopeepay": {
        "display_name": "ShopeePay",
        "android_id": "com.shopee.vn",
        "ios_id": "959841854",
        "youtube_query": "ShopeePay review đánh giá thanh toán",
        "reddit_query": "ShopeePay Vietnam Shopee payment",
        "tinhte_query": "shopeepay",
        "voz_query": "shopeepay shopee pay",
    },
    "vnpay": {
        "display_name": "VNPay",
        "android_id": "com.vnpay.vnpayqr",
        "ios_id": "1436080875",
        "youtube_query": "VNPay review đánh giá QR thanh toán",
        "reddit_query": "VNPay Vietnam QR payment",
        "tinhte_query": "vnpay",
        "voz_query": "vnpay",
    },
    "viettelmoney": {
        "display_name": "ViettelMoney",
        "android_id": "com.viettel.viettelmoney",
        "ios_id": "1493028346",
        "youtube_query": "ViettelMoney review đánh giá",
        "reddit_query": "ViettelMoney Vietnam Viettel Pay",
        "tinhte_query": "viettelmoney",
        "voz_query": "viettelmoney",
    },
}

DEFAULT_SOURCES = ["google_play", "app_store", "youtube", "reddit", "tinhte", "voz"]


def _resolve_app(name: str) -> dict:
    """Find app config by name (case-insensitive, partial match ok)."""
    key = name.lower().replace(" ", "").replace("-", "")
    if key in APP_REGISTRY:
        return APP_REGISTRY[key]
    for reg_key, cfg in APP_REGISTRY.items():
        if key in reg_key or reg_key in key:
            return cfg
    # Unknown app — fall back to the name as given (no crawl IDs needed anymore).
    return {"display_name": name}


def _review_title(review: dict):
    metadata = review.get("metadata") or {}
    return (
        metadata.get("video_title")
        or metadata.get("thread_title")
        or metadata.get("review_title")
    )


def _build_references(reviews: list) -> list:
    """Build compact source references the agent can cite in its final answer."""
    references = []
    seen = set()
    for review in reviews:
        url = review.get("url") or (review.get("metadata") or {}).get("video_url")
        if not url:
            continue
        key = (review.get("source"), url, review.get("id"))
        if key in seen:
            continue
        seen.add(key)
        references.append({
            "source": review.get("source"),
            "app": review.get("app"),
            "title": _review_title(review),
            "url": url,
            "date": review.get("date"),
            "review_id": review.get("id"),
        })
    return references


def process_reviews(
    raw_reviews: list,
    apps: list,
    goal: str,
    days_back: int = 180,
    focus_area: str = None,
    rating_min: int = 1,
    rating_max: int = 5,
    min_length: int = 30,
    allowed_langs: list = None,
) -> dict:
    """
    Process agent-collected raw review records into analysis-ready data.

    raw_reviews : list of dicts the agent built from web_fetch output.
                  Each needs at least source/app/content; the rest is defaulted.
    Returns the same shape the analysis phase expects:
        { apps, goal, focus_area, reviews, reviews_by_app, references, stats, params }
    """
    if allowed_langs is None:
        allowed_langs = ["vi", "en"]

    display_names = [_resolve_app(a)["display_name"] for a in apps]
    default_app = display_names[0] if len(display_names) == 1 else None

    normalized = [make_review(r, default_app=default_app) for r in raw_reviews]

    deduped = deduplicate(normalized)
    qualified_all = qualify(
        deduped,
        days_back=days_back,
        min_chars=min_length,
        allowed_langs=allowed_langs,
        rating_min=rating_min,
        rating_max=rating_max,
    )
    qualified_all = mark_near_duplicates(qualified_all)
    qualified = [r for r in qualified_all if r.get("qualified")]

    if focus_area:
        kw = focus_area.lower()

        def _sort_key(r):
            return 0 if kw in (r.get("content") or "").lower() else 1

        qualified = sorted(qualified, key=_sort_key)

    # Per-app stats use ALL normalized records for totals, qualified for the rest.
    raw_by_app = {}
    for r in normalized:
        raw_by_app.setdefault(r.get("app"), []).append(r)

    reviews_by_app = {}
    for r in qualified:
        reviews_by_app.setdefault(r.get("app"), []).append(r)

    stats = {}
    for app_name in set(list(raw_by_app) + list(reviews_by_app) + display_names):
        q = reviews_by_app.get(app_name, [])
        by_source = {}
        for r in q:
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        stats[app_name] = {
            "total": len(raw_by_app.get(app_name, [])),
            "qualified": len(q),
            "by_source": by_source,
        }

    logger.info("Processed %d raw → %d qualified", len(normalized), len(qualified))

    return {
        "apps": display_names,
        "goal": goal,
        "focus_area": focus_area,
        "reviews": qualified,
        "reviews_by_app": reviews_by_app,
        "references": _build_references(qualified),
        "stats": stats,
        "params": {
            "days_back": days_back,
            "rating_min": rating_min,
            "rating_max": rating_max,
            "min_length": min_length,
            "allowed_langs": allowed_langs,
        },
    }
