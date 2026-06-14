# Web-Tools Rewrite of `user-review-aggregator` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the review aggregator's two Python crawl paths (remote `review-crawler-service` delegation + local urllib/yt-dlp crawlers) with the agent's built-in `web_search`/`web_fetch` tools, leaving Python responsible only for deterministic processing.

**Architecture:** The agent drives discovery (`web_search`) and fetching (`web_fetch`) during its turn, extracts raw review records, then calls a new synchronous `process_reviews()` that reuses the existing `pipeline.py` (dedup/qualify/near-dup/stats) and returns the same `data` contract the analysis phase already consumes.

**Tech Stack:** Python 3 stdlib, `langdetect` (qualification only), `unittest`. No HTTP libraries, no browser engines.

**Spec:** `docs/superpowers/specs/2026-06-15-web-tools-rewrite-design.md`

**Working directory for all commands:** `/Users/la60716/PTO_Projects/hackathon_v2/user-review-aggregator`
**Python interpreter:** `/usr/bin/python3`
**Branch:** `rewrite-web-tools` (already created)

---

### Task 1: Add `scripts/processing.py` (schema normalization helpers)

Lift the source-agnostic normalization helpers out of the soon-to-be-deleted `scripts/sources/__init__.py` into a standalone module. These turn an agent-supplied raw record (a plain dict) into the canonical review schema.

**Files:**
- Create: `scripts/processing.py`
- Test: `tests/test_processing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_processing.py`:

```python
import importlib
import unittest


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.processing = importlib.import_module("scripts.processing")

    def test_make_review_fills_schema_and_generates_id(self):
        raw = {
            "source": "voz", "app": "MoMo", "author": "user1",
            "content": "  Chuyển tiền nhanh, không lỗi.  ",
            "date": "2026-05-01", "url": "https://voz.vn/t/abc",
        }
        r = self.processing.make_review(raw)
        self.assertEqual(r["source"], "voz")
        self.assertEqual(r["app"], "MoMo")
        self.assertEqual(r["content"], "Chuyển tiền nhanh, không lỗi.")
        self.assertTrue(r["id"].startswith("sha256:"))
        self.assertIsNone(r["rating"])
        self.assertIsNone(r["qualified"])
        self.assertEqual(r["disqualification_reasons"], [])
        self.assertIn("thread_title", r["metadata"])

    def test_make_review_preserves_existing_id_and_casts_rating(self):
        raw = {
            "id": "sha256:keep", "source": "app_store", "app": "MoMo",
            "rating": "4", "content": "thanh toán tốt",
            "metadata": {"review_title": "ok"},
        }
        r = self.processing.make_review(raw)
        self.assertEqual(r["id"], "sha256:keep")
        self.assertEqual(r["rating"], 4)
        self.assertEqual(r["metadata"]["review_title"], "ok")

    def test_make_review_handles_bad_rating_and_default_app(self):
        raw = {"source": "reddit", "rating": "n/a", "content": "x"}
        r = self.processing.make_review(raw, default_app="ZaloPay")
        self.assertIsNone(r["rating"])
        self.assertEqual(r["app"], "ZaloPay")

    def test_to_iso_handles_epoch_and_none(self):
        self.assertIsNone(self.processing.to_iso(None))
        self.assertTrue(self.processing.to_iso(0).startswith("1970-01-01"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/usr/bin/python3 -m unittest tests.test_processing -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.processing'`

- [ ] **Step 3: Write the implementation**

Create `scripts/processing.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/usr/bin/python3 -m unittest tests.test_processing -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/processing.py tests/test_processing.py
git commit -m "feat: add processing.py review-record normalization helpers"
```

---

### Task 2: Rewrite `scripts/agent_api.py` → `process_reviews()`

Strip both crawl paths. Keep `APP_REGISTRY`, `_resolve_app`, `_review_title`, `_build_references`. Add a synchronous `process_reviews()` that normalizes raw records, runs the pipeline, and returns the existing `data` shape. Replace the test file in the same task (tests and impl change together).

