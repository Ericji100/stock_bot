from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from .models import CommandRequest, SourceItem


def target_for_snapshots(request: CommandRequest, structured_data: dict[str, Any]) -> str:
    if request.command == "research":
        stock = structured_data.get("stock") or {}
        return str(stock.get("code") or request.target or "unknown")
    return str(request.target or request.theme_scope or request.market_scope or request.candidate_pool or request.command)


def build_source_snapshots(request: CommandRequest, sources: list[SourceItem], structured_data: dict[str, Any], gemini_raw: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    target = target_for_snapshots(request, structured_data)
    report_date = request.report_date.isoformat() if request.report_date else None
    snapshots: list[dict[str, Any]] = []
    for source in sources:
        snapshots.append(
            {
                "target": target,
                "command": request.command,
                "source_url": source.url,
                "title": source.title,
                "source_level": source.source_level,
                "published_date": source.published_date,
                "fetched_at": now,
                "report_date": report_date,
                "content_type": "source_item",
                "content": {"source": asdict(source), "request": request.raw_text},
            }
        )
    if gemini_raw:
        snapshots.append(
            {
                "target": target,
                "command": request.command,
                "source_url": f"gemini://{request.command}/{now}",
                "title": "Gemini raw grounding metadata",
                "source_level": "Level 3",
                "published_date": report_date,
                "fetched_at": now,
                "report_date": report_date,
                "content_type": "gemini_raw",
                "content": _compact_gemini_raw(gemini_raw),
            }
        )
    return snapshots


def snapshots_to_structured_context(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "historical_snapshots_loaded" if snapshots else "no_historical_snapshots",
        "snapshot_count": len(snapshots),
        "policy": "歷史日期模式只允許 Gemini 整理已保存 snapshot，不直接使用現在網路搜尋。",
        "items": [
            {
                "title": item.get("title"),
                "source_url": item.get("source_url"),
                "source_level": item.get("source_level"),
                "published_date": item.get("published_date"),
                "fetched_at": item.get("fetched_at"),
                "content_type": item.get("content_type"),
                "content": item.get("content"),
            }
            for item in snapshots[:80]
        ],
    }


def _compact_gemini_raw(raw: dict[str, Any]) -> dict[str, Any]:
    candidates = raw.get("candidates") or []
    if not candidates:
        return {"status": "empty"}
    metadata = candidates[0].get("groundingMetadata") or candidates[0].get("grounding_metadata") or {}
    return {
        "groundingMetadata": metadata,
        "usageMetadata": raw.get("usageMetadata") or raw.get("usage_metadata"),
    }
