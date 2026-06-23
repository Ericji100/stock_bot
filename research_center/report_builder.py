from __future__ import annotations

import html
import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import CommandRequest, ReportArtifacts, SourceItem
from .convergence_service import build_report_convergence_layer
from .report_display_normalizer import normalize_report_text
from .report_validator import append_qa_notes, validate_report
from .company_knowledge_update_service import source_quality_score
from .report_quality_service import build_report_quality_layer
from .artifact_registry import build_artifact_record, register_artifact

DISCLAIMER = "本報告為研究輔助資訊，不構成任何投資建議。投資決策仍需自行判斷並承擔風險。"


def build_report_json(
    request: CommandRequest,
    markdown: str,
    summary: str,
    sources: list[SourceItem],
    ai_used: bool,
    fallback_reason: str | None,
    structured_data: dict[str, Any] | None = None,
    report_variant: str | None = None,
    report_id: str | None = None,
) -> dict[str, Any]:
    data = structured_data or {}
    convergence_layer = build_report_convergence_layer(
        request,
        data,
        sources,
        ai_used=ai_used,
        fallback_reason=fallback_reason,
        report_id=report_id,
        report_variant=report_variant,
    )
    shared_data_layer = _shared_data_layer_summary(data)
    report_quality = build_report_quality_layer(request, data, sources)
    local_scoring = data.get("local_scoring") or {}
    scores = _normalize_scores(local_scoring.get("scores") or [])
    buy_rating = local_scoring.get("buy_rating")
    ai_status = str(data.get("ai_status") or ("fallback_success" if fallback_reason else ("ai_success" if ai_used else "not_used")))
    return {
        "schema_version": "report_json_v2",
        "report_title": _report_title(request),
        "report_type": request.command,
        "target": request.target or request.market_scope or request.candidate_pool or "latest",
        "mode": request.mode,
        "report_variant": report_variant,
        "report_date": (request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat()),
        "summary": summary,
        "sections": _parse_sections(markdown),
        "scores": scores,
        "buy_rating": buy_rating,
        "risks": _extract_list_after_heading(markdown, ("風險", "反證", "扣分")),
        "positive_factors": _extract_list_after_heading(markdown, ("利多", "正面", "優勢", "證據")),
        "watch_items": _extract_list_after_heading(markdown, ("觀察", "追蹤", "未來")),
        "sources": [asdict(item) for item in sources],
        "report_metadata": convergence_layer["report_metadata"],
        "data_source_summary": convergence_layer["data_source_summary"],
        "candidate_snapshot": convergence_layer["candidate_snapshot"],
        "metadata": {
            "ai_used": ai_used,
            "ai_status": ai_status,
            "report_variant": report_variant,
            "analysis_model": data.get("analysis_model"),
            "analysis_model_choice": data.get("analysis_model_choice"),
            "analysis_provider": data.get("analysis_provider"),
            "market_data_date": data.get("market_data_date"),
            "report_generated_at": data.get("report_generated_at"),
            "comparison_reports": data.get("comparison_reports"),
            "fallback_reason": fallback_reason,
            "minimax_retry_diagnostics": data.get("minimax_retry_diagnostics"),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "output_formats": list(request.output_formats),
            "local_scoring_policy": local_scoring.get("policy"),
            "local_scoring": local_scoring,
            "ai_final_scoring_policy": {
                "role": "AI 最終投研評分",
                "rule": "AI 必須根據全部資料、搜尋來源與反證重新評估；若高於本地量化底稿需說明原因。",
            },
            "gemini_search_diagnostics": data.get("gemini_search_diagnostics"),
            "gemini_search_discovery": data.get("gemini_search_discovery"),
            "minimax_search_discovery": data.get("minimax_search_discovery"),
            "tavily_search_discovery": data.get("tavily_search_discovery"),
            "minimax_diagnostics": data.get("minimax_diagnostics"),
            "segmented_ai_analysis": data.get("segmented_ai_analysis"),
            "segmented_ai_prompt_paths": data.get("segmented_ai_prompt_paths"),
            "opencode_diagnostics": data.get("opencode_diagnostics"),
            "shared_data_layer": shared_data_layer,
            "ai_data_center": data.get("ai_data_center"),
            "ai_input_audit": data.get("ai_input_audit"),
            "report_confidence": data.get("report_confidence"),
            "ai_prompt_context": data.get("ai_prompt_context"),
            "low_model_digest": data.get("low_model_digest"),
            "low_model_model": data.get("low_model_model"),
            "low_model_prompt_path": data.get("low_model_prompt_path"),
            "low_model_diagnostics": data.get("low_model_diagnostics"),
            "low_model_validation": data.get("low_model_validation"),
            "high_model_input_mode": data.get("high_model_input_mode"),
            "high_model_input_package": data.get("high_model_input_package"),
            "ai_workflow_policy": data.get("ai_workflow_policy"),
            "ai_workflow_coverage": data.get("ai_workflow_coverage"),
            "news_context": shared_data_layer.get("news_context"),
            "saved_news_context": shared_data_layer.get("saved_news_context"),
            "news_persistence_status": shared_data_layer.get("news_persistence_status"),
            "feature_pack": shared_data_layer.get("feature_pack"),
            "data_coverage": shared_data_layer.get("data_coverage"),
            "value_scan_candidate_count": len(data.get("ai_candidates") or data.get("candidates") or []) if request.command == "value_scan" else None,
            "value_scan_candidates": _value_scan_candidate_refs(data) if request.command == "value_scan" else None,
            "report_schema_version": report_quality["schema_version"],
            "report_quality": report_quality,
            "source_quality": report_quality["source_quality"],
            "source_coverage_summary": report_quality["source_coverage_summary"],
            "data_coverage_score": report_quality["data_coverage_score"],
        },
        "structured_data": _structured_data_report_snapshot(request, data, report_quality),
    }


def _compact_for_metadata(value: Any, *, depth: int = 4, max_list: int = 30, max_keys: int = 80, max_string: int = 1200) -> Any:
    if depth <= 0:
        if isinstance(value, (dict, list, tuple)):
            return f"<{type(value).__name__} truncated>"
        return value
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_keys:
                compact["_truncated_keys"] = len(value) - max_keys
                break
            compact[str(key)] = _compact_for_metadata(item, depth=depth - 1, max_list=max_list, max_keys=max_keys, max_string=max_string)
        return compact
    if isinstance(value, (list, tuple)):
        items = [
            _compact_for_metadata(item, depth=depth - 1, max_list=max_list, max_keys=max_keys, max_string=max_string)
            for item in list(value)[:max_list]
        ]
        if len(value) > max_list:
            items.append({"_truncated_items": len(value) - max_list})
        return items
    if isinstance(value, str) and len(value) > max_string:
        return value[:max_string].rstrip() + "...<truncated>"
    return value


def _shared_data_layer_summary(data: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "news_context",
        "saved_news_context",
        "news_persistence_status",
        "feature_pack",
        "feature_pack_artifact",
        "data_coverage",
        "data_gap_summary",
        "unified_evidence_pack",
        "news_events",
        "news_event_summary",
        "search_query_log",
        "resolved_entity",
        "resolved_topic",
        "event_context",
        "event_context_summary",
        "prompt_bundle",
        "ai_error_classification",
    )
    summary = {
        key: _compact_for_metadata(data.get(key))
        for key in keys
        if data.get(key) is not None
    }
    command_role = data.get("command_role")
    if command_role == "sector_strength":
        summary["sector_strength"] = _sector_strength_metadata_summary(data)
    elif data.get("sector_strength"):
        summary["sector_strength"] = _sector_strength_metadata_summary(data.get("sector_strength") or {})
    if data.get("market_movers"):
        summary["market_movers"] = _market_movers_metadata_summary(data.get("market_movers") or {})
    return summary


