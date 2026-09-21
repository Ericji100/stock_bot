from __future__ import annotations

from scripts import v2_core_campaign_reference_coverage_v2 as module


def test_r2_pool_retains_every_structured_positive_reference_object() -> None:
    report = module.build_report(module.ARTIFACT_DIR)
    assert report["structured_positive_reference_count"] == 14
    assert report["exact_reference_object_retained_count"] == 14
    assert report["exact_reference_object_pool_coverage_percent"] == 100.0
    assert report["status"] == "REFERENCE_OBJECT_POOL_READY_FOR_ONE_PASS_ALIGNMENT"


def test_r2_pool_audit_preserves_blind_boundaries() -> None:
    report = module.build_report(module.ARTIFACT_DIR)
    assert report["generator_was_frozen_before_reference_join"] is True
    assert report["legacy_answers_exposed_to_pool_generator"] is False
    assert report["legacy_answers_exposed_to_ai"] is False
    assert report["formal_ai_calls"] == 0
    assert report["future_performance_used"] is False
    assert report["locked_reproduction_set_opened"] is False
    serialized = str(report).lower()
    for forbidden in ("stock_code", "stock_name", "future_return", "mfe", "mae"):
        assert forbidden not in serialized
