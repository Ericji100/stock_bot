from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import CommandRequest

EVIDENCE_PACK_SCHEMA_VERSION = "evidence_pack_v1"
THREE_LAYER_CONTEXT_SCHEMA_VERSION = "three_layer_context_v1"
AI_COMPACT_CONTEXT_SCHEMA_VERSION = "ai_compact_context_v1"


def attach_unified_evidence_pack(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    structured_data["unified_evidence_pack"] = build_unified_evidence_pack(request, structured_data)
    return structured_data


def build_three_layer_evidence_context(
    *,
    raw_sources: list[dict[str, Any]] | None = None,
    evidence_pack: dict[str, Any] | None = None,
    final_context: dict[str, Any] | None = None,
    min_source_count: int = 0,
) -> dict[str, Any]:
    """Build a standard raw/evidence/final context object.

    The raw layer intentionally keeps full source records. The final layer is
    the AI-facing compact context and must not be treated as the only copy of
    the evidence.
    """

    sources = list(raw_sources or [])
    pack = dict(evidence_pack or {})
    context = dict(final_context or {})
    provider_counts: dict[str, int] = {}
    level_counts: dict[str, int] = {}
    for source in sources:
        provider = str(source.get("provider") or source.get("fetch_provider") or "unknown")
        level = str(source.get("source_level") or "unknown")
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        level_counts[level] = level_counts.get(level, 0) + 1
    sufficiency = {
        "source_count": len(sources),
        "min_source_count": int(min_source_count or 0),
        "sufficient": len(sources) >= int(min_source_count or 0),
        "provider_counts": provider_counts,
        "source_level_counts": level_counts,
    }
    context.setdefault("source_sufficiency", sufficiency)
    return {
        "schema_version": THREE_LAYER_CONTEXT_SCHEMA_VERSION,
        "raw_sources": sources,
        "evidence_pack": pack,
        "final_context": context,
        "source_sufficiency": sufficiency,
    }


def build_ai_compact_context(
    value: Any,
    *,
    max_sources: int = 10,
    max_list: int = 12,
    max_keys: int = 50,
    max_string: int = 300,
    depth: int = 4,
) -> dict[str, Any]:
    """Build an AI-facing compact context while preserving full data elsewhere.

    This helper is intentionally generic. Commands should keep their complete
    evidence pack in artifacts/cache, and send this compact version to models
    when prompt size or latency matters.
    """

    source_limited = _limit_source_lists(value, max_sources=max_sources, max_string=max_string)
    compact = _compact(
        source_limited,
        depth=depth,
        max_list=max_list,
        max_keys=max_keys,
        max_string=max_string,
    )
    return {
        "schema_version": AI_COMPACT_CONTEXT_SCHEMA_VERSION,
        "policy": "AI-facing compact context only; full evidence remains in local artifacts.",
        "limits": {
            "max_sources": max_sources,
            "max_list": max_list,
            "max_keys": max_keys,
            "max_string": max_string,
            "depth": depth,
        },
        "payload": compact,
    }


def build_unified_evidence_pack(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    _append_if_present(items, "feature_pack", structured_data.get("feature_pack"), confidence="medium")
    _append_if_present(items, "data_gap_summary", structured_data.get("data_gap_summary"), confidence="medium")
    _append_if_present(items, "report_quality", structured_data.get("report_quality"), confidence="medium")
    _append_if_present(items, "local_scoring", structured_data.get("local_scoring"), confidence="medium")
    _append_if_present(items, "rerating_snapshot", structured_data.get("local_rerating_snapshot"), confidence="medium")
    _append_if_present(items, "news_events", structured_data.get("news_events"), confidence="medium")
    _append_if_present(items, "news_context", structured_data.get("news_context"), confidence="medium")
    _append_if_present(items, "chip", _chip_summary(structured_data), confidence="medium")
    _append_if_present(items, "technical", structured_data.get("technical_data") or structured_data.get("price_data"), confidence="medium")
    if request.command == "value_scan":
        _append_if_present(items, "ai_candidate_evidence_pack", structured_data.get("ai_candidate_evidence_pack"), confidence="medium")
    return {
        "schema_version": EVIDENCE_PACK_SCHEMA_VERSION,
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "item_count": len(items),
        "items": items,
    }


def _append_if_present(items: list[dict[str, Any]], kind: str, payload: Any, *, confidence: str) -> None:
    if not _has_value(payload):
        return
    item = {
        "type": kind,
        "confidence": confidence,
        "used_by_ai": True,
        "summary": _summary_for(kind, payload),
        "payload": _compact(payload),
    }
    if kind == "data_gap_summary":
        item["missing_data"] = list((payload or {}).get("missing_fields") or [])
    items.append(item)


def _chip_summary(data: dict[str, Any]) -> Any:
    chip = data.get("chip_backup_data") or {}
    if isinstance(chip, dict):
        return chip.get("summary") or chip
    return chip


def _summary_for(kind: str, payload: Any) -> str:
    if isinstance(payload, dict):
        if payload.get("schema_version"):
            return f"{kind}:{payload.get('schema_version')}"
        if payload.get("status"):
            return f"{kind}:status={payload.get('status')}"
    if isinstance(payload, list):
        return f"{kind}:items={len(payload)}"
    return kind


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        status = str(value.get("status") or "").lower()
        if status in {"missing", "unavailable", "insufficient"}:
            return False
        return bool(value)
    if isinstance(value, (list, tuple, set, str)):
        return bool(value)
    return True


def _compact(
    value: Any,
    *,
    depth: int = 4,
    max_list: int = 20,
    max_keys: int = 60,
    max_string: int | None = None,
) -> Any:
    if depth <= 0:
        if isinstance(value, (dict, list, tuple)):
            return f"<{type(value).__name__} truncated>"
        return value
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_keys:
                output["_truncated_keys"] = len(value) - max_keys
                break
            output[str(key)] = _compact(item, depth=depth - 1, max_list=max_list, max_keys=max_keys, max_string=max_string)
        return output
    if isinstance(value, (list, tuple)):
        result = [
            _compact(item, depth=depth - 1, max_list=max_list, max_keys=max_keys, max_string=max_string)
            for item in list(value)[:max_list]
        ]
        if len(value) > max_list:
            result.append({"_truncated_items": len(value) - max_list})
        return result
    if isinstance(value, str) and max_string is not None and len(value) > max_string:
        return value[:max_string].rstrip() + "...<truncated>"
    return value


def _limit_source_lists(value: Any, *, max_sources: int, max_string: int) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if _looks_like_source_list_key(str(key)) and isinstance(item, list):
                output[key] = [_compact_source_for_ai(row, max_string=max_string) for row in item[:max_sources]]
                if len(item) > max_sources:
                    output[f"{key}_truncated_count"] = len(item) - max_sources
            else:
                output[key] = _limit_source_lists(item, max_sources=max_sources, max_string=max_string)
        return output
    if isinstance(value, list):
        return [_limit_source_lists(item, max_sources=max_sources, max_string=max_string) for item in value]
    return value


def _looks_like_source_list_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in {"sources", "web_sources", "ai_sources", "research_sources", "raw_sources", "external_sources"}


def _compact_source_for_ai(source: Any, *, max_string: int) -> Any:
    if not isinstance(source, dict):
        return _compact(source, max_string=max_string)
    return {
        "title": _compact(source.get("title"), max_string=max_string),
        "url": source.get("url"),
        "published_date": source.get("published_date"),
        "provider": source.get("provider") or source.get("fetch_provider"),
        "source_level": source.get("source_level"),
        "snippet": _compact(source.get("snippet") or source.get("summary") or source.get("content"), max_string=max_string),
    }
