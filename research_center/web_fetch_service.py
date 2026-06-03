"""
Web fetch service using requests + BeautifulSoup.
Fetches article content from URLs with fallback to Tavily Extract.

Search order: requests+BS4 → Tavily Extract
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

import bs4
import httpx
import requests

from .models import SourceItem

ProgressCallback = Callable[[str], None] | None

TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
MIN_CHINESE_CHARS = 300
MIN_ENGLISH_CHARS = 800


@dataclass(frozen=True)
class WebFetchResult:
    url: str
    title: str | None
    content: str
    content_status: str = "success"  # "success", "partial", "failed"
    fetch_provider: str = "requests_bs4"  # "requests_bs4", "tavily_extract"
    failure_reason: str | None = None


@dataclass(frozen=True)
class WebFetchServiceResult:
    results: list[WebFetchResult]
    total_urls: int
    successful: int
    failed: int
    diagnostics: dict[str, Any]


def fetch_web_content(
    url: str,
    timeout: float = 15.0,
    progress: ProgressCallback = None,
) -> WebFetchResult:
    """
    Fetch content from a single URL using requests + BeautifulSoup.
    Falls back to Tavily Extract on failure or partial content.
    """
    try:
        result = _fetch_with_requests(url, timeout)
        if result.content_status == "success":
            return result
        if result.content_status == "partial":
            return _fetch_with_tavily(url, None, progress)
        return _fetch_with_tavily(url, None, progress)
    except Exception as exc:
        return _fetch_with_tavily(url, str(exc), progress)


class WebFetchService:
    """
    Web fetch service using requests + BeautifulSoup.
    Provides structured fetch with progress reporting and Tavily Extract fallback.

    Usage:
        service = WebFetchService()
        result = service.fetch(url)
        results = service.fetch_many([url1, url2, ...])
    """

    def __init__(
        self,
        timeout: float = 15.0,
        max_workers: int = 4,
        min_chinese_chars: int = MIN_CHINESE_CHARS,
        min_english_chars: int = MIN_ENGLISH_CHARS,
    ):
        self.timeout = timeout
        self.max_workers = max_workers
        self.min_chinese_chars = min_chinese_chars
        self.min_english_chars = min_english_chars

    def fetch(self, url: str, progress: ProgressCallback = None) -> WebFetchResult:
        """Fetch content from a single URL."""
        return fetch_web_content(url, timeout=self.timeout, progress=progress)

    def fetch_many(self, urls: list[str], progress: ProgressCallback = None) -> WebFetchServiceResult:
        """
        Fetch content from multiple URLs concurrently.
        Uses ThreadPoolExecutor for parallel fetching.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: list[WebFetchResult] = []
        failed_urls: list[str] = []
        total = len(urls)

        if not urls:
            return WebFetchServiceResult([], 0, 0, 0, {})

        def fetch_one(url: str) -> WebFetchResult:
            return fetch_web_content(url, timeout=self.timeout, progress=progress)

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_url = {executor.submit(fetch_one, url): url for url in urls}
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    result = future.result()
                    results.append(result)
                    if result.content_status == "failed":
                        failed_urls.append(url)
                except Exception:
                    results.append(WebFetchResult(
                        url=url,
                        title=None,
                        content="",
                        content_status="failed",
                        fetch_provider="requests_bs4",
                    ))
                    failed_urls.append(url)

        successful = sum(1 for r in results if r.content_status == "success")
        failed = len(failed_urls)

        diagnostics: dict[str, Any] = {
            "total_urls": total,
            "successful": successful,
            "failed": failed,
            "fetch_provider": "requests_bs4",
        }

        return WebFetchServiceResult(results, total, successful, failed, diagnostics)