def _sector_strength_metadata_summary(data: dict[str, Any]) -> dict[str, Any]:
    return _compact_for_metadata(
        {
            "command_role": data.get("command_role"),
            "report_date": data.get("report_date"),
            "market_data_date": data.get("market_data_date"),
            "report_generated_at": data.get("report_generated_at"),
            "source": data.get("source"),
            "lookback_days": data.get("lookback_days"),
            "sector_rankings": (data.get("sector_rankings") or [])[:20],
            "subsector_rankings": (data.get("subsector_rankings") or [])[:20],
            "data_quality": data.get("data_quality"),
            "market_movers": _market_movers_metadata_summary(data.get("market_movers") or {}),
        },
        depth=5,
        max_list=20,
        max_keys=120,
    )


def _market_movers_metadata_summary(data: dict[str, Any]) -> dict[str, Any]:
    return _compact_for_metadata(
        {
            "market_data_date": data.get("market_data_date"),
            "report_generated_at": data.get("report_generated_at") or data.get("generated_at"),
            "source_mode": data.get("source_mode"),
            "hard_filter_policy": data.get("hard_filter_policy"),
            "data_quality": data.get("data_quality"),
            "top_gainers": (data.get("top_gainers") or [])[:20],
            "top_losers": (data.get("top_losers") or [])[:20],
            "top_volume_surge": (data.get("top_volume_surge") or [])[:20],
            "top_turnover": (data.get("top_turnover") or [])[:20],
            "new_highs": (data.get("new_highs") or [])[:20],
            "new_lows": (data.get("new_lows") or [])[:20],
            "sector_mover_rankings": (data.get("sector_mover_rankings") or [])[:20],
        },
        depth=4,
        max_list=12,
        max_keys=100,
    )


