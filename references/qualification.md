# Data Qualification Rules

All reviews pass through these gates. Each failed gate adds a reason to `disqualification_reasons[]`. A review with **any** disqualification reason has `qualified: false`.

---

## 1. Recency Filter

```python
from datetime import datetime, timedelta, timezone

def check_recency(review_date: datetime, days_back: int = 365) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    return review_date.replace(tzinfo=timezone.utc) >= cutoff
```

**Failure reason**: `"too_old"` — review date is older than `--days-back` days.

**Edge case**: Reviews with no date (`date: null`) are kept but flagged: `"date_unknown"`. Include in qualified set but mark separately.

---

## 2. Minimum Length Filter

```python
import re

def check_length(content: str, min_chars: int = 30) -> bool:
    cleaned = re.sub(r'\s+', ' ', content).strip()
    return len(cleaned) >= min_chars
```

**Failure reason**: `"too_short"` — content is fewer than `min_chars` characters after whitespace normalization.

**Why 30?** A meaningful opinion needs at least a subject and a verb. "Ứng dụng tốt, dễ dùng, ổn định" = 34 chars and is borderline acceptable. "OK" or "5 sao" = discard.

---

## 3. Language Filter

```python
from langdetect import detect, LangDetectException

def check_language(content: str, allowed: list = ["vi", "en"]) -> tuple[bool, str]:
    try:
        lang = detect(content)
        return lang in allowed, lang
    except LangDetectException:
        # Very short text — default to "vi" for Vietnamese-market sources
        return True, "vi"
```

**Failure reason**: `"wrong_language:{detected_lang}"` — e.g. `"wrong_language:zh-cn"` for Chinese content.

**Notes**:
- `langdetect` struggles with very short Vietnamese text — texts under 20 chars are always passed with `lang: "vi"` assumed
- Mixed VN+EN content: `langdetect` usually returns `"vi"` — that's fine, keep it
- For Reddit results in English about VN apps, `"en"` is allowed by default

---

## 4. Star Rating Filter

```python
def check_rating(rating, rating_min: int = 1, rating_max: int = 5) -> bool:
    if rating is None:
        return True  # No rating available (YouTube, Reddit, forums) — don't disqualify
    return rating_min <= int(rating) <= rating_max
```

**Failure reason**: `"rating_out_of_range"` — rating outside `[--rating-min, --rating-max]`.

**Default**: Keep all ratings (1–5). Common use case: `--rating-min 1 --rating-max 2` to focus on negative feedback only.

---

## 5. Spam & Bot Detection

A review fails spam detection if it matches **any** of the following signals:

### 5a. Repetition pattern
```python
def is_repetitive(content: str) -> bool:
    words = content.lower().split()
    if len(words) < 3:
        return False
    # More than 60% of words are identical
    from collections import Counter
    counter = Counter(words)
    most_common_freq = counter.most_common(1)[0][1]
    return most_common_freq / len(words) > 0.6
```

### 5b. Phone number / referral spam
```python
import re

SPAM_PATTERNS = [
    r'\b0[3-9]\d{8}\b',          # Vietnamese phone numbers
    r'\bref(erral)?\s*code\b',    # Referral code spam
    r'\bliên hệ\b.*\b0\d{9}\b',  # "Contact me at 0xxx"
    r'(http|https)://\S+',        # Links (suspicious in reviews)
    r'\bclick\s+here\b',
    r'\btải\s+ngay\b.*\bmiễn phí\b',  # "Download now for free" promo
]

def has_spam_pattern(content: str) -> bool:
    content_lower = content.lower()
    return any(re.search(p, content_lower, re.IGNORECASE) for p in SPAM_PATTERNS)
```

### 5c. Copy-paste duplicate detection (near-duplicate)
```python
from difflib import SequenceMatcher

def is_near_duplicate(content: str, existing_contents: list, threshold: float = 0.85) -> bool:
    for existing in existing_contents:
        ratio = SequenceMatcher(None, content.lower(), existing.lower()).ratio()
        if ratio >= threshold:
            return True
    return False
```
Note: This is computationally expensive for large sets. Only run on reviews that share the same `(source, date)` tuple to limit comparisons.

### 5d. Gibberish detection
```python
def is_gibberish(content: str) -> bool:
    # High ratio of non-alphanumeric, non-Vietnamese characters
    clean = re.sub(r'[\s\.,!?;:\-\(\)]', '', content)
    if not clean:
        return True
    non_alpha = sum(1 for c in clean if not (c.isalpha() or '\u00c0' <= c <= '\u024f' or '\u1e00' <= c <= '\u1eff'))
    return non_alpha / len(clean) > 0.4
```

**Failure reasons**: `"spam_repetitive"`, `"spam_pattern_match"`, `"spam_near_duplicate"`, `"spam_gibberish"`

---

## 6. Quality Standard (Chất Lượng Review Đạt Chuẩn)

Beyond the mechanical filters above, a review is considered **high-quality** if it has:
- Length ≥ 80 characters
- Mentions a specific feature, issue, or experience (not just sentiment words)
- Has a `thumbs_up` / `like_count` > 0 (signals other users found it useful)

High-quality reviews get `metadata.high_quality: true` for downstream analysis prioritization. This is **not** a disqualification gate — a review can be `qualified: true` but `high_quality: false`.

```python
FEATURE_KEYWORDS_VI = [
    "chuyển tiền", "nạp tiền", "rút tiền", "thanh toán", "lãi suất",
    "phí", "tài khoản", "đăng nhập", "bảo mật", "otp", "xác thực",
    "app", "ứng dụng", "cập nhật", "lỗi", "sập", "chậm", "nhanh",
    "dịch vụ", "hỗ trợ", "chăm sóc", "hoàn tiền", "khuyến mãi"
]

def is_high_quality(review: dict) -> bool:
    content = review["content"].lower()
    has_feature = any(kw in content for kw in FEATURE_KEYWORDS_VI)
    has_length = len(content) >= 80
    has_engagement = review.get("metadata", {}).get("thumbs_up", 0) > 0
    return has_feature and has_length
```

---

## Qualification Summary Report

After qualification, print:
```
=== Qualification Summary ===
Total collected  : 1,842
Qualified        : 1,204 (65.4%)
Disqualified     : 638

Disqualification breakdown:
  too_old              : 201
  too_short            : 189
  spam_pattern_match   : 98
  spam_near_duplicate  : 74
  wrong_language:zh-cn : 41
  spam_repetitive      : 35

High-quality reviews : 487 (40.4% of qualified)
```
