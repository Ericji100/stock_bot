from __future__ import annotations

import asyncio
import ast
import json
import sqlite3
import threading
import warnings
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationBuilder
from telegram.request import BaseRequest
from telegram.warnings import PTBUserWarning

from stock_ai_bot.monitoring.etf_monitor import integration
from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.models import MonitorPhase
from stock_ai_bot.monitoring.etf_monitor.routing import FailClosedTelegramRouter
from stock_ai_bot.monitoring.etf_monitor.store import EtfMonitorStore


class OfflineRequest(BaseRequest):
    """Actual PTB lifecycle, no token, polling, network or Telegram messages."""

    def __init__(self):
        self.methods = []

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        api = url.rsplit("/", 1)[-1]
        self.methods.append(api)
        assert api == "getMe", "Unexpected Bot API call in offline lifecycle test"
        return 200, json.dumps({
            "ok": True,
            "result": {"id": 123, "is_bot": True, "first_name": "Offline", "username": "offline_bot"},
        }).encode()


def application():
    request = OfflineRequest()
    app = (
        ApplicationBuilder().token("123:OFFLINE_TEST_ONLY")
        .request(request).get_updates_request(OfflineRequest())
        .post_init(integration.initialize_etf_monitor)
        .post_stop(integration.shutdown_etf_monitor)
        .post_shutdown(integration.shutdown_etf_monitor).build()
    )
    return app, request


def runtime(app, *, recover=None, update=None, router=None, service=None):
    async def noop(**kwargs):
        return 0

    result = integration.EtfMonitorRuntime(
        config=replace(load_etf_monitor_config(), enabled=True),
        store=SimpleNamespace(recover_after_restart=recover or (lambda: None)),
        router=router or SimpleNamespace(verify_route=noop, drain=noop),
        service=service or SimpleNamespace(data_provider=SimpleNamespace(
            delta_updater=SimpleNamespace(ensure_through=update or (lambda day: None)))),
        locks={phase: asyncio.Lock() for phase in integration.JOB_NAMES},
    )
    app.bot_data[integration.RUNTIME_KEY] = result
    return result


def test_real_ptb_post_init_has_no_warning_and_original_jobs_still_run():
    async def scenario():
        app, request = application()
        state = runtime(app)
        original_job_ran = asyncio.Event()

        async def original_job(context):
            original_job_ran.set()

        app.job_queue.run_once(original_job, when=0, name="original-bot-job")
        await app.initialize()
        try:
            with warnings.catch_warnings(record=True) as observed:
                warnings.simplefilter("always", PTBUserWarning)
                assert app.running is False
                await app.post_init(app)
                await app.start()
                await asyncio.wait_for(state.startup_task, 2)
                await asyncio.wait_for(original_job_ran.wait(), 2)
                assert state.startup_status == "READY"
                assert not [w for w in observed if issubclass(w.category, PTBUserWarning)]
            await app.stop()
            await app.post_stop(app)
            assert state.startup_status == "STOPPED"
            assert not state.background_tasks
            assert integration.RUNTIME_KEY not in app.bot_data
            assert request.methods == ["getMe"]
        finally:
            if app.running:
                await app.stop()
            await app.post_stop(app)
            await app.shutdown()
            await app.post_shutdown(app)

    asyncio.run(scenario())


def test_real_registration_keeps_three_times_and_original_job(tmp_path, monkeypatch):
    async def scenario():
        app, _ = application()

        async def original_job(context):
            pass

        old = app.job_queue.run_once(original_job, when=86400, name="original")
        config = replace(load_etf_monitor_config(), enabled=True)
        monkeypatch.setattr(integration, "load_etf_monitor_config", lambda _: config)
        monkeypatch.setattr(integration, "get_workdata_path", lambda _: tmp_path / "monitor.sqlite3")
        state = integration.register_etf_monitor(app)
        assert state is not None
        assert len(state.jobs) == 3
        assert [(j.trigger.fields[5].expressions[0].first, j.trigger.fields[6].expressions[0].first)
                for j in state.jobs] == [(12, 45), (13, 55), (22, 0)]
        assert all(str(j.trigger.timezone) == "Asia/Taipei" for j in state.jobs)
        await integration.shutdown_etf_monitor(app)
        assert old in app.job_queue.jobs()
        assert all(j.removed for j in state.jobs)

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["recovery", "backfill", "route", "drain"])
def test_shutdown_cancels_startup_at_each_boundary_without_late_ready(stage):
    async def scenario():
        app, _ = application()
        entered = threading.Event()
        release = threading.Event()
        async_entered = asyncio.Event()
        calls = []

        def sync_step(label):
            calls.append(label)
            if stage == label:
                entered.set()
                release.wait(3)

        async def async_step(label):
            calls.append(label)
            if stage == label:
                async_entered.set()
                await asyncio.Event().wait()

        async def verify():
            await async_step("route")

        async def drain(**kwargs):
            await async_step("drain")

        state = runtime(app, recover=lambda: sync_step("recovery"),
                        update=lambda day: sync_step("backfill"),
                        router=SimpleNamespace(verify_route=verify, drain=drain))
        try:
            await integration.initialize_etf_monitor(app)
            if stage in {"recovery", "backfill"}:
                assert await asyncio.to_thread(entered.wait, 1)
            else:
                await asyncio.wait_for(async_entered.wait(), 1)
            await asyncio.wait_for(integration.shutdown_etf_monitor(app), 1)
            assert state.startup_task.cancelled()
            assert state.startup_status == "STOPPED"
            assert not state.background_tasks
            assert integration.RUNTIME_KEY not in app.bot_data
            count = len(calls)
            release.set()
            await asyncio.sleep(0.03)
            assert len(calls) == count
            await integration.initialize_etf_monitor(app)
            await integration.shutdown_etf_monitor(app)
            assert len(calls) == count
        finally:
            release.set()

    asyncio.run(scenario())


