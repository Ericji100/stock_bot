"""Validate Stage B1a fixed-segment AI semantics without making course judgements.

The validator binds a raw AI response to one public AS-OF probe packet and its
versioned objective candidate/focus catalogs.  It never repairs or upgrades an
AI verdict.  Its derived ``deep_review_pool_ids`` are merely the deterministic
intersection of three PASS verdicts already supplied by the AI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ATOMIC_FIELDS = (
    "coarse_anchor_fit",
    "as_of_structural_relevance",
    "role_assignability",
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_errors(response: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for error in sorted(validator.iter_errors(response), key=lambda item: list(item.path)):
        location = "/".join(str(part) for part in error.absolute_path) or "$"
        errors.append(f"SCHEMA:{location}:{error.message}")
    return errors


def _input_binding_errors(
    *,
    focus: dict[str, Any],
    candidates: dict[str, Any],
    packet: dict[str, Any],
    candidate_catalog_path: Path | None,
    packet_path: Path | None,
) -> list[str]:
    errors: list[str] = []
    review_ids = {
        str(focus.get("review_id", "")),
        str(candidates.get("review_id", "")),
        str(packet.get("review_id", "")),
    }
    if len(review_ids) != 1 or "" in review_ids:
        errors.append("INPUT_BINDING:REVIEW_ID_MISMATCH")

    as_of_values = {
        str(focus.get("as_of", "")),
        str(candidates.get("as_of", "")),
        str(packet.get("as_of", "")),
    }
    if len(as_of_values) != 1 or "" in as_of_values:
        errors.append("INPUT_BINDING:AS_OF_MISMATCH")

    if candidate_catalog_path is not None:
        expected = str(focus.get("source_segment_catalog_sha256", ""))
        actual = sha256_file(candidate_catalog_path)
        if not expected or expected != actual:
            errors.append("INPUT_BINDING:SEGMENT_CATALOG_SHA256_MISMATCH")

    if packet_path is not None:
        expected = str(candidates.get("source_packet_sha256", ""))
        actual = sha256_file(packet_path)
        if not expected or expected != actual:
            errors.append("INPUT_BINDING:PACKET_SHA256_MISMATCH")

    return errors


def validate_stage_b1a_response(
    *,
    response: dict[str, Any],
    schema: dict[str, Any],
    focus: dict[str, Any],
    candidates: dict[str, Any],
    packet: dict[str, Any],
    candidate_catalog_path: Path | None = None,
    packet_path: Path | None = None,
) -> dict[str, Any]:
    """Return a deterministic validation report; never mutate ``response``."""

    errors = _schema_errors(response, schema)
    errors.extend(
        _input_binding_errors(
            focus=focus,
            candidates=candidates,
            packet=packet,
            candidate_catalog_path=candidate_catalog_path,
            packet_path=packet_path,
        )
    )

    expected_ids = [str(item["segment_id"]) for item in focus.get("focus_segments", [])]
    expected_set = set(expected_ids)
    submitted = response.get("segment_assessments", [])
    submitted_ids = [str(item.get("candidate_id", "")) for item in submitted if isinstance(item, dict)]
    submitted_set = set(submitted_ids)

    duplicate_ids = sorted(item for item, count in Counter(submitted_ids).items() if count > 1)
    if duplicate_ids:
        errors.append(f"SEMANTIC:DUPLICATE_CANDIDATE_IDS:{','.join(duplicate_ids)}")
    missing_ids = sorted(expected_set - submitted_set)
    if missing_ids:
        errors.append(f"SEMANTIC:MISSING_CANDIDATE_IDS:{','.join(missing_ids)}")
    extra_ids = sorted(submitted_set - expected_set)
    if extra_ids:
        errors.append(f"SEMANTIC:EXTRA_CANDIDATE_IDS:{','.join(extra_ids)}")

    segment_index = {
        str(item.get("candidate_id")): item
        for item in candidates.get("objective_segment_candidates", [])
        if isinstance(item, dict) and item.get("candidate_id")
    }
    missing_catalog_ids = sorted(expected_set - set(segment_index))
    if missing_catalog_ids:
        errors.append(f"INPUT_BINDING:FOCUS_IDS_NOT_IN_CATALOG:{','.join(missing_catalog_ids)}")

    allowed_evidence_refs = {
        str(item.get("ref"))
        for item in packet.get("evidence_catalog", [])
        if isinstance(item, dict) and item.get("ref")
    }
    for segment in segment_index.values():
        allowed_evidence_refs.update(str(ref) for ref in segment.get("source_evidence_refs", []))

    invalid_evidence: set[str] = set()
    forming_parent_ids: set[str] = set()
    deep_review_pool_ids: list[str] = []
    for item in submitted:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id", ""))
        for field in ATOMIC_FIELDS:
            verdict = item.get(field)
            if not isinstance(verdict, dict):
                continue
            for key in ("supporting_evidence_refs", "contradicting_evidence_refs"):
                for ref in verdict.get(key, []):
                    if str(ref) not in allowed_evidence_refs:
                        invalid_evidence.add(str(ref))

        segment = segment_index.get(candidate_id)
        if (
            segment is not None
            and segment.get("status") != "CONFIRMED"
            and "PARENT" in item.get("role_candidates", [])
        ):
            forming_parent_ids.add(candidate_id)

        if (
            candidate_id in expected_set
            and all(
                isinstance(item.get(field), dict) and item[field].get("result") == "PASS"
                for field in ATOMIC_FIELDS
            )
            and bool(item.get("role_candidates"))
        ):
            deep_review_pool_ids.append(candidate_id)

    if invalid_evidence:
        errors.append(f"SEMANTIC:UNKNOWN_EVIDENCE_REFS:{','.join(sorted(invalid_evidence))}")
    if forming_parent_ids:
        errors.append(f"SEMANTIC:UNCONFIRMED_SEGMENT_AS_PARENT:{','.join(sorted(forming_parent_ids))}")

    errors = sorted(set(errors))
    return {
        "status": "VALID" if not errors else "INVALID",
        "review_id": focus.get("review_id"),
        "as_of": focus.get("as_of"),
        "expected_segment_count": len(expected_ids),
        "submitted_segment_count": len(submitted_ids),
        "deep_review_pool_count": len(set(deep_review_pool_ids)),
        "deep_review_pool_ids": sorted(set(deep_review_pool_ids)),
        "error_count": len(errors),
        "errors": errors,
        "validator_contract": {
            "ai_verdicts_repaired": False,
            "ai_verdicts_upgraded": False,
            "course_judgement_generated_by_program": False,
            "future_performance_used": False,
            "identity_used": False,
            "sealed_labels_used": False,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--focus", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = validate_stage_b1a_response(
        response=load_json(args.response),
        schema=load_json(args.schema),
        focus=load_json(args.focus),
        candidates=load_json(args.candidates),
        packet=load_json(args.packet),
        candidate_catalog_path=args.candidates,
        packet_path=args.packet,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["status"] == "VALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
