from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backtests.dual_ma.etf_0050_base_v1 import ETF0050Error, PACKAGE_ROOT_V3
from backtests.dual_ma.etf_0050_formal_v1 import load_package
from backtests.dual_ma.etf_0050_formal_v2 import (
    aggregate_independent_years,
    geometric_annual_return,
    render_continuous_report,
    simulate_buy_hold_cash_safe,
    validate_cash_safe_metric_semantics,
)


def test_geometric_annual_return_does_not_use_average_capital_shortcut() -> None:
    values = [Decimal("0.10"), Decimal("-0.20"), Decimal("0.30")]
    actual = geometric_annual_return(values)
    expected = Decimal(str(float(Decimal("1.10") * Decimal("0.80") * Decimal("1.30")) ** (1 / 3) - 1))
    assert actual == expected
    assert actual != sum(values, Decimal("0")) / 3


def test_buy_hold_never_borrows_and_corporate_ledgers_keep_terms() -> None:
    package = load_package(PACKAGE_ROOT_V3)
    result = simulate_buy_hold_cash_safe(
        package=package,
        start=date(2022, 1, 1),
        end=date(2026, 9, 30),
        slippage_bps=20,
        initial_capital=Decimal("500000"),
        reinvest=True,
    )
    assert result["metrics"]["minimum_cash"] >= 0
    assert result["metrics"]["additional_top_up_or_borrowing"] == 0
    assert all(row["amount_per_share"] is not None for row in result["dividends"])
    assert result["splits"][0]["ratio"] == Decimal("4")
    assert result["splits"][0]["transaction"] is False
    assert result["curve"][-1]["equity"] == result["metrics"]["ending_equity"]
    detail = result["details"][0]
    expected_trade_return = detail["net_pnl_or_estimate"] / detail["buy_total"]
    assert detail["return_rate_or_estimate"] == expected_trade_return
    assert result["metrics"]["average_return"] == expected_trade_return
    assert result["metrics"]["median_return"] == expected_trade_return
    assert result["metrics"]["max_trade_win"] == expected_trade_return
    assert result["metrics"]["max_trade_loss"] == expected_trade_return
    assert result["metrics"]["fees_and_tax"] == result["metrics"]["actual_fees_and_tax"]
    assert result["metrics"]["slippage_cost"] == result["metrics"]["actual_slippage_cost"]
    assert result["metrics"]["cutoff_estimated_sale_fees_and_tax"] > 0
    assert result["metrics"]["cutoff_estimated_sale_slippage_cost"] > 0
    assert result["metrics"]["final_shares"] > 0


def test_metric_semantics_require_final_shares_exactly_for_open_position() -> None:
    metrics = {
        "open_rounds": 1,
        "final_shares": 10,
        "fees_and_tax": Decimal("1"),
        "slippage_cost": Decimal("2"),
        "actual_fees_and_tax": Decimal("1"),
        "actual_slippage_cost": Decimal("2"),
        "cutoff_estimated_sale_fees_and_tax": Decimal("3"),
        "cutoff_estimated_sale_slippage_cost": Decimal("4"),
    }
    validate_cash_safe_metric_semantics(metrics)
    validate_cash_safe_metric_semantics({**metrics, "open_rounds": 0, "final_shares": 0})
    with pytest.raises(ETF0050Error):
        validate_cash_safe_metric_semantics({**metrics, "final_shares": 0})
    with pytest.raises(ETF0050Error):
        validate_cash_safe_metric_semantics({**metrics, "open_rounds": 0})


def test_five_year_aggregate_pools_trade_statistics_and_uses_geometric_return() -> None:
    metrics = []
    details = []
    annual_returns = [Decimal("0.10"), Decimal("0.20")]
    for year, annual_return in zip((2022, 2023), annual_returns):
        metrics.append(
            {
                "year": year,
                "slippage_bps": 20,
                "strategy_id": "S",
                "strategy_label": "測試",
                "external_principal": Decimal("10000"),
                "ending_equity": Decimal("10000") * (1 + annual_return),
                "cumulative_buy": Decimal("10000"),
                "peak_concurrent_investment": Decimal("10000"),
                "peak_investment_date": date(year, 1, 2),
                "realized_net_pnl": Decimal("10000") * annual_return,
                "cutoff_estimated_pnl": Decimal("0"),
                "total_return": annual_return,
                "max_asset_drawdown": Decimal("-0.10"),
                "idle_cash_ratio": Decimal("0.20"),
                "fees_and_tax": Decimal("1"),
                "slippage_cost": Decimal("2"),
                "transaction_count": 2,
                "skipped_for_cash": 0,
                "minimum_cash": Decimal("0"),
                "dividends_received": Decimal("0"),
            }
        )
    for value in (Decimal("-0.50"), Decimal("0.10"), Decimal("0.20")):
        details.append(
            {
                "slippage_bps": 20,
                "strategy_id": "S",
                "status_zh": "已賣出",
                "return_rate_or_estimate": value,
                "mfe": value + Decimal("0.10"),
                "mae": value - Decimal("0.10"),
            }
        )
    row = aggregate_independent_years(metrics, details)[0]
    assert row["average_return"] == pytest.approx(Decimal("-0.06666666666666666666666666667"))
    assert row["median_return"] == Decimal("0.10")
    assert row["cagr"] == geometric_annual_return(annual_returns)
    assert row["external_principal"] == Decimal("20000")


def test_continuous_report_explains_all_in_sizing_and_compounded_peak_cost() -> None:
    def row(strategy_id: str, label: str) -> dict[str, object]:
        return {
            "strategy_id": strategy_id,
            "strategy_label": label,
            "external_principal": Decimal("500000"),
            "additional_top_up_or_borrowing": Decimal("0"),
            "cumulative_buy": Decimal("9706960.20"),
            "peak_concurrent_investment": Decimal("1108599.94"),
            "ending_equity": Decimal("1207677.09"),
            "combined_pnl": Decimal("707677.09"),
            "total_return": Decimal("1.4154"),
            "cagr": Decimal("0.2042"),
            "max_asset_drawdown": Decimal("-0.1558"),
            "idle_cash_ratio": Decimal("0.3966"),
            "closed_rounds": 16,
            "open_rounds": 1,
            "win_rate": Decimal("0.5"),
            "average_return": Decimal("0.0618"),
            "median_return": Decimal("0.0325"),
            "profit_per_100_invested": Decimal("7.29"),
            "dividends_received": Decimal("47632.60"),
            "fees_and_tax": Decimal("16711"),
            "slippage_cost": Decimal("37762.36"),
            "actual_fees_and_tax": Decimal("16711"),
            "actual_slippage_cost": Decimal("37762.36"),
            "cutoff_estimated_sale_fees_and_tax": Decimal("1450"),
            "cutoff_estimated_sale_slippage_cost": Decimal("2217.20"),
        }

    report = render_continuous_report(
        [row("A_LONG", "基本版－長線"), row("BUY_HOLD_CASH", "0050 全程持有")]
    )
    assert "不是每筆固定 1 萬元" in report
    assert "單一時點最高持倉成本（含獲利再投入）" in report
    assert "| 基本版－長線 | 17 | 16 | 1 |" in report
    assert "實際已發生費稅與價差" in report
    assert "截止估值預留賣出費稅與價差" in report
