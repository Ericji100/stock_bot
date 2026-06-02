from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from .ai_context_policy import compact_source_for_prompt, select_sources_for_ai_input
from .ai_input_audit import build_ai_input_audit
from .evidence_pack_service import build_ai_compact_context, build_three_layer_evidence_context
from .models import CommandRequest, SourceItem
from .report_confidence_service import build_report_confidence


AI_DATA_CENTER_SCHEMA_VERSION = "ai_data_center_v1"


def attach_ai_data_center(
    request: CommandRequest,
    structured_data: dict[str, Any],
    sources: list[SourceItem],
) -> dict[str, Any]:
    bundle = build_ai_data_center_bundle(request, structured_data, sources)
    structured_data["ai_data_center"] = bundle
    structured_data["ai_input_audit"] = bundle["ai_input_audit"]
    structured_data["report_confidence"] = bundle["report_confidence"]
    structured_data["ai_prompt_context"] = bundle["ai_prompt_context"]
    return structured_data


def build_ai_data_center_bundle(
    request: CommandRequest,
    structured_data: dict[str, Any],
    sources: list[SourceItem],
) -> dict[str, Any]:
    source_selection = select_sources_for_ai_input(request, sources)
    raw_sources = [asdict(item) for item in sources]
    ai_prompt_context = build_ai_prompt_context(request, structured_data, source_selection)
    audit = build_ai_input_audit(
        request,
        source_selection=source_selection,
        structured_data=structured_data,
        ai_prompt_context=ai_prompt_context,
    )
    confidence = build_report_confidence(request, ai_input_audit=audit)
    three_layer = build_three_layer_evidence_context(
        raw_sources=raw_sources,
        evidence_pack=structured_data.get("unified_evidence_pack") or {},
        final_context=ai_prompt_context,
        min_source_count=int((source_selection.get("policy") or {}).get("min_prompt_sources") or 0),
    )
    return {
        "schema_version": AI_DATA_CENTER_SCHEMA_VERSION,
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy": source_selection.get("policy"),
        "source_selection": source_selection,
        "ai_prompt_context": ai_prompt_context,
        "ai_input_audit": audit,
        "report_confidence": confidence,
        "three_layer_context": three_layer,
        "full_data_policy": "完整資料保存在 report JSON、sources JSON 與本地 structured_data；AI prompt 使用規則化入模資料。",
    }


def build_ai_prompt_context(
    request: CommandRequest,
    structured_data: dict[str, Any],
    source_selection: dict[str, Any],
) -> dict[str, Any]:
    selected_sources = [
        compact_source_for_prompt(item)
        for item in (source_selection.get("selected_sources") or [])
    ]
    structured_pack = _structured_prompt_summary(request, structured_data)
    compact_structured = build_ai_compact_context(
        structured_pack,
        max_sources=12,
        max_list=_list_limit_for_command(request),
        max_keys=90,
        max_string=_string_limit_for_command(request),
        depth=5,
    )
    return {
        "schema_version": "ai_prompt_context_v1",
        "說明": "這是 AI 實際入模資料；完整資料仍保存在本地報告與 JSON。",
        "分析指令": request.command,
        "分析模式": request.mode,
        "分析目標": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "入模來源": selected_sources,
        "結構化資料摘要": compact_structured,
        "資料覆蓋": structured_data.get("data_coverage"),
        "資料缺口": structured_data.get("data_gap_summary"),
        "共用證據包摘要": _compact_evidence_pack(structured_data.get("unified_evidence_pack")),
    }


def _structured_prompt_summary(request: CommandRequest, data: dict[str, Any]) -> dict[str, Any]:
    if request.command == "research":
        return {
            "stock": data.get("stock"),
            "price_data": data.get("price_data"),
            "technical_data": data.get("technical_data"),
            "institutional_data": data.get("institutional_data"),
            "margin_data": data.get("margin_data"),
            "revenue_data": data.get("revenue_data"),
            "financial_data": data.get("financial_data"),
            "tdcc_data": data.get("tdcc_data"),
            "valuation_data": data.get("valuation_data"),
            "local_scoring": data.get("local_scoring"),
            "local_rerating_snapshot": data.get("local_rerating_snapshot"),
            "topic_context": data.get("topic_context"),
        }
    if request.command == "value_scan":
        return {
            "candidate_pool": data.get("candidate_pool"),
            "ai_candidates": data.get("ai_candidates"),
            "ai_candidate_evidence_pack": data.get("ai_candidate_evidence_pack"),
            "local_ranking": data.get("local_ranking"),
            "local_scoring": data.get("local_scoring"),
            "topic_context": data.get("topic_context"),
        }
    if request.command == "macro":
        return {
            "market_scope": data.get("market_scope"),
            "region_scope": data.get("region_scope"),
            "quantitative_market": data.get("quantitative_market"),
            "volatility": data.get("volatility"),
            "industry_flow": data.get("industry_flow"),
            "fear_greed": data.get("fear_greed"),
            "market_score": data.get("market_score"),
        }
    if request.command in {"theme", "theme_radar", "theme_flow", "sector_strength"}:
        return {
            "theme": data.get("theme") or request.theme_scope or request.target,
            "matched_companies": data.get("matched_companies") or data.get("matched_universe"),
            "supply_chain_profile": data.get("supply_chain_profile"),
            "topic_context": data.get("topic_context"),
            "theme_rankings": data.get("theme_rankings"),
            "sector_rankings": data.get("sector_rankings"),
            "subsector_rankings": data.get("subsector_rankings"),
            "data_quality": data.get("data_quality"),
        }
    return {
        "feature_pack": data.get("feature_pack"),
        "news_context": data.get("news_context"),
        "data_coverage": data.get("data_coverage"),
        "unified_evidence_pack": data.get("unified_evidence_pack"),
    }


def _compact_evidence_pack(value: Any) -> Any:
    return build_ai_compact_context(value, max_sources=8, max_list=12, max_keys=60, max_string=220, depth=4)


def _list_limit_for_command(request: CommandRequest) -> int:
    if request.command == "value_scan":
        return 35 if request.mode == "deep" else 24
    if request.command in {"theme", "theme_radar", "theme_flow", "sector_strength"}:
        return 40 if request.mode == "deep" else 28
    if request.command == "research":
        return 30 if request.mode == "deep" else 20
    return 24


def _string_limit_for_command(request: CommandRequest) -> int:
    if request.mode == "deep":
        return 520
    return 360
