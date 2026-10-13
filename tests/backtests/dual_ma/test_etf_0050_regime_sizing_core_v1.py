from __future__ import annotations

from decimal import Decimal

import pytest

from backtests.dual_ma import etf_0050_regime_sizing_core_v1 as study
from backtests.dual_ma import etf_0050_execution_stability_v1 as analysis
from backtests.dual_ma import etf_0050_ma21_ma144_regime_v1 as regime


@pytest.fixture(scope="module")
def setup():
    package, raw_rows, events = study.accepted.load_package()
    indicators = regime.build_indicator_rows(raw_rows, events)
    by_day = {row["date"]: row for row in indicators}
    frozen, _daily, _events = regime.build_regime_run(
        indicator_rows=indicators, raw_by_date=package.raw_by_date,
        strategy_id=regime.FIXED_ID, moving_stop=False,
        start=study.START, end=study.END, slippage_bps=20,
    )
    scheduled = {}
    for timing in (study.TIMING_NEXT_OPEN, study.TIMING_SIGNAL_CLOSE):
        run, _audit = analysis.schedule_frozen_orders(
            run=frozen, sessions=package.sessions, raw_by_date=package.raw_by_date,
            indicator_by_date=by_day, timing=timing,
        )
        scheduled[timing] = study.annotate_signal_regime(run, by_day)
    return package, scheduled


@pytest.fixture(scope="module")
def main_scenarios(setup):
    package, scheduled = setup
    return {
        (core, high): study.build_scenario(
            package=package, run=scheduled[study.TIMING_NEXT_OPEN],
            core_pct=core, high_pct=high, timing=study.TIMING_NEXT_OPEN, bps=20,
        )
        for core in study.CORE_WEIGHTS for high in study.HIGH_WEIGHTS
    }


def test_matrix_contract_is_exactly_six_main_combinations():
    assert {(core, high) for core in study.CORE_WEIGHTS for high in study.HIGH_WEIGHTS} == {
        (0, 50), (0, 75), (0, 100), (20, 50), (20, 75), (20, 100),
    }


def test_reader_facing_formatters_avoid_negative_zero_and_internal_exit_codes():
    assert study._fmt_pct3(Decimal("-0.00003799936020942862")) == "-0.004%"
    assert study._exit_reason_zh("MACD_RED_TO_GREEN") == "MACD 翻綠"
    assert study._exit_reason_zh("INITIAL_PROTECTIVE_STOP") == "固定初始防守"


def test_core_zero_high_100_is_exact_v17_parity(main_scenarios, monkeypatch, tmp_path):
    # The paths are environment-owned formal artifacts; skip only when a developer
    # intentionally runs this unit file without the shared workdata root.
    if not (study.SEALED_RUN / "next-session-open-scenario-summary.csv").is_file():
        pytest.skip("sealed v1.7 artifacts are not mounted")
    assert study.exact_v17_parity(main_scenarios[(0, 100)])["status"] == "PASS_EXACT"


@pytest.mark.parametrize("high_pct", study.HIGH_WEIGHTS)
def test_entry_budget_fraction_is_signal_regime_based(main_scenarios, high_pct):
    tx = main_scenarios[(0, high_pct)]["tactical"]["transactions"]
    entries = [row for row in tx if row["role"] == "MOTHER"]
    assert entries
    assert {row["signal_regime"] for row in entries} == {"HIGH", "LOW"}
    for row in entries:
        expected = Decimal("1") if row["signal_regime"] == "LOW" else Decimal(high_pct) / 100
        assert row["entry_budget_fraction"] == expected
        assert row["target_budget"] == row["available_cash_before_entry"] * expected
        assert row["unused_cash_after_entry"] >= 0


def test_no_mid_position_resize_or_stacking(main_scenarios):
    for result in main_scenarios.values():
        tactical = result["tactical"]
        assert {row["role"] for row in tactical["transactions"]} <= {"MOTHER", "EXIT"}
        active = None
        for row in tactical["transactions"]:
            if row["role"] == "MOTHER":
                assert active is None
                active = row["leg_id"]
            else:
                assert row["leg_id"] == active
                active = None


def test_twenty_percent_core_is_isolated_reinvesting_and_never_rebalanced(main_scenarios):
    result = main_scenarios[(20, 75)]
    roles = {row["role"] for row in result["core"]["transactions"]}
    assert roles <= {"CORE_INITIAL_BUY", "CORE_DIVIDEND_REINVEST", "SPLIT_REBASE_ONLY"}
    assert "CORE_INITIAL_BUY" in roles
    assert "CORE_DIVIDEND_REINVEST" in roles
    assert result["summary"]["minimum_core_cash"] >= 0
    assert result["summary"]["minimum_tactical_cash"] >= 0
    assert result["tactical"]["metrics"]["external_principal"] == Decimal("400000")


