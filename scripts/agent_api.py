"""
agent_api.py — Public data interface for the OpenClaw agent.

The agent (Claude with this skill loaded) calls run_research() to get
qualified reviews, then uses its own reasoning to analyse them.
No second API call needed — the agent IS the model.

Usage by the OpenClaw agent:
    from scripts.agent_api import run_research

    data = await run_research(
        apps=["MoMo", "ZaloPay"],
        goal="product",
        days_back=180,
        focus_area="Login",
    )
    # data["reviews"]        → qualified reviews, ready to reason over
    # data["reviews_by_app"] → split by app
    # data["stats"]          → counts per source per app

The agent then reads data["reviews"] and produces the analysis,
proposals, and report directly — no additional API call required.
"""

import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.sources import (
    GooglePlayCrawler, AppStoreCrawler, YouTubeCrawler,
    RedditCrawler, TinhteCrawler, VozCrawler,
)
from scripts.pipeline import deduplicate, qualify, mark_near_duplicates
from scripts import crawl_client

logger = logging.getLogger(__name__)

DEFAULT_CRAWL_SERVICE_URL = (
    "https://endpoint-503c0bb0-c12f-4b54-919d-edc2c10b633e."
    "agentbase-runtime.aiplatform.vngcloud.vn"
)

# ---------------------------------------------------------------------------
# App registry — pre-resolved IDs (mirrors references/fintech-apps.md)
# ---------------------------------------------------------------------------

APP_REGISTRY = {
    "momo": {
        "display_name": "MoMo",
        "android_id":   "com.mservice.momotransfer",
        "ios_id":       "918751511",
        "youtube_query": "MoMo ví điện tử review đánh giá",
        "reddit_query":  "MoMo Vietnam e-wallet payment",
        "tinhte_query":  "momo ví điện tử",
        "voz_query":     "momo ví điện tử",
    },
    "zalopay": {
        "display_name": "ZaloPay",
        "android_id":   "com.vinagame.zalopay",
        "ios_id":       "1107454800",
        "youtube_query": "ZaloPay review đánh giá ví điện tử",
        "reddit_query":  "ZaloPay Vietnam payment wallet",
        "tinhte_query":  "zalopay",
        "voz_query":     "zalopay",
    },
    "shopeepay": {
        "display_name": "ShopeePay",
        "android_id":   "com.shopee.vn",
        "ios_id":       "959841854",
        "youtube_query": "ShopeePay review đánh giá thanh toán",
        "reddit_query":  "ShopeePay Vietnam Shopee payment",
        "tinhte_query":  "shopeepay",
        "voz_query":     "shopeepay shopee pay",
    },
    "vnpay": {
        "display_name": "VNPay",
        "android_id":   "com.vnpay.vnpayqr",
        "ios_id":       "1436080875",
        "youtube_query": "VNPay review đánh giá QR thanh toán",
        "reddit_query":  "VNPay Vietnam QR payment",
        "tinhte_query":  "vnpay",
        "voz_query":     "vnpay",
    },
    "viettelmoney": {
        "display_name": "ViettelMoney",
        "android_id":   "com.viettel.viettelmoney",
        "ios_id":       "1493028346",
        "youtube_query": "ViettelMoney review đánh giá",
        "reddit_query":  "ViettelMoney Vietnam Viettel Pay",
        "tinhte_query":  "viettelmoney",
        "voz_query":     "viettelmoney",
    },
}

DEFAULT_SOURCES = ["google_play", "app_store", "youtube", "reddit", "tinhte", "voz"]


def _resolve_app(name: str) -> dict:
    """Find app config by name (case-insensitive, partial match ok)."""
    key = name.lower().replace(" ", "").replace("-", "")
    # Exact match
    if key in APP_REGISTRY:
        return APP_REGISTRY[key]
    # Partial match
    for reg_key, cfg in APP_REGISTRY.items():
        if key in reg_key or reg_key in key:
            return cfg
    raise ValueError(
        f"App '{name}' not found in registry. "
        f"Known apps: {list(APP_REGISTRY.keys())}. "
        f"Pass android_id / ios_id manually if this is a new app."
    )


def _build_crawlers(app_cfg: dict, sources: list, common_kwargs: dict) -> list:
    crawlers = []
    name = app_cfg["display_name"]

    if "google_play" in sources and app_cfg.get("android_id"):
        crawlers.append(GooglePlayCrawler(
            app_id=app_cfg["android_id"], app_name=name, **common_kwargs))

    if "app_store" in sources and app_cfg.get("ios_id"):
        crawlers.append(AppStoreCrawler(
            app_id=app_cfg["ios_id"], app_name=name, **common_kwargs))

    if "youtube" in sources:
        crawlers.append(YouTubeCrawler(
            search_query=app_cfg["youtube_query"], app_name=name, **common_kwargs))

    if "reddit" in sources:
        crawlers.append(RedditCrawler(
            search_query=app_cfg["reddit_query"], app_name=name, **common_kwargs))

    if "tinhte" in sources:
        crawlers.append(TinhteCrawler(
            search_query=app_cfg["tinhte_query"], app_name=name, **common_kwargs))

    if "voz" in sources:
        crawlers.append(VozCrawler(
            search_query=app_cfg["voz_query"], app_name=name, **common_kwargs))

    return crawlers


