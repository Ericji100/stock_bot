from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import CommandRequest
from .report_quality_service import build_data_completeness_matrix, build_report_evidence_pack

DATA_GAP_SCHEMA_VERSION = "data_gap_v1"


def attach_data_gap_summary(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    structured_data["data_gap_summary"] = build_data_gap_summary(request, structured_data)
    return structured_data


def build_data_gap_summary(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    evidence_pack = build_report_evidence_pack(request, structured_data)
    matrix = build_data_completeness_matrix(request, structured_data, evidence_pack)
    missing = [str(row.get("field")) for row in matrix if not row.get("available")]
    covered = [str(row.get("field")) for row in matrix if row.get("available")]
    priority = _priority_gaps(request.command, missing, structured_data)
    coverage_score = _coverage_score(matrix)
    return {
        "schema_version": DATA_GAP_SCHEMA_VERSION,
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "coverage_score": coverage_score,
        "gap_count": len(missing),
        "covered_fields": covered,
        "missing_fields": missing,
        "priority_gaps": priority,
        "backfill_recommended": bool(priority),
    }


def _coverage_score(matrix: list[dict[str, Any]]) -> int:
    if not matrix:
        return 0
    covered = sum(1 for row in matrix if row.get("available"))
    return int(round(covered / len(matrix) * 100))


def _priority_gaps(command: str, missing: list[str], data: dict[str, Any]) -> list[dict[str, Any]]:
    priorities = {
        "research": {
            "financial_data": "high",
            "gross_margin_cache": "high",
            "revenue_data": "high",
            "institutional_data": "medium",
            "chip_summary": "medium",
            "company_knowledge": "medium",
            "news_context": "medium",
        },
        "value_scan": {
            "ai_candidate_evidence_pack": "high",
            "ai_candidates": "high",
            "local_ranking": "medium",
            "company_knowledge_update_status": "medium",
            "news_context": "medium",
        },
        "macro": {
            "quantitative_market": "high",
            "market_score": "medium",
            "volatility": "medium",
            "industry_flow": "medium",
            "fear_greed": "low",
            "news_context": "medium",
        },
        "theme": {
            "matched_companies": "high",
            "topic_context": "high",
            "supply_chain_profile": "medium",
            "company_knowledge_summary": "medium",
            "news_context": "medium",
        },
        "theme_radar": {
            "market_movers": "high",
            "theme_rankings": "high",
            "sector_strength": "high",
            "news_context": "medium",
            "feature_pack": "low",
            "data_coverage": "low",
        },
        "theme_flow": {
            "theme": "high",
            "layers": "high",
            "layer_market_validation": "high",
            "related_stocks": "medium",
            "supply_chain_profile": "medium",
            "company_knowledge_summary": "medium",
            "news_context": "medium",
        },
        "sector_strength": {
            "market_movers": "high",
            "sector_rankings": "high",
            "news_context": "medium",
            "feature_pack": "low",
            "data_coverage": "low",
        },
    }
    mapping = priorities.get(command, {})
    gaps: list[dict[str, Any]] = []
    for field in missing:
        if field in mapping:
            gaps.append({
                "field": field,
                "priority": mapping[field],
                "recommended_action": _recommended_action(field, data),
            })
    return sorted(gaps, key=lambda row: {"high": 0, "medium": 1, "low": 2}.get(str(row.get("priority")), 9))


def _recommended_action(field: str, data: dict[str, Any]) -> str:
    if field in {"financial_data", "gross_margin_cache", "revenue_data"}:
        return "backfill_financial_cache"
    if field in {"institutional_data", "chip_summary"}:
        return "backfill_chip_cache"
    if field in {"ai_candidate_evidence_pack", "ai_candidates", "local_ranking"}:
        return "rebuild_value_scan_candidate_pack"
    if field in {"matched_companies", "topic_context", "supply_chain_profile"}:
        return "refresh_topic_source_sync"
    if field in {"market_movers", "sector_rankings", "theme_rankings"}:
        return "refresh_market_movers_and_topic_context"
    if field in {"layers", "layer_market_validation", "related_stocks"}:
        return "refresh_theme_flow_context"
    if field == "news_context":
        news = data.get("news_context") or {}
        return "search_and_persist_news" if news.get("search_recommended", True) else "reuse_news_context"
    return "collect_missing_structured_data"
