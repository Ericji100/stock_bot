from __future__ import annotations

import pandas as pd

from scripts.course_add_vs_no_add_backtest import (
    Costs,
    MODELS,
    _make_economic_frame,
    _quantity_for_budget,
    summarize,
    validate,
)


def test_integer_quantity_stays_inside_budget_with_minimum_fee():
    costs = Costs(minimum_commission=20.0)
    quantity = _quantity_for_budget(5_000.0, 31.0, costs)
    assert quantity == 160
    assert costs.buy_terms(quantity, 31.0)[2] <= 5_000.0
    assert costs.buy_terms(quantity + 1, 31.0)[2] > 5_000.0


def test_models_have_identical_campaign_cap():
    assert MODELS["ONE_SHOT"].campaign_budget == 10_000.0
    assert MODELS["PYRAMID"].campaign_budget == 10_000.0
    assert MODELS["PYRAMID"].mother_budget == 5_000.0
    assert len(MODELS["PYRAMID"].add_budgets) == 2


def test_economic_frame_removes_cash_dividend_gap():
    dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05"])
    raw = pd.DataFrame({
        "date": dates,
        "open": [100.0, 100.0, 98.0],
        "high": [101.0, 101.0, 99.0],
        "low": [99.0, 99.0, 97.0],
        "close": [100.0, 100.0, 98.0],
        "volume": [1000.0, 1000.0, 1000.0],
    })
    event = {"date": "2026-01-05", "cash": 2.0, "ratio": 1.0}
    frame, mapped, _ = _make_economic_frame(raw, [event], "2026-01-02")
    assert mapped["2026-01-05"][0]["cash"] == 2.0
    assert float(frame.iloc[-1].close) == 100.0


def test_summary_reconciles_final_equity():
    trade = {
        "trade_id": "x", "status": "OPEN", "exit_date": None,
        "net_pnl": 125.0, "gross_buy_value": 5000.0,
        "net_return_on_campaign_budget_pct": 1.25,
        "net_return_on_deployed_pct": 2.5,
        "total_cost_including_open_liquidation": 10.0,
        "final_open_book_cost": 5000.0,
        "add_count": 0, "legs": [], "reference_fixed_defense_mfe_pct": 10.0,
        "exit_reason_label": "AS_OF（截至日估值）", "transactions": [],
        "daily": [{"date": "2026-01-01", "net_liquidation_pnl": 125.0,
                   "open_book_cost": 5000.0}],
    }
    result = summarize([trade])
    assert result["net_pnl"] == 125.0
    assert result["final_net_liquidation_equity"] == 500_125.0
    assert result["max_concurrent_positions"] == 1


def test_empty_summary_is_defined_for_zero_entry_variant():
    result = summarize([])
    assert result["trade_count"] == 0
    assert result["final_net_liquidation_equity"] == 500_000.0
    assert result["positive_trade_rate_pct"] is None
    assert result["net_pnl_per_gross_buy_pct"] is None


def test_validation_rejects_future_pivot_confirmation():
    trade = {
        "trade_id": "x", "status": "OPEN", "exit_date": None,
        "exit_trigger_date": None, "net_pnl": 0.0, "gross_buy_value": 5000.0,
        "net_return_on_campaign_budget_pct": 0.0, "net_return_on_deployed_pct": 0.0,
        "total_cost_including_open_liquidation": 0.0, "final_open_book_cost": 5000.0,
        "add_count": 1, "reference_fixed_defense_mfe_pct": 0.0,
        "exit_reason_label": "AS_OF（截至日估值）",
        "legs": [{"buy_outflow": 5000.0, "net_pnl": 0.0}],
        "transactions": [],
        "daily": [{"date": "2026-01-01", "net_liquidation_pnl": 0.0,
                   "open_book_cost": 5000.0}],
        "add_events": [{"status": "FILLED（已加碼）", "signal_close_r": 1.0,
                        "threshold_r": 1.0, "pivot_confirmed_date": "2026-01-03",
                        "signal_date": "2026-01-02", "date": "2026-01-05"}],
    }
    one = {**trade, "add_count": 0, "add_events": []}
    payload = {
        "method_version": "test",
        "results": {
            "BASE": {
                "ONE_SHOT": {"trades": [one] * 89,
                             "summary": {"final_net_liquidation_equity": 500000.0,
                                         "funding_shortfall": 0.0,
                                         "minimum_cash_without_dividend_reinvestment": 1.0}},
                "PYRAMID": {"trades": [trade] * 89,
                            "summary": {"final_net_liquidation_equity": 500000.0,
                                        "funding_shortfall": 0.0,
                                        "minimum_cash_without_dividend_reinvestment": 1.0}},
            }
        },
    }
    result = validate(payload)
    assert any(row["name"].endswith("causal_pivots") and not row["passed"]
               for row in result["checks"])
