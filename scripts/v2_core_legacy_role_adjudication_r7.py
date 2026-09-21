"""R7 relative-role adjudication for multiple non-equivalent eligible objects.

This is an additional AI perception stage, not a recency or performance
tie-break.  The program resolves only a unique, evidenced direct controller.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from scripts.v2_core_legacy_role_qualification_r7 import _known_refs, transport_schema


VERSION = "v2-core-legacy-role-adjudication-r7-candidate-r1"
PRIORITIES = (
    "DIRECT_ROLE_CONTROLLER",
    "SUBORDINATE_NESTED_STRUCTURE",
    "BROADER_BACKGROUND_STRUCTURE",
    "HISTORICAL_NOT_CURRENT",
    "UNKNOWN",
)


def build_schema(packet: dict[str, Any], shortlist: dict[str, Any], role: str, qualification: dict[str, Any]) -> dict[str, Any]:
    if qualification["status"] != "VALID" or qualification["role"] != role:
        raise ValueError("qualification validation mismatch")
    if qualification["resolution_reason"] != "MULTIPLE_NON_EQUIVALENT_ELIGIBLE_CANDIDATES":
        raise ValueError("adjudication is only for non-equivalent eligible ambiguity")
    ids = qualification["eligible_candidate_ids"]
    if len(ids) < 2:
        raise ValueError("at least two eligible candidates required")
    rows = {row["candidate_id"]: row for row in shortlist["roles"][role]["candidate_rows"]}
    if not set(ids) <= set(rows):
        raise ValueError("eligible candidate outside role shortlist")
    refs = _known_refs(packet)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "review_id", "as_of", "role", "priority_assessments", "causal_attestation"],
        "properties": {
            "schema_version": {"type": "string", "const": VERSION},
            "review_id": {"type": "string", "const": packet["review_id"]},
            "as_of": {"type": "string", "const": packet["as_of"]},
            "role": {"type": "string", "const": role},
            "priority_assessments": {
                "type": "array", "minItems": len(ids), "maxItems": len(ids),
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["candidate_id", "evidence_option_id", "priority", "supporting_evidence_refs", "comparative_reason"],
                    "properties": {
                        "candidate_id": {"type": "string", "enum": ids},
                        "evidence_option_id": {"type": "string", "enum": sorted(rows[candidate_id]["evidence_option_id"] for candidate_id in ids)},
                        "priority": {"type": "string", "enum": list(PRIORITIES)},
                        "supporting_evidence_refs": {
                            "type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True,
                            "items": {"type": "string", "enum": refs},
                        },
                        "comparative_reason": {"type": "string", "minLength": 20, "maxLength": 600},
                    },
                },
            },
            "causal_attestation": {
                "type": "object", "additionalProperties": False,
                "required": ["as_of_only", "no_identity", "no_legacy_answer", "no_future_performance", "eligible_candidates_only", "no_scenario_or_trade_permission"],
                "properties": {key: {"type": "boolean", "const": True} for key in (
                    "as_of_only", "no_identity", "no_legacy_answer", "no_future_performance",
                    "eligible_candidates_only", "no_scenario_or_trade_permission",
                )},
            },
        },
    }


def validate_and_resolve(
    response: dict[str, Any],
    *,
    packet: dict[str, Any],
    shortlist: dict[str, Any],
    role: str,
    qualification: dict[str, Any],
) -> dict[str, Any]:
    schema = build_schema(packet, shortlist, role, qualification)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda e: list(map(str, e.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    rows = {row["candidate_id"]: row for row in shortlist["roles"][role]["candidate_rows"]}
    eligible = set(qualification["eligible_candidate_ids"])
    seen: set[str] = set()
    direct: list[str] = []
    errors: list[str] = []
    for assessment in response["priority_assessments"]:
        candidate_id = assessment["candidate_id"]
        if candidate_id in seen:
            errors.append(f"duplicate adjudication candidate: {candidate_id}")
        seen.add(candidate_id)
        row = rows[candidate_id]
        if assessment["evidence_option_id"] != row["evidence_option_id"]:
            errors.append(f"candidate/evidence option mismatch: {candidate_id}")
        if not set(assessment["supporting_evidence_refs"]).intersection(row["source_evidence_refs"]):
            errors.append(f"candidate-specific evidence missing: {candidate_id}")
        if assessment["priority"] == "DIRECT_ROLE_CONTROLLER":
            direct.append(candidate_id)
    if seen != eligible:
        errors.append(f"eligible set differs: missing={sorted(eligible-seen)}")
    if errors:
        return {"status": "INVALID", "errors": errors}
    selected = direct[0] if len(direct) == 1 else None
    return {
        "status": "VALID",
        "role": role,
        "role_status": "SELECTED" if selected else "UNRESOLVED",
        "resolution_reason": "UNIQUE_AI_EVIDENCED_DIRECT_CONTROLLER" if selected else "DIRECT_CONTROLLER_STILL_AMBIGUOUS",
        "selected_candidate_id": selected,
        "selected_evidence_option_id": rows[selected]["evidence_option_id"] if selected else None,
        "trade_permission_granted": False,
    }


def transport_for(packet: dict[str, Any], shortlist: dict[str, Any], role: str, qualification: dict[str, Any]) -> dict[str, Any]:
    return transport_schema(build_schema(packet, shortlist, role, qualification))
