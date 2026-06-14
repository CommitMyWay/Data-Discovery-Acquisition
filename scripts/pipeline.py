"""
pipeline.py — Deduplication and data qualification pipeline.
Runs after all sources are crawled and merged.
"""

import hashlib
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _normalize_for_hash(content: str) -> str:
    text = content.lower()
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'[^\w\s\u00c0-\u024f\u1e00-\u1eff]', '', text)
    return text


def _content_hash(author: str, content: str) -> str:
    # Source intentionally excluded — catches the same review posted across platforms
    key = f"{author}::{_normalize_for_hash(content)[:200]}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def dedup_by_content_hash(reviews: list) -> tuple[list, int]:
    seen = {}
    result = []
    for r in reviews:
        h = _content_hash(r.get("author", ""), r.get("content", ""))
        if h not in seen:
            seen[h] = True
            result.append(r)
    removed = len(reviews) - len(result)
    return result, removed


def dedup_by_composite_key(reviews: list) -> tuple[list, int]:
    seen = set()
    result = []
    for r in reviews:
        date_str = (r.get("date") or "")[:10]
        author = r.get("author") or ""
        rating = str(r.get("rating") or "null")

        # Skip composite-keying if not enough data
        if not author or author in ("anonymous", "[transcript]") or not date_str:
            result.append(r)
            continue

        key = f"{author}::{date_str}::{rating}"
        if key not in seen:
            seen.add(key)
            result.append(r)
    removed = len(reviews) - len(result)
    return result, removed


def deduplicate(reviews: list) -> list:
    after_hash, removed_hash = dedup_by_content_hash(reviews)
    after_composite, removed_composite = dedup_by_composite_key(after_hash)
    total_removed = removed_hash + removed_composite
    logger.info(
        "Dedup: removed %d exact + %d composite = %d total (%d remaining)",
        removed_hash, removed_composite, total_removed, len(after_composite)
    )
    return after_composite


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

# Unicode ranges for scripts that should never appear in vi/en reviews
_CJK_RANGE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')          # Chinese/Japanese kanji
_KOREAN_RANGE = re.compile(r'[\uac00-\ud7af\u1100-\u11ff]')         # Korean
_ARABIC_RANGE = re.compile(r'[\u0600-\u06ff]')                       # Arabic
_CYRILLIC_RANGE = re.compile(r'[\u0400-\u04ff]')                     # Russian/Cyrillic
_THAI_RANGE = re.compile(r'[\u0e00-\u0e7f]')                         # Thai

_SCRIPT_CHECKS = [
    (_CJK_RANGE,      "zh-cn"),
    (_KOREAN_RANGE,   "ko"),
    (_ARABIC_RANGE,   "ar"),
    (_CYRILLIC_RANGE, "ru"),
    (_THAI_RANGE,     "th"),
]

def detect_language(content: str) -> str:
    """
    Detect language with a two-stage approach:
    1. Unicode script pre-check — catches CJK, Korean, Arabic, etc. reliably
       before langdetect even runs (langdetect often misidentifies these as vi)
    2. langdetect — for distinguishing vi vs en and other Latin-script languages
    """
    # Stage 1: script pre-check (fast, reliable for non-Latin scripts)
    for pattern, lang_code in _SCRIPT_CHECKS:
        if pattern.search(content):
            return lang_code

    # Stage 2: langdetect for Latin-script languages
    if len(content.strip()) < 20:
        return "vi"  # Default for very short Vietnamese-market content
    try:
        from langdetect import detect
        return detect(content)
    except Exception:
        return "vi"


# ---------------------------------------------------------------------------
# Qualification filters
# ---------------------------------------------------------------------------

SPAM_PATTERNS = [
    re.compile(r'\b0[3-9]\d{8}\b'),                        # VN phone numbers
    re.compile(r'\bref(erral)?\s*code\b', re.IGNORECASE),  # Referral code spam
    re.compile(r'(http|https)://\S+'),                      # URLs in reviews
    re.compile(r'\bclick\s+here\b', re.IGNORECASE),
    re.compile(r'\btải\s+ngay\b.*\bmiễn phí\b', re.IGNORECASE),  # Promo spam
    re.compile(r'\bliên hệ\b.*\b0\d{9}\b', re.IGNORECASE),
    re.compile(r'(zalo|telegram|fb).*\b0\d{9}\b', re.IGNORECASE),
]

FEATURE_KEYWORDS_VI = [
    "chuyển tiền", "nạp tiền", "rút tiền", "thanh toán", "lãi suất",
    "phí", "tài khoản", "đăng nhập", "bảo mật", "otp", "xác thực",
    "app", "ứng dụng", "cập nhật", "lỗi", "sập", "chậm", "nhanh",
    "dịch vụ", "hỗ trợ", "chăm sóc", "hoàn tiền", "khuyến mãi",
    "giao dịch", "ngân hàng", "liên kết", "thẻ", "tích điểm", "cashback",
    "lỗi 404", "crash", "đóng băng", "không mở", "lâu", "bảo mật"
]


def check_recency(date_str: Optional[str], days_back: int) -> tuple[bool, Optional[str]]:
    if not date_str:
        return True, "date_unknown"  # Keep but note it
    try:
        if date_str.endswith("Z"):
            date_str = date_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        return dt >= cutoff, None
    except Exception:
        return True, "date_parse_error"


def check_length(content: str, min_chars: int) -> bool:
    return len(re.sub(r'\s+', ' ', content).strip()) >= min_chars


def check_language(content: str, allowed_langs: list) -> tuple[bool, str]:
    lang = detect_language(content)
    return lang in allowed_langs, lang


def check_rating(rating, rating_min: int, rating_max: int) -> bool:
    if rating is None:
        return True
    try:
        return rating_min <= int(rating) <= rating_max
    except (ValueError, TypeError):
        return True


