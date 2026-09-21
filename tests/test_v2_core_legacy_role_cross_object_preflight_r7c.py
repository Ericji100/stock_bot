"""Mixed R7B/R7C diagnostic must preserve role provenance and no-trade status."""

from scripts.v2_core_legacy_role_cross_object_preflight_r7c import build_report
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR


REVIEW_ID = "FP-38178c2820dd71aa800fd8d7"


def test_mixed_case2_report_is_explicitly_diagnostic() -> None:
    report = build_report(ARTIFACT_DIR, REVIEW_ID)
    assert report["status"] == "CROSS_ROLE_PRELIMINARY_READY"
    assert report["contradictions"] == []
    assert report["mixed_version_diagnostic_only"] is True
    assert report["trade_permission_granted"] is False
    assert report["role_source_versions"]["CONTROLLING_MATURE_CAMPAIGN"]["stage"] == "R7C_FIXED_KEY_QUALIFICATION"
    assert report["role_source_versions"]["UP_CONTROL_CHALLENGER"]["stage"] == "R7B_EPISODE_BOUND_ADJUDICATION"
    assert set(report["role_source_versions"]) == set(report["role_results"])
