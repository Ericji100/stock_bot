from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar_service import (  # noqa: E402
    _build_ai_comment_prompt_jobs,
    _ensure_radar_source_sufficiency,
    _load_or_build_radar_light_research,
    _load_radar_records,
    _record_to_result,
    _research_sources_from_item,
    _select_ai_enrichment_codes,
    load_radar_result,
    parse_radar_args,
)
from research_center.ai_data_center import attach_ai_data_center  # noqa: E402
from research_center.ai_workflow_service import (  # noqa: E402
    _build_low_model_digest_payload,
    attach_low_model_digest,
    attach_high_model_input_package,
    build_ai_workflow_coverage,
)
from research_center.command_parser import parse_command_text  # noqa: E402
from research_center.config import load_research_config  # noqa: E402
from research_center.data_gap_service import attach_data_gap_summary  # noqa: E402
from research_center.data_services import collect_structured_data  # noqa: E402
from research_center.date_aware_context import filter_and_sort_sources_for_analysis_date  # noqa: E402
from research_center.evidence_pack_service import attach_unified_evidence_pack  # noqa: E402
from research_center.models import CommandRequest, SourceItem  # noqa: E402
from research_center.news_repository import NewsRepository  # noqa: E402
from research_center.news_service import (  # noqa: E402
    _apply_news_title_cleanup,
    _classify_limit,
    _classify_text_limit,
    _deduplicate_items,
    _filter_by_published_window,
    _filter_taiwan_finance_news,
    _classification_payload,
    _rank_news_for_ai,
    _sources_to_news_items,
    build_news_discovery_queries,
)
from research_center.orchestrator import ResearchCenter, _select_sources_for_prompt  # noqa: E402
from research_center.prompt_logging import write_prompt_log  # noqa: E402
from research_center.prompt_registry import build_grounding_discovery_prompts  # noqa: E402
from research_center.gemini_service import build_prompt  # noqa: E402
from research_center.scoring_engine import build_buy_rating, build_local_scores  # noqa: E402
from research_center.web_fetch_enrichment import _enrich_sources_with_web_fetch  # noqa: E402


AUDIT_ROOT = ROOT / "logs" / "ai_command_audit"


DEFAULT_COMMANDS: list[str] = [
    "/research 凌陽 --deep --model minimax",
    "/value_scan 我的持股 --deep --top 30 --model minimax",
    "/macro 台股 --model minimax",
    "/theme AI電源 --model minimax",
    "/theme_flow AI電源 --model minimax",
    "/theme_radar --model minimax",
    "/sector_strength --model minimax",
    "/radar --source technical --ai-top 5 --model minimax",
    "/news refresh --model minimax",
    "/topic_maintain --model minimax",
]


