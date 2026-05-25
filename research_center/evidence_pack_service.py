from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import CommandRequest

EVIDENCE_PACK_SCHEMA_VERSION = "evidence_pack_v1"


def attach_unified_evidence_pack(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    structured_data["unified_evidence_pack"] = build_unified_evidence_pack(request, structured_data)
    return structured_data


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


def _compact(value: Any, *, depth: int = 4, max_list: int = 20, max_keys: int = 60) -> Any:
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
            output[str(key)] = _compact(item, depth=depth - 1, max_list=max_list, max_keys=max_keys)
        return output
    if isinstance(value, (list, tuple)):
        result = [_compact(item, depth=depth - 1, max_list=max_list, max_keys=max_keys) for item in list(value)[:max_list]]
        if len(value) > max_list:
            result.append({"_truncated_items": len(value) - max_list})
        return result
    return value
