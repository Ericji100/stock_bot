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


def _snapshot(*, complete=False, day: date = DAY, raw_open: Decimal = Decimal("50")) -> MarketDataSnapshot:
    return MarketDataSnapshot(
        symbol="0050",
        as_of=day,
        raw_rows=({"date": day, "open": raw_open, "high": 53, "low": 49, "close": 51},),
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


class _RecoveringData:
    def __init__(self, failures: int):
        self.failures = failures

    async def load(self, phase, as_of):
        if self.failures:
            self.failures -= 1
            raise EtfMonitorDataError("official source temporarily unavailable")
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


def test_reconciliation_retry_after_partial_completion_keeps_one_pending(tmp_path) -> None:
    service, store = _service(
        tmp_path,
        [
            Decision.BUY_NEXT_OPEN,
            Decision.BUY_NEXT_OPEN,
            Decision.BUY_NEXT_OPEN,
        ],
    )
    decision = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    first = asyncio.run(
        service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22))
    )
    store.finish_job(
        MonitorPhase.RECONCILIATION,
        DAY,
        succeeded=False,
        error="SIMULATED_POST_STATE_DELIVERY_FAILURE",
    )
    retry = asyncio.run(
        service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22, minute=5))
    )
    active = [
        row for row in _pending(store.path)
        if row["status"] == "PENDING_NEXT_SESSION_OPEN"
    ]
    assert first.event_ids == retry.event_ids
    assert len(active) == 1
    assert active[0]["source_event_id"] == decision.event_ids[0]


@pytest.mark.parametrize(
    ("before", "after", "old_status", "active_action", "public_change"),
    [
        (
            Decision.BUY_NEXT_OPEN,
            Decision.HOLD,
            "CANCELLED_BY_RECONCILIATION",
            None,
            "取消先前進場訊號",
        ),
        (
            Decision.BUY_NEXT_OPEN,
            Decision.SELL_NEXT_OPEN,
            "SUPERSEDED_BY_RECONCILIATION",
            "SELL_NEXT_OPEN",
            "先前進場訊號改為出場訊號",
        ),
        (
            Decision.SELL_NEXT_OPEN,
            Decision.HOLD,
            "CANCELLED_BY_RECONCILIATION",
            None,
            "取消先前出場訊號",
        ),
    ],
)
def test_reconciliation_cancels_or_supersedes_pending_action(
    tmp_path, before, after, old_status, active_action, public_change
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
    assert public_change in body
    assert before.value not in body
    assert after.value not in body


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
    assert "更新先前進場訊號的參考資訊" in body
    assert "L1" not in body
    assert "L2" not in body
    assert "action_id" not in body


def test_confirmed_pending_action_settles_once_at_fixed_next_open_and_survives_restart(
    tmp_path,
) -> None:
    service, store = _service(
        tmp_path,
        [Decision.BUY_NEXT_OPEN, Decision.BUY_NEXT_OPEN],
    )
    asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    confirmation = asyncio.run(
        service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22))
    )
    fill_day = DAY.replace(day=22)
    pending = _pending(store.path)[0]
    assert pending["expected_fill_date"] == fill_day.isoformat()
    assert pending["reconciled_by_event_id"] == confirmation.event_ids[0]

    reopened = EtfMonitorStore(store.path)
    reopened.recover_after_restart()
    filled = reopened.settle_pending_actions_at_open(
        trading_date=fill_day,
        raw_open=Decimal("52.5"),
        snapshot_id="verified-next-session",
    )
    assert len(filled) == 1
    assert reopened.settle_pending_actions_at_open(
        trading_date=fill_day,
        raw_open=Decimal("52.5"),
        snapshot_id="verified-next-session",
    ) == ()
    row = _pending(reopened.path)[0]
    assert row["status"] == "FILLED_SHADOW_NEXT_OPEN"
    assert row["fill_date"] == "2026-10-22"
    assert Decimal(row["fill_price"]) == Decimal("52.5")
    assert row["fill_snapshot_id"] == "verified-next-session"