def _shared_data_layer_markdown(structured_data: dict[str, Any]) -> str:
    summary = _shared_data_layer_summary(structured_data)
    if not summary:
        return "- 本次 structured_data 未包含 news_context / feature_pack / data_coverage。"
    return "```json\n" + json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n```"


def write_report_artifacts(
    report_root: Path,
    request: CommandRequest,
    markdown: str,
    summary: str,
    sources: list[SourceItem],
    ai_used: bool,
    fallback_reason: str | None,
    structured_data: dict[str, Any] | None = None,
    report_variant: str | None = None,
) -> tuple[ReportArtifacts, dict[str, Any]]:
    report_id = _make_report_id(request, report_variant)
    report_type = "stock" if request.command == "research" else request.command
    output_dir = report_root / report_type / _safe_slug(request.target or request.market_scope or request.candidate_pool or "latest")
    output_dir.mkdir(parents=True, exist_ok=True)

    markdown = normalize_report_text(sanitize_report_markdown(markdown))
    summary = normalize_report_text(sanitize_report_markdown(summary))
    markdown = _append_value_scan_candidate_analysis(request, markdown, structured_data)
    markdown = _append_report_quality_summary(request, markdown, structured_data, sources)
    markdown = _append_source_reference_bridge(markdown, sources)
    markdown = _append_complete_source_appendix(markdown, sources)
    markdown = normalize_report_text(markdown)
    report_json = build_report_json(request, markdown, summary, sources, ai_used, fallback_reason, structured_data, report_variant, report_id)
    qa = validate_report(markdown, request, sources, report_json)
    markdown = append_qa_notes(markdown, qa)
    markdown = normalize_report_text(markdown)
    if markdown != report_json.get("markdown_validated_source"):
        report_json = build_report_json(request, markdown, summary, sources, ai_used, fallback_reason, structured_data, report_variant, report_id)
    report_json["metadata"]["qa_validation"] = qa

    markdown_path = output_dir / f"{report_id}.md" if "md" in request.output_formats else Path("__no_markdown_file__")
    html_path = output_dir / f"{report_id}.html" if "html" in request.output_formats else Path("__no_html_file__")
    json_path = output_dir / f"{report_id}.json" if "json" in request.output_formats else Path("__no_json_file__")
    sources_path = output_dir / f"{report_id}.sources.json"

    if "md" in request.output_formats:
        markdown_path.write_text(markdown, encoding="utf-8")
    if "html" in request.output_formats:
        html_path.write_text(render_html(report_json, markdown), encoding="utf-8")
    if "json" in request.output_formats:
        json_path.write_text(json.dumps(report_json, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    sources_path.write_text(json.dumps([asdict(item) for item in sources], ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _register_report_artifacts(request, structured_data or {}, report_id, json_path, sources_path)

    return (
        ReportArtifacts(
            report_id=report_id,
            report_type=request.command,
            markdown_path=markdown_path,
            html_path=html_path,
            json_path=json_path,
            sources_path=sources_path,
        ),
        report_json,
    )


def _register_report_artifacts(
    request: CommandRequest,
    data: dict[str, Any],
    report_id: str,
    json_path: Path,
    sources_path: Path,
) -> None:
    data_date = request.report_date.isoformat() if request.report_date else data.get("market_data_date") or data.get("report_date")
    for path, artifact_type in ((json_path, "report_json"), (sources_path, "report_sources")):
        if not path.exists() or path.name.startswith("__no_"):
            continue
        try:
            register_artifact(
                build_artifact_record(
                    path=path,
                    artifact_type=artifact_type,
                    source=request.command,
                    schema_version="report_artifact_v1",
                    data_date=str(data_date) if data_date else None,
                    completeness=data.get("report_confidence") or data.get("data_coverage_score"),
                    metadata={
                        "report_id": report_id,
                        "command": request.command,
                        "mode": request.mode,
                    },
                )
            )
        except Exception:
            pass


def sanitize_report_markdown(markdown: str) -> str:
    """Normalize model-produced report markdown before saving/rendering."""
    text = str(markdown or "").strip()
    text = _strip_outer_markdown_fence(text)
    text = _strip_model_preface(text)
    text = re.sub(r"\[S\s*\?\s*\]", "來源未對應", text)
    return text.strip() + ("\n" if text else "")


def _strip_outer_markdown_fence(markdown: str) -> str:
    lines = markdown.strip().splitlines()
    if len(lines) < 2:
        return markdown
    first = lines[0].strip().lower()
    last = lines[-1].strip()
    if first in {"```", "```markdown", "```md"} and last == "```":
        return "\n".join(lines[1:-1]).strip()
    return markdown


MODEL_PREFACE_PATTERNS = (
    re.compile(r"^好的[，,].{0,80}(報告|產出|分析|根據).*$"),
    re.compile(r"^我將.{0,80}(報告|產出|分析).*$"),
    re.compile(r"^以下是.{0,80}(報告|分析).*$"),
    re.compile(r"^根據您提供的.{0,80}(報告|分析).*$"),
)


def _strip_model_preface(markdown: str) -> str:
    lines = markdown.splitlines()
    first_heading = next((index for index, line in enumerate(lines) if line.lstrip().startswith("#")), None)
    if first_heading is None:
        return "\n".join(line for line in lines if not _is_model_preface_line(line))
    prefix = [line for line in lines[:first_heading] if line.strip() and not _is_model_preface_line(line)]
    if prefix:
        return "\n".join([*prefix, *lines[first_heading:]]).strip()
    return "\n".join(lines[first_heading:]).strip()


def _is_model_preface_line(line: str) -> bool:
    clean = line.strip().strip("*_- ")
    if not clean:
        return False
    return any(pattern.match(clean) for pattern in MODEL_PREFACE_PATTERNS)


def _append_source_reference_bridge(markdown: str, sources: list[SourceItem], max_refs: int = 5) -> str:
    if not sources or re.search(r"\[S\d{3}\]", _qa_main_markdown(markdown)):
        return markdown
    refs = []
    for source in sources[:max_refs]:
        title = (source.title or source.url or "未命名來源").strip()
        refs.append(f"[{source.source_id}] {title}")
    if not refs:
        return markdown
    lines = [
        markdown.rstrip(),
        "",
        "## 來源引用補充",
        "- 模型正文未逐段標示來源；系統保留本次報告主要參考來源：" + "；".join(refs),
    ]
    return "\n".join(lines).strip() + "\n"


def _qa_main_markdown(markdown: str) -> str:
    return re.split(r"\n## (完整資料來源清單|規格檢查提醒)\b", markdown, maxsplit=1)[0]



def _append_value_scan_candidate_analysis(request: CommandRequest, markdown: str, structured_data: dict[str, Any] | None) -> str:
    data = structured_data or {}
    if request.command != "value_scan":
        return markdown
    # 使用實際送 AI 的候選股（ai_candidates），不是本地 display 的 candidates
    candidates = data.get("ai_candidates") or data.get("candidates") or []
    if not candidates:
        return markdown
    marker = "\u5b8c\u6574\u5019\u9078\u80a1\u9010\u6a94\u91cd\u4f30\u5206\u6790"
    if marker in markdown:
        return markdown
    lines = [markdown.rstrip(), "", "---", "", "## " + marker, ""]
    lines.append("\u4ee5\u4e0b\u7ae0\u7bc0\u7531\u7a0b\u5f0f\u4f9d\u672c\u6b21\u5be6\u969b\u9001\u5165 AI \u7684 ai_candidates \u81ea\u52d5\u9644\u52a0\uff0c\u78ba\u4fdd\u5be6\u969b\u5206\u6790\u5019\u9078\u80a1\u6709\u53ef\u8ffd\u6eaf\u7684\u9010\u6a94\u5206\u6790\u5e95\u7a3f\u3002")
    matrix = _value_scan_data_completeness_matrix(data)
    if matrix:
        lines.extend(["", "### 資料完整度矩陣", ""])
        lines.append("| 股票 | 財報細項 | 毛利率 | 籌碼 | 估值 | TDCC | MOPS | 來源事件 | 公司知識庫 | 缺漏 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for item in matrix:
            lines.append(
                "| {stock} | {financial_detail} | {gross_margin_cache} | {chip_backup_summary} | {valuation_data} | {tdcc_data} | {mops_documents} | {source_events} | {company_knowledge} | {missing} |".format(
                    stock=item["stock"],
                    financial_detail=item["financial_detail"],
                    gross_margin_cache=item["gross_margin_cache"],
                    chip_backup_summary=item["chip_backup_summary"],
                    valuation_data=item["valuation_data"],
                    tdcc_data=item["tdcc_data"],
                    mops_documents=item["mops_documents"],
                    source_events=item["source_events"],
                    company_knowledge=item["company_knowledge"],
                    missing=item["missing"],
                )
            )
    for index, row in enumerate(candidates, 1):
        code = str(row.get("code") or "").strip()
        name = str(row.get("name") or "").strip()
        title = " ".join(part for part in [code, name] if part) or f"candidate_{index}"
        score = _fmt_value(row.get("rerating_score"))
        verify = _fmt_value(row.get("verification_score"))
        old_label = row.get("old_market_label") or "\u8cc7\u6599\u4e0d\u8db3"
        new_label = row.get("new_market_label") or "\u8cc7\u6599\u4e0d\u8db3"
        lines.extend(["", f"### {index}. {title}", ""])
        lines.append(f"- \u820a\u5e02\u5834\u6a19\u7c64\uff1a{old_label}")
        lines.append(f"- \u65b0\u5e02\u5834\u6a19\u7c64\uff1a{new_label}")
        lines.append(f"- \u91cd\u4f30\u5206\u6578\uff1a{score}/100\uff1b\u8b49\u64da\u8986\u84cb\u5206\uff1a{verify}/100")
        lines.append(f"- \u7522\u696d\uff1a{row.get('industry') or '\u672a\u5206\u985e'}\uff1b\u80a1\u50f9\uff1a{_fmt_value(row.get('price'))}\uff1b20 \u65e5\u5747\u91cf\uff1a{_fmt_value(row.get('avg_volume_20d'))}")
        lines.append(f"- \u6700\u65b0\u6708\u71df\u6536\uff1a{_fmt_value(row.get('latest_monthly_revenue'))}\uff1bYoY\uff1a{_fmt_value(row.get('revenue_yoy'))}%")
        lines.append("- \u91cd\u4f30\u8b49\u64da\uff1a" + _inline_list(row.get("rerating_evidence")))
        lines.append("- \u8a55\u5206\u7d44\u6210\uff1a" + _component_summary(row.get("score_components")))
        lines.append("- \u71df\u6536\u8207\u8ca1\u5831\u9a57\u8b49\uff1a" + _financial_validation_summary(row))
        lines.append("- \u6cd5\u4eba\u3001\u7c4c\u78bc\u8207\u6280\u8853\u78ba\u8a8d\uff1a" + _chip_technical_summary(row))
        lines.append("- \u662f\u5426\u53ea\u662f\u8e6d\u984c\u6750\uff1a" + _hype_judgement(row))
        lines.append("- \u5e02\u5834\u6b63\u5728\u4ea4\u6613\u4ec0\u9ebc\u6545\u4e8b\uff1a" + _market_story_summary(row))
        lines.append("- \u65e9\u671f\u86db\u7d72\u99ac\u8de1\uff1a" + _early_clue_summary(row))
        lines.append("- \u4e0b\u4e00\u6ce2\u53ef\u80fd\u767c\u9175\u7684\u50ac\u5316\u5291\uff1a" + _catalyst_summary(row))
        lines.append("- \u5982\u679c\u8981\u5927\u6f32\uff0c\u9084\u7f3a\u4ec0\u9ebc\u8a0a\u865f\uff1a" + _missing_breakout_signal_summary(row))
        lines.append("- \u53cd\u5411\u9a57\u8b49\u8207\u5931\u6557\u689d\u4ef6\uff1a" + _failure_condition_summary(row))
        lines.append("- \u60f3\u50cf\u529b\u7d50\u8ad6\uff1a" + _rerating_hypothesis_summary(row))
        lines.append("- \u672a\u4f86 1\uff5e3 \u500b\u6708\u89c0\u5bdf\u91cd\u9ede\uff1a" + _watch_items_summary(row))
        lines.append("- \u98a8\u96aa\u8207\u53cd\u8b49\uff1a" + _risk_summary(row))
    return "\n".join(lines).strip() + "\n"


def _append_report_quality_summary(
    request: CommandRequest,
    markdown: str,
    structured_data: dict[str, Any] | None,
    sources: list[SourceItem],
) -> str:
    marker = "報告資料完整度與來源品質"
    if marker in markdown:
        return markdown
    quality = build_report_quality_layer(request, structured_data or {}, sources)
    lines = [markdown.rstrip(), "", "---", "", "## " + marker, ""]
    lines.append(f"- Report Quality Schema：{quality.get('schema_version')}")
    lines.append(f"- 資料覆蓋分數：{quality.get('data_coverage_score')}/100")
    source_summary = (quality.get("source_coverage_summary") or {})
    lines.append(
        f"- 來源總數：{source_summary.get('total_sources', 0)}；"
        f"明確日期來源：{source_summary.get('explicit_dated_sources', 0)}；"
        f"推測日期來源：{source_summary.get('inferred_dated_sources', 0)}；"
        f"日期不可驗證：{source_summary.get('undated_sources', 0)}"
    )
    warnings = quality.get("qa_warnings") or []
    lines.append("- QA 提醒：" + ("；".join(str(item) for item in warnings) if warnings else "無"))
    rows = quality.get("data_completeness_matrix") or []
    if rows:
        lines.extend(["", "| 欄位 | 狀態 | 數量 |", "|---|---:|---:|"])
        for row in rows:
            status = "有資料" if row.get("available") else "缺資料"
            lines.append(f"| {row.get('field')} | {status} | {row.get('count', 0)} |")
    lines.extend(["", "### Missing Data Policy", ""])
    policy = quality.get("missing_data_policy") or {}
    for key, rule in policy.items():
        lines.append(f"- {key}：{rule}")
    return "\n".join(lines).strip() + "\n"


def _value_scan_candidate_refs(data: dict[str, Any]) -> list[dict[str, str]]:
    refs = []
    # 使用實際送 AI 的候選股（ai_candidates），不是本地 display 的 candidates
    for row in data.get("ai_candidates") or data.get("candidates") or []:
        refs.append({"code": str(row.get("code") or ""), "name": str(row.get("name") or "")})
    return refs


def _structured_data_report_snapshot(
    request: CommandRequest,
    data: dict[str, Any],
    report_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality = report_quality or {}
    common = {
        "schema_version": quality.get("schema_version"),
        "evidence_pack": quality.get("evidence_pack"),
        "data_completeness_matrix": quality.get("data_completeness_matrix"),
        "data_coverage_score": quality.get("data_coverage_score"),
        "source_coverage_summary": quality.get("source_coverage_summary"),
        "qa_warnings": quality.get("qa_warnings"),
        "data_gap_summary": quality.get("data_gap_summary") or data.get("data_gap_summary"),
        "unified_evidence_pack": quality.get("unified_evidence_pack") or data.get("unified_evidence_pack"),
        "news_event_summary": quality.get("news_event_summary") or data.get("news_event_summary"),
        "segmented_ai_analysis": data.get("segmented_ai_analysis"),
        "required_data_gap_summary": data.get("required_data_gap_summary"),
        "required_data_gap_backfill_tasks": data.get("required_data_gap_backfill_tasks"),
        "required_data_gap_remaining_tasks": data.get("required_data_gap_remaining_tasks"),
        "required_gap_minimax_discovery": data.get("required_gap_minimax_discovery"),
        "required_gap_tavily_discovery": data.get("required_gap_tavily_discovery"),
        "resolved_entity": data.get("resolved_entity"),
        "resolved_topic": data.get("resolved_topic"),
        "event_context_summary": data.get("event_context_summary"),
        "prompt_bundle": data.get("prompt_bundle") or (data.get("prompt_policy") or {}).get("prompt_bundle"),
        "ai_error_classification": data.get("ai_error_classification"),
    }
    if request.command == "value_scan":
        return {
            **common,
            "ai_candidate_evidence_pack": _compact_for_metadata(
                data.get("ai_candidate_evidence_pack") or [],
                depth=7,
                max_list=40,
                max_keys=160,
                max_string=2000,
            ),
            "data_completeness_matrix": _value_scan_data_completeness_matrix(data),
            "company_knowledge_update_status": data.get("company_knowledge_update_status"),
        }
    if request.command in {"research", "theme"}:
        return {
            **common,
            "company_knowledge": data.get("company_knowledge"),
            "company_knowledge_summary": data.get("company_knowledge_summary"),
            "company_knowledge_update_status": data.get("company_knowledge_update_status"),
        }
    return common


def _value_scan_data_completeness_matrix(data: dict[str, Any]) -> list[dict[str, Any]]:
    pack = data.get("ai_candidate_evidence_pack") or []
    if not isinstance(pack, list):
        return []
    matrix = []
    for row in pack:
        if not isinstance(row, dict):
            continue
        missing = row.get("missing_data_status") or []
        stock = " ".join(part for part in (str(row.get("code") or ""), str(row.get("name") or "")) if part).strip()
        matrix.append({
            "code": str(row.get("code") or ""),
            "name": str(row.get("name") or ""),
            "stock": stock or "unknown",
            "financial_detail": _coverage_mark(row.get("financial_detail"), "financial_detail" in missing),
            "gross_margin_cache": _coverage_mark(row.get("gross_margin_cache"), "gross_margin_cache" in missing),
            "chip_backup_summary": _coverage_mark(row.get("chip_backup_summary"), "chip_backup_data" in missing),
            "valuation_data": _coverage_mark(row.get("valuation_data"), False),
            "tdcc_data": _coverage_mark(row.get("tdcc_data"), False),
            "mops_documents": _coverage_mark(row.get("mops_documents"), "mops_documents" in missing),
            "source_events": _coverage_mark(row.get("source_events"), "source_events" in missing),
            "company_knowledge": _coverage_mark(row.get("company_knowledge"), "company_knowledge" in missing),
            "missing": ", ".join(str(item) for item in missing) if missing else "無",
        })
    return matrix


def _coverage_mark(value: Any, is_missing: bool) -> str:
    if is_missing:
        return "缺"
    if isinstance(value, dict):
        status = str(value.get("status") or "").lower()
        if status in {"missing", "unavailable", "no data"}:
            return "缺"
        return "有" if value else "缺"
    if isinstance(value, list):
        return "有" if value else "缺"
    return "有" if value not in (None, "") else "缺"


def _source_quality_metadata(sources: list[SourceItem]) -> dict[str, Any]:
    items = []
    buckets = {"high": 0, "medium": 0, "low": 0, "rejected": 0}
    for source in sources:
        quality = source_quality_score(source)
        buckets[quality["level"]] = buckets.get(quality["level"], 0) + 1
        items.append({
            "source_id": source.source_id,
            "title": source.title,
            "url": source.url,
            "source_level": source.source_level,
            "source_quality_score": quality["score"],
            "source_quality_level": quality["level"],
            "usable_for_company_knowledge": quality["usable_for_company_knowledge"],
        })
    return {"summary": buckets, "items": items}


def _fmt_value(value: Any) -> str:
    if value is None or value == "":
        return "\u8cc7\u6599\u4e0d\u8db3"
    if isinstance(value, (int, float)):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _inline_list(value: Any, limit: int = 8) -> str:
    if not value:
        return "\u8cc7\u6599\u4e0d\u8db3\uff0c\u9700\u4fdd\u5b88\u89e3\u8b80\u3002"
    if isinstance(value, dict):
        value = [f"{key}: {val}" for key, val in value.items()]
    if not isinstance(value, list):
        return str(value)
    return "\uff1b".join(str(item) for item in value[:limit]) or "\u8cc7\u6599\u4e0d\u8db3\u3002"


def _component_summary(components: Any) -> str:
    if not isinstance(components, dict) or not components:
        return "\u8cc7\u6599\u4e0d\u8db3\u3002"
    return "\uff1b".join(f"{key}={_fmt_value(value)}" for key, value in components.items())


def _financial_validation_summary(row: dict[str, Any]) -> str:
    financial = row.get("financial_detail") or {}
    cross = row.get("cross_validation") or {}
    status = financial.get("status") or cross.get("status") or "unknown"
    fields = financial.get("fields") or []
    latest = financial.get("latest_period") or "unknown"
    return f"financial_detail={status}\uff0clatest_period={latest}\uff0cfields={_inline_list(fields, 6)}\uff0ccross_validation={cross.get('verification_score', '\u8cc7\u6599\u4e0d\u8db3')}"


def _chip_technical_summary(row: dict[str, Any]) -> str:
    chip = (row.get("chip_backup_data") or {}).get("summary") or {}
    parts = []
    if chip:
        parts.append(f"recent_10d_foreign_net_lots={_fmt_value(chip.get('recent_10d_foreign_net_lots'))}")
        parts.append(f"latest_big_holder_pct={_fmt_value(chip.get('latest_big_holder_pct'))}")
        parts.append(f"latest_retail_holder_pct={_fmt_value(chip.get('latest_retail_holder_pct'))}")
    parts.append(f"price={_fmt_value(row.get('price'))}")
    parts.append(f"avg_volume_20d={_fmt_value(row.get('avg_volume_20d'))}")
    return "\uff1b".join(parts)


def _hype_judgement(row: dict[str, Any]) -> str:
    score = row.get("rerating_score") or 0
    verify = row.get("verification_score") or 0
    evidence = row.get("rerating_evidence") or []
    if verify and verify >= 60 and evidence:
        return "\u6709\u90e8\u5206\u71df\u6536\u3001\u8ca1\u5831\u6216\u516c\u958b\u4f86\u6e90\u652f\u6490\uff0c\u4e0d\u5b9c\u76f4\u63a5\u8996\u70ba\u7d14\u8e6d\u984c\u6750\u3002"
    if score and score >= 70:
        return "\u91cd\u4f30\u5206\u6578\u504f\u9ad8\uff0c\u4f46\u8b49\u64da\u8986\u84cb\u4e0d\u8db3\u6642\u9700\u4fdd\u5b88\u6aa2\u8996\u662f\u5426\u53ea\u662f\u8e6d\u984c\u6750\u3002"
    return "\u76ee\u524d\u8b49\u64da\u4e0d\u8db3\u4ee5\u8b49\u660e\u5df2\u5b8c\u6210\u91cd\u4f30\uff0c\u9700\u4fdd\u5b88\u89e3\u8b80\u3002"


def _market_story_summary(row: dict[str, Any]) -> str:
    old_label = row.get("old_market_label") or "\u820a\u696d\u52d9"
    new_label = row.get("new_market_label") or row.get("industry") or "\u65b0\u6210\u9577\u984c\u6750"
    evidence = _inline_list(row.get("rerating_evidence"), 3)
    return (
        f"\u5e02\u5834\u82e5\u9858\u610f\u8cb7\u55ae\uff0c\u6838\u5fc3\u6545\u4e8b\u662f\u300c{old_label}\u300d\u88ab\u91cd\u65b0\u8cbc\u6a19\u70ba\u300c{new_label}\u300d\u3002"
        f"\u76ee\u524d\u53ef\u89c0\u5bdf\u7dda\u7d22\uff1a{evidence}\u3002"
    )


def _early_clue_summary(row: dict[str, Any]) -> str:
    clues: list[str] = []
    if row.get("revenue_yoy") not in (None, "", "unknown"):
        clues.append(f"\u71df\u6536 YoY={_fmt_value(row.get('revenue_yoy'))}%")
    if row.get("avg_volume_20d") not in (None, "", "unknown"):
        clues.append(f"20 \u65e5\u5747\u91cf={_fmt_value(row.get('avg_volume_20d'))}")
    chip = (row.get("chip_backup_data") or {}).get("summary") or {}
    if chip.get("recent_10d_foreign_net_lots") not in (None, "", "unknown"):
        clues.append(f"\u8fd1 10 \u65e5\u5916\u8cc7={_fmt_value(chip.get('recent_10d_foreign_net_lots'))}")
    if row.get("source_events"):
        clues.append(f"\u4f86\u6e90\u4e8b\u4ef6 {len(row.get('source_events') or [])} \u7b46")
    return "\uff1b".join(clues) or "\u73fe\u968e\u6bb5\u65e9\u671f\u8a0a\u865f\u4e0d\u8db3\uff0c\u53ea\u80fd\u5148\u5217\u70ba\u89c0\u5bdf\u5047\u8aaa\u3002"


def _catalyst_summary(row: dict[str, Any]) -> str:
    catalysts = []
    if row.get("new_market_label"):
        catalysts.append(f"\u65b0\u6a19\u7c64\u300c{row.get('new_market_label')}\u300d\u51fa\u73fe\u516c\u544a\u3001\u5ba2\u6236\u6216\u71df\u6536\u9a57\u8b49")
    catalysts.extend([
        "\u6708\u71df\u6536\u6216\u6bdb\u5229\u7387\u9023\u7e8c\u6539\u5584",
        "\u6cd5\u8aaa\u6703\u91cb\u51fa\u65b0\u7522\u54c1\u6216\u8a02\u55ae\u80fd\u898b\u5ea6",
        "\u6cd5\u4eba\u8207\u5927\u6236\u7c4c\u78bc\u540c\u6b65\u8f49\u5f37",
    ])
    return "\uff1b".join(catalysts[:4])


def _missing_breakout_signal_summary(row: dict[str, Any]) -> str:
    missing = list(row.get("missing_data") or row.get("data_gaps") or [])
    if row.get("revenue_yoy") is None:
        missing.append("\u9700\u6700\u65b0\u6708\u71df\u6536 YoY \u652f\u6301")
    if not row.get("rerating_evidence"):
        missing.append("\u9700\u53ef\u8ffd\u6eaf\u7684\u984c\u6750\u6216\u7522\u54c1\u8b49\u64da")
    if not row.get("source_events"):
        missing.append("\u9700\u5916\u90e8\u4f86\u6e90\u4e8b\u4ef6\u652f\u6301")
    return _inline_list(missing, 5)


def _failure_condition_summary(row: dict[str, Any]) -> str:
    counter = list(row.get("counter_evidence") or [])
    base = [
        "\u71df\u6536\u6216 EPS \u672a\u8ddf\u4e0a\u984c\u6750",
        "\u65b0\u6a19\u7c64\u53ea\u6709\u65b0\u805e\u6216\u793e\u7fa4\u60c5\u7dd2\uff0c\u7f3a\u5c11\u516c\u544a\u9a57\u8b49",
        "\u7c4c\u78bc\u8f49\u5f31\u6216\u4f30\u503c\u5df2\u904e\u5ea6\u53cd\u6620",
    ]
    return _inline_list(counter + base, 5)


def _rerating_hypothesis_summary(row: dict[str, Any]) -> str:
    score = float(row.get("rerating_score") or 0)
    verify = float(row.get("verification_score") or 0)
    if score >= 80 and verify >= 60:
        return "\u5c6c\u65bc\u6709\u8b49\u64da\u7684\u91cd\u4f30\u5019\u9078\uff0c\u53ef\u9032\u4e00\u6b65\u7528 /research --deep \u9a57\u8b49\u662f\u5426\u5f62\u6210\u6ce2\u6bb5\u4e3b\u7dda\u3002"
    if score >= 70:
        return "\u5c6c\u65bc\u984c\u6750\u60f3\u50cf\u8207\u65e9\u671f\u91cd\u4f30\u5047\u8aaa\uff0c\u9700\u88dc\u5b98\u65b9\u71df\u6536\u3001\u7522\u54c1\u6216\u5ba2\u6236\u8b49\u64da\u624d\u80fd\u5347\u7d1a\u3002"
    return "\u76ee\u524d\u66f4\u50cf\u89c0\u5bdf\u540d\u55ae\uff0c\u9084\u4e0d\u9069\u5408\u628a\u5e02\u5834\u60f3\u50cf\u8f49\u6210\u5f37\u7d50\u8ad6\u3002"


def _watch_items_summary(row: dict[str, Any]) -> str:
    items = ["\u6708\u71df\u6536 YoY \u662f\u5426\u6301\u7e8c\u6539\u5584", "\u6bdb\u5229\u7387\u8207 EPS \u662f\u5426\u8ddf\u4e0a\u984c\u6750", "\u5916\u8cc7\u8207\u5927\u6236\u7c4c\u78bc\u662f\u5426\u7e8c\u5f37"]
    if row.get("new_market_label"):
        items.insert(0, f"\u65b0\u6a19\u7c64\u300c{row.get('new_market_label')}\u300d\u662f\u5426\u6709\u71df\u6536\u6216\u516c\u544a\u9a57\u8b49")
    return "\uff1b".join(items[:4])


def _risk_summary(row: dict[str, Any]) -> str:
    cross = row.get("cross_validation") or {}
    missing = []
    checks = cross.get("checks") or {}
    if isinstance(checks, dict):
        missing = [key for key, value in checks.items() if isinstance(value, dict) and value.get("status") == "missing"]
    risks = []
    if missing:
        risks.append("\u7f3a\u5c11\u9a57\u8b49\u9805\uff1a" + ", ".join(missing[:5]))
    if row.get("revenue_yoy") is None:
        risks.append("\u6700\u65b0\u71df\u6536 YoY \u7f3a\u6f0f")
    if not row.get("rerating_evidence"):
        risks.append("\u91cd\u4f30\u8b49\u64da\u4e0d\u8db3")
    return "\uff1b".join(risks) or "\u672a\u898b\u660e\u986f\u81f4\u547d\u53cd\u8b49\uff0c\u4f46\u4ecd\u9700\u8ffd\u8e64\u516c\u544a\u8207\u8ca1\u5831\u3002"


def _append_complete_source_appendix(markdown: str, sources: list[SourceItem]) -> str:
    if not sources:
        return markdown
    marker = "\u5b8c\u6574\u8cc7\u6599\u4f86\u6e90\u6e05\u55ae"
    if marker in markdown:
        return markdown
    heading = "## " + marker
    lines = [markdown.rstrip(), "", "---", "", heading, ""]
    max_markdown_sources = 40
    lines.append(
        "\u4ee5\u4e0b\u70ba Markdown \u7cbe\u7c21\u4f86\u6e90\u8868\uff1b\u5b8c\u6574\u4f86\u6e90\u8207\u8f03\u9577\u6458\u8981\u4fdd\u7559\u5728 HTML \u300c\u5b8c\u6574\u8cc7\u6599\u4f86\u6e90\u300d\u5206\u9801\u8207 .sources.json\u3002"
    )
    lines.append("")
    lines.append("| ID | 層級 | 來源 | 日期 | 標題 | URL |")
    lines.append("|---|---|---|---|---|---|")
    for item in sources[:max_markdown_sources]:
        title = _truncate_source_text(item.title or item.url, 90)
        provider = _truncate_source_text(item.provider or "unknown", 32)
        date_part = item.published_date or "-"
        lines.append(
            f"| {item.source_id} | {item.source_level} | {provider} | {date_part} | {title} | {item.url} |"
        )
    if len(sources) > max_markdown_sources:
        lines.append(
            f"\n- Markdown 僅列前 {max_markdown_sources} 筆精簡來源；完整 {len(sources)} 筆請查看同名 `.sources.json` 或 HTML 報告的完整來源分頁。"
        )
    return "\n".join(lines).strip() + "\n"


def _truncate_source_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"

def render_html(report_json: dict[str, Any], markdown: str) -> str:
    from .report_html_renderer import render_report_html

    return render_report_html(report_json, markdown, DISCLAIMER)

    title = html.escape(str(report_json.get("report_title", "AI 投資研究報告")))
    tabs = _build_html_tabs(report_json, markdown)
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ width: 100%; max-width: 100%; overflow-x: hidden; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; line-height: 1.72; margin: 0; color: #172033; background: #eef2f7; }}
    main {{ width: min(100%, 1040px); margin: 0 auto; padding: 24px 16px 56px; background: #fff; min-height: 100vh; }}
    h1, h2, h3 {{ color: #111827; line-height: 1.35; overflow-wrap: anywhere; }}
    h1 {{ font-size: 28px; border-bottom: 2px solid #e5e7eb; padding-bottom: 12px; margin-top: 0; }}
    h2 {{ font-size: 22px; margin-top: 28px; }}
    h3 {{ font-size: 18px; margin-top: 22px; }}
    p, li, a, td, th {{ overflow-wrap: anywhere; word-break: break-word; }}
    a {{ color: #2563eb; }}
    pre {{ white-space: pre-wrap; overflow-x: auto; max-width: 100%; background: #f3f4f6; padding: 12px; border-radius: 6px; }}
    code {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px; text-align: left; vertical-align: top; }}
    .report-shell {{ width: 100%; }}
    .report-meta {{ margin: 0 0 16px; color: #4b5563; font-size: 14px; }}
    .tab-input {{ position: absolute; opacity: 0; pointer-events: none; }}
    .tab-list {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0 20px; position: sticky; top: 0; z-index: 2; background: rgba(255,255,255,.96); padding: 8px 0; border-bottom: 1px solid #e5e7eb; }}
    .tab-label {{ display: inline-flex; align-items: center; min-height: 38px; padding: 8px 12px; border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; color: #334155; font-size: 14px; cursor: pointer; user-select: none; }}
    .tab-label:hover {{ border-color: #2563eb; color: #1d4ed8; }}
    .tab-panel {{ display: none; width: 100%; min-width: 0; }}
    .source-card {{ border: 1px solid #e5e7eb; border-radius: 6px; padding: 12px; margin: 10px 0; background: #f8fafc; }}
    .source-title {{ font-weight: 700; color: #111827; }}
    .source-meta {{ color: #64748b; font-size: 13px; margin-top: 4px; }}
    .disclaimer {{ margin-top: 32px; padding: 12px; background: #fff7ed; border-left: 4px solid #f97316; overflow-wrap: anywhere; }}
    #tab-main:checked ~ .tab-list label[for="tab-main"],
    #tab-qa:checked ~ .tab-list label[for="tab-qa"],
    #tab-sources:checked ~ .tab-list label[for="tab-sources"],
    #tab-metadata:checked ~ .tab-list label[for="tab-metadata"],
    #tab-candidates:checked ~ .tab-list label[for="tab-candidates"],
    #tab-local-scoring:checked ~ .tab-list label[for="tab-local-scoring"] {{ background: #1d4ed8; color: #fff; border-color: #1d4ed8; }}
    #tab-main:checked ~ .panels #panel-main,
    #tab-qa:checked ~ .panels #panel-qa,
    #tab-sources:checked ~ .panels #panel-sources,
    #tab-metadata:checked ~ .panels #panel-metadata,
    #tab-candidates:checked ~ .panels #panel-candidates,
    #tab-local-scoring:checked ~ .panels #panel-local-scoring {{ display: block; }}
    @media (max-width: 640px) {{
      main {{ padding: 16px 12px 42px; }}
      h1 {{ font-size: 23px; }}
      h2 {{ font-size: 19px; }}
      h3 {{ font-size: 17px; }}
      .tab-list {{ position: static; gap: 6px; }}
      .tab-label {{ flex: 1 1 calc(50% - 6px); justify-content: center; padding: 8px; font-size: 13px; }}
      .source-card {{ padding: 10px; }}
    }}
  </style>
</head>
<body><main>{tabs}<div class="disclaimer">{html.escape(DISCLAIMER)}</div></main></body>
</html>"""


def _build_html_tabs(report_json: dict[str, Any], markdown: str) -> str:
    sections = _split_markdown_for_html(markdown)
    title = html.escape(str(report_json.get("report_title", "AI 投資研究報告")))
    report_date = html.escape(str(report_json.get("report_date") or ""))
    mode = html.escape(str(report_json.get("mode") or ""))
    model = html.escape(str((report_json.get("metadata") or {}).get("analysis_model") or ""))
    main_html = _markdown_to_html(sections["main"] or markdown)
    qa_html = _markdown_to_html(sections["qa"] or _qa_markdown(report_json))
    sources_html = _sources_to_html(report_json) or _markdown_to_html(sections["sources"] or "目前沒有完整資料來源清單。")
    metadata_html = f"<pre>{html.escape(json.dumps(_metadata_summary(report_json), ensure_ascii=False, indent=2, default=str))}</pre>"
    candidates_html = _markdown_to_html(sections["candidates"])
    local_scoring_html = _local_scoring_html(report_json)
    candidate_tab = ""
    candidate_panel = ""
    if sections["candidates"].strip():
        candidate_tab = '<label class="tab-label" for="tab-candidates">候選股逐檔資料</label>'
        candidate_panel = f'<section class="tab-panel" id="panel-candidates">{candidates_html}</section>'
    return f"""
<div class="report-shell">
  <h1>{title}</h1>
  <div class="report-meta">日期：{report_date}｜模式：{mode}{'｜模型：' + model if model else ''}</div>
  <input class="tab-input" type="radio" name="report-tabs" id="tab-main" checked>
  <input class="tab-input" type="radio" name="report-tabs" id="tab-local-scoring">
  <input class="tab-input" type="radio" name="report-tabs" id="tab-qa">
  <input class="tab-input" type="radio" name="report-tabs" id="tab-sources">
  <input class="tab-input" type="radio" name="report-tabs" id="tab-metadata">
  <input class="tab-input" type="radio" name="report-tabs" id="tab-candidates">
  <nav class="tab-list" aria-label="報告內容切換">
    <label class="tab-label" for="tab-main">主報告</label>
    <label class="tab-label" for="tab-local-scoring">本地量化底稿</label>
    <label class="tab-label" for="tab-qa">QA 驗證</label>
    <label class="tab-label" for="tab-sources">完整資料來源</label>
    <label class="tab-label" for="tab-metadata">Metadata</label>
    {candidate_tab}
  </nav>
  <div class="panels">
    <section class="tab-panel" id="panel-main">{main_html}</section>
    <section class="tab-panel" id="panel-local-scoring">{local_scoring_html}</section>
    <section class="tab-panel" id="panel-qa">{qa_html}</section>
    <section class="tab-panel" id="panel-sources">{sources_html}</section>
    <section class="tab-panel" id="panel-metadata">{metadata_html}</section>
    {candidate_panel}
  </div>
</div>
"""


def _split_markdown_for_html(markdown: str) -> dict[str, str]:
    buckets = {"main": [], "qa": [], "sources": [], "candidates": []}
    current = "main"
    for line in markdown.splitlines():
        if line.startswith("## "):
            heading = line.lstrip("#").strip()
            if "完整資料來源清單" in heading:
                current = "sources"
            elif "規格檢查提醒" in heading or "QA" in heading or "驗證" in heading:
                current = "qa"
            elif "完整候選股逐檔重估分析" in heading:
                current = "candidates"
            else:
                current = "main"
        buckets[current].append(line)
    return {key: "\n".join(value).strip() for key, value in buckets.items()}


def _qa_markdown(report_json: dict[str, Any]) -> str:
    qa = ((report_json.get("metadata") or {}).get("qa_validation") or {})
    if not qa:
        return "## QA 驗證\n\n目前沒有 QA 驗證資料。"
    lines = ["## QA 驗證", "", f"- 通過：{qa.get('passed')}"]
    for warning in qa.get("warnings") or []:
        lines.append(f"- {warning}")
    missing = qa.get("missing_sections") or []
    if missing:
        lines.append("- 缺少或未明確命名章節：" + ", ".join(missing))
    schema_errors = qa.get("schema_errors") or []
    if schema_errors:
        lines.append("- JSON schema 修補提醒：" + ", ".join(schema_errors))
    return "\n".join(lines)


def _local_scoring_html(report_json: dict[str, Any]) -> str:
    local_scoring = ((report_json.get("metadata") or {}).get("local_scoring") or {})
    scores = local_scoring.get("scores") or []
    if not scores:
        return '<h2>本地量化底稿</h2><p>本模式不產生本地量化底稿。</p>'
    rows = []
    for item in scores:
        rows.append(
            f"<tr>"
            f"<td>{html.escape(str(item.get('score_name') or ''))}</td>"
            f"<td>{html.escape(str(item.get('score_value') or ''))}/{html.escape(str(item.get('score_max') or ''))}</td>"
            f"<td>{html.escape(str(item.get('score_reason') or ''))}</td>"
            f"<td>{html.escape(str(item.get('deduction_reason') or ''))}</td>"
            f"</tr>"
        )
    return f"""<h2>本地量化底稿</h2>
<p>本區為機械式資料檢查，不是 AI 最終投研評分。AI 最終投研評分請見主報告。</p>
<div class="table-wrap"><table>
<thead><tr><th>項目</th><th>機械分數</th><th>理由</th><th>扣分原因</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table></div>"""


def _sources_to_html(report_json: dict[str, Any]) -> str:
    sources = report_json.get("sources") or []
    if not sources:
        return ""
    cards = ["<h2>完整資料來源</h2>"]
    for item in sources:
        sid = html.escape(str(item.get("source_id") or ""))
        level = html.escape(str(item.get("source_level") or ""))
        title = html.escape(str(item.get("title") or item.get("url") or ""))
        url = html.escape(str(item.get("url") or ""))
        date = html.escape(str(item.get("published_date") or ""))
        snippet = html.escape(str(item.get("snippet") or ""))
        provider = html.escape(str(item.get("provider") or "unknown"))
        provider_detail = html.escape(str(item.get("provider_detail") or ""))
        link = f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>' if url.startswith(("http://", "https://")) else url
        meta_parts = [part for part in [level, provider, date] if part]
        if provider_detail:
            meta_parts.append(provider_detail)
        meta = " / ".join(meta_parts)
        cards.append(f'<article class="source-card"><div class="source-title">[{sid}] {title}</div><div class="source-meta">{meta}</div><div>{link}</div>{f"<p>{snippet}</p>" if snippet else ""}</article>')
    return "\n".join(cards)


def _metadata_summary(report_json: dict[str, Any]) -> dict[str, Any]:
    metadata = report_json.get("metadata") or {}
    return {
        "report_type": report_json.get("report_type"),
        "target": report_json.get("target"),
        "mode": report_json.get("mode"),
        "report_date": report_json.get("report_date"),
        "ai_used": metadata.get("ai_used"),
        "analysis_model": metadata.get("analysis_model"),
        "comparison_reports": metadata.get("comparison_reports"),
        "gemini_search_diagnostics": metadata.get("gemini_search_diagnostics"),
        "gemini_search_discovery": metadata.get("gemini_search_discovery"),
        "minimax_search_discovery": metadata.get("minimax_search_discovery"),
        "tavily_search_discovery": metadata.get("tavily_search_discovery"),
        "shared_data_layer": metadata.get("shared_data_layer"),
        "news_context": metadata.get("news_context"),
        "feature_pack": metadata.get("feature_pack"),
        "data_coverage": metadata.get("data_coverage"),
        "qa_validation": metadata.get("qa_validation"),
    }


def summarize_for_telegram(markdown: str, limit: int = 1200) -> str:
    markdown = normalize_report_text(sanitize_report_markdown(markdown))
    title = _first_heading(markdown)
    cleaned = re.sub(r"[#*_`>]+", "", markdown)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    key_lines = _important_lines(cleaned)
    parts = [
        title,
        "",
        "總結：",
        _first_paragraph(cleaned),
        "",
        "關鍵判斷:",
        *[f"{index}. {line}" for index, line in enumerate(key_lines[:3], 1)],
        "",
        "完整報告：",
        "已附上 Markdown 與 HTML 檔案。",
    ]
    summary = "\n".join(part for part in parts if part is not None).strip()
    if len(summary) > limit:
        summary = summary[: limit - 24].rstrip() + "\n\n完整報告已附上。"
    return summary


