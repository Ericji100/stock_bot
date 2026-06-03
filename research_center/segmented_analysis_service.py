from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from .models import CommandRequest, SourceItem
from .prompt_logging import write_prompt_log

ProgressCallback = Callable[[str], None]

THEME_ANALYSIS_COMMANDS = {"theme_radar", "theme_flow", "sector_strength"}
SEGMENTED_ANALYSIS_PROMPT_THRESHOLD = 120_000
SEGMENTED_ANALYSIS_TARGET_CHARS = 120_000
SEGMENTED_ANALYSIS_HARD_CHARS = 180_000


class ReportGenerator(Protocol):
    def generate_report(self, prompt: str) -> Any:
        ...


@dataclass(frozen=True)
class SegmentRun:
    label: str
    title: str
    status: str
    prompt_chars: int
    prompt_path: str = ""
    output_chars: int = 0
    markdown: str = ""
    error: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SegmentedAnalysisResult:
    markdown: str
    raw: dict[str, Any]
    diagnostics: dict[str, Any]
    prompt_paths: list[str]
    segment_runs: list[SegmentRun]


def should_use_segmented_analysis(
    request: CommandRequest,
    selected_ai_model: str = "",
    *,
    prompt_chars: int | None = None,
    threshold_chars: int = SEGMENTED_ANALYSIS_PROMPT_THRESHOLD,
) -> bool:
    if request.command not in THEME_ANALYSIS_COMMANDS:
        return False
    if prompt_chars is None:
        return False
    return prompt_chars >= threshold_chars


