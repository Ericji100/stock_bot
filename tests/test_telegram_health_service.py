from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from telegram.error import NetworkError
from telegram.ext import ApplicationBuilder
from telegram.request import HTTPXRequest

from stock_ai_bot.telegram.telegram_health_service import (
    HEALTH_SERVICE_KEY, MonitoredHTTPXRequest, TelegramHealthService, handle_telegram_error,
)
from stock_ai_bot.telegram.telegram_push_service import sanitize_telegram_error


TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE"


@pytest.fixture
def health(tmp_path):
    time = [datetime(2026, 11, 6, 9, 0, tzinfo=timezone(timedelta(hours=8)))]
    output = []
    service = TelegramHealthService(tmp_path / "telegram", sensitive_values=(TOKEN, "99887766"),
                                    clock=lambda: time[0], emit=output.append)
    service.test_clock = time
    service.test_output = output
    return service


def events(service):
    return [json.loads(line) for path in (service.root / "diagnostics").glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


def network_error():
    try:
        raise httpx.ReadError(f"https://api.telegram.org/bot{TOKEN}/getUpdates")
    except httpx.ReadError as source:
        try:
            raise NetworkError("httpx.ReadError") from source
        except NetworkError as exc:
            return exc


def test_repeats_keep_every_chained_trace_but_dedupe_console_and_notices(health):
    for _ in range(4):
        health.failed("getUpdates", network_error())
    assert len(health.test_output) == 1
    assert len(health.state["notices"]) == 1
    assert health.snapshot()["channels"]["polling"]["failures"] == 4
    logs = events(health)
    assert len(logs) == 4
    assert all("httpx.ReadError" in log["traceback"] and "direct cause" in log["traceback"] for log in logs)
    assert TOKEN not in json.dumps(logs)
    health.test_clock[0] += timedelta(minutes=5)
    health.failed("getUpdates", network_error())
    assert len(health.test_output) == 2
    assert len(events(health)) == 5


def test_get_me_does_not_clear_polling_and_real_poll_success_recovers_once(health):
    health.failed("getUpdates", network_error())
    health.succeeded("getMe")
    assert health.snapshot()["channels"]["polling"]["status"] == "degraded"
    health.succeeded("getUpdates")
    health.succeeded("getUpdates")
    assert health.snapshot()["channels"]["polling"]["status"] == "healthy"
    assert len([e for e in events(health) if e["event"] == "transport_recovered"]) == 1
    assert [n["delivery"] for n in health.state["notices"]] == ["merged_into_recovery", "pending"]


def test_new_incident_after_recovery_has_its_own_notice(health):
    health.failed("getUpdates", network_error())
    before = health.snapshot()["channels"]["polling"]["incident_id"]
    health.succeeded("getUpdates")
    health.failed("getUpdates", network_error())
    assert health.snapshot()["channels"]["polling"]["incident_id"] != before
    assert len(health.state["notices"]) == 3


def test_independent_api_and_polling_health(health):
    health.failed("getUpdates", network_error())
    health.failed("sendMessage", network_error())
    health.succeeded("getUpdates")
    assert health.snapshot()["channels"]["api"]["status"] == "degraded"


def test_notification_sent_once_and_no_retry_or_self_generated_incident(health):
    health.failed("getUpdates", network_error())
    health.succeeded("getUpdates")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=23)))
    asyncio.run(health.flush_notices(bot, 99887766))
    asyncio.run(health.flush_notices(bot, 99887766))
    assert bot.send_message.await_count == 1
    text = bot.send_message.call_args.kwargs["text"]
    assert "已恢復" in text and "異常1次" in text and "不會因此重跑" in text
    assert health.snapshot()["notice_counts"]["sent"] == 1


def test_outage_notice_then_recovery_notice(health):
    health.failed("getUpdates", network_error())
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=23)))
    asyncio.run(health.flush_notices(bot, 1))
    health.succeeded("getUpdates")
    asyncio.run(health.flush_notices(bot, 1))
    assert bot.send_message.await_count == 2
    assert "已恢復" not in bot.send_message.call_args_list[0].kwargs["text"]
    assert "已恢復" in bot.send_message.call_args_list[1].kwargs["text"]


@pytest.mark.parametrize("error", [NetworkError("read failed"), asyncio.CancelledError()])
def test_unknown_notification_is_preserved_and_never_retried(health, error):
    health.failed("getUpdates", network_error())
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=error))
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(health.flush_notices(bot, 1))
    else:
        asyncio.run(health.flush_notices(bot, 1))
    asyncio.run(health.flush_notices(bot, 1))
    assert bot.send_message.await_count == 1
    assert health.snapshot()["notice_counts"]["unknown"] == 1
    assert health.snapshot()["channels"]["polling"]["failures"] == 1


