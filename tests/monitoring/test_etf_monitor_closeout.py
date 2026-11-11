from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from stock_ai_bot.monitoring.etf_monitor import execution, recovery
from stock_ai_bot.monitoring.etf_monitor.models import Decision, MonitorPhase, canonical_json
from stock_ai_bot.monitoring.etf_monitor.historical import reconcile_historical
from stock_ai_bot.monitoring.etf_monitor.store import EtfMonitorStore
from test_etf_monitor_startup_recovery import make_runtime
from test_etf_monitor_reconciliation_recovery import install_service
from test_etf_monitor_service import _core, _snapshot


TZ = ZoneInfo("Asia/Taipei")
DAY = date(2026, 11, 9)
OPEN = datetime(2026, 11, 10, 9, tzinfo=TZ)


@pytest.fixture(autouse=True)
def receipt_clock(monkeypatch):
    from stock_ai_bot.monitoring.etf_monitor import store
    monkeypatch.setattr(store, "_utc_now", lambda: datetime(2026, 11, 11, 16, tzinfo=TZ).isoformat())


def append_trade(store, *, phase=MonitorPhase.RECONCILIATION, legacy=False):
    payload = {"core": {"decision": Decision.BUY_NEXT_OPEN.value},
               "data": {"is_complete": phase is MonitorPhase.RECONCILIATION}}
    if not legacy:
        payload["execution_window"] = {"expected_open_at": OPEN.isoformat()}
    store.append_event_with_outbox(
        event_id="trade", phase=phase, trading_date=DAY, event_type="BUY_NEXT_OPEN", payload=payload,
        target_chat_id="-5527055845", body="0050 趨勢策略｜觸發進場\n策略安排：下一交易日開盤進場")


def assessment(store):
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute("SELECT * FROM execution_status WHERE event_id='trade'").fetchone())


@pytest.mark.parametrize("phase", [MonitorPhase.DECISION, MonitorPhase.RECONCILIATION])
@pytest.mark.parametrize("late", [False, True])
def test_trade_delivery_has_real_window_and_separate_shadow_semantics(tmp_path, monkeypatch, phase, late):
    _, state, bot = make_runtime(tmp_path, lambda day: None)
    append_trade(state.store, phase=phase)
    clock = OPEN.replace(hour=10 if late else 8)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.astimezone(tz)
    monkeypatch.setattr(execution, "datetime", Clock)
    asyncio.run(state.router.deliver("trade"))
    text = bot.sent[0]["text"]
    if late:
        assert "原定開盤價已無法據以執行" in text
        assert "策略安排：下一交易日開盤進場" not in text
        assert "影子模型補帳" in text
    else:
        assert "2026-11-10 09:00" in text
    assert assessment(state.store)["expected_open_at"] == OPEN.isoformat()
    # API acknowledgement uses the store's real UTC clock, not the mocked
    # pre-send clock. An acknowledgement after the opening is always late.
    assert assessment(state.store)["executable_status"] == "LATE_DELIVERY"
    assert asyncio.run(state.router.deliver("trade")) is False
    assert len(bot.sent) == 1


def test_actual_delivery_before_and_after_open_is_classified(tmp_path):
    store = EtfMonitorStore(tmp_path / "db.sqlite3")
    append_trade(store)
    store.claim_outbox("trade")
    execution.prepare_execution_message(store, "trade", now=OPEN.replace(hour=8))
    store.begin_job(MonitorPhase.RECONCILIATION, DAY)
    store.finish_job(MonitorPhase.RECONCILIATION, DAY, succeeded=True)
    with store.transaction() as connection:
        connection.execute("UPDATE outbox SET status='DELIVERED',delivered_at=?", (OPEN.replace(hour=8).isoformat(),))
    execution.refresh_execution_status(store, now=OPEN.replace(hour=8, minute=30))
    assert assessment(store)["executable_status"] == "CONFIRMED_BEFORE_OPEN"
    execution.refresh_execution_status(store, now=OPEN)
    assert assessment(store)["executable_status"] == "WINDOW_CLOSED"
    with store.transaction() as connection:
        connection.execute("UPDATE outbox SET delivered_at=?", (OPEN.isoformat(),))
    execution.refresh_execution_status(store, now=OPEN)
    assert assessment(store)["executable_status"] == "LATE_DELIVERY"


def test_unknown_legacy_and_manual_delivery_are_reviewed_without_invented_time(tmp_path):
    store = EtfMonitorStore(tmp_path / "db.sqlite3")
    append_trade(store, legacy=True)
    store.claim_outbox("trade")
    text = execution.prepare_execution_message(store, "trade", now=OPEN.replace(hour=8))
    assert "缺少證據" in text
    assert assessment(store)["expected_open_at"] is None
    store.mark_outbox_uncertain("trade", "unknown")
    execution.refresh_execution_status(store, now=OPEN.replace(hour=8))
    assert store.claim_outbox("trade") is None
    store.resolve_uncertain_delivery("trade", delivered=True, telegram_message_id="12")
    execution.refresh_execution_status(store)
    assert assessment(store)["executable_status"] == "LEGACY_WINDOW_UNKNOWN"


