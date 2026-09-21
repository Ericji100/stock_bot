from pathlib import Path

from scripts.v2_core_calibration_reference_alignment_v1 import build_report


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"


def test_alignment_audit_uses_only_calibration_references_without_future_data():
    report = build_report(ARTIFACT_DIR)
    assert report["scope"] == "CALIBRATION_ONLY_POSTHOC_AUDIT（僅校準集事後稽核）"
    assert report["formal_legacy_reproduction"] is False
    assert report["locked_reproduction_set_opened"] is False
    assert report["future_performance_used"] is False
    assert report["legacy_answers_exposed_to_ai"] is False
    assert report["identity_emitted_in_report"] is False
    assert report["reference_case_count"] == 48


def test_r8_comparison_covers_three_frozen_rounds_and_sixteen_positive_references():
    report = build_report(ARTIFACT_DIR)
    rounds = report["r8_alignment"]["round_metrics"]
    assert len(rounds) == 3
    assert {row["round"] for row in rounds} == {1, 2, 3}
    assert all(row["case_count"] == 48 for row in rounds)
    assert all(row["positive_reference_count"] == 16 for row in rounds)
    assert all(row["positive_with_structured_legacy_trigger_count"] == 14 for row in rounds)
    assert all(row["nonpositive_reference_count"] == 32 for row in rounds)
    assert report["findings"]["no_r8_round_fully_aligned_to_all_positive_reference_dimensions"] is True


def test_b1b_candidate_space_does_not_yet_express_the_two_positive_legacy_anchors():
    report = build_report(ARTIFACT_DIR)
    b1b = report["b1b_alignment"]
    assert b1b["selected_case_count"] == 5
    assert b1b["positive_reference_count"] == 2
    assert b1b["positive_exact_reference_object_coverage_in_full_catalog_percent"] == 0.0
    assert b1b["positive_exact_reference_object_coverage_in_parent_pool_percent"] == 0.0
    positives = [case for case in b1b["cases"] if case["case_role"] == "POSITIVE_REFERENCE"]
    assert all(
        case["role_atom_comparability"] == "NOT_COMPARABLE_CANDIDATE_OBJECT_MISMATCH"
        for case in positives
    )
    nonpositives = [case for case in b1b["cases"] if case["case_role"] != "POSITIVE_REFERENCE"]
    assert all(
        case["role_atom_comparability"] == "NOT_COMPARABLE_REFERENCE_GRANULARITY"
        for case in nonpositives
    )
    assert report["findings"]["b1b2_r2_should_continue_before_object_gap_is_fixed"] is False
