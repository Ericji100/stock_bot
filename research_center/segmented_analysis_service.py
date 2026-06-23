from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from .models import CommandRequest, SourceItem
from .prompt_logging import write_prompt_log

ProgressCallback = Callable[[str], None]

SEGMENTED_ANALYSIS_COMMANDS = {"research", "value_scan", "macro", "theme", "theme_radar", "theme_flow", "sector_strength"}
SEGMENTED_ANALYSIS_PROMPT_THRESHOLD = 120_000
SEGMENTED_ANALYSIS_TARGET_CHARS = 110_000
SEGMENTED_ANALYSIS_HARD_CHARS = 160_000
SEGMENTED_ANALYSIS_FINAL_HARD_CHARS = 180_000
SEGMENTED_ANALYSIS_CALL_TIMEOUT_SECONDS = 900.0
SEGMENTED_ANALYSIS_MAX_HIGH_MODEL_PACKET_SEGMENTS = 8
SEGMENTED_ANALYSIS_MAX_THEME_FLOW_SEGMENTS = 10
SEGMENTED_ANALYSIS_MERGE_SMALL_CHARS = 110_000
SEGMENTED_ANALYSIS_PARALLEL_MIN_SEGMENTS = 4
SEGMENTED_ANALYSIS_MAX_PARALLEL_CALLS = 2


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
    if request.command not in SEGMENTED_ANALYSIS_COMMANDS:
        return False
    if request.command == "research" and request.mode != "deep":
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
    max_segments = _max_segment_plan_count(request)
    if len(plans) > max_segments:
        _emit(progress, f"\u5206\u6bb5 AI\uff1a\u539f\u59cb\u5206\u6bb5 {len(plans)} \u6bb5\u8d85\u904e\u4e0a\u9650 {max_segments} \u6bb5\uff0c\u6539\u7528\u6574\u5408\u5206\u6bb5\u3002")
        plans = _bounded_integration_plans(request, structured_data, plans)
    segment_runs: list[SegmentRun] = []
    prompt_paths: list[str] = []
    outputs: list[dict[str, Any]] = []
    _emit(progress, f"\u5206\u6bb5 AI\uff1a\u958b\u59cb\u57f7\u884c\uff0csegments={len(plans)} model={model_name} timeout={int(call_timeout_seconds)}s")
    _emit(progress, f"\u5206\u6bb5 AI\uff1a\u5c07\u4f9d\u5e8f\u5206\u6790 {len(plans)} \u6bb5\u8cc7\u6599\uff0cmodel={model_name}")

    def execute_segment(index: int, plan: dict[str, Any], prior_outputs: list[dict[str, Any]]) -> tuple[int, str, SegmentRun, dict[str, Any]]:
        prompt = _build_segment_prompt(request, structured_data, plan, prior_outputs)
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
        _emit(
            progress,
            f"\u5206\u6bb5 AI\uff1a\u9001\u51fa {index}/{len(plans)}\uff5c{plan['title']}\uff5cprompt={len(prompt)} chars est_tokens={max(1, len(prompt) // 4)} sources={len(segment_sources)} timeout={int(call_timeout_seconds)}s",
        )
        _emit(progress, f"\u5206\u6bb5 AI\uff1a{index}/{len(plans)}\uff5c{plan['title']}\uff5cprompt={len(prompt)} chars")
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
            _emit(progress, f"\u5206\u6bb5 AI\uff1a\u5b8c\u6210 {index}/{len(plans)}\uff5c{plan['title']}\uff5coutput={len(markdown)} chars elapsed={elapsed:.1f}s")
            _emit(progress, f"\u5206\u6bb5 AI\uff1a\u5b8c\u6210\uff5c{plan['title']}\uff5coutput={len(markdown)} chars")
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
            _emit(progress, f"\u5206\u6bb5 AI\uff1a\u5931\u6557 {index}/{len(plans)}\uff5c{plan['title']}\uff5c\u6539\u7528\u672c\u5730 fallback\uff5celapsed={elapsed:.1f}s error={exc}")
            _emit(progress, f"\u5206\u6bb5 AI\uff1a\u6b64\u6bb5\u6a21\u578b\u5206\u6790\u5931\u6557\uff0c\u5df2\u4fdd\u7559\u672c\u5730\u6458\u8981\u4f5c\u70ba fallback\uff5c{plan['title']}\uff5c\u932f\u8aa4\uff1a{exc}")
        return index, str(prompt_path), run, _segment_output(plan, run.markdown, run)

    parallel_workers = _segment_parallel_workers(request, plans)
    if parallel_workers > 1:
        _emit(progress, f"\u5206\u6bb5 AI\uff1a\u555f\u7528\u5e73\u884c\u5206\u6790 workers={parallel_workers} segments={len(plans)}")
        old_timeout = getattr(ai_client, "timeout_seconds", None) if hasattr(ai_client, "timeout_seconds") else None
        if hasattr(ai_client, "timeout_seconds"):
            setattr(ai_client, "timeout_seconds", call_timeout_seconds)
        result_rows: dict[int, tuple[str, SegmentRun, dict[str, Any]]] = {}
        try:
            remaining_plans = list(enumerate(plans, 1))
            if remaining_plans and remaining_plans[0][1].get("label") == "local_core_packet":
                index, plan = remaining_plans.pop(0)
                _, prompt_path, run, output = execute_segment(index, plan, [])
                result_rows[index] = (prompt_path, run, output)
            with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
                futures = {
                    executor.submit(execute_segment, index, plan, []): index
                    for index, plan in remaining_plans
                }
                for future in as_completed(futures):
                    index, prompt_path, run, output = future.result()
                    result_rows[index] = (prompt_path, run, output)
        finally:
            if hasattr(ai_client, "timeout_seconds") and old_timeout is not None:
                setattr(ai_client, "timeout_seconds", old_timeout)
        for index in sorted(result_rows):
            prompt_path, run, output = result_rows[index]
            prompt_paths.append(prompt_path)
            segment_runs.append(run)
            outputs.append(output)
    else:
        for index, plan in enumerate(plans, 1):
            _, prompt_path, run, output = execute_segment(index, plan, outputs)
            prompt_paths.append(prompt_path)
            segment_runs.append(run)
            outputs.append(output)

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
    _emit(progress, f"\u5206\u6bb5 AI\uff1a\u9001\u51fa\u6700\u7d42\u6574\u5408 prompt={len(final_prompt)} chars est_tokens={max(1, len(final_prompt) // 4)} sources={len(final_sources)} timeout={int(call_timeout_seconds)}s")
    _emit(progress, f"\u5206\u6bb5 AI\uff1a\u6700\u7d42\u6574\u5408 prompt={len(final_prompt)} chars")
    final_started = time.monotonic()
    final_retry_diagnostics: dict[str, Any] | None = None
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
        _emit(progress, f"\u5206\u6bb5 AI\uff1a\u6700\u7d42\u6574\u5408\u5b8c\u6210 output={len(markdown)} chars elapsed={final_elapsed:.1f}s")
        _emit(progress, f"\u5206\u6bb5 AI\uff1a\u6700\u7d42\u6574\u5408\u5b8c\u6210 output={len(markdown)} chars")
    except Exception as exc:
        retry_prompt = _build_compact_final_retry_prompt(request, structured_data, outputs, final_sources, exc)
        retry_prompt_path = write_prompt_log(
            request,
            retry_prompt,
            f"{model_name}_final_retry_compact",
            False,
            final_sources,
            {
                **(structured_data.get("prompt_policy") or {}),
                "purpose": "segmented_theme_final_retry_compact",
                "segment_count": len(segment_runs),
                "original_final_prompt_chars": len(final_prompt),
                "prompt_chars": len(retry_prompt),
                "estimated_tokens": max(1, len(retry_prompt) // 4),
                "source_count": len(final_sources),
                "call_timeout_seconds": call_timeout_seconds,
                "original_error": str(exc),
            },
        )
        prompt_paths.append(str(retry_prompt_path))
        _emit(
            progress,
            f"\u5206\u6bb5 AI\uff1a\u6700\u7d42\u6574\u5408\u5931\u6557\uff0c\u6539\u7528\u7cbe\u7c21 retry prompt\u3002original={len(final_prompt)} chars retry={len(retry_prompt)} chars prompt={retry_prompt_path}",
        )
        retry_started = time.monotonic()
        try:
            retry_result = _call_ai_with_timeout_setting(ai_client, retry_prompt, call_timeout_seconds)
            markdown = str(getattr(retry_result, "markdown", "") or "").strip()
            final_elapsed = time.monotonic() - final_started
            retry_elapsed = time.monotonic() - retry_started
            retry_diagnostics = dict(getattr(retry_result, "diagnostics", {}) or {})
            final_retry_diagnostics = {
                **retry_diagnostics,
                "status": "success",
                "retry": True,
                "prompt_path": str(retry_prompt_path),
                "prompt_chars": len(retry_prompt),
                "elapsed_seconds": round(retry_elapsed, 2),
                "timeout_seconds": call_timeout_seconds,
                "original_error": str(exc),
            }
            final_diagnostics = {
                "status": "success_after_retry",
                "retry": True,
                "original_error": str(exc),
                "elapsed_seconds": round(final_elapsed, 2),
                "timeout_seconds": call_timeout_seconds,
                "retry_diagnostics": final_retry_diagnostics,
            }
            for model_key in ("actual_model", "model"):
                if retry_diagnostics.get(model_key):
                    final_diagnostics[model_key] = retry_diagnostics.get(model_key)
            if final_prompt_too_large:
                final_diagnostics["final_prompt_too_large"] = True
                final_diagnostics["final_prompt_hard_chars"] = SEGMENTED_ANALYSIS_FINAL_HARD_CHARS
            raw = dict(getattr(retry_result, "raw", {}) or {})
            final_status = "success"
            final_error = None
            _emit(progress, f"\u5206\u6bb5 AI\uff1a\u6700\u7d42\u6574\u5408 retry \u6210\u529f output={len(markdown)} chars elapsed={retry_elapsed:.1f}s")
        except Exception as retry_exc:
            final_elapsed = time.monotonic() - final_started
            retry_elapsed = time.monotonic() - retry_started
            markdown = _compose_segmented_fallback_report(request, outputs, retry_exc)
            final_retry_diagnostics = {
                "status": "failed",
                "retry": True,
                "prompt_path": str(retry_prompt_path),
                "prompt_chars": len(retry_prompt),
                "elapsed_seconds": round(retry_elapsed, 2),
                "timeout_seconds": call_timeout_seconds,
                "original_error": str(exc),
                "retry_error": str(retry_exc),
            }
            final_diagnostics = {
                "status": "fallback",
                "error": str(retry_exc),
                "original_error": str(exc),
                "elapsed_seconds": round(final_elapsed, 2),
                "timeout_seconds": call_timeout_seconds,
                "retry_diagnostics": final_retry_diagnostics,
            }
            if final_prompt_too_large:
                final_diagnostics["final_prompt_too_large"] = True
                final_diagnostics["final_prompt_hard_chars"] = SEGMENTED_ANALYSIS_FINAL_HARD_CHARS
            raw = {}
            final_status = "fallback"
            final_error = str(retry_exc)
            _emit(progress, f"\u5206\u6bb5 AI\uff1a\u6700\u7d42\u6574\u5408 retry \u5931\u6557\uff0c\u6539\u7528\u5206\u6bb5 fallback \u5831\u544a\uff5celapsed={final_elapsed:.1f}s error={retry_exc}")
            _emit(progress, f"\u5206\u6bb5 AI\uff1a\u6700\u7d42\u6574\u5408\u5931\u6557\uff0c\u5831\u544a\u5df2\u6a19\u793a\u70ba fallback\uff0c\u932f\u8aa4\uff1a{retry_exc}")

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
        "parallel_workers": parallel_workers,
        "segment_count": len(segment_runs),
        "success_count": sum(1 for item in segment_runs if item.status == "success"),
        "fallback_count": sum(1 for item in segment_runs if item.status != "success"),
        "final_status": final_status,
        "final_error": final_error,
        "final_prompt_chars": len(final_prompt),
        "final_diagnostics": final_diagnostics,
        "final_retry_diagnostics": final_retry_diagnostics,
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
                        "title": f"{plan.get('title', 'large_payload')} {index}/{total}",
                        "payload": {
                            "segment_split": {
                                "source_label": plan.get("label"),
                                "part_index": index,
                                "part_total": total,
                                "policy": "split large JSON payload; full data remains in prompt slices and report artifacts.",
                            },
                            **chunk,
                        },
                    }
                )
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
                    "title": f"{plan.get('title', 'large_payload')} {index}/{total}",
                    "payload": {
                        "segment_split": {
                            "source_label": plan.get("label"),
                            "part_index": index,
                            "part_total": total,
                            "policy": "split large payload for model stability; core data is not deleted.",
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

    if any(isinstance(plan.get("payload"), dict) and plan["payload"].get("segment_split") for plan in plans):
        merged = _merge_small_segment_plans(request, structured_data, plans)
        return _limit_segment_plan_count(
            merged,
            max_segments=_max_segment_plan_count(request),
        )
    packet_plans = _high_model_packet_plans(structured_data)
    if packet_plans:
        return _limit_segment_plan_count(
            packet_plans,
            max_segments=_max_segment_plan_count(request),
        )
    return _limit_segment_plan_count(
        plans,
        max_segments=_max_segment_plan_count(request),
    )


def _max_segment_plan_count(request: CommandRequest) -> int:
    if request.command == "theme_flow":
        return SEGMENTED_ANALYSIS_MAX_THEME_FLOW_SEGMENTS
    return SEGMENTED_ANALYSIS_MAX_HIGH_MODEL_PACKET_SEGMENTS


def _segment_parallel_workers(request: CommandRequest, plans: list[dict[str, Any]]) -> int:
    if request.command not in {"theme_radar", "sector_strength"}:
        return 1
    if len(plans) < SEGMENTED_ANALYSIS_PARALLEL_MIN_SEGMENTS:
        return 1
    return min(SEGMENTED_ANALYSIS_MAX_PARALLEL_CALLS, len(plans))


def _merge_small_segment_plans(
    request: CommandRequest,
    structured_data: dict[str, Any],
    plans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge tiny adjacent segments to reduce API calls without dropping data."""

    merged: list[dict[str, Any]] = []
    buffer: list[dict[str, Any]] = []

    def flush_buffer() -> None:
        nonlocal buffer
        if not buffer:
            return
        if len(buffer) == 1:
            merged.append(buffer[0])
        else:
            merged.append(_combined_segment_plan(buffer, len(merged) + 1))
        buffer = []

    for plan in plans:
        candidate = [*buffer, plan]
        candidate_plan = _combined_segment_plan(candidate, len(merged) + 1) if len(candidate) > 1 else plan
        candidate_prompt = _build_segment_prompt(request, structured_data, candidate_plan, [])
        if len(candidate_prompt) <= SEGMENTED_ANALYSIS_MERGE_SMALL_CHARS:
            buffer = candidate
            continue
        flush_buffer()
        single_prompt = _build_segment_prompt(request, structured_data, plan, [])
        if len(single_prompt) <= SEGMENTED_ANALYSIS_MERGE_SMALL_CHARS:
            buffer = [plan]
        else:
            merged.append(plan)

    flush_buffer()
    return merged or plans


def _limit_segment_plan_count(plans: list[dict[str, Any]], *, max_segments: int) -> list[dict[str, Any]]:
    """Cap high-model calls by merging adjacent plans, never by dropping them."""

    if len(plans) <= max_segments or max_segments <= 0:
        return plans
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(max_segments)]
    total = len(plans)
    for index, plan in enumerate(plans):
        bucket_index = min((index * max_segments) // total, max_segments - 1)
        buckets[bucket_index].append(plan)
    limited: list[dict[str, Any]] = []
    for bucket in buckets:
        if not bucket:
            continue
        if len(bucket) == 1:
            limited.append(bucket[0])
        else:
            limited.append(_combined_segment_plan(bucket, len(limited) + 1))
    return limited


def _combined_segment_plan(plans: list[dict[str, Any]], index: int) -> dict[str, Any]:
    labels = [str(plan.get("label") or f"segment_{idx}") for idx, plan in enumerate(plans, 1)]
    titles = [str(plan.get("title") or label) for plan, label in zip(plans, labels)]
    return {
        "label": f"merged_segment_{index}",
        "title": "Merged segment: " + " / ".join(titles[:3]) + (" etc." if len(titles) > 3 else ""),
        "payload": {
            "merged_segment": {
                "part_count": len(plans),
                "labels": labels,
                "policy": "Merged to keep high-model call count bounded. Original payloads are kept under sections; core data is not deleted.",
            },
            "sections": [
                {"label": plan.get("label"), "title": plan.get("title"), "payload": plan.get("payload")}
                for plan in plans
            ],
        },
    }


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
            {"segment_type": "long_text_slice", "part_index": index + 1, "content": value[start:start + target_chars]}
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
            {"label": "sector_subsector", "title": "Sector and subsector ranking", "payload": _sector_payload(data)},
        ]
    if request.command == "theme_flow":
        return _theme_flow_plans(data)
    return [
        *_market_strength_plans(data),
        *_theme_evidence_plans(data),
        {"label": "extension_path", "title": "Theme diffusion and next path", "payload": _radar_flow_payload(data)},
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
            "title": "\u672c\u5730\u6838\u5fc3\u8cc7\u6599\u5305",
            "no_auto_split": True,
            "payload": {
                "input_policy": "Keep required core data. Use master tables and relation tables to reduce duplication without deleting themes, stocks, sectors, sources, risks, or counter-evidence.",
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
            "title": "\u8b49\u64da\u3001\u53cd\u8b49\u8207\u4f4e\u968e\u6a21\u578b\u6574\u7406",
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
            "title": "\u4f86\u6e90\u7d22\u5f15\u8207\u5fc5\u8981\u539f\u6587\u6458\u9304",
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
            "title": "\u672c\u5730\u8a55\u5206\u8207\u5165\u6a21\u7a3d\u6838",
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
    return _sanitize_prompt_text("\n".join([
        "# Segmented AI analysis task",
        "",
        f"Command: {request.command}",
        f"Data date: {data.get('report_date') or request.report_date or 'latest'}",
        f"Segment topic: {plan.get('title')}",
        "",
        "Task rules:",
        "- Organize verifiable observations from this segment only; do not make the final investment conclusion.",
        "- Preserve risks, counter-evidence, data gaps, source IDs, company codes, theme names, sector names, and important numbers.",
        "- If this segment conflicts with prior segment notes, mark the conflict instead of hiding it.",
        "- Write in Traditional Chinese for the analysis text and avoid exposing internal field names in final prose.",
        "",
        "Prior segment state:",
        _json(prior_state),
        "",
        "Segment payload:",
        _json(payload),
    ]))


def _build_final_prompt(
    request: CommandRequest,
    data: dict[str, Any],
    outputs: list[dict[str, Any]],
    sources: list[SourceItem],
) -> str:
    command_specific_rules = _final_synthesis_command_rules(request.command)
    return _sanitize_prompt_text("\n".join([
        "# Segmented AI final synthesis task",
        "",
        f"Command: {request.command}",
        f"Data date: {data.get('report_date') or request.report_date or 'latest'}",
        "",
        "Use the local core packet, segment notes, source index, risks and counter-evidence to produce the formal report.",
        "",
        "Synthesis rules:",
        "- Main report must be in Traditional Chinese and must not expose unreadable internal parameter names.",
        "- Segment outputs and low-model digest are drafts only; re-evaluate the evidence independently.",
        "- Include supporting evidence, counter-evidence, risks, data gaps, and follow-up validation conditions.",
        "- If evidence is insufficient, lower confidence explicitly instead of forcing a strong conclusion.",
        "- You may provide reality-based scenario thinking, but cite the evidence and failure conditions.",
        "- Do not output source_id, rank_score, prompt_chars, coverage_pct, V/C or other internal field names in prose.",
        *command_specific_rules,
        "",
        "Local core summary:",
        _json(_final_local_summary(data)),
        "",
        "Segment analysis notes:",
        _json(_segment_outputs_state(outputs)),
        "",
        "Source index:",
        _json(_source_refs(sources)),
    ]))


def _final_synthesis_command_rules(command: str) -> list[str]:
    if command == "theme_flow":
        return [
            "- The final report must include an explicit section named 「資金流向與資金輪動判斷」.",
            "- If fund-flow evidence is insufficient, write 「目前資金流證據不足」 and explain which evidence is missing.",
        ]
    if command == "macro":
        return [
            "- The final report must clearly cover market liquidity, risk appetite, index structure, and Taiwan market fund flow.",
        ]
    if command == "theme":
        return [
            "- The final report must distinguish core beneficiaries, secondary beneficiaries, watch-only names, and stocks that should not be included.",
        ]
    if command == "value_scan":
        return [
            "- The final report must rank candidates by verified rerating evidence, counter-evidence, and data gaps; do not rely only on price strength.",
        ]
    return []


def _build_compact_final_retry_prompt(
    request: CommandRequest,
    data: dict[str, Any],
    outputs: list[dict[str, Any]],
    sources: list[SourceItem],
    original_error: Exception,
) -> str:
    """Build a smaller final synthesis prompt after the normal final call fails.

    The retry keeps the decision-critical material: segment conclusions, risks,
    counter-evidence, missing data and source references. Full raw data remains
    in the segment prompt logs and report JSON.
    """

    compact_outputs = []
    for item in outputs:
        if not isinstance(item, dict):
            continue
        markdown = str(item.get("markdown") or "").strip()
        compact_outputs.append(
            {
                "label": item.get("label"),
                "title": item.get("title"),
                "status": item.get("status"),
                "error": item.get("error"),
                "note_excerpt": _truncate_segment_text(markdown, 600),
            }
        )
    retry_sources = _source_refs(sources[:80])
    return _sanitize_prompt_text("\n".join([
        "# Segmented AI final synthesis retry",
        "",
        f"Command: {request.command}",
        f"Data date: {data.get('report_date') or request.report_date or 'latest'}",
        f"Previous final synthesis failed: {original_error}",
        "",
        "Retry rules:",
        "- This is still a formal AI retry, not a local fallback report.",
        "- Keep the key conclusions, risks, counter-evidence, missing data and source references.",
        "- Use concise Traditional Chinese and avoid exposing internal field names.",
        "- If the evidence is insufficient, state the confidence limit clearly.",
        "",
        "Compact local core summary:",
        _json(_compact(_final_local_summary(data), depth=3, max_list=8, max_keys=40, max_string=800)),
        "",
        "Segment notes:",
        _json({"segment_count": len(compact_outputs), "segments": compact_outputs}),
        "",
        "Source index:",
        _json(retry_sources),
    ]))


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
                "note_excerpt": _truncate_segment_text(markdown, 900),
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
    return "\n".join([
        f"## {plan.get('title')}",
        "",
        f"Segment AI failed; this local summary is fallback only and not formal AI analysis. Error: {exc}",
        "",
        "```json",
        _json(_compact(payload, depth=3, max_list=10, max_keys=60, max_string=1200)),
        "```",
    ])


def _compose_segmented_fallback_report(request: CommandRequest, outputs: list[dict[str, Any]], exc: Exception) -> str:
    title = {
        "theme_radar": "theme_radar fallback report",
        "theme_flow": "theme_flow fallback report",
        "sector_strength": "sector_strength fallback report",
        "research": "research fallback report",
    }.get(request.command, "segmented AI fallback report")
    lines = [
        f"# {title}",
        "",
        f"Final AI synthesis failed. The following content only aggregates segment outputs and local summaries; it is not a formal AI report. Error: {exc}",
        "",
    ]
    for output in outputs:
        lines.extend([f"## {output.get('title')}", "", str(output.get("markdown") or "No usable segment output."), ""])
    return "\n".join(lines).strip() + "\n"


def _market_strength_plans(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"label": "market_price_rankings", "title": "Market price and volume rankings", "payload": _market_price_payload(data)},
        {"label": "market_sector_movers", "title": "Sector price and volume movers", "payload": _market_sector_mover_payload(data)},
        {"label": "sector_strength", "title": "Sector strength ranking", "payload": _market_sector_strength_payload(data)},
        {"label": "subsector_strength", "title": "Subsector strength ranking", "payload": _market_subsector_strength_payload(data)},
    ]


def _theme_evidence_plans(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"label": "theme_rankings", "title": "Theme rankings and matched companies", "payload": _theme_rankings_payload(data)},
        {"label": "theme_strong_stocks", "title": "Theme strong stocks and relations", "payload": _theme_strong_stocks_payload(data)},
        {"label": "theme_news_stats", "title": "Theme news statistics", "payload": _theme_news_payload(data)},
    ]


def _theme_flow_plans(data: dict[str, Any]) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = [
        {"label": "theme_flow_profile", "title": "Theme flow profile", "payload": _theme_flow_profile_payload(data)}
    ]
    related = data.get("related_stocks") or []
    related_chunks = _chunked(related, 30) or [[]]
    for index, chunk in enumerate(related_chunks, 1):
        plans.append({
            "label": f"theme_flow_related_stocks_{index}",
            "title": f"Theme related stocks {index}/{len(related_chunks)}",
            "payload": _theme_flow_related_stocks_payload(data, chunk, index, len(related_chunks)),
        })
    layers = data.get("layers") or []
    layer_chunks = _chunked(layers, 2) or [[]]
    for index, chunk in enumerate(layer_chunks, 1):
        plans.append({
            "label": f"theme_flow_layers_{index}",
            "title": f"Theme supply-chain layers {index}/{len(layer_chunks)}",
            "payload": _theme_flow_layers_payload(data, chunk, index, len(layer_chunks)),
        })
    plans.extend([
        {"label": "theme_flow_market_validation", "title": "Market validation", "payload": _theme_flow_validation_payload(data)},
        {"label": "theme_flow_next_candidates", "title": "Next diffusion candidates", "payload": _theme_flow_next_candidates_payload(data)},
        {"label": "theme_flow_news_stats", "title": "Theme news statistics", "payload": _theme_flow_news_payload(data)},
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
        "sector_rankings": _final_top_rows(sector_data.get("sector_rankings") or data.get("sector_rankings") or [], limit=4),
        "subsector_rankings": _final_top_rows(sector_data.get("subsector_rankings") or data.get("subsector_rankings") or [], limit=5),
        "theme_rankings": _final_top_rows(data.get("theme_rankings") or [], limit=5),
        "strong_stocks": _stock_refs(data.get("strong_stocks") or data.get("market_movers") or [], 10),
        "data_quality": _final_compact(data.get("data_quality") or sector_data.get("data_quality") or {}, depth=2, max_list=6, max_keys=20, max_string=420),
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
            item[key] = _truncate_segment_text(value.strip(), 420)
    for key in ("representative_stocks", "candidate_stocks", "strong_samples", "sector_strong_samples"):
        stocks = _stock_refs(row.get(key), 2)
        if stocks:
            item[key] = stocks
    nodes = row.get("strong_nodes") or row.get("nodes") or []
    if isinstance(nodes, list) and nodes:
        item["key_nodes"] = [_final_compact(node, depth=2, max_list=2, max_keys=8, max_string=160) for node in nodes[:2]]
        if len(nodes) > 2:
            item["omitted_node_count"] = len(nodes) - 2
    score_breakdown = row.get("score_breakdown")
    if isinstance(score_breakdown, dict):
        item["score_breakdown"] = _final_compact(score_breakdown, depth=2, max_list=6, max_keys=12, max_string=160)
    display_groups = row.get("display_stock_groups")
    if isinstance(display_groups, dict):
        item["display_stock_groups"] = {
            "verified_representatives": _stock_refs(display_groups.get("verified_representatives"), 2),
            "inferred_representatives": _stock_refs(display_groups.get("inferred_representatives"), 2),
            "candidate_watchlist": _stock_refs(display_groups.get("candidate_watchlist"), 2),
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
    selected = sources[:60]
    return [
        {
            "source_id": source.source_id,
            "title": _truncate_segment_text(source.title, 160),
            "source_level": source.source_level,
            "published_date": source.published_date,
            "provider": source.provider,
        }
        for source in selected
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
            result["remaining_items"] = _compact(remaining, depth=depth - 1, max_list=0, max_keys=0, max_string=max_string)
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
                "overflow_type": "remaining_list_items",
                "remaining_count": len(remaining),
                "full_data_preserved": True,
                "remaining_items": _compact(remaining, depth=depth - 1, max_list=0, max_keys=max_keys, max_string=max_string),
            })
        return items
    if isinstance(value, str) and len(value) > max_string:
        return {
            "overflow_type": "long_text",
            "original_length": len(value),
            "slice_size": max_string,
            "full_data_preserved": True,
            "slices": [
                {"part_index": index + 1, "text": value[start:start + max_string]}
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


def _sanitize_prompt_text(value: str) -> str:
    """Remove corrupted private-use glyphs before sending prompts to AI providers."""

    return re.sub(r"[\ue000-\uf8ff\ufffd]", "", value)


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)