def run_segmented_theme_analysis(
    *,
    request: CommandRequest,
    structured_data: dict[str, Any],
    sources: list[SourceItem],
    ai_client: ReportGenerator,
    model_name: str,
    trigger: str = "prompt_size",
    original_prompt_chars: int | None = None,
    threshold_chars: int = SEGMENTED_ANALYSIS_PROMPT_THRESHOLD,
    progress: ProgressCallback | None = None,
) -> SegmentedAnalysisResult:
    """Run market-theme commands through multiple smaller AI calls.

    Full structured data remains in the caller and report JSON. This service
    only builds task-focused prompt slices so providers with smaller context
    windows do not receive the entire local data pack at once.
    """

    plans = _segment_plans(request, structured_data)
    segment_runs: list[SegmentRun] = []
    prompt_paths: list[str] = []
    outputs: list[dict[str, Any]] = []

    _emit(progress, f"分段 AI 分析啟動：{len(plans)} 個分析段，model={model_name}")
    for index, plan in enumerate(plans, 1):
        prompt = _build_segment_prompt(request, structured_data, plan, outputs)
        segment_sources = _sources_for_segment_plan(plan, sources)
        prompt_path = write_prompt_log(
            request,
            prompt,
            model_name,
            False,
            segment_sources,
            {
                **(structured_data.get("prompt_policy") or {}),
                "purpose": "segmented_theme_analysis",
                "segment_label": plan["label"],
                "segment_index": index,
                "segment_total": len(plans),
                "prompt_chars": len(prompt),
                "estimated_tokens": max(1, len(prompt) // 4),
                "source_count": len(segment_sources),
            },
        )
        prompt_paths.append(str(prompt_path))
        _emit(progress, f"分段 AI {index}/{len(plans)}：{plan['title']}，prompt={len(prompt)} chars")
        try:
            result = ai_client.generate_report(prompt)
            markdown = str(getattr(result, "markdown", "") or "").strip()
            diagnostics = dict(getattr(result, "diagnostics", {}) or {})
            run = SegmentRun(
                label=plan["label"],
                title=plan["title"],
                status="success",
                prompt_chars=len(prompt),
                prompt_path=str(prompt_path),
                output_chars=len(markdown),
                markdown=markdown,
                diagnostics=diagnostics,
            )
            outputs.append(_segment_output(plan, markdown, run))
            _emit(progress, f"分段 AI 完成：{plan['title']}，output={len(markdown)} chars")
        except Exception as exc:
            fallback = _segment_local_fallback(plan, structured_data, exc)
            run = SegmentRun(
                label=plan["label"],
                title=plan["title"],
                status="fallback",
                prompt_chars=len(prompt),
                prompt_path=str(prompt_path),
                output_chars=len(fallback),
                markdown=fallback,
                error=str(exc),
            )
            outputs.append(_segment_output(plan, fallback, run))
            _emit(progress, f"分段 AI 失敗，改用本地段落摘要：{plan['title']}，原因：{exc}")
        segment_runs.append(run)

    final_sources = _sources_for_segment_outputs(outputs, sources)
    final_prompt = _build_final_prompt(request, structured_data, outputs, final_sources)
    final_prompt_path = write_prompt_log(
        request,
        final_prompt,
        model_name,
        False,
        final_sources,
        {
            **(structured_data.get("prompt_policy") or {}),
            "purpose": "segmented_theme_final_report",
            "segment_count": len(segment_runs),
            "prompt_chars": len(final_prompt),
            "estimated_tokens": max(1, len(final_prompt) // 4),
            "source_count": len(final_sources),
        },
    )
    prompt_paths.append(str(final_prompt_path))
    _emit(progress, f"分段 AI 最終整合：prompt={len(final_prompt)} chars")
    try:
        final_result = ai_client.generate_report(final_prompt)
        markdown = str(getattr(final_result, "markdown", "") or "").strip()
        final_diagnostics = dict(getattr(final_result, "diagnostics", {}) or {})
        raw = dict(getattr(final_result, "raw", {}) or {})
        final_status = "success"
        final_error = None
        _emit(progress, f"分段 AI 最終整合完成：output={len(markdown)} chars")
    except Exception as exc:
        markdown = _compose_segmented_fallback_report(request, outputs, exc)
        final_diagnostics = {"status": "fallback", "error": str(exc)}
        raw = {}
        final_status = "fallback"
        final_error = str(exc)
        _emit(progress, f"分段 AI 最終整合失敗，改用分段摘要組報告：{exc}")

    diagnostics = {
        "mode": "segmented_theme_analysis",
        "model": model_name,
        "command": request.command,
        "trigger": trigger,
        "original_prompt_chars": original_prompt_chars,
        "threshold_chars": threshold_chars,
        "segment_count": len(segment_runs),
        "success_count": sum(1 for item in segment_runs if item.status == "success"),
        "fallback_count": sum(1 for item in segment_runs if item.status != "success"),
        "final_status": final_status,
        "final_error": final_error,
        "prompt_paths": prompt_paths,
        "segment_runs": [_run_to_metadata(item) for item in segment_runs],
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    return SegmentedAnalysisResult(
        markdown=markdown,
        raw=raw,
        diagnostics=diagnostics,
        prompt_paths=prompt_paths,
        segment_runs=segment_runs,
    )


def _segment_plans(request: CommandRequest, data: dict[str, Any]) -> list[dict[str, Any]]:
    if request.command == "sector_strength":
        return [
            *_market_strength_plans(data),
            {"label": "sector_subsector", "title": "族群與子族群整合", "payload": _sector_payload(data)},
        ]
    if request.command == "theme_flow":
        return _theme_flow_plans(data)
    return [
        *_market_strength_plans(data),
        *_theme_evidence_plans(data),
        {"label": "extension_path", "title": "題材擴散與下一層候選", "payload": _radar_flow_payload(data)},
    ]


def _build_segment_prompt(
    request: CommandRequest,
    data: dict[str, Any],
    plan: dict[str, Any],
    prior_outputs: list[dict[str, Any]],
) -> str:
    payload = _compact_segment_payload(plan.get("payload") or {})
    return "\n".join(
        [
            "# 台股族群題材分段分析",
            "",
            f"指令：/{request.command}",
            f"分析日期：{data.get('report_date') or request.report_date or 'latest'}",
            f"目前段落：{plan.get('title')}",
            "",
            "請嚴格遵守：",
            "- 只能根據本段資料與前段結論判斷，不要憑空新增公司或題材。",
            "- 產業/子族群強勢可以作為市場線索，但不能直接說成 verified 題材證據。",
            "- 若資料不足，請明確標示「市場強勢、題材證據待補」或「資料不足」。",
            "- 不得輸出買進、賣出、目標價、保證獲利、必漲等投資指令。",
            "- 請用 Markdown，並輸出精簡但可直接合併進正式報告的分析段落。",
            "",
            "前段結論：",
            _json(_compact(prior_outputs, depth=3, max_list=6, max_keys=40, max_string=1800)),
            "",
            "本段資料：",
            _json(payload),
        ]
    )


def _build_final_prompt(
    request: CommandRequest,
    data: dict[str, Any],
    outputs: list[dict[str, Any]],
    sources: list[SourceItem],
) -> str:
    return "\n".join(
        [
            "# 台股族群題材正式報告整合",
            "",
            f"指令：/{request.command}",
            f"分析日期：{data.get('report_date') or request.report_date or 'latest'}",
            "",
            "請根據下列分段結論，整合成一份正式 Markdown 報告。",
            "",
            "硬規則：",
            "- 必須分開說明「市場強弱族群」與「題材庫證據映射」。",
            "- 不得預設 AI、半導體、伺服器為主線；以分段市場結論為準。",
            "- 若某族群市場很強但題材庫證據不足，要標示「市場強勢、題材證據待補」。",
            "- 代表股只能來自 verified/inferred；candidate 只能稱為觀察名單。",
            "- 請保留題材擴散推論，但推論要標示依據與待驗證點。",
            "- 不得輸出買進、賣出、加碼、追價、停損、停利、目標價、保證獲利。",
            "",
            "本地摘要資料：",
            _json(_final_local_summary(data)),
            "",
            "分段分析結果：",
            _json(_compact(outputs, depth=4, max_list=8, max_keys=80, max_string=6000)),
            "",
            "可引用來源清單：",
            _json(_source_refs(sources)),
        ]
    )


def _segment_output(plan: dict[str, Any], markdown: str, run: SegmentRun) -> dict[str, Any]:
    return {
        "label": plan.get("label"),
        "title": plan.get("title"),
        "status": run.status,
        "error": run.error,
        "markdown": markdown,
    }


def _segment_local_fallback(plan: dict[str, Any], data: dict[str, Any], exc: Exception) -> str:
    payload = plan.get("payload") or {}
    return "\n".join(
        [
            f"## {plan.get('title')}",
            "",
            f"本段 AI 分析失敗，已保留本地資料摘要。原因：{exc}",
            "",
            "```json",
            _json(_compact(payload, depth=3, max_list=10, max_keys=60, max_string=1200)),
            "```",
        ]
    )


def _compose_segmented_fallback_report(request: CommandRequest, outputs: list[dict[str, Any]], exc: Exception) -> str:
    title = {
        "theme_radar": "市場題材雷達與族群強弱分析",
        "theme_flow": "題材擴散路徑分析",
        "sector_strength": "族群強弱排行",
    }.get(request.command, "族群題材分析")
    lines = [
        f"# {title}",
        "",
        f"最終 AI 整合失敗，以下保留分段分析結果。原因：{exc}",
        "",
    ]
    for output in outputs:
        lines.extend([f"## {output.get('title')}", "", str(output.get("markdown") or "資料不足"), ""])
    return "\n".join(lines).strip() + "\n"


def _market_strength_plans(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"label": "market_price_rankings", "title": "市場漲跌與量能排行", "payload": _market_price_payload(data)},
        {"label": "market_sector_movers", "title": "全市場產業排行", "payload": _market_sector_mover_payload(data)},
        {"label": "sector_strength", "title": "族群強弱排行", "payload": _market_sector_strength_payload(data)},
        {"label": "subsector_strength", "title": "子族群強弱排行", "payload": _market_subsector_strength_payload(data)},
    ]


def _theme_evidence_plans(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"label": "theme_rankings", "title": "題材排行與證據分級", "payload": _theme_rankings_payload(data)},
        {"label": "theme_strong_stocks", "title": "強勢股題材命中", "payload": _theme_strong_stocks_payload(data)},
        {"label": "theme_news_stats", "title": "新聞趨勢與題材熱度", "payload": _theme_news_payload(data)},
    ]


def _theme_flow_plans(data: dict[str, Any]) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = [
        {"label": "theme_flow_profile", "title": "題材概況與資料品質", "payload": _theme_flow_profile_payload(data)}
    ]
    related = data.get("related_stocks") or []
    related_chunks = _chunked(related, 30) or [[]]
    for index, chunk in enumerate(related_chunks, 1):
        plans.append({
            "label": f"theme_flow_related_stocks_{index}",
            "title": f"相關股票分批分析 {index}/{len(related_chunks)}",
            "payload": _theme_flow_related_stocks_payload(data, chunk, index, len(related_chunks)),
        })
    layers = data.get("layers") or []
    layer_chunks = _chunked(layers, 2) or [[]]
    for index, chunk in enumerate(layer_chunks, 1):
        plans.append({
            "label": f"theme_flow_layers_{index}",
            "title": f"供應鏈層級分批分析 {index}/{len(layer_chunks)}",
            "payload": _theme_flow_layers_payload(data, chunk, index, len(layer_chunks)),
        })
    plans.extend([
        {"label": "theme_flow_market_validation", "title": "供應鏈層級盤面驗證", "payload": _theme_flow_validation_payload(data)},
        {"label": "theme_flow_next_candidates", "title": "下一層受惠候選", "payload": _theme_flow_next_candidates_payload(data)},
        {"label": "theme_flow_news_stats", "title": "題材新聞趨勢", "payload": _theme_flow_news_payload(data)},
    ])
    return plans


def _market_price_payload(data: dict[str, Any]) -> dict[str, Any]:
    market = data.get("market_movers") or {}
    return {
        "market_movers": {
            "market_data_date": market.get("market_data_date"),
            "source_mode": market.get("source_mode"),
            "top_gainers": _stocks(market.get("top_gainers"), 40),
            "top_losers": _stocks(market.get("top_losers"), 30),
            "top_volume_surge": _stocks(market.get("top_volume_surge"), 40),
            "top_turnover": _stocks(market.get("top_turnover"), 40),
            "top_trend_strength": _stocks(market.get("top_trend_strength"), 40),
            "new_highs": _stocks(market.get("new_highs"), 40),
            "new_lows": _stocks(market.get("new_lows"), 30),
        },
        "data_quality": _compact(data.get("data_quality") or {}, depth=3, max_list=15, max_keys=40),
    }


def _market_sector_mover_payload(data: dict[str, Any]) -> dict[str, Any]:
    market = data.get("market_movers") or {}
    return {
        "market_data_date": market.get("market_data_date"),
        "sector_mover_rankings": _sector_mover_rows(market.get("sector_mover_rankings") or [], limit=40, sample_limit=3),
    }


def _market_sector_strength_payload(data: dict[str, Any]) -> dict[str, Any]:
    sector_data = data if data.get("command_role") == "sector_strength" else data.get("sector_strength") or {}
    return {
        "sector_rankings": _sector_ranking_rows(sector_data.get("sector_rankings") or data.get("sector_rankings") or [], limit=35, sample_limit=4),
        "analysis_policy": data.get("analysis_policy"),
    }


def _market_subsector_strength_payload(data: dict[str, Any]) -> dict[str, Any]:
    sector_data = data if data.get("command_role") == "sector_strength" else data.get("sector_strength") or {}
    return {
        "subsector_rankings": _subsector_ranking_rows(sector_data.get("subsector_rankings") or data.get("subsector_rankings") or [], limit=45, sample_limit=4),
        "analysis_policy": data.get("analysis_policy"),
    }


def _market_payload(data: dict[str, Any]) -> dict[str, Any]:
    market = data.get("market_movers") or {}
    sector_data = data if data.get("command_role") == "sector_strength" else data.get("sector_strength") or {}
    return {
        "market_movers": {
            "market_data_date": market.get("market_data_date"),
            "source_mode": market.get("source_mode"),
            "top_gainers": _stocks(market.get("top_gainers"), 30),
            "top_losers": _stocks(market.get("top_losers"), 20),
            "top_volume_surge": _stocks(market.get("top_volume_surge"), 30),
            "top_turnover": _stocks(market.get("top_turnover"), 30),
            "new_highs": _stocks(market.get("new_highs"), 30),
            "new_lows": _stocks(market.get("new_lows"), 20),
            "sector_mover_rankings": _sector_mover_rows(market.get("sector_mover_rankings") or [], limit=30, sample_limit=3),
        },
        "sector_rankings": _sector_ranking_rows(sector_data.get("sector_rankings") or data.get("sector_rankings") or [], limit=30, sample_limit=3),
        "subsector_rankings": _subsector_ranking_rows(sector_data.get("subsector_rankings") or data.get("subsector_rankings") or [], limit=40, sample_limit=3),
        "data_quality": _compact(data.get("data_quality") or sector_data.get("data_quality") or {}, depth=3, max_list=20, max_keys=60),
    }


def _sector_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "sector_rankings": _sector_ranking_rows(data.get("sector_rankings") or [], limit=35, sample_limit=4),
        "subsector_rankings": _subsector_ranking_rows(data.get("subsector_rankings") or [], limit=45, sample_limit=4),
        "analysis_policy": data.get("analysis_policy"),
    }


