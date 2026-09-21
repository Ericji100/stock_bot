from pathlib import Path

from scripts.v2_core_stage_b1a1e_failure_analysis_v1 import analyze_failure


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)


def _report():
    return analyze_failure(
        manifest_path=ARTIFACT_DIR
        / "stage_b1a1e_smoke_execution_manifest_candidate_r1.json",
        artifact_dir=ARTIFACT_DIR,
    )


def test_failure_analysis_matches_formal_smoke_and_remains_blind():
    report = _report()
    assert report["source_smoke_status"] == (
        "SMOKE_FEASIBILITY_FAILED（Smoke可行性失敗）"
    )
    assert report["source_metrics"]["ai_perception_consistency_percent"] == 90.0
    assert report["source_metrics"]["derived_atom_consistency_percent"] == 84.09
    assert report["source_metrics"]["partial_eligible_set_consistency_percent"] == 50.0
    assert report["future_performance_used"] is False
    assert report["identity_used"] is False
    assert report["sealed_labels_used"] is False
    assert report["ai_outputs_modified"] is False


def test_failure_analysis_localizes_stable_and_unstable_cases():
    report = _report()
    cases = {row["review_id"]: row for row in report["cases"]}
    assert cases["FP-0684f3edad3f828e1f0c2a7f"]["candidate_drift_count"] == 0
    assert cases["FP-07fa37e112c31c6858219e5e"]["candidate_drift_count"] > 0
    assert report["candidate_drift_count"] > 0
