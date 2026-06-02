"""Normalize small AI topic-maintenance outputs into TopicChangeAction objects."""
from __future__ import annotations

import re
from typing import Any

from .topic_data_normalizer import normalize_string_list, normalize_text_tree, to_traditional_text
from .topic_models import TopicActionType, TopicChangeAction, TopicConfidence, TopicEvidence, TopicSourceLevel


def slugify_theme_id(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^a-z0-9_]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:64] or "unknown_topic"


def normalize_topic_candidate(data: Any, index: int = 0) -> dict[str, Any] | None:
    """Return a safe topic candidate dict, or None when unusable."""
    if not isinstance(data, dict):
        return None
    data = normalize_text_tree(data)
    theme_name = str(
        data.get("theme_name")
        or data.get("topic_hint")
        or data.get("name")
        or data.get("title")
        or ""
    ).strip()
    if not theme_name:
        return None
    raw_id = data.get("theme_id") or data.get("id") or theme_name
    return {
        "theme_id": slugify_theme_id(str(raw_id)),
        "theme_name": to_traditional_text(theme_name),
        "keywords": normalize_string_list(data.get("keywords") or [theme_name]),
        "reason": to_traditional_text(str(data.get("reason") or data.get("summary") or "")),
        "source_refs": _as_list(data.get("source_refs") or data.get("sources")),
        "candidate_companies": _normalize_company_items(data.get("candidate_companies") or data.get("companies")),
        "rank": int(data.get("rank") or index + 1),
    }


def normalize_topic_detail_action(
    data: Any,
    *,
    fallback_candidate: dict[str, Any] | None = None,
    default_action_type: TopicActionType = TopicActionType.CREATE_THEME,
) -> TopicChangeAction | None:
    """Normalize one AI-produced topic detail into a TopicChangeAction."""
    if not isinstance(data, dict):
        return None
    data = normalize_text_tree(data)
    fallback_candidate = fallback_candidate or {}

    action_type = _parse_action_type(data.get("action_type"), default_action_type)
    theme_name = str(data.get("theme_name") or fallback_candidate.get("theme_name") or "").strip()
    theme_id = str(data.get("theme_id") or fallback_candidate.get("theme_id") or theme_name).strip()
    if not theme_name and not theme_id:
        return None
    theme_id = slugify_theme_id(theme_id)
    if not theme_name:
        theme_name = theme_id

    evidence = _normalize_evidence(data.get("evidence") or data.get("sources"))
    affected_companies = _normalize_company_items(
        data.get("affected_companies") or fallback_candidate.get("candidate_companies")
    )
    company_relations = _normalize_company_relations(data.get("company_relations"), theme_id, evidence)
    supply_chain_nodes = _normalize_supply_chain_nodes(data.get("supply_chain_nodes"), theme_id, evidence)

    if not supply_chain_nodes:
        supply_chain_nodes = [{
            "theme_id": theme_id,
            "company_code": "",
            "company_name": "",
            "role": "待補供應鏈或題材關聯",
            "confidence": "low",
            "source_level": "L3_community",
            "evidence": [item.to_dict() for item in evidence],
            "risk_notes": ["待後續維護補強"],
            "missing_data": ["待補供應鏈節點"],
            "upstream": [],
            "downstream": [],
            "product_keywords": [],
        }]

    return TopicChangeAction(
        action_type=action_type,
        theme_id=theme_id,
        theme_name=to_traditional_text(theme_name),
        keywords=normalize_string_list(data.get("keywords") or fallback_candidate.get("keywords") or [theme_name]),
        industries=normalize_string_list(data.get("industries")),
        supply_chain_role=to_traditional_text(str(data.get("supply_chain_role") or "")),
        confidence=_parse_confidence(data.get("confidence")),
        reason=to_traditional_text(str(data.get("reason") or fallback_candidate.get("reason") or "")),
        evidence=evidence,
        target_theme_id=data.get("target_theme_id"),
        affected_companies=affected_companies,
        company_relations=company_relations,
        risk_notes=normalize_string_list(data.get("risk_notes")) or ["待後續維護補強"],
        missing_data=normalize_string_list(data.get("missing_data")) or ["待後續維護補強"],
        supply_chain_nodes=supply_chain_nodes,
        counter_evidence=_as_list(data.get("counter_evidence")),
        extra=data.get("extra") if isinstance(data.get("extra"), dict) else {},
    )


