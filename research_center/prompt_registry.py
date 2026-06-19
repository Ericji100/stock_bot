from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROOT_DIR
from .models import CommandRequest, SourceItem
from .preferred_sources import build_site_queries
from .search_query_service import build_search_discovery_tasks

PROMPT_ROOT = ROOT_DIR / "prompt"
PROMPT_BASE_DIR = PROMPT_ROOT / "base"
PROMPT_REPORT_DIR = PROMPT_ROOT / "report"
PROMPT_DISCOVERY_DIR = PROMPT_ROOT / "discovery"
PROMPT_SCORING_DIR = PROMPT_ROOT / "scoring"
PROMPT_RULES_DIR = PROMPT_ROOT / "rules"
SCORING_RULES_CHAR_LIMIT = 36000

TEMPLATE_MAP = {
    ("research", "normal"): "research_summary.md",
    ("research", "score"): "research_score.md",
    ("research", "deep"): "research_deep.md",
    ("macro", "normal"): "macro.md",
    ("macro", "brief"): "macro.md",
    ("macro", "deep"): "macro.md",
    ("theme", "normal"): "theme.md",
    ("theme", "deep"): "theme_deep.md",
    ("theme_radar", "normal"): "theme_radar.md",
    ("theme_radar", "deep"): "theme_radar.md",
    ("theme_flow", "normal"): "theme_flow.md",
    ("theme_flow", "deep"): "theme_flow.md",
    ("sector_strength", "normal"): "sector_strength.md",
    ("sector_strength", "deep"): "sector_strength.md",
    ("value_scan", "normal"): "value_scan.md",
    ("value_scan", "deep"): "value_scan.md",
    ("source_only", "source_only"): "source_only_summary.md",
}


def _prompt_structured_data(request: CommandRequest, structured_data: dict[str, Any]) -> str:
    """??????????????????structured prompt pack???[:22000] ?????????????

    - value_scan: ????? ai_candidate_evidence_pack?????????
    - research: ????? research ??pack????local_rerating_snapshot / local_scoring
    - macro: ????? macro ??pack????quantitative_market / fear_greed ??
    - theme: ????? theme ??pack????matched_universe / matched_companies
    """
    high_model_package = structured_data.get("high_model_input_package")
    if isinstance(high_model_package, dict):
        return _json({
            "high_model_input_package": high_model_package,
            "ai_workflow_policy": structured_data.get("ai_workflow_policy"),
            "high_model_input_mode": structured_data.get("high_model_input_mode"),
            "low_model_validation": structured_data.get("low_model_validation"),
            "analysis_model": structured_data.get("analysis_model"),
            "analysis_model_choice": structured_data.get("analysis_model_choice"),
            "\u9ad8\u968e\u6a21\u578b\u5165\u6a21\u63d0\u9192": [
                "\u4f60\u6536\u5230\u7684\u662f\u672c\u5730\u8cc7\u6599\u4e2d\u5fc3\u6574\u7406\u5f8c\u7684\u9ad8\u968e\u6a21\u578b\u8cc7\u6599\u5305\uff0c\u4e0d\u662f\u5b8c\u6574\u539f\u59cb\u8cc7\u6599\uff1b\u5b8c\u6574\u4f86\u6e90\u4ecd\u4fdd\u5b58\u5728\u5831\u544a JSON \u8207 HTML \u9644\u9304\u3002",
                "MiniMax M3 \u7684\u8cc7\u6599\u6574\u7406\u53ea\u80fd\u4f5c\u70ba\u5e95\u7a3f\uff0c\u4e0d\u80fd\u76f4\u63a5\u8996\u70ba\u6700\u7d42\u6295\u7814\u7d50\u8ad6\u3002",
                "\u6700\u7d42\u5224\u65b7\u5fc5\u9808\u91cd\u65b0\u6aa2\u67e5\u4f86\u6e90\u3001\u53cd\u8b49\u3001\u8cc7\u6599\u7f3a\u53e3\u3001\u786c\u6578\u64da\u8207\u53ef\u4fe1\u5ea6\u3002",
                "\u82e5\u8cc7\u6599\u5305\u7f3a\u5c11\u95dc\u9375\u8cc7\u8a0a\uff0c\u5fc5\u9808\u660e\u78ba\u6a19\u793a\u8cc7\u6599\u4e0d\u8db3\uff0c\u4e0d\u5f97\u81ea\u884c\u88dc\u5beb\u6216\u8a87\u5927\u3002",
            ],
        })

    if request.command == "value_scan":
        if "ai_candidate_evidence_pack" in structured_data:
            pack = structured_data["ai_candidate_evidence_pack"]
            return _json({
                "ai_candidate_evidence_pack": pack,
                "candidate_pool": structured_data.get("candidate_pool"),
                "report_date": structured_data.get("report_date"),
                "total_candidate_count": structured_data.get("total_candidate_count"),
                "ai_candidate_limit": structured_data.get("ai_candidate_limit"),
                "scoring_rules": structured_data.get("scoring_rules"),
                "topic_context": structured_data.get("topic_context"),
                "company_knowledge_update_status": structured_data.get("company_knowledge_update_status"),
                **_date_context_prompt_fields(structured_data),
            })
        return _json(structured_data)

    if request.command == "research":
        return _json(_research_structured_prompt_data(structured_data))

    if request.command == "macro":
        return _json(_macro_structured_prompt_data(structured_data))

    if request.command == "theme":
        return _json(_theme_structured_prompt_data(structured_data))

    if request.command == "theme_radar":
        return _json(_theme_radar_structured_prompt_data(structured_data))

    if request.command == "theme_flow":
        return _json(_theme_flow_structured_prompt_data(structured_data))

    if request.command == "sector_strength":
        return _json(_sector_strength_structured_prompt_data(structured_data))

    return _json(structured_data)