def _theme_rankings_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "theme_rankings": _theme_ranking_rows(data.get("theme_rankings") or [], limit=25, sample_limit=2),
        "topic_library_summary": data.get("topic_library_summary"),
        "analysis_policy": data.get("analysis_policy"),
    }


def _theme_strong_stocks_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "strong_stocks": _stocks(data.get("strong_stocks"), 40),
        "analysis_policy": data.get("analysis_policy"),
    }


def _theme_news_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "news_theme_stats": _compact(data.get("news_theme_stats") or [], depth=4, max_list=30, max_keys=60, max_string=600),
        "topic_library_summary": data.get("topic_library_summary"),
        "analysis_policy": data.get("analysis_policy"),
    }


def _theme_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        **_theme_rankings_payload(data),
        **_theme_strong_stocks_payload(data),
        **_theme_news_payload(data),
        "analysis_policy": data.get("analysis_policy"),
    }


def _radar_flow_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "theme_flow_summaries": _compact(data.get("theme_flow_summaries") or [], depth=5, max_list=6, max_keys=80, max_string=1200),
        "theme_rankings": _theme_ranking_rows(data.get("theme_rankings") or [], limit=10, sample_limit=2),
        "subsector_rankings": _subsector_ranking_rows(data.get("subsector_rankings") or [], limit=20, sample_limit=2),
    }


