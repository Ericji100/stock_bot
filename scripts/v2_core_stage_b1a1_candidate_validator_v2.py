"""Validate Stage B1a1 R2 fixed-segment eligibility outputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ATOMIC_FIELDS = (
    "directional_coherence",
    "structural_challenge_or_break",
    "invalidation_traceability",
    "as_of_relation_link",
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _relation_segments(relation: dict[str, Any]) -> set[str]:
    return {
        str(value)
        for key, value in relation.items()
        if key.endswith("_segment_id") and value is not None
    }


def validate_stage_b1a1_response(
    *, response: dict[str, Any], schema: dict[str, Any], packet: dict[str, Any]
) -> dict[str, Any]:
    errors = sorted(
        error.message for error in Draft202012Validator(schema).iter_errors(response)
    )
    expected_ids = {str(row["candidate_id"]) for row in packet["focus_segments"]}
    submitted = response.get("candidate_assessments", [])
    submitted_ids = [str(row.get("candidate_id", "")) for row in submitted]
    if len(submitted_ids) != len(set(submitted_ids)):
        errors.append("candidate IDs are duplicated")
    if set(submitted_ids) != expected_ids:
        errors.append("candidate ID set differs from fixed focus segments")

    evidence_refs = {str(row["ref"]) for row in packet["evidence_catalog"]}
    relations = {
        str(row["candidate_id"]): row for row in packet["focus_relations"]
    }
    eligible: list[str] = []
    for row in submitted:
        candidate_id = str(row.get("candidate_id", ""))
        all_pass = True
        for field in ATOMIC_FIELDS:
            verdict = row.get(field)
            if not isinstance(verdict, dict):
                all_pass = False
                continue
            refs = []
            for key in (
                "primary_evidence_refs",
                "supporting_evidence_refs",
                "contradicting_evidence_refs",
            ):
                refs.extend(str(value) for value in verdict.get(key, []))
            missing_refs = sorted(set(refs) - evidence_refs)
            if missing_refs:
                errors.append(
                    f"{candidate_id}:{field}:unknown evidence refs {missing_refs}"
                )
            if verdict.get("result") != "PASS":
                all_pass = False

        relation_verdict = row.get("as_of_relation_link", {})
        if relation_verdict.get("result") == "PASS":
            primary = relation_verdict.get("primary_evidence_refs", [])
            if len(primary) != 1 or primary[0] not in relations:
                errors.append(
                    f"{candidate_id}:as_of_relation_link:PASS requires one fixed RELC primary ref"
                )
            elif candidate_id not in _relation_segments(relations[primary[0]]):
                errors.append(
                    f"{candidate_id}:as_of_relation_link:primary relation does not contain candidate"
                )
        if all_pass:
            eligible.append(candidate_id)

    return {
        "status": "VALID" if not errors else "INVALID",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "expected_candidate_count": len(expected_ids),
        "submitted_candidate_count": len(submitted_ids),
        "eligible_count": len(eligible),
        "eligible_ids": sorted(eligible),
        "error_count": len(set(errors)),
        "errors": sorted(set(errors)),
        "validator_contract": {
            "ai_verdicts_repaired": False,
            "ai_verdicts_upgraded": False,
            "course_judgement_generated_by_program": False,
            "roles_generated_by_program": False,
            "future_performance_used": False,
            "identity_used": False,
            "sealed_labels_used": False,
        },
    }
