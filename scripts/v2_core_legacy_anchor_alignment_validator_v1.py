"""Validate one-pass legacy controlling-anchor alignment responses."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _atoms(selection: dict[str, Any]) -> list[dict[str, Any]]:
    return [selection["data_sufficiency"], *selection["anchor_quality"].values()]


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
    if set(candidates) != set(option_by_candidate):
        errors.append("packet candidate/evidence-option sets differ")
    selection = response.get("selection")
    if not isinstance(selection, dict):
        return {"status": "INVALID", "errors": sorted(set(errors or ["selection missing"]))}

    selected_id = str(selection.get("controlling_candidate_id"))
    selected_option = str(selection.get("controlling_evidence_option_id"))
    alternatives = [str(value) for value in selection.get("alternative_candidate_ids", [])]
    if len(alternatives) != len(set(alternatives)):
        errors.append("alternative candidate ids duplicated")
    if any(value not in candidates for value in alternatives):
        errors.append("alternative candidate id not in packet")
    if selected_id in alternatives:
        errors.append("controlling candidate repeated as alternative")
    uncertainty = [str(value) for value in selection.get("uncertainty_codes", [])]
    if "NONE" in uncertainty and len(uncertainty) != 1:
        errors.append("NONE uncertainty cannot be combined")

    status = selection.get("selection_status")
    scenario = selection.get("recommended_scenario_family")
    reason = selection.get("control_reason")
    if status == "SELECTED":
        if selected_id not in candidates:
            errors.append("selected candidate id not in packet")
        else:
            expected_option = option_by_candidate[selected_id]
            if selected_option != expected_option:
                errors.append("selected evidence option does not match candidate")
            for atom in _atoms(selection):
                if atom.get("evidence_option_id") != expected_option:
                    errors.append("selected atom references a different evidence option")
            if selection["data_sufficiency"]["judgement"] != "PASS":
                errors.append("SELECTED requires data_sufficiency PASS")
            if selection["anchor_quality"]["anchor_traceable"]["judgement"] != "PASS":
                errors.append("SELECTED requires anchor_traceable PASS")
            if selection["anchor_quality"]["controls_current_context"]["judgement"] != "PASS":
                errors.append("SELECTED requires controls_current_context PASS")
            if scenario == "UNRESOLVED_NO_TRADE":
                errors.append("SELECTED cannot recommend unresolved scenario")
            if selection.get("alternative_conflict") is not False:
                errors.append("SELECTED cannot retain a permission-changing alternative conflict")
            direction = candidates[selected_id]["direction"]
            if direction == "DOWN" and scenario != "BEAR_REVERSAL_LEFT_RIGHT":
                errors.append("DOWN controlling anchor requires BEAR scenario family")
            if direction == "UP" and scenario == "BEAR_REVERSAL_LEFT_RIGHT":
                errors.append("UP controlling anchor cannot recommend BEAR scenario family")
            expected_reason = {
                "BEAR_REVERSAL_LEFT_RIGHT": "ACTIVE_DOWN_ANCHOR_CONTROLS",
                "FRESH_Q1_EXPANSION": "FORMING_UP_REPLACES_DOWN_CONTROL",
                "MACRO_COPY_RESONANCE": "COMPLETED_UP_PARENT_CONTROLS_FIRST_COPY",
                "MATURE_TREND_PULLBACK": "MATURE_UP_CAMPAIGN_CONTROLS",
            }.get(str(scenario))
            if expected_reason and reason != expected_reason:
                errors.append("control reason does not match recommended scenario family")
        if selected_option == "NONE":
            errors.append("SELECTED cannot use NONE evidence option")
    elif status == "UNRESOLVED":
        if selected_id != "NONE" or selected_option != "NONE":
            errors.append("UNRESOLVED must use NONE candidate and evidence option")
        if scenario != "UNRESOLVED_NO_TRADE":
            errors.append("UNRESOLVED must recommend UNRESOLVED_NO_TRADE")
        if reason not in {"NESTED_SCALE_CONFLICT", "INSUFFICIENT_CAUSAL_EVIDENCE"}:
            errors.append("UNRESOLVED reason is not a conflict/insufficiency reason")
        for atom in _atoms(selection):
            if atom.get("evidence_option_id") != "NONE":
                errors.append("UNRESOLVED atoms must use NONE evidence option")
    else:
        errors.append("unknown selection status")

    if errors:
        return {"status": "INVALID", "errors": sorted(set(errors))}
    selected_candidate = candidates.get(selected_id)
    return {
        "status": "VALID",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "selection_status": status,
        "selected_candidate": selected_candidate,
        "alternative_candidate_ids": alternatives,
        "recommended_scenario_family": scenario,
        "validator_contract": {
            "ai_selection_repaired": False,
            "ai_selection_upgraded": False,
            "candidate_dates_bound_by_program": True,
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
        response=load_json(args.response),
        schema=load_json(args.schema),
        packet=load_json(args.packet),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "VALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
