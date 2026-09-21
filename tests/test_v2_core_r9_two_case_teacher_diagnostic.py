"""R9 partial teacher comparison must not be mistaken for trade reproduction."""

from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from scripts.v2_core_r9_two_case_teacher_diagnostic import build_report, render_markdown


def test_r9_two_case_partial_score_and_boundaries() -> None:
    report = build_report(ARTIFACT_DIR)
    assert report["case_count"] == 2
    assert report["defense_exact_matches"] == 2
    assert report["teacher_working_retrieved_count"] == 2
    assert report["working_exact_candidate_matches"] == 0
    assert report["working_boundary_matches"] == 0
    assert report["relationship_class_family_matches"] == 2
    assert report["not_formal_reproduction_or_performance"] is True
    assert all(not row["trade_permission_granted"] for row in report["rows"])
    assert "校準診斷" in render_markdown(report)
