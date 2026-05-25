"""Import external AI topic-library JSON into a topic change pack."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .topic_maintain_service import (
    _normalize_company_knowledge_updates,
    _parse_ai_json_response,
    _validate_initial_change_pack_quality,
)
from .topic_models import (
    TopicActionType,
    TopicChangeAction,
    TopicChangeMode,
    TopicChangePack,
    TopicChangeStatus,
    TopicConfidence,
    TopicEvidence,
)
from .topic_repository import raw_response_path, save_change_pack
from .topic_quality import normalize_change_pack_quality


class TopicImportError(ValueError):
    """Raised when pasted external AI data cannot be imported."""


def import_topic_change_pack(raw_payload: str, model: str = "external", user_id: str | None = None) -> TopicChangePack:
    """Normalize external AI JSON into a pending topic change pack."""
    payload = (raw_payload or "").strip()
    if not payload:
        raise TopicImportError("匯入內容是空的，請貼上外部 AI 產生的 JSON")

    try:
        data = _parse_ai_json_response(payload)
    except Exception as exc:
        raise TopicImportError("匯入失敗：內容不是有效 JSON，請確認只貼上 JSON object") from exc

    if not isinstance(data, dict) and not isinstance(data, list):
        raise TopicImportError("匯入失敗：JSON 必須是 object 或 topic list")

    change_id = f"change_{datetime.now().strftime('%Y%m%d_%H%M%S')}_import"
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    raw_path = raw_response_path(change_id)
    raw_path.write_text(payload, encoding="utf-8")

    if _looks_like_supply_chain_nodes_payload(data):
        actions = _actions_from_supply_chain_nodes(_extract_supply_chain_nodes(data))
    else:
        source_items = _extract_action_items(data)
        actions = [_normalize_action(item, index) for index, item in enumerate(source_items, start=1)]
    inferred_mode = _infer_import_mode(data, actions)
    warnings = _as_str_list(data.get("warnings", [])) if isinstance(data, dict) else []
    if inferred_mode == TopicChangeMode.UPDATE and isinstance(data, dict) and str(data.get("mode") or "").lower() == "initial":
        warnings = _remove_initial_only_warnings(warnings)
        warnings.append("匯入內容以 update_theme 為主，系統已自動改為 update 模式。")
    sources = _as_dict_list(data.get("sources", [])) if isinstance(data, dict) else []
    pack = TopicChangePack(
        change_id=change_id,
        parent_change_id=None,
        mode=inferred_mode,
        status=TopicChangeStatus.PENDING,
        model=model or "external",
        created_at=now,
        updated_at=now,
        summary=str(data.get("summary") or "外部 AI 匯入題材庫") if isinstance(data, dict) else "外部 AI 匯入題材庫",
        confidence=str(data.get("confidence") or "medium") if isinstance(data, dict) else "medium",
        actions=actions,
        warnings=warnings,
        sources=sources,
        company_knowledge_updates=(
            _normalize_company_knowledge_updates(data.get("company_knowledge_updates", {}))
            if isinstance(data, dict)
            else {}
        ),
        raw_response_path=str(raw_path),
        prompt_log_path="",
        extra={"import_source": "external_ai", "imported_by": user_id or "system"},
    )
    normalize_change_pack_quality(pack)
    if pack.mode == TopicChangeMode.INITIAL:
        _validate_initial_change_pack_quality(pack)
    save_change_pack(pack)
    return pack


def _extract_action_items(data: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    for key in ("actions", "topics", "topic_library", "themes"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _infer_import_mode(data: dict[str, Any] | list[Any], actions: list[TopicChangeAction]) -> TopicChangeMode:
    explicit_mode = ""
    if isinstance(data, dict):
        explicit_mode = str(data.get("mode") or "").strip().lower()

    has_create = any(action.action_type == TopicActionType.CREATE_THEME for action in actions)
    has_update = any(action.action_type == TopicActionType.UPDATE_THEME for action in actions)
    has_non_create = any(action.action_type != TopicActionType.CREATE_THEME for action in actions)

    if _looks_like_supply_chain_nodes_payload(data):
        return TopicChangeMode.UPDATE
    if explicit_mode == "update":
        return TopicChangeMode.UPDATE
    if explicit_mode == "initial" and has_create:
        return TopicChangeMode.INITIAL
    if has_update or has_non_create:
        return TopicChangeMode.UPDATE
    return TopicChangeMode.INITIAL


def _remove_initial_only_warnings(warnings: list[str]) -> list[str]:
    blocked_patterns = (
        "初始化未產生任何 create_theme",
        "無法建立題材庫",
    )
    return [warning for warning in warnings if not any(pattern in warning for pattern in blocked_patterns)]


def _looks_like_supply_chain_nodes_payload(data: dict[str, Any] | list[Any]) -> bool:
    nodes = _extract_supply_chain_nodes(data)
    return bool(nodes) and all(_looks_like_supply_chain_node(item) for item in nodes)


def _extract_supply_chain_nodes(data: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    value = data.get("supply_chain_nodes") if isinstance(data, dict) else None
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _looks_like_supply_chain_node(item: dict[str, Any]) -> bool:
    if item.get("action_type"):
        return False
    node_keys = {"node_id", "theme_id", "company_code", "company_name", "role"}
    return bool(item.get("theme_id")) and len(node_keys.intersection(item.keys())) >= 3


def _actions_from_supply_chain_nodes(nodes: list[dict[str, Any]]) -> list[TopicChangeAction]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        theme_id = str(node.get("theme_id") or "").strip()
        if not theme_id:
            continue
        grouped.setdefault(theme_id, []).append(node)

    actions: list[TopicChangeAction] = []
    for index, (theme_id, theme_nodes) in enumerate(grouped.items(), start=1):
        companies = []
        evidence = []
        risk_notes: list[str] = []
        missing_data: list[str] = []
        for node in theme_nodes:
            company_code = str(node.get("company_code") or "").strip()
            company_name = str(node.get("company_name") or "").strip()
            if company_code or company_name:
                companies.append(
                    {
                        "company_code": company_code,
                        "company_name": company_name,
                        "role": str(node.get("role") or ""),
                        "verification_status": str(node.get("verification_status") or node.get("status") or ""),
                        "evidence": _as_dict_list(node.get("evidence", [])),
                        "missing_data": _as_str_list(node.get("missing_data", [])),
                    }
                )
            evidence.extend(_as_dict_list(node.get("evidence", [])))
            risk_notes.extend(_as_str_list(node.get("risk_notes", [])))
            missing_data.extend(_as_str_list(node.get("missing_data", [])))

        unique_risks = list(dict.fromkeys(risk_notes))
        unique_missing = list(dict.fromkeys(missing_data))
        action = TopicChangeAction(
            action_type=TopicActionType.UPDATE_THEME,
            theme_id=_safe_theme_id(theme_id, "", index),
            theme_name=str(theme_nodes[0].get("theme_name") or theme_id),
            confidence=_normalize_confidence(theme_nodes[0].get("confidence")),
            reason="外部匯入供應鏈節點補強。",
            evidence=[TopicEvidence.from_dict(item) for item in evidence if isinstance(item, dict)],
            affected_companies=companies,
            risk_notes=unique_risks,
            missing_data=unique_missing,
            supply_chain_nodes=theme_nodes,
        )
        actions.append(action)
    return actions


def _normalize_action(item: dict[str, Any], index: int) -> TopicChangeAction:
    theme_name = str(item.get("theme_name") or item.get("name") or item.get("display") or item.get("title") or "").strip()
    theme_id = _safe_theme_id(str(item.get("theme_id") or item.get("id") or ""), theme_name, index)
    confidence = _normalize_confidence(item.get("confidence"))
    evidence = [TopicEvidence.from_dict(e) for e in _as_dict_list(item.get("evidence", []))]
    return TopicChangeAction(
        action_type=_normalize_action_type(item.get("action_type")),
        theme_id=theme_id,
        theme_name=theme_name or theme_id,
        keywords=_as_str_list(item.get("keywords", [])),
        industries=_as_str_list(item.get("industries", [])),
        supply_chain_role=str(item.get("supply_chain_role") or item.get("role") or ""),
        confidence=confidence,
        reason=str(item.get("reason") or item.get("description") or ""),
        evidence=evidence,
        company_relations=_as_dict_list(item.get("company_relations", [])),
        affected_companies=_normalize_affected_companies(item.get("affected_companies", [])),
        risk_notes=_as_str_list(item.get("risk_notes", [])),
        missing_data=_as_str_list(item.get("missing_data", [])),
        supply_chain_nodes=_as_dict_list(item.get("supply_chain_nodes", [])),
        counter_evidence=_as_dict_list(item.get("counter_evidence", [])),
        extra={k: v for k, v in item.items() if k not in _KNOWN_ACTION_KEYS},
    )


def _safe_theme_id(raw_id: str, theme_name: str, index: int) -> str:
    candidate = raw_id.strip().lower()
    candidate = re.sub(r"[^a-z0-9_]+", "_", candidate)
    candidate = re.sub(r"_+", "_", candidate).strip("_")
    if not candidate and theme_name:
        candidate = re.sub(r"[^a-z0-9_]+", "_", theme_name.lower())
        candidate = re.sub(r"_+", "_", candidate).strip("_")
    return candidate or f"external_topic_{index:02d}"


def _normalize_action_type(value: Any) -> TopicActionType:
    try:
        return TopicActionType(str(value or "create_theme"))
    except ValueError:
        return TopicActionType.CREATE_THEME


def _normalize_confidence(value: Any) -> TopicConfidence:
    try:
        return TopicConfidence(str(value or "medium").lower())
    except ValueError:
        return TopicConfidence.MEDIUM


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _normalize_affected_companies(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return _as_str_list(value)
    if any(isinstance(item, dict) for item in value):
        normalized_items: list[Any] = []
        for item in value:
            if isinstance(item, dict):
                normalized = dict(item)
                if "company_code" not in normalized and "code" in normalized:
                    normalized["company_code"] = normalized.get("code")
                if "company_name" not in normalized and "name" in normalized:
                    normalized["company_name"] = normalized.get("name")
                normalized_items.append(normalized)
            elif item is not None:
                normalized_items.append(str(item))
        return [item for item in normalized_items if item]
    result: list[str] = []
    for item in value:
        if isinstance(item, dict):
            code = str(item.get("code") or item.get("company_code") or "").strip()
            name = str(item.get("name") or item.get("company_name") or "").strip()
            role = str(item.get("role") or "").strip()
            label = " ".join(part for part in (code, name) if part)
            result.append(f"{label}：{role}" if role and label else label or role)
        elif item is not None:
            result.append(str(item))
    return [item for item in result if item]


_KNOWN_ACTION_KEYS = {
    "action_type",
    "theme_id",
    "id",
    "theme_name",
    "name",
    "display",
    "title",
    "keywords",
    "industries",
    "supply_chain_role",
    "role",
    "confidence",
    "reason",
    "description",
    "evidence",
    "company_relations",
    "affected_companies",
    "risk_notes",
    "missing_data",
    "supply_chain_nodes",
    "counter_evidence",
}
