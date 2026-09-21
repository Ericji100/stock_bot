"""Candidate R7 role qualification contract and deterministic role resolution.

This stage has no scenario or trade permission.  The model assesses every
teacher-blind candidate independently; the program only validates bindings
and reduces those atoms under the R7 equivalence/ambiguity rules.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator


VERSION = "v2-core-legacy-role-qualification-r7-candidate-r1"
ROLE_ATOMS = (
    "ROLE_DIRECTION_AND_LIFECYCLE_FIT",
    "DIRECT_CAUSAL_LINK_TO_CURRENT_EPISODE",
    "BOUNDARY_AND_SCALE_FIT",
    "PRICE_STRUCTURE_CORROBORATION",
)
MANDATORY_ROLE = "CURRENT_EPISODE_UP"
JUDGEMENTS = ("PASS", "FAIL", "UNKNOWN")


def _known_refs(packet: dict[str, Any]) -> list[str]:
    refs = {
        str(ref)
        for option in packet["candidate_evidence_options"]
        for ref in option["source_evidence_refs"]
    }
    refs.update(str(row["ref"]) for row in packet.get("proxy_evidence", []))
    return sorted(refs)


def _role_rows(shortlist: dict[str, Any], role: str) -> list[dict[str, Any]]:
    if role not in shortlist["roles"]:
        raise ValueError(f"unknown R7 role: {role}")
    rows = shortlist["roles"][role]["candidate_rows"]
    if len(rows) != shortlist["roles"][role]["shortlist_count"]:
        raise ValueError("shortlist count mismatch")
    if len({row["candidate_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate shortlist candidate")
    return rows


def build_schema(packet: dict[str, Any], shortlist: dict[str, Any], role: str) -> dict[str, Any]:
    if packet["review_id"] != shortlist["review_id"] or packet["as_of"] != shortlist["as_of"]:
        raise ValueError("packet/shortlist identity mismatch")
    rows = _role_rows(shortlist, role)
    candidate_ids = sorted(row["candidate_id"] for row in rows)
    option_ids = sorted(row["evidence_option_id"] for row in rows)
    refs = _known_refs(packet)
    if not rows or not refs:
        raise ValueError("empty candidate or evidence universe")
    judgement = {"type": "string", "enum": list(JUDGEMENTS)}
    evidence_refs = {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "uniqueItems": True,
        "items": {"type": "string", "enum": refs},
    }
    assessment = {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidate_id", "evidence_option_id", "atoms", "supporting_evidence_refs", "explanation"],
        "properties": {
            "candidate_id": {"type": "string", "enum": candidate_ids},
            "evidence_option_id": {"type": "string", "enum": option_ids},
            "atoms": {
                "type": "object",
                "additionalProperties": False,
                "required": list(ROLE_ATOMS),
                "properties": {atom: judgement for atom in ROLE_ATOMS},
            },
            "supporting_evidence_refs": evidence_refs,
            "explanation": {"type": "string", "minLength": 12, "maxLength": 420},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version", "review_id", "as_of", "role", "candidate_assessments",
            "non_applicability", "cross_basis_comparison", "causal_attestation",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": VERSION},
            "review_id": {"type": "string", "const": packet["review_id"]},
            "as_of": {"type": "string", "const": packet["as_of"]},
            "role": {"type": "string", "const": role},
            "candidate_assessments": {
                "type": "array", "minItems": len(rows), "maxItems": len(rows), "items": assessment,
            },
            "non_applicability": {
                "type": "object",
                "additionalProperties": False,
                "required": ["judgement", "supporting_evidence_refs", "explanation"],
                "properties": {
                    "judgement": judgement,
                    "supporting_evidence_refs": evidence_refs,
                    "explanation": {"type": "string", "minLength": 12, "maxLength": 420},
                },
            },
            "cross_basis_comparison": {"type": "string", "minLength": 12, "maxLength": 700},
            "causal_attestation": {
                "type": "object", "additionalProperties": False,
                "required": ["as_of_only", "no_identity", "no_legacy_answer", "no_future_performance", "fixed_candidates_only", "no_scenario_or_trade_permission"],
                "properties": {key: {"type": "boolean", "const": True} for key in (
                    "as_of_only", "no_identity", "no_legacy_answer", "no_future_performance",
                    "fixed_candidates_only", "no_scenario_or_trade_permission",
                )},
            },
        },
    }


def transport_schema(local_schema: dict[str, Any]) -> dict[str, Any]:
    """Make the tested local schema compatible with strict structured output.

    Only local duplicate detection relies on uniqueItems; no semantic rule is
    removed.  The transport contains no references or unsupported branches.
    """
    transport = json.loads(json.dumps(local_schema))

    def clean(value: Any) -> Any:
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value
        return {
            key: clean(child)
            for key, child in value.items()
            if key not in {"$schema", "uniqueItems"}
        }

    transport = clean(transport)
    Draft202012Validator.check_schema(transport)
    if "$ref" in json.dumps(transport):
        raise ValueError("R7 transport schema must be fully inline")
    return transport


def validate_and_resolve(
    response: dict[str, Any],
    *,
    packet: dict[str, Any],
    shortlist: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    schema = build_schema(packet, shortlist, role)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda e: list(map(str, e.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}

    rows = _role_rows(shortlist, role)
    expected = {row["candidate_id"]: row for row in rows}
    known = set(_known_refs(packet))
    seen: set[str] = set()
    errors: list[str] = []
    eligible: list[str] = []
    for assessment in response["candidate_assessments"]:
        candidate_id = assessment["candidate_id"]
        if candidate_id in seen:
            errors.append(f"duplicate assessment: {candidate_id}")
        seen.add(candidate_id)
        row = expected[candidate_id]
        if assessment["evidence_option_id"] != row["evidence_option_id"]:
            errors.append(f"candidate/evidence option mismatch: {candidate_id}")
        refs = set(assessment["supporting_evidence_refs"])
        if not refs <= known or not refs.intersection(row["source_evidence_refs"]):
            errors.append(f"candidate-specific evidence missing: {candidate_id}")
        if all(assessment["atoms"][atom] == "PASS" for atom in ROLE_ATOMS):
            eligible.append(candidate_id)
    missing = set(expected) - seen
    if missing:
        errors.append(f"missing assessments: {sorted(missing)}")
    if role == MANDATORY_ROLE and response["non_applicability"]["judgement"] == "PASS":
        errors.append("CURRENT_EPISODE_UP cannot be NOT_APPLICABLE")
    if eligible and response["non_applicability"]["judgement"] == "PASS":
        errors.append("eligible candidate contradicts positive non-applicability")
    if errors:
        return {"status": "INVALID", "errors": errors}

    groups: list[set[str]] = []
    for group in shortlist["roles"][role]["objective_equivalence_groups"]:
        ids = set(group["candidate_ids"])
        if not ids <= set(expected):
            return {"status": "INVALID", "errors": ["equivalence group outside shortlist"]}
        groups.append(ids)

    eligible_set = set(eligible)
    if not eligible:
        role_status = "NOT_APPLICABLE" if response["non_applicability"]["judgement"] == "PASS" else "UNRESOLVED"
        selected = None
        reason = "NO_ELIGIBLE_CANDIDATE"
    elif len(eligible) == 1:
        role_status = "SELECTED"
        selected = eligible[0]
        reason = "UNIQUE_ELIGIBLE_CANDIDATE"
    elif any(eligible_set <= group for group in groups):
        role_status = "SELECTED"
        selected = sorted(eligible)[0]
        reason = "OBJECTIVELY_EQUIVALENT_ELIGIBLE_CANDIDATES"
    else:
        role_status = "UNRESOLVED"
        selected = None
        reason = "MULTIPLE_NON_EQUIVALENT_ELIGIBLE_CANDIDATES"
    return {
        "status": "VALID",
        "role": role,
        "role_status": role_status,
        "resolution_reason": reason,
        "eligible_candidate_ids": sorted(eligible),
        "selected_candidate_id": selected,
        "selected_evidence_option_id": expected[selected]["evidence_option_id"] if selected else None,
        "trade_permission_granted": False,
    }
