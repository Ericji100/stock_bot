"""Validate R6 B0B scenario-bound role selection without granting trade permission."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from scripts.v2_core_legacy_lifecycle_b0a_validator_v6 import (
    load_json,
    validate_response as validate_lifecycle_response,
)


ROLE_KEYS = {
    "CAMPAIGN_CONTEXT": "campaign_context",
    "SCENARIO_WORKING": "scenario_working",
    "PARENT": "parent",
    "EPISODE_STRUCTURE": "episode_structure",
}


def _basis_family(basis: Any) -> str:
    value = str(basis)
    if value.startswith("MACD_"):
        return "MACD"
    if value.startswith("PIVOT_"):
        return "PIVOT"
    return "OTHER"


def _value_at_path(document: dict[str, Any], dotted_path: str | None) -> Any:
    if dotted_path is None:
        return None
    value: Any = document
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _role_atoms(role: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        role["data_sufficiency"],
        role["role_fit"],
        role["structural_corroboration"],
        role["relationship_to_current_context"],
    ]


def _packet_maps(
    packet: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, dict[str, Any]], list[str]]:
    errors: list[str] = []
    candidates: dict[str, dict[str, Any]] = {}
    for row in packet.get("candidate_pool", []):
        if not isinstance(row, dict) or not isinstance(row.get("candidate_id"), str):
            errors.append("packet contains malformed candidate")
            continue
        candidate_id = row["candidate_id"]
        if candidate_id in candidates:
            errors.append(f"packet duplicate candidate: {candidate_id}")
        candidates[candidate_id] = row
    option_by_candidate: dict[str, str] = {}
    for row in packet.get("candidate_evidence_options", []):
        if not isinstance(row, dict):
            errors.append("packet contains malformed evidence option")
            continue
        candidate_id = row.get("candidate_id")
        option_id = row.get("evidence_option_id")
        if not isinstance(candidate_id, str) or not isinstance(option_id, str):
            errors.append("packet evidence option lacks candidate or option id")
            continue
        option_by_candidate[candidate_id] = option_id
    if set(candidates) != set(option_by_candidate):
        errors.append("packet candidate/evidence-option sets differ")
    candidate_by_option = {
        option_id: candidates[candidate_id]
        for candidate_id, option_id in option_by_candidate.items()
        if candidate_id in candidates
    }
    return candidates, option_by_candidate, candidate_by_option, errors


def _binding(
    *,
    label: str,
    candidate_id: Any,
    option_id: Any,
    candidates: dict[str, dict[str, Any]],
    option_by_candidate: dict[str, str],
    errors: list[str],
) -> dict[str, Any] | None:
    candidate_text = str(candidate_id)
    option_text = str(option_id)
    if candidate_text == "NONE" or option_text == "NONE":
        if candidate_text != "NONE" or option_text != "NONE":
            errors.append(f"{label}: candidate and option must both be NONE or both be fixed")
        return None
    candidate = candidates.get(candidate_text)
    if candidate is None:
        errors.append(f"{label}: candidate not in packet")
        return None
    if option_by_candidate.get(candidate_text) != option_text:
        errors.append(f"{label}: evidence option does not match candidate")
    return candidate


def validate_response(
    *,
    response: dict[str, Any],
    schema: dict[str, Any],
    packet: dict[str, Any],
    b0a_response: dict[str, Any],
    b0a_schema: dict[str, Any],
    lifecycle_truth_table: dict[str, Any],
    role_truth_table: dict[str, Any],
) -> dict[str, Any]:
    errors = [
        f"SCHEMA:{'/'.join(str(part) for part in error.absolute_path)}:{error.message}"
        for error in sorted(
            Draft202012Validator(schema).iter_errors(response),
            key=lambda item: list(item.path),
        )
    ]
    lifecycle_validation = validate_lifecycle_response(
        response=b0a_response,
        schema=b0a_schema,
        packet=packet,
        truth_table=lifecycle_truth_table,
    )
    if lifecycle_validation["status"] != "VALID":
        errors.append("B0A lifecycle output is invalid")
        scenario = "UNRESOLVED_NO_TRADE"
    else:
        scenario = lifecycle_validation["program_derived_scenario_family"]
        if lifecycle_validation["route_status"] != "ROUTED":
            errors.append("B0B requires one uniquely routed B0A scenario")

    if role_truth_table.get("input_schema_version") != response.get("schema_version"):
        errors.append("role truth table input schema version mismatch")
    if role_truth_table.get("no_trade_permission_at_this_stage") is not True:
        errors.append("role truth table must deny trade permission at B0B")
    profile = role_truth_table.get("profiles", {}).get(scenario)
    if profile is None:
        errors.append("role truth table has no profile for B0A scenario")

    candidates, option_by_candidate, candidate_by_option, packet_errors = _packet_maps(packet)
    errors.extend(packet_errors)
    roles = response.get("roles")
    anti_drift = response.get("anti_drift_checks")
    if not isinstance(roles, dict) or not isinstance(anti_drift, dict):
        return {"status": "INVALID", "errors": sorted(set(errors or ["role sections missing"]))}

    role_candidates: dict[str, dict[str, Any] | None] = {}
    role_statuses: dict[str, str] = {}
    role_options: dict[str, str] = {}
    resolution_reasons: list[str] = []
    for role_name, role_key in ROLE_KEYS.items():
        role = roles.get(role_key)
        if not isinstance(role, dict):
            errors.append(f"missing role: {role_key}")
            continue
        status = str(role.get("selection_status"))
        candidate_id = str(role.get("candidate_id"))
        option_id = str(role.get("evidence_option_id"))
        alternatives = [str(value) for value in role.get("alternative_candidate_ids", [])]
        role_statuses[role_name] = status
        role_options[role_name] = option_id
        if candidate_id in alternatives:
            errors.append(f"{role_key}: selected candidate repeated as alternative")
        if any(value not in candidates for value in alternatives):
            errors.append(f"{role_key}: alternative not in packet")
        if role.get("alternative_changes_role_conclusion") is True:
            if not alternatives:
                errors.append(f"{role_key}: conclusion-changing alternative requires candidate")
            resolution_reasons.append(f"{role_name}_ALTERNATIVE_CHANGES_CONCLUSION")
        if role_name != "PARENT" and status == "NOT_APPLICABLE":
            errors.append(f"{role_key}: NOT_APPLICABLE allowed only for parent")

        if status == "SELECTED":
            candidate = _binding(
                label=role_key,
                candidate_id=candidate_id,
                option_id=option_id,
                candidates=candidates,
                option_by_candidate=option_by_candidate,
                errors=errors,
            )
            role_candidates[role_name] = candidate
            if candidate is None:
                errors.append(f"{role_key}: SELECTED requires fixed candidate")
            for atom in _role_atoms(role):
                if atom.get("judgement") != "PASS" or atom.get("evidence_option_id") != option_id:
                    errors.append(f"{role_key}: SELECTED atoms must be PASS with selected option")
        elif status == "UNRESOLVED":
            role_candidates[role_name] = None
            if candidate_id != "NONE" or option_id != "NONE":
                errors.append(f"{role_key}: UNRESOLVED must use NONE candidate/option")
            if alternatives or role.get("alternative_changes_role_conclusion") is True:
                errors.append(f"{role_key}: UNRESOLVED cannot retain alternatives/conflict")
            for atom in _role_atoms(role):
                if atom.get("judgement") != "UNKNOWN" or atom.get("evidence_option_id") != "NONE":
                    errors.append(f"{role_key}: UNRESOLVED atoms must be UNKNOWN/NONE")
            resolution_reasons.append(f"{role_name}_UNRESOLVED")
        elif status == "NOT_APPLICABLE":
            role_candidates[role_name] = None
            if role_name != "PARENT" or candidate_id != "NONE":
                errors.append(f"{role_key}: invalid NOT_APPLICABLE use")
            if option_id not in candidate_by_option:
                errors.append(f"{role_key}: NOT_APPLICABLE requires a fixed evidence option")
            if alternatives or role.get("alternative_changes_role_conclusion") is True:
                errors.append(f"{role_key}: NOT_APPLICABLE cannot retain alternatives/conflict")
            for atom in _role_atoms(role):
                if atom.get("judgement") != "PASS" or atom.get("evidence_option_id") != option_id:
                    errors.append(f"{role_key}: NOT_APPLICABLE atoms must be PASS with fixed option")
        else:
            role_candidates[role_name] = None
            errors.append(f"{role_key}: unknown selection status")

    if role_statuses.get("PARENT") == "NOT_APPLICABLE":
        if role_options.get("PARENT") != role_options.get("SCENARIO_WORKING"):
            errors.append("parent NOT_APPLICABLE option must cite selected scenario working evidence")

    comparison = response.get("basis_comparison")
    if not isinstance(comparison, dict):
        errors.append("basis comparison missing")
    else:
        families = {_basis_family(row.get("basis")) for row in candidates.values()}
        expected_availability = (
            "BOTH"
            if {"MACD", "PIVOT"}.issubset(families)
            else "MACD_ONLY"
            if "MACD" in families
            else "PIVOT_ONLY"
            if "PIVOT" in families
            else "NEITHER"
        )
        if comparison.get("availability") != expected_availability:
            errors.append("basis comparison availability does not match packet")
        macd_id = str(comparison.get("macd_candidate_id"))
        pivot_id = str(comparison.get("pivot_candidate_id"))
        if expected_availability in {"BOTH", "MACD_ONLY"}:
            if macd_id not in candidates or _basis_family(candidates[macd_id].get("basis")) != "MACD":
                errors.append("basis comparison lacks valid MACD candidate")
        elif macd_id != "NONE":
            errors.append("basis comparison MACD candidate must be NONE")
        if expected_availability in {"BOTH", "PIVOT_ONLY"}:
            if pivot_id not in candidates or _basis_family(candidates[pivot_id].get("basis")) != "PIVOT":
                errors.append("basis comparison lacks valid pivot candidate")
        elif pivot_id != "NONE":
            errors.append("basis comparison pivot candidate must be NONE")
        selected_working_id = (
            role_candidates.get("SCENARIO_WORKING", {}) or {}
        ).get("candidate_id", "NONE")
        if comparison.get("selected_working_candidate_id") != selected_working_id:
            errors.append("basis comparison selected working candidate mismatch")
        if expected_availability == "BOTH" and comparison.get("conclusion") == "ONE_FAMILY_ONLY":
            errors.append("basis comparison cannot claim one family when both exist")
        if expected_availability in {"MACD_ONLY", "PIVOT_ONLY"} and comparison.get(
            "conclusion"
        ) not in {"ONE_FAMILY_ONLY", "UNRESOLVED"}:
            errors.append("single-family comparison conclusion mismatch")

    required_checks = set(role_truth_table.get("required_anti_drift_checks", []))
    if set(anti_drift) != required_checks:
        errors.append("anti-drift check set mismatch")
    for name, check in anti_drift.items():
        if not isinstance(check, dict):
            errors.append(f"{name}: malformed anti-drift check")
            continue
        _binding(
            label=f"anti_drift:{name}",
            candidate_id=check.get("candidate_id"),
            option_id=check.get("evidence_option_id"),
            candidates=candidates,
            option_by_candidate=option_by_candidate,
            errors=errors,
        )
        if check.get("result") != "PASS":
            resolution_reasons.append(f"{name.upper()}_NOT_PASS")

    if profile is not None and role_candidates.get("SCENARIO_WORKING") is not None:
        working = role_candidates["SCENARIO_WORKING"]
        working_rule = profile["scenario_working"]
        if working.get("direction") not in working_rule["direction"]:
            errors.append("scenario working direction violates routed profile")
        if working.get("status") not in working_rule["status"]:
            errors.append("scenario working status violates routed profile")
        expected_working = _value_at_path(b0a_response, working_rule.get("bind_to_b0a"))
        if expected_working is not None and working.get("candidate_id") != expected_working:
            errors.append("scenario working candidate violates B0A binding")

        episode = role_candidates.get("EPISODE_STRUCTURE")
        episode_rule = profile["episode_structure"]
        if episode is not None:
            if episode.get("direction") not in episode_rule["direction"]:
                errors.append("episode direction violates routed profile")
            if episode.get("status") not in episode_rule["status"]:
                errors.append("episode status violates routed profile")
            expected_episode = _value_at_path(b0a_response, episode_rule.get("bind_to_b0a"))
            if expected_episode is not None and episode.get("candidate_id") != expected_episode:
                errors.append("episode candidate violates B0A binding")

        if role_statuses.get("PARENT") not in profile["parent_selection_status"]:
            errors.append("parent status violates routed profile")
        expected_parent = _value_at_path(b0a_response, profile.get("parent_bind_to_b0a"))
        if expected_parent is not None:
            parent = role_candidates.get("PARENT")
            if parent is None or parent.get("candidate_id") != expected_parent:
                errors.append("parent candidate violates B0A binding")

        candidate_ids = {
            role_name: candidate.get("candidate_id")
            for role_name, candidate in role_candidates.items()
            if candidate is not None and role_name in {"SCENARIO_WORKING", "PARENT", "EPISODE_STRUCTURE"}
        }
        allowed_equalities = set(profile.get("allowed_role_equalities", []))
        names = list(candidate_ids)
        for index, left in enumerate(names):
            for right in names[index + 1 :]:
                if candidate_ids[left] != candidate_ids[right]:
                    continue
                equality = f"{left}={right}"
                reverse = f"{right}={left}"
                if equality not in allowed_equalities and reverse not in allowed_equalities:
                    resolution_reasons.append(f"ROLE_COMPRESSION:{equality}")

    if errors:
        return {"status": "INVALID", "errors": sorted(set(errors))}

    role_resolution_status = "RESOLVED" if not resolution_reasons else "UNRESOLVED"
    role_ids = {
        role_name: candidate.get("candidate_id") if candidate is not None else "NONE"
        for role_name, candidate in role_candidates.items()
    }
    return {
        "status": "VALID",
        "errors": [],
        "program_derived_scenario_family": scenario,
        "role_resolution_status": role_resolution_status,
        "role_resolution_reasons": sorted(set(resolution_reasons)),
        "selected_role_candidate_ids": role_ids,
        "validator_contract": {
            "ai_revotes_scenario": False,
            "program_invents_role_answers": False,
            "program_validates_b0a_role_bindings": True,
            "trade_permission_granted": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--b0a-response", type=Path, required=True)
    parser.add_argument("--b0a-schema", type=Path, required=True)
    parser.add_argument("--lifecycle-truth-table", type=Path, required=True)
    parser.add_argument("--role-truth-table", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_response(
        response=load_json(args.response),
        schema=load_json(args.schema),
        packet=load_json(args.packet),
        b0a_response=load_json(args.b0a_response),
        b0a_schema=load_json(args.b0a_schema),
        lifecycle_truth_table=load_json(args.lifecycle_truth_table),
        role_truth_table=load_json(args.role_truth_table),
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["status"] == "VALID" else 2


if __name__ == "__main__":
    raise SystemExit(main())
