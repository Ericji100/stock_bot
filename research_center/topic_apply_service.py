"""Topic apply service - confirms or rejects topic change packs."""
from __future__ import annotations

import json
import re
from typing import Any

from .topic_models import (
    TopicActionType,
    TopicApplyResult,
    TopicAuditEntry,
    TopicChangeAction,
    TopicChangePack,
    TopicChangeStatus,
    TopicCompanyRelation,
    TopicProfile,
    TopicSupplyChainNode,
)
from .topic_repository import (
    backup_topic_files,
    load_change_pack,
    load_company_knowledge_data,
    load_company_topic_map,
    load_supply_chain_nodes,
    load_topic_profiles,
    save_company_knowledge_data,
    save_company_topic_map,
    save_supply_chain_nodes,
    save_topic_profiles,
    update_change_pack_status,
    write_topic_audit_log,
)
from .topic_quality import applyable_field_value, infer_status, is_applyable_status, is_retainable_company_status, normalize_status


def confirm_change_pack(change_id: str, user_id: str = "system") -> TopicApplyResult:
    """Apply a pending change pack to the formal topic library.

    AI only creates pending change packs. This function is the reviewed write
    boundary and updates theme_profiles, company_theme_map, supply_chain_nodes,
    and company_knowledge.
    """
    pack = load_change_pack(change_id)
    if pack is None:
        return TopicApplyResult(change_id=change_id, success=False, errors=[f"找不到變更包：{change_id}"])
    if pack.status != TopicChangeStatus.PENDING:
        return TopicApplyResult(
            change_id=change_id,
            success=False,
            errors=[f"變更包狀態為 {pack.status.value}，只能套用 pending 變更包"],
        )

    backup_result = backup_topic_files(f"confirm_{change_id}")
    backup_path = backup_result.get("backup_root", "")

    profiles = load_topic_profiles()
    profile_map = {p.theme_id: p for p in profiles}
    company_map = load_company_topic_map()
    supply_nodes = load_supply_chain_nodes()
    company_knowledge = load_company_knowledge_data()

    result = TopicApplyResult(change_id=change_id, success=True, backup_path=backup_path)
    updated_at = pack.created_at[:19]

    for action in pack.actions:
        try:
            if action.action_type == TopicActionType.CREATE_THEME:
                if action.theme_id in profile_map:
                    result.skipped += 1
                    _apply_company_and_nodes(action, company_map, supply_nodes, updated_at)
                    continue
                profile = _profile_from_action(action, pack)
                profiles.append(profile)
                profile_map[action.theme_id] = profile
                result.created += 1
                _apply_company_and_nodes(action, company_map, supply_nodes, updated_at)

            elif action.action_type == TopicActionType.UPDATE_THEME:
                if action.theme_id not in profile_map:
                    result.skipped += 1
                    _apply_company_and_nodes(action, company_map, supply_nodes, updated_at)
                    continue
                existing = profile_map[action.theme_id]
                existing.theme_name = action.theme_name or existing.theme_name
                existing.keywords = action.keywords or existing.keywords
                existing.industries = action.industries or existing.industries
                existing.supply_chain_role = action.supply_chain_role or existing.supply_chain_role
                existing.updated_at = updated_at
                if action.risk_notes:
                    existing.risk_notes = action.risk_notes
                if action.missing_data:
                    existing.missing_data = action.missing_data
                result.updated += 1
                _apply_company_and_nodes(action, company_map, supply_nodes, updated_at)

            elif action.action_type == TopicActionType.MERGE_THEME:
                if _merge_theme(action, profile_map, profiles, updated_at):
                    profiles = [p for p in profiles if p.theme_id in profile_map]
                    result.merged += 1
                else:
                    result.skipped += 1

            elif action.action_type == TopicActionType.RENAME_THEME:
                if action.theme_id not in profile_map:
                    result.skipped += 1
                    continue
                profile_map[action.theme_id].theme_name = action.theme_name
                profile_map[action.theme_id].updated_at = updated_at
                result.updated += 1

            else:
                result.skipped += 1

        except Exception as exc:
            result.failed += 1
            result.errors.append(f"action {action.action_type.value} {action.theme_id}: {exc}")

    if result.failed > 0:
        result.success = False
        return result

    company_knowledge_changed = _apply_company_knowledge_updates(
        company_knowledge,
        pack.company_knowledge_updates,
        updated_at,
    )

    try:
        save_topic_profiles(profiles)
        save_company_topic_map(company_map)
        save_supply_chain_nodes(supply_nodes)
        if company_knowledge_changed:
            save_company_knowledge_data(company_knowledge)
    except Exception as exc:
        result.success = False
        result.errors.append(f"寫入正式題材庫失敗：{exc}")
        return result

    update_change_pack_status(change_id, TopicChangeStatus.CONFIRMED)
    write_topic_audit_log(
        TopicAuditEntry.create(
            "confirmed",
            change_id,
            user_id,
            {
                "created": result.created,
                "updated": result.updated,
                "merged": result.merged,
                "skipped": result.skipped,
                "failed": result.failed,
                "company_knowledge_updated": company_knowledge_changed,
                "backup_path": backup_path,
            },
        )
    )
    return result


