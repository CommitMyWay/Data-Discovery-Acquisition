"""
crawl.py — Base crawler with exponential-backoff retry and fallback dataset support.
All source-specific crawlers inherit from BaseCrawler.
"""

import json
import logging
import os
import time
from typing import Callable, Any

logger = logging.getLogger(__name__)


class CrawlError(Exception):
    """Raised when all retries for a source are exhausted."""
    pass


class BaseCrawler:
    """
    Base class for all source crawlers.

    Provides:
      - fetch_with_retry(): wraps any callable with exponential-backoff retry
      - load_fallback(): loads records from the fallback dataset for this source
    """

    def __init__(
        self,
        app_name: str,
        source_name: str,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        fallback_dataset_path: str = None,
    ):
        self.app_name = app_name
        self.source_name = source_name
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.fallback_dataset_path = fallback_dataset_path

    def fetch_with_retry(self, func: Callable, *args, **kwargs) -> Any:
        """
        Call func(*args, **kwargs) up to max_retries times.
        On failure, waits backoff_base^attempt seconds before retrying.
        Raises CrawlError if all attempts fail.
        """
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                wait = self.backoff_base ** attempt
                logger.warning(
                    "[%s] Attempt %d/%d failed: %s — retrying in %.1fs",
                    self.source_name, attempt, self.max_retries, exc, wait
                )
                time.sleep(wait)

        raise CrawlError(
            f"[{self.source_name}] All {self.max_retries} retries exhausted. "
            f"Last error: {last_exc}"
        )

    def load_fallback(self) -> list:
        """
        Load fallback records for this source + app from the fallback dataset.
        Returns an empty list if no fallback is available or path is not set.
        """
        if not self.fallback_dataset_path:
            logger.info("[%s] No fallback dataset configured.", self.source_name)
            return []

        if not os.path.exists(self.fallback_dataset_path):
            logger.warning("[%s] Fallback path not found: %s", self.source_name, self.fallback_dataset_path)
            return []

        try:
            with open(self.fallback_dataset_path, "r", encoding="utf-8") as f:
                all_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error("[%s] Failed to load fallback dataset: %s", self.source_name, e)
            return []

        matched = [
            r for r in all_data
            if r.get("source") == self.source_name
            and r.get("app", "").lower() == self.app_name.lower()
        ]

        logger.info("[%s] Fallback loaded %d records.", self.source_name, len(matched))

        # Mark fallback records so they can be tracked in output
        for r in matched:
            r.setdefault("metadata", {})
            r["metadata"]["from_fallback"] = True

        return matched

    def crawl(self) -> list:
        """
        Override in subclasses. Should return a list of review dicts
        matching the schema in references/data-pipeline.md.
        Falls back to self.load_fallback() on CrawlError.
        """
        raise NotImplementedError

    def run(self) -> list:
        """
        Execute crawl with automatic fallback on failure.
        This is the public entry point — call this, not crawl() directly.
        """
        try:
            results = self.crawl()
            logger.info("[%s] Crawled %d reviews live.", self.source_name, len(results))
            return results
        except CrawlError as e:
            logger.error("%s — falling back to dataset.", e)
            return self.load_fallback()
        except Exception as e:
            logger.error("[%s] Unexpected error: %s — falling back.", self.source_name, e)
            return self.load_fallback()