**Files:**
- Modify (full rewrite): `scripts/agent_api.py`
- Modify (full rewrite): `tests/test_agent_api.py`

- [ ] **Step 1: Replace the test file with the new behavior tests**

Overwrite `tests/test_agent_api.py`:

```python
import builtins
import importlib
import sys
import unittest
from unittest import mock


# These crawl-only deps must NOT be required to import the module anymore.
OPTIONAL_IMPORTS = {"google_play_scraper", "yt_dlp", "requests", "bs4", "crawl4ai"}


class AgentApiTests(unittest.TestCase):
    def tearDown(self):
        for name in ["scripts.agent_api", "scripts.processing", "scripts.pipeline"]:
            sys.modules.pop(name, None)

    def test_import_does_not_require_crawl_dependencies(self):
        real_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            root = name.split(".", 1)[0]
            if root in OPTIONAL_IMPORTS:
                raise ModuleNotFoundError(f"No module named '{root}'")
            return real_import(name, globals, locals, fromlist, level)

        for name in list(sys.modules):
            if name.split(".", 1)[0] in OPTIONAL_IMPORTS or name.startswith("scripts."):
                sys.modules.pop(name, None)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            module = importlib.import_module("scripts.agent_api")

        self.assertTrue(hasattr(module, "process_reviews"))

    def test_process_reviews_qualifies_and_builds_references(self):
        agent_api = importlib.import_module("scripts.agent_api")
        raw = {
            "id": "sha256:test",
            "source": "youtube",
            "app": "ZaloPay",
            "author": "user1",
            "rating": None,
            "content": "Đăng nhập OTP thường xuyên lỗi khi thanh toán tại quầy sau khi cập nhật ứng dụng.",
            "date": "2026-06-01T00:00:00+00:00",
            "url": "https://www.youtube.com/watch?v=abc",
            "metadata": {
                "video_title": "ZaloPay review",
                "video_url": "https://www.youtube.com/watch?v=abc",
                "is_transcript": False,
            },
        }

        result = agent_api.process_reviews(
            [raw], apps=["ZaloPay"], goal="product", days_back=30, focus_area=None
        )

        self.assertEqual(len(result["reviews"]), 1)
        self.assertEqual(result["reviews_by_app"]["ZaloPay"][0]["id"], "sha256:test")
        self.assertEqual(result["stats"]["ZaloPay"]["qualified"], 1)
        self.assertEqual(
            result["references"],
            [
                {
                    "source": "youtube",
                    "app": "ZaloPay",
                    "title": "ZaloPay review",
                    "url": "https://www.youtube.com/watch?v=abc",
                    "date": "2026-06-01T00:00:00+00:00",
                    "review_id": "sha256:test",
                }
            ],
        )

    def test_process_reviews_drops_unqualified(self):
        agent_api = importlib.import_module("scripts.agent_api")
        too_short = {"source": "reddit", "app": "MoMo", "author": "u",
                     "content": "ok", "date": "2026-06-01", "url": "https://r/x"}

        result = agent_api.process_reviews(
            [too_short], apps=["MoMo"], goal="qa", days_back=30
        )

        self.assertEqual(result["reviews"], [])
        self.assertEqual(result["stats"]["MoMo"]["total"], 1)
        self.assertEqual(result["stats"]["MoMo"]["qualified"], 0)

    def test_process_reviews_handles_empty_input(self):
        agent_api = importlib.import_module("scripts.agent_api")
        result = agent_api.process_reviews([], apps=["MoMo"], goal="product")
        self.assertEqual(result["reviews"], [])
        self.assertEqual(result["apps"], ["MoMo"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/usr/bin/python3 -m unittest tests.test_agent_api -v`
Expected: FAIL — current `agent_api.py` imports `scripts.sources`/`scripts.crawl_client` and has no `process_reviews` (errors on import or `AttributeError`).

- [ ] **Step 3: Rewrite `scripts/agent_api.py`**

