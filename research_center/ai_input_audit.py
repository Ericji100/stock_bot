from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import CommandRequest


AUDIT_SCHEMA_VERSION = "ai_input_audit_v1"


def build_ai_input_audit(
    request: CommandRequest,
    *,
    source_selection: dict[str, Any],
    structured_data: dict[str, Any],
    ai_prompt_context: dict[str, Any],
) -> dict[str, Any]:
    selected_sources = source_selection.get("selected_sources") or []
    omitted_sources = source_selection.get("omitted_sources") or []
    structured_summary = _structured_coverage(request, structured_data)
    context_summary = _context_size_summary(ai_prompt_context)
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "ai_received": {
            "selected_source_count": len(selected_sources),
            "structured_sections": structured_summary["available_sections"],
            "prompt_context_chars": context_summary["prompt_context_chars"],
        },
        "ai_not_received_directly": {
            "omitted_source_count": len(omitted_sources),
            "omitted_reason_counts": _reason_counts(omitted_sources),
            "note": "未直接入模的資料仍保存在完整報告 JSON 或來源檔。",
        },
        "source_coverage": source_selection.get("coverage") or {},
        "all_source_coverage": source_selection.get("all_source_coverage") or {},
        "structured_coverage": structured_summary,
        "context_size": context_summary,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _structured_coverage(request: CommandRequest, data: dict[str, Any]) -> dict[str, Any]:
    fields = _fields_for_command(request.command)
    available = []
    missing = []
    counts: dict[str, int] = {}
    for field in fields:
        value = data.get(field)
        if _has_value(value):
            available.append(field)
            counts[field] = _value_count(value)
        else:
            missing.append(field)
            counts[field] = 0
    return {
        "required_sections": fields,
        "available_sections": available,
        "missing_sections": missing,
        "section_counts": counts,
        "coverage_ratio": round(len(available) / len(fields), 4) if fields else 1,
    }


def _context_size_summary(value: Any) -> dict[str, int]:
    import json

    text = json.dumps(value, ensure_ascii=False, default=str)
    return {
        "prompt_context_chars": len(text),
        "prompt_context_bytes": len(text.encode("utf-8")),
    }


def _reason_counts(omitted_sources: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in omitted_sources:
        reason = str(item.get("status") or item.get("reason") or "未入模")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _fields_for_command(command: str) -> list[str]:
    if command == "research":
        return [
            "stock",
            "price_data",
            "institutional_data",
            "margin_data",
            "revenue_data",
            "financial_data",
            "local_rerating_snapshot",
            "local_scoring",
            "topic_context",
            "unified_evidence_pack",
        ]
    if command == "value_scan":
        return [
            "ai_candidates",
            "ai_candidate_evidence_pack",
            "local_ranking",
            "local_scoring",
            "topic_context",
            "unified_evidence_pack",
        ]
    if command == "macro":
        return [
            "quantitative_market",
            "volatility",
            "industry_flow",
            "fear_greed",
            "market_score",
            "unified_evidence_pack",
        ]
    if command in {"theme", "theme_radar", "theme_flow", "sector_strength"}:
        return [
            "theme",
            "matched_companies",
            "topic_context",
            "supply_chain_profile",
            "theme_rankings",
            "sector_rankings",
            "data_quality",
            "unified_evidence_pack",
        ]
    if command == "radar":
        return ["candidates", "evidence_pack", "data_coverage"]
    if command == "news":
        return ["news_batch", "news_context", "sources"]
    if command == "topic_maintain":
        return ["existing_profiles", "source_candidates", "topic_context"]
    return ["unified_evidence_pack", "feature_pack", "data_coverage"]


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, (list, tuple, set, str)):
        return bool(value)
    return True


def _value_count(value: Any) -> int:
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if value is None:
        return 0
    return 1