def test_post_shutdown_cleans_up_if_start_never_happened():
    async def scenario():
        app, request = application()
        entered = asyncio.Event()

        async def verify():
            entered.set()
            await asyncio.Event().wait()

        state = runtime(app, router=SimpleNamespace(verify_route=verify, drain=None))
        await app.initialize()
        await app.post_init(app)
        await asyncio.wait_for(entered.wait(), 1)
        assert not app.running
        await app.shutdown()
        await app.post_shutdown(app)
        assert state.startup_task.cancelled()
        assert state.startup_status == "STOPPED"
        assert not state.background_tasks
        assert request.methods == ["getMe"]

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", list(integration.JOB_NAMES))
def test_running_phase_is_cancelled_but_original_ptb_task_is_not(phase):
    async def scenario():
        app, _ = application()
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        original_finished = asyncio.Event()

        async def run(phase):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        state = runtime(app, service=SimpleNamespace(run=run))
        state.startup_status = "READY"
        await app.initialize()
        await app.start()
        context = SimpleNamespace(application=app, job=SimpleNamespace(
            data={"phase": phase.value}))
        await integration._scheduled_job(context)
        await asyncio.wait_for(entered.wait(), 1)

        async def old_task():
            await asyncio.sleep(0.02)
            original_finished.set()

        app.create_task(old_task(), name="original-task")
        # ETF work must not hold Application.stop open for its 20-minute timeout.
        await asyncio.wait_for(app.stop(), 1)
        assert original_finished.is_set()
        await app.post_stop(app)
        assert cancelled.is_set()
        assert not state.background_tasks
        await integration._scheduled_job(context)
        assert not state.background_tasks
        await app.shutdown()
        await app.post_shutdown(app)

    asyncio.run(scenario())


def test_shutdown_is_bounded_for_noncooperative_dependency(monkeypatch, capsys):
    async def scenario():
        app, _ = application()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def verify():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        state = runtime(app, router=SimpleNamespace(verify_route=verify, drain=lambda **_: None))
        monkeypatch.setattr(integration, "SHUTDOWN_TIMEOUT_SECONDS", 0.03)
        await integration.initialize_etf_monitor(app)
        await asyncio.wait_for(entered.wait(), 1)
        try:
            await asyncio.wait_for(integration.shutdown_etf_monitor(app), 0.5)
            assert state.startup_status == "STOPPED_WITH_PENDING_TASKS"
            assert state.shutting_down
            await state.dispatch(MonitorPhase.DECISION)
        finally:
            release.set()
            await asyncio.wait_for(state.startup_task, 1)
            assert state.startup_status != "READY"
            await integration.shutdown_etf_monitor(app)

    asyncio.run(scenario())
    assert "shutdown timed out" in capsys.readouterr().out


