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
    """依指令類型使用專用 structured prompt pack，避免 [:22000] 截斷導致重要資料遺漏。

    - value_scan: 使用 ai_candidate_evidence_pack，完整不打折扣
    - research: 使用 research 專用 pack，保留 local_rerating_snapshot / local_scoring
    - macro: 使用 macro 專用 pack，保留 quantitative_market / fear_greed 等
    - theme: 使用 theme 專用 pack，保留 matched_universe / matched_companies
    """
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
    }


def _research_structured_prompt_data(structured_data: dict[str, Any]) -> dict[str, Any]:
    """Research 指令專用 structured prompt pack，完整保留評分與重估資料。"""
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
    """Macro 指令專用 structured prompt pack，完整保留總經指標與情緒資料。"""
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
        "industry_index_data": structured_data.get("industry_index_data"),
        "free_public_sources": structured_data.get("free_public_sources"),
        "local_scoring": structured_data.get("local_scoring"),
        "historical_data_policy": structured_data.get("historical_data_policy"),
        **_date_context_prompt_fields(structured_data),
    }


def _theme_structured_prompt_data(structured_data: dict[str, Any]) -> dict[str, Any]:
    """Theme 指令專用 structured prompt pack，完整保留題材命中公司資料。"""
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
        "strong_stocks": [_compact_stock(row) for row in (structured_data.get("strong_stocks") or [])[:30]],
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
        "related_stocks": [_compact_stock(row) for row in (structured_data.get("related_stocks") or [])[:60]],
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
                "top_subsectors": _compact_subsector_rankings(row.get("top_subsectors") or [], limit=5),
                "sector_strong_samples": [_compact_stock(s) for s in (row.get("sector_strong_samples") or [])[:5]],
                "representative_stocks": [_compact_stock(s) for s in (row.get("representative_stocks") or [])[:3]],
                "candidate_stocks": [_compact_stock(s) for s in (row.get("candidate_stocks") or [])[:3]],
            }
            for row in (structured_data.get("sector_rankings") or [])[:20]
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
    for m in (row.get("theme_matches") or [])[:2]:
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
        "subsector_matches": (row.get("subsector_matches") or [])[:2],
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
    for row in rankings[:8]:
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
            "strong_nodes": (row.get("strong_nodes") or [])[:4],
            "representative_stocks": [_compact_stock(s) for s in (row.get("representative_stocks") or [])[:3]],
            "news_stats": row.get("news_stats"),
            "main_risks": (row.get("main_risks") or [])[:3],
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
        for flow in flows[:3]
    ]


def _compact_subsector_rankings(rankings: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    result = []
    for row in rankings[:limit]:
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
            "strong_samples": [_compact_stock(s) for s in (row.get("strong_samples") or [])[:5]],
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
            "representative_stocks": [_compact_stock(s) for s in (row.get("representative_stocks") or [])[:3]],
            "inference": row.get("inference"),
            "verification_needed": row.get("verification_needed"),
        }
        for row in layers[:4]
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
                "top_subsectors": _compact_subsector_rankings(row.get("top_subsectors") or [], limit=5),
                "sector_strong_samples": [_compact_stock(s) for s in (row.get("sector_strong_samples") or [])[:5]],
                "representative_stocks": [_compact_stock(s) for s in (row.get("representative_stocks") or [])[:3]],
                "candidate_stocks": [_compact_stock(s) for s in (row.get("candidate_stocks") or [])[:3]],
            }
            for row in (data.get("sector_rankings") or [])[:20]
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
        "top_gainers": [_compact_stock(row) for row in (data.get("top_gainers") or [])[:20]],
        "top_losers": [_compact_stock(row) for row in (data.get("top_losers") or [])[:20]],
        "top_volume_surge": [_compact_stock(row) for row in (data.get("top_volume_surge") or [])[:20]],
        "top_turnover": [_compact_stock(row) for row in (data.get("top_turnover") or [])[:20]],
        "top_trend_strength": [_compact_stock(row) for row in (data.get("top_trend_strength") or [])[:20]],
        "new_highs": [_compact_stock(row) for row in (data.get("new_highs") or [])[:20]],
        "new_lows": [_compact_stock(row) for row in (data.get("new_lows") or [])[:20]],
        "sector_mover_rankings": (data.get("sector_mover_rankings") or [])[:20],
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
        "market_scope": request.market_scope or "全球 + 台股",
        "theme_scope": request.theme_scope or "未指定",
        "region_scope": request.region_scope or "global",
        "theme": request.theme_scope or request.target or "未指定題材",
        "candidate_pool": request.candidate_pool or request.target or "精選選股",
        # value_scan 使用實際 AI 候選股數量，不要只看 request.top（deep 模式 request.top=None）
        "top_n": str(len(structured_data.get("ai_candidates", [])) or request.top or _default_top(request)),
        "report_date": report_date,
    }
    task_prompt = template.format(**variables)
    historical_rules = _historical_rules(request, structured_data)
    discovery_rules = _discovery_rules(request)

    # report context template (from prompt/rules/report_context.md)
    # 依指令使用專用 structured prompt pack，不做 [:22000] 截斷
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

---

評分與重估規則：
{scoring_rules}

---

{report_context}

{local_scoring_rules}

{rules_blocks}

