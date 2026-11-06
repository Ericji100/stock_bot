from __future__ import annotations

import asyncio
import sqlite3
import pytest
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from stock_ai_bot.monitoring.etf_monitor import integration, recovery
from stock_ai_bot.monitoring.etf_monitor.models import Decision, MonitorPhase
from stock_ai_bot.monitoring.etf_monitor.service import EtfMonitorService
from test_etf_monitor_service import _snapshot, _core
from test_etf_monitor_startup_recovery import make_runtime


DAY = date(2026, 11, 3)
PHASE = MonitorPhase.RECONCILIATION


@pytest.fixture(autouse=True)
def after_midnight(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 4, 0, 30, tzinfo=ZoneInfo("Asia/Taipei")).astimezone(tz)
    monkeypatch.setattr(recovery, "datetime", Clock)


def install_service(state, *, failures=0):
    calls = []

    class Data:
        async def load(self, phase, as_of):
            from stock_ai_bot.monitoring.etf_monitor.errors import EtfMonitorDataError
            calls.append(as_of)
            if len(calls) <= failures:
                raise EtfMonitorDataError("TWSE TWT48U receipt is not valid JSON")
            return _snapshot(complete=True, day=as_of)

    state.service = EtfMonitorService(
        config=state.config, store=state.store, router=state.router,
        data_provider=Data(), core=SimpleNamespace(evaluate=lambda snapshot: _core(Decision.HOLD, day=snapshot.as_of)),
        trading_day_predicate=lambda day: True,
        accounting=SimpleNamespace(validate=lambda snapshot: {}, reconcile=lambda snapshot, store: {}),
    )
    return calls


def mark_failed(store, day=DAY):
    store.begin_job(PHASE, day)
    store.finish_job(PHASE, day, succeeded=False, error="official API invalid JSON")


def test_more_than_four_failures_then_real_service_recovers_and_notifies_once(tmp_path):
    async def scenario():
        _, state, bot = make_runtime(tmp_path, lambda day: None)
        state.startup_status = "READY"
        calls = install_service(state, failures=8)
        mark_failed(state.store)
        for _ in range(10):
            await recovery.reconciliation_recovery_sweep(state)
        assert calls == [DAY] * 9
        assert state.store.job_status(PHASE, DAY) == "SUCCEEDED"
        assert len(bot.sent) == 2  # one failure alert + one complete confirmation
        assert "資料恢復後補發" in bot.sent[-1]["text"]
        with sqlite3.connect(state.store.path) as db:
            assert db.execute("select count(*) from data_failures").fetchone()[0] == 8
            assert db.execute("select count(*) from outbox where status='DELIVERED'").fetchone()[0] == 2
        assert state.reconciliation_recovery["status"] == "IDLE"
    asyncio.run(scenario())


def test_restart_and_midnight_preserve_original_date(tmp_path):
    async def scenario():
        _, old, _ = make_runtime(tmp_path, lambda day: None)
        old.store.begin_job(PHASE, DAY)  # crashed before finish
        _, state, bot = make_runtime(tmp_path, lambda day: None)
        state.store.recover_after_restart()
        state.startup_status = "READY"
        calls = install_service(state)
        await recovery.reconciliation_recovery_sweep(state)
        assert calls == [DAY]
        assert state.store.latest_state()["as_of"] == DAY.isoformat()
        assert state.store.job_status(PHASE, DAY) == "SUCCEEDED"
        assert len(bot.sent) == 1
        assert DAY.isoformat() in bot.sent[0]["text"]
    asyncio.run(scenario())


def test_pending_dates_are_not_lost_when_phase_lock_is_busy(tmp_path):
    async def scenario():
        _, state, _ = make_runtime(tmp_path, lambda day: None)
        state.startup_status = "READY"
        lock = state.locks[PHASE]
        await lock.acquire()
        try:
            await state.dispatch(PHASE, trading_date=DAY, single_attempt=True)
        finally:
            lock.release()
        assert state.store.job_status(PHASE, DAY) == "RETRY_PENDING"
        install_service(state)
        await recovery.reconciliation_recovery_sweep(state)
        assert state.store.job_status(PHASE, DAY) == "SUCCEEDED"
    asyncio.run(scenario())


def test_recovery_loop_survives_exception_and_shutdown_cancels_wait(tmp_path, monkeypatch):
    async def scenario():
        app, state, _ = make_runtime(tmp_path, lambda day: None)
        state.startup_status = "READY"
        failures = 0
        entered = asyncio.Event()
        async def sweep(runtime):
            nonlocal failures
            failures += 1
            if failures == 1:
                raise OSError("temporary SQLite read failure")
            entered.set()
        monkeypatch.setattr(recovery, "reconciliation_recovery_sweep", sweep)
        monkeypatch.setattr(recovery, "RECOVERY_INTERVAL_SECONDS", 0.01)
        task = integration._create_runtime_task(app, state, recovery.reconciliation_recovery_loop(app, state), name="recovery")
        await asyncio.wait_for(entered.wait(), 2)
        assert failures >= 2
        await integration.shutdown_etf_monitor(app)
        assert task.cancelled()
        count = failures
        await asyncio.sleep(0.03)
        assert failures == count
    asyncio.run(scenario())


