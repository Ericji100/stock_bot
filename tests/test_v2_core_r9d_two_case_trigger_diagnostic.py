"""Frozen two-case R9D comparison remains explicitly diagnostic."""

from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from scripts.v2_core_r9d_two_case_trigger_diagnostic import build_report, render_markdown


def test_frozen_two_case_teacher_diagnostic() -> None:
    report = build_report(ARTIFACT_DIR)
    assert report["case_count"] == 2
    assert report["scenario_matches"] == 2
    assert report["defense_exact_matches"] == 2
    assert report["signal_decision_matches"] == 0
    assert report["formal_ai_used_teacher_answer"] is False
    assert report["future_performance_used"] is False
    assert report["not_formal_reproduction_or_performance"] is True
    assert "舊紀錄內部不一致" in render_markdown(report)
