import json
from pathlib import Path

from scripts.v2_core_stage_b1a2_active_link_compare_v1 import compare
from scripts.v2_core_stage_b1a2_active_link_validation_input_v1 import generate_all
from scripts.v2_core_stage_b1a2_active_link_validation_manifest_v1 import generate
from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)


def test_validation_inputs_use_second_r4_batch_and_exclude_calibration_answers() -> None:
    manifest = generate_all(ARTIFACT_DIR)
    assert manifest["case_count"] == 4
    assert manifest["candidate_count"] > 0
    assert manifest["calibration_b1a2_answers_used"] is False
    calibration = load_json(
        ARTIFACT_DIR / "stage_b1a2_active_link_input_manifest_candidate_r1.json"
    )
    assert {row["review_id"] for row in manifest["rows"]}.isdisjoint(
        {row["review_id"] for row in calibration["rows"]}
    )
    assert manifest["source_r4_execution_manifest_file"] == (
        "stage_b1a1e_primary_gate_validation_execution_manifest_r1.json"
    )


def test_validation_packets_are_blind_and_option_ids_are_unique() -> None:
    manifest = generate_all(ARTIFACT_DIR)
    seen: set[str] = set()
    for row in manifest["rows"]:
        packet = load_json(
            ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"]
        )
        assert packet["packet_version"] == "v2-core-stage-b1a2-active-link-heldout-input-r1"
        assert packet["review_constraints"]["future_performance_blind"] is True
        assert packet["review_constraints"]["identity_blind"] is True
        serialized = json.dumps(packet, ensure_ascii=False)
        assert "MFE" not in serialized and "MAE" not in serialized
        for option in packet["role_link_evidence_options"]:
            assert option["evidence_option_id"] not in seen
            seen.add(option["evidence_option_id"])


def test_validation_manifest_reuses_frozen_contract() -> None:
    manifest = generate(ARTIFACT_DIR)
    assert manifest["case_count"] == 4
    assert manifest["expected_case_rounds"] == 12
    assert manifest["formal_model"] == "gpt-5.6-sol"
    assert manifest["reasoning_effort"] == "xhigh"
    assert manifest["prompt_file"] == "v2_core_stage_b1a2_active_link.prompt.candidate_r1.md"
    assert manifest["schema_file"] == "v2_core_stage_b1a2_active_link.schema.candidate_r1.json"
    assert manifest["calibration_b1a2_answers_used"] is False
    assert set(manifest["bindings"]) == {
        "input_manifest_sha256",
        "prompt_sha256",
        "schema_sha256",
        "design_sha256",
        "policy_sha256",
        "runner_sha256",
        "validator_sha256",
        "comparator_sha256",
    }


def test_validation_report_never_publishes_partial_metrics() -> None:
    manifest = generate(ARTIFACT_DIR)
    report = compare(
        artifact_dir=ARTIFACT_DIR,
        manifest_path=ARTIFACT_DIR / "stage_b1a2_active_link_validation_execution_manifest_r1.json",
    )
    if report["completed_case_rounds"] < report["expected_case_rounds"]:
        assert report["status"] == "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）"
        assert report["metrics_published"] is False
        assert report["metrics"] is None
    else:
        assert report["metrics_published"] is True
