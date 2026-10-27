"""Public ETF wording tests; internal strategy and audit fields stay unchanged."""

from __future__ import annotations

import runpy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_ai_bot.monitoring.etf_monitor.messages import (
    render_core_message,
    render_data_unavailable,
    render_offline_test,
)
from stock_ai_bot.monitoring.etf_monitor.models import Decision, IntradayQuote, MonitorPhase


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


@pytest.mark.parametrize("phase,label", [
    (MonitorPhase.PREVIEW, "盤中現價"),
    (MonitorPhase.DECISION, "收盤價"),
])
def test_quote_prices_dates_and_clocks_are_public_without_changing_action(phase, label) -> None:
    tz = ZoneInfo("Asia/Taipei")
    source_time = datetime(2026, 10, 21, 13, 54, 58, tzinfo=tz)
    trade_time = source_time.replace(hour=13, minute=30, second=0)
    quote = IntradayQuote(
        symbol="0050", observed_at=source_time, trade_date=DAY,
        raw_open=Decimal("50"), raw_high=Decimal("52"), raw_low=Decimal("49"),
        raw_last=Decimal("51"), raw_volume=Decimal("1000"),
        source="TWSE_MIS_OFFICIAL", source_endpoint="https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
        receipt_sha256="c" * 64, source_timestamp=source_time, last_trade_at=trade_time,
        is_market_closed=phase is MonitorPhase.DECISION,
    )
    snapshot = replace(H["_snapshot"](), quote=quote, source="HASH_VERIFIED_REGISTRY_PLUS_TWSE_MIS")
    # Public prices are raw execution-basis data, never adjusted indicator prices.
    core = replace(H["_core"](Decision.HOLD), technical_close=Decimal("999"))
    body = render_core_message(phase=phase, core=core, snapshot=snapshot)
    _assert_plain_chinese_public_body(body)
    assert f"{label}：51.00 元" in body
    assert "999" not in body
    kind = "盤中" if phase is MonitorPhase.PREVIEW else "收盤"
    assert f"價格時間：2026-10-21 13:30（{kind}）" in body
    assert "資料日期：" not in body and "成交時間：" not in body
    assert "行情更新：" not in body and "資料取得：" not in body
    assert "13:54:58" not in body
    assert "資料來源：臺灣證券交易所（官方即時行情）" in body
    if phase is MonitorPhase.DECISION:
        assert "策略狀態：目前空手，等待下一次正式訊號" in body
        assert "策略安排：本次沒有新增進出場安排" in body


def test_complete_official_daily_message_has_no_invented_trade_clock() -> None:
    snapshot = replace(H["_snapshot"](complete=True), source="HASH_VERIFIED_ETF_SNAPSHOT_REGISTRY")
    snapshot = replace(snapshot, raw_rows=tuple({**row, "source": "TWSE_OFFICIAL_DAILY_DELTA"} for row in snapshot.raw_rows))
    body = render_core_message(phase=MonitorPhase.RECONCILIATION, core=H["_core"](Decision.HOLD), snapshot=snapshot)
    assert "收盤價：51.00 元" in body
    assert "價格日期：2026-10-21" in body
    assert "資料來源：臺灣證券交易所（官方日行情）" in body
    assert "資料取得：" not in body
    assert "價格時間：" not in body
    assert "成交時間：" not in body


def test_unknown_source_and_missing_clock_are_not_invented() -> None:
    body = render_core_message(phase=MonitorPhase.DECISION, core=H["_core"](Decision.HOLD), snapshot=H["_snapshot"]())
    assert "資料來源：已驗證策略行情" in body
    assert "臺灣證券交易所" not in body
    assert "行情更新：" not in body and "成交時間：" not in body


def test_preview_failure_is_scoped_to_this_preview_and_has_no_fake_price() -> None:
    body = render_data_unavailable(phase=MonitorPhase.PREVIEW, trading_date=DAY.isoformat(), reason_code="PRIVATE")
    _assert_plain_chinese_public_body(body)
    assert "本次無法形成盤中預覽" in body
    assert "暫停產生正式策略訊號" not in body
    assert "現價：無可用報價" in body
    assert "價格時間：未能驗證" in body
    assert "預定資料來源：臺灣證券交易所（官方資料）" in body
    assert "PRIVATE" not in body


def test_unavailable_core_never_displays_an_unverified_price() -> None:
    core = replace(H["_core"](Decision.HOLD), decision=Decision.DATA_UNAVAILABLE, raw_close=Decimal("999"))
    body = render_core_message(phase=MonitorPhase.PREVIEW, core=core, snapshot=H["_snapshot"]())
    assert "999" not in body
    assert "現價：無可用報價" in body


@pytest.mark.parametrize("decision", list(Decision))
def test_offline_test_renderer_is_public_chinese(decision: Decision) -> None:
    body = render_offline_test(decision)
    assert body.startswith("模擬測試｜非真實交易訊號\n")
    for token in PUBLICLY_FORBIDDEN:
        assert token not in body