def fallback_markdown(request: CommandRequest, structured_data: dict[str, Any], sources: list[SourceItem], reason: str | None = None) -> str:
    title = _report_title(request)
    source_lines = [f"- [{item.source_id}] {item.source_level} {item.title} {item.url}" for item in sources] or ["- 目前沒有外部來源。"]
    data_block = json.dumps(structured_data, ensure_ascii=False, indent=2, default=str)[:12000]
    fallback_note = f"\n\n公開網路搜尋失敗，本報告僅使用本地結構化資料。原因：{reason}" if reason else ""
    if request.source_only:
        analysis_note = "本報告為 source-only 模式，只整理資料與來源，不做主觀分析、不做評分。"
    else:
        analysis_note = "AI 分析目前使用本地 fallback 產生，若資料不足會保守標示。"
    imagination_block = _fallback_market_imagination_markdown(request, structured_data)
    return f"""# {title}

## 摘要
⚠️ 這不是正式 AI 完成報告，而是本地資料 fallback 報告。請勿把本報告視為完整 AI 投研結論。

{analysis_note}{fallback_note}

## 資料基準
- 模式：{request.mode}
- 日期：{request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat()}
- 目標：{request.target or request.market_scope or request.candidate_pool or 'latest'}

{imagination_block}

## 結構化資料
```json
{data_block}
```

## 共享資料層摘要
{_shared_data_layer_markdown(structured_data)}

## 本地量化底稿（機械式資料檢查，非最終投研評分）
{_score_markdown(structured_data)}

## 主要限制
- 歷史日期模式目前採保守切片，部分來源若無發布日期會排除或標註資料不足。
- 論壇資料屬 best-effort；若網站改版或阻擋，會降級但不中斷報告。
- 本報告不構成投資建議。

## 資料來源列表
{chr(10).join(source_lines)}

## 風險提醒
- 若公開網路搜尋或來源日期不足，結論可信度會下降。
- 若只有價格或籌碼資料，無法完整判斷基本面與題材真實性。

{DISCLAIMER}
"""


