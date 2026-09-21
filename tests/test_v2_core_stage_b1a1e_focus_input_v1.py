from __future__ import annotations

import json

from scripts.v2_core_objective_candidate_catalog_v1 import sha256_path
from scripts.v2_core_stage_b1a1e_focus_input_v1 import (
    ARTIFACT_DIR,
    OUTPUT_DIRECTORY,
    OUTPUT_MANIFEST,
    SELECTION_FILE,
    generate_all,
    load_json,
)


def test_focus_selection_has_14_boundaries_and_stable_controls():
    manifest = generate_all(ARTIFACT_DIR)
    selection = load_json(ARTIFACT_DIR / SELECTION_FILE)
    roles = [
        selected["selection_role"]
        for row in selection["rows"]
        for selected in row["selected"]
    ]
    assert roles.count("R4_UNSTABLE_BOUNDARY") == 14
    assert roles.count("STABLE_ELIGIBLE_CONTROL") == 4
    assert roles.count("STABLE_INELIGIBLE_CONTROL") == 4
    assert manifest["selected_candidate_count"] == 22
    assert manifest["case_count"] == 4


def test_prior_answers_and_selection_roles_are_absent_from_ai_packets():
    manifest = generate_all(ARTIFACT_DIR)
    for row in manifest["rows"]:
        path = ARTIFACT_DIR / OUTPUT_DIRECTORY / row["input_packet_file"]
        packet = load_json(path)
        rendered = json.dumps(packet, ensure_ascii=False)
        assert "selection_role" not in rendered
        assert "R4_UNSTABLE_BOUNDARY" not in rendered
        assert "STABLE_ELIGIBLE_CONTROL" not in rendered
        assert "STABLE_INELIGIBLE_CONTROL" not in rendered
        assert "primary_evidence_refs" not in rendered
        assert "reason_code" not in rendered
        assert packet["review_constraints"]["prior_smoke_answers_hidden"] is True


def test_focus_packets_are_hash_bound_and_use_only_two_atoms():
    manifest = generate_all(ARTIFACT_DIR)
    assert manifest["ready_for_formal_ai"] is False
    assert manifest["formal_ai_calls"] == 0
    assert manifest["future_performance_used"] is False
    assert manifest["identity_used"] is False
    assert manifest["sealed_labels_used"] is False
    assert manifest["course_judgement_generated_by_program"] is False
    for row in manifest["rows"]:
        path = ARTIFACT_DIR / OUTPUT_DIRECTORY / row["input_packet_file"]
        assert sha256_path(path) == row["input_packet_sha256"]
        packet = load_json(path)
        assert {option["atom_name"] for option in packet["evidence_options"]} == {
            "directional_coherence",
            "structural_challenge_or_break",
        }
        candidate_ids = {
            segment["candidate_id"] for segment in packet["selected_focus_segments"]
        }
        assert {option["candidate_id"] for option in packet["evidence_options"]} == candidate_ids


def test_generation_is_deterministic():
    first = generate_all(ARTIFACT_DIR)
    second = generate_all(ARTIFACT_DIR)
    assert first == second
    assert sha256_path(ARTIFACT_DIR / OUTPUT_MANIFEST)