請輸出完整 Markdown 報告。所有章節標題必須使用指定章節文字，且不得省略資料來源列表。
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
        return [f"台股 類股強弱 {suffix}".strip()]
    return [f"台股 {' '.join(sectors)} {suffix}".strip()]


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
    prompts: list[dict[str, str]] = []
    for index, task in enumerate(tasks, 1):
        label = str(task.get("label") or f"task_{index}")
        objective = str(task.get("objective") or _discovery_rules(request))

        exclude_items = task.get("exclude") or ["無"]
        exclude_text = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(exclude_items))

        queries = task.get("queries") or []
        query_lines: list[str] = []
        for group in queries:
            if isinstance(group, dict):
                query_lines.append(f"{group.get('title', '')}：")
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
            exclude_text=exclude_text,
            query_text=query_text,
            local_brief_json=_json(local_brief)[:5000],
            existing_sources=existing_sources,
        ).strip()
        flat_queries = _flatten_queries(queries)
        prompts.append({"label": label, "prompt": prompt, "queries": flat_queries, "objective": objective})
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
    target = request.target or request.market_scope or request.theme_scope or request.candidate_pool or "latest"
    stock = structured_data.get("stock") or {}
    stock_name = stock.get("name") or ""
    target_label = " ".join(part for part in [str(target), str(stock_name or "")] if part).strip()
    if request.command == "research":
        tasks = [
            {
                "label": "官方公告與財報",
                "objective": "請尋找公開資訊觀測站重大訊息、月營收公告、季報與年報、法說會簡報、股利政策、股東會資料、公司官網與投資人關係資料。",
                "exclude": ["技術面走勢", "法人買賣超", "論壇討論", "題材熱度", "產業競爭者比較"],
                    "queries": [
                        {"title": "官方公告", "items": [
                            f"{target_label} 公開資訊觀測站 重大訊息",
                            f"{target_label} MOPS material information",
                            f"{target_label} 公司官網 投資人關係 年報 法說會",
                            f"{target_label} annual report investor relations presentation"
                        ]},
                        {"title": "月營收與財報", "items": [
                            f"{target_label} 月營收 財報 2026",
                            f"{target_label} Q1 財報 毛利率 EPS",
                            f"{target_label} monthly revenue financial report",
                            f"{target_label} 營益率 自由現金流 存貨週轉 應收帳款"
                        ]},
                    {"title": "法說會與投資人資料", "items": [
                        f"{target_label} 法說會 簡報 2026",
                        f"{target_label} 投資人關係 investor relations",
                        f"{target_label} investor conference presentation"
                    ]},
                    {"title": "股利與股東會", "items": [
                        f"{target_label} 股利政策 除息 股東會",
                        f"{target_label} dividend policy"
                    ]},
                    {"title": "風險與負面資訊", "items": [
                        f"{target_label} 營收 衰退 毛利率 下滑",
                        f"{target_label} 客戶 庫存 需求 風險",
                        f"{target_label} risk margin decline inventory"
                    ]}
                ],
            },
            {
                "label": "近期新聞與公司事件",
                "objective": "請尋找近期公司新聞、訂單、產品、管理層、營運展望、產能、客戶、併購、訴訟或市場事件。",
                "exclude": ["技術線型", "論壇情緒", "未具名爆料", "沒有日期的轉貼文"],
                    "queries": [
                        {"title": "近期新聞", "items": [
                            f"{target_label} 近期新聞 營收 訂單 產品",
                            f"{target_label} MoneyDJ 鉅亨 工商 經濟日報 中央社",
                            f"{target_label} recent news revenue earnings product order",
                            f"{target_label} 今日新聞 本週新聞 近況 法說"
                        ]},
                    {"title": "公司事件", "items": [
                        f"{target_label} 新產品 客戶 產能 展望",
                        f"{target_label} 管理層 併購 訴訟 風險"
                    ]}
                ],
            },
            {
                "label": "產業與題材",
                "objective": "請尋找產業趨勢、產品線、需求驅動、CAGR、市場規模、技術護城河、供應鏈位置、轉型效益與題材連結證據。",
                "exclude": ["短線技術面", "法人買賣超", "論壇喊單", "沒有營收連結的純題材文章"],
                    "queries": [
                        {"title": "產業成長", "items": [
                            f"{target_label} 產業 趨勢 市場規模 CAGR",
                            f"{target_label} market size CAGR demand driver",
                            f"{target_label} 同業 競爭者 產業排名 市占率"
                        ]},
                        {"title": "產品與技術", "items": [
                            f"{target_label} 產品線 技術優勢 護城河",
                            f"{target_label} product line technology moat",
                            f"{target_label} 產品 客戶 營收占比 主要應用"
                        ]},
                    {"title": "供應鏈與轉型", "items": [
                        f"{target_label} 供應鏈 客戶 營收占比",
                        f"{target_label} 轉型 新產品 新應用"
                    ]}
                ],
            },
            {
                "label": "籌碼與法人",
                "objective": "請尋找公開可驗證的法人關注、外資投信、自營商、融資融券、股權結構、集保股權分散、董監持股或大戶籌碼資料。",
                "exclude": ["未具名主力傳聞", "論壇猜測", "技術型態解讀", "沒有來源的籌碼截圖"],
                    "queries": [
                        {"title": "法人與籌碼", "items": [
                            f"{target_label} 外資 投信 自營商 買賣超",
                            f"{target_label} institutional investors foreign buying investment trust",
                            f"{target_label} 法人報告 摘要 目標價 評等"
                        ]},
                    {"title": "股權結構", "items": [
                        f"{target_label} 集保 股權分散 大戶 董監持股",
                        f"{target_label} shareholder structure TDCC margin trading"
                    ]}
                ],
            },
            {
                "label": "風險與反證",
                "objective": "請尋找負面證據、風險、利空、需求放緩、毛利率壓力、庫存、客戶集中、價格競爭、景氣循環與看法矛盾之處。",
                "exclude": ["無來源的看空留言", "純技術面回檔", "沒有日期的舊新聞"],
                "queries": [
                    {"title": "營運風險", "items": [
                        f"{target_label} 風險 毛利率 下滑 庫存 需求放緩",
                        f"{target_label} risk margin pressure inventory demand slowdown"
                    ]},
                        {"title": "反證與矛盾", "items": [
                            f"{target_label} 利空 下修 競爭 客戶集中",
                            f"{target_label} bearish risk customer concentration competition",
                            f"{target_label} 砍單 延後 出貨 放緩 庫存調整"
                        ]}
                ],
            },
        ]
        if request.mode in {"score", "deep"}:
            tasks.append(
                {
                    "label": "評分證據",
                    "objective": "請尋找可用於量化評分與價值重估的證據，包括 CAGR、護城河、轉型效益、題材熱度、估值重估、反證與扣分依據。不得只因新聞熱門就給高分。",
                    "exclude": ["最終買賣建議", "目標價", "無來源評分", "純論壇情緒"],
                    "queries": [
                        {"title": "評分資料", "items": [
                            f"{target_label} CAGR 護城河 轉型效益 題材熱度",
                            f"{target_label} valuation rerating moat transformation evidence",
                            f"{target_label} 已驗證加分 推論型加分 題材想像空間"
                        ]},
                        {"title": "扣分資料", "items": [
                            f"{target_label} 估值過高 營收未跟上 題材水分",
                            f"{target_label} weak revenue link valuation risk hype"
                        ]}
                    ],
                }
            )
        return _with_preferred_site_queries(tasks, max_base_queries=3, max_site_per_task=5)
    if request.command == "macro":
        market = request.market_scope or target
        macro_exclude = ["個股買賣建議", "未具名市場傳言", "無來源社群情緒", "過期資料"]
        tasks = [
            {
                "label": "官方總經與市場資料",
                "objective": "請尋找台灣與全球的官方公開總經與市場資料，包括指數、利率、匯率、資金流與官方風險指標。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "官方資料", "items": [
                        f"{market} 官方 總經 數據 指數 匯率 利率 資金",
                        f"{market} official market data index FX rates liquidity",
                        f"{market} 主計總處 央行 金管會 證交所 櫃買中心"
                    ]}
                ],
            },
            {
                "label": "台股市場新聞",
                "objective": "請尋找近期台股市場新聞、指數驅動因素、類股輪動、法人資金流與風險事件。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "台股盤勢", "items": [
                        f"{market} 台股 盤勢 類股輪動 法人資金 風險",
                        f"{market} Taiwan stock market news sector rotation institutional flow"
                    ]}
                ],
            },
            {
                "label": "全球跨資產",
                "objective": "請尋找全球跨資產環境：美股四大指數、SOX、美債殖利率、美元/台幣、原油、黃金、VIX 與主要風險事件。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "跨資產", "items": [
                        "美債殖利率 美元 台幣 SOX Nasdaq VIX 油價 金價",
                        "US10Y USD TWD SOX Nasdaq VIX oil gold risk",
                        "S&P 500 Nasdaq Dow Jones Russell 2000 semiconductor index"
                    ]}
                ],
            },
            {
                "label": "地緣政治與貿易",
                "objective": "請尋找當前地緣政治、戰爭、制裁、關稅、出口管制、貿易政策發展，可能影響全球市場與台股。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "國際局勢", "items": [
                        f"{market} 國際局勢 戰爭 制裁 關稅 出口管制 貿易政策",
                        f"{market} geopolitics war sanctions tariffs export controls trade policy",
                        f"{market} 中美關係 台海風險 紅海 俄烏 中東 供應鏈"
                    ]}
                ],
            },
            {
                "label": "央行政策與利率",
                "objective": "請尋找 Fed、歐洲央行、日本央行、中國人行、台灣央行政策、升降息預期、債券殖利率、美元指數、匯率。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "央行政策", "items": [
                        f"{market} Fed 歐洲央行 日本央行 中國人行 升息 降息 匯率",
                        f"{market} Fed ECB BOJ PBOC rate cut hike bond yield DXY FX",
                        f"{market} FOMC 點陣圖 CPI PCE 就業 非農 通膨預期"
                    ]}
                ],
            },
            {
                "label": "原物料與能源",
                "objective": "請尋找原油、天然氣、銅、鋁、黃金、鋼鐵、塑化、農產品原物料趨勢與成本壓力對市場和台灣產業的影響。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "原物料", "items": [
                        f"{market} 油價 天然氣 銅 鋁 黃金 鋼鐵 原物料 通膨",
                        f"{market} oil natural gas copper aluminum gold steel commodities"
                    ]}
                ],
            },
            {
                "label": "房地產與信用風險",
                "objective": "請尋找美國、中國、台灣、歐洲房地產、房貸、信用風險、銀行壓力與房市政策風險。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "房市信用", "items": [
                        f"{market} 房地產 房貸 信用風險 銀行壓力 中國房市 美國房市",
                        f"{market} real estate mortgage credit risk banking stress",
                        f"{market} 中國房地產 歐洲房地產 商用不動產 違約"
                    ]}
                ],
            },
            {
                "label": "期貨與波動率",
                "objective": "請尋找台指期、選擇權、波動率、期貨法人未平倉與台灣 VIX 資料。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "期貨波動率", "items": [
                        "台指期 選擇權 三大法人 未平倉 波動率 台灣 VIX TAIFEX",
                        "TAIFEX futures institutional open interest volatility Taiwan VIX"
                    ]}
                ],
            },
            {
                "label": "總經風險",
                "objective": "請尋找可能推翻樂觀看法的總經風險、政策風險、地緣政治風險、流動性風險、信用風險、原物料與匯率衝擊。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "總經風險", "items": [
                        f"{market} 總經 風險 流動性 信用風險 匯率 原物料",
                        f"{market} macro risk liquidity credit commodity FX shock",
                        f"{market} 反證 利空 避險 資金外流 降評"
                    ]}
                ],
            },
            {
                "label": "論壇與社群情緒",
                "objective": "請尋找 PTT、Dcard、Mobile01、理財寶等社群討論線索，只能作為市場情緒、熱度與待驗證議題，不得單獨支撐高分或投資結論。",
                "exclude": ["將論壇留言當作已驗證事實", "無日期或無來源截圖", "喊單與目標價"],
                "queries": [
                    {"title": "論壇社群 site query", "items": [
                        f"site:ptt.cc/bbs/Stock {target_label}",
                        f"site:dcard.tw/f {target_label} 股票 投資",
                        f"site:mobile01.com {target_label} 股票 投資",
                        f"site:social.cmoney.tw/forum/stock {target_label}",
                    ]}
                ],
            },
        ]
        return _with_preferred_site_queries(tasks, max_base_queries=2, max_site_per_task=4)
    if request.command == "theme":
        theme = request.theme_scope or request.target or target
        theme_exclude = ["個股買賣建議", "無營收連結的題材文章", "論壇喊單", "沒有來源的供應鏈名單"]
        tasks = [
            {
                "label": "題材定義與市場規模",
                "objective": "請尋找題材的明確定義、需求驅動力、市場規模、CAGR 與關鍵產業證據。",
                "exclude": theme_exclude,
                "queries": [
                    {"title": "題材定義", "items": [
                        f"{theme} 題材 定義 市場規模 CAGR 需求驅動",
                        f"{theme} market size CAGR demand driver Taiwan stocks",
                        f"{theme} 產業趨勢 滲透率 成長率 主要受惠環節"
                    ]}
                ],
            },
            {
                "label": "台股供應鏈",
                "objective": "請尋找台股相關供應鏈公司、產品角色、上下游關係與每個角色的證據。",
                "exclude": theme_exclude,
                "queries": [
                    {"title": "供應鏈", "items": [
                        f"{theme} 台股 供應鏈 公司 產品 角色",
                        f"{theme} Taiwan supply chain companies product role",
                        f"{theme} 上游 中游 下游 關鍵零組件 代表股"
                    ]}
                ],
            },
            {
                "label": "公司產品客戶與營收占比",
                "objective": "請尋找可能受惠公司的產品、客戶分類、營收占比、投資人資料與官方證據。",
                "exclude": theme_exclude,
                "queries": [
                    {"title": "公司證據", "items": [
                        f"{theme} 公司 產品 客戶 營收占比 法說會",
                        f"{theme} company revenue exposure customer product investor",
                        f"{theme} 投資人關係 年報 法說會 產品應用 客戶分類"
                    ]}
                ],
            },
            {
                "label": "近期催化因素",
                "objective": "請尋找近期新聞、訂單、政策催化、資本支出、需求轉折與短期事件。",
                "exclude": theme_exclude,
                "queries": [
                    {"title": "催化因素", "items": [
                        f"{theme} 近期新聞 訂單 政策 催化 資本支出",
                        f"{theme} recent news orders capex catalyst demand inflection",
                        f"{theme} 今日新聞 本週新聞 新規格 新產品 新客戶"
                    ]}
                ],
            },
            {
                "label": "題材水分與反證",
                "objective": "請尋找題材水分、估值風險、營收連結薄弱的證據、矛盾資訊與只沾邊的股票。",
                "exclude": theme_exclude,
                "queries": [
                    {"title": "風險反證", "items": [
                        f"{theme} 風險 估值過高 題材水分 營收連結不足",
                        f"{theme} risk valuation hype weak revenue link contradiction",
                        f"{theme} 替代技術 競爭者 退燒 庫存 需求放緩"
                    ]}
                ],
            },
        ]
        return _with_preferred_site_queries(tasks, max_base_queries=2, max_site_per_task=4)
    if request.command == "value_scan":
        pool = request.candidate_pool or request.target or target
        candidates = structured_data.get("ai_candidates") or structured_data.get("candidates") or []
        vs_exclude = ["最終買賣建議", "無來源評分", "純論壇情緒", "沒有日期的舊資料"]
        tasks = [
            {
                "label": "候選股近期新聞",
                "objective": "請尋找價值重估候選股近期新聞，特別是產品、訂單、客戶、營收與管理層變化。",
                "exclude": vs_exclude,
                "queries": [
                    _query_group("近期新聞", _value_scan_focus_queries(pool, candidates, "近期新聞 營收 產品 訂單 客戶 法說")),
                    _query_group("英文新聞", _value_scan_focus_queries(pool, candidates, "recent news earnings product orders customer")),
                ],
            },
            {
                "label": "官方公告與法說",
                "objective": "請尋找候選股的 MOPS 公告、財報、月營收、法說會與官方公司資料。",
                "exclude": vs_exclude,
                "queries": [
                    _query_group("官方資料", _value_scan_focus_queries(pool, candidates, "公開資訊觀測站 月營收 財報 法說會")),
                    _query_group("英文官方資料", _value_scan_focus_queries(pool, candidates, "MOPS monthly revenue financial report investor conference")),
                ],
            },
            {
                "label": "新舊標籤重估證據",
                "objective": "請尋找舊市場標籤與新市場標籤的證據，包括轉型、新產品線、新需求與客戶分類變化。",
                "exclude": vs_exclude,
                "queries": [
                    _query_group("重估證據", _value_scan_focus_queries(pool, candidates, "轉型 新產品 新應用 價值重估 新標籤")),
                    _query_group("英文重估證據", _value_scan_focus_queries(pool, candidates, "transformation new product rerating customer revenue exposure")),
                ],
            },
            {
                "label": "估值與財務品質",
                "objective": "請尋找本益比、EPS、毛利率、營收成長、庫存、現金流等與重估品質相關的估值與財務證據。",
                "exclude": vs_exclude,
                "queries": [
                    _query_group("估值財務", _value_scan_focus_queries(pool, candidates, "本益比 EPS 毛利率 庫存 現金流 營收年增")),
                    _query_group("英文估值財務", _value_scan_focus_queries(pool, candidates, "valuation EPS margin revenue inventory cash flow")),
                ],
            },
            {
                "label": "法人與籌碼",
                "objective": "請尋找法人關注、籌碼變化、外資/投信動態、股權集中度與流動性證據。",
                "exclude": vs_exclude,
                "queries": [
                    _query_group("法人籌碼", _value_scan_focus_queries(pool, candidates, "外資 投信 大戶 集保 融資融券 法人報告")),
                    _query_group("英文法人籌碼", _value_scan_focus_queries(pool, candidates, "institutional investor foreign buying shareholder concentration")),
                ],
            },
            {
                "label": "重估失敗風險與反證",
                "objective": "請尋找重估失敗的下行風險、無營收的題材、客戶集中、景氣循環下行與矛盾看法。",
                "exclude": vs_exclude,
                "queries": [
                    _query_group("風險反證", _value_scan_focus_queries(pool, candidates, "重估失敗 風險 題材水分 營收未跟上 客戶集中")),
                    _query_group("英文風險反證", _value_scan_focus_queries(pool, candidates, "rerating risk hype revenue weak customer concentration downturn")),
                ],
            },
        ]
        return _with_preferred_site_queries(tasks, max_base_queries=2, max_site_per_task=4)
    if request.command == "theme_radar":
        lookback = request.lookback_days or 7
        theme_exclude = [
            "個股買賣建議",
            "無來源題材傳聞",
            "純社群喊單",
            "非台股市場活動",
            "farmers market / food market / event calendar / holiday calendar",
            "加密貨幣 market cap 或非台股市場概況",
        ]
        tasks = [
            {
                "label": "熱門題材與資金輪動",
                "objective": "請尋找近期待升溫或正在擴散的台股題材、類股輪動、法人資金流與主流媒體證據。",
                "exclude": theme_exclude,
                "queries": [
                    _query_group("熱門題材", [
                        f"台股 近{lookback}天 熱門題材 類股輪動 資金流向",
                        "上市櫃 今日 漲停 量增 題材 族群 輪動",
                        "台股 強勢族群 漲幅排行 量增排行 創高 題材",
                        f"Taiwan equities TWSE TPEx sector rotation stock themes fund flow {lookback} days",
                    ]),
                ],
            },
            {
                "label": "題材催化與新聞爆量",
                "objective": "請尋找造成題材升溫的政策、訂單、法說會、月營收、國際大廠與供應鏈事件。",
                "exclude": [*theme_exclude, "沒有日期的舊聞", "無產業或營收連結的短線文章"],
                "queries": [
                    _query_group("催化事件", [
                        "台股 題材 催化 訂單 法說會 月營收 供應鏈",
                        "台股 產業新聞 國際大廠 NVIDIA AMD Apple Tesla 供應鏈",
                        "Taiwan listed companies supply chain catalyst orders revenue investor conference TWSE TPEx",
                    ]),
                ],
            },
            {
                "label": "退燒題材與反證",
                "objective": "請尋找題材退燒、估值過熱、營收未跟上、庫存與需求放緩的反證。",
                "exclude": [*theme_exclude, "只有股價回檔但沒有基本面原因的內容"],
                "queries": [
                    _query_group("題材風險", [
                        "台股 題材 退燒 過熱 營收未跟上 庫存 需求放緩",
                        "Taiwan equities theme stocks risk hype weak revenue inventory slowdown TWSE TPEx",
                    ]),
                ],
            },
        ]
        return _with_preferred_site_queries(tasks, max_base_queries=2, max_site_per_task=4)
    if request.command == "theme_flow":
        theme = request.theme_scope or request.target or target
        tasks = [
            {
                "label": "題材擴散路徑",
                "objective": "請尋找指定題材從核心受惠股擴散到上游、中游、下游與周邊族群的證據。",
                "exclude": ["沒有產品角色的股票清單", "無來源社群傳聞"],
                "queries": [
                    _query_group("擴散路徑", [
                        f"{theme} 台股 擴散 上游 中游 下游 供應鏈 代表股",
                        f"{theme} 供應鏈 層級 產品角色 關鍵零組件 受惠公司",
                        f"{theme} Taiwan supply chain upstream downstream beneficiary stocks",
                    ]),
                ],
            },
            {
                "label": "下一層受惠與替代題材",
                "objective": "請尋找尚未完全反映但可能擴散的下一層供應鏈、替代技術、替代材料與反證。",
                "exclude": ["沒有營收或產品連結的延伸想像"],
                "queries": [
                    _query_group("下一層擴散", [
                        f"{theme} 下一波 受惠股 替代技術 替代材料 供應鏈",
                        f"{theme} 風險 退燒 替代方案 競爭者 營收連結不足",
                    ]),
                ],
            },
        ]
        return _with_preferred_site_queries(tasks, max_base_queries=2, max_site_per_task=4)
    if request.command == "sector_strength":
        tasks = [
            {
                "label": "族群強弱與法人資金",
                "objective": "請尋找台股族群強弱、類股輪動、法人資金流向、成交量變化與主流媒體證據。",
                "exclude": ["個股買賣建議", "無來源排行", "純技術線型解讀", "非台股市場活動或國外展覽資訊"],
                "queries": [
                    _query_group("族群強弱", [
                        "台股 族群強弱 類股輪動 法人資金 成交量 三大法人",
                        "台股 強勢族群 弱勢族群 三大法人 買超 類股 證交所 櫃買",
                        "Taiwan stocks sector strength rotation institutional fund flow TWSE TPEx",
                        *_sector_strength_focus_queries(structured_data, "強勢族群 法人 買超 成交量"),
                    ]),
                ],
            },
            {
                "label": "族群風險與過熱",
                "objective": "請尋找強勢族群的估值過熱、利空、政策反轉、庫存與需求放緩風險。",
                "exclude": ["無來源市場耳語", "非台股市場活動或國外展覽資訊"],
                "queries": [
                    _query_group("族群風險", [
                        "台股 強勢族群 過熱 風險 利空 庫存 需求放緩 月營收",
                        "Taiwan stocks sector risk overheating valuation inventory revenue slowdown",
                        *_sector_strength_focus_queries(structured_data, "過熱 風險 月營收 庫存 需求放緩"),
                    ]),
                ],
            },
        ]
        return _with_preferred_site_queries(tasks, max_base_queries=2, max_site_per_task=4)
    if request.command == "topic_maintain":
        mode = request.mode or "normal"
        is_deep = mode in {"deep", "score"}
        is_initial = structured_data.get("topic_maintain_mode_hint") == "initial"
        focus_theme = (request.target or request.theme_scope or "").strip()

        # Try to load discovery prompt from prompt/topic/topic_discovery_search.md
        root = Path(__file__).resolve().parents[1]
        discovery_prompt_path = root / "prompt" / "topic" / "topic_discovery_search.md"
        if discovery_prompt_path.exists():
            try:
                discovery_text = discovery_prompt_path.read_text(encoding="utf-8")
                # Build tasks from the loaded prompt - parse it into query tasks
                # Use the prompt content to drive discovery, but still use our task structure
                base_queries = [
                    {"title": "台股熱門題材", "items": ["台股 近期熱門題材 2026", "Taiwan stock hot themes sectors 2026"]},
                    {"title": "AI與半導體", "items": ["AI伺服器 GB200 HBM 供應鏈 2026", "AI server supply chain Taiwan semiconductor"]},
                    {"title": "PCB與CCL材料", "items": ["PCB CCL 銅箔 玻纖 供應鏈", "printed circuit board laminate material Taiwan"]},
                    {"title": "散熱與電源", "items": ["散熱 液冷 電源供應器 AI伺服器", "thermal management power supply AI Taiwan"]},
                    {"title": "機器人與自動化", "items": ["機器人 自動化 人形機器人 供應鏈", "robot automation humanoid Taiwan supply chain"]},
                    {"title": "車用電子", "items": ["車用電子 電動車 第三類半導體", "automotive electronics EV Taiwan supply chain"]},
                    {"title": "政策與綠能", "items": ["台灣 政策受惠 綠能 儲能 電網", "Taiwan policy beneficiary green energy storage grid"]},
                    {"title": "法人與財經媒體", "items": ["法人買超 三大法人 題材 2026", "institutional investor sector rotation Taiwan media"]},
                ]
                deep_extra = [
                    {"title": "台股供應鏈全景", "items": ["台灣供應鏈 AI 半導體 電子 代工", "Taiwan supply chain AI semiconductor electronics"]},
                    {"title": "先進封裝與HBM", "items": ["CoWoS SoIC HBM 先進封裝 良率", "CoWoS SoIC HBM advanced packaging yield Taiwan"]},
                    {"title": "國際大廠動態", "items": ["NVIDIA AMD Intel AI 產品 供應鏈變化", "NVIDIA AMD Intel AI product supply chain change"]},
                    {"title": "題材風險與退燒", "items": ["題材退燒 過熱 風險 營收連結不足", "theme cooling risk weak revenue link Taiwan"]},
                    {"title": "軍工與水下電纜", "items": ["軍工 國防 潛在商機 台灣", "military defense submarine cable Taiwan opportunity"]},
                ]
            except Exception:
                discovery_text = ""
                base_queries = [
                    {"title": "台股熱門題材", "items": ["台股 近期熱門題材 2026", "Taiwan stock hot themes sectors 2026"]},
                    {"title": "AI與半導體", "items": ["AI伺服器 GB200 HBM 供應鏈 2026", "AI server supply chain Taiwan semiconductor"]},
                    {"title": "PCB與CCL材料", "items": ["PCB CCL 銅箔 玻纖 供應鏈", "printed circuit board laminate material Taiwan"]},
                    {"title": "散熱與電源", "items": ["散熱 液冷 電源供應器 AI伺服器", "thermal management power supply AI Taiwan"]},
                    {"title": "機器人與自動化", "items": ["機器人 自動化 人形機器人 供應鏈", "robot automation humanoid Taiwan supply chain"]},
                    {"title": "車用電子", "items": ["車用電子 電動車 第三類半導體", "automotive electronics EV Taiwan supply chain"]},
                    {"title": "政策與綠能", "items": ["台灣 政策受惠 綠能 儲能 電網", "Taiwan policy beneficiary green energy storage grid"]},
                    {"title": "法人與財經媒體", "items": ["法人買超 三大法人 題材 2026", "institutional investor sector rotation Taiwan media"]},
                ]
                deep_extra = [
                    {"title": "台股供應鏈全景", "items": ["台灣供應鏈 AI 半導體 電子 代工", "Taiwan supply chain AI semiconductor electronics"]},
                    {"title": "先進封裝與HBM", "items": ["CoWoS SoIC HBM 先進封裝 良率", "CoWoS SoIC HBM advanced packaging yield Taiwan"]},
                    {"title": "國際大廠動態", "items": ["NVIDIA AMD Intel AI 產品 供應鏈變化", "NVIDIA AMD Intel AI product supply chain change"]},
                    {"title": "題材風險與退燒", "items": ["題材退燒 過熱 風險 營收連結不足", "theme cooling risk weak revenue link Taiwan"]},
                    {"title": "軍工與水下電纜", "items": ["軍工 國防 潛在商機 台灣", "military defense submarine cable Taiwan opportunity"]},
                ]
        else:
            discovery_text = ""
            base_queries = [
                {"title": "台股熱門題材", "items": ["台股 近期熱門題材 2026", "Taiwan stock hot themes sectors 2026"]},
                {"title": "AI與半導體", "items": ["AI伺服器 GB200 HBM 供應鏈 2026", "AI server supply chain Taiwan semiconductor"]},
                {"title": "PCB與CCL材料", "items": ["PCB CCL 銅箔 玻纖 供應鏈", "printed circuit board laminate material Taiwan"]},
                {"title": "散熱與電源", "items": ["散熱 液冷 電源供應器 AI伺服器", "thermal management power supply AI Taiwan"]},
                {"title": "機器人與自動化", "items": ["機器人 自動化 人形機器人 供應鏈", "robot automation humanoid Taiwan supply chain"]},
                {"title": "車用電子", "items": ["車用電子 電動車 第三類半導體", "automotive electronics EV Taiwan supply chain"]},
                {"title": "政策與綠能", "items": ["台灣 政策受惠 綠能 儲能 電網", "Taiwan policy beneficiary green energy storage grid"]},
                {"title": "法人與財經媒體", "items": ["法人買超 三大法人 題材 2026", "institutional investor sector rotation Taiwan media"]},
            ]
            deep_extra = [
                {"title": "台股供應鏈全景", "items": ["台灣供應鏈 AI 半導體 電子 代工", "Taiwan supply chain AI semiconductor electronics"]},
                {"title": "先進封裝與HBM", "items": ["CoWoS SoIC HBM 先進封裝 良率", "CoWoS SoIC HBM advanced packaging yield Taiwan"]},
                {"title": "國際大廠動態", "items": ["NVIDIA AMD Intel AI 產品 供應鏈變化", "NVIDIA AMD Intel AI product supply chain change"]},
                {"title": "題材風險與退燒", "items": ["題材退燒 過熱 風險 營收連結不足", "theme cooling risk weak revenue link Taiwan"]},
                {"title": "軍工與水下電纜", "items": ["軍工 國防 潛在商機 台灣", "military defense submarine cable Taiwan opportunity"]},
            ]

        all_tasks: list[dict[str, Any]] = []
        if focus_theme:
            all_tasks.append({
                "label": f"聚焦題材：{focus_theme}",
                "objective": (
                    f"聚焦研究「{focus_theme}」的台股代表公司、產品、客戶、營收曝險、"
                    "供應鏈層級、受惠邏輯、反證與資料缺口。"
                ),
                "exclude": ["只列股價上漲但沒有產品或證據的名單", "社群傳言單獨支撐高信心"],
                "queries": [{
                    "title": f"{focus_theme} 題材公司與供應鏈",
                    "items": [
                        f"{focus_theme} 台股 代表股 供應鏈 產品 客戶 營收 法說",
                        f"{focus_theme} Taiwan stocks supply chain products customers revenue",
                        f"{focus_theme} 受惠股 反證 風險 庫存 訂單 毛利",
                    ],
                }],
            })
        for group in base_queries:
            all_tasks.append({
                "label": group.get("title", ""),
                "objective": f"請尋找 {group.get('title', '相關')} 的最新資訊與證據。",
                "exclude": ["個股買賣建議", "無來源的題材傳聞"],
                "queries": [group],
            })
        if is_deep:
            for group in deep_extra:
                all_tasks.append({
                    "label": group.get("title", ""),
                    "objective": f"請尋找 {group.get('title', '相關')} 的最新資訊與證據。",
                    "exclude": ["個股買賣建議", "無來源的題材傳聞"],
                    "queries": [group],
                })
        if is_initial:
            all_tasks.insert(0, {
                "label": "全市場產業輪動",
                "objective": "請尋找台股各產業輪動趨勢、近期強弱勢族群與資金流向。",
                "exclude": ["個股買賣建議", "無來源的題材傳聞"],
                "queries": [{"title": "產業輪動", "items": ["台股 類股輪動 資金流向 2026", "Taiwan sector rotation fund flow 2026"]}],
            })
        # Inject topic_discovery_search.md content into each task's objective
        if discovery_text:
            for task in all_tasks:
                task["objective"] = f"[主題探索指引]\n{discovery_text[:500]}\n\n{task.get('objective', '')}"
        # Add preferred-source site: queries for topic_maintain (limit to avoid query explosion)
        MAX_SITE_PER_TASK = 3
        for task in all_tasks:
            existing_items: list[str] = []
            queries = task.get("queries") or []
            for group in queries:
                if isinstance(group, dict):
                    existing_items.extend(group.get("items", []))
                elif isinstance(group, str):
                    existing_items.append(group)
            added = 0
            for base_query in existing_items[:2]:
                if added >= MAX_SITE_PER_TASK:
                    break
                site_qs = build_site_queries(base_query, max_domains=MAX_SITE_PER_TASK)
                for sq in site_qs:
                    if added >= MAX_SITE_PER_TASK:
                        break
                    queries.append(sq)
                    added += 1
        return all_tasks
    if request.command == "news":
        # Use Taiwan-finance-specific queries from news_service
        from .news_service import build_news_discovery_queries
        period = "7d" if (request.target or "").strip() == "7d" else "latest"
        news_tasks = build_news_discovery_queries(period)
        if news_tasks:
            return news_tasks
        # Fallback if news_service is unavailable
        return [{
            "label": "台股財經新聞",
            "objective": "請尋找最新台股、台灣財經、股票、產業相關新聞。",
            "exclude": ["個股買賣建議", "無來源傳聞", "字典頁"],
            "queries": [
                {"title": "台股重點新聞", "items": ["台股 今日 重點 新聞", "Taiwan stock market news today"]},
                {"title": "AI與半導體", "items": ["AI 半導體 台股 新聞", "Taiwan AI semiconductor stock news"]},
                {"title": "金融與高股息", "items": ["金融 高股息 台股 新聞", "Taiwan financial high dividend stock news"]},
                {"title": "政策與總經", "items": ["台灣 政策 總經 利率 匯率", "Taiwan macro policy interest rate exchange rate stock"]},
            ],
        }]
    return [{"label": "一般搜尋", "objective": _discovery_rules(request), "exclude": [], "queries": [{"title": "一般搜尋", "items": [str(target)]}]}]


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
        scoring.append("股票量化評分標準.md")
        scoring.append("股票標籤重估模型.md")
    if request.command == "value_scan":
        scoring.append("股票標籤重估模型.md")
        scoring.append("股票量化評分標準.md")
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
        supplements.append("Brief 模式補充要求：仍可產出本地報告檔，但 Telegram 摘要必須更短，只保留市場總結、風險等級、持股水位與 3 個觀察重點。")
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

def _scoring_rules_for_request(request: CommandRequest) -> str:
    blocks: list[str] = []
    if request.command == "research" and request.mode in {"score", "deep"}:
        blocks.append("## 股票量化評分標準原稿\n" + _read_scoring("股票量化評分標準.md"))
        blocks.append("## 股票標籤重估模型原稿\n" + _read_scoring("股票標籤重估模型.md"))
    elif request.command == "value_scan":
        blocks.append("## 股票標籤重估模型原稿\n" + _read_scoring("股票標籤重估模型.md"))
        blocks.append("## 股票量化評分標準中與重估相關的原稿\n" + _read_scoring("股票量化評分標準.md"))
    else:
        blocks.append("本模式不要求完整量化評分；若資料不足，不得自行給分。")
    return "\n\n".join(blocks)[:SCORING_RULES_CHAR_LIMIT]


def _rules_for_request(request: CommandRequest) -> str:
    """Load prompt rules files based on command and mode."""
    blocks: list[str] = []
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
    return "評分原稿檔案不存在，該項不得高分。"


def _source_text(source_list: list[SourceItem]) -> str:
    if not source_list:
        return "目前沒有外部來源。"
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
