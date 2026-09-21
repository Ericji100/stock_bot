from __future__ import annotations

from pathlib import Path

from scripts.v2_core_calibration_cases_v1 import (
    FORBIDDEN_RESULT_KEYS,
    extract_cases,
    recursive_keys,
    summarize,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports/course_backtest/2026-09-10/v2_core_reproducible_goal_v1"


def test_cases_use_only_calibration_rows_and_no_performance_fields() -> None:
    cases = extract_cases(ROOT, OUTPUT)
    calibration_manifest = __import__("json").loads(
        (OUTPUT / "calibration_manifest.json").read_text(encoding="utf-8")
    )
    allowed = {
        (str(row["batch_id"]), int(row["line_index"]))
        for row in calibration_manifest["rows"]
    }

    assert cases
    assert {
        (str(row["batch_id"]), int(row["source_line_index"])) for row in cases
    } <= allowed
    assert recursive_keys(cases).isdisjoint(FORBIDDEN_RESULT_KEYS)
    assert all(row["future_performance_included"] is False for row in cases)


def test_all_four_positive_scenarios_are_present() -> None:
    cases = extract_cases(ROOT, OUTPUT)
    scenarios = set(summarize(cases)["positive_scenarios"])
    assert scenarios == {
        "MATURE_TREND_PULLBACK",
        "MACRO_COPY_RESONANCE",
        "BEAR_REVERSAL_LEFT_RIGHT",
        "FRESH_Q1_EXPANSION",
    }


def test_positive_case_has_signal_day_fact_and_full_legacy_trigger() -> None:
    cases = extract_cases(ROOT, OUTPUT)
    positives = [row for row in cases if row["legacy_v2_trigger"]]
    assert positives
    for row in positives:
        assert row["daily_visible_fact"]["date"] == row["as_of"]
        assert row["legacy_v2_trigger"]["signal_date"] == row["as_of"]
        assert row["legacy_v2_trigger"].get("required_gates")


def test_nearby_negative_is_not_mislabeled_as_explicit_ai_wait() -> None:
    cases = extract_cases(ROOT, OUTPUT)
    nearby = [row for row in cases if row["case_kind"].startswith("HARD_NEGATIVE_NEARBY")]
    assert nearby
    assert all("不表示舊AI曾逐欄提交WAIT" in row["legacy_v2_no_trade_reason"] for row in nearby)


def test_objective_challenge_is_one_ranked_day_per_no_trigger_row() -> None:
    cases = extract_cases(ROOT, OUTPUT)
    challenges = [
        row
        for row in cases
        if row["case_kind"].startswith("HARD_NEGATIVE_OBJECTIVE_CHALLENGE")
    ]
    assert challenges
    keys = [(row["batch_id"], row["source_line_index"]) for row in challenges]
    assert len(keys) == len(set(keys))
    assert all(row["expected_permission"].startswith("LEGACY_NO_TRIGGER") for row in challenges)
    assert all("不表示舊AI曾在該日逐欄提交WAIT" in row["legacy_v2_no_trade_reason"] for row in challenges)
