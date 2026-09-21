from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_heldout_manifest_uses_fresh_cases_and_frozen_r4_contract() -> None:
    manifest = load_json(ARTIFACT_DIR / "stage_b1a1e_primary_gate_validation_execution_manifest_r1.json")
    prior_names = (
        "stage_b1a1e_smoke_execution_manifest_candidate_r2.json",
        "stage_b1a1e_validation_execution_manifest_r1.json",
        "stage_b1a1e_primary_execution_manifest_candidate_r3.json",
        "stage_b1a1e_primary_gate_execution_manifest_candidate_r4.json",
    )
    prior_ids = {
        str(row["review_id"])
        for name in prior_names
        for row in load_json(ARTIFACT_DIR / name)["rows"]
    }
    current_ids = {str(row["review_id"]) for row in manifest["rows"]}
    assert len(current_ids) == 4
    assert not prior_ids.intersection(current_ids)
    assert manifest["expected_case_rounds"] == 12
    assert manifest["formal_model"] == "gpt-5.6-sol"
    assert manifest["reasoning_effort"] == "xhigh"
    assert manifest["stop_remaining_percent_lte"] == 3
    assert manifest["prompt_file"] == "v2_core_stage_b1a1e_primary_gate.prompt.candidate_r4.md"
    assert manifest["schema_file"] == "v2_core_stage_b1a1e_primary_gate.schema.candidate_r4.json"
    assert manifest["runner_file"] == "scripts/v2_core_stage_b1a1e_primary_gate_runner_v1.py"
    assert manifest["validator_file"] == "scripts/v2_core_stage_b1a1e_primary_gate_validator_v1.py"


def test_heldout_ai_packets_hide_program_gate_and_forbidden_data() -> None:
    manifest = load_json(ARTIFACT_DIR / "stage_b1a1e_primary_gate_validation_execution_manifest_r1.json")
    for row in manifest["rows"]:
        packet = load_json(ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"])
        text = json.dumps(packet, ensure_ascii=False).lower()
        assert "objective_same_scale_contact" not in text
        assert "supporting_evidence_option_ids" not in text
        assert "mfe" not in text
        assert "mae" not in text
        assert "profit" not in text
        assert packet["review_constraints"]["future_performance_blind"] is True
        assert packet["review_constraints"]["identity_blind"] is True
        assert packet["review_constraints"]["sealed_labels_blind"] is True


def test_heldout_program_gate_is_objective_and_candidate_sets_match() -> None:
    manifest = load_json(ARTIFACT_DIR / "stage_b1a1e_primary_gate_validation_execution_manifest_r1.json")
    for row in manifest["rows"]:
        packet = load_json(ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"])
        gate = load_json(ARTIFACT_DIR / manifest["program_gate_directory"] / row["program_gate_file"])
        packet_ids = {str(value["candidate_id"]) for value in packet["selected_focus_segments"]}
        gate_ids = {str(value["candidate_id"]) for value in gate["candidate_rows"]}
        assert packet_ids == gate_ids
        assert all(str(value["scale"]) in {"LARGE", "SMALL"} for value in packet["selected_focus_segments"])
        assert gate["course_judgement_generated"] is False
        assert gate["future_performance_used"] is False
        assert gate["identity_used"] is False
        assert gate["sealed_labels_used"] is False
