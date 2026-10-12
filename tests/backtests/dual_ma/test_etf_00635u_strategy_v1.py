import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtests.dual_ma import etf_00635u_strategy_v1 as subject
from backtests.dual_ma import etf_00635u_research_v1 as research
from backtests.dual_ma.costs import CATHAY_WEB_028_TW_V1


START = date(2016, 1, 4)


def row(
    offset,
    *,
    close,
    open_=None,
    low=None,
    sma21=10,
    sma144=9,
    hist=-1,
    k=60,
    d=50,
    **extra,
):
    return subject.IndicatorRow(
        day=START + timedelta(days=offset),
        raw_open=Decimal(str(close if open_ is None else open_)),
        raw_close=Decimal(str(close)),
        technical_close=Decimal(str(close)),
        technical_low=Decimal(str(close if low is None else low)),
        sma21=None if sma21 is None else Decimal(str(sma21)),
        sma144=None if sma144 is None else Decimal(str(sma144)),
        macd_histogram=None if hist is None else Decimal(str(hist)),
        k=None if k is None else Decimal(str(k)),
        d=None if d is None else Decimal(str(d)),
        **extra,
    )


def config(
    strategy=subject.StrategyVersion.B,
    timing=subject.ExecutionTiming.SIGNAL_CLOSE,
    bps=20,
):
    return subject.ETFResearchConfig(strategy=strategy, timing=timing, slippage_bps=bps)


def high_entry_rows(*tail):
    return [
        row(0, close=9, low=8, hist=-1, k=60, d=50),
        row(1, close=11, low=9, hist=-0.5, k=60, d=50),
        *tail,
    ]


def test_before_2r_kd_death_cross_is_ignored():
    rows = high_entry_rows(row(2, close=12, low=11, k=40, d=50))
    result = subject.run_strategy(rows, config())
    assert result.state.position is not None
    assert any(event["event"] == "KD_DEATH_CROSS_IGNORED_BEFORE_2R" for event in result.state.events)
    assert not any(trade["role"] == "EXIT" for trade in result.state.trades)


def test_2r_arm_is_permanent_and_later_new_death_cross_exits():
    rows = high_entry_rows(
        row(2, close=25, low=24, k=60, d=50),
        row(3, close=12, low=11, k=60, d=50),
        row(4, close=12, low=11, k=40, d=50),
    )
    result = subject.run_strategy(rows, config())
    arm = next(event for event in result.state.events if event["event"] == "KD_2R_PERMANENTLY_ARMED")
    exit_trade = result.state.trades[-1]
    assert arm["date"] == rows[2].day
    assert exit_trade["exit_reason"] == subject.ExitReason.KD_2R.value
    assert exit_trade["fill_date"] == rows[4].day
    assert arm["position_id"] == exit_trade["position_id"]
    assert exit_trade["kd_2r_arm_date"] == arm["date"]
    assert len({event["event_id"] for event in result.state.events}) == len(result.state.events)


def test_arm_while_already_k_below_d_has_no_retroactive_exit():
    rows = high_entry_rows(
        row(2, close=12, low=11, k=40, d=50),
        row(3, close=25, low=24, k=40, d=50),
        row(4, close=24, low=23, k=40, d=50),
    )
    result = subject.run_strategy(rows, config())
    assert any(event["event"] == "KD_2R_PERMANENTLY_ARMED" for event in result.state.events)
    assert result.state.position is not None
    assert not any(trade["role"] == "EXIT" for trade in result.state.trades)


def test_new_death_cross_on_arm_day_can_exit():
    rows = high_entry_rows(row(2, close=25, low=24, k=40, d=50))
    result = subject.run_strategy(rows, config())
    assert [trade["role"] for trade in result.state.trades] == ["ENTRY", "EXIT"]
    assert result.state.trades[-1]["exit_reason"] == subject.ExitReason.KD_2R.value
    assert any(event["event"] == "KD_2R_PERMANENTLY_ARMED" for event in result.state.events)


def test_fixed_stop_has_same_day_priority_over_armed_kd_cross():
    rows = high_entry_rows(
        row(2, close=25, low=24, k=60, d=50),
        row(3, close=7, low=7, k=40, d=50),
    )
    result = subject.run_strategy(rows, config())
    assert result.state.trades[-1]["exit_reason"] == subject.ExitReason.INITIAL_STOP.value
    assert any(event["event"] == "STOP_PRIORITY_OVER_KD" for event in result.state.events)