@pytest.mark.parametrize("cancel_at", ["get_chat", "send_message"])
def test_shutdown_delivery_boundary_is_durable_and_never_blindly_resends(tmp_path, cancel_at):
    async def scenario():
        store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
        store.append_event_with_outbox(event_id="one", phase=MonitorPhase.RECONCILIATION,
            trading_date=date(2026, 10, 23), event_type="HOLD", payload={"decision": "HOLD"},
            target_chat_id="-5527055845", body="offline test only")
        entered = asyncio.Event()
        calls = []

        class Bot:
            async def get_chat(self, **kwargs):
                calls.append(("get_chat", kwargs["chat_id"]))
                if cancel_at == "get_chat":
                    entered.set()
                    await asyncio.Event().wait()
                return SimpleNamespace(id=-5527055845, type="supergroup")

            async def send_message(self, **kwargs):
                calls.append(("send_message", kwargs["chat_id"]))
                entered.set()
                await asyncio.Event().wait()

        router = FailClosedTelegramRouter(config=load_etf_monitor_config(), store=store, bot=Bot())
        task = asyncio.create_task(router.deliver("one"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with sqlite3.connect(store.path) as connection:
            status, attempts = connection.execute("select status, attempts from outbox").fetchone()
        assert status == ("RETRY" if cancel_at == "get_chat" else "DELIVERY_UNCERTAIN")
        assert attempts == 1
        store.recover_after_restart()
        if cancel_at == "send_message":
            assert store.claim_outbox("one") is None
        assert all(target == "-5527055845" for _, target in calls)

    asyncio.run(scenario())


def test_main_uses_cleanup_hooks_and_keeps_one_application_and_polling():
    tree = ast.parse(Path("main.py").read_text(encoding="utf-8-sig"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    for hook in ("post_stop", "post_shutdown"):
        matched = [node for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr == hook]
        assert len(matched) == 1
        assert ast.unparse(matched[0].args[0]) == "shutdown_etf_monitor"
    assert sum(isinstance(c.func, ast.Name) and c.func.id == "ApplicationBuilder" for c in calls) == 1
    assert sum(isinstance(c.func, ast.Attribute) and c.func.attr == "run_polling" for c in calls) == 1


def test_disabled_hooks_do_not_touch_original_application_state():
    async def scenario():
        app, request = application()
        original = object()
        app.bot_data["original"] = original
        await integration.initialize_etf_monitor(app)
        await integration.shutdown_etf_monitor(app)
        assert app.bot_data == {"original": original}
        assert request.methods == []

    asyncio.run(scenario())


def test_shutdown_during_retry_prevents_another_attempt():
    async def scenario():
        app, _ = application()
        calls = []
        entered = asyncio.Event()

        async def run(phase):
            calls.append(phase)
            entered.set()
            return SimpleNamespace(status="DATA_UNAVAILABLE")

        state = runtime(app, service=SimpleNamespace(run=run))
        state.startup_status = "READY"
        context = SimpleNamespace(application=app, job=SimpleNamespace(
            data={"phase": MonitorPhase.RECONCILIATION.value}))
        await integration._scheduled_job(context)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0)
        await integration.shutdown_etf_monitor(app)
        assert calls == [MonitorPhase.RECONCILIATION]
        assert not state.background_tasks

    asyncio.run(scenario())


def test_unexpected_owned_task_exception_is_retrieved_and_fails_closed(capsys):
    async def scenario():
        app, _ = application()
        state = runtime(app)

        async def broken():
            raise RuntimeError("secret-token-must-not-be-logged")

        task = integration._create_runtime_task(app, state, broken(), name="offline-broken")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task.done()
        assert state.startup_status == "DISABLED"
        assert integration.RUNTIME_KEY not in app.bot_data
        assert not state.background_tasks

    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert "background task disabled ETF runtime: RuntimeError" in output
    assert "secret-token" not in output


def test_background_failure_keeps_sibling_reachable_for_shutdown():
    async def scenario():
        app, _ = application()
        state = runtime(app)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def sibling():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        async def broken():
            raise ValueError("private")

        task = integration._create_runtime_task(app, state, sibling(), name="sibling")
        await entered.wait()
        integration._create_runtime_task(app, state, broken(), name="broken")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert state.startup_status == "DISABLED"
        assert app.bot_data[integration.RUNTIME_KEY] is state
        release.set()
        await integration.shutdown_etf_monitor(app)
        assert task.done()
        assert state.startup_status == "STOPPED"
        assert not state.background_tasks
        assert integration.RUNTIME_KEY not in app.bot_data

    asyncio.run(scenario())


def test_etf_health_distinguishes_ready_from_bot_liveness(monkeypatch):
    from stock_ai_bot.monitoring import bot_runtime_health

    recorded = []
    monkeypatch.setattr(bot_runtime_health, "update_schedule_health", lambda **kw: recorded.append(kw))

    async def scenario():
        app, _ = application()
        state = runtime(app)
        await integration.initialize_etf_monitor(app)
        await state.startup_task
        await integration.shutdown_etf_monitor(app)

    asyncio.run(scenario())
    assert [r["etf_monitor"]["status"] for r in recorded] == ["PENDING", "READY", "STOPPED"]
    for row in recorded:
        assert row["etf_monitor"]["mode"] == "shadow"
        assert row["etf_monitor"]["schedule"] == {
            "preview": "12:45:00", "decision": "13:55:00", "reconciliation": "22:00:00"}
        assert "token" not in json.dumps(row)
