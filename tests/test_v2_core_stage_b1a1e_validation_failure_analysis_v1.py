from __future__ import annotations

from scripts.v2_core_stage_b1a1e_validation_failure_analysis_v1 import analyze
from scripts.v2_core_stage_b1a1e_validation_input_v1 import ARTIFACT_DIR


def test_completed_heldout_failure_is_decomposed_without_future_data():
    report = analyze(
        manifest_path=ARTIFACT_DIR / "stage_b1a1e_validation_execution_manifest_r1.json",
        artifact_dir=ARTIFACT_DIR,
    )
    assert report["source_status"] == "SMOKE_FEASIBILITY_FAILED（Smoke可行性失敗）"
    assert report["eligibility_drift_candidate_count"] == 2
    assert {
        (row["review_id"], row["candidate_id"])
        for row in report["eligibility_drifts"]
    } == {
        ("FP-4e7acf37bc8935bfd7b1591c", "SEG-d4fae2960d7d010676d8"),
        ("FP-1f42d30a09935f030d12d3c9", "SEG-352fbb34085f46274db2"),
    }
    assert report["future_performance_used"] is False
    assert report["identity_used"] is False
    assert report["sealed_labels_used"] is False
    assert report["ai_outputs_modified"] is False
