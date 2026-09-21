import json
from pathlib import Path

from scripts.v2_core_stage_b1b1_parent_source_compare_v1 import compare
from scripts.v2_core_stage_b1b1_parent_source_input_v1 import generate_all
from scripts.v2_core_stage_b1b1_parent_source_manifest_v1 import generate
from scripts.v2_core_stage_b1b1_parent_source_validator_v1 import load_json, validate_response


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"


def test_input_is_deterministic_blind_and_contains_only_confirmed_parent_candidates():
    first = generate_all(ARTIFACT_DIR)
    second = generate_all(ARTIFACT_DIR)
    assert first == second
    assert first["case_count"] == 5
    assert first["candidate_count"] > 0
    assert first["future_performance_used"] is False
    assert first["identity_used"] is False
    assert first["sealed_labels_used"] is False
    assert first["prior_ai_role_answers_used"] is False
    for row in first["rows"]:
        packet = load_json(ARTIFACT_DIR / first["input_packet_directory"] / row["input_packet_file"])
        assert len(packet["parent_candidate_ids"]) == row["candidate_count"]
        assert all(candidate["status"] == "CONFIRMED" for candidate in packet["candidate_segments"])
        assert all(option["fixed_relation_paths"] for option in packet["parent_source_evidence_options"])
        assert all(option["candidate_status"] == "CONFIRMED" for option in packet["parent_source_evidence_options"])
        assert packet["future_performance_used"] is False
        assert packet["identity_used"] is False
        assert packet["sealed_labels_used"] is False
        assert packet["prior_ai_role_answers_used"] is False


def test_validator_accepts_exact_atomic_contract_and_rejects_reason_mismatch():
    source = generate_all(ARTIFACT_DIR)
    row = source["rows"][0]
    packet = load_json(ARTIFACT_DIR / source["input_packet_directory"] / row["input_packet_file"])
    schema = load_json(ARTIFACT_DIR / "v2_core_stage_b1b1_parent_source.schema.candidate_r1.json")
    options = {item["candidate_id"]: item for item in packet["parent_source_evidence_options"]}
    response = {
        "schema_version": "v2-core-stage-b1b1-parent-source-r1-candidate",
        "candidate_parent_perceptions": [
            {
                "candidate_id": candidate_id,
                "parent_source_relation": {
                    "judgement": "SUPPORTS",
                    "reason_code": "COMPLETED_PREDECESSOR_GENERATES_CURRENT_CHAIN",
                    "evidence_option_id": options[candidate_id]["evidence_option_id"],
                },
            }
            for candidate_id in packet["parent_candidate_ids"]
        ],
    }
    valid = validate_response(response=response, schema=schema, packet=packet)
    assert valid["status"] == "VALID"
    assert valid["submitted_candidate_count"] == len(packet["parent_candidate_ids"])
    assert valid["parent_source_eligible_ids"] == sorted(packet["parent_candidate_ids"])
    response["candidate_parent_perceptions"][0]["parent_source_relation"]["reason_code"] = (
        "FIXED_PATH_DOES_NOT_ESTABLISH_GENERATIVE_ROLE"
    )
    invalid = validate_response(response=response, schema=schema, packet=packet)
    assert invalid["status"] == "INVALID"
    assert any("reason_code does not match judgement" in error for error in invalid["errors"])


def test_execution_manifest_freezes_model_budget_and_all_implementation_bindings():
    manifest = generate(ARTIFACT_DIR)
    assert manifest["formal_model"] == "gpt-5.6-sol"
    assert manifest["reasoning_effort"] == "xhigh"
    assert manifest["required_rounds"] == 3
    assert manifest["expected_case_rounds"] == manifest["case_count"] * 3
    assert manifest["stop_remaining_percent_lte"] == 60
    assert manifest["single_atom_only"] == "parent_source_relation"
    assert manifest["final_course_role_assigned"] is False
    assert manifest["trade_permission_granted"] is False
    for key in (
        "input_manifest_sha256",
        "prompt_sha256",
        "schema_sha256",
        "design_sha256",
        "operational_policy_sha256",
        "runner_sha256",
        "validator_sha256",
        "comparator_sha256",
    ):
        assert len(manifest[key]) == 64


def test_comparator_with_no_runs_publishes_no_metrics(tmp_path):
    manifest = generate(ARTIFACT_DIR)
    isolated = dict(manifest)
    isolated["run_directory"] = "stage_b1b1_parent_source_runs_missing_for_test"
    isolated_manifest = tmp_path / "manifest.json"
    isolated_manifest.write_text(json.dumps(isolated, ensure_ascii=False), encoding="utf-8")
    report = compare(
        artifact_dir=ARTIFACT_DIR,
        manifest_path=isolated_manifest,
    )
    assert report["status"] == "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）"
    assert report["completed_case_rounds"] == 0
    assert len(report["missing_case_rounds"]) == manifest["expected_case_rounds"]
    assert report["metrics_published"] is False
    assert report["metrics"] is None