def _fetch_with_requests(url: str, timeout: float) -> WebFetchResult:
    """Fetch URL using requests + BeautifulSoup."""
    try:
        response = requests.get(url, headers=COMMON_HEADERS, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
    except Exception as exc:
        return WebFetchResult(
            url=url,
            title=None,
            content="",
            content_status="failed",
            fetch_provider="requests_bs4",
            failure_reason=str(exc),
        )
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        return WebFetchResult(
            url=url,
            title=None,
            content=response.text[:10000],
            content_status="success",
            fetch_provider="requests_bs4",
        )

    soup = bs4.BeautifulSoup(response.text, "html.parser")

    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    main = soup.find("main") or soup.find("article") or soup.find("div", class_=re.compile(r"content|article|post|entry", re.I)) or soup

    text = main.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    total_chars = len(text)
    if chinese_chars >= MIN_CHINESE_CHARS or total_chars >= MIN_ENGLISH_CHARS:
        title = soup.find("title")
        title_text = title.get_text(strip=True) if title else None
        return WebFetchResult(
            url=url,
            title=title_text,
            content=text[:15000],
            content_status="success",
            fetch_provider="requests_bs4",
        )
    else:
        return WebFetchResult(
            url=url,
            title=soup.find("title").get_text(strip=True) if soup.find("title") else None,
            content=text[:5000],
            content_status="partial",
            fetch_provider="requests_bs4",
            failure_reason=f"content_too_short: chinese={chinese_chars}, total={total_chars}",
        )


def _fetch_with_tavily(url: str, exc: str | None = None, progress: ProgressCallback = None) -> WebFetchResult:
    """
    Fallback fetch via Tavily Extract API.
    """
    if progress:
        progress(f"WebFetch Tavily Extract fallback: {url}")

    from .config import load_research_config

    config = load_research_config()
    api_keys = list(config.tavily_api_keys or ())
    if not api_keys and config.tavily_api_key:
        api_keys = [config.tavily_api_key]

    if not api_keys:
        return WebFetchResult(
            url=url,
            title=None,
            content="",
            content_status="failed",
            fetch_provider="tavily_extract",
            failure_reason=exc or "tavily_api_key_not_configured",
        )

    if not config.enable_tavily_extract:
        return WebFetchResult(
            url=url,
            title=None,
            content="",
            content_status="failed",
            fetch_provider="tavily_extract",
            failure_reason=exc or "tavily_extract_disabled_by_config",
        )

    last_result: WebFetchResult | None = None
    for key_index, api_key in enumerate(api_keys, 1):
        result = _fetch_with_tavily_key(url, api_key, key_index)
        if result.content_status == "success":
            return result
        last_result = result
        if not _is_tavily_quota_failure(result.failure_reason or ""):
            return result
    return last_result or WebFetchResult(
        url=url,
        title=None,
        content="",
        content_status="failed",
        fetch_provider="tavily_extract",
        failure_reason=exc or "all_tavily_extract_keys_failed",
    )


def _fetch_with_tavily_key(url: str, api_key: str, key_index: int) -> WebFetchResult:
    try:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: dict[str, Any] = {
            "urls": [url],
            "extract_depth": "basic",
            "format": "markdown",
        }

        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.post(TAVILY_EXTRACT_URL, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        results = data.get("results") or []
        if results:
            item = results[0]
            return WebFetchResult(
                url=url,
                title=str(item.get("title") or url),
                content=str(item.get("raw_content") or item.get("content") or item.get("markdown") or "")[:15000],
                content_status="success",
                fetch_provider="tavily_extract",
                failure_reason=None,
            )
        else:
            return WebFetchResult(
                url=url,
                title=None,
                content="",
                content_status="failed",
                fetch_provider="tavily_extract",
                failure_reason=f"tavily_extract_returned_no_results:key_index={key_index}",
            )
    except Exception as fetch_exc:
        return WebFetchResult(
            url=url,
            title=None,
            content="",
            content_status="failed",
            fetch_provider="tavily_extract",
            failure_reason=f"{fetch_exc}:key_index={key_index}",
        )


def _is_tavily_quota_failure(text: str) -> bool:
    lower = str(text or "").lower()
    return any(term in lower for term in ("quota", "credit", "limit", "insufficient", "exceeded", "402", "429", "432", "433"))


def fetch_many(
    urls: list[str],
    progress: ProgressCallback = None,
    max_workers: int = 4,
) -> WebFetchServiceResult:
    """
    Fetch content from multiple URLs concurrently.
    Uses ThreadPoolExecutor for parallel fetching.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: list[WebFetchResult] = []
    failed_urls: list[str] = []
    total = len(urls)

    if not urls:
        return WebFetchServiceResult([], 0, 0, 0, {})

    def fetch_one(url: str) -> WebFetchResult:
        return fetch_web_content(url, progress=progress)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(fetch_one, url): url for url in urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                result = future.result()
                results.append(result)
                if result.content_status == "fetch_failed":
                    failed_urls.append(url)
            except Exception as exc:
                results.append(WebFetchResult(
                    url=url,
                    title=None,
                    content="",
                    content_status="fetch_failed",
                    fetch_provider="requests_bs4",
                ))
                failed_urls.append(url)

    successful = sum(1 for r in results if r.content_status not in ("fetch_failed", "tavily_extract_failed"))
    failed = len(failed_urls)

    diagnostics = {
        "total_urls": total,
        "successful": successful,
        "failed": failed,
        "fetch_provider": "requests_bs4",
    }

    return WebFetchServiceResult(results, total, successful, failed, diagnostics)
