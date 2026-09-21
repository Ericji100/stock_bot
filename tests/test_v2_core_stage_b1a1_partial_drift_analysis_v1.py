from __future__ import annotations

from pathlib import Path

from scripts.v2_core_stage_b1a1_partial_drift_analysis_v1 import (
    analyze_partial_drift,
)


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)


def test_r3_two_round_drift_is_revalidated_and_decomposed():
    report = analyze_partial_drift(
        manifest_path=ARTIFACT_DIR
        / "stage_b1a1_smoke_execution_manifest_candidate_r3.json",
        artifact_dir=ARTIFACT_DIR,
    )
    assert report["status"] == (
        "PARTIAL_DIAGNOSTIC_ONLY（部分診斷、非正式三輪門檻）"
    )
    assert report["completed_case_rounds"] == 8
    assert report["expected_case_rounds"] == 12
    assert report["formal_gate_published"] is False
    assert report["overall"]["atomic_comparisons"] == 300
    assert report["overall"]["atomic_result_agreement_percent"] == 92.0
    assert report["overall"]["primary_evidence_agreement_percent"] == 60.67
    assert report["overall"]["eligible_set_exact_agreement_percent"] == 50.0
    assert report["overall"]["eligibility_flip_count"] == 8
    assert report["atom_metrics"]["invalidation_traceability"][
        "result_agreement_percent"
    ] == 100.0
    assert report["future_performance_used"] is False
    assert report["identity_used"] is False
    assert report["sealed_labels_used"] is False
    assert report["ai_outputs_modified"] is False


def test_r3_drift_report_identifies_both_flip_directions():
    report = analyze_partial_drift(
        manifest_path=ARTIFACT_DIR
        / "stage_b1a1_smoke_execution_manifest_candidate_r3.json",
        artifact_dir=ARTIFACT_DIR,
    )
    assert report["overall"]["eligibility_flip_direction_counts"] == {
        "ELIGIBLE->INELIGIBLE": 5,
        "INELIGIBLE->ELIGIBLE": 3,
    }
    changed_families = {
        atom["atom"]
        for row in report["eligibility_flips"]
        for atom in row["changed_atoms"]
    }
    assert "structural_challenge_or_break" in changed_families
    assert "as_of_relation_link" in changed_families
