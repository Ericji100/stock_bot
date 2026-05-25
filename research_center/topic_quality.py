"""Validation and normalization helpers for topic-library change packs."""
from __future__ import annotations

from typing import Any


APPLY_STATUSES = {"verified", "inferred"}
NON_APPLY_STATUSES = {"candidate", "missing"}
ALL_STATUSES = APPLY_STATUSES | NON_APPLY_STATUSES


def normalize_status(value: Any, *, default: str = "") -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "verify": "verified",
        "confirmed": "verified",
        "fact": "verified",
        "factual": "verified",
        "direct": "verified",
        "official": "verified",
        "infer": "inferred",
        "inference": "inferred",
        "indirect": "inferred",
        "reasoned": "inferred",
        "sentiment": "candidate",
        "watch": "candidate",
        "needs_verification": "candidate",
        "unverified": "candidate",
        "unknown": "missing",
        "insufficient": "missing",
        "not_found": "missing",
        "none": "missing",
    }
    normalized = aliases.get(text, text)
    return normalized if normalized in ALL_STATUSES else default


def is_applyable_status(value: Any) -> bool:
    return normalize_status(value) in APPLY_STATUSES


def is_retainable_company_status(value: Any) -> bool:
    return normalize_status(value) in APPLY_STATUSES | {"candidate"}


def evidence_source_levels(evidence: Any) -> list[str]:
    if not isinstance(evidence, list):
        return []
    levels: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("source_level") or item.get("level") or "").strip()
        if raw:
            levels.append(raw)
    return levels


def has_meaningful_evidence(evidence: Any) -> bool:
    if not isinstance(evidence, list):
        return False
    for item in evidence:
        if isinstance(item, dict) and (
            item.get("source")
            or item.get("source_id")
            or item.get("source_title")
            or item.get("url")
            or item.get("content")
            or item.get("claim")
        ):
            return True
    return False


def infer_status(item: dict[str, Any], fallback_evidence: list[dict[str, Any]] | None = None) -> str:
    """Infer item verification status without trusting broad confidence alone."""
    explicit = (
        item.get("verification_status")
        or item.get("evidence_status")
        or item.get("claim_status")
        or item.get("status")
    )
    status = normalize_status(explicit)
    if status:
        return status

    relation_type = normalize_status(item.get("relation_type"))
    if relation_type in NON_APPLY_STATUSES:
        return relation_type

    evidence = item.get("evidence")
    if not has_meaningful_evidence(evidence) and fallback_evidence:
        evidence = fallback_evidence
    levels = [level.lower() for level in evidence_source_levels(evidence)]
    if any("l1" in level or "official" in level or "company" in level for level in levels):
        return "verified"
    if any("l2" in level or "media" in level for level in levels):
        return "inferred"
    if has_meaningful_evidence(evidence):
        return "inferred"
    if item.get("missing_data") and not _has_substantive_value(item):
        return "missing"
    return "candidate"


def normalize_field(value: Any) -> dict[str, Any]:
    """Normalize scalar/list field or structured {value,status,evidence} field."""
    if isinstance(value, dict) and any(k in value for k in ("value", "status", "verification_status", "evidence_status")):
        status = normalize_status(
            value.get("verification_status") or value.get("evidence_status") or value.get("status")
        )
        return {
            "value": value.get("value"),
            "status": status or "candidate",
            "evidence": value.get("evidence") if isinstance(value.get("evidence"), list) else [],
            "missing_data": _as_list(value.get("missing_data")),
        }
    return {"value": value, "status": "", "evidence": [], "missing_data": []}


def applyable_field_value(value: Any) -> tuple[Any, list[dict[str, Any]], list[str]]:
    """Return (value, evidence, missing_data) for fields allowed into formal library."""
    field = normalize_field(value)
    status = field["status"]
    if status in NON_APPLY_STATUSES:
        return None, [], field["missing_data"]
    return field["value"], field["evidence"], field["missing_data"]


def normalize_change_pack_quality(pack: Any) -> None:
    """Annotate change-pack actions with quality summary and downgrade obvious weak claims."""
    summary = {
        "verified_items": 0,
        "inferred_items": 0,
        "candidate_items": 0,
        "missing_items": 0,
        "policy": "topic_confirm applies verified+inferred as formal relations; candidate company relations are retained as hypothesis_only; candidate supply-chain nodes are skipped; missing is recorded as missing_data.",
    }
    for action in getattr(pack, "actions", []) or []:
        fallback_evidence = [ev.to_dict() if hasattr(ev, "to_dict") else ev for ev in getattr(action, "evidence", []) or []]
        for entry in getattr(action, "company_relations", []) or []:
            if not isinstance(entry, dict):
                continue
            status = infer_status(entry, fallback_evidence)
            entry.setdefault("verification_status", status)
            _bump(summary, status)
        for entry in getattr(action, "affected_companies", []) or []:
            if not isinstance(entry, dict):
                continue
            status = infer_status(entry, fallback_evidence)
            entry.setdefault("verification_status", status)
            _bump(summary, status)
        for entry in getattr(action, "supply_chain_nodes", []) or []:
            if not isinstance(entry, dict):
                continue
            status = infer_status(entry, fallback_evidence)
            entry.setdefault("verification_status", status)
            _bump(summary, status)
        _collect_action_missing_data(action)

    extra = getattr(pack, "extra", None)
    if isinstance(extra, dict):
        extra["quality_summary"] = summary
    if hasattr(pack, "warnings"):
        warning = "topic_confirm will apply verified+inferred records, retain candidate company relations as hypothesis_only, and skip candidate supply-chain nodes."
        if warning not in pack.warnings:
            pack.warnings.append(warning)


def _collect_action_missing_data(action: Any) -> None:
    missing = list(getattr(action, "missing_data", []) or [])
    for collection_name in ("company_relations", "affected_companies", "supply_chain_nodes"):
        for entry in getattr(action, collection_name, []) or []:
            if not isinstance(entry, dict):
                continue
            status = normalize_status(entry.get("verification_status") or entry.get("status"))
            if status == "missing":
                label = entry.get("company_name") or entry.get("company_code") or entry.get("role") or collection_name
                for item in _as_list(entry.get("missing_data")) or [f"{label}: missing verifiable evidence"]:
                    if item not in missing:
                        missing.append(item)
            for field in ("products", "customers", "revenue_exposure", "benefit_logic"):
                field_data = normalize_field(entry.get(field))
                if field_data["status"] == "missing":
                    for item in field_data["missing_data"] or [f"{field}: missing verifiable evidence"]:
                        if item not in missing:
                            missing.append(item)
    action.missing_data = missing


def _bump(summary: dict[str, Any], status: str) -> None:
    key = f"{status}_items"
    if key in summary:
        summary[key] += 1


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item not in (None, "")]
    if isinstance(value, tuple):
        return [item for item in value if item not in (None, "")]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _has_substantive_value(item: dict[str, Any]) -> bool:
    for key in ("products", "customers", "revenue_exposure", "benefit_logic", "role"):
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("value") if "value" in value else value
        if value:
            return True
    return False
