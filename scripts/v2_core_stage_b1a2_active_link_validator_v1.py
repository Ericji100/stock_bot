"""Validate B1a2 active-link perceptions and derive the next shortlist."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


REASON_BY_JUDGEMENT = {
    "SUPPORTS": {"VISIBLE_CHAIN_SUPPORTS_ACTIVE_LINK"},
    "CONTRADICTS": {"VISIBLE_CHAIN_CONTRADICTS_ACTIVE_LINK"},
    "INSUFFICIENT": {
        "FIXED_CHAIN_EVIDENCE_INSUFFICIENT",
        "CONFLICTING_VISIBLE_CHAINS",
    },
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_response(
    *, response: dict[str, Any], schema: dict[str, Any], packet: dict[str, Any]
) -> dict[str, Any]:
    validator = Draft202012Validator(schema)
    errors = [
        f"SCHEMA:{'/'.join(str(part) for part in error.absolute_path)}:{error.message}"
        for error in sorted(validator.iter_errors(response), key=lambda item: list(item.path))
    ]
    expected = set(str(value) for value in packet["upstream_partial_eligible_candidate_ids"])
    submitted = [str(row["candidate_id"]) for row in response.get("candidate_link_perceptions", [])]
    if len(submitted) != len(set(submitted)):
        errors.append("candidate_id duplicated")
    if set(submitted) != expected:
        errors.append("candidate_id set differs from input")

    segment_ids = {str(row["candidate_id"]) for row in packet["path_context_segments"]}
    relation_ids = {str(row["candidate_id"]) for row in packet["path_context_relations"]}
    terminals = {str(row["candidate_id"]) for row in packet["current_context_segments"]}
    options: dict[str, dict[str, Any]] = {}
    for option in packet["role_link_evidence_options"]:
        candidate_id = str(option["candidate_id"])
        if candidate_id in options:
            errors.append(f"{candidate_id}:duplicate input option")
        options[candidate_id] = option
        if option["atom_name"] != "active_campaign_link":
            errors.append(f"{candidate_id}:wrong atom name")
        for path in option["fixed_relation_paths"]:
            nodes = [str(value) for value in path["segment_ids"]]
            edges = [str(value) for value in path["relation_ids"]]
            if not nodes or nodes[0] != candidate_id:
                errors.append(f"{candidate_id}:path does not start at candidate")
            if nodes and nodes[-1] not in terminals:
                errors.append(f"{candidate_id}:path does not end at current context")
            if len(edges) != max(0, len(nodes) - 1):
                errors.append(f"{candidate_id}:path edge count mismatch")
            if not set(nodes).issubset(segment_ids):
                errors.append(f"{candidate_id}:path references missing segment")
            if not set(edges).issubset(relation_ids):
                errors.append(f"{candidate_id}:path references missing relation")
        if bool(option["no_fixed_path_found"]) == bool(option["fixed_relation_paths"]):
            errors.append(f"{candidate_id}:no-path flag mismatch")
    if set(options) != expected:
        errors.append("input option candidate set differs from upstream set")

    derived = []
    result_map = {"SUPPORTS": "PASS", "CONTRADICTS": "FAIL", "INSUFFICIENT": "UNKNOWN"}
    for row in response.get("candidate_link_perceptions", []):
        candidate_id = str(row["candidate_id"])
        if candidate_id not in expected or candidate_id not in options:
            continue
        value = row["active_campaign_link"]
        option = options[candidate_id]
        if value["evidence_option_id"] != option["evidence_option_id"]:
            errors.append(f"{candidate_id}:evidence option mismatch")
        if value["reason_code"] not in REASON_BY_JUDGEMENT[value["judgement"]]:
            errors.append(f"{candidate_id}:reason_code does not match judgement")
        if option["no_fixed_path_found"] and value["judgement"] == "SUPPORTS":
            errors.append(f"{candidate_id}:SUPPORTS without a fixed path")
        result = result_map[value["judgement"]]
        derived.append(
            {
                "candidate_id": candidate_id,
                "active_campaign_link": {
                    "result": result,
                    "primary_evidence_option_id": (
                        value["evidence_option_id"] if result != "UNKNOWN" else None
                    ),
                },
                "b1a2_shortlisted": result == "PASS",
            }
        )
    if errors:
        return {"status": "INVALID", "errors": sorted(set(errors))}
    derived.sort(key=lambda row: str(row["candidate_id"]))
    return {
        "status": "VALID",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "submitted_candidate_count": len(derived),
        "derived_candidate_results": derived,
        "b1a2_shortlist_ids": [
            row["candidate_id"] for row in derived if row["b1a2_shortlisted"]
        ],
        "validator_contract": {
            "ai_perceptions_repaired": False,
            "ai_perceptions_upgraded": False,
            "aggregate_results_derived_by_frozen_program": True,
            "course_role_assigned": False,
            "controlling_anchor_decided": False,
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
    result = validate_response(
        response=load_json(args.response), schema=load_json(args.schema), packet=load_json(args.packet)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "VALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
