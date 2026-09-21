from __future__ import annotations

import json

from scripts.v2_core_objective_candidate_catalog_v1 import sha256_path
from scripts.v2_core_stage_b1a1e_focus_input_v2 import (
    ARTIFACT_DIR,
    OUTPUT_DIRECTORY,
    generate_all,
    load_json,
)


def test_r2_preserves_22_candidates_and_stays_answer_blind():
    manifest = generate_all(ARTIFACT_DIR)
    assert manifest["case_count"] == 4
    assert manifest["selected_candidate_count"] == 22
    assert manifest["formal_ai_calls"] == 0
    assert manifest["ready_for_formal_ai"] is False
    assert manifest["prior_answers_present_in_ai_packets"] is False
    for row in manifest["rows"]:
        path = ARTIFACT_DIR / OUTPUT_DIRECTORY / row["input_packet_file"]
        assert sha256_path(path) == row["input_packet_sha256"]
        rendered = json.dumps(load_json(path), ensure_ascii=False)
        assert "selection_role" not in rendered
        assert "primary_evidence_refs" not in rendered


def test_auxiliary_candidates_receive_large_and_small_targets_when_available():
    manifest = generate_all(ARTIFACT_DIR)
    checked = 0
    for row in manifest["rows"]:
        packet = load_json(ARTIFACT_DIR / OUTPUT_DIRECTORY / row["input_packet_file"])
        for segment in packet["selected_focus_segments"]:
            if segment["scale"] != "AUXILIARY":
                continue
            checked += 1
            scales = {
                option["target"]["target_scale"]
                for option in packet["evidence_options"]
                if option["candidate_id"] == segment["candidate_id"]
                and option["atom_name"] == "structural_challenge_or_break"
            }
            assert scales <= {"SMALL", "LARGE"}
            assert scales
    assert checked == manifest["auxiliary_candidate_count"]
    assert checked > 0


def test_directional_options_add_only_objective_ratio():
    manifest = generate_all(ARTIFACT_DIR)
    for row in manifest["rows"]:
        packet = load_json(ARTIFACT_DIR / OUTPUT_DIRECTORY / row["input_packet_file"])
        for option in packet["evidence_options"]:
            if option["atom_name"] != "directional_coherence":
                continue
            metrics = option["path"]["objective_metrics"]
            assert "countermove_to_directional_ratio" in metrics
            if metrics["countermove_to_directional_ratio"] is not None:
                assert metrics["countermove_to_directional_ratio"] >= 0
