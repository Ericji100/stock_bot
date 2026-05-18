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
from .report_validator import append_qa_notes, validate_report

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
) -> dict[str, Any]:
    data = structured_data or {}
    local_scoring = data.get("local_scoring") or {}
    scores = _normalize_scores(local_scoring.get("scores") or [])
    buy_rating = local_scoring.get("buy_rating")
    return {
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
        "metadata": {
            "ai_used": ai_used,
            "report_variant": report_variant,
            "analysis_model": data.get("analysis_model"),
            "analysis_model_choice": data.get("analysis_model_choice"),
            "analysis_provider": data.get("analysis_provider"),
            "comparison_reports": data.get("comparison_reports"),
            "fallback_reason": fallback_reason,
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
            "minimax_diagnostics": data.get("minimax_diagnostics"),
            "opencode_diagnostics": data.get("opencode_diagnostics"),
            "value_scan_candidate_count": len(data.get("ai_candidates") or data.get("candidates") or []) if request.command == "value_scan" else None,
            "value_scan_candidates": _value_scan_candidate_refs(data) if request.command == "value_scan" else None,
        },
    }


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

    markdown = _append_value_scan_candidate_analysis(request, markdown, structured_data)
    markdown = _append_complete_source_appendix(markdown, sources)
    report_json = build_report_json(request, markdown, summary, sources, ai_used, fallback_reason, structured_data, report_variant)
    qa = validate_report(markdown, request, sources, report_json)
    markdown = append_qa_notes(markdown, qa)
    if markdown != report_json.get("markdown_validated_source"):
        report_json = build_report_json(request, markdown, summary, sources, ai_used, fallback_reason, structured_data, report_variant)
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
        json_path.write_text(json.dumps(report_json, ensure_ascii=False, indent=2), encoding="utf-8")
    sources_path.write_text(json.dumps([asdict(item) for item in sources], ensure_ascii=False, indent=2), encoding="utf-8")

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
        lines.append("- \u672a\u4f86 1\uff5e3 \u500b\u6708\u89c0\u5bdf\u91cd\u9ede\uff1a" + _watch_items_summary(row))
        lines.append("- \u98a8\u96aa\u8207\u53cd\u8b49\uff1a" + _risk_summary(row))
    return "\n".join(lines).strip() + "\n"


def _value_scan_candidate_refs(data: dict[str, Any]) -> list[dict[str, str]]:
    refs = []
    # 使用實際送 AI 的候選股（ai_candidates），不是本地 display 的 candidates
    for row in data.get("ai_candidates") or data.get("candidates") or []:
        refs.append({"code": str(row.get("code") or ""), "name": str(row.get("name") or "")})
    return refs


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
    lines.append("\u4ee5\u4e0b\u6e05\u55ae\u7531\u7a0b\u5f0f\u4f9d\u5be6\u969b\u4fdd\u5b58\u7684 sources.json \u4f86\u6e90\u81ea\u52d5\u9644\u52a0\uff1bAI \u6b63\u6587\u53ef\u80fd\u53ea\u5217\u51fa\u6709\u76f4\u63a5\u5f15\u7528\u7684\u90e8\u5206\u4f86\u6e90\u3002")
    lines.append("")
    for item in sources:
        title = item.title or item.url
        date_part = f"，date={item.published_date}" if item.published_date else ""
        provider = item.provider or "unknown"
        provider_detail = f"，provider_detail={item.provider_detail}" if item.provider_detail else ""
        snippet_part = f"，{item.snippet}" if item.snippet else ""
        lines.append(f"- [{item.source_id}] {item.source_level} / {provider} {title} - {item.url}{date_part}{provider_detail}{snippet_part}")
    return "\n".join(lines).strip() + "\n"

def render_html(report_json: dict[str, Any], markdown: str) -> str:
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
        "qa_validation": metadata.get("qa_validation"),
    }


def summarize_for_telegram(markdown: str, limit: int = 1200) -> str:
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
    return f"""# {title}

## 摘要
{analysis_note}{fallback_note}

## 資料基準
- 模式：{request.mode}
- 日期：{request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat()}
- 目標：{request.target or request.market_scope or request.candidate_pool or 'latest'}

## 結構化資料
```json
{data_block}
```

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
        if any(keyword in clean for keyword in ("分", "風險", "利多", "觀察", "重估", "籌碼", "營收")):
            lines.append(clean[:160])
    return lines[:6]








