"""R8 joint hierarchy perception: one work anchor against a frozen defense.

This is an exploratory calibration contract, not the formal V2 trade gate.
The model decides structural control; the program validates AS-OF legality,
fixed candidates/evidence and comparison.  No trade permission is possible.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from scripts.v2_core_legacy_hierarchy_temporal_r8 import check_temporal_facts
from scripts.v2_core_legacy_role_qualification_r7 import _known_refs, transport_schema


VERSION = "v2-core-working-anchor-judgement-r8-candidate-r1"
ATOMS = (
    "CLEAN_MEATY_PRICE_ANCHOR",
    "STRUCTURAL_BREAK_AND_DIRECTION",
    "DIRECT_CONTROL_OF_DEFENSE_EPISODE",
    "GENERATION_AND_LOCATION_FIT",
    "INDEPENDENT_INVALIDATION_TRACEABLE",
)
RELATIONSHIPS = (
    "FRESH_FORMING_UP_ANCHOR",
    "COMPLETED_MACRO_UP_PARENT",
    "MATURE_UP_CAMPAIGN",
    "ACTIVE_BEAR_CONTROL",
    "UNRESOLVED",
)


def build_schema(packet: dict[str, Any], defense: dict[str, Any], defense_id: str) -> dict[str, Any]:
    if packet["review_id"] != defense["review_id"] or packet["as_of"] != defense["as_of"]:
        raise ValueError("packet/defense binding mismatch")
    if defense_id not in {row["candidate_id"] for row in defense["candidate_rows"]}:
        raise ValueError("selected defense not in shortlist")
    candidates = packet["candidate_pool"]
    ids = sorted(row["candidate_id"] for row in candidates)
    if len(set(ids)) != len(ids) or len(ids) < 3:
        raise ValueError("candidate pool invalid/too small")
    refs = _known_refs(packet)
    comparison = {
        "type": "object", "additionalProperties": False,
        "required": ["candidate_id", "control_judgement", "supporting_evidence_refs", "explanation"],
        "properties": {
            "candidate_id": {"type": "string", "enum": ids},
            "control_judgement": {"type": "string", "enum": ["PASS", "FAIL", "UNKNOWN"]},
            "supporting_evidence_refs": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True, "items": {"type": "string", "enum": refs}},
            "explanation": {"type": "string", "minLength": 20, "maxLength": 700},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "review_id", "as_of", "bound_defense_candidate_id", "selected_anchor_candidate_id", "relationship_class", "selected_anchor_atoms", "selected_anchor_evidence_refs", "comparison_rows", "selection_reason", "causal_attestation"],
        "properties": {
            "schema_version": {"type": "string", "const": VERSION},
            "review_id": {"type": "string", "const": packet["review_id"]},
            "as_of": {"type": "string", "const": packet["as_of"]},
            "bound_defense_candidate_id": {"type": "string", "const": defense_id},
            "selected_anchor_candidate_id": {"type": "string", "enum": [*ids, "NONE"]},
            "relationship_class": {"type": "string", "enum": list(RELATIONSHIPS)},
            "selected_anchor_atoms": {
                "type": "object", "additionalProperties": False,
                "required": list(ATOMS),
                "properties": {atom: {"type": "string", "enum": ["PASS", "FAIL", "UNKNOWN"]} for atom in ATOMS},
            },
            "selected_anchor_evidence_refs": {
                "type": "array", "minItems": 1, "maxItems": 12, "uniqueItems": True,
                "items": {"type": "string", "enum": refs},
            },
            "comparison_rows": {"type": "array", "minItems": 3, "maxItems": 8, "items": comparison},
            "selection_reason": {"type": "string", "minLength": 60, "maxLength": 1200},
            "causal_attestation": {
                "type": "object", "additionalProperties": False,
                "required": ["as_of_only", "no_identity", "no_teacher_answer", "no_future_performance", "defense_not_changed", "no_scenario_or_trade_permission"],
                "properties": {key: {"type": "boolean", "const": True} for key in (
                    "as_of_only", "no_identity", "no_teacher_answer", "no_future_performance", "defense_not_changed", "no_scenario_or_trade_permission",
                )},
            },
        },
    }


def validate_and_resolve(
    response: dict[str, Any], *, packet: dict[str, Any], defense: dict[str, Any], defense_id: str,
) -> dict[str, Any]:
    schema = build_schema(packet, defense, defense_id)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda error: list(map(str, error.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    candidates = {row["candidate_id"]: row for row in packet["candidate_pool"]}
    selected_id = response["selected_anchor_candidate_id"]
    relation = response["relationship_class"]
    comparisons = response["comparison_rows"]
    comparison_ids = [row["candidate_id"] for row in comparisons]
    errors = []
    if len(set(comparison_ids)) != len(comparison_ids):
        errors.append("comparison candidate IDs repeated")
    for item in comparisons:
        own_refs = set(candidates[item["candidate_id"]]["source_evidence_refs"])
        if not own_refs.intersection(item["supporting_evidence_refs"]):
            errors.append(f"comparison missing candidate-specific evidence: {item['candidate_id']}")
    if selected_id == "NONE":
        if relation != "UNRESOLVED":
            errors.append("NONE anchor requires UNRESOLVED relationship")
        temporal = None
    else:
        selected = candidates[selected_id]
        if selected_id not in comparison_ids:
            errors.append("selected anchor not explicitly compared")
        if not all(response["selected_anchor_atoms"][atom] == "PASS" for atom in ATOMS):
            errors.append("selected anchor lacks all-PASS atoms")
        if not set(selected["source_evidence_refs"]).intersection(response["selected_anchor_evidence_refs"]):
            errors.append("selected anchor lacks own evidence")
        selected_rows = [row for row in comparisons if row["candidate_id"] == selected_id]
        if len(selected_rows) != 1 or selected_rows[0]["control_judgement"] != "PASS":
            errors.append("selected anchor comparison not PASS")
        different_basis = any(
            candidates[candidate_id]["basis"] != selected["basis"]
            for candidate_id in comparison_ids if candidate_id != selected_id
        )
        if not different_basis:
            errors.append("no cross-basis comparison")
        expected = {
            "FRESH_FORMING_UP_ANCHOR": ("UP", "FORMING"),
            "COMPLETED_MACRO_UP_PARENT": ("UP", "CONFIRMED"),
            "MATURE_UP_CAMPAIGN": ("UP", None),
            "ACTIVE_BEAR_CONTROL": ("DOWN", None),
        }
        if relation not in expected:
            errors.append("selected anchor requires a resolved relationship")
        else:
            direction, status = expected[relation]
            if selected["direction"] != direction or (status is not None and selected["status"] != status):
                errors.append("anchor direction/lifecycle conflicts with relationship")
        defense_row = next(row for row in defense["candidate_rows"] if row["candidate_id"] == defense_id)
        temporal = check_temporal_facts(selected, defense_row, packet["as_of"])
        if temporal["status"] != "LEGAL_AS_OF":
            errors.extend(temporal["errors"])
    if errors:
        return {"status": "INVALID", "errors": errors[:20]}
    return {
        "status": "VALID",
        "resolution_status": "SELECTED" if selected_id != "NONE" else "UNRESOLVED_NO_TRADE",
        "selected_anchor_candidate_id": None if selected_id == "NONE" else selected_id,
        "relationship_class": relation,
        "bound_defense_candidate_id": defense_id,
        "temporal_facts": temporal,
        "trade_permission_granted": False,
    }


def transport_for(packet: dict[str, Any], defense: dict[str, Any], defense_id: str) -> dict[str, Any]:
    return transport_schema(build_schema(packet, defense, defense_id))


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
        raise ValueError("anchor output root must be object")
    return result
