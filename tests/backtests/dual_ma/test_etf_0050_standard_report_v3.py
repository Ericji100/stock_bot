from datetime import date
from decimal import Decimal

from backtests.dual_ma import etf_0050_standard_report_v3 as target


def test_curve_metrics_uses_carried_starting_equity_for_year_slice() -> None:
    values = [
        (date(2023, 1, 3), Decimal("90")),
        (date(2023, 6, 1), Decimal("80")),
        (date(2023, 12, 29), Decimal("110")),
    ]

    metrics = target.curve_metrics(values, Decimal("100"))

    assert metrics["start_value"] == Decimal("100")
    assert metrics["end_value"] == Decimal("110")
    assert metrics["cumulative_return"] == Decimal("0.1")
    assert metrics["max_drawdown"] == Decimal("-0.2")


def test_overall_summaries_counts_calendar_years_without_resetting_account() -> None:
    metrics = [
        {
            "strategy_id": "A_LONG",
            "strategy_label": "基本版－長線",
            "ending_equity": "650000",
            "cagr": "0.10",
            "max_asset_drawdown": "-0.20",
        }
    ]
    year_ends = (450000, 495000, 490000, 588000, 650000)
    annual = []
    prior = Decimal("500000")
    for year, end in zip(target.YEARS, year_ends, strict=True):
        ending = Decimal(end)
        annual.append(
            {
                "strategy_id": "A_LONG",
                "year": year,
                "starting_equity": prior,
                "ending_equity": ending,
                "year_return": ending / prior - 1,
                "year_max_drawdown": Decimal("-0.10"),
                "benchmark_return": Decimal("0"),
                "benchmark_max_drawdown": Decimal("-0.15"),
            }
        )
        prior = ending

    [summary] = target.overall_summaries(metrics, annual)

    assert summary["profitable_year_count"] == 3
    assert summary["profitable_years"] == "2023、2025、2026"
    assert summary["losing_years"] == "2022、2024"
    assert summary["profit_through_2024"] == Decimal("-10000")
    assert summary["profit_during_2025_2026"] == Decimal("160000")


def test_fixed_report_contract_forbids_calendar_year_reset() -> None:
    contract = (
        target.Path(__file__).resolve().parents[3]
        / "docs"
        / "strategies"
        / "dual_ma"
        / "standard-backtest-report-v2.md"
    ).read_text(encoding="utf-8")

    assert "只在研究起始日投入一次本金" in contract
    assert "不得在每年一月重新投入" in contract
    assert "年度統計只能從同一條連續資產曲線切片" in contract


def test_no_trade_explanation_names_failed_conditions() -> None:
    summaries = [
        {
            "strategy_id": "C_LONG",
            "strategy_label": "嚴格長線版",
            "method_kind": "擇時策略",
            "rounds": "0",
        }
    ]
    audit = [
        {
            "strategy_id": "C_LONG",
            "period": "2022-2026",
            "candidate_decisions": "46",
            "ma144_not_up": "2",
            "risk_width_below_7pct": "40",
            "extension_below_5pct": "4",
            "input_undetermined": "0",
            "other_rejected": "0",
        }
    ]

    [line] = target.no_trade_explanations(summaries, audit)

    assert "46 次完整訊號候選" in line
    assert "風險寬度不足7% 40 次" in line
    assert "高於21日線不足5% 4 次" in line
