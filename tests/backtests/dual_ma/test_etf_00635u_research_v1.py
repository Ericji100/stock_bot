from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtests.dual_ma import etf_00635u_prep_v1 as prep
from backtests.dual_ma import etf_00635u_research_v1 as research
from backtests.dual_ma import etf_00635u_strategy_v1 as strategy


def raw_rows(count=170):
    start = date(2015, 4, 15)
    rows = []
    for index in range(count):
        close = Decimal("20") + Decimal(index) / Decimal("10")
        rows.append(
            strategy.RawETFRow(
                day=start + timedelta(days=index),
                open=close - Decimal("0.1"),
                high=close + Decimal("0.5"),
                low=close - Decimal("0.5"),
                close=close,
            )
        )
    return rows


def test_causal_indicator_builder_waits_for_own_history_and_is_prefix_stable():
    source = raw_rows()
    prefix = strategy.build_causal_indicator_rows(source[:150])
    full = strategy.build_causal_indicator_rows(source)
    assert prefix == full[:150]
    assert full[142].sma144 is None
    assert full[143].sma144 is not None
    assert full[7].k is None and full[7].d is None
    assert full[8].k is not None and full[8].d is not None


def test_future_split_does_not_rewrite_already_emitted_indicator_rows():
    source = raw_rows()
    changed = list(source)
    event = changed[155]
    changed[155] = strategy.RawETFRow(
        **{
            **event.__dict__,
            "price_rebase_factor": Decimal("0.5"),
            "share_factor": Decimal("2"),
        }
    )
    baseline_prefix = strategy.build_causal_indicator_rows(source[:155])
    full_with_future_event = strategy.build_causal_indicator_rows(changed)
    assert baseline_prefix == full_with_future_event[:155]


def test_main_comparison_is_exactly_seven_lines_and_taiex_is_not_main():
    rows = prep.fixture_rows()
    gold = {row.day: Decimal("100") + index for index, row in enumerate(rows)}
    lines = research.run_seven_lines(rows, gold)
    assert tuple(line.line_id for line in lines) == strategy.expected_comparison_lines()
    assert len(lines) == 7
    assert all("TAIEX" not in line.line_id for line in lines)
    assert lines[-1].role == "NON_TRADABLE_MARKET_BENCHMARK"


def test_official_index_alignment_fails_closed_on_missing_session():
    rows = prep.fixture_rows()
    gold = {row.day: Decimal("100") for row in rows[:-1]}
    with pytest.raises(ValueError, match="missing aligned sessions"):
        research.run_index_line(gold, [row.day for row in rows])


def test_gold_er_last_known_alignment_is_causal_and_has_staleness_provenance():
    sessions = [date(2021, 1, 4), date(2021, 1, 5), date(2021, 1, 6)]
    observations = [
        research.IndexObservation(
            source_date=sessions[0],
            available_date=sessions[0],
            value=Decimal("100"),
            source_timezone="America/New_York",
        ),
        research.IndexObservation(
            source_date=sessions[1],
            available_date=sessions[2],
            value=Decimal("110"),
            source_timezone="America/New_York",
        ),
    ]
    line = research.run_index_line(
        observations,
        sessions,
        alignment_policy="LAST_KNOWN_PUBLISHED",
    )
    assert [value for _, value in line.curve] == [
        Decimal("500000"), Decimal("500000"), Decimal("550000")
    ]
    ledger = line.audit["alignment_ledger"]
    assert ledger[1]["status"] == "LAST_KNOWN_PUBLISHED"
    assert ledger[1]["source_date"] == sessions[0]
    assert ledger[1]["staleness_calendar_days"] == 1
    assert line.audit["silent_forward_fill"] is False


def test_annual_slices_include_first_session_return_from_prior_year_end():
    curve = (
        (date(2020, 12, 31), Decimal("100")),
        (date(2021, 1, 4), Decimal("110")),
        (date(2021, 12, 31), Decimal("121")),
    )
    years = research.annual_slices(curve, initial_capital=Decimal("100"))
    year_2021 = next(item for item in years if item["start"].year == 2021)
    assert year_2021["starting_equity"] == Decimal("100")
    assert year_2021["ending_equity"] == Decimal("121")
    assert year_2021["return"] == Decimal("0.21")


def test_buy_hold_cash_and_reinvest_are_distinct_declared_comparators():
    rows = prep.fixture_rows()
    rows[2] = strategy.IndicatorRow(
        **{**rows[2].__dict__, "cash_dividend_per_share": Decimal("1")}
    )
    cash = research.run_buy_hold(rows, reinvest=False)
    reinvest = research.run_buy_hold(rows, reinvest=True)
    assert cash.line_id == "00635U_BUY_HOLD_CASH"
    assert reinvest.line_id == "00635U_BUY_HOLD_REINVEST"
    assert cash.curve != reinvest.curve


