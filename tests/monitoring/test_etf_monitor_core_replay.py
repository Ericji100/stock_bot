from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backtests.dual_ma import etf_0050_ma21_ma144_regime_v1 as accepted
from backtests.dual_ma.models import Bar

from stock_ai_bot.monitoring.etf_monitor.core import (
    ACCEPTED_START_DATE,
    RESEARCH_SLIPPAGE_BPS,
    AcceptedFinalV17Core,
)
from stock_ai_bot.monitoring.etf_monitor.messages import render_core_message
from stock_ai_bot.monitoring.etf_monitor.models import Decision, MarketDataSnapshot, MonitorPhase
from stock_ai_bot.monitoring.etf_monitor.replay import verify_final_v17_artifacts
from stock_ai_bot.monitoring.etf_monitor.replay import run_full_live_replay_parity


_FORMAL_ROOT_TEXT = os.environ.get("STOCK_AI_BOT_ETF_FROZEN_MARKET_DATA_ROOT", "")
FORMAL_MARKET_DATA_ROOT = Path(_FORMAL_ROOT_TEXT) if _FORMAL_ROOT_TEXT else Path("missing")


def _synthetic_rows(count: int = 430) -> tuple[dict, ...]:
    rows = []
    day = date(2005, 1, 3)
    index = 0
    while len(rows) < count:
        if day.weekday() < 5:
            close = Decimal(str(round(50 + index * 0.025 + math.sin(index / 9) * 2.4, 4)))
            rows.append(
                {
                    "date": day,
                    "open": close - Decimal("0.20"),
                    "high": close + Decimal("0.50"),
                    "low": close - Decimal("0.60"),
                    "close": close,
                    "volume": 1_000_000 + index,
                }
            )
            index += 1
        day += timedelta(days=1)
    return tuple(rows)


def _snapshot(rows: tuple[dict, ...]) -> MarketDataSnapshot:
    return MarketDataSnapshot(
        symbol="0050",
        as_of=rows[-1]["date"],
        raw_rows=rows,
        corporate_actions=(),
        source="TEST_ACCEPTED_RAW",
        price_basis="UNADJUSTED_MAINBOARD_OHLCV",
        snapshot_id="test-snapshot",
        snapshot_version="1",
        snapshot_sha256="a" * 64,
        fetched_at=datetime.now(tz=ZoneInfo("Asia/Taipei")),
        is_complete=True,
    )


def test_adapter_is_an_exact_thin_wrapper_around_accepted_core() -> None:
    rows = _synthetic_rows()
    snapshot = _snapshot(rows)
    state = AcceptedFinalV17Core().evaluate(snapshot)

    indicators = accepted.build_indicator_rows(rows, ())
    raw = {
        row["date"]: Bar.from_values(
            row["date"], row["open"], row["high"], row["low"], row["close"]
        )
        for row in rows
    }
    run, daily, events = accepted.build_regime_run(
        indicator_rows=indicators,
        raw_by_date=raw,
        strategy_id=accepted.FIXED_ID,
        moving_stop=False,
        start=ACCEPTED_START_DATE,
        end=snapshot.as_of,
        slippage_bps=RESEARCH_SLIPPAGE_BPS,
    )

    assert state.strategy == accepted.FIXED_ID
    assert state.core_daily_row == daily[-1]
    assert state.core_transactions == tuple(run.transactions)
    assert state.core_events == tuple(events)
    today = [row for row in run.transactions if row["date"] == snapshot.as_of]
    expected = (
        Decision.HOLD
        if not today
        else Decision.BUY_NEXT_OPEN
        if today[0]["role"] == "MOTHER"
        else Decision.SELL_NEXT_OPEN
    )
    assert state.decision is expected


def test_accepted_final_v17_artifacts_remain_exact() -> None:
    result = verify_final_v17_artifacts()
    assert result["status"] == "PASS_EXACT"
    assert result["row_counts"] == {
        "next-session-open-daily-equity.csv": 5093,
        "next-session-open-transactions.csv": 181,
        "next-session-open-corporate-actions.csv": 63,
        "next-session-open-trade-details.csv": 91,
    }


@pytest.mark.skipif(
    not _FORMAL_ROOT_TEXT or not FORMAL_MARKET_DATA_ROOT.is_dir(),
    reason="formal frozen ETF registry is not installed in this checkout",
)
def test_formal_full_history_live_replay_exact_parity() -> None:
    result = run_full_live_replay_parity(market_data_root=FORMAL_MARKET_DATA_ROOT)
    assert result["status"] == "PASS_EXACT"
    assert result["live_adapter_daily_rows"] == 5093
    assert result["signal_rows"] == 183
    assert result["total_mismatch_count"] == 0
    lifecycle = result["service_lifecycle"]
    assert lifecycle["service_days"] == 5093
    assert lifecycle["counts"]["events"] == 15279
    assert lifecycle["counts"]["job_runs"] == 15279
    assert lifecycle["pending_schedule_mismatch_count"] == 0
    assert lifecycle["state_mismatch_count"] == 0
    assert lifecycle["restart_mismatch_count"] == 0
    assert lifecycle["future_rows_exposed"] == 0
    assert lifecycle["telegram_send_attempts"] == 0
    assert result["live_adapter_daily_state_sha256"] == (
        "e8ab3587332175596c1381193ff0b29ea97a53a2d3d4cdc5e56e4c4a9310101f"
    )


def test_next_open_message_never_invents_unknown_fill_price() -> None:
    state = AcceptedFinalV17Core().evaluate(_snapshot(_synthetic_rows()))
    state = state.__class__(**{**state.__dict__, "decision": Decision.BUY_NEXT_OPEN})
    message = render_core_message(
        phase=MonitorPhase.DECISION,
        core=state,
        snapshot=_snapshot(_synthetic_rows()),
    )
    assert "下一交易日開盤進場" in message
    assert "進場參考價" in message
    assert "（台北時間）" not in message
    assert "成交狀態：" not in message
    assert "提醒：" not in message
