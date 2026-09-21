from __future__ import annotations

import pandas as pd

import scripts.course_exit_variant_backtest as exits


def _trade() -> dict:
    return {
        "trade_id": "1234-2026-05-21-1",
        "code": "1234",
        "name": "測試股",
        "entry_date": "2026-05-21",
        "entry_price": 10.0,
        "defense": 9.0,
        "initial_risk_pct": 10.0,
        "mfe_pct": 40.0,
        "max_favorable_price": 14.0,
    }


def test_one_and_two_close_ma21_variants_exit_on_different_sessions() -> None:
    frame = pd.DataFrame(
        [
            {"date": "2026-05-21", "open": 10.0, "high": 10.8, "low": 9.8, "close": 10.5, "MA21": 10.0},
            {"date": "2026-05-22", "open": 10.4, "high": 10.5, "low": 9.7, "close": 9.8, "MA21": 10.2},
            {"date": "2026-05-25", "open": 9.7, "high": 9.9, "low": 9.5, "close": 9.7, "MA21": 10.1},
            {"date": "2026-05-26", "open": 9.6, "high": 9.9, "low": 9.5, "close": 9.8, "MA21": 10.0},
        ]
    )
    frame["date"] = pd.to_datetime(frame["date"])

    one = exits.simulate_simple_variant(frame=frame, trade=_trade(), variant="MA21_ONE_CLOSE")
    two = exits.simulate_simple_variant(frame=frame, trade=_trade(), variant="MA21_TWO_CLOSES")
    fixed = exits.simulate_simple_variant(frame=frame, trade=_trade(), variant="FIXED_PIVOT")

    assert one["exit_trigger_date"] == "2026-05-22"
    assert one["exit_date"] == "2026-05-25"
    assert one["performance_return_pct"] == -3.0
    assert two["exit_trigger_date"] == "2026-05-25"
    assert two["exit_date"] == "2026-05-26"
    assert two["performance_return_pct"] == -4.0
    assert fixed["status_label"] == "OPEN（持有中）"


def test_hybrid_partials_then_exits_on_dynamic_defense() -> None:
    frame = pd.DataFrame(
        [
            {"date": "2026-05-21", "open": 10.0, "high": 10.8, "low": 9.8, "close": 10.5, "volume": 100.0, "VOL_MA20": 100.0, "MA21": 10.0, "ATR14": 1.0},
            {"date": "2026-05-22", "open": 10.5, "high": 12.5, "low": 10.4, "close": 12.0, "volume": 100.0, "VOL_MA20": 100.0, "MA21": 10.0, "ATR14": 1.0},
            {"date": "2026-05-25", "open": 12.1, "high": 13.0, "low": 12.0, "close": 12.1, "volume": 300.0, "VOL_MA20": 100.0, "MA21": 10.0, "ATR14": 1.0},
            {"date": "2026-05-26", "open": 12.0, "high": 12.6, "low": 11.8, "close": 12.4, "volume": 100.0, "VOL_MA20": 100.0, "MA21": 10.5, "ATR14": 1.0},
            {"date": "2026-05-27", "open": 12.2, "high": 12.3, "low": 10.5, "close": 10.8, "volume": 100.0, "VOL_MA20": 100.0, "MA21": 10.6, "ATR14": 1.0},
            {"date": "2026-05-28", "open": 11.0, "high": 11.2, "low": 10.8, "close": 11.0, "volume": 100.0, "VOL_MA20": 100.0, "MA21": 10.6, "ATR14": 1.0},
        ]
    )
    frame["date"] = pd.to_datetime(frame["date"])
    pivot_defenses = {0: 9.0, 1: 10.5, 2: 11.0, 3: 11.0, 4: 11.0, 5: 11.0}

    result = exits.simulate_hybrid_variant(
        frame=frame,
        trade=_trade(),
        pivot_defenses=pivot_defenses,
    )

    assert result["profit_protect_activation_date"] == "2026-05-22"
    assert result["volume_warning_date"] == "2026-05-25"
    assert result["partial_exits"][0]["date"] == "2026-05-26"
    assert result["partial_exits"][0]["price"] == 12.0
    assert result["exit_trigger_date"] == "2026-05-27"
    assert result["exit_reason"] == "DYNAMIC_DEFENSE"
    assert result["exit_date"] == "2026-05-28"
    assert result["performance_return_pct"] == 13.3333
    assert result["status_label"] == "CLOSED（交易已結束）"
