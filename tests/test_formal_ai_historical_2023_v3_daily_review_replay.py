from __future__ import annotations

from scripts.formal_ai_historical_2023_v3_daily_review_replay import V2_SCENARIOS, _decode


def test_decode_lean_hard_event_absent_row() -> None:
    row = ["D-x", "S-x", "2023-06-08", 5, ["TECH_MA21_BREAKOUT"], 900, 0, 0]
    decoded = _decode(row)
    assert decoded["review_id"] == "D-x"
    assert decoded["selection_today"] == ["TECH_MA21_BREAKOUT"]
    assert decoded["trigger_completed"] is False
    assert decoded["causal_stop_candidate"] is None
    assert decoded["confirmed_pivots_asof"] == []


def test_decode_full_row_preserves_causal_stop_and_pivots() -> None:
    row = [
        "D-y", "S-y", "2023-06-09", 6, [], 900, 1, 0,
        [10, 11, 9.8, 10.8, 4, 1.5, 0.5, 0.1, 0.03],
        [10, 9.9, 9.8, 9.5, 9.0, 8.5], [1, 1], [4, 5, 6, 8, 11],
        [["S", "L", "2023-06-01", "2023-06-06", 9.5]],
        ["2023-06-01", "2023-06-06", 9.5, 12.0, 2.6], 1,
        [], [], [["L", "H", "2023-05-20", "2023-06-07", 10.5]],
    ]
    decoded = _decode(row)
    assert decoded["trigger_completed"] is True
    assert decoded["market"]["close"] == 10.8
    assert decoded["causal_stop_candidate"] == {
        "source_date": "2023-06-01",
        "confirmation_date": "2023-06-06",
        "price": 9.5,
    }
    assert decoded["confirmed_pivots_asof"][0]["scale"] == "LARGE"
    assert decoded["confirmed_pivots_asof"][0]["side"] == "HIGH"


def test_v2_core_accepts_all_four_frozen_v2_scenarios() -> None:
    assert V2_SCENARIOS == {
        "MATURE_TREND_PULLBACK",
        "MACRO_COPY_RESONANCE",
        "FRESH_Q1_EXPANSION",
        "BEAR_REVERSAL_LEFT_RIGHT",
    }
