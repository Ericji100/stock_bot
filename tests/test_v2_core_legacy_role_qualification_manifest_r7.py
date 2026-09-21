"""R7 first-stage execution matrix is teacher-blind and fully source-bound."""

from __future__ import annotations

from scripts.v2_core_legacy_role_qualification_manifest_r7 import build_manifest
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR


def test_manifest_covers_all_cases_and_roles_once() -> None:
    manifest = build_manifest(ARTIFACT_DIR)
    assert manifest["case_count"] == 14
    assert manifest["role_stage_count"] == 70
    assert len({(row["review_id"], row["role"]) for row in manifest["rows"]}) == 70
    assert manifest["formal_model"] == "gpt-5.6-sol"
    assert manifest["reasoning_effort"] == "xhigh"
    assert manifest["stop_remaining_percent_lte"] == 70
    assert manifest["formal_ai_calls"] == 0
    assert manifest["teacher_answers_read"] is False
    assert manifest["locked_set_opened"] is False
    assert manifest["future_performance_used"] is False
