from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backtests.dual_ma import etf_0050_ma21_ma144_regime_v1 as subject


def row(offset, *, close, low=None, sma21=10, sma144=9, hist=0, factor=1, dow=()):
    day = date(2020, 1, 1) + timedelta(days=offset)
    return {
        "date": day, "technical_close": Decimal(str(close)),
        "technical_low": Decimal(str(low if low is not None else close)),
        "sma21": None if sma21 is None else Decimal(str(sma21)),
        "sma144": None if sma144 is None else Decimal(str(sma144)),
        "histogram": Decimal(str(hist)), "price_rebase_factor": Decimal(str(factor)),
        "dow_events": dow,
    }


def run(rows, *, moving=False, bps=20):
    raw = {item["date"]: SimpleNamespace(close=item["technical_close"]) for item in rows}
    sid = subject.MOVING_ID if moving else subject.FIXED_ID
    return subject.build_regime_run(
        indicator_rows=rows, raw_by_date=raw, strategy_id=sid, moving_stop=moving,
        start=rows[0]["date"], end=rows[-1]["date"], slippage_bps=bps,
    )


def test_sma_equality_uses_ma21_breakout_branch():
    rows = [row(0, close=9, low=8, sma21=10, sma144=10, hist=-1),
            row(1, close=11, low=9, sma21=10, sma144=10, hist=-.5)]
    frozen, daily, events = run(rows)
    assert daily[-1]["regime"] == "SMA21_GE_SMA144"
    assert daily[-1]["selected_entry_type"] == "MA21_BREAKOUT"
    assert frozen.transactions[0]["role"] == "MOTHER"


@pytest.mark.parametrize(
    "rows",
    [
        [row(0, close=11, sma21=10, sma144=9, hist=-1), row(1, close=11, sma21=10, sma144=9, hist=1)],
        [row(0, close=9, sma21=10, sma144=11, hist=-1), row(1, close=11, sma21=10, sma144=11, hist=-.5)],
    ],
)
def test_non_selected_cross_is_ignored(rows):
    frozen, daily, _events = run(rows)
    assert not frozen.transactions
    assert daily[-1]["selected_entry_type"] == ""


def test_macd_green_to_red_entry_is_armed_immediately():
    rows = [row(0, close=9, low=8, sma21=10, sma144=11, hist=0),
            row(1, close=9.5, low=9, sma21=10, sma144=11, hist=.1)]
    _frozen, daily, events = run(rows)
    entry = next(item for item in events if item["event"] == "ENTRY")
    assert daily[-1]["selected_entry_type"] == "MACD_GREEN_TO_RED"
    assert entry["armed_on_entry"] is True


def test_equal_entry_and_candidate_stop_is_skipped_and_stays_flat():
    rows = [row(0, close=11, low=11, sma21=10, sma144=12, hist=0),
            row(1, close=9.5, low=9.5, sma21=10, sma144=11, hist=.1)]
    frozen, daily, events = run(rows)
    assert not frozen.transactions
    assert daily[-1]["entry_status"] == "SKIPPED_INVALID_STOP"
    assert any(item["event"] == "ENTRY_SKIPPED_INVALID_STOP" for item in events)


def test_stop_has_priority_over_same_day_macd_exit():
    rows = [row(0, close=9, low=8, sma21=10, sma144=10, hist=.2),
            row(1, close=11, low=9, sma21=10, sma144=10, hist=.3),
            row(2, close=7, low=7, sma21=10, sma144=9, hist=-.1)]
    frozen, _daily, events = run(rows)
    assert frozen.transactions[-1]["exit_reason"] == "INITIAL_PROTECTIVE_STOP"
    assert any(item["event"] == "STOP_PRIORITY_OVER_MACD" for item in events)


def test_low_regime_suppresses_price_stop_and_macd_is_only_exit():
    rows = [row(0, close=9, low=8, sma21=10, sma144=10, hist=.2),
            row(1, close=11, low=9, sma21=10, sma144=10, hist=.3),
            row(2, close=7, low=7, sma21=9, sma144=10, hist=.2),
            row(3, close=7, low=7, sma21=9, sma144=10, hist=-.1)]
    frozen, daily, events = run(rows)
    assert [item["role"] for item in frozen.transactions] == ["MOTHER", "EXIT"]
    assert frozen.transactions[-1]["exit_reason"] == "MACD_RED_TO_GREEN"
    assert daily[-2]["stop_enforcement"] == "INACTIVE_LT_REGIME"
    assert daily[-2]["suppressed_stop_hit"] is True
    assert any(item["event"] == "STOP_EXIT_SUPPRESSED_LOW_REGIME" for item in events)


def test_stop_state_reactivates_when_regime_returns_high():
    rows = [row(0, close=9, low=8, sma21=10, sma144=10, hist=.2),
            row(1, close=11, low=9, sma21=10, sma144=10, hist=.3),
            row(2, close=7, low=7, sma21=9, sma144=10, hist=.2),
            row(3, close=7, low=7, sma21=10, sma144=9, hist=.1)]
    frozen, daily, events = run(rows)
    assert frozen.transactions[-1]["exit_reason"] == "INITIAL_PROTECTIVE_STOP"
    assert daily[-2]["suppressed_stop_hit"] is True
    assert any(item["event"] == "STOP_ENFORCEMENT_REACTIVATED_GE_REGIME" for item in events)


def test_moving_stop_keeps_updating_while_enforcement_is_inactive():
    defense = SimpleNamespace(defense=Decimal("10"), version=2)
    rows = [row(0, close=9, low=8, sma21=10, sma144=10, hist=.2),
            row(1, close=11, low=9, sma21=10, sma144=10, hist=.3),
            row(2, close=9, low=8, sma21=9, sma144=10, hist=.2, dow=(defense,)),
            row(3, close=9, low=8, sma21=10, sma144=9, hist=.1)]
    frozen, daily, events = run(rows, moving=True)
    assert daily[-2]["suppressed_stop_hit"] is True
    assert any(item["event"] == "DOW_DEFENSE_APPLIED_TO_LEG" and item["stop"] == Decimal("10") for item in events)
    assert frozen.transactions[-1]["exit_reason"] == "STRUCTURAL_STOP"


def test_exit_day_cannot_reenter_on_ma21_breakout():
    rows = [row(0, close=9, low=8, sma21=10, sma144=11, hist=0),
            row(1, close=9, low=8.5, sma21=10, sma144=11, hist=.2),
            row(2, close=11, low=9, sma21=10, sma144=9, hist=-.1)]
    frozen, daily, _events = run(rows)
    assert [item["role"] for item in frozen.transactions] == ["MOTHER", "EXIT"]
    assert daily[-1]["selected_entry_type"] == "MA21_BREAKOUT"
    assert daily[-1]["entry_status"] == "SKIPPED_SAME_DAY_EXIT"


def test_corporate_action_rebases_fixed_stop_and_never_adds_dow_events():
    rows = [row(0, close=9, low=8, sma21=10, sma144=10, hist=-1),
            row(1, close=11, low=9, sma21=10, sma144=10, hist=-.5),
            row(2, close=3, low=2.9, sma21=2.5, sma144=2.5, hist=-.1, factor=.25)]
    _frozen, _daily, events = run(rows)
    assert any(item["event"] == "CORPORATE_ACTION_REBASE" and item["factor"] == Decimal("0.25") for item in events)
    assert not any(str(item["event"]).startswith("DOW_") for item in events)