def reject_change_pack(change_id: str, user_id: str = "system", reason: str = "") -> dict[str, Any]:
    """Reject a change pack without modifying the formal library."""
    pack = load_change_pack(change_id)
    if pack is None:
        return {"success": False, "error": f"找不到變更包：{change_id}"}
    if pack.status != TopicChangeStatus.PENDING:
        return {"success": False, "error": f"變更包狀態為 {pack.status.value}，只能拒絕 pending 變更包"}

    update_change_pack_status(change_id, TopicChangeStatus.REJECTED)
    write_topic_audit_log(TopicAuditEntry.create("rejected", change_id, user_id, {"reason": reason}))
    return {"success": True, "change_id": change_id, "action": "rejected", "reason": reason}


def _profile_from_action(action: TopicChangeAction, pack: TopicChangePack) -> TopicProfile:
    return TopicProfile(
        theme_id=action.theme_id,
        theme_name=action.theme_name,
        keywords=action.keywords,
        industries=action.industries,
        supply_chain_role=action.supply_chain_role,
        confidence=action.confidence.value,
        source_level=action.evidence[0].source_level.value if action.evidence else "L2_media",
        status="active",
        created_at=pack.created_at[:19],
        updated_at=pack.created_at[:19],
        risk_notes=action.risk_notes,
        missing_data=action.missing_data,
    )


def _merge_theme(
    action: TopicChangeAction,
    profile_map: dict[str, TopicProfile],
    profiles: list[TopicProfile],
    updated_at: str,
) -> bool:
    target_id = action.target_theme_id
    if not target_id or target_id not in profile_map:
        return False
    target = profile_map[target_id]
    source_theme_id = action.theme_id
    if source_theme_id in profile_map:
        source = profile_map[source_theme_id]
        target.keywords = _merge_list_values(target.keywords, source.keywords)
        target.industries = _merge_list_values(target.industries, source.industries)
        target.updated_at = updated_at
        profile_map.pop(source_theme_id, None)
        profiles[:] = [p for p in profiles if p.theme_id != source_theme_id]
    return True


def _apply_company_and_nodes(
    action: TopicChangeAction,
    company_map: dict[str, TopicCompanyRelation],
    supply_nodes: list[TopicSupplyChainNode],
    updated_at: str,
) -> None:
    action_evidence = _evidence_to_dicts(action)
    for company in _company_entries_for_action(action):
        _merge_company_relation(company_map, action, company, action_evidence, updated_at)
    for index, node_data in enumerate(action.supply_chain_nodes or []):
        if isinstance(node_data, dict) and _has_explicit_non_apply_status(node_data):
            continue
        node = _node_from_action(action, node_data, index, action_evidence, updated_at)
        _upsert_supply_node(supply_nodes, node)


def _evidence_to_dicts(action: TopicChangeAction) -> list[dict[str, Any]]:
    return [item.to_dict() for item in action.evidence]


def _merge_list_values(existing: list[Any], additions: list[Any]) -> list[Any]:
    merged = list(existing or [])
    seen = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in merged}
    for item in additions or []:
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            merged.append(item)
            seen.add(marker)
    return merged


def _merge_many_list_values(*values: Any) -> list[Any]:
    merged: list[Any] = []
    for value in values:
        merged = _merge_list_values(merged, _as_list(value))
    return merged


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


_COMPANY_KNOWLEDGE_LIST_FIELDS = {
    "product_lines",
    "customers",
    "revenue_exposure",
    "supply_chain_roles",
    "evidence_sources",
    "risk_notes",
    "missing_data",
    "keywords",
}