def normalize_topic_detail_actions(
    payload: Any,
    *,
    fallback_candidates: list[dict[str, Any]] | None = None,
    default_action_type: TopicActionType = TopicActionType.CREATE_THEME,
) -> list[TopicChangeAction]:
    """Normalize a detail payload into actions."""
    raw_items: list[Any]
    if isinstance(payload, dict):
        raw_items = payload.get("actions") or payload.get("topics") or payload.get("items") or []
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []
    fallback_candidates = fallback_candidates or []
    actions: list[TopicChangeAction] = []
    for idx, item in enumerate(raw_items):
        fallback = fallback_candidates[idx] if idx < len(fallback_candidates) else None
        action = normalize_topic_detail_action(
            item,
            fallback_candidate=fallback,
            default_action_type=default_action_type,
        )
        if action is not None:
            actions.append(action)
    return actions


def _parse_action_type(value: Any, default: TopicActionType) -> TopicActionType:
    try:
        return TopicActionType(str(value or default.value))
    except ValueError:
        return default


def _parse_confidence(value: Any) -> TopicConfidence:
    try:
        return TopicConfidence(str(value or "medium").lower())
    except ValueError:
        return TopicConfidence.MEDIUM


def _normalize_evidence(value: Any) -> list[TopicEvidence]:
    items = _as_list(value)
    result: list[TopicEvidence] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            level = TopicSourceLevel(str(item.get("source_level") or "L2_media"))
        except ValueError:
            level = TopicSourceLevel.L2_MEDIA
        source = str(item.get("source") or item.get("source_title") or item.get("title") or "").strip()
        content = str(item.get("content") or item.get("claim") or item.get("snippet") or "").strip()
        if not source and not content:
            continue
        result.append(TopicEvidence(
            source=source or "未命名來源",
            source_level=level,
            content=to_traditional_text(content),
            url=item.get("url"),
            publish_date=item.get("publish_date") or item.get("published_date"),
            score_contribution=float(item.get("score_contribution") or 0.0),
        ))
    return result


def _normalize_company_items(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _as_list(value):
        if isinstance(item, str):
            item = {"company_code": item, "company_name": ""}
        if not isinstance(item, dict):
            continue
        code = str(item.get("company_code") or item.get("code") or "").strip()
        name = to_traditional_text(str(item.get("company_name") or item.get("name") or "").strip())
        role = to_traditional_text(str(item.get("role") or item.get("supply_chain_role") or "").strip())
        if not code and not name:
            continue
        normalized = dict(item)
        normalized["company_code"] = code
        normalized["company_name"] = name
        normalized["role"] = role or "待補題材角色"
        normalized.setdefault("verification_status", item.get("status") or "candidate")
        normalized.setdefault("evidence", item.get("evidence") if isinstance(item.get("evidence"), list) else [])
        result.append(normalized)
    return result


def _normalize_company_relations(value: Any, theme_id: str, fallback_evidence: list[TopicEvidence]) -> list[dict[str, Any]]:
    result = _normalize_company_items(value)
    evidence = [item.to_dict() for item in fallback_evidence]
    for item in result:
        item.setdefault("theme_id", theme_id)
        item.setdefault("relation_strength", item.get("confidence") or "low")
        item.setdefault("relation_type", "candidate")
        item.setdefault("evidence", evidence)
        item.setdefault("missing_data", [])
    return result


def _normalize_supply_chain_nodes(value: Any, theme_id: str, fallback_evidence: list[TopicEvidence]) -> list[dict[str, Any]]:
    evidence = [item.to_dict() for item in fallback_evidence]
    result: list[dict[str, Any]] = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        node = dict(item)
        node["theme_id"] = str(node.get("theme_id") or theme_id)
        node["company_code"] = str(node.get("company_code") or node.get("code") or "").strip()
        node["company_name"] = to_traditional_text(str(node.get("company_name") or node.get("name") or "").strip())
        node["role"] = to_traditional_text(str(node.get("role") or "待補供應鏈角色"))
        node.setdefault("confidence", node.get("relation_strength") or "low")
        node.setdefault("source_level", "L2_media" if evidence else "L3_community")
        node.setdefault("evidence", evidence)
        node.setdefault("risk_notes", ["待後續維護補強"])
        node.setdefault("missing_data", ["待補供應鏈節點證據"])
        node.setdefault("upstream", [])
        node.setdefault("downstream", [])
        node.setdefault("product_keywords", [])
        result.append(node)
    return result


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]
