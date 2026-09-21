from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_core_objective_candidate_catalog_v1 import sha256_path
from scripts.v2_core_stage_b1a1_candidate_validator_v2 import (
    ATOMIC_FIELDS,
    validate_stage_b1a1_response,
)
from scripts.v2_core_stage_b1a1_input_packet_v2 import (
    ARTIFACT_DIR,
    OUTPUT_DIRECTORY,
    build_stage_b1a1_input,
    generate_all,
    load_json,
)


def _first_packet_inputs():
    source_manifest = load_json(ARTIFACT_DIR / "stage_b1a_input_manifest_candidate_r1.json")
    focus_manifest = load_json(ARTIFACT_DIR / "objective_focus_catalog_manifest_r1.json")
    relation_manifest = load_json(
        ARTIFACT_DIR / "objective_relation_catalog_manifest_r1.json"
    )
    source_row = source_manifest["rows"][0]
    review_id = source_row["review_id"]
    focus_row = next(row for row in focus_manifest["rows"] if row["review_id"] == review_id)
    relation_row = next(
        row for row in relation_manifest["rows"] if row["review_id"] == review_id
    )
    source_path = ARTIFACT_DIR / "stage_b1a_input_packets_candidate_r1" / source_row[
        "input_packet_file"
    ]
    focus_path = ARTIFACT_DIR / "objective_focus_catalogs_r1" / focus_row[
        "focus_catalog_file"
    ]
    relation_path = ARTIFACT_DIR / "objective_relation_catalogs_r1" / relation_row[
        "relation_catalog_file"
    ]
    return source_path, focus_path, relation_path


def _unknown():
    return {
        "result": "UNKNOWN",
        "primary_evidence_refs": [],
        "supporting_evidence_refs": [],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": ["MISSING_FIXED_RELATION"],
        "reason_code": "REQUIRED_EVIDENCE_MISSING",
    }


def test_r2_input_adds_only_fixed_focus_relations_and_stays_blind():
    source_path, focus_path, relation_path = _first_packet_inputs()
    packet = build_stage_b1a1_input(
        source_input=load_json(source_path),
        source_input_sha256=sha256_path(source_path),
        relation_catalog=load_json(relation_path),
        relation_catalog_sha256=sha256_path(relation_path),
        focus_catalog=load_json(focus_path),
        focus_catalog_sha256=sha256_path(focus_path),
    )
    focus = load_json(focus_path)
    assert [row["candidate_id"] for row in packet["focus_relations"]] == focus[
        "focus_relation_ids"
    ]
    relation_refs = {
        row["ref"] for row in packet["evidence_catalog"] if row["kind"] == "RELATION"
    }
    assert relation_refs == set(focus["focus_relation_ids"])
    segment_ids = {row["candidate_id"] for row in packet["focus_segments"]}
    for relation in packet["focus_relations"]:
        related = {
            value
            for key, value in relation.items()
            if key.endswith("_segment_id") and value is not None
        }
        assert related <= segment_ids
    rendered = json.dumps(packet, ensure_ascii=False).lower()
    assert "mfe" not in rendered
    assert "mae" not in rendered
    assert "profit" not in rendered


def test_unknown_response_is_valid_and_program_does_not_invent_eligibility():
    manifest = generate_all(ARTIFACT_DIR)
    row = manifest["rows"][0]
    packet = load_json(ARTIFACT_DIR / OUTPUT_DIRECTORY / row["input_packet_file"])
    schema = load_json(ARTIFACT_DIR / manifest["output_schema_file"])
    response = {
        "schema_version": "v2-core-stage-b1a1-r2-candidate",
        "candidate_assessments": [
            {
                "candidate_id": segment["candidate_id"],
                **{field: _unknown() for field in ATOMIC_FIELDS},
            }
            for segment in packet["focus_segments"]
        ],
    }
    result = validate_stage_b1a1_response(response=response, schema=schema, packet=packet)
    assert result["status"] == "VALID"
    assert result["eligible_count"] == 0
    assert result["validator_contract"]["course_judgement_generated_by_program"] is False


def test_relation_link_pass_must_use_relation_that_contains_candidate():
    manifest = generate_all(ARTIFACT_DIR)
    row = manifest["rows"][0]
    packet = load_json(ARTIFACT_DIR / OUTPUT_DIRECTORY / row["input_packet_file"])
    schema = load_json(ARTIFACT_DIR / manifest["output_schema_file"])
    response = {
        "schema_version": "v2-core-stage-b1a1-r2-candidate",
        "candidate_assessments": [
            {
                "candidate_id": segment["candidate_id"],
                **{field: _unknown() for field in ATOMIC_FIELDS},
            }
            for segment in packet["focus_segments"]
        ],
    }
    target = response["candidate_assessments"][0]
    unrelated = next(
        relation["candidate_id"]
        for relation in packet["focus_relations"]
        if target["candidate_id"]
        not in {
            value
            for key, value in relation.items()
            if key.endswith("_segment_id") and value is not None
        }
    )
    target["as_of_relation_link"] = {
        "result": "PASS",
        "primary_evidence_refs": [unrelated],
        "supporting_evidence_refs": [],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": [],
        "reason_code": "VISIBLE_EVIDENCE_SATISFIES_DEFINITION",
    }
    result = validate_stage_b1a1_response(response=response, schema=schema, packet=packet)
    assert result["status"] == "INVALID"
    assert any("does not contain candidate" in error for error in result["errors"])


def test_all_48_r2_packets_are_hash_bound_and_not_formally_run():
    manifest = generate_all(ARTIFACT_DIR)
    assert manifest["case_count"] == 48
    assert manifest["formal_ai_calls"] == 0
    assert manifest["ready_for_formal_ai"] is False
    assert manifest["future_performance_used"] is False
    assert manifest["identity_used"] is False
    assert manifest["sealed_labels_used"] is False
    assert manifest["total_focus_relations"] > 0
    for row in manifest["rows"]:
        path = ARTIFACT_DIR / OUTPUT_DIRECTORY / row["input_packet_file"]
        packet = load_json(path)
        assert sha256_path(path) == row["input_packet_sha256"]
        assert packet["daily_structure_context_to_as_of"][-1]["date"] == packet["as_of"]
        assert len(packet["focus_relations"]) == row["focus_relation_count"]
