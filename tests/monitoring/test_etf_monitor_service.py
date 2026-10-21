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


def _core(
    decision: Decision,
    *,
    day: date = DAY,
    leg_id: str = "L1",
    reference_price: Decimal = Decimal("51"),
) -> AcceptedCoreState:
    transaction = None
    active = False
    leg = None
    if decision is Decision.BUY_NEXT_OPEN:
        transaction = {
            "date": day,
            "role": "MOTHER",
            "leg_id": leg_id,
            "reference_price": reference_price,
            "initial_stop": Decimal("48"),
        }
        active, leg = True, leg_id
    elif decision is Decision.SELL_NEXT_OPEN:
        transaction = {
            "date": day,
            "role": "EXIT",
            "leg_id": leg_id,
            "reference_price": reference_price,
            "exit_reason": "INITIAL_PROTECTIVE_STOP",
        }
    return AcceptedCoreState(
        symbol="0050",
        strategy="MA21_MA144_REGIME_FIXED_INITIAL_STOP_MACD_21_55_55",
        as_of=day,
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
        signal_reference_price=None if transaction is None else reference_price,
        signal_transaction=transaction,
        core_daily_row={"date": day, "regime": "SMA21_GE_SMA144"},
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
        value = next(self.decisions)
        return value if isinstance(value, AcceptedCoreState) else _core(value)


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


def _pending(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            "SELECT * FROM pending_actions ORDER BY created_at, action_id"
        )]


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


def test_reconciliation_confirmation_does_not_duplicate_same_pending_action(tmp_path) -> None:
    service, store = _service(
        tmp_path,
        [Decision.BUY_NEXT_OPEN, Decision.BUY_NEXT_OPEN],
    )
    decision = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    outcome = asyncio.run(
        service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22))
    )
    assert outcome.status == "COMPLETED"
    active = [
        row for row in _pending(store.path)
        if row["status"] == "PENDING_NEXT_SESSION_OPEN"
    ]
    assert len(active) == 1
    assert active[0]["source_event_id"] == decision.event_ids[0]


@pytest.mark.parametrize(
    ("before", "after", "old_status", "active_action"),
    [
        (Decision.BUY_NEXT_OPEN, Decision.HOLD, "CANCELLED_BY_RECONCILIATION", None),
        (
            Decision.BUY_NEXT_OPEN,
            Decision.SELL_NEXT_OPEN,
            "SUPERSEDED_BY_RECONCILIATION",
            "SELL_NEXT_OPEN",
        ),
        (Decision.SELL_NEXT_OPEN, Decision.HOLD, "CANCELLED_BY_RECONCILIATION", None),
    ],
)
def test_reconciliation_cancels_or_supersedes_pending_action(
    tmp_path, before, after, old_status, active_action
) -> None:
    service, store = _service(tmp_path, [before, after])
    asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    asyncio.run(service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22)))
    rows = _pending(store.path)
    assert rows[0]["status"] == old_status
    assert rows[0]["superseded_by_event_id"]
    active = [row for row in rows if row["status"] == "PENDING_NEXT_SESSION_OPEN"]
    assert (active[0]["action"] if active else None) == active_action
    with sqlite3.connect(store.path) as connection:
        body = connection.execute(
            "SELECT body FROM outbox ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
    assert f"{before.value} → {after.value}" in body


def test_pending_supersession_survives_restart(tmp_path) -> None:
    service, store = _service(
        tmp_path, [Decision.BUY_NEXT_OPEN, Decision.SELL_NEXT_OPEN]
    )
    asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    asyncio.run(service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22)))
    reopened = EtfMonitorStore(store.path)
    reopened.recover_after_restart()
    rows = _pending(reopened.path)
    assert [row["status"] for row in rows] == [
        "SUPERSEDED_BY_RECONCILIATION",
        "PENDING_NEXT_SESSION_OPEN",
    ]


def test_reconciliation_atomically_replaces_same_direction_corrected_details(tmp_path) -> None:
    service, store = _service(
        tmp_path,
        [
            _core(Decision.BUY_NEXT_OPEN, leg_id="L1", reference_price=Decimal("51")),
            _core(Decision.BUY_NEXT_OPEN, leg_id="L2", reference_price=Decimal("52")),
        ],
    )
    asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    correction = asyncio.run(
        service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22))
    )
    rows = _pending(store.path)
    assert correction.status == "CORRECTED"
    assert len(rows) == 2
    assert rows[0]["status"] == "SUPERSEDED_BY_RECONCILIATION"
    active = [row for row in rows if row["status"] == "PENDING_NEXT_SESSION_OPEN"]
    assert len(active) == 1
    assert active[0]["leg_id"] == "L2"
    assert Decimal(active[0]["signal_reference_price"]) == Decimal("52")
    assert active[0]["source_event_id"] == correction.event_ids[0]
    with sqlite3.connect(store.path) as connection:
        body = connection.execute(
            "SELECT body FROM outbox WHERE event_id=?", (correction.event_ids[0],)
        ).fetchone()[0]
    assert "leg=L1 → L2" in body
    assert "參考價=51 → 52" in body


def test_pending_action_settles_once_at_next_observed_raw_open_and_survives_restart(
    tmp_path,
) -> None:
    store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
    core = _core(Decision.BUY_NEXT_OPEN)
    store.save_pending_action(core, event_id="decision-event")
    fill_day = DAY.replace(day=22)
    filled = store.settle_pending_actions_at_open(
        trading_date=fill_day,
        raw_open=Decimal("52.5"),
        snapshot_id="next-session-preview",
    )
    assert len(filled) == 1
    assert store.settle_pending_actions_at_open(
        trading_date=fill_day,
        raw_open=Decimal("52.5"),
        snapshot_id="next-session-preview",
    ) == ()
    reopened = EtfMonitorStore(store.path)
    row = _pending(reopened.path)[0]
    assert row["status"] == "FILLED_SHADOW_NEXT_OPEN"
    assert row["fill_date"] == "2026-10-22"
    assert Decimal(row["fill_price"]) == Decimal("52.5")
    assert row["fill_snapshot_id"] == "next-session-preview"


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
