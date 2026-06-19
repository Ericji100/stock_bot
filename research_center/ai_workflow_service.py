from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .minimax_service import MiniMaxService
from .models import CommandRequest, SourceItem
from .prompt_logging import write_prompt_log

LOW_MODEL_DIGEST_SCHEMA_VERSION = "low_model_digest_v1"
LOW_MODEL_DIGEST_PROMPT_VERSION = "low_model_digest_prompt_v1"
HIGH_MODEL_INPUT_SCHEMA_VERSION = "high_model_input_package_v1"
AI_WORKFLOW_COVERAGE_SCHEMA_VERSION = "ai_workflow_coverage_v1"
DEFAULT_LOW_MODEL_NAME = "MiniMax-M3"
PROMPT_WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "prompt" / "workflow"
LOW_MODEL_ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "logs" / "ai_low_model"
HIGH_MODEL_BALANCED_THRESHOLD_CHARS = 180_000
HIGH_MODEL_COMPACT_THRESHOLD_CHARS = 320_000
LOW_MODEL_PROMPT_SOFT_LIMIT_CHARS = 320_000
LOW_MODEL_SEGMENT_TARGET_CHARS = 220_000
LOW_MODEL_SEGMENT_HARD_LIMIT_CHARS = 420_000
LOW_MODEL_MAX_SEGMENTS = 300
LOW_MODEL_EXECUTION_MAX_SEGMENTS = 48
LOW_MODEL_TEXT_EVIDENCE_LIMIT = 320
LOW_MODEL_QUOTA_COOLDOWN_HOURS = 6
TOKEN_ESTIMATE_DIVISOR = 4
SEMANTIC_COMMAND_CONTEXT_SCHEMA_VERSION = "semantic_command_context_v1"
COMPLETE_SEGMENT_CONTEXT_SCHEMA_VERSION = "complete_segment_context_v1"
AI_WORKFLOW_STANDARD_CAPABILITIES = [
    "local_data_package",
    "low_model_digest",
    "high_model_input_package",
    "deduped_or_indexed_input",
    "source_index",
    "input_audit",
    "html_sections",
    "diagnostics",
]
VOLATILE_FINGERPRINT_KEYS = {
    "generated_at",
    "created_at",
    "updated_at",
    "timestamp",
    "iso_timestamp",
    "run_at",
    "completed_at",
    "prompt_path",
    "prompt_paths",
    "artifact_paths",
    "diagnostics",
    "elapsed_seconds",
    "prompt_chars",
    "estimated_tokens",
    "rough_prompt_tokens",
    "usage",
    "token_usage",
}

ProgressCallback = Callable[[str], None]


def build_ai_workflow_coverage(
    command: str,
    *,
    local_data_package: bool,
    low_model_digest: dict[str, Any] | None,
    high_model_input_package: bool,
    dedupe_strategy: str,
    source_index: bool,
    input_audit: bool,
    html_sections: bool,
    diagnostics: dict[str, Any] | None = None,
    notes: list[str] | None = None,
    not_applicable: list[str] | None = None,
) -> dict[str, Any]:
    """Build a shared coverage record for every AI-powered command.

    This record is intentionally simple and stable: report commands, Radar,
    News, and topic maintenance can all expose the same capability checklist
    even when their internal data shape is different.
    """
    digest = low_model_digest if isinstance(low_model_digest, dict) else {}
    low_status = str(digest.get("status") or "missing")
    low_traceable = low_status in {"success", "partial_success", "cached", "skipped", "failed"}
    not_applicable_set = {str(item) for item in (not_applicable or [])}
    checks = {
        "local_data_package": bool(local_data_package),
        "low_model_digest": low_traceable,
        "high_model_input_package": bool(high_model_input_package),
        "deduped_or_indexed_input": bool(dedupe_strategy),
        "source_index": bool(source_index),
        "input_audit": bool(input_audit),
        "html_sections": bool(html_sections),
        "diagnostics": bool(diagnostics),
    }
    missing = [
        key
        for key, ok in checks.items()
        if not ok and key not in not_applicable_set
    ]
    return {
        "schema_version": AI_WORKFLOW_COVERAGE_SCHEMA_VERSION,
        "command": command,
        "standard_capabilities": list(AI_WORKFLOW_STANDARD_CAPABILITIES),
        "checks": checks,
        "not_applicable": sorted(not_applicable_set),
        "status": "aligned" if not missing else "partial",
        "missing_capabilities": missing,
        "low_model_status": low_status,
        "low_model_model": digest.get("model"),
        "dedupe_strategy": str(dedupe_strategy or ""),
        "diagnostics": diagnostics or {},
        "notes": notes or [],
    }


def _low_model_cooldown_file() -> Path:
    return LOW_MODEL_ARTIFACT_DIR / "cooldown" / "minimax_m2_7.json"


def _low_model_cooldown_status(now: datetime | None = None) -> dict[str, Any] | None:
    path = _low_model_cooldown_file()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    until_text = str(payload.get("cooldown_until") or "")
    if not until_text:
        return None
    try:
        until = datetime.fromisoformat(until_text.replace("Z", "+00:00"))
    except ValueError:
        return None
    current = now or datetime.now(timezone.utc)
    if until <= current:
        return None
    payload["cooldown_until"] = until.isoformat()
    return payload


def _write_low_model_cooldown(exc: BaseException | str, *, model_name: str) -> dict[str, Any]:
    error = str(exc)
    now = datetime.now(timezone.utc)
    reset_match = re.search(r"(?:reset|resets at|reset_at|until)[^0-9]*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", error, re.IGNORECASE)
    if reset_match:
        until = datetime.fromisoformat(reset_match.group(1).replace("Z", "+00:00"))
    else:
        until = now + timedelta(hours=LOW_MODEL_QUOTA_COOLDOWN_HOURS)
    payload = {
        "status": "cooldown",
        "model": model_name,
        "reason": "low_model_quota_or_rate_limit",
        "cooldown_until": until.isoformat(),
        "created_at": now.isoformat(),
        "error": error,
    }
    path = _low_model_cooldown_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return payload


def _is_low_model_quota_error(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "429",
            "quota",
            "rate limit",
            "rate_limit",
            "too many requests",
            "usage limit",
            "weekly",
            "insufficient balance",
        )
    )


def _low_model_cooldown_digest(
    request: CommandRequest,
    *,
    model_name: str,
    fingerprint: str,
    purpose: str,
    cooldown: dict[str, Any],
    sources: list[SourceItem],
) -> dict[str, Any]:
    digest = {
        "schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION,
        "status": "skipped",
        "enabled": True,
        "model": model_name,
        "reason": "low_model_quota_cooldown",
        "cooldown_until": cooldown.get("cooldown_until"),
        "error": cooldown.get("error"),
        "fingerprint": fingerprint,
        "prompt_chars": 0,
        "estimated_tokens": 0,
        "source_count": len(sources),
        "warnings": [
            "MiniMax M3 額度或速率限制仍在冷卻中，本次略過低階資料整理；高階模型會改用本地資料中心與完整來源索引。"
        ],
    }
    digest["validation"] = validate_low_model_digest(digest)
    digest["artifact_paths"] = save_low_model_digest_artifacts(request, digest, fingerprint=fingerprint, purpose=purpose)
    return digest


def should_run_low_model_digest(
    request: CommandRequest,
    *,
    enabled: bool,
    minimax: MiniMaxService,
) -> bool:
    if not enabled:
        return False
    if not minimax.is_configured():
        return False
    if request.source_only or request.command == "report":
        return False
    if request.command in {
        "data_status",
        "backfill_status",
        "news_status",
        "topic_maintain",
        "topic_review",
        "topic_confirm",
        "topic_reject",
        "topic_profiles",
        "topic_reset",
        "topic_seed_prompt",
        "topic_import",
        "topic_source_sync",
    }:
        return False
    return True