def test_retries_regenerate_time_warning_and_preserve_original_body(tmp_path):
    store = EtfMonitorStore(tmp_path / "db.sqlite3")
    append_trade(store)
    store.claim_outbox("trade")
    execution.prepare_execution_message(store, "trade", now=OPEN.replace(hour=8))
    store.mark_outbox_retry("trade", "route")
    store.claim_outbox("trade")
    text = execution.prepare_execution_message(store, "trade", now=OPEN.replace(hour=10))
    assert text.count("交易安排時間檢查") == 1
    assert "原定開盤價已無法據以執行" in text
    assert assessment(store)["original_body"].startswith("0050 趨勢策略｜觸發進場")


def test_correction_invalidates_original_instruction_status(tmp_path):
    store = EtfMonitorStore(tmp_path / "db.sqlite3")
    append_trade(store)
    store.append_event_with_outbox(event_id="cancel", phase=MonitorPhase.RECONCILIATION,
        trading_date=DAY, event_type="CORRECTION", payload={"core": {"decision": "HOLD"}},
        target_chat_id=None, body=None, supersedes_event_id="trade")
    execution.refresh_execution_status(store, now=OPEN.replace(hour=8))
    assert assessment(store)["executable_status"] == "SUPERSEDED"


def test_missing_run_is_discovered_after_downtime_and_reconciled_without_rollback(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 11, 16, tzinfo=TZ).astimezone(tz)
    monkeypatch.setattr(recovery, "datetime", Clock)
    async def scenario():
        _, state, bot = make_runtime(tmp_path, lambda day: None)
        state.startup_status = "READY"
        calls = install_service(state)
        # The skipped evening has no job at all, while a newer live state exists.
        await state.service.run(MonitorPhase.DECISION, now=datetime(2026, 11, 9, 13, 55, tzinfo=TZ))
        await state.service.run(MonitorPhase.DECISION, now=datetime(2026, 11, 10, 13, 55, tzinfo=TZ))
        await state.service.run(MonitorPhase.RECONCILIATION, now=datetime(2026, 11, 10, 22, tzinfo=TZ))
        before = state.store.latest_state()
        await recovery.reconciliation_recovery_sweep(state)
        after = state.store.latest_state()
        assert before == after
        assert state.store.job_status(MonitorPhase.RECONCILIATION, DAY) == "SUCCEEDED"
        assert state.store.job_status(MonitorPhase.RECONCILIATION, date(2026, 11, 11)) is None
        count = len(bot.sent)
        await recovery.reconciliation_recovery_sweep(state)
        assert len(bot.sent) == count
        assert "歷史晚間核對補充" in bot.sent[-1]["text"]
        assert calls[-1] == DAY
    asyncio.run(scenario())


@pytest.mark.parametrize("decision", [Decision.BUY_NEXT_OPEN, Decision.SELL_NEXT_OPEN])
def test_historical_trade_requires_review_and_preserves_newer_state(tmp_path, decision):
    async def scenario():
        _, state, bot = make_runtime(tmp_path, lambda day: None)
        install_service(state)
        await state.service.run(MonitorPhase.DECISION, now=datetime(2026, 11, 10, 13, 55, tzinfo=TZ))
        state.service.core = SimpleNamespace(evaluate=lambda snapshot: _core(decision, day=DAY))
        before = state.store.latest_state()
        state.store.queue_reconciliation(DAY)
        await reconcile_historical(state.service, DAY)
        assert state.store.job_status(MonitorPhase.RECONCILIATION, DAY) == "REVIEW_REQUIRED"
        assert state.store.latest_state() == before
        assert len(bot.sent) == 2
        await reconcile_historical(state.service, DAY)
        assert len(bot.sent) == 2
    asyncio.run(scenario())


def test_historical_timeout_remains_retryable_without_restart(tmp_path):
    async def scenario():
        _, state, _ = make_runtime(tmp_path, lambda day: None)
        install_service(state)
        async def stalled(*args):
            await asyncio.Event().wait()
        original = state.service.data_provider.load
        state.service.data_provider.load = stalled
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reconcile_historical(state.service, DAY), timeout=0.05)
        assert state.store.job_status(MonitorPhase.RECONCILIATION, DAY) == "FAILED"
        assert DAY in state.store.reconciliation_retry_dates()
        state.service.data_provider.load = original
        await state.service.run(MonitorPhase.DECISION, now=datetime(2026, 11, 9, 13, 55, tzinfo=TZ))
        await reconcile_historical(state.service, DAY)
        assert state.store.job_status(MonitorPhase.RECONCILIATION, DAY) == "SUCCEEDED"
    asyncio.run(scenario())


