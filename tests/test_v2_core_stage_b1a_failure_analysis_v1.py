from __future__ import annotations

from pathlib import Path

from scripts.v2_core_stage_b1a_failure_analysis_v1 import analyze_failure


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)


def test_completed_r2_smoke_failure_is_deterministically_decomposed():
    report = analyze_failure(
        manifest_path=ARTIFACT_DIR
        / "stage_b1a_smoke_execution_manifest_candidate_r2.json",
        artifact_dir=ARTIFACT_DIR,
    )
    assert report["status"] == "FAILURE_DIAGNOSIS_COMPLETE（失敗診斷完成）"
    assert report["completed_case_rounds"] == 12
    assert report["protocol_error_count"] == 0
    assert report["future_performance_used"] is False
    assert report["identity_used"] is False
    assert report["sealed_labels_used"] is False
    assert report["ai_outputs_modified"] is False
    assert {row["gate"] for row in report["failed_gates"]} == {
        "ATOMIC_RESULT",
        "ROLE_SET",
        "DEEP_REVIEW_POOL",
    }
    assert report["pool_metrics"]["candidate_membership_consistency_percent"] < 100
    assert all(
        len(row["pool_sizes_by_round"]) == 3
        for row in report["pool_metrics"]["cases"]
    )
