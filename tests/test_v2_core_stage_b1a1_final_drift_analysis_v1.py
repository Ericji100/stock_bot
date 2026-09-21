from pathlib import Path

from scripts.v2_core_stage_b1a1_final_drift_analysis_v1 import analyze_final_drift


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)


def _report():
    return analyze_final_drift(
        manifest_path=ARTIFACT_DIR
        / "stage_b1a1_smoke_execution_manifest_candidate_r4.json",
        artifact_dir=ARTIFACT_DIR,
    )


def test_r4_final_drift_revalidates_completed_failed_smoke():
    report = _report()
    assert report["source_smoke_status"] == (
        "SMOKE_FEASIBILITY_FAILED（Smoke可行性失敗）"
    )
    assert report["completed_case_rounds"] == 12
    assert report["expected_case_rounds"] == 12
    assert report["source_metrics"]["atomic_result_consistency_percent"] == 95.0
    assert report["source_metrics"]["primary_evidence_consistency_percent"] == 50.67
    assert report["source_metrics"]["eligible_set_consistency_percent"] == 75.0
    assert report["future_performance_used"] is False
    assert report["identity_used"] is False
    assert report["sealed_labels_used"] is False
    assert report["ai_outputs_modified"] is False


def test_r4_final_drift_localizes_eligibility_to_one_case():
    report = _report()
    drifting_cases = [row for row in report["cases"] if row["eligibility_drift_count"]]
    assert len(drifting_cases) == 1
    assert drifting_cases[0]["review_id"] == "FP-07fa37e112c31c6858219e5e"
    assert drifting_cases[0]["eligible_sizes_by_round"] == [4, 7, 4]
    assert report["summary"]["eligibility_drift_candidate_count"] == 3


def test_r4_counterfactual_primary_is_diagnostic_only():
    report = _report()
    for row in report["atom_metrics"].values():
        assert 0 <= row["counterfactual_canonical_primary_consistency_percent"] <= 100
    assert report["ai_outputs_modified"] is False