def test_restart_does_not_claim_healthy_or_retry_uncertain_notice(health):
    health.failed("getUpdates", network_error())
    health.succeeded("getUpdates")
    health.state["notices"][-1]["delivery"] = "sending"
    health.persist()
    resumed = TelegramHealthService(health.root)
    assert resumed.snapshot()["channels"]["polling"]["status"] == "unverified"
    assert resumed.snapshot()["notice_counts"]["unknown"] == 1


def test_restart_does_not_send_stale_recovery_until_polling_really_succeeds(health):
    health.failed("getUpdates", network_error())
    health.succeeded("getUpdates")
    resumed = TelegramHealthService(health.root, emit=lambda _: None)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    asyncio.run(resumed.flush_notices(bot, 1))
    bot.send_message.assert_not_awaited()
    resumed.succeeded("getUpdates")
    asyncio.run(resumed.flush_notices(bot, 1))
    assert bot.send_message.await_count == 1


def test_new_outage_supersedes_unsent_old_recovery(health):
    health.failed("getUpdates", network_error())
    health.succeeded("getUpdates")
    health.failed("getUpdates", network_error())
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    asyncio.run(health.flush_notices(bot, 1))
    assert bot.send_message.await_count == 1
    assert "已恢復" not in bot.send_message.call_args.kwargs["text"]


def test_no_notification_without_durable_sending_marker(health):
    health.failed("getUpdates", network_error())
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch.object(health, "persist", return_value=False):
        asyncio.run(health.flush_notices(bot, 1))
    bot.send_message.assert_not_awaited()
    assert health.snapshot()["notice_counts"]["pending"] == 1


def test_restart_preserves_pending_incident_until_actual_success(health):
    health.failed("getUpdates", network_error())
    resumed = TelegramHealthService(health.root)
    assert resumed.snapshot()["channels"]["polling"]["status"] == "degraded"
    resumed.succeeded("getUpdates")
    assert resumed.snapshot()["channels"]["polling"]["status"] == "healthy"


@pytest.mark.parametrize("bad", ['{', '{"schema_version":99}', '{"schema_version":1,"channels":{},"notices":[]}'])
def test_invalid_state_is_preserved_and_diagnosed(tmp_path, bad):
    (tmp_path / "state.json").write_text(bad, encoding="utf-8")
    service = TelegramHealthService(tmp_path, emit=lambda _: None)
    service.failed("getUpdates", network_error())
    assert (tmp_path / "state.json").read_text(encoding="utf-8") == bad
    assert events(service)[0]["event"] == "state_load_failed"
    assert service.snapshot()["diagnostic_storage_warning"] is True


def test_unexpected_handler_errors_keep_full_diagnostics_and_no_false_recovery(health):
    error = ValueError("unexpected " + "x" * 1200)
    health.application_error(error, update_id=10)
    health.application_error(error, update_id=11)
    assert len(health.test_output) == 1
    assert len(events(health)) == 2
    assert len(events(health)[0]["traceback"]) > 1200
    assert health.state["notices"] == []
    assert health.snapshot()["channels"]["polling"]["status"] == "unverified"


def test_sanitizer_default_limit_and_full_diagnostic_mode():
    raw = f"{TOKEN} chat=99887766 Bearer test-secret sk-testsecret ?api_key=another-secret " + "x" * 1300
    assert len(sanitize_telegram_error(raw)) == 500
    clean = sanitize_telegram_error(raw, "99887766", max_length=None)
    assert len(clean) > 1200
    assert all(value not in clean for value in (TOKEN, "99887766", "test-secret", "sk-testsecret", "another-secret"))


def test_transport_result_and_native_error_are_unchanged_and_success_is_real(health):
    async def run():
        request = MonitoredHTTPXRequest(health=health, connection_pool_size=1)
        error = network_error()
        good = (200, b'{"ok":true,"result":[]}')
        try:
            with patch.object(HTTPXRequest, "do_request", new=AsyncMock(side_effect=[error, good])) as original:
                with pytest.raises(NetworkError) as raised:
                    await request.do_request(f"https://api.telegram.org/bot{TOKEN}/getUpdates", "POST")
                assert raised.value is error
                result = await request.do_request(f"https://api.telegram.org/bot{TOKEN}/getUpdates", "POST")
                assert result is good
                assert original.await_count == 2
        finally:
            await request.shutdown()
    asyncio.run(run())
    assert health.snapshot()["channels"]["polling"]["status"] == "healthy"