def _theme_flow_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        **_theme_flow_profile_payload(data),
        "related_stocks": _stocks(data.get("related_stocks"), 60),
        "news_stats": _compact(data.get("news_stats") or [], depth=4, max_list=30, max_keys=60, max_string=600),
    }


def _flow_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "layers": _theme_flow_layer_rows(data.get("layers") or [], limit=8, sample_limit=4),
        "layer_market_validation": _compact(data.get("layer_market_validation") or [], depth=4, max_list=20, max_keys=60, max_string=700),
        "next_layer_candidates": _compact(data.get("next_layer_candidates") or [], depth=4, max_list=30, max_keys=60, max_string=700),
    }


def _theme_flow_profile_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "theme_query": data.get("theme_query"),
        "theme": _compact(data.get("theme") or {}, depth=4, max_list=12, max_keys=50, max_string=700),
        "related_stock_count": data.get("related_stock_count"),
        "market_data_date": data.get("market_data_date"),
        "lookback_days": data.get("lookback_days"),
        "data_quality": _compact(data.get("data_quality") or {}, depth=4, max_list=20, max_keys=50, max_string=700),
        "analysis_policy": data.get("analysis_policy"),
        "data_coverage": _compact(data.get("data_coverage") or {}, depth=3, max_list=12, max_keys=40, max_string=500),
        "feature_pack": _compact(data.get("feature_pack") or {}, depth=3, max_list=12, max_keys=40, max_string=500),
    }


