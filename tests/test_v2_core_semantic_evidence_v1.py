from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_core_semantic_evidence_v1 import (
    POSITIVE_KIND,
    SCENARIOS,
    build_summary,
    classify_reason,
    distribution,
)


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
CASES = OUT / "calibration_daily_cases_v2.jsonl"


def test_distribution_is_stable() -> None:
    assert distribution([3.0, 1.0, 2.0]) == {"count": 3, "min": 1.0, "median": 2.0, "max": 3.0}


def test_reason_index_is_multilabel_and_has_fallback() -> None:
    labels = classify_reason("長多慣性尚未確認，收盤仍低於MA105，且防線過遠")
    assert "LONG_MA_HABIT_MISSING（長均線慣性不足）" in labels
    assert "CAUSAL_STOP_MISSING_OR_TOO_FAR（因果防線缺失或過遠）" in labels
    assert classify_reason("沒有可辨識文字") == ["UNCLASSIFIED_LEGACY_WORDING（舊理由文字未歸類）"]


def test_summary_uses_only_calibration_cases() -> None:
    summary = build_summary(CASES)
    assert summary["input"]["rows"] == 2317
    assert summary["positive_count"] == 127
    assert summary["future_performance_used"] is False
    assert summary["sealed_answers_opened"] is False


def test_all_four_scenarios_and_counts_are_preserved() -> None:
    summary = build_summary(CASES)
    assert set(summary["scenarios"]) == set(SCENARIOS)
    assert summary["positive_scenario_counts"] == {
        "BEAR_REVERSAL_LEFT_RIGHT": 2,
        "FRESH_Q1_EXPANSION": 16,
        "MACRO_COPY_RESONANCE": 65,
        "MATURE_TREND_PULLBACK": 44,
    }


def test_legacy_positive_causal_invariants_hold() -> None:
    summary = build_summary(CASES)
    assert summary["positive_invariants"]["positive_count"] == 127
    assert summary["positive_invariants"]["violation_count"] == 0


def test_cross_batch_duplicate_labels_are_exposed() -> None:
    summary = build_summary(CASES)
    duplicate = summary["duplicate_positive_analysis"]
    assert duplicate["row_level_positive_count"] == 127
    assert duplicate["unique_stock_date_count"] == 121
    assert duplicate["duplicate_stock_date_groups"] == 6
    assert duplicate["scenario_conflict_groups"] == 3
    assert duplicate["trigger_path_conflict_groups"] == 6


def test_case_file_does_not_embed_forbidden_performance_fields() -> None:
    forbidden = {"mfe", "mae", "pnl", "profit", "return_pct", "exit_date", "exit_price", "future_outcome"}
    for line in CASES.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        assert forbidden.isdisjoint(row.keys())
        assert row["future_performance_included"] is False
        if row["case_kind"] == POSITIVE_KIND:
            assert row["expected_permission"] == "TRADE_APPROVED（核准交易）"
