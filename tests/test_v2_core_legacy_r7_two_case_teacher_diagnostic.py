"""Post-freeze teacher diagnostic preserves honest partial-score boundaries."""

from scripts.v2_core_legacy_r7_two_case_teacher_diagnostic import build_report, render_markdown
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR


def test_two_case_comparison_is_not_formal_reproduction() -> None:
    report = build_report(ARTIFACT_DIR)
    assert report["case_count"] == 2
    assert report["scenario_matches"] == 1
    assert report["working_fixed_candidate_matches"] == 0
    assert report["working_boundary_matches"] == 0
    assert report["not_formal_14_case_reproduction"] is True
    assert report["not_performance_backtest"] is True
    assert report["teacher_used_in_formal_ai_input"] is False
    assert report["rows"][0]["teacher_candidate_role_assessment"]["atoms"]["BOUNDARY_AND_SCALE_FIT"] == "FAIL"
    assert report["rows"][1]["teacher_candidate_role_assessment"]["atoms"]["DIRECT_CAUSAL_LINK_TO_CURRENT_EPISODE"] == "FAIL"
    text = render_markdown(report)
    assert "情境相同：1/2" in text
    assert "工作錨固定候選相同：0/2" in text
