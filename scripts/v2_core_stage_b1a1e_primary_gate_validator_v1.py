"""Validate R4 direction perceptions and derive the objective primary gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


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


def validate_response(
    *,
    response: dict[str, Any],
    schema: dict[str, Any],
    packet: dict[str, Any],
    program_gate: dict[str, Any],
) -> dict[str, Any]:
    validator = Draft202012Validator(schema)
    errors = [
        f"SCHEMA:{'/'.join(str(part) for part in error.absolute_path)}:{error.message}"
        for error in sorted(validator.iter_errors(response), key=lambda item: list(item.path))
    ]
    expected_candidates = {
        str(row["candidate_id"]) for row in packet["selected_focus_segments"]
    }
    submitted = [str(row["candidate_id"]) for row in response.get("candidate_perceptions", [])]
    if len(submitted) != len(set(submitted)):
        errors.append("candidate_id duplicated")
    if set(submitted) != expected_candidates:
        errors.append("candidate_id set differs from input")

    options_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for option in packet["evidence_options"]:
        if option["atom_name"] != "directional_coherence":
            errors.append("AI packet contains non-directional evidence option")
        options_by_candidate.setdefault(str(option["candidate_id"]), []).append(option)
    gate_rows = {
        str(row["candidate_id"]): row for row in program_gate["candidate_rows"]
    }
    if set(gate_rows) != expected_candidates:
        errors.append("program gate candidate set differs from input")
    if program_gate.get("review_id") != packet.get("review_id"):
        errors.append("program gate review_id mismatch")
    if program_gate.get("as_of") != packet.get("as_of"):
        errors.append("program gate as_of mismatch")

    directional_map = {
        "SUPPORTS": "PASS",
        "CONTRADICTS": "FAIL",
        "INSUFFICIENT": "UNKNOWN",
    }
    derived = []
    for row in response.get("candidate_perceptions", []):
        candidate_id = str(row["candidate_id"])
        if candidate_id not in expected_candidates:
            continue
        expected = options_by_candidate.get(candidate_id, [])
        if len(expected) != 1:
            errors.append(f"{candidate_id}:input directional option count is not one")
            continue
        value = row["directional_path"]
        if value["evidence_option_id"] != expected[0]["evidence_option_id"]:
            errors.append(f"{candidate_id}:directional option id mismatch")
        if value["reason_code"] not in REASON_BY_JUDGEMENT[value["judgement"]]:
            errors.append(f"{candidate_id}:reason_code does not match judgement")
        gate = gate_rows.get(candidate_id)
        if gate is None:
            continue
        if gate["candidate_scale"] not in {"LARGE", "SMALL"}:
            errors.append(f"{candidate_id}:program gate contains forbidden scale")
        contact = bool(gate["objective_same_scale_contact"])
        support_ids = list(gate["supporting_evidence_option_ids"])
        if contact != bool(support_ids):
            errors.append(f"{candidate_id}:program gate contact/support mismatch")
        directional_result = directional_map[value["judgement"]]
        derived.append(
            {
                "candidate_id": candidate_id,
                "directional_coherence": {
                    "result": directional_result,
                    "primary_evidence_option_id": (
                        value["evidence_option_id"]
                        if directional_result != "UNKNOWN"
                        else None
                    ),
                },
                "objective_same_scale_contact": contact,
                "program_contact_evidence_option_ids": support_ids,
                "partial_eligible": directional_result == "PASS" and contact,
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
        "partial_eligible_ids": [
            row["candidate_id"] for row in derived if row["partial_eligible"]
        ],
        "validator_contract": {
            "ai_directional_perceptions_repaired": False,
            "ai_directional_perceptions_upgraded": False,
            "program_gate_hidden_from_ai": True,
            "aggregate_results_derived_by_frozen_program": True,
            "controlling_anchor_or_course_role_decided": False,
            "trade_permission_granted": False,
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
    parser.add_argument("--program-gate", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = validate_response(
        response=load_json(args.response),
        schema=load_json(args.schema),
        packet=load_json(args.packet),
        program_gate=load_json(args.program_gate),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "VALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
