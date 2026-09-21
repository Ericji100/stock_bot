from __future__ import annotations

from scripts.v2_core_partial_case_audit_v1 import OUT, build_report, markdown


def _walk_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_walk_keys(child))
    return keys


def test_partial_audit_revalidates_all_completed_case_sets() -> None:
    report = build_report()
    completed = report["scope"]["completed_case_count"]
    assert 12 <= completed <= report["scope"]["expected_cases_in_round"]
    assert completed == len(report["cases"])
    assert completed == report["artifact_validation"]["validated_complete_sets"]
    assert report["artifact_validation"]["failed_sets"] == 1
    assert report["artifact_validation"]["status"] == "PARTIAL_FAILURE_PRESERVED（已保留部分失敗）"
    assert sum(report["aggregate"]["program_permission_counts"].values()) == completed


def test_partial_audit_does_not_open_identity_labels_or_future_results() -> None:
    report = build_report()
    assert report["causal_scope"] == {
        "as_of_only": True,
        "sealed_labels_used": False,
        "stock_identity_used": False,
        "legacy_answers_used": False,
        "future_performance_used": False,
    }
    forbidden_keys = {
        "anonymous_stock_id",
        "symbol",
        "code",
        "name",
        "case_role",
        "intended_scenario",
        "expected_permission",
        "legacy_v2_trigger",
        "legacy_v2_no_trade_reason",
        "future_outcome",
        "mfe",
        "mae",
        "pnl",
        "profit",
        "return_pct",
        "exit_date",
        "exit_price",
    }
    assert _walk_keys(report).isdisjoint(forbidden_keys)


def test_partial_audit_does_not_claim_consistency_or_performance() -> None:
    report = build_report()
    keys = _walk_keys(report)
    assert "rates_percent" not in keys
    assert "final_permission_consistency_rate" not in keys
    assert "performance" not in keys
    rendered = markdown(report)
    assert "不是三輪一致性結果，也不是績效回測" in rendered
    assert "不計算正式一致率" in rendered
    assert "Do not resume this execution manifest" in rendered


def test_v7_partial_audit_validates_program_bound_stage_d_outputs() -> None:
    report = build_report(
        OUT,
        OUT / "feasibility_probe_execution_manifest_v7.json",
    )
    assert report["scope"]["published_permission_count"] == 4
    assert report["scope"]["completed_case_count"] == 0
    assert report["artifact_validation"] == {
        "validated_complete_sets": 0,
        "failed_sets": 4,
        "status": "PARTIAL_FAILURE_PRESERVED（已保留部分失敗）",
    }
    assert "Do not resume this execution manifest" in report["next_safe_ai_resume"]


def test_partial_audit_records_model_and_program_recommendation_gap() -> None:
    report = build_report()
    completed = report["scope"]["completed_case_count"]
    assert report["scope"]["formal_model"] == "gpt-5.6-sol"
    assert report["scope"]["reasoning_effort"] == "xhigh"
    assert 0 <= report["aggregate"]["permission_recommendation_match_count"] <= completed
    assert 0 <= report["aggregate"]["trade_action_match_count"] <= completed
    for row in report["cases"]:
        assert row["review_id"].startswith("FP-")
        assert row["artifact_validation"] == "PASS（通過）"
        assert row["program_permission"] in {
            "TRADE_APPROVED",
            "WAIT",
            "REMOVE",
            "UNKNOWN",
            "INVALID",
        }


def test_partial_audit_distinguishes_exact_permission_from_trade_action() -> None:
    report = build_report()
    completed = report["scope"]["completed_case_count"]
    assert report["aggregate"]["trade_action_match_count"] >= report["aggregate"][
        "permission_recommendation_match_count"
    ]
    assert report["aggregate"]["trade_action_match_count"] <= completed
    assert sum(report["aggregate"]["permission_gap_reason_counts"].values()) == completed
    assert "若只比較立即動作『交易／不交易』" in markdown(report)
    assert "AI建議REMOVE不等於campaign已失效" in markdown(report)


def test_v8_partial_audit_accepts_latest_operational_stop_threshold() -> None:
    report = build_report(
        OUT,
        OUT / "feasibility_probe_execution_manifest_v8.json",
        operational_stop_threshold_percent=50,
    )
    assert "above 50%." in report["next_safe_ai_resume"]
