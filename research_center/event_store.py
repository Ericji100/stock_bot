from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

from .models import CommandRequest, SourceItem


def build_source_events(request: CommandRequest, sources: list[SourceItem], structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    target = _target_for_request(request, structured_data)
    events: list[dict[str, Any]] = []
    for source in sources:
        published = source.published_date or (request.report_date.isoformat() if request.report_date else None)
        events.append(
            {
                "event_type": _event_type_from_source(source),
                "target": target,
                "title": source.title,
                "source_url": source.url,
                "source_level": source.source_level,
                "published_date": published,
                "payload": {
                    "source": asdict(source),
                    "request_command": request.command,
                    "report_mode": request.mode,
                },
            }
        )
    return events


def extract_structured_events(structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for event in structured_data.get("source_events") or []:
        if isinstance(event, dict):
            events.append(event)
    for candidate in structured_data.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        for event in candidate.get("source_events") or []:
            if isinstance(event, dict):
                events.append(event)
    return events

def historical_policy(request: CommandRequest, dropped_sources: list[str] | None = None) -> dict[str, Any]:
    if request.report_date is None:
        return {
            "mode": "current",
            "status": "live_data_allowed",
            "notes": ["未指定 --date，允許使用目前可取得的最新資料。"],
        }
    notes = [
        "指定 --date 時，結構化資料會切到報告日期以前。",
        "外部來源若發布日期晚於報告日，或沒有可判定日期，會被保守排除或降權。",
        "事件資料庫會保留已看過的來源，讓未來歷史報告可逐步提高完整度。",
    ]
    if dropped_sources:
        notes.append(f"本次排除 {len(dropped_sources)} 筆不符合日期治理的來源。")
    return {"mode": "historical", "status": "guarded", "report_date": request.report_date.isoformat(), "notes": notes, "dropped_sources": dropped_sources or []}


def _target_for_request(request: CommandRequest, structured_data: dict[str, Any]) -> str:
    if request.command == "research":
        stock = structured_data.get("stock") or {}
        return str(stock.get("code") or request.target or "unknown")
    return str(request.target or request.theme_scope or request.market_scope or request.candidate_pool or request.command)


def _event_type_from_source(source: SourceItem) -> str:
    url = source.url.lower()
    if "mops.twse.com.tw" in url:
        return "mops"
    if "twse.com.tw" in url or "tpex.org.tw" in url:
        return "market_data"
    if "ptt.cc" in url or "dcard.tw" in url or "mobile01.com" in url:
        return "forum"
    if "broker" in url or "research" in url:
        return "broker_report"
    return "source"
