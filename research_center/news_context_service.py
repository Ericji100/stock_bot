from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

from .date_aware_context import build_saved_news_context
from .models import CommandRequest, SourceItem
from .news_models import NewsItem, apply_news_signal_tags
from .news_repository import NewsRepository
from .news_source_filter import is_irrelevant_market_source

ProgressCallback = Callable[[str], None]

NEWS_CONTEXT_COMMANDS = {
    "research",
    "value_scan",
    "theme",
    "macro",
    "theme_radar",
    "theme_flow",
    "sector_strength",
}

SEARCH_PROVIDER_KEYS = {
    "gemini",
    "gemini_search",
    "google_search",
    "minimax",
    "minimax_mcp",
    "minimax_mcp_search",
    "tavily",
    "tavily_search",
    "web_fetch",
}


def attach_news_context(
    request: CommandRequest,
    structured_data: dict[str, Any],
    repository: NewsRepository | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Attach reusable news context from the local news database."""
    if request.command not in NEWS_CONTEXT_COMMANDS:
        return structured_data
    try:
        repo = repository or NewsRepository()
    except Exception as exc:
        context = {
            "status": "unavailable",
            "usable_count": 0,
            "items": [],
            "error": str(exc),
        }
        structured_data["news_context"] = {
            "status": "unavailable",
            "usable_count": 0,
            "minimum_expected": _minimum_news_items(request),
            "search_recommended": True,
            "items": [],
            "policy": {},
            "error": str(exc),
        }
        structured_data["saved_news_context"] = context
        if progress:
            progress(f"News context unavailable: {exc}")
        return structured_data
    context = build_saved_news_context(request, structured_data, repository=repo)
    usable_count = int(context.get("usable_count") or len(context.get("items") or []))
    minimum = _minimum_news_items(request)
    status = "sufficient" if usable_count >= minimum else "partial" if usable_count else "insufficient"
    news_context = {
        "status": status,
        "usable_count": usable_count,
        "minimum_expected": minimum,
        "search_recommended": usable_count < minimum,
        "items": context.get("items") or [],
        "policy": context.get("policy") or {},
    }
    structured_data["news_context"] = news_context
    structured_data["saved_news_context"] = context
    if progress:
        progress(f"News context attached: {usable_count}/{minimum}, status={status}")
    return structured_data


def persist_search_sources_to_news(
    request: CommandRequest,
    structured_data: dict[str, Any],
    sources: list[SourceItem],
    repository: NewsRepository | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Persist usable external search sources into the local news database."""
    if request.command not in NEWS_CONTEXT_COMMANDS:
        return {"enabled": False, "reason": "command does not use news context"}
    try:
        repo = repository or NewsRepository()
    except Exception as exc:
        status = {"enabled": True, "saved": 0, "skipped": 0, "candidate_count": 0, "status": "unavailable", "error": str(exc)}
        structured_data["news_persistence_status"] = status
        if progress:
            progress(f"News DB persistence unavailable: {exc}")
        return status
    items = [
        _source_to_news_item(request, structured_data, source)
        for source in sources
        if _is_persistable_search_source(source)
        and not is_irrelevant_market_source(source, request.command)
    ]
    items = [item for item in items if item is not None]
    if not items:
        status = {"enabled": True, "saved": 0, "skipped": 0, "candidate_count": 0}
        structured_data["news_persistence_status"] = status
        return status
    try:
        saved, skipped = repo.save_many(items)
    except Exception as exc:
        status = {"enabled": True, "saved": 0, "skipped": 0, "candidate_count": len(items), "status": "failed", "error": str(exc)}
        structured_data["news_persistence_status"] = status
        if progress:
            progress(f"News DB persistence failed: {exc}")
        return status
    status = {
        "enabled": True,
        "saved": saved,
        "skipped": skipped,
        "candidate_count": len(items),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    structured_data["news_persistence_status"] = status
    if progress and (saved or skipped):
        progress(f"News DB updated from search sources: saved={saved}, skipped={skipped}")
    attach_news_context(request, structured_data, repo, progress=None)
    return status


def build_news_status(target: str | None = None, days: int = 7, repository: NewsRepository | None = None) -> dict[str, Any]:
    repo = repository or NewsRepository()
    hours = max(1, int(days)) * 24
    target_text = str(target or "").strip()
    if target_text:
        if _looks_like_stock_code(target_text):
            items = repo.query_by_symbol(target_text, hours=hours)
        else:
            items = repo.query_by_topic(target_text, hours=hours)
    else:
        items = repo.query_all_recent(hours=hours)
    return {
        "target": target_text or "all",
        "days": days,
        "item_count": len(items),
        "items": [item.to_dict() for item in items[:20]],
    }


def format_news_status(status: dict[str, Any]) -> str:
    lines = [
        f"News status: {status.get('target')} ({status.get('days')}d)",
        f"- items: {status.get('item_count', 0)}",
    ]
    for item in status.get("items", [])[:8]:
        title = str(item.get("title") or "").strip()
        source = str(item.get("source") or "").strip()
        date = str(item.get("published_at") or item.get("created_at") or "").strip()
        lines.append(f"- {date} {source} {title}".strip())
    return "\n".join(lines)


def _minimum_news_items(request: CommandRequest) -> int:
    if request.command == "value_scan":
        return 12 if request.mode == "deep" else 6
    if request.command in {"theme", "theme_radar", "theme_flow", "sector_strength"}:
        return 8
    if request.command == "macro":
        return 8
    return 5 if request.mode == "deep" else 3


def _is_persistable_search_source(source: SourceItem) -> bool:
    if not source.url or not source.title:
        return False
    provider_tokens = {
        str(source.provider or "").lower(),
        str(source.provider_detail or "").lower(),
        str(source.fetch_provider or "").lower(),
        *[str(item).lower() for item in (source.found_by or [])],
    }
    if any(any(key in token for key in SEARCH_PROVIDER_KEYS) for token in provider_tokens):
        return True
    if source.snippet and source.published_date:
        return True
    return False


def _source_to_news_item(request: CommandRequest, structured_data: dict[str, Any], source: SourceItem) -> NewsItem | None:
    if not source.url or not source.title:
        return None
    parsed = urlparse(source.url)
    source_name = parsed.netloc or source.provider or "unknown"
    symbols, topics = _related_terms(request, structured_data)
    return apply_news_signal_tags(NewsItem(
        title=source.title[:300],
        url=source.url,
        source=source_name[:120],
        published_at=source.published_date or "",
        category=request.command,
        related_symbols=symbols,
        related_topics=topics,
        summary=source.snippet or source.title,
        full_text=source.snippet or "",
        importance_score=1,
        impact_direction="",
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    ))


def _related_terms(request: CommandRequest, structured_data: dict[str, Any]) -> tuple[list[str], list[str]]:
    symbols: set[str] = set()
    topics: set[str] = set()
    target = str(request.target or request.market_scope or request.theme_scope or "").strip()
    if _looks_like_stock_code(target):
        symbols.add(target)
    elif target:
        topics.add(target)
    stock = structured_data.get("stock") or {}
    if isinstance(stock, dict):
        code = str(stock.get("code") or stock.get("stock_id") or "").strip()
        name = str(stock.get("name") or stock.get("stock_name") or "").strip()
        if code:
            symbols.add(_normalize_code(code))
        if name:
            topics.add(name)
    for row in structured_data.get("ai_candidates") or []:
        if isinstance(row, dict):
            code = str(row.get("code") or row.get("stock_id") or "").strip()
            name = str(row.get("name") or row.get("stock_name") or "").strip()
            if code:
                symbols.add(_normalize_code(code))
            if name:
                topics.add(name)
    theme = structured_data.get("theme") or request.theme_scope
    if isinstance(theme, dict):
        for key in ("theme_name", "theme_id", "name"):
            value = str(theme.get(key) or "").strip()
            if value:
                topics.add(value)
    elif theme:
        topics.add(str(theme))
    return sorted(s for s in symbols if s), sorted(t for t in (_clean_topic(topic) for topic in topics) if t)


def _clean_topic(topic: Any) -> str:
    text = str(topic or "").strip()
    if not text:
        return ""
    if text.startswith("{") or text.startswith("[") or "':" in text or '":' in text:
        return ""
    if len(text) > 80:
        return ""
    return text


def _looks_like_stock_code(text: str) -> bool:
    return bool(re.fullmatch(r"\d{4,6}", str(text or "").strip()))


def _normalize_code(text: str) -> str:
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    return digits[:4] if len(digits) >= 4 else digits