def _fallback_market_imagination_markdown(request: CommandRequest, structured_data: dict[str, Any]) -> str:
    if request.source_only:
        return ""
    if request.command == "value_scan":
        candidates = (structured_data or {}).get("ai_candidates") or (structured_data or {}).get("candidates") or []
        if not candidates:
            return ""
        lines = [
            "## 市場推演摘要（fallback 版）",
            "",
            "這一段不是最終買賣建議，而是在 AI 模型失敗時，用本地底稿保留最低限度的投研推演骨架。",
            "",
            "### 市場正在交易什麼故事",
        ]
        stories = []
        clues = []
        catalysts = []
        missing = []
        failures = []
        for row in candidates[:3]:
            if not isinstance(row, dict):
                continue
            title = " ".join(str(part) for part in (row.get("code"), row.get("name")) if part)
            stories.append(f"- {title}：{_market_story_summary(row)}")
            clues.append(f"- {title}：{_early_clue_summary(row)}")
            catalysts.append(f"- {title}：{_catalyst_summary(row)}")
            missing.append(f"- {title}：{_missing_breakout_signal_summary(row)}")
            failures.append(f"- {title}：{_failure_condition_summary(row)}")
        lines.extend(stories or ["- 目前資料不足，無法形成明確市場故事。"])
        lines.extend(["", "### 早期蛛絲馬跡", ""])
        lines.extend(clues or ["- 目前早期訊號不足，需補技術、籌碼、營收、新聞與產業資料。"])
        lines.extend(["", "### 下一波可能發酵的催化劑", ""])
        lines.extend(catalysts or ["- 需要補足營收、產品、客戶、法說或籌碼證據。"])
        lines.extend(["", "### 如果要大漲，還缺什麼訊號", ""])
        lines.extend(missing or ["- 需要更完整的官方與產業證據。"])
        lines.extend(["", "### 反向驗證與失敗條件", ""])
        lines.extend(failures or ["- 若營收、籌碼與題材證據無法延續，應降級為觀察名單。"])
        lines.extend(["", "### 想像力結論", ""])
        lines.append("目前只能把市場想像視為候選假說；只有當營收、產品、客戶、供應鏈與籌碼訊號互相支持時，才可升級為高可信度重估。")
        return "\n".join(lines).strip()
    return (
        "## 市場推演摘要（fallback 版）\n\n"
        "AI 模型失敗時，本地 fallback 僅能保守呈現結構化資料。請優先檢查事實、推論、市場假說、反證與資料缺口是否足夠。"
    )