@pytest.mark.parametrize("response", [(400, b'{"ok":false}'), (429, b'{"ok":false}'),
                                      (200, b'not json'), (200, b'{"ok":false}'),
                                      (200, b'{"ok":true}')])
def test_bad_or_invalid_api_response_does_not_mark_recovery(health, response):
    async def run():
        request = MonitoredHTTPXRequest(health=health)
        try:
            with patch.object(HTTPXRequest, "do_request", new=AsyncMock(return_value=response)):
                assert await request.do_request("https://example/getUpdates", "POST") is response
        finally:
            await request.shutdown()
    health.failed("getUpdates", network_error())
    asyncio.run(run())
    assert health.snapshot()["channels"]["polling"]["status"] == "degraded"


def test_server_error_is_recorded_but_returned_unchanged(health):
    async def run():
        request = MonitoredHTTPXRequest(health=health)
        try:
            response = (503, b'{"ok":false}')
            with patch.object(HTTPXRequest, "do_request", new=AsyncMock(return_value=response)):
                assert await request.do_request("https://example/getUpdates", "POST") is response
        finally:
            await request.shutdown()
    asyncio.run(run())
    assert health.snapshot()["channels"]["polling"]["status"] == "degraded"


def test_notice_transport_does_not_recursively_generate_alerts(health):
    async def run():
        request = MonitoredHTTPXRequest(health=health)
        async def send(**kwargs):
            await request.do_request("https://example/sendMessage", "POST")
        health.failed("getUpdates", network_error())
        try:
            with patch.object(HTTPXRequest, "do_request", new=AsyncMock(side_effect=network_error())):
                await health.flush_notices(SimpleNamespace(send_message=send), 1)
        finally:
            await request.shutdown()
    asyncio.run(run())
    assert len(health.state["notices"]) == 1
    assert health.snapshot()["channels"]["api"]["status"] == "unverified"


