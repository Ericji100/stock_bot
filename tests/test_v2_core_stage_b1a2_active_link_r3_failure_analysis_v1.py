from pathlib import Path

from scripts.v2_core_stage_b1a2_active_link_r3_failure_analysis_v1 import analyze


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"


def test_r3_heldout_failure_is_explained_without_future_data():
    report = analyze(
        artifact_dir=ARTIFACT_DIR,
        source_manifest=ARTIFACT_DIR / "stage_b1a2_active_link_validation_r3_execution_manifest_r1.json",
        reference_manifests=[
            ARTIFACT_DIR / "stage_b1a2_active_link_execution_manifest_candidate_r2a.json",
            ARTIFACT_DIR / "stage_b1a2_active_link_validation_r2a_execution_manifest_r1.json",
        ],
    )
    assert report["source_drift_candidate_count"] == 1
    drift = report["source_drift_candidates"][0]
    assert drift["review_id"] == "FP-5d130963e17ef8b413cda989"
    assert drift["candidate_id"] == "SEG-e0284ea367d80d1b5321"
    assert drift["result_pattern"] == ["PASS", "FAIL", "PASS"]
    assert drift["min_fixed_path_edges"] == 2
    by_threshold = {row["min_fixed_path_edges"]: row for row in report["threshold_counterfactuals"]}
    assert by_threshold[3]["drift_capture_percent"] < 100.0
    assert by_threshold[2]["drift_capture_percent"] == 100.0
    assert by_threshold[2]["final_shortlist_reproducibility_percent"] == 100.0
    assert report["minimal_revision_hypothesis"]["observed_shortest_drift_path_edges"] == 2
    assert report["future_performance_used"] is False
    assert report["ai_outputs_modified"] is False


def test_r4_failure_shows_path_length_only_escalation_has_become_too_broad():
    report = analyze(
        artifact_dir=ARTIFACT_DIR,
        source_manifest=ARTIFACT_DIR / "stage_b1a2_active_link_validation_r4_execution_manifest_r1.json",
        reference_manifests=[
            ARTIFACT_DIR / "stage_b1a2_active_link_execution_manifest_candidate_r2a.json",
            ARTIFACT_DIR / "stage_b1a2_active_link_validation_r2a_execution_manifest_r1.json",
            ARTIFACT_DIR / "stage_b1a2_active_link_validation_r3_execution_manifest_r1.json",
        ],
    )
    drift = report["source_drift_candidates"][0]
    assert drift["candidate_id"] == "SEG-a06076e0237e8beb3835"
    assert drift["result_pattern"] == ["PASS", "FAIL", "FAIL"]
    assert drift["min_fixed_path_edges"] == 1
    hypothesis = report["minimal_revision_hypothesis"]
    assert hypothesis["observed_shortest_drift_path_edges"] == 1
    assert hypothesis["implied_escalation_rate_percent"] == 41.27
    assert hypothesis["recommendation"] == "DO_NOT_FREEZE_PATH_LENGTH_ONLY_REVISION_WITHOUT_SEMANTIC_DIAGNOSTIC"
