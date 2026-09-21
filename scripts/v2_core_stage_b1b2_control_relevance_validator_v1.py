"""Validate B1b2 current-control perceptions and derive a relevance pool."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


REASONS = {
    "SUPPORTS": {"BOUNDARY_STILL_GOVERNS_CURRENT_CONTEXT", "NESTED_SCALE_RETAINS_INDEPENDENT_CONTROL"},
    "CONTRADICTS": {
        "LATER_CONFIRMED_STRUCTURE_SUPERSEDES_CONTROL",
        "INTERNAL_LEG_WITHOUT_INDEPENDENT_CONTROL",
        "DIRECTIONAL_CONTROL_REPLACED_BEFORE_ASOF",
    },
    "INSUFFICIENT": {"COMPETING_SCALES_PREVENT_CONTROL_DECISION", "VISIBLE_PATHS_DO_NOT_RESOLVE_REPLACEMENT"},
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_response(*, response: dict[str, Any], schema: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    errors = [
        f"SCHEMA:{'/'.join(str(part) for part in error.absolute_path)}:{error.message}"
        for error in sorted(Draft202012Validator(schema).iter_errors(response), key=lambda item: list(item.path))
    ]
    expected = {str(value) for value in packet["control_candidate_ids"]}
    submitted = [str(row["candidate_id"]) for row in response.get("candidate_control_perceptions", [])]
    if len(submitted) != len(set(submitted)):
        errors.append("candidate_id duplicated")
    if set(submitted) != expected:
        errors.append("candidate_id set differs from input")
    segments = {str(row["candidate_id"]): row for row in packet["candidate_segments"]}
    options = {str(row["candidate_id"]): row for row in packet["control_relevance_evidence_options"]}
    if set(segments) != expected or set(options) != expected:
        errors.append("packet candidate sets differ")
    for candidate_id in expected:
        if segments[candidate_id]["status"] != "CONFIRMED" or options[candidate_id]["candidate_status"] != "CONFIRMED":
            errors.append(f"{candidate_id}:control candidate is not confirmed")
        if options[candidate_id]["atom_name"] != "current_control_relevance":
            errors.append(f"{candidate_id}:wrong atom name")
        if not options[candidate_id]["fixed_relation_paths"]:
            errors.append(f"{candidate_id}:missing fixed path")
    result_map = {"SUPPORTS": "PASS", "CONTRADICTS": "FAIL", "INSUFFICIENT": "UNKNOWN"}
    derived = []
    for row in response.get("candidate_control_perceptions", []):
        candidate_id = str(row["candidate_id"])
        if candidate_id not in expected or candidate_id not in options:
            continue
        atom = row["current_control_relevance"]
        option = options[candidate_id]
        if atom["evidence_option_id"] != option["evidence_option_id"]:
            errors.append(f"{candidate_id}:evidence option mismatch")
        if atom["reason_code"] not in REASONS[atom["judgement"]]:
            errors.append(f"{candidate_id}:reason_code does not match judgement")
        result = result_map[atom["judgement"]]
        derived.append(
            {
                "candidate_id": candidate_id,
                "current_control_relevance": {
                    "result": result,
                    "reason_code": atom["reason_code"],
                    "primary_evidence_option_id": atom["evidence_option_id"],
                },
                "control_relevant": result == "PASS",
            }
        )
    if errors:
        return {"status": "INVALID", "errors": sorted(set(errors))}
    derived.sort(key=lambda row: row["candidate_id"])
    return {
        "status": "VALID",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "submitted_candidate_count": len(derived),
        "derived_candidate_results": derived,
        "control_relevant_ids": [row["candidate_id"] for row in derived if row["control_relevant"]],
        "validator_contract": {
            "ai_perceptions_repaired": False,
            "ai_perceptions_upgraded": False,
            "aggregate_results_derived_by_frozen_program": True,
            "unique_controlling_anchor_selected": False,
            "trade_permission_granted": False,
            "future_performance_used": False,
            "identity_used": False,
            "sealed_labels_used": False,
        },
        "errors": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    args = parser.parse_args()
    result = validate_response(response=load_json(args.response), schema=load_json(args.schema), packet=load_json(args.packet))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "VALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
