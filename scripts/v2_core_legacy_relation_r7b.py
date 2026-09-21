"""R7B fixed-object relationship atoms and deterministic scenario routing.

AI judges causal relationships among five previously frozen AS-OF objects.
The program validates chronology/bindings and applies a fixed truth table;
neither this stage nor the program grants trade permission.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator

from scripts.v2_core_legacy_role_qualification_r7 import _known_refs, transport_schema
from scripts.v2_core_legacy_role_shortlist_r7 import canonical_bytes


VERSION = "v2-core-legacy-relation-r7b-candidate-r1"
ATOMS = (
    "DOWN_CONTROLS_CURRENT_EPISODE",
    "CHALLENGER_REPLACED_DOWN_CONTROL",
    "IMMEDIATE_PARENT_CONTROLS_CURRENT_EPISODE",
    "CURRENT_EPISODE_IS_FIRST_COPY_OF_IMMEDIATE_PARENT",
    "MATURE_CAMPAIGN_CONTROLS_CURRENT_EPISODE",
    "SAME_LINEAGE_SUCCESS_PREEXISTS_CURRENT_EPISODE",
    "SAME_LINEAGE_LONG_HABIT_PREEXISTS_CURRENT_EPISODE",
    "CURRENT_EPISODE_IS_FRESH_FORMING_ANCHOR",
)
DOMINANT_RELATIONS = (
    "BEAR_CONTROL",
    "FRESH_ANCHOR_CONTROL",
    "DIRECT_PARENT_COPY_CONTROL",
    "MATURE_CAMPAIGN_PULLBACK_CONTROL",
    "UNKNOWN",
)
ROUTE_BY_DOMINANT = {
    "BEAR_CONTROL": "BEAR_REVERSAL_LEFT_RIGHT",
    "FRESH_ANCHOR_CONTROL": "FRESH_Q1_EXPANSION",
    "DIRECT_PARENT_COPY_CONTROL": "MACRO_COPY_RESONANCE",
    "MATURE_CAMPAIGN_PULLBACK_CONTROL": "MATURE_TREND_PULLBACK",
}
WORKING_ROLE_BY_ROUTE = {
    "BEAR_REVERSAL_LEFT_RIGHT": "ACTIVE_DOWN_CONTROLLER",
    "FRESH_Q1_EXPANSION": "UP_CONTROL_CHALLENGER",
    "MACRO_COPY_RESONANCE": "IMMEDIATE_COMPLETED_UP_PARENT",
    "MATURE_TREND_PULLBACK": "CONTROLLING_MATURE_CAMPAIGN",
}


def role_fingerprint(report: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes({
        "review_id": report["review_id"],
        "as_of": report["as_of"],
        "role_results": report["role_results"],
        "selected_role_objects": report["selected_role_objects"],
    })).hexdigest()


def _forced_fail_atoms(report: dict[str, Any]) -> set[str]:
    selected = report["selected_role_objects"]
    forced: set[str] = set()
    if selected["ACTIVE_DOWN_CONTROLLER"] is None:
        forced.add("DOWN_CONTROLS_CURRENT_EPISODE")
    if selected["UP_CONTROL_CHALLENGER"] is None:
        forced.update(("CHALLENGER_REPLACED_DOWN_CONTROL", "CURRENT_EPISODE_IS_FRESH_FORMING_ANCHOR"))
    if selected["IMMEDIATE_COMPLETED_UP_PARENT"] is None:
        forced.update(("IMMEDIATE_PARENT_CONTROLS_CURRENT_EPISODE", "CURRENT_EPISODE_IS_FIRST_COPY_OF_IMMEDIATE_PARENT"))
    if selected["CONTROLLING_MATURE_CAMPAIGN"] is None:
        forced.update((
            "MATURE_CAMPAIGN_CONTROLS_CURRENT_EPISODE",
            "SAME_LINEAGE_SUCCESS_PREEXISTS_CURRENT_EPISODE",
            "SAME_LINEAGE_LONG_HABIT_PREEXISTS_CURRENT_EPISODE",
        ))
    return forced


def build_schema(packet: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    if report["status"] != "CROSS_ROLE_PRELIMINARY_READY" or report["review_id"] != packet["review_id"]:
        raise ValueError("R7B role preflight not ready for relationship judgement")
    if report["selected_role_objects"]["CURRENT_EPISODE_UP"] is None:
        raise ValueError("current episode unresolved")
    refs = _known_refs(packet)
    candidates = sorted(row["candidate_id"] for row in packet["candidate_pool"])
    forced = _forced_fail_atoms(report)
    ref_array = {
        "type": "array", "minItems": 1, "maxItems": 10, "uniqueItems": True,
        "items": {"type": "string", "enum": refs},
    }
    atom_properties = {}
    for name in ATOMS:
        atom_properties[name] = {
            "type": "object", "additionalProperties": False,
            "required": ["judgement", "supporting_evidence_refs", "explanation"],
            "properties": {
                "judgement": {"type": "string", "const": "FAIL"} if name in forced else {"type": "string", "enum": ["PASS", "FAIL", "UNKNOWN"]},
                "supporting_evidence_refs": ref_array,
                "explanation": {"type": "string", "minLength": 20, "maxLength": 700},
            },
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "review_id", "as_of", "bound_roles_sha256", "relationship_atoms", "dominant_relation", "mature_lineage_proof", "causal_attestation"],
        "properties": {
            "schema_version": {"type": "string", "const": VERSION},
            "review_id": {"type": "string", "const": packet["review_id"]},
            "as_of": {"type": "string", "const": packet["as_of"]},
            "bound_roles_sha256": {"type": "string", "const": role_fingerprint(report)},
            "relationship_atoms": {
                "type": "object", "additionalProperties": False,
                "required": list(ATOMS), "properties": atom_properties,
            },
            "dominant_relation": {
                "type": "object", "additionalProperties": False,
                "required": ["value", "supporting_evidence_refs", "explanation"],
                "properties": {
                    "value": {"type": "string", "enum": list(DOMINANT_RELATIONS)},
                    "supporting_evidence_refs": ref_array,
                    "explanation": {"type": "string", "minLength": 20, "maxLength": 900},
                },
            },
            "mature_lineage_proof": {
                "type": "object", "additionalProperties": False,
                "required": ["parent_up_candidate_id", "correction_down_candidate_id", "successful_copy_up_candidate_id", "supporting_evidence_refs", "explanation"],
                "properties": {
                    "parent_up_candidate_id": {"type": "string", "enum": [*candidates, "NONE"]},
                    "correction_down_candidate_id": {"type": "string", "enum": [*candidates, "NONE"]},
                    "successful_copy_up_candidate_id": {"type": "string", "enum": [*candidates, "NONE"]},
                    "supporting_evidence_refs": ref_array,
                    "explanation": {"type": "string", "minLength": 20, "maxLength": 700},
                },
            },
            "causal_attestation": {
                "type": "object", "additionalProperties": False,
                "required": ["as_of_only", "no_identity", "no_legacy_answer", "no_future_performance", "frozen_roles_only", "no_scenario_or_trade_permission"],
                "properties": {key: {"type": "boolean", "const": True} for key in (
                    "as_of_only", "no_identity", "no_legacy_answer", "no_future_performance",
                    "frozen_roles_only", "no_scenario_or_trade_permission",
                )},
            },
        },
    }


def _mature_proof_errors(response: dict[str, Any], packet: dict[str, Any], report: dict[str, Any]) -> list[str]:
    atoms = response["relationship_atoms"]
    proof = response["mature_lineage_proof"]
    names = ("parent_up_candidate_id", "correction_down_candidate_id", "successful_copy_up_candidate_id")
    ids = [proof[name] for name in names]
    mature_pass = atoms["SAME_LINEAGE_SUCCESS_PREEXISTS_CURRENT_EPISODE"]["judgement"] == "PASS"
    if not mature_pass:
        return [] if ids == ["NONE"] * 3 else ["non-PASS mature lineage must use NONE candidate IDs"]
    if "NONE" in ids or len(set(ids)) != 3:
        return ["mature PASS requires three distinct lineage candidates"]
    candidates = {row["candidate_id"]: row for row in packet["candidate_pool"]}
    parent, correction, copy = (candidates[candidate_id] for candidate_id in ids)
    episode = report["selected_role_objects"]["CURRENT_EPISODE_UP"]
    assert episode is not None
    errors = []
    for item, direction, label in ((parent, "UP", "parent"), (correction, "DOWN", "correction"), (copy, "UP", "copy")):
        if item["direction"] != direction or item["status"] != "CONFIRMED" or not item["confirmed_end_date"]:
            errors.append(f"mature proof {label} must be completed {direction}")
        if not set(proof["supporting_evidence_refs"]).intersection(item["source_evidence_refs"]):
            errors.append(f"mature proof {label} lacks candidate-specific evidence")
    if not (parent["start_date"] < correction["start_date"] < copy["start_date"] < episode["start_date"]):
        errors.append("mature proof candidate starts not in parent-correction-copy-episode order")
    if copy["confirmed_end_date"] >= episode["start_date"]:
        errors.append("mature successful copy must complete before current episode")
    return errors


def validate_and_route(response: dict[str, Any], *, packet: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    schema = build_schema(packet, report)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda e: list(map(str, e.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    atoms = {name: response["relationship_atoms"][name]["judgement"] for name in ATOMS}
    selected = report["selected_role_objects"]
    errors = _mature_proof_errors(response, packet, report)
    if atoms["CURRENT_EPISODE_IS_FIRST_COPY_OF_IMMEDIATE_PARENT"] == "PASS" and atoms["IMMEDIATE_PARENT_CONTROLS_CURRENT_EPISODE"] != "PASS":
        errors.append("first copy PASS requires direct parent control PASS")
    if atoms["SAME_LINEAGE_LONG_HABIT_PREEXISTS_CURRENT_EPISODE"] == "PASS" and atoms["SAME_LINEAGE_SUCCESS_PREEXISTS_CURRENT_EPISODE"] != "PASS":
        errors.append("mature long habit PASS requires lineage success PASS")
    if atoms["SAME_LINEAGE_SUCCESS_PREEXISTS_CURRENT_EPISODE"] == "PASS" and atoms["MATURE_CAMPAIGN_CONTROLS_CURRENT_EPISODE"] != "PASS":
        errors.append("same-lineage success PASS requires mature campaign control PASS")
    parent = selected["IMMEDIATE_COMPLETED_UP_PARENT"]
    episode = selected["CURRENT_EPISODE_UP"]
    if parent is not None and episode is not None and atoms["IMMEDIATE_PARENT_CONTROLS_CURRENT_EPISODE"] == "PASS":
        if parent["confirmed_end_date"] >= episode["start_date"]:
            errors.append("parent control PASS contradicts parent/episode chronology")
    if errors:
        return {"status": "INVALID", "errors": errors}

    dominant = response["dominant_relation"]["value"]
    route = ROUTE_BY_DOMINANT.get(dominant)
    challenger = selected["UP_CONTROL_CHALLENGER"]
    fresh_nesting = (
        challenger is not None
        and episode is not None
        and challenger["status"] == "FORMING"
        and challenger["start_date"] <= episode["start_date"]
        and challenger["observed_through"] >= episode["observed_through"]
    )
    required: dict[str, bool] = {
        "BEAR_REVERSAL_LEFT_RIGHT": (
            selected["ACTIVE_DOWN_CONTROLLER"] is not None
            and atoms["DOWN_CONTROLS_CURRENT_EPISODE"] == "PASS"
            and atoms["CHALLENGER_REPLACED_DOWN_CONTROL"] == "FAIL"
        ),
        "FRESH_Q1_EXPANSION": (
            fresh_nesting
            and atoms["CHALLENGER_REPLACED_DOWN_CONTROL"] == "PASS"
            and atoms["IMMEDIATE_PARENT_CONTROLS_CURRENT_EPISODE"] == "FAIL"
            and atoms["MATURE_CAMPAIGN_CONTROLS_CURRENT_EPISODE"] == "FAIL"
            and atoms["CURRENT_EPISODE_IS_FRESH_FORMING_ANCHOR"] == "PASS"
        ),
        "MACRO_COPY_RESONANCE": (
            parent is not None
            and atoms["IMMEDIATE_PARENT_CONTROLS_CURRENT_EPISODE"] == "PASS"
            and atoms["CURRENT_EPISODE_IS_FIRST_COPY_OF_IMMEDIATE_PARENT"] == "PASS"
        ),
        "MATURE_TREND_PULLBACK": (
            selected["CONTROLLING_MATURE_CAMPAIGN"] is not None
            and atoms["MATURE_CAMPAIGN_CONTROLS_CURRENT_EPISODE"] == "PASS"
            and atoms["SAME_LINEAGE_SUCCESS_PREEXISTS_CURRENT_EPISODE"] == "PASS"
            and atoms["SAME_LINEAGE_LONG_HABIT_PREEXISTS_CURRENT_EPISODE"] == "PASS"
        ),
    }
    if route and required[route]:
        working_role = WORKING_ROLE_BY_ROUTE[route]
        working = selected[working_role]
        assert working is not None
        return {
            "status": "VALID",
            "routing_status": "RESOLVED",
            "program_derived_scenario": route,
            "program_working_role": working_role,
            "program_working_candidate_id": working["candidate_id"],
            "program_episode_candidate_id": episode["candidate_id"] if episode else None,
            "atom_judgements": atoms,
            "trade_permission_granted": False,
        }
    return {
        "status": "VALID",
        "routing_status": "UNRESOLVED_NO_TRADE",
        "program_derived_scenario": None,
        "program_working_role": None,
        "program_working_candidate_id": None,
        "program_episode_candidate_id": episode["candidate_id"] if episode else None,
        "atom_judgements": atoms,
        "trade_permission_granted": False,
    }


def transport_for(packet: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    return transport_schema(build_schema(packet, report))
