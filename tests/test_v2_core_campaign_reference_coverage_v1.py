from __future__ import annotations

from scripts import v2_core_campaign_reference_coverage_v1 as module


def test_campaign_reference_audit_is_post_hoc_and_does_not_open_locked_set() -> None:
    report = module.build_report(module.ARTIFACT_DIR)
    assert report["scope"] == "CALIBRATION_ONLY_POST_HOC_REPRESENTATION_AUDIT"
    assert report["generator_was_frozen_before_reference_join"] is True
    assert report["legacy_answers_exposed_to_generator"] is False
    assert report["legacy_answers_exposed_to_ai"] is False
    assert report["formal_ai_calls"] == 0
    assert report["future_performance_used"] is False
    assert report["locked_reproduction_set_opened"] is False


def test_campaign_catalog_recovers_full_exact_reference_representation() -> None:
    report = module.build_report(module.ARTIFACT_DIR)
    assert report["structured_positive_reference_count"] == 14
    assert report["r1_full_exact_coverage_percent"] == 14.29
    assert report["campaign_r1_full_exact_coverage_percent"] == 100.0
    assert report["campaign_r1_full_exact_covered_count"] == 14
    assert report["campaign_r1_shortlist_exact_covered_count"] == 11
    assert report["status"] == "FULL_REPRESENTATION_RECOVERED_SHORTLIST_REVISION_REQUIRED"


def test_report_contains_no_identity_or_future_performance_fields() -> None:
    report = module.build_report(module.ARTIFACT_DIR)
    serialized = str(report).lower()
    for forbidden in ("stock_code", "stock_name", "future_return", "mfe", "mae"):
        assert forbidden not in serialized