def _apply_company_knowledge_updates(store: dict[str, Any], updates: dict[str, Any], updated_at: str) -> int:
    if not isinstance(updates, dict):
        return 0
    companies_updates = updates.get("companies") if isinstance(updates.get("companies"), dict) else updates
    if not isinstance(companies_updates, dict):
        return 0
    companies = store.setdefault("companies", {})
    if not isinstance(companies, dict):
        companies = {}
        store["companies"] = companies

    changed = 0
    for raw_code, update in companies_updates.items():
        code = str(raw_code or "").strip()
        if not code or not isinstance(update, dict):
            continue
        existing = companies.get(code)
        if not isinstance(existing, dict):
            existing = {}
        merged = _merge_company_knowledge_entry(existing, update, updated_at)
        companies[code] = merged
        changed += 1

    metadata = store.setdefault("metadata", {})
    if isinstance(metadata, dict) and changed:
        metadata["updated_at"] = updated_at
        metadata["source"] = metadata.get("source") or "topic_change_pack"
    return changed


def _merge_company_knowledge_entry(existing: dict[str, Any], update: dict[str, Any], updated_at: str) -> dict[str, Any]:
    merged = dict(existing or {})
    for field, value in update.items():
        if field in {"company_code", "code"}:
            continue
        if field in _COMPANY_KNOWLEDGE_LIST_FIELDS:
            merged[field] = _merge_list_values(_knowledge_as_list(merged.get(field)), _knowledge_as_list(value))
        elif isinstance(value, dict):
            current = merged.get(field) if isinstance(merged.get(field), dict) else {}
            merged[field] = {**current, **value}
        elif value not in (None, "", []):
            merged[field] = value
    merged["updated_at"] = str(update.get("updated_at") or updated_at)
    if "company_name" not in merged and update.get("name"):
        merged["company_name"] = update.get("name")
    return merged


def _knowledge_as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _parse_company_entry(entry: Any) -> dict[str, Any]:
    if isinstance(entry, dict):
        products, product_evidence, product_missing = applyable_field_value(entry.get("products") or entry.get("product_keywords"))
        customers, customer_evidence, customer_missing = applyable_field_value(entry.get("customers"))
        revenue_exposure, revenue_evidence, revenue_missing = applyable_field_value(entry.get("revenue_exposure"))
        benefit_logic, benefit_evidence, benefit_missing = applyable_field_value(entry.get("benefit_logic") or entry.get("reason"))
        evidence = entry.get("evidence") if isinstance(entry.get("evidence"), list) else []
        merged_evidence = _merge_many_list_values(
            evidence,
            product_evidence,
            customer_evidence,
            revenue_evidence,
            benefit_evidence,
        )
        missing_data = _merge_many_list_values(
            _as_list(entry.get("missing_data")),
            product_missing,
            customer_missing,
            revenue_missing,
            benefit_missing,
        )
        return {
            "company_code": str(entry.get("company_code") or entry.get("code") or "").strip(),
            "company_name": str(entry.get("company_name") or entry.get("name") or "").strip(),
            "role": str(entry.get("role") or entry.get("supply_chain_role") or "").strip(),
            "evidence": merged_evidence,
            "relation_strength": str(entry.get("relation_strength") or entry.get("confidence") or "").strip(),
            "relation_type": str(entry.get("relation_type") or "").strip(),
            "verification_status": infer_status(entry),
            "usage_policy": str(entry.get("usage_policy") or "").strip(),
            "not_representative": bool(entry.get("not_representative") or False),
            "products": _as_list(products),
            "customers": _as_list(customers),
            "revenue_exposure": revenue_exposure if isinstance(revenue_exposure, dict) else {},
            "benefit_logic": str(benefit_logic or "").strip(),
            "counter_evidence": entry.get("counter_evidence") if isinstance(entry.get("counter_evidence"), list) else [],
            "missing_data": missing_data,
        }

    text = str(entry or "").strip()
    match = re.match(r"^(?P<code>\d{4,6})(?:\s+(?P<rest>.+))?$", text)
    if not match:
        return {"company_code": text, "company_name": "", "role": "", "evidence": []}
    rest = (match.group("rest") or "").strip()
    name = rest
    role = ""
    for delimiter in ("：", ":", "-", "—"):
        if delimiter in rest:
            name, role = [part.strip() for part in rest.split(delimiter, 1)]
            break
    return {"company_code": match.group("code"), "company_name": name, "role": role, "evidence": []}


def _company_entries_for_action(action: TopicChangeAction) -> list[dict[str, Any]]:
    entries = []
    for raw in action.company_relations or []:
        parsed = _parse_company_entry(raw)
        if parsed.get("company_code") and _should_apply_company_entry(raw, parsed, action):
            entries.append(parsed)
    for raw in action.affected_companies or []:
        parsed = _parse_company_entry(raw)
        if parsed.get("company_code") and _should_apply_company_entry(raw, parsed, action):
            entries.append(parsed)

    merged: dict[str, dict[str, Any]] = {}
    for item in entries:
        code = str(item.get("company_code") or "")
        if code not in merged:
            merged[code] = item
            continue
        current = merged[code]
        for key in ("company_name", "role", "relation_strength", "relation_type", "benefit_logic"):
            if item.get(key) and not current.get(key):
                current[key] = item[key]
        for key in ("evidence", "products", "customers", "counter_evidence", "missing_data"):
            current[key] = _merge_list_values(current.get(key, []), item.get(key, []))
        if item.get("revenue_exposure") and not current.get("revenue_exposure"):
            current["revenue_exposure"] = item["revenue_exposure"]
    return list(merged.values())


