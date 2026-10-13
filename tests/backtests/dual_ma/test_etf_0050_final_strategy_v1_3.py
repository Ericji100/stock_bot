from decimal import Decimal

from backtests.dual_ma import etf_0050_final_strategy_v1 as v12
from backtests.dual_ma import etf_0050_final_strategy_v1_3 as report


def test_v13_is_isolated_and_uses_friendly_names() -> None:
    assert report.VERSION.endswith("v1.3-r3")
    assert report.RUN_ROOT_DEFAULT.name.endswith("v1-3-r3")
    assert report.REPORT_ROOT_DEFAULT.name.endswith("v1-3-r3")
    assert tuple(report.NAMES) == v12.MAIN_IDS
    assert all("FINAL_" not in name and "BUY_HOLD" not in name for name in report.NAMES.values())


def test_performance_summary_counts_years_and_keeps_five_lines() -> None:
    summary = [{"comparison_id": item, "ending_equity": "1", "total_return": "0", "cagr": "0", "max_drawdown": "0", "rounds": "1", "closed_rounds": "0", "open_rounds": "1", "peak_concurrent_investment": "1"} for item in v12.MAIN_IDS]
    annual = [{"comparison_id": item, "return": value} for item in v12.MAIN_IDS for value in ("0.1", "-0.1", "0")]
    rows = report.build_performance_summary(summary, annual)
    assert len(rows) == 5
    assert [(row["獲利年度"], row["虧損年度"], row["平盤年度"]) for row in rows] == [(1, 1, 1)] * 5


def test_relative_difference_signs_are_recomputable() -> None:
    rows = []
    for item, ending, cagr, drawdown in (
        (v12.SIGNAL_CLOSE_ID, "120", "0.12", "-0.20"),
        (v12.NEXT_OPEN_ID, "110", "0.11", "-0.25"),
        (v12.BUY_HOLD_CASH_ID, "100", "0.10", "-0.40"),
        (v12.BUY_HOLD_REINVEST_ID, "150", "0.15", "-0.50"),
        (v12.BENCHMARK_ID, "130", "0.13", "-0.55"),
    ):
        rows.append({"comparison_id": item, "ending_equity": ending, "cagr": cagr, "max_drawdown": drawdown})
    diff = report.build_relative_differences(rows)[0]
    assert diff["相較大盤期末資產差異"] == Decimal("-10")
    assert diff["相較大盤年化報酬率差異"] == Decimal("-0.01")
    assert diff["相較大盤最大回落改善"] == Decimal("0.35")
    assert diff["相較0050股息再投入期末資產差異"] == Decimal("-30")


def test_required_table_headers_are_fixed() -> None:
    assert report.MAIN_HEADERS == (
        "策略／比較基準", "成交方式", "期末資產", "總報酬", "年化報酬率", "最大回落",
        "獲利年度", "虧損年度", "平盤年度", "進場／出場／未平倉", "最大投入",
    )
    assert report.ANNUAL_HEADERS[-5:] == ("大盤報酬", "大盤最大回落", "進場", "出場", "年底／截止仍持有")


def test_parameter_values_are_human_readable() -> None:
    assert report._parameter_value("cagr", "0.1277708447") == "12.78%"
    assert report._parameter_value("ending_equity", "6067613.24822") == "6,067,613.25"
    assert report._parameter_value("calmar", "0.551521") == "0.552"


def test_validator_rejects_raw_price_as_ma21_signal_coordinate() -> None:
    bad = "前一日未還原收盤價不高於前一日 MA21，且當日未還原收盤價高於當日 MA21"
    errors = report._report_contract_errors(bad)
    assert any("raw price incorrectly used for technical rule" in error for error in errors)
