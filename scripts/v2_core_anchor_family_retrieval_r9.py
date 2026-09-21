"""R9 teacher-blind, per-basis-family anchor representative contract.

Every AS-OF basis/direction/lifecycle group returns one candidate, including
families later judged irrelevant.  This is retrieval, not final work-anchor
selection, scenario routing or trade permission.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from scripts.v2_core_legacy_role_qualification_r7 import _known_refs, transport_schema


VERSION = "v2-core-anchor-family-retrieval-r9-candidate-r1"


def build_schema(packet: dict[str, Any], groups: dict[str, Any], defense_id: str) -> dict[str, Any]:
    if packet["review_id"] != groups["review_id"] or packet["as_of"] != groups["as_of"]:
        raise ValueError("R9 packet/group binding mismatch")
    if len(groups["groups"]) != groups["group_count"] or not groups["groups"]:
        raise ValueError("R9 group count mismatch")
    refs = _known_refs(packet)
    group_properties = {}
    for group in groups["groups"]:
        ids = group["candidate_ids"]
        if len(ids) != group["candidate_count"] or not ids:
            raise ValueError("R9 group candidate count mismatch")
        group_properties[group["group_id"]] = {
            "type": "object", "additionalProperties": False,
            "required": ["representative_candidate_id", "compared_candidate_ids", "relevance_judgement", "supporting_evidence_refs", "selection_reason"],
            "properties": {
                "representative_candidate_id": {"type": "string", "enum": ids},
                "compared_candidate_ids": {
                    "type": "array", "minItems": min(2, len(ids)), "maxItems": min(6, len(ids)),
                    "uniqueItems": True, "items": {"type": "string", "enum": ids},
                },
                "relevance_judgement": {"type": "string", "enum": ["PASS", "FAIL", "UNKNOWN"]},
                "supporting_evidence_refs": {
                    "type": "array", "minItems": 1, "maxItems": 10, "uniqueItems": True,
                    "items": {"type": "string", "enum": refs},
                },
                "selection_reason": {"type": "string", "minLength": 40, "maxLength": 850},
            },
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "review_id", "as_of", "bound_defense_candidate_id", "group_representatives", "retrieval_attestation"],
        "properties": {
            "schema_version": {"type": "string", "const": VERSION},
            "review_id": {"type": "string", "const": packet["review_id"]},
            "as_of": {"type": "string", "const": packet["as_of"]},
            "bound_defense_candidate_id": {"type": "string", "const": defense_id},
            "group_representatives": {
                "type": "object", "additionalProperties": False,
                "required": sorted(group_properties), "properties": group_properties,
            },
            "retrieval_attestation": {
                "type": "object", "additionalProperties": False,
                "required": ["as_of_only", "no_identity", "no_teacher_answer", "no_future_performance", "defense_not_changed", "no_final_anchor_or_trade_permission"],
                "properties": {key: {"type": "boolean", "const": True} for key in (
                    "as_of_only", "no_identity", "no_teacher_answer", "no_future_performance", "defense_not_changed", "no_final_anchor_or_trade_permission",
                )},
            },
        },
    }


def validate_and_retrieve(
    response: dict[str, Any], *, packet: dict[str, Any], groups: dict[str, Any], defense_id: str,
) -> dict[str, Any]:
    schema = build_schema(packet, groups, defense_id)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda error: list(map(str, error.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    candidates = {row["candidate_id"]: row for row in packet["candidate_pool"]}
    errors = []
    selected = {}
    for group in groups["groups"]:
        group_id = group["group_id"]
        item = response["group_representatives"][group_id]
        candidate_id = item["representative_candidate_id"]
        if candidate_id not in item["compared_candidate_ids"]:
            errors.append(f"representative not compared: {group_id}")
        if not set(candidates[candidate_id]["source_evidence_refs"]).intersection(item["supporting_evidence_refs"]):
            errors.append(f"representative missing own evidence: {group_id}")
        if any(compared_id not in candidates for compared_id in item["compared_candidate_ids"]):
            errors.append(f"unknown compared candidate: {group_id}")
        selected[group_id] = {
            "candidate_id": candidate_id,
            "basis": group["basis"],
            "direction": group["direction"],
            "status": group["status"],
            "relevance_judgement": item["relevance_judgement"],
        }
    if errors:
        return {"status": "INVALID", "errors": errors[:20]}
    return {
        "status": "VALID",
        "group_count": len(selected),
        "group_representatives": selected,
        "bound_defense_candidate_id": defense_id,
        "final_anchor_selected": False,
        "trade_permission_granted": False,
    }


def transport_for(packet: dict[str, Any], groups: dict[str, Any], defense_id: str) -> dict[str, Any]:
    return transport_schema(build_schema(packet, groups, defense_id))


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
        raise ValueError("R9 retrieval output root must be object")
    return result
