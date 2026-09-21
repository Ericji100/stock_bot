from copy import deepcopy

import pandas as pd
import pytest

from scripts.course_state_switch_backtest import Costs, fixed_notional, simulate_state


def frame(rows):
    return pd.DataFrame([
        {"date": pd.Timestamp(f"2026-06-{i+1:02}"), "open": o, "high": h,
         "low": l, "close": c, "MA21": ma, "volume": 100.0,
         "VOL_MA20": 100.0, "ATR14": 1.0}
        for i, (o, h, l, c, ma) in enumerate(rows)
    ])


def trade():
    return {"trade_id": "test", "code": "1234", "name": "測試",
            "entry_date": "2026-06-02", "armed_date": "2026-06-01",
            "entry_price": 10.0, "defense": 8.0, "initial_risk_pct": 20.0,
            "mfe_pct": 50.0, "max_favorable_price": 15.0}


def test_half_only_once_next_open_then_runner_no_rebuy():
    f = frame([(9, 10, 8.5, 9.5, 9), (10, 11, 9, 9.6, 9.7),
               (9.7, 10.8, 9.4, 10.5, 10), (10.6, 11, 9.2, 9.5, 10),
               (10, 14.2, 9.8, 13.5, 10), (13.5, 14, 10.2, 11, 10)])
    r = simulate_state(f, trade(), "STATE_HALF", {})
    assert len(r["partial_exits"]) == 1
    assert r["partial_exits"][0]["signal_date"] == "2026-06-02"
    assert r["partial_exits"][0]["date"] == "2026-06-03"
    assert r["partial_exits"][0]["price"] == 9.7
    assert r["remaining_fraction"] == 0.5
    assert r["activation_date"] == "2026-06-05"
    assert r["performance_return_pct"] == 3.5  # -.03/2 + .10/2


def test_wait_mode_holds_when_only_ma21_fails():
    f = frame([(9, 10, 8.5, 9.5, 9), (10, 11, 9, 9.6, 9.7),
               (9.7, 10.8, 9.4, 10.5, 10)])
    r = simulate_state(f, trade(), "STATE_WAIT", {})
    assert r["partial_exits"] == []
    assert r["status"] == "OPEN"
    assert any(e["state"] == "EARLY_WEAKNESS" for e in r["state_events"])


def test_early_structure_full_exit_takes_precedence_over_half():
    f = frame([(9, 10, 9.2, 9.5, 9), (10, 11, 8.7, 9.0, 9.5),
               (8.8, 9.3, 8.5, 9, 9.4)])
    r = simulate_state(f, trade(), "STATE_HALF", {})
    assert r["exit_reason"] == "EARLY_STRUCTURE"
    assert r["exit_trigger_date"] == "2026-06-02"
    assert r["exit_date"] == "2026-06-03"
    assert r["exit_price"] == 8.8
    assert not r["partial_exits"]


def test_initial_defense_always_exits_even_above_ma21():
    f = frame([(9, 10, 8.5, 9.5, 9), (10, 11, 7.5, 7.8, 7.7),
               (7.4, 8.0, 7, 7.8, 7.7)])
    r = simulate_state(f, trade(), "STATE_WAIT", {})
    assert r["exit_reason"] == "FIXED_DEFENSE"
    assert r["exit_price"] == 7.4


def test_setup_low_is_an_explicit_proxy_not_the_same_as_a_confirmed_pivot():
    f = frame([(9.3, 10, 9.2, 9.5, 9), (10, 11, 8.7, 9.0, 9.5),
               (8.8, 9.3, 8.5, 9, 9.4)])
    literal = simulate_state(f, trade(), "STATE_WAIT", {})
    pivot_only = simulate_state(f, trade(), "STATE_WAIT", {}, use_setup_low=False)
    assert literal["exit_reason"] == "EARLY_STRUCTURE"
    assert pivot_only["status"] == "OPEN"


def test_touch_two_r_applies_cost_defense_same_close():
    f = frame([(9, 10, 8.5, 9.5, 9), (10, 14.1, 9.3, 9.8, 9),
               (9.5, 10.2, 9.2, 10, 9)])
    r = simulate_state(f, trade(), "STATE_WAIT", {})
    assert r["activation_date"] == "2026-06-02"
    assert r["exit_trigger_date"] == "2026-06-02"
    assert r["exit_reason"] == "DYNAMIC_DEFENSE"