def test_combined_daily_conservation(main_scenarios):
    for result in main_scenarios.values():
        for row in result["daily"]:
            assert row["equity"] == row["core_equity"] + row["tactical_equity"]
            assert row["cash"] == row["core_cash"] + row["tactical_cash"]
            assert row["core_cash"] >= 0 and row["tactical_cash"] >= 0


def test_cross_regime_changes_do_not_change_an_open_position(main_scenarios):
    result = main_scenarios[(0, 50)]
    entry_regime = {
        row["leg_id"]: row["signal_regime"]
        for row in result["tactical"]["transactions"] if row["role"] == "MOTHER"
    }
    assert entry_regime
    for row in result["tactical"]["curve"]:
        if row["active_leg_id"] is not None:
            assert row["active_signal_regime"] == entry_regime[row["active_leg_id"]]


def test_higher_slippage_is_a_fresh_replay_and_costs_more(setup):
    package, scheduled = setup
    low = study.build_scenario(
        package=package, run=scheduled[study.TIMING_NEXT_OPEN], core_pct=0,
        high_pct=75, timing=study.TIMING_NEXT_OPEN, bps=20,
    )
    high = study.build_scenario(
        package=package, run=scheduled[study.TIMING_NEXT_OPEN], core_pct=0,
        high_pct=75, timing=study.TIMING_NEXT_OPEN, bps=60,
    )
    assert high["tactical"]["metrics"]["actual_slippage_cost"] > low["tactical"]["metrics"]["actual_slippage_cost"]
    assert high["summary"]["ending_equity"] < low["summary"]["ending_equity"]


def test_signal_close_is_only_a_paired_sensitivity(setup):
    package, scheduled = setup
    result = study.build_scenario(
        package=package, run=scheduled[study.TIMING_SIGNAL_CLOSE], core_pct=20,
        high_pct=50, timing=study.TIMING_SIGNAL_CLOSE, bps=20,
    )
    assert result["summary"]["execution_timing"] == study.TIMING_SIGNAL_CLOSE
    assert result["summary"]["slippage_bps"] == 20


@pytest.mark.parametrize("high_pct", (50, 75))
def test_ma21_equal_ma144_is_high_and_later_regime_change_does_not_resize(setup, high_pct):
    package, scheduled = setup
    source = scheduled[study.TIMING_NEXT_OPEN]
    first_entry = next(row for row in source.transactions if row["role"] == "MOTHER")
    synthetic = {}
    for row in source.transactions:
        if row["role"] != "MOTHER":
            continue
        if row["leg_id"] == first_entry["leg_id"]:
            synthetic[row["signal_date"]] = {"sma21": Decimal("100"),
                                              "sma144": Decimal("100")}
        elif row["signal_regime"] == "HIGH":
            synthetic[row["signal_date"]] = {"sma21": Decimal("101"),
                                              "sma144": Decimal("100")}
        else:
            synthetic[row["signal_date"]] = {"sma21": Decimal("99"),
                                              "sma144": Decimal("100")}
    entry_index = package.sessions.index(first_entry["date"])
    post_entry_day = package.sessions[entry_index + 1]
    synthetic[post_entry_day] = {"sma21": Decimal("90"), "sma144": Decimal("100")}

    annotated = study.annotate_signal_regime(source, synthetic)
    boundary = next(row for row in annotated.transactions
                    if row["leg_id"] == first_entry["leg_id"] and row["role"] == "MOTHER")
    assert boundary["signal_regime"] == "HIGH"
    assert boundary["regime_equality_is_high"] is True

    replay = study.replay_regime_sizing(
        package=package, run=annotated, start=study.START, end=study.END,
        slippage_bps=20, initial_capital=study.CAPITAL,
        high_weight_pct=high_pct,
    )
    filled = next(row for row in replay["transactions"]
                  if row["leg_id"] == first_entry["leg_id"] and row["role"] == "MOTHER")
    assert filled["entry_budget_fraction"] == Decimal(high_pct) / 100
    curve = {row["date"]: row for row in replay["curve"]}
    assert curve[first_entry["date"]]["shares"] > 0
    assert curve[post_entry_day]["shares"] == curve[first_entry["date"]]["shares"]
    assert curve[post_entry_day]["active_signal_regime"] == "HIGH"
    assert {row["role"] for row in replay["transactions"]
            if row["leg_id"] == first_entry["leg_id"]} == {"MOTHER", "EXIT"}