@pytest.mark.parametrize("strategy", [subject.StrategyVersion.A, subject.StrategyVersion.B])
def test_low_regime_position_keeps_strategy_a_macd_behavior(strategy):
    rows = [
        row(0, close=9, low=8, sma21=10, sma144=11, hist=0),
        row(1, close=9.5, low=9, sma21=10, sma144=11, hist=0.2),
        row(2, close=7, low=7, sma21=9, sma144=11, hist=-0.1),
    ]
    result = subject.run_strategy(rows, config(strategy=strategy))
    assert result.state.trades[0]["entry_type"] == subject.EntryType.LOW_MACD.value
    assert result.state.trades[-1]["exit_reason"] == subject.ExitReason.MACD.value
    assert any(event["event"] == "STOP_EXIT_SUPPRESSED_LOW_REGIME" for event in result.state.events)


def test_b_high_entry_fixed_stop_stays_active_after_regime_turns_low():
    rows = high_entry_rows(row(2, close=7, low=7, sma21=8, sma144=9, hist=-0.4))
    result = subject.run_strategy(rows, config())
    assert result.state.trades[-1]["exit_reason"] == subject.ExitReason.INITIAL_STOP.value


def test_signal_close_and_next_session_open_never_mix_execution_prices():
    rows = high_entry_rows(
        row(2, close=12, open_=20, low=11, k=60, d=50),
        row(3, close=25, open_=30, low=24, k=40, d=50),
        row(4, close=22, open_=18, low=21, k=40, d=50),
    )
    close_run = subject.run_strategy(rows, config(timing=subject.ExecutionTiming.SIGNAL_CLOSE))
    open_run = subject.run_strategy(rows, config(timing=subject.ExecutionTiming.NEXT_SESSION_OPEN))
    close_entry = close_run.state.trades[0]
    open_entry = open_run.state.trades[0]
    assert close_entry["signal_date"] == close_entry["fill_date"] == rows[1].day
    assert close_entry["reference_price"] == rows[1].raw_close
    assert open_entry["signal_date"] == rows[1].day
    assert open_entry["fill_date"] == rows[2].day
    assert open_entry["reference_price"] == rows[2].raw_open
    assert all(trade["timing"] == subject.ExecutionTiming.NEXT_SESSION_OPEN.value for trade in open_run.state.trades)


def test_pending_next_open_skips_missing_open_zero_volume_and_suspension():
    rows = high_entry_rows(
        row(
            2,
            close=11,
            open_=11,
            low=10,
            volume=Decimal("0"),
            tradable=False,
            tradability_reason="SUSPENDED",
        ),
        row(3, close=12, open_=12, low=11, volume=Decimal("0"), tradability_reason="ZERO_VOLUME"),
        row(4, close=13, open_=15, low=12, volume=Decimal("1000")),
    )
    rows[3] = subject.IndicatorRow(**{**rows[3].__dict__, "raw_open": None})
    result = subject.run_strategy(
        rows,
        config(timing=subject.ExecutionTiming.NEXT_SESSION_OPEN),
    )
    entry = result.state.trades[0]
    assert entry["signal_date"] == rows[1].day
    assert entry["fill_date"] == rows[4].day
    assert entry["reference_price"] == Decimal("15")
    deferred = [
        event for event in result.state.events
        if event["event"] == "PENDING_ORDER_DEFERRED_NON_TRADABLE"
    ]
    assert [event["date"] for event in deferred] == [rows[2].day, rows[3].day]


def test_nontradable_close_cannot_fill_or_create_signal_until_next_observable_session():
    rows = [
        row(0, close=9, low=8),
        row(1, close=11, low=9, volume=Decimal("0"), tradable=False),
        row(2, close=11, low=9, volume=Decimal("100")),
    ]
    result = subject.run_strategy(rows, config(timing=subject.ExecutionTiming.SIGNAL_CLOSE))
    assert result.state.trades[0]["signal_date"] == rows[2].day
    assert result.state.daily[1]["signal_observable"] is False
    assert result.state.daily[1]["shares"] == 0


def test_next_open_gap_at_or_below_stop_has_named_rejection_not_no_cash():
    rows = high_entry_rows(row(2, close=7, open_=7, low=7))
    result = subject.run_strategy(
        rows,
        config(timing=subject.ExecutionTiming.NEXT_SESSION_OPEN),
    )
    names = [event["event"] for event in result.state.events]
    assert "ENTRY_REJECTED_GAP_AT_OR_BELOW_STOP" in names
    assert "ENTRY_REJECTED_NO_CASH" not in names
    assert result.state.position is None


