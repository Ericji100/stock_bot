"""Validate the R5 lifecycle-first four-role alignment candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROLE_KEYS = {
    "CAMPAIGN_CONTEXT": "campaign_context",
    "SCENARIO_WORKING": "scenario_working",
    "PARENT": "parent",
    "EPISODE_STRUCTURE": "episode_structure",
}

SCENARIO_CLASS = {
    "BEAR_REVERSAL_LEFT_RIGHT": "ACTIVE_DOWN_LIFECYCLE_REFERENCE",
    "FRESH_Q1_EXPANSION": "FORMING_UP_NEW_ANCHOR_REFERENCE",
    "MACRO_COPY_RESONANCE": "COMPLETED_UP_PARENT_REFERENCE",
    "MATURE_TREND_PULLBACK": "PERSISTENT_UP_CAMPAIGN_REFERENCE",
    "UNRESOLVED_NO_TRADE": "UNRESOLVED",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _basis_family(basis: str) -> str:
    if basis.startswith("MACD_"):
        return "MACD"
    if basis.startswith("PIVOT_"):
        return "PIVOT"
    return "OTHER"


def _role_atoms(role: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        role["data_sufficiency"],
        role["role_fit"],
        role["structural_corroboration"],
        role["relationship_to_current_context"],
    ]


def _derive_scenario(
    *,
    working_role_class: str,
    role_candidates: dict[str, dict[str, Any] | None],
    lifecycle: dict[str, dict[str, Any]],
    anti_drift: dict[str, dict[str, Any]],
    roles: dict[str, dict[str, Any]],
) -> tuple[str, list[str]]:
    if any(role.get("alternative_changes_role_conclusion") is True for role in roles.values()):
        return "UNRESOLVED_NO_TRADE", ["PERMISSION_CHANGING_ROLE_ALTERNATIVE"]
    working = role_candidates["SCENARIO_WORKING"]
    if working is None:
        return "UNRESOLVED_NO_TRADE", ["SCENARIO_WORKING_ANCHOR_UNRESOLVED"]
    if anti_drift["no_recency_default"]["result"] != "PASS":
        return "UNRESOLVED_NO_TRADE", ["RECENCY_DEFAULT_NOT_CLEARED"]
    if anti_drift["cross_family_comparison_completed"]["result"] != "PASS":
        return "UNRESOLVED_NO_TRADE", ["CROSS_FAMILY_COMPARISON_NOT_CLEARED"]

    value = {name: atom["judgement"] for name, atom in lifecycle.items()}
    scenario = "UNRESOLVED_NO_TRADE"
    reasons = ["NO_LIFECYCLE_RULE_MATCH"]
    if (
        working_role_class == "ACTIVE_DOWN_LIFECYCLE_REFERENCE"
        and working["direction"] == "DOWN"
        and value["active_large_down_still_controls"] == "PASS"
        and value["up_episode_has_not_replaced_down_control"] == "PASS"
        and anti_drift["bear_control_transfer_resolved"]["result"] == "PASS"
    ):
        scenario, reasons = "BEAR_REVERSAL_LEFT_RIGHT", ["UNIQUE_BEAR_PROFILE_MATCH"]
    elif (
        working_role_class == "FORMING_UP_NEW_ANCHOR_REFERENCE"
        and working["direction"] == "UP"
        and working["status"] == "FORMING"
        and value["active_large_down_still_controls"] == "FAIL"
        and value["fresh_up_anchor_without_completed_parent"] == "PASS"
        and value["completed_up_parent_controls_copy"] == "FAIL"
        and value["mature_campaign_repeated_success"] == "FAIL"
        and anti_drift["fresh_positive_anchor_evidence_present"]["result"] == "PASS"
    ):
        scenario, reasons = "FRESH_Q1_EXPANSION", ["UNIQUE_FRESH_PROFILE_MATCH"]
    elif (
        working_role_class == "COMPLETED_UP_PARENT_REFERENCE"
        and working["direction"] == "UP"
        and working["status"] == "CONFIRMED"
        and role_candidates["PARENT"] is not None
        and value["active_large_down_still_controls"] == "FAIL"
        and value["completed_up_parent_controls_copy"] == "PASS"
        and value["current_is_first_independent_copy"] == "PASS"
        and value["mature_campaign_repeated_success"] == "FAIL"
        and anti_drift["parent_episode_separated"]["result"] == "PASS"
    ):
        scenario, reasons = "MACRO_COPY_RESONANCE", ["UNIQUE_MACRO_PROFILE_MATCH"]
    elif (
        working_role_class == "PERSISTENT_UP_CAMPAIGN_REFERENCE"
        and working["direction"] == "UP"
        and value["active_large_down_still_controls"] == "FAIL"
        and value["mature_campaign_repeated_success"] == "PASS"
        and value["long_trend_habit_preexists_current_episode"] == "PASS"
        and anti_drift["mature_campaign_episode_separated"]["result"] == "PASS"
    ):
        scenario, reasons = "MATURE_TREND_PULLBACK", ["UNIQUE_MATURE_PROFILE_MATCH"]
    elif "UNKNOWN" in value.values():
        reasons = ["REQUIRED_LIFECYCLE_ATOM_UNKNOWN"]
    return scenario, reasons


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
    candidate_by_option = {
        option: candidates[candidate] for candidate, option in option_by_candidate.items()
    }
    if set(candidates) != set(option_by_candidate):
        errors.append("packet candidate/evidence-option sets differ")

    roles = response.get("roles")
    lifecycle = response.get("lifecycle_atoms")
    anti_drift = response.get("anti_drift_checks")
    if not isinstance(roles, dict) or not isinstance(lifecycle, dict) or not isinstance(anti_drift, dict):
        return {"status": "INVALID", "errors": sorted(set(errors or ["required sections missing"]))}

    role_candidates: dict[str, dict[str, Any] | None] = {}
    role_options: dict[str, str | None] = {}
    role_statuses: dict[str, str] = {}
    for role_name, role_key in ROLE_KEYS.items():
        role = roles.get(role_key)
        if not isinstance(role, dict):
            errors.append(f"missing role: {role_key}")
            role_candidates[role_name] = None
            role_options[role_name] = None
            role_statuses[role_name] = "INVALID"
            continue
        status = str(role.get("selection_status"))
        candidate_id = str(role.get("candidate_id"))
        option_id = str(role.get("evidence_option_id"))
        alternatives = [str(value) for value in role.get("alternative_candidate_ids", [])]
        role_statuses[role_name] = status
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
        elif status == "UNRESOLVED":
            role_candidates[role_name] = None
            role_options[role_name] = None
            if candidate_id != "NONE" or option_id != "NONE":
                errors.append(f"{role_key}: UNRESOLVED must use NONE candidate/option")
            if alternatives or role.get("alternative_changes_role_conclusion") is True:
                errors.append(f"{role_key}: UNRESOLVED cannot retain alternatives/conflict")
            for atom in _role_atoms(role):
                if atom.get("judgement") != "UNKNOWN" or atom.get("evidence_option_id") != "NONE":
                    errors.append(f"{role_key}: UNRESOLVED atoms must be UNKNOWN/NONE")
        elif status == "NOT_APPLICABLE":
            role_candidates[role_name] = None
            role_options[role_name] = option_id
            evidence_candidate = candidate_by_option.get(option_id)
            if role_key != "parent":
                errors.append(f"{role_key}: NOT_APPLICABLE allowed only for parent")
            if candidate_id != "NONE" or evidence_candidate is None:
                errors.append(f"{role_key}: NOT_APPLICABLE requires NONE and fixed option")
            selected_context_ids = {
                candidate["candidate_id"]
                for selected_role, candidate in role_candidates.items()
                if selected_role in {"CAMPAIGN_CONTEXT", "SCENARIO_WORKING"}
                and candidate is not None
            }
            if evidence_candidate is not None and evidence_candidate["candidate_id"] not in selected_context_ids:
                errors.append(f"{role_key}: NOT_APPLICABLE option must belong to context/working")
            if alternatives or role.get("alternative_changes_role_conclusion") is True:
                errors.append(f"{role_key}: NOT_APPLICABLE cannot retain alternatives/conflict")
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
        elif option_id != role_options[supporting_role]:
            errors.append(f"{atom_name}: evidence option does not match supporting role")

    comparison = response.get("basis_comparison", {})
    families = {_basis_family(str(row["basis"])) for row in candidates.values()}
    expected_availability = (
        "BOTH" if {"MACD", "PIVOT"}.issubset(families)
        else "MACD_ONLY" if "MACD" in families
        else "PIVOT_ONLY" if "PIVOT" in families
        else "NEITHER"
    )
    if comparison.get("availability") != expected_availability:
        errors.append("basis_comparison: availability does not match packet")
    macd_id = str(comparison.get("macd_candidate_id"))
    pivot_id = str(comparison.get("pivot_candidate_id"))
    if (macd_id == "NONE") != ("MACD" not in families):
        errors.append("basis_comparison: MACD candidate presence mismatch")
    if macd_id != "NONE" and (macd_id not in candidates or _basis_family(candidates[macd_id]["basis"]) != "MACD"):
        errors.append("basis_comparison: invalid MACD candidate")
    if (pivot_id == "NONE") != ("PIVOT" not in families):
        errors.append("basis_comparison: pivot candidate presence mismatch")
    if pivot_id != "NONE" and (pivot_id not in candidates or _basis_family(candidates[pivot_id]["basis"]) != "PIVOT"):
        errors.append("basis_comparison: invalid pivot candidate")
    working = role_candidates.get("SCENARIO_WORKING")
    selected_working_id = str(comparison.get("selected_working_candidate_id"))
    expected_working_id = working["candidate_id"] if working is not None else "NONE"
    if selected_working_id != expected_working_id:
        errors.append("basis_comparison: selected working candidate mismatch")
    conclusion = comparison.get("conclusion")
    if working is not None:
        family = _basis_family(str(working["basis"]))
        allowed = {
            "MACD": {"MACD_BETTER_ROLE_FIT", "EQUIVALENT_BOUNDARY", "ONE_FAMILY_ONLY"},
            "PIVOT": {"PIVOT_BETTER_ROLE_FIT", "EQUIVALENT_BOUNDARY", "ONE_FAMILY_ONLY"},
        }.get(family, {"UNRESOLVED"})
        if conclusion not in allowed:
            errors.append("basis_comparison: conclusion incompatible with working family")

    for check_name, check in anti_drift.items():
        result = check.get("result")
        candidate_id = str(check.get("candidate_id"))
        option_id = str(check.get("evidence_option_id"))
        if result == "NOT_APPLICABLE":
            if candidate_id != "NONE" or option_id != "NONE":
                errors.append(f"{check_name}: NOT_APPLICABLE must use NONE candidate/option")
        else:
            candidate = candidates.get(candidate_id)
            if candidate is None or option_by_candidate.get(candidate_id) != option_id:
                errors.append(f"{check_name}: candidate/option must be fixed and matching")

    provisional = response.get("provisional_scenario_family")
    role_class = str(response.get("working_role_class"))
    if SCENARIO_CLASS.get(str(provisional)) != role_class:
        errors.append("provisional scenario and working role class mismatch")
    recommendation = response.get("recommended_scenario_family")
    if recommendation != provisional:
        errors.append("recommended scenario must equal provisional scenario")
    uncertainty = [str(value) for value in response.get("uncertainty_codes", [])]
    if "NONE" in uncertainty and len(uncertainty) != 1:
        errors.append("NONE uncertainty cannot be combined")
    if errors:
        return {"status": "INVALID", "errors": sorted(set(errors))}

    program_scenario, route_reasons = _derive_scenario(
        working_role_class=role_class,
        role_candidates=role_candidates,
        lifecycle=lifecycle,
        anti_drift=anti_drift,
        roles=roles,
    )
    working_conflict = roles["scenario_working"]["alternative_changes_role_conclusion"] is True
    program_working = None if working_conflict else working
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
            "episode_structure_separate": True,
            "cross_family_comparison_validated": True,
            "scenario_derived_by_frozen_truth_table": True,
            "legacy_answers_used": False,
            "future_performance_used": False,
            "identity_used": False,
            "trade_permission_granted": False
        },
        "errors": []
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    args = parser.parse_args()
    result = validate_response(
        response=load_json(args.response),
        schema=load_json(args.schema),
        packet=load_json(args.packet),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "VALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