def _theme_flow_related_stocks_payload(
    data: dict[str, Any],
    rows: list[Any],
    index: int,
    total: int,
) -> dict[str, Any]:
    return {
        "theme_query": data.get("theme_query"),
        "chunk_index": index,
        "chunk_total": total,
        "related_stock_count": data.get("related_stock_count"),
        "related_stocks": _stocks(rows, 30),
        "analysis_policy": data.get("analysis_policy"),
    }


def _theme_flow_layers_payload(
    data: dict[str, Any],
    rows: list[Any],
    index: int,
    total: int,
) -> dict[str, Any]:
    return {
        "theme_query": data.get("theme_query"),
        "chunk_index": index,
        "chunk_total": total,
        "layers": _theme_flow_layer_rows(rows, limit=2, sample_limit=5),
        "analysis_policy": data.get("analysis_policy"),
    }


def _theme_flow_validation_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "theme_query": data.get("theme_query"),
        "market_data_date": data.get("market_data_date"),
        "layer_market_validation": _compact(data.get("layer_market_validation") or [], depth=4, max_list=30, max_keys=60, max_string=700),
        "market_movers": _theme_flow_market_snapshot(data.get("market_movers") or {}),
        "sector_rankings": _sector_ranking_rows(data.get("sector_rankings") or [], limit=15, sample_limit=3),
        "subsector_rankings": _subsector_ranking_rows(data.get("subsector_rankings") or [], limit=20, sample_limit=3),
    }


