from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from backtests.dual_ma.etf_0050_base_v1 import StrategyRunSlice, StrategySpec
from backtests.dual_ma.etf_0050_execution_stability_v1 import (
    PARAMETER_GRID,
    TIMING_NEXT_CLOSE,
    TIMING_NEXT_OPEN,
    _broken_markdown_links,
    _normalize_allocation_dividend_event,
    _readme_missing_artifacts,
    _transfer_at_close,
    annual_rebalanced_sleeves,
    build_parameter_indicator_rows,
    causal_market_regimes,
    combine_isolated_sleeves,
    parameter_robustness,
    replay_core_sleeve,
    schedule_frozen_orders,
    trade_stability,
)
from backtests.dual_ma.models import BacktestResult, Bar, StrategyKind
from backtests.dual_ma.costs import CATHAY_WEB_028_TW_V1, CostModel


def _run(day):
    result = object.__new__(BacktestResult)
    spec = StrategySpec("TEST", "test", StrategyKind.LONG, "CUSTOM")
    return StrategyRunSlice(spec, result, (), ({
        "date": day,
        "role": "MOTHER",
        "status": "FILLED",
        "leg_id": "L1",
        "reference_price": Decimal("100"),
        "entry_reference_close": Decimal("100"),
        "initial_stop": Decimal("90"),
        "exit_reason": None,
    },), ())


def _dividend_package():
    days = tuple(date(2006, 11, day) for day in (28, 29, 30)) + (date(2006, 12, 1),)
    raw = {day: Bar.from_values(day, 100, 101, 99, 100) for day in days}
    event = SimpleNamespace(
        ex_date=days[1], payment_date=days[2], amount_per_share=Decimal("10"),
    )
    return SimpleNamespace(
        sessions=days, raw_by_date=raw, dividends=(event,),
        split=SimpleNamespace(effective_date=date(2099, 1, 1), ratio=Decimal("1")),
    )


def _empty_run():
    result = object.__new__(BacktestResult)
    spec = StrategySpec("EMPTY", "empty", StrategyKind.LONG, "CUSTOM")
    return StrategyRunSlice(spec, result, (), (), ())


def test_next_open_uses_future_raw_open_without_revising_signal_and_rebases_stop():
    first, second = date(2020, 1, 2), date(2020, 1, 3)
    raw = {
        first: Bar.from_values(first, 99, 101, 98, 100),
        second: Bar.from_values(second, 51, 53, 50, 52),
    }
    indicators = {
        first: {"technical_close": Decimal("100"), "price_rebase_factor": Decimal("1")},
        second: {"technical_close": Decimal("52"), "price_rebase_factor": Decimal("0.5")},
    }
    delayed, audit = schedule_frozen_orders(
        run=_run(first), sessions=(first, second), raw_by_date=raw,
        indicator_by_date=indicators, timing=TIMING_NEXT_OPEN,
    )
    order = delayed.transactions[0]
    assert order["signal_date"] == first
    assert order["date"] == second
    assert order["reference_price"] == Decimal("51")
    assert order["initial_stop_raw_override"] == Decimal("45")
    assert audit[0]["intervening_rebase_factor"] == Decimal("0.5")


def test_next_close_uses_next_raw_close_and_last_session_is_not_filled():
    first, second = date(2020, 1, 2), date(2020, 1, 3)
    raw = {
        first: Bar.from_values(first, 99, 101, 98, 100),
        second: Bar.from_values(second, 100, 104, 99, 103),
    }
    indicators = {
        first: {"technical_close": Decimal("100"), "price_rebase_factor": Decimal("1")},
        second: {"technical_close": Decimal("103"), "price_rebase_factor": Decimal("1")},
    }
    delayed, audit = schedule_frozen_orders(
        run=_run(first), sessions=(first, second), raw_by_date=raw,
        indicator_by_date=indicators, timing=TIMING_NEXT_CLOSE,
    )
    assert delayed.transactions[0]["reference_price"] == Decimal("103")
    last, last_audit = schedule_frozen_orders(
        run=_run(second), sessions=(first, second), raw_by_date=raw,
        indicator_by_date=indicators, timing=TIMING_NEXT_CLOSE,
    )
    assert not last.transactions
    assert last_audit[0]["status"] == "NO_NEXT_SESSION_BEFORE_CUTOFF"


def test_research_cost_model_allows_requested_40_and_60_bps_without_relaxing_v04_gate():
    assert CostModel().with_research_slippage(40).slippage_bps == 40
    assert CostModel().with_research_slippage(60).slippage_bps == 60
    try:
        CostModel().with_slippage(40)
    except ValueError:
        pass
    else:
        raise AssertionError("accepted v0.4 slippage gate must remain closed")


def test_parameter_grid_is_complete_unique_factorial():
    assert len(PARAMETER_GRID) == 243
    assert len(set(PARAMETER_GRID)) == 243
    assert (21, 144, 21, 55, 55) in PARAMETER_GRID


