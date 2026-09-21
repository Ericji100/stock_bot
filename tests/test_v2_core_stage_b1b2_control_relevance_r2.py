from pathlib import Path

from scripts.v2_core_stage_b1b2_control_relevance_input_r2 import generate_all
from scripts.v2_core_stage_b1b2_control_relevance_manifest_r2 import generate
from scripts.v2_core_stage_b1b2_control_relevance_validator_v1 import load_json


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"


def test_r2_supplies_a_snapshot_for_every_fixed_path_segment():
    source = generate_all(ARTIFACT_DIR)
    for row in source["rows"]:
        packet = load_json(ARTIFACT_DIR / source["input_packet_directory"] / row["input_packet_file"])
        visible = {
            str(item["candidate_id"])
            for key in ("candidate_segments", "current_context_segments", "relation_path_segments")
            for item in packet[key]
        }
        referenced = {
            str(segment_id)
            for option in packet["control_relevance_evidence_options"]
            for path in option["fixed_relation_paths"]
            for segment_id in path["segment_ids"]
        }
        assert referenced <= visible
        assert packet["all_fixed_path_segment_snapshots_present"] is True
        assert packet["future_performance_used"] is False
        assert packet["identity_used"] is False


def test_r2_manifest_keeps_atom_and_threshold_but_binds_new_evidence_complete_prompt():
    manifest = generate(ARTIFACT_DIR)
    assert manifest["single_atom_only"] == "current_control_relevance"
    assert manifest["all_fixed_path_segment_snapshots_present"] is True
    assert manifest["stop_remaining_percent_lte"] == 60
    assert manifest["prompt_file"].endswith("candidate_r2.md")
    assert manifest["expected_case_rounds"] == 15