def _theme_flow_next_candidates_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "theme_query": data.get("theme_query"),
        "next_layer_candidates": _compact(data.get("next_layer_candidates") or [], depth=4, max_list=40, max_keys=60, max_string=700),
        "layers_summary": _theme_flow_layer_rows(data.get("layers") or [], limit=8, sample_limit=2),
    }


def _theme_flow_news_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "theme_query": data.get("theme_query"),
        "news_stats": _compact(data.get("news_stats") or [], depth=4, max_list=40, max_keys=60, max_string=700),
        "news_context": _compact(data.get("news_context") or {}, depth=3, max_list=15, max_keys=50, max_string=600),
    }


def _theme_flow_market_snapshot(market: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_data_date": market.get("market_data_date"),
        "source_mode": market.get("source_mode"),
        "top_gainers": _stocks(market.get("top_gainers"), 15),
        "top_volume_surge": _stocks(market.get("top_volume_surge"), 15),
        "top_turnover": _stocks(market.get("top_turnover"), 15),
        "new_highs": _stocks(market.get("new_highs"), 15),
    }


def _sector_mover_rows(rows: Any, *, limit: int, sample_limit: int) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    scalar_keys = (
        "sector",
        "sector_display_name",
        "sector_score",
        "stock_count",
        "advancers",
        "decliners",
        "avg_change_pct",
        "median_change_pct",
        "volume_surge_count",
        "new_high_count",
        "new_low_count",
        "limit_up_count",
        "limit_down_count",
        "turnover_sum",
    )
    list_keys = ("top_gainers", "top_losers", "top_volume_surge", "top_turnover")
    result = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        item = {key: row.get(key) for key in scalar_keys if row.get(key) not in (None, "", [])}
        for key in list_keys:
            samples = _stocks(row.get(key), sample_limit)
            if samples:
                item[key] = samples
        result.append(item)
    return result


def _sector_ranking_rows(rows: Any, *, limit: int, sample_limit: int) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    scalar_keys = (
        "sector",
        "sector_display_name",
        "sector_score",
        "strong_stock_count",
        "avg_change_pct",
        "volume_surge_count",
        "new_high_count",
        "active_breakout_count",
        "trend_pullback_count",
        "avg_trend_score",
        "sector_state",
        "limit_up_count",
        "avg_volume_20d",
        "theme_hit_count",
        "theme_relation_status_counts",
        "representative_policy",
        "interpretation_hint",
    )
    result = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        item = {key: row.get(key) for key in scalar_keys if row.get(key) not in (None, "", [])}
        for key in ("sector_strong_samples", "representative_stocks", "candidate_stocks"):
            samples = _stocks(row.get(key), sample_limit)
            if samples:
                item[key] = samples
        top_subsectors = _subsector_ranking_rows(row.get("top_subsectors") or [], limit=5, sample_limit=2)
        if top_subsectors:
            item["top_subsectors"] = top_subsectors
        result.append(item)
    return result


def _subsector_ranking_rows(rows: Any, *, limit: int, sample_limit: int) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    scalar_keys = (
        "sector",
        "sector_display_name",
        "subsector",
        "subsector_score",
        "strong_stock_count",
        "avg_change_pct",
        "volume_surge_count",
        "new_high_count",
        "active_breakout_count",
        "trend_pullback_count",
        "avg_trend_score",
        "subsector_state",
        "limit_up_count",
        "avg_volume_20d",
        "theme_hit_count",
        "interpretation_hint",
    )
    result = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        item = {key: row.get(key) for key in scalar_keys if row.get(key) not in (None, "", [])}
        samples = _stocks(row.get("strong_samples"), sample_limit)
        if samples:
            item["strong_samples"] = samples
        result.append(item)
    return result


