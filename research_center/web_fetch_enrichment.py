"""Shared WebFetch enrichment helper — extracted to avoid circular imports."""
from __future__ import annotations

import os
from dataclasses import replace
from typing import Any, Callable
from urllib.parse import urlparse

from .models import CommandRequest, SourceItem
from .source_rank import sort_sources_by_preferred_weight
from .web_fetch_service import WebFetchService

ProgressCallback = Callable[[str], None]


def _enrich_sources_with_web_fetch(
    request: CommandRequest,
    sources: list[SourceItem],
    structured_data: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> None:
    """Best-effort page-content enrichment; failures must never block AI analysis."""
    if not sources:
        return

    if request.command == "topic_maintain":
        max_urls = 30 if request.mode == "deep" else 12
    elif request.command == "news":
        max_urls = 12
        try:
            env_max_urls = int(os.environ.get("NEWS_REFRESH_WEBFETCH_MAX_URLS", "0") or "0")
        except ValueError:
            env_max_urls = 0
        if env_max_urls > 0:
            max_urls = min(max_urls, env_max_urls)
    else:
        max_urls = 8 if request.mode == "deep" else 4

    candidate_sources = sort_sources_by_preferred_weight(sources)
    candidates: list[SourceItem] = []
    seen: set[str] = set()
    for source in candidate_sources:
        url = (source.url or "").strip()
        if not url or url in seen:
            continue
        lower = url.lower()
        if not lower.startswith(("http://", "https://")):
            continue
        if any(lower.endswith(ext) for ext in (".pdf", ".xls", ".xlsx", ".csv", ".zip")):
            continue
        if request.command == "news" and _looks_like_non_article_fetch_url(lower):
            continue
        candidates.append(source)
        seen.add(url)
    selected = sorted(candidates, key=_web_fetch_source_priority, reverse=True)[:max_urls]

    if not selected:
        structured_data["web_fetch_diagnostics"] = {
            "enabled": True,
            "status": "skipped",
            "reason": "no_fetchable_urls",
            "total_urls": 0,
        }
        return

    try:
        if progress:
            progress(f"WebFetch start: selected={len(selected)}")
        service = WebFetchService(timeout=12.0, max_workers=3)
        result = service.fetch_many(
            [item.url for item in selected],
            progress=progress,
            expected_terms=_web_fetch_expected_terms(request, structured_data),
        )
        by_url = {item.url: item for item in result.results}
        enriched_sources: list[SourceItem] = []
        enriched_count = 0
        for source in sources:
            fetched = by_url.get(source.url)
            if not fetched:
                enriched_sources.append(source)
                continue
            if fetched.content:
                enriched_count += 1
                snippet = fetched.content[:2000]
            else:
                snippet = source.snippet
            enriched_sources.append(
                replace(
                    source,
                    title=fetched.title or source.title,
                    published_date=fetched.published_date or source.published_date,
                    snippet=snippet,
                    fetch_provider=fetched.fetch_provider,
                    fetch_status=fetched.content_status,
                    fetch_quality=fetched.fetch_quality,
                    failure_reason=fetched.failure_reason,
                )
            )
        sources[:] = enriched_sources
        success_ratio = round(enriched_count / len(selected), 4) if selected else 0
        structured_data["web_fetch_diagnostics"] = {
            **result.diagnostics,
            "enabled": True,
            "status": "completed",
            "selected_url_count": len(selected),
            "enriched_source_count": enriched_count,
            "success_ratio": success_ratio,
            "quality_status": "weak" if success_ratio < 0.5 else "ok",
            "selected_urls": [item.url for item in selected],
        }
        structured_data["web_fetched_sources"] = [
            {
                "url": item.url,
                "title": item.title,
                "content_status": item.content_status,
                "fetch_provider": item.fetch_provider,
                "fetch_quality": item.fetch_quality,
                "failure_reason": item.failure_reason,
                "published_date": item.published_date,
                "content_preview": item.content[:1200],
            }
            for item in result.results
        ]
        if progress:
            progress(f"WebFetch completed: enriched={enriched_count}/{len(selected)}")
    except Exception as exc:
        structured_data["web_fetch_diagnostics"] = {
            "enabled": True,
            "status": "failed",
            "error": str(exc),
            "selected_url_count": len(selected),
        }
        if progress:
            progress(f"WebFetch failed; continuing AI analysis: {exc}")


_SOCIAL_OR_VIDEO_DOMAINS = (
    "ptt.cc",
    "dcard.tw",
    "mobile01.com",
    "cmoney.tw",
    "social.cmoney.tw",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "threads.com",
    "threads.net",
    "x.com",
    "twitter.com",
)


def _web_fetch_source_priority(source: SourceItem) -> int:
    url = source.url or ""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    title = (source.title or "").lower()
    provider = source.provider or ""
    level = source.source_level or ""
    score = 0
    if level in {"Level 1", "L1_official"}:
        score += 20
    if level in {"Level 2", "L2_media", "L2_industry"}:
        score += 35
    if provider in {"tavily_extract", "html_fetch", "requests_bs4"}:
        score += 18
    if provider in {"minimax_mcp_search", "gemini_search", "gemini_grounding"}:
        score += 8
    article_markers = (
        "/news/", "/money/story/", "/article/", "/articles/", "/story/", "/post/",
        "/industry/", "/richclub/", "/view/", "/news/id/", ".html", ".htm",
    )
    if any(marker in path for marker in article_markers):
        score += 30
    if any(pattern in url.lower() for pattern in _NEWS_FETCH_PREFERRED_PATTERNS):
        score += 18
    if any(domain in host for domain in ("money.udn.com", "news.cnyes.com", "ctee.com.tw", "moneydj.com", "technews.tw", "tw.stock.yahoo.com")):
        score += 12
    weak_markers = (
        "showbuysalechart", "qfii", "indicesreport", "announcement/list", "comparison",
        "market_comparison", "zbd", "calendar", "rank", "search", "query",
    )
    if any(marker in path for marker in weak_markers) or any(pattern in url.lower() for pattern in _NEWS_FETCH_DEMOTE_PATTERNS):
        score -= 25
    if path in {"", "/"}:
        score -= 35
    if any(domain in host for domain in _SOCIAL_OR_VIDEO_DOMAINS):
        score -= 30
    if title in {host, host.replace("www.", "")}:
        score -= 20
    return score


def _web_fetch_expected_terms(request: CommandRequest, structured_data: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for value in (request.target, request.market_scope, request.theme_scope, request.candidate_pool):
        if value:
            terms.extend(str(value).split())
    if request.command == "macro":
        terms.extend(["台股", "加權", "櫃買", "VIX", "台指期", "外資"])
    elif request.command == "research":
        stock_name = ((structured_data.get("stock") or {}).get("name") if isinstance(structured_data.get("stock"), dict) else None)
        stock_code = ((structured_data.get("stock") or {}).get("code") if isinstance(structured_data.get("stock"), dict) else None)
        terms.extend([str(stock_code or ""), str(stock_name or "")])
    elif request.command in {"theme", "theme_flow", "theme_radar", "sector_strength"}:
        terms.extend(["台股", "題材", "產業", "族群", "供應鏈"])
    elif request.command == "value_scan":
        terms.extend(["台股", "營收", "財報", "重估", "法人"])
    elif request.command == "news":
        terms.extend(["台股", "市場", "法人", "題材"])
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        value = str(term or "").strip()
        if len(value) < 2:
            continue
        key = value.lower()
        if key not in seen:
            deduped.append(value)
            seen.add(key)
    return deduped[:20]


_NEWS_FETCH_PREFERRED_PATTERNS = (
    "money.udn.com/money/story/",
    "news.cnyes.com/news/id/",
    "m.cnyes.com/news/id/",
    "ctee.com.tw/news/",
    "tw.stock.yahoo.com/news/",
    "technews.tw/",
    "finance.technews.tw/",
    "moneydj.com/",
    "moneyweekly.com.tw/",
    "cna.com.tw/news/",
)

_NEWS_FETCH_DEMOTE_PATTERNS = (
    "twse.com.tw/zh/trading/",
    "twse.com.tw/en/",
    "tpex.org.tw/en-us/",
    "tpex.org.tw/zh-tw/mainboard/trading/",
    "institutional-trading",
    "/etf/",
    "/statistics/",
    "/announcement/list",
    "/company/event/",
)


def _rank_news_fetch_candidates(sources: list[SourceItem]) -> list[SourceItem]:
    def key(source: SourceItem) -> tuple[int, str]:
        url = (source.url or "").lower()
        if any(pattern in url for pattern in _NEWS_FETCH_PREFERRED_PATTERNS):
            return (0, url)
        if any(pattern in url for pattern in _NEWS_FETCH_DEMOTE_PATTERNS):
            return (2, url)
        return (1, url)

    return sorted(sources, key=key)


def _looks_like_non_article_fetch_url(lower_url: str) -> bool:
    parsed = urlparse(lower_url)
    path = parsed.path.rstrip("/")
    if not path or path == "/":
        return True
    if any(part in path for part in ("/query", "/search", "/list", "/statistics", "/trading/", "/major-institutional/", "/announcement/list", "institutional-trading", "a_qfii", "showbuysalechart", "tw-rank", "afterhours", "/stock/institutional-investors/")):
        return True
    if lower_url.endswith(("/news", "/news/", "/index.html")):
        return True
    return False