def _should_apply_company_entry(raw: Any, parsed: dict[str, Any], action: TopicChangeAction) -> bool:
    action_evidence = _evidence_to_dicts(action)
    if isinstance(raw, dict):
        return is_retainable_company_status(infer_status(raw, action_evidence))
    return is_retainable_company_status(infer_status(parsed, action_evidence))


def _has_explicit_non_apply_status(item: dict[str, Any]) -> bool:
    explicit = item.get("verification_status") or item.get("evidence_status") or item.get("claim_status") or item.get("status")
    status = normalize_status(explicit)
    return status in {"candidate", "missing"}


def _merge_company_relation(
    company_map: dict[str, TopicCompanyRelation],
    action: TopicChangeAction,
    company: dict[str, Any],
    action_evidence: list[dict[str, Any]],
    updated_at: str,
) -> None:
    code = str(company.get("company_code") or "").strip()
    if not code:
        return
    rel = company_map.get(code)
    if rel is None:
        rel = TopicCompanyRelation(
            company_code=code,
            company_name=str(company.get("company_name") or ""),
            themes=[],
            primary_theme="" if normalize_status(company.get("verification_status")) == "candidate" else action.theme_id,
        )
        company_map[code] = rel

    status = normalize_status(company.get("verification_status"))
    if status == "candidate":
        _merge_candidate_theme_relation(rel, action, company, updated_at)
    else:
        if action.theme_id not in rel.themes:
            rel.themes.append(action.theme_id)
        if not rel.primary_theme:
            rel.primary_theme = action.theme_id
    if company.get("company_name") and not rel.company_name:
        rel.company_name = str(company.get("company_name"))
    if company.get("role"):
        rel.role = str(company.get("role"))
    if company.get("relation_strength"):
        rel.relation_strength = str(company.get("relation_strength"))
    elif not rel.relation_strength:
        rel.relation_strength = action.confidence.value
    if company.get("relation_type"):
        rel.relation_type = str(company.get("relation_type"))
    if company.get("benefit_logic"):
        rel.benefit_logic = str(company.get("benefit_logic"))
    rel.products = _merge_list_values(rel.products, company.get("products", []))
    rel.customers = _merge_list_values(rel.customers, company.get("customers", []))
    rel.evidence = _merge_list_values(rel.evidence, company.get("evidence") or action_evidence)
    rel.counter_evidence = _merge_list_values(rel.counter_evidence, company.get("counter_evidence", []))
    rel.missing_data = _merge_list_values(rel.missing_data, company.get("missing_data", []))
    if company.get("revenue_exposure"):
        rel.revenue_exposure = company["revenue_exposure"]
    rel.updated_at = updated_at


def _merge_candidate_theme_relation(
    rel: TopicCompanyRelation,
    action: TopicChangeAction,
    company: dict[str, Any],
    updated_at: str,
) -> None:
    candidates = rel.extra.get("candidate_themes")
    if not isinstance(candidates, list):
        candidates = []
    entry = {
        "theme_id": action.theme_id,
        "theme_name": action.theme_name,
        "verification_status": "candidate",
        "usage_policy": company.get("usage_policy") or "hypothesis_only",
        "not_representative": True,
        "role": company.get("role") or "",
        "evidence": company.get("evidence") or [],
        "missing_data": company.get("missing_data") or [],
        "updated_at": updated_at,
    }
    existing = next((item for item in candidates if isinstance(item, dict) and item.get("theme_id") == action.theme_id), None)
    if isinstance(existing, dict):
        existing.update({k: v for k, v in entry.items() if v not in ("", [], None)})
    else:
        candidates.append(entry)
    rel.extra["candidate_themes"] = candidates
    rel.extra["candidate_usage_policy"] = "hypothesis_only"


def _node_id_for(action: TopicChangeAction, node_data: dict[str, Any], index: int) -> str:
    node_id = str(node_data.get("node_id") or "").strip()
    if node_id:
        return node_id
    company_code = str(node_data.get("company_code") or node_data.get("code") or "").strip()
    role = str(node_data.get("role") or index + 1).strip()
    safe_role = re.sub(r"\W+", "_", role).strip("_")[:24] or str(index + 1)
    return f"{action.theme_id}_{company_code or 'node'}_{safe_role}"