def _theme_ranking_rows(rows: Any, *, limit: int, sample_limit: int) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    scalar_keys = (
        "theme_id",
        "theme_name",
        "theme_strength_score",
        "lifecycle",
        "theme_state",
        "active_breakout_count",
        "trend_pullback_count",
        "weak_count",
        "avg_trend_score",
        "score_breakdown",
        "strong_stock_count",
        "weighted_strong_stock_count",
        "direct_relation_count",
        "inferred_relation_count",
        "candidate_relation_count",
        "representative_policy",
        "news_stats",
        "main_risks",
    )
    result = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        item = {key: row.get(key) for key in scalar_keys if row.get(key) not in (None, "", [])}
        if item.get("score_breakdown"):
            item["score_breakdown"] = _compact(item["score_breakdown"], depth=2, max_list=8, max_keys=20)
        if item.get("news_stats"):
            item["news_stats"] = _compact(item["news_stats"], depth=3, max_list=8, max_keys=24, max_string=300)
        if item.get("main_risks"):
            item["main_risks"] = _compact(item["main_risks"], depth=2, max_list=4, max_keys=12, max_string=300)
        strong_nodes = _compact(row.get("strong_nodes") or [], depth=2, max_list=8, max_keys=20, max_string=300)
        if strong_nodes:
            item["strong_nodes"] = strong_nodes
        for key in ("representative_stocks", "candidate_stocks"):
            samples = _stocks(row.get(key), sample_limit)
            if samples:
                item[key] = samples
        display_groups = row.get("display_stock_groups")
        if isinstance(display_groups, dict):
            item["display_stock_groups"] = {
                "verified_representatives": _stocks(display_groups.get("verified_representatives"), sample_limit),
                "inferred_representatives": _stocks(display_groups.get("inferred_representatives"), sample_limit),
                "candidate_watchlist": _stocks(display_groups.get("candidate_watchlist"), sample_limit),
                "candidate_label": display_groups.get("candidate_label"),
                "required_terms": _compact(display_groups.get("required_terms") or {}, depth=2, max_list=6, max_keys=12, max_string=200),
            }
        result.append(item)
    return result


def _theme_flow_layer_rows(rows: Any, *, limit: int, sample_limit: int) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    scalar_keys = (
        "layer",
        "name",
        "current_strength",
        "stage",
        "inference",
        "verification_needed",
        "market_validated",
        "status",
        "avg_change_pct",
        "strong_stock_count",
        "volume_surge_count",
        "new_high_count",
        "theme_hit_count",
    )
    result = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        item = {key: row.get(key) for key in scalar_keys if row.get(key) not in (None, "", [])}
        item["nodes"] = _compact(row.get("nodes") or [], depth=3, max_list=12, max_keys=40, max_string=400)
        for key in ("representative_stocks", "candidate_stocks", "strong_samples"):
            samples = _stocks(row.get(key), sample_limit)
            if samples:
                item[key] = samples
        display_groups = row.get("display_stock_groups")
        if isinstance(display_groups, dict):
            item["display_stock_groups"] = {
                "verified_representatives": _stocks(display_groups.get("verified_representatives"), sample_limit),
                "inferred_representatives": _stocks(display_groups.get("inferred_representatives"), sample_limit),
                "candidate_watchlist": _stocks(display_groups.get("candidate_watchlist"), sample_limit),
                "candidate_label": display_groups.get("candidate_label"),
            }
        result.append(item)
    return result


def _final_local_summary(data: dict[str, Any]) -> dict[str, Any]:
    sector_data = data if data.get("command_role") == "sector_strength" else data.get("sector_strength") or {}
    return {
        "command_role": data.get("command_role"),
        "report_date": data.get("report_date"),
        "market_data_date": data.get("market_data_date") or sector_data.get("market_data_date"),
        "sector_rankings": _compact(sector_data.get("sector_rankings") or data.get("sector_rankings") or [], depth=3, max_list=12, max_keys=60),
        "subsector_rankings": _compact(sector_data.get("subsector_rankings") or data.get("subsector_rankings") or [], depth=3, max_list=15, max_keys=60),
        "theme_rankings": _compact(data.get("theme_rankings") or [], depth=3, max_list=10, max_keys=60),
        "data_quality": _compact(data.get("data_quality") or sector_data.get("data_quality") or {}, depth=3, max_list=15, max_keys=60),
    }