def test_runner_pivots_only_raise_and_do_not_disappear_after_break():
    f = frame([(9, 10, 8.5, 9.5, 9), (10, 14.1, 10, 14, 10),
               (13.9, 14.2, 12.5, 13, 10), (12.5, 13, 11, 11.8, 10),
               (11.5, 12.2, 11, 12, 10)])
    p = {1: {"price": 12, "confirmed_date": "2026-06-02"},
         2: {"price": 11, "confirmed_date": "2026-06-03"}, 3: None}
    r = simulate_state(f, trade(), "STATE_WAIT", p)
    assert r["daily_audit"][0]["dynamic_defense"] == 10
    assert r["daily_audit"][1]["dynamic_defense"] == 12
    assert r["daily_audit"][2]["dynamic_defense"] == 12
    assert r["exit_reason"] == "DYNAMIC_DEFENSE"
    assert r["exit_date"] == "2026-06-05"


def test_two_runner_closes_below_ma21_required():
    f = frame([(9, 10, 8.5, 9.5, 9), (10, 14, 10, 13.5, 14),
               (13.5, 14, 13, 13.2, 14), (13, 14, 12, 13.4, 14)])
    r = simulate_state(f, trade(), "STATE_HALF", {})
    assert r["exit_trigger_date"] == "2026-06-03"
    assert r["exit_reason"] == "MA21_TWO_CLOSES"
    assert not r["partial_exits"]


def test_volume_warning_has_no_sale_and_prefix_is_causal():
    f = frame([(9, 10, 8.5, 9.5, 9), (10, 14, 10, 13, 10),
               (13, 16, 12, 13, 10), (13, 15, 12, 14, 11)])
    f.loc[2, "volume"] = 400
    short = simulate_state(f.iloc[:3], trade(), "STATE_HALF", {})
    long = simulate_state(f, trade(), "STATE_HALF", {})
    assert any(e["state"] == "EXIT_WARNING" for e in long["state_events"])
    assert not long["partial_exits"]
    assert short["daily_audit"] == long["daily_audit"][:2]


def test_last_bar_exit_and_half_remain_unfilled():
    f = frame([(9, 10, 8.5, 9.5, 9), (10, 11, 9, 9.6, 10)])
    r = simulate_state(f, trade(), "STATE_HALF", {})
    assert r["pending_half_signal_date"] == "2026-06-02"
    assert not r["partial_exits"]
    assert r["remaining_fraction"] == 1
    f.loc[1, "close"] = 7.9
    r = simulate_state(f, trade(), "STATE_HALF", {})
    assert r["status"] == "EXIT_TRIGGERED"
    assert r["exit_date"] is None


def test_fixed_cash_integer_shares_accounting_and_no_input_mutation():
    f = frame([(9, 10, 8.5, 9.5, 9), (10, 11, 9, 9.6, 10),
               (9.7, 11, 9, 10.5, 10), (10.6, 11, 7, 7.5, 9),
               (7.4, 8, 7, 7.5, 9)])
    r = simulate_state(f, trade(), "STATE_HALF", {})
    original = deepcopy(r)
    fees = Costs(commission=0, tax=0)
    result = fixed_notional([r], {"1234": f}, "2026-06-01", "2026-06-05", fees, budget=105)
    t = result["trades"][0]
    assert t["quantity"] == 10
    assert t["fills"][1]["shares"] == 5
    assert t["net_pnl"] == pytest.approx(5*9.7+5*7.4-100)
    assert result["summary"]["minimum_recycled_funding"] == 100
    assert result["summary"]["final_equity"] == pytest.approx(500000 + t["net_pnl"])
    assert r == original
    again = fixed_notional([r], {"1234": f}, "2026-06-01", "2026-06-05", fees, budget=105)
    assert result == again


def test_cost_minimum_and_slippage():
    c = Costs(minimum_fee=20, slippage=0.001)
    assert c.buy(100, 10) == pytest.approx(1021)
    assert c.sell(100, 10) == pytest.approx(999-20-999*.003)
