"""Condition explanations use accepted signal evidence, including after exits."""
from __future__ import annotations

import runpy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from stock_ai_bot.monitoring.etf_monitor.messages import render_core_message
from stock_ai_bot.monitoring.etf_monitor.models import (
    Decision, MonitorPhase, accepted_core_payload, canonical_json,
)

H = runpy.run_path(str(Path(__file__).with_name("test_etf_monitor_service.py")))


def state(case):
    buy = case.startswith("entry")
    core = H["_core"](Decision.BUY_NEXT_OPEN if buy else Decision.SELL_NEXT_OPEN)
    low = case.endswith("low")
    regime = "SMA21_LT_SMA144" if low else "SMA21_GE_SMA144"
    reason = None if buy else "INITIAL_PROTECTIVE_STOP" if case == "stop" else "MACD_RED_TO_GREEN"
    return replace(
        core, regime=regime, sma144=Decimal("52") if low else Decimal("49"),
        raw_close=Decimal("47") if case == "stop" else Decimal("51"),
        histogram=Decimal("0.125") if buy else Decimal("-0.125"),
        previous_histogram=Decimal("-0.25") if buy else Decimal("0.25"),
        exit_reason=reason,
        signal_transaction={**core.signal_transaction, "exit_reason": reason},
        core_daily_row={"selected_entry_type": "MACD_GREEN_TO_RED" if low else "MA21_BREAKOUT"},
        core_events=() if buy else (
            {"date": core.as_of, "leg_id": "another-leg", "event": "INITIAL_STOP_EXIT", "stop": "999"},
            {"date": core.as_of.replace(day=core.as_of.day - 1), "leg_id": "L1", "event": "INITIAL_STOP_EXIT", "stop": "888"},
            {"date": core.as_of, "leg_id": "L1", "event": "INITIAL_STOP_EXIT" if case == "stop" else "VALID_RED_TO_GREEN_EXIT", "stop": "48"},
        ),
    )


@pytest.mark.parametrize("phase", [MonitorPhase.PREVIEW, MonitorPhase.DECISION, MonitorPhase.RECONCILIATION])
@pytest.mark.parametrize("case", ["entry-high", "entry-low", "stop", "macd-high", "macd-low"])
def test_all_active_conditions_show_actual_values_without_mutating_core(case, phase):
    core = state(case)
    before = canonical_json(accepted_core_payload(core))
    body = render_core_message(phase=phase, core=core, snapshot=H["_snapshot"]())
    assert "MA21 50.0000 元" in body
    assert ("MA144 52.0000 元" if case.endswith("low") else "MA144 49.0000 元") in body
    assert ("暫時符合" if phase is MonitorPhase.PREVIEW else "收盤確認") in body
    if case == "entry-high":
        assert "向上突破 MA21" in body
        assert "前一交易日價格 ≤ 前一交易日 MA21" in body
        assert "51.00 元 > MA21 50.0000 元" in body
    elif case == "entry-low":
        assert "綠翻紅" in body
        assert "-0.250000（≤ 0） → 本次 +0.125000（> 0）" in body
        assert "突破 MA21" not in body
    elif case == "stop":
        # The post-exit position has no stop. The exit event owns the correct value.
        assert core.fixed_initial_stop is None
        assert "47.00 元 < 固定初始防守價 48.0000 元" in body
        assert "999" not in body and "888" not in body
    else:
        assert "紅翻綠" in body
        assert "+0.250000（≥ 0） → 本次 -0.125000（< 0）" in body
        assert "自進場起曾出現柱狀值 ≥ 0" in body
        assert "停利" not in body
        assert ("不執行價格防守出場" in body) == case.endswith("low")
    if phase is MonitorPhase.PREVIEW:
        assert "須待收盤後正式決策確認" in body
        assert "下一交易日開盤進場" not in body and "下一交易日開盤出場" not in body
    assert canonical_json(accepted_core_payload(core)) == before


def test_same_day_stop_priority_and_missing_evidence_do_not_invent_a_price():
    core = state("stop")
    core = replace(core, core_events=({**core.core_events[-1], "event": "STOP_PRIORITY_OVER_MACD"},))
    assert "防守價 48.0000 元" in render_core_message(phase=MonitorPhase.DECISION, core=core, snapshot=H["_snapshot"]())
    core = replace(core, core_events=())
    assert "防守價 — 元" in render_core_message(phase=MonitorPhase.DECISION, core=core, snapshot=H["_snapshot"]())


def test_tiny_crossing_does_not_round_into_zero():
    core = replace(state("entry-low"), histogram=Decimal("0.0000000001"), previous_histogram=Decimal("-0.0000000001"))
    body = render_core_message(phase=MonitorPhase.DECISION, core=core, snapshot=H["_snapshot"]())
    assert "-1.000E-10" in body and "+1.000E-10" in body


def test_unknown_entry_type_is_not_replaced_with_an_invented_condition():
    core = replace(state("entry-high"), core_daily_row={"selected_entry_type": "UNKNOWN"})
    with pytest.raises(ValueError, match="unmapped entry type"):
        render_core_message(phase=MonitorPhase.DECISION, core=core, snapshot=H["_snapshot"]())