async def _crawl_app(app_cfg: dict, sources: list, common_kwargs: dict) -> list:
    """Crawl all sources for one app and return qualified reviews."""
    crawlers = _build_crawlers(app_cfg, sources, common_kwargs)
    all_reviews = []

    for crawler in crawlers:
        logger.info("  [%s] crawling %s ...", app_cfg["display_name"], crawler.source_name)
        reviews = crawler.run()
        logger.info("  [%s] %s → %d raw", app_cfg["display_name"], crawler.source_name, len(reviews))
        all_reviews.extend(reviews)

    return all_reviews


def _review_title(review: dict):
    metadata = review.get("metadata") or {}
    return (
        metadata.get("video_title")
        or metadata.get("thread_title")
        or metadata.get("review_title")
    )


def _build_references(reviews: list[dict]) -> list[dict]:
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_research(
    apps: list[str],
    goal: str,
    days_back: int = 180,
    sources: list[str] = None,
    focus_area: str = None,
    fallback_dataset_path: str = None,
    max_retries: int = 3,
    rating_min: int = 1,
    rating_max: int = 5,
    min_length: int = 30,
    allowed_langs: list[str] = None,
    market: str = "VN",
    crawl_service_url: str = None,
    crawl_service_token: str = None,
    crawl_service_timeout: int = 900,
    crawl_filters: dict = None,
) -> dict:
    """
    Crawl reviews for one or more apps and return qualified results.

    Parameters
    ----------
    apps          : list of app names, e.g. ["MoMo", "ZaloPay"]
    goal          : "product" | "marketing" | "qa"
    days_back     : recency window (default 180)
    sources       : which platforms to crawl (default: all 6)
    focus_area    : optional topic to surface, e.g. "Login", "Thanh toán"
    fallback_dataset_path : path to backup JSON if live crawl fails

    Returns
    -------
    {
        "apps": [...],
        "goal": "product",
        "focus_area": "Login" | None,
        "reviews": [...],          # all qualified reviews across all apps
        "reviews_by_app": {        # split by app name
            "MoMo":    [...],
            "ZaloPay": [...],
        },
        "references": [            # compact source links for citations
            {"source": "youtube", "title": "...", "url": "...", ...},
        ],
        "stats": {
            "MoMo":    {"total": 300, "qualified": 210, "by_source": {...}},
            "ZaloPay": {"total": 280, "qualified": 195, "by_source": {...}},
        },
        "params": { ... }          # echo back the params used
    }
    """
    if sources is None:
        sources = DEFAULT_SOURCES
    if allowed_langs is None:
        allowed_langs = ["vi", "en"]

    if crawl_service_url == "":
        service_url = None
    else:
        service_url = crawl_service_url or os.getenv("REVIEW_CRAWLER_SERVICE_URL") or DEFAULT_CRAWL_SERVICE_URL
    if service_url:
        return await _run_delegated_research(
            apps=apps,
            goal=goal,
            days_back=days_back,
            sources=sources,
            focus_area=focus_area,
            rating_min=rating_min,
            rating_max=rating_max,
            min_length=min_length,
            allowed_langs=allowed_langs,
            market=market,
            crawl_service_url=service_url,
            crawl_service_token=crawl_service_token or os.getenv("REVIEW_CRAWLER_SERVICE_TOKEN"),
            crawl_service_timeout=crawl_service_timeout,
            crawl_filters=crawl_filters,
        )

    common_kwargs = dict(
        max_retries=max_retries,
        fallback_dataset_path=fallback_dataset_path,
    )

    all_reviews = []
    stats = {}

    for app_name in apps:
        app_cfg = _resolve_app(app_name)
        logger.info("=== Crawling %s ===", app_cfg["display_name"])

        raw = await _crawl_app(app_cfg, sources, common_kwargs)

        # Pipeline
        deduped = deduplicate(raw)
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

        # Stats
        by_source = {}
        for r in qualified:
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1

        stats[app_cfg["display_name"]] = {
            "total":     len(raw),
            "qualified": len(qualified),
            "by_source": by_source,
        }

        all_reviews.extend(qualified)

    # Split by app
    reviews_by_app = {}
    for r in all_reviews:
        reviews_by_app.setdefault(r["app"], []).append(r)

    # If focus_area set, bubble up matching reviews first
    if focus_area:
        kw = focus_area.lower()
        def _sort_key(r):
            return 0 if kw in (r.get("content") or "").lower() else 1
        all_reviews = sorted(all_reviews, key=_sort_key)
        for app_name in reviews_by_app:
            reviews_by_app[app_name] = sorted(reviews_by_app[app_name], key=_sort_key)

    logger.info("=== Done. Total qualified: %d ===", len(all_reviews))

    return {
        "apps":           [_resolve_app(a)["display_name"] for a in apps],
        "goal":           goal,
        "focus_area":     focus_area,
        "reviews":        all_reviews,
        "reviews_by_app": reviews_by_app,
        "references":     _build_references(all_reviews),
        "stats":          stats,
        "params": {
            "days_back":     days_back,
            "sources":       sources,
            "rating_min":    rating_min,
            "rating_max":    rating_max,
            "min_length":    min_length,
            "allowed_langs": allowed_langs,
        },
    }