def test_fee_tax_and_20_percent_risk_sizing_match_accepted_cost_model():
    rows = high_entry_rows(row(2, close=7, low=7, k=40, d=50))
    result = subject.run_strategy(rows, config(bps=20))
    entry, exit_ = result.state.trades
    model = CATHAY_WEB_028_TW_V1.with_research_slippage(20)
    assert entry["cash_amount"] <= Decimal("500000")
    assert entry["initial_r"] <= Decimal("100000")
    assert entry["commission"] == model.buy(Decimal("11"), entry["shares"]).commission
    expected_sale = model.sell(Decimal("7"), exit_["shares"], subject.InstrumentType.ETF)
    assert exit_["commission"] == expected_sale.commission
    assert exit_["tax"] == expected_sale.tax > 0


def test_corporate_action_rebases_stop_and_shares_without_false_trade():
    rows = high_entry_rows(
        row(
            2,
            close=6,
            low=5.5,
            sma21=5,
            sma144=4.5,
            hist=-0.4,
            k=60,
            d=50,
            price_rebase_factor=Decimal("0.5"),
            share_factor=Decimal("2"),
        ),
        row(3, close=3.5, low=3.5, sma21=5, sma144=4.5, k=60, d=50),
    )
    result = subject.run_strategy(rows, config())
    entry_shares = result.state.trades[0]["shares"]
    assert any(event["event"] == "CORPORATE_ACTION_REBASE" for event in result.state.events)
    assert result.state.trades[-1]["role"] == "EXIT"
    assert result.state.trades[-1]["shares"] == entry_shares * 2
    assert result.state.trades[-1]["exit_reason"] == subject.ExitReason.INITIAL_STOP.value


def test_cash_dividend_and_daily_accounting_invariant():
    rows = high_entry_rows(
        row(2, close=12, low=11, cash_dividend_per_share=Decimal("0.5")),
        row(3, close=13, low=12),
    )
    result = subject.run_strategy(rows, config())
    assert any(event["event"] == "CASH_DIVIDEND" for event in result.state.events)
    for daily in result.state.daily:
        assert daily["cash"] >= 0
        assert daily["equity"] == daily["cash"] + daily["liquidation_value"]


def test_dividend_entitlement_survives_exit_until_later_payment():
    rows = high_entry_rows(
        row(
            2,
            close=7,
            low=7,
            dividend_event_id="DIV-1",
            dividend_stage="ENTITLEMENT",
            cash_dividend_per_share=Decimal("0.5"),
        ),
        row(
            3,
            close=7,
            low=7,
            dividend_event_id="DIV-1",
            dividend_stage="PAYMENT",
        ),
    )
    result = subject.run_strategy(
        rows,
        config(strategy=subject.StrategyVersion.A),
    )
    exit_trade = result.state.trades[-1]
    assert exit_trade["role"] == "EXIT"
    assert exit_trade["cash_distributions"] == Decimal("0.5") * exit_trade["shares"]
    assert any(event["event"] == "DIVIDEND_ENTITLEMENT_LOCKED" for event in result.state.events)
    assert any(event["event"] == "DIVIDEND_CASH_PAID" for event in result.state.events)
    assert result.state.dividend_receivables == {}

    first = subject.run_strategy(
        rows[:3],
        config(strategy=subject.StrategyVersion.A),
    )
    resumed = subject.run_strategy(
        rows[3:],
        config(strategy=subject.StrategyVersion.A),
        initial_state=first.checkpoint(),
    )
    assert resumed.checkpoint() == result.checkpoint()


def test_checkpoint_resume_is_byte_semantically_equivalent_to_one_pass():
    rows = high_entry_rows(
        row(2, close=25, low=24, k=60, d=50),
        row(3, close=12, low=11, k=40, d=50),
    )
    full = subject.run_strategy(rows, config())
    first = subject.run_strategy(rows[:2], config())
    resumed = subject.run_strategy(rows[2:], config(), initial_state=first.checkpoint())
    assert resumed.checkpoint() == full.checkpoint()