def _buy_rating_markdown(structured_data: dict[str, Any]) -> str:
    rating = (structured_data.get("local_scoring") or {}).get("buy_rating")
    if not rating:
        return "- 本模式不產生推薦買入評分。"
    return f"- {rating.get('score')}/{rating.get('max')}：{rating.get('label')}。{rating.get('reason')} 風險：{rating.get('risk')}"

def _score_markdown(structured_data: dict[str, Any]) -> str:
    scores = ((structured_data.get("local_scoring") or {}).get("scores") or [])
    if not scores:
        return "- 本模式不產生本地量化底稿。"
    lines = ["- 此分數僅為結構化資料檢查結果，不是 AI 最終投研評分。", ""]
    lines.extend(
        f"- {item.get('score_name')}: {item.get('score_value')}/{item.get('score_max')}。{item.get('score_reason')} 扣分：{item.get('deduction_reason')}"
        for item in scores
    )
    return "\n".join(lines)


def _report_title(request: CommandRequest) -> str:
    target = request.target or request.market_scope or request.candidate_pool or "latest"
    if request.command == "theme_radar":
        return f"{target} 市場題材雷達報告"
    if request.command == "theme_flow":
        return f"{target} 題材擴散路徑報告"
    if request.command == "sector_strength":
        return f"{target} 傳統類股強弱報告"
    labels = {
        "research": "個股研究報告",
        "macro": "宏觀市場報告",
        "theme": "題材研究報告",
        "value_scan": "價值重估掃描報告",
        "report": "歷史報告查詢",
    }
    return f"{target} {labels.get(request.command, 'AI 投研報告')}"


