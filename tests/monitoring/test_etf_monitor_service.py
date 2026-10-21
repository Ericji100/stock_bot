from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.errors import EtfMonitorDataError
from stock_ai_bot.monitoring.etf_monitor.models import (
    AcceptedCoreState,
    Decision,
    MarketDataSnapshot,
    MonitorPhase,
)
from stock_ai_bot.monitoring.etf_monitor.service import EtfMonitorService
from stock_ai_bot.monitoring.etf_monitor.store import EtfMonitorStore


DAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 13, 55, tzinfo=ZoneInfo("Asia/Taipei"))


def _snapshot(*, complete=False) -> MarketDataSnapshot:
    return MarketDataSnapshot(
        symbol="0050",
        as_of=DAY,
        raw_rows=({"date": DAY, "open": 50, "high": 52, "low": 49, "close": 51},),
        corporate_actions=(),
        source="TEST",
        price_basis="UNADJUSTED_MAINBOARD_OHLCV",
        snapshot_id="snapshot-final" if complete else "snapshot-preview",
        snapshot_version="1",
        snapshot_sha256=("b" if complete else "a") * 64,
        fetched_at=NOW,
        is_complete=complete,
        is_provisional=not complete,
    )


def _core(decision: Decision) -> AcceptedCoreState:
    transaction = None
    active = False
    leg = None
    if decision is Decision.BUY_NEXT_OPEN:
        transaction = {
            "date": DAY,
            "role": "MOTHER",
            "leg_id": "L1",
            "reference_price": Decimal("51"),
            "initial_stop": Decimal("48"),
        }
        active, leg = True, "L1"
    elif decision is Decision.SELL_NEXT_OPEN:
        transaction = {
            "date": DAY,
            "role": "EXIT",
            "leg_id": "L1",
            "reference_price": Decimal("51"),
            "exit_reason": "INITIAL_PROTECTIVE_STOP",
        }
    return AcceptedCoreState(
        symbol="0050",
        strategy="MA21_MA144_REGIME_FIXED_INITIAL_STOP_MACD_21_55_55",
        as_of=DAY,
        decision=decision,
        regime="SMA21_GE_SMA144",
        raw_close=Decimal("51"),
        technical_close=Decimal("51"),
        sma21=Decimal("50"),
        sma144=Decimal("49"),
        histogram=Decimal("0.1"),
        previous_histogram=Decimal("0.0"),
        active_position=active,
        active_leg_id=leg,
        fixed_initial_stop=Decimal("48") if active else None,
        macd_armed=active,
        exit_reason=None if transaction is None else transaction.get("exit_reason"),
        signal_reference_price=None if transaction is None else Decimal("51"),
        signal_transaction=transaction,
        core_daily_row={"date": DAY, "regime": "SMA21_GE_SMA144"},
        core_events=(),
        core_transactions=() if transaction is None else (transaction,),
    )


class _Data:
    def __init__(self, *, fail=False):
        self.fail = fail

    async def load(self, phase, as_of):
        if self.fail:
            raise EtfMonitorDataError("stale")
        return _snapshot(complete=phase is MonitorPhase.RECONCILIATION)


class _Core:
    def __init__(self, decisions):
        self.decisions = iter(decisions)

    def evaluate(self, snapshot):
        return _core(next(self.decisions))


def _service(tmp_path, decisions, *, fail=False, daily_status=True):
    config = replace(
        load_etf_monitor_config(),
        enabled=True,
        daily_shadow_status=daily_status,
    )
    store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
    service = EtfMonitorService(
        config=config,
        store=store,
        data_provider=_Data(fail=fail),
        router=None,
        core=_Core(decisions),
        trading_day_predicate=lambda _: True,
    )
    return service, store


def _count(path, table):
    with sqlite3.connect(path) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_preview_does_not_mutate_formal_strategy_state(tmp_path) -> None:
    service, store = _service(tmp_path, [Decision.HOLD])
    outcome = asyncio.run(service.run(MonitorPhase.PREVIEW, now=NOW))
    assert outcome.status == "COMPLETED"
    assert store.latest_state() is None
    assert _count(store.path, "pending_actions") == 0


def test_decision_persists_state_pending_action_and_is_idempotent(tmp_path) -> None:
    service, store = _service(tmp_path, [Decision.BUY_NEXT_OPEN])
    first = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    second = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    assert first.decision is Decision.BUY_NEXT_OPEN
    assert second.status == "ALREADY_COMPLETED"
    assert store.latest_state()["position_status"] == "LONG"
    persisted = json.loads(store.latest_state()["core_state_json"])["core"]
    assert "core_events" not in persisted
    assert persisted["core_event_count"] == 0
    assert _count(store.path, "pending_actions") == 1
    assert _count(store.path, "events") == 1


def test_reconciliation_appends_correction_without_overwriting_event(tmp_path) -> None:
    service, store = _service(tmp_path, [Decision.HOLD, Decision.BUY_NEXT_OPEN])
    decision = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    correction = asyncio.run(
        service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22))
    )
    assert correction.status == "CORRECTED"
    assert _count(store.path, "events") == 2
    assert _count(store.path, "corrections") == 1
    assert store.event(decision.event_ids[0]) is not None


def test_data_failure_is_fail_closed_and_records_health_event(tmp_path) -> None:
    service, store = _service(tmp_path, [], fail=True)
    outcome = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    assert outcome.decision is Decision.DATA_UNAVAILABLE
    assert store.latest_state() is None
    assert _count(store.path, "events") == 1


def test_hold_notification_can_be_disabled_without_disabling_state(tmp_path) -> None:
    service, store = _service(tmp_path, [Decision.HOLD], daily_status=False)
    asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    assert store.latest_state()["decision"] == "HOLD"
    assert _count(store.path, "outbox") == 0


def test_offline_cases_are_marked_and_never_enter_outbox(tmp_path) -> None:
    service, store = _service(tmp_path, [])
    ids = service.create_offline_test_events(trading_date=DAY)
    assert len(ids) == 5
    assert _count(store.path, "events") == 5
    assert _count(store.path, "outbox") == 0


def test_non_trading_day_skips_without_data_or_state(tmp_path) -> None:
    config = replace(load_etf_monitor_config(), enabled=True)
    store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
    service = EtfMonitorService(
        config=config,
        store=store,
        data_provider=_Data(),
        router=None,
        core=_Core([]),
        trading_day_predicate=lambda _: False,
    )
    outcome = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    assert outcome.status == "SKIPPED_NON_TRADING_DAY"
    assert store.latest_state() is None
    assert _count(store.path, "job_runs") == 0
