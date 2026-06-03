from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .evidence_pack_service import build_ai_compact_context
from .minimax_service import MiniMaxService
from .models import CommandRequest, SourceItem
from .prompt_logging import write_prompt_log

LOW_MODEL_DIGEST_SCHEMA_VERSION = "low_model_digest_v1"
LOW_MODEL_DIGEST_PROMPT_VERSION = "low_model_digest_prompt_v1"
HIGH_MODEL_INPUT_SCHEMA_VERSION = "high_model_input_package_v1"
DEFAULT_LOW_MODEL_NAME = "MiniMax-M2.7"
PROMPT_WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "prompt" / "workflow"
LOW_MODEL_ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "logs" / "ai_low_model"
HIGH_MODEL_BALANCED_THRESHOLD_CHARS = 180_000
HIGH_MODEL_COMPACT_THRESHOLD_CHARS = 320_000
LOW_MODEL_PROMPT_SOFT_LIMIT_CHARS = 320_000
LOW_MODEL_SEGMENT_TARGET_CHARS = 220_000
LOW_MODEL_SEGMENT_HARD_LIMIT_CHARS = 420_000
LOW_MODEL_MAX_SEGMENTS = 80
TOKEN_ESTIMATE_DIVISOR = 4
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
}

ProgressCallback = Callable[[str], None]


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
        "sources": [asdict(item) for item in sources[:250]],
    }
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
    """Run MiniMax M2.7 as a fact organizer before final analysis."""

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
        _emit(progress, f"MiniMax M2.7 資料整理使用快取：facts={len(cached.get('facts') or [])} sources={len(cached.get('source_map') or [])}")
        return cached

    _emit(progress, f"MiniMax M2.7 資料整理開始：model={model_name}")
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
        _emit(progress, f"MiniMax M2.7 資料整理完成：facts={len(digest.get('facts') or [])} sources={len(digest.get('source_map') or [])}")
        if (digest.get("artifact_paths") or {}).get("json_path"):
            _emit(progress, f"低階資料包已保存：{(digest.get('artifact_paths') or {}).get('json_path')}")
        return digest
    except Exception as exc:
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
        _emit(progress, f"MiniMax M2.7 資料整理失敗，改用本地資料中心繼續：{exc}")
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
        "sources": [asdict(item) for item in sources[:250]],
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
    compact_payload = build_ai_compact_context(
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
    low_digest = structured_data.get("low_model_digest") or {}
    low_validation = validate_low_model_digest(low_digest)
    selected_sources = _selected_sources_from_data(structured_data, sources)
    excerpt_limit = 28 if input_mode == "balanced" else 18 if input_mode == "compact" else 45
    command_slice = _command_specific_slice(request, structured_data, input_mode=input_mode)
    package = {
        "schema_version": HIGH_MODEL_INPUT_SCHEMA_VERSION,
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "report_date": request.report_date.isoformat() if request.report_date else None,
        "input_mode": input_mode,
        "prompt_chars_estimate_before_package": prompt_chars_estimate,
        "workflow_policy": {
            "目的": "在不犧牲報告品質的前提下，讓高階模型優先使用可信證據包與必要原文摘錄，避免反覆吃完整原始資料。",
            "完整資料保留": "完整原始資料、完整來源、來源快照與結構化資料仍保存在報告 JSON、sources JSON 與本地快取。",
            "低階模型限制": "MiniMax M2.7 只做資料整理、去重、摘要、來源對照、缺口與反證標記，不做最終評分或買賣建議。",
            "高階模型責任": "高階模型必須重新判斷、重新評分、檢查反證與資料缺口，不得直接照抄低階資料包。",
        },
        "token_budget_policy": {
            "balanced_threshold_chars": HIGH_MODEL_BALANCED_THRESHOLD_CHARS,
            "compact_threshold_chars": HIGH_MODEL_COMPACT_THRESHOLD_CHARS,
            "quality_first": True,
            "compression_method": "保留完整資料於本地；高階模型入模使用證據包、可信度、反證、缺口與必要原文摘錄。",
        },
        "ai_data_center": structured_data.get("ai_data_center"),
        "ai_prompt_context": structured_data.get("ai_prompt_context"),
        "ai_input_audit": structured_data.get("ai_input_audit"),
        "report_confidence": structured_data.get("report_confidence"),
        "data_gap_summary": structured_data.get("data_gap_summary"),
        "unified_evidence_pack": _compact_value(structured_data.get("unified_evidence_pack"), input_mode=input_mode),
        "low_model_digest": _compact_low_model_digest(low_digest, input_mode=input_mode),
        "low_model_validation": low_validation,
        "local_scoring": _compact_value(structured_data.get("local_scoring"), input_mode=input_mode),
        "command_specific_data": command_slice,
        "selected_sources": selected_sources[:excerpt_limit],
        "required_original_excerpts": _source_excerpts(sources, limit=excerpt_limit),
        "full_data_locations": {
            "report_json": "reports/.../*.json",
            "sources_json": "reports/.../*.sources.json",
            "low_model_artifacts": (low_digest.get("artifact_paths") or {}),
            "source_count": len(sources),
            "selected_source_count": len(selected_sources),
        },
    }
    return package


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
    return [{"source": asdict(item), "reasons": ["來源清單保留"], "status": "入模"} for item in sources[:60]]


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


def _command_specific_slice(request: CommandRequest, structured_data: dict[str, Any], *, input_mode: str) -> dict[str, Any]:
    if request.command == "research":
        keys = [
            "stock", "price_data", "technical_data", "institutional_data", "margin_data",
            "revenue_data", "financial_data", "valuation_data", "tdcc_data", "source_events",
            "company_knowledge", "local_rerating_snapshot", "topic_context",
        ]
    elif request.command == "value_scan":
        keys = [
            "candidate_pool", "report_date", "total_candidate_count", "ai_candidate_limit",
            "ai_candidate_evidence_pack", "ai_candidates", "scoring_rules", "topic_context",
        ]
    elif request.command == "macro":
        keys = [
            "market_scope", "region_scope", "quantitative_market", "volatility",
            "industry_flow", "fear_greed", "market_score", "free_public_sources",
        ]
    elif request.command in {"theme", "theme_flow", "theme_radar", "sector_strength"}:
        keys = [
            "theme", "theme_scope", "theme_query", "supply_chain_profile",
            "company_knowledge_summary", "theme_quality_context", "matched_universe",
            "matched_companies", "theme_rankings", "sector_rankings", "subsector_rankings",
            "theme_flow_summaries", "strong_stocks", "topic_context",
        ]
    else:
        keys = ["news_context", "feature_pack", "data_coverage", "local_scoring"]
    return _compact_value({key: structured_data.get(key) for key in keys if key in structured_data}, input_mode=input_mode)


def _compact_value(value: Any, *, input_mode: str) -> Any:
    if input_mode == "compact":
        return build_ai_compact_context(value, max_sources=40, max_list=35, max_keys=70, max_string=700, depth=4)
    if input_mode == "balanced":
        return build_ai_compact_context(value, max_sources=70, max_list=60, max_keys=100, max_string=900, depth=5)
    return build_ai_compact_context(value, max_sources=100, max_list=90, max_keys=140, max_string=1100, depth=6)


def _compact_low_model_digest(digest: dict[str, Any], *, input_mode: str) -> dict[str, Any]:
    if not isinstance(digest, dict):
        return {}
    limits = {
        "compact": {"facts": 40, "events": 25, "risk_evidence": 25, "counter_evidence": 25, "missing_data": 25, "source_map": 45},
        "balanced": {"facts": 70, "events": 40, "risk_evidence": 40, "counter_evidence": 40, "missing_data": 35, "source_map": 70},
        "full": {"facts": 90, "events": 60, "risk_evidence": 60, "counter_evidence": 60, "missing_data": 50, "source_map": 90},
    }[input_mode]
    result = {key: digest.get(key) for key in ("schema_version", "status", "model", "model_role", "prompt_path", "artifact_paths", "validation", "cache_hit")}
    for key, limit in limits.items():
        result[key] = (digest.get(key) or [])[:limit]
    return result


def _digest_to_markdown(digest: dict[str, Any]) -> str:
    lines = [
        "# MiniMax M2.7 資料整理底稿",
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
        _emit(progress, f"MiniMax M2.7 低階整理快取命中：facts={len(cached.get('facts') or [])} sources={len(cached.get('source_map') or [])}")
        return cached

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
        f"MiniMax M2.7 低階整理開始：model={model_name} prompt={prompt_chars} chars est_tokens={_estimate_tokens(prompt_chars)} sources={source_count}",
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
        _emit(progress, f"MiniMax M2.7 低階整理完成：facts={len(digest.get('facts') or [])} sources={len(digest.get('source_map') or [])}")
        return digest
    except Exception as exc:
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
        digest["validation"] = validate_low_model_digest(digest)
        digest["artifact_paths"] = save_low_model_digest_artifacts(request, digest, fingerprint=fingerprint, purpose=purpose)
        _emit(progress, f"MiniMax M2.7 低階整理失敗，改用本地資料中心繼續：{exc}")
        return digest


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
    segments = _build_low_model_payload_segments(payload)
    _emit(
        progress,
        f"MiniMax M2.7 低階整理啟用分段：segments={len(segments)} original_prompt={original_prompt_chars} chars est_tokens={_estimate_tokens(original_prompt_chars)} sources={len(sources) or _payload_source_count(payload)}",
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
            prompt = build_low_model_digest_prompt_from_payload(
                _force_compact_segment_payload(segment_payload),
                max_sources=30,
                max_list=35,
                max_keys=60,
                max_string=500,
                depth=4,
            )
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
            f"MiniMax M2.7 低階整理分段 {index}/{len(segments)}：{segment['label']} prompt={len(prompt)} chars est_tokens={_estimate_tokens(len(prompt))} sources={len(segment_sources)}",
        )
        run = {
            "label": segment["label"],
            "status": "pending",
            "prompt_chars": len(prompt),
            "estimated_tokens": _estimate_tokens(len(prompt)),
            "source_count": len(segment_sources),
            "prompt_path": str(prompt_path),
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
            _emit(progress, f"MiniMax M2.7 低階整理分段完成 {index}/{len(segments)}：facts={run['output_facts']} sources={run['output_sources']}")
        except Exception as exc:
            run["status"] = "failed"
            run["error"] = str(exc)
            _emit(progress, f"MiniMax M2.7 低階整理分段失敗 {index}/{len(segments)}：{exc}")
        runs.append(run)

    merged = _merge_low_model_digests(digests, model_name=model_name)
    success_count = sum(1 for item in runs if item.get("status") == "success")
    merged["status"] = "success" if success_count == len(runs) else "partial_success" if success_count else "failed"
    merged["enabled"] = True
    merged["fingerprint"] = fingerprint
    merged["prompt_paths"] = prompt_paths
    merged["segment_runs"] = runs
    merged["prompt_chars"] = sum(int(item.get("prompt_chars") or 0) for item in runs)
    merged["estimated_tokens"] = sum(int(item.get("estimated_tokens") or 0) for item in runs)
    merged["original_prompt_chars"] = original_prompt_chars
    merged["diagnostics"] = {
        "mode": "segmented_low_model_digest",
        "segment_count": len(runs),
        "success_count": success_count,
        "failed_count": len(runs) - success_count,
        "usage_summary": _sum_usage([item.get("usage") for item in runs if item.get("usage")]),
    }
    if success_count < len(runs):
        merged.setdefault("warnings", []).append("部分 MiniMax M2.7 低階整理分段失敗；高階模型仍會使用成功分段與本地資料中心。")
    merged["validation"] = validate_low_model_digest(merged)
    merged["artifact_paths"] = save_low_model_digest_artifacts(request, merged, fingerprint=fingerprint, purpose=purpose)
    _emit(
        progress,
        f"MiniMax M2.7 低階整理分段彙整完成：success={success_count}/{len(runs)} facts={len(merged.get('facts') or [])} sources={len(merged.get('source_map') or [])}",
    )
    return merged


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
        segments = segments[:LOW_MODEL_MAX_SEGMENTS]
        segments[-1]["payload"] = {
            **header,
            "low_model_truncation_notice": {
                "reason": "segment_count_exceeded_safety_limit",
                "max_segments": LOW_MODEL_MAX_SEGMENTS,
                "policy": "低階模型以完整分段為主；只有超過安全段數才截斷，完整原始資料仍保存在本地 JSON。",
            },
            **segments[-1]["payload"],
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
        if current and current_chars + row_chars > target_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += row_chars
    if current:
        chunks.append(current)
    return chunks or [[]]


def _force_compact_segment_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return build_ai_compact_context(
        payload,
        max_sources=25,
        max_list=25,
        max_keys=45,
        max_string=380,
        depth=3,
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
    sanitized = _drop_volatile_fields(payload)
    return build_ai_compact_context(
        sanitized,
        max_sources=120,
        max_list=100,
        max_keys=140,
        max_string=1000,
        depth=6,
    )


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
        return [_drop_volatile_fields(item) for item in value]
    return value
