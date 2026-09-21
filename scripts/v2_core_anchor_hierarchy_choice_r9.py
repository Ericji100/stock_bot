"""R9B hierarchy choice among frozen R9A family representatives.

Separates campaign context from path-specific working anchor and fixed trade
defense.  The model judges causal roles; the program checks AS-OF/evidence and
role consistency.  No trade is authorized at this calibration stage.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from scripts.v2_core_legacy_hierarchy_temporal_r8 import check_temporal_facts
from scripts.v2_core_legacy_role_qualification_r7 import _known_refs, transport_schema


VERSION = "v2-core-anchor-hierarchy-choice-r9-candidate-r1"
RELATIONSHIPS = (
    "FRESH_FORMING_UP_ANCHOR",
    "COMPLETED_MACRO_UP_PARENT",
    "MATURE_UP_CAMPAIGN",
    "ACTIVE_BEAR_CONTROL",
    "UNRESOLVED",
)
ATOMS = (
    "WORKING_ANCHOR_EXECUTION_FIT",
    "CAMPAIGN_CONTEXT_FIT",
    "DIRECT_CONTROL_OF_DEFENSE_EPISODE",
    "PRICE_STRUCTURE_CORROBORATION",
)


def _representatives(retrieval: dict[str, Any]) -> list[str]:
    if retrieval["status"] != "VALID" or retrieval["final_anchor_selected"] or retrieval["trade_permission_granted"]:
        raise ValueError("R9A retrieval not a valid no-trade precursor")
    ids = [row["candidate_id"] for row in retrieval["group_representatives"].values()]
    if len(set(ids)) != len(ids) or len(ids) != retrieval["group_count"]:
        raise ValueError("R9A representative IDs invalid")
    return sorted(ids)


def build_schema(packet: dict[str, Any], defense: dict[str, Any], defense_id: str, retrieval: dict[str, Any]) -> dict[str, Any]:
    if packet["review_id"] != defense["review_id"] or packet["as_of"] != defense["as_of"]:
        raise ValueError("packet/defense mismatch")
    if defense_id not in {row["candidate_id"] for row in defense["candidate_rows"]}:
        raise ValueError("bound defense absent")
    if retrieval["bound_defense_candidate_id"] != defense_id:
        raise ValueError("retrieval/defense mismatch")
    ids = _representatives(retrieval)
    refs = _known_refs(packet)
    item = {
        "type": "object", "additionalProperties": False,
        "required": ["atoms", "supporting_evidence_refs", "explanation"],
        "properties": {
            "atoms": {"type": "object", "additionalProperties": False, "required": list(ATOMS), "properties": {atom: {"type": "string", "enum": ["PASS", "FAIL", "UNKNOWN"]} for atom in ATOMS}},
            "supporting_evidence_refs": {"type": "array", "minItems": 1, "maxItems": 10, "uniqueItems": True, "items": {"type": "string", "enum": refs}},
            "explanation": {"type": "string", "minLength": 20, "maxLength": 700},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "review_id", "as_of", "bound_defense_candidate_id", "representative_assessments", "selected_working_anchor_id", "selected_campaign_context_id", "relationship_class", "role_comparison_reason", "causal_attestation"],
        "properties": {
            "schema_version": {"type": "string", "const": VERSION},
            "review_id": {"type": "string", "const": packet["review_id"]},
            "as_of": {"type": "string", "const": packet["as_of"]},
            "bound_defense_candidate_id": {"type": "string", "const": defense_id},
            "representative_assessments": {"type": "object", "additionalProperties": False, "required": ids, "properties": {candidate_id: item for candidate_id in ids}},
            "selected_working_anchor_id": {"type": "string", "enum": [*ids, "NONE"]},
            "selected_campaign_context_id": {"type": "string", "enum": [*ids, "NONE"]},
            "relationship_class": {"type": "string", "enum": list(RELATIONSHIPS)},
            "role_comparison_reason": {"type": "string", "minLength": 100, "maxLength": 1800},
            "causal_attestation": {
                "type": "object", "additionalProperties": False,
                "required": ["as_of_only", "no_identity", "no_teacher_answer", "no_future_performance", "defense_not_changed", "context_and_working_anchor_separate", "no_trade_permission"],
                "properties": {key: {"type": "boolean", "const": True} for key in (
                    "as_of_only", "no_identity", "no_teacher_answer", "no_future_performance",
                    "defense_not_changed", "context_and_working_anchor_separate", "no_trade_permission",
                )},
            },
        },
    }


def validate_and_choose(
    response: dict[str, Any], *, packet: dict[str, Any], defense: dict[str, Any],
    defense_id: str, retrieval: dict[str, Any],
) -> dict[str, Any]:
    schema = build_schema(packet, defense, defense_id, retrieval)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda error: list(map(str, error.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    candidates = {row["candidate_id"]: row for row in packet["candidate_pool"]}
    assessments = response["representative_assessments"]
    errors = []
    for candidate_id, item in assessments.items():
        if not set(candidates[candidate_id]["source_evidence_refs"]).intersection(item["supporting_evidence_refs"]):
            errors.append(f"representative lacks candidate-specific evidence: {candidate_id}")
    working_id = response["selected_working_anchor_id"]
    context_id = response["selected_campaign_context_id"]
    relation = response["relationship_class"]
    temporal = None
    if working_id == "NONE":
        if relation != "UNRESOLVED":
            errors.append("NONE working anchor requires UNRESOLVED relationship")
    else:
        working = candidates[working_id]
        atoms = assessments[working_id]["atoms"]
        if any(atoms[name] != "PASS" for name in (
            "WORKING_ANCHOR_EXECUTION_FIT", "DIRECT_CONTROL_OF_DEFENSE_EPISODE", "PRICE_STRUCTURE_CORROBORATION",
        )):
            errors.append("selected working anchor lacks required PASS atoms")
        if any(row["candidate_id"] == working_id and row["relevance_judgement"] == "FAIL" for row in retrieval["group_representatives"].values()):
            errors.append("selected working anchor previously marked irrelevant")
        expected = {
            "FRESH_FORMING_UP_ANCHOR": ("UP", "FORMING"),
            "COMPLETED_MACRO_UP_PARENT": ("UP", "CONFIRMED"),
            "MATURE_UP_CAMPAIGN": ("UP", None),
            "ACTIVE_BEAR_CONTROL": ("DOWN", None),
        }
        if relation not in expected:
            errors.append("selected working anchor requires resolved relationship")
        else:
            direction, status = expected[relation]
            if working["direction"] != direction or (status is not None and working["status"] != status):
                errors.append("working anchor direction/lifecycle conflicts with relationship")
        defense_row = next(row for row in defense["candidate_rows"] if row["candidate_id"] == defense_id)
        temporal = check_temporal_facts(working, defense_row, packet["as_of"])
        if temporal["status"] != "LEGAL_AS_OF":
            errors.extend(temporal["errors"])
    if context_id != "NONE" and assessments[context_id]["atoms"]["CAMPAIGN_CONTEXT_FIT"] != "PASS":
        errors.append("selected campaign context lacks context PASS")
    if context_id == working_id and context_id != "NONE" and relation != "MATURE_UP_CAMPAIGN":
        errors.append("context and working anchor collapsed outside mature path")
    if errors:
        return {"status": "INVALID", "errors": errors[:20]}
    return {
        "status": "VALID",
        "resolution_status": "SELECTED" if working_id != "NONE" else "UNRESOLVED_NO_TRADE",
        "selected_working_anchor_id": None if working_id == "NONE" else working_id,
        "selected_campaign_context_id": None if context_id == "NONE" else context_id,
        "relationship_class": relation,
        "bound_defense_candidate_id": defense_id,
        "temporal_facts": temporal,
        "trade_permission_granted": False,
    }


def transport_for(packet: dict[str, Any], defense: dict[str, Any], defense_id: str, retrieval: dict[str, Any]) -> dict[str, Any]:
    return transport_schema(build_schema(packet, defense, defense_id, retrieval))


def parse_raw_without_duplicate_keys(raw_bytes: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    result = json.loads(raw_bytes.decode("utf-8-sig"), object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError("R9B hierarchy output root must be object")
    return result