def test_parameter_indicator_windows_and_macd_mapping_are_explicit():
    rows = [
        {"date": date(2020, 1, day).isoformat(), "open": day, "high": day,
         "low": day, "close": day}
        for day in range(1, 7)
    ]
    result = build_parameter_indicator_rows(
        rows, (), ma_fast=2, ma_slow=4,
        macd_fast=2, macd_slow=3, macd_signal=2,
    )
    assert result[0]["sma21"] is None and result[0]["sma144"] is None
    assert result[1]["sma21"] == Decimal("1.5")
    assert result[3]["sma144"] == Decimal("2.5")
    assert result[-1]["histogram"] is not None


def test_combined_sleeves_conserve_assets_without_cross_contamination():
    first, second = date(2020, 1, 2), date(2021, 1, 4)
    core = {"curve": [
        {"date": first, "cash": Decimal("10"), "shares": 1, "equity": Decimal("200000")},
        {"date": second, "cash": Decimal("12"), "shares": 1, "equity": Decimal("220000")},
    ]}
    tactical = {"curve": [
        {"date": first, "cash": Decimal("300000"), "shares": 0, "equity": Decimal("300000")},
        {"date": second, "cash": Decimal("310000"), "shares": 0, "equity": Decimal("310000")},
    ]}
    daily, summary = combine_isolated_sleeves(core=core, tactical=tactical, core_weight_pct=40)
    assert daily[0]["equity"] == Decimal("500000")
    assert daily[1]["equity"] == Decimal("530000")
    assert daily[1]["cash"] == Decimal("310012")
    assert daily[1]["core_cash"] == Decimal("12")
    assert daily[1]["tactical_cash"] == Decimal("310000")
    assert summary["ending_equity"] == Decimal("530000")


def test_market_regime_uses_prior_session_trailing_return_not_current_return():
    rows = [
        {"date": date(2020, 1, 1).replace(day=1).isoformat(), "equivalent_500k": "100"}
        for _ in range(254)
    ]
    for index, row in enumerate(rows):
        row["date"] = date(2020, 1, 1).fromordinal(date(2020, 1, 1).toordinal() + index).isoformat()
    rows[-2]["equivalent_500k"] = "111"
    rows[-1]["equivalent_500k"] = "50"
    result = causal_market_regimes(rows)
    assert result[-1]["regime"] == "BULL"
    assert result[-1]["prior_session_trailing_252_return"] == Decimal("0.11")


def test_trade_stability_counts_consecutive_losses_and_extremes():
    rows = [
        {"exit_date": f"2020-01-0{i}", "leg_id": f"L{i}", "net_pnl_or_estimate": pnl}
        for i, pnl in enumerate(("5", "-2", "-3", "4", "-9"), 1)
    ]
    result = trade_stability(rows)
    assert result["longest_losing_streak"] == 2
    assert result["best_leg_id"] == "L1"
    assert result["worst_leg_id"] == "L5"


def test_annual_rebalance_cash_donor_has_no_etf_sale_or_sell_tax():
    donor = {"cash": Decimal("517790.3326"), "shares": 0}
    recipient = {"cash": Decimal("100"), "shares": 10}
    ledger = []
    transferred = _transfer_at_close(
        day=date(2008, 1, 2), reference=Decimal("100"), desired=Decimal("6264.4783"),
        donor_name="TACTICAL", donor=donor, recipient_name="CORE", recipient=recipient,
        model=CATHAY_WEB_028_TW_V1.with_research_slippage(20), ledger=ledger,
    )
    assert transferred == Decimal("6264.4783")
    assert [row["role"] for row in ledger] == ["ANNUAL_CASH_TRANSFER"]
    assert ledger[0]["securities_sold"] is False
    assert ledger[0]["sell_tax"] == 0
    assert donor["shares"] == 0


def test_annual_rebalance_invested_donor_sells_real_whole_shares():
    donor = {"cash": Decimal("0"), "shares": 1000}
    recipient = {"cash": Decimal("0"), "shares": 0}
    ledger = []
    transferred = _transfer_at_close(
        day=date(2021, 1, 4), reference=Decimal("100"), desired=Decimal("10000"),
        donor_name="CORE", donor=donor, recipient_name="TACTICAL", recipient=recipient,
        model=CATHAY_WEB_028_TW_V1.with_research_slippage(20), ledger=ledger,
    )
    sale, transfer = ledger
    assert sale["role"] == "ANNUAL_REBALANCE_SELL"
    assert sale["shares"] > 0
    assert sale["tax"] > 0
    assert donor["shares"] == 1000 - sale["shares"]
    assert transfer["role"] == "ANNUAL_CASH_TRANSFER"
    assert transfer["securities_sold"] is True
    assert transferred == Decimal("10000")


