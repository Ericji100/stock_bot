"""Validate Stage B1a1E perceptions and derive frozen aggregate results."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REASON_BY_JUDGEMENT = {
    "SUPPORTS": {"VISIBLE_EVIDENCE_SUPPORTS"},
    "CONTRADICTS": {"VISIBLE_EVIDENCE_CONTRADICTS"},
    "INSUFFICIENT": {
        "REQUIRED_EVIDENCE_INSUFFICIENT",
        "CONFLICTING_VISIBLE_EVIDENCE",
    },
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _schema_errors(response: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    return [
        f"SCHEMA:{'/'.join(str(part) for part in error.absolute_path)}:{error.message}"
        for error in sorted(validator.iter_errors(response), key=lambda item: list(item.path))
    ]


def _validate_judgement(value: dict[str, Any], path: str, errors: list[str]) -> None:
    if value["reason_code"] not in REASON_BY_JUDGEMENT[value["judgement"]]:
        errors.append(f"{path}:reason_code does not match judgement")


def _aggregate_structural(
    option_rows: list[dict[str, Any]], perceptions: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    supporting = []
    decisive_negative = []
    unresolved = []
    for option in sorted(option_rows, key=lambda row: int(row["focus_evidence_rank"])):
        option_id = option["evidence_option_id"]
        value = perceptions[option_id]
        target = value["same_level_meaningful_target"]["judgement"]
        challenge = value["challenge_or_break_realized"]["judgement"]
        if target == "SUPPORTS" and challenge == "SUPPORTS":
            supporting.append(option)
        elif target == "CONTRADICTS" or (
            target == "SUPPORTS" and challenge == "CONTRADICTS"
        ):
            decisive_negative.append(option)
        else:
            unresolved.append(option)
    if supporting:
        result = "PASS"
        primary = supporting[0]["evidence_option_id"]
    elif unresolved:
        result = "UNKNOWN"
        primary = None
    else:
        result = "FAIL"
        primary = decisive_negative[0]["evidence_option_id"]
    return {
        "result": result,
        "primary_evidence_option_id": primary,
        "supporting_option_ids": [row["evidence_option_id"] for row in supporting],
        "decisive_negative_option_ids": [
            row["evidence_option_id"] for row in decisive_negative
        ],
        "unresolved_option_ids": [row["evidence_option_id"] for row in unresolved],
    }


def validate_stage_b1a1e_response(
    *, response: dict[str, Any], schema: dict[str, Any], packet: dict[str, Any]
) -> dict[str, Any]:
    errors = _schema_errors(response, schema)
    if errors:
        return {"status": "INVALID", "errors": errors}

    expected_candidates = {
        row["candidate_id"] for row in packet["selected_focus_segments"]
    }
    submitted = [row["candidate_id"] for row in response["candidate_perceptions"]]
    if len(submitted) != len(set(submitted)):
        errors.append("candidate_id duplicated")
    if set(submitted) != expected_candidates:
        errors.append("candidate_id set differs from input")

    options_by_candidate: dict[str, dict[str, list[dict[str, Any]]]] = {}
    option_index = {}
    for option in packet["evidence_options"]:
        option_id = option["evidence_option_id"]
        option_index[option_id] = option
        options_by_candidate.setdefault(option["candidate_id"], {}).setdefault(
            option["atom_name"], []
        ).append(option)

    derived = []
    for row in response["candidate_perceptions"]:
        candidate_id = row["candidate_id"]
        if candidate_id not in expected_candidates:
            continue
        expected_directional = options_by_candidate[candidate_id]["directional_coherence"]
        if len(expected_directional) != 1:
            errors.append(f"{candidate_id}:input directional option count is not one")
            continue
        directional = row["directional_path"]
        if directional["evidence_option_id"] != expected_directional[0]["evidence_option_id"]:
            errors.append(f"{candidate_id}:directional option id mismatch")
        _validate_judgement(directional, f"{candidate_id}:directional_path", errors)

        expected_structural = {
            option["evidence_option_id"]: option
            for option in options_by_candidate[candidate_id][
                "structural_challenge_or_break"
            ]
        }
        submitted_structural = [
            value["evidence_option_id"] for value in row["structural_targets"]
        ]
        if len(submitted_structural) != len(set(submitted_structural)):
            errors.append(f"{candidate_id}:structural option duplicated")
        if set(submitted_structural) != set(expected_structural):
            errors.append(f"{candidate_id}:structural option set differs from input")
        perception_index = {}
        for value in row["structural_targets"]:
            option_id = value["evidence_option_id"]
            if option_id not in expected_structural:
                continue
            perception_index[option_id] = value
            _validate_judgement(
                value["same_level_meaningful_target"],
                f"{candidate_id}:{option_id}:same_level",
                errors,
            )
            _validate_judgement(
                value["challenge_or_break_realized"],
                f"{candidate_id}:{option_id}:challenge",
                errors,
            )
            if (
                not expected_structural[option_id]["target"][
                    "price_reached_or_crossed_objective"
                ]
                and value["challenge_or_break_realized"]["judgement"]
                != "CONTRADICTS"
            ):
                errors.append(
                    f"{candidate_id}:{option_id}:unreached target must be CONTRADICTS"
                )

        if set(perception_index) != set(expected_structural):
            continue
        directional_map = {
            "SUPPORTS": "PASS",
            "CONTRADICTS": "FAIL",
            "INSUFFICIENT": "UNKNOWN",
        }
        directional_result = directional_map[directional["judgement"]]
        structural_result = _aggregate_structural(
            list(expected_structural.values()), perception_index
        )
        derived.append(
            {
                "candidate_id": candidate_id,
                "directional_coherence": {
                    "result": directional_result,
                    "primary_evidence_option_id": (
                        directional["evidence_option_id"]
                        if directional_result != "UNKNOWN"
                        else None
                    ),
                },
                "structural_challenge_or_break": structural_result,
                "partial_eligible": directional_result == "PASS"
                and structural_result["result"] == "PASS",
            }
        )

    if errors:
        return {"status": "INVALID", "errors": sorted(set(errors))}
    derived.sort(key=lambda row: row["candidate_id"])
    partial_eligible_ids = [
        row["candidate_id"] for row in derived if row["partial_eligible"]
    ]
    return {
        "status": "VALID",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "submitted_candidate_count": len(derived),
        "derived_candidate_results": derived,
        "partial_eligible_ids": partial_eligible_ids,
        "validator_contract": {
            "ai_evidence_perceptions_repaired": False,
            "ai_evidence_perceptions_upgraded": False,
            "aggregate_results_derived_by_frozen_program": True,
            "future_performance_used": False,
            "identity_used": False,
            "sealed_labels_used": False,
        },
        "errors": [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = validate_stage_b1a1e_response(
        response=load_json(args.response),
        schema=load_json(args.schema),
        packet=load_json(args.packet),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "VALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