def _stocks(rows: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    keys = (
        "code",
        "name",
        "industry",
        "sector",
        "sector_display_name",
        "primary_subsector",
        "change_pct",
        "change_pct_5d",
        "change_pct_10d",
        "change_pct_20d",
        "volume_ratio",
        "turnover",
        "new_high_days",
        "new_low_days",
        "days_since_high",
        "near_high_20d",
        "pullback_from_high_pct",
        "above_ma5",
        "above_ma10",
        "above_ma20",
        "trend_score",
        "trend_state",
        "trend_summary",
        "avg_volume_20d",
        "latest_monthly_revenue",
        "revenue_yoy",
        "revenue_mom",
        "gross_margin",
        "operating_margin",
        "eps",
        "primary_theme_name",
        "theme_matches",
        "subsector_matches",
    )
    result = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        item = {key: row.get(key) for key in keys if row.get(key) not in (None, "", [])}
        if item.get("theme_matches"):
            item["theme_matches"] = _compact(item["theme_matches"], depth=3, max_list=2, max_keys=24, max_string=360)
        if item.get("subsector_matches"):
            item["subsector_matches"] = _compact(item["subsector_matches"], depth=3, max_list=2, max_keys=24, max_string=360)
        result.append(item)
    return result


def _source_refs(sources: list[SourceItem]) -> list[dict[str, Any]]:
    return [
        {
            "source_id": source.source_id,
            "title": source.title,
            "url": source.url,
            "source_level": source.source_level,
            "published_date": source.published_date,
            "provider": source.provider,
        }
        for source in sources[:30]
    ]


def _compact_segment_payload(payload: Any) -> Any:
    compact = _compact(payload, depth=4, max_list=45, max_keys=90, max_string=1200)
    if len(_json(compact)) <= SEGMENTED_ANALYSIS_TARGET_CHARS:
        return compact
    compact = _compact(payload, depth=3, max_list=28, max_keys=60, max_string=700)
    if len(_json(compact)) <= SEGMENTED_ANALYSIS_HARD_CHARS:
        return compact
    return _compact(payload, depth=3, max_list=18, max_keys=45, max_string=420)


def _sources_for_segment_plan(plan: dict[str, Any], sources: list[SourceItem]) -> list[SourceItem]:
    if not sources:
        return []
    text = _json(plan.get("payload") or {})
    ids = set(re.findall(r"S\d{3,}", text))
    if ids:
        matched = [item for item in sources if item.source_id in ids]
        if matched:
            return matched[:60]
    return sources[: min(25, len(sources))]


def _sources_for_segment_outputs(outputs: list[dict[str, Any]], sources: list[SourceItem]) -> list[SourceItem]:
    if not sources:
        return []
    text = _json(outputs)
    ids = set(re.findall(r"S\d{3,}", text))
    if ids:
        matched = [item for item in sources if item.source_id in ids]
        if matched:
            return matched[:80]
    return sources[: min(30, len(sources))]


def _compact(
    value: Any,
    *,
    depth: int = 4,
    max_list: int = 20,
    max_keys: int = 80,
    max_string: int = 1600,
) -> Any:
    if depth <= 0:
        if isinstance(value, (dict, list, tuple)):
            return f"<{type(value).__name__} truncated>"
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_keys:
                result["_truncated_keys"] = len(value) - max_keys
                break
            result[str(key)] = _compact(item, depth=depth - 1, max_list=max_list, max_keys=max_keys, max_string=max_string)
        return result
    if isinstance(value, (list, tuple)):
        items = [
            _compact(item, depth=depth - 1, max_list=max_list, max_keys=max_keys, max_string=max_string)
            for item in list(value)[:max_list]
        ]
        if len(value) > max_list:
            items.append({"_truncated_items": len(value) - max_list})
        return items
    if isinstance(value, str) and len(value) > max_string:
        return value[:max_string].rstrip() + "...<truncated>"
    return value


def _chunked(rows: Any, size: int) -> list[list[Any]]:
    if not isinstance(rows, list) or size <= 0:
        return []
    return [rows[index:index + size] for index in range(0, len(rows), size)]


def _run_to_metadata(run: SegmentRun) -> dict[str, Any]:
    return {
        "label": run.label,
        "title": run.title,
        "status": run.status,
        "prompt_chars": run.prompt_chars,
        "prompt_path": run.prompt_path,
        "output_chars": run.output_chars,
        "error": run.error,
        "diagnostics": run.diagnostics,
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=_json_default)


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)