def _date_context_prompt_fields(structured_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "analysis_date": structured_data.get("analysis_date"),
        "date_window_policy": structured_data.get("date_window_policy"),
        "saved_news_context": structured_data.get("saved_news_context"),
        "news_context": structured_data.get("news_context"),
        "news_persistence_status": structured_data.get("news_persistence_status"),
        "feature_pack": structured_data.get("feature_pack"),
        "data_coverage": structured_data.get("data_coverage"),
        "date_aware_context": structured_data.get("date_aware_context"),
        "ai_prompt_context": structured_data.get("ai_prompt_context"),
        "ai_input_audit": structured_data.get("ai_input_audit"),
        "report_confidence": structured_data.get("report_confidence"),
        "low_model_digest": structured_data.get("low_model_digest"),
        "low_model_model": structured_data.get("low_model_model"),
        "low_model_prompt_path": structured_data.get("low_model_prompt_path"),
        "high_model_input_package": structured_data.get("high_model_input_package"),
        "high_model_input_mode": structured_data.get("high_model_input_mode"),
        "ai_workflow_policy": structured_data.get("ai_workflow_policy"),
        "low_model_validation": structured_data.get("low_model_validation"),
    }


def _research_structured_prompt_data(structured_data: dict[str, Any]) -> dict[str, Any]:
    """Return research structured prompt data."""
    chip_data = structured_data.get("chip_backup_data") or {}
    chip_summary = chip_data.get("summary") if isinstance(chip_data, dict) else None
    return {
        "stock": structured_data.get("stock"),
        "report_date": structured_data.get("report_date"),
        "technical_data": structured_data.get("technical_data"),
        "price_data": structured_data.get("price_data"),
        "institutional_data": structured_data.get("institutional_data"),
        "margin_data": structured_data.get("margin_data"),
        "revenue_data": structured_data.get("revenue_data"),
        "financial_data": structured_data.get("financial_data"),
        "strategy_summary": structured_data.get("strategy_summary"),
        "valuation_data": structured_data.get("valuation_data"),
        "tdcc_data": structured_data.get("tdcc_data"),
        "gross_margin_cache": structured_data.get("gross_margin_cache"),
        "chip_backup_summary": chip_summary,
        "mops_documents": structured_data.get("mops_documents"),
        "source_events": structured_data.get("source_events"),
        "company_knowledge": structured_data.get("company_knowledge"),
        "company_knowledge_update_status": structured_data.get("company_knowledge_update_status"),
        "local_rerating_snapshot": structured_data.get("local_rerating_snapshot"),
        "local_scoring": structured_data.get("local_scoring"),
        "historical_data_policy": structured_data.get("historical_data_policy"),
        "historical_snapshots": structured_data.get("historical_snapshots"),
        "topic_context": structured_data.get("topic_context"),
        **_date_context_prompt_fields(structured_data),
    }


def _macro_structured_prompt_data(structured_data: dict[str, Any]) -> dict[str, Any]:
    """Return macro structured prompt data."""
    return {
        "market_scope": structured_data.get("market_scope"),
        "theme_scope": structured_data.get("theme_scope"),
        "region_scope": structured_data.get("region_scope"),
        "report_date": structured_data.get("report_date"),
        "noon_market_report": structured_data.get("noon_market_report"),
        "morning_market_report": structured_data.get("morning_market_report"),
        "quantitative_market": structured_data.get("quantitative_market"),
        "volatility": structured_data.get("volatility"),
        "industry_flow": structured_data.get("industry_flow"),
        "fear_greed": structured_data.get("fear_greed"),
        "market_score": structured_data.get("market_score"),
        "macro_data_guard": structured_data.get("macro_data_guard"),
        "industry_index_data": structured_data.get("industry_index_data"),
        "free_public_sources": structured_data.get("free_public_sources"),
        "local_scoring": structured_data.get("local_scoring"),
        "historical_data_policy": structured_data.get("historical_data_policy"),
        **_date_context_prompt_fields(structured_data),
    }


def _theme_structured_prompt_data(structured_data: dict[str, Any]) -> dict[str, Any]:
    """Return theme structured prompt data."""
    return {
        "theme": structured_data.get("theme"),
        "report_date": structured_data.get("report_date"),
        "supply_chain_profile": structured_data.get("supply_chain_profile"),
        "company_knowledge_summary": structured_data.get("company_knowledge_summary"),
        "company_knowledge_update_status": structured_data.get("company_knowledge_update_status"),
        "theme_quality_context": structured_data.get("theme_quality_context"),
        "theme_prompt_source_selection": structured_data.get("theme_prompt_source_selection"),
        "matched_universe": structured_data.get("matched_universe"),
        "matched_companies": structured_data.get("matched_companies") or structured_data.get("matched_universe"),
        "topic_context": structured_data.get("topic_context"),
        "local_scoring": structured_data.get("local_scoring"),
        "historical_data_policy": structured_data.get("historical_data_policy"),
        **_date_context_prompt_fields(structured_data),
    }


def _theme_radar_structured_prompt_data(structured_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_role": structured_data.get("command_role"),
        "report_date": structured_data.get("report_date"),
        "market_data_date": structured_data.get("market_data_date"),
        "report_generated_at": structured_data.get("report_generated_at"),
        "lookback_days": structured_data.get("lookback_days"),
        "source": structured_data.get("source"),
        "theme_rankings": _compact_theme_rankings(structured_data.get("theme_rankings") or []),
        "subsector_rankings": _compact_subsector_rankings(structured_data.get("subsector_rankings") or []),
        "theme_flow_summaries": _compact_theme_flows(structured_data.get("theme_flow_summaries") or []),
        "sector_strength": _compact_sector_strength(structured_data.get("sector_strength") or {}),
        "news_theme_stats": structured_data.get("news_theme_stats"),
        "strong_stocks": [_compact_stock(row) for row in (structured_data.get("strong_stocks") or [])],
        "topic_library_summary": structured_data.get("topic_library_summary"),
        "data_quality": structured_data.get("data_quality"),
        "analysis_policy": structured_data.get("analysis_policy"),
        "local_scoring": structured_data.get("local_scoring"),
        "historical_data_policy": structured_data.get("historical_data_policy"),
        "analysis_date": structured_data.get("analysis_date"),
        "date_window_policy": structured_data.get("date_window_policy"),
        "news_context": structured_data.get("news_context"),
        "feature_pack": structured_data.get("feature_pack"),
        "data_coverage": structured_data.get("data_coverage"),
    }


