from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from .models import CommandRequest, SourceItem
from .prompt_logging import write_prompt_log

ProgressCallback = Callable[[str], None]

THEME_ANALYSIS_COMMANDS = {"theme_radar", "theme_flow", "sector_strength"}
SEGMENTED_ANALYSIS_PROMPT_THRESHOLD = 120_000
SEGMENTED_ANALYSIS_TARGET_CHARS = 80_000
SEGMENTED_ANALYSIS_HARD_CHARS = 120_000
SEGMENTED_ANALYSIS_FINAL_HARD_CHARS = 300_000
SEGMENTED_ANALYSIS_CALL_TIMEOUT_SECONDS = 900.0
SEGMENTED_ANALYSIS_MAX_HIGH_MODEL_PACKET_SEGMENTS = 12


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


def _call_ai_with_timeout_setting(ai_client: ReportGenerator, prompt: str, timeout_seconds: float) -> Any:
    if not hasattr(ai_client, "timeout_seconds"):
        return ai_client.generate_report(prompt)
    old_timeout = getattr(ai_client, "timeout_seconds")
    try:
        setattr(ai_client, "timeout_seconds", timeout_seconds)
        return ai_client.generate_report(prompt)
    finally:
        setattr(ai_client, "timeout_seconds", old_timeout)


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
    call_timeout_seconds: float = SEGMENTED_ANALYSIS_CALL_TIMEOUT_SECONDS,
    progress: ProgressCallback | None = None,
) -> SegmentedAnalysisResult:
    """Run market-theme commands through multiple smaller AI calls.

    Full structured data remains in the caller and report JSON. This service
    only builds task-focused prompt slices so providers with smaller context
    windows do not receive the entire local data pack at once.
    """

    base_plans = _segment_plans(request, structured_data)
    plans = _split_oversized_plans(request, structured_data, base_plans)
    if len(plans) > SEGMENTED_ANALYSIS_MAX_HIGH_MODEL_PACKET_SEGMENTS:
        _emit(
            progress,
            f"分段 AI 分析預估 {len(plans)} 段，超過上限 {SEGMENTED_ANALYSIS_MAX_HIGH_MODEL_PACKET_SEGMENTS}，改用保真核心包整合模式。",
        )
        plans = _bounded_integration_plans(request, structured_data, plans)
    segment_runs: list[SegmentRun] = []
    prompt_paths: list[str] = []
    outputs: list[dict[str, Any]] = []
    _emit(progress, f"分段 AI 分析開始：segments={len(plans)} model={model_name} timeout={int(call_timeout_seconds)}s")

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
                "call_timeout_seconds": call_timeout_seconds,
            },
        )
        prompt_paths.append(str(prompt_path))
        _emit(
            progress,
            f"分段 AI 呼叫 {index}/{len(plans)}：{plan['title']} prompt={len(prompt)} chars est_tokens={max(1, len(prompt) // 4)} sources={len(segment_sources)} timeout={int(call_timeout_seconds)}s",
        )
        _emit(progress, f"分段 AI {index}/{len(plans)}：{plan['title']}，prompt={len(prompt)} chars")
        started = time.monotonic()
        try:
            result = _call_ai_with_timeout_setting(ai_client, prompt, call_timeout_seconds)
            markdown = str(getattr(result, "markdown", "") or "").strip()
            diagnostics = dict(getattr(result, "diagnostics", {}) or {})
            elapsed = time.monotonic() - started
            diagnostics = {**diagnostics, "elapsed_seconds": round(elapsed, 2), "timeout_seconds": call_timeout_seconds}
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
            _emit(progress, f"分段 AI 完成 {index}/{len(plans)}：{plan['title']} output={len(markdown)} chars elapsed={elapsed:.1f}s")
            _emit(progress, f"分段 AI 完成：{plan['title']}，output={len(markdown)} chars")
        except Exception as exc:
            elapsed = time.monotonic() - started
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
                diagnostics={"elapsed_seconds": round(elapsed, 2), "timeout_seconds": call_timeout_seconds},
            )
            outputs.append(_segment_output(plan, fallback, run))
            _emit(progress, f"分段 AI 失敗 {index}/{len(plans)}：{plan['title']}，已保留該段並繼續；elapsed={elapsed:.1f}s error={exc}")
            _emit(progress, f"分段 AI 失敗，改用本地段落摘要：{plan['title']}，原因：{exc}")
        segment_runs.append(run)

    final_sources = _sources_for_segment_outputs(outputs, sources)
    final_prompt = _build_final_prompt(request, structured_data, outputs, final_sources)
    final_prompt_too_large = len(final_prompt) > SEGMENTED_ANALYSIS_FINAL_HARD_CHARS
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
            "call_timeout_seconds": call_timeout_seconds,
        },
    )
    prompt_paths.append(str(final_prompt_path))
    _emit(progress, f"分段 AI 最終整合開始：prompt={len(final_prompt)} chars est_tokens={max(1, len(final_prompt) // 4)} sources={len(final_sources)} timeout={int(call_timeout_seconds)}s")
    _emit(progress, f"分段 AI 最終整合：prompt={len(final_prompt)} chars")
    final_started = time.monotonic()
    try:
        if final_prompt_too_large:
            raise ValueError(
                f"segmented final prompt too large: {len(final_prompt)} chars "
                f"> {SEGMENTED_ANALYSIS_FINAL_HARD_CHARS}"
            )
        final_result = _call_ai_with_timeout_setting(ai_client, final_prompt, call_timeout_seconds)
        markdown = str(getattr(final_result, "markdown", "") or "").strip()
        final_diagnostics = dict(getattr(final_result, "diagnostics", {}) or {})
        final_elapsed = time.monotonic() - final_started
        final_diagnostics = {**final_diagnostics, "elapsed_seconds": round(final_elapsed, 2), "timeout_seconds": call_timeout_seconds}
        raw = dict(getattr(final_result, "raw", {}) or {})
        final_status = "success"
        final_error = None
        _emit(progress, f"分段 AI 最終整合完成：output={len(markdown)} chars elapsed={final_elapsed:.1f}s")
        _emit(progress, f"分段 AI 最終整合完成：output={len(markdown)} chars")
    except Exception as exc:
        final_elapsed = time.monotonic() - final_started
        markdown = _compose_segmented_fallback_report(request, outputs, exc)
        final_diagnostics = {"status": "fallback", "error": str(exc), "elapsed_seconds": round(final_elapsed, 2), "timeout_seconds": call_timeout_seconds}
        if final_prompt_too_large:
            final_diagnostics["final_prompt_too_large"] = True
            final_diagnostics["final_prompt_hard_chars"] = SEGMENTED_ANALYSIS_FINAL_HARD_CHARS
        raw = {}
        final_status = "fallback"
        final_error = str(exc)
        _emit(progress, f"分段 AI 最終整合失敗，改用分段底稿 fallback：elapsed={final_elapsed:.1f}s error={exc}")
        _emit(progress, f"分段 AI 最終整合失敗，改用分段摘要組報告：{exc}")

    diagnostics = {
        "mode": "segmented_theme_analysis",
        "model": model_name,
        "command": request.command,
        "trigger": trigger,
        "original_prompt_chars": original_prompt_chars,
        "threshold_chars": threshold_chars,
        "target_segment_chars": SEGMENTED_ANALYSIS_TARGET_CHARS,
        "hard_segment_chars": SEGMENTED_ANALYSIS_HARD_CHARS,
        "call_timeout_seconds": call_timeout_seconds,
        "segment_count": len(segment_runs),
        "success_count": sum(1 for item in segment_runs if item.status == "success"),
        "fallback_count": sum(1 for item in segment_runs if item.status != "success"),
        "final_status": final_status,
        "final_error": final_error,
        "final_prompt_chars": len(final_prompt),
        "final_diagnostics": final_diagnostics,
        "actual_model": final_diagnostics.get("actual_model") or final_diagnostics.get("model") or model_name,
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


def _split_oversized_plans(
    request: CommandRequest,
    structured_data: dict[str, Any],
    plans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for plan in plans:
        if plan.get("no_auto_split"):
            result.append(plan)
            continue
        probe_prompt = _build_segment_prompt(request, structured_data, plan, [])
        if len(probe_prompt) <= SEGMENTED_ANALYSIS_HARD_CHARS:
            result.append(plan)
            continue
        chunks = _split_payload_for_segment(plan.get("payload") or {}, target_chars=SEGMENTED_ANALYSIS_TARGET_CHARS)
        if len(chunks) <= 1:
            result.append(plan)
            continue
        total = len(chunks)
        for index, chunk in enumerate(chunks, 1):
            result.append(
                {
                    **plan,
                    "label": f"{plan.get('label', 'segment')}_part_{index}",
                    "title": f"{plan.get('title', '分段資料')} {index}/{total}",
                    "payload": {
                        "segment_split": {
                            "source_label": plan.get("label"),
                            "part_index": index,
                            "part_total": total,
                            "policy": "原始段落過大，改為更小完整分段；不刪除核心資料。",
                        },
                        **chunk,
                    },
                }
            )
    return result


def _bounded_integration_plans(
    request: CommandRequest,
    structured_data: dict[str, Any],
    plans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep high-model calls bounded without deleting source data.

    When a legacy plan would explode into many small prompts, the official
    high-model input package is preferred because it already contains the
    deduplicated stock index, relation tables, evidence, source index, and
    audit metadata. Full raw data remains in report JSON and HTML appendices.
    """

    packet_plans = _high_model_packet_plans(structured_data)
    if packet_plans:
        return packet_plans[:SEGMENTED_ANALYSIS_MAX_HIGH_MODEL_PACKET_SEGMENTS]
    return plans[:SEGMENTED_ANALYSIS_MAX_HIGH_MODEL_PACKET_SEGMENTS]


def _split_payload_for_segment(payload: Any, *, target_chars: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return [{"payload": payload}]
    chunks: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    current_chars = 0
    for key, value in payload.items():
        entry = {key: value}
        entry_chars = len(_json(_compact_segment_payload(entry)))
        if entry_chars > target_chars:
            if current:
                chunks.append(current)
                current = {}
                current_chars = 0
            for part_index, part in enumerate(_split_value_for_segment(value, target_chars=target_chars), 1):
                chunks.append({f"{key}__part_{part_index}": part})
            continue
        if current and current_chars + entry_chars > target_chars:
            chunks.append(current)
            current = {}
            current_chars = 0
        current[key] = value
        current_chars += entry_chars
    if current:
        chunks.append(current)
    return chunks or [payload]


def _split_value_for_segment(value: Any, *, target_chars: int) -> list[Any]:
    if isinstance(value, list):
        chunks: list[list[Any]] = []
        current: list[Any] = []
        current_chars = 0
        for item in value:
            item_chars = len(_json(_compact_segment_payload(item)))
            if item_chars > target_chars:
                if current:
                    chunks.append(current)
                    current = []
                    current_chars = 0
                chunks.extend([[part] for part in _split_value_for_segment(item, target_chars=target_chars)])
                continue
            if current and current_chars + item_chars > target_chars:
                chunks.append(current)
                current = []
                current_chars = 0
            current.append(item)
            current_chars += item_chars
        if current:
            chunks.append(current)
        return chunks or [[]]
    if isinstance(value, dict):
        return _split_payload_for_segment(value, target_chars=target_chars)
    if isinstance(value, str) and len(value) > target_chars:
        return [
            {"資料型態": "完整分段文字", "段落": index + 1, "內容": value[start:start + target_chars]}
            for index, start in enumerate(range(0, len(value), target_chars))
        ]
    return [value]


def _segment_plans(request: CommandRequest, data: dict[str, Any]) -> list[dict[str, Any]]:
    packet_plans = _high_model_packet_plans(data)
    if packet_plans:
        return packet_plans
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


def _high_model_packet_plans(data: dict[str, Any]) -> list[dict[str, Any]]:
    package = data.get("high_model_input_package")
    if not isinstance(package, dict):
        return []
    command_specific = package.get("command_specific_data")
    if not isinstance(command_specific, dict):
        return []
    command_payload = command_specific.get("payload")
    if not isinstance(command_payload, dict) or not command_payload:
        return []
    plans = [
        {
            "label": "local_core_packet",
            "title": "本地核心資料包",
            "no_auto_split": True,
            "payload": {
                "input_policy": "本段使用共用本地整理模組產出的等價重組資料，不是語意刪減摘要。",
                "command_specific_data": {
                    "schema_version": command_specific.get("schema_version"),
                    "input_mode": command_specific.get("input_mode"),
                    "core_input_audit": command_specific.get("core_input_audit"),
                    "payload": command_payload,
                },
            },
        },
        {
            "label": "evidence_and_low_model",
            "title": "證據、反證與低階模型底稿",
            "no_auto_split": True,
            "payload": {
                "unified_evidence_pack": package.get("unified_evidence_pack"),
                "low_model_digest": package.get("low_model_digest"),
                "low_model_validation": package.get("low_model_validation"),
                "low_model_input_policy": package.get("low_model_input_policy"),
                "low_model_text_evidence_count": package.get("low_model_text_evidence_count"),
                "low_model_skipped_structured_sections": package.get("low_model_skipped_structured_sections"),
                "data_gap_summary": package.get("data_gap_summary"),
                "report_confidence": package.get("report_confidence"),
            },
        },
        {
            "label": "sources_and_excerpts",
            "title": "來源索引與必要原文摘錄",
            "no_auto_split": True,
            "payload": {
                "selected_sources": package.get("selected_sources"),
                "required_original_excerpts": package.get("required_original_excerpts"),
                "complete_source_index": package.get("complete_source_index"),
                "full_data_locations": package.get("full_data_locations"),
            },
        },
        {
            "label": "local_scoring_and_audit",
            "title": "本地量化底稿與入模審計",
            "no_auto_split": True,
            "payload": {
                "local_scoring": package.get("local_scoring"),
                "ai_data_center": package.get("ai_data_center"),
                "ai_input_audit": package.get("ai_input_audit"),
                "workflow_policy": package.get("workflow_policy"),
                "token_budget_policy": package.get("token_budget_policy"),
            },
        },
    ]
    return [plan for plan in plans if _has_payload_value(plan.get("payload"))]


def _has_payload_value(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_payload_value(item) for item in value.values())
    if isinstance(value, list):
        return bool(value)
    return value not in (None, "", [], {})


def _build_segment_prompt(
    request: CommandRequest,
    data: dict[str, Any],
    plan: dict[str, Any],
    prior_outputs: list[dict[str, Any]],
) -> str:
    payload = _compact_segment_payload(plan.get("payload") or {})
    prior_state = _prior_outputs_state(prior_outputs)
    return "\n".join(
        [
            "# 台股 AI 投研分段閱讀任務",
            "",
            f"指令：/{request.command}",
            f"分析日期：{data.get('report_date') or request.report_date or 'latest'}",
            f"目前段落：{plan.get('title')}",
            "",
            "請嚴格遵守：",
            "- 本段資料已由本地系統做機械式去重、分類與分段；不得再因主觀判斷刪除題材、公司、來源或反證。",
            "- 只能根據本段資料與前段結論判斷，不要憑空新增公司或題材。",
            "- 產業/子族群強勢可以作為市場線索，但不能直接說成已驗證題材證據。",
            "- 若有題材、族群、公司、供應鏈資料，請保留代號、名稱、關係、證據來源，不得只寫概括摘要。",
            "- 若資料不足，請明確標示「市場強勢、題材證據待補」或「資料不足」。",
            "- 不得輸出買進、賣出、目標價、保證獲利、必漲等投資指令。",
            "- 請用 Markdown，輸出可供最終整合模型引用的分段閱讀筆記。",
            "",
            "前段結論：",
            _json(prior_state),
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
            "請根據下列分段結論、本地摘要與來源索引，整合成一份正式 Markdown 報告。",
            "",
            "硬規則：",
            "- 必須分開說明「市場強弱族群」與「題材庫證據映射」。",
            "- 不得預設 AI、半導體、伺服器為主線；以分段市場結論為準。",
            "- 若某族群市場很強但題材庫證據不足，要標示「市場強勢、題材證據待補」。",
            "- 若有命中公司、代表股或候選股，必須列出股票代號與名稱，不得只寫數量。",
            "- 代表股只能來自已驗證或合理推論；候選股只能稱為觀察名單。",
            "- 請保留題材擴散推論，但推論要標示依據與待驗證點。",
            "- 不得輸出買進、賣出、加碼、追價、停損、停利、目標價、保證獲利。",
            "",
            "本地摘要資料：",
            _json(_final_local_summary(data)),
            "",
            "分段分析結果：",
            _json(_segment_outputs_state(outputs)),
            "",
            "可引用來源清單：",
            _json(_source_refs(sources)),
        ]
    )


def _prior_outputs_state(outputs: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep prior segment continuity without resending every Markdown note."""

    status_counts: dict[str, int] = {}
    processed: list[dict[str, Any]] = []
    for item in outputs:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        markdown = str(item.get("markdown") or "")
        processed.append(
            {
                "label": item.get("label"),
                "title": item.get("title"),
                "status": status,
                "error": item.get("error"),
                "output_chars": len(markdown),
            }
        )
    recent_notes: list[dict[str, Any]] = []
    for item in outputs[-3:]:
        if not isinstance(item, dict):
            continue
        markdown = str(item.get("markdown") or "").strip()
        if markdown:
            recent_notes.append(
                {
                    "label": item.get("label"),
                    "title": item.get("title"),
                    "note_excerpt": _truncate_segment_text(markdown, 1200),
                }
            )
    return {
        "policy": "prior segment outputs are represented as a running state table; full intermediate Markdown is not resent to avoid prompt growth.",
        "processed_segment_count": len(processed),
        "status_counts": status_counts,
        "processed_segments": processed,
        "recent_note_excerpts": recent_notes,
    }


def _segment_outputs_state(outputs: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize segment outputs for final synthesis without quadratic growth."""

    rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for item in outputs:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        markdown = str(item.get("markdown") or "").strip()
        rows.append(
            {
                "label": item.get("label"),
                "title": item.get("title"),
                "status": status,
                "error": item.get("error"),
                "output_chars": len(markdown),
                "note_excerpt": _truncate_segment_text(markdown, 2200),
            }
        )
    return {
        "policy": "final synthesis receives one bounded note per segment; full intermediate Markdown remains in prompt logs and diagnostics.",
        "segment_count": len(rows),
        "status_counts": status_counts,
        "segments": rows,
    }


def _truncate_segment_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...(intermediate note truncated; full note is stored in prompt logs)"


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
            f"本段 AI 分析失敗，已保留本地完整分段資料供最終整合與入模審計使用。原因：{exc}",
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
            "top_gainers": _stocks(market.get("top_gainers"), 0),
            "top_losers": _stocks(market.get("top_losers"), 0),
            "top_volume_surge": _stocks(market.get("top_volume_surge"), 0),
            "top_turnover": _stocks(market.get("top_turnover"), 0),
            "top_trend_strength": _stocks(market.get("top_trend_strength"), 0),
            "new_highs": _stocks(market.get("new_highs"), 0),
            "new_lows": _stocks(market.get("new_lows"), 0),
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
        "sector_rankings": _sector_ranking_rows(sector_data.get("sector_rankings") or data.get("sector_rankings") or [], limit=0, sample_limit=0),
        "analysis_policy": data.get("analysis_policy"),
    }


def _market_subsector_strength_payload(data: dict[str, Any]) -> dict[str, Any]:
    sector_data = data if data.get("command_role") == "sector_strength" else data.get("sector_strength") or {}
    return {
        "subsector_rankings": _subsector_ranking_rows(sector_data.get("subsector_rankings") or data.get("subsector_rankings") or [], limit=0, sample_limit=0),
        "analysis_policy": data.get("analysis_policy"),
    }


def _market_payload(data: dict[str, Any]) -> dict[str, Any]:
    market = data.get("market_movers") or {}
    sector_data = data if data.get("command_role") == "sector_strength" else data.get("sector_strength") or {}
    return {
        "market_movers": {
            "market_data_date": market.get("market_data_date"),
            "source_mode": market.get("source_mode"),
            "top_gainers": _stocks(market.get("top_gainers"), 0),
            "top_losers": _stocks(market.get("top_losers"), 0),
            "top_volume_surge": _stocks(market.get("top_volume_surge"), 0),
            "top_turnover": _stocks(market.get("top_turnover"), 0),
            "new_highs": _stocks(market.get("new_highs"), 0),
            "new_lows": _stocks(market.get("new_lows"), 0),
            "sector_mover_rankings": _sector_mover_rows(market.get("sector_mover_rankings") or [], limit=0, sample_limit=0),
        },
        "sector_rankings": _sector_ranking_rows(sector_data.get("sector_rankings") or data.get("sector_rankings") or [], limit=0, sample_limit=0),
        "subsector_rankings": _subsector_ranking_rows(sector_data.get("subsector_rankings") or data.get("subsector_rankings") or [], limit=0, sample_limit=0),
        "data_quality": _compact(data.get("data_quality") or sector_data.get("data_quality") or {}, depth=3, max_list=20, max_keys=60),
    }


def _sector_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "sector_rankings": _sector_ranking_rows(data.get("sector_rankings") or [], limit=0, sample_limit=0),
        "subsector_rankings": _subsector_ranking_rows(data.get("subsector_rankings") or [], limit=0, sample_limit=0),
        "analysis_policy": data.get("analysis_policy"),
    }


def _theme_rankings_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "theme_rankings": _theme_ranking_rows(data.get("theme_rankings") or [], limit=0, sample_limit=0),
        "topic_library_summary": data.get("topic_library_summary"),
        "analysis_policy": data.get("analysis_policy"),
    }


def _theme_strong_stocks_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "strong_stocks": _stocks(data.get("strong_stocks"), 0),
        "analysis_policy": data.get("analysis_policy"),
    }


def _theme_news_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "news_theme_stats": _compact(data.get("news_theme_stats") or [], depth=4, max_list=0, max_keys=0, max_string=600),
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
        "theme_flow_summaries": _compact(data.get("theme_flow_summaries") or [], depth=5, max_list=0, max_keys=0, max_string=1200),
        "theme_rankings": _theme_ranking_rows(data.get("theme_rankings") or [], limit=0, sample_limit=0),
        "subsector_rankings": _subsector_ranking_rows(data.get("subsector_rankings") or [], limit=0, sample_limit=0),
    }


def _theme_flow_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        **_theme_flow_profile_payload(data),
        "related_stocks": _stocks(data.get("related_stocks"), 0),
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
        "related_stocks": _stocks(rows, 0),
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
        "top_gainers": _stocks(market.get("top_gainers"), 30),
        "top_volume_surge": _stocks(market.get("top_volume_surge"), 30),
        "top_turnover": _stocks(market.get("top_turnover"), 30),
        "new_highs": _stocks(market.get("new_highs"), 30),
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
    selected_rows = rows if limit <= 0 else rows[:limit]
    for row in selected_rows:
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
    selected_rows = rows if limit <= 0 else rows[:limit]
    for row in selected_rows:
        if not isinstance(row, dict):
            continue
        item = {key: row.get(key) for key in scalar_keys if row.get(key) not in (None, "", [])}
        for key in ("sector_strong_samples", "representative_stocks", "candidate_stocks"):
            samples = _stocks(row.get(key), sample_limit)
            if samples:
                item[key] = samples
        top_subsectors = _subsector_ranking_rows(row.get("top_subsectors") or [], limit=0 if sample_limit <= 0 else 5, sample_limit=sample_limit)
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
    selected_rows = rows if limit <= 0 else rows[:limit]
    for row in selected_rows:
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
    selected_rows = rows if limit <= 0 else rows[:limit]
    for row in selected_rows:
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
    selected_rows = rows if limit <= 0 else rows[:limit]
    for row in selected_rows:
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
        "sector_rankings": _final_top_rows(sector_data.get("sector_rankings") or data.get("sector_rankings") or [], limit=6),
        "subsector_rankings": _final_top_rows(sector_data.get("subsector_rankings") or data.get("subsector_rankings") or [], limit=8),
        "theme_rankings": _final_top_rows(data.get("theme_rankings") or [], limit=8),
        "strong_stocks": _stock_refs(data.get("strong_stocks") or data.get("market_movers") or [], 15),
        "data_quality": _final_compact(data.get("data_quality") or sector_data.get("data_quality") or {}, depth=3, max_list=12, max_keys=40),
    }


def _final_top_rows(rows: Any, *, limit: int) -> dict[str, Any]:
    if not isinstance(rows, list):
        return {"total_count": 0, "items": []}
    selected = rows[:limit]
    return {
        "total_count": len(rows),
        "included_count": len(selected),
        "omitted_count": max(0, len(rows) - len(selected)),
        "items": [_final_ranking_row(row) for row in selected if isinstance(row, dict)],
        "note": "Final synthesis receives top rows only; full rows remain in report JSON, sources, and segment prompt logs.",
    }


def _final_ranking_row(row: dict[str, Any]) -> dict[str, Any]:
    scalar_keys = (
        "sector",
        "sector_display_name",
        "subsector",
        "subsector_display_name",
        "theme_id",
        "theme_name",
        "theme_display_name",
        "sector_score",
        "subsector_score",
        "theme_strength_score",
        "score",
        "lifecycle",
        "sector_state",
        "subsector_state",
        "theme_state",
        "strong_stock_count",
        "active_breakout_count",
        "trend_pullback_count",
        "weak_count",
        "volume_surge_count",
        "new_high_count",
        "theme_hit_count",
        "direct_relation_count",
        "inferred_relation_count",
        "candidate_relation_count",
        "avg_change_pct",
        "avg_trend_score",
        "news_heat",
        "diffusion_potential",
        "interpretation_hint",
    )
    item = {
        key: row.get(key)
        for key in scalar_keys
        if row.get(key) not in (None, "", [])
    }
    for key in ("description", "summary", "reason"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            item[key] = _truncate_segment_text(value.strip(), 1200)
    for key in ("representative_stocks", "candidate_stocks", "strong_samples", "sector_strong_samples"):
        stocks = _stock_refs(row.get(key), 3)
        if stocks:
            item[key] = stocks
    nodes = row.get("strong_nodes") or row.get("nodes") or []
    if isinstance(nodes, list) and nodes:
        item["key_nodes"] = [_final_compact(node, depth=2, max_list=3, max_keys=12, max_string=220) for node in nodes[:5]]
        if len(nodes) > 5:
            item["omitted_node_count"] = len(nodes) - 5
    score_breakdown = row.get("score_breakdown")
    if isinstance(score_breakdown, dict):
        item["score_breakdown"] = _final_compact(score_breakdown, depth=2, max_list=6, max_keys=12, max_string=160)
    display_groups = row.get("display_stock_groups")
    if isinstance(display_groups, dict):
        item["display_stock_groups"] = {
            "verified_representatives": _stock_refs(display_groups.get("verified_representatives"), 3),
            "inferred_representatives": _stock_refs(display_groups.get("inferred_representatives"), 3),
            "candidate_watchlist": _stock_refs(display_groups.get("candidate_watchlist"), 3),
            "candidate_label": display_groups.get("candidate_label"),
        }
    return item


def _stock_refs(rows: Any, limit: int) -> list[dict[str, Any]]:
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
        "change_pct_20d",
        "volume_ratio",
        "turnover",
        "new_high_days",
        "days_since_high",
        "near_high_20d",
        "pullback_from_high_pct",
        "trend_score",
        "trend_state",
        "primary_theme_name",
    )
    result: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        result.append({key: row.get(key) for key in keys if row.get(key) not in (None, "", [])})
    return result


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
    selected_rows = rows if limit <= 0 else rows[:limit]
    for row in selected_rows:
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
        for source in sources
    ]


def _final_compact(
    value: Any,
    *,
    depth: int = 4,
    max_list: int = 20,
    max_keys: int = 80,
    max_string: int = 1600,
) -> Any:
    """Bound final synthesis payloads without embedding omitted raw data."""

    if depth <= 0:
        if isinstance(value, dict):
            return {"type": "dict", "key_count": len(value), "note": "omitted at depth limit"}
        if isinstance(value, (list, tuple)):
            return {"type": "list", "item_count": len(value), "note": "omitted at depth limit"}
        text = str(value)
        if len(text) > max_string:
            return text[:max_string].rstrip() + "...(omitted)"
        return value
    if isinstance(value, dict):
        items = list(value.items())
        selected = items[:max_keys] if max_keys > 0 else items
        result = {
            str(key): _final_compact(item, depth=depth - 1, max_list=max_list, max_keys=max_keys, max_string=max_string)
            for key, item in selected
        }
        if max_keys > 0 and len(items) > max_keys:
            result["omitted_key_count"] = len(items) - max_keys
        return result
    if isinstance(value, (list, tuple)):
        rows = list(value)
        selected = rows[:max_list] if max_list > 0 else rows
        result = [
            _final_compact(item, depth=depth - 1, max_list=max_list, max_keys=max_keys, max_string=max_string)
            for item in selected
        ]
        if max_list > 0 and len(rows) > max_list:
            result.append({"omitted_item_count": len(rows) - max_list})
        return result
    if isinstance(value, str) and len(value) > max_string:
        return value[:max_string].rstrip() + "...(omitted)"
    return value


def _compact_segment_payload(payload: Any) -> Any:
    return _compact(payload, depth=6, max_list=0, max_keys=0, max_string=1800)


def _sources_for_segment_plan(plan: dict[str, Any], sources: list[SourceItem]) -> list[SourceItem]:
    if not sources:
        return []
    label = str(plan.get("label") or "")
    if label.startswith("sources_and_excerpts"):
        return sources
    text = _json(plan.get("payload") or {})
    ids = set(re.findall(r"S\d{3,}", text))
    if ids:
        matched = [item for item in sources if item.source_id in ids]
        if matched:
            return matched
    return []


def _sources_for_segment_outputs(outputs: list[dict[str, Any]], sources: list[SourceItem]) -> list[SourceItem]:
    if not sources:
        return []
    text = _json(outputs)
    ids = set(re.findall(r"S\d{3,}", text))
    if ids:
        matched = [item for item in sources if item.source_id in ids]
        if matched:
            return matched
    return sources


def _compact(
    value: Any,
    *,
    depth: int = 4,
    max_list: int = 20,
    max_keys: int = 80,
    max_string: int = 1600,
) -> Any:
    if depth <= 0:
        if isinstance(value, dict):
            return {str(key): item for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return list(value)
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        selected_items = list(value.items()) if max_keys <= 0 else list(value.items())[:max_keys]
        for key, item in selected_items:
            result[str(key)] = _compact(item, depth=depth - 1, max_list=max_list, max_keys=max_keys, max_string=max_string)
        if max_keys > 0 and len(value) > max_keys:
            remaining = dict(list(value.items())[max_keys:])
            result["其餘欄位完整分段"] = _compact(remaining, depth=depth - 1, max_list=0, max_keys=0, max_string=max_string)
        return result
    if isinstance(value, (list, tuple)):
        rows = list(value)
        selected_rows = rows if max_list <= 0 else rows[:max_list]
        items = [
            _compact(item, depth=depth - 1, max_list=max_list, max_keys=max_keys, max_string=max_string)
            for item in selected_rows
        ]
        if max_list > 0 and len(rows) > max_list:
            remaining = rows[max_list:]
            items.append({
                "資料型態": "其餘資料完整分段",
                "總筆數": len(remaining),
                "資料未刪除": True,
                "資料": _compact(remaining, depth=depth - 1, max_list=0, max_keys=max_keys, max_string=max_string),
            })
        return items
    if isinstance(value, str) and len(value) > max_string:
        return {
            "資料型態": "完整分段文字",
            "總字數": len(value),
            "每段字數": max_string,
            "資料未刪除": True,
            "段落": [
                {"段號": index + 1, "文字": value[start:start + max_string]}
                for index, start in enumerate(range(0, len(value), max_string))
            ],
        }
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
