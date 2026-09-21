import json
from pathlib import Path

from scripts.v2_core_stage_b1b2_control_relevance_compare_v1 import compare
from scripts.v2_core_stage_b1b2_control_relevance_input_v1 import generate_all
from scripts.v2_core_stage_b1b2_control_relevance_manifest_v1 import generate
from scripts.v2_core_stage_b1b2_control_relevance_validator_v1 import load_json, validate_response


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"


def test_input_consumes_only_three_round_parent_consensus_and_remains_blind():
    first = generate_all(ARTIFACT_DIR)
    second = generate_all(ARTIFACT_DIR)
    assert first == second
    assert first["case_count"] == 5
    assert first["candidate_count"] == 9
    assert first["upstream_parent_source_consensus_used"] is True
    assert first["upstream_parent_source_answers_exposed"] is False
    assert first["future_performance_used"] is False
    assert first["identity_used"] is False
    assert first["sealed_labels_used"] is False
    assert first["prior_ai_control_answers_used"] is False
    for row in first["rows"]:
        packet = load_json(ARTIFACT_DIR / first["input_packet_directory"] / row["input_packet_file"])
        assert len(packet["control_candidate_ids"]) == row["candidate_count"]
        assert all(item["status"] == "CONFIRMED" for item in packet["candidate_segments"])
        assert all(item["fixed_relation_paths"] for item in packet["control_relevance_evidence_options"])
        assert packet["upstream_parent_source_answers_exposed"] is False


def test_validator_accepts_exact_control_atom_and_rejects_reason_mismatch():
    source = generate_all(ARTIFACT_DIR)
    row = source["rows"][0]
    packet = load_json(ARTIFACT_DIR / source["input_packet_directory"] / row["input_packet_file"])
    schema = load_json(ARTIFACT_DIR / "v2_core_stage_b1b2_control_relevance.schema.candidate_r1.json")
    options = {item["candidate_id"]: item for item in packet["control_relevance_evidence_options"]}
    response = {
        "schema_version": "v2-core-stage-b1b2-control-relevance-r1-candidate",
        "candidate_control_perceptions": [
            {
                "candidate_id": candidate_id,
                "current_control_relevance": {
                    "judgement": "SUPPORTS",
                    "reason_code": "BOUNDARY_STILL_GOVERNS_CURRENT_CONTEXT",
                    "evidence_option_id": options[candidate_id]["evidence_option_id"],
                },
            }
            for candidate_id in packet["control_candidate_ids"]
        ],
    }
    valid = validate_response(response=response, schema=schema, packet=packet)
    assert valid["status"] == "VALID"
    assert valid["submitted_candidate_count"] == len(packet["control_candidate_ids"])
    assert valid["control_relevant_ids"] == sorted(packet["control_candidate_ids"])
    response["candidate_control_perceptions"][0]["current_control_relevance"]["reason_code"] = (
        "INTERNAL_LEG_WITHOUT_INDEPENDENT_CONTROL"
    )
    invalid = validate_response(response=response, schema=schema, packet=packet)
    assert invalid["status"] == "INVALID"
    assert any("reason_code does not match judgement" in error for error in invalid["errors"])


def test_manifest_freezes_model_atom_and_no_unique_anchor_decision():
    manifest = generate(ARTIFACT_DIR)
    assert manifest["formal_model"] == "gpt-5.6-sol"
    assert manifest["reasoning_effort"] == "xhigh"
    assert manifest["stop_remaining_percent_lte"] == 60
    assert manifest["single_atom_only"] == "current_control_relevance"
    assert manifest["unique_controlling_anchor_selected"] is False
    assert manifest["trade_permission_granted"] is False
    assert manifest["expected_case_rounds"] == 15


def test_empty_isolated_comparator_publishes_no_metrics(tmp_path):
    manifest = generate(ARTIFACT_DIR)
    isolated = dict(manifest)
    isolated["run_directory"] = "stage_b1b2_control_relevance_missing_for_test"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(isolated, ensure_ascii=False), encoding="utf-8")
    report = compare(artifact_dir=ARTIFACT_DIR, manifest_path=manifest_path)
    assert report["status"] == "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）"
    assert report["metrics_published"] is False
    assert len(report["missing_case_rounds"]) == manifest["expected_case_rounds"]