Overwrite `scripts/agent_api.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/usr/bin/python3 -m unittest tests.test_agent_api -v`
Expected: PASS (4 tests). The `references` assertion confirms the analysis contract is preserved.

- [ ] **Step 5: Commit**

```bash
git add scripts/agent_api.py tests/test_agent_api.py
git commit -m "refactor: replace crawl paths with process_reviews() entry point"
```

---

### Task 3: Delete dead crawl modules and trim dependencies

Now that `agent_api.py` no longer imports them, remove the crawl machinery and its test, and trim `requirements.txt`.

**Files:**
- Delete: `scripts/crawl.py`, `scripts/crawl_client.py`, `scripts/sources/__init__.py`, `scripts/sources/` (dir), `scripts/main.py`, `tests/test_crawl_client.py`
- Modify: `scripts/requirements.txt`

- [ ] **Step 1: Delete the dead modules and test**

```bash
git rm scripts/crawl.py scripts/crawl_client.py scripts/sources/__init__.py scripts/main.py tests/test_crawl_client.py
rmdir scripts/sources 2>/dev/null || true
```

- [ ] **Step 2: Replace `scripts/requirements.txt`**

Overwrite `scripts/requirements.txt`:

```text
# Collection is done by the agent's built-in web_search / web_fetch tools — no
# HTTP libraries or browser engines are needed here. The only third-party dep is
# langdetect, used by pipeline.py for vi/en language qualification. It is imported
# lazily and falls back to "vi" if unavailable, so installs are optional.
#
#   pip install -r requirements.txt

langdetect>=1.0.9   # vi/en language qualification (pipeline.py)
```

- [ ] **Step 3: Verify nothing still references the deleted modules**

Run: `grep -rn -e "crawl_client" -e "scripts.sources" -e "BaseCrawler" -e "from scripts.crawl " -e "run_research" -e "REVIEW_CRAWLER" scripts/ tests/`
Expected: no output (exit code 1 / empty). If any line prints, fix that reference before continuing.

- [ ] **Step 4: Run the full test suite**

Run: `/usr/bin/python3 -m unittest discover -s tests -v`
Expected: PASS — only `tests.test_processing` and `tests.test_agent_api` remain (8 tests total).

- [ ] **Step 5: Commit**

```bash
git add -A scripts/ tests/
git commit -m "chore: remove crawl modules + trim deps to langdetect"
```

---

### Task 4: Rewrite `SKILL.md` to the four-phase web-tools workflow

**Files:**
- Modify (full rewrite): `SKILL.md`

- [ ] **Step 1: Rewrite `SKILL.md`**

Keep the existing YAML frontmatter (`name`, `description`, triggers) **verbatim** — activation conditions are unchanged. Replace the body (everything after the frontmatter `---`) with the structure below. Write it out fully; do not leave outline stubs.

Required body sections, in order:

1. **Title + intro paragraph.** State the three working phases plus analysis. Include this framing sentence prominently:
   > `web_search` and `web_fetch` are **built-in OpenClaw platform tools** the agent calls directly during its turn — they are not skills in `/app/skills/`, and not Python functions. All discovery and fetching happens through them.

2. **Phase 1 — Discover.**
   - App stores: build URLs directly from the IDs in `references/fintech-apps.md` (no search).
   - YouTube / Reddit / Tinhte / Voz: use `web_search` with the per-source query (see `APP_REGISTRY` in `scripts/agent_api.py` or `references/fintech-apps.md`) to collect candidate URLs.
   - For apps not in `fintech-apps.md`, `web_search` for the app's store listings and discussion threads.