def test_cross_day_service_sequence_confirms_then_fills_at_next_raw_open(tmp_path) -> None:
    next_day = DAY.replace(day=22)

    class _DayData:
        async def load(self, phase, as_of):
            return _snapshot(
                complete=phase is MonitorPhase.RECONCILIATION,
                day=as_of,
                raw_open=Decimal("52.5") if as_of == next_day else Decimal("50"),
            )

    config = replace(load_etf_monitor_config(), enabled=True)
    store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
    service = EtfMonitorService(
        config=config,
        store=store,
        data_provider=_DayData(),
        router=None,
        core=_Core(
            [
                _core(Decision.BUY_NEXT_OPEN, day=DAY),
                _core(Decision.BUY_NEXT_OPEN, day=DAY),
                _core(Decision.HOLD, day=next_day),
                _core(Decision.HOLD, day=next_day),
            ]
        ),
        trading_day_predicate=lambda _: True,
    )
    asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    asyncio.run(service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22)))
    before_preview = _pending(store.path)
    before_state = store.latest_state()
    before_corporate_actions = _count(store.path, "corporate_actions")
    preview = asyncio.run(
        service.run(
            MonitorPhase.PREVIEW,
            now=NOW.replace(day=22, hour=12, minute=45),
        )
    )
    assert _pending(store.path) == before_preview
    assert store.latest_state() == before_state
    assert _count(store.path, "corporate_actions") == before_corporate_actions
    decision = asyncio.run(
        service.run(
            MonitorPhase.DECISION,
            now=NOW.replace(day=22, hour=13, minute=55),
        )
    )
    row = _pending(store.path)[0]
    assert preview.status == "COMPLETED"
    assert preview.metadata["shadow_next_open_fills"] == ()
    assert decision.status == "COMPLETED"
    assert row["status"] == "FILLED_SHADOW_NEXT_OPEN"
    assert row["fill_date"] == next_day.isoformat()
    assert Decimal(row["fill_price"]) == Decimal("52.5")
    assert len(decision.metadata["shadow_next_open_fills"]) == 1


def test_data_failure_is_fail_closed_and_records_health_event(tmp_path) -> None:
    service, store = _service(tmp_path, [], fail=True)
    outcome = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    assert outcome.decision is Decision.DATA_UNAVAILABLE
    assert store.latest_state() is None
    assert _count(store.path, "events") == 1


def test_detailed_failures_survive_changed_retries_and_legacy_alert(tmp_path) -> None:
    service, store = _service(tmp_path, [])
    first = asyncio.run(service._data_unavailable(
        phase=MonitorPhase.PREVIEW, trading_date=DAY,
        exc=EtfMonitorDataError("legacy failure"),
    ))
    with sqlite3.connect(store.path) as connection:
        # Simulate the unchanged alert already sent by the prior code version.
        connection.execute("DELETE FROM data_failures")
        original = connection.execute("SELECT payload_json FROM events").fetchone()[0]
    for price in ("-", "NaN"):
        outcome = asyncio.run(service._data_unavailable(
            phase=MonitorPhase.PREVIEW, trading_date=DAY,
            exc=EtfMonitorDataError(
                f"invalid actual trade: {price}", reason_code="MIS_INVALID_LAST_PRICE",
                details={"raw_last": price, "source_endpoint": "TWSE_MIS_OFFICIAL"},
            ),
        ))
        assert outcome.event_ids == first.event_ids
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute(
            "SELECT reason_code, message, details_json FROM data_failures ORDER BY failure_id"
        ).fetchall()
        assert len(rows) == 2
        assert [json.loads(row[2])["raw_last"] for row in rows] == ["-", "NaN"]
        assert all(row[0] == "MIS_INVALID_LAST_PRICE" for row in rows)
        assert connection.execute("SELECT payload_json FROM events").fetchone()[0] == original
        assert connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
        assert "MIS_INVALID_LAST_PRICE" not in connection.execute("SELECT body FROM outbox").fetchone()[0]


def test_failed_job_retains_concrete_error_text(tmp_path) -> None:
    service, store = _service(tmp_path, [], fail=True)
    asyncio.run(service.run(MonitorPhase.PREVIEW, now=NOW))
    with sqlite3.connect(store.path) as connection:
        assert "stale" in connection.execute("SELECT error FROM job_runs").fetchone()[0]
        assert connection.execute("SELECT message FROM data_failures").fetchone()[0] == "stale"