def test_storage_failure_never_changes_request_result(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    service = TelegramHealthService(blocker, emit=lambda _: None)
    async def run():
        request = MonitoredHTTPXRequest(health=service)
        try:
            with patch.object(HTTPXRequest, "do_request", new=AsyncMock(side_effect=network_error())):
                with pytest.raises(NetworkError):
                    await request.do_request("https://example/getUpdates", "POST")
            response = (200, b'{"ok":true,"result":[]}')
            with patch.object(HTTPXRequest, "do_request", new=AsyncMock(return_value=response)):
                assert await request.do_request("https://example/getUpdates", "POST") is response
        finally:
            await request.shutdown()
    asyncio.run(run())
    assert service.snapshot()["diagnostic_storage_warning"] is True


def test_real_ptb_error_handler_consumes_recorded_error_without_duplicate_diagnostic(health):
    async def run():
        app = ApplicationBuilder().token(TOKEN).build()
        app.bot_data[HEALTH_SERVICE_KEY] = health
        app.add_error_handler(handle_telegram_error)
        error = network_error()
        health.failed("getUpdates", error)
        try:
            await app.process_error(update=None, error=error)
            await app.process_error(update=SimpleNamespace(update_id=42), error=ValueError("unexpected"))
        finally:
            for request in app.bot._request:
                await request.shutdown()
    asyncio.run(run())
    assert len([e for e in events(health) if e["event"] == "transport_error"]) == 1
    assert events(health)[-1]["update_id"] == 42


@pytest.mark.parametrize("missing", [True, False])
def test_error_handler_fallback_keeps_original_trace_if_service_missing_or_broken(health, caplog, missing):
    error = network_error()
    context = SimpleNamespace(error=error, application=SimpleNamespace(bot_data={} if missing else {HEALTH_SERVICE_KEY: health}))
    with patch.object(health, "application_error", side_effect=OSError("storage failure")):
        asyncio.run(handle_telegram_error(None, context))
    assert "direct cause" in caplog.text and "httpx.ReadError" in caplog.text
    assert TOKEN not in caplog.text


def test_real_httpx_transport_records_failure_then_valid_empty_poll(health):
    async def run():
        count = 0
        async def respond(request):
            nonlocal count
            count += 1
            if count == 1:
                raise httpx.ReadError("connection reset", request=request)
            return httpx.Response(200, json={"ok": True, "result": []})
        request = MonitoredHTTPXRequest(health=health, connection_pool_size=1,
                                       httpx_kwargs={"transport": httpx.MockTransport(respond)})
        try:
            with pytest.raises(NetworkError):
                await request.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates")
            assert await request.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates") == []
        finally:
            await request.shutdown()
        assert count == 2
    asyncio.run(run())
    logs = events(health)
    assert "httpx.ReadError" in logs[0]["traceback"]
    assert health.snapshot()["channels"]["polling"]["status"] == "healthy"


def test_main_wires_both_requests_and_global_handler_without_changing_timeouts(monkeypatch, tmp_path):
    import main
    from telegram.ext import Application
    captured = []
    async def unused(*args, **kwargs):
        raise AssertionError("No business handlers may run during registration")
    monkeypatch.setattr(main, "load_config", lambda: {"api_token": TOKEN, "chat_id": 99887766})
    monkeypatch.setattr(main, "get_workdata_path", lambda *args: tmp_path / "health")
    monkeypatch.setattr(main, "register_etf_monitor", lambda app: None)
    monkeypatch.setattr(main, "build_research_handlers", lambda *args: defaultdict(lambda: unused))
    monkeypatch.setattr(Application, "run_polling", lambda self, **kwargs: captured.append((self, kwargs)))
    main.main()
    app, polling_args = captured[0]
    assert polling_args == {"bootstrap_retries": -1}
    assert handle_telegram_error in app.error_handlers
    assert all(isinstance(r, MonitoredHTTPXRequest) for r in app.bot._request)
    assert app.bot._request[0].health is app.bot._request[1].health is app.bot_data[HEALTH_SERVICE_KEY]
    assert app.bot._request[0].read_timeout == 5
    assert app.bot._request[1].read_timeout == 60
    assert app.bot._request[0]._client._transport._pool._max_connections == 1
    assert app.bot._request[1]._client._transport._pool._max_connections == 256
    async def close():
        for request in app.bot._request:
            await request.shutdown()
    asyncio.run(close())


def test_native_ptb_polling_retries_read_error_and_recovers_without_restart(health):
    async def run():
        recovered = asyncio.Event()
        polls = 0
        async def respond(request):
            nonlocal polls
            operation = request.url.path.rsplit("/", 1)[-1]
            if operation == "getMe":
                result = {"id": 1, "is_bot": True, "first_name": "Offline", "username": "offline_bot"}
            elif operation == "deleteWebhook":
                result = True
            elif operation == "getUpdates":
                polls += 1
                if polls == 1:
                    raise httpx.ReadError("simulated dropped polling connection", request=request)
                result = []
                recovered.set()
            else:
                raise AssertionError(f"Unexpected Telegram call: {operation}")
            return httpx.Response(200, json={"ok": True, "result": result})
        requests = [MonitoredHTTPXRequest(health=health, connection_pool_size=1,
                                          httpx_kwargs={"transport": httpx.MockTransport(respond)}) for _ in range(2)]
        app = ApplicationBuilder().token(TOKEN).get_updates_request(requests[0]).request(requests[1]).build()
        app.bot_data[HEALTH_SERVICE_KEY] = health
        app.add_error_handler(handle_telegram_error)
        await app.initialize()
        await app.start()
        try:
            await app.updater.start_polling(error_callback=lambda error: app.create_task(app.process_error(None, error)))
            await asyncio.wait_for(recovered.wait(), timeout=4)
        finally:
            if app.updater.running:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
        assert polls >= 2
    asyncio.run(run())
    assert health.snapshot()["channels"]["polling"]["status"] == "healthy"
    assert health.snapshot()["channels"]["polling"]["failures"] == 1
    assert len([e for e in events(health) if e["event"] == "transport_error"]) == 1
    assert len([e for e in events(health) if e["event"] == "transport_recovered"]) == 1


def test_heartbeat_is_written_before_notice_send_and_without_restarting_work(health):
    import main
    order = []
    health.failed("getUpdates", network_error())
    async def send(**kwargs):
        order.append("send")
        return SimpleNamespace(message_id=1)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={HEALTH_SERVICE_KEY: health}),
                              bot=SimpleNamespace(send_message=send))
    with patch.object(main, "update_schedule_health", side_effect=lambda **kwargs: order.append("heartbeat")) as write, \
            patch.object(main, "load_config", return_value={"chat_id": 1}), \
            patch.object(main, "_job_queue_available", return_value=True), \
            patch.object(main, "_current_scheduled_task_snapshot", return_value=None):
        asyncio.run(main.bot_heartbeat_tick(context))
    assert order == ["heartbeat", "send"]
    assert write.call_args.kwargs["telegram_transport"]["channels"]["polling"]["status"] == "degraded"
    assert write.call_args.kwargs["schedule_unhealthy_reason"] is None