3. **Phase 2 — Fetch.** Reproduce this exact table:

   | Source | `web_fetch` target | Notes |
   |--------|-------------------|-------|
   | App Store | `https://itunes.apple.com/rss/customerreviews/id={ios_id}/sortBy=mostRecent/json?country=vn` | Clean JSON; iterate `feed.entry[]` for rating/title/content/date |
   | Reddit | append `.json` to the thread URL | Clean JSON; read post + `replies` tree |
   | Tinhte / Voz | thread URLs from Phase 1 | Readable post text; if gated/blocked, skip and note it |
   | Google Play | `https://play.google.com/store/apps/details?id={android_id}&hl=vi&gl=VN` | JS-rendered — rely on `web_search` review snippets plus whatever the page yields |
   | YouTube | the video page from Phase 1 | Titles + visible comments only; transcripts and full comment trees are NOT reachable |

   State the honest expectation: JSON sources are reliable; JS-heavy sources are best-effort. **There is no fallback dataset** — a source that fails is skipped and noted in the final analysis.

4. **Phase 3 — Extract & process.** Instruct the agent to read each fetched payload, pull `author / rating / content / date / url` (+ optional `metadata`) per review into a `raw_reviews` list, then call:

   ```python
   from scripts.agent_api import process_reviews

   data = process_reviews(
       raw_reviews,                 # records the agent built from web_fetch output
       apps=["MoMo"],               # one or more apps
       goal="product",             # product | marketing | qa
       days_back=180,
       focus_area="Login",         # optional deep-dive topic
   )
   # data["reviews"]        → qualified reviews
   # data["reviews_by_app"] → split by app
   # data["references"]     → compact source links for citations
   # data["stats"]          → per-app counts by source
   ```

   Include the raw-record shape and the valid `source` values (`google_play`, `app_store`, `youtube`, `reddit`, `tinhte`, `voz`):

   ```python
   {"source": "voz", "app": "MoMo", "author": "user1", "rating": None,
    "content": "...", "date": "2026-05-01", "url": "https://voz.vn/t/...",
    "metadata": {"thread_title": "..."}}
   ```

5. **Deduplication / Data Qualification / Output Schema.** Keep these explanatory sections (carry over the dedup, qualification-table, and output-schema content from the current `SKILL.md` — they still describe what `process_reviews()` does). Point to `references/qualification.md` and `references/data-pipeline.md`.

6. **Platform Reference Files.** Keep the bullets pointing to the four reference files.

7. **After Collection — Agent Analysis.** Carry over the current analysis section **unchanged** (exec summary, top issues, feature gaps, competitor delta, PO/QA/Marketing proposals, references), but update the example call from `await run_research(...)` to `process_reviews(raw_reviews, apps=["MoMo"], goal="product", focus_area="Login")`.

**Remove entirely:** the "Delegated Crawl" section, the AgentBase endpoint URL, `crawl_service_url`/`REVIEW_CRAWLER_*` config table, the "Local service smoke test" block, and the "Retry & Fallback Behavior" section.

- [ ] **Step 2: Verify no stale references remain in SKILL.md**

Run: `grep -n -e "run_research" -e "review-crawler-service" -e "crawl_service" -e "REVIEW_CRAWLER" -e "fallback-dataset" -e "fallback_dataset" SKILL.md`
Expected: no output.

- [ ] **Step 3: Verify the Python example in SKILL.md actually runs**

Run:
```bash
/usr/bin/python3 - <<'PY'
from scripts.agent_api import process_reviews
data = process_reviews(
    [{"source": "voz", "app": "MoMo", "author": "u1",
      "content": "Chuyển tiền nhanh nhưng đăng nhập hay lỗi OTP khi thanh toán.",
      "date": "2026-05-01", "url": "https://voz.vn/t/abc"}],
    apps=["MoMo"], goal="product", days_back=365, focus_area="OTP",
)
print("apps:", data["apps"])
print("reviews:", len(data["reviews"]))
print("stats:", data["stats"])
PY
```
Expected: prints `apps: ['MoMo']`, `reviews: 1`, and a stats dict with `qualified: 1`.

- [ ] **Step 4: Commit**

```bash
git add SKILL.md
git commit -m "docs: rewrite SKILL.md for web_search/web_fetch workflow"
```

---

### Task 5: Rewrite `references/sources.md`

