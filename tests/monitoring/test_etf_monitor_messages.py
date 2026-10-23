"""Public ETF wording tests; internal strategy and audit fields stay unchanged."""

from __future__ import annotations

import runpy
from dataclasses import replace
from pathlib import Path

import pytest

from stock_ai_bot.monitoring.etf_monitor.messages import (
    render_core_message,
    render_data_unavailable,
    render_offline_test,
)
from stock_ai_bot.monitoring.etf_monitor.models import Decision, MonitorPhase


H = runpy.run_path(str(Path(__file__).with_name("test_etf_monitor_service.py")))
DAY = H["DAY"]

PUBLICLY_FORBIDDEN = (
    "final-v1.7",
    "BUY_NEXT_OPEN",
    "SELL_NEXT_OPEN",
    "HOLD",
    "DATA_UNAVAILABLE",
    "Regime",
    "armed",
    "snapshot",
    "action_id",
    "LONG",
    "FLAT",
    "fail-closed",
    "MA21",
    "MA144",
    "MACD",
)


def _assert_plain_chinese_public_body(body: str) -> None:
    for token in PUBLICLY_FORBIDDEN:
        assert token not in body
    assert "0050 趨勢策略" in body
    assert "（台北時間）" not in body
    assert "成交狀態：" not in body
    assert "實際成交價格以執行結果為準" not in body
    assert "提醒：" not in body
    assert "本訊息只通知策略訊號，不會自動下單" not in body


def test_entry_message_uses_public_name_and_does_not_invent_fill() -> None:
    body = render_core_message(
        phase=MonitorPhase.DECISION,
        core=H["_core"](Decision.BUY_NEXT_OPEN),
        snapshot=H["_snapshot"](),
    )
    _assert_plain_chinese_public_body(body)
    assert body.startswith("0050 趨勢策略｜觸發進場\n")
    assert "訊號時間：" in body
    assert "進場參考價：51.00 元" in body
    assert "初始防守價：48.00 元" in body
    assert "策略安排：下一交易日開盤進場" in body


@pytest.mark.parametrize(
    ("reason", "label"),
    [
        ("INITIAL_PROTECTIVE_STOP", "跌破固定初始防守價"),
        ("STRUCTURAL_STOP", "跌破策略防守價"),
        ("MACD_RED_TO_GREEN", "動能轉弱條件成立"),
    ],
)
def test_exit_message_translates_true_internal_reason(reason: str, label: str) -> None:
    core = replace(
        H["_core"](Decision.SELL_NEXT_OPEN),
        exit_reason=reason,
        signal_transaction={
            **H["_core"](Decision.SELL_NEXT_OPEN).signal_transaction,
            "exit_reason": reason,
        },
    )
    body = render_core_message(
        phase=MonitorPhase.DECISION,
        core=core,
        snapshot=H["_snapshot"](),
    )
    _assert_plain_chinese_public_body(body)
    assert body.startswith("0050 趨勢策略｜觸發出場\n")
    assert "出場參考價：51.00 元" in body
    assert f"出場原因：{label}" in body
    assert "策略安排：下一交易日開盤出場" in body
    assert reason not in body


def test_unmapped_exit_reason_fails_closed_instead_of_leaking_code() -> None:
    core = replace(
        H["_core"](Decision.SELL_NEXT_OPEN),
        exit_reason="UNMAPPED_INTERNAL_REASON",
    )
    with pytest.raises(ValueError, match="unmapped exit reason"):
        render_core_message(
            phase=MonitorPhase.DECISION,
            core=core,
            snapshot=H["_snapshot"](),
        )


@pytest.mark.parametrize(
    ("active", "title", "state"),
    [
        (True, "持有觀察", "目前持有，持續觀察"),
        (False, "空手等待", "目前空手，等待下一次正式訊號"),
    ],
)
def test_hold_wording_follows_actual_position(active, title, state) -> None:
    core = replace(
        H["_core"](Decision.HOLD),
        active_position=active,
        active_leg_id="內部持倉識別" if active else None,
        fixed_initial_stop=H["_core"](Decision.BUY_NEXT_OPEN).fixed_initial_stop if active else None,
    )
    body = render_core_message(
        phase=MonitorPhase.DECISION,
        core=core,
        snapshot=H["_snapshot"](),
    )
    _assert_plain_chinese_public_body(body)
    assert body.startswith(f"0050 趨勢策略｜{title}\n")
    assert f"策略狀態：{state}" in body
    assert "內部持倉識別" not in body


def test_preview_is_visibly_non_formal_and_has_no_execution_arrangement() -> None:
    body = render_core_message(
        phase=MonitorPhase.PREVIEW,
        core=H["_core"](Decision.BUY_NEXT_OPEN),
        snapshot=H["_snapshot"](),
    )
    _assert_plain_chinese_public_body(body)
    assert body.startswith("0050 趨勢策略｜盤中預覽\n")
    assert "盤中預覽（非正式訊號）" in body
    assert "進場條件暫時出現" in body
    assert "下一交易日開盤進場" not in body
    assert "觸發進場" not in body


def test_correction_is_plain_language_and_hides_internal_identifiers() -> None:
    transition = {
        "old_action": Decision.BUY_NEXT_OPEN.value,
        "new_action": Decision.HOLD.value,
        "outcome": "CANCELLED_BY_RECONCILIATION",
        "old_status": "PENDING_NEXT_SESSION_OPEN",
        "old_action_id": "do-not-show-action-id",
        "old_leg_id": "do-not-show-leg-id",
        "new_leg_id": None,
    }
    body = render_core_message(
        phase=MonitorPhase.RECONCILIATION,
        core=H["_core"](Decision.HOLD),
        snapshot=H["_snapshot"](complete=True),
        correction_of="do-not-show-event-id",
        pending_transition=transition,
    )
    _assert_plain_chinese_public_body(body)
    assert body.startswith("0050 趨勢策略｜訊號更正\n")
    assert "晚間完整資料確認" in body
    assert "收盤資料確認後，取消先前進場訊號，維持空手" in body
    assert "do-not-show" not in body
    assert "原紀錄保留於內部稽核" in body


def test_data_alert_is_chinese_and_never_emits_a_false_signal() -> None:
    body = render_data_unavailable(
        phase=MonitorPhase.DECISION,
        trading_date=DAY.isoformat(),
        reason_code="INTERNAL_TECHNICAL_REASON",
    )
    _assert_plain_chinese_public_body(body)
    assert body.startswith("0050 趨勢策略｜資料異常\n")
    assert "資料不足，暫停產生正式策略訊號" in body
    assert "INTERNAL_TECHNICAL_REASON" not in body
    assert "觸發進場" not in body
    assert "觸發出場" not in body


@pytest.mark.parametrize("decision", list(Decision))
def test_offline_test_renderer_is_public_chinese(decision: Decision) -> None:
    body = render_offline_test(decision)
    assert body.startswith("模擬測試｜非真實交易訊號\n")
    for token in PUBLICLY_FORBIDDEN:
        assert token not in body
