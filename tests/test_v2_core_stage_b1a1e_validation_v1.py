from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_core_stage_b1a1e_smoke_compare_v1 import compare_smoke
from scripts.v2_core_stage_b1a1e_validation_input_v1 import (
    ARTIFACT_DIR,
    CASE_COUNT,
    MAX_CANDIDATES_PER_CASE,
    generate_all,
)
from scripts.v2_core_stage_b1a1e_validation_manifest_v1 import build_manifest


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def test_heldout_selection_is_answer_blind_and_disjoint():
    manifest = generate_all(ARTIFACT_DIR)
    selection = _load(ARTIFACT_DIR / "stage_b1a1e_validation_selection_r1.json")
    calibration = _load(
        ARTIFACT_DIR / "stage_b1a1e_smoke_execution_manifest_candidate_r2.json"
    )
    calibration_ids = {row["review_id"] for row in calibration["rows"]}
    validation_ids = {row["review_id"] for row in selection["rows"]}
    assert len(validation_ids) == CASE_COUNT
    assert validation_ids.isdisjoint(calibration_ids)
    assert selection["uses_prior_ai_answers"] is False
    assert selection["uses_future_performance"] is False
    assert manifest["selection_uses_prior_calibration_outputs"] is False
    for row in selection["rows"]:
        assert 1 <= len(row["selected_candidates"]) <= MAX_CANDIDATES_PER_CASE


def test_packets_are_blind_and_have_frozen_options():
    manifest = generate_all(ARTIFACT_DIR)
    for row in manifest["rows"]:
        packet = _load(
            ARTIFACT_DIR
            / manifest["input_packet_directory"]
            / row["input_packet_file"]
        )
        constraints = packet["review_constraints"]
        assert constraints["future_performance_blind"] is True
        assert constraints["identity_blind"] is True
        assert constraints["sealed_labels_blind"] is True
        assert constraints["selection_uses_prior_ai_answers"] is False
        assert constraints["held_out_from_r2_calibration"] is True
        candidate_ids = {row["candidate_id"] for row in packet["selected_focus_segments"]}
        option_candidate_ids = {row["candidate_id"] for row in packet["evidence_options"]}
        assert candidate_ids == option_candidate_ids


def test_execution_manifest_and_empty_preflight(tmp_path: Path):
    manifest = build_manifest(ARTIFACT_DIR)
    assert manifest["case_count"] == CASE_COUNT
    assert manifest["expected_case_rounds"] == CASE_COUNT * 3
    assert manifest["stop_remaining_percent_lte"] == 20
    assert manifest["selection_uses_prior_calibration_outputs"] is False
    report = compare_smoke(
        manifest_path=ARTIFACT_DIR / "stage_b1a1e_validation_execution_manifest_r1.json",
        artifact_dir=ARTIFACT_DIR,
        run_dir=tmp_path,
    )
    assert report["completed_case_rounds"] == 0
    assert report["metrics_published"] is False
    assert report["metrics"] is None
