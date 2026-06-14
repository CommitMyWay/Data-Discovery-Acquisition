# Data Pipeline — Schema, Dedup & Fallback

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

## Fallback Dataset Format

When a live source fails after all retries, the pipeline loads from `--fallback-dataset`. The fallback file must be a JSON array of reviews in the same schema above, with an additional `"fallback_collected_at"` field indicating when the data was originally captured.

```json
[
  {
    "id": "sha256:abc123...",
    "source": "google_play",
    "app": "ZaloPay",
    "author": "tran_thi_b",
    "rating": 2,
    "content": "App hay bị lỗi khi đăng nhập...",
    "date": "2024-05-10T14:22:00Z",
    "url": "https://play.google.com/...",
    "language": "vi",
    "qualified": null,
    "disqualification_reasons": [],
    "metadata": { "thumbs_up": 3 },
    "fallback_collected_at": "2024-07-01T00:00:00Z"
  }
]
```

### Loading fallback data

```python
def load_fallback(fallback_path: str, source: str, app: str) -> list:
    if not fallback_path or not os.path.exists(fallback_path):
        print(f"  [fallback] No fallback dataset at {fallback_path}")
        return []
    
    with open(fallback_path, "r", encoding="utf-8") as f:
        all_data = json.load(f)
    
    # Filter to only the failed source + app
    matched = [r for r in all_data if r.get("source") == source and r.get("app") == app]
    print(f"  [fallback] Loaded {len(matched)} records for {source}/{app}")
    return matched
```

Reviews loaded from fallback get `metadata.from_fallback: true` so they can be tracked in the output.

---

## Pipeline Execution Order

```
1. Crawl all sources (parallel where possible, serial if rate limited)
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
8. Sort by date DESC
       ↓
9. Write to output JSON + CSV
       ↓
10. Print summary report
```
