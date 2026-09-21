from __future__ import annotations

import pytest

from scripts.course_add_vs_no_add_backtest import Costs, MODELS, simulate_trade
from scripts.course_corporate_action_backtest import load_inputs
from scripts.course_corporate_action_data import RUN, read
from scripts.course_profit_protect_1r_backtest import SOURCE, _trade_input


def _case(code: str, variant: str = "HYBRID_RESONANCE"):
    source = read(SOURCE)
    row = next(
        trade for trade in source["results"][variant]["trades"]
        if str(trade["code"]) == code
    )
    manifest = read(RUN / "input_manifest.json")
    item = next(item for item in manifest["items"] if str(item["code"]) == code)
    raw, actions, _ = load_inputs(item)
    return source, row, raw, actions


def test_taiwan_ceramic_profit_protect_exits_causally_after_close_giveback():
    source, row, raw, actions = _case("3221")
    result = simulate_trade(
        trade=_trade_input(row), raw=raw, actions=actions,
        model=MODELS["ONE_SHOT"], costs=Costs(), as_of=source["as_of"],
        profit_protect_1r=True, profit_giveback_fraction=0.50,
    )

    assert result["profit_protect_activation_date"] == "2026-07-03"
    assert result["exit_trigger_date"] == "2026-07-06"
    assert result["exit_date"] == "2026-07-07"
    assert result["exit_reason"] == "PROFIT_PROTECT_1R_GIVEBACK"
    assert result["net_pnl"] == pytest.approx(174.44, abs=0.01)


def test_default_exit_replay_for_taiwan_ceramic_is_unchanged():
    source, row, raw, actions = _case("3221")
    result = simulate_trade(
        trade=_trade_input(row), raw=raw, actions=actions,
        model=MODELS["ONE_SHOT"], costs=Costs(), as_of=source["as_of"],
    )

    assert result["exit_date"] == row["exit_date"] == "2026-07-15"
    assert result["net_pnl"] == pytest.approx(row["net_pnl"], abs=1e-6)


def test_existing_runner_rule_remains_in_force_for_bairong():
    source, row, raw, actions = _case("2483", "SMALL_BASELINE")
    result = simulate_trade(
        trade=_trade_input(row), raw=raw, actions=actions,
        model=MODELS["ONE_SHOT"], costs=Costs(), as_of=source["as_of"],
        profit_protect_1r=True, profit_giveback_fraction=0.50,
    )

    assert result["profit_protect_activation_date"] == "2026-06-04"
    assert any("TREND_RUNNER" in event["state"] for event in result["state_events"])
    assert result["exit_reason"] == "DYNAMIC_PIVOT"
    assert result["exit_date"] == row["exit_date"]
    assert result["net_pnl"] == pytest.approx(row["net_pnl"], abs=1e-6)
