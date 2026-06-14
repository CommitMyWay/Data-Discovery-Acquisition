# Data Pipeline — Schema, Dedup & Input Contract

---

## Review Schema

Every review, regardless of source, is normalized to this schema:

```json
{
  "id": "sha256:abc123...",
  "source": "google_play",
  "app": "ZaloPay",
  "author": "nguyen_van_a",
  "rating": 4,
  "content": "Ứng dụng rất tiện, chuyển tiền nhanh và không mất phí...",
  "date": "2024-06-15T08:30:00Z",
  "url": "https://play.google.com/store/apps/details?id=com.vinagame.zalopay&reviewId=...",
  "language": "vi",
  "qualified": true,
  "disqualification_reasons": [],
  "metadata": {
    "thumbs_up": 12,
    "reply_content": null,
    "high_quality": true,
    "raw_source_id": "gp_review_xyz",

    // YouTube-specific
    "video_id": null,
    "video_title": null,
    "video_url": null,
    "is_transcript": false,

    // Reddit-specific
    "subreddit": null,
    "post_score": null,
    "comment_depth": null,

    // Forum-specific (Tinhte/Voz)
    "thread_title": null,
    "like_count": null
  }
}
```

### Field notes
| Field | Sources with value | Null for |
|-------|-------------------|----------|
| `rating` | Google Play, App Store | YouTube, Reddit, Tinhte, Voz |
| `author` | All (anonymized username) | YouTube transcripts → set `"[transcript]"` |
| `url` | All | — |
| `metadata.thumbs_up` | Google Play | Others → `0` |
| `metadata.video_id` | YouTube only | Others → `null` |
| `metadata.is_transcript` | YouTube only | Others → `false` |
| `metadata.subreddit` | Reddit only | Others → `null` |
| `metadata.thread_title` | Tinhte, Voz | Others → `null` |

---

## Generating the `id` Field

```python
import hashlib, re, json

def make_review_id(review: dict) -> str:
    # Normalize content for stable hashing
    content = re.sub(r'\s+', ' ', review["content"].lower()).strip()
    content = re.sub(r'[^\w\s\u00c0-\u024f\u1e00-\u1eff]', '', content)
    
    key = f"{review['source']}::{review['author']}::{content[:200]}"
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
```

---

## Deduplication Pipeline

Run after all sources are collected, before qualification.

### Pass 1: Exact content hash

```python
def dedup_by_content_hash(reviews: list) -> list:
    seen_hashes = {}
    result = []
    for r in reviews:
        h = make_review_id(r)
        if h not in seen_hashes:
            seen_hashes[h] = True
            result.append(r)
        # else: silently drop duplicate
    return result
```

### Pass 2: Composite key (author + date + rating)

Catches the same review posted on both Google Play and App Store by the same user (rare but happens with cross-platform review syndication).

```python
from datetime import datetime

def dedup_by_composite_key(reviews: list) -> list:
    seen_keys = {}
    result = []
    for r in reviews:
        date_str = r["date"][:10] if r["date"] else "unknown"  # YYYY-MM-DD
        key = f"{r['author']}::{date_str}::{r['rating']}"
        if key == "::unknown::None":
            result.append(r)  # Can't composite-key, always keep
            continue
        if key not in seen_keys:
            seen_keys[key] = True
            result.append(r)
    return result

def deduplicate(reviews: list) -> list:
    after_pass1 = dedup_by_content_hash(reviews)
    after_pass2 = dedup_by_composite_key(after_pass1)
    removed = len(reviews) - len(after_pass2)
    print(f"Dedup: removed {removed} duplicates ({len(after_pass2)} remaining)")
    return after_pass2
```

---

## Raw Record Input Contract

The agent collects raw review records via its built-in `web_search`/`web_fetch` tools and passes them as dicts to `process_reviews()` in `scripts/agent_api.py`. Each raw record must conform to the following shape:

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

Only `source`, `app`, and `content` are required; `make_review()` (in `scripts/processing.py`) defaults the rest.

Valid `source` values: `google_play`, `app_store`, `youtube`, `reddit`, `tinhte`, `voz`.

---

## Pipeline Execution Order

```
1. Agent collects reviews via built-in web_search/web_fetch tools (see SKILL.md)
       ↓
2. Merge all raw reviews into one list
       ↓
3. Dedup Pass 1 (content hash)
       ↓
4. Dedup Pass 2 (composite key)
       ↓
5. Language detection (add "language" field)
       ↓
6. Qualification (add "qualified" + "disqualification_reasons")
       ↓
7. Quality scoring (add "metadata.high_quality")
       ↓
8. Apply focus_area ordering (only when a focus topic is provided; otherwise reviews keep qualification order)
       ↓
9. Return analysis-ready data from process_reviews()
```