def _theme_flow_structured_prompt_data(structured_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_role": structured_data.get("command_role"),
        "report_date": structured_data.get("report_date"),
        "market_data_date": structured_data.get("market_data_date"),
        "report_generated_at": structured_data.get("report_generated_at"),
        "lookback_days": structured_data.get("lookback_days"),
        "theme_query": structured_data.get("theme_query"),
        "theme": structured_data.get("theme"),
        "related_stock_count": structured_data.get("related_stock_count"),
        "related_stocks": [_compact_stock(row) for row in (structured_data.get("related_stocks") or [])],
        "layers": _compact_layers(structured_data.get("layers") or []),
        "layer_market_validation": structured_data.get("layer_market_validation"),
        "next_layer_candidates": structured_data.get("next_layer_candidates"),
        "news_stats": structured_data.get("news_stats"),
        "data_quality": structured_data.get("data_quality"),
        "analysis_policy": structured_data.get("analysis_policy"),
        "local_scoring": structured_data.get("local_scoring"),
        "analysis_date": structured_data.get("analysis_date"),
        "date_window_policy": structured_data.get("date_window_policy"),
        "news_context": structured_data.get("news_context"),
        "feature_pack": structured_data.get("feature_pack"),
        "data_coverage": structured_data.get("data_coverage"),
    }


def _sector_strength_structured_prompt_data(structured_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_role": structured_data.get("command_role"),
        "report_date": structured_data.get("report_date"),
        "market_data_date": structured_data.get("market_data_date"),
        "report_generated_at": structured_data.get("report_generated_at"),
        "lookback_days": structured_data.get("lookback_days"),
        "source": structured_data.get("source"),
        "market_movers": _compact_market_movers(structured_data.get("market_movers") or {}),
        "sector_rankings": [
            {
                **{k: row.get(k) for k in ("sector", "sector_score", "strong_stock_count", "avg_change_pct", "volume_surge_count", "new_high_count", "limit_up_count", "avg_volume_20d", "theme_hit_count", "theme_relation_status_counts", "interpretation_hint")},
                "sector_display_name": row.get("sector_display_name"),
                "top_subsectors": _compact_subsector_rankings(row.get("top_subsectors") or [], limit=0),
                "sector_strong_samples": [_compact_stock(s) for s in (row.get("sector_strong_samples") or [])],
                "representative_stocks": [_compact_stock(s) for s in (row.get("representative_stocks") or [])],
                "candidate_stocks": [_compact_stock(s) for s in (row.get("candidate_stocks") or [])],
            }
            for row in (structured_data.get("sector_rankings") or [])
        ],
        "subsector_rankings": _compact_subsector_rankings(structured_data.get("subsector_rankings") or []),
        "data_quality": structured_data.get("data_quality"),
        "analysis_policy": structured_data.get("analysis_policy"),
        "local_scoring": structured_data.get("local_scoring"),
        "analysis_date": structured_data.get("analysis_date"),
        "date_window_policy": structured_data.get("date_window_policy"),
        "news_context": structured_data.get("news_context"),
        "feature_pack": structured_data.get("feature_pack"),
        "data_coverage": structured_data.get("data_coverage"),
    }


def _compact_stock(row: dict[str, Any]) -> dict[str, Any]:
    matches = []
    for m in (row.get("theme_matches") or []):
        matches.append({
            "theme_id": m.get("theme_id"),
            "theme_name": m.get("theme_name"),
            "relation_score": m.get("relation_score"),
            "match_method": m.get("match_method"),
            "supply_chain_role": m.get("supply_chain_role"),
        })
    return {
        "code": row.get("code"),
        "name": row.get("name"),
        "industry": row.get("industry"),
        "sector": row.get("sector"),
        "sector_display_name": row.get("sector_display_name"),
        "primary_subsector": row.get("primary_subsector"),
        "subsector_matches": (row.get("subsector_matches") or []),
        "price": row.get("price"),
        "change_pct": row.get("change_pct"),
        "change_pct_5d": row.get("change_pct_5d"),
        "change_pct_10d": row.get("change_pct_10d"),
        "change_pct_20d": row.get("change_pct_20d"),
        "volume": row.get("volume"),
        "volume_ratio": row.get("volume_ratio"),
        "turnover": row.get("turnover"),
        "new_high_days": row.get("new_high_days"),
        "new_low_days": row.get("new_low_days"),
        "days_since_high": row.get("days_since_high"),
        "near_high_20d": row.get("near_high_20d"),
        "pullback_from_high_pct": row.get("pullback_from_high_pct"),
        "above_ma5": row.get("above_ma5"),
        "above_ma10": row.get("above_ma10"),
        "above_ma20": row.get("above_ma20"),
        "trend_score": row.get("trend_score"),
        "trend_state": row.get("trend_state"),
        "trend_summary": row.get("trend_summary"),
        "price_date": row.get("price_date"),
        "avg_volume_20d": row.get("avg_volume_20d"),
        "primary_theme_id": row.get("primary_theme_id"),
        "primary_theme_name": row.get("primary_theme_name"),
        "theme_matches": matches,
    }