def test_core_dividend_is_cash_on_payment_day_and_reinvested_next_session():
    package = _dividend_package()
    result = replay_core_sleeve(package=package, initial_capital=Decimal("100000"))
    before, payment, reinvest = result["curve"][1:]
    entitlement = Decimal(before["shares"]) * Decimal("10")
    assert payment["cash"] == before["cash"] + entitlement
    assert payment["shares"] == before["shares"]
    assert reinvest["shares"] > payment["shares"]
    assert reinvest["cash"] < payment["cash"]
    reinvest_tx = next(row for row in result["transactions"] if row["role"] == "CORE_DIVIDEND_REINVEST")
    assert reinvest_tx["date"] == package.sessions[3]


def test_no_rebalance_and_annual_match_daily_before_first_rebalance():
    package = _dividend_package()
    core = replay_core_sleeve(package=package, initial_capital=Decimal("100000"))
    tactical = {"curve": [
        {"date": day, "cash": Decimal("400000"), "shares": 0,
         "liquidation_value": Decimal("0"), "equity": Decimal("400000"),
         "drawdown": Decimal("0")}
        for day in package.sessions
    ]}
    no_rebalance, _summary = combine_isolated_sleeves(
        core=core, tactical=tactical, core_weight_pct=20,
    )
    annual, ledger, summary = annual_rebalanced_sleeves(
        package=package, run=_empty_run(), core_weight_pct=20,
    )
    assert summary["rebalance_events"] == 0
    assert not any(row["role"].startswith("ANNUAL_") for row in ledger)
    for plain, adjusted in zip(no_rebalance, annual):
        for key in ("core_cash", "core_shares", "core_equity",
                    "tactical_cash", "tactical_shares", "tactical_equity", "equity"):
            assert plain[key] == adjusted[key]


def test_no_rebalance_dividend_ledger_has_causal_date_and_cash_amount():
    sessions = (date(2006, 11, 30), date(2006, 12, 1), date(2006, 12, 4))
    common = {
        "ex_date": date(2006, 11, 30), "payment_date": date(2006, 12, 1),
        "shares": 1965, "amount_per_share": Decimal("4"), "cash": Decimal("7860"),
    }
    entitlement = _normalize_allocation_dividend_event(
        {**common, "stage": "ENTITLEMENT_LOCKED"}, sessions,
    )
    payment = _normalize_allocation_dividend_event(
        {**common, "stage": "CASH_PAID"}, sessions,
    )
    assert entitlement["date"] == date(2006, 11, 30)
    assert entitlement["role"] == "DIVIDEND_ENTITLEMENT_LOCKED"
    assert entitlement["cash_amount"] == Decimal("7860")
    assert payment["date"] == date(2006, 12, 1)
    assert payment["role"] == "DIVIDEND_CASH_PAID"
    assert payment["cash_amount"] == Decimal("7860")


def test_report_link_validator_checks_markdown_anchor_targets(tmp_path):
    (tmp_path / "report.md").write_text("# 報告\n\n## 主摘要\n", encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text("[摘要](report.md#主摘要)\n", encoding="utf-8")
    assert _broken_markdown_links(tmp_path) == []
    readme.write_text("[不存在](report.md#不存在)\n", encoding="utf-8")
    assert _broken_markdown_links(tmp_path) == ["README.md->report.md#不存在 (missing anchor)"]


def test_readme_catalog_must_cover_every_manifest_artifact(tmp_path):
    (tmp_path / "report.md").write_text("# 報告\n", encoding="utf-8")
    (tmp_path / "listed.csv").write_text("x\n", encoding="utf-8")
    (tmp_path / "missing.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[報告](report.md)\n[附件](listed.csv)\n", encoding="utf-8",
    )
    artifacts = ("README.md", "report.md", "listed.csv", "missing.json")
    assert _readme_missing_artifacts(tmp_path, artifacts) == ["missing.json"]


def test_parameter_robustness_returns_neighborhood_distribution_and_heatmap():
    rows = []
    for ma_fast, ma_slow, macd_fast, macd_slow, macd_signal in PARAMETER_GRID:
        value = Decimal(ma_fast + ma_slow + macd_fast + macd_slow + macd_signal) / Decimal("1000")
        rows.append({
            "parameter_id": f"{ma_fast}-{ma_slow}-{macd_fast}-{macd_slow}-{macd_signal}",
            "ma_fast": ma_fast, "ma_slow": ma_slow, "macd_fast": macd_fast,
            "macd_slow": macd_slow, "macd_signal": macd_signal,
            "cagr": value, "ending_equity": value * Decimal("500000"),
            "max_drawdown": -value, "calmar": Decimal("1"),
            "rolling_5y_worst_cagr": value / Decimal("2"),
        })
    neighborhoods, distributions, heatmap = parameter_robustness(rows)
    assert len(neighborhoods) == 243
    assert len(distributions) == 5
    assert len(heatmap) == 9
    center = next(row for row in neighborhoods if row["parameter_id"] == "21-144-21-55-55")
    assert center["neighbor_count"] == 11