**Files:**
- Modify (full rewrite): `references/sources.md`

- [ ] **Step 1: Rewrite `references/sources.md`**

Replace the crawler-internals content with web-tool guidance. For **each** of the 6 sources write a subsection containing: (a) the Phase-1 `web_search` query (or "build URL directly from ID" for app stores), (b) the Phase-2 `web_fetch` target URL, (c) which fields to extract into the raw record, (d) a reachability note. Use these specifics:

- **Google Play** — `web_search`: `"{app} review site:play.google.com"` + general review search; `web_fetch`: `https://play.google.com/store/apps/details?id={android_id}&hl=vi&gl=VN`. Extract: author, score→rating, content, date. Note: JS-rendered, partial yield; supplement with search snippets. `rating` 1–5.
- **App Store** — build URL directly from `ios_id`; `web_fetch`: `https://itunes.apple.com/rss/customerreviews/id={ios_id}/sortBy=mostRecent/json?country=vn&limit=50&page={n}` (pages 1–10). Extract from `feed.entry[]`: `author.name.label`, `im:rating.label`→rating, `content.label`, `updated.label`→date, `title.label`→`metadata.review_title`. Note: clean JSON, ~500 most-recent cap.
- **YouTube** — `web_search`: the `youtube_query`; `web_fetch`: each video page. Extract visible comment author + text (rating=None), and the video title into `metadata.video_title`/`video_url`. Note: transcripts and full comment trees not reachable via `web_fetch`.
- **Reddit** — `web_search`: the `reddit_query` (mention r/VietNam, r/vietnam, r/fintech); `web_fetch`: `{thread_url}.json`. Extract post `title`+`selftext` and each comment `body` (rating=None), `subreddit`→`metadata.subreddit`, `score`→`metadata.post_score`. Note: clean JSON, no auth.
- **Tinhte** — `web_search`: `"{tinhte_query} site:tinhte.vn"`; `web_fetch`: each thread URL. Extract post author + body text (rating=None), thread title→`metadata.thread_title`. Note: best-effort, may be gated → skip.
- **Voz** — `web_search`: `"{voz_query} site:voz.vn"`; `web_fetch`: each thread URL. Extract post author + body text (rating=None), thread title→`metadata.thread_title`. Note: best-effort, anti-bot/login walls → skip on failure.

Add a short header note: *Collection uses the agent's built-in `web_search`/`web_fetch` tools — no crawler libraries, no browser engine, no fallback dataset.*

- [ ] **Step 2: Verify no stale library references remain**

Run: `grep -n -e "google-play-scraper" -e "yt-dlp" -e "yt_dlp" -e "urllib" -e "crawl4ai" -e "HTMLParser" -e "Playwright" references/sources.md`
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add references/sources.md
git commit -m "docs: rewrite sources.md for web_search/web_fetch per source"
```

---

### Task 6: Update `references/data-pipeline.md`

**Files:**
- Modify: `references/data-pipeline.md`

- [ ] **Step 1: Edit `references/data-pipeline.md`**

Make these targeted edits (keep the Review Schema, `id` generation, and Deduplication sections as-is — they remain accurate):

1. Replace the **"Fallback Dataset Format"** section and its "Loading fallback data" subsection with a **"Raw Record Input Contract"** section describing the dict the agent passes to `process_reviews()`:

   ```json
   {
     "source": "voz",
     "app": "MoMo",
     "author": "user1",
     "rating": null,
     "content": "...",
     "date": "2026-05-01",
     "url": "https://voz.vn/t/...",
     "metadata": { "thread_title": "..." }
   }
   ```
   Note: only `source`, `app`, and `content` are needed; `make_review()` defaults the rest. Valid `source` values: `google_play`, `app_store`, `youtube`, `reddit`, `tinhte`, `voz`.

2. Update the **"Pipeline Execution Order"** list so step 1 reads:
   `1. Agent collects reviews via built-in web_search/web_fetch tools (see SKILL.md)`
   and remove the final "Write to output JSON + CSV" / "Print summary report" steps (no CLI now); end at "Return analysis-ready data from process_reviews()".

3. Remove any remaining mention of `--fallback-dataset`, `from_fallback`, or `fallback_collected_at` as an input mechanism (the `metadata.from_fallback` schema field can stay documented as legacy/optional, but it is no longer produced).

- [ ] **Step 2: Verify**

Run: `grep -n -e "fallback-dataset" -e "fallback_collected_at" -e "output JSON + CSV" references/data-pipeline.md`
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add references/data-pipeline.md
git commit -m "docs: update data-pipeline.md input contract + execution order"
```

