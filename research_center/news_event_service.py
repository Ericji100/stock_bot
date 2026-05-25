from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import CommandRequest

NEWS_EVENT_SCHEMA_VERSION = "news_event_v1"


def attach_news_events(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    structured_data["news_events"] = build_news_events_from_context(request, structured_data)
    structured_data["news_event_summary"] = {
        "schema_version": NEWS_EVENT_SCHEMA_VERSION,
        "event_count": len(structured_data["news_events"]),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    return structured_data


def build_news_events_from_context(request: CommandRequest, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    context = structured_data.get("news_context") or structured_data.get("saved_news_context") or {}
    items = context.get("items") or []
    events: list[dict[str, Any]] = []
    for index, item in enumerate(items[:30], 1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        events.append({
            "event_type": _event_type(title, item),
            "target": _target(request, structured_data),
            "title": title[:300],
            "source_url": item.get("url") or item.get("source_url") or "",
            "source_level": item.get("source_level") or item.get("level") or "",
            "published_date": item.get("published_at") or item.get("date") or item.get("created_at") or "",
            "payload": {
                "schema_version": NEWS_EVENT_SCHEMA_VERSION,
                "index": index,
                "category": item.get("category") or request.command,
                "source": item.get("source") or "",
                "related_symbols": item.get("related_symbols") or [],
                "related_topics": item.get("related_topics") or [],
                "summary": item.get("summary") or item.get("snippet") or "",
            },
        })
    return events


def _target(request: CommandRequest, data: dict[str, Any]) -> str:
    stock = data.get("stock") or {}
    if isinstance(stock, dict) and stock.get("code"):
        return str(stock.get("code"))
    return str(request.target or request.market_scope or request.theme_scope or request.candidate_pool or request.command)


def _event_type(title: str, item: dict[str, Any]) -> str:
    text = " ".join(str(part or "") for part in [title, item.get("summary"), item.get("snippet")]).lower()
    if any(term in text for term in ("revenue", "營收", "eps", "毛利", "財報")):
        return "news_financial"
    if any(term in text for term in ("risk", "風險", "砍單", "庫存", "衰退")):
        return "news_risk"
    if any(term in text for term in ("供應鏈", "客戶", "產品", "訂單")):
        return "news_supply_chain"
    if any(term in text for term in ("fed", "利率", "匯率", "油價", "關稅")):
        return "news_macro"
    return "news"
