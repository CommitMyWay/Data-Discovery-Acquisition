"""
HTTP client for delegating crawl work to review-crawler-service.

The skill keeps analysis locally; this module only sends the crawl request,
polls the async job, and returns the service payload.
"""

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_POLL_INTERVAL_SECONDS = 2
DEFAULT_MAX_WAIT_SECONDS = 900


async def crawl_reviews(
    *,
    base_url: str,
    subject: str,
    market: str,
    goal: str,
    focus: str = None,
    sources: list = None,
    rating_min: int = 1,
    rating_max: int = 5,
    days_back: int = 180,
    filters: dict = None,
    token: str = None,
    request_timeout: int = DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
) -> dict:
    payload = {
        "subject": subject,
        "market": market,
        "goal": goal,
        "focus": focus,
        "data_source": sources or [],
        "rating_min": rating_min,
        "rating_max": rating_max,
        "days_back": days_back,
        "filters": filters or {},
    }

    root = base_url.rstrip("/")
    created = await asyncio.to_thread(
        _request_json,
        "POST",
        f"{root}/crawl",
        payload=payload,
        token=token,
        timeout=request_timeout,
    )

    job_id = created.get("job_id")
    if not job_id:
        raise RuntimeError(f"crawler service did not return job_id: {created}")

    deadline = time.monotonic() + max_wait_seconds
    while True:
        result = await asyncio.to_thread(
            _request_json,
            "GET",
            f"{root}/crawl/{urllib.parse.quote(str(job_id), safe='')}",
            token=token,
            timeout=request_timeout,
        )

        status = result.get("status")
        if status == "completed":
            return result
        if status == "failed":
            raise RuntimeError(result.get("error") or "crawler service job failed")
        if status != "running":
            raise RuntimeError(f"unexpected crawler service status: {status}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"crawler service job timed out after {max_wait_seconds}s")

        await asyncio.sleep(poll_interval)


def _request_json(
    method: str,
    url: str,
    *,
    payload: dict = None,
    token: str = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"{method} {url} failed: {error.reason}") from error