def is_repetitive(content: str) -> bool:
    words = content.lower().split()
    if len(words) < 5:
        return False
    counter = Counter(words)
    most_common_freq = counter.most_common(1)[0][1]
    return most_common_freq / len(words) > 0.6


def has_spam_pattern(content: str) -> bool:
    return any(p.search(content) for p in SPAM_PATTERNS)


def is_gibberish(content: str) -> bool:
    clean = re.sub(r'[\s\.,!?;:\-\(\)"\']', '', content)
    if not clean:
        return True
    non_alpha = sum(
        1 for c in clean
        if not (c.isalpha() or '\u00c0' <= c <= '\u024f' or '\u1e00' <= c <= '\u1eff')
    )
    return non_alpha / len(clean) > 0.5


def is_high_quality(review: dict) -> bool:
    content = (review.get("content") or "").lower()
    has_feature = any(kw in content for kw in FEATURE_KEYWORDS_VI)
    has_length = len(content.strip()) >= 80
    engagement = (
        ((review.get("metadata") or {}).get("thumbs_up") or 0) > 0
        or ((review.get("metadata") or {}).get("like_count") or 0) > 0
        or ((review.get("metadata") or {}).get("post_score") or 0) > 2
    )
    return has_feature and has_length


def qualify_review(
    review: dict,
    days_back: int = 365,
    min_chars: int = 30,
    allowed_langs: list = None,
    rating_min: int = 1,
    rating_max: int = 5,
) -> dict:
    if allowed_langs is None:
        allowed_langs = ["vi", "en"]

    reasons = []
    content = review.get("content") or ""

    # 1. Recency
    passed, note = check_recency(review.get("date"), days_back)
    if note:
        reasons.append(note)  # "date_unknown" or "date_parse_error" — not disqualifying
    if not passed:
        reasons.append("too_old")

    # 2. Length
    if not check_length(content, min_chars):
        reasons.append("too_short")

    # 3. Language
    lang_ok, detected_lang = check_language(content, allowed_langs)
    review["language"] = detected_lang
    if not lang_ok:
        reasons.append(f"wrong_language:{detected_lang}")

    # 4. Rating
    if not check_rating(review.get("rating"), rating_min, rating_max):
        reasons.append("rating_out_of_range")

    # 5. Spam checks
    if is_repetitive(content):
        reasons.append("spam_repetitive")
    if has_spam_pattern(content):
        reasons.append("spam_pattern_match")
    if is_gibberish(content):
        reasons.append("spam_gibberish")

    # Disqualifying reasons = everything except "date_unknown" / "date_parse_error"
    blocking_reasons = [r for r in reasons if r not in ("date_unknown", "date_parse_error")]

    review["qualified"] = len(blocking_reasons) == 0
    review["disqualification_reasons"] = reasons

    # 6. High-quality flag
    if review["qualified"]:
        review["metadata"] = review.get("metadata") or {}
        review["metadata"]["high_quality"] = is_high_quality(review)

    return review


def qualify(reviews: list, **kwargs) -> list:
    for r in reviews:
        qualify_review(r, **kwargs)
    return reviews


# ---------------------------------------------------------------------------
# Near-duplicate detection (post-qualification)
# ---------------------------------------------------------------------------

def mark_near_duplicates(reviews: list, threshold: float = 0.85) -> list:
    """
    Among qualified reviews, mark near-duplicates (similarity >= threshold).
    Groups reviews by (source, date[:10]) to limit O(n²) comparisons.
    """
    qualified = [r for r in reviews if r.get("qualified")]
    groups: dict[str, list] = {}
    for r in qualified:
        key = f"{r.get('source', '')}::{(r.get('date') or '')[:10]}"
        groups.setdefault(key, []).append(r)

    for group in groups.values():
        if len(group) < 2:
            continue
        contents = [_normalize_for_hash(r.get("content", "")) for r in group]
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                ratio = SequenceMatcher(None, contents[i], contents[j]).ratio()
                if ratio >= threshold:
                    # Mark the later one as duplicate (keep earlier)
                    group[j]["qualified"] = False
                    reasons = group[j].get("disqualification_reasons", [])
                    if "spam_near_duplicate" not in reasons:
                        reasons.append("spam_near_duplicate")
                    group[j]["disqualification_reasons"] = reasons

    return reviews


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------

def print_summary(reviews: list, app_name: str):
    total = len(reviews)
    qualified = [r for r in reviews if r.get("qualified")]
    disqualified = [r for r in reviews if not r.get("qualified")]
    high_quality = [r for r in qualified if r.get("metadata", {}).get("high_quality")]

    # By source
    sources = Counter(r.get("source", "unknown") for r in qualified)
    # Disqualification reasons
    all_reasons = []
    for r in disqualified:
        all_reasons.extend(r.get("disqualification_reasons", []))
    reason_counts = Counter(all_reasons)

    print(f"\n{'='*50}")
    print(f" Review Collection Summary — {app_name}")
    print(f"{'='*50}")
    print(f" Total collected   : {total:,}")
    print(f" Qualified         : {len(qualified):,} ({100*len(qualified)/max(total,1):.1f}%)")
    print(f" Disqualified      : {len(disqualified):,}")
    print()
    print(f" Source breakdown (qualified):")
    for source, count in sorted(sources.items(), key=lambda x: -x[1]):
        print(f"   {source:<20}: {count:,}")
    print()
    print(f" Disqualification reasons:")
    for reason, count in reason_counts.most_common():
        print(f"   {reason:<30}: {count:,}")
    print()
    print(f" High-quality reviews: {len(high_quality):,} ({100*len(high_quality)/max(len(qualified),1):.1f}% of qualified)")
    print(f"{'='*50}\n")
