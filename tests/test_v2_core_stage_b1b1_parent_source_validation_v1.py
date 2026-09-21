from pathlib import Path

from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json
from scripts.v2_core_stage_b1b1_parent_source_validation_input_v1 import generate_all
from scripts.v2_core_stage_b1b1_parent_source_validation_manifest_v1 import generate


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"


def test_heldout_selection_is_fresh_blind_and_deterministic():
    first = generate_all(ARTIFACT_DIR)
    second = generate_all(ARTIFACT_DIR)
    calibration = load_json(ARTIFACT_DIR / "stage_b1b1_parent_source_selection_candidate_r1.json")
    calibration_ids = {str(row["review_id"]) for row in calibration["rows"]}
    heldout_ids = {str(row["review_id"]) for row in first["rows"]}
    assert first == second
    assert len(heldout_ids) == 5
    assert heldout_ids.isdisjoint(calibration_ids)
    assert first["candidate_count"] > 0
    assert first["future_performance_used"] is False
    assert first["identity_used"] is False
    assert first["sealed_labels_used"] is False
    assert first["prior_ai_role_answers_used"] is False


def test_heldout_manifest_freezes_the_same_atom_and_contract():
    manifest = generate(ARTIFACT_DIR)
    assert manifest["formal_model"] == "gpt-5.6-sol"
    assert manifest["reasoning_effort"] == "xhigh"
    assert manifest["stop_remaining_percent_lte"] == 60
    assert manifest["calibration_cases_excluded"] is True
    assert manifest["single_atom_only"] == "parent_source_relation"
    assert manifest["expected_case_rounds"] == 15
    assert manifest["prior_ai_role_answers_used"] is False
