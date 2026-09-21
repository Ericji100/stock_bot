"""Validate R4 role alignment with evidence-bearing PARENT not-applicability."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from scripts.v2_core_legacy_role_alignment_validator_v3 import (
    ROLE_KEYS,
    _derive_scenario,
    _role_atoms,
    load_json,
)


def validate_response(
    *, response: dict[str, Any], schema: dict[str, Any], packet: dict[str, Any]
) -> dict[str, Any]:
    errors = [
        f"SCHEMA:{'/'.join(str(part) for part in error.absolute_path)}:{error.message}"
        for error in sorted(
            Draft202012Validator(schema).iter_errors(response), key=lambda item: list(item.path)
        )
    ]
    candidates = {str(row["candidate_id"]): row for row in packet["candidate_pool"]}
    option_by_candidate = {
        str(row["candidate_id"]): str(row["evidence_option_id"])
        for row in packet["candidate_evidence_options"]
    }
    candidate_by_option = {option: candidates[candidate] for candidate, option in option_by_candidate.items()}
    if set(candidates) != set(option_by_candidate):
        errors.append("packet candidate/evidence-option sets differ")

    roles = response.get("roles")
    lifecycle = response.get("lifecycle_atoms")
    if not isinstance(roles, dict) or not isinstance(lifecycle, dict):
        return {"status": "INVALID", "errors": sorted(set(errors or ["roles/lifecycle missing"]))}

    role_candidates: dict[str, dict[str, Any] | None] = {}
    role_options: dict[str, str | None] = {}
    role_statuses: dict[str, str] = {}
    not_applicable_evidence_candidates: dict[str, dict[str, Any] | None] = {}
    for role_name, role_key in ROLE_KEYS.items():
        role = roles.get(role_key)
        if not isinstance(role, dict):
            errors.append(f"missing role: {role_key}")
            role_candidates[role_name] = None
            role_options[role_name] = None
            role_statuses[role_name] = "INVALID"
            continue
        status = str(role.get("selection_status"))
        role_statuses[role_name] = status
        candidate_id = str(role.get("candidate_id"))
        option_id = str(role.get("evidence_option_id"))
        alternatives = [str(value) for value in role.get("alternative_candidate_ids", [])]
        if len(alternatives) != len(set(alternatives)):
            errors.append(f"{role_key}: duplicate alternatives")
        if any(value not in candidates for value in alternatives):
            errors.append(f"{role_key}: alternative not in packet")
        if candidate_id in alternatives:
            errors.append(f"{role_key}: selected candidate repeated as alternative")
        if role.get("alternative_changes_role_conclusion") is True and not alternatives:
            errors.append(f"{role_key}: conclusion-changing alternative requires candidate")
        if role_key != "parent" and status == "NOT_APPLICABLE":
            errors.append(f"{role_key}: NOT_APPLICABLE is forbidden")

        if status == "SELECTED":
            candidate = candidates.get(candidate_id)
            role_candidates[role_name] = candidate
            role_options[role_name] = option_id
            if candidate is None:
                errors.append(f"{role_key}: selected candidate not in packet")
            else:
                expected_option = option_by_candidate[candidate_id]
                if option_id != expected_option:
                    errors.append(f"{role_key}: evidence option does not match candidate")
                for atom in _role_atoms(role):
                    if atom.get("evidence_option_id") != expected_option:
                        errors.append(f"{role_key}: role atom uses a different evidence option")
                    if atom.get("judgement") != "PASS":
                        errors.append(f"{role_key}: SELECTED requires all role atoms PASS")
            if option_id == "NONE":
                errors.append(f"{role_key}: SELECTED cannot use NONE option")
        elif status == "UNRESOLVED":
            role_candidates[role_name] = None
            role_options[role_name] = None
            if candidate_id != "NONE" or option_id != "NONE":
                errors.append(f"{role_key}: UNRESOLVED must use NONE candidate/option")
            if alternatives:
                errors.append(f"{role_key}: UNRESOLVED cannot retain alternatives")
            if role.get("alternative_changes_role_conclusion") is True:
                errors.append(f"{role_key}: UNRESOLVED cannot assert alternative conflict")
            for atom in _role_atoms(role):
                if atom.get("judgement") != "UNKNOWN" or atom.get("evidence_option_id") != "NONE":
                    errors.append(f"{role_key}: UNRESOLVED atoms must be UNKNOWN/NONE")
        elif status == "NOT_APPLICABLE":
            role_candidates[role_name] = None
            role_options[role_name] = option_id
            evidence_candidate = candidate_by_option.get(option_id)
            not_applicable_evidence_candidates[role_name] = evidence_candidate
            if role_key != "parent":
                errors.append(f"{role_key}: NOT_APPLICABLE is allowed only for parent")
            if candidate_id != "NONE":
                errors.append(f"{role_key}: NOT_APPLICABLE must use NONE candidate")
            if evidence_candidate is None:
                errors.append(f"{role_key}: NOT_APPLICABLE requires a fixed evidence option")
            else:
                selected_context_ids = {
                    candidate["candidate_id"]
                    for selected_role, candidate in role_candidates.items()
                    if selected_role in {"CAMPAIGN_CONTEXT", "SCENARIO_WORKING"}
                    and candidate is not None
                }
                if evidence_candidate["candidate_id"] not in selected_context_ids:
                    errors.append(
                        f"{role_key}: NOT_APPLICABLE option must belong to a selected context/working role"
                    )
            if alternatives:
                errors.append(f"{role_key}: NOT_APPLICABLE cannot retain alternatives")
            if role.get("alternative_changes_role_conclusion") is True:
                errors.append(f"{role_key}: NOT_APPLICABLE cannot assert alternative conflict")
            for atom in _role_atoms(role):
                if atom.get("judgement") != "PASS" or atom.get("evidence_option_id") != option_id:
                    errors.append(f"{role_key}: NOT_APPLICABLE atoms must be PASS with its option")
        else:
            role_candidates[role_name] = None
            role_options[role_name] = None
            errors.append(f"{role_key}: unknown selection status")

    for atom_name, atom in lifecycle.items():
        judgement = atom.get("judgement")
        supporting_role = str(atom.get("supporting_role"))
        option_id = str(atom.get("evidence_option_id"))
        if supporting_role == "NONE":
            if judgement != "UNKNOWN" or option_id != "NONE":
                errors.append(f"{atom_name}: NONE role is allowed only for UNKNOWN/NONE")
            continue
        if supporting_role not in ROLE_KEYS:
            errors.append(f"{atom_name}: unknown supporting role")
            continue
        if role_candidates.get(supporting_role) is None:
            if not (
                supporting_role == "PARENT"
                and role_statuses.get("PARENT") == "NOT_APPLICABLE"
                and option_id == role_options.get("PARENT")
            ):
                errors.append(f"{atom_name}: supporting role is not selected/applicable")
            continue
        if option_id != role_options[supporting_role]:
            errors.append(f"{atom_name}: evidence option does not match supporting role")

    recommendation = response.get("recommended_scenario_family")
    working = role_candidates.get("SCENARIO_WORKING")
    if working is not None:
        if recommendation == "BEAR_REVERSAL_LEFT_RIGHT" and working["direction"] != "DOWN":
            errors.append("BEAR recommendation requires DOWN scenario working anchor")
        if recommendation in {
            "FRESH_Q1_EXPANSION",
            "MACRO_COPY_RESONANCE",
            "MATURE_TREND_PULLBACK",
        } and working["direction"] != "UP":
            errors.append("long recommendation requires UP scenario working anchor")

    uncertainty = [str(value) for value in response.get("uncertainty_codes", [])]
    if "NONE" in uncertainty and len(uncertainty) != 1:
        errors.append("NONE uncertainty cannot be combined")
    if errors:
        return {"status": "INVALID", "errors": sorted(set(errors))}

    program_scenario, route_reasons = _derive_scenario(
        roles=roles, role_candidates=role_candidates, lifecycle=lifecycle
    )
    working_conflict = roles["scenario_working"]["alternative_changes_role_conclusion"] is True
    program_working = None if working_conflict else role_candidates["SCENARIO_WORKING"]
    route_status = (
        "ROUTED"
        if program_scenario != "UNRESOLVED_NO_TRADE"
        and program_working is not None
        and recommendation == program_scenario
        else (
            "CONFLICT_REVIEW_REQUIRED"
            if program_scenario != "UNRESOLVED_NO_TRADE" and recommendation != program_scenario
            else "UNRESOLVED"
        )
    )
    return {
        "status": "VALID",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "role_candidates": role_candidates,
        "role_statuses": role_statuses,
        "not_applicable_evidence_candidates": not_applicable_evidence_candidates,
        "program_usable_scenario_working_anchor": program_working,
        "program_derived_scenario_family": program_scenario,
        "ai_recommended_scenario_family": recommendation,
        "recommendation_matches_program": recommendation == program_scenario,
        "route_status": route_status,
        "route_reasons": route_reasons,
        "validator_contract": {
            "ai_roles_repaired": False,
            "ai_roles_upgraded": False,
            "candidate_dates_bound_by_program": True,
            "not_applicable_requires_positive_fixed_evidence": True,
            "scenario_derived_by_frozen_truth_table": True,
            "legacy_answers_used": False,
            "future_performance_used": False,
            "identity_used": False,
            "trade_permission_granted": False,
        },
        "errors": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    args = parser.parse_args()
    result = validate_response(
        response=load_json(args.response), schema=load_json(args.schema), packet=load_json(args.packet)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "VALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