def _compact_theme_rankings(rankings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rankings:
        result.append({
            "theme_id": row.get("theme_id"),
            "theme_name": row.get("theme_name"),
            "theme_strength_score": row.get("theme_strength_score"),
            "lifecycle": row.get("lifecycle"),
            "theme_state": row.get("theme_state"),
            "avg_trend_score": row.get("avg_trend_score"),
            "trend_pullback_count": row.get("trend_pullback_count"),
            "active_breakout_count": row.get("active_breakout_count"),
            "score_breakdown": row.get("score_breakdown"),
            "strong_stock_count": row.get("strong_stock_count"),
            "direct_relation_count": row.get("direct_relation_count"),
            "strong_nodes": (row.get("strong_nodes") or []),
            "representative_stocks": [_compact_stock(s) for s in (row.get("representative_stocks") or [])],
            "news_stats": row.get("news_stats"),
            "main_risks": (row.get("main_risks") or []),
        })
    return result


def _compact_theme_flows(flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "theme_query": flow.get("theme_query"),
            "theme": flow.get("theme"),
            "market_data_date": flow.get("market_data_date"),
            "related_stock_count": flow.get("related_stock_count"),
            "layers": _compact_layers(flow.get("layers") or []),
            "next_layer_candidates": flow.get("next_layer_candidates"),
            "news_stats": flow.get("news_stats"),
            "data_quality": flow.get("data_quality"),
        }
        for flow in flows
    ]


def _compact_subsector_rankings(rankings: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    result = []
    selected = rankings if limit <= 0 else rankings[:limit]
    for row in selected:
        result.append({
            "sector": row.get("sector"),
            "sector_display_name": row.get("sector_display_name"),
            "subsector": row.get("subsector"),
            "subsector_score": row.get("subsector_score"),
            "strong_stock_count": row.get("strong_stock_count"),
            "avg_change_pct": row.get("avg_change_pct"),
            "volume_surge_count": row.get("volume_surge_count"),
            "new_high_count": row.get("new_high_count"),
            "limit_up_count": row.get("limit_up_count"),
            "theme_hit_count": row.get("theme_hit_count"),
            "strong_samples": [_compact_stock(s) for s in (row.get("strong_samples") or [])],
            "interpretation_hint": row.get("interpretation_hint"),
        })
    return result


def _compact_layers(layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "layer": row.get("layer"),
            "name": row.get("name"),
            "nodes": row.get("nodes"),
            "current_strength": row.get("current_strength"),
            "stage": row.get("stage"),
            "representative_stocks": [_compact_stock(s) for s in (row.get("representative_stocks") or [])],
            "inference": row.get("inference"),
            "verification_needed": row.get("verification_needed"),
        }
        for row in layers
    ]


def _compact_sector_strength(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_role": data.get("command_role"),
        "report_date": data.get("report_date"),
        "market_data_date": data.get("market_data_date"),
        "report_generated_at": data.get("report_generated_at"),
        "lookback_days": data.get("lookback_days"),
        "source": data.get("source"),
        "market_movers": _compact_market_movers(data.get("market_movers") or {}),
        "sector_rankings": [
            {
                **{k: row.get(k) for k in ("sector", "sector_score", "strong_stock_count", "avg_change_pct", "volume_surge_count", "new_high_count", "limit_up_count", "avg_volume_20d", "theme_hit_count", "theme_relation_status_counts", "interpretation_hint")},
                "sector_display_name": row.get("sector_display_name"),
                "top_subsectors": _compact_subsector_rankings(row.get("top_subsectors") or [], limit=0),
                "sector_strong_samples": [_compact_stock(s) for s in (row.get("sector_strong_samples") or [])],
                "representative_stocks": [_compact_stock(s) for s in (row.get("representative_stocks") or [])],
                "candidate_stocks": [_compact_stock(s) for s in (row.get("candidate_stocks") or [])],
            }
            for row in (data.get("sector_rankings") or [])
        ],
        "subsector_rankings": _compact_subsector_rankings(data.get("subsector_rankings") or []),
        "data_quality": data.get("data_quality"),
    }


def _compact_market_movers(data: dict[str, Any]) -> dict[str, Any]:
    if not data:
        return {}
    return {
        "market_data_date": data.get("market_data_date"),
        "report_generated_at": data.get("report_generated_at") or data.get("generated_at"),
        "source_mode": data.get("source_mode"),
        "hard_filter_policy": data.get("hard_filter_policy"),
        "data_quality": data.get("data_quality"),
        "top_gainers": [_compact_stock(row) for row in (data.get("top_gainers") or [])],
        "top_losers": [_compact_stock(row) for row in (data.get("top_losers") or [])],
        "top_volume_surge": [_compact_stock(row) for row in (data.get("top_volume_surge") or [])],
        "top_turnover": [_compact_stock(row) for row in (data.get("top_turnover") or [])],
        "top_trend_strength": [_compact_stock(row) for row in (data.get("top_trend_strength") or [])],
        "new_highs": [_compact_stock(row) for row in (data.get("new_highs") or [])],
        "new_lows": [_compact_stock(row) for row in (data.get("new_lows") or [])],
        "sector_mover_rankings": (data.get("sector_mover_rankings") or []),
    }


