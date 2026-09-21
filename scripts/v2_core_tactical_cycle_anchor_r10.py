"""R10 source-bound tactical-cycle role, distinct from broad price campaign.

R9B's broad working anchor is immutable.  This stage may select a nested
indicator/price cycle to interpret the present Taiji generation, but cannot
rename it as the campaign parent, replace the trade defense, or authorize a
trade.  All options come from R9A's teacher-blind frozen family retrieval.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from scripts.v2_core_legacy_role_qualification_r7 import _known_refs, transport_schema


VERSION = "v2-core-tactical-cycle-anchor-r10-candidate-r1"
STATUS_BY_SCENARIO = {
    "FRESH_Q1_EXPANSION": "FORMING",
    "MACRO_COPY_RESONANCE": "CONFIRMED",
}
ATOMS = (
    "CURRENT_CYCLE_FIT",
    "NESTED_OR_EQUIVALENT_TO_BROAD_CAMPAIGN",
    "INDEPENDENT_PRICE_CORROBORATION",
    "GENERATION_CLOCK_USEFULNESS",
)


def candidate_options(packet: dict[str, Any], hierarchy: dict[str, Any], retrieval: dict[str, Any]) -> dict[str, dict[str, Any]]:
    scenario = {
        "FRESH_FORMING_UP_ANCHOR": "FRESH_Q1_EXPANSION",
        "COMPLETED_MACRO_UP_PARENT": "MACRO_COPY_RESONANCE",
    }.get(hierarchy["relationship_class"])
    if scenario not in STATUS_BY_SCENARIO:
        raise ValueError("R10 pilot covers only FRESH and MACRO")
    pool = {row["candidate_id"]: row for row in packet["candidate_pool"]}
    broad_id = hierarchy["selected_working_anchor_id"]
    if broad_id not in pool:
        raise ValueError("R10 broad anchor missing")
    broad = pool[broad_id]
    result = {}
    for family in retrieval["group_representatives"].values():
        candidate = pool[family["candidate_id"]]
        if candidate["direction"] != "UP" or candidate["status"] != STATUS_BY_SCENARIO[scenario]:
            continue
        if candidate["start_date"] < broad["start_date"] or candidate["start_date"] > packet["as_of"]:
            continue
        if candidate["confirmed_end_date"] and candidate["confirmed_end_date"] > packet["as_of"]:
            continue
        result[candidate["candidate_id"]] = {
            "candidate": candidate,
            "family_relevance_judgement": family["relevance_judgement"],
        }
    if broad_id not in result:
        raise ValueError("R10 broad anchor absent from tactical comparison options")
    return dict(sorted(result.items()))


def build_schema(packet: dict[str, Any], hierarchy: dict[str, Any], retrieval: dict[str, Any], defense_id: str) -> dict[str, Any]:
    options = candidate_options(packet, hierarchy, retrieval)
    ids = list(options)
    refs = _known_refs(packet)
    assessment = {
        "type": "object", "additionalProperties": False,
        "required": ["atoms", "supporting_evidence_refs", "explanation"],
        "properties": {
            "atoms": {
                "type": "object", "additionalProperties": False,
                "required": list(ATOMS),
                "properties": {atom: {"type": "string", "enum": ["PASS", "FAIL", "UNKNOWN"]} for atom in ATOMS},
            },
            "supporting_evidence_refs": {
                "type": "array", "minItems": 1, "maxItems": 12,
                "items": {"type": "string", "enum": refs},
            },
            "explanation": {"type": "string", "minLength": 40, "maxLength": 800},
        },
    }
    scenario = "FRESH_Q1_EXPANSION" if hierarchy["relationship_class"] == "FRESH_FORMING_UP_ANCHOR" else "MACRO_COPY_RESONANCE"
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "review_id", "as_of", "bound_defense_candidate_id", "bound_broad_anchor_id", "program_scenario", "option_assessments", "selected_tactical_cycle_anchor_id", "broad_vs_tactical_reason", "causal_attestation"],
        "properties": {
            "schema_version": {"type": "string", "const": VERSION},
            "review_id": {"type": "string", "const": packet["review_id"]},
            "as_of": {"type": "string", "const": packet["as_of"]},
            "bound_defense_candidate_id": {"type": "string", "const": defense_id},
            "bound_broad_anchor_id": {"type": "string", "const": hierarchy["selected_working_anchor_id"]},
            "program_scenario": {"type": "string", "const": scenario},
            "option_assessments": {
                "type": "object", "additionalProperties": False,
                "required": ids, "properties": {candidate_id: assessment for candidate_id in ids},
            },
            "selected_tactical_cycle_anchor_id": {"type": "string", "enum": [*ids, "NONE"]},
            "broad_vs_tactical_reason": {"type": "string", "minLength": 100, "maxLength": 1800},
            "causal_attestation": {
                "type": "object", "additionalProperties": False,
                "required": ["as_of_only", "no_identity", "no_teacher_answer", "no_future_performance", "broad_anchor_unchanged", "episode_defense_unchanged", "no_trade_permission"],
                "properties": {key: {"type": "boolean", "const": True} for key in (
                    "as_of_only", "no_identity", "no_teacher_answer", "no_future_performance",
                    "broad_anchor_unchanged", "episode_defense_unchanged", "no_trade_permission",
                )},
            },
        },
    }


def validate_and_select(
    response: dict[str, Any], *, packet: dict[str, Any], hierarchy: dict[str, Any],
    retrieval: dict[str, Any], defense_id: str,
) -> dict[str, Any]:
    schema = build_schema(packet, hierarchy, retrieval, defense_id)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda error: list(map(str, error.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    options = candidate_options(packet, hierarchy, retrieval)
    errors = []
    for candidate_id, assessment in response["option_assessments"].items():
        candidate_refs = set(options[candidate_id]["candidate"]["source_evidence_refs"])
        if not candidate_refs.intersection(assessment["supporting_evidence_refs"]):
            errors.append(f"candidate-specific evidence missing: {candidate_id}")
    selected = response["selected_tactical_cycle_anchor_id"]
    if selected != "NONE":
        chosen = response["option_assessments"][selected]
        if any(chosen["atoms"][atom] != "PASS" for atom in ATOMS):
            errors.append("selected tactical cycle requires all four PASS role atoms")
        if not any(ref.startswith(("PRICE:", "PIVOT:")) for ref in chosen["supporting_evidence_refs"]):
            errors.append("selected tactical cycle lacks independent price/pivot reference")
    if errors:
        return {"status": "INVALID", "errors": errors[:20]}
    return {
        "status": "VALID",
        "resolution_status": "SELECTED" if selected != "NONE" else "UNRESOLVED_NO_TRADE",
        "selected_tactical_cycle_anchor_id": None if selected == "NONE" else selected,
        "bound_broad_anchor_id": hierarchy["selected_working_anchor_id"],
        "bound_defense_candidate_id": defense_id,
        "program_scenario": response["program_scenario"],
        "trade_permission_granted": False,
    }


def transport_for(packet: dict[str, Any], hierarchy: dict[str, Any], retrieval: dict[str, Any], defense_id: str) -> dict[str, Any]:
    return transport_schema(build_schema(packet, hierarchy, retrieval, defense_id))


def parse_raw_without_duplicate_keys(raw_bytes: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(raw_bytes.decode("utf-8-sig"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("R10 tactical anchor output root must be object")
    return value