---

### Task 7: Update `evals/evals.json`

**Files:**
- Modify: `evals/evals.json`

- [ ] **Step 1: Edit eval #2's `expected_output`**

In `evals/evals.json`, eval #2 currently references a fallback dataset and CSV export. The prompt text stays, but update `expected_output` (id 2) to:

```text
Discover ZaloPay handles from fintech-apps.md, then use web_search/web_fetch to collect from all 6 sources (incl. YouTube comments and Reddit). Pass the collected records to process_reviews(); report qualified reviews and references. (No fallback dataset and no CSV export — collection is via the agent's built-in web tools.)
```

Leave evals #1, #3, #4 unchanged (their expected behavior still holds, minus any implicit crawler/CLI framing).

- [ ] **Step 2: Verify the file is valid JSON**

Run: `/usr/bin/python3 -c "import json; json.load(open('evals/evals.json')); print('valid')"`
Expected: prints `valid`.

- [ ] **Step 3: Commit**

```bash
git add evals/evals.json
git commit -m "docs: update eval #2 for web-tools collection"
```

---

### Task 8: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `/usr/bin/python3 -m unittest discover -s tests -v`
Expected: PASS — `tests.test_processing` (4) + `tests.test_agent_api` (4) = 8 tests, 0 failures.

- [ ] **Step 2: Repo-wide stale-reference sweep**

Run:
```bash
grep -rn -e "run_research" -e "review-crawler-service" -e "crawl_service" -e "REVIEW_CRAWLER" \
  -e "google-play-scraper" -e "yt-dlp" -e "BaseCrawler" -e "crawl_client" \
  SKILL.md scripts/ references/ evals/ tests/
```
Expected: no output. Any hit is a stale reference to fix.

- [ ] **Step 3: Confirm deleted files are gone**

Run: `ls scripts/crawl.py scripts/crawl_client.py scripts/main.py scripts/sources 2>&1 | head`
Expected: "No such file or directory" for each.

- [ ] **Step 4: Final commit (if the sweep required fixes)**

```bash
git add -A
git commit -m "chore: final cleanup for web-tools rewrite" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:**
- Delete crawl machinery → Task 3 ✓
- Add `processing.py` → Task 1 ✓
- `process_reviews()` interface + return contract → Task 2 ✓
- `SKILL.md` four-phase workflow → Task 4 ✓
- `references/sources.md` rewrite → Task 5 ✓
- `references/data-pipeline.md` edits → Task 6 ✓
- `requirements.txt` trim → Task 3 ✓
- Test rewrite + delete `test_crawl_client.py` → Tasks 2 & 3 ✓
- `evals.json` edit → Task 7 ✓
- Error/edge cases (empty input, unqualified dropped, unknown app) → Tasks 1, 2 tests ✓

**Type/name consistency:** `make_review(raw, default_app=...)`, `make_id`, `to_iso` (Task 1) are used exactly as defined by `process_reviews()` (Task 2). `process_reviews()` signature is identical in Task 2 impl, Task 4 SKILL.md example, and the spec. Return keys (`reviews`, `reviews_by_app`, `references`, `stats`, `apps`, `params`) consistent across tasks.

**Placeholder scan:** No TBD/TODO. The `...` tokens are sample review text only. Doc tasks specify exact tables, URLs, and field mappings rather than "write appropriate content."
