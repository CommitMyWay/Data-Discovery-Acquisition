"""
main.py — CLI entry point for the Vietnamese fintech review aggregator.

Usage example:
    python scripts/main.py \
        --app "ZaloPay" \
        --app-id-android "com.vinagame.zalopay" \
        --app-id-ios "1107454800" \
        --youtube-query "ZaloPay review đánh giá ví điện tử" \
        --reddit-query "ZaloPay Vietnam payment wallet" \
        --tinhte-query "zalopay" \
        --voz-query "zalopay" \
        --sources google_play,app_store,youtube,reddit,tinhte,voz \
        --fallback-dataset data/fallback.json \
        --output output/reviews_zalopay.json \
        --output-csv output/reviews_zalopay.csv
"""

import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime

# Allow running from project root: python scripts/main.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.crawl import BaseCrawler
from scripts.sources import (
    GooglePlayCrawler, AppStoreCrawler, YouTubeCrawler,
    RedditCrawler, TinhteCrawler, VozCrawler
)
from scripts.pipeline import deduplicate, qualify, mark_near_duplicates, print_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def parse_args():
    p = argparse.ArgumentParser(
        description="Aggregate Vietnamese fintech app reviews from 6 sources"
    )

    # App identity
    p.add_argument("--app", required=True, help="App name (e.g. ZaloPay)")
    p.add_argument("--app-id-android", default=None, help="Google Play app ID")
    p.add_argument("--app-id-ios", default=None, help="Apple App Store app ID")
    p.add_argument("--youtube-query", default=None, help="YouTube search query")
    p.add_argument("--reddit-query", default=None, help="Reddit search query")
    p.add_argument("--tinhte-query", default=None, help="Tinhte search query")
    p.add_argument("--voz-query", default=None, help="Voz search query")

    # Source selection
    p.add_argument(
        "--sources",
        default="google_play,app_store,youtube,reddit,tinhte,voz",
        help="Comma-separated list of sources to crawl"
    )

    # Crawler options
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--fallback-dataset", default=None, help="Path to fallback JSON dataset")

    # Qualification options
    p.add_argument("--days-back", type=int, default=365, help="Recency window in days")
    p.add_argument("--min-length", type=int, default=30, help="Minimum review character count")
    p.add_argument("--rating-min", type=int, default=1)
    p.add_argument("--rating-max", type=int, default=5)
    p.add_argument("--lang", default="vi,en", help="Allowed language codes (comma-separated)")
    p.add_argument("--no-qualify", action="store_true", help="Skip qualification step")
    p.add_argument("--only-qualified", action="store_true", help="Output only qualified reviews")

    # Output
    p.add_argument("--output", default="output/reviews.json", help="Output JSON file path")
    p.add_argument("--output-csv", default=None, help="Optional CSV output path")

    return p.parse_args()


def build_crawlers(args) -> list[BaseCrawler]:
    sources = [s.strip() for s in args.sources.split(",")]
    crawlers = []
    common = dict(
        app_name=args.app,
        max_retries=args.max_retries,
        fallback_dataset_path=args.fallback_dataset,
    )

    if "google_play" in sources:
        if not args.app_id_android:
            logger.warning("--app-id-android not provided; skipping Google Play")
        else:
            crawlers.append(GooglePlayCrawler(app_id=args.app_id_android, **common))

    if "app_store" in sources:
        if not args.app_id_ios:
            logger.warning("--app-id-ios not provided; skipping App Store")
        else:
            crawlers.append(AppStoreCrawler(app_id=args.app_id_ios, **common))

    if "youtube" in sources:
        query = args.youtube_query or f"{args.app} review đánh giá"
        crawlers.append(YouTubeCrawler(search_query=query, **common))

    if "reddit" in sources:
        query = args.reddit_query or f"{args.app} Vietnam fintech"
        crawlers.append(RedditCrawler(search_query=query, **common))

    if "tinhte" in sources:
        query = args.tinhte_query or args.app.lower()
        crawlers.append(TinhteCrawler(search_query=query, **common))

    if "voz" in sources:
        query = args.voz_query or args.app.lower()
        crawlers.append(VozCrawler(search_query=query, **common))

    return crawlers


def write_output(reviews: list, output_path: str, csv_path: str = None):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)
    logger.info("Wrote %d reviews to %s", len(reviews), output_path)

    if csv_path:
        os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
        flat_cols = ["id", "source", "app", "author", "rating", "content",
                     "date", "url", "language", "qualified", "disqualification_reasons"]
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=flat_cols, extrasaction="ignore")
            writer.writeheader()
            for r in reviews:
                row = {**r}
                row["disqualification_reasons"] = ", ".join(r.get("disqualification_reasons") or [])
                writer.writerow(row)
        logger.info("Wrote CSV to %s", csv_path)


def main():
    args = parse_args()
    allowed_langs = [l.strip() for l in args.lang.split(",")]

    logger.info("=== Starting review aggregation for: %s ===", args.app)
    logger.info("Sources: %s", args.sources)

    # 1. Crawl all sources
    crawlers = build_crawlers(args)
    all_reviews = []
    for crawler in crawlers:
        logger.info("Crawling %s...", crawler.source_name)
        reviews = crawler.run()
        logger.info("  → %d records from %s", len(reviews), crawler.source_name)
        all_reviews.extend(reviews)

    logger.info("Total raw records: %d", len(all_reviews))

    # 2. Deduplication
    all_reviews = deduplicate(all_reviews)

    # 3. Qualification
    if not args.no_qualify:
        all_reviews = qualify(
            all_reviews,
            days_back=args.days_back,
            min_chars=args.min_length,
            allowed_langs=allowed_langs,
            rating_min=args.rating_min,
            rating_max=args.rating_max,
        )
        all_reviews = mark_near_duplicates(all_reviews)
    else:
        logger.info("Qualification skipped (--no-qualify).")

    # 4. Filter if requested
    if args.only_qualified:
        before = len(all_reviews)
        all_reviews = [r for r in all_reviews if r.get("qualified")]
        logger.info("Filtered to %d qualified reviews (removed %d)", len(all_reviews), before - len(all_reviews))

    # 5. Sort by date DESC
    def sort_key(r):
        return r.get("date") or ""
    all_reviews.sort(key=sort_key, reverse=True)

    # 6. Write output
    write_output(all_reviews, args.output, args.output_csv)

    # 7. Summary
    print_summary(all_reviews, args.app)

    logger.info("Done.")


if __name__ == "__main__":
    main()