def test_additive_migration_preserves_legacy_rows_and_shadow_fields(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = EtfMonitorStore(path)
    append_trade(store, legacy=True)
    with store.transaction() as connection:
        connection.execute("DROP TABLE execution_status")
        connection.execute("UPDATE schema_meta SET value='6'")
    with sqlite3.connect(path) as connection:
        prior = connection.execute("SELECT * FROM outbox").fetchall()
    updated = EtfMonitorStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM outbox").fetchall() == prior
        assert connection.execute("SELECT value FROM schema_meta").fetchone()[0] == "7"
    assert updated.latest_state() is None


def test_acceptance_requires_complete_real_day_and_historical_gap(tmp_path):
    from tools.etf_monitor_shadow_acceptance import inspect_day
    store = EtfMonitorStore(tmp_path / "acceptance.sqlite3")
    target = date(2026, 11, 12)
    for phase, hour, minute in (
        (MonitorPhase.PREVIEW, 12, 45), (MonitorPhase.DECISION, 13, 55),
        (MonitorPhase.RECONCILIATION, 22, 0),
    ):
        due = datetime(2026, 11, 12, hour, minute, tzinfo=TZ).isoformat()
        store.begin_job(phase, target)
        store.finish_job(phase, target, succeeded=True)
        with store.transaction() as connection:
            connection.execute("UPDATE job_runs SET started_at=?,finished_at=? WHERE run_key=?",
                               (due, due, f"{phase.value}:{target}"))
        store.append_event_with_outbox(event_id=phase.value, phase=phase, trading_date=target,
            event_type="HOLD" if phase is not MonitorPhase.RECONCILIATION else "RECONCILIATION_CONFIRMED",
            payload={"core": {"decision": "HOLD"}, "data": {"is_complete": True}}, target_chat_id="-5527055845", body="offline")
        store.claim_outbox(phase.value)
        store.mark_outbox_delivered(phase.value, str(hour))
    store.begin_job(MonitorPhase.RECONCILIATION, DAY)
    store.finish_job(MonitorPhase.RECONCILIATION, DAY, succeeded=True)
    before = inspect_day(target, database=store.path, now=datetime(2026, 11, 12, 21, tzinfo=TZ))
    assert before["status"] == "WAITING_SCHEDULE"
    after = inspect_day(target, database=store.path, now=datetime(2026, 11, 12, 22, 30, tzinfo=TZ))
    assert after["status"] == "PASS"
    with store.transaction() as connection:
        connection.execute("DELETE FROM job_runs WHERE run_key='reconciliation_2200:2026-11-09'")
    assert inspect_day(target, database=store.path, now=datetime(2026, 11, 12, 22, 30, tzinfo=TZ))["status"] == "REVIEW_REQUIRED"


def test_manual_timing_and_uncertainty_never_become_automatic_execution_approval(tmp_path):
    store = EtfMonitorStore(tmp_path / "db.sqlite3")
    append_trade(store)
    store.claim_outbox("trade")
    store.mark_outbox_uncertain("trade", "timeout")
    execution.refresh_execution_status(store, now=OPEN.replace(hour=8))
    assert assessment(store)["executable_status"] == "DELIVERY_UNCERTAIN"
    store.resolve_uncertain_delivery("trade", delivered=True, telegram_message_id="2")
    with store.transaction() as connection:
        connection.execute("UPDATE outbox SET delivered_at=?", (OPEN.replace(hour=8).isoformat(),))
    execution.refresh_execution_status(store, now=OPEN.replace(hour=8))
    assert assessment(store)["executable_status"] == "MANUAL_TIMING_REVIEW"


@pytest.mark.parametrize("passed", [True, False])
def test_finite_closeout_waits_persists_verdict_and_notifies_once(tmp_path, monkeypatch, passed):
    from stock_ai_bot.monitoring.etf_monitor import acceptance
    async def scenario():
        _, state, bot = make_runtime(tmp_path, lambda day: None)
        install_service(state)
        state.startup_status = "READY"
        state.config = replace(state.config, mode="shadow", enabled=True)
        request = tmp_path / "shadow-closeout.json"
        original = {"target_day": "2026-11-12", "status": "WAITING", "config_sha256": "fixed"}
        request.write_text(json.dumps(original), encoding="utf-8")
        calls = []
        def inspect(*args, **kwargs):
            calls.append(True)
            return {"status": "PASS" if passed else "REVIEW_REQUIRED",
                    "config_sha256": "fixed", "checks": {"real_day": passed}}
        monkeypatch.setattr(acceptance, "inspect_day", inspect)
        await acceptance.closeout_sweep(state, request_path=request, now=datetime(2026, 11, 12, 21, tzinfo=TZ))
        assert not calls and not bot.sent
        await acceptance.closeout_sweep(state, request_path=request, now=datetime(2026, 11, 12, 22, 30, tzinfo=TZ))
        assert len(bot.sent) == 1
        assert json.loads(request.read_text())["status"] == ("COMPLETED" if passed else "REVIEW_REQUIRED")
        # Even a crash before recording the receipt cannot create another verdict.
        request.write_text(json.dumps(original), encoding="utf-8")
        await acceptance.closeout_sweep(state, request_path=request, now=datetime(2026, 11, 12, 22, 35, tzinfo=TZ))
        assert len(bot.sent) == 1 and len(calls) == 1
        await acceptance.closeout_sweep(state, request_path=request, now=datetime(2026, 11, 13, 22, 35, tzinfo=TZ))
        assert len(bot.sent) == 1
    asyncio.run(scenario())
