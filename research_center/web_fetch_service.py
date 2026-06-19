"""
Web fetch service using requests + BeautifulSoup.
Fetches article content from URLs with fallback to Tavily Extract.

Search order: requests+BS4 → Tavily Extract
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

import bs4
import httpx
import requests

from .models import SourceItem
from .source_date_normalizer import normalize_published_date

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
    published_date: str | None = None
    content_status: str = "success"  # "success", "partial", "failed"
    fetch_provider: str = "requests_bs4"  # "requests_bs4", "tavily_extract"
    fetch_quality: str = "high"  # "high", "medium", "low"
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
    expected_terms: list[str] | None = None,
) -> WebFetchResult:
    """
    Fetch content from a single URL using requests + BeautifulSoup.
    Falls back to Tavily Extract on failure or partial content.
    """
    try:
        result = _fetch_with_requests(url, timeout, expected_terms=expected_terms)
        if result.content_status == "success":
            return result
        if result.content_status == "partial":
            fallback = _fetch_with_tavily(url, None, progress, expected_terms=expected_terms)
            if fallback.content_status == "success":
                return _merge_fetch_results(result, fallback)
            return result
        fallback = _fetch_with_tavily(url, None, progress, expected_terms=expected_terms)
        return fallback
    except Exception as exc:
        return _fetch_with_tavily(url, str(exc), progress, expected_terms=expected_terms)


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

    def fetch_many(
        self,
        urls: list[str],
        progress: ProgressCallback = None,
        expected_terms: list[str] | None = None,
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
            return fetch_web_content(url, timeout=self.timeout, progress=progress, expected_terms=expected_terms)

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
                        fetch_quality="low",
                        failure_reason="timeout_or_fetch_exception",
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


def _fetch_with_requests(url: str, timeout: float, expected_terms: list[str] | None = None) -> WebFetchResult:
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
            fetch_quality="low",
            failure_reason=str(exc),
        )
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        quality, reason = _assess_fetch_quality(url, response.text[:10000], None, None, expected_terms)
        return WebFetchResult(
            url=url,
            title=None,
            content=response.text[:10000],
            content_status="success" if quality != "low" else "partial",
            fetch_provider="requests_bs4",
            fetch_quality=quality,
            failure_reason=reason,
        )

    soup = bs4.BeautifulSoup(response.text, "html.parser")
    published_date = _extract_html_published_date(soup, response.text, getattr(response, "url", None) or url)

    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    main = soup.find("main") or soup.find("article") or soup.find("div", class_=re.compile(r"content|article|post|entry", re.I)) or soup

    text = main.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    total_chars = len(text)
    title = soup.find("title")
    title_text = title.get_text(strip=True) if title else None
    quality, reason = _assess_fetch_quality(url, text, title_text, published_date, expected_terms)
    if chinese_chars >= MIN_CHINESE_CHARS or total_chars >= MIN_ENGLISH_CHARS:
        return WebFetchResult(
            url=url,
            title=title_text,
            content=text[:15000],
            published_date=published_date,
            content_status="success" if quality != "low" else "partial",
            fetch_provider="requests_bs4",
            fetch_quality=quality,
            failure_reason=reason,
        )
    else:
        short_reason = f"too_short: chinese={chinese_chars}, total={total_chars}"
        combined_reason = f"{short_reason}; {reason}" if reason else short_reason
        return WebFetchResult(
            url=url,
            title=soup.find("title").get_text(strip=True) if soup.find("title") else None,
            content=text[:5000],
            published_date=published_date,
            content_status="partial",
            fetch_provider="requests_bs4",
            fetch_quality="low" if quality == "low" else "medium",
            failure_reason=combined_reason,
        )


def _fetch_with_tavily(
    url: str,
    exc: str | None = None,
    progress: ProgressCallback = None,
    expected_terms: list[str] | None = None,
) -> WebFetchResult:
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
            fetch_quality="low",
            failure_reason=exc or "tavily_api_key_not_configured",
        )

    if not config.enable_tavily_extract:
        return WebFetchResult(
            url=url,
            title=None,
            content="",
            content_status="failed",
            fetch_provider="tavily_extract",
            fetch_quality="low",
            failure_reason=exc or "tavily_extract_disabled_by_config",
        )

    last_result: WebFetchResult | None = None
    for key_index, api_key in enumerate(api_keys, 1):
        result = _fetch_with_tavily_key(url, api_key, key_index, expected_terms=expected_terms)
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
        fetch_quality="low",
        failure_reason=exc or "all_tavily_extract_keys_failed",
    )


def _fetch_with_tavily_key(url: str, api_key: str, key_index: int, expected_terms: list[str] | None = None) -> WebFetchResult:
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
            content = str(item.get("raw_content") or item.get("content") or item.get("markdown") or "")
            title = str(item.get("title") or url)
            published_date = normalize_published_date(item)
            quality, reason = _assess_fetch_quality(url, content, title, published_date, expected_terms)
            return WebFetchResult(
                url=url,
                title=title,
                content=content[:15000],
                published_date=published_date,
                content_status="success" if quality != "low" else "partial",
                fetch_provider="tavily_extract",
                fetch_quality=quality,
                failure_reason=reason,
            )
        else:
            return WebFetchResult(
                url=url,
                title=None,
                content="",
                content_status="failed",
                fetch_provider="tavily_extract",
                fetch_quality="low",
                failure_reason=f"tavily_extract_returned_no_results:key_index={key_index}",
            )
    except Exception as fetch_exc:
        return WebFetchResult(
            url=url,
            title=None,
            content="",
            content_status="failed",
            fetch_provider="tavily_extract",
            fetch_quality="low",
            failure_reason=f"{fetch_exc}:key_index={key_index}",
        )


def _assess_fetch_quality(
    url: str,
    content: str,
    title: str | None,
    published_date: str | None,
    expected_terms: list[str] | None = None,
) -> tuple[str, str | None]:
    text = f"{title or ''} {content or ''}".strip()
    lower_url = (url or "").lower()
    reasons: list[str] = []
    if _looks_like_non_article_page(lower_url):
        reasons.append("non_article_page")
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    total_chars = len(text)
    if chinese_chars < 120 and total_chars < 500:
        reasons.append("too_short")
    terms = [term.strip().lower() for term in (expected_terms or []) if str(term or "").strip()]
    if terms:
        haystack = f"{text} {lower_url}".lower()
        if not any(term in haystack for term in terms):
            reasons.append("no_keyword_match")
    if not published_date:
        reasons.append("missing_published_date")
    if not _looks_like_article_text(text):
        reasons.append("not_article_like")

    severe = {"non_article_page", "too_short", "no_keyword_match"}
    if any(reason in severe for reason in reasons):
        return "low", ";".join(reasons)
    if reasons:
        return "medium", ";".join(reasons)
    return "high", None


def _looks_like_non_article_page(lower_url: str) -> bool:
    parsed = urlparse(lower_url)
    path = parsed.path.rstrip("/")
    if not path or path == "/":
        return True
    if any(part in path for part in (
        "/search", "/query", "/tag", "/tags", "/category", "/categories", "/list",
        "/rank", "/ranking", "/watch", "/channel", "/user", "/profile", "/login",
        "/market", "/quote", "/stock/", "/stocks/",
    )):
        return True
    if any(domain in parsed.netloc for domain in ("youtube.com", "youtu.be", "facebook.com", "instagram.com", "threads.net", "threads.com")):
        return True
    return False


def _looks_like_article_text(text: str) -> bool:
    if not text:
        return False
    paragraph_count = len([part for part in re.split(r"\n{1,}", text) if len(part.strip()) >= 30])
    punctuation_count = sum(text.count(mark) for mark in ("。", "，", "；", ".", ","))
    return paragraph_count >= 2 or punctuation_count >= 8


def _is_tavily_quota_failure(text: str) -> bool:
    lower = str(text or "").lower()
    return any(term in lower for term in ("quota", "credit", "limit", "insufficient", "exceeded", "402", "429", "432", "433"))


def _extract_html_published_date(soup: bs4.BeautifulSoup, raw_html: str, url: str = "") -> str | None:
    candidates: list[str] = []
    meta_selectors = [
        ("property", "article:published_time"),
        ("property", "article:modified_time"),
        ("name", "date"),
        ("name", "pubdate"),
        ("name", "publishdate"),
        ("name", "published_time"),
        ("itemprop", "datePublished"),
        ("itemprop", "dateModified"),
    ]
    for attr, value in meta_selectors:
        for tag in soup.find_all("meta", attrs={attr: value}):
            content = tag.get("content")
            if content:
                candidates.append(str(content))
    for tag in soup.find_all("time"):
        for attr in ("datetime", "content"):
            value = tag.get(attr)
            if value:
                candidates.append(str(value))
        text = tag.get_text(" ", strip=True)
        if text:
            candidates.append(text)
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        text = script.get_text(" ", strip=True)
        if text:
            candidates.append(text[:20000])
    page_text = soup.get_text(" ", strip=True)
    for pattern in (
        r"(?:發布時間|更新時間|發稿時間|刊登時間|出版時間|記者|中央社)\s*[:：]?\s*([12]\d{3}[年/-]\d{1,2}[月/-]\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",
        r"([12]\d{3}[年/-]\d{1,2}[月/-]\d{1,2}(?:日)?\s+\d{1,2}:\d{2}(?::\d{2})?)",
    ):
        for match in re.finditer(pattern, page_text):
            candidates.append(match.group(1))
    candidates.append(raw_html[:50000])
    if published_date := normalize_published_date(*candidates):
        return published_date
    return _extract_site_specific_published_date(soup, raw_html, url)


_SITE_DATE_CONTEXT_RE = re.compile(
    r"(?:發布|發佈|刊登|上架|更新|時間|日期|中央社|鉅亨網新聞中心|MoneyDJ|記者)"
    r"[\s\S]{0,80}?"
    r"((?:20\d{2}|19\d{2})[/-]\d{1,2}[/-]\d{1,2}"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",
    re.IGNORECASE,
)
_SITE_DATE_COMPACT_RE = re.compile(
    r"(?<!\d)((?:20\d{2}|19\d{2})[/-]\d{1,2}[/-]\d{1,2}"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)(?!\d)"
)
_URL_DATE_RE = re.compile(r"/((?:20\d{2}|19\d{2})[/-](?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01]))(?:/|$)")
_COMPACT_URL_DATE_RE = re.compile(r"(?<!\d)((?:20\d{2}|19\d{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))(?!\d)")
_SITE_DATE_DOMAINS = (
    "money.udn.com",
    "news.cnyes.com",
    "m.cnyes.com",
    "tw.stock.yahoo.com",
    "ctee.com.tw",
    "technews.tw",
    "moneydj.com",
    "moneyweekly.com.tw",
)


def _extract_site_specific_published_date(soup: bs4.BeautifulSoup, raw_html: str, url: str) -> str | None:
    """Best-effort date parser for common Taiwan finance news pages.

    This is intentionally scoped to publish-date extraction only. It does not
    change article classification, ranking, preferences, or Telegram rendering.
    """
    domain = (urlparse(url).netloc or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if not any(domain == item or domain.endswith(f".{item}") for item in _SITE_DATE_DOMAINS):
        return None

    text_candidates: list[str] = []
    for selector in (
        "time",
        "[class*=date]",
        "[class*=time]",
        "[class*=publish]",
        "[class*=article]",
        "[class*=story]",
        "[id*=date]",
        "[id*=time]",
    ):
        for tag in soup.select(selector):
            text = tag.get_text(" ", strip=True)
            if text:
                text_candidates.append(text[:500])
    page_text = soup.get_text(" ", strip=True)
    text_candidates.extend([page_text[:20000], raw_html[:50000]])

    for text in text_candidates:
        if match := _SITE_DATE_CONTEXT_RE.search(text):
            if published_date := normalize_published_date(match.group(1)):
                return published_date
    for text in text_candidates:
        if match := _SITE_DATE_COMPACT_RE.search(text):
            if published_date := normalize_published_date(match.group(1)):
                return published_date

    parsed_url = urlparse(url)
    url_text = f"{parsed_url.path} {parsed_url.query}"
    if match := _URL_DATE_RE.search(url_text):
        if published_date := normalize_published_date(match.group(1)):
            return published_date
    if match := _COMPACT_URL_DATE_RE.search(url_text):
        if published_date := normalize_published_date(match.group(1)):
            return published_date
    return None


def _merge_fetch_results(primary: WebFetchResult, fallback: WebFetchResult) -> WebFetchResult:
    return WebFetchResult(
        url=fallback.url or primary.url,
        title=fallback.title or primary.title,
        content=fallback.content or primary.content,
        published_date=fallback.published_date or primary.published_date,
        content_status=fallback.content_status,
        fetch_provider=fallback.fetch_provider,
        fetch_quality=fallback.fetch_quality,
        failure_reason=fallback.failure_reason,
    )


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
                    fetch_quality="low",
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
