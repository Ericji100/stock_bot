"""R10B touched calibration cases are not a blind performance claim."""

from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from scripts.v2_core_r10b_two_case_teacher_diagnostic import build_report


def test_two_case_diagnostic_separates_decision_from_explanation() -> None:
    report = build_report(ARTIFACT_DIR)
    assert report["case_count"] == 2
    assert report["tactical_anchor_exact_matches"] == 2
    assert report["defense_exact_matches"] == 2
    assert report["signal_permission_matches"] == 1
    assert report["trigger_path_exact_matches"] == 0
    assert report["quadrant_exact_matches"] == 1
    assert report["teacher_used_in_formal_ai_input"] is False
    assert report["future_performance_used"] is False
    assert report["locked_reproduction_set_opened"] is False
    assert report["not_formal_reproduction_or_performance"] is True