def attach_low_model_digest(
    request: CommandRequest,
    structured_data: dict[str, Any],
    sources: list[SourceItem],
    *,
    minimax: MiniMaxService,
    enabled: bool,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    if structured_data.get("low_model_digest"):
        return structured_data
    model_name = getattr(minimax, "model", DEFAULT_LOW_MODEL_NAME) or DEFAULT_LOW_MODEL_NAME
    if not should_run_low_model_digest(request, enabled=enabled, minimax=minimax):
        structured_data["low_model_digest"] = {
            "schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION,
            "status": "skipped",
            "enabled": bool(enabled),
            "model": model_name,
            "reason": "low_model_digest_not_applicable_or_not_configured",
        }
        return structured_data

    payload = _build_low_model_digest_payload(request, structured_data, sources)
    structured_data["low_model_input_policy"] = payload.get("low_model_input_policy")
    structured_data["low_model_text_evidence_count"] = len(payload.get("text_evidence") or [])
    structured_data["low_model_skipped_structured_sections"] = payload.get("skipped_structured_sections") or []
    _emit(
        progress,
        f"MiniMax M3 低階選擇性整理：text_evidence={len(payload.get('text_evidence') or [])} skipped_structured={len(payload.get('skipped_structured_sections') or [])}",
    )
    digest = run_low_model_digest_for_payload(
        request,
        payload,
        sources=sources,
        minimax=minimax,
        enabled=True,
        progress=progress,
        purpose="low_model_data_digest",
    )
    structured_data["low_model_prompt_path"] = str(digest.get("prompt_path") or "")
    structured_data["low_model_model"] = str(digest.get("model") or model_name)
    structured_data["low_model_digest"] = digest
    structured_data["low_model_diagnostics"] = digest.get("diagnostics") or {
        "status": digest.get("status"),
        "error": digest.get("error"),
        "validation": digest.get("validation"),
    }
    return structured_data


def _build_low_model_digest_payload(
    request: CommandRequest,
    structured_data: dict[str, Any],
    sources: list[SourceItem],
) -> dict[str, Any]:
    target = request.target or request.market_scope or request.theme_scope or request.candidate_pool
    evidence = _collect_low_model_text_evidence(structured_data, sources)
    skipped_sections = _low_model_skipped_structured_sections(request.command, structured_data)
    return {
        "command": request.command,
        "mode": request.mode,
        "target": target,
        "report_date": request.report_date.isoformat() if request.report_date else None,
        "low_model_input_policy": {
            "role": "text_evidence_organizer_only",
            "included": "新聞、搜尋結果、論壇、公告摘要、來源 snippet、風險反證、資料缺口等非結構化文字證據。",
            "excluded_from_low_model": "大型結構化表格、全市場排行、完整個股清單與本地量化表，不送低階模型重複整理。",
            "high_model_still_receives": "本地完整分段資料包仍會送高階模型分析；低階略過不代表高階未收到。",
            "forbidden": "低階模型不得做最終評分、投資結論、買賣建議或刪除核心資料。",
        },
        "text_evidence": evidence,
        "skipped_structured_sections": skipped_sections,
        "data_gap_summary": structured_data.get("data_gap_summary"),
        "report_confidence": structured_data.get("report_confidence"),
        "ai_input_audit": structured_data.get("ai_input_audit"),
    }


LOW_MODEL_TEXT_KEYS = {
    "news",
    "forum",
    "source",
    "sources",
    "evidence",
    "risk",
    "counter",
    "missing",
    "gap",
    "announcement",
    "official",
    "filing",
    "snippet",
    "summary",
    "description",
    "narrative",
    "context",
    "article",
    "headline",
    "title",
    "content",
}

LOW_MODEL_STRUCTURED_SKIP_KEYS = {
    "market_movers",
    "market_price_rankings",
    "sector_rankings",
    "subsector_rankings",
    "strong_stocks",
    "matched_companies",
    "related_stocks",
    "ai_candidates",
    "local_ranking",
    "local_scoring",
    "price_data",
    "technical_data",
    "institutional_data",
    "margin_data",
    "revenue_data",
    "financial_data",
    "quantitative_market",
    "theme_rankings",
    "layers",
    "layer_market_validation",
    "next_layer_candidates",
}


def _collect_low_model_text_evidence(
    structured_data: dict[str, Any],
    sources: list[SourceItem],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(row: dict[str, Any]) -> None:
        text = " ".join(str(value or "") for value in row.values()).strip()
        if not text:
            return
        marker = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()
        if marker in seen:
            return
        seen.add(marker)
        rows.append(row)

    for source in sources:
        text = " ".join(part for part in [source.title, source.snippet] if part).strip()
        if text:
            add(
                {
                    "資料類型": "來源文字",
                    "source_id": source.source_id,
                    "title": source.title,
                    "snippet": source.snippet,
                    "source_level": source.source_level,
                    "published_date": source.published_date,
                    "provider": source.provider,
                    "url": source.url,
                }
            )

    for key in (
        "unified_evidence_pack",
        "news_context",
        "news_batch",
        "forum_context",
        "topic_context",
        "supply_chain_profile",
        "data_gap_summary",
        "risk_evidence",
        "counter_evidence",
        "official_sources",
        "mops_sources",
        "search_results",
    ):
        if key in structured_data:
            _collect_text_evidence_from_value(key, structured_data.get(key), add)
        if len(rows) >= LOW_MODEL_TEXT_EVIDENCE_LIMIT:
            break

    return rows[:LOW_MODEL_TEXT_EVIDENCE_LIMIT]


def _collect_text_evidence_from_value(
    path: str,
    value: Any,
    add: Callable[[dict[str, Any]], None],
    *,
    depth: int = 0,
) -> None:
    if depth > 5:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            lower = key_text.lower()
            next_path = f"{path}.{key_text}"
            if isinstance(item, str) and _is_low_model_text_key(lower):
                add({"資料類型": "文字證據", "path": next_path, "text": _truncate_text(item, 2400)})
            elif isinstance(item, (dict, list, tuple)):
                _collect_text_evidence_from_value(next_path, item, add, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            if isinstance(item, dict):
                row = _text_evidence_row_from_dict(f"{path}[{index}]", item)
                if row:
                    add(row)
                _collect_text_evidence_from_value(f"{path}[{index}]", item, add, depth=depth + 1)
            elif isinstance(item, str):
                add({"資料類型": "文字證據", "path": f"{path}[{index}]", "text": _truncate_text(item, 2400)})


def _text_evidence_row_from_dict(path: str, row: dict[str, Any]) -> dict[str, Any] | None:
    result: dict[str, Any] = {"資料類型": "文字證據", "path": path}
    for key in ("source_id", "title", "headline", "summary", "snippet", "content", "description", "stance", "supports", "contradicts", "published_date", "url", "source_level", "provider"):
        value = row.get(key)
        if value not in (None, "", [], {}):
            result[key] = _truncate_text(value, 2400) if isinstance(value, str) else value
    if len(result) <= 2:
        return None
    return result


def _is_low_model_text_key(key: str) -> bool:
    return any(token in key for token in LOW_MODEL_TEXT_KEYS)


def _low_model_skipped_structured_sections(command: str, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in sorted(LOW_MODEL_STRUCTURED_SKIP_KEYS):
        value = structured_data.get(key)
        if value in (None, "", [], {}):
            continue
        rows.append(
            {
                "section": key,
                "reason": "大型結構化資料由本地完整分段資料包直接提供給高階模型，低階模型不重複整理。",
                "size_hint": _value_size_hint(value),
            }
        )
    return rows


def _value_size_hint(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {"type": "list", "count": len(value)}
    if isinstance(value, dict):
        return {"type": "dict", "keys": len(value)}
    return {"type": type(value).__name__, "chars": len(str(value))}


def run_low_model_digest_for_payload(
    request: CommandRequest,
    payload: dict[str, Any],
    *,
    sources: list[SourceItem] | None = None,
    minimax: MiniMaxService,
    enabled: bool,
    progress: ProgressCallback | None = None,
    purpose: str = "low_model_data_digest",
    max_sources: int = 250,
    max_list: int = 300,
    max_keys: int = 300,
    max_string: int = 2500,
    depth: int = 7,
) -> dict[str, Any]:
    """Run MiniMax M3 as a fact organizer before final analysis."""

    model_name = getattr(minimax, "model", DEFAULT_LOW_MODEL_NAME) or DEFAULT_LOW_MODEL_NAME
    if not enabled or not minimax.is_configured():
        return {
            "schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION,
            "status": "skipped",
            "enabled": bool(enabled),
            "model": model_name,
            "reason": "low_model_not_configured_or_disabled",
        }

    return _run_low_model_digest_guarded(
        request,
        payload,
        sources=sources or [],
        minimax=minimax,
        model_name=model_name,
        progress=progress,
        purpose=purpose,
        max_sources=max_sources,
        max_list=max_list,
        max_keys=max_keys,
        max_string=max_string,
        depth=depth,
    )

    prompt = build_low_model_digest_prompt_from_payload(
        payload,
        max_sources=max_sources,
        max_list=max_list,
        max_keys=max_keys,
        max_string=max_string,
        depth=depth,
    )
    fingerprint = _digest_fingerprint(request, payload, purpose=purpose)
    cached = _read_cached_low_model_digest(fingerprint)
    if cached:
        cached["cache_hit"] = True
        _emit(progress, f"MiniMax M3 資料整理使用快取：facts={len(cached.get('facts') or [])} sources={len(cached.get('source_map') or [])}")
        return cached

    _emit(progress, f"MiniMax M3 資料整理開始：model={model_name}")
    prompt_path = write_prompt_log(
        request,
        prompt,
        model_name,
        False,
        sources or [],
        {
            "purpose": purpose,
            "prompt_version": LOW_MODEL_DIGEST_PROMPT_VERSION,
            "role": "fact_organizer_only",
            "fingerprint": fingerprint,
        },
    )
    try:
        result = minimax.generate_json(prompt)
        parsed = _parse_json_object(result.markdown)
        digest = _normalize_digest(parsed, model_name=model_name)
        digest["prompt_path"] = str(prompt_path)
        digest["diagnostics"] = result.diagnostics
        digest["fingerprint"] = fingerprint
        digest["validation"] = validate_low_model_digest(digest)
        digest["artifact_paths"] = save_low_model_digest_artifacts(request, digest, fingerprint=fingerprint, purpose=purpose)
        _emit(progress, f"MiniMax M3 資料整理完成：facts={len(digest.get('facts') or [])} sources={len(digest.get('source_map') or [])}")
        if (digest.get("artifact_paths") or {}).get("json_path"):
            _emit(progress, f"低階資料包已保存：{(digest.get('artifact_paths') or {}).get('json_path')}")
        return digest
    except Exception as exc:
        cooldown_payload = _write_low_model_cooldown(exc, model_name=model_name) if _is_low_model_quota_error(exc) else None
        digest = {
            "schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION,
            "status": "failed",
            "enabled": True,
            "model": model_name,
            "prompt_path": str(prompt_path),
            "error": str(exc),
            "fingerprint": fingerprint,
        }
        digest["validation"] = validate_low_model_digest(digest)
        digest["artifact_paths"] = save_low_model_digest_artifacts(request, digest, fingerprint=fingerprint, purpose=purpose)
        _emit(progress, f"MiniMax M3 資料整理失敗，改用本地資料中心繼續：{exc}")
        return digest


def build_low_model_digest_prompt(
    request: CommandRequest,
    structured_data: dict[str, Any],
    sources: list[SourceItem],
) -> str:
    payload = {
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "report_date": request.report_date.isoformat() if request.report_date else None,
        "ai_data_center": structured_data.get("ai_data_center"),
        "ai_prompt_context": structured_data.get("ai_prompt_context"),
        "ai_input_audit": structured_data.get("ai_input_audit"),
        "report_confidence": structured_data.get("report_confidence"),
        "local_scoring": structured_data.get("local_scoring"),
        "unified_evidence_pack": structured_data.get("unified_evidence_pack"),
        "data_gap_summary": structured_data.get("data_gap_summary"),
        "sources": [asdict(item) for item in sources],
    }
    return build_low_model_digest_prompt_from_payload(payload)


def build_low_model_digest_prompt_from_payload(
    payload: dict[str, Any],
    *,
    max_sources: int = 250,
    max_list: int = 300,
    max_keys: int = 300,
    max_string: int = 2500,
    depth: int = 7,
) -> str:
    compact_payload = _prepare_low_model_payload_for_prompt(
        payload,
        max_sources=max_sources,
        max_list=max_list,
        max_keys=max_keys,
        max_string=max_string,
        depth=depth,
    )
    template = _read_workflow_prompt("low_model_digest.md")
    return template.replace(
        "{compact_payload_json}",
        json.dumps(compact_payload, ensure_ascii=False, indent=2, default=str),
    )


def _prepare_low_model_payload_for_prompt(
    payload: dict[str, Any],
    *,
    max_sources: int,
    max_list: int,
    max_keys: int,
    max_string: int,
    depth: int,
) -> dict[str, Any]:
    """Format low-model input without semantic pruning.

    The old helper name in prompt templates still says compact payload for
    compatibility, but the content is now a cleaned and segmented payload:
    rows are not dropped for importance, long text is split, and repeated
    source rows are de-duplicated mechanically.
    """

    return _prepare_complete_payload_value(
        payload,
        max_sources=max_sources,
        max_list=max_list,
        max_keys=max_keys,
        max_string=max_string,
        depth=depth,
    )


def _prepare_complete_payload_value(
    value: Any,
    *,
    max_sources: int,
    max_list: int,
    max_keys: int,
    max_string: int,
    depth: int,
) -> Any:
    if depth <= 0:
        if isinstance(value, str):
            return _split_text_for_prompt(value, max_string=max_string)
        return value
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text == "sources" and isinstance(item, list):
                item = _dedupe_sources_for_prompt(item, limit=max_sources)
            output[key_text] = _prepare_complete_payload_value(
                item,
                max_sources=max_sources,
                max_list=max_list,
                max_keys=max_keys,
                max_string=max_string,
                depth=depth - 1,
            )
        return output
    if isinstance(value, list):
        items = [
            _prepare_complete_payload_value(
                item,
                max_sources=max_sources,
                max_list=max_list,
                max_keys=max_keys,
                max_string=max_string,
                depth=depth - 1,
            )
            for item in _dedupe_rows_for_prompt(value)
        ]
        if len(items) <= max_list:
            return items
        chunks = [items[index:index + max_list] for index in range(0, len(items), max_list)]
        return {
            "資料型態": "完整分段清單",
            "總筆數": len(items),
            "每段筆數": max_list,
            "段數": len(chunks),
            "資料未刪除": True,
            "段落": [
                {"段號": index + 1, "筆數": len(chunk), "資料": chunk}
                for index, chunk in enumerate(chunks)
            ],
        }
    if isinstance(value, str):
        return _split_text_for_prompt(value, max_string=max_string)
    return value


def _split_text_for_prompt(value: str, *, max_string: int) -> Any:
    if len(value) <= max_string:
        return value
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


def _dedupe_sources_for_prompt(rows: list[Any], *, limit: int) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            marker = str(row.get("source_id") or row.get("url") or row.get("title") or json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
        else:
            marker = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(row)
        if len(result) >= limit:
            break
    return result


def _dedupe_rows_for_prompt(rows: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for row in rows:
        marker = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(row)
    return result


def attach_high_model_input_package(
    request: CommandRequest,
    structured_data: dict[str, Any],
    sources: list[SourceItem],
    *,
    prompt_chars_estimate: int,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    package = build_high_model_input_package(
        request,
        structured_data,
        sources,
        prompt_chars_estimate=prompt_chars_estimate,
    )
    structured_data["high_model_input_package"] = package
    structured_data["high_model_input_mode"] = package["input_mode"]
    structured_data["ai_workflow_policy"] = package["workflow_policy"]
    structured_data["ai_workflow_coverage"] = package.get("ai_workflow_coverage")
    structured_data["low_model_validation"] = package.get("low_model_validation")
    if package["input_mode"] != "full":
        _emit(
            progress,
            f"高階模型入模切換為{package['input_mode']}模式：原始 prompt 約 {prompt_chars_estimate} chars，完整資料保留在 JSON/來源檔。",
        )
    return structured_data


def build_high_model_input_package(
    request: CommandRequest,
    structured_data: dict[str, Any],
    sources: list[SourceItem],
    *,
    prompt_chars_estimate: int,
) -> dict[str, Any]:
    input_mode = _input_mode_for_prompt(prompt_chars_estimate)
    low_digest = structured_data.get("low_model_digest") or {
        "schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION,
        "status": "skipped",
        "model": DEFAULT_LOW_MODEL_NAME,
        "reason": "low_model_digest_not_attached_before_high_model_package",
    }
    low_validation = validate_low_model_digest(low_digest)
    selected_sources = _selected_sources_from_data(structured_data, sources)
    input_quality_gate = build_input_quality_gate(
        request,
        structured_data,
        sources,
        prompt_chars_estimate=prompt_chars_estimate,
    )
    excerpt_limit = 28 if input_mode == "balanced" else 18 if input_mode == "compact" else 45
    command_slice = _command_specific_slice(request, structured_data, input_mode=input_mode)
    include_full_context = _should_include_full_context(request, structured_data, input_mode)
    package = {
        "schema_version": HIGH_MODEL_INPUT_SCHEMA_VERSION,
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "report_date": request.report_date.isoformat() if request.report_date else None,
        "input_mode": input_mode,
        "prompt_chars_estimate_before_package": prompt_chars_estimate,
        "workflow_policy": {
            "目的": "在不犧牲報告品質的前提下，將完整核心資料清洗、去重、分類後分段提供給高階模型閱讀。",
            "完整資料保留": "核心分析資料不得因省 token 被語意壓縮或刪除；完整原始資料、完整來源、來源快照與結構化資料仍保存在報告 JSON、sources JSON 與本地快取。",
            "低階模型限制": "MiniMax M3 只做格式整理、去重、來源對照與資料缺口標記，不得判斷重要性、不得刪資料、不得做最終評分或買賣建議。",
            "高階模型責任": "高階模型必須分段閱讀完整核心資料，重新判斷、重新評分、檢查反證與資料缺口，不得直接照抄低階資料包。",
        },
        "token_budget_policy": {
            "balanced_threshold_chars": HIGH_MODEL_BALANCED_THRESHOLD_CHARS,
            "compact_threshold_chars": HIGH_MODEL_COMPACT_THRESHOLD_CHARS,
            "quality_first": True,
            "compression_method": "不做語意壓縮；只做去重、格式整理、來源標記與分段。若資料過大，改用完整資料分段閱讀，不以摘要取代核心資料。",
        },
        "input_quality_gate": input_quality_gate,
        "ai_data_center": structured_data.get("ai_data_center") if include_full_context else None,
        "ai_data_center_summary": _ai_data_center_summary(structured_data.get("ai_data_center")),
        "ai_prompt_context": structured_data.get("ai_prompt_context") if include_full_context else None,
        "ai_prompt_context_summary": _ai_prompt_context_summary(structured_data.get("ai_prompt_context")),
        "ai_input_audit": structured_data.get("ai_input_audit"),
        "report_confidence": structured_data.get("report_confidence"),
        "data_gap_summary": structured_data.get("data_gap_summary"),
        "unified_evidence_pack": (
            _complete_segment_value(structured_data.get("unified_evidence_pack"), input_mode=input_mode)
            if include_full_context
            else _evidence_pack_summary(structured_data.get("unified_evidence_pack"))
        ),
        "low_model_digest": _compact_low_model_digest(low_digest, input_mode=input_mode),
        "low_model_validation": low_validation,
        "low_model_input_policy": structured_data.get("low_model_input_policy"),
        "low_model_text_evidence_count": structured_data.get("low_model_text_evidence_count"),
        "low_model_skipped_structured_sections": structured_data.get("low_model_skipped_structured_sections"),
        "local_scoring": _complete_segment_value(structured_data.get("local_scoring"), input_mode=input_mode),
        "command_specific_data": command_slice,
        "selected_sources": (
            selected_sources[:excerpt_limit]
            if include_full_context
            else _source_index_summary(selected_sources, limit=excerpt_limit)
        ),
        "required_original_excerpts": _source_excerpts(sources, limit=excerpt_limit),
        "complete_source_index": (
            _complete_segment_value([asdict(item) for item in sources], input_mode=input_mode)
            if include_full_context
            else {
                "representation": "source_id_title_url_index",
                "source_count": len(sources),
                "sources": _source_index_summary(sources, limit=len(sources)),
            }
        ),
        "full_data_locations": {
            "report_json": "reports/.../*.json",
            "sources_json": "reports/.../*.sources.json",
            "low_model_artifacts": (low_digest.get("artifact_paths") or {}),
            "source_count": len(sources),
            "selected_source_count": len(selected_sources),
        },
    }
    coverage = build_ai_workflow_coverage(
        request.command,
        local_data_package=bool((command_slice.get("payload") if isinstance(command_slice, dict) else None)),
        low_model_digest=low_digest,
        high_model_input_package=True,
        dedupe_strategy=_dedupe_strategy_for_command(
            request.command,
            command_slice.get("payload") if isinstance(command_slice, dict) else {},
        ),
        source_index=bool(package.get("complete_source_index")),
        input_audit=bool(((command_slice or {}).get("core_input_audit") if isinstance(command_slice, dict) else None)),
        html_sections=True,
        diagnostics={
            "input_mode": input_mode,
            "prompt_chars_estimate_before_package": prompt_chars_estimate,
            "source_count": len(sources),
            "selected_source_count": len(selected_sources),
            "input_quality_gate": input_quality_gate,
        },
        notes=["報告型 AI 指令使用共用 high_model_input_package。", *input_quality_gate.get("warnings", [])],
    )
    package["ai_workflow_coverage"] = coverage
    return package


def build_input_quality_gate(
    request: CommandRequest,
    structured_data: dict[str, Any],
    sources: list[SourceItem],
    *,
    prompt_chars_estimate: int,
) -> dict[str, Any]:
    """Shared pre-analysis quality gate for high-model inputs.

    This does not remove or rewrite evidence. It only exposes whether the
    final model is receiving enough sources, enough candidate coverage, and a
    reasonable data-size shape to support a trustworthy report.
    """

    source_count = len(sources)
    min_sources = _minimum_source_count_for_command(request.command)
    warnings: list[str] = []
    if source_count < min_sources:
        warnings.append(f"來源數不足：目前 {source_count} 筆，建議至少 {min_sources} 筆；結論需降低確信度並標示資料缺口。")

    candidate_count = _candidate_count_for_quality_gate(request, structured_data)
    candidate_source_coverage = None
    if request.command == "value_scan":
        candidate_source_coverage = _value_scan_candidate_source_coverage(structured_data, sources)
        if candidate_count <= 0:
            warnings.append("價值重估候選股為 0，無法支撐正式重估排名。")
        elif source_count < max(min_sources, min(candidate_count, 12)):
            warnings.append(
                f"價值重估外部來源覆蓋不足：候選 {candidate_count} 檔、來源 {source_count} 筆；每檔重估結論需明確標示是否缺外部證據。"
            )
        if isinstance(candidate_source_coverage, dict) and candidate_source_coverage.get("zero_external_source_candidates"):
            zero_count = len(candidate_source_coverage.get("zero_external_source_candidates") or [])
            warnings.append(
                f"價值重估有 {zero_count} 檔候選股缺少可對應的外部搜尋來源；高階模型需把這些個股列為低確信或待補證據。"
            )

    if prompt_chars_estimate >= 1_000_000 and source_count < min_sources * 2:
        warnings.append("資料量極大但來源覆蓋偏少，可能是本地資料重複或候選股資料過重；高階模型需優先檢查證據密度。")

    return {
        "schema_version": "input_quality_gate_v1",
        "command": request.command,
        "status": "warning" if warnings else "ok",
        "source_count": source_count,
        "minimum_recommended_sources": min_sources,
        "candidate_count": candidate_count,
        "candidate_source_coverage": candidate_source_coverage,
        "prompt_chars_estimate_before_package": prompt_chars_estimate,
        "warnings": warnings,
        "quality_instruction": (
            "若 status=warning，高階模型仍可分析，但必須降低確信度、列出資料缺口、"
            "不得用單一低品質來源支撐高分或強結論。"
        ),
    }


def _minimum_source_count_for_command(command: str) -> int:
    return {
        "research": 8,
        "value_scan": 12,
        "macro": 10,
        "theme": 8,
        "theme_flow": 8,
        "theme_radar": 12,
        "sector_strength": 6,
        "topic_maintain": 8,
        "news": 12,
        "radar": 3,
    }.get(command, 6)


def _candidate_count_for_quality_gate(request: CommandRequest, structured_data: dict[str, Any]) -> int:
    if request.command == "value_scan":
        return len(structured_data.get("ai_candidates") or structured_data.get("candidates") or [])
    if request.command == "radar":
        return len(structured_data.get("candidates") or structured_data.get("prompt_jobs") or [])
    return 0


def _value_scan_candidate_source_coverage(structured_data: dict[str, Any], sources: list[SourceItem]) -> dict[str, Any]:
    candidates = structured_data.get("ai_candidates") or structured_data.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    evidence_pack = structured_data.get("ai_candidate_evidence_pack") or []
    evidence_by_code = {
        str(item.get("code") or "").strip(): item
        for item in evidence_pack
        if isinstance(item, dict) and str(item.get("code") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    source_texts = []
    for source in sources:
        source_texts.append(
            " ".join(
                str(part or "")
                for part in (
                    source.source_id,
                    source.title,
                    source.url,
                    source.snippet,
                    source.provider,
                    source.provider_detail,
                )
            )
        )

    for item in candidates:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        evidence = evidence_by_code.get(code) or {}
        source_events = item.get("source_events") or evidence.get("source_events") or []
        matched_sources = []
        for source, text in zip(sources, source_texts):
            if (code and code in text) or (name and name in text):
                matched_sources.append(source.source_id)
        rows.append({
            "code": code,
            "name": name,
            "external_source_count": len(matched_sources),
            "external_source_ids": matched_sources[:12],
            "local_source_event_count": len(source_events) if isinstance(source_events, list) else 0,
            "has_financial_detail": bool((item.get("financial_detail") or evidence.get("financial_detail")) not in (None, {}, [])),
            "has_company_knowledge": bool((item.get("company_knowledge") or evidence.get("company_knowledge")) not in (None, {}, [])),
        })
    zero_external = [row for row in rows if int(row.get("external_source_count") or 0) == 0]
    any_evidence = [
        row for row in rows
        if int(row.get("external_source_count") or 0) > 0 or int(row.get("local_source_event_count") or 0) > 0
    ]
    return {
        "schema_version": "value_scan_candidate_source_coverage_v1",
        "candidate_count": len(rows),
        "with_external_source_count": len(rows) - len(zero_external),
        "with_any_evidence_count": len(any_evidence),
        "zero_external_source_candidates": [
            {"code": row.get("code"), "name": row.get("name")}
            for row in zero_external[:30]
        ],
        "rows": rows,
    }


def _ai_data_center_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "schema_version": value.get("schema_version"),
        "command": value.get("command"),
        "target": value.get("target"),
        "policy": value.get("policy"),
        "report_confidence": value.get("report_confidence"),
        "ai_input_audit": value.get("ai_input_audit"),
        "source_selection": _source_selection_summary(value.get("source_selection")),
        "full_context_note": "完整 ai_data_center 已保留於報告 JSON；高階模型主要讀取 command_specific_data，避免同一資料重複入模。",
    }


def _should_include_full_context(request: CommandRequest, structured_data: dict[str, Any], input_mode: str) -> bool:
    if input_mode != "full":
        return False
    # Some upstream context builders intentionally compact their own snapshots.
    # Do not pass those generic truncation markers to the high model; use the
    # command-specific payload plus summaries instead, while preserving the full
    # original artifacts in JSON / sources files.
    for key in ("ai_data_center", "ai_prompt_context", "unified_evidence_pack"):
        value = structured_data.get(key)
        if _has_semantic_value(value) and _contains_truncation_marker(value):
            return False
    return True


def _source_selection_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    rows = value.get("selected_sources") or value.get("sources") or []
    if not isinstance(rows, list):
        rows = []
    return {
        "selected_count": len(rows),
        "policy": value.get("policy") or value.get("selection_policy"),
        "level_counts": value.get("level_counts") or value.get("source_level_counts"),
        "provider_counts": value.get("provider_counts"),
    }


def _source_index_summary(rows: list[Any], *, limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in rows[:limit]:
        if isinstance(item, SourceItem):
            result.append({
                "source_id": item.source_id,
                "title": item.title,
                "url": item.url,
                "source_level": item.source_level,
                "published_date": item.published_date,
                "provider": item.provider,
            })
        elif isinstance(item, dict):
            result.append({
                key: item.get(key)
                for key in ("source_id", "title", "url", "source_level", "published_date", "provider")
                if item.get(key) not in (None, "", [], {})
            })
    return result


def _ai_prompt_context_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    keep_keys = [
        "schema_version",
        "command",
        "target",
        "report_date",
        "data_quality",
        "data_gap_summary",
        "report_confidence",
        "ai_input_audit",
    ]
    summary = {key: value.get(key) for key in keep_keys if value.get(key) not in (None, "", [], {})}
    summary["full_context_note"] = "完整 ai_prompt_context 已保留於報告 JSON；高階模型不重複接收大型副本。"
    return summary


def _evidence_pack_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    items = value.get("items") if isinstance(value.get("items"), list) else []
    return {
        "schema_version": value.get("schema_version"),
        "item_count": len(items),
        "items": [_compact_evidence_summary_item(item) for item in items[:30]],
        "full_context_note": "完整 unified_evidence_pack 已保留於 command_specific_data 或報告 JSON；此處只保留摘要避免重複。",
    }


def _compact_evidence_summary_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"摘要": _truncate_text(item, 300)}
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    row: dict[str, Any] = {}
    for key in [
        "source_id",
        "source_level",
        "source_type",
        "provider",
        "title",
        "url",
        "published_date",
        "data_type",
        "evidence_type",
        "stance",
        "quality",
        "confidence",
        "summary",
        "finding",
        "supports",
        "contradicts",
        "missing_data",
    ]:
        value = item.get(key)
        if value in (None, "", [], {}):
            value = source.get(key)
        if value in (None, "", [], {}):
            value = payload.get(key)
        if value not in (None, "", [], {}):
            row[key] = _truncate_text(value, 500)
    if not row:
        row["摘要"] = _truncate_text(item, 500)
    return row


def validate_low_model_digest(digest: dict[str, Any] | None) -> dict[str, Any]:
    data = digest if isinstance(digest, dict) else {}
    warnings: list[str] = []
    status = str(data.get("status") or "missing")
    if not data:
        warnings.append("未取得低階模型資料包。")
    if data.get("schema_version") not in {None, LOW_MODEL_DIGEST_SCHEMA_VERSION}:
        warnings.append("低階資料包 schema_version 不符合預期。")
    if status not in {"success", "partial_success", "cached", "skipped"}:
        warnings.append(f"低階資料包狀態為 {status}。")
    counts = {
        "facts": len(data.get("facts") or []),
        "events": len(data.get("events") or []),
        "risk_evidence": len(data.get("risk_evidence") or []),
        "counter_evidence": len(data.get("counter_evidence") or []),
        "missing_data": len(data.get("missing_data") or []),
        "source_map": len(data.get("source_map") or []),
        "failed_segments": len(data.get("failed_segment_index") or []),
    }
    if status in {"success", "partial_success"} and counts["facts"] == 0 and counts["events"] == 0:
        warnings.append("低階資料包沒有整理出事實或事件。")
    if status in {"success", "partial_success"} and counts["source_map"] == 0:
        warnings.append("低階資料包缺少來源對照。")
    return {
        "valid": status in {"success", "partial_success", "cached"} and not any("schema_version" in item for item in warnings),
        "status": status,
        "counts": counts,
        "warnings": warnings,
    }


def save_low_model_digest_artifacts(
    request: CommandRequest,
    digest: dict[str, Any],
    *,
    fingerprint: str,
    purpose: str,
) -> dict[str, str]:
    date_dir = datetime.now().strftime("%Y%m%d")
    out_dir = LOW_MODEL_ARTIFACT_DIR / date_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_target = _safe_slug(request.target or request.market_scope or request.theme_scope or request.candidate_pool or "target")
    safe_purpose = _safe_slug(purpose)
    base_name = f"{request.command}_{safe_target}_{safe_purpose}_{fingerprint[:12]}"
    json_path = out_dir / f"{base_name}.json"
    md_path = out_dir / f"{base_name}.md"
    json_path.write_text(json.dumps(digest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(_digest_to_markdown(digest), encoding="utf-8")
    return {"json_path": str(json_path), "markdown_path": str(md_path)}


def _read_workflow_prompt(name: str) -> str:
    path = PROMPT_WORKFLOW_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8-sig")
    return (
        "你是台股 AI 投研資料中心的低階資料整理模型。\n"
        "你只能整理資料、來源、風險、反證與缺口，不得做最終投資判斷。\n"
        "請輸出可被 json.loads() 解析的 JSON。\n"
        "{compact_payload_json}"
    )


def _normalize_digest(value: dict[str, Any], *, model_name: str) -> dict[str, Any]:
    result = dict(value) if isinstance(value, dict) else {}
    result["schema_version"] = str(result.get("schema_version") or LOW_MODEL_DIGEST_SCHEMA_VERSION)
    result["status"] = str(result.get("status") or "success")
    result["enabled"] = True
    result["model"] = model_name
    result["model_role"] = str(result.get("model_role") or "資料整理員")
    for key in ("facts", "events", "risk_evidence", "counter_evidence", "missing_data", "source_map", "warnings"):
        if not isinstance(result.get(key), list):
            result[key] = []
    return result


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("MiniMax low model digest is not a JSON object.")
    return parsed


def _digest_fingerprint(request: CommandRequest, payload: dict[str, Any], *, purpose: str) -> str:
    identity = {
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "report_date": request.report_date.isoformat() if request.report_date else None,
        "purpose": purpose,
        "schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION,
        "payload": _stable_fingerprint_payload(payload),
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _read_cached_low_model_digest(fingerprint: str) -> dict[str, Any] | None:
    for path in LOW_MODEL_ARTIFACT_DIR.glob(f"**/*_{fingerprint[:12]}.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        validation = validate_low_model_digest(data)
        if validation.get("valid"):
            data["validation"] = validation
            data["artifact_paths"] = {
                "json_path": str(path),
                "markdown_path": str(path.with_suffix(".md")),
            }
            data["status"] = "cached"
            return data
    return None


def _input_mode_for_prompt(prompt_chars_estimate: int) -> str:
    if prompt_chars_estimate >= HIGH_MODEL_COMPACT_THRESHOLD_CHARS:
        return "compact"
    if prompt_chars_estimate >= HIGH_MODEL_BALANCED_THRESHOLD_CHARS:
        return "balanced"
    return "full"


def _selected_sources_from_data(structured_data: dict[str, Any], sources: list[SourceItem]) -> list[dict[str, Any]]:
    selected = ((structured_data.get("ai_data_center") or {}).get("source_selection") or {}).get("selected_sources") or []
    result: list[dict[str, Any]] = []
    for item in selected:
        if isinstance(item, dict):
            source = item.get("source") or {}
            result.append({
                "source": source,
                "reasons": item.get("reasons") or [],
                "status": item.get("status") or "入模",
            })
    if result:
        return result
    return [{"source": asdict(item), "reasons": ["完整來源索引保留"], "status": "已入模"} for item in sources]


def _source_excerpts(sources: list[SourceItem], *, limit: int) -> list[dict[str, Any]]:
    excerpts: list[dict[str, Any]] = []
    for item in sources[:limit]:
        excerpts.append({
            "source_id": item.source_id,
            "title": item.title,
            "url": item.url,
            "source_level": item.source_level,
            "published_date": item.published_date,
            "provider": item.provider,
            "snippet": _truncate_text(item.snippet, 900),
        })
    return excerpts


def _dedupe_strategy_for_command(command: str, payload: Any) -> str:
    if isinstance(payload, dict):
        schema = str(payload.get("schema_version") or "")
        if schema in {"theme_radar_relation_payload_v1", "sector_strength_relation_payload_v1"}:
            return "stock_index_relation_tables"
        if payload.get("stock_index"):
            return "stock_index_with_references"
        if payload.get("unified_evidence_pack"):
            return "evidence_summary_with_source_index"
    if command in {"research", "value_scan", "macro", "theme", "theme_flow"}:
        return "semantic_core_sections_with_evidence_summary"
    if command == "radar":
        return "radar_candidate_compact_pack"
    if command == "news":
        return "news_batch_deduped_classification"
    if command == "topic_maintain":
        return "topic_change_pack_batches"
    return "semantic_core_sections"



def _command_specific_slice(request: CommandRequest, structured_data: dict[str, Any], *, input_mode: str) -> dict[str, Any]:
    payload = _semantic_command_payload(request, structured_data, input_mode=input_mode)
    core_audit = _build_core_input_audit(request.command, structured_data, payload)
    return {
        "schema_version": COMPLETE_SEGMENT_CONTEXT_SCHEMA_VERSION,
        "legacy_schema_version": SEMANTIC_COMMAND_CONTEXT_SCHEMA_VERSION,
        "input_mode": input_mode,
        "policy": "指令感知完整分段：核心資料不做語意壓縮，只做清洗、去重、來源標記與分段；高階模型必須閱讀所有入模段落後再產出結論。",
        "core_input_audit": core_audit,
        "payload": payload,
    }


def _semantic_command_payload(request: CommandRequest, structured_data: dict[str, Any], *, input_mode: str) -> dict[str, Any]:
    command = request.command
    if command == "research":
        return _semantic_payload_for_keys(
            structured_data,
            [
                "stock", "price_data", "technical_data", "institutional_data", "margin_data",
                "revenue_data", "financial_data", "valuation_data", "tdcc_data", "source_events",
                "company_knowledge", "local_rerating_snapshot", "topic_context", "local_scoring",
                "unified_evidence_pack",
            ],
            input_mode=input_mode,
            max_list=24,
            depth=5,
        )
    if command == "value_scan":
        return _value_scan_semantic_payload(structured_data, input_mode=input_mode)
    if command == "macro":
        return _semantic_payload_for_keys(
            structured_data,
            [
                "market_scope", "region_scope", "quantitative_market", "volatility",
                "industry_flow", "fear_greed", "market_score", "free_public_sources",
                "global_public_macro", "news_context", "unified_evidence_pack",
            ],
            input_mode=input_mode,
            max_list=30,
            depth=5,
        )
    if command == "theme_radar":
        return _theme_radar_semantic_payload(structured_data, input_mode=input_mode)
    if command == "theme_flow":
        return _semantic_payload_for_keys(
            structured_data,
            [
                "theme", "theme_query", "related_stock_count", "related_stocks", "layers",
                "layer_market_validation", "next_layer_candidates", "news_stats",
                "topic_context", "supply_chain_profile", "data_quality", "unified_evidence_pack",
            ],
            input_mode=input_mode,
            max_list=40,
            depth=6,
        )
    if command == "sector_strength":
        return _sector_strength_semantic_payload(structured_data, input_mode=input_mode)
    if command == "theme":
        return _semantic_payload_for_keys(
            structured_data,
            [
                "theme", "theme_scope", "theme_query", "matched_universe", "matched_companies",
                "supply_chain_profile", "company_knowledge_summary", "theme_quality_context",
                "topic_context", "theme_rankings", "sector_rankings", "unified_evidence_pack",
            ],
            input_mode=input_mode,
            max_list=50,
            depth=6,
        )
    if command == "radar":
        return _semantic_payload_for_keys(
            structured_data,
            ["candidates", "evidence_pack", "ai_compact_pack", "feature_pack", "data_coverage", "local_scoring"],
            input_mode=input_mode,
            max_list=50,
            depth=5,
        )
    if command == "news":
        return _semantic_payload_for_keys(
            structured_data,
            ["news_batch", "news_context", "sources", "feature_pack", "data_coverage"],
            input_mode=input_mode,
            max_list=80,
            depth=5,
        )
    if command == "topic_maintain":
        return _semantic_payload_for_keys(
            structured_data,
            [
                "existing_profiles", "source_candidates", "candidate_topics", "candidate_companies",
                "change_pack", "topic_context", "evidence_pack", "data_gap_summary",
            ],
            input_mode=input_mode,
            max_list=60,
            depth=6,
        )
    return _semantic_payload_for_keys(
        structured_data,
        ["news_context", "feature_pack", "data_coverage", "local_scoring", "unified_evidence_pack"],
        input_mode=input_mode,
        max_list=30,
        depth=5,
    )


def _value_scan_semantic_payload(structured_data: dict[str, Any], *, input_mode: str) -> dict[str, Any]:
    evidence_pack = structured_data.get("ai_candidate_evidence_pack") or []
    raw_candidates = structured_data.get("ai_candidates") or []
    candidate_summary: list[dict[str, Any]] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        source_events = item.get("source_events") or []
        candidate_summary.append({
            "code": item.get("code"),
            "name": item.get("name"),
            "industry": item.get("industry"),
            "price": item.get("price"),
            "revenue_yoy": item.get("revenue_yoy"),
            "old_market_label": item.get("old_market_label"),
            "new_market_label": item.get("new_market_label"),
            "rerating_score": item.get("rerating_score"),
            "verification_score": item.get("verification_score"),
            "early_signal_priority": item.get("early_signal_priority"),
            "score_components": _value_scan_score_components(item.get("score_components")),
            "rerating_evidence": _value_scan_short_list(item.get("rerating_evidence"), limit=8),
            "counter_evidence": _value_scan_short_list(item.get("counter_evidence"), limit=8),
            "financial_key_metrics": _value_scan_financial_key_metrics(item.get("financial_detail")),
            "company_knowledge_summary": _value_scan_company_knowledge_summary(item.get("company_knowledge")),
            "source_event_summary": {
                "count": len(source_events) if isinstance(source_events, list) else 0,
                "examples": _value_scan_source_event_examples(source_events, limit=5),
            },
            "raw_candidate_location": "structured_data.json.ai_candidates",
        })
    payload = {
        "candidate_pool": structured_data.get("candidate_pool"),
        "report_date": structured_data.get("report_date"),
        "total_candidate_count": structured_data.get("total_candidate_count"),
        "ai_candidate_limit": structured_data.get("ai_candidate_limit"),
        "ai_candidates": candidate_summary,
        "ai_candidate_evidence_summary": _value_scan_evidence_pack_summary(evidence_pack),
        "local_ranking": _semantic_compact(structured_data.get("local_ranking"), input_mode=input_mode, max_list=40, depth=4),
        "scoring_rules": _semantic_compact(structured_data.get("scoring_rules"), input_mode=input_mode, max_list=20, depth=4),
        "topic_context": _semantic_compact(structured_data.get("topic_context"), input_mode=input_mode, max_list=20, depth=4),
        "local_scoring": _semantic_compact(structured_data.get("local_scoring"), input_mode=input_mode, max_list=30, depth=5),
        "unified_evidence_pack": _evidence_pack_summary(structured_data.get("unified_evidence_pack")),
        "raw_data_policy": {
            "ai_candidates": "高階模型收到候選主檔摘要；完整 raw 候選資料保留在 structured_data.json.ai_candidates。",
            "ai_candidate_evidence_pack": "高階模型收到 ai_candidate_evidence_summary；完整逐檔證據包保留在 structured_data.json.ai_candidate_evidence_pack。",
            "chip_backup_data": "避免逐檔完整籌碼備援資料重複展開；高階模型使用 chip_backup_summary / source_event_summary / evidence_pack 判斷，需要追查時看附錄 JSON。",
        },
    }
    return {key: value for key, value in payload.items() if _has_semantic_value(value)}


def _value_scan_score_components(value: Any) -> Any:
    if not isinstance(value, dict):
        return value if _has_semantic_value(value) else None
    keep_keys = [
        "financial",
        "valuation",
        "growth",
        "quality",
        "chip",
        "technical",
        "theme",
        "rerating",
        "verification",
        "risk",
        "total",
        "composite",
    ]
    result = {key: value.get(key) for key in keep_keys if value.get(key) not in (None, "", [], {})}
    if result:
        return result
    return {
        str(key): _truncate_text(item, 220)
        for key, item in list(value.items())[:12]
        if item not in (None, "", [], {})
    }


def _value_scan_short_list(value: Any, *, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return [value] if _has_semantic_value(value) else []
    result: list[Any] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            result.append({
                str(key): _truncate_text(val, 260)
                for key, val in item.items()
                if val not in (None, "", [], {})
            })
        else:
            result.append(_truncate_text(item, 260))
    if len(value) > limit:
        result.append({"其餘筆數": len(value) - limit, "完整資料位置": "structured_data.json.ai_candidates"})
    return result


def _value_scan_financial_key_metrics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "unavailable"} if value in (None, "", [], {}) else {"summary": _truncate_text(value, 300)}
    keep_keys = [
        "status",
        "revenue_yoy",
        "revenue_mom",
        "gross_margin",
        "operating_margin",
        "net_margin",
        "eps",
        "roe",
        "roa",
        "free_cash_flow",
        "inventory_turnover",
        "debt_ratio",
        "latest_quarter",
        "latest_month",
        "data_quality",
        "missing_data",
    ]
    result = {key: value.get(key) for key in keep_keys if value.get(key) not in (None, "", [], {})}
    if result:
        result["full_financial_detail_location"] = "structured_data.json.ai_candidates[].financial_detail"
        return result
    return {
        "field_count": len(value),
        "full_financial_detail_location": "structured_data.json.ai_candidates[].financial_detail",
    }


def _value_scan_company_knowledge_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keep_keys = [
        "products",
        "customers",
        "supply_chain",
        "moat",
        "transformation",
        "themes",
        "risks",
        "missing_data",
    ]
    result: dict[str, Any] = {}
    for key in keep_keys:
        item = value.get(key)
        if item in (None, "", [], {}):
            continue
        if isinstance(item, list):
            result[key] = [_truncate_text(row, 180) for row in item[:8]]
            if len(item) > 8:
                result[key].append({"其餘筆數": len(item) - 8})
        else:
            result[key] = _truncate_text(item, 300)
    if result:
        result["full_company_knowledge_location"] = "structured_data.json.ai_candidates[].company_knowledge"
    return result


def _value_scan_source_event_examples(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            result.append({"summary": _truncate_text(item, 240)})
            continue
        row: dict[str, Any] = {}
        for key in ("source_id", "title", "event", "summary", "stance", "published_date", "source_type", "provider"):
            if item.get(key) not in (None, "", [], {}):
                row[key] = _truncate_text(item.get(key), 240)
        if row:
            result.append(row)
    return result


def _value_scan_evidence_pack_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source_events = item.get("source_events") if isinstance(item.get("source_events"), list) else []
        row: dict[str, Any] = {
            "code": item.get("code"),
            "name": item.get("name"),
            "rerating_reason": _first_semantic_value(
                item,
                "rerating_reason",
                "reason",
                "rerating_thesis",
                "investment_thesis",
                "value_unlock_reason",
            ),
            "supporting_evidence": _value_scan_short_list(
                item.get("supporting_evidence")
                or item.get("rerating_evidence")
                or item.get("positive_evidence")
                or item.get("evidence"),
                limit=8,
            ),
            "counter_evidence": _value_scan_short_list(
                item.get("counter_evidence") or item.get("negative_evidence"),
                limit=8,
            ),
            "failure_conditions": _value_scan_short_list(
                item.get("failure_conditions")
                or item.get("failure_condition")
                or item.get("risk_notes")
                or item.get("risks"),
                limit=8,
            ),
            "source_ids": _value_scan_source_ids(item),
            "rerating_score": item.get("rerating_score"),
            "verification_score": item.get("verification_score"),
            "early_signal_priority": item.get("early_signal_priority"),
            "local_score_summary": _value_scan_local_score_summary(item),
            "score_components": _value_scan_score_components(item.get("score_components")),
            "financial_key_metrics": _value_scan_financial_key_metrics(item.get("financial_detail")),
            "source_event_count": len(source_events),
            "source_event_examples": _value_scan_source_event_examples(source_events, limit=4),
            "chip_backup_summary": item.get("chip_backup_summary"),
            "cross_validation": _value_scan_cross_validation_summary(item.get("cross_validation")),
            "missing_data": _value_scan_short_list(item.get("missing_data"), limit=8),
            "raw_evidence_location": "structured_data.json.ai_candidate_evidence_pack",
        }
        rows.append({key: val for key, val in row.items() if val not in (None, "", [], {})})
    return {
        "schema_version": "value_scan_evidence_summary_v1",
        "candidate_count": len(rows),
        "summary_policy": "保留每檔重估分數、財務關鍵欄位、交叉驗證、來源事件數與代表證據；完整逐檔證據包留在 JSON 附錄。",
        "candidates": rows,
    }


def _first_semantic_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if _has_semantic_value(value):
            return _truncate_text(value, 500)
    return None


def _value_scan_source_ids(item: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("source_id", "id", "url", "source_url"):
                candidate = value.get(key)
                if candidate not in (None, "", [], {}) and str(candidate) not in found:
                    found.append(str(candidate))
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    collect(nested)
        elif isinstance(value, list):
            for row in value:
                collect(row)

    for key in ("source_events", "sources", "evidence", "rerating_evidence", "supporting_evidence", "counter_evidence"):
        collect(item.get(key))
    return found[:12]


def _value_scan_local_score_summary(item: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "rerating_score",
        "verification_score",
        "early_signal_priority",
        "local_rerating_composite_score",
        "valuation_score",
        "growth_score",
        "quality_score",
        "chip_score",
        "technical_score",
        "risk_score",
    ]
    summary = {key: item.get(key) for key in keys if item.get(key) not in (None, "", [], {})}
    components = _value_scan_score_components(item.get("score_components"))
    if components:
        summary["score_components"] = components
    return summary


def _value_scan_cross_validation_summary(value: Any) -> Any:
    if not isinstance(value, dict):
        return value if _has_semantic_value(value) else None
    keep_keys = [
        "status",
        "verified_points",
        "conflicting_points",
        "missing_points",
        "source_count",
        "confidence",
        "warnings",
    ]
    result = {key: value.get(key) for key in keep_keys if value.get(key) not in (None, "", [], {})}
    return result or {"field_count": len(value)}


def _semantic_payload_for_keys(
    structured_data: dict[str, Any],
    keys: list[str],
    *,
    input_mode: str,
    max_list: int,
    depth: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in keys:
        value = _resolve_structured_value(structured_data, key)
        if _has_semantic_value(value):
            if key == "unified_evidence_pack":
                payload[key] = _evidence_pack_summary(value)
            else:
                payload[key] = _semantic_compact(value, input_mode=input_mode, max_list=max_list, depth=depth)
    return payload


def _theme_radar_semantic_payload(structured_data: dict[str, Any], *, input_mode: str) -> dict[str, Any]:
    sector_strength = _resolve_structured_value(structured_data, "sector_strength") or {}
    payload = {
        "theme": _semantic_compact(_resolve_structured_value(structured_data, "theme"), input_mode=input_mode, max_list=5, depth=4),
        "matched_companies": _semantic_compact(
            _resolve_structured_value(structured_data, "matched_companies")
            or _resolve_structured_value(structured_data, "matched_universe")
            or _resolve_structured_value(structured_data, "related_stocks"),
            input_mode=input_mode,
            max_list=60,
            depth=5,
        ),
        "topic_context": _semantic_compact(_resolve_structured_value(structured_data, "topic_context"), input_mode=input_mode, max_list=30, depth=5),
        "supply_chain_profile": _semantic_compact(_resolve_structured_value(structured_data, "supply_chain_profile"), input_mode=input_mode, max_list=30, depth=5),
        "theme_rankings": [
            _compact_theme_radar_row(row, input_mode=input_mode)
            for row in list(_resolve_structured_value(structured_data, "theme_rankings") or [])
            if isinstance(row, dict)
        ],
        "sector_rankings": _semantic_compact(
            _resolve_structured_value(structured_data, "sector_rankings") or (sector_strength or {}).get("sector_rankings"),
            input_mode=input_mode,
            max_list=20,
            depth=5,
        ),
        "subsector_rankings": _semantic_compact(
            _resolve_structured_value(structured_data, "subsector_rankings") or (sector_strength or {}).get("subsector_rankings"),
            input_mode=input_mode,
            max_list=30,
            depth=5,
        ),
        "theme_flow_summaries": _semantic_compact(_resolve_structured_value(structured_data, "theme_flow_summaries"), input_mode=input_mode, max_list=6, depth=5),
        "strong_stocks": [_compact_stock_core(row, input_mode=input_mode) for row in list(_resolve_structured_value(structured_data, "strong_stocks") or []) if isinstance(row, dict)],
        "news_theme_stats": _semantic_compact(_resolve_structured_value(structured_data, "news_theme_stats"), input_mode=input_mode, max_list=30, depth=4),
        "data_quality": _semantic_compact(_resolve_structured_value(structured_data, "data_quality"), input_mode=input_mode, max_list=20, depth=4),
        "unified_evidence_pack": _evidence_pack_summary(_resolve_structured_value(structured_data, "unified_evidence_pack")),
    }
    return _dedupe_theme_radar_payload(payload, input_mode=input_mode)


def _dedupe_theme_radar_payload(payload: dict[str, Any], *, input_mode: str) -> dict[str, Any]:
    """Represent theme radar data as a stock index plus relation tables.

    This keeps every core relationship, but avoids repeatedly expanding the
    same stock object inside theme, sector, subsector, and strong-stock lists.
    """

    stock_index: dict[str, dict[str, Any]] = {}

    def add_stock(row: Any) -> str | None:
        if not isinstance(row, dict):
            return None
        code = str(row.get("code") or row.get("stock_id") or "").strip()
        name = str(row.get("name") or row.get("stock_name") or "").strip()
        if not code and not name:
            return None
        key = code or name
        compact = _compact_stock_core(row, input_mode=input_mode)
        compact["code"] = code or compact.get("code") or key
        if name and not compact.get("name"):
            compact["name"] = name
        if key in stock_index:
            stock_index[key] = _merge_stock_core(stock_index[key], compact)
        else:
            stock_index[key] = compact
        return key

    def refs(rows: Any) -> list[str]:
        if isinstance(rows, dict) and isinstance(rows.get("畾菔"), list):
            output: list[str] = []
            for chunk in rows.get("畾菔") or []:
                if isinstance(chunk, dict):
                    output.extend(refs(chunk.get("鞈?")))
            return output
        if not isinstance(rows, list):
            return []
        output = []
        for row in rows:
            ref = add_stock(row)
            if ref:
                output.append(ref)
        return output

    def group_refs(groups: Any) -> dict[str, Any]:
        if not isinstance(groups, dict):
            return {}
        result: dict[str, Any] = {}
        for key, value in groups.items():
            if isinstance(value, list):
                result[f"{key}_codes"] = refs(value)
            else:
                result[key] = value
        return result

    theme_rows: list[dict[str, Any]] = []
    for row in payload.get("theme_rankings") or []:
        if not isinstance(row, dict):
            continue
        item = {key: value for key, value in row.items() if key not in {"representative_stocks", "candidate_stocks", "display_stock_groups"}}
        item["representative_stock_codes"] = refs(row.get("representative_stocks"))
        item["candidate_stock_codes"] = refs(row.get("candidate_stocks"))
        display_groups = group_refs(row.get("display_stock_groups"))
        if display_groups:
            item["display_stock_group_codes"] = display_groups
        theme_rows.append(item)

    sector_rows: list[dict[str, Any]] = []
    for row in payload.get("sector_rankings") or []:
        if not isinstance(row, dict):
            continue
        item = {
            key: value
            for key, value in row.items()
            if key not in {"sector_strong_samples", "representative_stocks", "candidate_stocks", "top_subsectors", "display_stock_groups"}
        }
        for source_key, dest_key in (
            ("sector_strong_samples", "sector_strong_codes"),
            ("representative_stocks", "representative_stock_codes"),
            ("candidate_stocks", "candidate_stock_codes"),
        ):
            stock_refs = refs(row.get(source_key))
            if stock_refs:
                item[dest_key] = stock_refs
        top_subsectors = []
        for sub in row.get("top_subsectors") or []:
            if isinstance(sub, dict):
                sub_item = {key: value for key, value in sub.items() if key != "strong_samples"}
                sub_refs = refs(sub.get("strong_samples"))
                if sub_refs:
                    sub_item["strong_stock_codes"] = sub_refs
                top_subsectors.append(sub_item)
        if top_subsectors:
            item["top_subsectors"] = top_subsectors
        display_groups = group_refs(row.get("display_stock_groups"))
        if display_groups:
            item["display_stock_group_codes"] = display_groups
        sector_rows.append(item)

    subsector_rows: list[dict[str, Any]] = []
    for row in payload.get("subsector_rankings") or []:
        if not isinstance(row, dict):
            continue
        item = {key: value for key, value in row.items() if key != "strong_samples"}
        strong_refs = refs(row.get("strong_samples"))
        if strong_refs:
            item["strong_stock_codes"] = strong_refs
        subsector_rows.append(item)

    matched_refs = refs(payload.get("matched_companies"))
    strong_refs = refs(payload.get("strong_stocks"))
    flow_rows: list[dict[str, Any]] = []
    for flow in payload.get("theme_flow_summaries") or []:
        if not isinstance(flow, dict):
            continue
        flow_item = {
            key: value
            for key, value in flow.items()
            if key not in {"related_stocks", "layers", "layer_market_validation", "next_layer_candidates"}
        }
        related_refs = refs(flow.get("related_stocks"))
        if related_refs:
            flow_item["related_stock_codes"] = related_refs
        layers = []
        for layer in flow.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            layer_item = {
                key: value
                for key, value in layer.items()
                if key not in {"stocks", "companies", "related_stocks", "candidate_stocks", "representative_stocks", "display_stock_groups"}
            }
            for source_key, dest_key in (
                ("stocks", "stock_codes"),
                ("companies", "company_codes"),
                ("related_stocks", "related_stock_codes"),
                ("candidate_stocks", "candidate_stock_codes"),
                ("representative_stocks", "representative_stock_codes"),
            ):
                layer_refs = refs(layer.get(source_key))
                if layer_refs:
                    layer_item[dest_key] = layer_refs
            display_groups = group_refs(layer.get("display_stock_groups"))
            if display_groups:
                layer_item["display_stock_group_codes"] = display_groups
            layers.append(layer_item)
        if layers:
            flow_item["layers"] = layers
        flow_item["layer_market_validation"] = _semantic_compact(
            flow.get("layer_market_validation"),
            input_mode=input_mode,
            max_list=20,
            depth=4,
        )
        flow_item["next_layer_candidates"] = _semantic_compact(
            flow.get("next_layer_candidates"),
            input_mode=input_mode,
            max_list=20,
            depth=4,
        )
        flow_rows.append(flow_item)

    return {
        "schema_version": "theme_radar_relation_payload_v1",
        "representation_policy": {
            "method": "deduplicated_stock_index_with_relation_tables",
            "data_integrity": "stock rows are stored once in stock_index; theme, sector, subsector and strong-stock sections reference them by code/name.",
            "raw_full_data": "full original data remains in report JSON and HTML appendices.",
        },
        "theme": payload.get("theme"),
        "stock_index": list(stock_index.values()),
        "matched_company_codes": matched_refs,
        "matched_companies": {
            "representation": "stock_code_refs",
            "stock_codes": matched_refs,
        },
        "theme_rankings": theme_rows,
        "sector_rankings": sector_rows,
        "subsector_rankings": subsector_rows,
        "strong_stock_codes": strong_refs,
        "strong_stocks": {
            "representation": "stock_code_refs",
            "stock_codes": strong_refs,
        },
        "topic_context": payload.get("topic_context"),
        "supply_chain_profile": payload.get("supply_chain_profile"),
        "theme_flow_summaries": flow_rows,
        "news_theme_stats": payload.get("news_theme_stats"),
        "data_quality": payload.get("data_quality"),
        "unified_evidence_pack": payload.get("unified_evidence_pack"),
        "integrity_counts": {
            "stock_index_count": len(stock_index),
            "matched_company_ref_count": len(matched_refs),
            "theme_count": len(theme_rows),
            "sector_count": len(sector_rows),
            "subsector_count": len(subsector_rows),
            "theme_flow_count": len(flow_rows),
            "strong_stock_ref_count": len(strong_refs),
        },
    }


def _sector_strength_semantic_payload(structured_data: dict[str, Any], *, input_mode: str) -> dict[str, Any]:
    """Build a deduplicated sector-strength packet for the high model.

    Sector strength can contain the same stock in market movers, sector rows,
    subsector rows, theme rows, and strong-stock rows. Store each stock once in
    stock_index and reference it by code/name everywhere else.
    """

    stock_index: dict[str, dict[str, Any]] = {}

    def add_stock(row: Any) -> str | None:
        if not isinstance(row, dict):
            return None
        code = str(row.get("code") or row.get("stock_id") or "").strip()
        name = str(row.get("name") or row.get("stock_name") or "").strip()
        if not code and not name:
            return None
        key = code or name
        compact = _compact_stock_core(row, input_mode=input_mode)
        compact["code"] = code or compact.get("code") or key
        if name and not compact.get("name"):
            compact["name"] = name
        if key in stock_index:
            stock_index[key] = _merge_stock_core(stock_index[key], compact)
        else:
            stock_index[key] = compact
        return key

    def refs(rows: Any) -> list[str]:
        if not isinstance(rows, list):
            return []
        output: list[str] = []
        for row in rows:
            ref = add_stock(row)
            if ref:
                output.append(ref)
        return output

    def compact_market_movers(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        list_keys = (
            "top_gainers",
            "top_losers",
            "top_volume_surge",
            "top_turnover",
            "top_trend_strength",
            "new_highs",
            "new_lows",
            "active_movers",
        )
        result = {
            key: value.get(key)
            for key in (
                "market_data_date",
                "report_generated_at",
                "source_mode",
                "hard_filter_policy",
                "data_quality",
            )
            if value.get(key) not in (None, "", [], {})
        }
        for key in list_keys:
            stock_refs = refs(value.get(key))
            if stock_refs:
                result[f"{key}_codes"] = stock_refs
        mover_rows = []
        for row in value.get("sector_mover_rankings") or []:
            if not isinstance(row, dict):
                continue
            item = {
                key: row.get(key)
                for key in (
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
                if row.get(key) not in (None, "", [], {})
            }
            for list_key in ("top_gainers", "top_losers", "top_volume_surge", "top_turnover"):
                stock_refs = refs(row.get(list_key))
                if stock_refs:
                    item[f"{list_key}_codes"] = stock_refs
            mover_rows.append(item)
        if mover_rows:
            result["sector_mover_rankings"] = mover_rows
        return result

    def compact_sector_rows(rows: Any) -> list[dict[str, Any]]:
        result = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            item = {
                key: row.get(key)
                for key in (
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
                if row.get(key) not in (None, "", [], {})
            }
            for source_key, dest_key in (
                ("sector_strong_samples", "sector_strong_codes"),
                ("representative_stocks", "representative_stock_codes"),
                ("candidate_stocks", "candidate_stock_codes"),
            ):
                stock_refs = refs(row.get(source_key))
                if stock_refs:
                    item[dest_key] = stock_refs
            top_subsectors = []
            for sub in row.get("top_subsectors") or []:
                if not isinstance(sub, dict):
                    continue
                sub_item = {
                    key: sub.get(key)
                    for key in (
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
                    if sub.get(key) not in (None, "", [], {})
                }
                stock_refs = refs(sub.get("strong_samples"))
                if stock_refs:
                    sub_item["strong_stock_codes"] = stock_refs
                top_subsectors.append(sub_item)
            if top_subsectors:
                item["top_subsectors"] = top_subsectors
            result.append(item)
        return result

    def compact_subsector_rows(rows: Any) -> list[dict[str, Any]]:
        result = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            item = {
                key: row.get(key)
                for key in (
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
                if row.get(key) not in (None, "", [], {})
            }
            stock_refs = refs(row.get("strong_samples"))
            if stock_refs:
                item["strong_stock_codes"] = stock_refs
            result.append(item)
        return result

    def compact_theme_rows(rows: Any) -> list[dict[str, Any]]:
        result = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            item = _compact_theme_radar_row(row, input_mode=input_mode)
            item.pop("representative_stocks", None)
            item.pop("candidate_stocks", None)
            representative_refs = refs(row.get("representative_stocks"))
            candidate_refs = refs(row.get("candidate_stocks"))
            if representative_refs:
                item["representative_stock_codes"] = representative_refs
            if candidate_refs:
                item["candidate_stock_codes"] = candidate_refs
            result.append(item)
        return result

    market_movers = _resolve_structured_value(structured_data, "market_movers")
    sector_rankings = _resolve_structured_value(structured_data, "sector_rankings")
    subsector_rankings = _resolve_structured_value(structured_data, "subsector_rankings")
    strong_stocks = _resolve_structured_value(structured_data, "strong_stocks")
    theme_rankings = _resolve_structured_value(structured_data, "theme_rankings")

    strong_refs = refs(strong_stocks)
    return {
        "schema_version": "sector_strength_relation_payload_v1",
        "representation_policy": {
            "method": "deduplicated_stock_index_with_relation_tables",
            "data_integrity": "stock rows are stored once in stock_index; market, sector, subsector and theme sections reference them by code/name.",
            "raw_full_data": "full original data remains in report JSON and HTML appendices.",
        },
        "market_movers": compact_market_movers(market_movers),
        "sector_rankings": compact_sector_rows(sector_rankings),
        "subsector_rankings": compact_subsector_rows(subsector_rankings),
        "theme_rankings": compact_theme_rows(theme_rankings),
        "strong_stock_codes": strong_refs,
        "strong_stocks": {
            "representation": "stock_code_refs",
            "stock_codes": strong_refs,
        },
        "stock_index": list(stock_index.values()),
        "data_quality": _semantic_compact(_resolve_structured_value(structured_data, "data_quality"), input_mode=input_mode, max_list=20, depth=4),
        "unified_evidence_pack": _evidence_pack_summary(_resolve_structured_value(structured_data, "unified_evidence_pack")),
        "integrity_counts": {
            "stock_index_count": len(stock_index),
            "sector_count": len(sector_rankings or []),
            "subsector_count": len(subsector_rankings or []),
            "theme_count": len(theme_rankings or []),
            "strong_stock_ref_count": len(strong_refs),
        },
    }


def _merge_stock_core(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if value in (None, "", [], {}):
            continue
        if key not in merged or merged.get(key) in (None, "", [], {}):
            merged[key] = value
    return merged


def _compact_theme_radar_row(row: dict[str, Any], *, input_mode: str) -> dict[str, Any]:
    keys = [
        "theme_id", "theme_name", "theme_strength_score", "theme_score", "lifecycle",
        "theme_state", "avg_trend_score", "trend_pullback_count", "active_breakout_count",
        "strong_stock_count", "direct_relation_count", "candidate_count", "score_breakdown",
        "main_risks", "news_stats", "interpretation_hint", "representative_policy",
    ]
    compact = {key: _semantic_compact(row.get(key), input_mode=input_mode, max_list=8, depth=3) for key in keys if row.get(key) is not None}
    compact["strong_nodes"] = _semantic_compact(row.get("strong_nodes") or [], input_mode=input_mode, max_list=8, depth=4)
    compact["representative_stocks"] = [_compact_stock_core(item, input_mode=input_mode) for item in (row.get("representative_stocks") or []) if isinstance(item, dict)]
    compact["candidate_stocks"] = [_compact_stock_core(item, input_mode=input_mode) for item in (row.get("candidate_stocks") or []) if isinstance(item, dict)]
    return compact


def _compact_stock_core(row: dict[str, Any], *, input_mode: str) -> dict[str, Any]:
    keys = [
        "code", "name", "industry", "sector", "sector_display_name", "primary_subsector",
        "price", "change_pct", "change_pct_5d", "change_pct_10d", "change_pct_20d",
        "volume_ratio", "turnover", "new_high_days", "near_high_20d",
        "pullback_from_high_pct", "trend_score", "trend_state", "trend_summary",
        "price_date", "primary_theme_id", "primary_theme_name", "relation_score",
        "match_method", "supply_chain_role", "reason", "evidence_summary",
    ]
    compact = {key: row.get(key) for key in keys if row.get(key) is not None}
    compact["theme_matches"] = [
        _compact_theme_match(item)
        for item in (row.get("theme_matches") or [])[:6]
        if isinstance(item, dict)
    ]
    compact["subsector_matches"] = [
        _compact_subsector_match(item)
        for item in (row.get("subsector_matches") or [])[:6]
        if isinstance(item, dict)
    ]
    return compact


def _compact_theme_match(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence") if isinstance(row.get("evidence"), list) else []
    counter = row.get("counter_evidence") if isinstance(row.get("counter_evidence"), list) else []
    missing = row.get("missing_data")
    missing_count = len(missing) if isinstance(missing, list) else len(missing or {}) if isinstance(missing, dict) else 0
    return {
        key: value
        for key, value in {
            "theme_id": row.get("theme_id"),
            "theme_name": row.get("theme_name"),
            "match_method": row.get("match_method"),
            "relation_score": row.get("relation_score"),
            "confidence": row.get("confidence"),
            "source_level": row.get("source_level"),
            "relation_type": row.get("relation_type"),
            "verification_status": row.get("verification_status"),
            "supply_chain_role": row.get("supply_chain_role"),
            "layer": row.get("layer"),
            "benefit_logic": _truncate_text(str(row.get("benefit_logic") or ""), 260) if row.get("benefit_logic") else None,
            "evidence_count": len(evidence),
            "counter_evidence_count": len(counter),
            "missing_data_count": missing_count,
            "risk_note_count": len(row.get("risk_notes") or []) if isinstance(row.get("risk_notes"), list) else 0,
        }.items()
        if value not in (None, "", [], {})
    }


def _compact_subsector_match(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "sector": row.get("sector"),
            "sector_display_name": row.get("sector_display_name"),
            "subsector": row.get("subsector"),
            "subsector_score": row.get("subsector_score"),
            "match_method": row.get("match_method"),
            "confidence": row.get("confidence"),
        }.items()
        if value not in (None, "", [], {})
    }


def _resolve_structured_value(structured_data: dict[str, Any], key: str) -> Any:
    if key in structured_data:
        return structured_data.get(key)
    feature_pack = structured_data.get("feature_pack") if isinstance(structured_data.get("feature_pack"), dict) else {}
    if key in feature_pack:
        return feature_pack.get(key)
    shared = structured_data.get("shared_data_layer") if isinstance(structured_data.get("shared_data_layer"), dict) else {}
    if key in shared:
        return shared.get(key)
    sector_strength = shared.get("sector_strength") if isinstance(shared.get("sector_strength"), dict) else {}
    if key in sector_strength:
        return sector_strength.get(key)
    report_quality = structured_data.get("report_quality") if isinstance(structured_data.get("report_quality"), dict) else {}
    evidence_pack = report_quality.get("evidence_pack") if isinstance(report_quality.get("evidence_pack"), dict) else {}
    if key in evidence_pack:
        return evidence_pack.get(key)
    return None


def _build_core_input_audit(command: str, structured_data: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    sections = _core_sections_for_command(command)
    rows = []
    for section in sections:
        aliases = _core_section_aliases(section)
        raw_value = None
        for alias in aliases:
            raw_value = _resolve_structured_value(structured_data, alias)
            if _has_semantic_value(raw_value):
                break
        sent_value = None
        for alias in aliases:
            if alias in payload:
                sent_value = payload.get(alias)
                break
        optional_sections = _not_required_when_missing_sections(command)
        if _contains_truncation_marker(sent_value):
            status = "compression_error"
            note = "核心資料被截斷標記取代，違反完整分段入模規則。"
        elif _has_semantic_value(sent_value):
            status = "direct"
            note = "高階模型已收到完整核心資料或完整分段資料。"
        elif _has_semantic_value(raw_value):
            status = "appendix_only"
            note = "原始資料存在，但本次未直接送入高階模型。"
        elif section in optional_sections:
            status = "not_required"
            note = "本指令不是全市場排行型任務，這類資料本次不需要。"
        else:
            status = "source_missing"
            note = "資料源不足或本次未取得。"
        rows.append({
            "section": section,
            "status": status,
            "raw_count": _semantic_count(raw_value),
            "sent_count": _semantic_count(sent_value),
            "note": note,
        })
    return {
        "schema_version": "core_input_audit_v1",
        "command": command,
        "sections": rows,
        "status_counts": {
            status: sum(1 for row in rows if row["status"] == status)
            for status in ("direct", "appendix_only", "source_missing", "compression_error", "not_required")
        },
    }


def _core_sections_for_command(command: str) -> list[str]:
    mapping = {
        "research": ["stock", "price_data", "technical_data", "institutional_data", "margin_data", "revenue_data", "financial_data", "local_scoring", "topic_context", "unified_evidence_pack"],
        "value_scan": ["ai_candidates", "ai_candidate_evidence_pack", "local_ranking", "local_scoring", "topic_context", "unified_evidence_pack"],
        "macro": ["quantitative_market", "volatility", "industry_flow", "fear_greed", "market_score", "global_public_macro", "unified_evidence_pack"],
        "theme": ["theme", "matched_companies", "topic_context", "supply_chain_profile", "theme_rankings", "sector_rankings", "unified_evidence_pack"],
        "theme_radar": ["theme", "matched_companies", "topic_context", "supply_chain_profile", "theme_rankings", "sector_rankings", "subsector_rankings", "strong_stocks", "news_theme_stats", "unified_evidence_pack"],
        "theme_flow": ["theme", "related_stocks", "layers", "layer_market_validation", "next_layer_candidates", "news_stats", "topic_context", "supply_chain_profile", "unified_evidence_pack"],
        "sector_strength": ["sector_rankings", "subsector_rankings", "strong_stocks", "market_movers", "theme_rankings", "unified_evidence_pack"],
        "radar": ["candidates", "evidence_pack", "ai_compact_pack", "feature_pack", "data_coverage"],
        "news": ["news_batch", "news_context", "sources", "feature_pack", "data_coverage"],
        "topic_maintain": ["existing_profiles", "source_candidates", "candidate_topics", "candidate_companies", "change_pack", "topic_context", "evidence_pack", "data_gap_summary"],
    }
    return mapping.get(command, ["feature_pack", "news_context", "data_coverage", "unified_evidence_pack"])


def _not_required_when_missing_sections(command: str) -> set[str]:
    mapping = {
        "theme": {"theme_rankings", "sector_rankings"},
        "research": {"theme_rankings", "sector_rankings", "subsector_rankings"},
        "value_scan": {"theme_rankings", "sector_rankings", "subsector_rankings"},
        "macro": {"theme_rankings", "sector_rankings", "subsector_rankings"},
    }
    return mapping.get(command, set())


def _core_section_aliases(section: str) -> list[str]:
    aliases = {
        "matched_companies": ["matched_companies", "matched_universe", "related_stocks"],
        "sector_rankings": ["sector_rankings"],
        "subsector_rankings": ["subsector_rankings"],
    }
    return aliases.get(section, [section])


def _semantic_compact(value: Any, *, input_mode: str, max_list: int, depth: int, max_string: int | None = None) -> Any:
    if not _has_semantic_value(value):
        return value
    string_limit = max_string or (700 if input_mode == "compact" else 900 if input_mode == "balanced" else 1100)
    return _semantic_segment_value(value, max_list=max_list, depth=depth, max_string=string_limit)


def _semantic_segment_value(value: Any, *, max_list: int, depth: int, max_string: int) -> Any:
    """Prepare core data for model input without semantic deletion.

    `max_list` is kept for compatibility with older callers, but this function
    does not drop rows. Large lists are represented as chunks so downstream
    prompts can read them by segment while retaining every item.
    """

    if isinstance(value, dict):
        return {
            str(key): _semantic_segment_value(item, max_list=max_list, depth=max(depth - 1, 0), max_string=max_string)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = [
            _semantic_segment_value(item, max_list=max_list, depth=max(depth - 1, 0), max_string=max_string)
            for item in list(value)
        ]
        if len(items) <= max_list:
            return items
        chunks = [items[index:index + max_list] for index in range(0, len(items), max_list)]
        return {
            "資料型態": "完整分段清單",
            "總筆數": len(items),
            "每段筆數": max_list,
            "段數": len(chunks),
            "資料未刪除": True,
            "段落": [
                {"段號": index + 1, "筆數": len(chunk), "資料": chunk}
                for index, chunk in enumerate(chunks)
            ],
        }
    if isinstance(value, str) and len(value) > max_string:
        return {
            "資料型態": "完整分段文字",
            "總字數": len(value),
            "每段字數": max_string,
            "資料未刪除": True,
            "段落": [
                {"段號": index + 1, "文字": value[index:index + max_string]}
                for index in range(0, len(value), max_string)
            ],
        }
    return value


def _semantic_compact_value(value: Any, *, max_list: int, depth: int, max_string: int) -> Any:
    if depth <= 0:
        if isinstance(value, dict):
            return {"摘要": f"{len(value)} 個欄位，完整資料在 JSON / HTML 附錄"}
        if isinstance(value, (list, tuple)):
            return {"摘要": f"{len(value)} 筆資料，完整資料在 JSON / HTML 附錄"}
        return _truncate_text(value, max_string) if isinstance(value, str) else value
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            output[str(key)] = _semantic_compact_value(item, max_list=max_list, depth=depth - 1, max_string=max_string)
        return output
    if isinstance(value, (list, tuple)):
        result = [_semantic_compact_value(item, max_list=max_list, depth=depth - 1, max_string=max_string) for item in list(value)[:max_list]]
        omitted = len(value) - max_list
        if omitted > 0:
            result.append({"省略筆數": omitted, "說明": "超出高階模型核心摘要上限，完整資料保留在 JSON / HTML 附錄"})
        return result
    if isinstance(value, str) and len(value) > max_string:
        return value[:max_string].rstrip() + "...（完整文字在 JSON / HTML 附錄）"
    return value


def _has_semantic_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, (list, tuple, set, str)):
        return bool(value)
    return True


def _semantic_count(value: Any) -> int:
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if value is None:
        return 0
    return 1 if _has_semantic_value(value) else 0


def _contains_truncation_marker(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return "<list truncated>" in text or "<dict truncated>" in text


def _complete_segment_value(value: Any, *, input_mode: str) -> Any:
    string_limit = 900 if input_mode == "compact" else 1200 if input_mode == "balanced" else 1600
    list_segment_size = 60 if input_mode == "compact" else 90 if input_mode == "balanced" else 120
    return {
        "schema_version": COMPLETE_SEGMENT_CONTEXT_SCHEMA_VERSION,
        "policy": "完整分段封包：不做語意壓縮、不刪核心資料；清單與長文只切成段落供高階模型逐段閱讀。",
        "payload": _semantic_segment_value(
            value,
            max_list=list_segment_size,
            depth=7,
            max_string=string_limit,
        ),
    }


def _compact_value(value: Any, *, input_mode: str) -> Any:
    return _complete_segment_value(value, input_mode=input_mode)


def _compact_low_model_digest(digest: dict[str, Any], *, input_mode: str) -> dict[str, Any]:
    if not isinstance(digest, dict):
        return {}
    result = {key: digest.get(key) for key in ("schema_version", "status", "model", "model_role", "prompt_path", "artifact_paths", "validation", "cache_hit", "diagnostics")}
    for key in ("facts", "events", "risk_evidence", "counter_evidence", "missing_data", "source_map"):
        result[key] = _complete_segment_value(digest.get(key) or [], input_mode=input_mode)
    result["failed_segment_index"] = _complete_segment_value(digest.get("failed_segment_index") or [], input_mode=input_mode)
    result["segment_runs_summary"] = _complete_segment_value(
        [
            {
                "label": item.get("label"),
                "status": item.get("status"),
                "prompt_chars": item.get("prompt_chars"),
                "retry_prompt_chars": item.get("retry_prompt_chars"),
                "source_count": item.get("source_count"),
            }
            for item in (digest.get("segment_runs") or [])
            if isinstance(item, dict)
        ],
        input_mode=input_mode,
    )
    return result


def _digest_to_markdown(digest: dict[str, Any]) -> str:
    lines = [
        "# MiniMax M3 資料整理底稿",
        "",
        f"- 狀態：{digest.get('status')}",
        f"- 模型：{digest.get('model')}",
        f"- 指紋：{digest.get('fingerprint') or ''}",
        "",
    ]
    for key, title in [
        ("facts", "事實整理"),
        ("events", "事件整理"),
        ("risk_evidence", "風險證據"),
        ("counter_evidence", "反證整理"),
        ("missing_data", "資料缺口"),
        ("warnings", "整理警示"),
    ]:
        rows = digest.get(key) or []
        if not rows:
            continue
        lines.extend([f"## {title}", ""])
        for row in rows:
            lines.append(f"- {json.dumps(row, ensure_ascii=False, default=str)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _safe_slug(value: Any) -> str:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", str(value or "")).strip("_")
    return text[:48] or "item"


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "...(截斷，完整內容保存在來源檔)"


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)


def _run_low_model_digest_guarded(
    request: CommandRequest,
    payload: dict[str, Any],
    *,
    sources: list[SourceItem],
    minimax: MiniMaxService,
    model_name: str,
    progress: ProgressCallback | None,
    purpose: str,
    max_sources: int,
    max_list: int,
    max_keys: int,
    max_string: int,
    depth: int,
) -> dict[str, Any]:
    fingerprint = _digest_fingerprint(request, payload, purpose=purpose)
    cached = _read_cached_low_model_digest(fingerprint)
    if cached:
        cached["cache_hit"] = True
        _emit(progress, f"MiniMax M3 低階整理快取命中：facts={len(cached.get('facts') or [])} sources={len(cached.get('source_map') or [])}")
        return cached

    cooldown = _low_model_cooldown_status()
    if cooldown:
        digest = _low_model_cooldown_digest(
            request,
            model_name=model_name,
            fingerprint=fingerprint,
            purpose=purpose,
            cooldown=cooldown,
            sources=sources,
        )
        _emit(progress, f"MiniMax M3 額度冷卻中，略過低階整理：cooldown_until={digest.get('cooldown_until')}")
        return digest

    prompt = build_low_model_digest_prompt_from_payload(
        payload,
        max_sources=max_sources,
        max_list=max_list,
        max_keys=max_keys,
        max_string=max_string,
        depth=depth,
    )
    prompt_chars = len(prompt)
    source_count = len(sources) or _payload_source_count(payload)
    if prompt_chars > LOW_MODEL_PROMPT_SOFT_LIMIT_CHARS:
        return _run_segmented_low_model_digest(
            request,
            payload,
            sources=sources,
            minimax=minimax,
            model_name=model_name,
            progress=progress,
            purpose=purpose,
            fingerprint=fingerprint,
            max_sources=max_sources,
            max_list=max_list,
            max_keys=max_keys,
            max_string=max_string,
            depth=depth,
            original_prompt_chars=prompt_chars,
        )

    _emit(
        progress,
        f"MiniMax M3 低階整理開始：model={model_name} prompt={prompt_chars} chars est_tokens={_estimate_tokens(prompt_chars)} sources={source_count}",
    )
    prompt_path = write_prompt_log(
        request,
        prompt,
        model_name,
        False,
        sources,
        {
            "purpose": purpose,
            "prompt_version": LOW_MODEL_DIGEST_PROMPT_VERSION,
            "role": "fact_organizer_only",
            "fingerprint": fingerprint,
            "prompt_chars": prompt_chars,
            "estimated_tokens": _estimate_tokens(prompt_chars),
            "source_count": source_count,
            "low_model_mode": "single",
        },
    )
    try:
        result = minimax.generate_json(prompt)
        parsed = _parse_json_object(result.markdown)
        digest = _normalize_digest(parsed, model_name=model_name)
        digest["prompt_path"] = str(prompt_path)
        digest["diagnostics"] = result.diagnostics
        digest["fingerprint"] = fingerprint
        digest["prompt_chars"] = prompt_chars
        digest["estimated_tokens"] = _estimate_tokens(prompt_chars)
        digest["validation"] = validate_low_model_digest(digest)
        digest["artifact_paths"] = save_low_model_digest_artifacts(request, digest, fingerprint=fingerprint, purpose=purpose)
        _emit(progress, f"MiniMax M3 低階整理完成：facts={len(digest.get('facts') or [])} sources={len(digest.get('source_map') or [])}")
        return digest
    except Exception as exc:
        if not _is_low_model_quota_error(exc):
            retry_digest = _retry_single_low_model_digest(
                request,
                minimax,
                model_name=model_name,
                purpose=purpose,
                fingerprint=fingerprint,
                payload=payload,
                sources=sources,
                prompt_path=prompt_path,
                prompt_chars=prompt_chars,
                source_count=source_count,
                first_error=str(exc),
                progress=progress,
            )
            if retry_digest is not None:
                return retry_digest
        cooldown_payload = _write_low_model_cooldown(exc, model_name=model_name) if _is_low_model_quota_error(exc) else None
        digest = {
            "schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION,
            "status": "failed",
            "enabled": True,
            "model": model_name,
            "prompt_path": str(prompt_path),
            "error": str(exc),
            "fingerprint": fingerprint,
            "prompt_chars": prompt_chars,
            "estimated_tokens": _estimate_tokens(prompt_chars),
        }
        if cooldown_payload:
            digest["reason"] = "low_model_quota_or_rate_limit"
            digest["cooldown_until"] = cooldown_payload.get("cooldown_until")
        digest["validation"] = validate_low_model_digest(digest)
        digest["artifact_paths"] = save_low_model_digest_artifacts(request, digest, fingerprint=fingerprint, purpose=purpose)
        _emit(progress, f"MiniMax M3 低階整理失敗，改用本地資料中心繼續：{exc}")
        return digest


def _retry_single_low_model_digest(
    request: CommandRequest,
    minimax: MiniMaxService,
    *,
    model_name: str,
    purpose: str,
    fingerprint: str,
    payload: dict[str, Any],
    sources: list[SourceItem],
    prompt_path: Path,
    prompt_chars: int,
    source_count: int,
    first_error: str,
    progress: ProgressCallback | None,
) -> dict[str, Any] | None:
    source_ids = [item.source_id for item in sources[:80]]
    _emit(progress, f"MiniMax M3 低階整理失敗，改用精簡重試：{first_error}")
    retry_prompt = _build_low_model_retry_prompt(
        payload,
        error=first_error,
        segment_label="single_payload",
        source_ids=source_ids,
    )
    retry_path = write_prompt_log(
        request,
        retry_prompt,
        model_name,
        False,
        sources[:80],
        {
            "purpose": purpose,
            "prompt_version": f"{LOW_MODEL_DIGEST_PROMPT_VERSION}_retry",
            "role": "fact_organizer_retry_only",
            "fingerprint": fingerprint,
            "low_model_mode": "single_retry",
            "prompt_chars": len(retry_prompt),
            "estimated_tokens": _estimate_tokens(len(retry_prompt)),
            "source_count": min(source_count, len(sources[:80]) or source_count),
            "original_prompt_path": str(prompt_path),
            "original_prompt_chars": prompt_chars,
        },
    )
    try:
        retry_result = minimax.generate_json(retry_prompt)
        parsed = _parse_json_object(retry_result.markdown)
        digest = _normalize_digest(parsed, model_name=model_name)
        digest["status"] = "success_after_retry"
        digest["prompt_path"] = str(prompt_path)
        digest["retry_prompt_path"] = str(retry_path)
        digest["diagnostics"] = retry_result.diagnostics
        digest["fingerprint"] = fingerprint
        digest["prompt_chars"] = prompt_chars
        digest["retry_prompt_chars"] = len(retry_prompt)
        digest["estimated_tokens"] = _estimate_tokens(prompt_chars) + _estimate_tokens(len(retry_prompt))
        digest["first_error"] = first_error
        digest["validation"] = validate_low_model_digest(digest)
        digest["artifact_paths"] = save_low_model_digest_artifacts(request, digest, fingerprint=fingerprint, purpose=purpose)
        _emit(progress, f"MiniMax M3 低階整理重試成功：facts={len(digest.get('facts') or [])} sources={len(digest.get('source_map') or [])}")
        return digest
    except Exception as retry_exc:
        if _is_low_model_quota_error(retry_exc):
            return None
        _emit(progress, f"MiniMax M3 低階整理重試失敗：{retry_exc}")
        return None


def _run_segmented_low_model_digest(
    request: CommandRequest,
    payload: dict[str, Any],
    *,
    sources: list[SourceItem],
    minimax: MiniMaxService,
    model_name: str,
    progress: ProgressCallback | None,
    purpose: str,
    fingerprint: str,
    max_sources: int,
    max_list: int,
    max_keys: int,
    max_string: int,
    depth: int,
    original_prompt_chars: int,
) -> dict[str, Any]:
    cooldown = _low_model_cooldown_status()
    if cooldown:
        digest = _low_model_cooldown_digest(
            request,
            model_name=model_name,
            fingerprint=fingerprint,
            purpose=purpose,
            cooldown=cooldown,
            sources=sources,
        )
        _emit(progress, f"MiniMax M3 額度冷卻中，略過分段低階整理：cooldown_until={digest.get('cooldown_until')}")
        return digest
    segments = _build_low_model_payload_segments(payload)
    if len(segments) > LOW_MODEL_EXECUTION_MAX_SEGMENTS:
        digest = {
            "schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION,
            "status": "skipped",
            "enabled": True,
            "model": model_name,
            "reason": "low_model_segment_count_exceeded",
            "fingerprint": fingerprint,
            "original_prompt_chars": original_prompt_chars,
            "prompt_chars": 0,
            "estimated_tokens": 0,
            "segment_count": len(segments),
            "segment_limit": LOW_MODEL_EXECUTION_MAX_SEGMENTS,
            "source_count": len(sources) or _payload_source_count(payload),
            "warnings": [
                "MiniMax M3 低階整理分段過多，已跳過低階整理；高階模型仍會使用本地保真核心資料包與完整來源索引。"
            ],
            "failed_segment_index": [
                {
                    "label": "low_model_segment_count_exceeded",
                    "status": "skipped",
                    "error": f"segment_count={len(segments)} exceeds limit={LOW_MODEL_EXECUTION_MAX_SEGMENTS}",
                    "source_ids": [],
                    "fallback_action": "use_local_fidelity_package_for_final_model",
                }
            ],
            "diagnostics": {
                "mode": "skipped_low_model_digest",
                "segment_count": len(segments),
                "segment_limit": LOW_MODEL_EXECUTION_MAX_SEGMENTS,
                "original_prompt_chars": original_prompt_chars,
                "fallback_action": "use_local_fidelity_package_for_final_model",
            },
        }
        digest["validation"] = validate_low_model_digest(digest)
        digest["artifact_paths"] = save_low_model_digest_artifacts(request, digest, fingerprint=fingerprint, purpose=purpose)
        _emit(
            progress,
            f"MiniMax M3 低階資料整理略過：segments={len(segments)} 超過上限 {LOW_MODEL_EXECUTION_MAX_SEGMENTS}，改由本地保真核心資料包供高階模型分析。",
        )
        return digest
    _emit(
        progress,
        f"MiniMax M3 分段資料整理開始：segments={len(segments)} original_prompt={original_prompt_chars} chars est_tokens={_estimate_tokens(original_prompt_chars)} sources={len(sources) or _payload_source_count(payload)}",
    )
    digests: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    prompt_paths: list[str] = []
    for index, segment in enumerate(segments, 1):
        segment_payload = segment["payload"]
        prompt = build_low_model_digest_prompt_from_payload(
            segment_payload,
            max_sources=max_sources,
            max_list=max_list,
            max_keys=max_keys,
            max_string=max_string,
            depth=depth,
        )
        if len(prompt) > LOW_MODEL_SEGMENT_HARD_LIMIT_CHARS:
            run = {
                "label": segment["label"],
                "status": "failed_oversized_segment",
                "prompt_chars": len(prompt),
                "estimated_tokens": _estimate_tokens(len(prompt)),
                "source_count": len(_sources_for_segment_payload(sources, segment_payload)),
                "prompt_path": "",
                "source_ids": [item.source_id for item in _sources_for_segment_payload(sources, segment_payload)],
                "error": "低階整理段落仍超過安全上限；未改用語意壓縮，保留給高階模型與 HTML/JSON 入模審計。",
                "fallback_action": "record_failed_segment_for_audit",
            }
            runs.append(run)
            _emit(progress, f"MiniMax M3 分段資料整理略過過大段落 {index}/{len(segments)}：{segment['label']} prompt={len(prompt)} chars，保留原始資料給高階模型")
            continue
        segment_sources = _sources_for_segment_payload(sources, segment_payload)
        prompt_path = write_prompt_log(
            request,
            prompt,
            model_name,
            False,
            segment_sources,
            {
                "purpose": purpose,
                "prompt_version": LOW_MODEL_DIGEST_PROMPT_VERSION,
                "role": "fact_organizer_only",
                "fingerprint": fingerprint,
                "low_model_mode": "segmented",
                "segment_label": segment["label"],
                "segment_index": index,
                "segment_total": len(segments),
                "prompt_chars": len(prompt),
                "estimated_tokens": _estimate_tokens(len(prompt)),
                "source_count": len(segment_sources),
                "original_prompt_chars": original_prompt_chars,
            },
        )
        prompt_paths.append(str(prompt_path))
        _emit(
            progress,
            f"MiniMax M3 分段資料整理 {index}/{len(segments)}：{segment['label']} prompt={len(prompt)} chars est_tokens={_estimate_tokens(len(prompt))} sources={len(segment_sources)}",
        )
        run = {
            "label": segment["label"],
            "status": "pending",
            "prompt_chars": len(prompt),
            "estimated_tokens": _estimate_tokens(len(prompt)),
            "source_count": len(segment_sources),
            "prompt_path": str(prompt_path),
            "source_ids": [item.source_id for item in segment_sources],
        }
        try:
            result = minimax.generate_json(prompt)
            parsed = _parse_json_object(result.markdown)
            digest = _normalize_digest(parsed, model_name=model_name)
            digest["prompt_path"] = str(prompt_path)
            digest["diagnostics"] = result.diagnostics
            digests.append(digest)
            run["status"] = "success"
            run["output_facts"] = len(digest.get("facts") or [])
            run["output_sources"] = len(digest.get("source_map") or [])
            run["usage"] = _extract_usage(result.diagnostics)
            _emit(progress, f"MiniMax M3 分段資料整理完成 {index}/{len(segments)}：facts={run['output_facts']} sources={run['output_sources']}")
        except Exception as exc:
            if _is_low_model_quota_error(exc):
                cooldown_payload = _write_low_model_cooldown(exc, model_name=model_name)
                run["status"] = "failed_quota_cooldown"
                run["error"] = str(exc)
                run["cooldown_until"] = cooldown_payload.get("cooldown_until")
                run["fallback_action"] = "skip_low_model_until_cooldown_expires"
                _emit(progress, f"MiniMax M3 額度或速率限制，停止後續低階分段：cooldown_until={run.get('cooldown_until')}")
                runs.append(run)
                break
            _retry_low_model_segment(
                request,
                minimax,
                model_name=model_name,
                purpose=purpose,
                fingerprint=fingerprint,
                segment_payload=segment_payload,
                segment_sources=segment_sources,
                segment_label=str(segment["label"]),
                segment_index=index,
                segment_total=len(segments),
                original_prompt_chars=original_prompt_chars,
                first_error=str(exc),
                digests=digests,
                prompt_paths=prompt_paths,
                run=run,
                progress=progress,
            )
        runs.append(run)

    merged = _merge_low_model_digests(digests, model_name=model_name)
    success_count = sum(1 for item in runs if item.get("status") in {"success", "success_after_retry"})
    failed_runs = [item for item in runs if str(item.get("status") or "").startswith("failed")]
    merged["status"] = "success" if success_count == len(runs) else "partial_success" if success_count else "failed"
    merged["enabled"] = True
    merged["fingerprint"] = fingerprint
    merged["prompt_paths"] = prompt_paths
    merged["segment_runs"] = runs
    merged["prompt_chars"] = sum(int(item.get("prompt_chars") or 0) for item in runs)
    merged["estimated_tokens"] = sum(int(item.get("estimated_tokens") or 0) for item in runs)
    merged["estimated_tokens"] += sum(int(item.get("retry_estimated_tokens") or 0) for item in runs)
    merged["original_prompt_chars"] = original_prompt_chars
    merged["failed_segment_index"] = [
        {
            "label": item.get("label"),
            "status": item.get("status"),
            "error": item.get("error") or item.get("first_error"),
            "source_ids": item.get("source_ids") or [],
            "fallback_action": item.get("fallback_action") or "none",
            "retry_prompt_path": item.get("retry_prompt_path"),
        }
        for item in failed_runs
    ]
    merged["diagnostics"] = {
        "mode": "segmented_low_model_digest",
        "segment_count": len(runs),
        "success_count": success_count,
        "failed_count": len(failed_runs),
        "retry_count": sum(1 for item in runs if item.get("retry_prompt_path")),
        "usage_summary": _sum_usage([item.get("usage") for item in runs if item.get("usage")]),
    }
    if failed_runs:
        merged.setdefault("warnings", []).append(
            "部分 MiniMax M3 分段整理失敗；失敗段來源已列入 failed_segment_index，報告會保留診斷並由高階模型搭配本地資料中心判斷。"
        )
    merged["validation"] = validate_low_model_digest(merged)
    merged["artifact_paths"] = save_low_model_digest_artifacts(request, merged, fingerprint=fingerprint, purpose=purpose)
    _emit(
        progress,
        f"MiniMax M3 分段資料整理結束：success={success_count}/{len(runs)} failed={len(failed_runs)} facts={len(merged.get('facts') or [])} sources={len(merged.get('source_map') or [])}",
    )
    return merged


def _retry_low_model_segment(
    request: CommandRequest,
    minimax: MiniMaxService,
    *,
    model_name: str,
    purpose: str,
    fingerprint: str,
    segment_payload: dict[str, Any],
    segment_sources: list[SourceItem],
    segment_label: str,
    segment_index: int,
    segment_total: int,
    original_prompt_chars: int,
    first_error: str,
    digests: list[dict[str, Any]],
    prompt_paths: list[str],
    run: dict[str, Any],
    progress: ProgressCallback | None,
) -> None:
    run["first_error"] = first_error
    _emit(progress, f"MiniMax M3 分段資料整理失敗 {segment_index}/{segment_total}，改用精簡重試：{first_error}")
    retry_prompt = _build_low_model_retry_prompt(
        segment_payload,
        error=first_error,
        segment_label=segment_label,
        source_ids=run.get("source_ids") or [],
    )
    retry_path = write_prompt_log(
        request,
        retry_prompt,
        model_name,
        False,
        segment_sources,
        {
            "purpose": purpose,
            "prompt_version": f"{LOW_MODEL_DIGEST_PROMPT_VERSION}_retry",
            "role": "fact_organizer_retry_only",
            "fingerprint": fingerprint,
            "low_model_mode": "segmented_retry",
            "segment_label": segment_label,
            "segment_index": segment_index,
            "segment_total": segment_total,
            "prompt_chars": len(retry_prompt),
            "estimated_tokens": _estimate_tokens(len(retry_prompt)),
            "source_count": len(segment_sources),
            "original_prompt_chars": original_prompt_chars,
        },
    )
    prompt_paths.append(str(retry_path))
    run["retry_prompt_path"] = str(retry_path)
    run["retry_prompt_chars"] = len(retry_prompt)
    run["retry_estimated_tokens"] = _estimate_tokens(len(retry_prompt))
    try:
        retry_result = minimax.generate_json(retry_prompt)
        parsed = _parse_json_object(retry_result.markdown)
        digest = _normalize_digest(parsed, model_name=model_name)
        digest["prompt_path"] = str(retry_path)
        digest["diagnostics"] = retry_result.diagnostics
        digests.append(digest)
        run["status"] = "success_after_retry"
        run["output_facts"] = len(digest.get("facts") or [])
        run["output_sources"] = len(digest.get("source_map") or [])
        run["usage"] = _extract_usage(retry_result.diagnostics)
        _emit(progress, f"MiniMax M3 分段資料整理重試成功 {segment_index}/{segment_total}：facts={run['output_facts']} sources={run['output_sources']}")
    except Exception as retry_exc:
        cooldown_payload = _write_low_model_cooldown(retry_exc, model_name=model_name) if _is_low_model_quota_error(retry_exc) else None
        run["status"] = "failed_after_retry"
        run["error"] = str(retry_exc)
        run["fallback_action"] = "record_failed_segment_for_audit"
        if cooldown_payload:
            run["status"] = "failed_quota_cooldown"
            run["cooldown_until"] = cooldown_payload.get("cooldown_until")
            run["fallback_action"] = "skip_low_model_until_cooldown_expires"
        _emit(progress, f"MiniMax M3 分段資料整理重試失敗 {segment_index}/{segment_total}：{retry_exc}")


def _build_low_model_payload_segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    header_keys = {"command", "mode", "target", "report_date", "analysis_date", "stock_id", "stock_name"}
    header = {key: payload.get(key) for key in header_keys if key in payload}
    segments: list[dict[str, Any]] = []
    for key, value in payload.items():
        if key in header_keys or value in (None, "", [], {}):
            continue
        for chunk_index, chunk in enumerate(_chunk_payload_value(value), 1):
            segment_payload = dict(header)
            segment_payload[key] = chunk
            segments.append({"label": f"{key}_{chunk_index}", "payload": segment_payload})
    if not segments:
        segments.append({"label": "payload_1", "payload": payload})
    if len(segments) > LOW_MODEL_MAX_SEGMENTS:
        for segment in segments:
            segment["payload"] = {
                **header,
                "low_model_large_batch_notice": {
                    "reason": "segment_count_exceeded_previous_safety_limit",
                    "segment_total": len(segments),
                    "previous_limit": LOW_MODEL_MAX_SEGMENTS,
                    "policy": "低階模型額度較充足，仍保留所有完整分段；不得因段數過多刪除核心資料。",
                },
                **segment["payload"],
            }
    return segments


def _chunk_payload_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return _chunk_sequence_by_size(value, target_chars=LOW_MODEL_SEGMENT_TARGET_CHARS)
    if isinstance(value, dict):
        items = list(value.items())
        chunks: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        current_chars = 0
        for key, item_value in items:
            row = {key: item_value}
            row_chars = _json_size(row)
            if row_chars > LOW_MODEL_SEGMENT_TARGET_CHARS:
                if current:
                    chunks.append(current)
                    current = {}
                    current_chars = 0
                for part_index, part in enumerate(_split_large_value(item_value, target_chars=LOW_MODEL_SEGMENT_TARGET_CHARS), 1):
                    chunks.append({f"{key}__part_{part_index}": part})
                continue
            if current and current_chars + row_chars > LOW_MODEL_SEGMENT_TARGET_CHARS:
                chunks.append(current)
                current = {}
                current_chars = 0
            current[str(key)] = item_value
            current_chars += row_chars
        if current:
            chunks.append(current)
        return chunks or [{}]
    return [value]


def _chunk_sequence_by_size(items: list[Any], *, target_chars: int) -> list[list[Any]]:
    chunks: list[list[Any]] = []
    current: list[Any] = []
    current_chars = 0
    for item in items:
        row_chars = _json_size(item)
        if row_chars > target_chars:
            if current:
                chunks.append(current)
                current = []
                current_chars = 0
            for part in _split_large_value(item, target_chars=target_chars):
                chunks.append([part])
            continue
        if current and current_chars + row_chars > target_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += row_chars
    if current:
        chunks.append(current)
    return chunks or [[]]


def _split_large_value(value: Any, *, target_chars: int) -> list[Any]:
    if isinstance(value, str):
        return [
            {"資料型態": "完整文字分段", "段號": index + 1, "文字": value[start:start + target_chars]}
            for index, start in enumerate(range(0, len(value), target_chars))
        ]
    if isinstance(value, dict):
        return _chunk_payload_value(value)
    if isinstance(value, list):
        return _chunk_sequence_by_size(value, target_chars=target_chars)
    return [value]


def _build_low_model_retry_prompt(
    payload: dict[str, Any],
    *,
    error: str,
    segment_label: str,
    source_ids: list[str],
) -> str:
    compact_payload = _prepare_low_model_payload_for_prompt(
        payload,
        max_sources=18,
        max_list=22,
        max_keys=38,
        max_string=360,
        depth=3,
    )
    template = _read_workflow_prompt("low_model_digest_retry.md")
    return template.format(
        schema_version=LOW_MODEL_DIGEST_SCHEMA_VERSION,
        segment_label=segment_label,
        source_ids_json=json.dumps(source_ids, ensure_ascii=False),
        error=_truncate_text(error, 600),
        compact_payload_json=json.dumps(compact_payload, ensure_ascii=False, indent=2, default=str),
    )


def _merge_low_model_digests(digests: list[dict[str, Any]], *, model_name: str) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION,
        "status": "success",
        "enabled": True,
        "model": model_name,
        "model_role": "資料整理底稿",
    }
    for key in ("facts", "events", "risk_evidence", "counter_evidence", "missing_data", "source_map", "warnings"):
        merged[key] = _dedupe_list([row for digest in digests for row in (digest.get(key) or [])])
    return merged


def _dedupe_list(rows: list[Any], *, limit: int = 700) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for row in rows:
        marker = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(row)
        if len(result) >= limit:
            break
    return result


def _sources_for_segment_payload(sources: list[SourceItem], payload: dict[str, Any]) -> list[SourceItem]:
    if not sources:
        return []
    text = json.dumps(payload, ensure_ascii=False, default=str)
    ids = set(re.findall(r"S\d{3,}", text))
    if ids:
        matched = [item for item in sources if item.source_id in ids]
        if matched:
            return matched[:80]
    return sources[: min(len(sources), 30)]


def _payload_source_count(payload: dict[str, Any]) -> int:
    sources = payload.get("sources")
    if isinstance(sources, list):
        return len(sources)
    return 0


def _estimate_tokens(chars: int) -> int:
    return max(1, int(chars / TOKEN_ESTIMATE_DIVISOR))


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _extract_usage(diagnostics: Any) -> dict[str, int]:
    usage: dict[str, int] = {}
    if not isinstance(diagnostics, dict):
        return usage
    candidates = [diagnostics.get("usage"), diagnostics.get("token_usage"), diagnostics]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"):
            value = candidate.get(key)
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + int(value)
    return usage


def _sum_usage(items: list[dict[str, int] | None]) -> dict[str, int]:
    total: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total


def _stable_fingerprint_payload(payload: dict[str, Any]) -> Any:
    return _drop_volatile_fields(payload)


def _drop_volatile_fields(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if key_lower in VOLATILE_FINGERPRINT_KEYS:
                continue
            if key_lower.endswith("_at") and any(token in key_lower for token in ("created", "updated", "generated", "completed")):
                continue
            result[key_text] = _drop_volatile_fields(item)
        return result
    if isinstance(value, list):
        normalized = [_drop_volatile_fields(item) for item in value]
        if all(isinstance(item, dict) for item in normalized):
            return sorted(normalized, key=_fingerprint_sort_key)
        return normalized
    return value


def _fingerprint_sort_key(value: Any) -> str:
    if isinstance(value, dict):
        preferred = [
            "source_id",
            "id",
            "url",
            "title",
            "path",
            "text",
            "snippet",
            "summary",
            "fact",
            "event",
            "code",
            "name",
        ]
        parts = [str(value.get(key) or "") for key in preferred]
        if any(parts):
            return "|".join(parts)
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return str(value)
    return str(value)