def build_prompt_from_request(
    request: CommandRequest,
    structured_data: dict[str, Any],
    source_list: list[SourceItem],
) -> str:
    template = _template_for_request(request)
    base = _read_base_prompt("base.md")
    mode_supplement = _mode_supplement(request)
    scoring_rules = _scoring_rules_for_request(request)
    source_text = _source_text(source_list)
    report_date = request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat()
    variables = {
        "target": request.target or request.market_scope or request.candidate_pool or "latest",
        "stock_id": request.target or "",
        "stock_name": _stock_name(structured_data),
        "market_scope": request.market_scope or "\u53f0\u80a1\u8207\u5168\u7403",
        "theme_scope": request.theme_scope or "\u672a\u6307\u5b9a\u984c\u6750",
        "region_scope": request.region_scope or "global",
        "theme": request.theme_scope or request.target or "\u672a\u6307\u5b9a\u984c\u6750",
        "candidate_pool": request.candidate_pool or request.target or "\u5019\u9078\u80a1\u7968\u6c60",
        "top_n": str(len(structured_data.get("ai_candidates", [])) or request.top or _default_top(request)),
        "report_date": report_date,
    }
    task_prompt = template.format(**variables)
    historical_rules = _historical_rules(request, structured_data)
    discovery_rules = _discovery_rules(request)
    macro_guard_rules = _macro_guard_rules(request, structured_data)

    # report context template (from prompt/rules/report_context.md)
    # ????????????????structured prompt pack????[:22000] ???
    structured_data_json = _prompt_structured_data(request, structured_data)
    report_context = _read_rule_prompt("report_context.md").format(
        request_json=_json(asdict(request)),
        structured_data_json=structured_data_json,
        source_text=source_text,
    )

    # load rules based on command/mode
    rules_blocks = _rules_for_request(request)
    local_scoring_rules = _read_rule_prompt("local_scoring_and_ai_final_scoring.md")

    return f"""{base}

---

{task_prompt}

{mode_supplement}

{historical_rules}

{discovery_rules}

{macro_guard_rules}

---

??????????
{scoring_rules}

---

{report_context}

{local_scoring_rules}

{rules_blocks}

????????Markdown ?????????????????????????????????????
""".strip()


def _safe_task_id(value: str) -> str:
    import re
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", str(value)).strip("_")[:40] or "target"


def _flatten_queries(queries: list) -> list[str]:
    flat: list[str] = []
    for item in queries or []:
        if isinstance(item, dict):
            flat.extend(str(q) for q in item.get("items", []) if str(q).strip())
        elif str(item).strip():
            flat.append(str(item))
    return flat


DISCOVERY_QUERY_BUDGETS = {
    "research": 8,
    "value_scan": 9,
    "macro": 9,
    "theme": 8,
    "theme_flow": 8,
    "theme_radar": 10,
    "sector_strength": 8,
    "radar": 6,
    "news": 6,
    "topic_maintain": 6,
}


def _discovery_query_budget_for_command(command: str) -> int:
    import os

    key = f"AI_DISCOVERY_MAX_QUERIES_{str(command or '').upper()}"
    raw = os.environ.get(key) or os.environ.get("AI_DISCOVERY_MAX_QUERIES_PER_TASK", "")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DISCOVERY_QUERY_BUDGETS.get(str(command or ""), 8)