def test_buy_hold_initial_entry_waits_for_first_tradable_open():
    rows = prep.fixture_rows()
    rows[0] = strategy.IndicatorRow(
        **{
            **rows[0].__dict__,
            "raw_open": None,
            "volume": Decimal("0"),
            "tradable": False,
            "tradability_reason": "SUSPENDED",
        }
    )
    result = research.run_buy_hold(rows, reinvest=False)
    assert result.curve[0][1] == Decimal("500000")
    assert result.audit["initial_entry_deferred_sessions"] == (rows[0].day.isoformat(),)


def test_parameter_grid_matches_accepted_243_shape_without_kd_or_r_search():
    grid = research.parameter_grid()
    assert len(grid) == 243 == len(set(grid))
    assert grid[0] == (18, 126, 18, 50, 45)
    assert grid[-1] == (24, 162, 24, 60, 65)
    assert all(len(cell) == 5 for cell in grid)


def test_cost_sensitivity_has_both_strategies_both_timings_and_three_costs():
    output = research.cost_sensitivity(prep.fixture_rows())
    assert len(output) == 12
    assert {row["slippage_bps"] for row in output} == {20, 40, 60}
    assert {row["strategy"] for row in output} == {item.value for item in strategy.StrategyVersion}
    assert {row["timing"] for row in output} == {item.value for item in strategy.ExecutionTiming}


def test_rolling_windows_explicitly_labels_insufficient_history():
    short = ((date(2020, 1, 1), Decimal("100")), (date(2022, 1, 1), Decimal("110")))
    assert research.rolling_windows(short, 3) == [
        {"years": 3, "status": "INSUFFICIENT_DATA"}
    ]
    with pytest.raises(ValueError):
        research.rolling_windows(short, 7)


def test_regime_trade_stability_and_core_allocation_surfaces():
    result = strategy.run_strategy(
        prep.fixture_rows(),
        strategy.ETFResearchConfig(
            strategy=strategy.StrategyVersion.A,
            timing=strategy.ExecutionTiming.SIGNAL_CLOSE,
        ),
    )
    regimes = research.regime_summary(result.state.daily)
    assert {row["regime"] for row in regimes} == {"HIGH", "LOW"}
    stability = research.trade_stability(result.state.trades)
    assert stability["closed_trades"] >= 1
    curve = tuple((row["date"], row["equity"]) for row in result.state.daily)
    allocations = research.core_allocation_curves(curve, curve)
    assert set(allocations) == {0, 20, 40, 60}
    assert all(allocations[weight] == curve for weight in allocations)

    exact = research.run_core_allocations(
        prep.fixture_rows(),
        strategy=strategy.StrategyVersion.A,
        timing=strategy.ExecutionTiming.SIGNAL_CLOSE,
    )
    assert set(exact) == {0, 20, 40, 60}
    assert all(len(item) == len(prep.fixture_rows()) for item in exact.values())
    study = research.run_core_allocation_study(
        prep.fixture_rows(),
        strategy=strategy.StrategyVersion.A,
        timing=strategy.ExecutionTiming.SIGNAL_CLOSE,
    )
    assert len(study["summary"]) == 4
    assert all(row["reconstruction_error"] == 0 for row in study["daily"])
    assert all("core_cash" in row and "tactical_cash" in row for row in study["daily"])


def test_complete_metrics_rolling_drawdown_path_and_parameter_distribution():
    curve = (
        (date(2020, 1, 31), Decimal("500000")),
        (date(2020, 2, 28), Decimal("450000")),
        (date(2020, 3, 31), Decimal("475000")),
        (date(2023, 1, 31), Decimal("600000")),
    )
    metrics = research.performance_metrics(curve)
    assert metrics["total_return"] == Decimal("0.2")
    assert metrics["max_drawdown"] == Decimal("-0.1")
    path = research.path_diagnostics(curve)
    assert path["longest_consecutive_losing_months"] == 1
    windows = research.rolling_windows(curve, 3)
    assert windows[0]["sampling"] == "MONTH_END"
    assert "max_drawdown" in windows[0]

    grid = []
    for index, cell in enumerate(research.parameter_grid()):
        grid.append({
            "ma_fast": cell[0], "ma_slow": cell[1], "macd_fast": cell[2],
            "macd_slow": cell[3], "macd_signal": cell[4],
            "ending_equity": Decimal("400000") + index,
            "rolling_5y_worst_cagr": Decimal("-0.1") + Decimal(index) / Decimal("10000"),
        })
    summary = research.parameter_grid_summary(grid)
    assert summary["cells"] == 243
    assert summary["local_neighborhood_cells"] == 11
    assert summary["rolling_5y_worst_cagr"] == Decimal("-0.1")