def _make_report_id(request: CommandRequest, report_variant: str | None = None) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = _safe_slug(request.target or request.market_scope or request.candidate_pool or request.command)
    variant = f"_{_safe_slug(report_variant)}" if report_variant else ""
    return f"{request.command}_{target}{variant}_{stamp}_{uuid4().hex[:8]}"


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]+", "_", str(value)).strip("_")
    return slug[:80] or "report"


def _markdown_to_html(markdown: str) -> str:
    lines: list[str] = []
    in_code = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            lines.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            lines.append(html.escape(line))
        elif line.startswith("# "):
            lines.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            lines.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("### "):
            lines.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("- "):
            lines.append(f"<p>{html.escape(line)}</p>")
        elif not line:
            lines.append("")
        else:
            lines.append(f"<p>{html.escape(line)}</p>")
    return "\n".join(lines)


def _parse_sections(markdown: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current_title = "摘要"
    current_lines: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if current_lines:
                content = "\n".join(current_lines).strip()
                sections.append({"section_title": current_title, "content": content, "evidence_sources": _source_refs(content)})
            current_title = line[3:].strip()
            current_lines = []
        elif not line.startswith("# "):
            current_lines.append(line)
    if current_lines:
        content = "\n".join(current_lines).strip()
        sections.append({"section_title": current_title, "content": content, "evidence_sources": _source_refs(content)})
    return sections


def _extract_list_after_heading(markdown: str, keywords: tuple[str, ...]) -> list[str]:
    results: list[str] = []
    capture = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            capture = any(keyword in line for keyword in keywords)
            continue
        if capture and line.startswith("- "):
            results.append(line[2:].strip())
        elif capture and line.startswith("#"):
            break
    return results[:10]


def _normalize_scores(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in scores:
        normalized.append(
            {
                "score_name": str(item.get("score_name") or "未命名評分"),
                "score_value": float(item.get("score_value") or 0),
                "score_max": float(item.get("score_max") or 100),
                "score_reason": str(item.get("score_reason") or "資料不足，無法判斷。"),
                "deduction_reason": str(item.get("deduction_reason") or "資料不足或未提供扣分原因。"),
            }
        )
    return normalized


def _source_refs(text: str) -> list[str]:
    return sorted(set(re.findall(r"S\d{3}", text)))


def _first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "AI 投研摘要"


def _first_paragraph(text: str) -> str:
    for block in text.split("\n\n"):
        clean = block.strip()
        if clean and not clean.startswith("資料來源"):
            return clean[:300]
    return "資料不足，無法產生摘要。"


def _important_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        clean = line.strip(" -0123456789.、")
        if not clean:
            continue
        if any(keyword in clean for keyword in ("分", "風險", "利多", "觀察", "重估", "籌碼", "營收", "盤面", "新聞", "強勢", "來源", "媒體")):
            lines.append(clean[:160])
    return lines[:6]