def _apply_discovery_query_budget(request: CommandRequest, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply a shared per-command search query budget before any provider runs.

    This keeps every discovery task/category, but prevents one task from
    expanding into too many provider calls after site and date query expansion.
    """
    limit = _discovery_query_budget_for_command(request.command)
    if limit <= 0:
        return tasks
    for task in tasks:
        flat = _flatten_queries(task.get("queries") or [])
        original_count = len(flat)
        if original_count > limit:
            task["queries"] = flat[:limit]
        task["query_budget"] = {
            "schema_version": "discovery_query_budget_v1",
            "command": request.command,
            "max_queries_per_task": limit,
            "original_query_count": original_count,
            "final_query_count": min(original_count, limit),
            "strategy": "keep_task_categories_cap_queries",
        }
    return tasks


def _query_group(title: str, items: list[str]) -> dict[str, list[str] | str]:
    return {"title": title, "items": [item for item in items if str(item).strip()]}


def _with_preferred_site_queries(tasks: list[dict[str, Any]], *, max_base_queries: int = 2, max_site_per_task: int = 4) -> list[dict[str, Any]]:
    """Append controlled preferred-source site: queries without changing provider flow."""
    for task in tasks:
        queries = task.setdefault("queries", [])
        base_items: list[str] = []
        for group in queries:
            if isinstance(group, dict):
                base_items.extend(str(q) for q in group.get("items", []) if str(q).strip())
            elif str(group).strip():
                base_items.append(str(group).strip())
        added = 0
        for base_query in base_items[:max_base_queries]:
            if added >= max_site_per_task:
                break
            for site_query in build_site_queries(base_query, max_domains=max_site_per_task):
                if added >= max_site_per_task:
                    break
                if site_query not in queries:
                    queries.append(site_query)
                    added += 1
    return tasks


def _candidate_batches(candidates: list[dict[str, Any]], *, batch_size: int = 4, max_batches: int = 8) -> list[str]:
    labels: list[str] = []
    for row in candidates:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        name = str(row.get("name") or "").strip()
        label = " ".join(part for part in (code, name) if part).strip()
        if label:
            labels.append(label)
    batches: list[str] = []
    for index in range(0, len(labels), batch_size):
        chunk = labels[index:index + batch_size]
        if chunk:
            batches.append(" ".join(chunk))
        if len(batches) >= max_batches:
            break
    return batches


def _value_scan_focus_queries(pool: str, candidates: list[dict[str, Any]], suffix: str) -> list[str]:
    batches = _candidate_batches(candidates, batch_size=4, max_batches=8)
    if not batches:
        return [f"{pool} {suffix}".strip()]
    return [f"{batch} {suffix}".strip() for batch in batches]


def _sector_strength_focus_queries(structured_data: dict[str, Any], suffix: str) -> list[str]:
    rankings = structured_data.get("sector_rankings") or []
    sectors = [str(row.get("sector") or "").strip() for row in rankings[:5] if isinstance(row, dict) and str(row.get("sector") or "").strip()]
    if not sectors:
        return [f"????????????? {suffix}".strip()]
    return [f"???{' '.join(sectors)} {suffix}".strip()]


def build_grounding_discovery_prompts(
    request: CommandRequest,
    structured_data: dict[str, Any],
    source_list: list[SourceItem],
) -> list[dict[str, Any]]:
    """Build multiple compact search prompts for higher quality Gemini grounding."""
    target = request.target or request.market_scope or request.theme_scope or request.candidate_pool or "latest"
    stock = structured_data.get("stock") or {}
    stock_name = stock.get("name") or ""
    report_date = request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat()
    existing_sources = _source_text(source_list[:12])
    local_brief = _grounding_local_brief(request, structured_data)
    tasks = _grounding_discovery_tasks(request, structured_data)
    try:
        from .date_aware_context import augment_discovery_tasks_with_date_context

        max_date_queries = 2 if request.command == "topic_maintain" else 4
        tasks = augment_discovery_tasks_with_date_context(request, structured_data, tasks, max_added_per_task=max_date_queries)
    except Exception:
        pass
    tasks = _apply_discovery_query_budget(request, tasks)
    prompts: list[dict[str, str]] = []
    for index, task in enumerate(tasks, 1):
        label = str(task.get("label") or f"task_{index}")
        objective = str(task.get("objective") or _discovery_rules(request))
        evidence_role = str(task.get("evidence_role") or "supplementary evidence")

        exclude_items = task.get("exclude") or ["Do not rely on model memory without sources."]
        exclude_text = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(exclude_items))

        queries = task.get("queries") or []
        query_lines: list[str] = []
        for group in queries:
            if isinstance(group, dict):
                query_lines.append(f"{group.get('title', '')}:" )
                for q in group.get("items", []):
                    query_lines.append(f"- {q}")
            elif str(group).strip():
                query_lines.append(f"- {group}")
        query_text = "\n".join(query_lines)

        prompt = _format_discovery_prompt(
            _read_discovery_prompt("discovery_task.md"),
            index=index,
            total=len(tasks),
            label=label,
            command=request.command,
            target=target,
            mode=request.mode,
            report_date=report_date,
            stock_name=stock_name,
            objective=objective,
            evidence_role=evidence_role,
            exclude_text=exclude_text,
            query_text=query_text,
            local_brief_json=_json(local_brief)[:5000],
            existing_sources=existing_sources,
        ).strip()
        flat_queries = _flatten_queries(queries)
        prompts.append({
            "label": label,
            "prompt": prompt,
            "queries": flat_queries,
            "objective": objective,
            "evidence_role": evidence_role,
            "query_budget": task.get("query_budget") or {},
        })
    return prompts


def build_grounding_discovery_prompt(
    request: CommandRequest,
    structured_data: dict[str, Any],
    source_list: list[SourceItem],
) -> str:
    """Backward compatible single discovery prompt; new code should use build_grounding_discovery_prompts."""
    prompts = build_grounding_discovery_prompts(request, structured_data, source_list)
    return prompts[0]["prompt"] if prompts else ""


def _grounding_discovery_tasks(request: CommandRequest, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    return build_search_discovery_tasks(request, structured_data)

def _grounding_local_brief(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    if request.command == "research":
        return {
            "stock": structured_data.get("stock"),
            "technical_data": structured_data.get("technical_data"),
            "latest_revenue": (structured_data.get("revenue_data") or [])[-3:],
            "latest_financial": (structured_data.get("financial_data") or [])[-4:],
            "valuation_data": structured_data.get("valuation_data"),
            "chip_backup_summary": (structured_data.get("chip_backup_data") or {}).get("summary"),
        }
    if request.command == "macro":
        return {
            "market_scope": request.market_scope,
            "market_score": structured_data.get("market_score"),
            "fear_greed": structured_data.get("fear_greed"),
            "global_public_macro": structured_data.get("global_public_macro"),
        }
    if request.command == "theme":
        matched = structured_data.get("matched_companies") or structured_data.get("matched_universe") or []
        return {
            "theme": request.theme_scope or request.target,
            "matched_count": len(matched),
            "company_knowledge_summary": structured_data.get("company_knowledge_summary"),
            "top_companies": matched[:20],
        }
    if request.command == "value_scan":
        candidates = structured_data.get("ai_candidates") or structured_data.get("candidates") or []
        return {
            "candidate_pool": request.candidate_pool or request.target,
            "top_n": structured_data.get("top_n"),
            "candidate_count": len(candidates),
            "top_candidates": candidates[:20],
        }
    return {"command": request.command}


def prompt_metadata(request: CommandRequest) -> dict[str, Any]:
    template_name = _template_name_for_request(request)
    scoring = []
    if request.command == "research" and request.mode in {"score", "deep"}:
        scoring.extend([
            "financial_hard_metrics.md",
            "theme_soft_metrics.md",
            "high_growth_gene.md",
            "rerating_model.md",
            "final_research_score.md",
        ])
    if request.command == "value_scan":
        scoring.extend([
            "rerating_model.md",
            "theme_soft_metrics.md",
            "financial_hard_metrics.md",
        ])
    return {
        "template": template_name,
        "base_prompt": "base.md",
        "scoring_files": scoring,
        "strict_sections": True,
        "source_rules": True,
    }


def _template_for_request(request: CommandRequest) -> str:
    return _read_report_prompt(_template_name_for_request(request))


def _template_name_for_request(request: CommandRequest) -> str:
    if request.source_only:
        return TEMPLATE_MAP[("source_only", "source_only")]
    return TEMPLATE_MAP.get((request.command, request.mode)) or TEMPLATE_MAP.get((request.command, "normal"), "research_summary.md")


def _mode_supplement(request: CommandRequest) -> str:
    supplements: list[str] = []
    if request.command == "macro" and request.mode == "deep":
        supplements.append(_read_report_prompt("macro_deep.md"))
    if request.command == "macro" and request.mode == "brief":
        supplements.append("Brief mode: produce a concise Telegram-friendly macro summary with at most 3 key points.")
    if request.command == "value_scan" and request.mode == "deep":
        supplements.append(_read_report_prompt("value_scan_deep.md"))
    return "\n\n".join(supplements)


def _historical_rules(request: CommandRequest, structured_data: dict[str, Any]) -> str:
    if request.report_date is None:
        return ""
    snapshots = structured_data.get("historical_snapshots") or {}
    return _read_rule_prompt("historical_rules.md").format(
        report_date=request.report_date.isoformat(),
        snapshot_status=snapshots.get("status", "unknown"),
        snapshot_count=snapshots.get("snapshot_count", 0),
    )


def _discovery_rules(request: CommandRequest) -> str:
    if request.report_date is not None:
        return ""
    if request.command == "research":
        if request.mode in {"score", "deep"}:
            return _read_rule_prompt("discovery_research.md")
        return _read_rule_prompt("discovery_research.md")
    if request.command == "theme":
        return _read_rule_prompt("discovery_theme.md")
    if request.command in {"theme_radar", "theme_flow", "sector_strength"}:
        return _read_rule_prompt("discovery_theme.md")
    if request.command == "value_scan":
        return _read_rule_prompt("discovery_value_scan.md")
    if request.command == "macro":
        return _read_rule_prompt("discovery_macro.md")
    return ""

def _macro_guard_rules(request: CommandRequest, structured_data: dict[str, Any]) -> str:
    if request.command != "macro":
        return ""
    guard = structured_data.get("macro_data_guard") or {}
    rules = guard.get("prompt_rules") or []
    alerts = guard.get("alerts") or []
    missing = guard.get("missing_data") or []
    label_key = "\u6a19\u7684"
    kind_key = "\u7570\u5e38\u985e\u578b"
    value_key = "\u6578\u503c"
    limit_key = "\u4f7f\u7528\u9650\u5236"
    lines = [
        "## \u5b8f\u89c0\u786c\u6578\u64da\u8b77\u6b04",
        "\u4ee5\u4e0b\u898f\u5247\u512a\u5148\u65bc\u4e00\u822c\u6558\u4e8b\u8207\u6a21\u578b\u8a18\u61b6\uff0c\u907f\u514d\u7522\u751f\u4e0d\u53ef\u9a57\u8b49\u6216\u7570\u5e38\u7684\u5b8f\u89c0\u786c\u6578\u5b57\uff1a",
        *[f"- {rule}" for rule in rules],
    ]
    if alerts:
        lines.extend(["", "\u7570\u5e38\u6578\u5b57\u8b66\u793a\uff1a"])
        for item in alerts[:8]:
            lines.append(f"- {item.get(label_key)}: {item.get(kind_key)}={item.get(value_key)}; {item.get(limit_key)}")
    if missing:
        lines.extend(["", "\u786c\u6578\u5b57\u8cc7\u6599\u7f3a\u53e3\uff1a"])
        for item in missing[:8]:
            lines.append(f"- {item}")
    return "\n".join(lines)



def _scoring_rules_for_request(request: CommandRequest) -> str:
    blocks: list[str] = []
    if request.command == "research" and request.mode in {"score", "deep"}:
        blocks.extend(_split_scoring_blocks([
            ("financial_hard_metrics", "financial_hard_metrics.md"),
            ("theme_soft_metrics", "theme_soft_metrics.md"),
            ("high_growth_gene", "high_growth_gene.md"),
            ("rerating_model", "rerating_model.md"),
            ("final_research_score", "final_research_score.md"),
        ]))
    elif request.command == "value_scan":
        scoring_files = [
            ("rerating_model", "rerating_model.md"),
            ("theme_soft_metrics", "theme_soft_metrics.md"),
            ("financial_hard_metrics", "financial_hard_metrics.md"),
        ]
        if request.mode == "deep":
            scoring_files.append(("high_growth_gene", "high_growth_gene.md"))
        blocks.extend(_split_scoring_blocks(scoring_files))
    else:
        blocks.append("Use verifiable data conservatively; do not assign high scores when evidence is insufficient.")
    return "\n\n".join(blocks)[:SCORING_RULES_CHAR_LIMIT]


def _split_scoring_blocks(files: list[tuple[str, str]]) -> list[str]:
    blocks: list[str] = []
    for title, fname in files:
        content = _read_scoring(fname)
        if content:
            blocks.append(f"## {title}\n{content}")
    if blocks:
        return blocks
    return ["## fallback_scoring_rules\n" + _read_scoring("rerating_model.md")]


def _rules_for_request(request: CommandRequest) -> str:
    """Load prompt rules files based on command and mode."""
    blocks: list[str] = []
    quality_rule = _read_rule_prompt("output_quality_rules.md")
    if quality_rule:
        blocks.append(f"## output_quality_rules.md\n{quality_rule}")
    imagination_rule = _embedded_market_imagination_rule_for_request(request)
    if imagination_rule:
        blocks.append(f"## embedded_market_imagination_rules.md\n{imagination_rule}")
    rules_map = {
        "research": {
            "normal": [
                "company_knowledge_update_rules.md",
                "source_quality_rules.md",
                "risk_and_counter_evidence_rules.md",
            ],
            "deep": [
                "company_knowledge_update_rules.md",
                "local_scoring_and_ai_final_scoring.md",
                "quantitative_score_rules.md",
                "rerating_snapshot_rules.md",
                "chip_score_rules.md",
                "technical_score_rules.md",
                "source_quality_rules.md",
                "risk_and_counter_evidence_rules.md",
            ],
            "score": [
                "company_knowledge_update_rules.md",
                "local_scoring_and_ai_final_scoring.md",
                "quantitative_score_rules.md",
                "rerating_snapshot_rules.md",
                "source_quality_rules.md",
            ],
        },
        "value_scan": {
            "normal": [
                "company_knowledge_update_rules.md",
                "local_scoring_and_ai_final_scoring.md",
                "rerating_snapshot_rules.md",
                "source_quality_rules.md",
                "risk_and_counter_evidence_rules.md",
            ],
            "deep": [
                "company_knowledge_update_rules.md",
                "local_scoring_and_ai_final_scoring.md",
                "quantitative_score_rules.md",
                "rerating_snapshot_rules.md",
                "chip_score_rules.md",
                "technical_score_rules.md",
                "source_quality_rules.md",
                "risk_and_counter_evidence_rules.md",
            ],
            "source_only": [
                "company_knowledge_update_rules.md",
                "source_quality_rules.md",
            ],
        },
        "macro": {
            "normal": ["source_quality_rules.md"],
            "deep": ["source_quality_rules.md"],
            "brief": ["source_quality_rules.md"],
        },
        "theme": {
            "normal": ["company_knowledge_update_rules.md", "source_quality_rules.md"],
            "deep": ["company_knowledge_update_rules.md", "source_quality_rules.md"],
            "source_only": ["company_knowledge_update_rules.md", "source_quality_rules.md"],
        },
        "theme_radar": {
            "normal": ["source_quality_rules.md", "risk_and_counter_evidence_rules.md"],
            "deep": ["source_quality_rules.md", "risk_and_counter_evidence_rules.md"],
            "source_only": ["source_quality_rules.md"],
        },
        "theme_flow": {
            "normal": ["source_quality_rules.md", "risk_and_counter_evidence_rules.md"],
            "deep": ["source_quality_rules.md", "risk_and_counter_evidence_rules.md"],
            "source_only": ["source_quality_rules.md"],
        },
        "sector_strength": {
            "normal": ["source_quality_rules.md"],
            "deep": ["source_quality_rules.md", "risk_and_counter_evidence_rules.md"],
            "source_only": ["source_quality_rules.md"],
        },
    }
    rule_files = rules_map.get(request.command, {}).get(request.mode, [])
    for fname in rule_files:
        content = _read_rule_prompt(fname)
        if content:
            blocks.append(f"## {fname}\n{content}")
    # --date mode loads historical_rules.md in addition
    if request.report_date is not None:
        hist = _read_rule_prompt("historical_date_rules.md")
        if hist:
            blocks.append(f"## historical_date_rules.md\n{hist}")
    return "\n\n".join(blocks)


def _embedded_market_imagination_rule_for_request(request: CommandRequest) -> str:
    if request.source_only or request.mode == "source_only":
        return ""
    if request.command in {
        "research",
        "value_scan",
        "macro",
        "theme",
        "theme_radar",
        "theme_flow",
        "sector_strength",
    }:
        return _read_rule_prompt("embedded_market_imagination_rules.md")
    return ""


def _read_base_prompt(name: str) -> str:
    path = PROMPT_BASE_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig")


def _read_report_prompt(name: str) -> str:
    path = PROMPT_REPORT_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig")


def _read_rule_prompt(name: str) -> str:
    path = PROMPT_RULES_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig")


def _format_discovery_prompt(template: str, **kwargs) -> str:
    """Format discovery prompt template with safe variable replacement.

    Uses regex-based replacement that only matches simple {var_name} patterns
    (alphanumeric + underscore, NOT containing whitespace or quotes).
    This prevents JSON example content like {level} or {finding} from being
    incorrectly treated as format placeholders.
    """
    import re
    # Match {word_chars} only - prevents matching JSON-like content with spaces/quotes
    def replacer(m):
        key = m.group(1)
        if key in kwargs:
            return str(kwargs[key])
        # Leave unrecognized placeholders as-is (original text)
        return m.group(0)
    return re.sub(r'\{([A-Za-z0-9_]+)\}', replacer, template)


def _read_discovery_prompt(name: str) -> str:
    path = PROMPT_DISCOVERY_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig")


def _read_prompt(name: str) -> str:
    """Read from legacy config/prompts/ for backward compatibility; new code uses _read_base_prompt / _read_report_prompt."""
    # Try new location first
    if name == "base.md":
        new_path = PROMPT_BASE_DIR / name
        if new_path.exists():
            return new_path.read_text(encoding="utf-8-sig")
    new_path = PROMPT_REPORT_DIR / name
    if new_path.exists():
        return new_path.read_text(encoding="utf-8-sig")
    # Fallback to legacy config/prompts/
    legacy_path = ROOT_DIR / "config" / "prompts" / name
    if legacy_path.exists():
        return legacy_path.read_text(encoding="utf-8-sig")
    return ""


def _read_scoring(name: str) -> str:
    # Try new location first, then fallback to legacy config/scoring/
    new_path = PROMPT_SCORING_DIR / name
    if new_path.exists():
        return new_path.read_text(encoding="utf-8-sig")
    legacy_path = ROOT_DIR / "config" / "scoring" / name
    if legacy_path.exists():
        return legacy_path.read_text(encoding="utf-8-sig")
    return "Scoring rules file not found. Use verifiable evidence conservatively."


def _source_text(source_list: list[SourceItem]) -> str:
    if not source_list:
        return "No citable sources are currently available."
    return "\n".join(
        f"[{item.source_id}] {item.source_level} {item.title} {item.url} published_date={item.published_date or 'unknown'}"
        for item in source_list
    )


def _stock_name(structured_data: dict[str, Any]) -> str:
    stock = structured_data.get("stock") or {}
    return str(stock.get("name") or "")


def _default_top(request: CommandRequest) -> int:
    if request.command == "theme":
        return 10
    if request.command == "value_scan":
        return 10
    return 0


def _json(value: Any) -> str:
    return json_dumps(value)


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