async def _run_delegated_research(
    *,
    apps: list[str],
    goal: str,
    days_back: int,
    sources: list[str],
    focus_area: str,
    rating_min: int,
    rating_max: int,
    min_length: int,
    allowed_langs: list[str],
    market: str,
    crawl_service_url: str,
    crawl_service_token: str,
    crawl_service_timeout: int,
    crawl_filters: dict,
) -> dict:
    all_reviews = []
    reviews_by_app = {}
    stats = {}
    service_results = {}

    for app_name in apps:
        app_cfg = _resolve_app(app_name)
        display_name = app_cfg["display_name"]
        filters = {
            "min_length": min_length,
            "allowed_langs": allowed_langs,
        }
        if crawl_filters:
            filters.update(crawl_filters)
        discovered_targets_path = os.getenv("REVIEW_CRAWLER_DISCOVERED_TARGETS_PATH")
        if discovered_targets_path and "discovered_targets_path" not in filters:
            filters["discovered_targets_path"] = discovered_targets_path

        service_payload = await crawl_client.crawl_reviews(
            base_url=crawl_service_url,
            subject=display_name,
            market=market,
            goal=goal,
            focus=focus_area,
            sources=sources or DEFAULT_SOURCES,
            rating_min=rating_min,
            rating_max=rating_max,
            days_back=days_back,
            filters=filters,
            token=crawl_service_token,
            max_wait_seconds=crawl_service_timeout,
        )

        service_reviews = []
        for review in service_payload.get("reviews", []):
            if not review.get("qualified"):
                continue
            normalized = dict(review)
            normalized["app"] = normalized.get("app") or display_name
            normalized["subject"] = normalized.get("subject") or display_name
            service_reviews.append(normalized)

        reviews_by_app[display_name] = service_reviews
        all_reviews.extend(service_reviews)
        stats[display_name] = _service_stats(service_payload, service_reviews)
        service_results[display_name] = {
            "stats": service_payload.get("stats", {}),
            "outcomes": service_payload.get("outcomes", []),
            "references": service_payload.get("references", []),
        }

    if focus_area:
        kw = focus_area.lower()

        def _sort_key(review):
            return 0 if kw in (review.get("content") or "").lower() else 1

        all_reviews = sorted(all_reviews, key=_sort_key)
        for app_name in reviews_by_app:
            reviews_by_app[app_name] = sorted(reviews_by_app[app_name], key=_sort_key)

    return {
        "apps": [_resolve_app(a)["display_name"] for a in apps],
        "goal": goal,
        "focus_area": focus_area,
        "reviews": all_reviews,
        "reviews_by_app": reviews_by_app,
        "reviews_by_source": _reviews_by_source(all_reviews),
        "references": _build_references(all_reviews),
        "stats": stats,
        "service_results": service_results,
        "params": {
            "days_back": days_back,
            "sources": sources,
            "rating_min": rating_min,
            "rating_max": rating_max,
            "min_length": min_length,
            "allowed_langs": allowed_langs,
            "market": market,
            "crawl_service_url": crawl_service_url,
            "crawl_filters": crawl_filters or {},
        },
    }


def _service_stats(service_payload: dict, reviews: list) -> dict:
    raw_total = 0
    by_source = {}
    for source, source_stats in (service_payload.get("stats") or {}).items():
        if isinstance(source_stats, dict):
            raw_total += int(source_stats.get("raw") or 0)
    for review in reviews:
        source = review.get("source")
        by_source[source] = by_source.get(source, 0) + 1
    return {
        "total": raw_total or len(service_payload.get("reviews", [])),
        "qualified": len(reviews),
        "by_source": by_source,
    }


def _reviews_by_source(reviews: list) -> dict:
    grouped = {}
    for review in reviews:
        grouped.setdefault(review.get("source"), []).append(review)
    return grouped