def test_newer_formal_state_is_deferred_until_previous_eod_recovers(tmp_path):
    async def scenario():
        _, state, bot = make_runtime(tmp_path, lambda day: None)
        calls = install_service(state)
        mark_failed(state.store)
        tomorrow = datetime(2026, 11, 4, 13, 55, tzinfo=ZoneInfo("Asia/Taipei"))
        result = await state.service.run(MonitorPhase.DECISION, now=tomorrow)
        assert result.status == "DATA_UNAVAILABLE"
        assert result.metadata["data_error"]["reason_code"] == "PRIOR_RECONCILIATION_PENDING"
        assert calls == []
        assert state.store.latest_state() is None
        state.startup_status = "READY"
        await recovery.reconciliation_recovery_sweep(state)
        assert state.store.latest_state()["as_of"] == DAY.isoformat()
    asyncio.run(scenario())


def test_source_recovery_never_blindly_resends_uncertain_telegram(tmp_path):
    async def scenario():
        _, state, bot = make_runtime(tmp_path, lambda day: None)
        state.startup_status = "READY"
        install_service(state)
        mark_failed(state.store)
        async def unknown(**kw):
            bot.sent.append(kw)
            raise TimeoutError("Telegram response unknown")
        bot.send_message = unknown
        await recovery.reconciliation_recovery_sweep(state)
        count = len(bot.sent)
        await recovery.reconciliation_recovery_sweep(state)
        assert len(bot.sent) == count
        with sqlite3.connect(state.store.path) as db:
            assert db.execute("select count(*) from outbox where status='DELIVERY_UNCERTAIN'").fetchone()[0] >= 1
    asyncio.run(scenario())


def test_timed_out_eod_is_not_stuck_running_until_restart(tmp_path):
    from dataclasses import replace
    async def scenario():
        _, state, _ = make_runtime(tmp_path, lambda day: None)
        state.startup_status = "READY"
        install_service(state)
        state.config = replace(state.config, job_timeouts_seconds={
            **state.config.job_timeouts_seconds, "reconciliation": 0.02})
        original = state.service.data_provider.load
        async def hung(*args):
            await asyncio.Event().wait()
        state.service.data_provider.load = hung
        mark_failed(state.store)
        await recovery.reconciliation_recovery_sweep(state)
        assert state.store.job_status(PHASE, DAY) == "FAILED"
        assert state.store.reconciliation_retry_dates() == (DAY,)
        state.config = replace(state.config, job_timeouts_seconds={
            **state.config.job_timeouts_seconds, "reconciliation": 3})
        state.service.data_provider.load = original
        await recovery.reconciliation_recovery_sweep(state)
        assert state.store.job_status(PHASE, DAY) == "SUCCEEDED"
    asyncio.run(scenario())


def decision_clock(monkeypatch, hour, minute):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 4, hour, minute, tzinfo=ZoneInfo("Asia/Taipei")).astimezone(tz)

    monkeypatch.setattr(recovery, "datetime", Clock)
    monkeypatch.setattr(integration, "datetime", Clock)


def test_decision_retries_after_short_batch_and_only_delivers_one_recovery(tmp_path, monkeypatch):
    decision_clock(monkeypatch, 14, 20)
    today = date(2026, 11, 4)
    phase = MonitorPhase.DECISION

    async def scenario():
        _, state, bot = make_runtime(tmp_path, lambda day: None)
        state.startup_status = "READY"
        calls = install_service(state, failures=5)
        for day in (DAY, today):
            state.store.begin_job(phase, day)
            state.store.finish_job(phase, day, succeeded=False, error="MIS_STALE_TRADE")
        for _ in range(8):
            await recovery.decision_recovery_sweep(state)
        assert calls == [today] * 6
        assert state.store.job_status(phase, DAY) == "FAILED"
        assert state.store.job_status(phase, today) == "SUCCEEDED"
        assert len(bot.sent) == 2
        assert "行情恢復後補發" in bot.sent[-1]["text"]
        assert "原定 2026-11-04 13:55" in bot.sent[-1]["text"]

    asyncio.run(scenario())


@pytest.mark.parametrize("hour,minute,early_eod", [(13, 54, False), (21, 55, False), (14, 10, True)])
def test_decision_recovery_respects_schedule_and_reconciliation(tmp_path, monkeypatch, hour, minute, early_eod):
    decision_clock(monkeypatch, hour, minute)
    today = date(2026, 11, 4)

    async def scenario():
        _, state, bot = make_runtime(tmp_path, lambda day: None)
        state.startup_status = "READY"
        calls = install_service(state)
        state.store.begin_job(MonitorPhase.DECISION, today)
        state.store.finish_job(MonitorPhase.DECISION, today, succeeded=False)
        if early_eod:
            state.store.queue_reconciliation(today)
        await recovery.decision_recovery_sweep(state)
        assert calls == []
        assert bot.sent == []

    asyncio.run(scenario())