def _node_from_action(
    action: TopicChangeAction,
    node_data: dict[str, Any],
    index: int,
    action_evidence: list[dict[str, Any]],
    updated_at: str,
) -> TopicSupplyChainNode:
    node_evidence = node_data.get("evidence") if isinstance(node_data.get("evidence"), list) else []
    product_keywords, product_evidence, product_missing = applyable_field_value(node_data.get("product_keywords") or node_data.get("products"))
    customers, customer_evidence, customer_missing = applyable_field_value(node_data.get("customers"))
    revenue_exposure, revenue_evidence, revenue_missing = applyable_field_value(node_data.get("revenue_exposure"))
    benefit_logic, benefit_evidence, benefit_missing = applyable_field_value(node_data.get("benefit_logic"))
    merged_evidence = _merge_many_list_values(
        node_evidence or action_evidence,
        product_evidence,
        customer_evidence,
        revenue_evidence,
        benefit_evidence,
    )
    missing_data = _merge_many_list_values(
        _as_list(node_data.get("missing_data")) or action.missing_data,
        product_missing,
        customer_missing,
        revenue_missing,
        benefit_missing,
    )
    known = {
        "node_id", "company_code", "code", "company_name", "name", "role",
        "upstream", "downstream", "product_keywords", "theme_id",
        "confidence", "source_level", "evidence", "risk_notes", "missing_data",
        "layer", "customers", "revenue_exposure", "benefit_logic", "updated_at",
    }
    return TopicSupplyChainNode(
        node_id=_node_id_for(action, node_data, index),
        company_code=str(node_data.get("company_code") or node_data.get("code") or ""),
        company_name=str(node_data.get("company_name") or node_data.get("name") or ""),
        role=str(node_data.get("role") or ""),
        upstream=_as_list(node_data.get("upstream")),
        downstream=_as_list(node_data.get("downstream")),
        product_keywords=_as_list(product_keywords),
        theme_id=str(node_data.get("theme_id") or action.theme_id),
        confidence=str(node_data.get("confidence") or action.confidence.value),
        source_level=str(node_data.get("source_level") or (action.evidence[0].source_level.value if action.evidence else "L2_media")),
        evidence=merged_evidence,
        risk_notes=_as_list(node_data.get("risk_notes")) or action.risk_notes,
        missing_data=missing_data,
        layer=node_data.get("layer"),
        customers=_as_list(customers),
        revenue_exposure=revenue_exposure if isinstance(revenue_exposure, dict) else {},
        benefit_logic=str(benefit_logic or ""),
        updated_at=str(node_data.get("updated_at") or updated_at),
        extra={k: v for k, v in node_data.items() if k not in known},
    )


def _node_key(node: TopicSupplyChainNode) -> tuple[str, str, str]:
    return (node.theme_id, node.company_code, node.role)


def _upsert_supply_node(supply_nodes: list[TopicSupplyChainNode], incoming: TopicSupplyChainNode) -> None:
    incoming_key = _node_key(incoming)
    for node in supply_nodes:
        if node.node_id == incoming.node_id or _node_key(node) == incoming_key:
            _merge_supply_node(node, incoming)
            return
    supply_nodes.append(incoming)


def _merge_supply_node(existing: TopicSupplyChainNode, incoming: TopicSupplyChainNode) -> None:
    if incoming.company_name and not existing.company_name:
        existing.company_name = incoming.company_name
    existing.upstream = _merge_list_values(existing.upstream, incoming.upstream)
    existing.downstream = _merge_list_values(existing.downstream, incoming.downstream)
    existing.product_keywords = _merge_list_values(existing.product_keywords, incoming.product_keywords)
    existing.evidence = _merge_list_values(existing.evidence, incoming.evidence)
    existing.risk_notes = _merge_list_values(existing.risk_notes, incoming.risk_notes)
    existing.missing_data = _merge_list_values(existing.missing_data, incoming.missing_data)
    existing.customers = _merge_list_values(existing.customers, incoming.customers)
    if incoming.confidence:
        existing.confidence = incoming.confidence
    if incoming.source_level:
        existing.source_level = incoming.source_level
    if incoming.layer is not None:
        existing.layer = incoming.layer
    if incoming.revenue_exposure:
        existing.revenue_exposure = incoming.revenue_exposure
    if incoming.benefit_logic:
        existing.benefit_logic = incoming.benefit_logic
    if incoming.updated_at:
        existing.updated_at = incoming.updated_at
    existing.extra.update(incoming.extra)
