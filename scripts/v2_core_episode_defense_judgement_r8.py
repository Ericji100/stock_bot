"""R8 teacher-blind trade-episode defense perception contract.

AI selects a causal structural defense among fixed AS-OF pivots.  The program
validates candidate binding and evidence, but this stage never authorizes a
trade or chooses a course scenario.  Candidate work is not yet frozen.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from scripts.v2_core_legacy_role_qualification_r7 import _known_refs, transport_schema


VERSION = "v2-core-episode-defense-judgement-r8-candidate-r1"
ATOMS = (
    "CONFIRMED_AS_OF_SIGNAL_CLOSE",
    "CAUSES_CURRENT_ENTRY_INVALIDATION",
    "SAME_SCALE_CONTROL_NOT_INTERNAL_NOISE",
    "CAUSALLY_LINKED_TO_CURRENT_BREAK",
)
JUDGEMENTS = ("PASS", "FAIL", "UNKNOWN")


def build_schema(packet: dict[str, Any], shortlist: dict[str, Any]) -> dict[str, Any]:
    if packet["review_id"] != shortlist["review_id"] or packet["as_of"] != shortlist["as_of"]:
        raise ValueError("defense packet/shortlist binding mismatch")
    rows = shortlist["candidate_rows"]
    if len(rows) != shortlist["candidate_count"] or not rows:
        raise ValueError("defense shortlist empty/count mismatch")
    ids = [row["candidate_id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate defense candidate ID")
    refs = sorted(set(_known_refs(packet)) | {row["evidence_ref"] for row in rows})
    atoms = {
        atom: {"type": "string", "enum": list(JUDGEMENTS)}
        for atom in ATOMS
    }
    assessments = {}
    for row in rows:
        assessments[row["candidate_id"]] = {
            "type": "object", "additionalProperties": False,
            "required": ["atoms", "supporting_evidence_refs", "explanation"],
            "properties": {
                "atoms": {"type": "object", "additionalProperties": False, "required": list(ATOMS), "properties": atoms},
                "supporting_evidence_refs": {
                    "type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True,
                    "items": {"type": "string", "enum": refs},
                },
                "explanation": {"type": "string", "minLength": 20, "maxLength": 700},
            },
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "review_id", "as_of", "candidate_assessments", "selected_candidate_id", "compared_candidate_ids", "selection_reason", "causal_attestation"],
        "properties": {
            "schema_version": {"type": "string", "const": VERSION},
            "review_id": {"type": "string", "const": packet["review_id"]},
            "as_of": {"type": "string", "const": packet["as_of"]},
            "candidate_assessments": {"type": "object", "additionalProperties": False, "required": sorted(ids), "properties": assessments},
            "selected_candidate_id": {"type": "string", "enum": [*sorted(ids), "NONE"]},
            "compared_candidate_ids": {
                "type": "array", "minItems": min(2, len(ids)), "maxItems": min(8, len(ids)),
                "uniqueItems": True, "items": {"type": "string", "enum": sorted(ids)},
            },
            "selection_reason": {"type": "string", "minLength": 40, "maxLength": 1000},
            "causal_attestation": {
                "type": "object", "additionalProperties": False,
                "required": ["as_of_only", "no_identity", "no_teacher_answer", "no_future_performance", "no_scenario_or_trade_permission"],
                "properties": {key: {"type": "boolean", "const": True} for key in (
                    "as_of_only", "no_identity", "no_teacher_answer", "no_future_performance", "no_scenario_or_trade_permission",
                )},
            },
        },
    }


def validate_and_resolve(response: dict[str, Any], *, packet: dict[str, Any], shortlist: dict[str, Any]) -> dict[str, Any]:
    schema = build_schema(packet, shortlist)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda error: list(map(str, error.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    rows = {row["candidate_id"]: row for row in shortlist["candidate_rows"]}
    errors = []
    eligible = []
    for candidate_id, assessment in response["candidate_assessments"].items():
        if rows[candidate_id]["evidence_ref"] not in assessment["supporting_evidence_refs"]:
            errors.append(f"candidate-specific pivot evidence missing: {candidate_id}")
        if assessment["atoms"]["CONFIRMED_AS_OF_SIGNAL_CLOSE"] != "PASS":
            errors.append(f"AS-OF confirmed candidate not recognized: {candidate_id}")
        if all(assessment["atoms"][atom] == "PASS" for atom in ATOMS):
            eligible.append(candidate_id)
    selected = response["selected_candidate_id"]
    if selected != "NONE" and selected not in eligible:
        errors.append("selected defense lacks all-PASS atoms")
    if selected != "NONE" and selected not in response["compared_candidate_ids"]:
        errors.append("selected defense absent from explicit comparison")
    if errors:
        return {"status": "INVALID", "errors": errors[:20]}
    return {
        "status": "VALID",
        "resolution_status": "SELECTED" if selected != "NONE" else "UNRESOLVED_NO_TRADE",
        "selected_candidate_id": None if selected == "NONE" else selected,
        "eligible_candidate_ids": sorted(eligible),
        "trade_permission_granted": False,
    }


def transport_for(packet: dict[str, Any], shortlist: dict[str, Any]) -> dict[str, Any]:
    return transport_schema(build_schema(packet, shortlist))


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
        raise ValueError("defense output root must be object")
    return result