def test_checkpoint_full_schema_multi_cut_replay_and_date_boundary_fail_closed():
    rows = [
        row(0, close=9, low=8, hist=-1),
        row(1, close=11, low=9, hist=-0.5),
        row(2, close=12, open_=12, low=10, hist=0.1),
        row(
            3,
            close=12,
            low=11,
            hist=0.2,
            dividend_event_id="DIV-CHECKPOINT",
            dividend_stage="ENTITLEMENT",
            cash_dividend_per_share=Decimal("0.5"),
        ),
        row(4, close=7, low=7, hist=-0.1),
        row(
            5,
            close=3.5,
            open_=3.5,
            low=3.4,
            sma21=5,
            sma144=4.5,
            hist=-0.2,
            price_rebase_factor=Decimal("0.5"),
            share_factor=Decimal("2"),
            dividend_event_id="DIV-CHECKPOINT",
            dividend_stage="PAYMENT",
        ),
    ]
    cfg = config(
        strategy=subject.StrategyVersion.A,
        timing=subject.ExecutionTiming.NEXT_SESSION_OPEN,
    )
    full = subject.run_strategy(rows, cfg)
    canonical = lambda result: json.dumps(
        result.checkpoint(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    for cut in range(1, len(rows)):
        first = subject.run_strategy(rows[:cut], cfg)
        restored = subject.state_from_dict(first.checkpoint())
        if restored.daily:
            assert isinstance(restored.daily[-1]["date"], date)
            assert isinstance(restored.daily[-1]["equity"], Decimal)
        resumed = subject.run_strategy(rows[cut:], cfg, initial_state=first.checkpoint())
        assert canonical(resumed) == canonical(full)
        assert research.regime_summary(resumed.state.daily)

    checkpoint = subject.run_strategy(rows[:3], cfg).checkpoint()
    with pytest.raises(ValueError, match="strictly after checkpoint"):
        subject.run_strategy(rows[2:], cfg, initial_state=checkpoint)
    with pytest.raises(ValueError, match="strictly after checkpoint"):
        subject.run_strategy(rows[1:2], cfg, initial_state=checkpoint)


def test_dividend_unknown_duplicate_and_unpaid_receivable_are_fail_closed_and_visible():
    payment_only = row(
        0,
        close=9,
        dividend_event_id="UNKNOWN",
        dividend_stage="PAYMENT",
    )
    with pytest.raises(ValueError, match="no prior entitlement"):
        subject.run_strategy([payment_only], config())
    with pytest.raises(ValueError, match="no prior entitlement"):
        research.run_buy_hold([payment_only], reinvest=False)

    rows = high_entry_rows(
        row(
            2,
            close=12,
            low=11,
            dividend_event_id="OPEN-RIGHT",
            dividend_stage="ENTITLEMENT",
            cash_dividend_per_share=Decimal("0.25"),
        )
    )
    result = subject.run_strategy(rows, config())
    assert "OPEN-RIGHT" in result.state.dividend_receivables
    assert result.state.daily[-1]["dividend_receivable_cash_excluded"] > 0
    assert result.state.daily[-1]["equity"] < result.state.daily[-1]["equity_including_receivables_audit"]
    comparator = research.run_buy_hold(rows, reinvest=False)
    assert "OPEN-RIGHT" in comparator.audit["unpaid_dividend_receivables"]

    duplicate_payment_rows = rows + [
        row(3, close=12, low=11, dividend_event_id="OPEN-RIGHT", dividend_stage="PAYMENT"),
        row(4, close=12, low=11, dividend_event_id="OPEN-RIGHT", dividend_stage="PAYMENT"),
    ]
    with pytest.raises(ValueError, match="duplicate dividend payment"):
        subject.run_strategy(duplicate_payment_rows, config())
    with pytest.raises(ValueError, match="duplicate dividend payment"):
        research.run_buy_hold(duplicate_payment_rows, reinvest=False)


def test_no_prelisting_rows_and_no_signals_before_indicator_readiness():
    invalid = row(0, close=9)
    invalid = subject.IndicatorRow(**{**invalid.__dict__, "day": date(2015, 4, 14)})
    with pytest.raises(ValueError, match="pre-listing"):
        subject.run_strategy([invalid], config())

    rows = [
        row(0, close=9, low=8, sma144=None, k=None, d=None),
        row(1, close=11, low=9, sma144=9, k=60, d=50),
    ]
    result = subject.run_strategy(rows, config())
    assert not result.state.trades
    assert result.state.daily[0]["signal_ready"] is False


def test_frozen_research_dimensions_and_seven_main_lines():
    assert subject.STRESS_SLIPPAGE_BPS == (20, 40, 60)
    assert subject.CORE_ALLOCATIONS == (0, 20, 40, 60)
    assert subject.GRID_SIZE == 243
    assert len(subject.expected_comparison_lines()) == 7
    assert "TAIEX" not in " ".join(subject.expected_comparison_lines())