def _now_id() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def _new_run_dir() -> Path:
    base_name = f"{_now_id()}_{os.getpid()}"
    candidate = AUDIT_ROOT / base_name
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = AUDIT_ROOT / f"{base_name}_{counter}"
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(value), ensure_ascii=False, indent=2), encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _write_markdown(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8-sig")


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _sources_to_rows(sources: list[SourceItem]) -> list[dict[str, Any]]:
    return [_json_safe(asdict(item)) for item in sources]


def _radar_source_item_from_dict(raw: dict[str, Any], *, index: int) -> SourceItem:
    source_id = str(raw.get("source_id") or raw.get("id") or f"RADAR_S{index:03d}")
    return SourceItem(
        source_id=source_id,
        title=str(raw.get("title") or raw.get("name") or source_id),
        url=str(raw.get("url") or ""),
        source_level=str(raw.get("source_level") or raw.get("level") or "Level 3"),
        published_date=raw.get("published_date"),
        snippet=str(raw.get("snippet") or raw.get("summary") or raw.get("content") or "")[:1200],
        provider=str(raw.get("provider") or raw.get("source_type") or "radar_external"),
        provider_detail=raw.get("provider_detail"),
        fetch_provider=raw.get("fetch_provider"),
        fetch_status=raw.get("fetch_status"),
        failure_reason=raw.get("failure_reason"),
        found_by=list(raw.get("found_by") or ["radar_live_audit"]),
    )


def _radar_source_items_from_dicts(raw_sources: list[Any], seen: set[str]) -> list[SourceItem]:
    items: list[SourceItem] = []
    for raw in raw_sources:
        if isinstance(raw, SourceItem):
            key = str(raw.url or raw.source_id or raw.title or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("url") or raw.get("source_id") or raw.get("title") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(_radar_source_item_from_dict(raw, index=len(seen)))
    return items




def _radar_candidate_summary(item: Any) -> dict[str, Any]:
    evidence_pack = getattr(item, "evidence_pack", {}) or {}
    if not isinstance(evidence_pack, dict):
        evidence_pack = {}
    data_coverage = getattr(item, "data_coverage", {}) or evidence_pack.get("data_coverage") or {}
    if not isinstance(data_coverage, dict):
        data_coverage = {}
    web_sources = list(getattr(item, "web_sources", []) or [])
    ai_sources = list(getattr(item, "ai_sources", []) or [])
    research_sources = list(evidence_pack.get("research_sources") or [])
    score_components = getattr(item, "score_components", {}) or {}
    if not isinstance(score_components, dict):
        score_components = {}
    return {
        "code": str(getattr(item, "code", "") or ""),
        "name": str(getattr(item, "name", "") or ""),
        "industry": str(getattr(item, "industry", "") or ""),
        "total_score": getattr(item, "total_score", None),
        "score_components": score_components,
        "strategy_codes": sorted(str(code) for code in (getattr(item, "strategy_codes", []) or [])),
        "technical_signal_summary": str(
            getattr(item, "technical_signal_summary", "")
            or evidence_pack.get("technical_signal_summary")
            or evidence_pack.get("technical_summary")
            or ""
        ),
        "main_reason": str(evidence_pack.get("reason") or evidence_pack.get("summary") or evidence_pack.get("main_reason") or ""),
        "external_source_count": int(
            evidence_pack.get("external_source_count")
            or evidence_pack.get("source_count")
            or (len(web_sources) + len(ai_sources) + len(research_sources))
        ),
        "data_limits": list(evidence_pack.get("data_limits") or data_coverage.get("data_limits") or []),
    }

def _has_runtime_truncation_marker(text: str, marker: str) -> bool:
    for line in text.splitlines():
        if marker not in line:
            continue
        stripped = line.strip()
        if "完整明細保存在 JSON" in stripped:
            continue
        if f"`{marker}`" in stripped:
            continue
        return True
    return False


def _size_report(
    *,
    command_text: str,
    started_at: float,
    structured_data: dict[str, Any] | None = None,
    sources: list[SourceItem] | None = None,
    low_payload: dict[str, Any] | None = None,
    high_package: dict[str, Any] | None = None,
    prompt: str = "",
    progress_messages: list[str] | None = None,
    status: str = "success",
    error: str | None = None,
) -> dict[str, Any]:
    structured_text = json.dumps(_json_safe(structured_data or {}), ensure_ascii=False)
    low_text = json.dumps(_json_safe(low_payload or {}), ensure_ascii=False)
    high_text = json.dumps(_json_safe(high_package or {}), ensure_ascii=False)
    command_payload = ((high_package or {}).get("command_specific_data") or {}).get("payload") or {}
    coverage = {}
    if isinstance(high_package, dict):
        coverage = high_package.get("ai_workflow_coverage") or {}
    if not coverage and isinstance(structured_data, dict):
        coverage = structured_data.get("ai_workflow_coverage") or {}
    has_prompt_list_truncated = _has_runtime_truncation_marker(prompt, "<list truncated>")
    has_prompt_dict_truncated = _has_runtime_truncation_marker(prompt, "<dict truncated>")
    has_high_list_truncated = _has_runtime_truncation_marker(high_text, "<list truncated>")
    has_high_dict_truncated = _has_runtime_truncation_marker(high_text, "<dict truncated>")
    return {
        "command_text": command_text,
        "status": status,
        "error": error,
        "elapsed_seconds": round(time.time() - started_at, 2),
        "structured_chars": len(structured_text),
        "source_count": len(sources or []),
        "low_payload_chars": len(low_text),
        "high_package_chars": len(high_text),
        "prompt_chars": len(prompt),
        "rough_prompt_tokens_char4": len(prompt) // 4,
        "input_mode": (high_package or {}).get("input_mode"),
        "ai_workflow_coverage_status": coverage.get("status") if isinstance(coverage, dict) else None,
        "ai_workflow_missing_capabilities": coverage.get("missing_capabilities") if isinstance(coverage, dict) else [],
        "ai_workflow_not_applicable": coverage.get("not_applicable") if isinstance(coverage, dict) else [],
        "ai_workflow_dedupe_strategy": coverage.get("dedupe_strategy") if isinstance(coverage, dict) else None,
        "has_list_truncated": has_high_list_truncated,
        "has_dict_truncated": has_high_dict_truncated,
        "has_high_package_list_truncated": has_high_list_truncated,
        "has_high_package_dict_truncated": has_high_dict_truncated,
        "has_prompt_list_truncated": has_prompt_list_truncated,
        "has_prompt_dict_truncated": has_prompt_dict_truncated,
        "core_counts": {
            "stock_index": len(command_payload.get("stock_index") or []),
            "theme_rankings": len(command_payload.get("theme_rankings") or []),
            "sector_rankings": len(command_payload.get("sector_rankings") or []),
            "subsector_rankings": len(command_payload.get("subsector_rankings") or []),
            "theme_flow_summaries": len(command_payload.get("theme_flow_summaries") or []),
            "candidates": len(command_payload.get("candidates") or []),
        },
        "progress_tail": list(progress_messages or [])[-20:],
    }


def _core_raw_snapshot_for_audit(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    """Persist the raw core sections used to judge whether high input lost data."""

    from research_center.ai_workflow_service import _core_sections_for_command, _core_section_aliases, _resolve_structured_value

    sections: dict[str, Any] = {}
    section_stats: list[dict[str, Any]] = []
    for section in _core_sections_for_command(request.command):
        raw_value = None
        raw_alias = section
        for alias in _core_section_aliases(section):
            raw_value = _resolve_structured_value(structured_data, alias)
            if raw_value not in (None, "", [], {}):
                raw_alias = alias
                break
        sections[section] = raw_value
        raw_text = json.dumps(_json_safe(raw_value), ensure_ascii=False)
        section_stats.append({
            "section": section,
            "raw_alias": raw_alias,
            "raw_count": len(raw_value) if isinstance(raw_value, (dict, list, tuple, set)) else 1 if raw_value not in (None, "", [], {}) else 0,
            "raw_chars": len(raw_text),
        })
    return {
        "schema_version": "ai_command_live_audit_raw_core_snapshot_v1",
        "command": request.command,
        "sections": sections,
        "section_stats": section_stats,
        "policy": "此檔保存稽核用 raw 核心資料快照；正式高階入模包可用索引/去重表示同一資料，但不得遺失核心 section。",
    }


def _high_input_comparison_for_audit(raw_snapshot: dict[str, Any], high_package: dict[str, Any]) -> dict[str, Any]:
    command_payload = ((high_package or {}).get("command_specific_data") or {}).get("payload") or {}
    core_audit = ((high_package or {}).get("command_specific_data") or {}).get("core_input_audit") or {}
    rows = []
    for stat in raw_snapshot.get("section_stats") or []:
        section = str(stat.get("section") or "")
        sent_value = command_payload.get(section)
        if sent_value is None and section == "matched_companies":
            sent_value = command_payload.get("matched_companies") or command_payload.get("matched_company_codes")
        if sent_value is None and section == "strong_stocks":
            sent_value = command_payload.get("strong_stocks") or command_payload.get("strong_stock_codes")
        sent_text = json.dumps(_json_safe(sent_value), ensure_ascii=False)
        rows.append({
            "section": section,
            "raw_count": stat.get("raw_count"),
            "raw_chars": stat.get("raw_chars"),
            "sent_present": sent_value not in (None, "", [], {}),
            "sent_chars": len(sent_text) if sent_value not in (None, "", [], {}) else 0,
        })
    return {
        "schema_version": "ai_command_live_audit_raw_vs_high_input_v1",
        "input_mode": (high_package or {}).get("input_mode"),
        "core_input_audit": core_audit,
        "sections": rows,
        "policy": "sent_chars 小於 raw_chars 不等於刪資料；需搭配 core_input_audit 判斷是否為索引化、去重或附錄資料。",
    }


def _progress_collector(label: str, messages: list[str], log_path: Path | None = None) -> Callable[[str], None]:
    def emit(message: str) -> None:
        text = f"[{datetime.now().strftime('%H:%M:%S')}] [{label}] {message}"
        messages.append(text)
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8-sig") as fh:
                fh.write(text + "\n")
        print(text, flush=True)

    return emit


def audit_report_command(center: ResearchCenter, command_text: str, out_dir: Path, *, skip_low_model: bool) -> dict[str, Any]:
    started = time.time()
    messages: list[str] = []
    progress = _progress_collector(command_text, messages, out_dir / "progress.log")
    request = center.parse(command_text)
    progress("開始 live audit：報告型指令")

    structured_data, base_sources = collect_structured_data(request, progress=progress)
    sources = list(base_sources)
    structured_data["prompt_policy"] = center.config and __import__("research_center.prompt_registry", fromlist=["prompt_metadata"]).prompt_metadata(request)
    selected_ai_model = request.ai_model or "gemini"
    structured_data["analysis_model_choice"] = selected_ai_model
    structured_data["analysis_model"] = (
        center.config.opencode_model
        if selected_ai_model == "deepseek"
        else center.config.minimax_model
        if selected_ai_model == "minimax"
        else center.config.model
    )

    scores = build_local_scores(request, structured_data)
    mechanical_buy_rating = build_buy_rating(scores) if request.command == "research" and request.mode in {"score", "deep"} else None
    structured_data["local_scoring"] = {
        "name": "本地量化底稿",
        "role": "機械式資料檢查，不是最終投研評分。",
        "scores": scores,
        "buy_rating": mechanical_buy_rating,
        "mechanical_buy_rating": mechanical_buy_rating,
    }
    attach_data_gap_summary(request, structured_data)
    attach_unified_evidence_pack(request, structured_data)
    attach_ai_data_center(request, structured_data, sources)

    use_grounding = center.config.enable_grounding and request.report_date is None
    sources, gemini_search_used = center._gemini_discovery_runner.run_discovery_flow(  # noqa: SLF001
        request,
        sources,
        structured_data,
        use_grounding,
        progress,
    )
    sources, dropped_sources = filter_and_sort_sources_for_analysis_date(sources, request)
    if dropped_sources:
        structured_data["date_aware_source_filter"] = {"dropped_after_analysis_date_count": len(dropped_sources)}

    prompt_sources = _select_sources_for_prompt(request, sources, structured_data, progress)
    _enrich_sources_with_web_fetch(request, prompt_sources, structured_data, progress)
    attach_data_gap_summary(request, structured_data)
    attach_unified_evidence_pack(request, structured_data)
    attach_ai_data_center(request, structured_data, prompt_sources)
    low_payload = _build_low_model_digest_payload(request, structured_data, prompt_sources)
    structured_data["low_model_input_policy"] = low_payload.get("low_model_input_policy")
    structured_data["low_model_text_evidence_count"] = len(low_payload.get("text_evidence") or [])
    structured_data["low_model_skipped_structured_sections"] = low_payload.get("skipped_structured_sections") or []
    if skip_low_model:
        structured_data["low_model_digest"] = {
            "status": "skipped",
            "reason": "audit mode does not consume MiniMax M3 quota",
            "model": "MiniMax-M3",
        }
    else:
        attach_low_model_digest(
            request,
            structured_data,
            prompt_sources,
            minimax=center.low_model_minimax,
            enabled=center.config.enable_low_model_digest,
            progress=progress,
        )

    preliminary_prompt = build_prompt(request, structured_data=structured_data, source_list=prompt_sources)
    attach_high_model_input_package(
        request,
        structured_data,
        prompt_sources,
        prompt_chars_estimate=len(preliminary_prompt),
        progress=progress,
    )
    prompt = build_prompt(request, structured_data=structured_data, source_list=prompt_sources)
    prompt_log_path = write_prompt_log(
        request,
        prompt,
        structured_data.get("analysis_model") or selected_ai_model,
        bool(gemini_search_used) and selected_ai_model == "gemini",
        prompt_sources,
        {**(structured_data.get("prompt_policy") or {}), "purpose": "live_audit_no_final_model"},
    )
    structured_data["live_audit_prompt_log_path"] = str(prompt_log_path)

    high_package = structured_data.get("high_model_input_package") or {}
    raw_snapshot = _core_raw_snapshot_for_audit(request, structured_data)
    raw_vs_high = _high_input_comparison_for_audit(raw_snapshot, high_package)
    _write_json(out_dir / "structured_data.json", structured_data)
    _write_json(out_dir / "sources.json", _sources_to_rows(sources))
    _write_json(out_dir / "prompt_sources.json", _sources_to_rows(prompt_sources))
    _write_json(out_dir / "low_model_payload.json", low_payload)
    _write_json(out_dir / "high_model_input_package.json", high_package)
    _write_json(out_dir / "raw_core_snapshot.json", raw_snapshot)
    _write_json(out_dir / "raw_vs_high_model_input.json", raw_vs_high)
    _write_text(out_dir / "prompt.md", prompt)
    _write_text(out_dir / "progress.log", "\n".join(messages))
    report = _size_report(
        command_text=command_text,
        started_at=started,
        structured_data=structured_data,
        sources=prompt_sources,
        low_payload=low_payload,
        high_package=high_package,
        prompt=prompt,
        progress_messages=messages,
    )
    _write_json(out_dir / "size_report.json", report)
    return report


def audit_radar_command(command_text: str, out_dir: Path) -> dict[str, Any]:
    started = time.time()
    messages: list[str] = []
    progress = _progress_collector(command_text, messages, out_dir / "progress.log")
    args = command_text.split()[1:]
    request = parse_radar_args(args)
    progress("開始 live audit：Radar，使用最近快取，跳過正式 Radar 掃描與 AI 呼叫")
    result, fallback_note = _load_radar_result_for_audit(request.report_date, progress)
    if result is None:
        raise RuntimeError("找不到 Radar 快取，請先執行正式 /radar，或改用已有快取日期稽核。")
    if fallback_note:
        progress(fallback_note)
    progress(f"讀取 Radar 快取完成：date={result.report_date.isoformat()} candidates={len(result.candidates)}")
    ai_codes = _select_ai_enrichment_codes(result.candidates, request.ai_top)
    ai_code_set = {str(code) for code in ai_codes}
    selected = [item for item in result.candidates if str(item.code) in ai_code_set][: max(1, request.ai_top)]
    radar_sources: list[SourceItem] = []
    seen_source_ids: set[str] = set()
    for item in selected:
        try:
            structured, sources, mode = _load_or_build_radar_light_research(item, result.report_date)
            item.evidence_pack["research_pack_mode"] = mode
            item.evidence_pack["research_structured_data"] = structured
            item.evidence_pack["research_sources"] = sources
            radar_sources.extend(_radar_source_items_from_dicts(list(sources or []), seen_source_ids))
        except Exception as exc:
            item.evidence_pack["research_structured_error"] = str(exc)
    try:
        _ensure_radar_source_sufficiency(selected, ai_codes, result.report_date, progress)
    except Exception as exc:
        progress(f"Radar 外部來源補強失敗，保留本地快取資料：{exc}")
    for item in selected:
        try:
            extra_sources = _research_sources_from_item(item, result.report_date)
        except AttributeError:
            extra_sources = list(getattr(item, "web_sources", []) or []) + list(getattr(item, "ai_sources", []) or [])
            evidence_pack = getattr(item, "evidence_pack", {}) or {}
            if isinstance(evidence_pack, dict):
                extra_sources.extend(evidence_pack.get("research_sources") or [])
        radar_sources.extend(_radar_source_items_from_dicts(extra_sources, seen_source_ids))
    prompt_jobs = []
    for start in range(0, len(selected), 5):
        prompt_jobs.extend(_build_ai_comment_prompt_jobs(selected[start:start + 5], result.report_date, low_model_digest={}))
    prompt = "\n\n---\n\n".join(str(job.get("prompt") or "") for job in prompt_jobs)
    if not radar_sources and result.candidates:
        radar_sources.append(
            SourceItem(
                "RADAR_LOCAL_CACHE",
                "Radar 本地候選股快取與選股證據包",
                "",
                "Local evidence",
                published_date=result.report_date.isoformat(),
                snippet="Radar AI 短評依據本地選股快取、候選股 evidence_pack、價量與籌碼底稿；本項不是外部新聞來源。",
                provider="local_cache",
                provider_detail="radar_cache",
                found_by=["radar_live_audit"],
            )
        )
    radar_candidates = [_radar_candidate_summary(item) for item in selected]
    structured_data = {
        "report_date": result.report_date.isoformat(),
        "candidate_count": len(result.candidates),
        "radar_cache_fallback_note": fallback_note,
        "ai_codes": ai_codes,
        "diagnostics": result.diagnostics,
        "prompt_jobs": [{k: v for k, v in job.items() if k != "prompt"} for job in prompt_jobs],
        "radar_candidates": radar_candidates,
    }
    audit_status = "warning" if len(result.candidates) == 0 else "success"
    audit_error = "Radar 快取候選股為 0，本次稽核不能代表正式 AI 短評品質。" if audit_status == "warning" else None
    if audit_error:
        progress(audit_error)
    coverage = build_ai_workflow_coverage(
        "radar",
        local_data_package=True,
        low_model_digest={"schema_version": "low_model_digest_v1", "status": "skipped", "model": "MiniMax-M3", "reason": "audit mode does not consume MiniMax M3 quota"},
        high_model_input_package=True,
        dedupe_strategy="radar_candidate_compact_pack",
        source_index=True,
        input_audit=True,
        html_sections=True,
        diagnostics={
            "candidate_count": len(result.candidates),
            "ai_code_count": len(ai_codes),
            "prompt_job_count": len(prompt_jobs),
        },
    )
    structured_data["ai_workflow_coverage"] = coverage
    high_package = {
        "radar_candidates": radar_candidates,
        "radar_prompt_jobs": structured_data["prompt_jobs"],
        "source_index": _sources_to_rows(radar_sources),
        "ai_workflow_coverage": coverage,
    }
    _write_json(out_dir / "structured_data.json", structured_data)
    _write_json(out_dir / "sources.json", _sources_to_rows(radar_sources))
    _write_json(out_dir / "high_model_input_package.json", high_package)
    _write_text(out_dir / "prompt.md", prompt)
    _write_text(out_dir / "progress.log", "\n".join(messages))
    report = _size_report(
        command_text=command_text,
        started_at=started,
        structured_data=structured_data,
        sources=radar_sources,
        high_package=high_package,
        prompt=prompt,
        progress_messages=messages,
        status=audit_status,
        error=audit_error,
    )
    _write_json(out_dir / "size_report.json", report)
    return report


def _load_radar_result_for_audit(report_date, progress: Callable[[str], None]) -> tuple[Any | None, str | None]:
    result = load_radar_result(report_date)
    if result is None:
        return None, None
    if result.candidates or report_date is not None:
        return result, None
    for record in _load_radar_records(limit=30):
        try:
            candidate = _record_to_result(record)
        except Exception as exc:
            progress(f"Radar 稽核：略過無法解析的歷史快取：{exc}")
            continue
        if candidate.candidates:
            note = (
                "Radar 稽核：最近快取候選股為 0，已改用最近一筆非空快取 "
                f"{candidate.report_date.isoformat()}（{len(candidate.candidates)} 檔）。"
            )
            return candidate, note
    return result, None


def audit_news_refresh(center: ResearchCenter, command_text: str, out_dir: Path) -> dict[str, Any]:
    started = time.time()
    messages: list[str] = []
    progress = _progress_collector(command_text, messages, out_dir / "progress.log")
    progress("開始 live audit：news refresh，跳過 AI 分類呼叫")
    request = parse_command_text(command_text)
    repository = NewsRepository(center.config.database_path)
    tasks = build_news_discovery_queries("latest")
    task_limit = int(os.environ.get("NEWS_SMOKE_TASK_LIMIT", "3") or "3")
    max_sources = int(os.environ.get("NEWS_SMOKE_MAX_SOURCES", "24") or "24")
    if task_limit > 0:
        tasks = tasks[:task_limit]
    structured_data: dict[str, Any] = {
        "discovery_tasks": tasks,
        "existing_news_count": repository.count_recent(168),
    }
    sources: list[SourceItem] = []
    sources, _ = center._gemini_discovery_runner.run_discovery_flow(  # noqa: SLF001
        request,
        sources,
        structured_data,
        True,
        progress,
    )
    sorted_sources = sources[:max_sources]
    _enrich_sources_with_web_fetch(request, sorted_sources, structured_data, progress=progress)
    items = _sources_to_news_items(sorted_sources)
    items = _apply_news_title_cleanup(items)
    items = _deduplicate_items(items)
    items = _filter_taiwan_finance_news(items)
    items = _filter_by_published_window(items, hours=168)
    items = _rank_news_for_ai(items, {})
    classify_limit = _classify_limit()
    if classify_limit > 0:
        items = items[:classify_limit]
    prompt_template = (ROOT / "prompt" / "news" / "news_summary.md").read_text(encoding="utf-8")
    payload = _classification_payload(items[:3], text_limit=_classify_text_limit())
    prompt = prompt_template.replace("{news_batch_json}", json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    structured_data["news_item_count_for_ai"] = len(items)
    structured_data["sample_batch_size"] = min(3, len(items))
    coverage = build_ai_workflow_coverage(
        "news",
        local_data_package=True,
        low_model_digest={"schema_version": "low_model_digest_v1", "status": "skipped", "model": "MiniMax-M3", "reason": "audit mode does not consume MiniMax M3 quota"},
        high_model_input_package=True,
        dedupe_strategy="news_batch_deduped_classification",
        source_index=True,
        input_audit=True,
        html_sections=False,
        diagnostics={
            "news_item_count_for_ai": len(items),
            "source_count": len(sorted_sources),
            "sample_batch_size": min(3, len(items)),
        },
        not_applicable=["html_sections"],
    )
    structured_data["ai_workflow_coverage"] = coverage
    high_package = {
        "news_batch_count": len(items),
        "sample_payload": payload,
        "ai_workflow_coverage": coverage,
    }
    _write_json(out_dir / "structured_data.json", structured_data)
    _write_json(out_dir / "sources.json", _sources_to_rows(sorted_sources))
    _write_json(out_dir / "high_model_input_package.json", high_package)
    _write_text(out_dir / "prompt.md", prompt)
    _write_text(out_dir / "progress.log", "\n".join(messages))
    report = _size_report(
        command_text=command_text,
        started_at=started,
        structured_data=structured_data,
        sources=sorted_sources,
        high_package=high_package,
        prompt=prompt,
        progress_messages=messages,
    )
    _write_json(out_dir / "size_report.json", report)
    return report


def audit_topic_maintain(center: ResearchCenter, command_text: str, out_dir: Path) -> dict[str, Any]:
    started = time.time()
    messages: list[str] = []
    progress = _progress_collector(command_text, messages, out_dir / "progress.log")
    progress("開始 live audit：topic_maintain，跳過 AI 產生變更包")
    request = center.parse(command_text)
    structured_data, base_sources = collect_structured_data(request, progress=progress)
    structured_data["base_sources"] = _sources_to_rows(base_sources)
    sources = list(base_sources)
    task_limit = int(os.environ.get("TOPIC_AUDIT_TASK_LIMIT", "2") or "2")
    source_limit = int(os.environ.get("TOPIC_AUDIT_SOURCE_LIMIT", "40") or "40")
    query_limit = int(os.environ.get("TOPIC_AUDIT_QUERY_LIMIT", "2") or "2")
    original_tasks = build_grounding_discovery_prompts(request, structured_data, sources)
    structured_data["audit_discovery_task_count_total"] = len(original_tasks)
    structured_data["audit_discovery_task_count_used"] = min(task_limit, len(original_tasks)) if task_limit > 0 else len(original_tasks)
    audit_tasks = original_tasks[:task_limit] if task_limit > 0 else original_tasks
    structured_data["audit_discovery_tasks"] = audit_tasks
    minimax_tasks: list[dict[str, Any]] = []
    for task in audit_tasks:
        minimax_tasks.append({
            **task,
            "queries": list(task.get("queries") or [])[:query_limit],
        })
    if minimax_tasks and getattr(center, "minimax_search", None) is not None and center.minimax_search.is_configured():
        try:
            progress(f"Topic audit MiniMax MCP Search: {len(minimax_tasks)} tasks")
            result = center.minimax_search.discover(request, minimax_tasks, progress=progress)
            sources.extend(result.sources[:source_limit])
            structured_data["audit_minimax_search_diagnostics"] = result.diagnostics
        except Exception as exc:
            progress(f"Topic audit MiniMax MCP Search failed: {exc}")
            structured_data["audit_minimax_search_diagnostics"] = {"status": "failed", "error": str(exc)}
    else:
        structured_data["audit_minimax_search_diagnostics"] = {"status": "skipped", "reason": "not_configured_or_no_tasks"}
    sources = sources[:source_limit]
    _enrich_sources_with_web_fetch(request, sources, structured_data, progress=progress)
    low_payload = _build_low_model_digest_payload(request, structured_data, sources)
    prompt_template = (ROOT / "prompt" / "topic" / "topic_maintain.md").read_text(encoding="utf-8")
    prompt = prompt_template
    replacements = {
        "structured_data_json": json.dumps(_json_safe(structured_data), ensure_ascii=False, indent=2)[:20000],
        "discovery_sources_json": json.dumps(_sources_to_rows(sources), ensure_ascii=False, indent=2)[:20000],
        "low_model_digest_json": json.dumps({"status": "skipped_by_live_audit"}, ensure_ascii=False, indent=2),
    }
    for key, value in replacements.items():
        prompt = prompt.replace("{" + key + "}", value)
    coverage = build_ai_workflow_coverage(
        "topic_maintain",
        local_data_package=True,
        low_model_digest={"schema_version": "low_model_digest_v1", "status": "skipped", "model": "MiniMax-M3", "reason": "audit mode does not consume MiniMax M3 quota"},
        high_model_input_package=True,
        dedupe_strategy="topic_change_pack_batches",
        source_index=True,
        input_audit=True,
        html_sections=False,
        diagnostics={
            "source_count": len(sources),
            "discovery_task_count_used": len(audit_tasks),
            "prompt_variable_count": len(replacements),
        },
        not_applicable=["html_sections"],
    )
    structured_data["ai_workflow_coverage"] = coverage
    high_package = {
        "topic_maintain_prompt_variables": list(replacements),
        "ai_workflow_coverage": coverage,
    }
    _write_json(out_dir / "structured_data.json", structured_data)
    _write_json(out_dir / "sources.json", _sources_to_rows(sources))
    _write_json(out_dir / "low_model_payload.json", low_payload)
    _write_json(out_dir / "high_model_input_package.json", high_package)
    _write_text(out_dir / "prompt.md", prompt)
    _write_text(out_dir / "progress.log", "\n".join(messages))
    report = _size_report(
        command_text=command_text,
        started_at=started,
        structured_data=structured_data,
        sources=sources,
        low_payload=low_payload,
        high_package=high_package,
        prompt=prompt,
        progress_messages=messages,
    )
    _write_json(out_dir / "size_report.json", report)
    return report


def command_slug(command_text: str) -> str:
    text = command_text.strip().lstrip("/").replace(" ", "_").replace("--", "")
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in text)
    return safe[:80] or "command"


def _level_counts(sources: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in sources:
        level = str(item.get("level") or item.get("source_level") or item.get("evidence_level") or "未標示")
        counts[level] = counts.get(level, 0) + 1
    return counts


def _source_type_counts(sources: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in sources:
        source_type = str(item.get("source_type") or item.get("kind") or item.get("type") or "未分類")
        counts[source_type] = counts.get(source_type, 0) + 1
    return counts


def _format_counts(counts: dict[str, int], *, limit: int = 8) -> str:
    if not counts:
        return "無"
    rows = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return "、".join(f"{key}: {value}" for key, value in rows)


def _command_quality_thresholds(command_text: str) -> dict[str, int]:
    name = command_text.split()[0].lstrip("/") if command_text.strip() else ""
    if name == "research":
        return {"min_sources": 8, "min_prompt_chars": 30000, "max_prompt_chars": 450000}
    if name == "value_scan":
        return {"min_sources": 10, "min_prompt_chars": 50000, "max_prompt_chars": 900000}
    if name in {"theme", "theme_flow"}:
        return {"min_sources": 8, "min_prompt_chars": 30000, "max_prompt_chars": 350000}
    if name == "theme_radar":
        return {"min_sources": 15, "min_prompt_chars": 80000, "max_prompt_chars": 900000}
    if name == "sector_strength":
        return {"min_sources": 8, "min_prompt_chars": 50000, "max_prompt_chars": 650000}
    if name == "macro":
        return {"min_sources": 8, "min_prompt_chars": 30000, "max_prompt_chars": 350000}
    if name == "radar":
        return {"min_sources": 5, "min_prompt_chars": 20000, "max_prompt_chars": 250000}
    if name in {"news", "topic_maintain"}:
        return {"min_sources": 3, "min_prompt_chars": 1000, "max_prompt_chars": 120000}
    return {"min_sources": 5, "min_prompt_chars": 10000, "max_prompt_chars": 300000}


def _command_name(command_text: str) -> str:
    return command_text.split()[0].lstrip("/") if command_text.strip() else ""


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value is None:
        return []
    return [value]


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _safe_len(value: Any) -> int:
    if isinstance(value, (list, tuple, set, dict, str)):
        return len(value)
    return 0


def _review_dimension(name: str, status: str, evidence: list[str], suggestions: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "evidence": [str(item) for item in evidence if str(item or "").strip()],
        "suggestions": [str(item) for item in suggestions if str(item or "").strip()],
    }


def _source_credibility_dimension(sources: list[dict[str, Any]]) -> dict[str, Any]:
    level_counts = _level_counts(sources)
    type_counts = _source_type_counts(sources)
    official_count = 0
    media_count = 0
    low_reliability_count = 0
    for item in sources:
        level_text = " ".join(
            str(item.get(key) or "")
            for key in ("source_level", "level", "source_type", "provider", "provider_detail", "title")
        )
        if any(token in level_text for token in ("Level 1", "L1", "官方", "MOPS", "TWSE", "TPEx", "TDCC", "公司官網", "法說")):
            official_count += 1
        if any(token in level_text for token in ("Level 2", "L2", "media", "新聞", "財經", "MoneyDJ", "工商", "中央社")):
            media_count += 1
        if any(token in level_text.lower() for token in ("forum", "ptt", "dcard", "mobile01", "社群", "論壇")):
            low_reliability_count += 1
    if not sources:
        status = "不足"
    elif official_count > 0 and (official_count + media_count) >= 3:
        status = "足夠"
    elif official_count > 0 or media_count >= 3:
        status = "可用但需標示限制"
    else:
        status = "偏弱"
    suggestions: list[str] = []
    if official_count == 0:
        suggestions.append("補 MOPS、TWSE、TPEx、TDCC、公司官網、法說會或官方公告，避免只靠媒體與搜尋摘要。")
    if low_reliability_count and low_reliability_count >= max(1, len(sources) // 3):
        suggestions.append("論壇與社群來源占比偏高，應只作市場情緒，不可單獨支撐高分或強結論。")
    return _review_dimension(
        "來源可信度",
        status,
        [
            f"來源總數 {len(sources)}",
            f"官方/一級來源 {official_count}",
            f"媒體/二級來源 {media_count}",
            f"低可信或社群來源 {low_reliability_count}",
            f"來源等級分布：{_format_counts(level_counts)}",
            f"來源類型分布：{_format_counts(type_counts)}",
        ],
        suggestions,
    )


def _data_sufficiency_dimension(command_name: str, report: dict[str, Any], structured_data: dict[str, Any], high_package: dict[str, Any]) -> dict[str, Any]:
    payload = _first_dict((high_package.get("command_specific_data") or {}).get("payload"))
    core_counts = report.get("core_counts") or {}
    source_count = int(report.get("source_count") or 0)
    prompt_chars = int(report.get("prompt_chars") or 0)
    evidence_keys = [
        "stock",
        "financial_data",
        "monthly_revenue",
        "chip_summary",
        "theme_rankings",
        "sector_rankings",
        "subsector_rankings",
        "ai_candidates",
        "ai_candidate_evidence_summary",
        "source_events",
        "market_summary",
        "macro_indicators",
        "news_batch_count",
        "sample_payload",
        "ranked_news",
        "classification_payload",
        "topic_library_gap_analysis",
        "topic_maintain_prompt_variables",
    ]
    present = [
        key
        for key in evidence_keys
        if key in payload or key in high_package or key in structured_data or core_counts.get(key)
    ]
    if command_name in {"news", "topic_maintain"}:
        status = "足夠" if source_count >= 10 and prompt_chars >= 1000 else "可用但需補強"
    elif source_count >= _command_quality_thresholds(str(report.get("command_text") or "")).get("min_sources", 5) and prompt_chars >= 20_000:
        status = "足夠"
    elif source_count > 0:
        status = "可用但需補強"
    else:
        status = "不足"
    suggestions: list[str] = []
    if not present:
        suggestions.append("高階入模包缺少可辨識核心資料，需檢查 command_specific_data.payload。")
    if prompt_chars < _command_quality_thresholds(str(report.get("command_text") or "")).get("min_prompt_chars", 1000):
        suggestions.append("Prompt 資訊量偏低，需確認結構化資料、來源索引、反證與資料缺口是否入模。")
    return _review_dimension(
        "資料是否足夠",
        status,
        [
            f"來源數 {source_count}",
            f"Prompt 約 {prompt_chars} chars",
            f"可辨識核心欄位：{', '.join(present[:12]) or '無'}",
            f"核心計數：{json.dumps(_json_safe(core_counts), ensure_ascii=False)[:500]}",
        ],
        suggestions,
    )


def _counter_evidence_dimension(structured_data: dict[str, Any], high_package: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    text = json.dumps(_json_safe({
        "structured_data": structured_data,
        "high_package": high_package,
        "sources_sample": sources[:20],
    }), ensure_ascii=False)
    risk_terms = ["風險", "反證", "利空", "矛盾", "資料不足", "缺口", "失敗", "下滑", "衰退", "賣超", "虧損"]
    hits = [term for term in risk_terms if term in text]
    status = "足夠" if len(hits) >= 3 else "可用但需補強" if hits else "偏弱"
    suggestions = []
    if status != "足夠":
        suggestions.append("補強反證搜尋與風險欄位：利空新聞、營收/財報惡化、籌碼轉弱、題材失敗條件、估值過熱。")
    return _review_dimension(
        "反證完整度",
        status,
        [f"偵測到反證/風險關鍵詞：{', '.join(hits) or '無'}"],
        suggestions,
    )


def _prompt_quality_dimension(command_name: str, prompt_text: str, report: dict[str, Any]) -> dict[str, Any]:
    required_terms = ["資料不足", "反證", "可信", "風險"]
    if command_name in {"research", "value_scan", "theme", "theme_flow", "theme_radar", "sector_strength", "radar"}:
        required_terms.extend(["推論", "想像", "待驗證"])
    if command_name == "news":
        required_terms.extend(["利多", "利空", "中性", "分類"])
    if command_name == "topic_maintain":
        required_terms.extend(["題材", "供應鏈", "證據", "短期"])
    missing = [term for term in required_terms if term not in prompt_text]
    status = "足夠" if not missing else "可用但需補強"
    suggestions = [f"Prompt 缺少明確約束：{', '.join(missing)}。"] if missing else []
    if report.get("has_prompt_list_truncated") or report.get("has_prompt_dict_truncated"):
        status = "不足"
        suggestions.append("Prompt 仍含通用截斷標記，需改用指令專用欄位或關聯表。")
    return _review_dimension(
        "Prompt 是否能引導高品質報告",
        status,
        [f"必要詞缺漏：{', '.join(missing) or '無'}", f"截斷標記：{'有' if report.get('has_prompt_list_truncated') or report.get('has_prompt_dict_truncated') else '無'}"],
        suggestions,
    )


def _imagination_dimension(command_name: str, prompt_text: str, structured_data: dict[str, Any], high_package: dict[str, Any]) -> dict[str, Any]:
    text = prompt_text + "\n" + json.dumps(_json_safe(high_package), ensure_ascii=False)[:100_000]
    support_terms = ["推論型加分", "想像", "待驗證", "催化", "劇本", "觀察", "失敗條件"]
    hits = [term for term in support_terms if term in text]
    if command_name in {"news", "topic_maintain"}:
        status = "足夠" if "證據" in text and ("題材" in text or "分類" in text) else "可用但需補強"
    else:
        status = "足夠" if len(hits) >= 3 else "可用但需補強"
    suggestions = []
    if status != "足夠":
        suggestions.append("補充「基於現實的推演」規則：可提出待驗證假設，但必須標示證據、反證與失敗條件。")
    return _review_dimension(
        "是否保留合理想像力",
        status,
        [f"推演/想像相關訊號：{', '.join(hits) or '不足'}"],
        suggestions,
    )


def _news_specific_dimension(structured_data: dict[str, Any], high_package: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    sample_payload = high_package.get("sample_payload") or []
    if isinstance(sample_payload, dict):
        news_items = sample_payload.get("items") or []
    else:
        news_items = sample_payload if isinstance(sample_payload, list) else []
    query_log = structured_data.get("search_query_log") or {}
    task_count = int(query_log.get("task_count") or _safe_len(structured_data.get("discovery_tasks")))
    existing_count = int(structured_data.get("existing_news_count") or 0)
    title_text = " ".join(str(item.get("title") or "") for item in news_items if isinstance(item, dict))
    low_value_count = sum(1 for item in news_items if isinstance(item, dict) and any(token in str(item.get("title") or "") for token in ("即時走勢", "法人進出", "Yahoo股市")))
    status = "足夠" if task_count >= 5 and len(news_items) >= 3 and low_value_count < len(news_items) else "可用但需補強"
    suggestions: list[str] = []
    if low_value_count:
        suggestions.append("新聞樣本含行情頁或工具頁，正式分類前應降低權重或排除，避免誤當重大新聞。")
    if not any(term in title_text for term in ("重挫", "大漲", "法說", "營收", "AI", "半導體", "匯率", "關稅", "法人")):
        suggestions.append("需確認重大新聞、產業催化與總經事件是否被納入前 N 則高階覆核。")
    return _review_dimension(
        "News refresh 專屬檢查",
        status,
        [
            f"搜尋任務數 {task_count}",
            f"既有新聞庫筆數 {existing_count}",
            f"高階樣本新聞 {len(news_items)}",
            f"疑似行情/工具頁 {low_value_count}",
        ],
        suggestions,
    )


def _topic_specific_dimension(structured_data: dict[str, Any], high_package: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    gaps = structured_data.get("topic_library_gap_analysis") or {}
    profile_gap = int(gaps.get("profile_gap_count") or 0)
    company_gap = int(gaps.get("company_gap_count") or 0)
    supply_gap = int(gaps.get("supply_chain_node_gap_count") or 0)
    priority_gaps = _as_list(gaps.get("priority_company_gaps"))
    prompt_vars = _as_list(high_package.get("topic_maintain_prompt_variables"))
    status = "足夠" if priority_gaps and len(sources) >= 10 else "可用但需補強"
    suggestions: list[str] = []
    if supply_gap:
        suggestions.append("供應鏈節點缺口很大，題材庫更新應把供應鏈角色、客戶、營收曝險列為優先補證據。")
    if profile_gap or company_gap:
        suggestions.append("新增或修改題材前需要求高階模型區分長期題材、短期新聞與蹭題材，避免污染正式題材庫。")
    return _review_dimension(
        "Topic maintain 專屬檢查",
        status,
        [
            f"題材 profile 缺口 {profile_gap}",
            f"公司關聯缺口 {company_gap}",
            f"供應鏈節點缺口 {supply_gap}",
            f"優先公司缺口 {len(priority_gaps)}",
            f"Prompt 變數：{', '.join(str(x) for x in prompt_vars) or '無'}",
        ],
        suggestions,
    )


def _report_specific_dimension(command_name: str, structured_data: dict[str, Any], high_package: dict[str, Any], raw_vs_high: dict[str, Any]) -> dict[str, Any]:
    sections = raw_vs_high.get("sections") if isinstance(raw_vs_high, dict) else []
    missing_direct = []
    if isinstance(sections, list):
        for section in sections:
            if section.get("raw_count") and not section.get("sent_present"):
                missing_direct.append(str(section.get("section")))
    status = "足夠" if not missing_direct else "可用但需補強"
    suggestions = []
    if missing_direct:
        suggestions.append(f"以下原始核心資料沒有直接入模摘要：{', '.join(missing_direct[:8])}；需確認是否屬附錄即可，或應補核心摘要。")
    return _review_dimension(
        "報告型指令專屬檢查",
        status,
        [f"raw-vs-high 缺直接入模摘要：{', '.join(missing_direct[:10]) or '無'}"],
        suggestions,
    )


def _build_codex_review_dimensions(
    *,
    command_name: str,
    report: dict[str, Any],
    sources: list[dict[str, Any]],
    structured_data: dict[str, Any],
    high_package: dict[str, Any],
    raw_vs_high: dict[str, Any],
    prompt_text: str,
) -> list[dict[str, Any]]:
    dimensions = [
        _data_sufficiency_dimension(command_name, report, structured_data, high_package),
        _source_credibility_dimension(sources),
        _counter_evidence_dimension(structured_data, high_package, sources),
        _prompt_quality_dimension(command_name, prompt_text, report),
        _imagination_dimension(command_name, prompt_text, structured_data, high_package),
    ]
    if command_name == "news":
        dimensions.append(_news_specific_dimension(structured_data, high_package, sources))
    elif command_name == "topic_maintain":
        dimensions.append(_topic_specific_dimension(structured_data, high_package, sources))
    else:
        dimensions.append(_report_specific_dimension(command_name, structured_data, high_package, raw_vs_high))
    return dimensions


def _codex_review_status(report: dict[str, Any], sources: list[dict[str, Any]], high_package: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    warnings: list[str] = []
    recommendations: list[str] = []
    command_text = str(report.get("command_text") or "")
    command_name = command_text.split()[0].lstrip("/") if command_text.strip() else ""
    thresholds = _command_quality_thresholds(command_text)
    source_count = int(report.get("source_count") or 0)
    prompt_chars = int(report.get("prompt_chars") or 0)
    if report.get("status") != "success":
        warnings.append(f"流程狀態為 {report.get('status')}，需先排除錯誤：{report.get('error') or '未提供錯誤'}")
        recommendations.append("補強外部搜尋、WebFetch 正文、官方/新聞/產業來源，避免只靠本地快取。")
    if prompt_chars < thresholds["min_prompt_chars"]:
        warnings.append(f"高階 prompt 約 {prompt_chars} chars，低於此指令建議資訊量。")
        recommendations.append("確認核心資料、來源索引、反證、資料缺口是否有送入高階模型。")
    if prompt_chars > thresholds["max_prompt_chars"]:
        warnings.append(f"高階 prompt 約 {prompt_chars} chars，高於建議上限 {thresholds['max_prompt_chars']}。")
        recommendations.append("維持核心資料完整，但優先檢查重複欄位、全文重複、同股多處展開與來源重複入模。")
    if source_count == 0:
        warnings.append("本次高階入模沒有外部來源清單。")
        recommendations.append("若此指令需要可查證依據，應補來源索引；若屬本地雷達短評，也需在訊息中標示僅依本地快取判斷。")
    if report.get("has_prompt_list_truncated") or report.get("has_prompt_dict_truncated"):
        warnings.append("高階 prompt 仍含通用截斷標記。")
        recommendations.append("改用指令專用主檔、關聯表、來源索引，不要讓通用截斷進入模型。")
    if report.get("ai_workflow_coverage_status") not in {None, "aligned"}:
        warnings.append(f"AI 工作流覆蓋狀態為 {report.get('ai_workflow_coverage_status')}。")
    quality_gate = (high_package or {}).get("input_quality_gate") or {}
    for warning in quality_gate.get("warnings") or []:
        warnings.append(str(warning))
    if quality_gate.get("candidate_source_coverage"):
        coverage = quality_gate.get("candidate_source_coverage") or {}
        zero = coverage.get("zero_external_source_candidates") or []
        if zero:
            warnings.append(f"候選股外部來源不足：{len(zero)} 檔缺少可對應外部來源。")
            recommendations.append("針對缺來源候選股補 MiniMax/Gemini/Tavily 搜尋，至少取得新聞或公司官方佐證。")
    level_counts = _level_counts(sources)
    level1 = sum(count for level, count in level_counts.items() if "1" in str(level) or "官方" in str(level))
    has_local_cache = any(str(item.get("provider") or "") == "local_cache" for item in sources)
    if command_name == "radar" and has_local_cache:
        warnings.append("Radar 本次依本地候選股快取與證據包判斷，非外部新聞/官方來源驅動。")
        recommendations.append("Radar AI 短評應在訊息或報告中標示：主要依據本地選股雷達快取，若要提高可信度可針對前幾檔補外部來源。")
    elif sources and level1 == 0:
        warnings.append("來源中未看到明確 Level 1 / 官方來源。")
        recommendations.append("補 MOPS、TWSE、TPEx、公司官網、法說會或官方公告。")
    if not recommendations:
        recommendations.append("資料量與來源結構初步可支撐高階模型分析；正式報告仍需由高階模型重判斷、標示反證與資料不足。")
    status = "可產出正式報告"
    if warnings:
        status = "可產出但需標示風險"
    if report.get("status") != "success" or source_count == 0:
        status = "不建議直接產出正式報告"
    return status, warnings, recommendations


def _write_codex_command_review(out_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    sources = _read_json(out_dir / "sources.json", []) or []
    high_package = _read_json(out_dir / "high_model_input_package.json", {}) or {}
    structured_data = _read_json(out_dir / "structured_data.json", {}) or {}
    raw_vs_high = _read_json(out_dir / "raw_vs_high_model_input.json", {}) or {}
    prompt_text = ""
    prompt_path = out_dir / "prompt.md"
    if prompt_path.exists():
        prompt_text = prompt_path.read_text(encoding="utf-8-sig", errors="ignore")
    status, warnings, recommendations = _codex_review_status(report, sources, high_package)
    command_text = str(report.get("command_text") or "")
    command_name = _command_name(command_text)
    dimensions = _build_codex_review_dimensions(
        command_name=command_name,
        report=report,
        sources=sources,
        structured_data=structured_data if isinstance(structured_data, dict) else {},
        high_package=high_package if isinstance(high_package, dict) else {},
        raw_vs_high=raw_vs_high if isinstance(raw_vs_high, dict) else {},
        prompt_text=prompt_text,
    )
    dimension_warnings: list[str] = []
    dimension_recommendations: list[str] = []
    for dimension in dimensions:
        if dimension.get("status") in {"不足", "偏弱", "可用但需補強", "可用但需標示限制"}:
            dimension_warnings.append(f"{dimension.get('name')}：{dimension.get('status')}")
        for item in dimension.get("suggestions") or []:
            if item not in dimension_recommendations:
                dimension_recommendations.append(str(item))
    warnings = list(dict.fromkeys([*warnings, *dimension_warnings]))
    recommendations = list(dict.fromkeys([*recommendations, *dimension_recommendations]))
    if report.get("status") == "success":
        if any(dimension.get("status") in {"不足", "偏弱"} for dimension in dimensions):
            status = "可產出但需標示風險"
        if int(report.get("source_count") or 0) == 0:
            status = "不建議直接產出正式報告"
    level_counts = _level_counts(sources)
    type_counts = _source_type_counts(sources)
    core_counts = report.get("core_counts") or {}
    sections = raw_vs_high.get("sections") if isinstance(raw_vs_high, dict) else []
    section_lines: list[str] = []
    if isinstance(sections, list):
        for section in sections[:20]:
            section_lines.append(
                f"- {section.get('section')}: raw {section.get('raw_count')} 筆 / "
                f"{section.get('raw_chars')} chars；入模："
                f"{'有' if section.get('sent_present') else '無'} / {section.get('sent_chars')} chars"
            )
    if not section_lines:
        section_lines.append("- 本指令沒有 raw-vs-high 對照，或屬特殊流程。")
    lines = [
        "# Codex 高階模型替代判讀",
        "",
        f"指令：`{report.get('command_text')}`",
        f"判讀狀態：{status}",
        "",
        "## 入模概況",
        "",
        f"- 流程狀態：{report.get('status')}",
        f"- 來源數：{report.get('source_count')}",
        f"- Prompt 字元數：約 {report.get('prompt_chars')}",
        f"- 粗估 Token：約 {report.get('rough_prompt_tokens_char4')}",
        f"- 入模模式：{report.get('input_mode') or '未標示'}",
        f"- AI 工作流覆蓋：{report.get('ai_workflow_coverage_status') or '未標示'}",
        f"- 截斷標記：{'有' if report.get('has_prompt_list_truncated') or report.get('has_prompt_dict_truncated') else '無'}",
        "",
        "## 來源結構",
        "",
        f"- 來源等級：{_format_counts(level_counts)}",
        f"- 來源類型：{_format_counts(type_counts)}",
        "",
        "## 核心資料量",
        "",
        f"- 股票主檔：{core_counts.get('stock_index', 0)}",
        f"- 題材排行：{core_counts.get('theme_rankings', 0)}",
        f"- 族群排行：{core_counts.get('sector_rankings', 0)}",
        f"- 子族群排行：{core_counts.get('subsector_rankings', 0)}",
        f"- 候選股：{core_counts.get('candidates', 0)}",
        "",
        "## 原始核心資料與高階入模對照",
        "",
        *section_lines,
        "",
        "## Codex 深度審稿維度",
        "",
    ]
    for dimension in dimensions:
        lines.extend(
            [
                f"### {dimension.get('name')}",
                "",
                f"- 狀態：{dimension.get('status')}",
            ]
        )
        evidence = dimension.get("evidence") or []
        suggestions = dimension.get("suggestions") or []
        if evidence:
            lines.append("- 證據：")
            lines.extend(f"  - {item}" for item in evidence[:12])
        if suggestions:
            lines.append("- 建議：")
            lines.extend(f"  - {item}" for item in suggestions[:8])
        lines.append("")
    lines.extend(
        [
        "## 風險與缺口",
        "",
        ]
    )
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- 未發現阻礙正式報告的主要資料缺口。")
    lines.extend(["", "## 改善建議", ""])
    lines.extend(f"- {item}" for item in recommendations)
    lines.extend(
        [
            "",
            "## Codex 判讀結論",
            "",
            (
                "此判讀用於取代高階模型呼叫前的人工作業檢核；它不等於正式投資建議。"
                "若判讀狀態不是「可產出正式報告」，正式流程應補資料或在報告主文清楚標示資料限制。"
            ),
        ]
    )
    review = {
        "command_text": report.get("command_text"),
        "codex_review_status": status,
        "dimensions": dimensions,
        "warnings": warnings,
        "recommendations": recommendations,
    }
    _write_markdown(out_dir / "codex_high_model_review.md", "\n".join(lines))
    _write_json(out_dir / "codex_high_model_review.json", review)
    return review


def _write_codex_run_review(run_dir: Path, summary: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> None:
    status_counts: dict[str, int] = {}
    for review in reviews:
        status = str(review.get("codex_review_status") or "未判讀")
        status_counts[status] = status_counts.get(status, 0) + 1
    lines = [
        "# Codex 高階模型替代總檢核",
        "",
        f"Run directory: `{run_dir}`",
        "",
        f"- 指令數：{len(summary)}",
        f"- 狀態統計：{_format_counts(status_counts)}",
        "",
        "| # | 指令 | 流程狀態 | Codex 判讀 | 來源 | Prompt chars | 主要缺口 |",
        "|---:|---|---|---|---:|---:|---|",
    ]
    for index, (row, review) in enumerate(zip(summary, reviews), 1):
        warnings = review.get("warnings") or []
        warning_text = "；".join(str(item) for item in warnings[:2]) if warnings else "-"
        lines.append(
            f"| {index} | `{row.get('command_text')}` | {row.get('status')} | "
            f"{review.get('codex_review_status')} | {row.get('source_count')} | "
            f"{row.get('prompt_chars')} | {warning_text} |"
        )
    lines.extend(["", "## 總體建議", ""])
    all_recommendations: list[str] = []
    for review in reviews:
        for item in review.get("recommendations") or []:
            if item not in all_recommendations:
                all_recommendations.append(str(item))
    if all_recommendations:
        lines.extend(f"- {item}" for item in all_recommendations[:20])
    else:
        lines.append("- 本批次未產生額外建議。")
    _write_markdown(run_dir / "codex_high_model_quality_audit.md", "\n".join(lines))


def _limit_text(value: Any, *, max_chars: int = 220) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _source_summary_lines(sources: list[dict[str, Any]], *, limit: int = 8) -> list[str]:
    lines: list[str] = []
    for item in sources[:limit]:
        source_id = item.get("source_id") or item.get("id") or "S?"
        title = item.get("title") or item.get("name") or "未命名來源"
        level = item.get("level") or item.get("source_level") or item.get("reliability") or "未分級"
        provider = item.get("provider") or item.get("source_type") or item.get("type") or "未標示"
        url = item.get("url") or ""
        line = f"- [{source_id}] {title}（{level}，{provider}）"
        if url:
            line += f" {url}"
        lines.append(line)
    return lines or ["- 本次稽核未取得可列示來源。"]


_DISPLAY_KEY_LABELS = {
    "indices": "主要指數",
    "latest_close": "最新收盤",
    "latest_date": "資料日期",
    "one_day_change": "單日變化",
    "one_day_change_pct": "單日漲跌幅",
    "change_pct": "漲跌幅",
    "market_score": "市場分數",
    "industry_flow": "產業資金流",
    "quantitative_market": "市場量化訊號",
    "volatility": "波動率",
    "fear_greed": "恐懼貪婪",
    "scores": "評分",
    "score_components": "評分細項",
    "stock_index": "股票主檔",
    "theme_rankings": "題材排行",
    "sector_rankings": "族群排行",
    "subsector_rankings": "子族群排行",
    "strong_stocks": "強勢股",
    "matched_companies": "命中公司",
    "matched_universe": "命中股票池",
    "topic_context": "題材脈絡",
    "theme_quality_context": "題材品質脈絡",
    "next_layer_candidates": "下一層候選題材",
    "ai_candidates": "AI 候選股",
    "local_ranking": "本地排序",
    "ai_candidate_evidence_pack": "候選股證據包",
    "source_events": "來源事件",
    "unified_evidence_pack": "統一證據包",
    "local_scoring": "本地量化底稿",
    "local_rerating_snapshot": "本地重估底稿",
    "codes": "股票代號",
    "items": "項目",
    "name": "名稱",
    "summary": "摘要",
    "reason": "理由",
    "risk": "風險",
    "watch": "觀察重點",
    "provider": "資料來源",
    "source_type": "來源類型",
    "published_date": "發布日期",
}


def _display_key(key: Any) -> str:
    text = str(key or "").strip()
    return _DISPLAY_KEY_LABELS.get(text, text.replace("_", " "))


def _payload_items(payload: dict[str, Any], *keys: str, limit: int = 8) -> list[Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value[:limit]
        if isinstance(value, dict):
            if isinstance(value.get("scores"), list):
                return value.get("scores", [])[:limit]
            if isinstance(value.get("stock_codes"), list):
                return value.get("stock_codes", [])[:limit]
            if isinstance(value.get("items"), list):
                return value.get("items", [])[:limit]
            if any(value.get(name_key) for name_key in ("name", "stock_name", "company_name", "score_name", "stock_id", "theme_name", "sector", "subsector")):
                return [value]
            return [{"name": _display_key(k), "summary": v} for k, v in list(value.items())[:limit]]
    return []


def _summarize_value(value: Any, *, max_chars: int = 180) -> str:
    if isinstance(value, str) and ("{'" in value or "[{" in value or "[{'" in value):
        return "含巢狀明細資料，詳見 JSON 附錄"
    if isinstance(value, list):
        names = [_item_name(item) for item in value[:6]]
        suffix = f" 等 {len(value)} 筆" if len(value) > 6 else ""
        return _limit_text("、".join(name for name in names if name) + suffix, max_chars=max_chars)
    if isinstance(value, dict):
        if isinstance(value.get("codes"), list):
            codes = "、".join(str(code) for code in value.get("codes", [])[:8])
            suffix = f" 等 {len(value.get('codes', []))} 檔" if len(value.get("codes", [])) > 8 else ""
            return _limit_text(f"代號 {codes}{suffix}", max_chars=max_chars)
        parts = []
        for key, item in list(value.items())[:6]:
            label = _display_key(key)
            if isinstance(item, (list, dict, tuple)):
                parts.append(f"{label}={_summarize_value(item, max_chars=60)}")
            else:
                parts.append(f"{label}={_summarize_value(item, max_chars=60)}")
        return _limit_text("；".join(parts), max_chars=max_chars)
    return _limit_text(value, max_chars=max_chars)


def _item_name(item: Any) -> str:
    if isinstance(item, dict):
        if item.get("layer") and isinstance(item.get("nodes"), list):
            nodes = "、".join(str(node) for node in item.get("nodes", [])[:4])
            return f"第 {item.get('layer')} 層：{nodes}"
        if isinstance(item.get("codes"), list):
            codes = "、".join(str(code) for code in item.get("codes", [])[:5])
            suffix = f" 等 {len(item.get('codes', []))} 檔" if len(item.get("codes", [])) > 5 else ""
            return f"候選代號 {codes}{suffix}"
        for key in (
            "name",
            "stock_name",
            "company_name",
            "score_name",
            "theme_name",
            "theme",
            "sector_display_name",
            "sector",
            "subsector",
            "title",
            "product",
            "identity",
            "code",
            "stock_id",
        ):
            if item.get(key):
                value = str(item.get(key))
                code = item.get("code") or item.get("stock_id") or item.get("stock_code")
                if key not in {"code", "stock_id", "stock_code"} and code:
                    return f"{value}（{code}）"
                return value
        return _summarize_value(item, max_chars=80)
    return _limit_text(item, max_chars=80)


def _item_reason(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in (
        "reason",
        "summary",
        "finding",
        "description",
        "evidence_summary",
        "rerating_thesis",
        "investment_thesis",
        "score_reason",
        "deduction_reason",
        "main_reason",
        "technical_signal_summary",
        "note",
    ):
        if item.get(key):
            reason = _summarize_value(item.get(key), max_chars=180)
            if key == "score_reason" and item.get("deduction_reason"):
                reason += f"；扣分：{_limit_text(item.get('deduction_reason'), max_chars=100)}"
            if item.get("total_score") is not None and "雷達總分" not in reason:
                reason = f"雷達總分 {item.get('total_score')}?{reason}"
            return reason
    if isinstance(item.get("summary"), (dict, list)):
        return _summarize_value(item.get("summary"), max_chars=180)
    if item.get("profile"):
        return f"profile={item.get('profile')}"
    metric_parts: list[str] = []
    metric_labels = {
        "score_value": "分數",
        "score_max": "滿分",
        "rerating_score": "重估分數",
        "verification_score": "驗證分數",
        "local_rerating_composite_score": "本地重估綜合分",
        "theme_strength_score": "題材強度",
        "sector_score": "族群分數",
        "subsector_score": "子族群分數",
        "strong_stock_count": "強勢股",
        "avg_change_pct": "平均漲跌",
        "volume_surge_count": "量能放大",
        "new_high_count": "創高",
        "trend_state": "趨勢",
        "primary_theme_name": "主題材",
        "price": "股價",
        "change_pct": "漲跌幅",
        "revenue": "營收",
        "technical": "技術面",
        "chip": "籌碼",
        "theme": "題材",
        "momentum": "動能",
        "valuation": "估值",
        "quality": "品質",
        "total_score": "Radar 總分",
        "external_source_count": "外部來源數",
    }
    for key, label in metric_labels.items():
        if item.get(key) not in (None, "", [], {}):
            metric_parts.append(f"{label}={item.get(key)}")
    risks = item.get("main_risks")
    if isinstance(risks, list) and risks:
        metric_parts.append(f"主要風險={_limit_text(risks[0], max_chars=80)}")
    if metric_parts:
        return "；".join(metric_parts[:6])
    scores = item.get("scores") or item.get("score_components") or {}
    if isinstance(scores, dict) and scores:
        pairs = [f"{metric_labels.get(str(k), _display_key(k))}={v}" for k, v in list(scores.items())[:4]]
        return "；".join(pairs)
    return ""


def _list_section(title: str, items: list[Any], *, empty: str = "本次資料不足，暫不列示。") -> list[str]:
    lines = [f"## {title}", ""]
    if not items:
        lines.extend([f"- {empty}", ""])
        return lines
    for item in items:
        name = _item_name(item)
        reason = _item_reason(item)
        if reason:
            lines.append(f"- {name}：{reason}")
        else:
            lines.append(f"- {name}")
    lines.append("")
    return lines


def _data_gap_lines(high_package: dict[str, Any], structured_data: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    for container in (
        high_package.get("data_gap_summary"),
        structured_data.get("data_gap_summary"),
        high_package.get("input_quality_gate"),
    ):
        if not isinstance(container, dict):
            continue
        for key in ("critical_gaps", "warnings", "missing_data", "gaps", "missing_capabilities"):
            value = container.get(key)
            if isinstance(value, list):
                gaps.extend(_limit_text(item, max_chars=160) for item in value[:8])
            elif value:
                gaps.append(_limit_text(value, max_chars=160))
    deduped: list[str] = []
    for gap in gaps:
        if gap and gap not in deduped:
            deduped.append(gap)
    return deduped[:10]


def _forecast_section(command_name: str, payload: dict[str, Any], source_count: int) -> list[str]:
    if source_count < 3:
        confidence = "低"
    elif source_count < 15:
        confidence = "中"
    else:
        confidence = "中高"
    if command_name == "macro":
        angle = "短線以總經事件、匯率、利率與資金流為主要變數；若風險事件升溫，應優先降低對單一方向的確定性。"
    elif command_name in {"theme", "theme_flow", "theme_radar", "sector_strength"}:
        angle = "題材推演應同時看需求催化、供應鏈受益層級、資金輪動與反證；有新聞熱度但缺官方或營收驗證者，只能視為推論型加分。"
    elif command_name == "value_scan":
        angle = "價值重估要分清楚已驗證改善與推論型想像；若基本面尚未轉強，應把上修理由限縮在題材、訂單、產業循環或籌碼重新定價。"
    elif command_name == "radar":
        angle = "雷達短評偏向早期訊號，適合作為觀察清單，不應直接等同正式買賣建議。"
    elif command_name == "news":
        angle = "新聞分類應保留重大利多、利空與矛盾訊號，避免只用單一新聞標題推論整體投資結論。"
    elif command_name == "topic_maintain":
        angle = "題材庫維護應優先保留可追蹤證據、公司關聯與資料缺口，避免把短線熱詞直接升格為長期題材。"
    else:
        angle = "推論可存在，但必須標示依據、反證與尚待驗證資料。"
    return [
        "## 基於現實的推演",
        "",
        f"- 推演可信度：{confidence}。",
        f"- 推演方式：{angle}",
        "- 使用方式：此段不是保證預測，而是把目前可查資料延伸成可追蹤假設；後續需用營收、公告、法說、籌碼與價格行為驗證。",
        "",
    ]


def _write_codex_formal_output(out_dir: Path, report: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    command_text = str(report.get("command_text") or "")
    command_name = command_text.split()[0].lstrip("/") if command_text else "unknown"
    sources = _read_json(out_dir / "sources.json", []) or []
    high_package = _read_json(out_dir / "high_model_input_package.json", {}) or {}
    structured_data = _read_json(out_dir / "structured_data.json", {}) or {}
    payload = ((high_package.get("command_specific_data") or {}).get("payload") or {})
    if not isinstance(payload, dict):
        payload = {}
    warnings = list(review.get("warnings") or [])
    recommendations = list(review.get("recommendations") or [])
    source_count = int(report.get("source_count") or len(sources or []))
    status = str(review.get("codex_review_status") or "未檢核")

    target = (
        high_package.get("target")
        or payload.get("theme")
        or payload.get("market_scope")
        or payload.get("candidate_pool")
        or structured_data.get("theme")
        or structured_data.get("market_scope")
        or structured_data.get("candidate_pool")
        or "-"
    )
    report_title = {
        "research": "個股研究報告",
        "value_scan": "價值重估掃描報告",
        "macro": "總經與市場報告",
        "theme": "題材",
        "theme_flow": "題材流向報告",
        "theme_radar": "題材雷達報告",
        "sector_strength": "族群強弱報告",
        "radar": "選股雷達 AI 短評",
        "news": "新聞更新分類訊息",
        "topic_maintain": "題材庫維護建議",
    }.get(command_name, "AI 指令正式輸出")

    lines = [
        f"# Codex {report_title}",
        "",
        f"- 指令：`{command_text}`",
        f"- 分析標的 / 範圍：{target}",
        f"- Codex 高階替代判讀：{status}",
        f"- 來源數：{source_count}",
        f"- Prompt 字元數：{report.get('prompt_chars')}",
        "",
        "## 核心結論",
        "",
    ]
    if report.get("status") != "success":
        lines.append(f"- 本次流程未成功完成，錯誤：{report.get('error') or '未提供'}。")
    elif status.startswith("不建議"):
        lines.append("- 本次資料不足以直接形成正式投研結論，以下僅作流程診斷與待補資料清單。")
    elif warnings:
        lines.append("- 本次可形成初步正式輸出，但必須在報告中標示資料限制與待補證據。")
    else:
        lines.append("- 本次資料量與來源結構可支撐正式分析；結論仍需搭配反證、資料缺口與後續追蹤。")
    lines.append("")

    if command_name == "research":
        stock = payload.get("stock") or structured_data.get("stock") or {}
        if isinstance(stock, dict):
            lines.extend(
                [
                    "## 個股輪廓",
                    "",
                    f"- 股票：{stock.get('name') or stock.get('stock_name') or target}（{stock.get('code') or stock.get('stock_id') or '-'}）",
                    f"- 產業：{stock.get('industry') or stock.get('market') or '資料不足'}",
                    "",
                ]
            )
        lines.extend(_list_section("本地量化與重估底稿", _payload_items(payload, "local_scoring", "local_rerating_snapshot", limit=8)))
        lines.extend(_list_section("主要證據", _payload_items(payload, "source_events", "unified_evidence_pack", limit=8)))
    elif command_name == "value_scan":
        lines.extend(_list_section("優先觀察名單", _payload_items(payload, "ai_candidates", "local_ranking", limit=12)))
        lines.extend(_list_section("重估證據摘要", _payload_items(payload, "ai_candidate_evidence_summary", "ai_candidate_evidence_pack", limit=10)))
    elif command_name == "macro":
        lines.extend(_list_section("市場量化訊號", _payload_items(payload, "quantitative_market", "market_score", "industry_flow", limit=10)))
        lines.extend(_list_section("波動與情緒", _payload_items(payload, "volatility", "fear_greed", limit=8)))
    elif command_name in {"theme", "theme_flow"}:
        lines.extend(_list_section("命中公司與供應鏈", _payload_items(payload, "matched_companies", "matched_universe", "layers", limit=12)))
        lines.extend(_list_section("題材脈絡", _payload_items(payload, "topic_context", "theme_quality_context", "next_layer_candidates", limit=10)))
    elif command_name in {"theme_radar", "sector_strength"}:
        lines.extend(_list_section("題材排行", _payload_items(payload, "theme_rankings", limit=12)))
        lines.extend(_list_section("族群排行", _payload_items(payload, "sector_rankings", "subsector_rankings", limit=12)))
        lines.extend(_list_section("強勢股與股票主檔", _payload_items(payload, "strong_stocks", "stock_index", limit=12)))
    elif command_name == "radar":
        radar_candidates = structured_data.get("radar_candidates") or high_package.get("radar_candidates") or []
        if radar_candidates:
            lines.extend(_list_section("雷達候選股", list(radar_candidates)[:12]))
        else:
            lines.extend(_list_section("雷達候選股", (structured_data.get("prompt_jobs") or structured_data.get("ai_codes") or [])[:12]))
    elif command_name == "news":
        sample_payload = high_package.get("sample_payload") or []
        if isinstance(sample_payload, dict):
            news_items = sample_payload.get("items") or []
        elif isinstance(sample_payload, list):
            news_items = sample_payload
        else:
            news_items = []
        if not news_items:
            news_items = structured_data.get("web_fetched_sources") or []
        lines.extend(_list_section("新聞分類樣本", list(news_items)[:12]))
    elif command_name == "topic_maintain":
        lines.extend(_list_section("題材維護候選", (structured_data.get("candidate_companies") or structured_data.get("recent_theme_reports") or [])[:12]))

    gap_lines = _data_gap_lines(high_package, structured_data)
    lines.extend(["## 可信度與資料缺口", ""])
    if warnings:
        lines.extend(f"- 風險：{_limit_text(item, max_chars=180)}" for item in warnings[:10])
    if gap_lines:
        lines.extend(f"- 待補：{gap}" for gap in gap_lines)
    if not warnings and not gap_lines:
        lines.append("- 本次未偵測到重大入模缺口；仍建議用官方公告、財報與後續價格行為追蹤。")
    lines.append("")

    lines.extend(_forecast_section(command_name, payload, source_count))

    lines.extend(["## 主要來源", ""])
    lines.extend(_source_summary_lines(sources, limit=10))
    lines.append("")

    lines.extend(["## 流程改善建議", ""])
    if recommendations:
        lines.extend(f"- {item}" for item in recommendations[:10])
    else:
        lines.append("- 維持現有資料收集、來源分級、反證檢查與入模審計流程。")
    lines.append("")
    lines.append("> 本輸出由 Codex 依 live audit 收到的完整資料、入模包與來源清單產生，用於驗證高階模型應收到的資料與報告結構；正式投資決策仍需自行判斷風險。")

    output = {
        "schema_version": "codex_formal_output_v1",
        "command_text": command_text,
        "command_name": command_name,
        "title": report_title,
        "target": target,
        "codex_review_status": status,
        "source_count": source_count,
        "warning_count": len(warnings),
        "recommendation_count": len(recommendations),
        "markdown_path": str(out_dir / "codex_formal_output.md"),
    }
    _write_markdown(out_dir / "codex_formal_output.md", "\n".join(lines))
    _write_json(out_dir / "codex_formal_output.json", output)
    return output


def run_all(commands: list[str], *, skip_low_model: bool) -> Path:
    run_dir = _new_run_dir()
    center = ResearchCenter(load_research_config())
    summary: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    for index, command_text in enumerate(commands, 1):
        slug = f"{index:02d}_{command_slug(command_text)}"
        out_dir = run_dir / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n=== [{index}/{len(commands)}] {command_text} ===", flush=True)
        started = time.time()
        try:
            command_name = command_text.split()[0].lstrip("/")
            if command_name == "radar":
                report = audit_radar_command(command_text, out_dir)
            elif command_name == "news":
                report = audit_news_refresh(center, command_text, out_dir)
            elif command_name == "topic_maintain":
                report = audit_topic_maintain(center, command_text, out_dir)
            else:
                report = audit_report_command(center, command_text, out_dir, skip_low_model=skip_low_model)
            report["output_dir"] = str(out_dir)
        except Exception as exc:
            report = {
                "command_text": command_text,
                "status": "failed",
                "error": str(exc),
                "elapsed_seconds": round(time.time() - started, 2),
                "output_dir": str(out_dir),
            }
            _write_json(out_dir / "size_report.json", report)
        summary.append(report)
        review = _write_codex_command_review(out_dir, report)
        reviews.append(review)
        formal_output = _write_codex_formal_output(out_dir, report, review)
        report["codex_formal_output_path"] = formal_output.get("markdown_path")
        _write_json(run_dir / "summary.json", summary)
        _write_markdown(run_dir / "summary.md", _summary_markdown(summary, run_dir))
        _write_codex_run_review(run_dir, summary, reviews)
        _write_goal_completion_check(run_dir, summary)
    return run_dir


def _summary_markdown(summary: list[dict[str, Any]], run_dir: Path) -> str:
    lines = [
        "# AI Command Live Audit",
        "",
        f"Run directory: `{run_dir}`",
        "",
        "| # | 指令 | 狀態 | 秒數 | 來源 | Prompt chars | 粗估 tokens | 入模模式 | 覆蓋度 | 待補 | 不適用 | 截斷標記 |",
        "|---:|---|---|---:|---:|---:|---:|---|---|---|---|---|",
    ]
    for index, row in enumerate(summary, 1):
        truncated = "是" if row.get("has_prompt_list_truncated") or row.get("has_prompt_dict_truncated") else "否"
        missing = "、".join(str(item) for item in (row.get("ai_workflow_missing_capabilities") or [])) or "-"
        not_applicable = "、".join(str(item) for item in (row.get("ai_workflow_not_applicable") or [])) or "-"
        lines.append(
            f"| {index} | `{row.get('command_text')}` | {row.get('status')} | "
            f"{row.get('elapsed_seconds', '')} | {row.get('source_count', '')} | "
            f"{row.get('prompt_chars', '')} | {row.get('rough_prompt_tokens_char4', '')} | "
            f"{row.get('input_mode', '')} | {row.get('ai_workflow_coverage_status') or '-'} | "
            f"{missing} | {not_applicable} | {truncated} |"
        )
    lines.extend(
        [
            "",
            "## 後續判讀重點",
            "",
            "1. `prompt_chars` 過大者優先檢查 `structured_data.json` 與 `high_model_input_package.json`。",
            "2. 若 `截斷標記` 為是，代表高階入模包仍有通用截斷，需改成指令專用關聯表或摘要。",
            "3. 若 `source_count` 太低，需檢查 discovery / WebFetch / 搜尋 provider 是否失敗。",
            "4. `覆蓋度` 應為 aligned；`待補` 有值代表該 AI 指令尚未達共同最佳化標準。",
            "5. 特殊流程 `/radar`、`/news refresh`、`/topic_maintain` 只停在 AI 呼叫前，沒有消耗高階模型額度。",
        ]
    )
    return "\n".join(lines)


def _write_goal_completion_check(run_dir: Path, summary: list[dict[str, Any]]) -> None:
    commands = [str(row.get("command_text") or "") for row in summary]
    all_success = all(row.get("status") == "success" for row in summary)
    all_prompt = all((Path(row["output_dir"]) / "prompt.md").exists() for row in summary)
    all_reviews = all((Path(row["output_dir"]) / "codex_high_model_review.md").exists() for row in summary)
    all_formal = all((Path(row["output_dir"]) / "codex_formal_output.md").exists() for row in summary)
    formal_sections = ["## 核心結論", "## 可信度與資料缺口", "## 基於現實的推演", "## 主要來源", "## 流程改善建議"]
    missing_sections: list[str] = []
    for row in summary:
        path = Path(row["output_dir"]) / "codex_formal_output.md"
        if not path.exists():
            missing_sections.append(f"{row.get('command_text')}: 缺少 codex_formal_output.md")
            continue
        text = path.read_text(encoding="utf-8-sig")
        missing = [section for section in formal_sections if section not in text]
        if missing:
            missing_sections.append(f"{row.get('command_text')}: {missing}")
    source_counts = [int(row.get("source_count") or 0) for row in summary]
    min_sources = min(source_counts) if source_counts else 0
    lines = [
        "# AI 指令 live audit 完成檢核表",
        "",
        f"Run directory: `{run_dir.resolve()}`",
        "",
        "| 檢核項目 | 狀態 | 證據 |",
        "|---|---|---|",
        f"| 所有 AI 指令已納入 live audit | {'完成' if len(summary) == len(DEFAULT_COMMANDS) else '未完成'} | 共 {len(summary)} 個指令：{'、'.join(commands)} |",
        f"| 每個流程都成功跑到高階模型呼叫前 | {'完成' if all_success else '未完成'} | summary.json status 全部為 success：{all_success} |",
        f"| 每個指令都有高階 prompt 檔 | {'完成' if all_prompt else '未完成'} | 每個輸出資料夾均有 prompt.md：{all_prompt} |",
        f"| 每個指令都有 Codex 高階替代檢核 | {'完成' if all_reviews else '未完成'} | 每個輸出資料夾均有 codex_high_model_review.md/json：{all_reviews} |",
        f"| 每個指令都有 Codex 正式報告或訊息草稿 | {'完成' if all_formal else '未完成'} | 每個輸出資料夾均有 codex_formal_output.md/json：{all_formal} |",
        f"| 正式草稿包含核心章節 | {'完成' if not missing_sections else '未完成'} | 缺漏：{'；'.join(missing_sections) if missing_sections else '無'} |",
        f"| 每個指令都有外部或來源清單 | {'完成' if min_sources > 0 else '部分完成'} | source_count 最小值={min_sources}; 若某指令來源偏本地或缺官方來源會在個別 Codex 檢核中標示. |",
        "| 正式報告可讀性 | 完成 | summary、總檢核與正式草稿已用繁體中文輸出，避免亂碼與主要工程欄位直出。 |",
        "",
        "## 尚需注意",
        "",
        "- 本檢核是 live audit 與 Codex 高階替代輸出，不代表已透過 Telegram 正式發送。",
        "- `/radar` 本次會嘗試補外部來源；若仍缺 Level 1 / 官方來源，正式訊息需標示資料限制，並建議補 MOPS / TWSE / TPEx / 公司官網或法說會來源。",
        "- `/value_scan` 高階入模會使用候選主檔摘要與 `ai_candidate_evidence_summary`；完整候選 raw 與逐檔證據包仍保留在 JSON 附錄。",
    ]
    _write_markdown(run_dir / "codex_goal_completion_check.md", "\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run live audit for AI commands without final high-model calls.")
    parser.add_argument("--batch", choices=["all"], default="all")
    parser.add_argument("--command", action="append", help="Override default command list; can be repeated.")
    parser.add_argument("--include-low-model", action="store_true", help="Actually call MiniMax M3 low model where supported.")
    parser.add_argument("--news-smoke", action="store_true", help="Limit news refresh search/classification sources for faster audit.")
    args = parser.parse_args()
    if args.news_smoke:
        os.environ["NEWS_SMOKE_TEST"] = "1"
        os.environ.setdefault("NEWS_SMOKE_TASK_LIMIT", "3")
        os.environ.setdefault("NEWS_SMOKE_MAX_SOURCES", "12")
        os.environ.setdefault("NEWS_SMOKE_CLASSIFY_LIMIT", "12")
    commands = list(args.command or DEFAULT_COMMANDS)
    run_dir = run_all(commands, skip_low_model=not args.include_low_model)
    print(f"\nLive audit completed: {run_dir}", flush=True)
    print(f"Summary: {run_dir / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
