from __future__ import annotations

from scripts.v2_core_legacy_lifecycle_partial_compare_v6b import (
    ARTIFACT_DIR,
    build_report,
)


def test_r6b_partial_compare_revalidates_frozen_outputs_and_teacher_gap() -> None:
    report = build_report(ARTIFACT_DIR)
    assert report["formal_reproduction_rate_published"] is False
    assert report["teacher_revealed_only_after_r6b_terminated"] is True
    assert report["teacher_answers_available_to_ai"] is False
    assert report["future_performance_used"] is False
    assert report["b0a_valid_count"] == 8
    assert report["b0b_valid_count"] == 7
    assert report["b0b_invalid_count"] == 1
    assert report["not_started_b0a_count"] == 6
    assert report["partial_scenario_match_count"] == 2
    assert report["partial_scenario_match_percent"] == 25.0
    assert report["partial_working_anchor_match_count"] == 0
    assert report["partial_working_anchor_match_percent"] == 0.0
    assert report["invalid_errors"] == {
        "FP-a46bf89ae6077ab9e326306c": [
            "episode candidate violates B0A binding"
        ]
    }
    assert "MATURE_COLLAPSE_PERSISTS_IN_PARTIAL_RUN" in report[
        "diagnostic_conclusions"
    ]