@pytest.mark.parametrize("legacy", [False, True])
def test_quote_clock_audit_is_durable_and_legacy_retries_remain_compatible(tmp_path, legacy) -> None:
    from datetime import timedelta
    from stock_ai_bot.monitoring.etf_monitor.models import IntradayQuote, canonical_json

    service, store = _service(tmp_path, [Decision.HOLD, Decision.HOLD])
    quote = IntradayQuote(
        symbol="0050", observed_at=NOW, trade_date=DAY,
        raw_open=Decimal("50"), raw_high=Decimal("53"), raw_low=Decimal("49"),
        raw_last=Decimal("51"), raw_volume=Decimal("1000"),
        source="TWSE_MIS_OFFICIAL", source_endpoint="https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
        receipt_sha256="c" * 64, source_timestamp=NOW,
        last_trade_at=NOW.replace(hour=13, minute=30), is_market_closed=True,
    )

    class Data:
        async def load(self, phase, as_of):
            return replace(_snapshot(), quote=quote)

    service.data_provider = Data()
    first = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    with sqlite3.connect(store.path) as connection:
        raw = connection.execute("SELECT payload_json FROM events").fetchone()[0]
        payload = json.loads(raw)
        assert payload["data"]["quote_evidence"]["source_timestamp"] == NOW.isoformat()
        if legacy:
            del payload["data"]["quote_evidence"]
            connection.execute("UPDATE events SET payload_json=?", (canonical_json(payload),))
        before = connection.execute("SELECT payload_json FROM events").fetchone()[0]
        body = connection.execute("SELECT body FROM outbox").fetchone()[0]
    store.finish_job(MonitorPhase.DECISION, DAY, succeeded=False, error="AFTER_EVENT_RETRY")
    quote = replace(quote, observed_at=NOW + timedelta(seconds=30), source_timestamp=NOW + timedelta(seconds=30))
    retry = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    assert retry.event_ids == first.event_ids
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT payload_json FROM events").fetchone()[0] == before
        assert connection.execute("SELECT body FROM outbox").fetchone()[0] == body
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_data_unavailable_is_idempotent_and_same_phase_can_recover(tmp_path) -> None:
    config = replace(load_etf_monitor_config(), enabled=True)
    store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
    service = EtfMonitorService(
        config=config,
        store=store,
        data_provider=_RecoveringData(failures=2),
        router=None,
        core=_Core([Decision.HOLD]),
        trading_day_predicate=lambda _: True,
    )
    first = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    second = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    recovered = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    duplicate = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    assert first.status == second.status == "DATA_UNAVAILABLE"
    assert recovered.status == "COMPLETED"
    assert duplicate.status == "ALREADY_COMPLETED"
    assert store.latest_state()["decision"] == "HOLD"
    assert _count(store.path, "events") == 2
    with sqlite3.connect(store.path) as connection:
        status, error = connection.execute(
            "SELECT status, error FROM job_runs WHERE run_key=?",
            (f"{MonitorPhase.DECISION.value}:{DAY.isoformat()}",),
        ).fetchone()
    assert status == "SUCCEEDED"
    assert error is None


def test_reconciliation_failure_quarantines_pending_until_successful_retry(
    tmp_path,
) -> None:
    class _ReconRecovery:
        def __init__(self):
            self.recon_calls = 0

        async def load(self, phase, as_of):
            if phase is MonitorPhase.RECONCILIATION:
                self.recon_calls += 1
                if self.recon_calls == 1:
                    raise EtfMonitorDataError("full-day delta missing")
            return _snapshot(complete=phase is MonitorPhase.RECONCILIATION)

    config = replace(load_etf_monitor_config(), enabled=True)
    store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
    service = EtfMonitorService(
        config=config,
        store=store,
        data_provider=_ReconRecovery(),
        router=None,
        core=_Core([Decision.BUY_NEXT_OPEN, Decision.BUY_NEXT_OPEN]),
        trading_day_predicate=lambda _: True,
    )
    decision = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    failed = asyncio.run(
        service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22))
    )
    rows = _pending(store.path)
    assert decision.status == "COMPLETED"
    assert failed.status == "DATA_UNAVAILABLE"
    assert len(rows) == 1
    assert rows[0]["status"] == "QUARANTINED_RECONCILIATION_REQUIRED"
    assert rows[0]["quarantined_by_event_id"] == failed.event_ids[0]
    assert store.settle_pending_actions_at_open(
        trading_date=DAY.replace(day=22),
        raw_open=Decimal("52.5"),
        snapshot_id="next-day-preview",
    ) == ()

    recovered = asyncio.run(
        service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22, minute=5))
    )
    rows = _pending(store.path)
    assert recovered.status == "COMPLETED"
    assert len(rows) == 1
    assert rows[0]["status"] == "PENDING_NEXT_SESSION_OPEN"
    assert rows[0]["reconciled_by_event_id"] == recovered.event_ids[0]
    assert rows[0]["fill_price"] is None
    with sqlite3.connect(store.path) as connection:
        body = connection.execute(
            "SELECT body FROM outbox WHERE event_id=?", (recovered.event_ids[0],)
        ).fetchone()[0]
    assert "資料恢復確認後，恢復先前進場訊號" in body
    assert "RESTORED_AFTER_RECONCILIATION" not in body


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
    assert outcome.status == "SKIPPED_OFFICIAL_NON_TRADING_DAY"
    assert store.latest_state() is None
    assert _count(store.path, "job_runs") == 1


def test_official_trading_day_gate_outage_is_retryable_data_unavailable(tmp_path) -> None:
    def unavailable(_day):
        raise EtfMonitorDataError("official calendar offline")

    config = replace(load_etf_monitor_config(), enabled=True)
    store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
    service = EtfMonitorService(
        config=config,
        store=store,
        data_provider=_Data(),
        router=None,
        core=_Core([]),
        trading_day_predicate=unavailable,
    )
    first = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    second = asyncio.run(service.run(MonitorPhase.DECISION, now=NOW))
    assert first.status == second.status == "DATA_UNAVAILABLE"
    assert first.event_ids == second.event_ids
    assert store.latest_state() is None
    assert _count(store.path, "events") == 1
    with sqlite3.connect(store.path) as connection:
        status = connection.execute(
            "SELECT status FROM job_runs WHERE run_key=?",
            (f"{MonitorPhase.DECISION.value}:{DAY.isoformat()}",),
        ).fetchone()[0]
    assert status == "FAILED"
